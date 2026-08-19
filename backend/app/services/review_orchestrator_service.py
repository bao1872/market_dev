"""复盘编排服务 - 端到端编排 review run（PRD70 V2 §11）。

职责（PRD70 V2 编排顺序）：
1. create_run: 创建或复用 MarketReviewRun（幂等：唯一键 trade_date+source_runs+版本）
2. compute_run: 执行完整流程
   - [V2] 解析全部 Discovery scope 列表（market / major_index / style /
     industry_l1 / industry_l2 / industry_l3 / concept 平行独立）
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
- [AUD-04/05] Review 输入身份不含 chip 等增强产品；create_run 零次 chip 查询，
  且已存在的 run 不被后续 create_run 改写（on_conflict_do_nothing）
- 历史基线默认 120 日、最低 60 日
- 单 scope 失败不回滚其他 scope（写入 last_error 后继续）

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_orchestrator_service
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.filter_definitions import REVIEW_FILTER_VERSION

# [2026-08-13 双轨并存] 规范 Scope Observation 事实层（PRD §7.2-§7.17 v2.3）。
# 以下三者在 shadow 路径已被真实数据验证契约/invariant/readiness；
# 此处仅把它们接入 compute 主流程写入 ReviewScopeObservationFact，
# 供 EvidenceDrawer / scope_evidence_service 消费。Discovery/筛选器/信号管线
# 仍走 legacy P/Q/U/C/V（本轮不动），二者双轨并存。
#
# [REVIEW-EXECUTION-PATH-CONSOLIDATION] 规范事实层唯一 preparation owner =
# ``prepare_current_scope_observations_batch``（一次解析 memberships + union facts +
# slice）；orchestrator 不再逐 scope 调用单 scope 入口。
from app.domain.review.scope_observation import compute_scope_observation
from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.models.board_analysis_snapshot import BoardAnalysisRun, BoardAnalysisSnapshot
from app.models.factor_publication import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
from app.models.market_board import MarketBoard
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewRunItem,
    MarketReviewScopeSnapshot,
)
from app.services.board_membership_service import list_universe_definitions_at
from app.services.first_pyramid_service import HISTORY_CONTRACT_VERSION
from app.services.observation_prep import check_observation_invariants
from app.services.review_attribution_service import (
    compute_signal_attributions,
    compute_signal_instruments,
)
from app.services.review_bootstrap_service import (
    validate_canonical_history_run_readiness,
)
from app.services.review_observation_persistence_service import (
    is_scope_observation_persistence_excluded,
    save_scope_observation_fact,
)
from app.services.review_observation_prep_service import (
    ScopeReplaySpec,
    prepare_current_scope_observations_batch,
)
from app.services.review_publication_service import (
    REVIEW_PUBLISH_MIN_COVERAGE,
    ReviewPublishBlockError,
    evaluate_publish_gate,
    publish_review,
)
from app.services.review_scope_service import (
    LEVEL1_SCOPE_TYPES,
    OptionalScopeUnavailableError,
    ScopeDefinition,
    ScopeSnapshotError,
    apply_cross_section_percentiles,
    compute_scope_metrics,
    fetch_member_flat_list,
    load_day_fact_maps,
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
# review-2.0.0: typed PIT member facts, true daily returns, day-over-day U,
# dimensionally correct V, and two-pass cross-section-before-signal evaluation.
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


@dataclass(frozen=True)
class ReviewRunCreation:
    """create_run 的返回合同（替代裸 tuple）。

    [Phase4.1 corrective] 显式区分“本次新建”与“复用既有 run”：
    - run: 创建或复用的 MarketReviewRun 对象
    - created: True=本次新插入一行（可安全 compute）；False=复用既有 run
      （可能已 published，调用方必须按不可变语义处理，禁止原地重算）

    使用 dataclass 而非裸 tuple，避免调用方误把 (run, created) 当 run 使用，
    并在 mypy 层强制解构。
    """

    run: MarketReviewRun
    created: bool


def check_run_scope_compatibility(
    *,
    existing_canary: bool,
    existing_symbols: frozenset[str] | set[str] | list[str],
    requested_canary: bool,
    requested_symbols: frozenset[str] | set[str] | list[str],
) -> bool:
    """纯函数：判定既有 run 的 scope 是否允许复用给新请求。

    scope 由 (canary, symbols) 二元组定义。两者都一致才算兼容（可安全复用/
    自动 resume）；任一不一致即冲突，必须 fail-safe 拒绝复用（避免把
    canary/debug 结果续成 formal run）。

    覆盖五种行为：
    - formal / formal（两者皆全市场）：兼容
    - same canary（canary+相同 symbols）：兼容
    - formal → canary（既有 formal、新请求 canary）：冲突
    - canary → formal（既有 canary、新请求 formal）：冲突
    - different symbols（canary 但 symbols 不同）：冲突

    [Phase4.1 corrective / 临时安全限制] 当前无独立的 canary/formal DB
    namespace，跨 scope reuse 一律 fail-safe reject。永久 run_mode/namespace
    设计留待 Phase 5 / PRD 决策，不得宣称已彻底解决。
    """
    return (bool(existing_canary), frozenset(existing_symbols or [])) == (
        bool(requested_canary),
        frozenset(requested_symbols or []),
    )


# =============================================================================
# Run 创建与查询
# =============================================================================


async def _create_run_impl(
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
) -> tuple[MarketReviewRun, bool]:
    """创建或复用 review run（幂等：唯一键组合保证）的**内部实现**。

    [Phase4.2 corrective] 恢复 baseline(76e1338) backward-compat 合同：
    `create_run` 返回 **MarketReviewRun**（与全部既有生产调用方 after_close_orchestrator
    / review_compute_cli / PG integration 一致）。需要显式 created/reused 信息的调用方
    （如 Admin）改用 `create_run_with_result() -> ReviewRunCreation`。

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
        tuple[MarketReviewRun, bool]：(run 对象, created)。
        - created=True：本次新插入了一行（可安全 compute）
        - created=False：复用既有 run（可能已 published，禁止原地重算）
        公共入口 `create_run` 只返回 run；`create_run_with_result` 返回
        ReviewRunCreation(run, created)。

    Raises:
        ReviewOrchestratorError: 输入校验失败（缺 publication pointer 等）；
            或 [Phase4.1] 既有 run 的 canary/symbols scope 与新请求不一致
            （scope 冲突，避免 canary 结果续成 formal run）
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

    # [AUD-04/05 2026-08-07] Review 输入身份仅由 stock_core + market_aggregation
    # + 历史观测构成。chip 属增强产品，不参与 Review lineage：创建阶段零次 chip
    # 查询，也不写入 source_chip_run_id / degraded_reasons / chip_coverage。
    if dry_run:
        # dry-run：不写 DB，返回一个非持久化的 run 对象供调用方打印。
        # created=False —— 未真正插入任何行，调用方不得据此触发 compute。
        return (
            MarketReviewRun(
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
            ),
            False,
        )
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
    # [AUD-05 2026-08-07] Review run 一经创建即不可变：晚到的增强产品（chip 等）
    # 不得改写已存在的 run 行。DO NOTHING 是唯一能在 SQL 层保证
    # “已有 run 不被后续 create_run 修改”的写法。RETURNING id 让我们可以区分
    # “本事务新插入” vs “唯一键冲突被 DO NOTHING 跳过（复用既有行）”，从而对外
    # 暴露 created 语义供调用方决定能否 compute（published run 严禁原地重算）。
    stmt = stmt.on_conflict_do_nothing(
        constraint="uq_review_runs_date_core_board_algo_filter",
    ).returning(MarketReviewRun.id)
    insert_result = await session.execute(stmt)
    await session.flush()
    inserted_row = insert_result.scalar_one_or_none()
    created = inserted_row is not None

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

    # [Phase4.1 corrective] canary/debug 与 formal run identity 不得默认混用。
    # 既有 run 的 scope（canary/symbols）与新请求的 scope 不一致时，禁止复用/自动
    # resume（fail-safe），避免把 canary/debug 结果续成 formal run。现行 PRD 未明确
    # 允许共享 identity，故采用 fail-safe：scope 冲突即报明确错误（等价 409）。
    # scope 一致（含两者都是 formal 全市场）则复用是安全的，正常返回。
    # 判定抽成纯函数 check_run_scope_compatibility，可直接单测五种行为。
    if created is False:
        compatible = check_run_scope_compatibility(
            existing_canary=bool(run.metadata_json.get("canary", False)),
            existing_symbols=run.metadata_json.get("symbols") or [],
            requested_canary=bool(canary),
            requested_symbols=symbols or [],
        )
        if not compatible:
            existing_canary = bool(run.metadata_json.get("canary", False))
            existing_symbols = frozenset(run.metadata_json.get("symbols") or [])
            raise ReviewOrchestratorError(
                f"create_run scope 冲突：既有 run {run.id} 的 "
                f"canary={existing_canary}/symbols={sorted(existing_symbols)} "
                f"与新请求 canary={bool(canary)}/symbols={sorted(symbols or [])} "
                f"不一致；禁止跨 scope 复用同一 run identity（不得把 "
                f"canary/debug 结果续成 formal run）。请改用不同 trade_date 或算法版本，"
                f"或显式清理既有 run。",
            )

    return run, created


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

    [Phase4.2 corrective] 恢复 baseline(76e1338) backward-compat 合同：
    `create_run` 返回 **MarketReviewRun**（与全部既有生产调用方 after_close_orchestrator
    / review_compute_cli / PG integration 一致）。需要显式 created/reused 信息的调用方
    （如 Admin）改用 `create_run_with_result() -> ReviewRunCreation`。

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
        MarketReviewRun：本次新插入或复用的 run 对象。
        复用既有 run（可能已 published）时返回既有对象，调用方必须按不可变语义
        处理，禁止原地重算。

    Raises:
        ReviewOrchestratorError: 输入校验失败（缺 publication pointer 等）；
            或 [Phase4.1] 既有 run 的 canary/symbols scope 与新请求不一致
            （scope 冲突，避免 canary 结果续成 formal run）
    """
    run, _created = await _create_run_impl(
        session,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        source_board_run_id=source_board_run_id,
        algorithm_version=algorithm_version,
        filter_version=filter_version,
        baseline_window=baseline_window,
        canary=canary,
        symbols=symbols,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
    )
    return run


async def create_run_with_result(
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
) -> ReviewRunCreation:
    """创建或复用 review run，并返回显式的 created/reused 信息。

    [Phase4.2 corrective] 真正的 create_run_with_result 入口（非 alias 冒充兼容层）。
    内部复用 `create_run` 的真实实现，仅额外暴露 ReviewRunCreation(run, created) 合同，
    供需要区分“本次新建 vs 复用既有”的调用方（如 Admin）使用。

    Returns:
        ReviewRunCreation(run, created)
        - created=True：本次新插入了一行（可安全 compute）
        - created=False：复用既有 run（可能已 published，禁止原地重算）
    """
    run, created = await _create_run_impl(
        session,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        source_board_run_id=source_board_run_id,
        algorithm_version=algorithm_version,
        filter_version=filter_version,
        baseline_window=baseline_window,
        canary=canary,
        symbols=symbols,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
    )
    return ReviewRunCreation(run=run, created=created)


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
    source_board_run_id：直接使用 market_aggregation pointer 指向的
        board_analysis_runs.id

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        source_core_run_id: 调用方显式传入的 stock_core run_id（None 时自动解析）
        source_board_run_id: 调用方显式传入的 board_analysis_runs.id
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
    #
    # [PC-40/PC-41] Review 只消费正式 market_aggregation pointer：pointer 必须存在，
    # 且无论 source_board_run_id 是自动解析还是调用方显式传入，都必须严格等于
    # pointer.data_run_id。Review **不得**自行挑选 board run
    # （无 fallback / 无 latest-partial / 无绕 pointer 路径）。
    board_pub = await _get_publication(
        session, trade_date, PUBLICATION_KIND_MARKET_AGGREGATION,
    )
    if board_pub is None:
        raise ReviewOrchestratorError(
            f"trade_date={trade_date} 无已发布 board_analysis pointer，"
            f"必须先完成板块分析并发布",
        )
    resolved_board_id = (
        board_pub.data_run_id if source_board_run_id is None else source_board_run_id
    )
    if resolved_board_id != board_pub.data_run_id:
        raise ReviewOrchestratorError(
            f"[PC-41] source_board_run_id={resolved_board_id} 与正式 "
            f"market_aggregation pointer.data_run_id="
            f"{board_pub.data_run_id} 不一致",
        )

    board_run = await session.get(BoardAnalysisRun, resolved_board_id)
    if board_run is None:
        raise ReviewOrchestratorError(
            f"board_analysis_run id={resolved_board_id} 不存在",
        )
    if board_run.trade_date != trade_date:
        raise ReviewOrchestratorError("Board batch trade_date 与 Review 不一致")
    if board_run.source_core_run_id != resolved_core_id:
        raise ReviewOrchestratorError("Board batch 与 stock_core pointer 不同源")
    # [PRD 31 PC-42] board_aggregation 是 mandatory product，但 MANDATORY != PERFECT。
    # 正式 pointer 指向的 run 可以是 succeeded（READY）或 degraded-publishable 的
    # partial（DEGRADED）；DEGRADED 不阻断 Review，FAILED / 非终态阻断。
    # 这里接受的是「正式 pointer 指向的 degraded run」，而不是 Review 自己挑 partial run。
    if board_run.status not in ("succeeded", "partial"):
        raise ReviewOrchestratorError(
            f"Board batch 非 ready: status={board_run.status}",
        )

    return resolved_core_id, resolved_board_id


# [AUD-04/05 2026-08-07] `_resolve_chip_dependency` 与 `_load_core_expected_count`
# 已退役并删除。理由：其 source_chip_run_id 恒为 None（chip 无独立 run，只经
# core_run_id 挂靠 stock_core），唯一实际产出是把 chip 域的 degraded_reasons /
# chip_coverage 写进 Review lineage，使 Review 在创建阶段依赖增强产品，并让晚到的
# chip 通过 ON CONFLICT 改写已发布 Review。Review 的输入身份现仅由
# stock_core + market_aggregation + 历史观测构成。
# chip 就绪度改由 ProductReadiness / chip 域表达，不再经 Review 透出。


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
    # [Phase4.1 corrective] 不可变守卫：已发布的 run 是最终事实，禁止任何原地重算
    # （无论调用方是 Admin POST 复用既有 run，还是 canary/debug 误用正式 run 身份）。
    # 这是服务层最后一道防线，与 Admin 端 created/reused/published 语义互为冗余。
    if run.status == RUN_STATUS_PUBLISHED:
        raise ReviewOrchestratorError(
            f"run={run.id} 已发布（status=published），禁止原地重算；"
            f"如确需重算请走新 trade_date 或新算法版本，而非复用已发布 run",
        )
    run.started_at = datetime.now(UTC)
    run.status = RUN_STATUS_COMPUTING
    await session.flush()

    # 1. [V2] 解析全部 Discovery scope 列表（平行独立参与 observation）
    scopes = await _resolve_all_discovery_scopes(
        session, run, canary=canary, symbols=symbols,
    )
    run.expected_scope_count = len(scopes)
    await session.flush()

    # [Phase4C 2026-08-09 P0-B] 绑定 canonical history source（生命周期不漂移）：
    # - run.metadata_json 已有绑定 → 复用（resume/retry 绝不重新解析 latest ready run）
    # - 无绑定 → 解析当时最新合法 canonical source 并立即写入 metadata
    # - bound source 不存在 / contract mismatch / 非 canonical-compatible → fail closed
    (
        canonical_source_run_id,
        canonical_contract_version,
    ) = await _bind_or_reuse_canonical_history_source(session, run)

    # [REVIEW-FACT-PARITY-02 §10] load-once：整个 compute_run 只调用一次
    # load_day_fact_maps，scope loop 只按 instrument_id 从内存 map 取**引用**。
    # 禁止每 scope 再调 fetch_member_flat_list（date × scope × 400 日 bars 重复读取
    # 是此前 Review OOM 的主因），禁止 deepcopy / JSON roundtrip / per-scope rebuild。
    # lineage 由 §11 guard 在 loader 内 fail closed。
    day_fact_map = await load_day_fact_maps(
        session,
        trade_date=run.trade_date,
        source_core_run_id=run.source_core_run_id,
        required_source_history_run_id=canonical_source_run_id,
        required_history_contract_version=canonical_contract_version,
    )
    logger.info(
        "[ReviewOrchestrator] load_day_fact_maps once: trade_date=%s facts=%d "
        "source_history_run_id=%s",
        run.trade_date,
        len(day_fact_map),
        canonical_source_run_id,
    )

    # === 规范 Scope Observation 事实层 batch prepare（PRD §7.2-§7.17 v2.3）===
    # [REVIEW-EXECUTION-PATH-CONSOLIDATION] 唯一 preparation owner =
    # prepare_current_scope_observations_batch：一次解析 PIT(T)/PIT(T-1) memberships、
    # 一次加载 union member facts、slice 成各 Scope PreparedScope。scope 循环只从
    # 该 map SELECT 结果。双写失败（batch prepare 或落库）隔离在 try/except 内，
    # 不影响 legacy metrics/signal 主链；这是错误隔离，不是回退到旧路径。
    prepared_observations: dict[str, Any] | None = None
    try:
        eligible_specs = [
            ScopeReplaySpec(
                scope_type=s.scope_type,
                scope_key=s.scope_key,
                scope_name=s.scope_name,
                member_ids=(),
            )
            for s in scopes
            if not is_scope_observation_persistence_excluded(
                scope_type=s.scope_type,
                scope_name=s.scope_name,
            )
        ]
        if eligible_specs:
            prepared_observations = await prepare_current_scope_observations_batch(
                session, run.trade_date, eligible_specs
            )
            logger.info(
                "[ReviewOrchestrator] 规范事实层 batch prepare: scopes=%d prepared=%d",
                len(eligible_specs), len(prepared_observations),
            )
    except Exception as exc:  # noqa: BLE001 - 隔离双写失败，不破坏 Discovery
        prepared_observations = None
        logger.warning(
            "[ReviewOrchestrator] 规范事实层 batch prepare 失败（不影响 legacy signal）: %s",
            exc,
        )

    succeeded = 0
    failed = 0
    signals_total = 0

    metric_results: list[tuple[ScopeDefinition, MarketReviewScopeSnapshot, dict[str, dict[str, list[float]]] | None]] = []

    # 2. 第一遍：全部 scope 只计算 raw/normalized metrics。
    for scope in scopes:
        try:
            snapshot, history_maps = await _compute_scope_metrics_phase(
                session,
                run,
                scope,
                required_history_contract_version=canonical_contract_version,
                required_source_history_run_id=canonical_source_run_id,
                day_fact_map=day_fact_map,
                prepared_observations=prepared_observations,
            )
            succeeded += 1
            if snapshot is not None:
                metric_results.append((scope, snapshot, history_maps))
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

    # 3. 第二遍：同日同 family 横截面分位，完成后才能评估 signal。
    await apply_cross_section_percentiles(session, run.id)
    for scope, snapshot, history_maps in metric_results:
        try:
            signals_total += await _compute_scope_signal_pipeline(
                session, run, scope, snapshot,
                day_fact_map=day_fact_map,
                history_maps=history_maps,
            )
        except Exception as exc:  # noqa: BLE001
            succeeded -= 1
            failed += 1
            logger.exception(
                "[ReviewOrchestrator] signal/attribution 失败: %s/%s err=%s",
                scope.scope_type,
                scope.scope_name,
                exc,
            )

    # 4. 评估所有 active 追踪（即使有 scope 失败也执行）
    try:
        eval_count = await evaluate_all_active_trackings(session, run)
    except Exception as exc:  # noqa: BLE001
        eval_count = 0
        logger.exception("[ReviewOrchestrator] tracking 评估失败: %s", exc)

    # 5. 更新 run 状态
    run.succeeded_scope_count = succeeded
    run.failed_scope_count = failed
    run.completed_at = datetime.now(UTC)
    # [AUD-06 2026-08-07] coverage_ratio 表达真实有效样本覆盖率（数据口径），
    # 不再是 scope 执行成功率；执行率由 succeeded/expected 两列独立表达。
    run.coverage_ratio = await _aggregate_run_data_coverage(session, run.id)

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
        # [AUD-06] coverage_ratio = 真实有效样本覆盖率；执行率单独表达
        "coverage_ratio": float(run.coverage_ratio),
        "scope_execution_rate": _scope_execution_rate(run),
    }


async def _resolve_all_discovery_scopes(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    canary: bool = False,
    symbols: list[str] | None = None,
) -> list[ScopeDefinition]:
    """[V2] 解析全部 Discovery scope 列表（PRD70 §6.1-6.2 平行扫描模型）。

    所有 scope family 在发现阶段独立平行参与 observation：
    market / major_index / style / industry_l1 / industry_l2 / industry_l3 / concept

    Industry taxonomy hierarchy 不成为 discovery eligibility gate。
    canary 模式下限定范围数。
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

    # major_index 范围
    major_index_scopes = await _list_major_index_scopes(session, run.trade_date)
    if canary:
        major_index_scopes = major_index_scopes[:3]
    scopes.extend(major_index_scopes)

    # style 范围
    style_scopes = await _list_style_scopes(session, run.trade_date)
    if canary:
        style_scopes = style_scopes[:2]
    scopes.extend(style_scopes)

    # industry_l1 范围
    industry_l1_scopes = await _list_board_scopes_by_hierarchy(
        session, run.trade_date, run.source_board_run_id,
        board_type="industry", hierarchy_level="L1",
    )
    if canary:
        industry_l1_scopes = industry_l1_scopes[:5]
    scopes.extend(industry_l1_scopes)

    # [V2] industry_l2 范围（平行独立参与 discovery）
    industry_l2_scopes = await _list_board_scopes_by_hierarchy(
        session, run.trade_date, run.source_board_run_id,
        board_type="industry", hierarchy_level="L2",
    )
    if canary:
        industry_l2_scopes = industry_l2_scopes[:3]
    scopes.extend(industry_l2_scopes)

    # [V2] industry_l3 范围（平行独立参与 discovery）
    industry_l3_scopes = await _list_board_scopes_by_hierarchy(
        session, run.trade_date, run.source_board_run_id,
        board_type="industry", hierarchy_level="L3",
    )
    if canary:
        industry_l3_scopes = industry_l3_scopes[:3]
    scopes.extend(industry_l3_scopes)

    # [V2] concept 范围（平行独立参与 discovery，不依赖 industry 命中）
    concept_scopes = await _list_board_scopes_by_hierarchy(
        session, run.trade_date, run.source_board_run_id,
        board_type="concept", hierarchy_level=None,
    )
    if canary:
        concept_scopes = concept_scopes[:5]
    scopes.extend(concept_scopes)

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


