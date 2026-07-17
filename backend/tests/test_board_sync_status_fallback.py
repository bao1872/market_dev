"""板块同步状态回退 + 事件/metadata 持久化测试（PR #77 §三.3 / §三.4 收口）。

测试内容：
1. _get_board_sync_status_from_job：Redis 缺失时从 SchedulerJobRun.metadata_json 回退
   - 最近 after-close job 含 board_sync_result 时返回该 dict
   - 无任何 job 含 board_sync_result 时返回 None
   - 多个 job 时返回最近的
   - metadata_json 非法 JSON 时跳过
   - 异常时返回 None（不传播）

2. _record_board_sync_outcome：syncing_boards 结果写入 job_run_events + metadata_json
   - 成功：写 info 事件 + board_sync_result metadata
   - 失败：写 warn 事件 + error_code/reused_previous_snapshot
   - 跳过：写 info 事件 + reason_code
   - 保留已有 metadata 字段（不覆盖 orchestrator_status/trade_date）

3. get_market_boards 集成：Redis 缺失 + DB 回退
   - Redis 缺失 + job metadata 有 board_sync_result → 使用回退 source/status
   - Redis 缺失 + 无 job metadata + DB 有数据 → source="unknown"
   - Redis 有状态 → 使用 Redis 状态（不回退）

测试策略：
- 使用 db_session fixture（savepoint 隔离）
- 直接调用内部函数 + mock get_sync_status 模拟 Redis 缺失
- 创建 SchedulerJobRun + metadata_json 模拟 after-close 历史记录
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_board import MarketBoard
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    _record_board_sync_outcome,
)
from app.services.job_run_event_service import list_events
from app.services.market_stocks_service import (
    _get_board_sync_status_from_job,
    get_market_boards,
)


class _FakeSessionContext:
    """模拟 async with AsyncSessionLocal() 返回 db_session（不真正关闭）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *args) -> bool:
        return False


# =============================================================================
# 辅助函数
# =============================================================================


async def _create_after_close_job_with_board_result(
    db_session: AsyncSession,
    *,
    board_sync_result: dict | None,
    orchestrator_status: str = "succeeded",
    trade_date: date = date(2026, 7, 17),
    extra_meta: dict | None = None,
    created_at: datetime | None = None,
) -> SchedulerJobRun:
    """创建带 board_sync_result 的 after-close SchedulerJobRun。

    board_sync_result=None 时仅写 orchestrator_status（模拟未执行同步的 job）。
    """
    meta: dict = {
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date.isoformat(),
    }
    if board_sync_result is not None:
        meta["board_sync_result"] = board_sync_result
    if extra_meta:
        meta.update(extra_meta)

    now = created_at or datetime.now(UTC)
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status="succeeded",
        scheduled_at=now,
        started_at=now,
        finished_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
        created_at=now,
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


async def _create_market_board(
    db_session: AsyncSession,
    *,
    name: str = "测试行业",
    board_type: str = "industry",
    external_code: str = "wc:i:test",
) -> MarketBoard:
    """创建测试板块记录。"""
    board = MarketBoard(
        externalCode=external_code,
        name=name,
        type=board_type,
        updatedAt=datetime.now(UTC),
    )
    db_session.add(board)
    await db_session.flush()
    return board


# =============================================================================
# 1. _get_board_sync_status_from_job 测试
# =============================================================================


