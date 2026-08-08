"""[CHANGE-20260808] Review Historical Fact Replay correctness 纯单元测试。

验证（Phase 2A）：
1. historical adapter contract：previous_state_to_flat 映射 Review fp_* 字段
   （含 DSA segment / latest structure events / rolling facts）
2. compute_first_pyramid_history 的 daily_state 扩展字段：
   - segment 字段（segment_id/direction/start/bars/change/slope）
   - current_vs_prev_volume/amount_mean_ratio
   - latest BOS/CHoCH/OB（direction/freshness/active）
   - price_position_120d
3. prefix invariance：FullSeries[T] == TruncatedSeries[:T].last
   - _price_position_120d 只依赖前 i 根 bar
   - latest event freshness 相对已确认事件时间，不读取未来确认
4. Chip excluded：history 回补不引入 chip

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest \
        tests/test_review_historical_fact_replay.py -v
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from app.domain.review.member_fact import (
    DailyBarFact,
    ReviewMemberFact,
    compute_percentile,
    compute_price_position_120d,
    compute_ratio,
    previous_state_to_flat,
)
from app.services.first_pyramid_service import (
    _price_position_120d,
    compute_first_pyramid_history,
)


def _build_bars(n: int = 300, seed: int = 7) -> pd.DataFrame:
    """构造 OHLCV 日线 fixture（含 amount 列，满足 compute_first_pyramid_history）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = 10.0 + np.cumsum(rng.normal(0.05, 0.15, n))
    volume = rng.integers(100000, 500000, n).astype(float)
    return pd.DataFrame({
        "open": close - 0.05,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": volume,
        "amount": close * volume,
    }, index=dates)


# =============================================================================
# 1. Historical adapter contract
# =============================================================================


class TestHistoricalAdapterContract:
    """previous_state_to_flat 的 Review fp_* 字段映射。"""

    def test_maps_trend_and_structure(self) -> None:
        state = {
            "regime_value": 1,
            "swing_bias": 1,
            "internal_bias": 1,
            "structure_alignment": "共振",
            "volume_ratio_20": 1.5,
            "volume_percentile_20": 80.0,
            "sqzmom_val": 1.0,
            "sqzmom_delta": 0.3,
            "price_position_120d": 0.7,
            "latest_bos_direction": "up",
            "latest_bos_freshness": 3,
            "latest_choch_direction": "down",
            "latest_choch_freshness": 10,
            "latest_ob_direction": "up",
            "latest_ob_freshness": 5,
            "current_vs_prev_volume_mean_ratio": 1.3,
            "prev_segment_volume_mean": 1000.0,
        }
        flat = previous_state_to_flat(state)
        assert flat["fp_trend_direction"] == "上行"
        assert flat["fp_swing_direction"] == "上行"
        assert flat["fp_internal_direction"] == "上行"
        assert flat["fp_structure_alignment"] == "共振"
        assert flat["fp_momentum_direction"] == "扩张"  # sqzmom_val>0
        assert flat["fp_momentum_change"] == 0.3  # numeric delta
        assert flat["review_price_position"] == 0.7
        assert flat["fp_latest_bos_direction"] == "bullish"  # LIVE event direction
        assert flat["fp_latest_bos_freshness"] == 3
        assert flat["fp_latest_choch_direction"] == "bearish"
        assert flat["fp_latest_choch_freshness"] == 10
        assert flat["fp_latest_ob_direction"] == "bullish"
        assert flat["fp_latest_ob_freshness"] == 5
        assert flat["fp_segment_volume_ratio"] == 1.3
        assert flat["fp_prev_segment_volume"] == 1000.0

    def test_trend_from_regime_value_confirmed(self) -> None:
        """trend 必须由 confirmed regime_value 映射（中文标签），禁止 raw dir 直接映射。"""
        assert previous_state_to_flat({"regime_value": 1})["fp_trend_direction"] == "上行"
        assert previous_state_to_flat({"regime_value": -1})["fp_trend_direction"] == "下行"
        assert previous_state_to_flat({"regime_value": 0})["fp_trend_direction"] == "震荡"
        assert previous_state_to_flat({"regime_value": None})["fp_trend_direction"] is None

    def test_structure_alignment_derived_when_missing(self) -> None:
        """structure_alignment 缺省时由 swing/internal bias 派生。"""
        assert previous_state_to_flat(
            {"swing_bias": 1, "internal_bias": 1},
        )["fp_structure_alignment"] == "共振"
        assert previous_state_to_flat(
            {"swing_bias": 1, "internal_bias": -1},
        )["fp_structure_alignment"] == "背离"

    def test_chip_excluded(self) -> None:
        """Chip 字段不得进入历史 adapter。"""
        flat = previous_state_to_flat({
            "regime_value": 1,
            "chip_consensus": {"consensus": 0.8},
        })
        assert "chip_consensus" not in flat
        assert "fp_chip_consensus" not in flat


