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

# [REVIEW-LEGACY-BUSINESS-PATH-RETIREMENT] 规范 Scope Observation 事实层
# （PRD §7.2-§7.17 v2.3）是 orchestrator 的**唯一强制主链**：compute_run /
# resume_run 只执行 canonical Scope Observation 计算与落库。legacy
# P/Q/U/C/V→Filter→Signal→Attribution pipeline（owner A，已被 canonical
# 覆盖）已物理移除出主链，不再作为 mandatory path。
#
# [REVIEW-EXECUTION-PATH-CONSOLIDATION] 规范事实层唯一 preparation owner =
# ``prepare_current_scope_observations_batch``（一次解析 memberships + union facts +
# slice）；orchestrator 不再逐 scope 调用单 scope 入口。
from app.domain.review.analysis.internal_structure import compute_internal_structure
from app.domain.review.analysis.leadership_migration import (
    LeadershipSnapshot,
    compute_leadership_migration,
    serialize_leadership_migration,
)
from app.domain.review.analysis.member_attribution import compute_member_attribution
from app.domain.review.canonical_composition import (
    compose_canonical_review_scope,
    structured_unavailable_layer,
)
from app.domain.review.filter_definitions import REVIEW_FILTER_VERSION
from app.domain.review.review_capability import (
    resolve_scope_capability,
)
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
)
from app.services.board_membership_service import list_universe_definitions_at
from app.services.first_pyramid_service import HISTORY_CONTRACT_VERSION
from app.services.observation_prep import check_observation_invariants
from app.services.review_history_readiness_service import (
    validate_canonical_history_run_readiness,
)
from app.services.review_leadership_service import compute_scope_leadership_batch
from app.services.review_observation_persistence_service import (
    is_scope_observation_persistence_excluded,
    save_scope_composition_snapshot,
    save_scope_observation_fact,
)
from app.services.review_observation_prep_service import (
    ScopeReplaySpec,
    list_recent_trading_days,
    prepare_current_scope_observations_batch,
)
from app.services.review_publication_service import (
    REVIEW_PUBLISH_MIN_COVERAGE,
    ReviewPublishBlockError,
    evaluate_publish_gate,
    publish_review,
)
from app.services.review_scope_dynamics_service import (
    compute_current_static_scope_dynamics_batch,
)
from app.services.review_scope_service import (
    LEVEL1_SCOPE_TYPES,
    ScopeDefinition,
    validate_review_lineage_guard,
)

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

    # 每个 run 只执行一次轻量 lineage guard；member fact 由后续 batch owner 物化。
    await validate_review_lineage_guard(
        session,
        trade_date=run.trade_date,
        source_core_run_id=run.source_core_run_id,
        required_source_history_run_id=canonical_source_run_id,
        required_history_contract_version=canonical_contract_version,
        current_source="stock_core",
    )
    logger.info(
        "[ReviewOrchestrator] lineage guard once: trade_date=%s "
        "source_history_run_id=%s",
        run.trade_date,
        canonical_source_run_id,
    )

    # === 规范 Scope Observation 事实层 batch prepare（PRD §7.2-§7.17 v2.3）===
    # [REVIEW-LEGACY-BUSINESS-PATH-RETIREMENT] 唯一 preparation owner =
    # prepare_current_scope_observations_batch：一次解析 PIT(T)/PIT(T-1) memberships、
    # 一次加载 union member facts、slice 成各 Scope PreparedScope。scope 循环只从
    # 该 map SELECT 结果。此 batch prepare 是 orchestrator 的**强制主链前置步骤**，
    # 失败直接 fail closed，不再被隔离为可回退到 legacy 的 sidecar。
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
    prepared_observations: dict[str, Any] = {}
    if eligible_specs:
        prepared_observations = await prepare_current_scope_observations_batch(
            session, run.trade_date, eligible_specs
        )
        logger.info(
            "[ReviewOrchestrator] 规范事实层 batch prepare: scopes=%d prepared=%d",
            len(eligible_specs), len(prepared_observations),
        )

    succeeded = 0
    failed = 0

    # 2. [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 每 scope 只调用唯一 canonical
    # composition owner（_compute_canonical_composition_phase）：activated 家族
    # （industry_l1/l2/l3/concept）计算并落库规范 Scope Observation + 六键 canonical
    # composition；非激活家族（market/major_index/style）按 capability 合法跳过
    # （结构化 reason），绝不回退 legacy P/Q/U/C/V。legacy _compute_scope_metrics_phase
    # 及其 P/Q/U/C/V snapshot 写入已物理删除，不再是任何 runtime owner。
    # [REVIEW-BACKEND-FINAL-CLOSURE] 循环前按家族批量计算 Historical Dynamics
    # （唯一 batch owner），产出 scope_key→dynamics map 注入 composition。
    dynamics_map = await _compute_family_dynamics_maps(session, run, scopes)
    # [REVIEW-BACKEND-FINAL-CLOSURE Phase 5.5] 真实 T-1 → T Leadership migration：
    # family batch 一次加载 [T-1, T] member facts 并建立真实 previous snapshot，
    # 不再用 unavailable synthetic previous 包装 migration。
    leadership_map: dict[str, Any] = {}
    if eligible_specs:
        leadership_map = await compute_scope_leadership_batch(
            session, run.trade_date, eligible_specs
        )
        logger.info(
            "[ReviewOrchestrator] leadership T-1→T batch: scopes=%d produced=%d",
            len(eligible_specs), len(leadership_map),
        )
    for scope in scopes:
        try:
            await _compute_canonical_composition_phase(
                session,
                run,
                scope,
                prepared_observations=prepared_observations,
                dynamics_map=dynamics_map,
                leadership_map=leadership_map,
            )
            succeeded += 1
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

    # 3. 更新 run 状态。retirement 后 signal/tracking pipeline 不再计算；run 状态名
    # ``signals_ready`` 保留为 PUBLICATION_CONTRACT 的历史兼容状态 token：publication
    # gate（review_publication_service.evaluate_publish_gate）仍以 signals_ready /
    # published 作为可发布前提。Canonical readiness 已通过 run.metadata_json
    # ``canonical_composition_readiness`` 表达并消费（publication 已切 canonical）。
    # 业务语义：signal pipeline 已退出主链，run 成功终态现在只反映 canonical
    # scope observation +（owner C 兼容）snapshot 就绪。
    run.succeeded_scope_count = succeeded
    run.failed_scope_count = failed
    run.completed_at = datetime.now(UTC)
    # [AUD-06 2026-08-07] coverage_ratio 表达真实有效样本覆盖率（数据口径），
    # 不再是 scope 执行成功率；执行率由 succeeded/expected 两列独立表达。
    run.coverage_ratio = await _aggregate_run_data_coverage(session, run)

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
        "signal_count": 0,
        # [REVIEW-BACKEND-FINAL-CLOSURE] tracking pipeline 已退休（review_tracking_service
        # 已删除）；tracking_evaluations 固定为 0，不再触发逐日追踪评估。
        "tracking_evaluations": 0,
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