class TestGetBoardSyncStatusFromJob:
    """Redis 缺失时从 job metadata 回退读取板块同步状态。"""

    @pytest.mark.asyncio
    async def test_returns_board_sync_result_when_present(
        self, db_session: AsyncSession
    ) -> None:
        """job metadata 含 board_sync_result 时应返回该 dict。"""
        board_result = {
            "status": "succeeded",
            "source": "wencai",
            "raw_rows": 1000,
            "resolved": 950,
            "unresolved": 50,
        }
        await _create_after_close_job_with_board_result(
            db_session, board_sync_result=board_result
        )

        result = await _get_board_sync_status_from_job(db_session)

        assert result is not None
        assert result["status"] == "succeeded"
        assert result["source"] == "wencai"
        assert result["raw_rows"] == 1000

    @pytest.mark.asyncio
    async def test_returns_none_when_no_board_sync_result(
        self, db_session: AsyncSession
    ) -> None:
        """无任何 job 含 board_sync_result 时返回 None。"""
        # 创建不含 board_sync_result 的 job
        await _create_after_close_job_with_board_result(
            db_session, board_sync_result=None
        )

        result = await _get_board_sync_status_from_job(db_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_most_recent_when_multiple_jobs(
        self, db_session: AsyncSession
    ) -> None:
        """多个 job 含 board_sync_result 时返回最近创建的。"""
        # 旧 job（较早 created_at）
        await _create_after_close_job_with_board_result(
            db_session,
            board_sync_result={"status": "succeeded", "source": "wencai", "raw_rows": 100},
            created_at=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        )
        # 新 job（较晚 created_at）
        await _create_after_close_job_with_board_result(
            db_session,
            board_sync_result={"status": "failed", "source": "wencai", "raw_rows": 200},
            created_at=datetime(2026, 7, 17, 10, 0, tzinfo=UTC),
        )

        result = await _get_board_sync_status_from_job(db_session)

        assert result is not None
        # 应返回最近的新 job（failed）
        assert result["status"] == "failed"
        assert result["raw_rows"] == 200

    @pytest.mark.asyncio
    async def test_skips_invalid_json_metadata(
        self, db_session: AsyncSession
    ) -> None:
        """metadata_json 非法 JSON 时应跳过该 job，继续查找。"""
        # 创建非法 JSON 的 job
        now = datetime.now(UTC)
        invalid_job = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date="2026-07-17",
            run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
            status="succeeded",
            scheduled_at=now,
            started_at=now,
            finished_at=now,
            heartbeat_at=now,
            lease_expires_at=now,
            metadata_json="{invalid json}",  # 非法 JSON
            created_at=now,
        )
        db_session.add(invalid_job)
        await db_session.flush()

        # 再创建一个合法的 job
        await _create_after_close_job_with_board_result(
            db_session,
            board_sync_result={"status": "succeeded", "source": "wencai"},
        )

        result = await _get_board_sync_status_from_job(db_session)

        # 应跳过非法 JSON，返回合法 job 的结果
        assert result is not None
        assert result["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_skips_none_metadata(self, db_session: AsyncSession) -> None:
        """metadata_json=None 的 job 应被跳过。"""
        now = datetime.now(UTC)
        job = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date="2026-07-17",
            run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
            status="succeeded",
            scheduled_at=now,
            started_at=now,
            finished_at=now,
            heartbeat_at=now,
            lease_expires_at=now,
            metadata_json=None,
            created_at=now,
        )
        db_session.add(job)
        await db_session.flush()

        result = await _get_board_sync_status_from_job(db_session)

        assert result is None


# =============================================================================
# 2. _record_board_sync_outcome 测试
# =============================================================================


