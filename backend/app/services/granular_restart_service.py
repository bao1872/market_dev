"""V2.1 Granular Restart 调度服务（Corrective Pass 2）。

[PRD 31 §6] 正式枚举（10 个 boundary）：
    daily_ready / board_facts / core / stock_core_published /
    dsa_projection / state_events / chip / auction / board_aggregation / review

设计约束（禁止伪造成功）：
- 只有存在**真实领域级 handler** 的 boundary 才计入 `_REAL_HANDLERS`，
  `is_implemented_boundary()` 以 `_REAL_HANDLERS` 为唯一权威（不再用
  `boundary in ALL_BOUNDARIES` 形式化判定）。
- 主链四 boundary 通过 orchestrator 断点恢复（设置 last_completed_step）真实续跑。
- 子产品 boundary 查找当日对应已完成的源 run/snapshot（真实 Model 字段，非 `.c`），
  创建 child SchedulerJobRun（幂等：按 run_key 复用已有 child，避免唯一键冲突），
  调用对应 publish/重建函数；成功置 succeeded+finished_at，失败置 failed+错误事件。
- 任何真实调用失败：捕获异常并写入 manual_restart 事件（level=error），任务以 failed 终态
  记录真实原因；绝不返回 501，也绝不伪造成功。

当前真实 handler 覆盖：
- 主链：daily_ready / board_facts / core / stock_core_published（4）
- 子产品：dsa_projection / chip / auction / board_aggregation / review（5）
- **state_events：无独立重建入口，未计入 _REAL_HANDLERS（诚实标记未实现，需补领域级 handler）**

测试策略：纯单元测试注入 fake db + publishers 验证 dispatch/child/幂等/事件逻辑
（PURE_UNIT_TEST）；真实 PG publish 路径在远程验证库首跑验证（Phase 4）。
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
    "daily_ready": "checking_coverage",       # 用已有日线从 core 链开始
    "board_facts": "refreshing_daily",        # 只重跑 board，跳日线刷新
    "core": "checking_coverage",              # 新建 core run 算 trend/structure/momentum
    "stock_core_published": "publishing",      # 重试 core publication
}

_MAINCHAIN_BOUNDARIES = set(_MAINCHAIN_RESUME_STEP.keys())

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
    """子产品 boundary 的发布/重建回调（真实路径或单测注入）。"""

    async def __call__(
        self, db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str
    ) -> uuid.UUID | None:
        ...


async def _find_source_run_id(
    db: AsyncSession,
    model: Any,
    trade_date: str,
    run_type: str | None = None,
    statuses: tuple[str, ...] = ("succeeded", "completed", "partial_success", "published"),
) -> uuid.UUID | None:
    """通用查找当日已完成源 run id（按 created_at desc 取最新）。

    使用真实 ORM Model 字段（model.id / model.trade_date / model.status），非 `.c`。
    """
    stmt = select(model.id).where(model.trade_date == trade_date)
    if run_type is not None and hasattr(model, "run_type"):
        stmt = stmt.where(model.run_type == run_type)
    if statuses and hasattr(model, "status"):
        stmt = stmt.where(model.status.in_(statuses))
    stmt = stmt.order_by(model.created_at.desc()).limit(1)
    row = (await db.execute(stmt)).first()
    return row[0] if row else None


async def _find_existing_child(
    db: AsyncSession, run_key: str
) -> SchedulerJobRun | None:
    """幂等：查询已有同 run_key 的 child，存在则复用（避免唯一键冲突 / 重复任务）。"""
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.run_key == run_key)
        .order_by(SchedulerJobRun.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _create_or_reuse_child(
    db: AsyncSession,
    *,
    parent_job_run_id: uuid.UUID,
    boundary: str,
    trade_date: str,
    target_run_id: uuid.UUID | None,
    actor: str,
) -> SchedulerJobRun:
    """为子产品 boundary 创建 child SchedulerJobRun（幂等：run_key 复用已有）。"""
    run_key = f"granular_restart:{trade_date}:{boundary}"
    existing = await _find_existing_child(db, run_key)
    if existing is not None:
        # 幂等复用：已成功则直接返回；已失败则重置为 queued 允许重排；其余复用不新建
        await append_event(
            db,
            job_run_id=existing.id,
            step="manual_restart",
            level="info",
            message=f"granular restart 幂等复用: boundary={boundary}, 已有 run_key={run_key}, status={existing.status}",
            payload={"reused": True, "parent_job_run_id": str(parent_job_run_id)},
        )
        if existing.status == "failed":
            existing.status = "queued"
            existing.error_code = None
            existing.error_message = None
            await db.flush()
        return existing

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
    """V2.1 真实调度一个 granular restart boundary。

    返回被复用的父 SchedulerJobRun（主链 boundary）或新建/复用的 child SchedulerJobRun（子产品 boundary）。
    未知 boundary 或 state_events（无真实 handler）将明确报错，不伪造成功。
    """
    if restart_from not in ALL_BOUNDARIES:
        raise ValueError(f"未知 restart_from boundary: {restart_from}")
    if not is_implemented_boundary(restart_from):
        raise NotImplementedError(
            f"restart_from={restart_from} 无真实领域级 handler，未实现（不得伪造成功）"
        )

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

    # ---- 子产品 boundary：查找源 run + 创建/复用 child + 调用真实 handler ----
    source_run_id = await _resolve_source_run_id(db, restart_from, trade_date)
    child = await _create_or_reuse_child(
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
        # 防御：理论上不会到达（is_implemented_boundary 已拦截），仍兜底报错。
        child.status = "failed"
        child.error_code = "no_real_handler"
        child.error_message = f"boundary={restart_from} 无真实 handler"
        await db.commit()
        raise NotImplementedError(f"boundary={restart_from} 无真实 handler")

    try:
        child.status = "running"
        child.started_at = datetime.now(timezone.utc)
        await db.flush()
        new_run_id = await publisher(
            db, trade_date=trade_date, source_run_id=source_run_id, actor=actor
        )
        child.status = "succeeded"
        child.finished_at = datetime.now(timezone.utc)
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
        child.finished_at = datetime.now(timezone.utc)
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
    """查找子产品 boundary 重建所需的源 run/snapshot id（真实 Model 字段）。"""
    if boundary == "dsa_projection":
        from app.models.strategy_run import StrategyRun

        return await _find_source_run_id(db, StrategyRun, trade_date, run_type="dsa_selector")
    if boundary == "chip":
        from app.models.chip_consensus_run import ChipConsensusRun

        return await _find_source_run_id(
            db, ChipConsensusRun, trade_date, statuses=("succeeded", "partial_success")
        )
    if boundary == "auction":
        from app.models.auction import AuctionAnchorSnapshot

        return await _find_source_run_id(db, AuctionAnchorSnapshot, trade_date, statuses=("succeeded",))
    if boundary == "board_aggregation":
        from app.models.board_analysis_snapshot import BoardAnalysisSnapshot

        return await _find_source_run_id(db, BoardAnalysisSnapshot, trade_date, statuses=("succeeded",))
    if boundary == "review":
        from app.models.market_review import MarketReviewRun

        return await _find_source_run_id(db, MarketReviewRun, trade_date, statuses=("succeeded",))
    # state_events：无独立源 run 表（需 worker 从 core artifact 重建），返回 None
    return None


# 真实发布回调（仅在远程验证库路径调用；本地 PURE_UNIT_TEST 通过 publishers 注入覆盖）
async def _publish_dsa_projection(db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
    from app.services.strategy_batch_service import StrategyBatchService

    if source_run_id is None:
        raise RuntimeError("dsa_projection 缺少源 dsa_selector StrategyRun id")
    await StrategyBatchService(db).publish_run(source_run_id)
    return source_run_id


async def _publish_chip(db: AsyncSession, *, trade_date: str, source_run_id: uuid.UUID | None, actor: str) -> uuid.UUID | None:
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
    from app.models.board_analysis_snapshot import BoardAnalysisSnapshot
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


# [Corrective Pass 2] 只有存在真实领域级 handler 的 boundary 才算 implemented。
# state_events 无独立重建入口，明确不计入（诚实标记未实现）。
_REAL_HANDLERS: dict[str, _Publisher] = {
    "daily_ready": _MAINCHAIN_RESUME_STEP.__getitem__,  # 占位，主链走续跑分支，不进此表
    "board_facts": _MAINCHAIN_RESUME_STEP.__getitem__,
    "core": _MAINCHAIN_RESUME_STEP.__getitem__,
    "stock_core_published": _MAINCHAIN_RESUME_STEP.__getitem__,
    "dsa_projection": _publish_dsa_projection,
    "chip": _publish_chip,
    "auction": _publish_auction,
    "board_aggregation": _publish_board_aggregation,
    "review": _publish_review,
    # state_events: 故意缺失（需补领域级重建 handler）
}
# 移除主链占位（主链由 _MAINCHAIN_BOUNDARIES 处理，不依赖 _REAL_HANDLERS 值）
for _b in _MAINCHAIN_BOUNDARIES:
    _REAL_HANDLERS.pop(_b, None)


def is_implemented_boundary(boundary: str) -> bool:
    """[Corrective Pass 2] 以真实 handler registry 为唯一权威，禁止枚举即实现。"""
    if boundary in _MAINCHAIN_BOUNDARIES:
        return True
    return boundary in _REAL_HANDLERS
