"""[CHANGE-20260730-011] 板块分析 V1 服务。

设计原则（ref/instruction.md §五 板块分析 V1）：
1. Chip 是可选维度，不作为板块核心门禁
2. V1 输入仅趋势、结构、动量、量能、结构事件和权威行业/概念成员关系
3. 输入门禁：published stock_core pointer 同 run、core_factor_ready=true、
   valid_for_market_aggregation=true
4. coverage >= 0.95 才可正式发布（写入 factor_publications 指针）
5. 行业与概念分开计算，成员和股票因子必须同一 trade_date
6. 禁止使用未来数据

核心流程：
1. 读取已发布 stock_core pointer（必须存在）
2. 获取板块成员 instrument_ids
3. 一次性查询所有成员的 StockFeatureSnapshot WHERE source_run_id=pointer.data_run_id
4. 从 summary_payload.first_pyramid_flat 提取 99 个 fp_ 字段
5. 计算板块分布指标（趋势/结构/动量/量能/事件率）
6. 计算 coverage_ratio = ready_count / eligible_count
7. upsert board_analysis_snapshot 记录（幂等）
8. coverage >= 0.95 时写入 factor_publications 指针

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.board_analysis_service
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.first_pyramid_semantics import (
    Direction,
    MomentumChange,
    MomentumDirection,
    SqueezeState,
    StructureAlignment,
    VolumeBadge,
)
from app.models.board_analysis_snapshot import BoardAnalysisRun, BoardAnalysisSnapshot
from app.models.factor_publication import FactorPublication
from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.services.board_membership_service import (
    PITMembership,
    PITMembershipUnavailableError,
    batch_version,
    list_universe_definitions_at,
    resolve_board_membership_at,
    resolve_universe_membership_at,
)
from app.services.factor_publication_service import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    get_publication,
    get_published_snapshot_run_id,
    publish_market_aggregation,
)
from app.services.first_pyramid_semantic_adapter import FirstPyramidSemanticAdapter

logger = logging.getLogger("board_analysis_service")

# 板块分析算法版本（每次指标/契约变更时递增）
BOARD_ANALYSIS_ALGORITHM_VERSION = "board-v1-20260730"

# 发布门禁
BOARD_ANALYSIS_MIN_COVERAGE = 0.95

# 板块分析 publication scope_type（与 market-level 区分）
SCOPE_TYPE_BOARD = "board"


def _compute_parameter_hash() -> str:
    """计算参数 hash（V1 固定参数，无外部输入）。"""
    payload = f"{BOARD_ANALYSIS_ALGORITHM_VERSION}:v1:fixed_params"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# =============================================================================
# 纯函数：分布指标计算
# =============================================================================


def _safe_float(v: Any) -> float | None:
    """安全转换为 float，None/非数值返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    """安全转换为 int。"""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    """计算平均值（空列表返回 None）。"""
    if not values:
        return None
    return sum(values) / len(values)


