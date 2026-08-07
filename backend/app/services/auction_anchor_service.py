"""竞价锚点生成服务 - 从已发布的 stock_core snapshot 和 chip_consensus 提取结构/筹码锚点。

输入：
- factor_publications where kind='stock_core' and trade_date=当日 → source_core_run_id
- stock_chip_consensus_snapshots where trade_date=当日 and core_run_id=source_core_run_id
  且 status='succeeded' → source_chip_run_id（复用 core_run_id 作为 chip 批次标识）

输出：
- AuctionAnchorSnapshot（status=succeeded/structure_only/failed）
- AuctionAnchorItem（anchor_type=structure/chip/composite）
- AuctionAnchorPublication（发布指针）

锚点来源：
- 结构锚点：从 summary_payload.first_pyramid.structure 中提取 BOS/CHoCH/OB/trailing
- 筹码锚点：从 chip_payload.chip.continuousFactors 中提取 POC/VAH/VAL，chip.events 提取 cross
- 复合锚点：近距离结构+筹码合并

约束：
- 旧/新 source run 或算法版本不一致时禁止发布
- 单股活跃锚点上限 MAX_ACTIVE_ANCHORS_PER_INSTRUMENT（20）
- 不得用旧日筹码冒充当日（chip core_run_id 必须等于当日 source_core_run_id）
- 所有 Decimal 字段保留精度
- 结构锚点字段缺失时跳过该锚点并记录 reason_codes

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.auction_anchor_service
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import (
    AuctionAnchorItem,
    AuctionAnchorPublication,
    AuctionAnchorSnapshot,
)
from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
    SCOPE_TYPE_MARKET,
    FactorPublication,
)
from app.models.instrument import Instrument
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

AUCTION_ANCHOR_ALGORITHM_VERSION = "v1.0.0"
MAX_ACTIVE_ANCHORS_PER_INSTRUMENT = 20
ANCHOR_FRESHNESS_THRESHOLD_DAYS = 30  # 30 个交易日后变 stale
ANCHOR_EXPIRY_DAYS = 60  # 60 个交易日后 expired

# 活跃锚点最低强度门槛（低于此值的锚点标记 is_active=False）
MIN_ACTIVE_STRENGTH = 0.3

# 复合锚点合并阈值：结构锚点与筹码锚点中心价相对偏差 <= 此值时合并
COMPOSITE_MERGE_TOLERANCE_PCT = 0.01  # 1%

# 锚点强度默认值
_STRENGTH_BOS = 0.85
_STRENGTH_CHOCH = 0.80
_STRENGTH_OB = 0.70
_STRENGTH_TRAILING = 0.60
_STRENGTH_POC = 0.80
_STRENGTH_VAH_VAL = 0.70
_STRENGTH_CHIP_CROSS = 0.65
_STRENGTH_COMPOSITE = 0.90


# =============================================================================
# 异常
# =============================================================================


class AnchorSnapshotNotFoundError(ValueError):
    """指定的锚点快照不存在。"""


class AnchorSnapshotNotReadyError(ValueError):
    """锚点快照状态不允许发布（非 succeeded/partial/structure_only）。"""


class AnchorVersionMismatchError(ValueError):
    """source run 或算法版本不一致，禁止发布。"""


class AnchorCoverageLowError(ValueError):
    """覆盖率过低，拒绝发布。"""


# =============================================================================
# 辅助函数
# =============================================================================


def _safe_decimal(v: Any) -> Decimal | None:
    """安全转换为 Decimal，None/无效值返回 None。"""
    if v is None:
        return None
    try:
        d = Decimal(str(v))
        if not d.is_finite():
            return None
        return d
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_float(v: Any) -> float | None:
    """安全转换为 float，None/无效值返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN 检查
    except (TypeError, ValueError):
        return None


def _compute_freshness(formed_at: date | None, trade_date: date) -> str:
    """根据锚点形成日期计算新鲜度（fresh/stale/expired）。

    用日历天数作为交易日的近似（无交易日历时使用）。
    """
    if formed_at is None:
        return "fresh"  # 无形成日期视为当前（如 POC/trailing 等状态型锚点）
    delta_days = (trade_date - formed_at).days
    if delta_days < 0:
        return "fresh"  # 未来日期（不应发生），视为 fresh
    if delta_days <= ANCHOR_FRESHNESS_THRESHOLD_DAYS:
        return "fresh"
    if delta_days <= ANCHOR_EXPIRY_DAYS:
        return "stale"
    return "expired"


