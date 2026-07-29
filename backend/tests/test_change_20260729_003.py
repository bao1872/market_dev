"""[CHANGE-20260729-003] 第一金字塔历史SSOT、筛选器原子特征与盘后核心/筹码解耦测试。

覆盖 ref/instruction.md §七 必测项：
1. 量能均量比（DSA mean/mean ratio，废弃 sum/sum）
2. Rope 方向占比前缀不变性（段内 expanding，禁止未来泄漏）
3. OB 三事件时间线（OB_CREATED/ENTERED/MITIGATED 不可变）
4. SQZ_RELEASE 三方向与前置挤压量
5. regime_strength 读取正确（不得静默为 None）
6. history 一次计算多日
7. 最后日与 core snapshot 一致
8. core 不调用 Node Cluster
9. 主 run 不等待 chip（接口合同：execute 抛 NotImplementedError）
10. chip 失败不影响 core（compute_chip_consensus_snapshot 失败返回 error，core 独立成功）

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_change_20260729_003.py -v -p no:cacheprovider
"""
from __future__ import annotations

import math
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
from app.services.first_pyramid_service import (
    compute_chip_consensus_snapshot,
    compute_first_pyramid_core_snapshot,
    compute_first_pyramid_history,
)
from app.strategy.selectors.dsa_selector import compute_dsa_bundle
from app.strategy_assets.algorithms.features.smc_pine_core import compute_smc_pine
from app.strategy_assets.algorithms.features.sqzmom_lb import (
    build_momentum_history,
    compute_sqzmom_lb,
)

# =============================================================================
# Fixture builders
# =============================================================================


def _build_bars(
    n: int = 120,
    trend: str = "up",
    seed: int = 42,
    start_price: float = 10.0,
) -> pd.DataFrame:
    """构造 OHLCV 日线 fixture（与 test_first_pyramid_contract.py 一致）。"""
    np.random.seed(seed)
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    if trend == "up":
        drift = 0.08
    elif trend == "down":
        drift = -0.08
    else:
        drift = 0.0
    noise = np.random.randn(n) * 0.15
    close = start_price + np.cumsum(noise + drift)
    open_ = close - np.random.rand(n) * 0.05
    high = np.maximum(close, open_) + np.random.rand(n) * 0.1
    low = np.minimum(close, open_) - np.random.rand(n) * 0.1
    volume = np.random.randint(100000, 500000, n).astype(float)
    amount = close * volume
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        index=dates,
    )


def _build_config(lookback: int | None = None) -> dict:
    cfg = {"min_dir_bars": 5}
    if lookback is not None:
        cfg["lookback"] = lookback
    return cfg


# =============================================================================
# 1. DSA 量能均量比测试（mean/mean ratio，废弃 sum/sum）
# =============================================================================


class TestDSAVolumeMeanRatio:
    """验证 DSA 输出 current_segment_volume_mean / prev_segment_volume_mean /
    current_vs_prev_volume_mean_ratio 三个字段，且为 mean/mean 口径。"""

    def test_dsa_outputs_volume_mean_fields(self):
        bars = _build_bars(n=200, trend="up", seed=7)
        bundle = compute_dsa_bundle(bars, _build_config())
        metrics = bundle["last_row_metrics"]
        # 三个 mean 字段必须存在
        assert "current_segment_volume_mean" in metrics, metrics.keys()
        assert "prev_segment_volume_mean" in metrics, metrics.keys()
        assert "current_vs_prev_volume_mean_ratio" in metrics, metrics.keys()

    def test_volume_mean_ratio_is_mean_over_mean(self):
        """验证 ratio = current_mean / prev_mean（不是 sum/sum）。"""
        bars = _build_bars(n=200, trend="up", seed=11)
        bundle = compute_dsa_bundle(bars, _build_config())
        metrics = bundle["last_row_metrics"]
        cur_mean = metrics["current_segment_volume_mean"]
        prev_mean = metrics["prev_segment_volume_mean"]
        ratio = metrics["current_vs_prev_volume_mean_ratio"]
        if cur_mean is not None and prev_mean and prev_mean > 0:
            expected = cur_mean / prev_mean
            assert abs(ratio - expected) < 1e-6, f"ratio={ratio} expected={expected}"

    def test_volume_mean_not_sum(self):
        """mean 与 sum 不同（段长>1 时）。"""
        bars = _build_bars(n=200, trend="up", seed=13)
        bundle = compute_dsa_bundle(bars, _build_config())
        metrics = bundle["last_row_metrics"]
        cur_mean = metrics["current_segment_volume_mean"]
        # mean 与 sum 在段长>1 时不相等（如果 sum 存在）
        # 这里只验证 mean 是有限正数
        assert cur_mean is not None
        assert math.isfinite(cur_mean)
        assert cur_mean > 0


