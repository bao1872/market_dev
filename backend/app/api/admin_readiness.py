"""管理员 API 路由 - ProductReadiness 就绪状态 + 治理报告（Commit G）。

端点：
- GET /v1/admin/readiness/{trade_date}: 查询指定交易日的九节点就绪状态、闭包评估与治理报告

权限：
- 需要 admin 角色（RBAC）

用途：
- Admin 盘后工作台展示九节点状态（daily_refresh/board_facts/stock_core/dsa_projection/
  chip/state_events/auction_anchor/board_aggregation/review）
- 展示 terminal 与 consumable 分离、closure（pending/blocked/core_ready/degraded_ready/
  fully_ready）、unmatched active child、stale child、pointer lineage、degraded reasons
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_errors import admin_bad_request
from app.core.deps import get_db, require_roles
from app.schemas.product_readiness import (
    BenefitsIssueDTO,
    GovernanceReportDTO,
    ProductReadinessDTO,
    ProductReadinessResponse,
)
from app.services.product_readiness_service import (
    ProductReadinessService,
    evaluate_closure,
    evaluate_governance,
)

logger = logging.getLogger("admin_readiness")

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-readiness"],
)


def _to_dto(state, lineage: dict) -> ProductReadinessDTO:
    """将 ProductReadinessState 映射为 API DTO。

    [Corrective-3 §三/§四] 透传统一结构 lineage，并把后端权威治理动作
    （reasonCode/retryable/recommendedAction/operation/targetRunId）提升为
    一级 DTO 字段。前端只展示，不得再自行解释 reason code。
    """
    target = lineage.get("target_run_id")
    return ProductReadinessDTO(
        product=state.product,
        readiness=state.readiness,
        freshness=state.freshness,
        isMandatory=state.is_mandatory,
        isTerminal=state.is_terminal,
        isConsumable=state.is_consumable,
        dataSource=lineage.get("source_type", "unknown"),
        lineage=lineage,
        reasonCode=str(lineage.get("reason_code", "NONE")),
        retryable=bool(lineage.get("retryable", False)),
        recommendedAction=str(lineage.get("recommended_action", "none")),
        operation=str(lineage.get("operation", "no_operation")),
        targetRunId=str(target) if target else None,
    )


@router.get(
    "/readiness/{trade_date}",
    response_model=ProductReadinessResponse,
)
async def get_product_readiness_endpoint(
    trade_date: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> ProductReadinessResponse:
    """查询指定交易日的产品就绪状态 + 治理报告。

    流程：
    1. 解析 trade_date
    2. ProductReadinessService.evaluate_for_trade_date 聚合九节点状态并求闭包
    3. evaluate_governance 生成治理报告（pointer lineage / stale / unmatched / degraded）
    4. 组装响应

    Args:
        trade_date: 业务交易日（YYYY-MM-DD）
        db: 异步数据库会话
        current_user: 当前管理员（由 require_roles 注入）

    Returns:
        ProductReadinessResponse（九节点明细 + 闭包 + 治理报告）
    """
    try:
        trade_date_obj = date.fromisoformat(trade_date)
    except ValueError:
        raise admin_bad_request(
            "readiness_invalid_trade_date",
            f"trade_date 格式错误（需 YYYY-MM-DD）: {trade_date}",
            retryable=False,
            recommended_action="重新提交 YYYY-MM-DD 格式的交易日",
        ) from None

    service = ProductReadinessService()
    # 动态聚合：读取九节点 run/publication/pointer 并映射为就绪状态
    states = await service.collect_states(db, trade_date_obj)
    closure = evaluate_closure(states)
    governance = evaluate_governance(states, closure)

    # 组装产品明细（含真实 lineage 血缘）
    products = [
        _to_dto(state, governance.pointer_lineage.get(state.product, {}))
        for state in states
    ]

    return ProductReadinessResponse(
        tradeDate=trade_date_obj.isoformat(),
        closure=closure.closure,
        mandatoryProductsReady=closure.mandatory_products_ready,
        mandatoryProductsFullyFresh=closure.mandatory_products_full_fresh,
        enhancementJobsTerminal=closure.enhancement_jobs_terminal,
        products=products,
        governance=GovernanceReportDTO(
            pointerLineage=governance.pointer_lineage,
            staleChildren=governance.stale_children,
            unmatchedActiveChildren=governance.unmatched_active_children,
            readyProducts=governance.ready_products,
            pendingProducts=governance.pending_products,
            blockedProducts=governance.blocked_products,
            unavailableProducts=governance.unavailable_products,
            degradedReasons=[
                BenefitsIssueDTO(
                    product=i.get("product", ""),
                    code=i.get("code", ""),
                    severity=i.get("severity", "warning"),
                    recommendedAction=i.get("recommendedAction", ""),
                )
                for i in closure.issues
            ],
        ),
    )
