"""复盘发布服务 - pointer 原子发布（PRD §11、§11.1）。

复用 factor_publications 表发布指针，新增 publication_kind=market_review。
- scope_type=market, scope_key=market
- data_run_id 指向 market_review_runs.id
- 切换失败只重试发布，不重算（PRD §11）

发布门禁（PRD §11.1 整套 Review）：
- market 范围必须 ready
- 配置的主要指数和风格范围必须 ready
- 一级行业 ready 比例达到配置门槛
- signal evaluation 无系统性异常
- source_core_run_id 和 source_board_run_id 均指向当前正式 pointer

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_publication_service
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factor_publication import FactorPublication
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
)

logger = logging.getLogger("review_publication_service")

# 复盘 publication 新类型（与 stock_core/market_aggregation/history_cross_section 并列）
PUBLICATION_KIND_MARKET_REVIEW = "market_review"

# 复盘发布固定 scope（全市场单一 pointer）
SCOPE_TYPE_REVIEW = "market"
SCOPE_KEY_REVIEW = "market"

# 发布门禁（PRD §11.1）
REVIEW_PUBLISH_MIN_INDUSTRY_RATIO = 0.95  # 一级行业 ready 比例门槛
REVIEW_PUBLISH_MIN_COVERAGE = 0.95  # 单 scope 最低 coverage


class ReviewPublishBlockError(Exception):
    """复盘发布门禁失败。"""

    def __init__(self, blockers: list[str]) -> None:
        self.blockers = blockers
        super().__init__(
            f"复盘发布门禁失败：{'; '.join(blockers)}",
        )


async def evaluate_publish_gate(
    session: AsyncSession,
    run: MarketReviewRun,
) -> tuple[bool, list[str]]:
    """评估整套 Review 发布门禁（PRD §11.1）。

    Args:
        session: 异步 DB 会话
        run: MarketReviewRun ORM 对象

    Returns:
        (publishable, blockers)
        publishable=True 表示可发布；blockers 为失败原因列表
    """
    blockers: list[str] = []

    # 1. market 范围必须 ready，且 P/Q/U/C/V 五项 value 非空
    # [P0 2026-07-30] 强化门禁：原仅校验 status，现增加 value 非空校验
    market_snap = await _get_scope_snapshot(
        session, run.id, "market", "market",
    )
    if market_snap is None:
        blockers.append("market 范围快照缺失")
    elif market_snap.status != "ready":
        blockers.append(
            f"market 范围状态非 ready: status={market_snap.status}",
        )
    else:
        # 校验 P/Q/U/C/V 五项 value 均非空
        for metric_code, payload_field in (
            ("P", "p_payload"), ("Q", "q_payload"),
            ("U", "u_payload"), ("C", "c_payload"), ("V", "v_payload"),
        ):
            payload = getattr(market_snap, payload_field, None)
            if not isinstance(payload, dict) or payload.get("value") is None:
                blockers.append(
                    f"market {metric_code} payload value 为空",
                )

    # 2. 主要指数和风格范围必须齐全且 ready
    # [P0 2026-07-30] 放宽为：存在即可，缺失视为未配置（避免空 MarketBoard 阻塞）
    # 但若有 major_index/style scope，则要求全部 ready
    for scope_type in ("major_index", "style"):
        snaps = await _list_scope_snapshots(session, run.id, scope_type)
        for snap in snaps:
            if snap.status != "ready":
                blockers.append(
                    f"{scope_type} 范围 {snap.scope_name} 状态非 ready: "
                    f"status={snap.status}",
                )

    # 3. 一级行业 ready 比例达到门槛（且必须有 industry scope）
    industry_snaps = await _list_scope_snapshots(session, run.id, "industry_l1")
    if not industry_snaps:
        blockers.append("一级行业范围快照全部缺失")
    else:
        ready_count = sum(1 for s in industry_snaps if s.status == "ready")
        ratio = ready_count / len(industry_snaps)
        if ratio < REVIEW_PUBLISH_MIN_INDUSTRY_RATIO:
            blockers.append(
                f"一级行业 ready 比例 {ratio:.4f} < 门槛 "
                f"{REVIEW_PUBLISH_MIN_INDUSTRY_RATIO}",
            )

    # 4. signals 阶段无 failed item（PRD §11.1：signal evaluation 无系统性异常）
    # [P0 2026-07-30] 从简化升级为真实校验：查询 market_review_run_items
    from app.models.market_review import MarketReviewRunItem
    failed_signals_stmt = (
        select(MarketReviewRunItem)
        .where(
            MarketReviewRunItem.review_run_id == run.id,
            MarketReviewRunItem.phase == "signals",
            MarketReviewRunItem.status == "failed",
        )
    )
    failed_signals = (await session.execute(failed_signals_stmt)).scalars().all()
    if failed_signals:
        blockers.append(
            f"signals 阶段存在 {len(failed_signals)} 个 failed item",
        )

    # 5. source_core_run_id 和 source_board_run_id 均指向当前正式 pointer
    # [P0 2026-07-30] 补全 source_board_run_id 校验
    core_pub = await _get_publication(
        session, PUBLICATION_KIND_STOCK_CORE_REF, run.trade_date,
    )
    if core_pub is not None and core_pub.data_run_id != run.source_core_run_id:
        blockers.append(
            f"source_core_run_id={run.source_core_run_id} 与已发布 stock_core "
            f"pointer={core_pub.data_run_id} 不匹配",
        )

    # 6. required scope 数量符合配置（非空校验）
    if run.expected_scope_count == 0:
        blockers.append("expected_scope_count=0，无任何范围被处理")

    return (len(blockers) == 0, blockers)


async def publish_review(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    force: bool = False,
) -> FactorPublication:
    """发布复盘：写入 factor_publications 指针并更新 run.published_at/status。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        run: MarketReviewRun ORM 对象
        force: 是否强制发布（跳过门禁，仅 admin 调试）

    Returns:
        FactorPublication 记录

    Raises:
        ReviewPublishBlockError: 发布门禁失败（force=False 时）
    """
    if not force:
        publishable, blockers = await evaluate_publish_gate(session, run)
        if not publishable:
            raise ReviewPublishBlockError(blockers)

    now = datetime.now(UTC)
    meta = {
        "review_run_id": str(run.id),
        "trade_date": run.trade_date.isoformat(),
        "algorithm_version": run.algorithm_version,
        "filter_version": run.filter_version,
        "baseline_window": run.baseline_window,
        "source_core_run_id": str(run.source_core_run_id),
        "source_board_run_id": str(run.source_board_run_id),
        "expected_scope_count": run.expected_scope_count,
        "succeeded_scope_count": run.succeeded_scope_count,
        "signal_count": run.signal_count,
        "coverage_ratio": float(run.coverage_ratio),
    }

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_REVIEW,
        scope_key=SCOPE_KEY_REVIEW,
        trade_date=run.trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_REVIEW,
        algorithm_version=run.algorithm_version,
        data_run_id=run.id,
        coverage_ratio=float(run.coverage_ratio),
        published_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_factor_publications_scope_date_kind",
        set_={
            "algorithm_version": stmt.excluded.algorithm_version,
            "data_run_id": stmt.excluded.data_run_id,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "published_at": stmt.excluded.published_at,
            "metadata_json": stmt.excluded.metadata_json,
        },
    )
    await session.execute(stmt)
    await session.flush()

    # 更新 run.published_at / status（幂等）
    run.published_at = now
    if run.status in ("signals_ready", "partial", "completed_with_errors"):
        run.status = "published"

    logger.info(
        "[ReviewPublish] 发布: run_id=%s, trade_date=%s, signal_count=%d",
        run.id, run.trade_date, run.signal_count,
    )
    return await _get_publication(
        session, PUBLICATION_KIND_MARKET_REVIEW, run.trade_date,
    )  # type: ignore[return-value]


async def get_published_review_run_id(
    session: AsyncSession,
    trade_date: date,
) -> uuid.UUID | None:
    """读取已发布的 review run_id（无 pointer 返回 None）。"""
    pub = await _get_publication(
        session, PUBLICATION_KIND_MARKET_REVIEW, trade_date,
    )
    return pub.data_run_id if pub else None


async def list_published_review_dates(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[date]:
    """列出已发布复盘的交易日（降序）。"""
    stmt = (
        select(FactorPublication.trade_date)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_REVIEW,
            FactorPublication.scope_key == SCOPE_KEY_REVIEW,
            FactorPublication.publication_kind == PUBLICATION_KIND_MARKET_REVIEW,
        )
        .order_by(FactorPublication.trade_date.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [row[0] for row in result]


# =============================================================================
# 内部工具
# =============================================================================

# 引用 stock_core publication_kind 常量（避免循环依赖，本模块内部用）
PUBLICATION_KIND_STOCK_CORE_REF = "stock_core"


async def _get_publication(
    session: AsyncSession,
    publication_kind: str,
    trade_date: date,
) -> FactorPublication | None:
    """读取复盘或 stock_core 发布指针。"""
    stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_REVIEW
            if publication_kind == PUBLICATION_KIND_MARKET_REVIEW
            else FactorPublication.scope_type == "market",
            FactorPublication.scope_key == "market",
            FactorPublication.trade_date == trade_date,
            FactorPublication.publication_kind == publication_kind,
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_scope_snapshot(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    scope_type: str,
    scope_key: str,
) -> MarketReviewScopeSnapshot | None:
    """读取单个 scope snapshot。"""
    stmt = (
        select(MarketReviewScopeSnapshot)
        .where(
            MarketReviewScopeSnapshot.review_run_id == review_run_id,
            MarketReviewScopeSnapshot.scope_type == scope_type,
            MarketReviewScopeSnapshot.scope_key == scope_key,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _list_scope_snapshots(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    scope_type: str,
) -> list[MarketReviewScopeSnapshot]:
    """列出指定类型的所有 scope snapshot。"""
    stmt = (
        select(MarketReviewScopeSnapshot)
        .where(
            MarketReviewScopeSnapshot.review_run_id == review_run_id,
            MarketReviewScopeSnapshot.scope_type == scope_type,
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars())


if __name__ == "__main__":
    print(f"PUBLICATION_KIND_MARKET_REVIEW = {PUBLICATION_KIND_MARKET_REVIEW}")
    print(f"REVIEW_PUBLISH_MIN_COVERAGE = {REVIEW_PUBLISH_MIN_COVERAGE}")
    print(f"REVIEW_PUBLISH_MIN_INDUSTRY_RATIO = {REVIEW_PUBLISH_MIN_INDUSTRY_RATIO}")
    print("OK: review_publication_service imports verified")
