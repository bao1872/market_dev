"""R8 Job/Queue/Lock 测试 - 覆盖 JobRun/Outbox ORM、Redis 队列、分布式锁、幂等键。

测试策略：
- 纯函数测试（build_hash、manifest_validator）：直接验证
- ORM 模型元数据测试：验证表名/列名/索引
- Redis 依赖测试：使用 unittest.mock.AsyncMock 模拟 Redis 客户端
- DB 依赖测试：使用 mock 模拟 AsyncSession

不依赖外部 Redis/PostgreSQL 服务，确保测试可在 CI 环境运行。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ==================== ORM 模型元数据测试 ====================


class TestJobRunModel:
    """JobRun ORM 模型元数据测试。"""

    def test_table_name(self) -> None:
        from app.models.job import JobRun

        assert JobRun.__tablename__ == "job_runs"

    def test_columns(self) -> None:
        from app.models.job import JobRun

        cols = {c.name for c in JobRun.__table__.columns}
        expected = {
            "id", "job_type", "status", "payload", "result",
            "started_at", "finished_at", "error", "created_at",
        }
        assert expected.issubset(cols), f"缺失列: {expected - cols}"

    def test_indexes(self) -> None:
        from app.models.job import JobRun

        idx_names = {idx.name for idx in JobRun.__table__.indexes}
        assert "ix_job_runs_type_status" in idx_names


class TestOutboxModel:
    """Outbox ORM 模型元数据测试。"""

    def test_table_name(self) -> None:
        from app.models.outbox import Outbox

        assert Outbox.__tablename__ == "outbox"

    def test_columns(self) -> None:
        from app.models.outbox import Outbox

        cols = {c.name for c in Outbox.__table__.columns}
        expected = {
            "id", "aggregate_type", "aggregate_id", "event_type",
            "payload", "headers", "status", "retry_count",
            "created_at", "processed_at",
        }
        assert expected.issubset(cols), f"缺失列: {expected - cols}"

    def test_indexes(self) -> None:
        from app.models.outbox import Outbox

        idx_names = {idx.name for idx in Outbox.__table__.indexes}
        assert "ix_outbox_status_created" in idx_names


# ==================== build_hash 测试 ====================


class TestBuildHash:
    """build_hash 纯函数测试。"""

    def test_same_content_same_hash(self) -> None:
        from app.services.build_hash import compute_build_hash

        manifest = {
            "strategy_id": "test",
            "version": "1.0.0",
            "entrypoint": "test:Test",
        }
        h1 = compute_build_hash(manifest)
        h2 = compute_build_hash(manifest)
        assert h1 == h2, "相同内容应产生相同哈希"

    def test_different_content_different_hash(self) -> None:
        from app.services.build_hash import compute_build_hash

        m1 = {"strategy_id": "test", "version": "1.0.0", "entrypoint": "a:A"}
        m2 = {"strategy_id": "test", "version": "1.1.0", "entrypoint": "a:A"}
        assert compute_build_hash(m1) != compute_build_hash(m2)

    def test_hash_length_64(self) -> None:
        from app.services.build_hash import compute_build_hash

        h = compute_build_hash({"a": 1})
        assert len(h) == 64, "SHA256 哈希应为 64 字符"

    def test_schema_affects_hash(self) -> None:
        from app.services.build_hash import compute_build_hash

        manifest = {"strategy_id": "test", "entrypoint": "a:A"}
        h1 = compute_build_hash(manifest, schema=None)
        h2 = compute_build_hash(manifest, schema={"extra": True})
        assert h1 != h2, "不同 schema 应产生不同哈希"

    def test_entrypoint_from_manifest(self) -> None:
        from app.services.build_hash import compute_build_hash

        manifest = {"strategy_id": "test", "entrypoint": "test:Test"}
        h1 = compute_build_hash(manifest)  # entrypoint 从 manifest 读取
        h2 = compute_build_hash(manifest, entrypoint="test:Test")  # 显式传入
        assert h1 == h2, "显式 entrypoint 与 manifest 内 entrypoint 一致时应相同"


# ==================== manifest_validator 测试 ====================


class TestManifestValidator:
    """Manifest 校验器测试。"""

    def test_valid_manifest_passes(self) -> None:
        from app.services.manifest_validator import validate_manifest

        manifest = {
            "strategy_id": "dsa_selector",
            "kind": "selector",
            "version": "1.1.0",
            "display_name": "DSA 方向稳定性选股",
            "entrypoint": "strategies.selectors.dsa:DSASelector",
            "input": {"bar_frequency": "1d", "min_bars": 360},
            "parameters": [
                {
                    "key": "algorithm.lookback",
                    "type": "integer",
                    "default": 360,
                    "allowed_scopes": ["strategy"],
                }
            ],
            "outputs": [{"key": "dsa_dir_bars", "type": "integer"}],
            "capabilities": {"composable": True},
        }
        validate_manifest(manifest)  # 不抛异常即通过

    def test_missing_required_field_fails(self) -> None:
        from app.services.manifest_validator import (
            ManifestValidationError,
            validate_manifest,
        )

        bad_manifest = {"strategy_id": "test"}  # 缺少多个必填字段
        with pytest.raises(ManifestValidationError) as exc_info:
            validate_manifest(bad_manifest)
        assert len(exc_info.value.errors) > 0
        # 错误信息应包含字段路径
        assert "kind" in str(exc_info.value) or "version" in str(exc_info.value)

    def test_invalid_kind_fails(self) -> None:
        from app.services.manifest_validator import (
            ManifestValidationError,
            validate_manifest,
        )

        manifest = {
            "strategy_id": "test",
            "kind": "invalid_kind",  # 非 selector/monitor
            "version": "1.0.0",
            "entrypoint": "test:Test",
            "input": {"bar_frequency": "1d", "min_bars": 360},
            "parameters": [],
            "outputs": [],
            "capabilities": {"composable": True},
        }
        with pytest.raises(ManifestValidationError):
            validate_manifest(manifest)

    def test_error_contains_path(self) -> None:
        from app.services.manifest_validator import (
            ManifestValidationError,
            validate_manifest,
        )

        # capabilities 缺少必填 composable
        manifest = {
            "strategy_id": "test",
            "kind": "selector",
            "version": "1.0.0",
            "entrypoint": "test:Test",
            "input": {"bar_frequency": "1d", "min_bars": 360},
            "parameters": [],
            "outputs": [],
            "capabilities": {},  # 缺少 composable
        }
        with pytest.raises(ManifestValidationError) as exc_info:
            validate_manifest(manifest)
        # 应有指向 capabilities/composable 的错误
        paths = [
            "/".join(str(p) for p in err["path"])
            for err in exc_info.value.errors
        ]
        assert any("capabilities" in p for p in paths)


# ==================== 分布式锁测试（mock Redis） ====================


class TestDistributedLock:
    """分布式锁测试 - mock Redis 客户端。"""

    @pytest.mark.asyncio
    async def test_acquire_lock_success(self) -> None:
        from app.services import distributed_lock

        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        with patch.object(distributed_lock, "get_redis", return_value=mock_redis):
            holder = await distributed_lock.acquire_lock(
                "test_lock", ttl=10, holder="holder-1"
            )
        assert holder == "holder-1"
        mock_redis.set.assert_called_once()
        args, kwargs = mock_redis.set.call_args
        assert args[0] == "lock:test_lock"
        assert args[1] == "holder-1"
        assert kwargs.get("nx") is True
        assert kwargs.get("ex") == 10

    @pytest.mark.asyncio
    async def test_acquire_lock_failure(self) -> None:
        from app.services import distributed_lock

        mock_redis = AsyncMock()
        mock_redis.set.return_value = None  # 锁已被持有
        with patch.object(distributed_lock, "get_redis", return_value=mock_redis):
            holder = await distributed_lock.acquire_lock("test_lock", ttl=10)
        assert holder is None

    @pytest.mark.asyncio
    async def test_acquire_lock_auto_generate_holder(self) -> None:
        from app.services import distributed_lock

        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        with patch.object(distributed_lock, "get_redis", return_value=mock_redis):
            holder = await distributed_lock.acquire_lock("test_lock", ttl=10)
        assert holder is not None
        assert len(holder) > 0  # 自动生成的 holder 非空

    @pytest.mark.asyncio
    async def test_acquire_lock_invalid_ttl(self) -> None:
        from app.services import distributed_lock

        with pytest.raises(ValueError, match="ttl"):
            await distributed_lock.acquire_lock("test", ttl=0)

    @pytest.mark.asyncio
    async def test_release_lock_success(self) -> None:
        from app.services import distributed_lock

        mock_redis = AsyncMock()
        mock_redis.eval.return_value = 1  # Lua 脚本返回 1（释放成功）
        with patch.object(distributed_lock, "get_redis", return_value=mock_redis):
            result = await distributed_lock.release_lock("test_lock", "holder-1")
        assert result is True
        mock_redis.eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_lock_wrong_holder(self) -> None:
        from app.services import distributed_lock

        mock_redis = AsyncMock()
        mock_redis.eval.return_value = 0  # holder 不匹配
        with patch.object(distributed_lock, "get_redis", return_value=mock_redis):
            result = await distributed_lock.release_lock("test_lock", "wrong-holder")
        assert result is False

    @pytest.mark.asyncio
    async def test_renew_lock_success(self) -> None:
        from app.services import distributed_lock

        mock_redis = AsyncMock()
        mock_redis.eval.return_value = 1
        with patch.object(distributed_lock, "get_redis", return_value=mock_redis):
            result = await distributed_lock.renew_lock("test_lock", ttl=30, holder="h1")
        assert result is True

    @pytest.mark.asyncio
    async def test_renew_lock_wrong_holder(self) -> None:
        from app.services import distributed_lock

        mock_redis = AsyncMock()
        mock_redis.eval.return_value = 0
        with patch.object(distributed_lock, "get_redis", return_value=mock_redis):
            result = await distributed_lock.renew_lock("test_lock", ttl=30, holder="h1")
        assert result is False


# ==================== 幂等键测试（mock Redis） ====================


class TestIdempotency:
    """幂等键测试 - mock Redis 客户端。"""

    @pytest.mark.asyncio
    async def test_check_and_record_first_time(self) -> None:
        from app.services import idempotency

        mock_redis = AsyncMock()
        mock_redis.set.return_value = True  # 首次设置成功
        with patch.object(idempotency, "get_redis", return_value=mock_redis):
            result = await idempotency.check_and_record("test_key", ttl=60)
        assert result is True
        mock_redis.set.assert_called_once()
        args, kwargs = mock_redis.set.call_args
        assert args[0] == "idem:test_key"
        assert kwargs.get("nx") is True
        assert kwargs.get("ex") == 60

    @pytest.mark.asyncio
    async def test_check_and_record_duplicate(self) -> None:
        from app.services import idempotency

        mock_redis = AsyncMock()
        mock_redis.set.return_value = None  # key 已存在
        with patch.object(idempotency, "get_redis", return_value=mock_redis):
            result = await idempotency.check_and_record("test_key", ttl=60)
        assert result is False

    @pytest.mark.asyncio
    async def test_check_and_record_empty_key(self) -> None:
        from app.services import idempotency

        with pytest.raises(ValueError, match="key"):
            await idempotency.check_and_record("", ttl=60)

    @pytest.mark.asyncio
    async def test_check_and_record_invalid_ttl(self) -> None:
        from app.services import idempotency

        with pytest.raises(ValueError, match="ttl"):
            await idempotency.check_and_record("test", ttl=0)

    @pytest.mark.asyncio
    async def test_check_exists(self) -> None:
        from app.services import idempotency

        mock_redis = AsyncMock()
        mock_redis.get.return_value = "1"
        with patch.object(idempotency, "get_redis", return_value=mock_redis):
            result = await idempotency.check("test_key")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_not_exists(self) -> None:
        from app.services import idempotency

        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        with patch.object(idempotency, "get_redis", return_value=mock_redis):
            result = await idempotency.check("test_key")
        assert result is False


# ==================== Job 队列测试（mock Redis + DB） ====================


class TestJobQueue:
    """Job 队列测试 - mock Redis 客户端与 DB 会话。"""

    @pytest.mark.asyncio
    async def test_enqueue_new_job(self) -> None:
        """测试入队新任务。"""
        from app.models.job import JobRun
        from app.services import job_queue

        # mock Redis：幂等键不存在
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # 幂等键不存在
        mock_redis.set.return_value = True  # SET NX 成功

        # mock DB 会话
        mock_db = AsyncMock()
        added_objects: list[JobRun] = []

        def mock_add(obj):
            if isinstance(obj, JobRun):
                added_objects.append(obj)

        mock_db.add = MagicMock(side_effect=mock_add)

        async def mock_flush():
            for obj in added_objects:
                if obj.id is None:
                    obj.id = uuid4()

        mock_db.flush = AsyncMock(side_effect=mock_flush)

        with patch.object(job_queue, "get_redis", return_value=mock_redis):
            job_run = await job_queue.enqueue(
                mock_db,
                job_type="strategy_run",
                payload={"date": "2026-06-18"},
                idempotency_key="idem-001",
            )

        assert job_run.job_type == "strategy_run"
        assert job_run.status == "pending"
        assert job_run.payload == {"date": "2026-06-18"}
        # 验证 Redis 调用
        mock_redis.get.assert_called_with("job:idem:idem-001")
        mock_redis.lpush.assert_called_once()
        lpush_args = mock_redis.lpush.call_args
        assert lpush_args.args[0] == "job:queue:strategy_run"

    @pytest.mark.asyncio
    async def test_enqueue_idempotent_duplicate(self) -> None:
        """测试幂等键去重：相同 idempotency_key 不重复入队。"""
        from app.models.job import JobRun
        from app.services import job_queue

        existing_job_id = uuid4()
        existing_job = JobRun(
            id=existing_job_id,
            job_type="strategy_run",
            status="pending",
            payload={"date": "2026-06-18"},
        )

        # mock Redis：幂等键已存在
        mock_redis = AsyncMock()
        mock_redis.get.return_value = str(existing_job_id)

        # mock DB：返回已存在的 JobRun
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_job
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch.object(job_queue, "get_redis", return_value=mock_redis):
            job_run = await job_queue.enqueue(
                mock_db,
                job_type="strategy_run",
                payload={"date": "2026-06-18"},
                idempotency_key="idem-001",
            )

        # 应返回已存在的 JobRun，不重复入队
        assert job_run.id == existing_job_id
        # 不应调用 lpush（不重复入队）
        mock_redis.lpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_enqueue_empty_job_type(self) -> None:
        """测试空 job_type 抛出 ValueError。"""
        from app.services import job_queue

        mock_db = AsyncMock()
        with pytest.raises(ValueError, match="job_type"):
            await job_queue.enqueue(
                mock_db,
                job_type="",
                payload={},
                idempotency_key="idem-001",
            )

    @pytest.mark.asyncio
    async def test_enqueue_empty_idempotency_key(self) -> None:
        """测试空 idempotency_key 抛出 ValueError。"""
        from app.services import job_queue

        mock_db = AsyncMock()
        with pytest.raises(ValueError, match="idempotency_key"):
            await job_queue.enqueue(
                mock_db,
                job_type="strategy_run",
                payload={},
                idempotency_key="",
            )

    @pytest.mark.asyncio
    async def test_dequeue_success(self) -> None:
        """测试出队。"""
        from app.services import job_queue

        mock_redis = AsyncMock()
        # BRPOP 返回 (key, value)
        mock_redis.brpop.return_value = ("job:queue:strategy_run", "job-id-123")

        with patch.object(job_queue, "get_redis", return_value=mock_redis):
            result = await job_queue.dequeue(["strategy_run"], timeout=5)

        assert result is not None
        job_type, job_id = result
        assert job_type == "strategy_run"
        assert job_id == "job-id-123"

    @pytest.mark.asyncio
    async def test_dequeue_timeout(self) -> None:
        """测试出队超时返回 None。"""
        from app.services import job_queue

        mock_redis = AsyncMock()
        mock_redis.brpop.return_value = None  # 超时

        with patch.object(job_queue, "get_redis", return_value=mock_redis):
            result = await job_queue.dequeue(["strategy_run"], timeout=1)

        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_empty_job_types(self) -> None:
        """测试空 job_types 抛出 ValueError。"""
        from app.services import job_queue

        with pytest.raises(ValueError, match="job_types"):
            await job_queue.dequeue([], timeout=1)

    @pytest.mark.asyncio
    async def test_update_job_status_success(self) -> None:
        """测试更新任务状态为 succeeded。"""
        from app.models.job import JobRun
        from app.services import job_queue

        job = JobRun(
            id=uuid4(),
            job_type="strategy_run",
            status="running",
            payload={},
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        # mock Redis 元数据
        mock_redis = AsyncMock()
        mock_redis.get.return_value = json.dumps({"job_id": str(job.id), "status": "running"})

        with patch.object(job_queue, "get_redis", return_value=mock_redis):
            updated = await job_queue.update_job_status(
                mock_db,
                job.id,
                status="succeeded",
                result={"count": 10},
            )

        assert updated.status == "succeeded"
        assert updated.result == {"count": 10}
        assert updated.finished_at is not None

    @pytest.mark.asyncio
    async def test_update_job_status_not_found(self) -> None:
        """测试更新不存在的任务抛出 ValueError。"""
        from app.services import job_queue

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="任务不存在"):
            await job_queue.update_job_status(
                mock_db,
                uuid4(),
                status="succeeded",
            )


# ==================== Outbox 测试（mock Redis + DB） ====================


class TestOutbox:
    """Outbox 模式测试 - mock Redis 客户端与 DB 会话。"""

    @pytest.mark.asyncio
    async def test_write_outbox(self) -> None:
        """测试写入 outbox 记录。"""
        from app.models.outbox import Outbox
        from app.services import outbox_relay

        mock_db = AsyncMock()
        added: list[Outbox] = []

        def mock_add(obj):
            if isinstance(obj, Outbox):
                added.append(obj)

        mock_db.add = MagicMock(side_effect=mock_add)
        mock_db.flush = AsyncMock()

        record = await outbox_relay.write_outbox(
            mock_db,
            event_type="selector.run.completed",
            payload={"run_id": "123"},
            aggregate_type="strategy_run",
        )

        assert record.event_type == "selector.run.completed"
        assert record.status == "pending"
        assert record.retry_count == 0
        assert record.payload == {"run_id": "123"}
        assert len(added) == 1

    @pytest.mark.asyncio
    async def test_write_outbox_empty_event_type(self) -> None:
        """测试空 event_type 抛出 ValueError。"""
        from app.services import outbox_relay

        mock_db = AsyncMock()
        with pytest.raises(ValueError, match="event_type"):
            await outbox_relay.write_outbox(
                mock_db,
                event_type="",
                payload={},
                aggregate_type="strategy_run",
            )

    @pytest.mark.asyncio
    async def test_relay_outbox_success(self) -> None:
        """测试 outbox relay 投递成功。"""
        from app.models.outbox import Outbox
        from app.services import outbox_relay

        # 创建 pending 记录
        records = [
            Outbox(
                id=uuid4(),
                aggregate_type="strategy_run",
                event_type="selector.run.completed",
                payload={"run_id": str(i)},
                headers={},
                status="pending",
                retry_count=0,
                created_at=datetime.now(UTC),
            )
            for i in range(3)
        ]

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = records
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        mock_redis = AsyncMock()
        mock_redis.lpush = AsyncMock(return_value=1)

        with patch.object(outbox_relay, "get_redis", return_value=mock_redis):
            count = await outbox_relay.relay_outbox(mock_db, batch_size=10)

        assert count == 3
        assert mock_redis.lpush.call_count == 3
        for r in records:
            assert r.status == "processed"
            assert r.processed_at is not None

    @pytest.mark.asyncio
    async def test_relay_outbox_empty(self) -> None:
        """测试无 pending 记录时返回 0。"""
        from app.services import outbox_relay

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        mock_redis = AsyncMock()

        with patch.object(outbox_relay, "get_redis", return_value=mock_redis):
            count = await outbox_relay.relay_outbox(mock_db, batch_size=10)

        assert count == 0
        mock_redis.lpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_relay_outbox_failure_increments_retry(self) -> None:
        """测试投递失败时 retry_count 递增。"""
        from app.models.outbox import Outbox
        from app.services import outbox_relay

        record = Outbox(
            id=uuid4(),
            aggregate_type="strategy_run",
            event_type="selector.run.completed",
            payload={"run_id": "1"},
            headers={},
            status="pending",
            retry_count=0,
            created_at=datetime.now(UTC),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [record]
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        # mock Redis lpush 抛出异常
        mock_redis = AsyncMock()
        mock_redis.lpush = AsyncMock(side_effect=Exception("Redis 连接失败"))

        with patch.object(outbox_relay, "get_redis", return_value=mock_redis):
            count = await outbox_relay.relay_outbox(mock_db, batch_size=10)

        # 投递失败，count=0
        assert count == 0
        # retry_count 递增
        assert record.retry_count == 1
        # 仍为 pending（未超过 max_retry）
        assert record.status == "pending"

    @pytest.mark.asyncio
    async def test_relay_outbox_max_retry_marks_failed(self) -> None:
        """测试超过最大重试次数标记为 failed。"""
        from app.models.outbox import Outbox
        from app.services import outbox_relay

        record = Outbox(
            id=uuid4(),
            aggregate_type="strategy_run",
            event_type="selector.run.completed",
            payload={"run_id": "1"},
            headers={},
            status="pending",
            retry_count=4,  # 已重试 4 次，max_retry=5 时再失败即超过
            created_at=datetime.now(UTC),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [record]
        mock_db.execute = AsyncMock(return_value=mock_result)
        mock_db.flush = AsyncMock()

        mock_redis = AsyncMock()
        mock_redis.lpush = AsyncMock(side_effect=Exception("Redis 连接失败"))

        with patch.object(outbox_relay, "get_redis", return_value=mock_redis):
            await outbox_relay.relay_outbox(mock_db, batch_size=10, max_retry=5)

        # retry_count 达到 5，标记为 failed
        assert record.retry_count == 5
        assert record.status == "failed"


# ==================== 策略服务测试（mock DB） ====================


class TestStrategyService:
    """策略服务测试 - mock DB 会话。"""

    @pytest.mark.asyncio
    async def test_create_strategy_new(self) -> None:
        """测试创建新策略（mock DB）。"""
        from app.services import strategy_service

        manifest = {
            "strategy_id": "test_selector",
            "kind": "selector",
            "version": "1.0.0",
            "display_name": "Test Selector",
            "entrypoint": "test:Test",
            "input": {"bar_frequency": "1d", "min_bars": 360},
            "parameters": [],
            "outputs": [],
            "capabilities": {"composable": True},
        }

        # mock DB：definition 不存在
        mock_db = AsyncMock()
        mock_result_def = MagicMock()
        mock_result_def.scalar_one_or_none.return_value = None
        # version 不存在
        mock_result_ver = MagicMock()
        mock_result_ver.scalar_one_or_none.return_value = None

        mock_db.execute = AsyncMock(side_effect=[mock_result_def, mock_result_ver])

        added_objects = []

        def mock_add(obj):
            added_objects.append(obj)

        mock_db.add = MagicMock(side_effect=mock_add)

        async def mock_flush():
            for obj in added_objects:
                if not hasattr(obj, "id") or obj.id is None:
                    obj.id = uuid4()

        mock_db.flush = AsyncMock(side_effect=mock_flush)

        definition, version = await strategy_service.create_strategy(
            mock_db, manifest
        )

        assert definition.strategy_key == "test_selector"
        assert definition.kind == "selector"
        assert version.version == "1.0.0"
        assert version.status == "draft"
        assert len(version.build_hash) == 64

    @pytest.mark.asyncio
    async def test_create_strategy_invalid_manifest(self) -> None:
        """测试无效 manifest 抛出 ManifestValidationError。"""
        from app.services import strategy_service
        from app.services.manifest_validator import ManifestValidationError

        bad_manifest = {"strategy_id": "test"}  # 缺少必填字段
        mock_db = AsyncMock()

        with pytest.raises(ManifestValidationError):
            await strategy_service.create_strategy(mock_db, bad_manifest)


if __name__ == "__main__":
    # 自测入口：可直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