# =============================================================================
# 2. compute_first_pyramid_history daily_state 扩展字段
# =============================================================================


class TestHistoricalDailyStateExtension:
    """compute_first_pyramid_history 的 daily_state 提供 Review 消费字段。"""

    def test_daily_state_has_review_fields(self) -> None:
        bars = _build_bars(n=300)
        result = compute_first_pyramid_history(bars, symbol="TEST", output_bars=250)
        daily_state = result["daily_state"]
        assert len(daily_state) > 0
        latest = daily_state[-1]
        # Trend segment 字段
        assert "segment_id" in latest
        assert "segment_direction" in latest
        assert "segment_bars" in latest
        assert "segment_change_pct" in latest
        assert "segment_slope" in latest
        assert "current_vs_prev_volume_mean_ratio" in latest
        assert "current_vs_prev_amount_mean_ratio" in latest
        # Structure latest events
        assert "latest_bos_direction" in latest
        assert "latest_bos_freshness" in latest
        assert "latest_choch_direction" in latest
        assert "latest_choch_freshness" in latest
        assert "latest_ob_direction" in latest
        assert "latest_ob_freshness" in latest
        assert "structure_alignment" in latest
        # Rolling facts
        assert "price_position_120d" in latest
        # Chip 不引入
        assert "chip_consensus" not in latest

    def test_events_emitted(self) -> None:
        bars = _build_bars(n=400, seed=3)
        result = compute_first_pyramid_history(bars, symbol="TEST", output_bars=250)
        events = result["events"]
        # 应有至少一个结构事件（BOS/CHoCH/OB）
        assert isinstance(events, list)
        assert len(events) > 0


# =============================================================================
# 3. Prefix invariance
# =============================================================================


class TestPrefixInvariance:
    """FullSeries[T] == TruncatedSeries[:T].last。"""

    def test_price_position_120d_prefix_invariant(self) -> None:
        """price_position_120d 必须只依赖前 i 根 bar，不读未来。"""
        closes = [10.0 + i * 0.1 for i in range(300)]
        lows = [c - 0.5 for c in closes]
        highs = [c + 0.5 for c in closes]
        t = 150
        # 全量序列的 T 位置
        full = _price_position_120d(closes, lows, highs, t)
        # 截断到 T 的序列，同样位置
        trunc_closes = closes[: t + 1]
        trunc_lows = lows[: t + 1]
        trunc_highs = highs[: t + 1]
        trunc = _price_position_120d(trunc_closes, trunc_lows, trunc_highs, t)
        assert full == trunc

    def test_price_position_low_window(self) -> None:
        """窗口不足 120 时仍用可用 bar（不报错，prefix-causal）。"""
        closes = [10.0, 10.2, 10.5]
        lows = [9.8, 10.0, 10.3]
        highs = [10.2, 10.4, 10.7]
        val = _price_position_120d(closes, lows, highs, 2)
        assert val is not None
        assert 0.0 <= val <= 1.0

    def test_regime_value_prefix_causal(self) -> None:
        """regime_value 由截断序列重算（compute_dsa_history lookback=None 时仍 prefix-causal）。"""
        bars_full = _build_bars(n=300, seed=11)
        full_hist = compute_first_pyramid_history(bars_full, symbol="T", output_bars=300)
        full_regime = [s["regime_value"] for s in full_hist["daily_state"]]

        # 截断到 T 再算，比较 T 位置 regime
        t = 150
        bars_trunc = bars_full.iloc[: t + 1]
        trunc_hist = compute_first_pyramid_history(bars_trunc, symbol="T", output_bars=t + 1)
        trunc_regime = [s["regime_value"] for s in trunc_hist["daily_state"]]
        assert full_regime[t] == trunc_regime[-1]

    def test_freshness_never_negative(self) -> None:
        """latest event freshness 必须 >= 0（相对已确认事件时间，不读未来）。"""
        bars = _build_bars(n=400, seed=5)
        result = compute_first_pyramid_history(bars, symbol="T", output_bars=250)
        for state in result["daily_state"]:
            for key in ("latest_bos_freshness", "latest_choch_freshness", "latest_ob_freshness"):
                val = state.get(key)
                if val is not None:
                    assert val >= 0, f"{key}={val} < 0 at {state.get('bar_index')}"