class TestRecordBoardSyncOutcome:
    """syncing_boards 结果写入 job_run_events + metadata_json。

    _record_board_sync_outcome 内部使用 AsyncSessionLocal 创建独立 session，
    测试中 mock AsyncSessionLocal 返回 db_session（savepoint 模式），
    并 mock db_session.commit 为 db_session.flush 避免真正提交外层事务。
    """

    @pytest.mark.asyncio
    async def test_success_writes_info_event_and_metadata(
        self, db_session: AsyncSession
    ) -> None:
        """成功结果应写 info 级别事件 + board_sync_result 到 metadata。"""
        job_run = await _create_after_close_job_with_board_result(
            db_session, board_sync_result=None
        )
        outcome = {
            "status": "succeeded",
            "source": "wencai",
            "raw_rows": 1000,
            "resolved": 950,
            "unresolved": 50,
            "industries": 257,
            "concepts": 388,
            "relations": 73074,
            "duration_seconds": 45.2,
        }

        fake_session_local = MagicMock(return_value=_FakeSessionContext(db_session))
        with patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            new=fake_session_local,
        ), patch.object(db_session, "commit", new=db_session.flush):
            await _record_board_sync_outcome(
                job_run_id=job_run.id,
                outcome=outcome,
                level="info",
                message="板块同步成功: source=wencai, 257 行业/388 概念/73074 关系",
            )

        # 验证 metadata_json 含 board_sync_result
        await db_session.refresh(job_run)
        meta = json.loads(job_run.metadata_json)
        assert "board_sync_result" in meta
        assert meta["board_sync_result"]["status"] == "succeeded"
        assert meta["board_sync_result"]["source"] == "wencai"
        assert meta["board_sync_result"]["raw_rows"] == 1000
        # 保留原有 metadata 字段
        assert meta["orchestrator_status"] == "succeeded"
        assert meta["trade_date"] == "2026-07-17"

        # 验证事件已写入
        events = await list_events(db_session, job_run.id)
        sync_events = [e for e in events if e.step == AfterCloseRunStatus.SYNCING_BOARDS.value]
        assert len(sync_events) >= 1
        event = sync_events[-1]
        assert event.level == "info"
        assert "板块同步成功" in event.message
        assert event.payload["status"] == "succeeded"
        assert event.payload["source"] == "wencai"

    @pytest.mark.asyncio
    async def test_failure_writes_warn_event_with_error_code(
        self, db_session: AsyncSession
    ) -> None:
        """失败结果应写 warn 级别事件 + error_code/reused_previous_snapshot。"""
        job_run = await _create_after_close_job_with_board_result(
            db_session, board_sync_result=None
        )
        outcome = {
            "status": "failed",
            "source": "wencai",
            "error_code": "BOARD_SYNC_PROVIDER_ERROR",
            "reused_previous_snapshot": True,
            "duration_seconds": 5.0,
        }

        fake_session_local = MagicMock(return_value=_FakeSessionContext(db_session))
        with patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            new=fake_session_local,
        ), patch.object(db_session, "commit", new=db_session.flush):
            await _record_board_sync_outcome(
                job_run_id=job_run.id,
                outcome=outcome,
                level="warn",
                message="板块同步失败，软降级复用上次快照",
            )

        await db_session.refresh(job_run)
        meta = json.loads(job_run.metadata_json)
        assert meta["board_sync_result"]["status"] == "failed"
        assert meta["board_sync_result"]["error_code"] == "BOARD_SYNC_PROVIDER_ERROR"
        assert meta["board_sync_result"]["reused_previous_snapshot"] is True

        events = await list_events(db_session, job_run.id)
        sync_events = [e for e in events if e.step == AfterCloseRunStatus.SYNCING_BOARDS.value]
        assert len(sync_events) >= 1
        event = sync_events[-1]
        assert event.level == "warn"
        assert "失败" in event.message
        assert event.payload["error_code"] == "BOARD_SYNC_PROVIDER_ERROR"
        assert event.payload["reused_previous_snapshot"] is True

    @pytest.mark.asyncio
    async def test_skip_writes_info_event_with_reason_code(
        self, db_session: AsyncSession
    ) -> None:
        """跳过结果应写 info 级别事件 + reason_code。"""
        job_run = await _create_after_close_job_with_board_result(
            db_session, board_sync_result=None
        )
        outcome = {
            "status": "skipped",
            "reason_code": "board_sync_disabled",
        }

        fake_session_local = MagicMock(return_value=_FakeSessionContext(db_session))
        with patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            new=fake_session_local,
        ), patch.object(db_session, "commit", new=db_session.flush):
            await _record_board_sync_outcome(
                job_run_id=job_run.id,
                outcome=outcome,
                level="info",
                message="板块同步已跳过: BOARD_SYNC_ENABLED=false",
            )

        await db_session.refresh(job_run)
        meta = json.loads(job_run.metadata_json)
        assert meta["board_sync_result"]["status"] == "skipped"
        assert meta["board_sync_result"]["reason_code"] == "board_sync_disabled"

        events = await list_events(db_session, job_run.id)
        sync_events = [e for e in events if e.step == AfterCloseRunStatus.SYNCING_BOARDS.value]
        assert len(sync_events) >= 1
        event = sync_events[-1]
        assert event.level == "info"
        assert event.payload["reason_code"] == "board_sync_disabled"

    @pytest.mark.asyncio
    async def test_preserves_existing_metadata_fields(
        self, db_session: AsyncSession
    ) -> None:
        """写 board_sync_result 时应保留已有 metadata 字段。"""
        job_run = await _create_after_close_job_with_board_result(
            db_session,
            board_sync_result=None,
            extra_meta={
                "dsa_run_id": str(uuid.uuid4()),
                "last_completed_step": "feature_snapshot",
                "feature_snapshot_progress": "100%",
            },
        )

        fake_session_local = MagicMock(return_value=_FakeSessionContext(db_session))
        with patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            new=fake_session_local,
        ), patch.object(db_session, "commit", new=db_session.flush):
            await _record_board_sync_outcome(
                job_run_id=job_run.id,
                outcome={"status": "succeeded", "source": "wencai"},
                level="info",
                message="板块同步成功",
            )

        await db_session.refresh(job_run)
        meta = json.loads(job_run.metadata_json)
        # 新字段
        assert meta["board_sync_result"]["status"] == "succeeded"
        # 保留的原有字段
        assert "dsa_run_id" in meta
        assert meta["last_completed_step"] == "feature_snapshot"
        assert meta["feature_snapshot_progress"] == "100%"
        assert meta["orchestrator_status"] == "succeeded"