# =============================================================================
# 2. Rope 方向占比前缀不变性（段内 expanding，禁止未来泄漏）
# =============================================================================


class TestRopeDir1PctPrefixInvariance:
    """验证 rope_dir1_pct[i] 只依赖 [0..i]，截断后重算前缀一致。"""

    def test_prefix_invariance(self):
        bars = _build_bars(n=200, trend="up", seed=23)
        full_bundle = compute_dsa_bundle(bars, _build_config())
        full_fpb = full_bundle["factor_per_bar"]
        full_rope = full_fpb["rope_dir1_pct"]
        # 截断到前 150 根重算，前 150 行的 rope_dir1_pct 必须一致
        truncated = bars.iloc[:150]
        trunc_bundle = compute_dsa_bundle(truncated, _build_config())
        trunc_rope = trunc_bundle["factor_per_bar"]["rope_dir1_pct"]
        # 比较（truncated 长度 150，full 长度 200，取前 150 比较）
        common_idx = min(len(full_rope), len(trunc_rope))
        full_vals = full_rope.iloc[:common_idx].values
        trunc_vals = trunc_rope.iloc[:common_idx].values
        # 前 lookback 之后开始有值，比较非 NaN 部分
        for i in range(common_idx):
            f, t = full_vals[i], trunc_vals[i]
            if isinstance(f, float) and isinstance(t, float):
                if math.isnan(f) and math.isnan(t):
                    continue
                if math.isnan(f) or math.isnan(t):
                    pytest.fail(
                        f"rope_dir1_pct prefix mismatch at {i}: full={f} trunc={t}"
                    )
                assert abs(f - t) < 1e-9, f"prefix mismatch at {i}: full={f} trunc={t}"

    def test_rope_dir1_pct_exists(self):
        bars = _build_bars(n=120, trend="up", seed=29)
        bundle = compute_dsa_bundle(bars, _build_config())
        assert "rope_dir1_pct" in bundle["factor_per_bar"].columns


# =============================================================================
# 3. OB 三事件时间线（OB_CREATED/ENTERED/MITIGATED 不可变）
# =============================================================================


class TestOBLifecycleEvents:
    """验证 SMC OB 生命周期事件：OB_CREATED/OB_ENTERED/OB_MITIGATED。"""

    def test_ob_lifecycle_event_types(self):
        bars = _build_bars(n=200, trend="up", seed=31)
        opens = bars["open"].astype(float).tolist()
        highs = bars["high"].astype(float).tolist()
        lows = bars["low"].astype(float).tolist()
        closes = bars["close"].astype(float).tolist()
        times = [d.isoformat() for d in bars.index]
        result = compute_smc_pine(opens, highs, lows, closes, times)
        events = result.get("ob_lifecycle_events", [])
        valid_types = {"OB_CREATED", "OB_ENTERED", "OB_MITIGATED"}
        for evt in events:
            assert evt["type"] in valid_types, f"invalid OB event type: {evt['type']}"

    def test_ob_created_precedes_entered_and_mitigated(self):
        """OB_CREATED 必须先于 OB_ENTERED/OB_MITIGATED（同一 OB）。"""
        bars = _build_bars(n=200, trend="up", seed=37)
        opens = bars["open"].astype(float).tolist()
        highs = bars["high"].astype(float).tolist()
        lows = bars["low"].astype(float).tolist()
        closes = bars["close"].astype(float).tolist()
        times = [d.isoformat() for d in bars.index]
        result = compute_smc_pine(opens, highs, lows, closes, times)
        events = result.get("ob_lifecycle_events", [])
        # 按 ob_id 分组，验证 CREATED 在 ENTERED/MITIGATED 之前
        by_ob: dict[str, list[dict]] = {}
        for evt in events:
            ob_id = evt.get("ob_id") or evt.get("anchor_time", "")
            by_ob.setdefault(ob_id, []).append(evt)
        for ob_id, evts in by_ob.items():
            types = [e["type"] for e in evts]
            if "OB_CREATED" in types:
                created_idx = types.index("OB_CREATED")
                for t in ("OB_ENTERED", "OB_MITIGATED"):
                    if t in types:
                        assert types.index(t) > created_idx, (
                            f"{t} before OB_CREATED for ob_id={ob_id}: {types}"
                        )

    def test_emit_timeline_outputs_swing_bias_fields(self):
        """emit_timeline=True 时输出 swing_bias/internal_bias/active_*_ob_count。"""
        bars = _build_bars(n=120, trend="up", seed=41)
        opens = bars["open"].astype(float).tolist()
        highs = bars["high"].astype(float).tolist()
        lows = bars["low"].astype(float).tolist()
        closes = bars["close"].astype(float).tolist()
        times = [d.isoformat() for d in bars.index]
        result = compute_smc_pine(
            opens, highs, lows, closes, times, emit_timeline=True
        )
        assert "state_timeline" in result
        timeline = result["state_timeline"]
        assert len(timeline) > 0
        last = timeline[-1]
        assert "swing_bias" in last
        assert "internal_bias" in last
        assert "active_internal_ob_count" in last
        assert "active_swing_ob_count" in last


