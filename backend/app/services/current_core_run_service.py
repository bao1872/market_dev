"""CURRENT canonical CoreRun 解析（单一 service-level owner）。

CURRENT 盘后主链为 Core → History。正常 AfterClose DAG 在 ``finalize_snapshot_run_compute_complete``
之后即把 ``StockFeatureSnapshotRun.status`` 置为 ``succeeded``（计算终态），并不进入旧
``PUBLISHING`` 阶段；因此**每天真正持续推进的事实是 ``StockFeatureSnapshotRun``**，而非
``FactorPublication(stock_core)``。

本模块是 CURRENT canonical CoreRun 的**唯一**解析入口，供 ``app.api.stock_context``
（/first-pyramid 端点）与 ``app.services.market_stocks_service``（/market/stocks）共同调用，
避免在两处各写一套（那正是上一次事故中 filter/sort 与 display 分裂的成因）。

CURRENT lineage（canonical after-close CoreRun）：

    StockFeatureSnapshotRun
      WHERE run_type == after_close
        AND status == succeeded
        AND schema_version == 当前 _SCHEMA_VERSION
        AND finished_at IS NOT NULL
        AND metadata_["scope"] == "full"
      ORDER BY trade_date DESC, finished_at DESC, created_at DESC
      LIMIT 1

legacy ``FactorPublication(kind=stock_core)`` 仅保留为历史/兼容数据，**不再决定 CURRENT 身份**。
解析路径里不得再查询 FactorPublication，也不得先 SELECT id 再 ``session.get``。
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_feature_snapshot_run import (
    RUN_TYPE_AFTER_CLOSE,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.feature_snapshot_service import _SCHEMA_VERSION

logger = logging.getLogger(__name__)


async def resolve_current_core_run(
    session: AsyncSession,
    as_of: date | None = None,
) -> StockFeatureSnapshotRun | None:
    """解析 CURRENT canonical CoreRun（最新 canonical after-close CoreRun 为唯一 authority）。

    CURRENT 第一金字塔 / Core 身份严格限定为：

    - ``run_type == after_close``
    - ``status == succeeded``
    - ``schema_version == 当前 _SCHEMA_VERSION``
    - ``finished_at IS NOT NULL``
    - ``metadata_["scope"] == "full"``

    排序优先级：``trade_date DESC`` > ``finished_at DESC`` > ``created_at DESC``。

    Args:
        session: 异步 DB 会话
        as_of: 截止日期（point-in-time，含当天）。None 表示取最新合法 CoreRun。
            提供时只返回 ``trade_date <= as_of`` 的 run，绝不返回未来 run。

    Returns:
        满足全部条件的 ``StockFeatureSnapshotRun``；无合法 run 时返回 ``None``（fail-closed）。

    性能契约：单次 DB query；0 次 ``session.get``；0 次 FactorPublication 查询；0 次计算。
    """
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            StockFeatureSnapshotRun.run_type == RUN_TYPE_AFTER_CLOSE,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            StockFeatureSnapshotRun.schema_version == _SCHEMA_VERSION,
            StockFeatureSnapshotRun.finished_at.is_not(None),
            StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        )
    )
    if as_of is not None:
        stmt = stmt.where(StockFeatureSnapshotRun.trade_date <= as_of)
    stmt = stmt.order_by(
        StockFeatureSnapshotRun.trade_date.desc(),
        StockFeatureSnapshotRun.finished_at.desc(),
        StockFeatureSnapshotRun.created_at.desc(),
    ).limit(1)

    return (await session.execute(stmt)).scalar_one_or_none()
