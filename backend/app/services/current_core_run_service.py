"""CURRENT canonical CoreRun 解析（单一 service-level owner）。

CURRENT 盘后主链为 Core → History。旧 Review 产品已退役，复盘现由 Market Dashboard
projection 承载，不再是 Core 的发布 gate。

本模块是 CURRENT canonical CoreRun 的**唯一**解析入口，供 ``app.api.stock_context``
与 ``app.services.market_stocks_service`` 共同调用，避免在两处各写一套（那正是上一次
事故中 filter/sort 与 display 分裂的成因）。

CURRENT lineage（Core Compute Once → Core Ready → Market Dashboard Review）：

    formal stock_core FactorPublication pointer（live: superseded_by IS NULL）
      → data_run_id
      → StockFeatureSnapshotRun（canonical CoreRun）
      → 校验 succeeded / trade_date 一致 / schema_version

只读正式 pointer，不调用 legacy ``publish_stock_core``，不回退到任意 latest
succeeded CoreRun。
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.stock_feature_snapshot_run import (
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.feature_snapshot_service import _SCHEMA_VERSION

logger = logging.getLogger(__name__)


async def resolve_current_core_run(
    session: AsyncSession,
    as_of: date | None = None,
) -> StockFeatureSnapshotRun | None:
    """解析 CURRENT canonical CoreRun（正式 stock_core pointer 为唯一 authority）。

    Args:
        session: 异步 DB 会话
        as_of: 截止日期（point-in-time，含当天）。None 表示取最新正式 pointer。

    Returns:
        通过全部 lineage 校验的 ``StockFeatureSnapshotRun``；
        任一环节不成立返回 ``None``（fail-closed）。

        **禁止**回退到 arbitrary latest succeeded CoreRun。
    """
    pub_stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
            FactorPublication.scope_type == "market",
            FactorPublication.scope_key == "market",
            FactorPublication.superseded_by.is_(None),
        )
        .order_by(FactorPublication.trade_date.desc())
    )
    pub_rows = (await session.execute(pub_stmt)).scalars().all()
    if as_of is not None:
        pub_rows = [p for p in pub_rows if p.trade_date <= as_of]
    if not pub_rows:
        logger.info(
            "[current-core] 无 live stock_core pointer，CURRENT Core 解析失败 as_of=%s",
            as_of,
        )
        return None

    # pub_rows 为降序，首项即 point-in-time 下最新的正式 stock_core pointer
    pub = pub_rows[0]
    core_run_id = pub.data_run_id
    if core_run_id is None:
        logger.error(
            "[current-core] stock_core pointer trade_date=%s data_run_id 为空，fail-closed",
            pub.trade_date,
        )
        return None

    core_run = await session.get(StockFeatureSnapshotRun, core_run_id)
    if core_run is None:
        logger.error(
            "[current-core] stock_core pointer data_run_id=%s 不存在，fail-closed",
            core_run_id,
        )
        return None

    if core_run.status != STATUS_SUCCEEDED:
        logger.error(
            "[current-core] Core run=%s status=%s != succeeded，fail-closed",
            core_run_id, core_run.status,
        )
        return None

    if core_run.trade_date != pub.trade_date:
        logger.error(
            "[current-core] Core run=%s trade_date=%s 与 pointer trade_date=%s 不一致，fail-closed",
            core_run_id, core_run.trade_date, pub.trade_date,
        )
        return None

    if core_run.schema_version != _SCHEMA_VERSION:
        logger.error(
            "[current-core] Core run=%s schema_version=%s != %s，fail-closed",
            core_run_id, core_run.schema_version, _SCHEMA_VERSION,
        )
        return None

    return core_run