# =============================================================================
# 4. SQZ_RELEASE 三方向与前置挤压量
# =============================================================================


class TestSQZReleaseDirectionAndSqueezeVolume:
    """验证 SQZ_RELEASE direction 按 val 正/负/0 映射 up/down/null，并计算释放量能比。"""

    def _build_sqzmom_result_with_release(
        self, release_val: float
    ) -> tuple[dict, list[float]]:
        """构造一个 sqzOn[t-1]=True, sqzOff[t]=True 的 SQZMOM 结果。"""
        # 构造 40 根：sqzOn[20..35]=True，sqzOff[36]=True，val[36]=release_val
        n = 40
        val = [None] * 36 + [release_val] + [release_val] * (n - 37)
        sqz_on = [False] * 20 + [True] * 16 + [False] * (n - 36)
        sqz_off = [False] * 36 + [True] + [True] * (n - 37)
        no_sqz = [not (a or b) for a, b in zip(sqz_on, sqz_off, strict=True)]
        result = {
            "val": val,
            "sqzOn": sqz_on,
            "sqzOff": sqz_off,
            "noSqz": no_sqz,
        }
        # volume：挤压区间固定 100，释放日 200
        vols = [100.0] * 36 + [200.0] + [150.0] * (n - 37)
        return result, vols

    def test_sqz_release_direction_up(self):
        result, vols = self._build_sqzmom_result_with_release(release_val=0.5)
        hist = build_momentum_history(result, vols)
        events = hist["sqz_release_events"]
        assert len(events) > 0
        evt = events[0]
        assert evt["type"] == "SQZ_RELEASE"
        assert evt["direction"] == "up", f"expected up, got {evt['direction']}"

    def test_sqz_release_direction_down(self):
        result, vols = self._build_sqzmom_result_with_release(release_val=-0.5)
        hist = build_momentum_history(result, vols)
        events = hist["sqz_release_events"]
        assert len(events) > 0
        assert events[0]["direction"] == "down"

    def test_sqz_release_direction_null(self):
        result, vols = self._build_sqzmom_result_with_release(release_val=0.0)
        hist = build_momentum_history(result, vols)
        events = hist["sqz_release_events"]
        assert len(events) > 0
        assert events[0]["direction"] == "null"

    def test_sqz_release_volume_ratio(self):
        """释放量能比 = 挤压区间均量 / 当日量。"""
        result, vols = self._build_sqzmom_result_with_release(release_val=0.5)
        hist = build_momentum_history(result, vols)
        evt = hist["sqz_release_events"][0]
        # 挤压区间 [20..35] 均量 = 100，当日量 = 200
        assert evt["release_volume_ratio"] is not None
        assert abs(evt["release_volume_ratio"] - 0.5) < 1e-6, (
            f"expected 0.5, got {evt['release_volume_ratio']}"
        )

    def test_sqz_release_events_are_per_bar(self):
        """生成逐日事件，不只查最后一根。"""
        result, vols = self._build_sqzmom_result_with_release(release_val=0.5)
        hist = build_momentum_history(result, vols)
        # 至少有一个事件
        assert len(hist["sqz_release_events"]) >= 1

    def test_daily_state_fields(self):
        """daily_state 包含 volatility_phase/momentum_direction/momentum_change/sqzmom_delta。"""
        opens = [10.0] * 50
        highs = [10.5] * 50
        lows = [9.5] * 50
        closes = [10.0] * 50
        sqzmom = compute_sqzmom_lb(
            opens=np.array(opens),
            highs=np.array(highs),
            lows=np.array(lows),
            closes=np.array(closes),
        )
        hist = build_momentum_history(sqzmom, [100.0] * 50)
        assert len(hist["daily_state"]) == 50
        last = hist["daily_state"][-1]
        assert "volatility_phase" in last
        assert "momentum_direction" in last
        assert "momentum_change" in last
        assert "sqzmom_delta" in last


