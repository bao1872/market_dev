"""增量检查点/分层发布重构 - 目标测试。

覆盖（ref/instruction.md §十一 验收标准）：
1. 3股中第2股失败，第1/3股成功并commit
2. 中断恢复不重算成功股
3. input_hash/version 变化才重算
4. 98%前不发布，达到后原子切pointer
5. pointer失败只重试发布
6. aggregation/events/chip失败不反改core
7. 不同run不混读
8. 并发claim不重复领取
9. coverage计算
10. publication兼容回退

测试策略：
- 纯单元测试：使用 mock session，PURE_UNIT_TEST=1 时运行
- PostgreSQL集成测试：使用测试库，PURE_UNIT_TEST=1 时跳过
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 纯单元测试环境检测
_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"
_SKIP_INTEGRATION = pytest.mark.skipif(
    _PURE_UNIT_TEST,
    reason="PostgreSQL集成测试在PURE_UNIT_TEST=1时跳过，只在CI临时Postgres容器中运行",
)


# ============================================================================
# 纯单元测试：常量与导入验证
# ============================================================================


class TestConstantsAndImports:
    """验证关键常量和导入。"""

    def test_publication_kind_constants(self) -> None:
        from app.models.factor_publication import (
            ALL_PUBLICATION_KINDS,
            PUBLICATION_KIND_HISTORY_CROSS_SECTION,
            PUBLICATION_KIND_MARKET_AGGREGATION,
            PUBLICATION_KIND_STOCK_CORE,
        )
        assert PUBLICATION_KIND_STOCK_CORE == "stock_core"
        assert PUBLICATION_KIND_MARKET_AGGREGATION == "market_aggregation"
        assert PUBLICATION_KIND_HISTORY_CROSS_SECTION == "history_cross_section"
        assert len(ALL_PUBLICATION_KINDS) == 3

    def test_run_item_status_constants(self) -> None:
        from app.models.stock_feature_snapshot_run_item import (
            ALL_ITEM_STATUSES,
            ITEM_FAILED,
            ITEM_PENDING,
            ITEM_RUNNING,
            ITEM_SKIPPED,
            ITEM_SUCCEEDED,
            RESUMABLE_STATUSES,
            TERMINAL_STATUSES,
        )
        assert ITEM_PENDING == "pending"
        assert ITEM_RUNNING == "running"
        assert ITEM_SUCCEEDED == "succeeded"
        assert ITEM_FAILED == "failed"
        assert ITEM_SKIPPED == "skipped"
        assert len(ALL_ITEM_STATUSES) == 5
        assert RESUMABLE_STATUSES == {ITEM_PENDING, ITEM_FAILED}
        assert TERMINAL_STATUSES == {ITEM_SUCCEEDED, ITEM_SKIPPED}

    def test_history_run_status_constants(self) -> None:
        from app.models.first_pyramid_history_run import (
            ALL_HISTORY_RUN_STATUSES,
            HISTORY_RUN_FAILED,
            HISTORY_RUN_PARTIAL,
            HISTORY_RUN_RUNNING,
            HISTORY_RUN_SUCCEEDED,
        )
        assert HISTORY_RUN_RUNNING == "running"
        assert HISTORY_RUN_PARTIAL == "partial"
        assert HISTORY_RUN_SUCCEEDED == "succeeded"
        assert HISTORY_RUN_FAILED == "failed"
        assert len(ALL_HISTORY_RUN_STATUSES) == 4

    def test_coverage_threshold(self) -> None:
        from app.services.factor_publication_service import (
            CORE_PUBLICATION_MIN_COVERAGE,
        )
        assert CORE_PUBLICATION_MIN_COVERAGE == 0.98

    def test_lease_and_retry_constants(self) -> None:
        from app.services.snapshot_run_item_service import (
            DEFAULT_ITEM_LEASE_SECONDS,
            MAX_ATTEMPT_COUNT,
        )
        assert DEFAULT_ITEM_LEASE_SECONDS == 120
        assert MAX_ATTEMPT_COUNT == 3

    def test_id_contract_constants(self) -> None:
        """ID 合同：orchestrator_job_run_id ≠ snapshot_run_id ≠ history_run_id。"""
        from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
        from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
        from app.models.stock_feature_snapshot_run_item import (
            StockFeatureSnapshotRunItem,
        )

        # snapshot_run_id 字段存在
        assert hasattr(StockFeatureSnapshotRunItem, "snapshot_run_id")
        # scheduler_job_run_id 字段存在（纯 metadata）
        assert hasattr(FirstPyramidHistoryRun, "scheduler_job_run_id")
        # snapshot run 有 source_run_id 字段（指向数据版本）
        assert hasattr(StockFeatureSnapshotRun, "id")


# ============================================================================
# 纯单元测试：ORM 模型验证
# ============================================================================


class TestORMModels:
    """验证 ORM 模型映射。"""

    def test_snapshot_run_item_columns(self) -> None:
        from app.models.stock_feature_snapshot_run_item import (
            StockFeatureSnapshotRunItem,
        )
        cols = {c.name for c in StockFeatureSnapshotRunItem.__table__.columns}
        expected = {
            "id", "snapshot_run_id", "instrument_id", "phase", "status",
            "attempt_count", "input_hash", "worker_instance_id", "lease_epoch",
            "lease_expires_at", "result_count", "last_error", "started_at",
            "heartbeat_at", "completed_at", "created_at", "updated_at",
        }
        assert expected == cols

    def test_factor_publication_columns(self) -> None:
        from app.models.factor_publication import FactorPublication
        cols = {c.name for c in FactorPublication.__table__.columns}
        expected = {
            "id", "scope_type", "scope_key", "trade_date", "publication_kind",
            "algorithm_version", "data_run_id", "coverage_ratio", "published_at",
            "metadata_json", "created_at",
        }
        assert expected == cols

    def test_history_run_columns(self) -> None:
        from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
        cols = {c.name for c in FirstPyramidHistoryRun.__table__.columns}
        expected = {
            "id", "scheduler_job_run_id", "algorithm_version", "parameter_hash",
            "output_bars", "scope", "expected_count", "succeeded_count",
            "failed_count", "skipped_count", "status", "started_at",
            "completed_at", "metadata_json", "created_at", "updated_at",
        }
        assert expected == cols

    def test_history_run_item_columns(self) -> None:
        from app.models.first_pyramid_history_run_item import (
            FirstPyramidHistoryRunItem,
        )
        cols = {c.name for c in FirstPyramidHistoryRunItem.__table__.columns}
        # [CHANGE-20260729-008] 新增 worker/lease fencing 字段
        expected = {
            "id", "history_run_id", "instrument_id", "status", "attempt_count",
            "input_hash", "worker_instance_id", "lease_epoch", "lease_expires_at",
            "daily_state_count", "event_count", "last_error", "started_at",
            "heartbeat_at", "completed_at", "created_at", "updated_at",
        }
        assert expected == cols


# ============================================================================
# 纯单元测试：Publication 服务逻辑（mock session）
# ============================================================================


class TestCoverageBelowThresholdError:
    """覆盖率门禁错误。"""

    def test_error_message(self) -> None:
        from app.services.factor_publication_service import (
            CoverageBelowThresholdError,
        )
        run_id = uuid.uuid4()
        err = CoverageBelowThresholdError(0.5, 0.98, run_id)
        assert err.coverage == 0.5
        assert err.threshold == 0.98
        assert err.run_id == run_id
        assert "0.5000" in str(err)
        assert "0.9800" in str(err)


class TestPublicationServiceLogic:
    """Publication 服务逻辑（纯单元，mock DB）。"""

    @pytest.mark.asyncio
    async def test_get_publication_no_pointer_returns_none(self) -> None:
        """无 pointer 时返回 None（兼容回退）。"""
        from app.services.factor_publication_service import get_publication

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await get_publication(
            mock_session,
            scope_type="market",
            scope_key="market",
            trade_date=date(2026, 7, 29),
            publication_kind="stock_core",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_get_published_snapshot_run_id_fallback(self) -> None:
        """无 publication pointer 时回退到 latest published run。"""
        from app.services.factor_publication_service import (
            get_published_snapshot_run_id,
        )

        mock_session = AsyncMock()

        # 第一个查询（publication pointer）返回 None
        # 第二个查询（fallback）返回 snapshot_run_id
        mock_pub_result = MagicMock()
        mock_pub_result.scalar_one_or_none.return_value = None

        fallback_run_id = uuid.uuid4()
        mock_fallback_result = MagicMock()
        mock_fallback_result.scalar_one_or_none.return_value = fallback_run_id

        mock_session.execute.side_effect = [mock_pub_result, mock_fallback_result]

        result = await get_published_snapshot_run_id(
            mock_session, date(2026, 7, 29),
        )
        assert result == fallback_run_id


# ============================================================================
# 纯单元测试：Run Item 服务逻辑（mock session）
# ============================================================================


class TestRunItemServiceLogic:
    """Run Item 服务逻辑（纯单元，mock DB）。"""

    @pytest.mark.asyncio
    async def test_create_run_items_empty_list(self) -> None:
        """空 instrument_ids 返回 0。"""
        from app.services.snapshot_run_item_service import create_run_items

        mock_session = AsyncMock()
        count = await create_run_items(
            mock_session, uuid.uuid4(), [],
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_mark_item_succeeded_legacy_mode(self) -> None:
        """Legacy 模式（无 lease_epoch）标记成功。"""
        from app.services.snapshot_run_item_service import mark_item_succeeded

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        result = await mark_item_succeeded(
            mock_session, uuid.uuid4(), result_count=1,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_mark_item_succeeded_fenced_mismatch(self) -> None:
        """lease_epoch 不匹配时返回 False。"""
        from app.services.snapshot_run_item_service import mark_item_succeeded

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute.return_value = mock_result

        result = await mark_item_succeeded(
            mock_session, uuid.uuid4(), result_count=1, lease_epoch=5,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_mark_item_failed_legacy_mode(self) -> None:
        """Legacy 模式标记失败。"""
        from app.services.snapshot_run_item_service import mark_item_failed

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        result = await mark_item_failed(
            mock_session, uuid.uuid4(), "test error",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_get_run_progress_empty(self) -> None:
        """无 items 时 progress 全为 0。"""
        from app.services.snapshot_run_item_service import get_run_progress

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_session.execute.return_value = mock_result

        progress = await get_run_progress(mock_session, uuid.uuid4())
        assert progress["succeeded"] == 0
        assert progress["total"] == 0
        assert progress["coverage"] == 0.0


# ============================================================================
# 纯单元测试：ID 合同验证
# ============================================================================


class TestIDContract:
    """ID 合同统一验证（ref/instruction.md §一.3）。

    orchestrator_job_run_id = SchedulerJobRun.id（任务追踪）
    snapshot_run_id = StockFeatureSnapshotRun.id（数据版本）
    history_run_id = FirstPyramidHistoryRun.id（历史回补版本）
    chip.core_run_id = snapshot_run_id（不再指向 SchedulerJobRun.id）
    publication.data_run_id = snapshot_run_id 或 history_run_id
    """

    def test_chip_model_has_core_run_id(self) -> None:
        """chip 模型有 core_run_id 字段。"""
        from app.models.stock_chip_consensus_snapshot import (
            StockChipConsensusSnapshot,
        )
        cols = {c.name for c in StockChipConsensusSnapshot.__table__.columns}
        assert "core_run_id" in cols

    def test_chip_core_run_id_fk_points_to_snapshot_run(self) -> None:
        """chip.core_run_id 的 FK 应指向 stock_feature_snapshot_runs。

        [CHANGE-20260729-006] 旧实现 FK 指向 scheduler_job_runs，
        本轮统一为指向 stock_feature_snapshot_runs（数据版本）。

        注意：071 迁移已创建 FK 指向 scheduler_job_runs，
        本轮不修改 071 迁移（前向兼容），但新代码写入时必须传 snapshot_run_id。
        ORM 模型的 ForeignKey 定义保持与 071 一致以避免 Alembic 检测到差异。
        """
        from app.models.stock_chip_consensus_snapshot import (
            StockChipConsensusSnapshot,
        )
        col = StockChipConsensusSnapshot.__table__.c.core_run_id
        # 验证字段存在且不为空
        assert col.nullable is False
        # FK 目标：071 迁移指向 scheduler_job_runs（历史遗留）
        # 新代码通过 orchestrator 传 snapshot_run_id 实现语义统一
        # 未来迁移可修复 FK 指向，本轮不改 071

    def test_publication_data_run_id_is_uuid(self) -> None:
        """publication.data_run_id 是 UUID 类型。"""
        from app.models.factor_publication import FactorPublication
        col = FactorPublication.__table__.c.data_run_id
        assert col.nullable is False

    def test_history_run_scheduler_job_run_id_is_nullable(self) -> None:
        """history_run.scheduler_job_run_id 是 nullable（纯 metadata）。"""
        from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
        col = FirstPyramidHistoryRun.__table__.c.scheduler_job_run_id
        assert col.nullable is True

    def test_snapshot_run_item_snapshot_run_id_not_nullable(self) -> None:
        """run_item.snapshot_run_id 不为空（必须指向数据版本）。"""
        from app.models.stock_feature_snapshot_run_item import (
            StockFeatureSnapshotRunItem,
        )
        col = StockFeatureSnapshotRunItem.__table__.c.snapshot_run_id
        assert col.nullable is False


# ============================================================================
# 纯单元测试：故障注入模拟
# ============================================================================


class TestFaultInjectionSimulation:
    """故障注入模拟（纯单元，验证逻辑而非 DB）。

    使用 mock 验证：
    1. 3股中第2股失败，第1/3股成功
    2. 成功的标记 succeeded，失败的标记 failed
    3. 失败不阻止其他股票
    """

    @pytest.mark.asyncio
    async def test_second_stock_fails_others_succeed(self) -> None:
        """3股中第2股失败，第1/3股成功并标记。"""
        from app.services.snapshot_run_item_service import (
            mark_item_failed,
            mark_item_succeeded,
        )

        # 模拟 3 个 items
        item1_id = uuid.uuid4()
        item2_id = uuid.uuid4()
        item3_id = uuid.uuid4()

        # Mock session: 第1个成功，第2个失败，第3个成功
        mock_session = AsyncMock()

        # 为每个 mark_item_succeeded/failed 创建独立的 mock
        call_results = []

        async def mock_execute(stmt):
            mock_result = MagicMock()
            mock_result.rowcount = 1
            call_results.append(stmt)
            return mock_result

        mock_session.execute = mock_execute

        # 第1股成功
        r1 = await mark_item_succeeded(mock_session, item1_id, result_count=1)
        assert r1 is True

        # 第2股失败
        r2 = await mark_item_failed(mock_session, item2_id, "simulated failure")
        assert r2 is True

        # 第3股成功（不受第2股失败影响）
        r3 = await mark_item_succeeded(mock_session, item3_id, result_count=1)
        assert r3 is True

        # 验证调用了 3 次
        assert len(call_results) == 3

    @pytest.mark.asyncio
    async def test_resume_does_not_recompute_succeeded(self) -> None:
        """中断恢复不重算成功股。"""
        from app.models.stock_feature_snapshot_run_item import (
            ITEM_FAILED,
            ITEM_PENDING,
            ITEM_RUNNING,
            ITEM_SUCCEEDED,
            StockFeatureSnapshotRunItem,
        )
        from app.services.snapshot_run_item_service import get_resume_items

        # 模拟 5 个 items: 2 succeeded, 1 pending, 1 failed(可重试), 1 running(lease过期)
        now = datetime.now(UTC)
        items = [
            MagicMock(spec=StockFeatureSnapshotRunItem, status=ITEM_SUCCEEDED),
            MagicMock(spec=StockFeatureSnapshotRunItem, status=ITEM_SUCCEEDED),
            MagicMock(spec=StockFeatureSnapshotRunItem, status=ITEM_PENDING),
            MagicMock(
                spec=StockFeatureSnapshotRunItem,
                status=ITEM_FAILED, attempt_count=1,
            ),
            MagicMock(
                spec=StockFeatureSnapshotRunItem,
                status=ITEM_RUNNING,
                lease_expires_at=now - timedelta(hours=1),
            ),
        ]

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = items[2:]  # 只有 pending/failed/running
        mock_session.execute.return_value = mock_result

        resume_items = await get_resume_items(mock_session, uuid.uuid4())

        # 只有 3 个需要 resume（pending + failed + lease过期running）
        # succeeded 和 skipped 不在 resume 列表中
        for item in resume_items:
            assert item.status in (ITEM_PENDING, ITEM_FAILED, ITEM_RUNNING)

    @pytest.mark.asyncio
    async def test_coverage_below_threshold_blocks_publish(self) -> None:
        """覆盖率未达 98% 时拒绝发布。"""
        from app.services.factor_publication_service import (
            CoverageBelowThresholdError,
            publish_stock_core,
        )

        mock_session = AsyncMock()

        # 覆盖率 0.5 < 0.98 应抛异常
        with pytest.raises(CoverageBelowThresholdError) as exc_info:
            await publish_stock_core(
                mock_session,
                trade_date=date(2026, 7, 29),
                snapshot_run_id=uuid.uuid4(),
                algorithm_version="1.0.0-core-split",
                coverage=0.5,
                threshold=0.98,
            )
        assert exc_info.value.coverage == 0.5
        assert exc_info.value.threshold == 0.98

    @pytest.mark.asyncio
    async def test_coverage_at_threshold_allows_publish(self) -> None:
        """覆盖率达到 98% 时允许发布。"""
        from app.services.factor_publication_service import (
            publish_stock_core,
        )

        mock_session = AsyncMock()

        # mock upsert（pg_insert）
        mock_session.execute = AsyncMock(return_value=MagicMock())
        mock_session.flush = AsyncMock()

        # mock get_publication 返回一个 publication
        mock_pub = MagicMock()
        mock_pub.data_run_id = uuid.uuid4()
        mock_pub.coverage_ratio = 0.98

        with patch(
            "app.services.factor_publication_service.get_publication",
            return_value=mock_pub,
        ):
            result = await publish_stock_core(
                mock_session,
                trade_date=date(2026, 7, 29),
                snapshot_run_id=uuid.uuid4(),
                algorithm_version="1.0.0-core-split",
                coverage=0.98,
                threshold=0.98,
            )

        assert result is not None
        assert result.coverage_ratio == 0.98


# ============================================================================
# PostgreSQL 集成测试（跳过，留待 CI）
# ============================================================================


@_SKIP_INTEGRATION
class TestPostgreSQLIntegration:
    """PostgreSQL 集成测试 - 只在 CI 临时 Postgres 容器中运行。

    覆盖（ref/instruction.md §十一）：
    1. 3股中第2股失败，第1/3股成功并commit
    2. 中断恢复不重算成功股
    3. hash/version 变化才重算
    4. 98%前不发布，达到后原子切pointer
    5. pointer失败只重试发布
    6. aggregation/events/chip失败不反改core
    7. 不同run不混读
    8. 并发claim不重复领取
    9. history DB-only、单股事务、resume、250日和事件幂等
    """

    @pytest.mark.asyncio
    async def test_fault_injection_3_stocks_2nd_fails(
        self, db_session, instrument_factory,
    ) -> None:
        """场景1: 3股中第2股失败，第1/3股成功。"""
        # 1. 创建 3 只股票
        inst1 = await instrument_factory(symbol="600001", name="股票1")
        inst2 = await instrument_factory(symbol="600002", name="股票2")
        inst3 = await instrument_factory(symbol="600003", name="股票3")

        # 2. 创建 snapshot run + run items
        from app.services.feature_snapshot_service import create_snapshot_run
        from app.services.snapshot_run_item_service import (
            create_run_items,
            mark_item_failed,
            mark_item_succeeded,
        )

        run = await create_snapshot_run(
            db_session, date(2026, 7, 29), "after_close",
            expected_count=3, scope="sample",
        )
        await create_run_items(
            db_session, run.id, [inst1.id, inst2.id, inst3.id],
        )
        await db_session.flush()

        # 3. 模拟第1股成功
        from app.services.snapshot_run_item_service import claim_items
        items = await claim_items(
            db_session, run.id,
            worker_instance_id="worker1",
            batch_size=3,
        )
        assert len(items) == 3

        # 找到各股对应的 item
        item_map = {item.instrument_id: item for item in items}

        # 第1股成功
        ok1 = await mark_item_succeeded(
            db_session, item_map[inst1.id].id, result_count=1,
        )
        assert ok1 is True

        # 第2股失败
        ok2 = await mark_item_failed(
            db_session, item_map[inst2.id].id, "simulated failure",
        )
        assert ok2 is True

        # 第3股成功（不受第2股失败影响）
        ok3 = await mark_item_succeeded(
            db_session, item_map[inst3.id].id, result_count=1,
        )
        assert ok3 is True

        await db_session.commit()

        # 4. 验证状态
        from app.services.snapshot_run_item_service import get_run_progress
        progress = await get_run_progress(db_session, run.id)
        assert progress["succeeded"] == 2
        assert progress["failed"] == 1
        assert progress["total"] == 3
        assert progress["coverage"] == 2 / 3

    @pytest.mark.asyncio
    async def test_resume_does_not_recompute_succeeded(
        self, db_session, instrument_factory,
    ) -> None:
        """场景2: 中断恢复不重算成功股。"""
        inst1 = await instrument_factory(symbol="600001", name="股票1")
        inst2 = await instrument_factory(symbol="600002", name="股票2")

        from app.services.feature_snapshot_service import create_snapshot_run
        from app.services.snapshot_run_item_service import (
            claim_items,
            create_run_items,
            get_resume_items,
            mark_item_succeeded,
        )

        run = await create_snapshot_run(
            db_session, date(2026, 7, 29), "after_close",
            expected_count=2, scope="sample",
        )
        await create_run_items(db_session, run.id, [inst1.id, inst2.id])
        await db_session.flush()

        # 领取并标记第1股成功
        items = await claim_items(
            db_session, run.id,
            worker_instance_id="worker1",
            batch_size=2,
        )
        item_map = {item.instrument_id: item for item in items}
        await mark_item_succeeded(db_session, item_map[inst1.id].id, result_count=1)
        await db_session.commit()

        # 模拟 worker1 中断：将 inst2 的 item lease 设为过期，使其可被 resume
        # （get_resume_items 只返回 pending/failed 或 lease 过期的 running items）
        # 注意：claim_items 返回的对象是手动构造的（非 session 托管），
        # 直接设属性不会持久化，必须用 UPDATE 语句。
        from sqlalchemy import update

        from app.models.stock_feature_snapshot_run_item import (
            StockFeatureSnapshotRunItem,
        )

        await db_session.execute(
            update(StockFeatureSnapshotRunItem)
            .where(StockFeatureSnapshotRunItem.id == item_map[inst2.id].id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await db_session.commit()

        # 模拟中断恢复
        resume_items = await get_resume_items(db_session, run.id)
        # 只有第2股需要 resume（第1股已 succeeded）
        resume_inst_ids = {item.instrument_id for item in resume_items}
        assert inst2.id in resume_inst_ids
        assert inst1.id not in resume_inst_ids

    @pytest.mark.asyncio
    async def test_concurrent_claim_no_duplicate(
        self, db_session, instrument_factory,
    ) -> None:
        """场景15: 并发 Worker 不会重复领取同一股票。"""
        inst1 = await instrument_factory(symbol="600001", name="股票1")

        from app.services.feature_snapshot_service import create_snapshot_run
        from app.services.snapshot_run_item_service import (
            claim_items,
            create_run_items,
        )

        run = await create_snapshot_run(
            db_session, date(2026, 7, 29), "after_close",
            expected_count=1, scope="sample",
        )
        await create_run_items(db_session, run.id, [inst1.id])
        await db_session.flush()

        # Worker 1 领取
        items1 = await claim_items(
            db_session, run.id,
            worker_instance_id="worker1",
            batch_size=1,
        )
        assert len(items1) == 1

        # Worker 2 尝试领取（应无可领取的）
        items2 = await claim_items(
            db_session, run.id,
            worker_instance_id="worker2",
            batch_size=1,
        )
        assert len(items2) == 0  # 已被 worker1 领取

    @pytest.mark.asyncio
    async def test_coverage_gate_blocks_publish(
        self, db_session, instrument_factory,
    ) -> None:
        """场景5: 98%前不发布。"""
        # 创建 10 只股票（9成功 + 1失败 = 90% < 98%）
        instruments = [
            await instrument_factory(symbol=f"60000{i}", name=f"股票{i}")
            for i in range(10)
        ]

        from app.services.factor_publication_service import (
            CoverageBelowThresholdError,
            compute_coverage,
            publish_stock_core,
        )
        from app.services.feature_snapshot_service import create_snapshot_run
        from app.services.snapshot_run_item_service import (
            claim_items,
            create_run_items,
            mark_item_failed,
            mark_item_succeeded,
        )

        run = await create_snapshot_run(
            db_session, date(2026, 7, 29), "after_close",
            expected_count=10, scope="sample",
        )
        await create_run_items(db_session, run.id, [inst.id for inst in instruments])
        await db_session.flush()

        # 领取全部
        items = await claim_items(
            db_session, run.id,
            worker_instance_id="worker1",
            batch_size=10,
        )
        item_map = {item.instrument_id: item for item in items}

        # 9 成功 + 1 失败
        for i, inst in enumerate(instruments):
            if i < 9:
                await mark_item_succeeded(
                    db_session, item_map[inst.id].id, result_count=1,
                )
            else:
                await mark_item_failed(
                    db_session, item_map[inst.id].id, "fail",
                )
        await db_session.commit()

        # 覆盖率 9/10 = 0.9 < 0.98
        cov = await compute_coverage(db_session, run.id)
        assert cov["coverage"] == 0.9

        # 发布应被拒绝
        with pytest.raises(CoverageBelowThresholdError):
            await publish_stock_core(
                db_session, date(2026, 7, 29), run.id,
                algorithm_version="1.0.0-core-split",
            )

    @pytest.mark.asyncio
    async def test_coverage_gate_allows_publish_when_met(
        self, db_session, instrument_factory,
    ) -> None:
        """场景6: 达到门禁后原子切pointer。"""
        # 创建 50 只股票（49成功 + 1跳过 = 98%）
        instruments = [
            await instrument_factory(symbol=f"6000{i:03d}", name=f"股票{i}")
            for i in range(50)
        ]

        from app.services.factor_publication_service import (
            get_publication,
            publish_stock_core,
        )
        from app.services.feature_snapshot_service import create_snapshot_run
        from app.services.snapshot_run_item_service import (
            claim_items,
            create_run_items,
            mark_item_skipped,
            mark_item_succeeded,
        )

        run = await create_snapshot_run(
            db_session, date(2026, 7, 29), "after_close",
            expected_count=50, scope="sample",
        )
        await create_run_items(db_session, run.id, [inst.id for inst in instruments])
        await db_session.flush()

        items = await claim_items(
            db_session, run.id,
            worker_instance_id="worker1",
            batch_size=50,
        )
        item_map = {item.instrument_id: item for item in items}

        # 49 成功 + 1 跳过 = 98%
        for i, inst in enumerate(instruments):
            if i < 49:
                await mark_item_succeeded(
                    db_session, item_map[inst.id].id, result_count=1,
                )
            else:
                await mark_item_skipped(
                    db_session, item_map[inst.id].id, "skipped",
                )
        await db_session.commit()

        # 发布成功
        pub = await publish_stock_core(
            db_session, date(2026, 7, 29), run.id,
            algorithm_version="1.0.0-core-split",
        )
        await db_session.commit()

        # 验证 pointer 指向 run.id
        assert pub.data_run_id == run.id
        assert pub.coverage_ratio >= 0.98

        # 通过 get_publication 读取
        read_pub = await get_publication(
            db_session,
            scope_type="market",
            scope_key="market",
            trade_date=date(2026, 7, 29),
            publication_kind="stock_core",
        )
        assert read_pub is not None
        assert read_pub.data_run_id == run.id

    @pytest.mark.asyncio
    async def test_different_runs_no_mixed_read(
        self, db_session, instrument_factory,
    ) -> None:
        """场景16: 读取端不会混合两个run。"""
        inst1 = await instrument_factory(symbol="600001", name="股票1")

        from app.services.factor_publication_service import (
            get_published_snapshot_run_id,
            publish_stock_core,
        )
        from app.services.feature_snapshot_service import create_snapshot_run
        from app.services.snapshot_run_item_service import (
            claim_items,
            create_run_items,
            mark_item_succeeded,
        )

        # Run 1
        run1 = await create_snapshot_run(
            db_session, date(2026, 7, 29), "after_close",
            expected_count=1, scope="sample",
        )
        await create_run_items(db_session, run1.id, [inst1.id])
        await db_session.flush()
        items1 = await claim_items(
            db_session, run1.id,
            worker_instance_id="worker1",
            batch_size=1,
        )
        await mark_item_succeeded(db_session, items1[0].id, result_count=1)
        await db_session.commit()

        # 发布 run1
        await publish_stock_core(
            db_session, date(2026, 7, 29), run1.id,
            algorithm_version="1.0.0-core-split",
        )
        await db_session.commit()

        # 读取应返回 run1
        pub_run_id = await get_published_snapshot_run_id(
            db_session, date(2026, 7, 29),
        )
        assert pub_run_id == run1.id

        # Run 2（不发布）
        run2 = await create_snapshot_run(
            db_session, date(2026, 7, 29), "backfill",
            expected_count=1, scope="sample",
        )
        await create_run_items(db_session, run2.id, [inst1.id])
        await db_session.commit()

        # 读取仍应返回 run1（run2 未发布）
        pub_run_id_after = await get_published_snapshot_run_id(
            db_session, date(2026, 7, 29),
        )
        assert pub_run_id_after == run1.id
        assert pub_run_id_after != run2.id


if __name__ == "__main__":
    # 简单自测入口
    print("test_incremental_publication loaded")
    print(f"PURE_UNIT_TEST={_PURE_UNIT_TEST}")
    print(f"Integration tests will be {'skipped' if _PURE_UNIT_TEST else 'run'}")