async def _list_board_scopes_by_hierarchy(
    session: AsyncSession,
    trade_date: date,
    source_board_run_id: uuid.UUID,
    *,
    board_type: str,
    hierarchy_level: str | None,
) -> list[ScopeDefinition]:
    """从 board_analysis_snapshots 读取当日已计算的板块（通用）。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        source_board_run_id: board_analysis_runs.id
        board_type: "industry" 或 "concept"
        hierarchy_level: "L1"/"L2"/"L3"（concept 为 None）
    """
    stmt = (
        select(BoardAnalysisSnapshot)
        .join(MarketBoard, MarketBoard.id == BoardAnalysisSnapshot.board_id)
        .where(
            BoardAnalysisSnapshot.trade_date == trade_date,
            BoardAnalysisSnapshot.board_type == board_type,
            BoardAnalysisSnapshot.board_analysis_run_id == source_board_run_id,
        )
        .order_by(BoardAnalysisSnapshot.board_name.asc())
    )
    if hierarchy_level is not None:
        stmt = stmt.where(MarketBoard.hierarchyLevel == hierarchy_level)
    result = await session.execute(stmt)
    scope_type_map = {
        ("industry", "L1"): "industry_l1",
        ("industry", "L2"): "industry_l2",
        ("industry", "L3"): "industry_l3",
        ("concept", None): "concept",
    }
    scope_type = scope_type_map[(board_type, hierarchy_level)]
    out: list[ScopeDefinition] = []
    for snap in result.scalars():
        out.append(
            ScopeDefinition(
                scope_type=scope_type,
                scope_key=str(snap.board_id),
                scope_name=snap.board_name,
                source_board_snapshot_id=snap.id,
                taxonomy_version=snap.taxonomy_version,
                taxonomy_compatibility_key=snap.taxonomy_compatibility_key,
                membership_version=snap.membership_version,
            ),
        )
    return out