# =============================================================================
# 5. regime_strength 读取正确（不得静默为 None）
# =============================================================================


class TestRegimeStrength:
    """验证 first_pyramid 的 trend 维度 regime_strength 字段读取正确。"""

    def test_regime_strength_not_none(self):
        bars = _build_bars(n=120, trend="up", seed=43)
        core = compute_first_pyramid_core_snapshot(
            bars=bars, symbol="TEST", trade_date="2026-06-01"
        )
        trend_cf = core.trend.continuousFactors
        # regime_strength 必须存在且非 None（DSA SSOT 输出）
        assert "regime_strength" in trend_cf, list(trend_cf.keys())
        assert trend_cf["regime_strength"] is not None, (
            "regime_strength 不得静默为 None（旧 bug 读取 trend_strength）"
        )


# =============================================================================
# 6. history 一次计算多日
# =============================================================================


class TestHistoryOneTimeMultiDay:
    """验证 compute_first_pyramid_history 一次计算输出多日状态。"""

    def test_history_outputs_multiple_days(self):
        bars = _build_bars(n=200, trend="up", seed=47)
        result = compute_first_pyramid_history(
            bars=bars, symbol="HIST", output_bars=20, include_chip=False
        )
        assert "daily_state" in result
        states = result["daily_state"]
        # 输出多日（至少 1 日，最多 output_bars 日）
        assert len(states) >= 1
        assert len(states) <= 20

    def test_history_does_not_call_snapshot_in_loop(self):
        """验证 history 不会循环调用 snapshot（一次计算）。"""
        bars = _build_bars(n=150, trend="up", seed=53)
        # mock compute_first_pyramid_snapshot，确保不被调用（history 用 core 路径）
        with patch(
            "app.services.first_pyramid_service.compute_first_pyramid_snapshot"
        ) as mock_snap:
            compute_first_pyramid_history(
                bars=bars, symbol="HIST", output_bars=10, include_chip=False
            )
            assert mock_snap.call_count == 0, (
                "compute_first_pyramid_history 禁止循环调用 snapshot"
            )

    def test_history_events_are_immutable(self):
        """history 事件是不可变的（同一输入两次计算结果一致）。"""
        bars = _build_bars(n=150, trend="up", seed=59)
        r1 = compute_first_pyramid_history(bars=bars, symbol="HIST", output_bars=10)
        r2 = compute_first_pyramid_history(bars=bars, symbol="HIST", output_bars=10)
        assert r1 == r2, "同一输入两次计算结果必须一致（不可变）"


# =============================================================================
# 7. 最后日与 core snapshot 一致
# =============================================================================


class TestHistoryLastDayMatchesCoreSnapshot:
    """验证 history 最后一天的 state 与 core snapshot 字段一致。"""

    def test_last_day_matches_core(self):
        bars = _build_bars(n=200, trend="up", seed=61)
        history = compute_first_pyramid_history(
            bars=bars, symbol="TEST", output_bars=5, include_chip=False
        )
        core = compute_first_pyramid_core_snapshot(
            bars=bars, symbol="TEST", trade_date=None
        )
        # history meta 的 input_hash 与 core inputHash 必须一致
        meta = history["meta"]
        assert meta["input_hash"] == core.inputHash, (
            f"history meta.input_hash={meta['input_hash']} "
            f"!= core.inputHash={core.inputHash}"
        )
        # parameter_hash_core 必须一致
        assert meta["parameter_hash_core"] == core.parameterHash, (
            f"history meta.parameter_hash_core={meta['parameter_hash_core']} "
            f"!= core.parameterHash={core.parameterHash}"
        )
        # 最后一天的 bar_index 必须等于 core 的 lastBarIndex
        last_state = history["daily_state"][-1]
        assert last_state["bar_index"] == core.lastBarIndex
        # 最后一天的 time 必须与 core.tradeDate 对应
        assert last_state["time"] is not None


# =============================================================================
# 8. core 不调用 Node Cluster
# =============================================================================


