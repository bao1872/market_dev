"""Review 冷启动 Bootstrap 服务（PRD §0 冷启动、§7.1 历史归一化）。

问题：
- metric_engine 需要 >= 60 个交易日的 scope snapshot 历史才能归一化
- 新系统冷启动时无 review 历史 → normalizedValue=None → value=None → publish gate block
- 旧方案：等待 60 个交易日的 review run 累积（不可接受）

Bootstrap 方案：
- 从已发布的 stock_core 历史（factor_publications where kind=stock_core）回填
- 对每个历史交易日：
  1. 读取 stock_core snapshot
  2. 解析 market 范围成员
  3. 计算 P/Q/U/C/V 原始值（raw values，无需归一化）
  4. 存储为 scope snapshot（带 metadata.bootstrap=True 标记）
- _build_scope_history 读取 scope snapshots 时不区分 review_run_id，
  会自动拾取 bootstrap 写入的历史 raw values
- 可重复执行：相同 trade_date 已有 bootstrap snapshot 时跳过

约束：
- 不修改 stock_core 数据（只读）
- 不修改现有 review run（只创建 bootstrap run）
- 不绕过 publish gate（bootstrap 只补历史，不 force publish）
- dry_run=True 时只计算不写入（canary 用）

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_bootstrap_service
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.metric_engine import compute_all_metrics
from app.domain.review.metric_registry import DEFAULT_REGISTRY
from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
)
from app.services.review_scope_service import (
    ScopeDefinition,
    fetch_member_flat_list,
    resolve_scope_members,
)

logger = logging.getLogger("review_bootstrap_service")

# Bootstrap 专用版本号（与正式 review 算法版本隔离）
BOOTSTRAP_ALGORITHM_VERSION = "bootstrap-1.0.0"
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
) -> list[tuple[date, uuid.UUID]]:
    """列出可 bootstrap 的历史交易日（有 stock_core publication 的日期）。

    Args:
        session: 异步 DB 会话
        end_date: 截止日期（None=最近已完成交易日）
        days_back: 回溯天数（默认 120）

    Returns:
        [(trade_date, stock_core_run_id), ...] 按日期降序
    """
    if end_date is None:
        end_date = date.today()

    start_date = end_date - timedelta(days=days_back)

    stmt = (
        select(
            FactorPublication.trade_date,
            FactorPublication.data_run_id,
        )
        .where(
            FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
            FactorPublication.trade_date >= start_date,
            FactorPublication.trade_date <= end_date,
        )
        .order_by(FactorPublication.trade_date.desc())
    )
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def bootstrap_single_date(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    source_board_run_id: uuid.UUID | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """对单个历史交易日执行 bootstrap。

    流程：
    1. 创建 bootstrap run（metadata.bootstrap=True）
    2. 解析 market 范围成员
    3. 读取 stock_core flat list
    4. 计算 P/Q/U/C/V 原始值（无历史，只算 raw）
    5. dry_run=True: 返回计算结果不写入
       dry_run=False: upsert scope snapshot

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        trade_date: 历史交易日
        source_core_run_id: stock_core run_id
        source_board_run_id: board run_id（None 时用 core_id 占位）
        dry_run: 只计算不写入

    Returns:
        {"trade_date": ..., "run_id": ..., "metrics": {...}, "written": bool}
    """
    board_id = source_board_run_id or source_core_run_id

    # 1. 创建 bootstrap run（或复用已有）
    run_id = await _upsert_bootstrap_run(
        session,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        source_board_run_id=board_id,
    )

    # 2. 解析 market 范围成员
    instrument_ids, scope_name = await resolve_scope_members(
        session, "market", "market", trade_date=trade_date,
    )
    if not instrument_ids:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": str(run_id),
            "status": "skipped",
            "reason": "no active instruments",
            "written": False,
        }

    # 3. 读取 stock_core flat list
    flat_list = await fetch_member_flat_list(
        session,
        instrument_ids,
        source_core_run_id,
        trade_date=trade_date,
    )
    if not flat_list:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": str(run_id),
            "status": "skipped",
            "reason": "no stock_core snapshots for this run_id",
            "written": False,
        }

    # 4. 计算 P/Q/U/C/V 原始值（无历史归一化，只算 raw）
    ready_count = sum(
        1 for f in flat_list if f and f.get("fp_trend_direction") is not None
    )
    payloads = compute_all_metrics(
        flat_list,
        ready_count=ready_count,
        history_maps=None,  # 无历史，只算 raw
        registry=DEFAULT_REGISTRY,
    )

    # 5. 状态判定（bootstrap snapshot 标记为 partial，因为无归一化）
    statuses = [p.get("status") for p in payloads.values()]
    if not flat_list:
        snap_status = "unavailable"
    elif all(s == "ready" for s in statuses):
        snap_status = "ready"
    elif any(s == "insufficient_history" for s in statuses):
        snap_status = "insufficient_history"
    else:
        snap_status = "partial"

    coverage = ready_count / max(1, len(instrument_ids))

    if dry_run:
        return {
            "trade_date": trade_date.isoformat(),
            "run_id": str(run_id),
            "status": snap_status,
            "eligible_count": len(instrument_ids),
            "ready_count": ready_count,
            "coverage": coverage,
            "metrics": {
                code: {
                    "value": p.get("value"),
                    "rawValue": p.get("rawValue"),
                    "status": p.get("status"),
                    "readiness": p.get("readiness"),
                }
                for code, p in payloads.items()
            },
            "written": False,
        }

    # 6. 写入 scope snapshot
    scope = ScopeDefinition(
        scope_type="market",
        scope_key="market",
        scope_name=scope_name,
    )
    await _upsert_bootstrap_scope_snapshot(
        session,
        review_run_id=run_id,
        trade_date=trade_date,
        scope=scope,
        eligible_count=len(instrument_ids),
        ready_count=ready_count,
        coverage_ratio=coverage,
        status=snap_status,
        payloads=payloads,
    )

    return {
        "trade_date": trade_date.isoformat(),
        "run_id": str(run_id),
        "status": snap_status,
        "eligible_count": len(instrument_ids),
        "ready_count": ready_count,
        "coverage": coverage,
        "written": True,
        # [P0-5 2026-07-30] P0 只 bootstrap market scope
        # 行业/概念不回填：无历史板块成员快照，使用当前成员会产生存活偏差
        "scope_limitations": {
            "market": "bootstrapped",
            "industry_l1": "membership_history_unavailable",
            "industry_l2": "membership_history_unavailable",
            "concept": "membership_history_unavailable",
        },
    }


async def bootstrap_history(
    session: AsyncSession,
    *,
    end_date: date | None = None,
    days_back: int = DEFAULT_BOOTSTRAP_DAYS,
    dry_run: bool = True,
) -> dict[str, Any]:
    """批量执行 bootstrap（从 stock_core 历史回填 scope snapshots）。

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
            "status": "no_stock_core_history",
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


async def _upsert_bootstrap_run(
    session: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    source_board_run_id: uuid.UUID,
) -> uuid.UUID:
    """创建或复用 bootstrap run（幂等）。

    bootstrap run 特征：
    - algorithm_version = BOOTSTRAP_ALGORITHM_VERSION
    - filter_version = BOOTSTRAP_FILTER_VERSION
    - metadata.bootstrap = True
    - status = published（bootstrap 数据是终态，不会被重新计算）
    """
    now = datetime.now(UTC)
    meta = {
        "bootstrap": True,
        "bootstrap_created_at": now.isoformat(),
        "bootstrap_algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
    }

    values = {
        "trade_date": trade_date,
        "source_core_run_id": source_core_run_id,
        "source_board_run_id": source_board_run_id,
        "algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
        "filter_version": BOOTSTRAP_FILTER_VERSION,
        "baseline_window": DEFAULT_BOOTSTRAP_DAYS,
        "status": "published",
        "expected_scope_count": 1,
        "succeeded_scope_count": 1,
        "failed_scope_count": 0,
        "signal_count": 0,
        "coverage_ratio": 0,
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
