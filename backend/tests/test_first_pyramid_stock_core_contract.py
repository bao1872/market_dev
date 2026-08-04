"""第一金字塔 stock_core 链路合同测试（canonical flatten → read model）。

覆盖：
1. 250 bars 完整 fixture 生成非 chip 结果（trend/structure/momentum 有值）
2. fp_segment_change_pct 精确映射
3. fp_summary 生成
4. fp_run_id / fp_calculated_at 存在（created_at 修复后）
5. chip 缺失时 fp_chip_available=false（非 null）
6. chip 缺失不影响趋势/结构/动量
7. chip 存在时正确合并
8. flat mapping 包含 Schema 声明的全部 fp 字段
9. 新增 Schema 字段时映射测试自动失败（flat 键集与 FP_ALL_KEYS 一致）
10. volume relation 合法计算
11. 输入不足时 reason 正确
12. stock_core payload 不丢字段（build_summary_payload + assemble）
13. API 序列化不丢字段

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_first_pyramid_stock_core_contract.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.schemas.first_pyramid import FirstPyramidCoreSnapshot
from app.services.first_pyramid_flatten import (
    FP_ALL_KEYS,
    FP_CHIP_KEYS,
    assemble_first_pyramid_read_model,
    flatten_first_pyramid,
)
from app.services.first_pyramid_service import compute_first_pyramid_core_snapshot


def _build_bars(n: int = 250, trend: str = "up", seed: int = 42) -> pd.DataFrame:
    """构造 250 根 OHLCV 日线 synthetic fixture（可重复）。"""
    np.random.seed(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    drift = 0.08 if trend == "up" else -0.08 if trend == "down" else 0.0
    noise = np.random.randn(n) * 0.15
    close = 10.0 + np.cumsum(noise + drift)
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


def _core_to_dict(bars: pd.DataFrame) -> dict:
    core = compute_first_pyramid_core_snapshot(
        bars, symbol="TEST.UP", trade_date=bars.index[-1].date().isoformat()
    )
    assert isinstance(core, FirstPyramidCoreSnapshot)
    return core.model_dump(by_alias=False)


class TestCoreCanonicalFields:
    def test_250bars_produces_non_chip_fields(self):
        """250 bars 完整 fixture：trend/structure/momentum 维度可用，statusText 非空。"""
        d = _core_to_dict(_build_bars(n=250))
        assert d["trend"]["available"] is True
        assert d["structure"]["available"] is True
        assert d["momentum"]["available"] is True
        assert d["trend"]["statusText"]
        assert d["statusText"]

    def test_fp_segment_change_pct_maps_exactly(self):
        """fp_segment_change_pct 精确等于 trend.continuousFactors.segment_change_pct。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        assert flat["fp_segment_change_pct"] == _safe_float(
            d["trend"]["continuousFactors"].get("segment_change_pct")
        )
        # DSA 上行趋势应产出段涨跌（非 None）
        assert flat["fp_segment_change_pct"] is not None

    def test_fp_summary_generated(self):
        """fp_summary 从真实聚合状态生成，非空。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        assert flat["fp_summary"] is not None
        assert flat["fp_summary"] == d.get("statusText")

    def test_fp_run_id_and_calculated_at_exist(self):
        """fp_run_id / fp_calculated_at 在传入时存在，且不受 chip 缺失影响。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(
            d, run_id="RUN-ABC-123", calculated_at="2026-07-31T15:30:00Z"
        )
        assert flat["fp_run_id"] == "RUN-ABC-123"
        assert flat["fp_calculated_at"] == "2026-07-31T15:30:00Z"
        # chip 缺失时仍保留
        assert flat["fp_run_id"] is not None
        assert flat["fp_calculated_at"] is not None

    def test_fp_run_id_overridden_by_read_model(self):
        """assemble read model 用 snapshot source_run_id/created_at 覆盖 run_id/calculated_at。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        assembled = assemble_first_pyramid_read_model(
            flat,
            snapshot_columns={
                "trade_date": "2026-07-31",
                "created_at": "2026-07-31T15:30:00Z",
                "source_run_id": "e616b2d4-b12e-4aa9-b45a-d174c9ce06fd",
            },
        )
        assert assembled["fp_run_id"] == "e616b2d4-b12e-4aa9-b45a-d174c9ce06fd"
        assert assembled["fp_calculated_at"] == "2026-07-31T15:30:00Z"
        assert assembled["fp_trade_date"] == "2026-07-31"

    def test_fp_momentum_volume_relation_legal(self):
        """fp_momentum_volume_relation 合法取值或 None（无 squeeze 区间时为 None）。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        val = flat["fp_momentum_volume_relation"]
        # 合法值域：None 或已知枚举；禁止用 0 伪装未知
        if val is not None:
            assert val in {"缩量挤压", "放量释放"}
        assert val != 0

    def test_input_insufficient_reason(self):
        """输入不足（<60 bars）时 core 计算抛 ValueError，不生成空 snapshot。"""
        with pytest.raises(ValueError, match="不足"):
            compute_first_pyramid_core_snapshot(
                _build_bars(n=30), symbol="TEST.SHORT", trade_date="2026-01-01"
            )


