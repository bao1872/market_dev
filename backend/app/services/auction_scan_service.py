"""竞价扫描服务 - 基于已冻结锚点分析次日最终竞价价格相对锚点的位置迁移和事件。

输入：
- 已发布的 auction_anchor_publications（通过 get_published_anchors 获取）
- 当日 BarMinute 9:25 数据（最终竞价价/量/额）
- 前一日 BarDaily.close（prev_close）
- 过去 20 个交易日的 BarDaily（ATR / 趋势背景）
- 过去 20 个交易日的 BarMinute 9:25（竞价额中位数与分位）

输出：
- AuctionScanRun（status=succeeded/failed/partial）
- AuctionInstrumentResult（每股一条，含位置/事件/参与度/趋势标签）
- AuctionEventTracking（事件追踪，lifecycle=formed）

约束：
- 锚点未发布 → 抛 AnchorNotPublishedError
- 9:25 bar 缺失 → 标记 missing，记录 reason_codes，仍写一条 result
- 所有比例在 detail_payload 中同时返回分子和分母
- 使用 async/await + AsyncSession
- ATR 使用 app.strategy_assets.algorithms.features.atr_utils.compute_atr（Pine RMA）

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.auction_scan_service
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import (
    AuctionAnchorItem,
    AuctionAnchorSnapshot,
    AuctionEventTracking,
    AuctionInstrumentResult,
    AuctionScanRun,
)
from app.models.bar import BarDaily, BarMinute
from app.models.instrument import Instrument
from app.services.auction_anchor_service import get_published_anchors
from app.strategy_assets.algorithms.features.atr_utils import compute_atr

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

AUCTION_SCAN_ALGORITHM_VERSION = "v1.0.0"
AUCTION_FINAL_TIME = "09:25"  # 最终竞价时间
OPENING_VERIFY_WINDOW_MINUTES = 30  # 开盘后验证窗口
PARTICIPATION_PERCENTILE_LOW = 20  # 偏低分位
PARTICIPATION_PERCENTILE_HIGH = 80  # 偏高分位
PARTICIPATION_PERCENTILE_ABNORMAL_LOW = 5
PARTICIPATION_PERCENTILE_ABNORMAL_HIGH = 95

HISTORY_LOOKBACK_DAYS = 20  # 竞价额中位数与 ATR 计算窗口
ATR_LENGTH = 14  # ATR 周期（Pine RMA）

# 涨跌停阈值（含容差，A 股 ±10%，ST ±5%，保守用 9.9%）
LIMIT_UP_THRESHOLD = 9.9
LIMIT_DOWN_THRESHOLD = -9.9

# 复权因子变化阈值（超过则判定为除权除息）
EX_RIGHT_ADJ_FACTOR_CHANGE_THRESHOLD = 0.001

# 开盘窗口时间
OPENING_TIME = time(9, 30)


# =============================================================================
# 异常
# =============================================================================


class AnchorNotPublishedError(ValueError):
    """指定交易日锚点未发布。"""


class AnchorExpiredError(ValueError):
    """锚点快照已过期，禁止扫描。"""


# =============================================================================
# 辅助函数（纯函数）
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


def _compute_change_pct(
    price: Decimal | None, prev_close: Decimal | None
) -> tuple[float | None, dict[str, str | None]]:
    """计算涨跌幅（%）。

    Returns:
        (change_pct, components) — components 含分子和分母。
    """
    if price is None or prev_close is None or prev_close == 0:
        return None, {"price": str(price) if price else None, "prev_close": str(prev_close) if prev_close else None}
    pct = (float(price) - float(prev_close)) / float(prev_close) * 100.0
    return pct, {"price": str(price), "prev_close": str(prev_close)}


def _compute_relative_volume_median(
    current_amount: Decimal | None,
    history_amounts: list[Decimal],
) -> tuple[float | None, dict[str, Any]]:
    """计算相对 20 日竞价额中位数（倍数）。

    Returns:
        (ratio, components) — ratio = current / median，components 含分子和分母。
    """
    valid = [float(a) for a in history_amounts if a is not None and float(a) > 0]
    if current_amount is None or not valid:
        return None, {
            "current_amount": str(current_amount) if current_amount else None,
            "median_20d": None,
            "history_count": len(valid),
        }
    sorted_amounts = sorted(valid)
    mid = len(sorted_amounts) // 2
    if len(sorted_amounts) % 2 == 0:
        median = (sorted_amounts[mid - 1] + sorted_amounts[mid]) / 2.0
    else:
        median = sorted_amounts[mid]
    if median == 0:
        return None, {
            "current_amount": str(current_amount),
            "median_20d": "0",
            "history_count": len(valid),
        }
    ratio = float(current_amount) / median
    return ratio, {
        "current_amount": str(current_amount),
        "median_20d": str(Decimal(str(median))),
        "history_count": len(valid),
    }


def _compute_volume_percentile(
    current_amount: Decimal | None,
    history_amounts: list[Decimal],
) -> tuple[float | None, dict[str, Any]]:
    """计算当前竞价额在 20 日竞价额中的分位（0-100）。

    Returns:
        (percentile, components) — components 含分子和分母。
    """
    valid = [float(a) for a in history_amounts if a is not None and float(a) > 0]
    if current_amount is None or not valid:
        return None, {
            "current_amount": str(current_amount) if current_amount else None,
            "history_count": len(valid),
        }
    current = float(current_amount)
    below = sum(1 for a in valid if a < current)
    percentile = (below / len(valid)) * 100.0
    return percentile, {
        "current_amount": str(current_amount),
        "history_count": len(valid),
        "below_count": below,
    }


def _extract_ohlc_arrays(history: list[BarDaily]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """从历史日线 bars 提取 OHLC 数组（按时间升序，最旧在前）。"""
    bars = [
        b for b in reversed(history)
        if b.high is not None and b.low is not None and b.close is not None
    ]
    if not bars:
        return np.array([]), np.array([]), np.array([])
    highs = np.array([float(b.high) for b in bars])
    lows = np.array([float(b.low) for b in bars])
    closes = np.array([float(b.close) for b in bars])
    return highs, lows, closes


def _compute_atr_distance(
    price: Decimal | None,
    prev_close: Decimal | None,
    history: list[BarDaily],
) -> tuple[float | None, dict[str, Any]]:
    """计算 ATR 标准化距离（价格相对 prev_close 的距离 / ATR）。

    Returns:
        (atr_multiple, components) — components 含分子（distance）和分母（atr）。
    """
    if price is None or prev_close is None:
        return None, {"distance": None, "atr": None}
    highs, lows, closes = _extract_ohlc_arrays(history)
    if len(highs) < ATR_LENGTH:
        return None, {"distance": None, "atr": None, "reason": "insufficient_history"}
    atr_array = compute_atr(highs, lows, closes, length=ATR_LENGTH)
    atr = atr_array[-1]
    if atr is None or np.isnan(atr) or atr <= 0:
        return None, {"distance": None, "atr": None, "reason": "atr_unavailable"}
    distance = abs(float(price) - float(prev_close))
    return distance / atr, {
        "distance": str(Decimal(str(distance))),
        "atr": str(Decimal(str(atr))),
        "price": str(price),
        "prev_close": str(prev_close),
    }


def _detect_ex_right(history: list[BarDaily]) -> bool:
    """检测近期除权除息：比较最近两日的 adj_factor 变化。"""
    if len(history) < 2:
        return False
    recent_adj = history[0].adj_factor  # 最近一日（前收日）
    prev_adj = history[1].adj_factor  # 前一日
    if recent_adj is None or prev_adj is None:
        return False
    try:
        prev_f = float(prev_adj)
        recent_f = float(recent_adj)
        if prev_f == 0:
            return False
        change = abs(recent_f - prev_f) / prev_f
        return change > EX_RIGHT_ADJ_FACTOR_CHANGE_THRESHOLD
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def _detect_limits(change_pct: float | None) -> tuple[bool, bool]:
    """检测涨跌停。

    Returns:
        (is_limit_up, is_limit_down)
    """
    if change_pct is None:
        return False, False
    return change_pct >= LIMIT_UP_THRESHOLD, change_pct <= LIMIT_DOWN_THRESHOLD


# =============================================================================
# 分类函数（纯函数）
# =============================================================================


def _classify_structure_position(
    price: Decimal | None,
    anchors: list[AuctionAnchorItem],
) -> str | None:
    """基于结构锚点识别价格位置。

    分类：
    - below_low: 价格低于最低支撑锚点下沿
    - below_trigger: 价格跌破 BOS/CHoCH 支撑触发线（但未跌破所有支撑）
    - demand_ob: 价格在需求 OB 区间内（up direction OB）
    - normal: 价格在正常区间（支撑之上、阻力之下，不在 OB 内）
    - supply_ob: 价格在供应 OB 区间内（down direction OB）
    - above_trigger: 价格突破 BOS/CHoCH 阻力触发线（但未突破所有阻力）
    - above_high: 价格高于最高阻力锚点上沿
    """
    if price is None or not anchors:
        return None

    structure_anchors = [a for a in anchors if a.anchor_type == "structure"]
    if not structure_anchors:
        return None

    # 按方向和类型分类
    demand_obs: list[AuctionAnchorItem] = []
    supply_obs: list[AuctionAnchorItem] = []
    support_triggers: list[AuctionAnchorItem] = []  # BOS/CHoCH up
    resistance_triggers: list[AuctionAnchorItem] = []  # BOS/CHoCH down
    trailing_bottoms: list[AuctionAnchorItem] = []
    trailing_tops: list[AuctionAnchorItem] = []

    for a in structure_anchors:
        payload = a.structure_payload or {}
        kind = payload.get("kind") or payload.get("event_type", "")
        if a.direction == "up":
            if "OB" in kind:
                demand_obs.append(a)
            elif "trailing" in kind:
                trailing_bottoms.append(a)
            else:
                support_triggers.append(a)
        else:  # down
            if "OB" in kind:
                supply_obs.append(a)
            elif "trailing" in kind:
                trailing_tops.append(a)
            else:
                resistance_triggers.append(a)

    # 1. 检查 OB 区间包含
    for ob in demand_obs:
        if ob.lower_price is not None and ob.upper_price is not None:
            if ob.lower_price <= price <= ob.upper_price:
                return "demand_ob"
    for ob in supply_obs:
        if ob.lower_price is not None and ob.upper_price is not None:
            if ob.lower_price <= price <= ob.upper_price:
                return "supply_ob"

    # 所有支撑/阻力锚点
    all_supports = demand_obs + support_triggers + trailing_bottoms
    all_resistances = supply_obs + resistance_triggers + trailing_tops

    # 2. 检查极端位置
    if all_supports:
        lowest_support = min(a.lower_price for a in all_supports if a.lower_price is not None)
        if lowest_support is not None and price < lowest_support:
            return "below_low"

    if all_resistances:
        highest_resistance = max(a.upper_price for a in all_resistances if a.upper_price is not None)
        if highest_resistance is not None and price > highest_resistance:
            return "above_high"

    # 3. 检查触发线突破
    for trig in support_triggers:
        if trig.center_price is not None and price < trig.center_price:
            return "below_trigger"
    for trig in resistance_triggers:
        if trig.center_price is not None and price > trig.center_price:
            return "above_trigger"

    return "normal"


def _classify_chip_position(
    price: Decimal | None,
    anchors: list[AuctionAnchorItem],
) -> str | None:
    """基于筹码锚点识别价格位置。

    分类：
    - below_lower: 价格低于 VAL（下共识区）
    - lower_zone: 价格在 VAL 与 POC 之间
    - between: 价格在 POC 与 VAH 之间（价值区内）
    - upper_zone: 价格在 VAH 之上但未超过最高筹码阻力
    - above_upper: 价格超过所有筹码阻力锚点
    """
    if price is None:
        return None

    chip_anchors = [a for a in anchors if a.anchor_type == "chip"]
    if not chip_anchors:
        return None

    poc: Decimal | None = None
    vah: Decimal | None = None
    val: Decimal | None = None
    cross_down_prices: list[Decimal] = []

    for a in chip_anchors:
        payload = a.chip_payload or {}
        kind = payload.get("kind", "")
        if kind == "poc":
            poc = a.center_price
        elif kind == "vah":
            vah = a.center_price
        elif kind == "val":
            val = a.center_price
        elif kind == "cross_event" and a.direction == "down":
            cross_down_prices.append(a.center_price)

    if poc is None and vah is None and val is None:
        return None

    lower = val if val is not None else poc
    upper = vah if vah is not None else poc

    if lower is None or upper is None:
        return None

    # 计算上方阻力（VAH + cross_down 事件）
    upper_resistances = [upper] + [p for p in cross_down_prices if p is not None]
    highest_chip = max(upper_resistances) if upper_resistances else upper

    if price < lower:
        return "below_lower"
    if poc is not None and price < poc:
        return "lower_zone"
    if price > highest_chip:
        return "above_upper"
    if price > upper:
        return "upper_zone"
    return "between"


def _classify_participation_level(percentile: float | None) -> str | None:
    """基于竞价额分位识别参与度分级。"""
    if percentile is None:
        return None
    if percentile < PARTICIPATION_PERCENTILE_ABNORMAL_LOW:
        return "abnormal_low"
    if percentile < PARTICIPATION_PERCENTILE_LOW:
        return "low"
    if percentile <= PARTICIPATION_PERCENTILE_HIGH:
        return "normal"
    if percentile <= PARTICIPATION_PERCENTILE_ABNORMAL_HIGH:
        return "high"
    return "abnormal_high"


def _classify_trend_background(history_closes: list[Decimal]) -> str:
    """基于近期收盘价趋势判断背景（up/down/neutral）。

    history_closes 按降序传入（最新在前），内部反转为升序计算。
    使用前后半段均值比较，阈值 2%。
    """
    closes = [float(c) for c in reversed(history_closes) if c is not None]
    if len(closes) < 5:
        return "neutral"
    half = len(closes) // 2
    first_avg = sum(closes[:half]) / half
    second_avg = sum(closes[half:]) / (len(closes) - half)
    if first_avg == 0:
        return "neutral"
    change = (second_avg - first_avg) / first_avg
    if change > 0.02:
        return "up"
    if change < -0.02:
        return "down"
    return "neutral"


def _classify_event_type(
    structure_pos: str | None,
    chip_pos: str | None,
    participation_level: str | None,
    has_active_anchors: bool,
    all_expired: bool,
) -> str | None:
    """基于位置和参与度识别事件类型。

    事件类型：
    - dual_breakout: 结构+筹码同时突破
    - structure_breakout: 仅结构突破
    - chip_repricing: 仅筹码重新定价
    - support_confirm: 支撑确认（价格在需求 OB 内）
    - resistance_blocked: 阻力阻挡（价格在供应 OB 内）
    - test_upper: 测试上区间
    - test_lower: 测试下区间
    - inside_open: 区间内开盘
    - insufficient_participation: 参与度不足
    - structure_chip_conflict: 结构与筹码信号冲突
    - anchor_insufficient: 活跃锚点不足
    - anchor_expired: 锚点已过期
    """
    if all_expired:
        return "anchor_expired"
    if not has_active_anchors:
        return "anchor_insufficient"

    # 极端参与度不足优先标记
    if participation_level == "abnormal_low":
        return "insufficient_participation"

    # 结构与筹码信号方向
    structure_bullish = structure_pos in ("above_trigger", "above_high")
    structure_bearish = structure_pos in ("below_trigger", "below_low")
    chip_bullish = chip_pos == "above_upper"
    chip_bearish = chip_pos == "below_lower"

    # 双突破
    if structure_bullish and chip_bullish:
        return "dual_breakout"

    # 冲突检测
    if structure_pos and chip_pos:
        if (structure_bullish and chip_bearish) or (structure_bearish and chip_bullish):
            return "structure_chip_conflict"

    # 单维度突破
    if structure_bullish or structure_bearish:
        return "structure_breakout"
    if chip_bullish or chip_bearish:
        return "chip_repricing"

    # OB 区间事件
    if structure_pos == "demand_ob":
        return "support_confirm"
    if structure_pos == "supply_ob":
        return "resistance_blocked"

    # 测试区间
    if chip_pos == "upper_zone":
        return "test_upper"
    if chip_pos == "lower_zone":
        return "test_lower"

    # 默认：区间内
    return "inside_open"


def _determine_lifecycle_transition(
    event_type: str,
    opening_price: Decimal | None,
    trigger_price: Decimal | None,
) -> str:
    """根据开盘后价格判断事件生命周期转换。

    Returns:
        new lifecycle: formed/confirmed/weakened/failed
    """
    if opening_price is None or trigger_price is None:
        return "formed"

    open_f = float(opening_price)
    trig_f = float(trigger_price)
    if trig_f == 0:
        return "formed"

    # 突破类事件（价格应高于触发线）
    breakout_events = ("dual_breakout", "structure_breakout", "chip_repricing")
    if event_type in breakout_events:
        if open_f >= trig_f:
            return "confirmed"
        if open_f >= trig_f * 0.98:  # 回落 2% 以内视为减弱
            return "weakened"
        return "failed"

    # 支撑确认（价格应高于触发线/支撑）
    if event_type == "support_confirm":
        if open_f >= trig_f:
            return "confirmed"
        if open_f >= trig_f * 0.98:
            return "weakened"
        return "failed"

    # 阻力阻挡（价格应低于触发线/阻力）
    if event_type == "resistance_blocked":
        if open_f <= trig_f:
            return "confirmed"
        if open_f <= trig_f * 1.02:
            return "weakened"
        return "failed"

    # 测试类事件
    if event_type == "test_upper":
        if open_f >= trig_f:
            return "confirmed"
        return "weakened"
    if event_type == "test_lower":
        if open_f <= trig_f:
            return "confirmed"
        return "weakened"

    # inside_open / insufficient_participation / anchor_insufficient / anchor_expired
    # 无明确触发线，维持 formed
    return "formed"


# =============================================================================
# DB 加载函数
# =============================================================================


async def _get_active_a_share_instruments(session: AsyncSession) -> list[Instrument]:
    """获取所有活跃 A 股 instrument（symbol 6 位数字）。"""
    stmt = select(Instrument).where(
        Instrument.status == "active",
        Instrument.symbol.op("~")(r"^\d{6}$"),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _load_final_auction_bars(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
) -> dict[uuid.UUID, BarMinute]:
    """加载 trade_date 9:25 的 BarMinute（最终竞价数据）。

    Returns: dict[instrument_id, BarMinute]
    """
    if not instrument_ids:
        return {}

    start_time = datetime.combine(trade_date, time(9, 25, 0))
    end_time = datetime.combine(trade_date, time(9, 26, 0))

    stmt = (
        select(BarMinute)
        .where(
            BarMinute.instrument_id.in_(instrument_ids),
            BarMinute.trade_time >= start_time,
            BarMinute.trade_time < end_time,
        )
    )
    result = await session.execute(stmt)
    return {bar.instrument_id: bar for bar in result.scalars().all()}


async def _load_history_daily_bars(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
    *,
    lookback: int = HISTORY_LOOKBACK_DAYS,
) -> dict[uuid.UUID, list[BarDaily]]:
    """加载 trade_date 前 lookback 个交易日的 BarDaily。

    Returns: dict[instrument_id, list[BarDaily]] 按交易日期降序（最新在前）
    """
    if not instrument_ids:
        return {}

    # 20 个交易日 ≈ 30 个日历日，取 45 日历日确保覆盖
    calendar_lookback = lookback * 2 + 5

    stmt = (
        select(BarDaily)
        .where(
            BarDaily.instrument_id.in_(instrument_ids),
            BarDaily.trade_date < trade_date,
            BarDaily.trade_date >= trade_date - timedelta(days=calendar_lookback),
        )
        .order_by(BarDaily.instrument_id, BarDaily.trade_date.desc())
    )
    result = await session.execute(stmt)
    history_map: dict[uuid.UUID, list[BarDaily]] = {}
    for bar in result.scalars().all():
        history_map.setdefault(bar.instrument_id, []).append(bar)
    return {iid: bars[:lookback] for iid, bars in history_map.items()}


async def _load_history_auction_bars(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
    *,
    lookback: int = HISTORY_LOOKBACK_DAYS,
) -> dict[uuid.UUID, list[BarMinute]]:
    """加载 trade_date 前 lookback 个交易日的 9:25 BarMinute（历史竞价额）。

    Returns: dict[instrument_id, list[BarMinute]] 按交易时间降序（最新在前）
    """
    if not instrument_ids:
        return {}

    calendar_lookback = lookback * 2 + 5
    start_date = trade_date - timedelta(days=calendar_lookback)

    # 9:25 时间窗口：每日 [09:25:00, 09:26:00)
    start_time = datetime.combine(start_date, time(9, 25, 0))
    end_time = datetime.combine(trade_date, time(9, 26, 0))

    stmt = (
        select(BarMinute)
        .where(
            BarMinute.instrument_id.in_(instrument_ids),
            BarMinute.trade_time >= start_time,
            BarMinute.trade_time < end_time,
            func.extract("hour", BarMinute.trade_time) == 9,
            func.extract("minute", BarMinute.trade_time) == 25,
        )
        .order_by(BarMinute.instrument_id, BarMinute.trade_time.desc())
    )
    result = await session.execute(stmt)
    history_map: dict[uuid.UUID, list[BarMinute]] = {}
    for bar in result.scalars().all():
        history_map.setdefault(bar.instrument_id, []).append(bar)
    return {iid: bars[:lookback] for iid, bars in history_map.items()}


async def _load_instrument_anchors(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
    instrument_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[AuctionAnchorItem]]:
    """加载指定 snapshot 下指定 instruments 的锚点。

    Returns: dict[instrument_id, list[AuctionAnchorItem]]
    """
    if not instrument_ids:
        return {}

    stmt = (
        select(AuctionAnchorItem)
        .where(
            AuctionAnchorItem.snapshot_id == snapshot_id,
            AuctionAnchorItem.instrument_id.in_(instrument_ids),
        )
    )
    result = await session.execute(stmt)
    anchor_map: dict[uuid.UUID, list[AuctionAnchorItem]] = {}
    for anchor in result.scalars().all():
        anchor_map.setdefault(anchor.instrument_id, []).append(anchor)
    return anchor_map


async def _load_opening_window_bars(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
) -> dict[uuid.UUID, list[BarMinute]]:
    """加载 trade_date 开盘后 OPENING_VERIFY_WINDOW_MINUTES 内的 BarMinute。

    Returns: dict[instrument_id, list[BarMinute]] 按时间升序
    """
    if not instrument_ids:
        return {}

    start_time = datetime.combine(trade_date, OPENING_TIME)
    end_minute = (OPENING_TIME.minute + OPENING_VERIFY_WINDOW_MINUTES) % 60
    end_hour = OPENING_TIME.hour + (OPENING_TIME.minute + OPENING_VERIFY_WINDOW_MINUTES) // 60
    end_time = datetime.combine(trade_date, time(end_hour, end_minute, 0))

    stmt = (
        select(BarMinute)
        .where(
            BarMinute.instrument_id.in_(instrument_ids),
            BarMinute.trade_time >= start_time,
            BarMinute.trade_time < end_time,
        )
        .order_by(BarMinute.instrument_id, BarMinute.trade_time)
    )
    result = await session.execute(stmt)
    bars_map: dict[uuid.UUID, list[BarMinute]] = {}
    for bar in result.scalars().all():
        bars_map.setdefault(bar.instrument_id, []).append(bar)
    return bars_map


# =============================================================================
# 主入口：run_auction_scan
# =============================================================================


async def run_auction_scan(
    db: AsyncSession,
    trade_date: date,
    *,
    auction_type: str = "final",
    worker_id: str | None = None,
    lease_epoch: int | None = None,
) -> dict[str, Any]:
    """主入口：基于已冻结锚点扫描次日最终竞价价格位置与事件。

    流程：
    1. 查询已发布的锚点（get_published_anchors）
    2. 校验锚点可用性（未发布/过期 → 抛异常）
    3. 创建 AuctionScanRun（status=running）
    4. 遍历 A 股 instrument，计算位置/事件/参与度
    5. 写入 AuctionInstrumentResult 和 AuctionEventTracking
    6. 更新 scan_run status=succeeded/failed/partial

    Args:
        db: 异步 DB 会话
        trade_date: 业务交易日（竞价日）
        auction_type: final（最终竞价）/ opening（开盘验证）
        worker_id: Worker 标识
        lease_epoch: 租约 epoch

    Returns:
        {
            "run_id": uuid.UUID,
            "status": str,
            "eligible_count": int,
            "ready_count": int,
            "coverage_ratio": float,
            "missing_count": int,
            "missing_reasons": dict,
            "result_count": int,
            "event_count": int,
        }
    """
    logger.info(
        "[AuctionScan] 开始扫描: trade_date=%s type=%s worker_id=%s lease_epoch=%s",
        trade_date, auction_type, worker_id, lease_epoch,
    )

    # 1. 查询已发布的锚点
    anchors_info = await get_published_anchors(db, trade_date)
    publication_id = anchors_info.get("publication_id")
    snapshot_id = anchors_info.get("snapshot_id")

    if publication_id is None or snapshot_id is None:
        raise AnchorNotPublishedError(
            f"trade_date={trade_date} 锚点未发布，禁止扫描"
        )

    # 2. 校验锚点快照状态与算法版本
    snapshot = await db.get(AuctionAnchorSnapshot, snapshot_id)
    if snapshot is None:
        raise AnchorNotPublishedError(
            f"snapshot_id={snapshot_id} 不存在"
        )
    if snapshot.status not in ("succeeded", "structure_only"):
        raise AnchorExpiredError(
            f"snapshot status={snapshot.status!r} 不允许扫描（仅 succeeded/structure_only 可扫描）"
        )

    price_adjustment_version = snapshot.price_adjustment_version

    # 3. 创建 AuctionScanRun
    now = datetime.now(UTC)
    run = AuctionScanRun(
        trade_date=trade_date,
        auction_type=auction_type,
        source_anchor_snapshot_id=snapshot_id,
        source_anchor_publication_id=publication_id,
        algorithm_version=AUCTION_SCAN_ALGORITHM_VERSION,
        price_adjustment_version=price_adjustment_version,
        status="running",
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        started_at=now,
        heartbeat_at=now,
    )
    db.add(run)
    await db.flush()
    run_id = run.id

    try:
        # 4. 加载数据
        instruments = await _get_active_a_share_instruments(db)
        instrument_ids = [inst.id for inst in instruments]

        if not instrument_ids:
            logger.warning("[AuctionScan] 无活跃 A 股 instrument")
            run.status = "succeeded"
            run.eligible_count = 0
            run.ready_count = 0
            run.coverage_ratio = 0.0
            run.missing_count = 0
            run.finished_at = datetime.now(UTC)
            await db.flush()
            return {
                "run_id": run_id,
                "status": "succeeded",
                "eligible_count": 0,
                "ready_count": 0,
                "coverage_ratio": 0.0,
                "missing_count": 0,
                "missing_reasons": {},
                "result_count": 0,
                "event_count": 0,
            }

        auction_bars = await _load_final_auction_bars(db, instrument_ids, trade_date)
        history_daily = await _load_history_daily_bars(db, instrument_ids, trade_date)
        history_auction = await _load_history_auction_bars(db, instrument_ids, trade_date)
        anchor_map = await _load_instrument_anchors(db, snapshot_id, instrument_ids)

        # 5. 遍历处理
        results: list[AuctionInstrumentResult] = []
        events: list[AuctionEventTracking] = []
        missing_reasons: dict[str, int] = defaultdict(int)

        for instrument in instruments:
            instrument_id = instrument.id
            auction_bar = auction_bars.get(instrument_id)
            daily_history = history_daily.get(instrument_id, [])
            auction_history = history_auction.get(instrument_id, [])
            anchors = anchor_map.get(instrument_id, [])

            reason_codes: list[str] = []

            # 锚点可用性检查
            active_anchors = [
                a for a in anchors
                if a.is_active and a.freshness != "expired"
            ]
            all_expired = (
                bool(anchors) and not active_anchors
                and all(a.freshness == "expired" for a in anchors)
            )
            if not anchors:
                reason_codes.append("anchor_missing")
            elif not active_anchors and not all_expired:
                reason_codes.append("anchor_insufficient")

            # 竞价数据检查
            if auction_bar is None:
                reason_codes.append("auction_bar_missing")
                missing_reasons["auction_bar_missing"] += 1
                results.append(AuctionInstrumentResult(
                    scan_run_id=run_id,
                    trade_date=trade_date,
                    instrument_id=instrument_id,
                    is_suspended=True,
                    reason_codes=reason_codes,
                    detail_payload={"anchors_available": len(anchors)},
                ))
                continue

            # prev_close（来自最近一日日线）
            prev_close = daily_history[0].close if daily_history else None
            if prev_close is None:
                reason_codes.append("prev_close_missing")

            # 涨跌幅
            final_price = _safe_decimal(auction_bar.close)
            change_pct, change_components = _compute_change_pct(final_price, prev_close)

            # 竞价量和额
            auction_volume = (
                int(auction_bar.volume) if auction_bar.volume is not None else None
            )
            auction_amount = _safe_decimal(auction_bar.amount)

            # 相对 20 日竞价额中位数和分位
            history_auction_amounts = [
                _safe_decimal(b.amount) for b in auction_history
            ]
            rel_vol_median, vol_median_components = _compute_relative_volume_median(
                auction_amount, history_auction_amounts
            )
            vol_percentile, vol_percentile_components = _compute_volume_percentile(
                auction_amount, history_auction_amounts
            )

            # ATR 标准化距离
            atr_distance, atr_components = _compute_atr_distance(
                final_price, prev_close, daily_history
            )

            # 状态标记
            is_suspended = (
                auction_bar.volume is None or auction_bar.volume == 0
            )
            is_limit_up, is_limit_down = _detect_limits(change_pct)
            is_ex_right = _detect_ex_right(daily_history)

            # 位置分类
            structure_position = _classify_structure_position(final_price, active_anchors)
            chip_position = _classify_chip_position(final_price, active_anchors)

            # 参与度分级
            participation_level = _classify_participation_level(vol_percentile)

            # 趋势背景（只作标签）
            trend_closes = [
                b.close for b in daily_history if b.close is not None
            ]
            trend_background = _classify_trend_background(trend_closes)

            # 事件类型
            event_type = _classify_event_type(
                structure_position,
                chip_position,
                participation_level,
                bool(active_anchors),
                all_expired,
            )

            # 构造 detail_payload（含所有比例的分子和分母）
            detail_payload: dict[str, Any] = {
                "change_pct_components": change_components,
                "relative_volume_median_components": vol_median_components,
                "volume_percentile_components": vol_percentile_components,
                "atr_distance_components": atr_components,
                "active_anchor_count": len(active_anchors),
                "anchor_types": list({a.anchor_type for a in active_anchors}),
                "trend_background": trend_background,
            }

            # 构造 result
            result = AuctionInstrumentResult(
                scan_run_id=run_id,
                trade_date=trade_date,
                instrument_id=instrument_id,
                final_auction_price=final_price,
                prev_close=prev_close,
                change_pct=change_pct,
                auction_volume=auction_volume,
                auction_amount=auction_amount,
                relative_volume_median_20d=rel_vol_median,
                volume_percentile=vol_percentile,
                atr_distance_pct=atr_distance,
                is_suspended=is_suspended,
                is_limit_up=is_limit_up,
                is_limit_down=is_limit_down,
                is_ex_right=is_ex_right,
                structure_position=structure_position,
                chip_position=chip_position,
                event_type=event_type,
                event_lifecycle="formed" if event_type else None,
                participation_level=participation_level,
                trend_background=trend_background,
                anchor_ids=(
                    [str(a.id) for a in active_anchors] if active_anchors else None
                ),
                detail_payload=detail_payload,
                reason_codes=reason_codes,
            )
            results.append(result)

            # 构造 event tracking（仅对有意义的事件创建）
            if event_type and event_type not in (
                "inside_open",
                "anchor_insufficient",
                "anchor_expired",
                "insufficient_participation",
            ):
                # 选取最强锚点作为主触发锚点
                primary_anchor = (
                    max(active_anchors, key=lambda a: a.strength)
                    if active_anchors else None
                )
                trigger_price = (
                    primary_anchor.center_price if primary_anchor is not None
                    else final_price
                )
                events.append(AuctionEventTracking(
                    scan_run_id=run_id,
                    trade_date=trade_date,
                    instrument_id=instrument_id,
                    event_type=event_type,
                    lifecycle="formed",
                    anchor_id=primary_anchor.id if primary_anchor else None,
                    trigger_price=trigger_price,
                    trigger_condition=f"{structure_position}/{chip_position}",
                    formed_at=datetime.now(UTC),
                    reason_codes=reason_codes,
                ))

        # 6. 批量写入
        for r in results:
            db.add(r)
        for e in events:
            db.add(e)
        await db.flush()

        # 7. 统计与状态
        eligible_count = len(instruments)
        valid_count = sum(
            1 for r in results if "auction_bar_missing" not in r.reason_codes
        )
        ready_count = sum(1 for r in results if r.event_type is not None)
        coverage_ratio = (
            ready_count / eligible_count if eligible_count > 0 else 0.0
        )

        if valid_count == 0:
            final_status = "failed"
        elif valid_count < eligible_count:
            final_status = "partial"
        else:
            final_status = "succeeded"

        run.status = final_status
        run.eligible_count = eligible_count
        run.ready_count = ready_count
        run.coverage_ratio = round(coverage_ratio, 6)
        run.missing_count = eligible_count - valid_count
        run.missing_reasons = dict(missing_reasons)
        run.finished_at = datetime.now(UTC)
        await db.flush()

        logger.info(
            "[AuctionScan] 扫描完成: trade_date=%s run_id=%s status=%s "
            "eligible=%d valid=%d ready=%d coverage=%.4f events=%d",
            trade_date, run_id, final_status,
            eligible_count, valid_count, ready_count, coverage_ratio, len(events),
        )

        return {
            "run_id": run_id,
            "status": final_status,
            "eligible_count": eligible_count,
            "ready_count": ready_count,
            "coverage_ratio": round(coverage_ratio, 6),
            "missing_count": eligible_count - valid_count,
            "missing_reasons": dict(missing_reasons),
            "result_count": len(results),
            "event_count": len(events),
        }

    except Exception as exc:
        logger.error(
            "[AuctionScan] 扫描失败: trade_date=%s run_id=%s: %s",
            trade_date, run_id, exc,
            exc_info=True,
        )
        run.status = "failed"
        run.error_message = str(exc)[:1000]
        run.finished_at = datetime.now(UTC)
        await db.flush()
        return {
            "run_id": run_id,
            "status": "failed",
            "eligible_count": 0,
            "ready_count": 0,
            "coverage_ratio": 0.0,
            "missing_count": 0,
            "missing_reasons": {},
            "result_count": 0,
            "event_count": 0,
            "error_message": str(exc)[:1000],
        }


# =============================================================================
# 开盘后验证：update_event_lifecycle
# =============================================================================


async def update_event_lifecycle(
    db: AsyncSession,
    scan_run_id: uuid.UUID,
    *,
    check_time: datetime | None = None,
) -> dict[str, Any]:
    """开盘后验证更新事件生命周期。

    流程：
    1. 查询 formed 状态的事件
    2. 获取开盘后窗口内的 BarMinute 数据
    3. 判断 formed → confirmed/weakened/failed/expired
    4. 识别：突破后回落、破位后收回、成交不足、龙头孤立、扩散失败、指数背离
    5. 更新 AuctionEventTracking lifecycle 和时间戳

    Args:
        db: 异步 DB 会话
        scan_run_id: 扫描 run ID
        check_time: 检查时间（默认当前 UTC）

    Returns:
        {
            "scan_run_id": uuid.UUID,
            "total": int,
            "transitions": dict[str, int],
        }
    """
    run = await db.get(AuctionScanRun, scan_run_id)
    if run is None:
        raise ValueError(f"AuctionScanRun not found: {scan_run_id}")

    # 查询 formed 状态事件
    stmt = (
        select(AuctionEventTracking)
        .where(
            AuctionEventTracking.scan_run_id == scan_run_id,
            AuctionEventTracking.lifecycle == "formed",
        )
    )
    result = await db.execute(stmt)
    events = list(result.scalars().all())

    if not events:
        return {"scan_run_id": scan_run_id, "total": 0, "transitions": {}}

    # 加载开盘后窗口数据
    instrument_ids = list({e.instrument_id for e in events})
    opening_bars_map = await _load_opening_window_bars(
        db, instrument_ids, run.trade_date
    )

    transitions: dict[str, int] = defaultdict(int)
    now = check_time or datetime.now(UTC)

    for event in events:
        bars = opening_bars_map.get(event.instrument_id, [])
        if not bars:
            # 无开盘数据，维持 formed
            continue

        # 开盘价 = 第一根 bar 的 open；窗口末价 = 最后一根 bar 的 close
        opening_price = _safe_decimal(bars[0].open)
        window_end_price = _safe_decimal(bars[-1].close)
        window_volume = sum(
            int(b.volume) for b in bars if b.volume is not None
        )

        trigger_price = _safe_decimal(event.trigger_price)

        # 判断生命周期转换
        new_lifecycle = _determine_lifecycle_transition(
            event.event_type, opening_price, trigger_price
        )

        # 开盘后回落检测：突破后窗口末价回落至触发线下方
        if new_lifecycle == "confirmed" and window_end_price is not None and trigger_price is not None:
            breakout_events = ("dual_breakout", "structure_breakout", "chip_repricing")
            if event.event_type in breakout_events:
                if float(window_end_price) < float(trigger_price):
                    new_lifecycle = "weakened"

        if new_lifecycle == event.lifecycle:
            # 无变化
            continue

        event.lifecycle = new_lifecycle
        if new_lifecycle == "confirmed":
            event.confirmed_at = now
        elif new_lifecycle == "weakened":
            event.weakened_at = now
        elif new_lifecycle == "failed":
            event.failed_at = now
        elif new_lifecycle == "expired":
            event.expired_at = now

        event.confirmation_data = {
            "opening_price": str(opening_price) if opening_price else None,
            "window_end_price": str(window_end_price) if window_end_price else None,
            "trigger_price": str(trigger_price) if trigger_price else None,
            "check_time": now.isoformat(),
            "window_volume": window_volume,
            "window_bars_count": len(bars),
        }

        transitions[f"formed->{new_lifecycle}"] += 1

    await db.flush()

    logger.info(
        "[AuctionScan] 事件生命周期更新: scan_run_id=%s total=%d transitions=%s",
        scan_run_id, len(events), dict(transitions),
    )

    return {
        "scan_run_id": scan_run_id,
        "total": len(events),
        "transitions": dict(transitions),
    }


# =============================================================================
# 查询：get_scan_results
# =============================================================================


async def get_scan_results(
    db: AsyncSession,
    trade_date: date,
    *,
    auction_type: str = "final",
) -> dict[str, Any]:
    """查询已完成的扫描结果。

    Returns:
        {
            "trade_date": date,
            "auction_type": str,
            "run_id": uuid.UUID | None,
            "status": str | None,
            "eligible_count": int,
            "ready_count": int,
            "coverage_ratio": float,
            "results": list[AuctionInstrumentResult],
            "events": list[AuctionEventTracking],
        }
    """
    stmt = (
        select(AuctionScanRun)
        .where(
            AuctionScanRun.trade_date == trade_date,
            AuctionScanRun.auction_type == auction_type,
            AuctionScanRun.algorithm_version == AUCTION_SCAN_ALGORITHM_VERSION,
        )
        .order_by(AuctionScanRun.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()

    if run is None:
        return {
            "trade_date": trade_date,
            "auction_type": auction_type,
            "run_id": None,
            "status": None,
            "eligible_count": 0,
            "ready_count": 0,
            "coverage_ratio": 0.0,
            "results": [],
            "events": [],
        }

    results_stmt = (
        select(AuctionInstrumentResult)
        .where(AuctionInstrumentResult.scan_run_id == run.id)
    )
    results = (await db.execute(results_stmt)).scalars().all()

    events_stmt = (
        select(AuctionEventTracking)
        .where(AuctionEventTracking.scan_run_id == run.id)
    )
    events = (await db.execute(events_stmt)).scalars().all()

    return {
        "trade_date": trade_date,
        "auction_type": auction_type,
        "run_id": run.id,
        "status": run.status,
        "eligible_count": run.eligible_count,
        "ready_count": run.ready_count,
        "coverage_ratio": run.coverage_ratio,
        "missing_count": run.missing_count,
        "missing_reasons": run.missing_reasons,
        "results": list(results),
        "events": list(events),
    }


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯函数自测（不连 DB）
    print("auction_scan_service 自测...")

    # 常量校验
    assert AUCTION_SCAN_ALGORITHM_VERSION == "v1.0.0"
    assert AUCTION_FINAL_TIME == "09:25"
    assert OPENING_VERIFY_WINDOW_MINUTES == 30
    assert PARTICIPATION_PERCENTILE_LOW == 20
    assert PARTICIPATION_PERCENTILE_HIGH == 80
    assert PARTICIPATION_PERCENTILE_ABNORMAL_LOW == 5
    assert PARTICIPATION_PERCENTILE_ABNORMAL_HIGH == 95

    # _safe_decimal / _safe_float
    assert _safe_decimal(None) is None
    assert _safe_decimal("invalid") is None
    assert _safe_decimal("10.5") == Decimal("10.5")
    assert _safe_float(None) is None
    assert _safe_float("abc") is None
    assert _safe_float("3.14") == 3.14

    # _compute_change_pct
    pct, comp = _compute_change_pct(Decimal("11.0"), Decimal("10.0"))
    assert pct == 10.0
    assert comp["price"] == "11.0"
    assert comp["prev_close"] == "10.0"
    assert _compute_change_pct(None, Decimal("10.0"))[0] is None
    assert _compute_change_pct(Decimal("11.0"), Decimal("0"))[0] is None

    # _compute_relative_volume_median
    ratio, comp = _compute_relative_volume_median(
        Decimal("150.0"), [Decimal("100.0"), Decimal("200.0")]
    )
    assert ratio == 1.0  # 150 / median(100, 200)=150
    assert comp["current_amount"] == "150.0"
    assert _compute_relative_volume_median(None, [Decimal("100.0")])[0] is None
    assert _compute_relative_volume_median(Decimal("100.0"), [])[0] is None

    # _compute_volume_percentile
    pctile, comp = _compute_volume_percentile(
        Decimal("150.0"), [Decimal("100.0"), Decimal("200.0")]
    )
    assert pctile == 50.0  # 1/2 below
    assert comp["below_count"] == 1

    # _classify_participation_level
    assert _classify_participation_level(None) is None
    assert _classify_participation_level(2.0) == "abnormal_low"
    assert _classify_participation_level(10.0) == "low"
    assert _classify_participation_level(50.0) == "normal"
    assert _classify_participation_level(90.0) == "high"
    assert _classify_participation_level(97.0) == "abnormal_high"

    # _classify_trend_background
    closes_up = [Decimal("11.0"), Decimal("10.5"), Decimal("10.0"), Decimal("9.5"), Decimal("9.0")]
    assert _classify_trend_background(closes_up) == "up"  # 最新在前 → 反转后上升
    closes_down = [Decimal("9.0"), Decimal("9.5"), Decimal("10.0"), Decimal("10.5"), Decimal("11.0")]
    assert _classify_trend_background(closes_down) == "down"
    assert _classify_trend_background([Decimal("10.0")] * 5) == "neutral"

    # _detect_limits
    assert _detect_limits(10.0) == (True, False)
    assert _detect_limits(-10.0) == (False, True)
    assert _detect_limits(5.0) == (False, False)
    assert _detect_limits(None) == (False, False)

    # _detect_ex_right（需 BarDaily mock）
    class _MockBar:
        def __init__(self, adj_factor, close=None):
            self.adj_factor = adj_factor
            self.close = close
    assert _detect_ex_right([_MockBar(Decimal("1.05")), _MockBar(Decimal("1.0"))]) is True
    assert _detect_ex_right([_MockBar(Decimal("1.0")), _MockBar(Decimal("1.0"))]) is False
    assert _detect_ex_right([_MockBar(Decimal("1.0"))]) is False

    # _determine_lifecycle_transition
    assert _determine_lifecycle_transition(
        "dual_breakout", Decimal("11.0"), Decimal("10.0")
    ) == "confirmed"
    assert _determine_lifecycle_transition(
        "dual_breakout", Decimal("9.5"), Decimal("10.0")
    ) == "failed"
    assert _determine_lifecycle_transition(
        "resistance_blocked", Decimal("9.5"), Decimal("10.0")
    ) == "confirmed"
    assert _determine_lifecycle_transition(
        "resistance_blocked", Decimal("10.5"), Decimal("10.0")
    ) == "failed"
    assert _determine_lifecycle_transition(
        "inside_open", Decimal("10.0"), Decimal("10.0")
    ) == "formed"

    print("OK")
