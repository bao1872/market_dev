"""ChipConsensusRun 生命周期 + chip 发布编排（Corrective-3 §二）。

[Corrective-3 2026-08-05] 修复 Commit D 的真实业务链断裂：

修复前的事实缺陷：
1. **没有任何生产路径创建 `ChipConsensusRun`**：`after_close_chip_consensus_service`
   只写 `StockChipConsensusSnapshot`，领域 run 表 `chip_consensus_runs` 从未被写入。
2. **worker 用 `chip_run_id=None` 调用 `publish_chip_consensus`**，而真实签名要求
   `chip_run_id: uuid.UUID` 且内部 `session.get(ChipConsensusRun, chip_run_id)`
   必须命中，因此该调用在生产上 100% 抛 `ValueError`，只会落到软失败 warning。
3. **worker 传了不存在的参数** `core_run_id=` / `worker_id=`，并把返回的
   `FactorPublication` ORM 当作 dict 使用（`.get("status")`），属于 `AttributeError`。
4. **执行顺序颠倒**：auction anchor 重建发生在 chip pointer 发布之前，
   导致 auction 永远看不到当次 chip pointer，无法从 structure_only 升级。
5. **发布失败只写日志**，ProductReadiness 无法治理，运维无法发现。

本模块提供可注入依赖、不直接持有全局 session 的 orchestration helper，
使 worker 编排可以在不连接数据库的前提下被服务级测试覆盖（Corrective-3 §五）。

模块自测：
    python -m app.services.chip_consensus_run_lifecycle
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chip_consensus_run import ChipConsensusRun

logger = logging.getLogger(__name__)


# =============================================================================
# 常量：发布治理 metadata key（ProductReadiness / Admin 依赖这些键名）
# =============================================================================

META_PUBLICATION_STATUS = "chip_publication_status"
META_PUBLICATION_ERROR_CODE = "chip_publication_error_code"
META_PUBLICATION_ERROR_MESSAGE = "chip_publication_error_message"
META_PUBLICATION_RETRYABLE = "chip_publication_retryable"
META_PUBLICATION_ID = "chip_publication_id"
META_CHIP_RUN_ID = "chip_run_id"

PUBLICATION_STATUS_SUCCEEDED = "succeeded"
PUBLICATION_STATUS_FAILED = "failed"
PUBLICATION_STATUS_SKIPPED = "skipped"

# 推荐恢复动作（后端输出，前端只展示，不重新解释 reason code）
ACTION_RETRY_CHIP_PUBLICATION = "retry_chip_publication"

# chip 可发布终态
_PUBLISHABLE_STATUSES = frozenset({"succeeded", "partial"})

# 不可重试的发布错误（lineage 冲突类，重试也不会成功，需要人工介入）
_NON_RETRYABLE_MARKERS = (
    "与已发布 stock_core pointer",
    "非可发布终态",
    "trade_date",
    "不匹配",
)


# =============================================================================
# ChipConsensusRun 生命周期
# =============================================================================


async def resolve_or_create_chip_run(
    db: AsyncSession,
    *,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    algorithm_version: str,
    scheduler_job_run_id: uuid.UUID | None = None,
    expected_count: int = 0,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
    existing_run_id: uuid.UUID | None = None,
) -> ChipConsensusRun:
    """创建或解析唯一的 `ChipConsensusRun`（retry 复用，不重复建领域 run）。

    解析优先级：
    1. `existing_run_id`（来自 SchedulerJobRun metadata 的 `chip_run_id`）——
       resume/retry 必须复用同一领域 run，禁止每次重试新建。
    2. 同 (trade_date, source_core_run_id, algorithm_version) 的未终结 run。
    3. 都没有则新建。

    Args:
        db: 异步会话（调用方持有事务边界）
        trade_date: 业务交易日
        source_core_run_id: 关联的 stock_core run
        algorithm_version: chip 算法版本
        scheduler_job_run_id: 父 SchedulerJobRun
        expected_count: 预期计算 instrument 数
        worker_id: worker lineage
        lease_epoch: 租约 epoch（fencing lineage）
        existing_run_id: metadata 中已固定的 chip_run_id

    Returns:
        ChipConsensusRun（已 flush，`id` 可用）
    """
    run: ChipConsensusRun | None = None

    if existing_run_id is not None:
        run = await db.get(ChipConsensusRun, existing_run_id)
        if run is not None and run.trade_date != trade_date:
            # metadata 指向了错误交易日的 run，视为无效，不复用
            logger.warning(
                "[ChipRunLifecycle] metadata chip_run_id=%s 的 trade_date=%s "
                "与当前 %s 不一致，忽略复用",
                existing_run_id, run.trade_date, trade_date,
            )
            run = None

    if run is None:
        run = await db.scalar(
            select(ChipConsensusRun)
            .where(
                ChipConsensusRun.trade_date == trade_date,
                ChipConsensusRun.source_core_run_id == source_core_run_id,
                ChipConsensusRun.algorithm_version == algorithm_version,
                ChipConsensusRun.status.in_(
                    ("queued", "running", "interrupted", "resume_queued"),
                ),
            )
            .order_by(ChipConsensusRun.created_at.desc())
            .limit(1)
        )

    now = datetime.now(UTC)

    if run is None:
        # [Corrective-3.1 §P1] 原子 upsert：依赖唯一约束
        # uq_chip_consensus_runs_date_core_algo（migration 086）在数据库层阻止
        # 并发重复创建。ON CONFLICT DO NOTHING 后回读，确保并发竞争的败方也能
        # 拿到胜方创建的同一行，而不是各自新建。
        new_id = uuid.uuid4()
        stmt = (
            pg_insert(ChipConsensusRun)
            .values(
                id=new_id,
                scheduler_job_run_id=scheduler_job_run_id,
                trade_date=trade_date,
                source_core_run_id=source_core_run_id,
                algorithm_version=algorithm_version,
                status="running",
                expected_count=expected_count,
                succeeded_count=0,
                failed_count=0,
                skipped_count=0,
                coverage_ratio=0.0,
                started_at=now,
                heartbeat_at=now,
                worker_id=worker_id,
                lease_epoch=lease_epoch or 0,
            )
            .on_conflict_do_nothing(
                index_elements=["trade_date", "source_core_run_id", "algorithm_version"],
            )
            .returning(ChipConsensusRun.id)
        )
        inserted_id = await db.scalar(stmt)

        if inserted_id is not None:
            created = await db.get(ChipConsensusRun, inserted_id)
            if created is None:
                raise RuntimeError(
                    f"resolve_or_create_chip_run: 新建行回读失败 id={inserted_id}"
                )
            logger.info(
                "[ChipRunLifecycle] 新建 ChipConsensusRun: id=%s trade_date=%s "
                "core_run=%s",
                created.id, trade_date, source_core_run_id,
            )
            await db.flush()
            return created

        # 并发竞争败方：回读胜方创建的行，走下面统一的复用分支
        run = await db.scalar(
            select(ChipConsensusRun).where(
                ChipConsensusRun.trade_date == trade_date,
                ChipConsensusRun.source_core_run_id == source_core_run_id,
                ChipConsensusRun.algorithm_version == algorithm_version,
            ).limit(1)
        )
        if run is None:
            raise RuntimeError(
                "resolve_or_create_chip_run: ON CONFLICT 未插入且回读为空 "
                f"(trade_date={trade_date}, core_run={source_core_run_id}, "
                f"algo={algorithm_version})"
            )
        logger.info(
            "[ChipRunLifecycle] 并发竞争，复用已存在 ChipConsensusRun: id=%s", run.id,
        )

    # retry / resume / 并发败方：复用同一领域 run，只刷新执行 lineage
    run.status = "running"
    run.scheduler_job_run_id = scheduler_job_run_id or run.scheduler_job_run_id
    run.worker_id = worker_id or run.worker_id
    if lease_epoch is not None:
        run.lease_epoch = lease_epoch
    if expected_count:
        run.expected_count = expected_count
    run.heartbeat_at = now
    if run.started_at is None:
        run.started_at = now
    logger.info(
        "[ChipRunLifecycle] 复用 ChipConsensusRun: id=%s status→running", run.id,
    )

    await db.flush()
    return run


async def finalize_chip_run(
    db: AsyncSession,
    *,
    chip_run_id: uuid.UUID,
    chip_status: str,
    succeeded_count: int,
    failed_count: int,
    skipped_count: int,
    total_count: int,
    error_code: str | None = None,
    error_message: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> ChipConsensusRun:
    """把 chip 计算结果写入 `ChipConsensusRun` 终态。

    必须在 `publish_chip_consensus` 之前调用，因为发布函数会校验
    `chip_run.status in (succeeded, partial)` 且读取 `coverage_ratio`。

    coverage_ratio 由真实计数推导，不接受调用方任意传值。
    """
    run = await db.get(ChipConsensusRun, chip_run_id)
    if run is None:
        raise ValueError(f"finalize_chip_run 失败: chip_run_id={chip_run_id} 不存在")

    coverage = (succeeded_count / total_count) if total_count > 0 else 0.0

    run.status = chip_status
    run.succeeded_count = succeeded_count
    run.failed_count = failed_count
    run.skipped_count = skipped_count
    run.expected_count = total_count or run.expected_count
    run.coverage_ratio = round(coverage, 6)
    run.finished_at = datetime.now(UTC)
    run.readiness = (
        "ready" if chip_status == "succeeded"
        else "degraded" if chip_status == "partial"
        else "unavailable"
    )
    run.error_code = error_code
    run.error_message = (error_message or None) and str(error_message)[:500]
    if diagnostics:
        run.diagnostics_json = diagnostics

    await db.flush()
    logger.info(
        "[ChipRunLifecycle] ChipConsensusRun 终态: id=%s status=%s coverage=%.4f",
        run.id, chip_status, run.coverage_ratio,
    )
    return run


# =============================================================================
# 发布编排（可注入依赖，便于不连库的服务级测试）
# =============================================================================


class _PublishFn(Protocol):
    async def __call__(
        self,
        session: Any,
        *,
        trade_date: date,
        chip_run_id: uuid.UUID,
        algorithm_version: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any: ...


class _AuctionFn(Protocol):
    async def __call__(
        self,
        db: Any,
        trade_date: date,
        *,
        worker_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class ChipPublicationOutcome:
    """chip 发布编排结果（供 worker 写入 SchedulerJobRun metadata）。"""

    status: str
    publication_id: uuid.UUID | None = None
    data_run_id: uuid.UUID | None = None
    publication_kind: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    recommended_action: str | None = None
    auction_invoked: bool = False
    auction_result: dict[str, Any] | None = None

    def to_metadata(self) -> dict[str, Any]:
        """转成 SchedulerJobRun metadata 片段，使发布失败可被治理。"""
        meta: dict[str, Any] = {META_PUBLICATION_STATUS: self.status}
        if self.publication_id is not None:
            meta[META_PUBLICATION_ID] = str(self.publication_id)
        if self.status == PUBLICATION_STATUS_FAILED:
            meta[META_PUBLICATION_ERROR_CODE] = self.error_code
            meta[META_PUBLICATION_ERROR_MESSAGE] = self.error_message
            meta[META_PUBLICATION_RETRYABLE] = self.retryable
        return meta


def classify_publication_error(exc: BaseException) -> tuple[str, str, bool]:
    """把发布异常分类为 (error_code, error_message, retryable)。

    lineage 冲突类错误重试不会成功，必须标为不可重试并由人工介入。
    """
    message = str(exc)
    if isinstance(exc, ValueError):
        retryable = not any(marker in message for marker in _NON_RETRYABLE_MARKERS)
        code = "CHIP_PUBLICATION_LINEAGE_REJECTED" if not retryable \
            else "CHIP_PUBLICATION_REJECTED"
        return code, message[:500], retryable
    return "CHIP_PUBLICATION_UNEXPECTED_ERROR", message[:500], True


async def publish_chip_and_upgrade_auction(
    *,
    trade_date: date,
    chip_run_id: uuid.UUID,
    algorithm_version: str,
    chip_status: str,
    scheduler_job_run_id: uuid.UUID,
    worker_id: str,
    lease_epoch: int | None,
    anchor_rebuild_required: bool,
    session_factory: Any,
    publish_fn: _PublishFn,
    auction_fn: _AuctionFn,
    ownership_check: Any | None = None,
) -> ChipPublicationOutcome:
    """[Corrective-3 §二.3] chip 发布 + auction 升级的正确顺序编排。

    强制顺序：
        chip snapshots 完成
        → ChipConsensusRun 终态（调用方在此之前完成）
        → publish_chip_consensus
        → commit publication pointer
        → generate_and_publish_auction_anchors

    auction 升级只在 chip pointer 成功发布之后执行；发布失败时禁止触发
    auction composite upgrade（否则 auction 会基于没有 pointer 的 chip 升级，
    产生无法追溯的 composite 锚点）。

    所有依赖通过参数注入，因此本函数可以在 fake session/fake publisher 下
    被服务级测试完整覆盖，无需连接数据库。

    Args:
        trade_date: 业务交易日
        chip_run_id: 真实 ChipConsensusRun.id（禁止 None）
        algorithm_version: chip 算法版本
        chip_status: chip 终态（只有 succeeded/partial 才发布）
        scheduler_job_run_id: 父任务 id（写入 publication metadata lineage）
        worker_id: worker lineage
        lease_epoch: 租约 epoch
        anchor_rebuild_required: chip 是否要求重建锚点
        session_factory: 产生 AsyncSession 的 async context manager 工厂
        publish_fn: 真实 `publish_chip_consensus`
        auction_fn: 真实 `generate_and_publish_auction_anchors`
        ownership_check: 租约校验回调；失去租约则禁止任何发布/写入

    Returns:
        ChipPublicationOutcome
    """
    if chip_status not in _PUBLISHABLE_STATUSES:
        return ChipPublicationOutcome(
            status=PUBLICATION_STATUS_SKIPPED,
            error_code="CHIP_STATUS_NOT_PUBLISHABLE",
            error_message=f"chip_status={chip_status} 非可发布终态",
            retryable=False,
        )

    # fencing：失去租约后禁止 publication 和 auction 写入
    if ownership_check is not None:
        try:
            result = ownership_check()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.warning(
                "[ChipRunLifecycle] 失去租约，跳过 publication 与 auction: %s", exc,
            )
            return ChipPublicationOutcome(
                status=PUBLICATION_STATUS_SKIPPED,
                error_code="CHIP_LEASE_LOST",
                error_message=str(exc)[:500],
                retryable=True,
            )

    # ---- 步骤 1：发布 chip pointer ----
    try:
        async with session_factory() as pub_db:
            pub = await publish_fn(
                session=pub_db,
                trade_date=trade_date,
                chip_run_id=chip_run_id,
                algorithm_version=algorithm_version,
                metadata={
                    "scheduler_job_run_id": str(scheduler_job_run_id),
                    "worker_id": worker_id,
                    "lease_epoch": lease_epoch,
                },
            )
            await pub_db.commit()
    except Exception as exc:
        code, message, retryable = classify_publication_error(exc)
        logger.warning(
            "[ChipRunLifecycle] chip pointer 发布失败（软失败，可治理）: "
            "trade_date=%s chip_run_id=%s code=%s retryable=%s",
            trade_date, chip_run_id, code, retryable, exc_info=True,
        )
        # 发布失败：禁止触发 auction composite upgrade
        return ChipPublicationOutcome(
            status=PUBLICATION_STATUS_FAILED,
            error_code=code,
            error_message=message,
            retryable=retryable,
            recommended_action=ACTION_RETRY_CHIP_PUBLICATION,
            auction_invoked=False,
        )

    # 返回值是 FactorPublication ORM，禁止 .get()
    outcome = ChipPublicationOutcome(
        status=PUBLICATION_STATUS_SUCCEEDED,
        publication_id=getattr(pub, "id", None),
        data_run_id=getattr(pub, "data_run_id", None),
        publication_kind=getattr(pub, "publication_kind", None),
    )
    logger.info(
        "[ChipRunLifecycle] chip pointer 已发布: trade_date=%s publication_id=%s "
        "data_run_id=%s kind=%s",
        trade_date, outcome.publication_id, outcome.data_run_id,
        outcome.publication_kind,
    )

    # ---- 步骤 2：pointer 发布成功后才升级 auction ----
    if not anchor_rebuild_required:
        return outcome

    if ownership_check is not None:
        try:
            result = ownership_check()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.warning(
                "[ChipRunLifecycle] 发布后失去租约，跳过 auction 升级: %s", exc,
            )
            return outcome

    try:
        async with session_factory() as anchor_db:
            anchor_result = await auction_fn(
                anchor_db,
                trade_date=trade_date,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
            )
            await anchor_db.commit()
        outcome.auction_invoked = True
        outcome.auction_result = anchor_result
        logger.info(
            "[ChipRunLifecycle] chip pointer 发布后升级 auction: trade_date=%s "
            "anchor_status=%s",
            trade_date, (anchor_result or {}).get("status"),
        )
    except Exception:
        logger.warning(
            "[ChipRunLifecycle] auction 升级失败（软失败，不反改 chip）: trade_date=%s",
            trade_date, exc_info=True,
        )
        outcome.auction_invoked = True

    return outcome


@dataclass
class _SelfTestSession:
    """模块自测用的最小 fake session。"""

    committed: bool = False
    calls: list[str] = field(default_factory=list)

    async def __aenter__(self) -> _SelfTestSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        self.committed = True


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        order: list[str] = []
        session = _SelfTestSession()

        class _Pub:
            id = uuid.uuid4()
            data_run_id = uuid.uuid4()
            publication_kind = "chip_consensus"

        async def fake_publish(session: Any, **kwargs: Any) -> _Pub:
            order.append("publish")
            return _Pub()

        async def fake_auction(db: Any, trade_date: date, **kwargs: Any) -> dict[str, Any]:
            order.append("auction")
            return {"status": "succeeded"}

        def factory() -> _SelfTestSession:
            return session

        out = await publish_chip_and_upgrade_auction(
            trade_date=date(2026, 8, 5),
            chip_run_id=uuid.uuid4(),
            algorithm_version="v1",
            chip_status="succeeded",
            scheduler_job_run_id=uuid.uuid4(),
            worker_id="w1",
            lease_epoch=1,
            anchor_rebuild_required=True,
            session_factory=factory,
            publish_fn=fake_publish,
            auction_fn=fake_auction,
        )
        assert order == ["publish", "auction"], f"顺序错误: {order}"
        assert out.status == PUBLICATION_STATUS_SUCCEEDED
        assert out.publication_id is not None
        print("OK: publish → auction 顺序正确")

        # 发布失败禁止 auction
        order.clear()

        async def failing_publish(session: Any, **kwargs: Any) -> Any:
            order.append("publish")
            raise ValueError("chip_consensus 发布失败: 无已发布 stock_core pointer")

        out2 = await publish_chip_and_upgrade_auction(
            trade_date=date(2026, 8, 5),
            chip_run_id=uuid.uuid4(),
            algorithm_version="v1",
            chip_status="partial",
            scheduler_job_run_id=uuid.uuid4(),
            worker_id="w1",
            lease_epoch=1,
            anchor_rebuild_required=True,
            session_factory=factory,
            publish_fn=failing_publish,
            auction_fn=fake_auction,
        )
        assert order == ["publish"], f"发布失败仍触发了 auction: {order}"
        assert out2.status == PUBLICATION_STATUS_FAILED
        assert out2.recommended_action == ACTION_RETRY_CHIP_PUBLICATION
        assert out2.to_metadata()[META_PUBLICATION_STATUS] == "failed"
        print("OK: 发布失败不触发 auction，且 metadata 可治理")

    asyncio.run(_main())
