"""CURRENT canonical CoreRun 解析（单一 service-level owner）。

背景
----
AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01 之后，CURRENT 盘后主链为

    Core → Review → History

``stock_core FactorPublication`` 只是 **LEGACY compatibility**，不再被推进。
生产上该 pointer 停在 2026-08-26 / run ``ca5c3dd2``，而 Core snapshot 每天照常
succeeded。任何仍以该 pointer 为 CURRENT authority 的 consumer 都会被永久 pin
在 2026-08-26。

本模块是 CURRENT canonical CoreRun 的**唯一**解析入口，供
``app.api.stock_context`` 与 ``app.services.market_stocks_service`` 共同调用，
避免在两处各写一套（那正是上一次事故中 filter/sort 与 display 分裂的成因）。

CURRENT lineage（PRD31：Core Compute Once → Core Ready → Review(X) → History）：

    formal MarketReview publication
      → MarketReviewRun（formal guard）
      → MarketReviewRun.source_core_run_id
      → 校验 succeeded / trade_date 一致 / schema_version
      → StockFeatureSnapshotRun（canonical CoreRun）

复用 formal Review read owner，不复制其 publication 判定条件：
  - ``list_formally_published_review_dates``：FORMAL REVIEW READ OWNER
  - ``get_published_review_run_id``：LIVE POINTER RESOLVER
  - ``is_formally_published_review_run``：唯一布尔判定 owner
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_review import MarketReviewRun
from app.models.stock_feature_snapshot_run import (
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.feature_snapshot_service import _SCHEMA_VERSION
from app.services.review_publication_service import (
    get_published_review_run_id,
    is_formally_published_review_run,
    list_formally_published_review_dates,
)

logger = logging.getLogger(__name__)

# formal Review 交易日回看窗口（传给 list_formally_published_review_dates 的 limit）
CURRENT_CORE_REVIEW_LOOKBACK = 200


async def resolve_current_core_run(
    session: AsyncSession,
    as_of: date | None = None,
) -> StockFeatureSnapshotRun | None:
    """解析 CURRENT canonical CoreRun。

    Args:
        session: 异步 DB 会话
        as_of: 截止日期（point-in-time，含当天）。None 表示取最新正式 Review。

    Returns:
        通过全部 lineage 校验的 ``StockFeatureSnapshotRun``；
        任一环节不成立返回 ``None``（fail-closed）。

        **禁止**回退到 arbitrary latest succeeded CoreRun，
        **禁止**读取 ``stock_core`` FactorPublication 作为 CURRENT authority。
    """
    formal_dates = await list_formally_published_review_dates(
        session, limit=CURRENT_CORE_REVIEW_LOOKBACK,
    )
    if as_of is not None:
        formal_dates = [d for d in formal_dates if d <= as_of]
    if not formal_dates:
        logger.info(
            "[current-core] 无正式发布 Review，CURRENT Core 解析失败 as_of=%s", as_of,
        )
        return None

    # formal_dates 为降序，首项即 point-in-time 下最新的正式 Review 交易日
    review_trade_date = formal_dates[0]

    review_run_id = await get_published_review_run_id(session, review_trade_date)
    if review_run_id is None:
        logger.warning(
            "[current-core] 正式 Review 交易日 %s 无 live pointer，CURRENT Core 解析失败",
            review_trade_date,
        )
        return None

    review_run = await session.get(MarketReviewRun, review_run_id)
    if review_run is None:
        logger.error(
            "[current-core] Review pointer %s 指向不存在的 run=%s，fail-closed",
            review_trade_date, review_run_id,
        )
        return None

    if not is_formally_published_review_run(
        review_run, review_run_id, expected_trade_date=review_trade_date,
    ):
        logger.error(
            "[current-core] run=%s 不满足正式发布合同（status=%s, published_at=%s, "
            "run.trade_date=%s, expected=%s），fail-closed",
            review_run_id, review_run.status, review_run.published_at,
            review_run.trade_date, review_trade_date,
        )
        return None

    core_run_id = review_run.source_core_run_id
    if core_run_id is None:
        logger.error(
            "[current-core] Review run=%s 无 source_core_run_id，fail-closed", review_run_id,
        )
        return None

    core_run = await session.get(StockFeatureSnapshotRun, core_run_id)
    if core_run is None:
        logger.error(
            "[current-core] Review run=%s 的 source_core_run_id=%s 不存在，fail-closed",
            review_run_id, core_run_id,
        )
        return None

    if core_run.status != STATUS_SUCCEEDED:
        logger.error(
            "[current-core] Core run=%s status=%s != succeeded，fail-closed",
            core_run_id, core_run.status,
        )
        return None

    if core_run.trade_date != review_run.trade_date:
        logger.error(
            "[current-core] Core run=%s trade_date=%s 与 Review trade_date=%s 不一致，fail-closed",
            core_run_id, core_run.trade_date, review_run.trade_date,
        )
        return None

    if core_run.schema_version != _SCHEMA_VERSION:
        logger.error(
            "[current-core] Core run=%s schema_version=%s != %s，fail-closed",
            core_run_id, core_run.schema_version, _SCHEMA_VERSION,
        )
        return None

    return core_run
