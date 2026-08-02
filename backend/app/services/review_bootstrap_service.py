"""Point-in-time Review history bootstrap.

Historical facts come from canonical first-pyramid daily state, daily bars, and
PIT scope memberships. Missing historical membership is recorded as
``bootstrap_unavailable`` and never falls back to today's population. Dry-run is
strictly read-only. Applied runs materialize observations for the production
Review algorithm but never publish a Review pointer.

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_bootstrap_service
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.metric_engine import compute_all_metrics
from app.domain.review.metric_registry import DEFAULT_REGISTRY
from app.models.board_taxonomy import BoardDefinitionVersion
from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
from app.models.market_board import MarketBoard
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
)
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
from app.services.board_membership_service import list_universe_definitions_at
from app.services.review_metric_observation_service import persist_metric_observations
from app.services.review_scope_service import (
    ScopeDefinition,
    ScopeSnapshotError,
    fetch_historical_member_facts,
    resolve_scope_members,
)

logger = logging.getLogger("review_bootstrap_service")

# Bootstrap observations must be comparable with the production algorithm.
BOOTSTRAP_ALGORITHM_VERSION = "review-2.0.0"
BOOTSTRAP_FILTER_VERSION = "bootstrap"

# 默认回填天数（PRD §0：默认 120 日，最低 60 日）
DEFAULT_BOOTSTRAP_DAYS = 120
MIN_BOOTSTRAP_DAYS = 60


# =============================================================================
# 公开 API
# =============================================================================


async def list_bootstrap_eligible_dates(
    session: AsyncSession,
    *,
    end_date: date | None = None,
    days_back: int = DEFAULT_BOOTSTRAP_DAYS,
) -> list[tuple[date, uuid.UUID | None]]:
    """List canonical FP history dates and optional audit source run identities.

    Args:
        session: 异步 DB 会话
        end_date: 截止日期（None=最近已完成交易日）
        days_back: 回溯天数（默认 120）

    Returns:
        ``[(trade_date, stock_core_run_id | None), ...]`` 按日期降序。
        日期来源始终是 FP history；缺少 source identity 不删除该日期。
    """
    if end_date is None:
        end_date = date.today()

    start_date = end_date - timedelta(days=days_back)

    history_stmt = (
        select(FirstPyramidHistoryDailyState.trade_date)
        .where(
            FirstPyramidHistoryDailyState.trade_date >= start_date,
            FirstPyramidHistoryDailyState.trade_date <= end_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
        .distinct()
        .order_by(FirstPyramidHistoryDailyState.trade_date.desc())
    )
    dates = [row[0] for row in (await session.execute(history_stmt)).all()]
    if not dates:
        return []
    source_stmt = select(
        FactorPublication.trade_date, FactorPublication.data_run_id,
    ).where(
        FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
        FactorPublication.trade_date.in_(dates),
    )
    sources = {row[0]: row[1] for row in (await session.execute(source_stmt)).all()}
    return [(item, sources.get(item)) for item in dates]


async def bootstrap_single_date(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID | None,
    source_board_run_id: uuid.UUID | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Bootstrap every PIT scope family for one historical date.

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        trade_date: 历史交易日
        source_core_run_id: 可审计 stock_core source identity；apply 时必需
        source_board_run_id: 可审计 board source identity；apply 时必需
        dry_run: 只计算不写入

    Returns:
        {"trade_date": ..., "run_id": ..., "metrics": {...}, "written": bool}
    """
    scopes = await _list_bootstrap_scopes(session, trade_date)
    computed: list[tuple[ScopeDefinition, list[dict[str, Any]], int, int, float, str, dict[str, dict[str, Any]]]] = []
    scope_results: list[dict[str, Any]] = []
    for scope in scopes:
        try:
            if scope.scope_type == "market":
                instrument_ids = await _market_history_members(session, trade_date)
            else:
                instrument_ids, _ = await resolve_scope_members(
                    session, scope.scope_type, scope.scope_key, trade_date=trade_date,
                )
        except ScopeSnapshotError as exc:
            scope_results.append(_unavailable_scope(scope, str(exc)))
            continue
        if not instrument_ids:
            scope_results.append(_unavailable_scope(scope, "pit_membership_empty"))
            continue
        flat_list = await fetch_historical_member_facts(
            session, instrument_ids, trade_date=trade_date,
        )
        if not flat_list:
            scope_results.append(_unavailable_scope(scope, "historical_member_facts_missing"))
            continue
        ready_count = sum(
            1 for fact in flat_list if fact.get("fp_trend_direction") is not None
        )
        payloads = compute_all_metrics(
            flat_list, ready_count=ready_count, history_maps=None,
            registry=DEFAULT_REGISTRY,
        )
        coverage = ready_count / len(instrument_ids)
        status = "insufficient_history"
        computed.append(
            (scope, flat_list, len(instrument_ids), ready_count, coverage, status, payloads),
        )
        scope_results.append({
            "scope_type": scope.scope_type,
            "scope_key": scope.scope_key,
            "status": status,
            "eligible_count": len(instrument_ids),
            "ready_count": ready_count,
            "coverage": coverage,
        })

    if dry_run:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "dry_run",
            "scopes": scope_results,
            "written": False,
        }
    if source_core_run_id is None or source_board_run_id is None:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "source_run_identity_missing",
            "scopes": scope_results,
            "written": False,
        }
    if not computed:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": None,
            "status": "bootstrap_unavailable",
            "reason": "no_pit_scope_facts",
            "scopes": scope_results,
            "written": False,
        }

    run_id = await _upsert_bootstrap_run(
        session,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        source_board_run_id=source_board_run_id,
        expected_scope_count=len(scopes),
        succeeded_scope_count=len(computed),
        failed_scope_count=len(scopes) - len(computed),
        scope_results=scope_results,
    )
    for scope, flat_list, eligible_count, ready_count, coverage, status, payloads in computed:
        await _upsert_bootstrap_scope_snapshot(
            session,
            review_run_id=run_id,
            trade_date=trade_date,
            scope=scope,
            eligible_count=eligible_count,
            ready_count=ready_count,
            coverage_ratio=coverage,
            status=status,
            payloads=payloads,
        )
        await persist_metric_observations(
            session,
            review_run_id=run_id,
            trade_date=trade_date,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
            membership_version=scope.membership_version,
            algorithm_version=BOOTSTRAP_ALGORITHM_VERSION,
            flat_list=flat_list,
            payloads=payloads,
        )

    return {
        "trade_date": trade_date.isoformat(),
        "run_id": str(run_id),
        "status": "completed",
        "scopes": scope_results,
        "written": True,
    }


