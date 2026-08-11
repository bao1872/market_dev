"""复盘信号生成与生命周期服务（PRD §8、§10.1）。

职责：
- 对每个 scope snapshot 运行三类筛选器（A/B/C）
- 命中后生成 MarketReviewSignal 记录（幂等：唯一键 review_run_id +
  filter_family + signal_type + scope_type + scope_key）
- 根据前一交易日同 scope 同 signal_type 的信号决定生命周期状态
  （new/continuing/confirmed/weakened/invalidated/transformed）
- 计算 rank_key 并排序信号

输入：
- MarketReviewScopeSnapshot ORM 对象（含 P/Q/U/C/V payload）
- 历史基线（由 service 层从历史 scope snapshot 读取并构造 context）
- 前一交易日信号列表（用于状态机判定）

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_signal_service
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# 必须 import filter_engine 以触发 evaluator 注册
from app.domain.review import filter_engine  # noqa: F401
from app.domain.review.filter_definitions import (
    DEFAULT_FILTERS,
    REVIEW_FILTER_VERSION,
    FilterDefinition,
)
from app.domain.review.filter_engine import (
    build_signal_payloads,
    evaluate_filters,
    sort_signals_by_rank,
)
from app.domain.review.tracking_state_machine import (
    determine_signal_status,
    evaluate_confirmation,
    evaluate_invalidation,
)
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
    MarketReviewSignal,
    MarketReviewTracking,
)

logger = logging.getLogger("review_signal_service")


class SignalGenerationError(Exception):
    """信号生成失败。"""

    pass


# =============================================================================
# 构建 filter context
# =============================================================================


def build_filter_context(
    snapshot: MarketReviewScopeSnapshot,
    *,
    history_extras: dict[str, float | int] | None = None,
) -> dict[str, Any]:
    """从 scope snapshot 构建 filter_engine 评估上下文。

    Args:
        snapshot: MarketReviewScopeSnapshot ORM 对象
        history_extras: service 层预计算的历史分位注入字段（PRD §8.1/8.2/8.3）
            可包含：_pq_diff_history_pct / _q_delta1d_history_pct /
            _u_delta1d_history_pct / _v_delta1d_history_pct /
            _structure_breakdown_not_rising / _c_rising / _c_high_anomaly

    Returns:
        context dict（含 P/Q/U/C/V payload + coverage + 历史分位注入字段 +
            pyramid_v2 维度数据，供 D 族筛选器评估）
    """
    context: dict[str, Any] = {
        "P": snapshot.p_payload or {},
        "Q": snapshot.q_payload or {},
        "U": snapshot.u_payload or {},
        "C": snapshot.c_payload or {},
        "V": snapshot.v_payload or {},
        "coverage": float(snapshot.coverage_ratio),
        "ready_count": snapshot.ready_count,
    }
    # [P0-7 2026-07-30] 注入 pyramid_v2 维度数据（PRD §24 D 族筛选器）
    # pyramid_v2 存储在 scope snapshot 的 data_quality_json["pyramid_v2"] 中
    dq = snapshot.data_quality_json or {}
    pv2 = dq.get("pyramid_v2") if isinstance(dq, dict) else None
    if isinstance(pv2, dict):
        context["pyramid_v2"] = pv2
    if history_extras:
        for k, v in history_extras.items():
            context[k] = v
    return context


# =============================================================================
# 前序信号查询
# =============================================================================


async def find_previous_signals(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_key: str,
    before_trade_date: date,
    algorithm_version: str,
    limit_per_type: int = 10,
) -> list[MarketReviewSignal]:
    """查询前一交易日的同 scope 信号（用于状态机判定）。

    Args:
        session: 异步 DB 会话
        scope_type: 范围类型
        scope_key: 范围标识
        before_trade_date: 截止交易日（不含）
        algorithm_version: 算法版本（用于过滤同版本信号）
        limit_per_type: 每个 signal_type 最多取多少条

    Returns:
        前一交易日同 scope 同 algorithm_version 的信号列表
    """
    stmt = (
        select(MarketReviewSignal)
        .join(MarketReviewRun, MarketReviewSignal.review_run_id == MarketReviewRun.id)
        .where(
            MarketReviewSignal.scope_type == scope_type,
            MarketReviewSignal.scope_key == scope_key,
            MarketReviewSignal.trade_date < before_trade_date,
            MarketReviewRun.algorithm_version == algorithm_version,
        )
        .order_by(MarketReviewSignal.trade_date.desc())
        .limit(limit_per_type * 7)  # 7 个 signal_type
    )
    result = await session.execute(stmt)
    return list(result.scalars())


def find_previous_same_signal_type(
    previous_signals: list[MarketReviewSignal],
    signal_type: str,
    filter_family: str,
) -> MarketReviewSignal | None:
    """从前序信号中找到同 signal_type 同 filter_family 的最近一条。"""
    for sig in previous_signals:
        if (
            sig.signal_type == signal_type
            and sig.filter_family == filter_family
        ):
            return sig
    return None


# =============================================================================
# 信号生成（幂等 upsert）
# =============================================================================


async def generate_signals_for_scope(
    session: AsyncSession,
    run: MarketReviewRun,
    snapshot: MarketReviewScopeSnapshot,
    *,
    previous_signals: list[MarketReviewSignal] | None = None,
    history_extras: dict[str, float | int] | None = None,
    filters: list[FilterDefinition] | None = None,
) -> list[MarketReviewSignal]:
    """为单个 scope 运行筛选器并生成信号记录（replace-set 语义）。

    幂等：相同 (review_run_id, filter_family, signal_type, scope_type, scope_key)
    的信号不重复创建（on_conflict_do_update），保留稳定 ID。

    关键不变量（replace-set）：本函数完成后，该 (review_run_id, scope_type,
    scope_key) 下持久化的信号集合 **必须恰好等于** 当前筛选命中集合。
    即：先 upsert 当前全部命中（保留存活 ID），再删除「此前存在但当前不再命中」
    的 stale 信号。stale 信号若被 MarketReviewTracking 引用则 fail-closed 中止
    删除（绝不静默 SET NULL 引用）。

    事务顺序：evaluate → upsert 全部当前命中 → 计算 stale 集 → tracking 守卫
    → 删除 stale → 返回当前信号。evaluate/upsert 失败则 stale 对账不执行
    （caller 拥有事务所有权）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        run: MarketReviewRun ORM 对象
        snapshot: MarketReviewScopeSnapshot ORM 对象
        previous_signals: 前一交易日同 scope 的信号列表（None=自动查询）
        history_extras: 历史分位注入字段
        filters: 筛选器列表（None=DEFAULT_FILTERS）

    Returns:
        命中并 upsert 的 MarketReviewSignal 列表（= 当前命中集）

    Raises:
        SignalGenerationError: stale 信号被 MarketReviewTracking 引用时
            （machine-readable 前缀 STALE_SIGNAL_REFERENCED_BY_TRACKING）
    """
    if filters is None:
        filters = DEFAULT_FILTERS

    context = build_filter_context(snapshot, history_extras=history_extras)
    hits = evaluate_filters(context, filters=filters)

    # 查询前序信号（如未传入）
    if previous_signals is None:
        previous_signals = await find_previous_signals(
            session,
            scope_type=snapshot.scope_type,
            scope_key=snapshot.scope_key,
            before_trade_date=run.trade_date,
            algorithm_version=run.algorithm_version,
        )

    created: list[MarketReviewSignal] = []
    # [FIX] 当前命中身份键集合（persisted identity = filter_family + signal_type）。
    # 不发明新的信号身份维度；scope_type/scope_key/review_run_id 已由调用上下文固定。
    current_hit_keys: set[tuple[str, str]] = set()
    for filt in hits:
        # 找同 signal_type 的前序
        prev = find_previous_same_signal_type(
            previous_signals, filt.signal_type, filt.family.value,
        )
        prev_status = prev.status if prev else None
        prev_id = prev.id if prev else None
        first_seen_date = (
            prev.first_seen_date if prev else run.trade_date
        )
        consecutive_days = (
            (run.trade_date - first_seen_date).days + 1
            if prev else 1
        )

        # 评估确认/失效条件
        confirmation_met = evaluate_confirmation(
            filt.confirmation_rule,
            consecutive_days=consecutive_days,
            extra_conditions_met={},  # service 层可扩展
        )
        invalidation_met = evaluate_invalidation(
            filt.invalidation_rule, context=context,
        )

        # 决定状态
        status = determine_signal_status(
            is_hit=True,
            previous_status=prev_status,
            previous_signal_id=str(prev_id) if prev_id else None,
            consecutive_days=consecutive_days,
            confirmation_rule=filt.confirmation_rule,
            invalidation_rule=filt.invalidation_rule,
            confirmation_conditions_met=confirmation_met,
            invalidation_conditions_met=invalidation_met,
        )

        # 构建 payload
        payloads = build_signal_payloads(
            filt, context,
            duration_days=consecutive_days - 1,
            scope_type=snapshot.scope_type,
            scope_name=snapshot.scope_name,
        )

        signal = await _upsert_signal(
            session,
            run=run,
            snapshot=snapshot,
            filt=filt,
            status=status,
            first_seen_date=first_seen_date,
            previous_signal_id=prev_id,
            payloads=payloads,
        )
        created.append(signal)
        current_hit_keys.add((filt.family.value, filt.signal_type))

        logger.info(
            "[ReviewSignal] %s/%s %s status=%s consecutive=%d",
            snapshot.scope_type, snapshot.scope_name,
            filt.signal_type, status, consecutive_days,
        )

    # [FIX] replace-set 对账：删除此前存在但当前不再命中的 stale 信号。
    # 仅在全部当前命中 upsert 成功后执行。
    await _reconcile_stale_signals(
        session,
        run=run,
        snapshot=snapshot,
        current_hit_keys=current_hit_keys,
    )

    return created


async def _reconcile_stale_signals(
    session: AsyncSession,
    *,
    run: MarketReviewRun,
    snapshot: MarketReviewScopeSnapshot,
    current_hit_keys: set[tuple[str, str]],
) -> None:
    """replace-set 对账：删除本 (run, scope) 下非当前命中的 stale 信号。

    - 先查询本 (review_run_id, scope_type, scope_key) 现有全部信号。
    - stale = 现有身份键 − 当前命中身份键（当前命中为空 → 全部 stale）。
    - 若任一 stale 信号被 MarketReviewTracking.source_signal_id 引用 →
      fail-closed 抛 SignalGenerationError（前缀 STALE_SIGNAL_REFERENCED_BY_TRACKING），
      不删除、不改 tracking。
    - 否则仅删除 stale 信号；其 attribution / instrument 子表由 DB FK 级联删除。
    """
    existing_stmt = (
        select(MarketReviewSignal)
        .where(
            MarketReviewSignal.review_run_id == run.id,
            MarketReviewSignal.scope_type == snapshot.scope_type,
            MarketReviewSignal.scope_key == snapshot.scope_key,
        )
    )
    existing = list((await session.execute(existing_stmt)).scalars())

    if not existing:
        return

    stale = [
        sig for sig in existing
        if (sig.filter_family, sig.signal_type) not in current_hit_keys
    ]
    if not stale:
        return

    stale_ids = [sig.id for sig in stale]

    # FIX 4 — tracking 守卫（fail-closed）
    track_stmt = (
        select(MarketReviewTracking.id)
        .where(MarketReviewTracking.source_signal_id.in_(stale_ids))
        .limit(1)
    )
    referencing = (await session.execute(track_stmt)).scalar_one_or_none()
    if referencing is not None:
        raise SignalGenerationError(
            "STALE_SIGNAL_REFERENCED_BY_TRACKING:"
            f" review_run_id={run.id}"
            f" scope_type={snapshot.scope_type}"
            f" scope_key={snapshot.scope_key}"
            f" stale_signal_count={len(stale_ids)}"
            f" tracking_count>=1"
        )

    # FIX 5 — 仅删除 stale 信号（FK 级联删除 attribution / instrument）
    for sig in stale:
        await session.delete(sig)
    await session.flush()


async def _upsert_signal(
    session: AsyncSession,
    *,
    run: MarketReviewRun,
    snapshot: MarketReviewScopeSnapshot,
    filt: FilterDefinition,
    status: str,
    first_seen_date: date,
    previous_signal_id: uuid.UUID | None,
    payloads: dict[str, Any],
) -> MarketReviewSignal:
    """upsert signal 记录（幂等）。"""
    values = {
        "review_run_id": run.id,
        "trade_date": run.trade_date,
        "filter_family": filt.family.value,
        "signal_type": filt.signal_type,
        "scope_type": snapshot.scope_type,
        "scope_key": snapshot.scope_key,
        "scope_name": snapshot.scope_name,
        "status": status,
        "first_seen_date": first_seen_date,
        "previous_signal_id": previous_signal_id,
        "transformed_to_signal_id": None,
        "trigger_payload": payloads["trigger_payload"],
        "baseline_payload": payloads["baseline_payload"],
        "evidence_payload": payloads["evidence_payload"],
        "confirmation_rule": payloads["confirmation_rule"],
        "invalidation_rule": payloads["invalidation_rule"],
        "coverage_ratio": Decimal(str(snapshot.coverage_ratio)),
        "rank_key": payloads["rank_key"],
    }

    stmt = pg_insert(MarketReviewSignal).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_signals_run_family_type_scope",
        set_={
            "status": stmt.excluded.status,
            "first_seen_date": stmt.excluded.first_seen_date,
            "previous_signal_id": stmt.excluded.previous_signal_id,
            "trigger_payload": stmt.excluded.trigger_payload,
            "baseline_payload": stmt.excluded.baseline_payload,
            "evidence_payload": stmt.excluded.evidence_payload,
            "confirmation_rule": stmt.excluded.confirmation_rule,
            "invalidation_rule": stmt.excluded.invalidation_rule,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "rank_key": stmt.excluded.rank_key,
        },
    )
    await session.execute(stmt)
    await session.flush()

    # 读取 upsert 后的记录
    stmt_read = (
        select(MarketReviewSignal)
        .where(
            MarketReviewSignal.review_run_id == run.id,
            MarketReviewSignal.filter_family == filt.family.value,
            MarketReviewSignal.signal_type == filt.signal_type,
            MarketReviewSignal.scope_type == snapshot.scope_type,
            MarketReviewSignal.scope_key == snapshot.scope_key,
        )
        .limit(1)
    )
    result = await session.execute(stmt_read)
    return result.scalar_one()


# =============================================================================
# 信号查询（用于 API）
# =============================================================================


async def list_signals(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    *,
    filter_family: str | None = None,
    signal_type: str | None = None,
    status: str | None = None,
    scope_type: str | None = None,
    scope_key: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[MarketReviewSignal], int]:
    """分页查询 review run 的信号（按 rank_key 排序）。"""
    stmt = select(MarketReviewSignal).where(
        MarketReviewSignal.review_run_id == review_run_id,
    )
    if filter_family is not None:
        stmt = stmt.where(MarketReviewSignal.filter_family == filter_family)
    if signal_type is not None:
        stmt = stmt.where(MarketReviewSignal.signal_type == signal_type)
    if status is not None:
        stmt = stmt.where(MarketReviewSignal.status == status)
    if scope_type is not None:
        stmt = stmt.where(MarketReviewSignal.scope_type == scope_type)
    if scope_key is not None:
        stmt = stmt.where(MarketReviewSignal.scope_key == scope_key)

    # 总数
    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    # 分页
    offset = (page - 1) * page_size
    stmt = stmt.order_by(MarketReviewSignal.created_at.desc()).offset(offset).limit(page_size)
    result = await session.execute(stmt)
    signals = list(result.scalars())

    # 按 rank_key 排序（PRD §8.4）
    sig_dicts = [
        {"signal": s, "rank_key": s.rank_key or {}} for s in signals
    ]
    sorted_dicts = sort_signals_by_rank(sig_dicts)
    return [d["signal"] for d in sorted_dicts], total


async def get_signal(
    session: AsyncSession,
    signal_id: uuid.UUID,
) -> MarketReviewSignal | None:
    """读取单个信号详情。"""
    return await session.get(MarketReviewSignal, signal_id)


async def count_signals_by_status(
    session: AsyncSession,
    review_run_id: uuid.UUID,
) -> dict[str, int]:
    """统计 review run 各状态信号数（用于 overview.signalSummary）。"""
    from sqlalchemy import func
    stmt = (
        select(
            MarketReviewSignal.status,
            func.count(MarketReviewSignal.id).label("cnt"),
        )
        .where(MarketReviewSignal.review_run_id == review_run_id)
        .group_by(MarketReviewSignal.status)
    )
    result = await session.execute(stmt)
    out: dict[str, int] = {}
    for row in result:
        out[row.status] = row.cnt
    return out


async def update_run_signal_count(
    session: AsyncSession,
    run: MarketReviewRun,
) -> int:
    """更新 run.signal_count（统计当前 run 的信号总数）。"""
    from sqlalchemy import func
    stmt = (
        select(func.count(MarketReviewSignal.id))
        .where(MarketReviewSignal.review_run_id == run.id)
    )
    count = (await session.execute(stmt)).scalar_one()
    run.signal_count = count
    return count


if __name__ == "__main__":
    print(f"REVIEW_FILTER_VERSION = {REVIEW_FILTER_VERSION}")
    print(f"DEFAULT_FILTERS count = {len(DEFAULT_FILTERS)}")
    print("OK: review_signal_service imports verified")