def _percentile(values: list[float], pct: float) -> float | None:
    """简单百分位（线性插值）。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    k = (n - 1) * pct
    f_idx = int(k)
    c_idx = min(f_idx + 1, n - 1)
    frac = k - f_idx
    return s[f_idx] + (s[c_idx] - s[f_idx]) * frac


def _bucket(values: list[float], edges: list[float]) -> dict[str, int]:
    """分桶计数。edges=[e0,e1,...,en] 表示 n+1 个桶：
    "<e0", "[e0,e1)", "[e1,e2)", ..., ">=en"
    """
    bucket: dict[str, int] = {}
    if not edges:
        bucket["all"] = len(values)
        return bucket
    labels: list[str] = []
    for i, e in enumerate(edges):
        if i == 0:
            labels.append(f"<{e}")
        else:
            labels.append(f"[{edges[i-1]},{e})")
    labels.append(f">={edges[-1]}")
    for lbl in labels:
        bucket[lbl] = 0
    for v in values:
        placed = False
        for i, e in enumerate(edges):
            if v < e:
                bucket[labels[i]] += 1
                placed = True
                break
        if not placed:
            bucket[labels[-1]] += 1
    return bucket


def compute_board_payload(
    flat_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """从成员的 first_pyramid_flat 列表计算板块指标 payload（纯函数）。

    Args:
        flat_list: 每个元素为一个成员的 first_pyramid_flat dict（99 键），
            字段缺失或为 None 时计入 missing 但不参与指标计算

    Returns:
        payload dict 包含：
        - trend_dist: {up, down, neutral}
        - trend_strength: {avg, p25, p50, p75}
        - vwap_dev_pct: {avg, p25, p50, p75}
        - structure: {swing_up, swing_down, swing_neutral,
                     alignment_aligned, alignment_misaligned, alignment_neutral,
                     avg_active_ob_count}
        - structure_events: {bos_up, bos_down, choch_up, choch_down,
                            ob_up, ob_down, eqh_present, eql_present,
                            bos_rate, choch_rate, ob_rate}
        - momentum: {positive, negative, neutral,
                    squz, released, normal,
                    enhancing, fading, flat,
                    avg_sqzmom}
        - volume: {high, low, normal, unknown,
                  avg_volume_ratio20, avg_volume_ratio200,
                  percentile_20_dist, percentile_200_dist}
        - total_members, ready_members, missing_members
    """
    total = len(flat_list)
    ready = 0
    missing = 0

    # 趋势
    trend_up = trend_down = trend_neutral = 0
    trend_strengths: list[float] = []
    vwap_devs: list[float] = []

    # 结构
    swing_up = swing_down = swing_neutral = 0
    alignment_aligned = alignment_misaligned = alignment_neutral = 0
    active_ob_counts: list[int] = []

    # 结构事件
    bos_up = bos_down = 0
    choch_up = choch_down = 0
    ob_up = ob_down = 0
    eqh_present = eql_present = 0

    # 动量
    mom_pos = mom_neg = mom_neu = 0
    squz = released = normal = 0
    enhancing = fading = mom_flat = 0
    sqzmom_values: list[float] = []

    # 量能
    vol_high = vol_low = vol_normal = vol_unknown = 0
    vol_ratio20_list: list[float] = []
    vol_ratio200_list: list[float] = []
    vol_pct20_list: list[float] = []
    vol_pct200_list: list[float] = []

    for flat in flat_list:
        if not flat or not isinstance(flat, dict):
            missing += 1
            continue
        # ready 判定：fp_trend_direction 必须非空
        semantics = FirstPyramidSemanticAdapter(flat)
        if semantics.trend is None:
            missing += 1
            continue
        ready += 1

        # === 趋势 ===
        td = semantics.trend
        if td is Direction.UP:
            trend_up += 1
        elif td is Direction.DOWN:
            trend_down += 1
        else:
            trend_neutral += 1

        ts = _safe_float(flat.get("fp_trend_strength"))
        if ts is not None:
            trend_strengths.append(ts)
        vd = _safe_float(flat.get("fp_dsa_vwap_dev_pct"))
        if vd is not None:
            vwap_devs.append(vd)

        # === 结构 ===
        sd = semantics.swing
        if sd is Direction.UP:
            swing_up += 1
        elif sd is Direction.DOWN:
            swing_down += 1
        else:
            swing_neutral += 1

        sa = semantics.structure_alignment
        if sa is StructureAlignment.ALIGNED:
            alignment_aligned += 1
        elif sa is StructureAlignment.DIVERGENT:
            alignment_misaligned += 1
        else:
            alignment_neutral += 1

        obc = _safe_int(flat.get("fp_active_ob_count"))
        if obc is not None:
            active_ob_counts.append(obc)

        # === 结构事件 ===
        # BOS 方向（最新一次 BOS）
        bos_dir = semantics.event_direction("fp_latest_bos_direction")
        if bos_dir is Direction.UP:
            bos_up += 1
        elif bos_dir is Direction.DOWN:
            bos_down += 1
        choch_dir = semantics.event_direction("fp_latest_choch_direction")
        if choch_dir is Direction.UP:
            choch_up += 1
        elif choch_dir is Direction.DOWN:
            choch_down += 1
        ob_dir = semantics.event_direction("fp_latest_ob_direction")
        if ob_dir is Direction.UP:
            ob_up += 1
        elif ob_dir is Direction.DOWN:
            ob_down += 1

        # EQH/EQL presence（freshness != null 表示存在）
        if flat.get("fp_latest_eqh_freshness") is not None:
            eqh_present += 1
        if flat.get("fp_latest_eql_freshness") is not None:
            eql_present += 1

        # === 动量 ===
        md = semantics.momentum_direction
        if md is MomentumDirection.EXPANDING:
            mom_pos += 1
        elif md is MomentumDirection.CONTRACTING:
            mom_neg += 1
        else:
            mom_neu += 1
        sqz_state = semantics.squeeze_state
        if sqz_state is SqueezeState.SQUEEZE:
            squz += 1
        elif sqz_state is SqueezeState.RELEASED:
            released += 1
        elif sqz_state is SqueezeState.NORMAL:
            normal += 1

        mc = semantics.momentum_change
        if mc is MomentumChange.ENHANCING:
            enhancing += 1
        elif mc is MomentumChange.WEAKENING:
            fading += 1
        else:
            mom_flat += 1

        sqz_val = _safe_float(flat.get("fp_sqzmom_value"))
        if sqz_val is not None:
            sqzmom_values.append(sqz_val)

        # === 量能 ===
        vb = semantics.volume_badge
        if vb is VolumeBadge.HIGH:
            vol_high += 1
        elif vb is VolumeBadge.LOW:
            vol_low += 1
        elif vb is VolumeBadge.NORMAL:
            vol_normal += 1
        else:
            vol_unknown += 1

        vr20 = _safe_float(flat.get("fp_volume_ratio20"))
        if vr20 is not None:
            vol_ratio20_list.append(vr20)
        vr200 = _safe_float(flat.get("fp_volume_ratio200"))
        if vr200 is not None:
            vol_ratio200_list.append(vr200)
        vp20 = _safe_float(flat.get("fp_volume_percentile20"))
        if vp20 is not None:
            vol_pct20_list.append(vp20)
        vp200 = _safe_float(flat.get("fp_volume_percentile200"))
        if vp200 is not None:
            vol_pct200_list.append(vp200)

    # 事件率 = 有事件的股票 / ready 成员数
    bos_rate = (bos_up + bos_down) / ready if ready > 0 else 0.0
    choch_rate = (choch_up + choch_down) / ready if ready > 0 else 0.0
    ob_rate = (ob_up + ob_down) / ready if ready > 0 else 0.0

    payload: dict[str, Any] = {
        "trend_dist": {"up": trend_up, "down": trend_down, "neutral": trend_neutral},
        "trend_strength": {
            "avg": _avg(trend_strengths),
            "p25": _percentile(trend_strengths, 0.25),
            "p50": _percentile(trend_strengths, 0.50),
            "p75": _percentile(trend_strengths, 0.75),
        },
        "vwap_dev_pct": {
            "avg": _avg(vwap_devs),
            "p25": _percentile(vwap_devs, 0.25),
            "p50": _percentile(vwap_devs, 0.50),
            "p75": _percentile(vwap_devs, 0.75),
        },
        "structure": {
            "swing_up": swing_up,
            "swing_down": swing_down,
            "swing_neutral": swing_neutral,
            "alignment_aligned": alignment_aligned,
            "alignment_misaligned": alignment_misaligned,
            "alignment_neutral": alignment_neutral,
            "avg_active_ob_count": _avg([float(c) for c in active_ob_counts]),
        },
        "structure_events": {
            "bos_up": bos_up,
            "bos_down": bos_down,
            "choch_up": choch_up,
            "choch_down": choch_down,
            "ob_up": ob_up,
            "ob_down": ob_down,
            "eqh_present": eqh_present,
            "eql_present": eql_present,
            "bos_rate": round(bos_rate, 4),
            "choch_rate": round(choch_rate, 4),
            "ob_rate": round(ob_rate, 4),
        },
        "momentum": {
            "positive": mom_pos,
            "negative": mom_neg,
            "neutral": mom_neu,
            "squeeze": squz,
            "released": released,
            "normal": normal,
            "enhancing": enhancing,
            "fading": fading,
            "flat": mom_flat,
            "avg_sqzmom": _avg(sqzmom_values),
        },
        "volume": {
            "high": vol_high,
            "low": vol_low,
            "normal": vol_normal,
            "unknown": vol_unknown,
            "avg_volume_ratio20": _avg(vol_ratio20_list),
            "avg_volume_ratio200": _avg(vol_ratio200_list),
            "percentile_20_dist": _bucket(vol_pct20_list, [20.0, 40.0, 60.0, 80.0]),
            "percentile_200_dist": _bucket(vol_pct200_list, [20.0, 40.0, 60.0, 80.0]),
        },
        "total_members": total,
        "ready_members": ready,
        "missing_members": missing,
    }
    return payload


# =============================================================================
# 第二金字塔 V2 指标（pyramid_v2）
# =============================================================================
#
# 设计原则：
# - 在现有 V1 payload 基础上扩展，新增指标统一放入 payload["pyramid_v2"]
# - 保持 V1 指标与契约不变，仅做加法
# - 所有比例同时返回 numerator/denominator，便于前端精确展示
# - 行业(industry)与概念(concept)分别计算（每个 board 独立调用一次）
# - 状态迁移/新鲜度依赖 FirstPyramidHistoryDailyState + FirstPyramidHistoryEvent
#   （chip 维度不在第一金字塔历史回补范围内，include_chip=False，
#    故 chip_cross 类指标默认 0，待 chip 历史独立回补后才有值）
#
# 维度衰减周期：趋势 5 日、结构 10 日、动量 5 日、筹码 20 日

# 事件类型 → 维度映射（衰减周期不同）
_EVENT_DIMENSION_MAP: dict[str, str] = {
    "CHoCH": "trend",
    "BOS": "structure",
    "OB_CREATED": "structure",
    "OB_ENTERED": "structure",
    "OB_MITIGATED": "structure",
    "EQH": "structure",
    "EQL": "structure",
    "SQZ_RELEASE": "momentum",
    "SQZ_OFF": "momentum",
    "MOMENTUM_DIFFUSION": "momentum",
    "node_cluster_touch": "chip",
}

# 各维度衰减窗口（日）
_DIMENSION_WINDOW: dict[str, int] = {
    "trend": 5,
    "structure": 10,
    "momentum": 5,
    "chip": 20,
}


def _event_dimension(event_type: str | None) -> str:
    """将事件类型映射到四维之一（trend/structure/momentum/chip）。"""
    if not event_type:
        return "structure"
    if event_type in _EVENT_DIMENSION_MAP:
        return _EVENT_DIMENSION_MAP[event_type]
    if event_type.startswith("ZERO_CROSS"):
        return "momentum"
    if event_type.startswith("OB_"):
        return "structure"
    if event_type.startswith("NODE") or "node" in event_type.lower():
        return "chip"
    return "structure"


def _change_magnitude(flat: dict[str, Any]) -> float:
    """提取个股变化幅度（绝对值）。

    优先级：|fp_trend_strength| > |fp_dsa_vwap_dev_pct| > 0。
    """
    ts = _safe_float(flat.get("fp_trend_strength"))
    if ts is not None:
        return abs(ts)
    vd = _safe_float(flat.get("fp_dsa_vwap_dev_pct"))
    if vd is not None:
        return abs(vd)
    return 0.0


def _build_instrument_results(
    valid_member_ids: list[uuid.UUID],
    flat_map: dict[uuid.UUID, dict[str, Any]],
    symbol_map: dict[uuid.UUID, str],
) -> list[dict[str, Any]]:
    """构建 per-instrument 结果列表（用于 V2 集中度/离散度/概念额外等）。

    仅包含能取到 first_pyramid_flat 且 fp_trend_direction 非空的成员
    （与 V1 ready 口径一致）。
    """
    results: list[dict[str, Any]] = []
    for iid in valid_member_ids:
        flat = flat_map.get(iid)
        if not flat or FirstPyramidSemanticAdapter(flat).trend is None:
            continue
        results.append({
            "instrument_id": iid,
            "symbol": symbol_map.get(iid, str(iid)),
            "flat": flat,
            "change_magnitude": _change_magnitude(flat),
        })
    return results


def _compute_scope_metrics(
    instrument_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """从 instrument_results 计算板块自身汇总指标（用于相对强弱）。"""
    if not instrument_results:
        return {
            "count": 0,
            "avg_strength": None,
            "avg_vwap_dev_pct": None,
            "up_ratio": None,
        }
    strengths: list[float] = []
    vwap_devs: list[float] = []
    up = 0
    for r in instrument_results:
        ts = _safe_float(r["flat"].get("fp_trend_strength"))
        if ts is not None:
            strengths.append(ts)
        vd = _safe_float(r["flat"].get("fp_dsa_vwap_dev_pct"))
        if vd is not None:
            vwap_devs.append(vd)
        if FirstPyramidSemanticAdapter(r["flat"]).trend is Direction.UP:
            up += 1
    n = len(instrument_results)
    return {
        "count": n,
        "avg_strength": _avg(strengths),
        "avg_vwap_dev_pct": _avg(vwap_devs),
        "up_ratio": up / n if n > 0 else None,
    }


def _relative_label(ratio: float | None) -> str | None:
    """相对强弱标签：>1.05 strong / <0.95 weak / 否则 neutral。"""
    if ratio is None:
        return None
    if ratio > 1.05:
        return "strong"
    if ratio < 0.95:
        return "weak"
    return "neutral"


# -----------------------------------------------------------------------------
# 1. 状态迁移矩阵
# -----------------------------------------------------------------------------


async def _compute_state_transitions(
    session: AsyncSession,
    board_id: uuid.UUID,
    trade_date: date,
    instrument_ids: list[uuid.UUID],
) -> dict[str, Any]:
    """计算状态迁移矩阵（趋势修复/受损、BOS/CHoCH、挤压释放、筹码越区）。

    数据来源：
    - 趋势修复/受损：对比 prev/当日 FirstPyramidHistoryDailyState 的 regime_value
    - BOS/CHoCH/SQZ_RELEASE：当日 FirstPyramidHistoryEvent 事件计数（按方向）
    - 筹码越区：第一金字塔历史不含 chip，默认 0

    所有比例同时返回 numerator/denominator。
    """
    out: dict[str, Any] = {
        "trend_repair_count": 0,
        "trend_damage_count": 0,
        "bos_count": 0,
        "bos_up_count": 0,
        "bos_down_count": 0,
        "choch_count": 0,
        "choch_up_count": 0,
        "choch_down_count": 0,
        "squeeze_release_count": 0,
        "chip_cross_up_count": 0,
        "chip_cross_down_count": 0,
        "migrated_instrument_count": 0,
        "compared_count": 0,
        "today_state_count": 0,
        "total_instrument_ids": len(instrument_ids),
        "trend_repair_ratio": {"numerator": 0, "denominator": 0},
        "trend_damage_ratio": {"numerator": 0, "denominator": 0},
        "bos_ratio": {"numerator": 0, "denominator": 0},
        "choch_ratio": {"numerator": 0, "denominator": 0},
        "squeeze_release_ratio": {"numerator": 0, "denominator": 0},
        "chip_cross_up_ratio": {"numerator": 0, "denominator": 0},
        "chip_cross_down_ratio": {"numerator": 0, "denominator": 0},
        "migration_ratio": {"numerator": 0, "denominator": 0},
    }
    if not instrument_ids:
        return out

    # 1. 今日 daily_state
    today_rows = (
        await session.execute(
            select(
                FirstPyramidHistoryDailyState.instrument_id,
                FirstPyramidHistoryDailyState.state_payload,
            ).where(
                FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
                FirstPyramidHistoryDailyState.trade_date == trade_date,
            )
        )
    ).all()
    today_states: dict[uuid.UUID, dict[str, Any]] = {}
    for row in today_rows:
        sp = row[1] if isinstance(row[1], dict) else {}
        today_states[row[0]] = sp
    out["today_state_count"] = len(today_states)

    # 2. 前一交易日 = MAX(trade_date) < trade_date（跨 instrument）
    prev_date = await session.scalar(
        select(func.max(FirstPyramidHistoryDailyState.trade_date)).where(
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            FirstPyramidHistoryDailyState.trade_date < trade_date,
        )
    )
    prev_states: dict[uuid.UUID, dict[str, Any]] = {}
    if prev_date is not None:
        prev_rows = (
            await session.execute(
                select(
                    FirstPyramidHistoryDailyState.instrument_id,
                    FirstPyramidHistoryDailyState.state_payload,
                ).where(
                    FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
                    FirstPyramidHistoryDailyState.trade_date == prev_date,
                )
            )
        ).all()
        for row in prev_rows:
            sp = row[1] if isinstance(row[1], dict) else {}
            prev_states[row[0]] = sp

    # 3. 趋势修复/受损（regime_value 比较）
    migrated: set[uuid.UUID] = set()
    compared = 0
    for iid, today_sp in today_states.items():
        prev_sp = prev_states.get(iid)
        if prev_sp is None:
            continue
        compared += 1
        today_rv = _safe_int(today_sp.get("regime_value"))
        prev_rv = _safe_int(prev_sp.get("regime_value"))
        if today_rv is None or prev_rv is None:
            continue
        # 修复：今日上行且前日非上行
        if today_rv > 0 and prev_rv <= 0:
            out["trend_repair_count"] += 1
            migrated.add(iid)
        # 受损：今日下行且前日非下行
        elif today_rv < 0 and prev_rv >= 0:
            out["trend_damage_count"] += 1
            migrated.add(iid)
    out["compared_count"] = compared

    # 4. 当日结构/动量事件（BOS/CHoCH/SQZ_RELEASE）按方向计数
    date_prefix = f"{trade_date.isoformat()}%"
    event_rows = (
        await session.execute(
            select(
                FirstPyramidHistoryEvent.instrument_id,
                FirstPyramidHistoryEvent.event_type,
                FirstPyramidHistoryEvent.event_payload,
            ).where(
                FirstPyramidHistoryEvent.instrument_id.in_(instrument_ids),
                FirstPyramidHistoryEvent.event_time.isnot(None),
                FirstPyramidHistoryEvent.event_time.like(date_prefix),
            )
        )
    ).all()
    for row in event_rows:
        iid = row[0]
        etype = row[1]
        epayload = row[2] if isinstance(row[2], dict) else {}
        direction = epayload.get("direction")
        if etype == "BOS":
            out["bos_count"] += 1
            if direction == "up":
                out["bos_up_count"] += 1
            elif direction == "down":
                out["bos_down_count"] += 1
            migrated.add(iid)
        elif etype == "CHoCH":
            out["choch_count"] += 1
            if direction == "up":
                out["choch_up_count"] += 1
            elif direction == "down":
                out["choch_down_count"] += 1
            migrated.add(iid)
        elif etype == "SQZ_RELEASE":
            out["squeeze_release_count"] += 1
            migrated.add(iid)

    out["migrated_instrument_count"] = len(migrated)
    today_den = out["today_state_count"]
    out["trend_repair_ratio"] = {
        "numerator": out["trend_repair_count"], "denominator": compared,
    }
    out["trend_damage_ratio"] = {
        "numerator": out["trend_damage_count"], "denominator": compared,
    }
    out["bos_ratio"] = {"numerator": out["bos_count"], "denominator": today_den}
    out["choch_ratio"] = {"numerator": out["choch_count"], "denominator": today_den}
    out["squeeze_release_ratio"] = {
        "numerator": out["squeeze_release_count"], "denominator": today_den,
    }
    out["chip_cross_up_ratio"] = {"numerator": 0, "denominator": today_den}
    out["chip_cross_down_ratio"] = {"numerator": 0, "denominator": today_den}
    out["migration_ratio"] = {
        "numerator": len(migrated), "denominator": compared,
    }
    return out


# -----------------------------------------------------------------------------
# 2. 新鲜度密度
# -----------------------------------------------------------------------------


async def _compute_freshness_density(
    session: AsyncSession,
    board_id: uuid.UUID,
    trade_date: date,
    instrument_ids: list[uuid.UUID],
) -> dict[str, Any]:
    """计算当日/近N日/衰减加权的事件密度。

    四维可使用不同衰减周期（趋势 5 日、结构 10 日、动量 5 日、筹码 20 日）。
    事件窗口覆盖最大衰减周期 20 日；density = weighted_sum / instrument_count。
    """
    out: dict[str, Any] = {
        "today_count": 0,
        "last_5d_count": 0,
        "last_10d_count": 0,
        "last_20d_count": 0,
        "instrument_count": len(instrument_ids),
        "by_dimension": {
            dim: {
                "window_days": _DIMENSION_WINDOW[dim],
                "event_count": 0,
                "weighted_sum": 0.0,
                "density": 0.0,
            }
            for dim in ("trend", "structure", "momentum", "chip")
        },
        "decay_weighted_density": 0.0,
    }
    if not instrument_ids:
        return out

    inst_count = len(instrument_ids)
    start_iso = (trade_date - timedelta(days=20)).isoformat()
    rows = (
        await session.execute(
            select(
                FirstPyramidHistoryEvent.event_type,
                FirstPyramidHistoryEvent.event_time,
            ).where(
                FirstPyramidHistoryEvent.instrument_id.in_(instrument_ids),
                FirstPyramidHistoryEvent.event_time.isnot(None),
                FirstPyramidHistoryEvent.event_time >= start_iso,
            )
        )
    ).all()

    for etype, etime in rows:
        if not etime:
            continue
        try:
            ev_date = date.fromisoformat(etime[:10])
        except ValueError:
            continue
        days_ago = (trade_date - ev_date).days
        if days_ago < 0:
            continue
        out["last_20d_count"] += 1
        if days_ago <= 5:
            out["last_5d_count"] += 1
        if days_ago <= 10:
            out["last_10d_count"] += 1
        if days_ago == 0:
            out["today_count"] += 1
        dim = _event_dimension(etype)
        d = out["by_dimension"][dim]
        d["event_count"] += 1
        window = d["window_days"]
        w = max(0.0, 1.0 - days_ago / window) if window > 0 else 1.0
        d["weighted_sum"] = round(d["weighted_sum"] + w, 6)

    total_weighted = 0.0
    for d in out["by_dimension"].values():
        d["density"] = (
            round(d["weighted_sum"] / inst_count, 6) if inst_count > 0 else 0.0
        )
        total_weighted += d["weighted_sum"]
    # 整体衰减加权密度：四维 weighted_sum 平均 / instrument_count
    out["decay_weighted_density"] = (
        round(total_weighted / 4 / inst_count, 6) if inst_count > 0 else 0.0
    )
    return out


# -----------------------------------------------------------------------------
# 3. 扩散
# -----------------------------------------------------------------------------


def _compute_diffusion(state_transitions: dict[str, Any]) -> dict[str, Any]:
    """基于状态迁移计算扩散度（正负迁移数量、比例、参与覆盖率）。"""
    st = state_transitions or {}
    positive = (
        st.get("trend_repair_count", 0)
        + st.get("bos_up_count", 0)
        + st.get("choch_up_count", 0)
        + st.get("squeeze_release_count", 0)
        + st.get("chip_cross_up_count", 0)
    )
    negative = (
        st.get("trend_damage_count", 0)
        + st.get("bos_down_count", 0)
        + st.get("choch_down_count", 0)
        + st.get("chip_cross_down_count", 0)
    )
    total = positive + negative
    migrated = st.get("migrated_instrument_count", 0)
    compared = st.get("compared_count", 0)
    return {
        "positive_migration_count": positive,
        "negative_migration_count": negative,
        "total_migration_count": total,
        "positive_ratio": {"numerator": positive, "denominator": total},
        "negative_ratio": {"numerator": negative, "denominator": total},
        "participation_coverage": {
            "numerator": migrated,
            "denominator": compared,
        },
    }


# -----------------------------------------------------------------------------
# 4. 集中度
# -----------------------------------------------------------------------------


def _compute_concentration(
    instrument_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算 Top3/Top5 贡献度、HHI、龙头与中位数差。"""
    valid = [r for r in instrument_results if r.get("change_magnitude") is not None]
    mags = [r["change_magnitude"] for r in valid]
    n = len(mags)
    out: dict[str, Any] = {
        "top3_contribution": {"numerator": 0.0, "denominator": 0.0},
        "top5_contribution": {"numerator": 0.0, "denominator": 0.0},
        "hhi": 0.0,
        "leader_median_gap": None,
        "leader_symbol": None,
        "leader_magnitude": None,
        "median_magnitude": None,
        "count": n,
    }
    if n == 0:
        return out

    s = sorted(mags, reverse=True)
    total = sum(s)
    out["top3_contribution"] = {
        "numerator": round(sum(s[:3]), 6), "denominator": round(total, 6),
    }
    out["top5_contribution"] = {
        "numerator": round(sum(s[:5]), 6), "denominator": round(total, 6),
    }
    # HHI（归一化到 [0,1]：sum((share)^2)）
    if total > 0:
        out["hhi"] = round(sum((m / total) ** 2 for m in mags), 6)

    leader = max(valid, key=lambda r: r["change_magnitude"])
    out["leader_symbol"] = leader.get("symbol")
    out["leader_magnitude"] = round(leader["change_magnitude"], 6)
    med = _percentile(mags, 0.5)
    out["median_magnitude"] = round(med, 6) if med is not None else None
    out["leader_median_gap"] = round(
        leader["change_magnitude"] - (med or 0.0), 6,
    )
    return out