# [REVIEW-BACKEND-FINAL-CLOSURE] Historical Dynamics trading axis + family-batch
# wiring。禁止逐 scope 重建 120 天历史：必须复用唯一 batch owner
# compute_current_static_scope_dynamics_batch，其内部已 union members / VEC-1 /
# load once。本 helper 在 compute_run / resume_run 的 scope 循环前调用一次，产出
# scope_key → dynamics result map 注入 _compute_canonical_composition_phase。
DYNAMICS_AXIS_PRE_T_COUNT = 120  # 冻结合同：T 之前最近 120 个 observation slots


async def _build_dynamics_trading_axis(
    session: AsyncSession,
    asof_date: date,
) -> list[date]:
    """Build the canonical Historical Dynamics trading axis (frozen contract).

    axis = [T-120 ... T-1] (pre-T baseline, strictly ascending) + [T]
    (current value anchor, == analysis_asof_date). T is NEVER part of the
    baseline denominator (historical_position.compute_historical_position only
    consults strictly pre-T values). The batch owner does NOT query a calendar,
    so the caller must supply a complete, strictly-ascending axis here.
    """
    pre_t = await list_recent_trading_days(
        session, end_date=asof_date, n=DYNAMICS_AXIS_PRE_T_COUNT,
    )
    pre_t_asc = sorted(pre_t)  # list_recent_trading_days returns desc; ascending
    if pre_t_asc and pre_t_asc[-1] >= asof_date:
        # 防御：若 end_date=asof_date 返回含 asof 的点，截断到严格 pre-T
        pre_t_asc = [d for d in pre_t_asc if d < asof_date]
    axis = pre_t_asc + [asof_date]
    if len(axis) <= 1:
        # 历史数据不足：axis 退化为 [asof]（batch owner 会判 insufficient_history）
        return [asof_date]
    return axis


