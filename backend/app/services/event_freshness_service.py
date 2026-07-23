"""Event Freshness Service — 事件新鲜度纯函数与 DTO。

本模块是事件新鲜度层的唯一计算入口（纯函数，不连接数据库、不调用 MDAS）。
盘后 MarketFeatureComputationService 调用本模块构建 event_freshness_payload。

核心概念：
- 连续因子层回答"当前处于什么状态"（structural_payload / temporal_payload）
- 事件新鲜度层回答"最近一次客观事件距现在多久"（event_freshness_payload）

freshness 公式（PRD §7.2）：
- bar freshness: freshness = current_index - event_index（事件当根=0）
- trading day freshness: freshness = calendar_index(as_of) - calendar_index(event_trade_date)（当日=0）

state 语义：
- observed: 事件已发生，value 为非负 int
- never_observed: 从未发生，value=null（合法可发布）
- unavailable: 数据源失败，value=null + reason（不得伪装成 never_observed）

模块自测：
    python -m app.services.event_freshness_service
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# 事件新鲜度单位
UNIT_COMPLETED_DAILY_BARS = "completed_daily_bars"
UNIT_TRADING_DAYS = "trading_days"


class FreshnessState(StrEnum):
    """事件新鲜度状态枚举（PRD §7.1）。

    - observed: 事件已发生，value 为非负 int
    - never_observed: 从未发生，value=null（合法可发布）
    - unavailable: 数据源失败，value=null + reason（不得伪装成 never_observed）
    """

    OBSERVED = "observed"
    NEVER_OBSERVED = "never_observed"
    UNAVAILABLE = "unavailable"


@dataclass
class FreshnessItem:
    """单个事件新鲜度项（PRD §7.1 通用对象）。

    序列化为 dict 后写入 event_freshness_payload。
    """

    event_type: str
    state: FreshnessState
    value: int | None = None
    unit: str = UNIT_COMPLETED_DAILY_BARS
    event_time: str | None = None
    direction: str | None = None
    level: str | None = None
    entity_id: str | None = None
    boundary: float | None = None
    source: str | None = None
    source_hash: str | None = None
    profile_hash: str | None = None
    reason: str | None = None  # 仅 unavailable 时填写

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON-safe dict（null 字段保留以保持 schema 稳定）。"""
        d: dict[str, Any] = {
            "value": self.value,
            "unit": self.unit,
            "state": self.state.value,
            "event_type": self.event_type,
        }
        # 可选字段：非 None 时写入
        for k in (
            "event_time", "direction", "level", "entity_id",
            "boundary", "source", "source_hash", "profile_hash", "reason",
        ):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        return d


# =============================================================================
# 构造器
# =============================================================================


def build_observed(
    event_type: str,
    value: int,
    *,
    unit: str = UNIT_COMPLETED_DAILY_BARS,
    event_time: str | None = None,
    direction: str | None = None,
    level: str | None = None,
    entity_id: str | None = None,
    boundary: float | None = None,
    source: str | None = None,
    source_hash: str | None = None,
    profile_hash: str | None = None,
) -> FreshnessItem:
    """构造 observed 状态的 FreshnessItem。"""
    return FreshnessItem(
        event_type=event_type,
        state=FreshnessState.OBSERVED,
        value=value,
        unit=unit,
        event_time=event_time,
        direction=direction,
        level=level,
        entity_id=entity_id,
        boundary=boundary,
        source=source,
        source_hash=source_hash,
        profile_hash=profile_hash,
    )


def build_never_observed(
    event_type: str,
    *,
    unit: str = UNIT_COMPLETED_DAILY_BARS,
    direction: str | None = None,
    level: str | None = None,
    source: str | None = None,
) -> FreshnessItem:
    """构造 never_observed 状态的 FreshnessItem（从未发生，合法可发布）。"""
    return FreshnessItem(
        event_type=event_type,
        state=FreshnessState.NEVER_OBSERVED,
        value=None,
        unit=unit,
        direction=direction,
        level=level,
        source=source,
    )