# =============================================================================
# 3. get_market_boards 集成测试：Redis 缺失 + DB 回退
# =============================================================================


class TestGetMarketBoardsRedisFallback:
    """get_market_boards 在 Redis 缺失时从 job metadata 回退。"""

    @pytest.mark.asyncio
    async def test_redis_empty_falls_back_to_job_metadata(
        self, db_session: AsyncSession
    ) -> None:
        """Redis 缺失 + job metadata 含 board_sync_result → 使用回退 source/status。"""
        # 准备 DB 数据
        await _create_market_board(db_session, name="电子", board_type="industry")
        await _create_market_board(
            db_session, name="光刻机", board_type="concept", external_code="wc:c:1"
        )

        # 准备 job metadata 回退源
        await _create_after_close_job_with_board_result(
            db_session,
            board_sync_result={
                "status": "succeeded",
                "source": "wencai",
                "raw_rows": 1000,
            },
        )

        # mock Redis 缺失
        with patch(
            "app.services.board_sync_service.get_sync_status",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await get_market_boards(db_session)

        assert response.available is True
        assert response.source == "wencai"
        assert response.last_attempt_status == "succeeded"
        assert response.stale is False

    @pytest.mark.asyncio
    async def test_redis_empty_no_job_metadata_source_unknown(
        self, db_session: AsyncSession
    ) -> None:
        """Redis 缺失 + 无 job metadata + DB 有数据 → source="unknown"。"""
        await _create_market_board(db_session, name="电子", board_type="industry")

        # 无任何 after-close job 含 board_sync_result
        await _create_after_close_job_with_board_result(
            db_session, board_sync_result=None
        )

        with patch(
            "app.services.board_sync_service.get_sync_status",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await get_market_boards(db_session)

        assert response.available is True
        assert response.source == "unknown"
        assert response.last_attempt_status is None
        assert response.stale is False

    @pytest.mark.asyncio
    async def test_redis_empty_no_data_available_false(
        self, db_session: AsyncSession
    ) -> None:
        """Redis 缺失 + DB 无数据 → available=false, source=None。"""
        # 不创建任何板块数据
        # 也不创建 job metadata
        with patch(
            "app.services.board_sync_service.get_sync_status",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await get_market_boards(db_session)

        assert response.available is False
        assert response.source is None
        assert response.reason_code == "board_provider_unavailable"

    @pytest.mark.asyncio
    async def test_redis_has_status_no_fallback(
        self, db_session: AsyncSession
    ) -> None:
        """Redis 有状态时使用 Redis 状态，不回退到 job metadata。"""
        await _create_market_board(db_session, name="电子", board_type="industry")

        # job metadata 含不同状态（不应被使用）
        await _create_after_close_job_with_board_result(
            db_session,
            board_sync_result={"status": "succeeded", "source": "wencai"},
        )

        # Redis 返回 failed 状态（应优先使用）
        with patch(
            "app.services.board_sync_service.get_sync_status",
            new_callable=AsyncMock,
            return_value={"status": "failed", "source": "wencai"},
        ):
            response = await get_market_boards(db_session)

        assert response.available is True
        assert response.source == "wencai"
        assert response.last_attempt_status == "failed"
        # failed 状态 → stale=True
        assert response.stale is True

    @pytest.mark.asyncio
    async def test_redis_empty_fallback_failed_status_stale(
        self, db_session: AsyncSession
    ) -> None:
        """Redis 缺失 + job metadata 回退为 failed → stale=True。"""
        await _create_market_board(db_session, name="电子", board_type="industry")

        await _create_after_close_job_with_board_result(
            db_session,
            board_sync_result={
                "status": "failed",
                "source": "wencai",
                "error_code": "PROVIDER_ERROR",
            },
        )

        with patch(
            "app.services.board_sync_service.get_sync_status",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await get_market_boards(db_session)

        assert response.available is True
        assert response.source == "wencai"
        assert response.last_attempt_status == "failed"
        assert response.stale is True