# -----------------------------------------------------------------------------
# 5. 内部离散度
# -----------------------------------------------------------------------------


def _compute_dispersion(
    instrument_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算板块内部离散度（标准差/变异系数/分位差）。"""
    mags = [r["change_magnitude"] for r in instrument_results if r.get("change_magnitude") is not None]
    n = len(mags)
    out: dict[str, Any] = {
        "count": n,
        "mean": None,
        "std": None,
        "cv": None,
        "p25": None,
        "p50": None,
        "p75": None,
        "iqr": None,
        "min": None,
        "max": None,
        "range": None,
    }
    if n == 0:
        return out

    mean = sum(mags) / n
    var = sum((m - mean) ** 2 for m in mags) / n  # 总体方差
    std = var ** 0.5
    p25 = _percentile(mags, 0.25)
    p50 = _percentile(mags, 0.50)
    p75 = _percentile(mags, 0.75)
    mn, mx = min(mags), max(mags)
    out["mean"] = round(mean, 6)
    out["std"] = round(std, 6)
    out["cv"] = round(std / mean, 6) if mean != 0 else None
    out["p25"] = round(p25, 6) if p25 is not None else None
    out["p50"] = round(p50, 6) if p50 is not None else None
    out["p75"] = round(p75, 6) if p75 is not None else None
    out["iqr"] = (
        round(p75 - p25, 6)
        if p75 is not None and p25 is not None else None
    )
    out["min"] = round(mn, 6)
    out["max"] = round(mx, 6)
    out["range"] = round(mx - mn, 6)
    return out


# -----------------------------------------------------------------------------
# 6. 相对强弱
# -----------------------------------------------------------------------------


def _compute_relative_strength(
    scope_metrics: dict[str, Any],
    market_metrics: dict[str, Any] | None,
    parent_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """计算相对市场、相对上级行业（同类型 cohort）强弱。

    market_metrics = 全市场 peer（all types）avg_strength
    parent_metrics = 同类型 peer（industry/concept cohort）avg_strength
    缺失 peer 时对应 ratio 为 None（如实标记不可比）。
    """
    out: dict[str, Any] = {
        "vs_market": {"ratio": None, "label": None, "diff": None},
        "vs_parent": {"ratio": None, "label": None, "diff": None},
        "equal_weight_diff": None,
        "scope": scope_metrics,
        "market": market_metrics,
        "parent": parent_metrics,
    }
    s = scope_metrics or {}
    s_strength = _safe_float(s.get("avg_strength"))

    if market_metrics is not None:
        m_strength = _safe_float(market_metrics.get("avg_strength"))
        if s_strength is not None and m_strength is not None and m_strength != 0:
            r = s_strength / m_strength
            out["vs_market"] = {
                "ratio": round(r, 4),
                "label": _relative_label(r),
                "diff": round(s_strength - m_strength, 6),
            }

    if parent_metrics is not None:
        p_strength = _safe_float(parent_metrics.get("avg_strength"))
        if s_strength is not None and p_strength is not None and p_strength != 0:
            r = s_strength / p_strength
            out["vs_parent"] = {
                "ratio": round(r, 4),
                "label": _relative_label(r),
                "diff": round(s_strength - p_strength, 6),
            }

    out["equal_weight_diff"] = out["vs_market"]["diff"]
    return out


# -----------------------------------------------------------------------------
# 7. 概念额外（核心/边缘成员、置信度）
# -----------------------------------------------------------------------------


def _compute_concept_extras(
    board_id: uuid.UUID,
    instrument_results: list[dict[str, Any]],
    total_members: int,
) -> dict[str, Any]:
    """计算核心/边缘成员、置信度。

    核心成员 = 与板块方向一致且幅度超过中位数
    边缘成员 = 方向不一致或幅度低于中位数
    """
    valid = [r for r in instrument_results if r.get("change_magnitude") is not None]
    out: dict[str, Any] = {
        "core_count": 0,
        "peripheral_count": 0,
        "core_coverage": {"numerator": 0, "denominator": total_members},
        "confidence_level": "low",
        "board_direction": None,
        "median_magnitude": None,
        "ready_count": len(valid),
    }
    if not valid:
        return out

    # 板块方向：fp_trend_direction 多数票（上行 vs 下行）
    up = sum(
        1 for r in valid
        if FirstPyramidSemanticAdapter(r["flat"]).trend is Direction.UP
    )
    down = sum(
        1 for r in valid
        if FirstPyramidSemanticAdapter(r["flat"]).trend is Direction.DOWN
    )
    if up > down:
        board_dir = Direction.UP
    elif down > up:
        board_dir = Direction.DOWN
    else:
        board_dir = None  # 平票 / 震荡为主
    out["board_direction"] = board_dir.value if board_dir is not None else None

    mags = [r["change_magnitude"] for r in valid]
    med = _percentile(mags, 0.5)
    out["median_magnitude"] = round(med, 6) if med is not None else None

    core = 0
    peripheral = 0
    for r in valid:
        aligned = (
            board_dir is not None
            and FirstPyramidSemanticAdapter(r["flat"]).trend is board_dir
        )
        strong = med is not None and r["change_magnitude"] > med
        if aligned and strong:
            core += 1
        else:
            peripheral += 1
    out["core_count"] = core
    out["peripheral_count"] = peripheral
    out["core_coverage"] = {"numerator": core, "denominator": total_members}

    cov = core / total_members if total_members > 0 else 0.0
    if cov >= 0.5:
        out["confidence_level"] = "high"
    elif cov >= 0.3:
        out["confidence_level"] = "medium"
    else:
        out["confidence_level"] = "low"
    return out


# -----------------------------------------------------------------------------
# V2 辅助查询
# -----------------------------------------------------------------------------


async def _fetch_symbols(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """批量查询 instrument_id → symbol 映射。"""
    if not instrument_ids:
        return {}
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol).where(
                Instrument.id.in_(instrument_ids),
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _compute_peer_metrics(
    session: AsyncSession,
    trade_date: date,
    algorithm_version: str,
    board_id: uuid.UUID,
    board_type: str,
    *,
    by_type: bool,
) -> dict[str, Any] | None:
    """从同 trade_date 已持久化的 peer 快照计算市场/同类型 avg_strength。

    market：by_type=False（全市场 peer）
    parent：by_type=True（同类型 cohort peer）
    无 peer 时返回 None（如实标记不可比）。

    注：peer avg_strength 取自 V1 payload.trend_strength.avg（数值字段，
    不受 fp 方向标签本地化影响）。
    """
    stmt = select(BoardAnalysisSnapshot).where(
        BoardAnalysisSnapshot.trade_date == trade_date,
        BoardAnalysisSnapshot.algorithm_version == algorithm_version,
        BoardAnalysisSnapshot.status == "succeeded",
        BoardAnalysisSnapshot.board_id != board_id,
    )
    if by_type:
        stmt = stmt.where(BoardAnalysisSnapshot.board_type == board_type)
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return None

    strengths: list[float] = []
    for snap in rows:
        pl = snap.payload if isinstance(snap.payload, dict) else {}
        ts_raw = pl.get("trend_strength")
        ts = ts_raw if isinstance(ts_raw, dict) else {}
        avg_ts = _safe_float(ts.get("avg"))
        if avg_ts is not None:
            strengths.append(avg_ts)
    return {
        "count": len(rows),
        "avg_strength": _avg(strengths),
    }


# =============================================================================
# 数据库查询
# =============================================================================


async def _fetch_member_snapshots(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    source_run_id: uuid.UUID,
) -> dict[uuid.UUID, dict[str, Any]]:
    """批量查询成员股票在指定 run 下的 first_pyramid_flat。

    Returns:
        {instrument_id: first_pyramid_flat dict}，缺失成员不在结果中
    """
    if not instrument_ids:
        return {}

    stmt = (
        select(
            StockFeatureSnapshot.instrument_id,
            StockFeatureSnapshot.summary_payload,
        )
        .where(
            StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            StockFeatureSnapshot.source_run_id == source_run_id,
        )
    )
    result = await session.execute(stmt)
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for row in result:
        instrument_id = row[0]
        summary = row[1] or {}
        if not isinstance(summary, dict):
            continue
        flat = summary.get("first_pyramid_flat")
        if isinstance(flat, dict):
            out[instrument_id] = flat
    return out


async def _is_instrument_valid_for_aggregation(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> bool:
    """检查 instrument 是否可参与板块聚合（valid_for_market_aggregation）。

    退市股不参与聚合（symbol 后缀 .ST/.退/Status=delisted）。
    """
    from app.models.instrument import Instrument

    inst = await session.get(Instrument, instrument_id)
    if inst is None:
        return False
    # 简化：active 列表外的不参与（status != 'active'）
    return inst.status == "active"


# =============================================================================
# 主入口：compute_board_analysis
# =============================================================================


async def compute_board_analysis(
    session: AsyncSession,
    board_id: uuid.UUID,
    trade_date: date,
    *,
    source_core_run_id: uuid.UUID | None = None,
    board_analysis_run_id: uuid.UUID | None = None,
    pit_membership: PITMembership | None = None,
    algorithm_version: str = BOARD_ANALYSIS_ALGORITHM_VERSION,
    parameter_hash: str | None = None,
) -> BoardAnalysisSnapshot:
    """计算单个板块的分析快照并 upsert。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        board_id: 板块 ID
        trade_date: 业务交易日
        source_core_run_id: 输入 stock_core run_id（None 时从 publication pointer 读取）
        algorithm_version: 算法版本
        parameter_hash: 参数 hash（None 时自动计算）

    Returns:
        BoardAnalysisSnapshot ORM 对象
    """
    # 1. 读取已发布 stock_core pointer（若未指定 source_core_run_id）
    if source_core_run_id is None:
        source_core_run_id = await get_published_snapshot_run_id(
            session, trade_date, publication_kind="stock_core",
        )
        if source_core_run_id is None:
            raise ValueError(
                f"板块分析失败: trade_date={trade_date} 无已发布 stock_core pointer",
            )

    # 2. 查询板块信息
    board = await session.get(MarketBoard, board_id)
    if board is None:
        raise ValueError(f"板块不存在: board_id={board_id}")

    # 3. 使用交易日当时有效的 PIT 成员，禁止当前投影回填历史。
    membership = pit_membership or await resolve_board_membership_at(
        session, board_id, trade_date,
    )
    standalone_run = board_analysis_run_id is None
    if standalone_run:
        batch_run = BoardAnalysisRun(
            trade_date=trade_date,
            source_core_run_id=source_core_run_id,
            taxonomy_version=f"single:{membership.taxonomy_version}",
            taxonomy_compatibility_key=membership.compatibility_key,
            membership_version=(
                f"single:{board_id}:{membership.membership_version}"
            ),
            algorithm_version=algorithm_version,
            expected_count=1,
            succeeded_count=0,
            failed_count=0,
            coverage_ratio=0.0,
            status="running",
            blockers=[],
        )
        session.add(batch_run)
        await session.flush()
        board_analysis_run_id = batch_run.id
    assert board_analysis_run_id is not None
    member_ids = list(membership.instrument_ids)
    eligible_count = len(member_ids)
    if eligible_count == 0:
        # 空板块：直接写入空快照（避免后续 None 除零）
        payload = compute_board_payload([])
        payload["pyramid_v2"] = {}
        snapshot = await _upsert_snapshot(
            session,
            board=board,
            trade_date=trade_date,
            source_core_run_id=source_core_run_id,
            board_analysis_run_id=board_analysis_run_id,
            taxonomy_version=membership.taxonomy_version,
            taxonomy_compatibility_key=membership.compatibility_key,
            membership_version=membership.membership_version,
            algorithm_version=algorithm_version,
            parameter_hash=parameter_hash or _compute_parameter_hash(),
            eligible_count=0,
            ready_count=0,
            coverage_ratio=0.0,
            missing_count=0,
            missing_reasons={},
            payload=payload,
            status="blocked_external_population",
            error_message=(
                "blocked_external_population: PIT membership is empty"
            ),
        )
        if board_analysis_run_id is not None:
            run = await session.get(BoardAnalysisRun, board_analysis_run_id)
            if run is not None:
                run.succeeded_count = 0
                run.failed_count = 1
                run.coverage_ratio = 0.0
                run.status = "blocked_external_population"
                run.blockers = [{
                    "code": "blocked_external_population",
                    "board_id": str(board_id),
                }]
        return snapshot

    # 4. 一次性查询所有成员的 first_pyramid_flat
    flat_map = await _fetch_member_snapshots(session, member_ids, source_core_run_id)

    # 5. 过滤退市股（valid_for_market_aggregation=false）
    valid_member_ids = [
        iid for iid in member_ids
        if await _is_instrument_valid_for_aggregation(session, iid)
    ]

    # 6. 构建 flat_list：valid 成员中能取到 first_pyramid_flat 的
    flat_list: list[dict[str, Any]] = []
    missing_count = 0
    missing_reasons: dict[str, int] = {}

    for iid in valid_member_ids:
        flat = flat_map.get(iid)
        if flat is None:
            missing_count += 1
            missing_reasons["SNAPSHOT_MISSING"] = (
                missing_reasons.get("SNAPSHOT_MISSING", 0) + 1
            )
        elif FirstPyramidSemanticAdapter(flat).trend is None:
            missing_count += 1
            missing_reasons["FP_TREND_MISSING"] = (
                missing_reasons.get("FP_TREND_MISSING", 0) + 1
            )
        else:
            flat_list.append(flat)

    # 7. 计算指标 payload
    payload = compute_board_payload(flat_list)

    # 7.1 第二金字塔 V2 指标（pyramid_v2）
    #     保持 V1 指标不变，新增指标合并到 payload["pyramid_v2"] 子键下。
    symbol_map = await _fetch_symbols(session, valid_member_ids)
    instrument_results = _build_instrument_results(
        valid_member_ids, flat_map, symbol_map,
    )
    ready_ids = [r["instrument_id"] for r in instrument_results]
    state_transitions = await _compute_state_transitions(
        session, board.id, trade_date, ready_ids,
    )
    freshness = await _compute_freshness_density(
        session, board.id, trade_date, ready_ids,
    )
    diffusion = _compute_diffusion(state_transitions)
    concentration = _compute_concentration(instrument_results)
    dispersion = _compute_dispersion(instrument_results)
    scope_metrics = _compute_scope_metrics(instrument_results)
    market_metrics = await _compute_peer_metrics(
        session, trade_date, algorithm_version,
        board.id, board.type, by_type=False,
    )
    parent_metrics = await _compute_peer_metrics(
        session, trade_date, algorithm_version,
        board.id, board.type, by_type=True,
    )
    relative_strength = _compute_relative_strength(
        scope_metrics, market_metrics, parent_metrics,
    )
    concept_extras = _compute_concept_extras(
        board.id, instrument_results, eligible_count,
    )
    payload["pyramid_v2"] = {
        "state_transitions": state_transitions,
        "freshness": freshness,
        "diffusion": diffusion,
        "concentration": concentration,
        "dispersion": dispersion,
        "relative_strength": relative_strength,
        "concept_extras": concept_extras,
    }

    # eligible_count = 全部成员（含退市股），ready_count = 有效且 first_pyramid 完整的
    # 退市股不在 valid_member_ids 中，不进入 missing 计算
    eligible_for_coverage = len(valid_member_ids)
    ready_count = eligible_for_coverage - missing_count
    coverage_ratio = (
        ready_count / eligible_for_coverage if eligible_for_coverage > 0 else 0.0
    )

    # 8. upsert snapshot 记录
    snapshot = await _upsert_snapshot(
        session,
        board=board,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        board_analysis_run_id=board_analysis_run_id,
        taxonomy_version=membership.taxonomy_version,
        taxonomy_compatibility_key=membership.compatibility_key,
        membership_version=membership.membership_version,
        algorithm_version=algorithm_version,
        parameter_hash=parameter_hash or _compute_parameter_hash(),
        eligible_count=eligible_count,
        ready_count=ready_count,
        coverage_ratio=coverage_ratio,
        missing_count=missing_count,
        missing_reasons=missing_reasons,
        payload=payload,
        status=(
            "succeeded"
            if coverage_ratio >= BOARD_ANALYSIS_MIN_COVERAGE
            else "partial"
        ),
        error_message=None,
    )

    if standalone_run:
        run = await session.get(BoardAnalysisRun, board_analysis_run_id)
        if run is not None:
            run.succeeded_count = int(snapshot.status == "succeeded")
            run.failed_count = int(snapshot.status != "succeeded")
            run.coverage_ratio = float(snapshot.status == "succeeded")
            run.status = snapshot.status

    logger.info(
        "[BoardAnalysis] board=%s/%s, eligible=%d, ready=%d, coverage=%.4f, status=%s",
        board.type, board.name, eligible_count, ready_count, coverage_ratio,
        snapshot.status,
    )

    return snapshot


async def _upsert_snapshot(
    session: AsyncSession,
    *,
    board: MarketBoard,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    board_analysis_run_id: uuid.UUID,
    taxonomy_version: str,
    taxonomy_compatibility_key: str,
    membership_version: str,
    algorithm_version: str,
    parameter_hash: str,
    eligible_count: int,
    ready_count: int,
    coverage_ratio: float,
    missing_count: int,
    missing_reasons: dict[str, int],
    payload: dict[str, Any],
    status: str,
    error_message: str | None,
) -> BoardAnalysisSnapshot:
    """upsert board_analysis_snapshot 记录。

    唯一键 (board_analysis_run_id, board_id) 保证同一不可变批次内幂等。
    """
    now = datetime.now(UTC)

    # 先查现有记录（upsert 需要保留 started_at/created_at）
    existing_stmt = (
        select(BoardAnalysisSnapshot)
        .where(
            BoardAnalysisSnapshot.board_analysis_run_id == board_analysis_run_id,
            BoardAnalysisSnapshot.board_id == board.id,
        )
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()

    if existing is None:
        # 插入新记录
        snapshot = BoardAnalysisSnapshot(
            trade_date=trade_date,
            board_id=board.id,
            board_type=board.type,
            board_name=board.name,
            source_core_run_id=source_core_run_id,
            board_analysis_run_id=board_analysis_run_id,
            taxonomy_version=taxonomy_version,
            taxonomy_compatibility_key=taxonomy_compatibility_key,
            membership_version=membership_version,
            algorithm_version=algorithm_version,
            parameter_hash=parameter_hash,
            eligible_count=eligible_count,
            ready_count=ready_count,
            coverage_ratio=coverage_ratio,
            missing_count=missing_count,
            missing_reasons=missing_reasons,
            status=status,
            payload=payload,
            error_message=error_message,
            started_at=now,
            finished_at=now,
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    # 更新现有记录
    existing.board_type = board.type
    existing.board_name = board.name
    existing.source_core_run_id = source_core_run_id
    existing.board_analysis_run_id = board_analysis_run_id
    existing.taxonomy_version = taxonomy_version
    existing.taxonomy_compatibility_key = taxonomy_compatibility_key
    existing.membership_version = membership_version
    existing.parameter_hash = parameter_hash
    existing.eligible_count = eligible_count
    existing.ready_count = ready_count
    existing.coverage_ratio = coverage_ratio
    existing.missing_count = missing_count
    existing.missing_reasons = missing_reasons
    existing.status = status
    existing.payload = payload
    existing.error_message = error_message
    existing.finished_at = now
    await session.flush()
    return existing


# =============================================================================
# 发布指针
# =============================================================================


async def publish_board_analysis(
    session: AsyncSession,
    snapshot: BoardAnalysisSnapshot,
    *,
    threshold: float = BOARD_ANALYSIS_MIN_COVERAGE,
) -> FactorPublication | None:
    """发布板块分析：写入 factor_publications 指针（scope_type=board）。

    coverage_ratio < threshold 时不发布，返回 None。

    Args:
        session: 异步 DB 会话
        snapshot: 已计算完成的板块分析快照
        threshold: 发布门禁（默认 0.95）

    Returns:
        FactorPublication 记录（已发布）或 None（覆盖率不足）
    """
    import json

    if snapshot.board_analysis_run_id is None:
        raise ValueError("新 Board publication 必须指向真实 board_analysis_run")
    if snapshot.status != "succeeded":
        return None
    if snapshot.coverage_ratio < threshold:
        logger.info(
            "[BoardAnalysis] 不发布: board=%s, coverage=%.4f < threshold=%.4f",
            snapshot.board_name, snapshot.coverage_ratio, threshold,
        )
        return None

    now = datetime.now(UTC)
    meta = {
        "board_type": snapshot.board_type,
        "board_name": snapshot.board_name,
        "source_core_run_id": str(snapshot.source_core_run_id),
        "coverage_ratio": snapshot.coverage_ratio,
        "ready_count": snapshot.ready_count,
        "eligible_count": snapshot.eligible_count,
        "board_analysis_run_id": str(snapshot.board_analysis_run_id),
        "taxonomy_version": snapshot.taxonomy_version,
        "taxonomy_compatibility_key": snapshot.taxonomy_compatibility_key,
        "membership_version": snapshot.membership_version,
    }

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_BOARD,
        scope_key=str(snapshot.board_id),
        trade_date=snapshot.trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
        algorithm_version=snapshot.algorithm_version,
        data_run_id=snapshot.board_analysis_run_id,
        coverage_ratio=snapshot.coverage_ratio,
        published_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_key", "trade_date", "publication_kind"],
        index_where=text("superseded_by IS NULL"),
        set_={
            "algorithm_version": stmt.excluded.algorithm_version,
            "data_run_id": stmt.excluded.data_run_id,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "published_at": stmt.excluded.published_at,
            "metadata_json": stmt.excluded.metadata_json,
        },
    )
    await session.execute(stmt)
    await session.flush()

    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_BOARD,
        scope_key=str(snapshot.board_id),
        trade_date=snapshot.trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
    )
    logger.info(
        "[BoardAnalysis] 发布: board=%s, trade_date=%s, coverage=%.4f, snapshot_id=%s",
        snapshot.board_name, snapshot.trade_date, snapshot.coverage_ratio, snapshot.id,
    )
    return pub


async def get_published_board_snapshot_id(
    session: AsyncSession,
    board_id: uuid.UUID,
    trade_date: date,
) -> uuid.UUID | None:
    """Resolve both legacy snapshot pointers and new batch-run pointers."""
    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_BOARD,
        scope_key=str(board_id),
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
    )
    if pub is None:
        return None
    legacy = await session.get(BoardAnalysisSnapshot, pub.data_run_id)
    if legacy is not None and legacy.board_id == board_id:
        return legacy.id
    stmt = (
        select(BoardAnalysisSnapshot.id)
        .where(
            BoardAnalysisSnapshot.board_analysis_run_id == pub.data_run_id,
            BoardAnalysisSnapshot.board_id == board_id,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# =============================================================================
# 批量计算
# =============================================================================


async def compute_all_boards(
    session: AsyncSession,
    trade_date: date,
    *,
    board_type: str | None = None,
    limit: int | None = None,
    publish: bool = True,
    algorithm_version: str = BOARD_ANALYSIS_ALGORITHM_VERSION,
) -> dict[str, Any]:
    """Compute one immutable Board batch with PIT membership and real run identity."""
    source_core_run_id = await get_published_snapshot_run_id(
        session, trade_date, publication_kind="stock_core",
    )
    if source_core_run_id is None:
        raise ValueError(f"trade_date={trade_date} 无已发布 stock_core pointer")

    stmt = (
        select(MarketBoard)
        .where(MarketBoard.isActive.is_(True))
        .order_by(MarketBoard.name.asc())
    )
    if board_type in ("industry", "concept"):
        stmt = stmt.where(MarketBoard.type == board_type)
    if limit is not None:
        stmt = stmt.limit(limit)
    boards = list((await session.execute(stmt)).scalars())

    memberships: dict[uuid.UUID, PITMembership] = {}
    blockers: list[dict[str, Any]] = []
    formal_batch = board_type is None and limit is None
    if not formal_batch:
        blockers.append({
            "code": "partial_batch_request",
            "reason": "filtered/limited Board runs cannot publish market pointer",
        })

    for board in boards:
        try:
            membership = await resolve_board_membership_at(
                session, board.id, trade_date,
            )
            if (
                membership.population_status != "ready"
                or not membership.instrument_ids
            ):
                blockers.append({
                    "code": "blocked_external_population",
                    "scope_type": "board",
                    "scope_key": str(board.id),
                    "reason": membership.population_status,
                })
                continue
            memberships[board.id] = membership
        except PITMembershipUnavailableError as exc:
            blockers.append({
                "code": "blocked_external_population",
                "scope_type": "board",
                "scope_key": str(board.id),
                "reason": str(exc),
            })

    universe_definitions = (
        await list_universe_definitions_at(session, trade_date)
        if formal_batch
        else []
    )
    universe_memberships: list[PITMembership] = []
    universe_details: list[dict[str, Any]] = []
    for definition in universe_definitions:
        universe_membership: PITMembership | None
        try:
            _definition, resolved_universe_membership = (
                await resolve_universe_membership_at(
                    session, definition.universe_key, trade_date,
                )
            )
            universe_membership = resolved_universe_membership
            reason = resolved_universe_membership.population_status
        except PITMembershipUnavailableError as exc:
            universe_membership = None
            reason = str(exc)
        ready = bool(
            universe_membership is not None
            and universe_membership.population_status == "ready"
            and universe_membership.instrument_ids
        )
        universe_details.append({
            "scope_type": definition.universe_type,
            "scope_key": definition.universe_key,
            "scope_name": definition.name,
            "status": "succeeded" if ready else "blocked_external_population",
            "population_status": reason,
            "published": False,
        })
        if ready and universe_membership is not None:
            universe_memberships.append(universe_membership)
        else:
            blockers.append({
                "code": "blocked_external_population",
                "scope_type": definition.universe_type,
                "scope_key": definition.universe_key,
                "reason": reason,
            })

    all_memberships = [*memberships.values(), *universe_memberships]
    taxonomy_version = batch_version(
        [m.taxonomy_version for m in all_memberships], prefix="taxonomy",
    )
    compatibility_key = batch_version(
        [m.compatibility_key for m in all_memberships], prefix="compatibility",
    )
    membership_version = batch_version(
        [m.membership_version for m in all_memberships], prefix="membership",
    )
    expected_count = len(boards) + len(universe_definitions)
    run_stmt = select(BoardAnalysisRun).where(
        BoardAnalysisRun.trade_date == trade_date,
        BoardAnalysisRun.source_core_run_id == source_core_run_id,
        BoardAnalysisRun.taxonomy_version == taxonomy_version,
        BoardAnalysisRun.taxonomy_compatibility_key == compatibility_key,
        BoardAnalysisRun.algorithm_version == algorithm_version,
        BoardAnalysisRun.membership_version == membership_version,
    )
    batch_run = (await session.execute(run_stmt)).scalar_one_or_none()
    if batch_run is None:
        batch_run = BoardAnalysisRun(
            trade_date=trade_date,
            source_core_run_id=source_core_run_id,
            taxonomy_version=taxonomy_version,
            taxonomy_compatibility_key=compatibility_key,
            membership_version=membership_version,
            algorithm_version=algorithm_version,
            expected_count=expected_count,
            succeeded_count=0,
            failed_count=0,
            coverage_ratio=0.0,
            status="running",
            blockers=blockers,
        )
        session.add(batch_run)
        await session.flush()
    elif batch_run.published_at is not None:
        return {
            "board_analysis_run_id": str(batch_run.id),
            "trade_date": trade_date.isoformat(),
            "board_type_filter": board_type,
            "status": batch_run.status,
            "succeeded": batch_run.succeeded_count,
            "failed": batch_run.failed_count,
            "published": batch_run.succeeded_count,
            "coverage_below_threshold": 0,
            "details": [],
            "errors": list(batch_run.blockers or []),
            "idempotent_reuse": True,
        }
    else:
        batch_run.status = "running"
        batch_run.expected_count = expected_count
        batch_run.blockers = blockers

    population_blockers = [
        item for item in blockers
        if item.get("code") == "blocked_external_population"
    ]
    succeeded_boards = 0
    failed = len(population_blockers)
    published = 0
    coverage_below = 0
    details: list[dict[str, Any]] = list(universe_details)
    errors: list[dict[str, Any]] = list(blockers)
    for board in boards:
        board_membership = memberships.get(board.id)
        if board_membership is None:
            continue
        try:
            snapshot = await compute_board_analysis(
                session, board.id, trade_date,
                source_core_run_id=source_core_run_id,
                board_analysis_run_id=batch_run.id,
                pit_membership=board_membership,
                algorithm_version=algorithm_version,
            )
            if snapshot.status == "succeeded":
                succeeded_boards += 1
            else:
                failed += 1
                coverage_below += 1
            details.append({
                "board_id": str(board.id),
                "board_name": board.name,
                "board_type": board.type,
                "status": snapshot.status,
                "coverage": snapshot.coverage_ratio,
                "published": False,
                "snapshot": snapshot,
            })
        except Exception as exc:
            failed += 1
            errors.append({
                "code": "board_compute_failed",
                "board_id": str(board.id),
                "board_name": board.name,
                "error": str(exc),
            })
            logger.exception("[BoardAnalysis] 计算失败: board=%s/%s", board.type, board.name)

    succeeded = succeeded_boards + len(universe_memberships)
    batch_run.succeeded_count = succeeded
    batch_run.failed_count = failed
    batch_run.coverage_ratio = succeeded / expected_count if expected_count else 0.0
    if population_blockers:
        batch_run.status = "blocked_external_population"
    elif not formal_batch:
        batch_run.status = "partial"
    elif expected_count == 0:
        batch_run.status = "blocked_external_population"
        empty_blocker = {
            "code": "blocked_external_population",
            "scope_type": "board_catalog",
            "scope_key": "all",
            "reason": "no configured Board or universe definitions",
        }
        batch_run.blockers = [*blockers, empty_blocker]
        errors.append(empty_blocker)
        batch_run.failed_count = 1
    elif failed:
        batch_run.status = "partial" if succeeded else "failed"
    else:
        batch_run.status = "succeeded"

    if publish and batch_run.status == "succeeded":
        for detail in details:
            snapshot = detail.pop("snapshot", None)
            if snapshot is None:
                continue
            if await publish_board_analysis(session, snapshot) is not None:
                published += 1
                detail["published"] = True
        await publish_market_aggregation(
            session,
            trade_date=trade_date,
            source_core_run_id=source_core_run_id,
            aggregation_run_id=batch_run.id,
            algorithm_version=algorithm_version,
            metadata={
                "board_analysis_run_id": str(batch_run.id),
                "taxonomy_version": taxonomy_version,
                "taxonomy_compatibility_key": compatibility_key,
                "membership_version": membership_version,
            },
        )
        batch_run.published_at = datetime.now(UTC)
    else:
        for detail in details:
            detail.pop("snapshot", None)

    await session.flush()
    return {
        "board_analysis_run_id": str(batch_run.id),
        "trade_date": trade_date.isoformat(),
        "board_type_filter": board_type,
        "status": batch_run.status,
        "succeeded": succeeded,
        "failed": batch_run.failed_count,
        "published": published,
        "coverage_below_threshold": coverage_below,
        "details": details,
        "errors": errors,
    }


# =============================================================================
# 查询入口
# =============================================================================


async def list_board_analyses(
    session: AsyncSession,
    *,
    board_type: str | None = None,
    trade_date: date | None = None,
    sort: str = "coverage_desc",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """查询板块分析列表（分页）。

    Args:
        session: 异步 DB 会话
        board_type: 类型过滤（industry | concept）
        trade_date: 日期过滤（None 时取最新）
        sort: 排序字段（coverage_desc | coverage_asc | name_asc | ready_desc）
        page: 页码（1-based）
        page_size: 每页大小

    Returns:
        {items: list[BoardAnalysisSnapshot], total: int, page, page_size, has_more}
    """
    # 构建查询
    stmt = select(BoardAnalysisSnapshot)
    if board_type in ("industry", "concept"):
        stmt = stmt.where(BoardAnalysisSnapshot.board_type == board_type)
    if trade_date is not None:
        stmt = stmt.where(BoardAnalysisSnapshot.trade_date == trade_date)

    # 排序
    if sort == "coverage_asc":
        stmt = stmt.order_by(
            BoardAnalysisSnapshot.coverage_ratio.asc().nullslast(),
            BoardAnalysisSnapshot.board_name.asc(),
        )
    elif sort == "name_asc":
        stmt = stmt.order_by(BoardAnalysisSnapshot.board_name.asc())
    elif sort == "ready_desc":
        stmt = stmt.order_by(
            BoardAnalysisSnapshot.ready_count.desc().nullslast(),
            BoardAnalysisSnapshot.board_name.asc(),
        )
    else:  # coverage_desc 默认
        stmt = stmt.order_by(
            BoardAnalysisSnapshot.coverage_ratio.desc().nullslast(),
            BoardAnalysisSnapshot.board_name.asc(),
        )

    # count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # 分页
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)
    result = await session.execute(stmt)
    items = result.scalars().all()

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": (offset + len(items)) < total,
    }


async def get_board_analysis_detail(
    session: AsyncSession,
    board_id: uuid.UUID,
    trade_date: date | None = None,
) -> BoardAnalysisSnapshot | None:
    """查询板块分析详情。trade_date 为 None 时取最新。"""
    stmt = select(BoardAnalysisSnapshot).where(
        BoardAnalysisSnapshot.board_id == board_id,
    )
    if trade_date is not None:
        stmt = stmt.where(BoardAnalysisSnapshot.trade_date == trade_date)
    else:
        # 取最新日期
        stmt = stmt.order_by(BoardAnalysisSnapshot.trade_date.desc())
    stmt = stmt.limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def compute_is_stale(
    session: AsyncSession,
    snapshot_trade_date: date,
) -> bool:
    """判断快照是否过期（snapshot.trade_date < MAX(bars_daily.trade_date)）。"""
    from app.models.bar import BarDaily

    max_date = await session.scalar(select(func.max(BarDaily.trade_date)))
    if max_date is None:
        return False
    return snapshot_trade_date < max_date


async def check_is_published(
    session: AsyncSession,
    board_id: uuid.UUID,
    trade_date: date,
) -> bool:
    """检查板块是否已发布（存在 publication pointer）。"""
    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_BOARD,
        scope_key=str(board_id),
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
    )
    return pub is not None


if __name__ == "__main__":
    # 模块自测：纯函数计算
    test_flats = [
        {
            "fp_trend_direction": "up",
            "fp_trend_strength": 0.8,
            "fp_dsa_vwap_dev_pct": 1.2,
            "fp_swing_direction": "up",
            "fp_structure_alignment": "aligned",
            "fp_active_ob_count": 2,
            "fp_latest_bos_direction": "up",
            "fp_latest_choch_direction": None,
            "fp_latest_ob_direction": "down",
            "fp_latest_eqh_freshness": 5,
            "fp_latest_eql_freshness": None,
            "fp_momentum_direction": "up",
            "fp_squeeze_state": "released",
            "fp_momentum_change": "enhancing",
            "fp_sqzmom_value": 0.5,
            "fp_volume_badge": "放量",
            "fp_volume_ratio20": 1.5,
            "fp_volume_ratio200": 1.2,
            "fp_volume_percentile20": 85.0,
            "fp_volume_percentile200": 70.0,
        },
        {
            "fp_trend_direction": "down",
            "fp_trend_strength": 0.4,
            "fp_dsa_vwap_dev_pct": -0.8,
            "fp_swing_direction": "down",
            "fp_structure_alignment": "misaligned",
            "fp_active_ob_count": 0,
            "fp_latest_bos_direction": "down",
            "fp_latest_choch_direction": "down",
            "fp_latest_ob_direction": None,
            "fp_latest_eqh_freshness": None,
            "fp_latest_eql_freshness": None,
            "fp_momentum_direction": "down",
            "fp_squeeze_state": "squeeze",
            "fp_momentum_change": "fading",
            "fp_sqzmom_value": -0.3,
            "fp_volume_badge": "缩量",
            "fp_volume_ratio20": 0.6,
            "fp_volume_ratio200": 0.8,
            "fp_volume_percentile20": 30.0,
            "fp_volume_percentile200": 25.0,
        },
    ]
    payload = compute_board_payload(test_flats)
    assert payload["trend_dist"] == {"up": 1, "down": 1, "neutral": 0}
    assert payload["structure_events"]["bos_up"] == 1
    assert payload["structure_events"]["bos_down"] == 1
    assert payload["structure_events"]["ob_down"] == 1
    assert payload["structure_events"]["eqh_present"] == 1
    assert payload["momentum"]["enhancing"] == 1
    assert payload["momentum"]["fading"] == 1
    assert payload["volume"]["high"] == 1
    assert payload["volume"]["low"] == 1
    assert payload["ready_members"] == 2
    assert payload["missing_members"] == 0

    # 空输入测试
    empty_payload = compute_board_payload([])
    assert empty_payload["trend_dist"] == {"up": 0, "down": 0, "neutral": 0}
    assert empty_payload["ready_members"] == 0

    # 测试 missing 计入
    payload_with_missing = compute_board_payload([
        {"fp_trend_direction": "up"},
        {},  # empty flat -> missing
        {"fp_trend_direction": None},  # missing
    ])
    assert payload_with_missing["ready_members"] == 1
    assert payload_with_missing["missing_members"] == 2

    print(f"OK: BOARD_ANALYSIS_ALGORITHM_VERSION={BOARD_ANALYSIS_ALGORITHM_VERSION}")
    print(f"OK: BOARD_ANALYSIS_MIN_COVERAGE={BOARD_ANALYSIS_MIN_COVERAGE}")
    print(f"OK: parameter_hash={_compute_parameter_hash()}")
    print("OK: payload computed for 2 stocks, trend_up=1, trend_down=1")