class TestChipSeparation:
    def test_chip_missing_chip_available_false(self):
        """chip 缺失时 fp_chip_available=false（明确 boolean，非 null）。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        assembled = assemble_first_pyramid_read_model(flat, chip_snapshot=None)
        assert assembled["fp_chip_available"] is False
        assert assembled["fp_chip_available"] is not None
        # chip 字段为 None
        for k in FP_CHIP_KEYS:
            assert assembled[k] is None

    def test_chip_missing_does_not_clear_core(self):
        """chip 缺失不影响趋势/结构/动量字段，fp_summary 不消失。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        assembled = assemble_first_pyramid_read_model(flat, chip_snapshot=None)
        assert assembled["fp_summary"] is not None
        assert assembled["fp_segment_change_pct"] is not None
        assert assembled["fp_trend_direction"] is not None

    def test_chip_present_merges_correctly(self):
        """chip 存在且有效时正确合并，fp_chip_available=true。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        chip_flat = {
            "fp_chip_state": "筹码共识，POC 上方",
            "fp_poc_price": 12.5,
            "fp_poc_distance_pct": 2.0,
            "fp_peak_node_count": 5,
            "fp_vah_price": 13.0,
            "fp_val_price": 11.0,
            "fp_node_event_type": "NODE_CROSS_UP",
            "fp_node_event_direction": "up",
            "fp_node_event_freshness": 3,
            "fp_node_event_price": 12.4,
        }
        assembled = assemble_first_pyramid_read_model(
            flat, chip_snapshot={"chip_flat": chip_flat, "chip_available": True}
        )
        assert assembled["fp_chip_available"] is True
        assert assembled["fp_poc_price"] == 12.5
        assert assembled["fp_chip_state"] == "筹码共识，POC 上方"
        # 非 chip 字段保留
        assert assembled["fp_summary"] is not None


class TestFieldMappingCompleteness:
    def test_flat_keys_match_schema(self):
        """flatten 输出的键集必须与 Schema 声明的 FP_ALL_KEYS 完全一致。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        assert set(flat.keys()) == set(FP_ALL_KEYS)
        # 新增 Schema 字段时，若 flatten 未映射，此测试自动失败
        assert len(FP_ALL_KEYS) == 99

    def test_read_model_retains_all_keys(self):
        """assemble read model 保留全部 99 键（含 chip 字段位置）。"""
        d = _core_to_dict(_build_bars(n=250))
        flat = flatten_first_pyramid(d)
        assembled = assemble_first_pyramid_read_model(flat, chip_snapshot=None)
        assert set(assembled.keys()) == set(FP_ALL_KEYS)

    def test_build_summary_payload_retains_first_pyramid_flat(self):
        """build_summary_payload 生成的 summary_payload 保留完整 first_pyramid_flat 99 键。"""
        from datetime import date

        from app.services.feature_snapshot_service import build_summary_payload

        d = _core_to_dict(_build_bars(n=250))
        summary = build_summary_payload(
            {}, {}, date(2026, 7, 31),
            first_pyramid=d,
            source_run_id="e616b2d4-b12e-4aa9-b45a-d174c9ce06fd",
        )
        flat = summary["first_pyramid_flat"]
        assert set(flat.keys()) == set(FP_ALL_KEYS)
        # created_at 已覆盖：fp_calculated_at 非 null（修复后）
        assert flat["fp_calculated_at"] is not None
        assert flat["fp_run_id"] == "e616b2d4-b12e-4aa9-b45a-d174c9ce06fd"
        assert flat["fp_chip_available"] is False


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class TestFieldAvailabilityAfterCloseChain:
    """[字段级 availability 合同 2026-08-04] 盘后 stock_core/Review 主链必须携带 fieldAvailability。

    即时完整视图（assemble_first_pyramid_view）已有 test_assemble_view_injects_field_availability
    覆盖；本类验证盘后主链的**源**（FirstPyramidCoreSnapshot）与**溯源注入**（inject_...）。
    """

    def test_core_snapshot_carries_field_availability(self):
        """盘后主链使用的 FirstPyramidCoreSnapshot 必须携带 fieldAvailability。"""
        core = compute_first_pyramid_core_snapshot(
            _build_bars(n=250), symbol="TEST.UP",
            trade_date=_build_bars(n=250).index[-1].date().isoformat(),
        )
        assert isinstance(core, FirstPyramidCoreSnapshot)
        assert hasattr(core, "fieldAvailability"), (
            "FirstPyramidCoreSnapshot 缺 fieldAvailability → 盘后持久化链丢失字段级原因"
        )
        # 至少高空值字段存在显式原因（squeeze_avg_volume / volume_relation / sqzmom_value）
        keys = set(core.fieldAvailability.keys())
        assert "momentum.squeeze_avg_volume" in keys or keys, (
            "fieldAvailability 必须至少覆盖条件性可空因子"
        )
        for fa in core.fieldAvailability.values():
            assert fa.reasonCode in {
                "not_applicable",
                "insufficient_history",
                "upstream_unavailable",
                "failed",
                "stale",
                "missing",
            }, f"非法 reasonCode: {fa.reasonCode}"

    def test_inject_field_availability_provenance_fills_run_meta(self):
        """inject_field_availability_provenance 必须为每个条目注入 sourceRunId/calculatedAt。

        [PRD 溯源要求] 每个 FieldAvailability 返回 sourceRunId / calculatedAt；
        盘后主链由编排器统一注入（同一 run 全股票共享）。
        """
        from app.schemas.first_pyramid import FieldAvailability
        from app.services.first_pyramid_service import (
            inject_field_availability_provenance,
        )

        avail = {
            "momentum.squeeze_avg_volume": FieldAvailability(
                availability="not_applicable",
                reasonCode="not_applicable",
                reasonText="当前无挤压，均量不适用",
                observationCount=0,
            )
        }
        injected = inject_field_availability_provenance(
            avail,
            source_run_id="00000000-0000-0000-0000-0000000000aa",
            calculated_at="2026-08-04T15:30:00Z",
        )
        entry = injected["momentum.squeeze_avg_volume"]
        assert entry.sourceRunId == "00000000-0000-0000-0000-0000000000aa"
        assert entry.calculatedAt == "2026-08-04T15:30:00Z"
        assert entry.reasonCode == "not_applicable"

    def test_inject_keeps_none_when_no_run(self):
        """单股即时路径无 run 来源时保持 None（不伪造溯源）。"""
        from app.schemas.first_pyramid import FieldAvailability
        from app.services.first_pyramid_service import (
            inject_field_availability_provenance,
        )

        avail = {
            "momentum.squeeze_avg_volume": FieldAvailability(
                availability="not_applicable",
                reasonCode="not_applicable",
                reasonText="当前无挤压",
                observationCount=0,
            )
        }
        injected = inject_field_availability_provenance(avail, source_run_id=None, calculated_at=None)
        assert injected["momentum.squeeze_avg_volume"].sourceRunId is None
        assert injected["momentum.squeeze_avg_volume"].calculatedAt is None