async def _compute_family_dynamics_maps(
    session: AsyncSession,
    run: MarketReviewRun,
    scopes: list[ScopeDefinition],
) -> dict[str, Any]:
    """Compute Historical Dynamics for all activated scopes, grouped by family.

    Returns a single ``scope_key -> dynamics_result`` map consumed by
    ``_compute_canonical_composition_phase``.  Only ONE batch call per
    scope_type (family) is issued; never a per-scope 120-day reconstruction.
    """
    axis = await _build_dynamics_trading_axis(session, run.trade_date)
    by_family: dict[str, list[ScopeDefinition]] = {}
    for scope in scopes:
        by_family.setdefault(scope.scope_type, []).append(scope)
    result: dict[str, Any] = {}
    for scope_type, family_scopes in by_family.items():
        scope_keys = [s.scope_key for s in family_scopes]
        batch_results = await compute_current_static_scope_dynamics_batch(
            session,
            scope_type,
            scope_keys,
            axis,
            analysis_asof_date=run.trade_date,
        )
        for item in batch_results:
            sk = (item.get("scope") or {}).get("scope_key")
            if sk is not None:
                result[sk] = item
    return result


def _unavailable_leadership_snapshot(trade_date: date) -> LeadershipSnapshot:
    """诚实的 unavailable Leadership snapshot（仅作 migration fallback 用）。

    正常路径下 leadership 由 ``compute_scope_leadership_batch`` 真实计算，不会
    落到此处。此处只在 leadership_map 缺失该 scope 时兜底，明确 unavailable，
    绝不伪装成已计算的 previous/current。
    """
    return LeadershipSnapshot(
        trade_date=trade_date.isoformat(),
        status="unavailable",
        reason="leadership_map_missing_for_scope",
        direction=None,
        rankable_count=0,
        leader_set=None,
    )


