"""[Corrective-3 §二.5/§五] chip worker 编排服务级测试（fake session/adapter，不连数据库）。

与 `test_v21_readiness_auction_decision_integration.py`（纯决策函数）不同，
本文件调用**真实的** orchestration helper
`app.services.chip_consensus_run_lifecycle.publish_chip_and_upgrade_auction`，
并注入 fake session / fake publish adapter / fake auction adapter，
验证生产编排契约本身，而不是在测试里复制业务逻辑。

覆盖 §二.5 全部要求：
  1. succeeded → 使用真实 chip_run_id 发布 chip pointer；
  2. partial → 同样发布 pointer，coverage 由真实计数推导；
  3. publisher 收到真实 chip_run_id（非 None）；
  4. algorithm_version 正确透传；
  5. publisher 返回 ORM 对象，按属性读取（禁止 .get()）；
  6. publication 成功后才调用 auction upgrade（顺序断言）；
  7. publication 失败写入治理 metadata，且不触发 auction composite upgrade；
  8. retry 复用同一 ChipConsensusRun（不重复创建领域 run）；
  9. lease 丢失后禁止 publication 和 auction 写入。

运行（不连库）：
    PURE_UNIT_TEST=1 pytest backend/tests/test_chip_worker_orchestration.py
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.services.chip_consensus_run_lifecycle import (
    ACTION_RETRY_CHIP_PUBLICATION,
    META_PUBLICATION_ERROR_CODE,
    META_PUBLICATION_ID,
    META_PUBLICATION_RETRYABLE,
    META_PUBLICATION_STATUS,
    PUBLICATION_STATUS_FAILED,
    PUBLICATION_STATUS_SKIPPED,
    PUBLICATION_STATUS_SUCCEEDED,
    ChipPublicationOutcome,
    classify_publication_error,
    finalize_chip_run,
    publish_chip_and_upgrade_auction,
    resolve_or_create_chip_run,
)

TRADE_DATE = date(2026, 8, 5)
ALGO = "chip-consensus-1.0.0"


# =============================================================================
# Fakes
# =============================================================================


@dataclass
class FakeSession:
    """最小 AsyncSession 替身：支持 async context manager + commit 计数。"""

    commits: int = 0
    added: list[Any] = field(default_factory=list)
    store: dict[Any, Any] = field(default_factory=dict)
    scalar_result: Any = None

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def flush(self) -> None:
        pass

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if getattr(obj, "id", None) is not None:
            self.store[obj.id] = obj

    async def get(self, _model: Any, key: Any) -> Any:
        return self.store.get(key)

    async def scalar(self, _stmt: Any) -> Any:
        return self.scalar_result


@dataclass
class FakePublication:
    """FactorPublication ORM 替身 —— 只暴露属性，没有 .get()。"""

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    data_run_id: uuid.UUID = field(default_factory=uuid.uuid4)
    publication_kind: str = "chip_consensus"


class Recorder:
    """记录 publish / auction 的调用顺序与参数。"""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.publish_calls: list[dict[str, Any]] = []
        self.auction_calls: list[dict[str, Any]] = []
        self.publication = FakePublication()
        self.publish_error: Exception | None = None

    async def publish(self, session: Any, **kwargs: Any) -> FakePublication:
        self.order.append("publish")
        self.publish_calls.append(kwargs)
        if self.publish_error is not None:
            raise self.publish_error
        return self.publication

    async def auction(self, db: Any, trade_date: date, **kwargs: Any) -> dict[str, Any]:
        self.order.append("auction")
        self.auction_calls.append({"trade_date": trade_date, **kwargs})
        return {"status": "succeeded", "mode": "composite"}


def make_factory(session: FakeSession) -> Any:
    def _factory() -> FakeSession:
        return session
    return _factory


async def run_orchestration(
    recorder: Recorder,
    *,
    chip_run_id: uuid.UUID,
    chip_status: str = "succeeded",
    anchor_rebuild_required: bool = True,
    ownership_check: Any = None,
    session: FakeSession | None = None,
) -> ChipPublicationOutcome:
    return await publish_chip_and_upgrade_auction(
        trade_date=TRADE_DATE,
        chip_run_id=chip_run_id,
        algorithm_version=ALGO,
        chip_status=chip_status,
        scheduler_job_run_id=uuid.uuid4(),
        worker_id="worker-1",
        lease_epoch=7,
        anchor_rebuild_required=anchor_rebuild_required,
        session_factory=make_factory(session or FakeSession()),
        publish_fn=recorder.publish,
        auction_fn=recorder.auction,
        ownership_check=ownership_check,
    )


# =============================================================================
# 1/3/4/5/6: succeeded → 真实 chip_run_id 发布 → 之后才 auction
# =============================================================================


@pytest.mark.asyncio
async def test_succeeded_publishes_pointer_with_real_chip_run_id_then_auction() -> None:
    recorder = Recorder()
    chip_run_id = uuid.uuid4()

    outcome = await run_orchestration(recorder, chip_run_id=chip_run_id)

    # 6. 顺序：先 publish 后 auction
    assert recorder.order == ["publish", "auction"]

    call = recorder.publish_calls[0]
    # 3. publisher 使用真实 chip_run_id（禁止 None）
    assert call["chip_run_id"] == chip_run_id
    assert call["chip_run_id"] is not None
    # 4. algorithm_version 正确
    assert call["algorithm_version"] == ALGO
    assert call["trade_date"] == TRADE_DATE
    # 真实签名不含已废弃的 core_run_id / worker_id 顶层参数
    assert "core_run_id" not in call
    assert "worker_id" not in call
    # lineage metadata 透传
    assert call["metadata"]["worker_id"] == "worker-1"
    assert call["metadata"]["lease_epoch"] == 7

    # 5. publisher 返回 ORM，按属性读取
    assert outcome.status == PUBLICATION_STATUS_SUCCEEDED
    assert outcome.publication_id == recorder.publication.id
    assert outcome.data_run_id == recorder.publication.data_run_id
    assert outcome.publication_kind == "chip_consensus"
    assert outcome.auction_invoked is True

    meta = outcome.to_metadata()
    assert meta[META_PUBLICATION_STATUS] == PUBLICATION_STATUS_SUCCEEDED
    assert meta[META_PUBLICATION_ID] == str(recorder.publication.id)


# =============================================================================
# 2: partial 也必须发布 pointer
# =============================================================================


@pytest.mark.asyncio
async def test_partial_still_publishes_pointer() -> None:
    recorder = Recorder()
    outcome = await run_orchestration(
        recorder, chip_run_id=uuid.uuid4(), chip_status="partial",
    )
    assert outcome.status == PUBLICATION_STATUS_SUCCEEDED
    assert recorder.order == ["publish", "auction"]


@pytest.mark.asyncio
async def test_failed_chip_status_is_not_published() -> None:
    recorder = Recorder()
    outcome = await run_orchestration(
        recorder, chip_run_id=uuid.uuid4(), chip_status="failed",
    )
    assert outcome.status == PUBLICATION_STATUS_SKIPPED
    assert recorder.order == []


# =============================================================================
# 2 (coverage): finalize_chip_run 由真实计数推导 coverage_ratio
# =============================================================================


@pytest.mark.asyncio
async def test_finalize_chip_run_derives_partial_coverage() -> None:
    from app.models.chip_consensus_run import ChipConsensusRun

    run = ChipConsensusRun(
        id=uuid.uuid4(),
        trade_date=TRADE_DATE,
        source_core_run_id=uuid.uuid4(),
        algorithm_version=ALGO,
        status="running",
        expected_count=100,
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        coverage_ratio=0.0,
    )
    session = FakeSession(store={run.id: run})

    finalized = await finalize_chip_run(
        session,
        chip_run_id=run.id,
        chip_status="partial",
        succeeded_count=80,
        failed_count=15,
        skipped_count=5,
        total_count=100,
    )

    assert finalized.status == "partial"
    assert finalized.coverage_ratio == pytest.approx(0.8)
    assert finalized.readiness == "degraded"
    assert finalized.finished_at is not None


@pytest.mark.asyncio
async def test_finalize_chip_run_succeeded_full_coverage() -> None:
    from app.models.chip_consensus_run import ChipConsensusRun

    run = ChipConsensusRun(
        id=uuid.uuid4(),
        trade_date=TRADE_DATE,
        source_core_run_id=uuid.uuid4(),
        algorithm_version=ALGO,
        status="running",
        expected_count=50,
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        coverage_ratio=0.0,
    )
    session = FakeSession(store={run.id: run})

    finalized = await finalize_chip_run(
        session,
        chip_run_id=run.id,
        chip_status="succeeded",
        succeeded_count=50,
        failed_count=0,
        skipped_count=0,
        total_count=50,
    )
    assert finalized.coverage_ratio == pytest.approx(1.0)
    assert finalized.readiness == "ready"


# =============================================================================
# 7: publication 失败 → 治理 metadata，且不触发 auction
# =============================================================================


@pytest.mark.asyncio
async def test_publication_failure_records_metadata_and_blocks_auction() -> None:
    recorder = Recorder()
    recorder.publish_error = ValueError(
        "chip_consensus 发布失败: 无已发布 stock_core pointer",
    )

    outcome = await run_orchestration(recorder, chip_run_id=uuid.uuid4())

    # 不得触发 auction composite upgrade
    assert recorder.order == ["publish"]
    assert outcome.auction_invoked is False

    assert outcome.status == PUBLICATION_STATUS_FAILED
    assert outcome.recommended_action == ACTION_RETRY_CHIP_PUBLICATION

    meta = outcome.to_metadata()
    assert meta[META_PUBLICATION_STATUS] == PUBLICATION_STATUS_FAILED
    assert meta[META_PUBLICATION_ERROR_CODE]
    assert META_PUBLICATION_RETRYABLE in meta


@pytest.mark.asyncio
async def test_lineage_conflict_is_classified_non_retryable() -> None:
    code, message, retryable = classify_publication_error(
        ValueError("chip_consensus trade_date 与 stock_core pointer 不匹配"),
    )
    assert retryable is False
    assert code == "CHIP_PUBLICATION_LINEAGE_REJECTED"
    assert message


@pytest.mark.asyncio
async def test_unexpected_error_is_retryable() -> None:
    code, _msg, retryable = classify_publication_error(RuntimeError("连接超时"))
    assert retryable is True
    assert code == "CHIP_PUBLICATION_UNEXPECTED_ERROR"


# =============================================================================
# 8: retry 复用同一 ChipConsensusRun
# =============================================================================


@pytest.mark.asyncio
async def test_retry_reuses_existing_chip_run() -> None:
    from app.models.chip_consensus_run import ChipConsensusRun

    core_run_id = uuid.uuid4()
    existing = ChipConsensusRun(
        id=uuid.uuid4(),
        trade_date=TRADE_DATE,
        source_core_run_id=core_run_id,
        algorithm_version=ALGO,
        status="interrupted",
        expected_count=100,
        succeeded_count=40,
        failed_count=0,
        skipped_count=0,
        coverage_ratio=0.4,
        started_at=datetime.now(UTC),
    )
    session = FakeSession(store={existing.id: existing})

    resolved = await resolve_or_create_chip_run(
        session,
        trade_date=TRADE_DATE,
        source_core_run_id=core_run_id,
        algorithm_version=ALGO,
        expected_count=100,
        worker_id="worker-2",
        lease_epoch=9,
        existing_run_id=existing.id,
    )

    # 复用同一领域 run，不新建
    assert resolved.id == existing.id
    assert session.added == []
    assert resolved.status == "running"
    assert resolved.worker_id == "worker-2"
    assert resolved.lease_epoch == 9
    # 已完成进度不被清零
    assert resolved.succeeded_count == 40


@pytest.mark.asyncio
async def test_new_chip_run_created_when_none_exists() -> None:
    session = FakeSession()
    core_run_id = uuid.uuid4()

    created = await resolve_or_create_chip_run(
        session,
        trade_date=TRADE_DATE,
        source_core_run_id=core_run_id,
        algorithm_version=ALGO,
        expected_count=30,
        worker_id="worker-3",
        lease_epoch=1,
    )

    assert len(session.added) == 1
    assert created.id is not None
    assert created.trade_date == TRADE_DATE
    assert created.source_core_run_id == core_run_id
    assert created.algorithm_version == ALGO
    assert created.status == "running"
    assert created.expected_count == 30


@pytest.mark.asyncio
async def test_mismatched_trade_date_run_is_not_reused() -> None:
    from app.models.chip_consensus_run import ChipConsensusRun

    stale = ChipConsensusRun(
        id=uuid.uuid4(),
        trade_date=date(2026, 8, 4),
        source_core_run_id=uuid.uuid4(),
        algorithm_version=ALGO,
        status="interrupted",
        expected_count=10,
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        coverage_ratio=0.0,
    )
    session = FakeSession(store={stale.id: stale})

    resolved = await resolve_or_create_chip_run(
        session,
        trade_date=TRADE_DATE,
        source_core_run_id=uuid.uuid4(),
        algorithm_version=ALGO,
        existing_run_id=stale.id,
    )
    assert resolved.id != stale.id
    assert len(session.added) == 1


# =============================================================================
# 9: lease 丢失 → 禁止 publication 与 auction
# =============================================================================


@pytest.mark.asyncio
async def test_lease_lost_blocks_publication_and_auction() -> None:
    recorder = Recorder()

    def lost_lease() -> None:
        raise RuntimeError("JobLeaseLostError: lease epoch 已被抢占")

    outcome = await run_orchestration(
        recorder, chip_run_id=uuid.uuid4(), ownership_check=lost_lease,
    )

    assert recorder.order == []
    assert outcome.status == PUBLICATION_STATUS_SKIPPED
    assert outcome.error_code == "CHIP_LEASE_LOST"
    assert outcome.auction_invoked is False


@pytest.mark.asyncio
async def test_lease_lost_after_publish_still_skips_auction() -> None:
    recorder = Recorder()
    calls = {"n": 0}

    def lease_lost_second_time() -> None:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("lease lost after publish")

    outcome = await run_orchestration(
        recorder,
        chip_run_id=uuid.uuid4(),
        ownership_check=lease_lost_second_time,
    )

    # pointer 已发布，但 auction 被阻断
    assert recorder.order == ["publish"]
    assert outcome.status == PUBLICATION_STATUS_SUCCEEDED
    assert outcome.auction_invoked is False


# =============================================================================
# 附加：anchor_rebuild_required=False 时不调用 auction
# =============================================================================


@pytest.mark.asyncio
async def test_no_auction_when_rebuild_not_required() -> None:
    recorder = Recorder()
    outcome = await run_orchestration(
        recorder, chip_run_id=uuid.uuid4(), anchor_rebuild_required=False,
    )
    assert recorder.order == ["publish"]
    assert outcome.status == PUBLICATION_STATUS_SUCCEEDED
    assert outcome.auction_invoked is False


@pytest.mark.asyncio
async def test_auction_failure_does_not_reverse_chip_publication() -> None:
    recorder = Recorder()

    async def failing_auction(db: Any, trade_date: date, **kwargs: Any) -> dict[str, Any]:
        recorder.order.append("auction")
        raise RuntimeError("锚点重建失败")

    outcome = await publish_chip_and_upgrade_auction(
        trade_date=TRADE_DATE,
        chip_run_id=uuid.uuid4(),
        algorithm_version=ALGO,
        chip_status="succeeded",
        scheduler_job_run_id=uuid.uuid4(),
        worker_id="worker-1",
        lease_epoch=1,
        anchor_rebuild_required=True,
        session_factory=make_factory(FakeSession()),
        publish_fn=recorder.publish,
        auction_fn=failing_auction,
    )

    # chip publication 结果不被 auction 失败反改
    assert outcome.status == PUBLICATION_STATUS_SUCCEEDED
    assert outcome.publication_id == recorder.publication.id
    assert recorder.order == ["publish", "auction"]
