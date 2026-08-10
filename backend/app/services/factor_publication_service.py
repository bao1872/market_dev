"""分层发布服务 - 覆盖率门禁 + 原子指针切换。

核心能力（ref/instruction.md §四/§八）：
1. compute_coverage: 计算 snapshot run 的覆盖率
2. publish_stock_core: 检查覆盖率门禁后原子切换 stock_core pointer
3. publish_market_aggregation: 切换 market_aggregation pointer
4. publish_history_cross_section: 切换 history_cross_section pointer
5. get_publication: 读取当前 pointer 指向的 data_run_id
6. get_published_snapshot_run_id: 兼容回退：无 pointer 时回退到 latest published run

设计原则：
- 发布不复制结果，只做小事务切换指针
- 指针更新失败只重试指针，不重新计算数据
- 覆盖率未达门禁时不切换正式市场列表
- 不同 run 的数据禁止混合

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.factor_publication_service
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factor_publication import (
    PUBLICATION_KIND_CHIP_CONSENSUS,
    PUBLICATION_KIND_HISTORY_CROSS_SECTION,
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    SCOPE_TYPE_MARKET,
    FactorPublication,
)
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.stock_feature_snapshot_run_item import (
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_RUNNING,
    ITEM_SKIPPED,
    ITEM_SUCCEEDED,
    PHASE_CORE,
    StockFeatureSnapshotRunItem,
)

logger = logging.getLogger("factor_publication_service")

# 覆盖率门禁：核心发布最低覆盖率（ref/instruction.md §四.2）
CORE_PUBLICATION_MIN_COVERAGE = 0.98

# 历史横截面覆盖率门禁
HISTORY_CROSS_SECTION_MIN_COVERAGE = 0.98


class CoverageBelowThresholdError(Exception):
    """覆盖率未达到发布门禁，拒绝切换指针。"""

    def __init__(self, coverage: float, threshold: float, run_id: uuid.UUID) -> None:
        self.coverage = coverage
        self.threshold = threshold
        self.run_id = run_id
        super().__init__(
            f"覆盖率 {coverage:.4f} 低于门禁 {threshold:.4f}，"
            f"拒绝发布 snapshot_run_id={run_id}"
        )


class PublicationAlreadyExistsError(Exception):
    """同 scope+date+kind 已有更新的 publication，拒绝降级。"""

    pass


async def compute_coverage(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID,
    *,
    phase: str = PHASE_CORE,
) -> dict[str, Any]:
    """计算 snapshot run 指定 phase 的覆盖率。

    覆盖率 = succeeded_count / expected_count
    expected_count = snapshot_run.expected_count（冻结后不漂移）

    Args:
        session: 异步 DB 会话
        snapshot_run_id: StockFeatureSnapshotRun.id
        phase: 阶段（默认 core）

    Returns:
        {
            "succeeded": int,
            "failed": int,
            "pending": int,
            "running": int,
            "skipped": int,
            "expected": int,
            "coverage": float,
        }
    """
    # 1. 从 snapshot_run 获取 expected_count
    snapshot_run = await session.get(StockFeatureSnapshotRun, snapshot_run_id)
    if snapshot_run is None:
        raise ValueError(f"StockFeatureSnapshotRun not found: {snapshot_run_id}")

    expected = snapshot_run.expected_count or 0

    # 2. 按 status 分组统计 run items
    stmt = (
        select(
            StockFeatureSnapshotRunItem.status,
            func.count(StockFeatureSnapshotRunItem.id).label("cnt"),
        )
        .where(
            StockFeatureSnapshotRunItem.snapshot_run_id == snapshot_run_id,
            StockFeatureSnapshotRunItem.phase == phase,
        )
        .group_by(StockFeatureSnapshotRunItem.status)
    )
    result = await session.execute(stmt)
    status_counts: dict[str, int] = {}
    for row in result:
        status_counts[row.status] = row.cnt

    succeeded = status_counts.get(ITEM_SUCCEEDED, 0)
    failed = status_counts.get(ITEM_FAILED, 0)
    pending = status_counts.get(ITEM_PENDING, 0)
    running = status_counts.get(ITEM_RUNNING, 0)
    skipped = status_counts.get(ITEM_SKIPPED, 0)

    # 覆盖率 = succeeded / expected
    coverage = succeeded / expected if expected > 0 else 0.0

    return {
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "running": running,
        "skipped": skipped,
        "expected": expected,
        "coverage": coverage,
    }


async def publish_stock_core(
    session: AsyncSession,
    trade_date: date,
    snapshot_run_id: uuid.UUID,
    algorithm_version: str,
    *,
    coverage: float | None = None,
    threshold: float = CORE_PUBLICATION_MIN_COVERAGE,
    metadata: dict[str, Any] | None = None,
) -> FactorPublication:
    """[LEGACY] 检查覆盖率门禁后切换 stock_core publication pointer。

    [CHANGE-20260806-005 / Phase 2] 本函数为**旧 two-phase 发布**，不写 supersede 历史、
    不审计、无 fencing。生产 scheduled 路径已统一到唯一原子入口
    `app.services.stock_core_publication_service.publish_stock_core_atomically`
    （quality gate + fencing + pointer + supersede + run 标记 + audit 同事务）。
    本函数仅保留供向后兼容测试（test_incremental_publication）使用，生产代码**不得**调用。

    流程：
    1. 计算 coverage（如未传入）
    2. coverage < threshold 抛 CoverageBelowThresholdError
    3. upsert FactorPublication（on_conflict_do_update）
    4. 返回 publication 记录

    指针更新失败只重试本函数，不重新计算数据。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        snapshot_run_id: StockFeatureSnapshotRun.id（数据版本）
        algorithm_version: 算法版本
        coverage: 预计算的覆盖率（None 时自动计算）
        threshold: 覆盖率门禁（默认 0.98）
        metadata: 额外元数据

    Returns:
        FactorPublication 记录

    Raises:
        CoverageBelowThresholdError: 覆盖率低于门禁
    """
    import json

    # 1. 计算覆盖率（如未传入）
    if coverage is None:
        cov_data = await compute_coverage(session, snapshot_run_id)
        coverage = cov_data["coverage"]

    # 2. 检查门禁
    if coverage < threshold:
        raise CoverageBelowThresholdError(coverage, threshold, snapshot_run_id)

    # 3. upsert publication pointer（原子切换）
    now = datetime.now(UTC)
    meta_str = json.dumps(metadata, ensure_ascii=False) if metadata else None

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
        algorithm_version=algorithm_version,
        data_run_id=snapshot_run_id,
        coverage_ratio=coverage,
        published_at=now,
        metadata_json=meta_str,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_key", "trade_date", "publication_kind"],
        index_where=text("superseded_by IS NULL"),
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

    # 读取 upsert 后的记录
    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
    )
    logger.info(
        "[Publication] stock_core 发布: trade_date=%s, snapshot_run_id=%s, "
        "coverage=%.4f, published_at=%s",
        trade_date, snapshot_run_id, coverage, now,
    )
    return pub  # type: ignore[return-value]


async def publish_market_aggregation(
    session: AsyncSession,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    aggregation_run_id: uuid.UUID,
    algorithm_version: str,
    *,
    degraded_publishable: bool = False,
    metadata: dict[str, Any] | None = None,
) -> FactorPublication:
    """切换 market_aggregation publication pointer。

    前置条件（[CHANGE-20260729-007] 严格校验 + PC-42 DEGRADED PUBLISHABLE）：
    - source_core_run_id 必须等于该日期已发布的 stock_core pointer.data_run_id
    - 聚合失败只重跑聚合，不回滚核心
    - Canonical publishability 由 board aggregation layer 的
      _evaluate_degraded_publishable 决定，并经 degraded_publishable 显式传入。
      本函数只消费该已确定的可发布证据，不在内部复制 degraded 判定逻辑。

    接受条件（PC-42）：
    - succeeded：完全成功批次，正常发布。
    - partial AND degraded_publishable=True：board aggregation 已判定为
      degradation_only 的可降级发布批次（每 board 真实 coverage/eligible/
      ready/missing，无 execution/DB/contract failure，无 UNKNOWN）。
      合法 partial 被发布后，board_run.status 仍保留 partial，不得改写为
      succeeded；market pointer 只代表 formal/current/publishable，不代表
      所有 board 都 succeeded。
    - 其余一律拒绝：failed / blocked / execution-failed partial /
      not-computed partial / 无 canonical degraded_publishable 证据的 partial。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        source_core_run_id: 源 stock_core snapshot_run_id（必须匹配已发布 pointer）
        aggregation_run_id: 真实 board_analysis_runs.id
        algorithm_version: 算法版本
        degraded_publishable: 由 board aggregation layer 判定并传入的
            canonical 可降级发布证据（status=partial 时生效）
        metadata: 额外元数据

    Raises:
        ValueError: source_core_run_id 与已发布 stock_core pointer 不匹配
        ValueError: board batch 不满足发布前置条件
    """
    import json

    from app.models.board_analysis_snapshot import BoardAnalysisRun

    # [CHANGE-20260729-007] 严格校验：source_core_run_id 必须是已发布的 stock_core run
    published_core_run_id = await get_published_snapshot_run_id(
        session, trade_date, publication_kind=PUBLICATION_KIND_STOCK_CORE,
    )
    if published_core_run_id is None:
        raise ValueError(
            f"market_aggregation 发布失败: trade_date={trade_date} 无已发布 stock_core pointer，"
            f"必须先发布 stock_core"
        )
    if published_core_run_id != source_core_run_id:
        raise ValueError(
            f"market_aggregation 发布失败: source_core_run_id={source_core_run_id} "
            f"与已发布 stock_core pointer={published_core_run_id} 不匹配，"
            f"禁止聚合基于未发布或旧版本 core run"
        )

    board_run = await session.get(BoardAnalysisRun, aggregation_run_id)
    if board_run is None:
        raise ValueError(
            "market_aggregation 发布失败: data_run_id 必须是真实 "
            f"board_analysis_runs.id，未找到 {aggregation_run_id}"
        )
    if board_run.trade_date != trade_date:
        raise ValueError("market_aggregation 发布失败: Board batch trade_date 不匹配")
    if board_run.source_core_run_id != source_core_run_id:
        raise ValueError("market_aggregation 发布失败: Board batch core run 不匹配")

    # [PC-42] Canonical publishability 由 board aggregation layer 决定并经
    # degraded_publishable 传入；本层只做语义等价的接受条件判断。
    # 不再以 failed_count / succeeded_count != expected_count 作为阻断条件，
    # 因为合法 degraded partial 天然 succeeded_count < expected_count
    # （data completeness 缺失），这些由 _evaluate_degraded_publishable 已判定
    # 为 degradation_only，不属于 execution/DB/contract failure。
    _agg_publishable = board_run.status == "succeeded" or (
        board_run.status == "partial" and degraded_publishable
    )
    if not _agg_publishable:
        raise ValueError(
            "market_aggregation 发布失败: Board batch 不可发布，"
            f"status={board_run.status} degraded_publishable={degraded_publishable}；"
            f"只允许 succeeded 或 degraded-publishable 的 partial"
        )

    # [SUCCESS-CONSISTENCY-INVARIANT] 对 status=="succeeded" 必须内部一致：
    # 全 in-scope board 必须全部成功、无失败、达成 expected count。
    # 这不是 degraded publishability 的第二套计算逻辑，而是 succeeded state
    # 自身的一致性 invariant（执行/DB/contract 失败绝不可伪装成 succeeded）。
    if board_run.status == "succeeded":
        if board_run.failed_count != 0:
            raise ValueError(
                "market_aggregation 发布失败: succeeded batch 存在 failed item "
                f"failed_count={board_run.failed_count}，state 不一致"
            )
        if board_run.succeeded_count != board_run.expected_count:
            raise ValueError(
                "market_aggregation 发布失败: succeeded batch 未达成 expected count "
                f"succeeded={board_run.succeeded_count} expected={board_run.expected_count}，"
                f"state 不一致"
            )
    # 注意：status=="partial" AND degraded_publishable=True 路径
    # 不要求 succeeded_count == expected_count（合法 coverage-degraded partial
    # 可存在未 succeeded board），status 保持 partial，不改写为 succeeded。

    now = datetime.now(UTC)
    meta_payload = {
        "source_core_run_id": str(source_core_run_id),
        "board_analysis_run_id": str(aggregation_run_id),
        **(metadata or {}),
    }
    meta_str = json.dumps(meta_payload, ensure_ascii=False)

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
        algorithm_version=algorithm_version,
        data_run_id=aggregation_run_id,
        coverage_ratio=board_run.coverage_ratio,
        published_at=now,
        metadata_json=meta_str,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_key", "trade_date", "publication_kind"],
        index_where=text("superseded_by IS NULL"),
        set_={
            "algorithm_version": stmt.excluded.algorithm_version,
            "data_run_id": stmt.excluded.data_run_id,
            "published_at": stmt.excluded.published_at,
            "metadata_json": stmt.excluded.metadata_json,
        },
    )
    await session.execute(stmt)
    await session.flush()

    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
    )
    logger.info(
        "[Publication] market_aggregation 发布: trade_date=%s, "
        "source_core_run_id=%s, aggregation_run_id=%s",
        trade_date, source_core_run_id, aggregation_run_id,
    )
    return pub  # type: ignore[return-value]


async def publish_chip_consensus(
    session: AsyncSession,
    trade_date: date,
    chip_run_id: uuid.UUID,
    algorithm_version: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> FactorPublication:
    """切换 chip_consensus publication pointer（正式发布指针）。

    [Commit D 2026-08-05] 补齐 chip 正式发布指针：
    - 此前 chip 只持久化到 stock_chip_consensus_snapshots + ChipConsensusRun，
      从未写入 PUBLICATION_KIND_CHIP_CONSENSUS 发布指针，导致 product_readiness
      只能通过 ChipConsensusRun 状态回退，无法通过 pointer 判定 chip 已发布。
    - 本函数在 chip run 达到可发布终态（succeeded/partial）后原子写入发布指针，
      强化 chip 的 publication / pointer / lineage 合同。

    校验（严格 lineage，禁止基于旧/未发布 core run 发布 chip）：
    - chip_run 必须存在且 status 为可发布终态（succeeded/partial）
    - chip_run.trade_date 必须等于调用方 trade_date
    - chip_run.source_core_run_id 必须等于当日已发布 stock_core pointer.data_run_id
    - coverage 由 DB 统计（chip_run.coverage_ratio），不接受调用方任意传值

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        chip_run_id: ChipConsensusRun.id（数据版本）
        algorithm_version: 算法版本
        metadata: 额外元数据

    Returns:
        FactorPublication 记录

    Raises:
        ValueError: chip_run 不存在、状态不可发布、trade_date 不匹配、
            source_core_run_id 与已发布 stock_core pointer 不匹配
    """
    import json

    from app.models.chip_consensus_run import ChipConsensusRun

    chip_run = await session.get(ChipConsensusRun, chip_run_id)
    if chip_run is None:
        raise ValueError(f"chip_consensus 发布失败: chip_run_id={chip_run_id} 不存在")
    if chip_run.trade_date != trade_date:
        raise ValueError(
            f"chip_consensus 发布失败: chip_run.trade_date={chip_run.trade_date} "
            f"与调用方 {trade_date} 不匹配"
        )
    if chip_run.status not in ("succeeded", "partial"):
        raise ValueError(
            f"chip_consensus 发布失败: chip_run.status={chip_run.status!r} "
            f"非可发布终态（succeeded/partial），禁止发布"
        )

    # lineage：chip 必须基于已发布的 stock_core run
    published_core_run_id = await get_published_snapshot_run_id(
        session, trade_date, publication_kind=PUBLICATION_KIND_STOCK_CORE,
    )
    if published_core_run_id is None:
        raise ValueError(
            f"chip_consensus 发布失败: trade_date={trade_date} 无已发布 stock_core pointer，"
            f"必须先发布 stock_core"
        )
    if published_core_run_id != chip_run.source_core_run_id:
        raise ValueError(
            f"chip_consensus 发布失败: chip_run.source_core_run_id={chip_run.source_core_run_id} "
            f"与已发布 stock_core pointer={published_core_run_id} 不匹配，"
            f"禁止基于旧/未发布 core run 发布 chip"
        )

    # coverage 由 DB 统计（chip_run.coverage_ratio）
    coverage = chip_run.coverage_ratio or 0.0

    now = datetime.now(UTC)
    meta_payload = {
        "source_core_run_id": str(chip_run.source_core_run_id),
        "chip_run_status": chip_run.status,
        **(metadata or {}),
    }
    meta_str = json.dumps(meta_payload, ensure_ascii=False)

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_CHIP_CONSENSUS,
        algorithm_version=algorithm_version,
        data_run_id=chip_run_id,
        coverage_ratio=coverage,
        published_at=now,
        metadata_json=meta_str,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_key", "trade_date", "publication_kind"],
        index_where=text("superseded_by IS NULL"),
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

    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_CHIP_CONSENSUS,
    )
    logger.info(
        "[Publication] chip_consensus 发布: trade_date=%s, chip_run_id=%s, "
        "source_core_run_id=%s, coverage=%.4f",
        trade_date, chip_run_id, chip_run.source_core_run_id, coverage,
    )
    return pub  # type: ignore[return-value]


async def compute_history_coverage(
    session: AsyncSession,
    history_run_id: uuid.UUID,
) -> float | None:
    """从 DB 统计 history_run 的覆盖率。

    [CHANGE-20260729-007] coverage 必须由 DB 统计，不接受调用方任意传值。

    覆盖率 = succeeded_count / expected_count
    expected_count = history_run.expected_count

    Returns:
        覆盖率（0.0-1.0），history_run 不存在返回 None
    """
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun

    history_run = await session.get(FirstPyramidHistoryRun, history_run_id)
    if history_run is None:
        return None

    expected = history_run.expected_count or 0
    if expected == 0:
        return 0.0

    succeeded = history_run.succeeded_count or 0
    return succeeded / expected


async def publish_history_cross_section(
    session: AsyncSession,
    trade_date: date,
    history_run_id: uuid.UUID,
    algorithm_version: str,
    *,
    coverage: float | None = None,
    threshold: float = HISTORY_CROSS_SECTION_MIN_COVERAGE,
    metadata: dict[str, Any] | None = None,
) -> FactorPublication:
    """切换 history_cross_section publication pointer（历史横截面发布）。

    [CHANGE-20260729-007] coverage 必须由 DB 统计（compute_history_coverage），
    不接受调用方任意传值。如传入 coverage 则仅用于门禁检查，
    实际写入的 coverage_ratio 以 DB 统计为准。

    前置条件：history_run 覆盖率达到门禁。
    """
    import json

    # [CHANGE-20260729-007] 从 DB 统计 coverage，不接受调用方任意传值
    db_coverage = await compute_history_coverage(session, history_run_id)
    if db_coverage is None:
        raise ValueError(
            f"history_cross_section 发布失败: history_run_id={history_run_id} 不存在"
        )
    if coverage is not None and abs(coverage - db_coverage) > 0.001:
        logger.warning(
            "[Publication] history coverage 不一致: caller=%.4f, db=%.4f, 以 DB 为准",
            coverage, db_coverage,
        )
    coverage = db_coverage

    if coverage < threshold:
        raise CoverageBelowThresholdError(coverage, threshold, history_run_id)

    now = datetime.now(UTC)
    meta_str = json.dumps(metadata, ensure_ascii=False) if metadata else None

    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_HISTORY_CROSS_SECTION,
        algorithm_version=algorithm_version,
        data_run_id=history_run_id,
        coverage_ratio=coverage,
        published_at=now,
        metadata_json=meta_str,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_key", "trade_date", "publication_kind"],
        index_where=text("superseded_by IS NULL"),
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

    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_HISTORY_CROSS_SECTION,
    )
    logger.info(
        "[Publication] history_cross_section 发布: trade_date=%s, "
        "history_run_id=%s, coverage=%.4f",
        trade_date, history_run_id, coverage,
    )
    return pub  # type: ignore[return-value]


async def get_publication(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_key: str,
    trade_date: date | None,
    publication_kind: str,
) -> FactorPublication | None:
    """读取当前 publication pointer（返回 None 表示无 pointer）。

    兼容策略：无 pointer 时调用方应回退到 latest published run。
    """
    conditions = [
        FactorPublication.scope_type == scope_type,
        FactorPublication.scope_key == scope_key,
        FactorPublication.publication_kind == publication_kind,
    ]
    if trade_date is not None:
        conditions.append(FactorPublication.trade_date == trade_date)

    stmt = (
        select(FactorPublication)
        .where(*conditions)
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_published_snapshot_run_id(
    session: AsyncSession,
    trade_date: date,
    *,
    publication_kind: str = PUBLICATION_KIND_STOCK_CORE,
) -> uuid.UUID | None:
    """获取已发布的 snapshot_run_id（兼容回退）。

    优先级：
    1. factor_publications pointer（如存在）
    2. StockFeatureSnapshotRun.status=succeeded + published_at IS NOT NULL（回退）

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        publication_kind: 发布类型（默认 stock_core）

    Returns:
        snapshot_run_id 或 None
    """
    # 1. 优先读取 publication pointer
    pub = await get_publication(
        session,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=publication_kind,
    )
    if pub is not None:
        return pub.data_run_id

    # 2. 回退：查找 latest succeeded + published snapshot run
    stmt = (
        select(StockFeatureSnapshotRun.id)
        .where(
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.status == "succeeded",
            StockFeatureSnapshotRun.published_at.is_not(None),
        )
        .order_by(StockFeatureSnapshotRun.published_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    return row


async def is_stale_snapshot(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID,
    trade_date: date,
) -> bool:
    """判断快照是否过期：snapshot trade_date < MAX(bars_daily.trade_date)。

    [CHANGE-20260729-007 修复] 真源改为 bars_daily.max(trade_date)，
    不再使用 StockFeatureSnapshot.max(trade_date)（后者是快照自身日期，不是行情真源）。
    与 market_stocks_service._build_max_trade_date_subquery 口径一致。

    用于 is_stale computed 字段。
    """
    from app.models.bar import BarDaily

    # 真源：bars_daily 表中所有股票的最新 trade_date
    max_date_stmt = select(func.max(BarDaily.trade_date))
    result = await session.execute(max_date_stmt)
    max_trade_date = result.scalar_one_or_none()

    if max_trade_date is None:
        return False  # 无数据不算过期

    return trade_date < max_trade_date


if __name__ == "__main__":
    print(f"CORE_PUBLICATION_MIN_COVERAGE = {CORE_PUBLICATION_MIN_COVERAGE}")
    print(f"PUBLICATION_KIND_STOCK_CORE = {PUBLICATION_KIND_STOCK_CORE}")
    print(f"PUBLICATION_KIND_MARKET_AGGREGATION = {PUBLICATION_KIND_MARKET_AGGREGATION}")
    print(f"PUBLICATION_KIND_HISTORY_CROSS_SECTION = {PUBLICATION_KIND_HISTORY_CROSS_SECTION}")
    print("OK: factor_publication_service imports verified")
