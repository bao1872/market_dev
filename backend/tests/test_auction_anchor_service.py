# PURE_UNIT_TEST=1
"""竞价锚点生成服务纯单元测试 - 不连接数据库。

覆盖：
  1. 结构锚点提取（BOS/CHoCH 触发线、OB 区间、trailing 失效线）
  2. 筹码锚点提取（POC/VAH/VAL 共识区、cross_up/cross_down 事件）
  3. 复合锚点合并（近距离结构+筹码 ≤1% 偏差）
  4. 活跃锚点筛选（expired/stale 降级、低强度降级、上限 20）
  5. chip 未完成时只生成结构锚点（structure_only 状态）
  6. 旧/新 source run 不一致时禁止发布（AnchorVersionMismatchError）
  7. 锚点 freshness 衰减（fresh → stale → expired）

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_auction_anchor_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.auction_anchor_service import (
    _STRENGTH_BOS,
    _STRENGTH_CHIP_CROSS,
    _STRENGTH_CHOCH,
    _STRENGTH_COMPOSITE,
    _STRENGTH_OB,
    _STRENGTH_POC,
    _STRENGTH_TRAILING,
    _STRENGTH_VAH_VAL,
    ANCHOR_EXPIRY_DAYS,
    ANCHOR_FRESHNESS_THRESHOLD_DAYS,
    AUCTION_ANCHOR_ALGORITHM_VERSION,
    COMPOSITE_MERGE_TOLERANCE_PCT,
    MAX_ACTIVE_ANCHORS_PER_INSTRUMENT,
    MIN_ACTIVE_STRENGTH,
    AnchorCoverageLowError,
    AnchorSnapshotNotFoundError,
    AnchorSnapshotNotReadyError,
    AnchorVersionMismatchError,
    _build_composite_anchors,
    _compute_freshness,
    _deduplicate_anchors,
    _extract_chip_anchors,
    _extract_structure_anchors,
    _filter_active_anchors,
    _make_anchor_item,
    _parse_iso_date,
    _safe_decimal,
    _safe_float,
    generate_auction_anchors,
    publish_auction_anchors,
)

_TRADE_DATE = date(2026, 7, 30)


# =============================================================================
# 辅助构造函数
# =============================================================================


def _make_snapshot(
    instrument_id: uuid.UUID,
    summary_payload: dict,
) -> SimpleNamespace:
    """构造 StockFeatureSnapshot 的最小 mock（仅含被测字段）。"""
    return SimpleNamespace(
        instrument_id=instrument_id,
        summary_payload=summary_payload,
    )


def _make_chip_snapshot(
    instrument_id: uuid.UUID,
    chip_payload: dict,
) -> SimpleNamespace:
    """构造 StockChipConsensusSnapshot 的最小 mock。"""
    return SimpleNamespace(
        instrument_id=instrument_id,
        chip_payload=chip_payload,
    )


def _bos_event(
    price: float,
    direction: str = "up",
    occurred_at: str = "2026-07-25",
    structure_level: str | None = "swing_high",
) -> dict:
    """构造 BOS 事件。"""
    return {
        "type": "BOS",
        "direction": direction,
        "price": price,
        "occurredAt": occurred_at,
        "barIndex": 100,
        "extra": {
            "structure_level": structure_level,
            "anchor_index": 5,
        },
    }


def _choch_event(
    price: float,
    direction: str = "down",
    occurred_at: str = "2026-07-25",
) -> dict:
    """构造 CHoCH 事件。"""
    return {
        "type": "CHoCH",
        "direction": direction,
        "price": price,
        "occurredAt": occurred_at,
        "barIndex": 110,
        "extra": {"structure_level": "swing_low", "anchor_index": 6},
    }


def _ob_event(
    ob_high: float,
    ob_low: float,
    direction: str = "up",
    occurred_at: str = "2026-07-25",
) -> dict:
    """构造 OB_CREATED 事件。"""
    return {
        "type": "OB_CREATED",
        "direction": direction,
        "occurredAt": occurred_at,
        "barIndex": 120,
        "extra": {
            "ob_high": ob_high,
            "ob_low": ob_low,
            "structure_level": "demand_ob",
            "anchor_index": 7,
        },
    }


def _make_summary(
    events: list[dict] | None = None,
    trailing_top: float | None = None,
    trailing_bottom: float | None = None,
) -> dict:
    """构造 StockFeatureSnapshot.summary_payload。"""
    continuous: dict = {}
    if trailing_top is not None:
        continuous["trailing_top"] = trailing_top
    if trailing_bottom is not None:
        continuous["trailing_bottom"] = trailing_bottom
    return {
        "first_pyramid": {
            "structure": {
                "events": events or [],
                "continuousFactors": continuous,
            },
        },
    }


def _make_chip_payload(
    poc: float | None = 10.0,
    vah: float | None = 11.0,
    val: float | None = 9.0,
    last_close: float | None = 10.5,
    events: list[dict] | None = None,
    n_peak_nodes: int = 2,
) -> dict:
    """构造 StockChipConsensusSnapshot.chip_payload。"""
    continuous: dict = {"n_peak_nodes": n_peak_nodes}
    if last_close is not None:
        continuous["last_close"] = last_close
    if poc is not None:
        continuous["poc_price"] = poc
    if vah is not None:
        continuous["vah_price"] = vah
    if val is not None:
        continuous["val_price"] = val
    return {"chip": {"continuousFactors": continuous, "events": events or []}}


# =============================================================================
# 测试：常量校验
# =============================================================================


class TestConstants:
    """常量校验，防止阈值被误改。"""

    def test_algorithm_version(self) -> None:
        assert AUCTION_ANCHOR_ALGORITHM_VERSION == "v1.0.0"

    def test_max_active_anchors(self) -> None:
        assert MAX_ACTIVE_ANCHORS_PER_INSTRUMENT == 20

    def test_freshness_thresholds(self) -> None:
        assert ANCHOR_FRESHNESS_THRESHOLD_DAYS == 30
        assert ANCHOR_EXPIRY_DAYS == 60

    def test_min_active_strength(self) -> None:
        assert MIN_ACTIVE_STRENGTH == 0.3

    def test_composite_merge_tolerance(self) -> None:
        assert COMPOSITE_MERGE_TOLERANCE_PCT == 0.01

    def test_strength_constants(self) -> None:
        assert _STRENGTH_BOS == 0.85
        assert _STRENGTH_CHOCH == 0.80
        assert _STRENGTH_OB == 0.70
        assert _STRENGTH_TRAILING == 0.60
        assert _STRENGTH_POC == 0.80
        assert _STRENGTH_VAH_VAL == 0.70
        assert _STRENGTH_CHIP_CROSS == 0.65
        assert _STRENGTH_COMPOSITE == 0.90


# =============================================================================
# 测试：辅助纯函数
# =============================================================================


class TestSafeDecimal:
    """_safe_decimal 安全转换。"""

    def test_none_returns_none(self) -> None:
        assert _safe_decimal(None) is None

    def test_invalid_string_returns_none(self) -> None:
        assert _safe_decimal("invalid") is None

    def test_nan_returns_none(self) -> None:
        assert _safe_decimal("nan") is None

    def test_valid_string(self) -> None:
        assert _safe_decimal("10.5") == Decimal("10.5")

    def test_integer(self) -> None:
        assert _safe_decimal(10) == Decimal("10")

    def test_float_input(self) -> None:
        assert _safe_decimal(3.14) == Decimal("3.14")


class TestSafeFloat:
    """_safe_float 安全转换。"""

    def test_none_returns_none(self) -> None:
        assert _safe_float(None) is None

    def test_invalid_returns_none(self) -> None:
        assert _safe_float("abc") is None

    def test_valid_string(self) -> None:
        assert _safe_float("3.14") == 3.14


class TestComputeFreshness:
    """_compute_freshness 新鲜度衰减 fresh → stale → expired。"""

    def test_none_formed_at_returns_fresh(self) -> None:
        """无形成日期视为 fresh（POC/trailing 等状态型锚点）。"""
        assert _compute_freshness(None, _TRADE_DATE) == "fresh"

    def test_future_date_returns_fresh(self) -> None:
        """未来日期视为 fresh（不应发生）。"""
        assert _compute_freshness(date(2026, 8, 1), _TRADE_DATE) == "fresh"

    def test_recent_returns_fresh(self) -> None:
        """30 天内 → fresh。"""
        assert _compute_freshness(date(2026, 7, 29), _TRADE_DATE) == "fresh"
        assert _compute_freshness(date(2026, 6, 30), _TRADE_DATE) == "fresh"

    def test_30_to_60_days_returns_stale(self) -> None:
        """31~60 天 → stale。"""
        assert _compute_freshness(date(2026, 6, 29), _TRADE_DATE) == "stale"
        assert _compute_freshness(date(2026, 5, 31), _TRADE_DATE) == "stale"

    def test_over_60_days_returns_expired(self) -> None:
        """超过 60 天 → expired。"""
        assert _compute_freshness(date(2026, 5, 30), _TRADE_DATE) == "expired"
        assert _compute_freshness(date(2026, 1, 1), _TRADE_DATE) == "expired"


class TestParseIsoDate:
    """_parse_iso_date 日期解析。"""

    def test_none(self) -> None:
        assert _parse_iso_date(None) is None

    def test_iso_string(self) -> None:
        assert _parse_iso_date("2026-07-30") == date(2026, 7, 30)

    def test_iso_with_time(self) -> None:
        """只取前 10 位日期部分。"""
        assert _parse_iso_date("2026-07-30T09:25:00") == date(2026, 7, 30)

    def test_invalid_returns_none(self) -> None:
        assert _parse_iso_date("invalid") is None

    def test_date_object_passthrough(self) -> None:
        d = date(2026, 7, 30)
        assert _parse_iso_date(d) == d


# =============================================================================
# 测试：结构锚点提取
# =============================================================================


class TestExtractStructureAnchors:
    """_extract_structure_anchors 结构锚点提取。"""

    def test_bos_trigger_anchor(self) -> None:
        """BOS 事件 → 触发线锚点，strength=_STRENGTH_BOS。"""
        inst_id = uuid.uuid4()
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[_bos_event(price=10.5, direction="up")]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        item = items[0]
        assert item.anchor_type == "structure"
        assert item.direction == "up"
        assert item.center_price == Decimal("10.5")
        assert item.lower_price == Decimal("10.5")
        assert item.upper_price == Decimal("10.5")
        assert item.strength == _STRENGTH_BOS
        assert item.freshness == "fresh"
        assert item.structure_payload["event_type"] == "BOS"
        assert item.structure_payload["trigger_price"] == "10.5"

    def test_choch_trigger_anchor(self) -> None:
        """CHoCH 事件 → 触发线锚点，strength=_STRENGTH_CHOCH。"""
        inst_id = uuid.uuid4()
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[_choch_event(price=9.5, direction="down")]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        item = items[0]
        assert item.anchor_type == "structure"
        assert item.direction == "down"
        assert item.strength == _STRENGTH_CHOCH
        assert item.structure_payload["event_type"] == "CHoCH"

    def test_ob_range_anchor(self) -> None:
        """OB_CREATED 事件 → 区间锚点，center=(high+low)/2。"""
        inst_id = uuid.uuid4()
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[_ob_event(ob_high=11.0, ob_low=10.0, direction="up")]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        item = items[0]
        assert item.anchor_type == "structure"
        assert item.lower_price == Decimal("10.0")
        assert item.upper_price == Decimal("11.0")
        assert item.center_price == Decimal("10.5")
        assert item.strength == _STRENGTH_OB
        assert item.structure_payload["event_type"] == "OB_CREATED"
        assert item.structure_payload["ob_high"] == "11.0"
        assert item.structure_payload["ob_low"] == "10.0"

    def test_trailing_top_invalidation_line(self) -> None:
        """trailing_top → 失效线锚点（direction=down 阻力）。"""
        inst_id = uuid.uuid4()
        snap = _make_snapshot(
            inst_id, _make_summary(trailing_top=12.0),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        item = items[0]
        assert item.direction == "down"
        assert item.center_price == Decimal("12.0")
        assert item.strength == _STRENGTH_TRAILING
        assert item.freshness == "fresh"
        assert item.structure_payload["kind"] == "trailing_top"

    def test_trailing_bottom_invalidation_line(self) -> None:
        """trailing_bottom → 失效线锚点（direction=up 支撑）。"""
        inst_id = uuid.uuid4()
        snap = _make_snapshot(
            inst_id, _make_summary(trailing_bottom=8.0),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        assert items[0].direction == "up"
        assert items[0].center_price == Decimal("8.0")
        assert items[0].structure_payload["kind"] == "trailing_bottom"

    def test_invalid_direction_skipped_with_reason(self) -> None:
        """direction 不在 (up/down) 时跳过并记录 reason_codes。"""
        inst_id = uuid.uuid4()
        invalid_event = {
            "type": "BOS",
            "direction": "sideways",
            "price": 10.0,
            "occurredAt": "2026-07-25",
            "extra": {},
        }
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[invalid_event, _bos_event(price=11.0)]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        # 无效 direction 被跳过，仅保留有效锚点
        assert len(items) == 1
        # 跳过原因附加到首个有效锚点
        assert "BOS:invalid_direction" in items[0].reason_codes

    def test_bos_missing_price_skipped(self) -> None:
        """BOS 事件缺 price 时跳过。"""
        inst_id = uuid.uuid4()
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[
                {"type": "BOS", "direction": "up", "occurredAt": "2026-07-25", "extra": {}},
                _bos_event(price=11.0),
            ]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        assert "BOS:missing_price" in items[0].reason_codes

    def test_ob_missing_range_skipped(self) -> None:
        """OB_CREATED 缺 ob_high/ob_low 时跳过。"""
        inst_id = uuid.uuid4()
        bad_ob = {
            "type": "OB_CREATED",
            "direction": "up",
            "occurredAt": "2026-07-25",
            "extra": {"ob_high": 10.0},  # 缺 ob_low
        }
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[bad_ob, _bos_event(price=11.0)]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        assert "OB_CREATED:missing_ob_range" in items[0].reason_codes

    def test_ob_high_less_than_low_skipped(self) -> None:
        """OB_CREATED 的 ob_high < ob_low 时跳过。"""
        inst_id = uuid.uuid4()
        bad_ob = {
            "type": "OB_CREATED",
            "direction": "up",
            "occurredAt": "2026-07-25",
            "extra": {"ob_high": 9.0, "ob_low": 10.0},
        }
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[bad_ob, _bos_event(price=11.0)]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        assert "OB_CREATED:missing_ob_range" in items[0].reason_codes

    def test_stale_event_freshness(self) -> None:
        """事件 occurredAt 距今 40 天 → stale。"""
        inst_id = uuid.uuid4()
        snap = _make_snapshot(
            inst_id,
            _make_summary(events=[_bos_event(price=10.0, occurred_at="2026-06-20")]),
        )
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 1
        assert items[0].freshness == "stale"

    def test_empty_summary_returns_empty(self) -> None:
        """空 summary_payload 返回空列表。"""
        snap = SimpleNamespace(instrument_id=uuid.uuid4(), summary_payload={})
        items = _extract_structure_anchors(
            snap,
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert items == []


# =============================================================================
# 测试：筹码锚点提取
# =============================================================================


class TestExtractChipAnchors:
    """_extract_chip_anchors 筹码锚点提取。"""

    def test_poc_vah_val_extracted(self) -> None:
        """POC（主峰）、VAH（上共识区）、VAL（下共识区）三种筹码锚点提取。"""
        inst_id = uuid.uuid4()
        chip_snap = _make_chip_snapshot(
            inst_id,
            _make_chip_payload(poc=10.0, vah=11.0, val=9.0, last_close=10.5),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(items) == 3
        kinds = {(i.chip_payload or {}).get("kind") for i in items}
        assert kinds == {"poc", "vah", "val"}

    def test_poc_direction_based_on_last_close(self) -> None:
        """POC 方向：last_close >= poc → up（支撑），last_close < poc → down（阻力）。"""
        inst_id = uuid.uuid4()
        # last_close < poc → direction=down
        chip_snap = _make_chip_snapshot(
            inst_id,
            _make_chip_payload(poc=10.0, last_close=9.5),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        poc_item = next(i for i in items if (i.chip_payload or {}).get("kind") == "poc")
        assert poc_item.direction == "down"
        assert poc_item.strength == _STRENGTH_POC

        # last_close >= poc → direction=up
        chip_snap2 = _make_chip_snapshot(
            inst_id,
            _make_chip_payload(poc=10.0, last_close=10.5),
        )
        items2 = _extract_chip_anchors(
            chip_snap2,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        poc_item2 = next(i for i in items2 if (i.chip_payload or {}).get("kind") == "poc")
        assert poc_item2.direction == "up"

    def test_vah_is_resistance_direction_down(self) -> None:
        """VAH（上共识区）→ direction=down 阻力。"""
        inst_id = uuid.uuid4()
        chip_snap = _make_chip_snapshot(
            inst_id, _make_chip_payload(poc=10.0, vah=11.0, val=9.0),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        vah = next(i for i in items if (i.chip_payload or {}).get("kind") == "vah")
        assert vah.direction == "down"
        assert vah.strength == _STRENGTH_VAH_VAL

    def test_val_is_support_direction_up(self) -> None:
        """VAL（下共识区）→ direction=up 支撑。"""
        inst_id = uuid.uuid4()
        chip_snap = _make_chip_snapshot(
            inst_id, _make_chip_payload(poc=10.0, vah=11.0, val=9.0),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        val = next(i for i in items if (i.chip_payload or {}).get("kind") == "val")
        assert val.direction == "up"
        assert val.strength == _STRENGTH_VAH_VAL

    def test_cross_up_event(self) -> None:
        """cross_up 事件 → direction=up 锚点。"""
        inst_id = uuid.uuid4()
        chip_snap = _make_chip_snapshot(
            inst_id,
            _make_chip_payload(
                poc=10.0, vah=11.0, val=9.0,
                events=[{
                    # type 含 "cross_up" → 服务识别为 direction=up（与 first_pyramid_service 一致）
                    "type": "cross_up",
                    "price": 9.5,
                    "occurredAt": "2026-07-25",
                    "node_price": 9.5,
                }],
            ),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        cross_items = [
            i for i in items if (i.chip_payload or {}).get("kind") == "cross_event"
        ]
        assert len(cross_items) == 1
        assert cross_items[0].direction == "up"
        assert cross_items[0].strength == _STRENGTH_CHIP_CROSS

    def test_cross_down_event(self) -> None:
        """cross_down 事件 → direction=down 锚点。"""
        inst_id = uuid.uuid4()
        chip_snap = _make_chip_snapshot(
            inst_id,
            _make_chip_payload(
                poc=10.0, vah=11.0, val=9.0,
                events=[{
                    # type 含 "cross_down" → 服务识别为 direction=down
                    "type": "cross_down",
                    "price": 11.0,
                    "occurredAt": "2026-07-25",
                }],
            ),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        cross_items = [
            i for i in items if (i.chip_payload or {}).get("kind") == "cross_event"
        ]
        assert len(cross_items) == 1
        assert cross_items[0].direction == "down"

    def test_missing_poc_records_reason(self) -> None:
        """缺 POC 时记录 chip:missing_poc，其他锚点仍生成。"""
        inst_id = uuid.uuid4()
        chip_snap = _make_chip_snapshot(
            inst_id, _make_chip_payload(poc=None, vah=11.0, val=9.0),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        # 仍生成 vah/val
        assert len(items) == 2
        assert "chip:missing_poc" in items[0].reason_codes

    def test_cross_event_missing_price_skipped(self) -> None:
        """cross 事件缺 price 时跳过并记录原因。"""
        inst_id = uuid.uuid4()
        chip_snap = _make_chip_snapshot(
            inst_id,
            _make_chip_payload(
                poc=10.0, vah=11.0, val=9.0,
                events=[{
                    # type 含 "cross_up" 但缺 price → 记录 chip:cross_up:missing_price
                    "type": "cross_up",
                    "occurredAt": "2026-07-25",
                }],
            ),
        )
        items = _extract_chip_anchors(
            chip_snap,
            snapshot_id=uuid.uuid4(),
            source_chip_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        cross_items = [
            i for i in items if (i.chip_payload or {}).get("kind") == "cross_event"
        ]
        assert len(cross_items) == 0
        assert "chip:cross_up:missing_price" in items[0].reason_codes


# =============================================================================
# 测试：复合锚点合并
# =============================================================================


class TestBuildCompositeAnchors:
    """_build_composite_anchors 近距离结构+筹码合并。"""

    def _make_structure_item(
        self, center: float, direction: str = "up", freshness: str = "fresh",
    ) -> AuctionAnchorItemStub:
        return AuctionAnchorItemStub(
            anchor_type="structure",
            direction=direction,
            center_price=Decimal(str(center)),
            lower_price=Decimal(str(center)),
            upper_price=Decimal(str(center)),
            freshness=freshness,
            strength=_STRENGTH_BOS,
            structure_payload={"event_type": "BOS"},
        )

    def _make_chip_item(
        self, center: float, direction: str = "up", freshness: str = "fresh",
    ) -> AuctionAnchorItemStub:
        return AuctionAnchorItemStub(
            anchor_type="chip",
            direction=direction,
            center_price=Decimal(str(center)),
            lower_price=Decimal(str(center)),
            upper_price=Decimal(str(center)),
            freshness=freshness,
            strength=_STRENGTH_POC,
            chip_payload={"kind": "poc"},
        )

    def test_nearby_structure_and_chip_merged(self) -> None:
        """结构锚点与筹码锚点偏差 ≤1% 时合并为复合锚点。"""
        s_item = self._make_structure_item(center=10.0)
        c_item = self._make_chip_item(center=10.05)  # 偏差 0.5% < 1%
        composites = _build_composite_anchors(
            [s_item], [c_item],
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(composites) == 1
        comp = composites[0]
        assert comp.anchor_type == "composite"
        assert comp.strength == _STRENGTH_COMPOSITE
        # center = (10.0 + 10.05) / 2 = 10.025
        assert comp.center_price == Decimal("10.025")
        # 包络
        assert comp.lower_price == Decimal("10.0")
        assert comp.upper_price == Decimal("10.05")

    def test_far_structure_and_chip_not_merged(self) -> None:
        """偏差 >1% 时不合并。"""
        s_item = self._make_structure_item(center=10.0)
        c_item = self._make_chip_item(center=10.5)  # 偏差 5% > 1%
        composites = _build_composite_anchors(
            [s_item], [c_item],
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert composites == []

    def test_empty_structure_returns_empty(self) -> None:
        """结构锚点为空时不合并。"""
        c_item = self._make_chip_item(center=10.0)
        composites = _build_composite_anchors(
            [], [c_item],
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert composites == []

    def test_empty_chip_returns_empty(self) -> None:
        """筹码锚点为空时不合并。"""
        s_item = self._make_structure_item(center=10.0)
        composites = _build_composite_anchors(
            [s_item], [],
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert composites == []

    def test_one_structure_only_merges_one_chip(self) -> None:
        """一个结构锚点只合并一个筹码锚点（用完即停）。"""
        s_item = self._make_structure_item(center=10.0)
        c1 = self._make_chip_item(center=10.0)
        c2 = self._make_chip_item(center=10.05)
        composites = _build_composite_anchors(
            [s_item], [c1, c2],
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(composites) == 1

    def test_composite_freshness_takes_older(self) -> None:
        """复合锚点 freshness 取较旧（保守）。"""
        s_item = self._make_structure_item(center=10.0, freshness="stale")
        c_item = self._make_chip_item(center=10.0, freshness="fresh")
        composites = _build_composite_anchors(
            [s_item], [c_item],
            snapshot_id=uuid.uuid4(),
            source_core_run_id=uuid.uuid4(),
            price_adjustment_version="qfx_v1",
            trade_date=_TRADE_DATE,
        )
        assert len(composites) == 1
        assert composites[0].freshness == "stale"


# =============================================================================
# 测试：去重与活跃锚点筛选
# =============================================================================


class TestDeduplicateAnchors:
    """_deduplicate_anchors 按 (anchor_type, direction) 去重。"""

    def test_keep_strongest(self) -> None:
        """同 (type, direction) 保留 strength 最高。"""
        items = [
            AuctionAnchorItemStub(
                anchor_type="structure", direction="up",
                center_price=Decimal("10.0"), strength=0.5, freshness="fresh",
            ),
            AuctionAnchorItemStub(
                anchor_type="structure", direction="up",
                center_price=Decimal("11.0"), strength=0.85, freshness="fresh",
            ),
        ]
        deduped = _deduplicate_anchors(items)
        assert len(deduped) == 1
        assert deduped[0].strength == 0.85

    def test_same_strength_keep_fresher(self) -> None:
        """strength 相同时保留 freshness 更新的（fresh < stale）。"""
        items = [
            AuctionAnchorItemStub(
                anchor_type="chip", direction="down",
                center_price=Decimal("10.0"), strength=0.7, freshness="stale",
            ),
            AuctionAnchorItemStub(
                anchor_type="chip", direction="down",
                center_price=Decimal("11.0"), strength=0.7, freshness="fresh",
            ),
        ]
        deduped = _deduplicate_anchors(items)
        assert len(deduped) == 1
        assert deduped[0].freshness == "fresh"

    def test_different_keys_kept(self) -> None:
        """不同 (type, direction) 各自保留。"""
        items = [
            AuctionAnchorItemStub(
                anchor_type="structure", direction="up", strength=0.5,
            ),
            AuctionAnchorItemStub(
                anchor_type="structure", direction="down", strength=0.5,
            ),
            AuctionAnchorItemStub(
                anchor_type="chip", direction="up", strength=0.5,
            ),
        ]
        deduped = _deduplicate_anchors(items)
        assert len(deduped) == 3


class TestFilterActiveAnchors:
    """_filter_active_anchors 活跃锚点筛选。"""

    def test_expired_marked_inactive(self) -> None:
        """expired 锚点 is_active=False。"""
        items = [
            AuctionAnchorItemStub(
                anchor_type="structure", direction="up",
                strength=0.9, freshness="expired",
            ),
        ]
        result = _filter_active_anchors(items)
        assert result[0].is_active is False

    def test_low_strength_marked_inactive(self) -> None:
        """strength < MIN_ACTIVE_STRENGTH 的锚点 is_active=False。"""
        items = [
            AuctionAnchorItemStub(
                anchor_type="structure", direction="up",
                strength=0.2, freshness="fresh",  # 低于 0.3
            ),
        ]
        result = _filter_active_anchors(items)
        assert result[0].is_active is False

    def test_strong_fresh_kept_active(self) -> None:
        """strength >= MIN 且 fresh/stale 的锚点 is_active=True。"""
        items = [
            AuctionAnchorItemStub(
                anchor_type="structure", direction="up",
                strength=0.7, freshness="fresh",
            ),
            AuctionAnchorItemStub(
                anchor_type="chip", direction="down",
                strength=0.5, freshness="stale",
            ),
        ]
        result = _filter_active_anchors(items)
        assert all(r.is_active for r in result)

    def test_max_active_anchors_cap(self) -> None:
        """活跃锚点超过 MAX_ACTIVE_ANCHORS_PER_INSTRUMENT(20) 时降级多余的。"""
        items = [
            AuctionAnchorItemStub(
                anchor_type=f"type_{i}",
                direction="up" if i % 2 == 0 else "down",
                strength=0.5 + i * 0.001,  # 递增保证排序稳定
                freshness="fresh",
            )
            for i in range(MAX_ACTIVE_ANCHORS_PER_INSTRUMENT + 5)
        ]
        # 不同 anchor_type 保证不被去重
        result = _filter_active_anchors(items)
        active = [r for r in result if r.is_active]
        inactive = [r for r in result if not r.is_active]
        assert len(active) == MAX_ACTIVE_ANCHORS_PER_INSTRUMENT
        assert len(inactive) == 5


# =============================================================================
# 测试：_make_anchor_item 工厂
# =============================================================================


class TestMakeAnchorItem:
    """_make_anchor_item 工厂：validity 由 freshness 决定。"""

    def test_fresh_valid(self) -> None:
        item = _make_anchor_item(
            snapshot_id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            instrument_id=uuid.uuid4(),
            anchor_type="structure",
            source_run_id=uuid.uuid4(),
            direction="up",
            lower_price=Decimal("10.0"),
            upper_price=Decimal("10.0"),
            center_price=Decimal("10.0"),
            strength=0.8,
            freshness="fresh",
            price_adjustment_version="qfx_v1",
        )
        assert item.validity == "valid"
        assert item.is_active is True
        assert item.reason_codes == []

    def test_expired_invalid(self) -> None:
        item = _make_anchor_item(
            snapshot_id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            instrument_id=uuid.uuid4(),
            anchor_type="structure",
            source_run_id=uuid.uuid4(),
            direction="up",
            lower_price=Decimal("10.0"),
            upper_price=Decimal("10.0"),
            center_price=Decimal("10.0"),
            strength=0.8,
            freshness="expired",
            price_adjustment_version="qfx_v1",
        )
        assert item.validity == "invalid"

    def test_reason_codes_default_empty(self) -> None:
        item = _make_anchor_item(
            snapshot_id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            instrument_id=uuid.uuid4(),
            anchor_type="structure",
            source_run_id=uuid.uuid4(),
            direction="up",
            lower_price=Decimal("10.0"),
            upper_price=Decimal("10.0"),
            center_price=Decimal("10.0"),
            strength=0.8,
            freshness="fresh",
            price_adjustment_version="qfx_v1",
        )
        assert item.reason_codes == []

    def test_reason_codes_provided(self) -> None:
        item = _make_anchor_item(
            snapshot_id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            instrument_id=uuid.uuid4(),
            anchor_type="structure",
            source_run_id=uuid.uuid4(),
            direction="up",
            lower_price=Decimal("10.0"),
            upper_price=Decimal("10.0"),
            center_price=Decimal("10.0"),
            strength=0.8,
            freshness="fresh",
            price_adjustment_version="qfx_v1",
            reason_codes=["test:reason"],
        )
        assert item.reason_codes == ["test:reason"]


# =============================================================================
# 测试：generate_auction_anchors structure_only 路径（mock DB）
# =============================================================================


class _MockResult:
    """简化 mock execute 结果。"""

    def __init__(self, scalar: Any = None, scalars_list: list = None) -> None:
        self._scalar = scalar
        self._scalars_list = scalars_list or []

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> _MockScalars:
        return _MockScalars(self._scalars_list)


class _MockScalars:
    def __init__(self, items: list) -> None:
        self._items = items

    def all(self) -> list:
        return self._items


class TestGenerateAuctionAnchorsStructureOnly:
    """generate_auction_anchors：chip 未完成时 status=structure_only。"""

    @pytest.mark.asyncio
    async def test_chip_not_completed_returns_structure_only(self) -> None:
        """chip_consensus 未完成时只生成结构锚点，status=structure_only。"""
        inst_id = uuid.uuid4()
        core_run_id = uuid.uuid4()
        snap_id = uuid.uuid4()

        # 构造 mock StockFeatureSnapshot
        core_snapshot = SimpleNamespace(
            instrument_id=inst_id,
            summary_payload=_make_summary(events=[_bos_event(price=10.0)]),
        )

        # mock publication 查询返回
        pub_mock = SimpleNamespace(data_run_id=core_run_id)

        # mock snapshot 对象（ORM，可写属性）
        captured_snapshots: list[SimpleNamespace] = []

        # db.add 在 SQLAlchemy 中是同步方法（服务调用时不 await），
        # 必须用同步 MagicMock：AsyncMock 会让 db.add(snapshot) 返回协程，
        # 由于服务不 await，_add 永不执行，snapshot.id 不会被赋值。
        def _add(obj: Any) -> None:
            if obj.__class__.__name__ == "AuctionAnchorSnapshot":
                obj.id = snap_id
                captured_snapshots.append(obj)

        async def _flush() -> None:
            return None

        # 构造 db mock
        # execute 调用顺序：
        # 1. publication 查询 → pub_mock
        # 2. _load_core_snapshots → core_snapshot 列表
        # 3. upsert AuctionAnchorItem（all_items 非空时）→ 结果未使用
        db = AsyncMock()
        db.add = MagicMock(side_effect=_add)  # 同步：SQLAlchemy Session.add 不返回协程
        db.flush = AsyncMock(side_effect=_flush)
        db.execute = AsyncMock(side_effect=[
            _MockResult(scalar=pub_mock),  # publication 查询
            _MockResult(scalars_list=[core_snapshot]),  # _load_core_snapshots
            _MockResult(),  # upsert execute（结果未使用）
        ])
        db.get = AsyncMock(return_value=SimpleNamespace(
            adj_factor_hash="qfx_hash_v1",
            adjustment_as_of=None,
        ))
        # _check_chip_consensus_completed 返回 count=0 → False
        # 通过 patch 函数拦截
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.services.auction_anchor_service._check_chip_consensus_completed",
                AsyncMock(return_value=False),
            )
            mp.setattr(
                "app.services.auction_anchor_service._resolve_price_adjustment_version",
                AsyncMock(return_value="qfx_hash_v1"),
            )
            result = await generate_auction_anchors(
                db, _TRADE_DATE, worker_id="w1", lease_epoch=1,
            )

        assert result["status"] == "structure_only"
        assert result["chip_count"] == 0
        assert result["structure_count"] == 1
        assert result["snapshot_id"] == snap_id
        # snapshot 状态写入正确
        assert captured_snapshots[0].status == "structure_only"
        assert captured_snapshots[0].source_chip_run_id is None

    @pytest.mark.asyncio
    async def test_no_publication_returns_failed(self) -> None:
        """无已发布 stock_core pointer → status=failed。"""
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_MockResult(scalar=None))

        result = await generate_auction_anchors(db, _TRADE_DATE)
        assert result["status"] == "failed"
        assert result["snapshot_id"] is None
        assert "no published stock_core pointer" in result["error_message"]


# =============================================================================
# 测试：publish_auction_anchors 版本一致性
# =============================================================================


class TestPublishAuctionAnchors:
    """publish_auction_anchors 校验逻辑。"""

    @pytest.mark.asyncio
    async def test_snapshot_not_found_raises(self) -> None:
        """snapshot 不存在 → AnchorSnapshotNotFoundError。"""
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)

        with pytest.raises(AnchorSnapshotNotFoundError):
            await publish_auction_anchors(db, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_snapshot_running_status_raises(self) -> None:
        """snapshot.status=running → AnchorSnapshotNotReadyError。"""
        snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            status="running",
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            source_core_run_id=uuid.uuid4(),
            source_chip_run_id=None,
            coverage_ratio=0.5,
        )
        db = AsyncMock()
        db.get = AsyncMock(return_value=snapshot)

        with pytest.raises(AnchorSnapshotNotReadyError):
            await publish_auction_anchors(db, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_algorithm_version_mismatch_raises(self) -> None:
        """snapshot.algorithm_version 与当前版本不一致 → AnchorVersionMismatchError。"""
        snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            status="succeeded",
            algorithm_version="v0.9.0",  # 旧版本
            source_core_run_id=uuid.uuid4(),
            source_chip_run_id=None,
            coverage_ratio=0.5,
        )
        db = AsyncMock()
        db.get = AsyncMock(return_value=snapshot)

        with pytest.raises(AnchorVersionMismatchError):
            await publish_auction_anchors(db, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_source_core_run_id_mismatch_raises(self) -> None:
        """snapshot.source_core_run_id 与已发布 stock_core pointer.data_run_id 不一致 →
        AnchorVersionMismatchError（旧 source run 不得发布）。"""
        snapshot_core_run_id = uuid.uuid4()
        current_core_run_id = uuid.uuid4()  # 不同的 run id

        snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            status="succeeded",
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            source_core_run_id=snapshot_core_run_id,
            source_chip_run_id=None,
            coverage_ratio=0.5,
        )
        # mock: 当日已发布的 stock_core pointer 指向另一个 run id
        pub = SimpleNamespace(data_run_id=current_core_run_id)

        db = AsyncMock()
        db.get = AsyncMock(return_value=snapshot)
        db.execute = AsyncMock(return_value=_MockResult(scalar=pub))

        with pytest.raises(AnchorVersionMismatchError) as exc_info:
            await publish_auction_anchors(db, snapshot.id)
        assert "禁止发布" in str(exc_info.value) or "不一致" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_no_current_publication_raises(self) -> None:
        """当日无已发布 stock_core pointer → AnchorVersionMismatchError。"""
        snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            status="succeeded",
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            source_core_run_id=uuid.uuid4(),
            source_chip_run_id=None,
            coverage_ratio=0.5,
        )
        db = AsyncMock()
        db.get = AsyncMock(return_value=snapshot)
        db.execute = AsyncMock(return_value=_MockResult(scalar=None))

        with pytest.raises(AnchorVersionMismatchError):
            await publish_auction_anchors(db, snapshot.id)

    @pytest.mark.asyncio
    async def test_chip_run_id_mismatch_raises(self) -> None:
        """source_chip_run_id 与 source_core_run_id 不一致 → 版本错误。"""
        core_run_id = uuid.uuid4()
        different_chip_run_id = uuid.uuid4()

        snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            status="succeeded",
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            source_core_run_id=core_run_id,
            source_chip_run_id=different_chip_run_id,  # 不一致
            coverage_ratio=0.5,
        )
        pub = SimpleNamespace(data_run_id=core_run_id)

        db = AsyncMock()
        db.get = AsyncMock(return_value=snapshot)
        db.execute = AsyncMock(return_value=_MockResult(scalar=pub))

        with pytest.raises(AnchorVersionMismatchError):
            await publish_auction_anchors(db, snapshot.id)

    @pytest.mark.asyncio
    async def test_zero_coverage_raises(self) -> None:
        """coverage_ratio=0（无活跃锚点）→ AnchorCoverageLowError。"""
        core_run_id = uuid.uuid4()
        snapshot = SimpleNamespace(
            id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            status="succeeded",
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            source_core_run_id=core_run_id,
            source_chip_run_id=core_run_id,
            coverage_ratio=0.0,
        )
        pub = SimpleNamespace(data_run_id=core_run_id)

        db = AsyncMock()
        db.get = AsyncMock(return_value=snapshot)
        db.execute = AsyncMock(return_value=_MockResult(scalar=pub))

        with pytest.raises(AnchorCoverageLowError):
            await publish_auction_anchors(db, snapshot.id)

    @pytest.mark.asyncio
    async def test_successful_publish(self) -> None:
        """正常发布：返回 publication 记录。"""
        core_run_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        snapshot = SimpleNamespace(
            id=snapshot_id,
            trade_date=_TRADE_DATE,
            status="succeeded",
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            source_core_run_id=core_run_id,
            source_chip_run_id=core_run_id,
            coverage_ratio=0.85,
        )
        pub = SimpleNamespace(data_run_id=core_run_id)
        published = SimpleNamespace(
            id=uuid.uuid4(),
            trade_date=_TRADE_DATE,
            snapshot_id=snapshot_id,
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
        )

        db = AsyncMock()
        db.get = AsyncMock(return_value=snapshot)
        # execute 调用顺序：查询 pub → upsert → 读取
        db.execute = AsyncMock(side_effect=[
            _MockResult(scalar=pub),  # publication 查询
            _MockResult(),  # upsert execute（返回值未用）
            _MockResult(scalar=published),  # 读取 publication
        ])
        db.flush = AsyncMock(return_value=None)

        result = await publish_auction_anchors(db, snapshot_id)
        assert result is published


# =============================================================================
# 测试 stub 类
# =============================================================================


class AuctionAnchorItemStub:
    """简化 AuctionAnchorItem mock，仅含被测字段。

    用于测试 _build_composite_anchors / _deduplicate_anchors / _filter_active_anchors，
    避免构造完整 ORM 对象的负担。
    """

    def __init__(
        self,
        anchor_type: str = "structure",
        direction: str = "up",
        lower_price: Decimal | None = None,
        upper_price: Decimal | None = None,
        center_price: Decimal | None = None,
        strength: float = 0.7,
        freshness: str = "fresh",
        instrument_id: uuid.UUID | None = None,
        structure_payload: dict | None = None,
        chip_payload: dict | None = None,
        is_active: bool = True,
        reason_codes: list | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.anchor_type = anchor_type
        self.direction = direction
        self.lower_price = lower_price if lower_price is not None else Decimal("10.0")
        self.upper_price = upper_price if upper_price is not None else Decimal("10.0")
        self.center_price = center_price if center_price is not None else Decimal("10.0")
        self.strength = strength
        self.freshness = freshness
        self.instrument_id = instrument_id or uuid.uuid4()
        self.structure_payload = structure_payload
        self.chip_payload = chip_payload
        self.is_active = is_active
        self.reason_codes = reason_codes or []
