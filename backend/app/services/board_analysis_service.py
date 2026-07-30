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
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board_analysis_snapshot import BoardAnalysisSnapshot
from app.models.factor_publication import FactorPublication
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.services.factor_publication_service import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    get_publication,
    get_published_snapshot_run_id,
)

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
        if not flat.get("fp_trend_direction"):
            missing += 1
            continue
        ready += 1

        # === 趋势 ===
        td = flat.get("fp_trend_direction")
        if td == "up":
            trend_up += 1
        elif td == "down":
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
        sd = flat.get("fp_swing_direction")
        if sd == "up":
            swing_up += 1
        elif sd == "down":
            swing_down += 1
        else:
            swing_neutral += 1

        sa = flat.get("fp_structure_alignment")
        if sa == "aligned":
            alignment_aligned += 1
        elif sa == "misaligned":
            alignment_misaligned += 1
        else:
            alignment_neutral += 1

        obc = _safe_int(flat.get("fp_active_ob_count"))
        if obc is not None:
            active_ob_counts.append(obc)

        # === 结构事件 ===
        # BOS 方向（最新一次 BOS）
        bos_dir = flat.get("fp_latest_bos_direction")
        if bos_dir == "up":
            bos_up += 1
        elif bos_dir == "down":
            bos_down += 1
        choch_dir = flat.get("fp_latest_choch_direction")
        if choch_dir == "up":
            choch_up += 1
        elif choch_dir == "down":
            choch_down += 1
        ob_dir = flat.get("fp_latest_ob_direction")
        if ob_dir == "up":
            ob_up += 1
        elif ob_dir == "down":
            ob_down += 1

        # EQH/EQL presence（freshness != null 表示存在）
        if flat.get("fp_latest_eqh_freshness") is not None:
            eqh_present += 1
        if flat.get("fp_latest_eql_freshness") is not None:
            eql_present += 1

        # === 动量 ===
        md = flat.get("fp_momentum_direction")
        if md == "up":
            mom_pos += 1
        elif md == "down":
            mom_neg += 1
        else:
            mom_neu += 1
        sqz_state = flat.get("fp_squeeze_state")
        if sqz_state == "squeeze":
            squz += 1
        elif sqz_state == "released":
            released += 1
        elif sqz_state == "normal":
            normal += 1

        mc = flat.get("fp_momentum_change")
        if mc == "enhancing":
            enhancing += 1
        elif mc == "fading":
            fading += 1
        else:
            mom_flat += 1

        sqz_val = _safe_float(flat.get("fp_sqzmom_value"))
        if sqz_val is not None:
            sqzmom_values.append(sqz_val)

        # === 量能 ===
        vb = flat.get("fp_volume_badge")
        if vb == "放量":
            vol_high += 1
        elif vb == "缩量":
            vol_low += 1
        elif vb == "正常":
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
# 数据库查询
# =============================================================================


async def _get_board_members(
    session: AsyncSession,
    board_id: uuid.UUID,
) -> list[uuid.UUID]:
    """获取板块全部成员 instrument_id 列表。"""
    stmt = select(MarketBoardMembership.instrumentId).where(
        MarketBoardMembership.boardId == board_id,
    )
    result = await session.execute(stmt)
    return [row[0] for row in result]


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

    # 3. 获取板块成员
    member_ids = await _get_board_members(session, board_id)
    eligible_count = len(member_ids)
    if eligible_count == 0:
        # 空板块：直接写入空快照（避免后续 None 除零）
        payload = compute_board_payload([])
        snapshot = await _upsert_snapshot(
            session,
            board=board,
            trade_date=trade_date,
            source_core_run_id=source_core_run_id,
            algorithm_version=algorithm_version,
            parameter_hash=parameter_hash or _compute_parameter_hash(),
            eligible_count=0,
            ready_count=0,
            coverage_ratio=0.0,
            missing_count=0,
            missing_reasons={},
            payload=payload,
            status="succeeded",
            error_message=None,
        )
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
        elif not flat.get("fp_trend_direction"):
            missing_count += 1
            missing_reasons["FP_TREND_MISSING"] = (
                missing_reasons.get("FP_TREND_MISSING", 0) + 1
            )
        else:
            flat_list.append(flat)

    # 7. 计算指标 payload
    payload = compute_board_payload(flat_list)

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
        algorithm_version=algorithm_version,
        parameter_hash=parameter_hash or _compute_parameter_hash(),
        eligible_count=eligible_count,
        ready_count=ready_count,
        coverage_ratio=coverage_ratio,
        missing_count=missing_count,
        missing_reasons=missing_reasons,
        payload=payload,
        status="succeeded",
        error_message=None,
    )

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

    唯一键 (trade_date, board_id, algorithm_version) 保证幂等。
    """
    now = datetime.now(UTC)

    # 先查现有记录（upsert 需要保留 started_at/created_at）
    existing_stmt = (
        select(BoardAnalysisSnapshot)
        .where(
            BoardAnalysisSnapshot.trade_date == trade_date,
            BoardAnalysisSnapshot.board_id == board.id,
            BoardAnalysisSnapshot.algorithm_version == algorithm_version,
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
    }

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_BOARD,
        scope_key=str(snapshot.board_id),
        trade_date=snapshot.trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
        algorithm_version=snapshot.algorithm_version,
        data_run_id=snapshot.id,
        coverage_ratio=snapshot.coverage_ratio,
        published_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_factor_publications_scope_date_kind",
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
    """读取已发布的 board_analysis snapshot_id（无 pointer 返回 None）。"""
    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_BOARD,
        scope_key=str(board_id),
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
    )
    return pub.data_run_id if pub else None


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
    """批量计算所有板块分析（行业+概念）。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        board_type: 限定类型（industry | concept | None=both）
        limit: 限制每个类型的板块数（用于 canary）
        publish: 是否发布 coverage >= 0.95 的结果
        algorithm_version: 算法版本

    Returns:
        {
            "trade_date": str,
            "succeeded": int,
            "failed": int,
            "published": int,
            "coverage_below_threshold": int,
            "details": [{"board_id", "board_name", "status", "coverage", "published"}],
            "errors": [{"board_id", "board_name", "error"}],
        }
    """
    stmt = select(MarketBoard).order_by(MarketBoard.name.asc())
    if board_type in ("industry", "concept"):
        stmt = stmt.where(MarketBoard.type == board_type)
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    boards = result.scalars().all()

    succeeded = 0
    failed = 0
    published = 0
    coverage_below = 0
    details: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for board in boards:
        try:
            snapshot = await compute_board_analysis(
                session,
                board.id,
                trade_date,
                algorithm_version=algorithm_version,
            )
            succeeded += 1
            detail: dict[str, Any] = {
                "board_id": str(board.id),
                "board_name": board.name,
                "board_type": board.type,
                "status": snapshot.status,
                "coverage": snapshot.coverage_ratio,
                "published": False,
            }
            if publish:
                if snapshot.coverage_ratio >= BOARD_ANALYSIS_MIN_COVERAGE:
                    pub = await publish_board_analysis(session, snapshot)
                    if pub is not None:
                        published += 1
                        detail["published"] = True
                else:
                    coverage_below += 1
            details.append(detail)
        except Exception as exc:
            failed += 1
            errors.append({
                "board_id": str(board.id),
                "board_name": board.name,
                "error": str(exc),
            })
            logger.exception(
                "[BoardAnalysis] 计算失败: board=%s/%s", board.type, board.name,
            )

    return {
        "trade_date": trade_date.isoformat(),
        "board_type_filter": board_type,
        "succeeded": succeeded,
        "failed": failed,
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