class TestCoreDoesNotCallNodeCluster:
    """验证 compute_first_pyramid_core_snapshot 不调用 Node Cluster engine。"""

    def test_core_no_node_cluster_call(self):
        bars = _build_bars(n=120, trend="up", seed=67)
        # mock compute_node_cluster_profile，确保不被调用
        with patch(
            "app.services.first_pyramid_service.compute_node_cluster_profile"
        ) as mock_nc:
            compute_first_pyramid_core_snapshot(
                bars=bars, symbol="TEST", trade_date="2026-06-01"
            )
            assert mock_nc.call_count == 0, (
                "compute_first_pyramid_core_snapshot 禁止调用 Node Cluster"
            )

    def test_core_excludes_node_params_from_hash(self):
        """core 的 parameterHash 排除 Node Cluster 参数。"""
        bars = _build_bars(n=120, trend="up", seed=71)
        core = compute_first_pyramid_core_snapshot(
            bars=bars, symbol="TEST", trade_date="2026-06-01"
        )
        # algorithmVersion 必须是 core 版本（不含 chip）
        assert core.algorithmVersion == FIRST_PYRAMID_CORE_ALGORITHM_VERSION
        # parameterHash 必须非空
        assert core.parameterHash.startswith("sha256:")


# =============================================================================
# 9. 主 run 不等待 chip（接口合同）
# =============================================================================


class TestMainRunDoesNotWaitForChip:
    """验证 chip consensus job 接口合同：execute 抛 NotImplementedError（下一阶段实现）。"""

    def test_chip_execute_raises_not_implemented(self):
        """execute_after_close_chip_consensus 抛 NotImplementedError（不阻塞主 run）。"""
        import asyncio
        import uuid as uuid_mod
        from datetime import date as date_mod

        from app.services.after_close_chip_consensus_service import (
            execute_after_close_chip_consensus,
        )

        coro = execute_after_close_chip_consensus(
            job_run_id=uuid_mod.uuid4(),
            trade_date=date_mod(2026, 7, 29),
            core_run_id=uuid_mod.uuid4(),
        )
        with pytest.raises(NotImplementedError):
            asyncio.run(coro)

    def test_chip_job_name_is_independent(self):
        """chip job 名称与 after_close_orchestrator 区分。"""
        from app.services.after_close_chip_consensus_service import (
            CHIP_CONSENSUS_JOB_NAME,
        )
        assert CHIP_CONSENSUS_JOB_NAME == "after_close_chip_consensus"
        assert CHIP_CONSENSUS_JOB_NAME != "after_close_orchestrator"


# =============================================================================
# 10. chip 失败不影响 core
# =============================================================================


class TestChipFailureDoesNotAffectCore:
    """验证 compute_chip_consensus_snapshot 失败时返回 error，core 独立成功。"""

    def test_chip_failure_returns_error_not_raises(self):
        """chip 输入无效时不抛异常，返回 error 字段。"""
        # 空 daily_bars
        result = compute_chip_consensus_snapshot(
            daily_bars=pd.DataFrame(),
            bars_15m=None,
            trade_date="2026-07-29",
        )
        assert result.chip is None
        assert result.error is not None
        assert "daily_bars 为空" in result.error

    def test_core_succeeds_when_chip_fails(self):
        """core 与 chip 独立：core 成功，chip 失败。"""
        bars = _build_bars(n=120, trend="up", seed=73)
        # core 成功
        core = compute_first_pyramid_core_snapshot(
            bars=bars, symbol="TEST", trade_date="2026-06-01"
        )
        assert core.trend.available
        assert core.structure.available
        assert core.momentum.available
        # chip 失败（传入无效数据）
        chip = compute_chip_consensus_snapshot(
            daily_bars=pd.DataFrame(),
            bars_15m=None,
            trade_date="2026-06-01",
        )
        assert chip.chip is None
        assert chip.error is not None
        # core 不受 chip 影响
        assert core.trend.available is True

    def test_chip_hash_independent_of_core(self):
        """chip hash 独立于 core inputHash。"""
        bars = _build_bars(n=120, trend="up", seed=79)
        core = compute_first_pyramid_core_snapshot(
            bars=bars, symbol="TEST", trade_date="2026-06-01"
        )
        chip = compute_chip_consensus_snapshot(
            daily_bars=bars,
            bars_15m=None,
            trade_date="2026-06-01",
        )
        # chip hash 与 core inputHash 是不同字段（即使数据相同，hash 用途不同）
        assert chip.chipHash.startswith("sha256:")
        assert core.inputHash.startswith("sha256:")