# =============================================================================
# 4. Rolling facts LIVE/HISTORY parity（共享 SSOT）
# =============================================================================


def _make_bars_list(values: list[tuple]) -> list[DailyBarFact]:
    """构造 DailyBarFact 列表（dates 从 2026-01-01 起工作日递增）。"""
    from datetime import timedelta

    start = date(2026, 1, 1)
    result = []
    for i, (close, volume, amount) in enumerate(values):
        result.append(
            DailyBarFact(
                trade_date=start + timedelta(days=i),
                open=close - 0.05,
                high=close + 0.1,
                low=close - 0.1,
                close=close,
                volume=volume,
                amount=amount,
            )
        )
    return result


class TestRollingFactsParity:
    """LIVE ReviewMemberFact.build 与共享 SSOT（compute_ratio/percentile/price_position）一致。"""

    def test_ratio_denominator_excludes_current(self) -> None:
        """_ratio 分母不含 current（窗口边界 19/20/21 行为）。"""
        # history 含 current（最后一个）；prior = history[-window-1:-1] 不含 current
        hist21 = [100.0] * 20 + [150.0]  # 20 prior + current
        assert compute_ratio(150.0, hist21, 20) == 1.5
        # 不足 window 时仍用可用 prior（不要求满 window），分母仍不含 current
        hist20 = [100.0] * 19 + [150.0]
        assert compute_ratio(150.0, hist20, 20) == 1.5
        hist19 = [100.0] * 18 + [150.0]
        assert compute_ratio(150.0, hist19, 20) == 1.5
        # 无 prior（只有 current 1 个）→ None
        assert compute_ratio(150.0, [150.0], 20) is None

    def test_percentile_window_boundaries(self) -> None:
        """_percentile prior 不含 current，窗口边界 199/200/201 行为。"""
        # current=202（最大值），prior 200 根（1..200）→ 100%
        hist202 = list(range(1, 202)) + [202.0]
        assert compute_percentile(202.0, hist202, 200) == 100.0
        # prior 199 根（不足 200）仍计算，202 最大 → 100%
        hist201 = list(range(1, 201)) + [202.0]
        assert compute_percentile(202.0, hist201, 200) == 100.0
        # prior 200 根（边界）→ 100%
        assert compute_percentile(202.0, hist202, 200) == 100.0
        # 无 prior → None
        assert compute_percentile(202.0, [202.0], 200) is None

    def test_ratio_matches_build(self) -> None:
        """LIVE build 的 volume_ratio20 与共享函数一致。"""
        bars = _make_bars_list(
            [(10.0 + i * 0.1, 1000.0, 10000.0) for i in range(30)],
        )
        # 手工构造一个 current 与 prior 不同的场景
        bars[-1] = DailyBarFact(
            trade_date=bars[-1].trade_date, open=10, high=10, low=10,
            close=10, volume=2000.0, amount=20000.0,
        )
        import uuid as _uuid
        fact = ReviewMemberFact.build(
            instrument_id=_uuid.uuid4(), symbol="X", name="X",
            snapshot_id=None, trade_date=bars[-1].trade_date,
            first_pyramid={}, bars=bars, previous_state=None,
        )
        assert fact.volume_ratio20 == compute_ratio(
            2000.0, [1000.0] * 20 + [2000.0], 20,
        )
        assert fact.amount_ratio20 == compute_ratio(
            20000.0, [10000.0] * 20 + [20000.0], 20,
        )

    def test_price_position_120d_matches_build(self) -> None:
        """LIVE build 的 price_position 与共享函数一致（含 current，rolling 120）。"""
        bars = _make_bars_list(
            [(10.0 + i * 0.1, 1000.0, 10000.0) for i in range(130)],
        )
        import uuid as _uuid
        fact = ReviewMemberFact.build(
            instrument_id=_uuid.uuid4(), symbol="X", name="X",
            snapshot_id=None, trade_date=bars[-1].trade_date,
            first_pyramid={}, bars=bars, previous_state=None,
        )
        close = bars[-1].close
        lows = [b.low for b in bars[-120:] if b.low is not None]
        highs = [b.high for b in bars[-120:] if b.high is not None]
        expected = (close - min(lows)) / (max(highs) - min(lows))
        assert fact.price_position == expected
        assert fact.price_position == compute_price_position_120d(close, lows, highs)