async def bootstrap_history(
    session: AsyncSession,
    *,
    end_date: date | None = None,
    days_back: int = DEFAULT_BOOTSTRAP_DAYS,
    dry_run: bool = True,
) -> dict[str, Any]:
    """批量执行 canonical PIT history bootstrap。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        end_date: 截止日期（None=今天）
        days_back: 回溯天数（默认 120，最低 60）
        dry_run: 只计算不写入

    Returns:
        {
            "end_date": ...,
            "days_back": ...,
            "dry_run": bool,
            "eligible_dates": int,
            "processed": int,
            "skipped": int,
            "written": int,
            "results": [...],
        }
    """
    if days_back < MIN_BOOTSTRAP_DAYS:
        logger.warning(
            "[Bootstrap] days_back=%d < 最低值 %d，可能无法生成足够历史",
            days_back, MIN_BOOTSTRAP_DAYS,
        )

    # 1. 列出可 bootstrap 的日期
    eligible = await list_bootstrap_eligible_dates(
        session, end_date=end_date, days_back=days_back,
    )

    if not eligible:
        return {
            "end_date": (end_date or date.today()).isoformat(),
            "days_back": days_back,
            "dry_run": dry_run,
            "eligible_dates": 0,
            "processed": 0,
            "skipped": 0,
            "written": 0,
            "results": [],
            "status": "no_canonical_fp_history",
        }

    # 2. 逐日执行 bootstrap
    results: list[dict[str, Any]] = []
    processed = 0
    skipped = 0
    written = 0

    for trade_date, core_run_id in eligible:
        # dry_run 模式下不查询 board publication（省查询）
        board_run_id = await _try_resolve_board_run_id(session, trade_date)

        result = await bootstrap_single_date(
            session,
            trade_date=trade_date,
            source_core_run_id=core_run_id,
            source_board_run_id=board_run_id,
            dry_run=dry_run,
        )
        results.append(result)
        processed += 1
        if result.get("written"):
            written += 1
        elif result.get("status") == "skipped":
            skipped += 1

    return {
        "end_date": (end_date or date.today()).isoformat(),
        "days_back": days_back,
        "dry_run": dry_run,
        "eligible_dates": len(eligible),
        "processed": processed,
        "skipped": skipped,
        "written": written,
        "results": results,
        "status": "ok" if dry_run or written > 0 else "no_writes",
    }


