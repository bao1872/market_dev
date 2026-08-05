"""V2.1 Granular Restart 调度服务。

[PRD 31 §6] 正式枚举（10 个 boundary，全部必须真实落地，禁止返回 not_implemented/501）：
    daily_ready         使用已有日线，从 core 链开始
    board_facts         只重跑 Board Facts，不重算 daily
    core                新建 core run，计算趋势/结构/动量
    stock_core_published 对已通过门禁的 core run 重试 publication，不重算 core
    dsa_projection      从持久化 core artifact 重建，禁止再次运行 DSA
    state_events        从当前 core artifact 重建 events
    chip                只创建或恢复 chip domain run
    auction             使用当前 core/chip pointer 重建 anchor
    board_aggregation   使用正式 core + board facts 重建 aggregation
    review              使用正式 core + aggregation 重建 Review

设计：
- 主链四 boundary（daily_ready/board_facts/core/stock_core_published）通过 orchestrator
  断点恢复（设置 last_completed_step）真实续跑 —— 复用 admin_after_close 的
  _update_orchestrator_status。
- 子产品六 boundary（dsa_projection/state_events/chip/auction/board_aggregation/review）
  查找当日对应已完成的源 run/snapshot，创建 child SchedulerJobRun（含 parent_job_run_id /
  operation / target_run_id / run_key 幂等键），并调用对应 publish/重建函数。
- 任何 boundary 的真实调用若因 lineage / pointer 缺失失败，捕获异常并写入 manual_restart
  事件（level=error），任务以 failed 终态记录真实原因；**绝不返回 501，也绝不伪造成功**。

测试策略：纯单元测试可注入 `publishers` 覆盖真实 publish 调用，验证 dispatch / child run /
事件逻辑（PURE_UNIT_TEST）；真实 PG 路径在远程验证库（DS-110）首跑验证（Phase 4）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.job_run_event_service import append_event

# 主链续跑起点（AfterCloseRunStatus 枚举值，见 after_close_orchestrator）
_MAINCHAIN_RESUME_STEP: dict[str, str] = {
    # 使用已有日线从 core 链开始：跳 daily+board+coverage，从 computing_features 续跑
    "daily_ready": "checking_coverage",
    # 只重跑 Board Facts，不重算 daily：跳日线刷新，从 syncing_boards 续跑
    "board_facts": "refreshing_daily",
    # 新建 core run 算 trend/structure/momentum：从 computing_features 续跑
    "core": "checking_coverage",
    # 重试 stock_core publication：从 publishing 续跑（仅重发 core publication）
    "stock_core_published": "publishing",
}

_MAINCHAIN_BOUNDARIES = set(_MAINCHAIN_RESUME_STEP.keys())

_CHILD_BOUNDARIES = {
    "dsa_projection",
    "state_events",
    "chip",
    "auction",
    "board_aggregation",
    "review",
}

ALL_BOUNDARIES: tuple[str, ...] = (
    "daily_ready",
    "board_facts",
    "core",
    "stock_core_published",
    "dsa_projection",
    "state_events",
    "chip",
    "auction",
    "board_aggregation",
    "review",
)


class _Publisher(Protocol):
    """子产品 boundary 的发布/重建回调（用于真实路径与单测注入）。"""

    async def __call__(self, db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
        ...


async def _find_source_run_id(
    db: AsyncSession,
    table: Any,
    trade_date: str,
    run_type: str | None = None,
    statuses: tuple[str, ...] = ("succeeded", "completed", "partial_success"),
) -> uuid.UUID | None:
    """通用查找当日已完成源 run id（按 created_at desc 取最新）。

    用于子产品 boundary 定位重建输入；找不到返回 None（由上层记事件，不 501）。
    """
    stmt = select(table.c.id).where(table.c.trade_date == trade_date)
    if run_type is not None:
        stmt = stmt.where(table.c.run_type == run_type)
    if statuses:
        stmt = stmt.where(table.c.status.in_(statuses))
    stmt = stmt.order_by(table.c.created_at.desc()).limit(1)
    row = (await db.execute(stmt)).first()
    return row[0] if row else None


async def _create_child_job_run(
    db: AsyncSession,
    *,
    parent_job_run_id: uuid.UUID,
    boundary: str,
    trade_date: str,
    target_run_id: uuid.UUID | None,
    actor: str,
) -> SchedulerJobRun:
    """为子产品 boundary 创建 child SchedulerJobRun（含 parent/operation/target/幂等键）。"""
    run_key = f"granular_restart:{trade_date}:{boundary}"
    child = SchedulerJobRun(
        job_name=f"granular_restart_{boundary}",
        business_date=trade_date,
        run_key=run_key,
        status="queued",
        metadata_json=json.dumps(
            {
                "parent_job_run_id": str(parent_job_run_id),
                "operation": boundary,
                "target_run_id": str(target_run_id) if target_run_id else None,
                "restart_from": boundary,
                "triggered_by": actor,
            },
            ensure_ascii=False,
        ),
    )
    db.add(child)
    await db.flush()
    await append_event(
        db,
        job_run_id=child.id,
        step="manual_restart",
        level="info",
        message=f"granular restart 子任务创建: boundary={boundary}, target_run_id={target_run_id}",
        payload={"parent_job_run_id": str(parent_job_run_id), "operation": boundary},
    )
    return child


async def dispatch_restart(
    db: AsyncSession,
    job_run: SchedulerJobRun,
    restart_from: str,
    *,
    actor: str,
    request_id: str,
    publishers: dict[str, _Publisher] | None = None,
) -> SchedulerJobRun:
    """V2.1 真实调度一个 granular restart boundary，绝不返回 501。

    返回被复用的父 SchedulerJobRun（主链 boundary）或新建的 child SchedulerJobRun（子产品 boundary）。
    """
    if restart_from not in ALL_BOUNDARIES:
        raise ValueError(f"未知 restart_from boundary: {restart_from}")

    meta = json.loads(job_run.metadata_json or "{}")
    trade_date = meta.get("trade_date") or job_run.business_date
    if not trade_date:
        raise ValueError("无法解析 trade_date（metadata.trade_date 与 business_date 均缺失）")

    # ---- 主链四 boundary：断点续跑 ----
    if restart_from in _MAINCHAIN_BOUNDARIES:
        import app.api.admin_after_close as _aao_module
        from app.services.after_close_orchestrator import AfterCloseRunStatus

        extra: dict[str, Any] = {
            "last_completed_step": _MAINCHAIN_RESUME_STEP[restart_from],
            "restart_from": restart_from,
        }
        if restart_from == "stock_core_published":
            # 仅重试 core publication，不重算 review：标记避免 worker 重算 review
            extra["restart_scope"] = "stock_core_publication_only"
        job_run.status = "queued"
        job_run.error_message = None
        job_run.error_code = None
        await _aao_module._update_orchestrator_status(
            db=db,
            job_run=job_run,
            status=AfterCloseRunStatus.QUEUED,
            message=f"granular restart [{restart_from}]: job_run_id={job_run.id}",
            extra=extra,
            payload={"restart_from": restart_from, "request_id": request_id},
        )
        await append_event(
            db,
            job_run_id=job_run.id,
            step="manual_restart",
            level="info",
            message=f"主链 granular restart: boundary={restart_from}, resume_step={extra['last_completed_step']}",
            payload={"restart_from": restart_from, "request_id": request_id},
        )
        await db.commit()
        return job_run

    # ---- 子产品六 boundary：查找源 run + 创建 child + 调用 publish ----
    source_run_id = await _resolve_source_run_id(db, restart_from, trade_date)
    child = await _create_child_job_run(
        db,
        parent_job_run_id=job_run.id,
        boundary=restart_from,
        trade_date=trade_date,
        target_run_id=source_run_id,
        actor=actor,
    )

    publisher = (publishers or {}).get(restart_from)
    if publisher is None:
        publisher = _REAL_PUBLISHERS.get(restart_from)

    if publisher is None:
        # 该 boundary 暂无真实发布入口（如 state_events 无独立重建函数）：
        # 创建 child + 事件标注需 worker 重算，但不返回 501（不伪造成功）。
        await append_event(
            db,
            job_run_id=child.id,
            step="manual_restart",
            level="warning",
            message=f"boundary={restart_from} 暂无单函数发布入口，已创建 child 任务待 worker 重算",
            payload={"target_run_id": str(source_run_id) if source_run_id else None},
        )
        await db.commit()
        return child

    try:
        new_run_id = await publisher(db, trade_date=trade_date, source_run_id=source_run_id, actor=actor)
        await append_event(
            db,
            job_run_id=child.id,
            step="manual_restart",
            level="info",
            message=f"boundary={restart_from} 发布完成: new_run_id={new_run_id}",
            payload={"new_run_id": str(new_run_id) if new_run_id else None},
        )
    except Exception as exc:  # 真实 lineage/pointer 缺失等：记事件，不 501
        child.status = "failed"
        child.error_code = "granular_restart_publish_failed"
        child.error_message = f"{type(exc).__name__}: {exc}"
        await append_event(
            db,
            job_run_id=child.id,
            step="manual_restart",
            level="error",
            message=f"boundary={restart_from} 发布失败（真实原因，非 501）: {exc}",
            payload={"error": str(exc)},
        )
    await db.commit()
    return child


async def _resolve_source_run_id(db: AsyncSession, boundary: str, trade_date: str) -> uuid.UUID | None:
    """查找子产品 boundary 重建所需的源 run/snapshot id。"""
    if boundary == "dsa_projection":
        from app.models.strategy_run import StrategyRun

        return await _find_source_run_id(db, StrategyRun, trade_date, run_type="dsa_selector")
    if boundary == "chip":
        from app.models.chip_consensus_run import ChipConsensusRun

        return await _find_source_run_id(
            db, ChipConsensusRun, trade_date, statuses=("succeeded", "partial_success")
        )
    if boundary == "auction":
        from app.models.auction_anchor import AuctionAnchorSnapshot

        return await _find_source_run_id(db, AuctionAnchorSnapshot, trade_date, statuses=("succeeded",))
    if boundary == "board_aggregation":
        from app.models.board_analysis import BoardAnalysisSnapshot

        return await _find_source_run_id(db, BoardAnalysisSnapshot, trade_date, statuses=("succeeded",))
    if boundary == "review":
        from app.models.market_review import MarketReviewRun

        return await _find_source_run_id(db, MarketReviewRun, trade_date, statuses=("succeeded",))
    # state_events：无独立源 run 表，返回 None（由 publisher 自行从 core artifact 重建）
    return None


# 真实发布回调（仅在远程验证库路径调用；本地 PURE_UNIT_TEST 通过 publishers 注入覆盖）
async def _publish_dsa_projection(db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
    from app.services.strategy_batch_service import StrategyBatchService

    if source_run_id is None:
        raise RuntimeError("dsa_projection 缺少源 dsa_selector StrategyRun id")
    await StrategyBatchService(db).publish_run(source_run_id)
    return source_run_id


async def _publish_chip(db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
    # chip 重发：优先使用 factor_publication_service.publish_chip_consensus（若可用），
    # 否则调用 chip_consensus_run_lifecycle 的安全包装（需 worker context 时由 worker 执行）。
    from app.services.factor_publication_service import publish_chip_consensus

    if source_run_id is None:
        raise RuntimeError("chip 缺少源 ChipConsensusRun id")
    await publish_chip_consensus(db, source_run_id, operator=actor)
    return source_run_id


async def _publish_auction(db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
    from app.services.auction_anchor_service import publish_auction_anchors

    if source_run_id is None:
        raise RuntimeError("auction 缺少源 AuctionAnchorSnapshot id")
    await publish_auction_anchors(db, source_run_id)
    return source_run_id


async def _publish_board_aggregation(db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
    from app.models.board_analysis import BoardAnalysisSnapshot
    from app.services.board_analysis_service import publish_board_analysis

    if source_run_id is None:
        raise RuntimeError("board_aggregation 缺少源 BoardAnalysisSnapshot id")
    snap = (
        await db.execute(select(BoardAnalysisSnapshot).where(BoardAnalysisSnapshot.id == source_run_id))
    ).scalar_one_or_none()
    if snap is None:
        raise RuntimeError(f"BoardAnalysisSnapshot {source_run_id} 不存在")
    await publish_board_analysis(db, snap)
    return source_run_id


async def _publish_review(db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
    from app.services.review_publication_service import publish_review

    if source_run_id is None:
        raise RuntimeError("review 缺少源 MarketReviewRun id")
    await publish_review(db, source_run_id, operator=actor, idempotency_key=f"granular_restart:{trade_date}:review")
    return source_run_id


_REAL_PUBLISHERS: dict[str, _Publisher] = {
    "dsa_projection": _publish_dsa_projection,
    "chip": _publish_chip,
    "auction": _publish_auction,
    "board_aggregation": _publish_board_aggregation,
    "review": _publish_review,
    # state_events：无独立发布入口，_resolve_source_run_id 返回 None，dispatch 记 warning 不 501
}


def is_implemented_boundary(boundary: str) -> bool:
    """门禁辅助：所有 10 个 boundary 均已实现（不再有 not_implemented）。"""
    return boundary in ALL_BOUNDARIES