def build_unavailable(
    event_type: str,
    *,
    reason: str,
    unit: str = UNIT_COMPLETED_DAILY_BARS,
    direction: str | None = None,
    level: str | None = None,
    source: str | None = None,
) -> FreshnessItem:
    """构造 unavailable 状态的 FreshnessItem（数据源失败，不得伪装成 never_observed）。"""
    return FreshnessItem(
        event_type=event_type,
        state=FreshnessState.UNAVAILABLE,
        value=None,
        unit=unit,
        direction=direction,
        level=level,
        source=source,
        reason=reason,
    )


# =============================================================================
# freshness 公式（纯函数）
# =============================================================================


def freshness_from_bar_index(
    current_index: int,
    event_index: int | None,
) -> int | None:
    """bar 级 freshness（PRD §7.2）。

    freshness = current_index - event_index
    事件当根 = 0。

    Args:
        current_index: 最新已完成 bar 的 index
        event_index: 事件发生 bar 的 index（None → never_observed）

    Returns:
        freshness int（>= 0），或 None（事件未发生）

    Raises:
        ValueError: event_index > current_index（数据质量错误 → unavailable）
    """
    if event_index is None:
        return None
    if event_index > current_index:
        raise ValueError(
            f"event_index({event_index}) > current_index({current_index}): data_quality_error"
        )
    return current_index - event_index


def freshness_from_trading_day(
    as_of: date,
    event_trade_date: date | None,
    trading_calendar: list[date],
) -> int | None:
    """trading day 级 freshness（PRD §7.2）。

    freshness = calendar_index(as_of) - calendar_index(event_trade_date)
    当日事件 = 0。跨周末只按交易日计数。

    Args:
        as_of: 基准交易日
        event_trade_date: 事件发生交易日（None → never_observed）
        trading_calendar: 交易日列表（已排序，升序）

    Returns:
        freshness int（>= 0），或 None（事件未发生）

    Raises:
        ValueError: event > as_of（数据质量错误）或 calendar 缺失 → unavailable
    """
    if event_trade_date is None:
        return None
    if not trading_calendar:
        raise ValueError("trading_calendar 为空: unavailable")
    # 构建日期 → 序号映射
    cal_index = {d: i for i, d in enumerate(trading_calendar)}
    as_of_idx = cal_index.get(as_of)
    event_idx = cal_index.get(event_trade_date)
    if as_of_idx is None or event_idx is None:
        raise ValueError(
            f"日期不在 trading_calendar 中: as_of={as_of}, event={event_trade_date}"
        )
    if event_idx > as_of_idx:
        raise ValueError(
            f"event_trade_date({event_trade_date}) > as_of({as_of}): data_quality_error"
        )
    return as_of_idx - event_idx


# =============================================================================
# 批量事件聚合（SQL 层在 repository 实现，本函数做结果映射）
# =============================================================================


def aggregate_latest_monitor_events(
    raw_events: list[dict[str, Any]],
    *,
    as_of: date,
    trading_calendar: list[date],
) -> dict[str, dict[str, Any]]:
    """将批量查询的原始 monitor 事件聚合为最新事件 freshness 映射。

    本函数接收已批量查询的事件列表（SQL 层用 DISTINCT ON / 窗口函数取最新），
    不做 N+1 查询。输出按 (event_type, direction) 分组的最新 freshness。

    旧事件无 direction 时允许一次兼容推断并标记 legacy_inferred=true。
    新事件无 direction 应在 SQL 层被标为 unavailable。

    Args:
        raw_events: 批量查询的原始事件列表，每项含:
            - event_type: 事件类型
            - event_time: 事件时间（ISO 字符串或 datetime）
            - payload: 事件 payload dict（含 cross_direction / boundary / node_id 等）
            - direction: 显式方向（旧数据可能缺失）
        as_of: 基准交易日
        trading_calendar: 交易日列表

    Returns:
        dict: key="{event_type}:{direction}" → FreshnessItem.to_dict()
    """
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_events:
        event_type = raw.get("event_type")
        if not event_type:
            continue
        payload = raw.get("payload") or {}
        direction = raw.get("direction") or payload.get("cross_direction")
        event_time_str = raw.get("event_time")

        # 兼容推断旧数据方向
        legacy_inferred = False
        if direction is None:
            direction = _infer_legacy_direction(event_type, payload)
            legacy_inferred = direction is not None

        # 解析事件交易日
        event_trade_date = _parse_event_trade_date(event_time_str)
        if event_trade_date is None:
            item = build_unavailable(
                event_type,
                reason="event_time_parse_failed",
                direction=direction,
                source="strategy_events",
            )
        else:
            try:
                freshness = freshness_from_trading_day(
                    as_of, event_trade_date, trading_calendar,
                )
                if freshness is None:
                    item = build_never_observed(
                        event_type, direction=direction, source="strategy_events",
                    )
                else:
                    item = build_observed(
                        event_type,
                        freshness,
                        unit=UNIT_TRADING_DAYS,
                        event_time=event_time_str,
                        direction=direction,
                        boundary=payload.get("boundary") or payload.get("cluster_price"),
                        entity_id=payload.get("node_id") or raw.get("logical_entity_id"),
                        source="strategy_events",
                        profile_hash=payload.get("profile_hash"),
                    )
            except ValueError:
                item = build_unavailable(
                    event_type,
                    reason="calendar_or_data_quality_error",
                    direction=direction,
                    source="strategy_events",
                )

        key = f"{event_type}:{direction or 'unknown'}"
        d = item.to_dict()
        if legacy_inferred:
            d["legacy_inferred"] = True
        result[key] = d

    return result