async def _list_industry_l1_scopes(
    session: AsyncSession,
    trade_date: date,
    source_board_run_id: uuid.UUID,
) -> list[ScopeDefinition]:
    """从 board_analysis_snapshots 读取当日已计算的行业 L1 板块。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        source_board_run_id: 真实 board_analysis_runs.id

    Returns:
        ScopeDefinition 列表（每个行业 L1 一个）
    """
    stmt = (
        select(BoardAnalysisSnapshot)
        .join(MarketBoard, MarketBoard.id == BoardAnalysisSnapshot.board_id)
        .where(
            BoardAnalysisSnapshot.trade_date == trade_date,
            BoardAnalysisSnapshot.board_type == "industry",
            BoardAnalysisSnapshot.board_analysis_run_id == source_board_run_id,
            MarketBoard.hierarchyLevel == "L1",
        )
        .order_by(BoardAnalysisSnapshot.board_name.asc())
    )
    result = await session.execute(stmt)
    out: list[ScopeDefinition] = []
    for snap in result.scalars():
        out.append(
            ScopeDefinition(
                scope_type="industry_l1",
                # [P0 2026-07-30] scope_key 使用 str(board_id)，resolve_scope_members
                # 按 MarketBoard.id 查询（与 _list_major_index_scopes 一致）
                scope_key=str(snap.board_id),
                scope_name=snap.board_name,
                source_board_snapshot_id=snap.id,
                taxonomy_version=snap.taxonomy_version,
                taxonomy_compatibility_key=snap.taxonomy_compatibility_key,
                membership_version=snap.membership_version,
            ),
        )
    return out