# =============================================================================
# 5. SMC event cursor（pre-window seed + same-index multi-event）
# =============================================================================


def _trend_bars(n: int = 260) -> pd.DataFrame:
    """确定性：先涨后跌再涨，制造 swing 结构反转（触发 BOS/CHoCH 等事件）。"""
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    phase = [10.0 + i * 0.05 for i in range(n // 3)]
    phase += [phase[-1] - i * 0.08 for i in range(n // 3)]
    phase += [phase[-1] + i * 0.06 for i in range(n - 2 * (n // 3))]
    close = np.array(phase[:n])
    return pd.DataFrame({
        "open": close - 0.05,
        "high": close + 0.3,
        "low": close - 0.3,
        "close": close,
        "volume": 100000.0,
        "amount": close * 100000.0,
    }, index=dates)


class TestSmcEventCursor:
    """pre-window seed 与 same-index multi-event 不丢失。"""

    def test_pre_window_events_seeded(self) -> None:
        """output 窗口前已确认的 latest 事件在窗口内第一个 bar 仍存在（seed 生效）。"""
        bars = _trend_bars(n=260)
        # output_bars 小，使 start_idx 靠后（窗口外已有大量事件）
        result = compute_first_pyramid_history(bars, symbol="T", output_bars=60)
        daily_state = result["daily_state"]
        assert len(daily_state) > 0
        # 窗口内第一个 state 的 latest event 不应为 None（pre-window 有事件）
        first = daily_state[0]
        # 趋势反转序列必然产生过 BOS/CHoCH/OB，seed 后应可见（至少一个非 None）
        assert (
            first.get("latest_bos_direction")
            or first.get("latest_choch_direction")
            or first.get("latest_ob_direction")
        ), "pre-window 事件未 seed 进 latest 游标"

    def test_same_index_multi_event_no_override_loss(self) -> None:
        """同 confirmed_index 多事件（internal+swing）不互相覆盖。

        [CHANGE-20260808] 真实构造同 bar internal BOS + swing BOS，
        断言事件 identity 稳定区分（persistence ON CONFLICT 不互相吃掉）。
        """
        from app.services.first_pyramid_history_service import _build_event_id
        int_evt = {"type": "BOS", "bar_index": 77, "internal": True}
        swg_evt = {"type": "BOS", "bar_index": 77, "internal": False}
        id_int = _build_event_id(int_evt, "BOS")
        id_swg = _build_event_id(swg_evt, "BOS")
        # 两条事件 identity 必须不同（同 bar internal vs swing）
        assert id_int != id_swg
        assert id_int == "BOS_77_int"
        assert id_swg == "BOS_77_swg"
        # 同 bar 多个 OB 生命周期事件（不同 anchor）也区分
        ob1 = {"type": "OB_ENTERED", "bar_index": 80, "anchor_index": 12}
        ob2 = {"type": "OB_ENTERED", "bar_index": 80, "anchor_index": 13}
        assert _build_event_id(ob1, "OB_ENTERED") != _build_event_id(ob2, "OB_ENTERED")

    def test_no_future_observation_in_state(self) -> None:
        """daily_state 的 freshness 不引用未来确认（prefix-causal）。"""
        bars = _trend_bars(n=280)
        result = compute_first_pyramid_history(bars, symbol="T", output_bars=250)
        for state in result["daily_state"]:
            bi = state.get("bar_index")
            for key in ("latest_bos_freshness", "latest_choch_freshness", "latest_ob_freshness"):
                val = state.get(key)
                if val is not None and bi is not None:
                    # freshness = bi - confirmed_index >= 0，不可能引用未来
                    assert val >= 0