def _infer_legacy_direction(
    event_type: str,
    payload: dict[str, Any],
) -> str | None:
    """旧事件无 cross_direction 时的兼容推断（仅用于历史数据）。"""
    dev_pct = payload.get("dev_pct")
    if dev_pct is None:
        return None
    # bb_upper_touch / node 上穿 → up; bb_lower_touch / node 下穿 → down
    if "upper" in event_type or "eqh" in event_type:
        return "up" if float(dev_pct) >= 0 else "down"
    if "lower" in event_type or "eql" in event_type:
        return "down" if float(dev_pct) <= 0 else "up"
    if "mid" in event_type:
        return "up" if float(dev_pct) >= 0 else "down"
    return "up" if float(dev_pct) >= 0 else "down"


def _parse_event_trade_date(event_time: Any) -> date | None:
    """从 event_time 解析交易日 date（支持 ISO 字符串 / datetime）。"""
    if event_time is None:
        return None
    if isinstance(event_time, date) and not isinstance(event_time, type(None)):
        return event_time if isinstance(event_time, date) else None
    if isinstance(event_time, str):
        try:
            return date.fromisoformat(event_time[:10])
        except (ValueError, IndexError):
            return None
    # datetime 对象
    if hasattr(event_time, "date"):
        return event_time.date()
    return None


# =============================================================================
# SMC 日线结构新鲜度（从预计算 SMC DTO 构建，不调用 kernel）
# =============================================================================


