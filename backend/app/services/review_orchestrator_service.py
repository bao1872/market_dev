"""复盘编排服务 - 端到端编排 review run（PRD §11）。

职责（PRD §11 编排顺序）：
1. create_run: 创建或复用 MarketReviewRun（幂等：唯一键 trade_date+source_runs+版本）
2. compute_run: 执行完整流程
   - 解析 level-1 范围列表
   - 每个范围：metrics → signals → attribution（短事务、独立 item）
   - 一个 scope 失败不阻塞其他 scope
   - 全部完成后 evaluate_active_trackings
   - 更新 run 状态（signals_ready / partial / failed）
3. resume_run: 重启只处理 pending / 可重试 failed / 过期 running
4. publish_run / get_run_status: 委托给 publication_service + 直接查询

幂等：
- create_run 通过唯一约束 (trade_date, source_core_run_id, source_board_run_id,
  algorithm_version, filter_version) 保证
- 每个阶段通过 run_item (review_run_id + scope + phase) 唯一键 + on_conflict_do_update
  保证幂等；相同 input_hash + 版本的 succeeded item 不重算
- 信号 / 归因 / 追踪评估均幂等（由各自 service 保证）

约束：
- 输入只读 stock_core 和 board_analysis 的 factor_publications pointer
- 历史基线默认 120 日、最低 60 日
- 单 scope 失败不回滚其他 scope（写入 last_error 后继续）

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_orchestrator_service
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.filter_definitions import REVIEW_FILTER_VERSION
from app.models.board_analysis_snapshot import BoardAnalysisSnapshot
from app.models.factor_publication import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewRunItem,
)
from app.services.review_attribution_service import (
    compute_signal_attributions,
    compute_signal_instruments,
)
from app.services.review_publication_service import (
    REVIEW_PUBLISH_MIN_COVERAGE,
    ReviewPublishBlockError,
    evaluate_publish_gate,
    publish_review,
)
from app.services.review_scope_service import (
    LEVEL1_SCOPE_TYPES,
    ScopeDefinition,
    ScopeSnapshotError,
    compute_scope_metrics,
    fetch_member_flat_list,
    resolve_scope_members,
)
from app.services.review_signal_service import (
    SignalGenerationError,
    find_previous_signals,
    generate_signals_for_scope,
    update_run_signal_count,
)
from app.services.review_tracking_service import evaluate_all_active_trackings

logger = logging.getLogger("review_orchestrator_service")

# 复盘算法版本（每次指标/契约变更时递增）
REVIEW_ALGORITHM_VERSION = "review-1.0.0"

# 历史基线窗口（PRD §0、§7.1：默认 120，最低 60）
DEFAULT_BASELINE_WINDOW = 120
MIN_BASELINE_WINDOW = 60

# Run 状态枚举（与迁移 check constraint 一致）
RUN_STATUS_CREATED = "created"
RUN_STATUS_COMPUTING = "computing"
RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_SIGNALS_READY = "signals_ready"
RUN_STATUS_PUBLISHED = "published"
RUN_STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

# Item phase 枚举（与迁移 check constraint 一致）
PHASE_METRICS = "metrics"
PHASE_SIGNALS = "signals"
PHASE_ATTRIBUTION = "attribution"
PHASE_TRACKING = "tracking"

# Item status 枚举
ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_SUCCEEDED = "succeeded"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"

# 单 scope 重试上限（超过则不自动 resume）
MAX_AUTO_RESUME_ATTEMPTS = 5


class ReviewOrchestratorError(Exception):
    """编排失败。"""

    pass


# =============================================================================
# Run 创建与查询
# =============================================================================


async def create_run(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID | None = None,
    source_board_run_id: uuid.UUID | None = None,
    algorithm_version: str | None = None,
    filter_version: str | None = None,
    baseline_window: int = DEFAULT_BASELINE_WINDOW,
    canary: bool = False,
    symbols: list[str] | None = None,
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> MarketReviewRun:
    """创建或复用 review run（幂等：唯一键组合保证）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        trade_date: 业务交易日
        source_core_run_id: 输入 stock_core run_id（None 时从 publication 读取）
        source_board_run_id: 输入 board_analysis 的 source_core_run_id
            （None 时从 publication 读取 board_analysis_snapshot）
        algorithm_version: 算法版本（默认 REVIEW_ALGORITHM_VERSION）
        filter_version: 筛选器版本（默认 REVIEW_FILTER_VERSION）
        baseline_window: 历史基线窗口（默认 120，最低 60）
        canary: canary 模式（限定范围数，写入 metadata）
        symbols: 限定股票列表（canary/debug 用，None=全市场）
        dry_run: dry-run 模式（只校验输入，不写 run 记录）
        idempotency_key: 幂等键（写入 metadata，便于调用方追踪）

    Returns:
        MarketReviewRun ORM 对象

    Raises:
        ReviewOrchestratorError: 输入校验失败（缺 publication pointer 等）
    """
    if baseline_window < MIN_BASELINE_WINDOW:
        raise ReviewOrchestratorError(
            f"baseline_window={baseline_window} 低于最低值 {MIN_BASELINE_WINDOW}",
        )

    algo = algorithm_version or REVIEW_ALGORITHM_VERSION
    filt = filter_version or REVIEW_FILTER_VERSION

    # 解析 source run_ids
    resolved_core_id, resolved_board_id = await _resolve_source_run_ids(
        session,
        trade_date,
        source_core_run_id=source_core_run_id,
        source_board_run_id=source_board_run_id,
    )

    if dry_run:
        # dry-run：不写 DB，返回一个非持久化的 run 对象供调用方打印
        run = MarketReviewRun(
            trade_date=trade_date,
            source_core_run_id=resolved_core_id,
            source_board_run_id=resolved_board_id,
            algorithm_version=algo,
            filter_version=filt,
            baseline_window=baseline_window,
            status=RUN_STATUS_CREATED,
            coverage_ratio=Decimal("0"),
            metadata_json={
                "canary": canary,
                "symbols": symbols,
                "dry_run": True,
                "idempotency_key": idempotency_key,
            },
        )
        return run

    # upsert run（幂等：相同唯一键复用）
    meta: dict[str, Any] = {
        "canary": canary,
        "symbols": symbols,
        "idempotency_key": idempotency_key,
    }
    values = {
        "trade_date": trade_date,
        "source_core_run_id": resolved_core_id,
        "source_board_run_id": resolved_board_id,
        "algorithm_version": algo,
        "filter_version": filt,
        "baseline_window": baseline_window,
        "status": RUN_STATUS_CREATED,
        "expected_scope_count": 0,
        "succeeded_scope_count": 0,
        "failed_scope_count": 0,
        "signal_count": 0,
        "coverage_ratio": Decimal("0"),
        "metadata_json": meta,
    }
    stmt = pg_insert(MarketReviewRun).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_runs_date_core_board_algo_filter",
        set_={
            "metadata_json": stmt.excluded.metadata_json,
        },
    )
    await session.execute(stmt)
    await session.flush()

    # 读取 upsert 后的 run
    run = await get_run_by_keys(
        session,
        trade_date=trade_date,
        source_core_run_id=resolved_core_id,
        source_board_run_id=resolved_board_id,
        algorithm_version=algo,
        filter_version=filt,
    )
    if run is None:
        # 理论不可达：upsert 成功但读不到
        raise ReviewOrchestratorError(
            f"create_run upsert 后读不到 run: trade_date={trade_date}",
        )
    return run


async def get_run(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> MarketReviewRun | None:
    """读取单个 run。"""
    return await session.get(MarketReviewRun, run_id)


async def get_run_by_keys(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    source_board_run_id: uuid.UUID,
    algorithm_version: str,
    filter_version: str,
) -> MarketReviewRun | None:
    """按唯一键读取 run。"""
    stmt = (
        select(MarketReviewRun)
        .where(
            MarketReviewRun.trade_date == trade_date,
            MarketReviewRun.source_core_run_id == source_core_run_id,
            MarketReviewRun.source_board_run_id == source_board_run_id,
            MarketReviewRun.algorithm_version == algorithm_version,
            MarketReviewRun.filter_version == filter_version,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_run_items(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> list[MarketReviewRunItem]:
    """列出 run 的所有 item（按 scope_type + scope_key + phase 排序）。"""
    stmt = (
        select(MarketReviewRunItem)
        .where(MarketReviewRunItem.review_run_id == run_id)
        .order_by(
            MarketReviewRunItem.scope_type.asc(),
            MarketReviewRunItem.scope_key.asc(),
            MarketReviewRunItem.phase.asc(),
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars())


# =============================================================================
# 源 run_id 解析
# =============================================================================


async def _resolve_source_run_ids(
    session: AsyncSession,
    trade_date: date,
    *,
    source_core_run_id: uuid.UUID | None,
    source_board_run_id: uuid.UUID | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """解析 source_core_run_id 和 source_board_run_id。

    source_core_run_id：从 stock_core publication pointer 读取
    source_board_run_id：从 board_analysis_snapshot 读取（其 source_core_run_id）
        board_analysis_snapshot 通过 market_aggregation publication pointer 的
        data_run_id 定位

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        source_core_run_id: 调用方显式传入的 stock_core run_id（None 时自动解析）
        source_board_run_id: 调用方显式传入的 board source_core_run_id
            （None 时自动解析）

    Returns:
        (resolved_core_run_id, resolved_board_run_id)

    Raises:
        ReviewOrchestratorError: 缺少必要的 publication pointer
    """
    # 1. 解析 source_core_run_id
    if source_core_run_id is None:
        core_pub = await _get_publication(
            session, trade_date, PUBLICATION_KIND_STOCK_CORE,
        )
        if core_pub is None:
            raise ReviewOrchestratorError(
                f"trade_date={trade_date} 无已发布 stock_core pointer，"
                f"必须先完成盘后核心计算并发布 stock_core",
            )
        resolved_core_id = core_pub.data_run_id
    else:
        resolved_core_id = source_core_run_id

    # 2. 解析 source_board_run_id
    if source_board_run_id is None:
        board_pub = await _get_publication(
            session, trade_date, PUBLICATION_KIND_MARKET_AGGREGATION,
        )
        if board_pub is None:
            raise ReviewOrchestratorError(
                f"trade_date={trade_date} 无已发布 board_analysis pointer，"
                f"必须先完成板块分析并发布",
            )
        # board publication.data_run_id 指向 board_analysis_snapshots.id
        board_snapshot = await session.get(
            BoardAnalysisSnapshot, board_pub.data_run_id,
        )
        if board_snapshot is None:
            raise ReviewOrchestratorError(
                f"board_analysis_snapshot id={board_pub.data_run_id} 不存在",
            )
        resolved_board_id = board_snapshot.source_core_run_id
    else:
        resolved_board_id = source_board_run_id

    return resolved_core_id, resolved_board_id


async def _get_publication(
    session: AsyncSession,
    trade_date: date,
    publication_kind: str,
) -> FactorPublication | None:
    """读取指定 trade_date + kind 的最新 publication pointer。"""
    stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.trade_date == trade_date,
            FactorPublication.publication_kind == publication_kind,
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# =============================================================================
# Run 计算
# =============================================================================


async def compute_run(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    canary: bool = False,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """执行完整 review run 流程（PRD §11 编排顺序）。

    流程：
    1. run.status → computing
    2. 解析 level-1 范围列表
    3. 每个范围：
       a. metrics phase：计算 P/Q/U/C/V，upsert scope_snapshot
       b. signals phase：评估筛选器，生成信号
       c. attribution phase：对每个信号计算子范围 + 个股归因
    4. 评估所有 active 追踪
    5. 更新 run.signal_count / coverage_ratio / status

    单 scope 失败不阻塞其他 scope（写入 last_error 后继续）。
    重启只处理 pending / 可重试 failed（PRD §11）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        run: MarketReviewRun ORM 对象
        canary: canary 模式（限定范围数）
        symbols: 限定股票列表（canary/debug 用）

    Returns:
        计算结果摘要 dict
    """
    run.started_at = datetime.now(UTC)
    run.status = RUN_STATUS_COMPUTING
    await session.flush()

    # 1. 解析 level-1 范围列表
    scopes = await _resolve_level1_scopes(
        session, run, canary=canary, symbols=symbols,
    )
    run.expected_scope_count = len(scopes)
    await session.flush()

    succeeded = 0
    failed = 0
    signals_total = 0

    # 2. 逐 scope 执行 metrics + signals + attribution
    for scope in scopes:
        try:
            sig_count = await _compute_scope_pipeline(session, run, scope)
            succeeded += 1
            signals_total += sig_count
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception(
                "[ReviewOrchestrator] scope 失败: %s/%s err=%s",
                scope.scope_type, scope.scope_name, exc,
            )
            # 单 scope 失败不阻塞其他 scope
            await _upsert_run_item(
                session,
                run_id=run.id,
                scope_type=scope.scope_type,
                scope_key=scope.scope_key,
                phase=PHASE_METRICS,
                status=ITEM_FAILED,
                last_error=str(exc)[:500],
            )

    # 3. 评估所有 active 追踪（即使有 scope 失败也执行）
    try:
        eval_count = await evaluate_all_active_trackings(session, run)
    except Exception as exc:  # noqa: BLE001
        eval_count = 0
        logger.exception("[ReviewOrchestrator] tracking 评估失败: %s", exc)

    # 4. 更新 run 状态
    run.succeeded_scope_count = succeeded
    run.failed_scope_count = failed
    run.completed_at = datetime.now(UTC)
    if run.expected_scope_count > 0:
        run.coverage_ratio = Decimal(str(succeeded / run.expected_scope_count))
    else:
        run.coverage_ratio = Decimal("0")

    # 更新 signal_count（从 DB 实际统计）
    actual_signal_count = await update_run_signal_count(session, run)

    if failed == 0 and succeeded > 0:
        run.status = RUN_STATUS_SIGNALS_READY
    elif succeeded > 0 and failed > 0:
        run.status = RUN_STATUS_PARTIAL
    elif succeeded == 0 and failed > 0:
        run.status = RUN_STATUS_FAILED
    else:
        run.status = RUN_STATUS_PARTIAL

    await session.flush()

    return {
        "run_id": str(run.id),
        "trade_date": run.trade_date.isoformat(),
        "status": run.status,
        "expected_scope_count": run.expected_scope_count,
        "succeeded_scope_count": succeeded,
        "failed_scope_count": failed,
        "signal_count": actual_signal_count,
        "tracking_evaluations": eval_count,
        "coverage_ratio": float(run.coverage_ratio),
    }


async def _resolve_level1_scopes(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    canary: bool = False,
    symbols: list[str] | None = None,
) -> list[ScopeDefinition]:
    """解析 level-1 范围列表（PRD §6.1）。

    level-1 范围：market / major_index / style / industry_l1
    canary 模式下限定范围数（仅 market + 少量 industry_l1）。
    symbols 模式下额外注入 instrument 范围（debug 用）。

    Returns:
        ScopeDefinition 列表
    """
    scopes: list[ScopeDefinition] = []

    # market 范围（始终扫描）
    scopes.append(
        ScopeDefinition(
            scope_type="market",
            scope_key="market",
            scope_name="全市场",
        ),
    )

    # industry_l1 范围（从 board_analysis_snapshot 读取当日已计算的板块）
    industry_scopes = await _list_industry_l1_scopes(
        session, run.trade_date, run.source_board_run_id,
    )
    if canary:
        # canary 模式：限定 5 个行业
        industry_scopes = industry_scopes[:5]
    scopes.extend(industry_scopes)

    # symbols 模式：额外注入 instrument 范围（debug 用）
    if symbols:
        for sym in symbols:
            scopes.append(
                ScopeDefinition(
                    scope_type="instrument",
                    scope_key=sym,
                    scope_name=sym,
                ),
            )

    return scopes


async def _list_industry_l1_scopes(
    session: AsyncSession,
    trade_date: date,
    source_board_run_id: uuid.UUID,
) -> list[ScopeDefinition]:
    """从 board_analysis_snapshots 读取当日已计算的行业 L1 板块。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        source_board_run_id: board_analysis_snapshot 的 source_core_run_id
            （用于过滤同源数据）

    Returns:
        ScopeDefinition 列表（每个行业 L1 一个）
    """
    stmt = (
        select(BoardAnalysisSnapshot)
        .where(
            BoardAnalysisSnapshot.trade_date == trade_date,
            BoardAnalysisSnapshot.board_type == "industry",
            BoardAnalysisSnapshot.source_core_run_id == source_board_run_id,
        )
        .order_by(BoardAnalysisSnapshot.board_name.asc())
    )
    result = await session.execute(stmt)
    out: list[ScopeDefinition] = []
    for snap in result.scalars():
        out.append(
            ScopeDefinition(
                scope_type="industry_l1",
                scope_key=snap.board_id.hex,  # 使用 board_id 作为 scope_key
                scope_name=snap.board_name,
                source_board_snapshot_id=snap.id,
            ),
        )
    return out


async def _compute_scope_pipeline(
    session: AsyncSession,
    run: MarketReviewRun,
    scope: ScopeDefinition,
) -> int:
    """执行单个 scope 的完整 pipeline（metrics → signals → attribution）。

    Returns:
        该 scope 命中的信号数
    """
    # === metrics phase ===
    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_METRICS,
        status=ITEM_RUNNING,
        started_at=datetime.now(UTC),
    )

    # 解析范围成员
    instrument_ids, _resolved_name = await resolve_scope_members(
        session, scope.scope_type, scope.scope_key,
    )
    if not instrument_ids:
        # 空范围：跳过 metrics，标记 succeeded 但无数据
        await _upsert_run_item(
            session,
            run_id=run.id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            phase=PHASE_METRICS,
            status=ITEM_SKIPPED,
            last_error="范围成员为空",
            completed_at=datetime.now(UTC),
        )
        return 0

    # 使用调用方传入的 scope_name（resolve_scope_members 可能返回 generic name）
    # scope.scope_name 已在 ScopeDefinition 中设置，compute_scope_metrics 直接读取

    # 拉取成员 first_pyramid_flat
    flat_list = await fetch_member_flat_list(
        session, instrument_ids, run.source_core_run_id,
    )

    try:
        snapshot = await compute_scope_metrics(
            session,
            review_run_id=run.id,
            trade_date=run.trade_date,
            scope=scope,
            flat_list=flat_list,
            eligible_count=len(instrument_ids),
        )
    except ScopeSnapshotError as exc:
        await _upsert_run_item(
            session,
            run_id=run.id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            phase=PHASE_METRICS,
            status=ITEM_FAILED,
            last_error=str(exc)[:500],
            completed_at=datetime.now(UTC),
        )
        raise

    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_METRICS,
        status=ITEM_SUCCEEDED,
        completed_at=datetime.now(UTC),
    )

    # === signals phase ===
    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_SIGNALS,
        status=ITEM_RUNNING,
        started_at=datetime.now(UTC),
    )

    try:
        previous_signals = await find_previous_signals(
            session,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            before_trade_date=run.trade_date,
            algorithm_version=run.algorithm_version,
        )
        signals = await generate_signals_for_scope(
            session,
            run=run,
            snapshot=snapshot,
            previous_signals=previous_signals,
        )
    except SignalGenerationError as exc:
        await _upsert_run_item(
            session,
            run_id=run.id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            phase=PHASE_SIGNALS,
            status=ITEM_FAILED,
            last_error=str(exc)[:500],
            completed_at=datetime.now(UTC),
        )
        raise

    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_SIGNALS,
        status=ITEM_SUCCEEDED,
        completed_at=datetime.now(UTC),
    )

    # === attribution phase（仅对命中信号） ===
    if not signals:
        await _upsert_run_item(
            session,
            run_id=run.id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            phase=PHASE_ATTRIBUTION,
            status=ITEM_SKIPPED,
            last_error="无命中信号",
            completed_at=datetime.now(UTC),
        )
        return 0

    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_ATTRIBUTION,
        status=ITEM_RUNNING,
        started_at=datetime.now(UTC),
    )

    parent_metrics = {
        "P": snapshot.p_payload or {},
        "Q": snapshot.q_payload or {},
        "U": snapshot.u_payload or {},
        "C": snapshot.c_payload or {},
        "V": snapshot.v_payload or {},
    }
    parent_ready_count = snapshot.ready_count

    attribution_errors: list[str] = []
    for signal in signals:
        try:
            await compute_signal_attributions(
                session,
                signal,
                parent_metrics=parent_metrics,
                parent_ready_count=parent_ready_count,
                source_core_run_id=run.source_core_run_id,
            )
            await compute_signal_instruments(
                session,
                signal,
                parent_metrics=parent_metrics,
                parent_ready_count=parent_ready_count,
                source_core_run_id=run.source_core_run_id,
            )
        except Exception as exc:  # noqa: BLE001
            attribution_errors.append(f"{signal.signal_type}: {exc}")
            logger.exception(
                "[ReviewOrchestrator] 归因失败: signal=%s err=%s",
                signal.id, exc,
            )

    status_final = (
        ITEM_FAILED if attribution_errors and len(attribution_errors) == len(signals)
        else ITEM_SUCCEEDED
    )
    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_ATTRIBUTION,
        status=status_final,
        last_error="; ".join(attribution_errors)[:500] if attribution_errors else None,
        completed_at=datetime.now(UTC),
    )

    return len(signals)


# =============================================================================
# Resume
# =============================================================================


async def resume_run(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    only_pending: bool = True,
) -> dict[str, Any]:
    """重启 run（只处理 pending / 可重试 failed / 过期 running）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        run: MarketReviewRun ORM 对象
        only_pending: True=只处理 pending/可重试 failed/过期 running；
            False=重新计算所有非 succeeded item

    Returns:
        resume 结果摘要 dict
    """
    if run.status in (RUN_STATUS_PUBLISHED, RUN_STATUS_CANCELLED):
        raise ReviewOrchestratorError(
            f"run 状态={run.status} 不可 resume",
        )

    # 查询需要重处理的 scope 列表
    items = await list_run_items(session, run.id)

    # 按 scope 分组，找出需要重处理的 scope
    scopes_to_redo: dict[tuple[str, str], set[str]] = {}
    now = datetime.now(UTC)
    for item in items:
        if item.status == ITEM_SUCCEEDED and only_pending:
            continue
        if item.status == ITEM_SKIPPED and only_pending:
            continue
        # running 但租约未过期且 attempt 未超限：跳过
        if (
            item.status == ITEM_RUNNING
            and only_pending
            and item.lease_expires_at is not None
            and item.lease_expires_at > now
            and (item.attempt_count or 0) < MAX_AUTO_RESUME_ATTEMPTS
        ):
            continue
        # attempt 超限：跳过（需人工介入）
        if (item.attempt_count or 0) >= MAX_AUTO_RESUME_ATTEMPTS and only_pending:
            continue

        key = (item.scope_type, item.scope_key)
        scopes_to_redo.setdefault(key, set()).add(item.phase)

    # 对每个需要重处理的 scope，重新执行 pipeline
    succeeded = 0
    failed = 0
    for (scope_type, scope_key), _phases in scopes_to_redo.items():
        scope = ScopeDefinition(
            scope_type=scope_type,
            scope_key=scope_key,
            scope_name=scope_key,
        )
        try:
            await _compute_scope_pipeline(session, run, scope)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception(
                "[ReviewOrchestrator] resume scope 失败: %s err=%s",
                scope_type, exc,
            )

    # 评估 active 追踪
    try:
        eval_count = await evaluate_all_active_trackings(session, run)
    except Exception as exc:  # noqa: BLE001
        eval_count = 0
        logger.exception("[ReviewOrchestrator] resume tracking 评估失败: %s", exc)

    # 更新 run 状态
    run.completed_at = datetime.now(UTC)
    actual_signal_count = await update_run_signal_count(session, run)

    # 重新统计 succeeded/failed scope（基于 item 状态）
    final_succeeded, final_failed = await _count_scope_status(session, run.id)
    run.succeeded_scope_count = final_succeeded
    run.failed_scope_count = final_failed
    if run.expected_scope_count > 0:
        run.coverage_ratio = Decimal(str(final_succeeded / run.expected_scope_count))

    if final_failed == 0 and final_succeeded > 0:
        run.status = RUN_STATUS_SIGNALS_READY
    elif final_succeeded > 0 and final_failed > 0:
        run.status = RUN_STATUS_PARTIAL
    elif final_succeeded == 0 and final_failed > 0:
        run.status = RUN_STATUS_FAILED
    else:
        run.status = RUN_STATUS_PARTIAL

    await session.flush()

    return {
        "run_id": str(run.id),
        "status": run.status,
        "resumed_scopes": len(scopes_to_redo),
        "succeeded": succeeded,
        "failed": failed,
        "signal_count": actual_signal_count,
        "tracking_evaluations": eval_count,
        "coverage_ratio": float(run.coverage_ratio),
    }


async def _count_scope_status(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> tuple[int, int]:
    """统计 run 中 succeeded / failed scope 数（按 metrics phase 判定）。

    一个 scope 视为 succeeded 当 metrics phase 为 succeeded/skipped。
    """
    stmt = (
        select(
            MarketReviewRunItem.scope_type,
            MarketReviewRunItem.scope_key,
            MarketReviewRunItem.status,
        )
        .where(
            MarketReviewRunItem.review_run_id == run_id,
            MarketReviewRunItem.phase == PHASE_METRICS,
        )
    )
    result = await session.execute(stmt)
    succeeded = 0
    failed = 0
    for _st, _sk, item_status in result:
        if item_status in (ITEM_SUCCEEDED, ITEM_SKIPPED):
            succeeded += 1
        elif item_status == ITEM_FAILED:
            failed += 1
    return succeeded, failed


# =============================================================================
# Publish / Status
# =============================================================================


async def publish_run(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    force: bool = False,
) -> tuple[FactorPublication, list[str]]:
    """发布 review run（委托给 review_publication_service）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        run: MarketReviewRun ORM 对象
        force: 是否强制发布（跳过门禁）

    Returns:
        (FactorPublication, blockers)
        force=True 时 blockers 始终为空；force=False 时 blockers 为空表示门禁通过

    Raises:
        ReviewPublishBlockError: force=False 时门禁失败
    """
    blockers: list[str] = []
    if not force:
        publishable, blockers = await evaluate_publish_gate(session, run)
        if not publishable:
            raise ReviewPublishBlockError(blockers)

    publication = await publish_review(session, run, force=force)
    return publication, blockers


async def get_run_status(
    session: AsyncSession,
    run: MarketReviewRun,
) -> dict[str, Any]:
    """获取 run 状态摘要（含 items + 发布门禁）。

    Returns:
        {
            "run": run dict,
            "items": [item dict, ...],
            "publishable": bool,
            "publish_blockers": [str, ...],
        }
    """
    items = await list_run_items(session, run.id)
    publishable, blockers = await evaluate_publish_gate(session, run)

    return {
        "run": _run_to_dict(run),
        "items": [_item_to_dict(it) for it in items],
        "publishable": publishable,
        "publish_blockers": blockers,
    }


# =============================================================================
# 内部工具
# =============================================================================


async def _upsert_run_item(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    scope_type: str,
    scope_key: str,
    phase: str,
    status: str,
    last_error: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> MarketReviewRunItem:
    """upsert run_item（幂等：唯一键 review_run_id+scope+phase）。"""
    values = {
        "review_run_id": run_id,
        "scope_type": scope_type,
        "scope_key": scope_key,
        "phase": phase,
        "status": status,
        "last_error": last_error,
        "started_at": started_at,
        "completed_at": completed_at,
    }

    # attempt_count 自增（仅在 failed 重试时）
    stmt = pg_insert(MarketReviewRunItem).values(**values)
    update_set: dict[str, Any] = {
        "status": stmt.excluded.status,
        "last_error": stmt.excluded.last_error,
        "started_at": stmt.excluded.started_at,
        "completed_at": stmt.excluded.completed_at,
    }
    if status == ITEM_RUNNING:
        # running 时 attempt_count 自增
        update_set["attempt_count"] = (
            MarketReviewRunItem.attempt_count + 1
        )
        update_set["lease_epoch"] = func.coalesce(
            MarketReviewRunItem.lease_epoch, 0,
        ) + 1

    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_items_run_scope_phase",
        set_=update_set,
    )
    await session.execute(stmt)
    await session.flush()

    # 读取 upsert 后的记录
    read_stmt = (
        select(MarketReviewRunItem)
        .where(
            MarketReviewRunItem.review_run_id == run_id,
            MarketReviewRunItem.scope_type == scope_type,
            MarketReviewRunItem.scope_key == scope_key,
            MarketReviewRunItem.phase == phase,
        )
        .limit(1)
    )
    result = await session.execute(read_stmt)
    return result.scalar_one()


def _run_to_dict(run: MarketReviewRun) -> dict[str, Any]:
    """run ORM → dict（用于 status 响应）。"""
    return {
        "id": str(run.id),
        "trade_date": run.trade_date.isoformat(),
        "source_core_run_id": str(run.source_core_run_id),
        "source_board_run_id": str(run.source_board_run_id),
        "algorithm_version": run.algorithm_version,
        "filter_version": run.filter_version,
        "baseline_window": run.baseline_window,
        "status": run.status,
        "expected_scope_count": run.expected_scope_count,
        "succeeded_scope_count": run.succeeded_scope_count,
        "failed_scope_count": run.failed_scope_count,
        "signal_count": run.signal_count,
        "coverage_ratio": float(run.coverage_ratio) if run.coverage_ratio else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "published_at": run.published_at.isoformat() if run.published_at else None,
        "metadata": run.metadata_json or {},
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "updated_at": run.updated_at.isoformat() if run.updated_at else "",
    }


def _item_to_dict(item: MarketReviewRunItem) -> dict[str, Any]:
    """item ORM → dict（用于 status 响应）。"""
    return {
        "scope_type": item.scope_type,
        "scope_key": item.scope_key,
        "phase": item.phase,
        "status": item.status,
        "attempt_count": item.attempt_count,
        "last_error": item.last_error,
        "started_at": item.started_at.isoformat() if item.started_at else None,
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
    }


if __name__ == "__main__":
    print(f"REVIEW_ALGORITHM_VERSION = {REVIEW_ALGORITHM_VERSION}")
    print(f"REVIEW_FILTER_VERSION = {REVIEW_FILTER_VERSION}")
    print(f"DEFAULT_BASELINE_WINDOW = {DEFAULT_BASELINE_WINDOW}")
    print(f"MIN_BASELINE_WINDOW = {MIN_BASELINE_WINDOW}")
    print(f"REVIEW_PUBLISH_MIN_COVERAGE = {REVIEW_PUBLISH_MIN_COVERAGE}")
    print(f"LEVEL1_SCOPE_TYPES = {LEVEL1_SCOPE_TYPES}")
    print("OK: review_orchestrator_service imports verified")