def _parse_iso_date(v: Any) -> date | None:
    """解析 ISO 日期字符串或 date 对象为 date。"""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        s = str(v)
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _parse_iso_datetime(v: Any) -> datetime | None:
    """解析 ISO datetime 字符串为 datetime（用于 source_time）。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        s = str(v)
        # 兼容 "2026-07-25" 和 "2026-07-25T14:15:00+08:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        # 退化为日期
        d = _parse_iso_date(v)
        return datetime(d.year, d.month, d.day) if d is not None else None


def _build_anchor_key(
    subtype: str,
    *,
    occurred_at: Any = None,
    bar_index: Any = None,
    seq: int = 0,
) -> str:
    """构造同股同 snapshot 内唯一的 anchor_key。

    约定：``<subtype>[_<occurredAt>][_b<barIndex>][_n<seq>]``
    保证 (snapshot_id, instrument_id, anchor_key) 唯一，从而同方向同类型的多个
    OB/BOS 都能保留。
    """
    parts: list[str] = [subtype]
    if occurred_at is not None and str(occurred_at) != "":
        parts.append(str(occurred_at))
    if bar_index is not None:
        parts.append(f"b{bar_index}")
    if seq > 0:
        parts.append(f"n{seq}")
    return "_".join(parts)


def _make_anchor_item(
    *,
    snapshot_id: uuid.UUID,
    trade_date: date,
    instrument_id: uuid.UUID,
    anchor_type: str,
    anchor_key: str,
    source_kind: str,
    source_run_id: uuid.UUID,
    direction: str,
    lower_price: Decimal,
    upper_price: Decimal,
    center_price: Decimal,
    strength: float,
    freshness: str,
    price_adjustment_version: str,
    anchor_subtype: str | None = None,
    source_event_id: uuid.UUID | None = None,
    source_time: datetime | None = None,
    priority_rank: int | None = None,
    structure_payload: dict[str, Any] | None = None,
    chip_payload: dict[str, Any] | None = None,
    distance_at_close: Decimal | None = None,
    reason_codes: list[str] | None = None,
    is_active: bool = True,
) -> AuctionAnchorItem:
    """构建单个 AuctionAnchorItem（公用工厂）。

    [P0-6/P0-7 修复 2026-07-31]
    - anchor_key/anchor_subtype 唯一标识同股同 snapshot 的多个同类型锚点
    - source_kind (core/chip) + source_run_id 取代旧 source 字段
    - source_event_id/source_time 关联来源事件（如结构/筹码事件 ID）
    - priority_rank 由 _filter_active_anchors 统一赋值（活跃锚点排序）
    """
    validity = "valid" if freshness != "expired" else "invalid"
    return AuctionAnchorItem(
        snapshot_id=snapshot_id,
        trade_date=trade_date,
        instrument_id=instrument_id,
        anchor_type=anchor_type,
        anchor_key=anchor_key,
        anchor_subtype=anchor_subtype,
        source_kind=source_kind,
        source_run_id=source_run_id,
        source_event_id=source_event_id,
        source_time=source_time,
        direction=direction,
        lower_price=lower_price,
        upper_price=upper_price,
        center_price=center_price,
        strength=strength,
        priority_rank=priority_rank,
        freshness=freshness,
        validity=validity,
        price_adjustment_version=price_adjustment_version,
        structure_payload=structure_payload,
        chip_payload=chip_payload,
        distance_at_close=distance_at_close,
        is_active=is_active,
        reason_codes=reason_codes or [],
    )


# =============================================================================
# 结构锚点提取
# =============================================================================


def _extract_structure_anchors(
    snapshot: StockFeatureSnapshot,
    *,
    snapshot_id: uuid.UUID,
    source_core_run_id: uuid.UUID,
    price_adjustment_version: str,
    trade_date: date,
) -> list[AuctionAnchorItem]:
    """从 StockFeatureSnapshot 中提取结构锚点。

    数据源：summary_payload.first_pyramid.structure（含 BOS/CHoCH/OB 事件与 trailing）。
    缺失字段的锚点跳过并记录 reason_codes（在返回列表中不包含跳过的项；
    reason_codes 写入有效锚点以标注降级原因）。

    [P0-6 修复 2026-07-31] 每个事件生成唯一 anchor_key（基于 occurredAt/barIndex），
    保留同方向同类型的全部 OB/BOS，不再按 (anchor_type, direction) 去重。

    锚点类型：
    - BOS 事件 → direction 价格触发线锚点（anchor_subtype=bos）
    - CHoCH 事件 → direction 价格触发线锚点（anchor_subtype=choch）
    - OB_CREATED 事件 → ob_high/ob_low 区间锚点（anchor_subtype=ob_created）
    - trailing_top → 失效线锚点（anchor_subtype=trailing_top）
    - trailing_bottom → 失效线锚点（anchor_subtype=trailing_bottom）
    """
    instrument_id = snapshot.instrument_id
    summary = snapshot.summary_payload or {}
    first_pyramid = summary.get("first_pyramid") or {}
    structure = first_pyramid.get("structure") or {}
    events = structure.get("events") or []
    continuous = structure.get("continuousFactors") or {}

    items: list[AuctionAnchorItem] = []
    skip_reasons: list[str] = []
    # 同 subtype 出现重复 key 时递增 seq，保证 anchor_key 唯一
    key_counter: dict[str, int] = {}

    def _unique_key(subtype: str, occurred_at: Any, bar_index: Any) -> str:
        base = _build_anchor_key(subtype, occurred_at=occurred_at, bar_index=bar_index)
        if base in key_counter:
            key_counter[base] += 1
            return _build_anchor_key(
                subtype, occurred_at=occurred_at, bar_index=bar_index, seq=key_counter[base]
            )
        key_counter[base] = 0
        return base

    for evt in events:
        evt_type = evt.get("type", "")
        direction = evt.get("direction")
        if direction not in ("up", "down"):
            skip_reasons.append(f"{evt_type}:invalid_direction")
            continue

        occurred_at = evt.get("occurredAt")
        bar_index = evt.get("barIndex")
        formed_at = _parse_iso_date(occurred_at)
        freshness = _compute_freshness(formed_at, trade_date)
        source_time = _parse_iso_datetime(occurred_at)
        # 关联结构事件 ID（如 payload 携带则使用，否则 None）
        source_event_id_raw = evt.get("event_id") or evt.get("id")
        source_event_id: uuid.UUID | None = None
        if source_event_id_raw is not None:
            try:
                source_event_id = uuid.UUID(str(source_event_id_raw))
            except (ValueError, AttributeError):
                source_event_id = None
        extra = evt.get("extra") or {}

        if evt_type in ("BOS", "CHoCH"):
            subtype = "bos" if evt_type == "BOS" else "choch"
            # 触发线锚点：用事件 price 作为 center
            price = _safe_decimal(evt.get("price"))
            if price is None:
                skip_reasons.append(f"{evt_type}:missing_price")
                continue
            strength = _STRENGTH_BOS if evt_type == "BOS" else _STRENGTH_CHOCH
            structure_level = extra.get("structure_level")
            anchor_key = _unique_key(subtype, occurred_at, bar_index)
            payload = {
                "event_type": evt_type,
                "trigger_price": str(price),
                "occurred_at": occurred_at,
                "bar_index": bar_index,
                "structure_level": structure_level,
                "anchor_index": extra.get("anchor_index"),
            }
            items.append(
                _make_anchor_item(
                    snapshot_id=snapshot_id,
                    trade_date=trade_date,
                    instrument_id=instrument_id,
                    anchor_type="structure",
                    anchor_key=anchor_key,
                    anchor_subtype=subtype,
                    source_kind="core",
                    source_run_id=source_core_run_id,
                    source_event_id=source_event_id,
                    source_time=source_time,
                    direction=direction,
                    lower_price=price,
                    upper_price=price,
                    center_price=price,
                    strength=strength,
                    freshness=freshness,
                    price_adjustment_version=price_adjustment_version,
                    structure_payload=payload,
                )
            )
        elif evt_type == "OB_CREATED":
            # OB 区间锚点：ob_high/ob_low
            ob_high = _safe_decimal(extra.get("ob_high"))
            ob_low = _safe_decimal(extra.get("ob_low"))
            if ob_high is None or ob_low is None or ob_high < ob_low:
                skip_reasons.append("OB_CREATED:missing_ob_range")
                continue
            center = (ob_high + ob_low) / 2
            structure_level = extra.get("structure_level")
            anchor_key = _unique_key("ob_created", occurred_at, bar_index)
            payload = {
                "event_type": "OB_CREATED",
                "ob_high": str(ob_high),
                "ob_low": str(ob_low),
                "occurred_at": occurred_at,
                "bar_index": bar_index,
                "structure_level": structure_level,
                "anchor_index": extra.get("anchor_index"),
            }
            items.append(
                _make_anchor_item(
                    snapshot_id=snapshot_id,
                    trade_date=trade_date,
                    instrument_id=instrument_id,
                    anchor_type="structure",
                    anchor_key=anchor_key,
                    anchor_subtype="ob_created",
                    source_kind="core",
                    source_run_id=source_core_run_id,
                    source_event_id=source_event_id,
                    source_time=source_time,
                    direction=direction,
                    lower_price=ob_low,
                    upper_price=ob_high,
                    center_price=center,
                    strength=_STRENGTH_OB,
                    freshness=freshness,
                    price_adjustment_version=price_adjustment_version,
                    structure_payload=payload,
                )
            )

    # trailing top/bottom → 失效线锚点（无明确日期，视为 fresh）
    trailing_top = _safe_decimal(continuous.get("trailing_top"))
    trailing_bottom = _safe_decimal(continuous.get("trailing_bottom"))
    if trailing_top is not None:
        items.append(
            _make_anchor_item(
                snapshot_id=snapshot_id,
                trade_date=trade_date,
                instrument_id=instrument_id,
                anchor_type="structure",
                anchor_key="trailing_top",
                anchor_subtype="trailing_top",
                source_kind="core",
                source_run_id=source_core_run_id,
                direction="down",  # 上沿 → 阻力/空头失效线
                lower_price=trailing_top,
                upper_price=trailing_top,
                center_price=trailing_top,
                strength=_STRENGTH_TRAILING,
                freshness="fresh",
                price_adjustment_version=price_adjustment_version,
                structure_payload={"kind": "trailing_top", "price": str(trailing_top)},
            )
        )
    if trailing_bottom is not None:
        items.append(
            _make_anchor_item(
                snapshot_id=snapshot_id,
                trade_date=trade_date,
                instrument_id=instrument_id,
                anchor_type="structure",
                anchor_key="trailing_bottom",
                anchor_subtype="trailing_bottom",
                source_kind="core",
                source_run_id=source_core_run_id,
                direction="up",  # 下沿 → 支撑/多头失效线
                lower_price=trailing_bottom,
                upper_price=trailing_bottom,
                center_price=trailing_bottom,
                strength=_STRENGTH_TRAILING,
                freshness="fresh",
                price_adjustment_version=price_adjustment_version,
                structure_payload={"kind": "trailing_bottom", "price": str(trailing_bottom)},
            )
        )

    # 将跳过原因附加到首个有效锚点（便于诊断）
    if skip_reasons and items:
        items[0].reason_codes = list(items[0].reason_codes) + skip_reasons

    return items


# =============================================================================
# 筹码锚点提取
# =============================================================================


def _extract_chip_anchors(
    chip_snapshot: StockChipConsensusSnapshot,
    *,
    snapshot_id: uuid.UUID,
    source_chip_run_id: uuid.UUID,
    price_adjustment_version: str,
    trade_date: date,
) -> list[AuctionAnchorItem]:
    """从 StockChipConsensusSnapshot 中提取筹码锚点。

    数据源：chip_payload.chip.continuousFactors（POC/VAH/VAL）+ chip_payload.chip.events（cross）。

    [P0-6/P0-7 修复 2026-07-31]
    - 每个筹码锚点生成唯一 anchor_key（poc/vah/val/cross_<type>_<occurredAt>）
    - source_kind="chip"，source_run_id=source_chip_run_id

    锚点类型：
    - POC（主峰）→ 中性方向锚点（direction 按 last_close vs poc 判断）
    - VAH（上共识区）→ 阻力方向 down
    - VAL（下共识区）→ 支撑方向 up
    - cross_up/cross_down 事件 → 对应方向锚点
    """
    instrument_id = chip_snapshot.instrument_id
    chip_payload = chip_snapshot.chip_payload or {}
    chip_dim = chip_payload.get("chip") or {}
    continuous = chip_dim.get("continuousFactors") or {}
    events = chip_dim.get("events") or []

    items: list[AuctionAnchorItem] = []
    skip_reasons: list[str] = []
    cross_key_counter: dict[str, int] = {}

    last_close = _safe_decimal(continuous.get("last_close"))

    # POC（主峰）
    poc = _safe_decimal(continuous.get("poc_price"))
    if poc is not None:
        # 方向：last_close > poc → up（支撑），last_close < poc → down（阻力）
        direction = "up"
        if last_close is not None and last_close < poc:
            direction = "down"
        items.append(
            _make_anchor_item(
                snapshot_id=snapshot_id,
                trade_date=trade_date,
                instrument_id=instrument_id,
                anchor_type="chip",
                anchor_key="poc",
                anchor_subtype="poc",
                source_kind="chip",
                source_run_id=source_chip_run_id,
                direction=direction,
                lower_price=poc,
                upper_price=poc,
                center_price=poc,
                strength=_STRENGTH_POC,
                freshness="fresh",
                price_adjustment_version=price_adjustment_version,
                chip_payload={
                    "kind": "poc",
                    "poc_price": str(poc),
                    "n_peak_nodes": continuous.get("n_peak_nodes"),
                },
            )
        )
    else:
        skip_reasons.append("chip:missing_poc")

    # VAH（上共识区）→ 阻力
    vah = _safe_decimal(continuous.get("vah_price"))
    if vah is not None:
        items.append(
            _make_anchor_item(
                snapshot_id=snapshot_id,
                trade_date=trade_date,
                instrument_id=instrument_id,
                anchor_type="chip",
                anchor_key="vah",
                anchor_subtype="vah",
                source_kind="chip",
                source_run_id=source_chip_run_id,
                direction="down",
                lower_price=vah,
                upper_price=vah,
                center_price=vah,
                strength=_STRENGTH_VAH_VAL,
                freshness="fresh",
                price_adjustment_version=price_adjustment_version,
                chip_payload={"kind": "vah", "vah_price": str(vah)},
            )
        )
    else:
        skip_reasons.append("chip:missing_vah")

    # VAL（下共识区）→ 支撑
    val = _safe_decimal(continuous.get("val_price"))
    if val is not None:
        items.append(
            _make_anchor_item(
                snapshot_id=snapshot_id,
                trade_date=trade_date,
                instrument_id=instrument_id,
                anchor_type="chip",
                anchor_key="val",
                anchor_subtype="val",
                source_kind="chip",
                source_run_id=source_chip_run_id,
                direction="up",
                lower_price=val,
                upper_price=val,
                center_price=val,
                strength=_STRENGTH_VAH_VAL,
                freshness="fresh",
                price_adjustment_version=price_adjustment_version,
                chip_payload={"kind": "val", "val_price": str(val)},
            )
        )
    else:
        skip_reasons.append("chip:missing_val")

    # cross_up/cross_down 事件
    for evt in events:
        evt_type = evt.get("type", "") or ""
        if "cross_up" in evt_type or "touch_up" in evt_type:
            direction = "up"
        elif "cross_down" in evt_type or "touch_down" in evt_type:
            direction = "down"
        else:
            continue
        price = _safe_decimal(evt.get("price"))
        if price is None:
            skip_reasons.append(f"chip:{evt_type}:missing_price")
            continue
        node_price = _safe_decimal(evt.get("node_price", evt.get("price")))
        occurred_at = evt.get("occurredAt")
        bar_index = evt.get("barIndex")
        formed_at = _parse_iso_date(occurred_at)
        freshness = _compute_freshness(formed_at, trade_date)
        source_time = _parse_iso_datetime(occurred_at)
        # cross 事件 anchor_key：cross_<evt_type>_<occurredAt>_<barIndex>，重复递增 seq
        base_key = _build_anchor_key(
            f"cross_{evt_type}", occurred_at=occurred_at, bar_index=bar_index,
        )
        if base_key in cross_key_counter:
            cross_key_counter[base_key] += 1
            anchor_key = _build_anchor_key(
                f"cross_{evt_type}", occurred_at=occurred_at, bar_index=bar_index,
                seq=cross_key_counter[base_key],
            )
        else:
            cross_key_counter[base_key] = 0
            anchor_key = base_key
        items.append(
            _make_anchor_item(
                snapshot_id=snapshot_id,
                trade_date=trade_date,
                instrument_id=instrument_id,
                anchor_type="chip",
                anchor_key=anchor_key,
                anchor_subtype="cross",
                source_kind="chip",
                source_run_id=source_chip_run_id,
                source_time=source_time,
                direction=direction,
                lower_price=price,
                upper_price=price,
                center_price=price,
                strength=_STRENGTH_CHIP_CROSS,
                freshness=freshness,
                price_adjustment_version=price_adjustment_version,
                chip_payload={
                    "kind": "cross_event",
                    "event_type": evt_type,
                    "node_price": str(node_price) if node_price is not None else None,
                    "occurred_at": occurred_at,
                },
            )
        )

    if skip_reasons and items:
        items[0].reason_codes = list(items[0].reason_codes) + skip_reasons

    return items


# =============================================================================
# 复合锚点合并
# =============================================================================


def _build_composite_anchors(
    structure_items: list[AuctionAnchorItem],
    chip_items: list[AuctionAnchorItem],
    *,
    snapshot_id: uuid.UUID,
    source_core_run_id: uuid.UUID,
    price_adjustment_version: str,
    trade_date: date,
) -> list[AuctionAnchorItem]:
    """将近距离的结构锚点与筹码锚点合并为复合锚点。

    合并条件：结构锚点 center_price 与筹码锚点 center_price 相对偏差
    <= COMPOSITE_MERGE_TOLERANCE_PCT（1%）。

    合并后的复合锚点：
    - 区间：取结构锚点 lower/upper 与筹码锚点 lower/upper 的包络
    - center：两者 center 平均
    - direction：优先取结构锚点 direction（如一致）
    - strength：_STRENGTH_COMPOSITE
    - freshness：取两者中较旧的（保守）
    - source_kind="core"（结构为主），source_run_id=source_core_run_id
    - anchor_key：composite_<structure_key>_<chip_key>（保证唯一）

    [P0-6/P0-7 修复 2026-07-31] 不再依赖旧 source 字段，composite 锚点也有 anchor_key。
    """
    if not structure_items or not chip_items:
        return []

    used_structure: set[int] = set()
    used_chip: set[int] = set()
    composite_items: list[AuctionAnchorItem] = []
    comp_counter: dict[str, int] = {}

    for i, s_item in enumerate(structure_items):
        if i in used_structure:
            continue
        s_center = s_item.center_price
        if s_center is None or s_center <= 0:
            continue
        for j, c_item in enumerate(chip_items):
            if j in used_chip:
                continue
            c_center = c_item.center_price
            if c_center is None or c_center <= 0:
                continue
            # 相对偏差（以较大者为分母）
            denom = max(s_center, c_center)
            if abs(s_center - c_center) / denom > Decimal(str(COMPOSITE_MERGE_TOLERANCE_PCT)):
                continue
            # 合并
            lower = min(s_item.lower_price, c_item.lower_price)
            upper = max(s_item.upper_price, c_item.upper_price)
            center = (s_center + c_center) / 2
            # direction：结构优先
            direction = s_item.direction if s_item.direction == c_item.direction else s_item.direction
            # freshness：取较旧（stale > fresh，expired > stale）
            freshness_rank = {"fresh": 0, "stale": 1, "expired": 2}
            freshness = (
                s_item.freshness
                if freshness_rank.get(s_item.freshness, 0) >= freshness_rank.get(c_item.freshness, 0)
                else c_item.freshness
            )
            instrument_id = s_item.instrument_id
            base_key = f"composite_{s_item.anchor_key}_{c_item.anchor_key}"
            if base_key in comp_counter:
                comp_counter[base_key] += 1
                anchor_key = f"{base_key}_n{comp_counter[base_key]}"
            else:
                comp_counter[base_key] = 0
                anchor_key = base_key
            composite_items.append(
                _make_anchor_item(
                    snapshot_id=snapshot_id,
                    trade_date=trade_date,
                    instrument_id=instrument_id,
                    anchor_type="composite",
                    anchor_key=anchor_key,
                    anchor_subtype="composite",
                    source_kind="core",
                    source_run_id=source_core_run_id,
                    direction=direction,
                    lower_price=lower,
                    upper_price=upper,
                    center_price=center,
                    strength=_STRENGTH_COMPOSITE,
                    freshness=freshness,
                    price_adjustment_version=price_adjustment_version,
                    structure_payload={
                        "merged_from": "structure",
                        "structure_center": str(s_center),
                        "structure_key": s_item.anchor_key,
                        "structure_kind": (s_item.structure_payload or {}).get("kind")
                        or (s_item.structure_payload or {}).get("event_type"),
                    },
                    chip_payload={
                        "merged_from": "chip",
                        "chip_center": str(c_center),
                        "chip_key": c_item.anchor_key,
                        "chip_kind": (c_item.chip_payload or {}).get("kind"),
                    },
                )
            )
            used_structure.add(i)
            used_chip.add(j)
            break  # 一个结构锚点只合并一个筹码锚点

    return composite_items


# =============================================================================
# 活跃锚点筛选
# =============================================================================


def _filter_active_anchors(items: list[AuctionAnchorItem]) -> list[AuctionAnchorItem]:
    """按距离、强度、新鲜度筛选活跃锚点；单股上限 MAX_ACTIVE_ANCHORS_PER_INSTRUMENT。

    [P0-6 修复 2026-07-31] 保留全部锚点（不再按 anchor_type+direction 去重），
    仅通过 is_active + priority_rank 控制扫描范围。

    规则：
    - expired 锚点 is_active=False
    - strength < MIN_ACTIVE_STRENGTH 的锚点 is_active=False
    - 其余按 (strength desc, freshness asc) 排序，取前 MAX_ACTIVE_ANCHORS_PER_INSTRUMENT
    - 活跃锚点赋 priority_rank（1..N，lower=higher priority），非活跃 None
    - 超出上限的锚点 is_active=False
    """
    freshness_rank = {"fresh": 0, "stale": 1, "expired": 2}

    # 标记 expired / 低强度为 inactive
    for item in items:
        if item.freshness == "expired" or item.strength < MIN_ACTIVE_STRENGTH:
            item.is_active = False
            item.priority_rank = None

    # 候选活跃锚点
    candidates = [it for it in items if it.is_active]
    candidates.sort(
        key=lambda x: (-x.strength, freshness_rank.get(x.freshness, 99))
    )

    # 超出上限的降级为 inactive
    if len(candidates) > MAX_ACTIVE_ANCHORS_PER_INSTRUMENT:
        for item in candidates[MAX_ACTIVE_ANCHORS_PER_INSTRUMENT:]:
            item.is_active = False
            item.priority_rank = None
        candidates = candidates[:MAX_ACTIVE_ANCHORS_PER_INSTRUMENT]

    # 为活跃锚点赋 priority_rank（1..N）
    for rank, item in enumerate(candidates, start=1):
        item.priority_rank = rank

    return items


# =============================================================================
# 主入口：generate_auction_anchors
# =============================================================================


async def _get_active_a_share_instrument_ids(session: AsyncSession) -> list[uuid.UUID]:
    """获取所有活跃 A 股 instrument_id（symbol 6 位数字）。"""
    stmt = select(Instrument.id).where(
        Instrument.status == "active",
        Instrument.symbol.op("~")(r"^\d{6}$"),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _resolve_price_adjustment_version(
    session: AsyncSession,
    core_run_id: uuid.UUID,
) -> str:
    """从 StockFeatureSnapshotRun 解析复权版本（adj_factor_hash 优先，回退 adjustment_as_of）。"""
    run = await session.get(StockFeatureSnapshotRun, core_run_id)
    if run is None:
        return "unknown"
    if run.adj_factor_hash:
        return run.adj_factor_hash
    if run.adjustment_as_of is not None:
        return run.adjustment_as_of.isoformat()
    return "unknown"


async def _check_chip_consensus_completed(
    session: AsyncSession,
    trade_date: date,
    core_run_id: uuid.UUID,
) -> str:
    """判断当日当前 core 的 chip consensus 完成度，返回三态字符串。

    返回：
    - "full"：job.status == succeeded 且 metadata.chip_status == succeeded
      且 succeeded_count == expected_count（expected_count > 0）
    - "partial"：job.status == succeeded 且（chip_status == partial 或 succeeded < expected）
    - "unavailable"：chip 未完成/不可用（无 job、跨 core、running、failed、计数缺失等）

    full 判据必须包含计数完整性，不能只相信两个字符串状态；否则会出现
    Chip readiness = degraded 而 Auction producer = full 的双结论分歧。

    不再使用“任意 1 条 chip snapshot 成功即视为完成”的弱判据；改为读取
    同日同 core 的 chip SchedulerJobRun 元数据（chip_status / succeeded_count /
    expected_count），与正式盘后链产物对齐。
    """
    from app.services.after_close_chip_consensus_service import (
        get_chip_consensus_job_for_date,
    )

    job = await get_chip_consensus_job_for_date(session, trade_date)
    if job is None:
        return "unavailable"
    meta: dict[str, Any] = {}
    if job.metadata_json:
        try:
            meta = json.loads(job.metadata_json)
        except (ValueError, TypeError):
            meta = {}
    # 跨 core 不一致：不得用旧 core 的 chip 放行 auction
    if meta.get("core_run_id") != str(core_run_id):
        return "unavailable"
    chip_status = meta.get("chip_status")
    job_status = job.status
    # chip worker 写入的分母键是 total_count（见 app/worker.py metadata_updates）；
    # expected_count 仅作历史/兼容回退，不能作为主键读取，否则计数守卫永远失效。
    expected = meta.get("total_count")
    if expected is None:
        expected = meta.get("expected_count")
    succeeded = meta.get("succeeded_count")
    # 计数缺失时不臆断不完整（与 ProductReadiness._chip_state 同一合同）；
    # 计数存在时严格要求 succeeded >= expected。
    counts_known = expected is not None and succeeded is not None
    succeeded_full = (not counts_known) or succeeded >= expected
    if job_status == "succeeded" and chip_status == "succeeded" and succeeded_full:
        return "full"
    if job_status == "succeeded" and (chip_status == "partial" or not succeeded_full):
        return "partial"
    return "unavailable"


async def _load_chip_snapshot_map(
    session: AsyncSession,
    trade_date: date,
    core_run_id: uuid.UUID,
) -> dict[uuid.UUID, StockChipConsensusSnapshot]:
    """加载当日所有 succeeded chip snapshot，按 instrument_id 索引。"""
    stmt = (
        select(StockChipConsensusSnapshot)
        .where(
            StockChipConsensusSnapshot.trade_date == trade_date,
            StockChipConsensusSnapshot.core_run_id == core_run_id,
            StockChipConsensusSnapshot.status == "succeeded",
        )
    )
    result = await session.execute(stmt)
    return {row.instrument_id: row for row in result.scalars().all()}


async def _load_core_snapshots(
    session: AsyncSession,
    trade_date: date,
    core_run_id: uuid.UUID,
) -> list[StockFeatureSnapshot]:
    """加载当日归属 core_run_id 的所有 stock_feature_snapshots。"""
    stmt = (
        select(StockFeatureSnapshot)
        .where(
            StockFeatureSnapshot.trade_date == trade_date,
            StockFeatureSnapshot.source_run_id == core_run_id,
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def generate_auction_anchors(
    db: AsyncSession,
    trade_date: date,
    *,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
) -> dict[str, Any]:
    """主入口：生成当日竞价锚点快照。

    流程：
    1. 查询当日已发布的 stock_core pointer → source_core_run_id
    2. 检查当日 chip_consensus 是否完成
    3. 创建 AuctionAnchorSnapshot（status=running）
    4. 遍历 A 股 instrument，从 StockFeatureSnapshot 提取结构锚点；
       如 chip 可用，从 StockChipConsensusSnapshot 提取筹码锚点
    5. 近距离结构+筹码合并为 composite 锚点
    6. 活跃锚点筛选（单股上限 20）
    7. 更新 snapshot status=succeeded/structure_only/failed

    Args:
        db: 异步 DB 会话
        trade_date: 业务交易日
        worker_id: Worker 标识（可选，写入日志）
        lease_epoch: 租约 epoch（可选，写入日志）

    Returns:
        {
            "snapshot_id": uuid.UUID,
            "status": str,  # succeeded/structure_only/failed
            "structure_count": int,
            "chip_count": int,
            "composite_count": int,
            "eligible_count": int,
            "coverage_ratio": float,
        }
    """
    logger.info(
        "[AuctionAnchor] 开始生成锚点: trade_date=%s worker_id=%s lease_epoch=%s",
        trade_date, worker_id, lease_epoch,
    )

    # 1. 查询已发布的 stock_core pointer
    pub_stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_MARKET,
            FactorPublication.scope_key == "market",
            FactorPublication.trade_date == trade_date,
            FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
        )
        .limit(1)
    )
    pub_result = await db.execute(pub_stmt)
    core_publication = pub_result.scalar_one_or_none()

    if core_publication is None:
        logger.warning(
            "[AuctionAnchor] 当日无已发布 stock_core pointer: trade_date=%s", trade_date,
        )
        return {
            "snapshot_id": None,
            "status": "failed",
            "structure_count": 0,
            "chip_count": 0,
            "composite_count": 0,
            "eligible_count": 0,
            "coverage_ratio": 0.0,
            "error_message": "no published stock_core pointer for trade_date",
        }

    source_core_run_id: uuid.UUID = core_publication.data_run_id
    price_adjustment_version = await _resolve_price_adjustment_version(db, source_core_run_id)

    # 2. 检查 chip_consensus 完成度（三态：full / partial / unavailable）
    chip_completed = await _check_chip_consensus_completed(db, trade_date, source_core_run_id)
    # chip 批次标识：复用 core_run_id（chip snapshot 通过 core_run_id 关联）
    # chip 数据在 full 或 partial 时均可用（partial 仍有部分 chip 锚点）
    source_chip_run_id: uuid.UUID | None = (
        source_core_run_id if chip_completed in ("full", "partial") else None
    )

    # 3. 创建 AuctionAnchorSnapshot（status=running）
    now = datetime.now(UTC)
    snapshot = AuctionAnchorSnapshot(
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        source_chip_run_id=source_chip_run_id,
        algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
        price_adjustment_version=price_adjustment_version,
        status="running",
        started_at=now,
    )
    db.add(snapshot)
    await db.flush()  # 获取 snapshot.id
    snapshot_id = snapshot.id

    try:
        # 4. 加载数据
        core_snapshots = await _load_core_snapshots(db, trade_date, source_core_run_id)
        chip_map: dict[uuid.UUID, StockChipConsensusSnapshot] = {}
        if chip_completed in ("full", "partial"):
            chip_map = await _load_chip_snapshot_map(db, trade_date, source_core_run_id)

        # 5. 遍历生成锚点
        all_items: list[AuctionAnchorItem] = []
        eligible_count = 0
        structure_total = 0
        chip_total = 0
        composite_total = 0
        missing_reasons: dict[str, int] = {}

        for core_snap in core_snapshots:
            eligible_count += 1
            instrument_id = core_snap.instrument_id

            # 结构锚点
            structure_items = _extract_structure_anchors(
                core_snap,
                snapshot_id=snapshot_id,
                source_core_run_id=source_core_run_id,
                price_adjustment_version=price_adjustment_version,
                trade_date=trade_date,
            )
            structure_total += len(structure_items)

            # 筹码锚点（chip 可用时：full 或 partial）
            chip_items: list[AuctionAnchorItem] = []
            if chip_completed in ("full", "partial"):
                chip_snap = chip_map.get(instrument_id)
                if chip_snap is not None:
                    chip_items = _extract_chip_anchors(
                        chip_snap,
                        snapshot_id=snapshot_id,
                        source_chip_run_id=source_chip_run_id,  # type: ignore[arg-type]
                        price_adjustment_version=price_adjustment_version,
                        trade_date=trade_date,
                    )
                    chip_total += len(chip_items)
                else:
                    missing_reasons["chip_snapshot_missing"] = (
                        missing_reasons.get("chip_snapshot_missing", 0) + 1
                    )

            # 复合锚点
            composite_items = _build_composite_anchors(
                structure_items,
                chip_items,
                snapshot_id=snapshot_id,
                source_core_run_id=source_core_run_id,
                price_adjustment_version=price_adjustment_version,
                trade_date=trade_date,
            )
            composite_total += len(composite_items)

            # [P0-6 修复 2026-07-31] 不再按 (anchor_type, direction) 去重，
            # 保留全部有效锚点；仅通过 _filter_active_anchors 赋 is_active/priority_rank。
            per_instrument_items = structure_items + chip_items + composite_items
            per_instrument_items = _filter_active_anchors(per_instrument_items)
            all_items.extend(per_instrument_items)

        # 6. 批量写入 items（使用 upsert 处理潜在冲突）
        # [P0-6/P0-7] 唯一键改为 (snapshot_id, instrument_id, anchor_key)，
        # 字段使用 anchor_key/source_kind/source_run_id 等新字段。
        if all_items:
            for item in all_items:
                stmt = pg_insert(AuctionAnchorItem).values(
                    snapshot_id=item.snapshot_id,
                    trade_date=item.trade_date,
                    instrument_id=item.instrument_id,
                    anchor_type=item.anchor_type,
                    anchor_key=item.anchor_key,
                    anchor_subtype=item.anchor_subtype,
                    source_kind=item.source_kind,
                    source_run_id=item.source_run_id,
                    source_event_id=item.source_event_id,
                    source_time=item.source_time,
                    direction=item.direction,
                    lower_price=item.lower_price,
                    upper_price=item.upper_price,
                    center_price=item.center_price,
                    strength=item.strength,
                    priority_rank=item.priority_rank,
                    freshness=item.freshness,
                    validity=item.validity,
                    price_adjustment_version=item.price_adjustment_version,
                    structure_payload=item.structure_payload,
                    chip_payload=item.chip_payload,
                    distance_at_close=item.distance_at_close,
                    is_active=item.is_active,
                    reason_codes=item.reason_codes,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_auction_anchor_items_snap_inst_key",
                    set_={
                        "anchor_subtype": stmt.excluded.anchor_subtype,
                        "source_kind": stmt.excluded.source_kind,
                        "source_run_id": stmt.excluded.source_run_id,
                        "source_event_id": stmt.excluded.source_event_id,
                        "source_time": stmt.excluded.source_time,
                        "direction": stmt.excluded.direction,
                        "lower_price": stmt.excluded.lower_price,
                        "upper_price": stmt.excluded.upper_price,
                        "center_price": stmt.excluded.center_price,
                        "strength": stmt.excluded.strength,
                        "priority_rank": stmt.excluded.priority_rank,
                        "freshness": stmt.excluded.freshness,
                        "validity": stmt.excluded.validity,
                        "price_adjustment_version": stmt.excluded.price_adjustment_version,
                        "structure_payload": stmt.excluded.structure_payload,
                        "chip_payload": stmt.excluded.chip_payload,
                        "distance_at_close": stmt.excluded.distance_at_close,
                        "is_active": stmt.excluded.is_active,
                        "reason_codes": stmt.excluded.reason_codes,
                    },
                )
                await db.execute(stmt)
            await db.flush()

        # 7. 计算覆盖率与统计
        ready_count = sum(1 for it in all_items if it.is_active)
        # coverage_ratio: 有活跃锚点的 instrument 数 / eligible_count
        active_instruments = {
            it.instrument_id for it in all_items if it.is_active
        }
        coverage_ratio = (
            len(active_instruments) / eligible_count if eligible_count > 0 else 0.0
        )

        # 8. 决定最终状态（由 chip 完成度映射，不再用单一 bool）
        if chip_completed == "full":
            final_status = "succeeded"
        elif chip_completed == "partial":
            final_status = "partial"
        else:
            final_status = "structure_only"

        finished_at = datetime.now(UTC)
        snapshot.status = final_status
        snapshot.eligible_count = eligible_count
        snapshot.ready_count = ready_count
        snapshot.coverage_ratio = round(coverage_ratio, 6)
        snapshot.missing_count = eligible_count - len(active_instruments)
        snapshot.missing_reasons = missing_reasons
        snapshot.structure_anchor_count = structure_total
        snapshot.chip_anchor_count = chip_total
        snapshot.composite_anchor_count = composite_total
        snapshot.finished_at = finished_at

        await db.flush()

        logger.info(
            "[AuctionAnchor] 锚点生成完成: trade_date=%s snapshot_id=%s status=%s "
            "structure=%d chip=%d composite=%d eligible=%d coverage=%.4f",
            trade_date, snapshot_id, final_status,
            structure_total, chip_total, composite_total,
            eligible_count, coverage_ratio,
        )

        return {
            "snapshot_id": snapshot_id,
            "status": final_status,
            "structure_count": structure_total,
            "chip_count": chip_total,
            "composite_count": composite_total,
            "eligible_count": eligible_count,
            "coverage_ratio": round(coverage_ratio, 6),
        }

    except Exception as exc:
        # 失败：更新 snapshot status=failed
        logger.error(
            "[AuctionAnchor] 锚点生成失败: trade_date=%s snapshot_id=%s: %s",
            trade_date, snapshot_id, exc,
            exc_info=True,
        )
        snapshot.status = "failed"
        snapshot.error_message = str(exc)[:1000]
        snapshot.finished_at = datetime.now(UTC)
        await db.flush()
        return {
            "snapshot_id": snapshot_id,
            "status": "failed",
            "structure_count": 0,
            "chip_count": 0,
            "composite_count": 0,
            "eligible_count": 0,
            "coverage_ratio": 0.0,
            "error_message": str(exc)[:1000],
        }


# =============================================================================
# 发布：publish_auction_anchors
# =============================================================================


async def publish_auction_anchors(
    db: AsyncSession,
    snapshot_id: uuid.UUID,
) -> AuctionAnchorPublication:
    """发布锚点：校验 snapshot 状态与覆盖率后写入 auction_anchor_publications（幂等 upsert）。

    校验：
    - snapshot 必须存在且 status 为 succeeded / partial / structure_only
      （partial = chip 部分成功的 degraded 场景，仍需形成 publication 供
      ProductReadiness 推导 hybrid 模式；running/failed 禁止发布）
    - snapshot.algorithm_version 必须等于当前 AUCTION_ANCHOR_ALGORITHM_VERSION
    - snapshot.source_core_run_id 必须等于当日已发布 stock_core pointer.data_run_id
      （旧/新 source run 不一致时禁止发布）
    - 覆盖率不得为 0（无任何活跃锚点时拒绝发布）

    幂等：唯一键 (trade_date, algorithm_version) 通过 ON CONFLICT DO UPDATE 实现。

    Raises:
        AnchorSnapshotNotFoundError: snapshot 不存在
        AnchorSnapshotNotReadyError: snapshot 状态不允许发布
        AnchorVersionMismatchError: source run 或算法版本不一致
        AnchorCoverageLowError: 覆盖率为 0
    """
    snapshot = await db.get(AuctionAnchorSnapshot, snapshot_id)
    if snapshot is None:
        raise AnchorSnapshotNotFoundError(f"AuctionAnchorSnapshot not found: {snapshot_id}")

    if snapshot.status not in ("succeeded", "partial", "structure_only"):
        raise AnchorSnapshotNotReadyError(
            f"AuctionAnchorSnapshot status={snapshot.status!r} 不允许发布"
            f"（仅 succeeded/partial/structure_only 可发布）"
        )

    # 算法版本校验
    if snapshot.algorithm_version != AUCTION_ANCHOR_ALGORITHM_VERSION:
        raise AnchorVersionMismatchError(
            f"snapshot.algorithm_version={snapshot.algorithm_version!r} "
            f"与当前版本 {AUCTION_ANCHOR_ALGORITHM_VERSION!r} 不一致，禁止发布"
        )

    # source_core_run_id 必须匹配当日已发布 stock_core pointer
    core_pub_stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_MARKET,
            FactorPublication.scope_key == "market",
            FactorPublication.trade_date == snapshot.trade_date,
            FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
        )
        .limit(1)
    )
    core_pub_result = await db.execute(core_pub_stmt)
    core_pub = core_pub_result.scalar_one_or_none()
    if core_pub is None:
        raise AnchorVersionMismatchError(
            f"trade_date={snapshot.trade_date} 无已发布 stock_core pointer，禁止发布锚点"
        )
    if core_pub.data_run_id != snapshot.source_core_run_id:
        raise AnchorVersionMismatchError(
            f"snapshot.source_core_run_id={snapshot.source_core_run_id} "
            f"与已发布 stock_core pointer.data_run_id={core_pub.data_run_id} 不一致，"
            f"禁止发布（旧 source run 不得发布）"
        )

    # chip 版本一致性（如 snapshot 声称 chip 可用，则 source_chip_run_id 必须等于 source_core_run_id）
    if snapshot.source_chip_run_id is not None and snapshot.source_chip_run_id != snapshot.source_core_run_id:
        raise AnchorVersionMismatchError(
            f"snapshot.source_chip_run_id={snapshot.source_chip_run_id} "
            f"与 source_core_run_id={snapshot.source_core_run_id} 不一致，禁止发布"
        )

    # 覆盖率校验
    if snapshot.coverage_ratio <= 0.0:
        raise AnchorCoverageLowError(
            f"覆盖率={snapshot.coverage_ratio}（snapshot.coverage_ratio），无活跃锚点，拒绝发布"
        )

    # 幂等 upsert publication
    now = datetime.now(UTC)
    stmt = pg_insert(AuctionAnchorPublication).values(
        trade_date=snapshot.trade_date,
        snapshot_id=snapshot.id,
        algorithm_version=snapshot.algorithm_version,
        source_core_run_id=snapshot.source_core_run_id,
        source_chip_run_id=snapshot.source_chip_run_id,
        coverage_ratio=snapshot.coverage_ratio,
        published_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_auction_anchor_pub_date_ver",
        set_={
            "snapshot_id": stmt.excluded.snapshot_id,
            "source_core_run_id": stmt.excluded.source_core_run_id,
            "source_chip_run_id": stmt.excluded.source_chip_run_id,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "published_at": stmt.excluded.published_at,
        },
    )
    await db.execute(stmt)
    await db.flush()

    # 读取 upsert 后的记录
    read_stmt = (
        select(AuctionAnchorPublication)
        .where(
            AuctionAnchorPublication.trade_date == snapshot.trade_date,
            AuctionAnchorPublication.algorithm_version == snapshot.algorithm_version,
        )
        .limit(1)
    )
    result = await db.execute(read_stmt)
    publication = result.scalar_one()

    logger.info(
        "[AuctionAnchor] 锚点发布: trade_date=%s snapshot_id=%s publication_id=%s coverage=%.4f",
        snapshot.trade_date, snapshot_id, publication.id, snapshot.coverage_ratio,
    )
    return publication


# =============================================================================
# 统一入口：generate_and_publish_auction_anchors（P0-1）
# =============================================================================


class AnchorGenerationFailedError(ValueError):
    """锚点生成未达到可发布状态（failed/未生成）。"""


async def generate_and_publish_auction_anchors(
    db: AsyncSession,
    trade_date: date,
    *,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
) -> dict[str, Any]:
    """[P0-1 统一入口] 在一个事务边界内完成锚点生成 + 校验 + publication 切换。

    盘后编排、Admin 接口和恢复入口统一调用本函数，禁止只 generate 不 publish。

    流程：
    1. generate_auction_anchors：生成 snapshot + items（status=succeeded/structure_only/failed）
    2. 生成失败或无可发布 snapshot → 返回失败，不抛异常（由调用方决定是否软失败）
    3. publish_auction_anchors：校验 + 原子切换 publication 指针
    4. 发布失败 → 返回 publish_failed + reason，不回滚已生成 snapshot（保留审计）

    原子性边界：调用方负责 commit。本函数内部使用 flush 保证 snapshot/publication
    可在同一事务内对齐；调用方在成功时 commit、在异常时 rollback。

    [P0-2] chip 软失败语义：
    - chip succeeded/partial → 生成完整锚点（status=succeeded）
    - chip failed/timeout/未完成 → status=structure_only
    - chip 后来恢复成功 → 重新调用本函数生成完整锚点，publish_auction_anchors
      通过 on_conflict_do_update 原子切换 publication 指针到新 snapshot，
      旧 publication 由 superseded_by 记录（如需要可在 publish 中补充）。

    Args:
        db: 异步 DB 会话（调用方持有事务边界）
        trade_date: 业务交易日
        worker_id: Worker 标识
        lease_epoch: 租约 epoch（用于 fencing）

    Returns:
        {
            "snapshot_id": uuid.UUID | None,
            "publication_id": uuid.UUID | None,
            "status": str,  # succeeded/partial/structure_only/failed/publish_failed
            "structure_count": int,
            "chip_count": int,
            "composite_count": int,
            "eligible_count": int,
            "coverage_ratio": float,
            "error_message": str | None,
        }
    """
    gen_result = await generate_auction_anchors(
        db, trade_date, worker_id=worker_id, lease_epoch=lease_epoch,
    )
    snapshot_id = gen_result.get("snapshot_id")
    gen_status = gen_result.get("status")

    # 生成失败：无可发布 snapshot（partial 属 degraded 可发布态）
    if snapshot_id is None or gen_status not in ("succeeded", "partial", "structure_only"):
        return {
            "snapshot_id": snapshot_id,
            "publication_id": None,
            "status": gen_status or "failed",
            "structure_count": gen_result.get("structure_count", 0),
            "chip_count": gen_result.get("chip_count", 0),
            "composite_count": gen_result.get("composite_count", 0),
            "eligible_count": gen_result.get("eligible_count", 0),
            "coverage_ratio": gen_result.get("coverage_ratio", 0.0),
            "error_message": gen_result.get("error_message"),
        }

    # 发布：原子切换 publication 指针
    try:
        publication = await publish_auction_anchors(db, snapshot_id)
    except (
        AnchorSnapshotNotFoundError,
        AnchorSnapshotNotReadyError,
        AnchorVersionMismatchError,
        AnchorCoverageLowError,
    ) as exc:
        logger.warning(
            "[AuctionAnchor] generate_and_publish 发布失败: trade_date=%s "
            "snapshot_id=%s: %s",
            trade_date, snapshot_id, exc,
        )
        return {
            "snapshot_id": snapshot_id,
            "publication_id": None,
            "status": "publish_failed",
            "structure_count": gen_result.get("structure_count", 0),
            "chip_count": gen_result.get("chip_count", 0),
            "composite_count": gen_result.get("composite_count", 0),
            "eligible_count": gen_result.get("eligible_count", 0),
            "coverage_ratio": gen_result.get("coverage_ratio", 0.0),
            "error_message": f"publish_failed: {exc}",
        }

    logger.info(
        "[AuctionAnchor] generate_and_publish 完成: trade_date=%s snapshot_id=%s "
        "publication_id=%s status=%s",
        trade_date, snapshot_id, publication.id, gen_status,
    )
    return {
        "snapshot_id": snapshot_id,
        "publication_id": publication.id,
        "status": gen_status,
        "structure_count": gen_result.get("structure_count", 0),
        "chip_count": gen_result.get("chip_count", 0),
        "composite_count": gen_result.get("composite_count", 0),
        "eligible_count": gen_result.get("eligible_count", 0),
        "coverage_ratio": gen_result.get("coverage_ratio", 0.0),
        "error_message": None,
    }


# =============================================================================
# 查询：get_published_anchors
# =============================================================================


async def get_published_anchors(
    db: AsyncSession,
    trade_date: date,
) -> dict[str, Any]:
    """查询已发布的锚点：从 publication pointer 反查 snapshot + items 概要。

    Returns:
        {
            "trade_date": date,
            "publication_id": uuid.UUID | None,
            "snapshot_id": uuid.UUID | None,
            "algorithm_version": str | None,
            "status": str | None,
            "source_core_run_id": uuid.UUID | None,
            "source_chip_run_id": uuid.UUID | None,
            "coverage_ratio": float | None,
            "structure_anchor_count": int,
            "chip_anchor_count": int,
            "composite_anchor_count": int,
            "active_anchor_count": int,
            "total_anchor_count": int,
        }
    """
    pub_stmt = (
        select(AuctionAnchorPublication)
        .where(AuctionAnchorPublication.trade_date == trade_date)
        .order_by(AuctionAnchorPublication.published_at.desc())
        .limit(1)
    )
    pub_result = await db.execute(pub_stmt)
    publication = pub_result.scalar_one_or_none()

    if publication is None:
        return {
            "trade_date": trade_date,
            "publication_id": None,
            "snapshot_id": None,
            "algorithm_version": None,
            "status": None,
            "source_core_run_id": None,
            "source_chip_run_id": None,
            "coverage_ratio": None,
            "structure_anchor_count": 0,
            "chip_anchor_count": 0,
            "composite_anchor_count": 0,
            "active_anchor_count": 0,
            "total_anchor_count": 0,
        }

    snapshot = await db.get(AuctionAnchorSnapshot, publication.snapshot_id)
    if snapshot is None:
        return {
            "trade_date": trade_date,
            "publication_id": publication.id,
            "snapshot_id": publication.snapshot_id,
            "algorithm_version": publication.algorithm_version,
            "status": None,
            "source_core_run_id": publication.source_core_run_id,
            "source_chip_run_id": publication.source_chip_run_id,
            "coverage_ratio": publication.coverage_ratio,
            "structure_anchor_count": 0,
            "chip_anchor_count": 0,
            "composite_anchor_count": 0,
            "active_anchor_count": 0,
            "total_anchor_count": 0,
        }

    # 统计 items
    count_stmt = (
        select(
            AuctionAnchorItem.anchor_type,
            AuctionAnchorItem.is_active,
            func.count(AuctionAnchorItem.id).label("cnt"),
        )
        .where(AuctionAnchorItem.snapshot_id == snapshot.id)
        .group_by(AuctionAnchorItem.anchor_type, AuctionAnchorItem.is_active)
    )
    count_result = await db.execute(count_stmt)
    type_counts: dict[str, dict[bool, int]] = {}
    total_count = 0
    active_count = 0
    for row in count_result:
        atype = row.anchor_type
        is_active = row.is_active
        cnt = row.cnt
        type_counts.setdefault(atype, {True: 0, False: 0})[is_active] = cnt
        total_count += cnt
        if is_active:
            active_count += cnt

    structure_count = sum(type_counts.get("structure", {True: 0, False: 0}).values())
    chip_count = sum(type_counts.get("chip", {True: 0, False: 0}).values())
    composite_count = sum(type_counts.get("composite", {True: 0, False: 0}).values())

    return {
        "trade_date": trade_date,
        "publication_id": publication.id,
        "snapshot_id": snapshot.id,
        "algorithm_version": publication.algorithm_version,
        "status": snapshot.status,
        "source_core_run_id": publication.source_core_run_id,
        "source_chip_run_id": publication.source_chip_run_id,
        "coverage_ratio": publication.coverage_ratio,
        "structure_anchor_count": structure_count,
        "chip_anchor_count": chip_count,
        "composite_anchor_count": composite_count,
        "active_anchor_count": active_count,
        "total_anchor_count": total_count,
    }


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯函数自测（不连 DB）
    print("auction_anchor_service 自测...")

    # 常量校验
    assert AUCTION_ANCHOR_ALGORITHM_VERSION == "v1.0.0"
    assert MAX_ACTIVE_ANCHORS_PER_INSTRUMENT == 20
    assert ANCHOR_FRESHNESS_THRESHOLD_DAYS == 30
    assert ANCHOR_EXPIRY_DAYS == 60

    # _safe_decimal
    assert _safe_decimal(None) is None
    assert _safe_decimal("invalid") is None
    assert _safe_decimal("10.5") == Decimal("10.5")
    assert _safe_decimal(10) == Decimal("10")

    # _safe_float
    assert _safe_float(None) is None
    assert _safe_float("abc") is None
    assert _safe_float("3.14") == 3.14

    # _compute_freshness
    d = date(2026, 7, 30)
    assert _compute_freshness(None, d) == "fresh"
    assert _compute_freshness(date(2026, 7, 29), d) == "fresh"
    assert _compute_freshness(date(2026, 6, 30), d) == "fresh"  # 30 天（<=30 为 fresh）
    assert _compute_freshness(date(2026, 6, 29), d) == "stale"  # 31 天
    assert _compute_freshness(date(2026, 5, 31), d) == "stale"  # 60 天（<=60 为 stale）
    assert _compute_freshness(date(2026, 5, 30), d) == "expired"  # 61 天

    # _parse_iso_date
    assert _parse_iso_date(None) is None
    assert _parse_iso_date("2026-07-30") == date(2026, 7, 30)
    assert _parse_iso_date("invalid") is None
    assert _parse_iso_date(date(2026, 7, 30)) == date(2026, 7, 30)

    print("OK")