# =============================================================================
# 内部工具
# =============================================================================


async def _market_history_members(
    session: AsyncSession,
    trade_date: date,
) -> list[uuid.UUID]:
    stmt = select(FirstPyramidHistoryDailyState.instrument_id).where(
        FirstPyramidHistoryDailyState.trade_date == trade_date,
        FirstPyramidHistoryDailyState.algorithm_version
        == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
    )
    return list((await session.execute(stmt)).scalars())


async def _list_bootstrap_scopes(
    session: AsyncSession,
    trade_date: date,
) -> list[ScopeDefinition]:
    scopes = [ScopeDefinition("market", "market", "全市场", membership_version="fp-history")]
    for universe_type in ("major_index", "style"):
        definitions = await list_universe_definitions_at(
            session, trade_date, universe_type=universe_type,
        )
        scopes.extend(
            ScopeDefinition(
                definition.universe_type,
                definition.universe_key,
                definition.name,
                taxonomy_version=definition.version,
                taxonomy_compatibility_key=definition.compatibility_key,
                membership_version=definition.membership_version,
            )
            for definition in definitions
        )

    board_stmt = (
        select(BoardDefinitionVersion, MarketBoard)
        .join(MarketBoard, MarketBoard.id == BoardDefinitionVersion.board_id)
        .where(
            BoardDefinitionVersion.effective_from <= trade_date,
            or_(
                BoardDefinitionVersion.effective_to.is_(None),
                BoardDefinitionVersion.effective_to > trade_date,
            ),
            BoardDefinitionVersion.board_type.in_(("industry", "concept")),
        )
        .order_by(BoardDefinitionVersion.board_type, MarketBoard.name)
    )
    for definition, board in (await session.execute(board_stmt)).all():
        if definition.board_type == "concept":
            scope_type = "concept"
        else:
            level = definition.hierarchy_level.lower()
            if level not in {"l1", "l2", "l3"}:
                continue
            scope_type = f"industry_{level}"
        scopes.append(
            ScopeDefinition(
                scope_type,
                str(definition.board_id),
                board.name,
                parent_scope_type=(
                    "industry_l1" if definition.parent_board_id and scope_type == "industry_l2"
                    else "industry_l2" if definition.parent_board_id and scope_type == "industry_l3"
                    else None
                ),
                parent_scope_key=(
                    str(definition.parent_board_id)
                    if definition.parent_board_id is not None else None
                ),
                taxonomy_version=definition.taxonomy_version,
                taxonomy_compatibility_key=definition.taxonomy_compatibility_key,
                membership_version=definition.membership_version,
            ),
        )
    return scopes


def _unavailable_scope(scope: ScopeDefinition, reason: str) -> dict[str, Any]:
    return {
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "status": "bootstrap_unavailable",
        "reason": reason,
    }