def build_smc_daily_freshness(
    smc_dto: dict[str, Any] | None,
    bars: Any,
    current_index: int,
) -> dict[str, Any]:
    """从预计算 SMC DTO 构建 18 项日线 SMC freshness（不调用 SMC kernel）。

    [CHANGE-20260724-002] 从 structural_factor_service._compute_smc_freshness_factors 迁出，
    单次消费预计算 DTO，拆分 formation 和 first_touch 命名，增加 4 项 OB formation。

    18 项明细（PRD §7.3）：
    - BOS: 4 (bullish/bearish × internal/swing)
    - CHoCH: 4 (同上)
    - EQH/EQL: 2
    - OB formation: 4 (bullish/bearish × internal/swing) — 新增
    - OB first_touch: 4 (bullish/bearish × internal/swing) — 原 OB touch 改名

    Args:
        smc_dto: 预计算的 SMC DTO（来自 compute_smc_adapter，可为 None）
        bars: 已完成日线 DataFrame（用于 OB first_touch 影线相交检测）
        current_index: 最新已完成 bar 的 index

    Returns:
        dict: 18 个 freshness 值（int 或 None），key 命名:
        - smc_bos_{direction}_{level}_freshness_bars
        - smc_choch_{direction}_{level}_freshness_bars
        - smc_eqh_freshness_bars / smc_eql_freshness_bars
        - smc_ob_formation_{direction}_{level}_freshness_bars
        - smc_ob_first_touch_{direction}_{level}_freshness_bars
    """
    # 初始化所有 key 为 None（never_observed）
    result: dict[str, Any] = {}
    for etype in ("bos", "choch"):
        for direction in ("bullish", "bearish"):
            for level in ("internal", "swing"):
                result[f"smc_{etype}_{direction}_{level}_freshness_bars"] = None
    result["smc_eqh_freshness_bars"] = None
    result["smc_eql_freshness_bars"] = None
    for direction in ("bullish", "bearish"):
        for level in ("internal", "swing"):
            result[f"smc_ob_formation_{direction}_{level}_freshness_bars"] = None
            result[f"smc_ob_first_touch_{direction}_{level}_freshness_bars"] = None

    if smc_dto is None or bars is None:
        return result
    try:
        if hasattr(bars, "empty") and bars.empty:
            return result
    except Exception:
        return result

    # --- BOS/CHoCH: 按 bullish/bearish × internal/swing 拆分 ---
    bos_choch_subtypes: dict[str, int] = {}
    for e in smc_dto.get("events", []):
        etype = e.get("type")
        if etype not in ("BOS", "CHoCH"):
            continue
        bullish = e.get("bullish")
        internal = e.get("internal")
        confirmed_idx = e.get("confirmed_index")
        if bullish is None or internal is None or confirmed_idx is None:
            continue
        direction = "bullish" if bullish else "bearish"
        level = "internal" if internal else "swing"
        key = f"{etype.lower()}_{direction}_{level}"
        idx = int(confirmed_idx)
        if key not in bos_choch_subtypes or idx > bos_choch_subtypes[key]:
            bos_choch_subtypes[key] = idx

    for key, best_idx in bos_choch_subtypes.items():
        factor_key = f"smc_{key}_freshness_bars"
        if factor_key in result:
            result[factor_key] = current_index - best_idx

    # --- OB formation: 按 bullish/bearish × internal/swing 拆分 ---
    # freshness 基于 OB 创建 bar（confirmed_index），不依赖触碰
    ob_formation_subtypes: dict[str, int] = {}
    for ob in smc_dto.get("order_blocks", []):
        confirmed_idx = ob.get("confirmed_index")
        bias = ob.get("bias")
        internal = ob.get("internal")
        if confirmed_idx is None or bias is None or internal is None:
            continue
        direction = "bullish" if bias == 1 else "bearish"
        level = "internal" if internal else "swing"
        key = f"ob_formation_{direction}_{level}"
        idx = int(confirmed_idx)
        if key not in ob_formation_subtypes or idx > ob_formation_subtypes[key]:
            ob_formation_subtypes[key] = idx

    for key, best_idx in ob_formation_subtypes.items():
        factor_key = f"smc_{key}_freshness_bars"
        if factor_key in result:
            result[factor_key] = current_index - best_idx

    # --- OB first_touch: 按 bullish/bearish × internal/swing 拆分 ---
    # 从创建 bar 之后搜索首次影线相交
    bars_high = bars["high"].to_numpy(dtype=float) if hasattr(bars, "__getitem__") else None
    bars_low = bars["low"].to_numpy(dtype=float) if hasattr(bars, "__getitem__") else None
    if bars_high is not None and bars_low is not None:
        ob_touch_subtypes: dict[str, int] = {}
        for ob in smc_dto.get("order_blocks", []):
            ob_high = ob.get("bar_high")
            ob_low = ob.get("bar_low")
            confirmed_idx = ob.get("confirmed_index")
            bias = ob.get("bias")
            internal = ob.get("internal")
            if ob_high is None or ob_low is None or confirmed_idx is None:
                continue
            if bias is None or internal is None:
                continue
            ob_high_f = float(ob_high)
            ob_low_f = float(ob_low)
            start_idx = int(confirmed_idx) + 1
            direction = "bullish" if bias == 1 else "bearish"
            level = "internal" if internal else "swing"
            key = f"ob_first_touch_{direction}_{level}"
            first_touch = -1
            for i in range(start_idx, len(bars)):
                if bars_high[i] >= ob_low_f and bars_low[i] <= ob_high_f:
                    first_touch = i
                    break
            if first_touch >= 0:
                if key not in ob_touch_subtypes or first_touch > ob_touch_subtypes[key]:
                    ob_touch_subtypes[key] = first_touch

        for key, best_idx in ob_touch_subtypes.items():
            factor_key = f"smc_{key}_freshness_bars"
            if factor_key in result:
                result[factor_key] = current_index - best_idx

    # --- EQH/EQL: 无 internal/swing 字段，单因子 ---
    for eqhl in smc_dto.get("equal_highs_lows", []):
        etype = eqhl.get("type")
        confirmed_idx = eqhl.get("confirmed_index")
        if etype is None or confirmed_idx is None:
            continue
        factor_key = f"smc_{etype.lower()}_freshness_bars"
        if factor_key not in result:
            continue
        idx = int(confirmed_idx)
        if result[factor_key] is None or idx > (current_index - result[factor_key]):
            result[factor_key] = current_index - idx

    return result