async def _compute_canonical_composition_phase(
    session: AsyncSession,
    run: MarketReviewRun,
    scope: ScopeDefinition,
    *,
    prepared_observations: dict[str, Any] | None = None,
    dynamics_map: dict[str, Any] | None = None,
    leadership_map: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] The ONLY per-scope runtime owner.

    ``compute_run`` and ``resume_run`` both call exactly this owner — never a
    second per-scope path, never a legacy fallback.  It computes + persists the
    canonical Review composition for one scope and terminalizes the metrics run
    item deterministically:

    - activated family (industry_l1/l2/l3/concept): compute canonical Scope
      Observation -> invariants -> persist fact -> compose the fixed 6-key
      canonical composition -> record composition_readiness -> item SUCCEEDED.
    - non-activated family (market/major_index/style): LEGAL SKIP with a
      structured capability reason (persistence not activated) — never a legacy
      P/Q/U/C/V snapshot, never a failure.
    - no-observation-today (PIT(T) unavailable / empty members / tiny concept /
      A-class mechanism label): LEGAL SKIP (no canonical fact today).
    - fail-closed: activated family whose batch prepare is missing, invariant
      failure, or canonical DB failure raises -> outer scope loop marks FAILED.
      Missing canonical data for an activated family is NEVER a silent return
      and NEVER falls back to a legacy result.

    Returns the composed dict when a canonical fact was persisted, else None
    (legal skip).  The return value drives run-item terminalization so a
    RUNNING residue can never block ``evaluate_publish_gate``.
    """
    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_METRICS,
        status=ITEM_RUNNING,
        started_at=datetime.now(UTC),
    )
    composition = await _persist_canonical_scope_observation(
        session,
        run,
        scope,
        prepared_observations=prepared_observations,
        dynamics_map=dynamics_map,
        leadership_map=leadership_map,
    )
    if composition is None:
        # Legal skip (non-activated family / no observation today): terminalize
        # SKIPPED so a RUNNING residue can never block evaluate_publish_gate.
        await _upsert_run_item(
            session,
            run_id=run.id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            phase=PHASE_METRICS,
            status=ITEM_SKIPPED,
            last_error=(
                "canonical capability legal skip "
                "(non-activated family or no observation today)"
            ),
            completed_at=datetime.now(UTC),
        )
        return None
    await _upsert_run_item(
        session,
        run_id=run.id,
        scope_type=scope.scope_type,
        scope_key=scope.scope_key,
        phase=PHASE_METRICS,
        status=ITEM_SUCCEEDED,
        completed_at=datetime.now(UTC),
    )
    return composition


async def _persist_canonical_scope_observation(
    session: AsyncSession,
    run: MarketReviewRun,
    scope: ScopeDefinition,
    *,
    prepared_observations: dict[str, Any] | None = None,
    dynamics_map: dict[str, Any] | None = None,
    leadership_map: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """持久化规范 Scope Observation 并生成 canonical composition。

    [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 返回:
    - canonical 六键 composition dict 当 canonical fact 成功计算并落库时；
    - None 当合法跳过（非激活家族 / A 级概念 / 当日无观察）。
    返回值驱动 ``_compute_canonical_composition_phase`` 的 run-item 终态
    （composition 非 None → SUCCEEDED；None → SKIPPED），保证 composition
    readiness 与 run item 状态一致，RUNNING 残留永不阻塞发布门禁。

    仅对 activated scope（industry_l1/l2/l3 + concept）生效；market/major_index/
    style 为非激活家族，按 ScopeCapability（persistence_activated=False）合法跳过
    （写 reason，不抛错、不写表），不再依赖 "prep unavailable => 隐式跳过"。也可选
    指定已激活家族（industry/concept）的 batch prepare 缺失时 fail-closed；仅
    PIT unavailable / 空成员 这种"当日无观察"才合法跳过。

    [REVIEW-EXECUTION-PATH-CONSOLIDATION] 本函数不拥有任何 membership 解析 /
    SQL / fact preparation：``prepared_observations`` 由 compute_run / resume_run
    通过唯一 owner ``prepare_current_scope_observations_batch`` 一次 batch prepare，
    此处只按 scope_key SELECT 对应 PreparedScope 后计算并落库。missing key 表示
    batch prepare 未包含该 scope（如 batch prepare 失败被隔离），直接跳过。

    [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 除落库外，本函数同时计算 canonical
    Review Composition 的三个纯层（internal_structure_facts / member_attribution /
    scope_observation）与两个运行时未接线层（historical_dynamics / leadership，
    结构化 unavailable_current），经 ``compose_canonical_review_scope`` 产出固定
    六键 composition + composition_readiness。这是唯一 composition 计算点；本函数
    只消费既有 canonical owner（compute_scope_observation / compute_internal_structure
    / compute_member_attribution），不重算任何算法。composition_readiness 同时写入
    run.metadata_json["canonical_composition_readiness"] 供 publication gate 消费。

    compute_scope_observation 输出经 check_observation_invariants 校验，非法
    payload 由 save_scope_observation_fact 的 validate_scope_observation_payload
    在落库前 fail-fast 拒绝（延续 Round 1C Blocker #1/#2/#3）。

    A 级机制/资格/事件标签概念（融资融券/沪深股通/专精特新/次新股/ST 等）按
    CONCEPT_OBSERVATION_PERSISTENCE_EXCLUDE_NAMES 直接排除，不写 ReviewScopeObservationFact。
    """
    # [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] Scope-family capability is the
    # single activation guard.  Non-activated families (market / major_index /
    # style) are a LEGAL SKIP with a structured reason — they must never reach
    # ``save_scope_observation_fact`` (which raises
    # ``ScopePersistenceNotActivatedError``), no matter whether their PIT
    # membership resolves today.  This fixes the stale comment that claimed
    # ``is_scope_observation_persistence_excluded`` gates them (it only excludes
    # concept names/samples); the fragile "prep unavailable => implicit skip"
    # path is no longer what protects them.
    capability = resolve_scope_capability(
        scope_type=scope.scope_type, scope_name=scope.scope_name
    )
    if not capability.persistence_activated:
        logger.info(
            "[ReviewOrchestrator] 规范事实层家族未激活(合法跳过): %s/%s reasons=%s",
            scope.scope_type, scope.scope_key, capability.reasons,
        )
        return None
    # concept A-class mechanism/event labels remain excluded at observation
    # granularity (family-independent of the activation gate above).
    if is_scope_observation_persistence_excluded(
        scope_type=scope.scope_type,
        scope_name=scope.scope_name,
    ):
        logger.info(
            "[ReviewOrchestrator] 规范事实层跳过 A 级机制/资格/事件标签概念: "
            "%s/%s scope_name=%s",
            scope.scope_type, scope.scope_key, scope.scope_name,
        )
        return None
    prep = (
        prepared_observations.get(scope.scope_key)
        if prepared_observations is not None
        else None
    )
    # Fail-closed: an ACTIVATED family whose batch prepare is missing must NOT be
    # silently skipped and must NOT fall back to any legacy result (the report's
    # fail-closed gate).  Missing prep for an activated family is a data-plane gap.
    if prep is None:
        raise ValueError(
            f"canonical batch prepare missing for activated scope "
            f"{scope.scope_type}/{scope.scope_key}; refusing to skip or fall back"
        )
    if prep.pit_status_t == "unavailable" or not prep.members:
        logger.info(
            "[ReviewOrchestrator] 规范事实层跳过 unavailable/空范围: "
            "%s/%s pit_status_t=%s member_count=%d",
            scope.scope_type, scope.scope_key, prep.pit_status_t, len(prep.members),
        )
        return None

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
        return None

    # 规范事实与 composition 在同一 savepoint 内执行，失败时原子回滚本 scope。
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
            session, prep, observation,
            algorithm_version=run.algorithm_version,
            review_run_id=run.id,
        )

        # [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] Canonical Review Composition —
        # the unique composition point.  Layers are produced ONLY by their single
        # canonical owners (never re-derived). Missing upstream layers remain
        # explicit unavailable_current with a structured reason. Readiness is
        # recorded into run metadata for the publication gate.
        # [REVIEW-BACKEND-FINAL-CLOSURE Phase 5.5] Leadership 真实 T-1 → T migration：
        # 由 compute_run/resume_run 在 scope 循环前按家族批量调用唯一 owner
        # compute_scope_leadership_batch，一次性加载 [T-1, T] member facts 并建立
        # 真实 LeadershipSnapshot(T-1) 与 (T) 后计算 migration。previous 不再是
        # 硬编码 unavailable 合成 snapshot；T-1 加载失败时该 owner 已诚实退回
        # unavailable migration（不 fake-ready）。leadership 为 domain dataclass，
        # Member Attribution 直接消费；序列化到 dict 仅用于 Composition 边界。
        leadership = leadership_map.get(scope.scope_key) if leadership_map else None
        if leadership is None:
            leadership = compute_leadership_migration(
                previous_snapshot=_unavailable_leadership_snapshot(prep.trade_date),
                current_snapshot=_unavailable_leadership_snapshot(prep.trade_date),
            )
        leadership_layer = serialize_leadership_migration(leadership)
        internal_structure = compute_internal_structure(observation)
        member_attribution = compute_member_attribution(
            members=prep.members, observation=observation,
            leadership_migration=leadership,
        )
        # [REVIEW-BACKEND-FINAL-CLOSURE] Historical Dynamics 已接 runtime：
        # 由 compute_run/resume_run 在 scope 循环前按家族批量调用唯一 batch owner
        # compute_current_static_scope_dynamics_batch，产出 scope_key→dynamics map
        # 注入此处。batch owner 内部已 union members / VEC-1 / load once；禁止逐
        # scope 重建 120 天历史。若某 scope 不在 map（如 capability 未激活 family
        # 的边界情况），退回结构化 unavailable。
        historical_dynamics = dynamics_map.get(scope.scope_key) if dynamics_map else None
        if historical_dynamics is None:
            historical_dynamics = structured_unavailable_layer(
                "historical_dynamics_not_in_batch_map: scope not produced by "
                "the family dynamics batch (may be a non-activated family edge)"
            )
        # Domain dataclass → single application serialization boundary for
        # Composition/persistence/API.  Member Attribution below consumes the
        # dataclass directly; this is the ONLY dict conversion point.
        # scope_observation / member_attribution layers carry a status wrapper;
        # the raw payload is still what ``save_scope_observation_fact`` persisted.
        scope_observation_layer = {"status": "ready", **observation}
        member_attribution_layer = {"status": "ready", **member_attribution}
        composition = compose_canonical_review_scope(
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            trade_date=prep.trade_date.isoformat(),
            capability=capability,
            scope_observation=scope_observation_layer,
            historical_dynamics=historical_dynamics,
            internal_structure_facts=internal_structure,
            leadership=leadership_layer,
            member_attribution=member_attribution_layer,
        )
        logger.info(
            "[ReviewOrchestrator] canonical composition: %s/%s readiness=%s "
            "layers=%s",
            scope.scope_type, scope.scope_key,
            composition["composition_readiness"],
            sorted(
                k for k, v in composition.items()
                if k in ("scope_observation", "historical_dynamics",
                         "internal_structure_facts", "leadership",
                         "member_attribution")
                and isinstance(v, dict) and v.get("status")
            ),
        )
        # Record the per-scope composition readiness for the publication gate.
        # [REVIEW-BACKEND-FINAL-CLOSURE P0] JSONB 必须整体重新赋值为**新 dict**：
        # `run.metadata_json` 是普通 mapped_column(JSONB)，无 MutableDict；
        # 就地 setdefault/__setitem__ 不会被 SQLAlchemy 标记 dirty，commit 后
        # 结果静默丢失（同文件 _bind_or_reuse_canonical_history_source 已踩过此坑）。
        _old_meta = dict(run.metadata_json or {})
        _new_readiness = {
            **(_old_meta.get("canonical_composition_readiness") or {}),
            scope.scope_key: composition["composition_readiness"],
        }
        _new_coverage = {
            **(_old_meta.get("canonical_coverage") or {}),
            scope.scope_key: {
                "provided": len(prep.members),
                "eligible": len(prep.pit_member_ids),
            },
        }
        run.metadata_json = {
            **_old_meta,
            "canonical_composition_readiness": _new_readiness,
            "canonical_coverage": _new_coverage,
        }
        # [REVIEW-BACKEND-FINAL-CLOSURE Phase 4] 落库完整 Composition 薄表
        # （grain = review_run_id + scope_type + scope_key），与 ObservationFact
        # 同事务。Payload 已验证单 JSONB 全存足够（典型 ~130 KiB 上限）。
        await save_scope_composition_snapshot(
            session,
            review_run_id=run.id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            trade_date=prep.trade_date,
            algorithm_version=run.algorithm_version,
            composition_payload=composition,
        )
        return composition


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

    # 先复用已绑定的 canonical history source 并验证 lineage。失败时在任何
    # observation 物化前 fail closed，避免对无效 lineage 做重计算。
    (
        resume_canonical_source_run_id,
        resume_canonical_contract_version,
    ) = await _bind_or_reuse_canonical_history_source(session, run)
    await validate_review_lineage_guard(
        session,
        trade_date=run.trade_date,
        source_core_run_id=run.source_core_run_id,
        required_source_history_run_id=resume_canonical_source_run_id,
        required_history_contract_version=resume_canonical_contract_version,
        current_source="stock_core",
    )

    # 对待重算 scope 集合一次 batch prepare，失败时直接终止。
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
    prepared_observations: dict[str, Any] = {}
    if redo_specs:
        prepared_observations = await prepare_current_scope_observations_batch(
            session, run.trade_date, redo_specs
        )

    # 对每个需要重处理的 scope，执行与 compute_run 完全相同的 per-scope owner。
    # 正式 scope 解析一次（补全 taxonomy_compatibility_key），供整段 resume 复用。
    resolved = await _resolve_all_discovery_scopes(session, run)
    succeeded = 0
    failed = 0
    # [REVIEW-BACKEND-FINAL-CLOSURE] resume 同样按家族批量计算 Dynamics（只针对
    # 需要重做的 scope），产出 scope_key→dynamics map 注入 composition。
    redo_scopes = [
        ScopeDefinition(scope_type=st, scope_key=sk, scope_name=sk)
        for (st, sk) in scopes_to_redo
    ]
    resume_dynamics_map = await _compute_family_dynamics_maps(
        session, run, redo_scopes
    )
    for (scope_type, scope_key), _phases in scopes_to_redo.items():
        scope = ScopeDefinition(
            scope_type=scope_type,
            scope_key=scope_key,
            scope_name=scope_key,
        )
        # 从正式 scope 解析补全 taxonomy_compatibility_key（resume 重建的
        # ScopeDefinition 缺此字段）。
        full_scope = next(
            (
                s
                for s in resolved
                if s.scope_type == scope.scope_type
                and s.scope_key == scope.scope_key
            ),
            scope,
        )
        try:
            await _compute_canonical_composition_phase(
                session,
                run,
                full_scope,
                prepared_observations=prepared_observations,
                dynamics_map=resume_dynamics_map,
            )
            succeeded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception(
                "[ReviewOrchestrator] resume scope 失败: %s/%s err=%s",
                scope_type, scope_key, exc,
            )

    # 更新 run 状态
    run.completed_at = datetime.now(UTC)

    # 重新统计 succeeded/failed scope（基于 item 状态）
    final_succeeded, final_failed = await _count_scope_status(session, run.id)
    run.succeeded_scope_count = final_succeeded
    run.failed_scope_count = final_failed
    # [AUD-06 2026-08-07] 与主路径同口径：真实有效样本覆盖率
    run.coverage_ratio = await _aggregate_run_data_coverage(session, run)

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
        "signal_count": 0,
        # [REVIEW-BACKEND-FINAL-CLOSURE] tracking pipeline 已退休；tracking_evaluations 固定为 0。
        "tracking_evaluations": 0,
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
    run: MarketReviewRun,
) -> Decimal:
    """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 聚合 run 级真实有效样本覆盖率。

    语义：SUM(provided) / SUM(eligible)，跨该 run 全部 canonical facts（来自
    ``run.metadata_json["canonical_coverage"]``，由唯一 composition owner 在每次
    持久化 canonical fact 时记录）。回答的是“底层数据有多少是有效的”，而非
    “有多少 scope 跑完了”。

    与 scope 执行成功率（succeeded_scope_count / expected_scope_count）严格区分：
    10/10 个 scope 全部执行成功，但每个 scope 只有 80/100 成员有效时，
    执行率为 1.0，而本函数返回 0.8。

    [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 数据源从已退役的
    ``MarketReviewScopeSnapshot.ready_count/eligible_count`` 聚合切换为 canonical
    coverage metadata：新 runtime owner 不再写 snapshot 表，旧聚合恒为 0 会把
    覆盖率错误拉低并阻塞发布。

    分母为 0（无 canonical fact，或全部 scope 成员数为 0）时返回 Decimal("0")，
    不得除零，也不得回落成执行率冒充数据覆盖。
    """
    coverage = (run.metadata_json or {}).get("canonical_coverage") or {}
    eligible_total = sum(int(v.get("eligible", 0)) for v in coverage.values())
    provided_total = sum(int(v.get("provided", 0)) for v in coverage.values())
    if eligible_total <= 0:
        return Decimal("0")
    return Decimal(str(provided_total / eligible_total))


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