async def _list_major_index_scopes(
    session: AsyncSession,
    trade_date: date,
) -> list[ScopeDefinition]:
    """List configured major-index universes valid on the trade date."""
    definitions = await list_universe_definitions_at(
        session, trade_date, universe_type="major_index",
    )
    return [
        ScopeDefinition(
            scope_type="major_index",
            scope_key=definition.universe_key,
            scope_name=definition.name,
            taxonomy_version=definition.version,
            taxonomy_compatibility_key=definition.compatibility_key,
            membership_version=definition.membership_version,
        )
        for definition in definitions
    ]


async def _list_style_scopes(
    session: AsyncSession,
    trade_date: date,
) -> list[ScopeDefinition]:
    """List configured style universes valid on the trade date."""
    definitions = await list_universe_definitions_at(
        session, trade_date, universe_type="style",
    )
    return [
        ScopeDefinition(
            scope_type="style",
            scope_key=definition.universe_key,
            scope_name=definition.name,
            taxonomy_version=definition.version,
            taxonomy_compatibility_key=definition.compatibility_key,
            membership_version=definition.membership_version,
        )
        for definition in definitions
    ]


async def _build_scope_history(
    session: AsyncSession,
    *,
    scope: ScopeDefinition,
    trade_date: date,
    algorithm_version: str,
    baseline_window: int,
    required_history_contract_version: str | None = None,
    required_taxonomy_compatibility_key: str | None = None,
    required_source_history_run_id: uuid.UUID | None = None,
) -> tuple[
    dict[str, dict[str, list[float]]] | None,
    dict[str, float] | None,
    dict[str, float] | None,
]:
    """从同算法版本的 observation SSOT 构建历史序列（PRD §7.1）。

    [Phase4C 2026-08-09] 绑定 canonical history lineage（P0-B）：
    必须显式传入 ``required_history_contract_version`` /
    ``required_taxonomy_compatibility_key`` / ``required_source_history_run_id``，
    由调用方从正式 canonical history readiness contract 解析得到（**禁止硬编码
    生产 run id**；market scope 的 taxonomy key 可为 None = canonical market series）。

    Args:
        session: 异步 DB 会话
        scope: 当前 scope 定义
        trade_date: 当前交易日（排除当日，只用历史）
        algorithm_version: 当前 Review 算法版本（禁止混用旧版本）
        baseline_window: 历史窗口长度（默认 120）
        required_history_contract_version: 允许的 history contract 版本
        required_taxonomy_compatibility_key: 允许兼容的 taxonomy key
            （market=None 表示接受 canonical market series）
        required_source_history_run_id: 正式 canonical HistoryRun id
            （来自 readiness contract，禁止 latest arbitrary / pre-v2 fallback）

    Returns:
        (history_maps, prev_values, prev5d_values)
        - history_maps: {metric_code: {component_name: [raw_value 序列]}}
        - prev_values: {metric_code: value}（最近一交易日）
        - prev5d_values: {metric_code: value}（最近第5交易日）

    只读取严格早于目标日且算法版本完全一致的 observation。无历史时返回
    ``(None, None, None)``，由 metric engine 标记 insufficient_history。
    """
    from app.services.review_metric_observation_service import load_metric_history

    result = await load_metric_history(
        session,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        trade_date=trade_date,
        algorithm_version=algorithm_version,
        baseline_window=baseline_window,
        required_history_contract_version=required_history_contract_version,
        required_taxonomy_compatibility_key=required_taxonomy_compatibility_key,
        required_source_history_run_id=required_source_history_run_id,
    )
    if len(result) == 4:
        return result
    # Backward compat: 3-tuple → add None date_indexed
    return result[0], result[1], result[2], None


async def _resolve_canonical_history_source(
    session: AsyncSession,
    required_trade_date: date | None = None,
) -> tuple[uuid.UUID | None, str]:
    """[Phase4C 2026-08-09] 解析正式 canonical HistoryRun（P0-B）。

    通过 canonical history readiness contract 从运行数据解析 source run id，
    **禁止硬编码生产 run id**（如 be56dcd2...）。contract 版本使用算法常量
    ``HISTORY_CONTRACT_VERSION``（版本字符串，非具体 run id）。

    [HISTORY-CURRENT-DATE-LIFECYCLE-01 §9/§10] ``required_trade_date`` 非 None 时，
    候选 run 还必须满足 TARGET_DATE_ELIGIBLE_SET == TARGET_DATE_STATE_SET，
    避免 latest state 停留在旧交易日的 run 对当日 Review 误判 ready。

    Returns:
        (source_history_run_id, history_contract_version)
        - 若找不到就绪的 canonical run：source_history_run_id=None，
          contract 版本仍为当前算法版本（load_metric_history 会据此返回 unavailable）。
    """
    contract_version = HISTORY_CONTRACT_VERSION
    # 找到 scope=all_a_share 的候选 run（contract 由 readiness contract 在 Python 侧解析
    # metadata_json 校验，避免依赖 Text 列的 JSON 操作符），再经 readiness contract 验证
    candidate_stmt = (
        select(FirstPyramidHistoryRun)
        .where(FirstPyramidHistoryRun.scope == "all_a_share")
        .order_by(FirstPyramidHistoryRun.created_at.desc())
    )
    candidates = list((await session.execute(candidate_stmt)).scalars())
    for run in candidates:
        result = await validate_canonical_history_run_readiness(
            session, run.id, contract_version,
            required_trade_date=required_trade_date,
        )
        if result.get("status") == "ok":
            return run.id, contract_version
    return None, contract_version


