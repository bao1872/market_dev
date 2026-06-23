"""[LEGACY] 选股组合结果查询仓储（C4 read side）- 已弃用，保留仅供参考。

新架构使用 selector_query_service 统一查询已发布的选股策略结果，
不再使用 SelectionPlanRun/Result/Evidence 模型。

选股组合结果查询仓储（C4 read side）。

提供已持久化的 SelectionPlanRun/Result/Evidence 查询能力，
供 C9 消息决策服务消费选股组合结果。

设计说明：
- 只读查询，不写入（写入由 selection_run_service 负责）
- 所有异常补充上下文后 re-raise（禁异常吞没）
- 按 plan_run_id 查询 matched=True 的结果，支持 rank_value 排序

Inputs:
    session: AsyncSession
    plan_run_id: UUID

How to Run:
    python -m app.repositories.selection_result_repository    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.selection_plan import (
    SelectionPlan,
    SelectionPlanRevision,
)
from app.models.selection_plan_run import (
    SelectionPlanResult,
    SelectionPlanRun,
    SelectionResultEvidence,
)

logger = logging.getLogger("selection_result_repository")


async def get_run_with_plan_and_revision(
    session: AsyncSession,
    plan_run_id: UUID,
) -> tuple[SelectionPlanRun, SelectionPlan, SelectionPlanRevision] | None:
    """查询运行记录及其关联的方案与版本。

    Args:
        session: 异步会话
        plan_run_id: 运行 ID

    Returns:
        (SelectionPlanRun, SelectionPlan, SelectionPlanRevision) 或 None

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = (
        select(SelectionPlanRun, SelectionPlan, SelectionPlanRevision)
        .join(
            SelectionPlan,
            SelectionPlanRun.selection_plan_id == SelectionPlan.id,
        )
        .join(
            SelectionPlanRevision,
            SelectionPlanRun.revision_id == SelectionPlanRevision.id,
        )
        .where(SelectionPlanRun.id == plan_run_id)
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 selection_plan_run 关联失败 plan_run_id=%s: %s",
            plan_run_id, exc,
        )
        raise
    row = result.first()
    if row is None:
        return None
    return row[0], row[1], row[2]


async def list_results_by_run(
    session: AsyncSession,
    plan_run_id: UUID,
    matched_only: bool = True,
    limit: int = 20,
) -> list[SelectionPlanResult]:
    """查询运行结果（命中标的）。

    Args:
        session: 异步会话
        plan_run_id: 运行 ID
        matched_only: 仅返回 matched=True 的结果
        limit: 最大返回数（默认 20，用于消息展示 Top N）

    Returns:
        SelectionPlanResult 列表（按 rank_value 降序，NULL 排最后）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(SelectionPlanResult).where(
        SelectionPlanResult.plan_run_id == plan_run_id
    )
    if matched_only:
        stmt = stmt.where(SelectionPlanResult.matched.is_(True))
    stmt = stmt.order_by(
        SelectionPlanResult.rank_value.desc().nullslast()
    ).limit(limit)
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 selection_plan_results 失败 plan_run_id=%s: %s",
            plan_run_id, exc,
        )
        raise
    return list(result.scalars().all())


async def list_evidence_by_result(
    session: AsyncSession,
    selection_result_id: UUID,
) -> list[SelectionResultEvidence]:
    """查询结果的证据链。

    Args:
        session: 异步会话
        selection_result_id: 结果 ID

    Returns:
        SelectionResultEvidence 列表

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(SelectionResultEvidence).where(
        SelectionResultEvidence.selection_result_id == selection_result_id
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 selection_result_evidence 失败 result_id=%s: %s",
            selection_result_id, exc,
        )
        raise
    return list(result.scalars().all())


async def count_matched_results(
    session: AsyncSession,
    plan_run_id: UUID,
) -> int:
    """统计运行中命中标的总数。

    Args:
        session: 异步会话
        plan_run_id: 运行 ID

    Returns:
        命中数量

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    from sqlalchemy import func

    stmt = select(func.count(SelectionPlanResult.id)).where(
        SelectionPlanResult.plan_run_id == plan_run_id,
        SelectionPlanResult.matched.is_(True),
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "统计 matched results 失败 plan_run_id=%s: %s",
            plan_run_id, exc,
        )
        raise
    return int(result.scalar() or 0)


if __name__ == "__main__":
    # 自测入口：验证函数签名（不连 DB，无副作用）
    import inspect

    for fn in (
        get_run_with_plan_and_revision,
        list_results_by_run,
        list_evidence_by_result,
        count_matched_results,
    ):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        print(f"{fn.__name__} params={list(inspect.signature(fn).parameters.keys())}")
    print("OK")