# =============================================================================
# event_freshness_payload 骨架
# =============================================================================


def build_empty_event_freshness_payload(
    *,
    as_of: date,
    schema_version: int = 5,
) -> dict[str, Any]:
    """构造空 event_freshness_payload 骨架（PRD §9 结构）。"""
    return {
        "daily_structure": {
            "dsa": {},
            "swing": {},
            "smc": {},
        },
        "monitor_interaction": {
            "smc": {},
            "node_cluster": {},
            "bollinger": {},
        },
        "meta": {
            "as_of": as_of.isoformat(),
            "schema_version": schema_version,
            "availability": "available",
            "degraded_reasons": [],
            "source_counts": {},
        },
    }


if __name__ == "__main__":
    # 自测入口：验证纯函数公式（不连接数据库）
    # freshness_from_bar_index
    assert freshness_from_bar_index(99, 99) == 0, "事件当根应为 0"
    assert freshness_from_bar_index(99, 98) == 1, "后一根应为 1"
    assert freshness_from_bar_index(99, 90) == 9, "9 根后应为 9"
    assert freshness_from_bar_index(99, None) is None, "None → never_observed"
    try:
        freshness_from_bar_index(99, 100)
        assert False, "event > current 应抛 ValueError"
    except ValueError:
        pass
    print("freshness_from_bar_index ✓")

    # freshness_from_trading_day
    cal = [date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22),
           date(2026, 7, 23), date(2026, 7, 24)]
    assert freshness_from_trading_day(date(2026, 7, 23), date(2026, 7, 23), cal) == 0
    assert freshness_from_trading_day(date(2026, 7, 23), date(2026, 7, 22), cal) == 1
    assert freshness_from_trading_day(date(2026, 7, 23), None, cal) is None
    try:
        freshness_from_trading_day(date(2026, 7, 23), date(2026, 7, 24), cal)
        assert False, "event > as_of 应抛 ValueError"
    except ValueError:
        pass
    print("freshness_from_trading_day ✓")

    # build_never_observed / build_unavailable
    ne = build_never_observed("smc_bos_bullish_internal")
    assert ne.state == FreshnessState.NEVER_OBSERVED
    assert ne.value is None
    assert ne.to_dict()["state"] == "never_observed"

    un = build_unavailable("smc_bos_bullish_internal", reason="smc_compute_failed")
    assert un.state == FreshnessState.UNAVAILABLE
    assert un.to_dict()["reason"] == "smc_compute_failed"

    ob = build_observed("smc_bos_bullish_internal", 5, direction="bullish", level="internal")
    assert ob.state == FreshnessState.OBSERVED
    assert ob.to_dict()["value"] == 5
    assert ob.to_dict()["direction"] == "bullish"
    print("build_never_observed / build_unavailable / build_observed ✓")

    # build_empty_event_freshness_payload
    payload = build_empty_event_freshness_payload(as_of=date(2026, 7, 23))
    assert payload["meta"]["schema_version"] == 5
    assert "daily_structure" in payload
    assert "monitor_interaction" in payload
    print("build_empty_event_freshness_payload ✓")

    # _parse_event_trade_date
    assert _parse_event_trade_date("2026-07-23T15:00:00+08:00") == date(2026, 7, 23)
    assert _parse_event_trade_date("2026-07-23") == date(2026, 7, 23)
    assert _parse_event_trade_date(None) is None
    print("_parse_event_trade_date ✓")

    print("OK")