async def _bind_or_reuse_canonical_history_source(
    session: AsyncSession,
    run: MarketReviewRun,
) -> tuple[uuid.UUID | None, str]:
    """[Phase4C 2026-08-09 P0-B] 解析并绑定 canonical history source（生命周期不漂移）。

    Lifecycle（B1~B3）：
    - run.metadata_json["canonical_history_source_run_id"] 已存在
      → 直接复用（resume / retry 绝不重新解析 latest ready run，防止 A→B 漂移）。
    - 无绑定 → 解析当时最新合法 canonical source，并立即写入 metadata_json
      （不迁移 schema；字段命名遵循项目 metadata 约定）。
    - bound source 不存在 / contract mismatch / 不再 canonical-compatible
      → fail closed（raise），不自动切换到新 source。

    Returns:
        (source_history_run_id, history_contract_version)
    """
    metadata = run.metadata_json or {}
    bound_run_id = metadata.get("canonical_history_source_run_id")
    bound_contract = metadata.get("canonical_history_contract_version")

    # [REVIEW-CURRENT-FACT-SOURCE-DRIFT FIX] 形式 Review 的 canonical history source
    # 是**历史 baseline 仅**，只需提供 trade_date < T 的 previous First Pyramid state，
    # **不要求**该 source 覆盖目标日 T 的 daily state（历史 state <= T-1 即接受）。
    # 因此不传 required_trade_date（默认值 None）：保留 contract/source/缺失的
    # fail-closed，但去掉 target-date 生命周期约束。其他 history/bootstrap 流程
    # 仍可能通过 validate_canonical_history_run_readiness(required_trade_date=...)
    # 显式要求目标日覆盖（该函数本身保留不变）。
    required_trade_date = None

    if bound_run_id is not None:
        # resume / retry：校验 bound source 仍然合法，否则 fail closed
        bound_uuid = uuid.UUID(str(bound_run_id))
        contract_version = bound_contract or HISTORY_CONTRACT_VERSION
        result = await validate_canonical_history_run_readiness(
            session, bound_uuid, contract_version,
            required_trade_date=required_trade_date,
        )
        if result.get("status") != "ok":
            raise ReviewOrchestratorError(
                f"run={run.id} 已绑定 canonical history source="
                f"{bound_uuid}（contract={contract_version}），但当前不再 "
                f"canonical-compatible（{result.get('reason')}）；禁止自动切换 "
                f"source，fail closed。",
            )
        return bound_uuid, contract_version

    # 新 run：解析当时最新合法 canonical source 并立即绑定到 metadata
    # [REVIEW-CURRENT-FACT-SOURCE-DRIFT FIX] 形式 Review 不要求目标日 history state
    # 覆盖，故传 required_trade_date=None（历史 baseline 即可）。
    source_run_id, contract_version = await _resolve_canonical_history_source(
        session, required_trade_date=required_trade_date,
    )
    if source_run_id is None:
        logger.warning(
            "[ReviewOrchestrator] 未找到就绪的 canonical HistoryRun；"
            "history lineage 将回退为 contract 级 unavailable（不阻塞 market scope）",
        )
    # [REVIEW-FACT-PARITY-02 §11] JSONB 必须整体重新赋值为**新 dict**：
    # `metadata` 与 run.metadata_json 是同一对象时，就地改键不会被 SQLAlchemy
    # 判定为脏数据，flush 静默丢弃绑定 → 下次 resume 会重新解析 latest，
    # 造成 lineage drift（实测 run=653b26c4 绑定未落库）。
    run.metadata_json = {
        **metadata,
        "canonical_history_source_run_id": (
            str(source_run_id) if source_run_id is not None else None
        ),
        "canonical_history_contract_version": contract_version,
    }
    await session.flush()
    return source_run_id, contract_version


