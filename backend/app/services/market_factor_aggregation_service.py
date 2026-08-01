"""市场因子聚合服务 - 基于 stock_core pointer 的独立 job。

设计目标（ref/instruction.md §三.5）：
1. 只读已发布 stock_core pointer 指向的 run
2. 成功后切换 market_aggregation pointer
3. 失败只重跑聚合，不回滚核心
4. 与核心解耦：独立 job，不依赖 after_close_orchestrator 主链

当前实现：
- 未提供 run ID 时执行完整 Board batch 计算并发布真实 batch identity
- 提供 run ID 时仅允许发布已存在且通过门禁的 BoardAnalysisRun

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.market_factor_aggregation_service
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.factor_publication_service import (
    PUBLICATION_KIND_STOCK_CORE,
    publish_market_aggregation,
)

logger = logging.getLogger(__name__)


async def run_market_factor_aggregation(
    session: AsyncSession,
    trade_date: date,
    *,
    algorithm_version: str,
    aggregation_run_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """[CHANGE-20260729-008] 市场因子聚合独立 job。

    流程：
    1. 读取已发布 stock_core pointer（必须存在）
    2. 校验 source_core_run_id 与 pointer 一致
    3. 切换 market_aggregation pointer（原子）

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        trade_date: 业务交易日
        algorithm_version: 算法版本
        aggregation_run_id: 已存在的真实 BoardAnalysisRun ID；None 时计算完整 batch
        metadata: 额外元数据

    Returns:
        {
            "aggregation_run_id": str,
            "source_core_run_id": str,
            "trade_date": str,
            "published_at": str,
        }

    Raises:
        ValueError: 无已发布 stock_core pointer 或校验失败
    """
    if aggregation_run_id is None:
        from app.services.board_analysis_service import compute_all_boards

        result = await compute_all_boards(
            session,
            trade_date,
            publish=True,
            algorithm_version=algorithm_version,
        )
        if not result.get("published"):
            raise ValueError(
                "market_aggregation 发布失败: Board batch 未通过完整性门禁 "
                f"status={result.get('status')}",
            )
        return result

    from app.services.factor_publication_service import get_publication

    core_pointer = await get_publication(
        session,
        scope_type="market",
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
    )
    if core_pointer is None:
        raise ValueError(
            f"market_aggregation 失败: trade_date={trade_date} 无已发布 stock_core pointer，"
            "必须先发布 stock_core",
        )
    source_core_run_id = core_pointer.data_run_id
    pub = await publish_market_aggregation(
        session,
        trade_date=trade_date,
        source_core_run_id=source_core_run_id,
        aggregation_run_id=aggregation_run_id,
        algorithm_version=algorithm_version,
        metadata={
            "aggregation_job": "market_factor_aggregation_service",
            "source_core_published_at": (
                core_pointer.published_at.isoformat()
                if core_pointer.published_at else None
            ),
            **(metadata or {}),
        },
    )
    logger.info(
        "[MarketAggregation] 发布既有 Board batch: trade_date=%s, core=%s, batch=%s",
        trade_date,
        source_core_run_id,
        aggregation_run_id,
    )
    return {
        "aggregation_run_id": str(aggregation_run_id),
        "source_core_run_id": str(source_core_run_id),
        "trade_date": trade_date.isoformat(),
        "published_at": pub.published_at.isoformat() if pub.published_at else None,
    }


if __name__ == "__main__":
    print("OK: market_factor_aggregation_service imports verified")
    print("    run_market_factor_aggregation(session, trade_date, algorithm_version=...)")