async def _upsert_bootstrap_run(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    source_board_run_id: uuid.UUID,
    expected_scope_count: int,
    succeeded_scope_count: int,
    failed_scope_count: int,
    scope_results: list[dict[str, Any]],
) -> uuid.UUID:
    """创建或复用 bootstrap run（幂等）。

    bootstrap run 特征：
    - algorithm_version = BOOTSTRAP_ALGORITHM_VERSION
    - filter_version = BOOTSTRAP_FILTER_VERSION
    - metadata.bootstrap = True
    - status = partial（仅提供历史 observation，不创建正式 publication）
    """
    now = datetime.now(UTC)
    meta = {
        "bootstrap": True,
        "bootstrap_created_at": now.isoformat(),
        "bootstrap_algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
        "bootstrap_scope_results": scope_results,
    }

    values = {
        "trade_date": trade_date,
        "source_core_run_id": source_core_run_id,
        "source_board_run_id": source_board_run_id,
        "algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
        "filter_version": BOOTSTRAP_FILTER_VERSION,
        "baseline_window": DEFAULT_BOOTSTRAP_DAYS,
        "status": "partial",
        "expected_scope_count": expected_scope_count,
        "succeeded_scope_count": succeeded_scope_count,
        "failed_scope_count": failed_scope_count,
        "signal_count": 0,
        "coverage_ratio": (
            succeeded_scope_count / expected_scope_count
            if expected_scope_count else 0
        ),
        "started_at": now,
        "completed_at": now,
        "metadata_json": meta,
    }
    stmt = pg_insert(MarketReviewRun).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_runs_date_core_board_algo_filter",
        set_={
            "metadata_json": stmt.excluded.metadata_json,
            "status": stmt.excluded.status,
            "expected_scope_count": stmt.excluded.expected_scope_count,
            "succeeded_scope_count": stmt.excluded.succeeded_scope_count,
            "failed_scope_count": stmt.excluded.failed_scope_count,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "completed_at": stmt.excluded.completed_at,
        },
    )
    await session.execute(stmt)
    await session.flush()

    # 读取 run_id
    stmt2 = (
        select(MarketReviewRun.id)
        .where(
            MarketReviewRun.trade_date == trade_date,
            MarketReviewRun.source_core_run_id == source_core_run_id,
            MarketReviewRun.source_board_run_id == source_board_run_id,
            MarketReviewRun.algorithm_version == BOOTSTRAP_ALGORITHM_VERSION,
            MarketReviewRun.filter_version == BOOTSTRAP_FILTER_VERSION,
        )
        .limit(1)
    )
    result = await session.execute(stmt2)
    run_id = result.scalar_one_or_none()
    if run_id is None:
        raise RuntimeError(
            f"bootstrap run upsert 后读不到 run_id: trade_date={trade_date}",
        )
    return run_id


async def _upsert_bootstrap_scope_snapshot(
    session: AsyncSession,
    *,
    review_run_id: uuid.UUID,
    trade_date: date,
    scope: ScopeDefinition,
    eligible_count: int,
    ready_count: int,
    coverage_ratio: float,
    status: str,
    payloads: dict[str, dict[str, Any]],
) -> None:
    """upsert bootstrap scope snapshot（幂等）。"""
    values = {
        "review_run_id": review_run_id,
        "trade_date": trade_date,
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "scope_name": scope.scope_name,
        "eligible_count": eligible_count,
        "ready_count": ready_count,
        "coverage_ratio": coverage_ratio,
        "status": status,
        "p_payload": payloads.get("P"),
        "q_payload": payloads.get("Q"),
        "u_payload": payloads.get("U"),
        "c_payload": payloads.get("C"),
        "v_payload": payloads.get("V"),
        "data_quality_json": {
            "bootstrap": True,
            "eligible_count": eligible_count,
            "ready_count": ready_count,
        },
    }
    stmt = pg_insert(MarketReviewScopeSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_scope_snapshots_run_scope",
        set_={
            "trade_date": stmt.excluded.trade_date,
            "eligible_count": stmt.excluded.eligible_count,
            "ready_count": stmt.excluded.ready_count,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "status": stmt.excluded.status,
            "p_payload": stmt.excluded.p_payload,
            "q_payload": stmt.excluded.q_payload,
            "u_payload": stmt.excluded.u_payload,
            "c_payload": stmt.excluded.c_payload,
            "v_payload": stmt.excluded.v_payload,
            "data_quality_json": stmt.excluded.data_quality_json,
        },
    )
    await session.execute(stmt)
    await session.flush()


async def _try_resolve_board_run_id(
    session: AsyncSession,
    trade_date: date,
) -> uuid.UUID | None:
    """尝试解析 board run_id（不存在返回 None）。"""
    from app.models.factor_publication import PUBLICATION_KIND_MARKET_AGGREGATION

    stmt = (
        select(FactorPublication.data_run_id)
        .where(
            FactorPublication.trade_date == trade_date,
            FactorPublication.publication_kind == PUBLICATION_KIND_MARKET_AGGREGATION,
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


if __name__ == "__main__":
    print(f"BOOTSTRAP_ALGORITHM_VERSION = {BOOTSTRAP_ALGORITHM_VERSION}")
    print(f"DEFAULT_BOOTSTRAP_DAYS = {DEFAULT_BOOTSTRAP_DAYS}")
    print(f"MIN_BOOTSTRAP_DAYS = {MIN_BOOTSTRAP_DAYS}")
    print("OK: review_bootstrap_service imports verified")