async def _fetch_pyramid_v2_for_scope(
    session: AsyncSession,
    scope: ScopeDefinition,
) -> dict[str, Any] | None:
    """[P0-7 2026-07-30] 从 board_analysis_snapshot 读取 pyramid_v2 维度数据。

    industry/concept scope 通过 scope.source_board_snapshot_id 关联到
    BoardAnalysisSnapshot，从中读取 payload["pyramid_v2"]。
    其他 scope（market/major_index/style/instrument）无 board_analysis，
    返回 None（D 族筛选器评估时 context 无 pyramid_v2，自动跳过）。

    Args:
        session: 异步 DB 会话
        scope: 范围定义（需含 source_board_snapshot_id）

    Returns:
        pyramid_v2 payload dict 或 None
    """
    if scope.source_board_snapshot_id is None:
        return None
    stmt = (
        select(BoardAnalysisSnapshot.payload)
        .where(BoardAnalysisSnapshot.id == scope.source_board_snapshot_id)
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    payload = row[0] if row[0] is not None else {}
    if not isinstance(payload, dict):
        return None
    pv2 = payload.get("pyramid_v2")
    return pv2 if isinstance(pv2, dict) else None


async def _compute_scope_metrics_phase(
    session: AsyncSession,
    run: MarketReviewRun,
    scope: ScopeDefinition,
    *,
    required_history_contract_version: str | None = None,
    required_source_history_run_id: uuid.UUID | None = None,
    day_fact_map: dict[uuid.UUID, dict[str, Any]] | None = None,
    prepared_observations: dict[str, Any] | None = None,
) -> tuple[MarketReviewScopeSnapshot | None, dict[str, dict[str, list[float]]] | None]:
    """Compute and persist raw/normalized metrics for one scope.

    Returns (snapshot, history_maps). date_indexed is passed through history_maps
    as a sentinel key '_date_indexed' for CR-01 date-aligned computations.

    [Phase4C 2026-08-09] 透传 canonical history lineage 过滤条件（P0-B）。

    [REVIEW-FACT-PARITY-02 §10] ``day_fact_map`` 由 ``compute_run`` 一次性加载并
    传入；本函数只按 instrument_id 取**引用**组装 flat_list，不再调用
    ``fetch_member_flat_list``，也不做 deepcopy / JSON roundtrip。
    仅当 ``day_fact_map is None``（非正式路径，如独立调试）才回退旧 loader。
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
    #
    # [REVIEW-OPTIONAL-SCOPE-TERMINALIZATION-01 2026-08-10]
    # optional scope（major_index / style / industry_l1）的 PIT membership 合法
    # 不可用不是执行失败：publication contract 已把它定义为 scope-level diagnostic。
    # 此处是唯一 ownership point——RUNNING item 建立后必须被确定性终态化为
    # SKIPPED 并回填 completed_at，否则异常逃逸到外层会留下 RUNNING 残留，
    # 进而永久阻塞 evaluate_publish_gate（running 是硬 blocker）。
    #
    # 在此处（而非 compute_run / resume_run 各自）处理，使两条路径共享同一行为。
    # 只捕获 typed OptionalScopeUnavailableError；其他 ScopeSnapshotError
    # （scope_type mismatch / 非法 UUID / board_not_found）和未预期异常继续传播为 failure。
    try:
        instrument_ids, _resolved_name = await resolve_scope_members(
            session, scope.scope_type, scope.scope_key, trade_date=run.trade_date,
        )
    except OptionalScopeUnavailableError as exc:
        logger.info(
            "[ReviewOrchestrator] optional scope 不可用，终态化为 skipped: "
            "%s/%s reason=%s population_status=%s",
            exc.scope_type,
            exc.scope_key,
            exc.reason,
            exc.population_status,
        )
        await _upsert_run_item(
            session,
            run_id=run.id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            phase=PHASE_METRICS,
            status=ITEM_SKIPPED,
            last_error=str(exc)[:500],
            completed_at=datetime.now(UTC),
        )
        # [FIX 5] OptionalScopeUnavailableError 合法不可用时返回 (None, None) 二元组，
        # 与正常分支 (_compute_scope_metrics 返回 (snapshot, history_maps)) 契约一致，
        # 关闭 "cannot unpack non-iterable NoneType" 生产错误。
        return None, None
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
        # [FIX 5] 空范围同样返回 (None, None) 二元组，保持 unpack 契约。
        return None, None

    # 使用调用方传入的 scope_name（resolve_scope_members 可能返回 generic name）
    # scope.scope_name 已在 ScopeDefinition 中设置，compute_scope_metrics 直接读取

    # 拉取成员 first_pyramid_flat
    if day_fact_map is not None:
        # [REVIEW-FACT-PARITY-02 §10] 只按 membership 从已加载 day fact map 取引用。
        # 共享引用是有意的（多 scope 重叠成员复用同一 fact 对象）；下游只读，
        # 禁止任何 in-place mutation。
        flat_list = [
            fact
            for fact in (day_fact_map.get(iid) for iid in instrument_ids)
            if fact is not None
        ]
    else:
        flat_list = await fetch_member_flat_list(
            session,
            instrument_ids,
            run.source_core_run_id,
            trade_date=run.trade_date,
        )

    # [P0 2026-07-30] 构建 history_maps/prev_values/prev5d_values
    # PRD §7.1：历史基线默认 120 日、最低 60 日；先保存 rawValue，
    # 达到 60 个观测后再计算 normalizedValue、P/Q/U/C/V、1d/5d 和分位。
    # 禁止用当前成员回填全部历史或使用未来数据。
    # 实现说明：
    # - 从 market_review_scope_snapshots 读取历史已发布同 scope 的 raw_value 序列
    # - 若无历史 review 数据（首次运行），history_maps=None，component status=insufficient_history
    # - prev_values/prev5d_values 从最近 1/5 个交易日的 scope_snapshot 读取
    history_maps, prev_values, prev5d_values, date_indexed = await _build_scope_history(
        session,
        scope=scope,
        trade_date=run.trade_date,
        algorithm_version=run.algorithm_version,
        baseline_window=run.baseline_window,
        required_history_contract_version=required_history_contract_version,
        required_taxonomy_compatibility_key=scope.taxonomy_compatibility_key,
        required_source_history_run_id=required_source_history_run_id,
    )

    try:
        # [P0-7 2026-07-30] 获取 pyramid_v2 维度数据（PRD §24 D 族筛选器）
        # industry/concept scope 通过 source_board_snapshot_id 关联到
        # board_analysis_snapshot，从中读取 payload["pyramid_v2"]
        pyramid_v2 = await _fetch_pyramid_v2_for_scope(session, scope)

        snapshot = await compute_scope_metrics(
            session,
            review_run_id=run.id,
            trade_date=run.trade_date,
            scope=scope,
            flat_list=flat_list,
            algorithm_version=run.algorithm_version,
            eligible_count=len(instrument_ids),
            history_maps=history_maps,
            prev_values=prev_values,
            prev5d_values=prev5d_values,
            pyramid_v2_payload=pyramid_v2,
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

    # Embed date_indexed into history_maps for CR-01 date-aligned computations
    if history_maps is not None and date_indexed is not None:
        history_maps["_date_indexed"] = date_indexed  # type: ignore[assignment]

    # === 规范 Scope Observation 事实层双写（PRD §7.2-§7.17 v2.3）===
    # [2026-08-13 双轨并存] 不替换 legacy P/Q/U/C/V：Discovery/筛选器/信号管线
    # 仍消费 MarketReviewScopeSnapshot（本轮不动）。此处仅把七段 Canonical
    # Observation 写入 ReviewScopeObservationFact，供 EvidenceDrawer /
    # scope_evidence_service 消费。双写失败不得影响 legacy metrics/signal，
    # 故隔离在 try/except 内，仅记录 diagnostic。
    # 仅 industry_l1/l2/l3 + concept（activated scope）会实际写入（market/
    # major_index/style 自动跳过）。
    # [REVIEW-EXECUTION-PATH-CONSOLIDATION] ``prepared_observations`` 由
    # compute_run / resume_run 一次 batch prepare 后传入，本函数只按 scope_key
    # SELECT 对应 PreparedScope，不再逐 scope 调用任何 single-scope preparation。
    try:
        await _persist_canonical_scope_observation(
            session, run, scope, prepared_observations=prepared_observations
        )
    except Exception as exc:  # noqa: BLE001 - 隔离双写失败，不破坏 Discovery
        logger.warning(
            "[ReviewOrchestrator] 规范事实层双写失败（不影响 legacy signal）: "
            "%s/%s trade_date=%s err=%s",
            scope.scope_type, scope.scope_key, run.trade_date, exc,
        )

    return snapshot, history_maps


async def _persist_canonical_scope_observation(
    session: AsyncSession,
    run: MarketReviewRun,
    scope: ScopeDefinition,
    *,
    prepared_observations: dict[str, Any] | None = None,
) -> None:
    """双写规范 Scope Observation 七段事实到 ReviewScopeObservationFact。

    仅对 activated scope（industry_l1/l2/l3 + concept）生效；market/major_index/
    style 由 batch prepare 返回 unavailable 自动跳过（不抛错、不写表）。

    [REVIEW-EXECUTION-PATH-CONSOLIDATION] 本函数不拥有任何 membership 解析 /
    SQL / fact preparation：``prepared_observations`` 由 compute_run / resume_run
    通过唯一 owner ``prepare_current_scope_observations_batch`` 一次 batch prepare，
    此处只按 scope_key SELECT 对应 PreparedScope 后计算并落库。missing key 表示
    batch prepare 未包含该 scope（如 batch prepare 失败被隔离），直接跳过。

    compute_scope_observation 输出经 check_observation_invariants 校验，非法
    payload 由 save_scope_observation_fact 的 validate_scope_observation_payload
    在落库前 fail-fast 拒绝（延续 Round 1C Blocker #1/#2/#3）。

    A 级机制/资格/事件标签概念（融资融券/沪深股通/专精特新/次新股/ST 等）按
    CONCEPT_OBSERVATION_PERSISTENCE_EXCLUDE_NAMES 直接排除，不写 ReviewScopeObservationFact。
    """
    if is_scope_observation_persistence_excluded(
        scope_type=scope.scope_type,
        scope_name=scope.scope_name,
    ):
        logger.info(
            "[ReviewOrchestrator] 规范事实层跳过 A 级机制/资格/事件标签概念: "
            "%s/%s scope_name=%s",
            scope.scope_type, scope.scope_key, scope.scope_name,
        )
        return
    prep = (
        prepared_observations.get(scope.scope_key)
        if prepared_observations is not None
        else None
    )
    if prep is None:
        logger.info(
            "[ReviewOrchestrator] 规范事实层跳过（batch prepare 未包含该 scope）: "
            "%s/%s",
            scope.scope_type, scope.scope_key,
        )
        return
    if prep.pit_status_t == "unavailable" or not prep.members:
        logger.info(
            "[ReviewOrchestrator] 规范事实层跳过 unavailable/空范围: "
            "%s/%s pit_status_t=%s member_count=%d",
            scope.scope_type, scope.scope_key, prep.pit_status_t, len(prep.members),
        )
        return

    # 成员数过小（<=10）的 concept 样本无统计意义，A 步不持久化（prepare 后拿真实 count）。
    if is_scope_observation_persistence_excluded(
        scope_type=scope.scope_type,
        scope_name=scope.scope_name,
        member_count=len(prep.members),
    ):
        logger.info(
            "[ReviewOrchestrator] 规范事实层跳过成员过小 concept: "
            "%s/%s scope_name=%s member_count=%d",
            scope.scope_type, scope.scope_key, scope.scope_name, len(prep.members),
        )
        return

    # CORRECTION: 规范事实层双写必须在 nested transaction / savepoint 内执行。
    # 若 canonical DB flush 失败，仅回滚该 savepoint，外层 legacy transaction
    # （metrics/signal）仍可继续提交，互不污染。
    async with session.begin_nested():
        observation = compute_scope_observation(
            scope_type=prep.scope_type,
            scope_key=prep.scope_key,
            trade_date=prep.trade_date,
            pit_member_ids=prep.pit_member_ids,
            pit_member_ids_t1=prep.pit_member_ids_t1,
            members=prep.members,
            events=prep.events,
            t1_membership_available=prep.t1_membership_available,
            event_coverage_member_ids=prep.event_coverage_member_ids,
        )
        checks = check_observation_invariants(observation)
        failed = [c for c in checks if not c["ok"]]
        if failed:
            raise ValueError(
                f"scope observation invariant failed: {failed!r}"
            )
        await save_scope_observation_fact(
            session, prep, observation, algorithm_version=run.algorithm_version,
        )


def _build_history_extras(
    snapshot: MarketReviewScopeSnapshot,
    history_maps: dict[str, dict[str, list[float]]] | None,
) -> dict[str, float | int]:
    """[CR-01] 从历史序列构建 filter engine 所需的 history_extras。

    CR01-D true date alignment:
    - history_maps["_date_indexed"] = {trade_date: {metric_code: {component_name: raw_value}}}
    - P-Q 仅在 P 和 Q 同时存在的 trade_date 计算
    - structure_breakdown 比较当前 snapshot vs 最近 canonical trade_date
    """
    extras: dict[str, float | int] = {}

    if history_maps is None:
        return extras

    date_indexed = history_maps.get("_date_indexed")

    # CR01-A: Q/U/V delta1d historical percentile — true date-aligned
    for metric_code, key in [("Q", "_q_delta1d_history_pct"),
                               ("U", "_u_delta1d_history_pct"),
                               ("V", "_v_delta1d_history_pct")]:
        payload = getattr(snapshot, f"{metric_code.lower()}_payload") or {}
        delta = payload.get("delta1d")
        if not isinstance(delta, (int, float)):
            continue
        # Use date_indexed for true 1D deltas (only adjacent canonical dates with data)
        delta_series = _extract_date_aligned_delta_series(date_indexed, metric_code)
        if delta_series:
            extras[key] = _percentile_rank(delta, delta_series)

    # CR01-B: P-Q date-aligned — only on trade_dates where both P and Q exist
    if isinstance(date_indexed, dict):
        pq_diffs = []
        for td in sorted(date_indexed.keys()):
            entry = date_indexed[td]
            p_entry = entry.get("P", {})
            q_entry = entry.get("Q", {})
            p_val = p_entry.get("_metric_value")
            q_val = q_entry.get("_metric_value")
            if p_val is not None and q_val is not None:
                pq_diffs.append(p_val - q_val)
        if pq_diffs:
            p_payload = snapshot.p_payload or {}
            q_payload = snapshot.q_payload or {}
            p_val = p_payload.get("value")
            q_val = q_payload.get("value")
            if isinstance(p_val, (int, float)) and isinstance(q_val, (int, float)):
                extras["_pq_diff_history_pct"] = _percentile_rank(p_val - q_val, pq_diffs)

    # CR01-C: structure_breakdown — current snapshot vs most recent canonical trade_date
    extras["_structure_breakdown_not_rising"] = (
        _check_structure_breakdown_vs_previous(snapshot, date_indexed)
    )

    # CR01-D: C context — use existing historyPercentile120d
    c_payload = snapshot.c_payload or {}
    c_delta1d = c_payload.get("delta1d")
    extras["_c_rising"] = 1 if isinstance(c_delta1d, (int, float)) and c_delta1d > 0 else 0
    c_history_pct = c_payload.get("historyPercentile120d")
    extras["_c_high_anomaly"] = (
        1 if isinstance(c_history_pct, (int, float)) and c_history_pct >= 80 else 0
    )

    return extras


def _get_metric_value_series(
    history_maps: dict[str, dict[str, list[float]]],
    metric_code: str,
) -> list[float] | None:
    components = history_maps.get(metric_code)
    if not components:
        return None
    return components.get("_metric_value")


def _extract_delta_series_from_metric_value(
    history_maps: dict[str, dict[str, list[float]]],
    metric_code: str,
) -> list[float] | None:
    values = _get_metric_value_series(history_maps, metric_code)
    if values is None or len(values) < 2:
        return None
    return [values[i] - values[i - 1] for i in range(1, len(values))]


def _extract_date_aligned_delta_series(
    date_indexed: dict | None,
    metric_code: str,
) -> list[float] | None:
    """从 date_indexed 计算真正的日期对齐 1D delta 序列。

    只在相邻 canonical trade_dates 且两天都有 _metric_value 时才计算。
    """
    if not isinstance(date_indexed, dict):
        return None
    dates = sorted(date_indexed.keys())
    deltas = []
    for i in range(1, len(dates)):
        prev_entry = date_indexed.get(dates[i - 1], {})
        curr_entry = date_indexed.get(dates[i], {})
        prev_val = prev_entry.get(metric_code, {}).get("_metric_value")
        curr_val = curr_entry.get(metric_code, {}).get("_metric_value")
        if prev_val is not None and curr_val is not None:
            deltas.append(curr_val - prev_val)
    return deltas if deltas else None


def _percentile_rank(value: float, series: list[float]) -> float:
    if not series:
        return 0.0
    below = sum(1 for v in series if v < value)
    return round(below / len(series) * 100, 1)


def _check_structure_breakdown_vs_previous(
    snapshot: MarketReviewScopeSnapshot,
    date_indexed: dict | None,
) -> int:
    """CR01-C: 比较当前 snapshot structure_breakdown_diffusion vs 最近 canonical trade_date。

    使用 date_indexed 获取最近 trade_date 的 Q.structure_breakdown_diffusion 值。
    """
    # Get current value from snapshot Q payload components
    q_payload = snapshot.q_payload or {}
    components = q_payload.get("components", [])
    current = None
    if isinstance(components, list):
        for c in components:
            if isinstance(c, dict) and c.get("name") == "structure_breakdown_diffusion":
                current = c.get("rawValue")
                if isinstance(current, (int, float)):
                    current = float(current)
                break

    if current is None:
        return 0

    # Get previous value from date_indexed
    if isinstance(date_indexed, dict) and date_indexed:
        most_recent_date = max(date_indexed.keys())
        entry = date_indexed[most_recent_date]
        q_entry = entry.get("Q", {})
        prev = q_entry.get("structure_breakdown_diffusion")
        if isinstance(prev, (int, float)):
            return 1 if current <= prev else 0

    return 0


async def _compute_scope_signal_pipeline(
    session: AsyncSession,
    run: MarketReviewRun,
    scope: ScopeDefinition,
    snapshot: MarketReviewScopeSnapshot,
    *,
    day_fact_map: dict[uuid.UUID, dict[str, Any]] | None = None,
    history_maps: dict[str, dict[str, list[float]]] | None = None,
) -> int:
    """Evaluate signals and attribution after the cross-section pass is complete.

    [REVIEW-FACT-PARITY-02 §10] ``day_fact_map`` 透传给 attribution，
    使 signal × child_scope 归因不再重复加载当日 facts。

    [CR-01] ``history_maps`` 用于构建 history_extras 注入 filter evaluation。
    """

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
        # [CR-01] 从历史序列构建 filter 所需的 history_extras
        history_extras = _build_history_extras(snapshot, history_maps)

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
            history_extras=history_extras,
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
                source_board_run_id=run.source_board_run_id,
                day_fact_map=day_fact_map,
            )
            await compute_signal_instruments(
                session,
                signal,
                parent_metrics=parent_metrics,
                parent_ready_count=parent_ready_count,
                source_core_run_id=run.source_core_run_id,
                day_fact_map=day_fact_map,
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


async def _compute_scope_pipeline(
    session: AsyncSession,
    run: MarketReviewRun,
    scope: ScopeDefinition,
    *,
    day_fact_cache: dict[str, dict[uuid.UUID, dict[str, Any]]] | None = None,
    prepared_observations: dict[str, Any] | None = None,
) -> int:
    """Resume-compatible single-scope pipeline using the same two ordered phases.

    [Phase4C 2026-08-09 P0-B] resume 必须复用 run 已绑定的 canonical history source，
    绝不重新解析 latest ready run（防止 A→B lineage 漂移）。

    [REVIEW-FACT-PARITY-02 §10] ``day_fact_cache`` 是 ``resume_run`` 传入的跨 scope
    可变缓存：当日 facts 只在第一个 scope 处加载一次，后续 scope 直接复用同一份
    内存 map（共享引用，无 copy）。为 None 时（独立调用）不启用 load-once。
    """
    canonical_source_run_id, canonical_contract_version = (
        await _bind_or_reuse_canonical_history_source(session, run)
    )
    # [REVIEW-FACT-PARITY-02 §10] resume load-once：facts 只在首个 scope 加载一次。
    day_fact_map: dict[uuid.UUID, dict[str, Any]] | None = None
    if day_fact_cache is not None:
        if "facts" not in day_fact_cache:
            day_fact_cache["facts"] = await load_day_fact_maps(
                session,
                trade_date=run.trade_date,
                source_core_run_id=run.source_core_run_id,
                required_source_history_run_id=canonical_source_run_id,
                required_history_contract_version=canonical_contract_version,
            )
            logger.info(
                "[ReviewOrchestrator] resume load_day_fact_maps once: "
                "trade_date=%s facts=%d source_history_run_id=%s",
                run.trade_date,
                len(day_fact_cache["facts"]),
                canonical_source_run_id,
            )
        day_fact_map = day_fact_cache["facts"]

    # 从正式 scope 解析补全 taxonomy_compatibility_key（resume 重建的 ScopeDefinition 缺此字段）
    resolved = await _resolve_all_discovery_scopes(session, run)
    full_scope = next(
        (s for s in resolved if s.scope_type == scope.scope_type and s.scope_key == scope.scope_key),
        scope,
    )
    snapshot, history_maps = await _compute_scope_metrics_phase(
        session,
        run,
        full_scope,
        required_history_contract_version=canonical_contract_version,
        required_source_history_run_id=canonical_source_run_id,
        day_fact_map=day_fact_map,
        prepared_observations=prepared_observations,
    )
    if snapshot is None:
        return 0
    await apply_cross_section_percentiles(session, run.id)
    return await _compute_scope_signal_pipeline(
        session, run, full_scope, snapshot,
        day_fact_map=day_fact_map,
        history_maps=history_maps,
    )


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

    # [REVIEW-FACT-PARITY-02 §10] resume 同样 load-once：当日 facts 只在第一个
    # scope 处惰性加载一次，之后所有待重算 scope 共享同一份内存 map。
    # 用可变 cache 传入，避免在此重复调用 _bind_or_reuse_canonical_history_source
    # （binding 已在 _compute_scope_pipeline 内完成，重复调用会多做一次 DB 解析）。
    resume_fact_cache: dict[str, dict[uuid.UUID, dict[str, Any]]] = {}

    # [REVIEW-EXECUTION-PATH-CONSOLIDATION] 规范事实层同样只走唯一 batch owner：
    # 对待重算 scope 集合一次 batch prepare，_compute_scope_pipeline 按 scope_key
    # SELECT。失败隔离（不影响 legacy resume 主链），不是回退到旧路径。
    prepared_observations: dict[str, Any] | None = None
    try:
        redo_specs = [
            ScopeReplaySpec(
                scope_type=scope_type,
                scope_key=scope_key,
                scope_name=scope_key,
                member_ids=(),
            )
            for scope_type, scope_key in scopes_to_redo
            if not is_scope_observation_persistence_excluded(
                scope_type=scope_type,
                scope_name=scope_key,
            )
        ]
        if redo_specs:
            prepared_observations = await prepare_current_scope_observations_batch(
                session, run.trade_date, redo_specs
            )
    except Exception as exc:  # noqa: BLE001 - 隔离双写失败，不破坏 Discovery
        prepared_observations = None
        logger.warning(
            "[ReviewOrchestrator] resume 规范事实层 batch prepare 失败（不影响 legacy signal）: %s",
            exc,
        )

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
            await _compute_scope_pipeline(
                session, run, scope, day_fact_cache=resume_fact_cache,
                prepared_observations=prepared_observations,
            )
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
    # [AUD-06 2026-08-07] 与主路径同口径：真实有效样本覆盖率
    run.coverage_ratio = await _aggregate_run_data_coverage(session, run.id)

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
        # [AUD-06] coverage_ratio = 真实有效样本覆盖率；执行率单独表达
        "coverage_ratio": float(run.coverage_ratio),
        "scope_execution_rate": _scope_execution_rate(run),
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


async def _aggregate_run_data_coverage(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> Decimal:
    """[AUD-06 2026-08-07] 聚合 run 级真实有效样本覆盖率。

    语义：SUM(ready_count) / SUM(eligible_count)，跨该 run 全部 scope 快照。
    回答的是“底层数据有多少是有效的”，而非“有多少 scope 跑完了”。

    与 scope 执行成功率（succeeded_scope_count / expected_scope_count）严格区分：
    10/10 个 scope 全部执行成功，但每个 scope 只有 80/100 成员有效时，
    执行率为 1.0，而本函数返回 0.8。

    分母为 0（无 scope 快照，或全部 scope 成员数为 0）时返回 Decimal("0")，
    不得除零，也不得回落成执行率冒充数据覆盖。
    """
    stmt = select(
        func.coalesce(func.sum(MarketReviewScopeSnapshot.ready_count), 0),
        func.coalesce(func.sum(MarketReviewScopeSnapshot.eligible_count), 0),
    ).where(MarketReviewScopeSnapshot.review_run_id == run_id)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return Decimal("0")
    ready_total, eligible_total = row
    if not eligible_total or int(eligible_total) <= 0:
        return Decimal("0")
    return Decimal(str(int(ready_total) / int(eligible_total)))


def _scope_execution_rate(run: MarketReviewRun) -> float:
    """scope 执行成功率 = succeeded_scope_count / expected_scope_count。

    [AUD-06] run.coverage_ratio 已改为真实数据覆盖率，执行率不再由该列承载，
    改由本派生值在返回结构中显式表达，避免两种语义互相冒充。
    """
    if not run.expected_scope_count or run.expected_scope_count <= 0:
        return 0.0
    return run.succeeded_scope_count / run.expected_scope_count


# =============================================================================
# Publish / Status
# =============================================================================


async def publish_run(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    force: bool = False,
    operator: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[FactorPublication | None, list[str]]:
    """发布 review run（委托给 review_publication_service）。

    [P0 安全收口 2026-08-01] force=True 只生成 provisional 标记，
    不写正式 pointer，publication 返回 None。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        run: MarketReviewRun ORM 对象
        force: True 时生成 provisional（不写正式 pointer，仅 admin 调试）
        operator: 操作者标识（审计用）
        idempotency_key: 调用方幂等键（审计用）

    Returns:
        (FactorPublication | None, blockers)
        正式发布返回 pointer；force（provisional）路径 publication=None，
        blockers 为门禁评估结果（仅记录，不阻断）。

    Raises:
        ReviewPublishBlockError: force=False 时门禁失败
    """
    blockers: list[str] = []
    if not force:
        publishable, blockers = await evaluate_publish_gate(session, run)
        if not publishable:
            raise ReviewPublishBlockError(blockers)

    publication = await publish_review(
        session, run,
        force=force, operator=operator, idempotency_key=idempotency_key,
    )
    if force:
        _publishable, blockers = await evaluate_publish_gate(session, run)
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
        # [AUD-06] coverage_ratio = 真实有效样本覆盖率；执行率单独表达
        "coverage_ratio": float(run.coverage_ratio) if run.coverage_ratio else None,
        "scope_execution_rate": _scope_execution_rate(run),
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
