"""Phase 6: 盘后编排 API 端点测试 - force?restart_from=daily_ready + resume 入口。

覆盖 4 个场景：
1. test_force_restart_from_daily_ready_insufficient_coverage_returns_409:
   当日无日线数据，调用 force?restart_from=daily_ready，返 409 + reason=DATA_COVERAGE_INSUFFICIENT
2. test_force_restart_from_daily_ready_sufficient_coverage_resets_queued:
   插入足够的日线数据（覆盖率 ≥ 90%），调用 force?restart_from=daily_ready，
   返 200 + status=queued + metadata.last_completed_step=refreshing_daily
3. test_resume_non_failed_returns_400:
   任务 status=running，调用 resume，返 400
4. test_resume_failed_resets_queued_preserves_checkpoint:
   任务 status=failed + metadata.last_completed_step=refreshing_daily，调用 resume，
   返 200 + status=queued + last_completed_step 保留

测试策略：
- 复用 conftest.py 的 PostgreSQL 测试库 + db_session fixture（事务性回滚）
- 创建 admin 用户 + admin 角色以满足 require_roles("admin")
- 通过 dependency_overrides 注入测试会话（commit 替换为 flush 保持 SAVEPOINT）
- 用 httpx.AsyncClient + ASGITransport 调用真实 FastAPI 路由

字段口径（与权威实现 bars_scheduler_service._check_daily_coverage_and_trigger_dsa 对齐）：
- 活跃股票：instruments.status='active'（非 is_active=true，spec 字段错误）
- 日线数据：bars_daily 表（非 bars WHERE timeframe='d'，bars 是分表设计）
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.main import app
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.user import Role, User, UserRole
from tests.conftest import make_asgi_transport

# ============================================================
# 测试 fixtures
# ============================================================


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession):
    """创建 admin 用户 + admin 角色以满足 require_roles("admin")。

    [AfterCloseEndpoint测试] - 描述: 幂等复用现有 admin 角色，避免重复创建违反 roles_name_key 唯一约束
    """
    from sqlalchemy import select as sa_select

    role_stmt = sa_select(Role).where(Role.name == "admin")
    role_result = await db_session.execute(role_stmt)
    admin_role = role_result.scalar_one_or_none()
    if admin_role is None:
        admin_role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        db_session.add(admin_role)

    admin_user = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
    )
    db_session.add(admin_user)
    db_session.add(UserRole(user_id=admin_user.id, role_id=admin_role.id))
    await db_session.flush()
    return admin_user


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _override_get_db(session: AsyncSession) -> None:
    """覆盖 app 的 get_db 依赖，使其使用测试会话。

    [AfterCloseEndpoint测试] - 描述: 将 endpoint 的 db.commit() 替换为 flush，
    保持 db_session fixture 的 SAVEPOINT（begin_nested）活跃。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        with patch.object(session, "commit", new=AsyncMock(side_effect=session.flush)):
            yield session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db


async def _create_active_instruments(
    db_session: AsyncSession, count: int
) -> list[Instrument]:
    """创建 count 个活跃股票（status='active'），symbol 用 002xxx 模式符合 A 股代码规则。"""
    instruments = []
    for i in range(count):
        inst = Instrument(
            id=uuid.uuid4(),
            # [测试] - symbol 用 6xxxxxx 模式（SH 主板），匹配 stock_symbol_sql_filter
            symbol=f"{random.randint(600000, 699999):06d}",
            name=f"测试标的{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        instruments.append(inst)
    await db_session.flush()
    return instruments


async def _insert_daily_bars(
    db_session: AsyncSession,
    instruments: list[Instrument],
    trade_date: date,
) -> None:
    """为给定股票列表插入当日日线数据（bars_daily）。"""
    for inst in instruments:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=trade_date,
            open=Decimal("10.0"),
            high=Decimal("10.5"),
            low=Decimal("9.8"),
            close=Decimal("10.2"),
            volume=Decimal("100000"),
            amount=Decimal("1000000"),
            adj_factor=Decimal("1.0"),
        )
        db_session.add(bar)
    await db_session.flush()


async def _create_after_close_job_run(
    db_session: AsyncSession,
    *,
    status: str = "running",
    orchestrator_status: str = "queued",
    trade_date: date = date(2026, 6, 25),
    last_completed_step: str | None = None,
    dsa_run_id: uuid.UUID | None = None,
) -> SchedulerJobRun:
    """直接创建测试用 after_close SchedulerJobRun（不经过 create_after_close_run）。"""
    now = datetime.now(UTC)
    meta: dict = {
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date.isoformat(),
    }
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step
    if dsa_run_id is not None:
        meta["dsa_run_id"] = str(dsa_run_id)

    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


# ============================================================
# Task 6.1: POST /admin/after-close-runs/{id}/force?restart_from=daily_ready
# [CHANGE-20260728-008] 原 dsa-only 独立端点已删除，功能并入 force + restart_from
# ============================================================


class TestForceRestartFromDailyReady:
    """POST /admin/after-close-runs/{id}/force?restart_from=daily_ready 端点测试。

    替代原 TestDsaOnlyEndpoint：从 DSA 阶段重算（跳过日线刷新，需覆盖率≥90%）。
    """

    @pytest.mark.asyncio
    async def test_force_restart_from_daily_ready_insufficient_coverage_returns_409(
        self, db_session, admin_user
    ) -> None:
        """场景 1：当日无日线数据，调用 force?restart_from=daily_ready，返 409。

        given: 数据库中有 10 个活跃股票，但 bars_daily 当日无数据（覆盖率 0%）
        when: POST /admin/after-close-runs/{id}/force?restart_from=daily_ready
        then: 响应 409，body.detail.reason == "DATA_COVERAGE_INSUFFICIENT"
        """
        _override_get_db(db_session)
        # 准备：创建一个已存在的 after_close 任务 + 10 个活跃股票，无日线数据
        job_run = await _create_after_close_job_run(
            db_session, status="succeeded", orchestrator_status="succeeded",
        )
        await _create_active_instruments(db_session, count=10)
        await db_session.flush()

        with patch(
            "app.core.time.shanghai_business_date", return_value=date(2099, 1, 1)
        ):
            transport = make_asgi_transport(app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/v1/admin/after-close-runs/{job_run.id}/force",
                    headers=_auth_headers(admin_user.id),
                    params={"restart_from": "daily_ready"},
                )

        assert response.status_code == 409, f"响应体: {response.text}"
        detail = response.json()["detail"]
        assert detail["reason"] == "DATA_COVERAGE_INSUFFICIENT"
        assert detail["threshold"] == 0.9
        assert "daily_coverage" in detail
        assert detail["daily_coverage"] < 0.9

    @pytest.mark.asyncio
    async def test_force_restart_from_daily_ready_invalid_value_returns_400(
        self, db_session, admin_user
    ) -> None:
        """场景 1.1：restart_from 传入非法值，应返 400。"""
        _override_get_db(db_session)
        job_run = await _create_after_close_job_run(
            db_session, status="succeeded", orchestrator_status="succeeded",
        )
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{job_run.id}/force",
                headers=_auth_headers(admin_user.id),
                params={"restart_from": "invalid_step"},
            )

        assert response.status_code == 400, f"响应体: {response.text}"
        assert "restart_from" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_force_restart_from_daily_ready_sufficient_coverage_resets_queued(
        self, db_session, admin_user
    ) -> None:
        """场景 2：覆盖率 ≥ 90%，调用 force?restart_from=daily_ready，返 200 + queued。

        given: 数据库中 N 个活跃股票，为全部活跃股票插入当日日线数据（覆盖率 100%）
        when: POST /admin/after-close-runs/{id}/force?restart_from=daily_ready
        then:
          - 响应 200
          - body.status == "queued"
          - 数据库中 metadata 含 last_completed_step='refreshing_daily'
          - metadata 中不含 dsa_run_id（已清除）
        """
        _override_get_db(db_session)
        trade_date = date(2099, 1, 1)

        # 创建一个已完成的 after_close 任务（带旧 dsa_run_id 便于验证清除）
        job_run = await _create_after_close_job_run(
            db_session, status="succeeded", orchestrator_status="succeeded",
        )
        # 注入旧 dsa_run_id 模拟历史 dsa_only 记录
        meta = json.loads(job_run.metadata_json or "{}")
        meta["dsa_run_id"] = str(uuid.uuid4())
        meta["trade_date"] = trade_date.isoformat()
        job_run.metadata_json = json.dumps(meta, ensure_ascii=False)

        # 为所有现有 active instruments 插入当日 bars_daily
        result = await db_session.execute(
            select(Instrument).where(Instrument.status == "active")
        )
        existing_instruments = list(result.scalars().all())
        for inst in existing_instruments:
            db_session.add(BarDaily(
                instrument_id=inst.id,
                trade_date=trade_date,
                open=Decimal("10.0"),
                high=Decimal("10.5"),
                low=Decimal("9.8"),
                close=Decimal("10.2"),
                volume=Decimal("100000"),
                amount=Decimal("1000000"),
                adj_factor=Decimal("1.0"),
            ))

        # 新增 10 个 active instruments + 对应 bars_daily（确保覆盖率 100%）
        new_instruments = await _create_active_instruments(db_session, count=10)
        await _insert_daily_bars(db_session, new_instruments, trade_date)
        await db_session.flush()

        with patch(
            "app.core.time.shanghai_business_date", return_value=date(2099, 1, 1)
        ):
            transport = make_asgi_transport(app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/v1/admin/after-close-runs/{job_run.id}/force",
                    headers=_auth_headers(admin_user.id),
                    params={"restart_from": "daily_ready"},
                )

        assert response.status_code == 200, f"响应体: {response.text}"
        data = response.json()
        assert data["status"] == "queued"
        assert data["orchestrator_status"] == "queued"

        # 验证数据库中 metadata
        updated = await db_session.get(SchedulerJobRun, job_run.id)
        assert updated is not None
        assert updated.metadata_json is not None
        meta = json.loads(updated.metadata_json)
        assert meta["last_completed_step"] == "refreshing_daily"
        assert meta["orchestrator_status"] == "queued"
        # dsa_run_id 应被清除
        assert "dsa_run_id" not in meta, f"dsa_run_id 应被清除，实际 metadata={meta}"


# ============================================================
# Task 6.2: POST /admin/after-close-runs/{id}/resume
# ============================================================


class TestResumeEndpoint:
    """POST /admin/after-close-runs/{id}/resume 端点测试。"""

    @pytest.mark.asyncio
    async def test_resume_non_failed_returns_400(
        self, db_session, admin_user
    ) -> None:
        """场景 3：任务 status=running，调用 resume，返 400。

        given: after_close 任务 status=running
        when: POST /admin/after-close-runs/{id}/resume
        then: 响应 400，body.detail 含 "仅 failed/interrupted 状态可恢复"
        """
        _override_get_db(db_session)
        job_run = await _create_after_close_job_run(
            db_session,
            status="running",
            orchestrator_status="refreshing_daily",
        )
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{job_run.id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 400, f"响应体: {response.text}"
        detail = response.json()["detail"]
        assert "仅 failed/interrupted 状态可恢复" in detail
        assert "running" in detail

    @pytest.mark.asyncio
    async def test_resume_failed_resets_queued_preserves_checkpoint(
        self, db_session, admin_user
    ) -> None:
        """场景 4：任务 status=failed + last_completed_step=refreshing_daily，调用 resume。

        given: after_close 任务 status=failed, metadata.last_completed_step=refreshing_daily
        when: POST /admin/after-close-runs/{id}/resume
        then:
          - 响应 200
          - body.status == "queued"
          - 数据库中 job_run.status == "queued"
          - metadata.last_completed_step 仍为 "refreshing_daily"（保留检查点）
        """
        _override_get_db(db_session)
        job_run = await _create_after_close_job_run(
            db_session,
            status="failed",
            orchestrator_status="failed",
            last_completed_step="refreshing_daily",
        )
        job_run.error_message = "模拟失败"
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{job_run.id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 200, f"响应体: {response.text}"
        data = response.json()
        assert data["status"] == "queued"
        assert data["orchestrator_status"] == "queued"

        # 验证数据库中状态重置 + 检查点保留
        await db_session.refresh(job_run)
        assert job_run.status == "queued"
        assert job_run.error_message is None
        assert job_run.finished_at is None
        meta = json.loads(job_run.metadata_json)
        assert meta["last_completed_step"] == "refreshing_daily"
        assert meta["orchestrator_status"] == "queued"

    @pytest.mark.asyncio
    async def test_resume_interrupted_resets_queued(
        self, db_session, admin_user
    ) -> None:
        """场景 4.1：任务 status=interrupted 也可 resume（spec 要求 failed/interrupted 都可恢复）。

        given: after_close 任务 status=interrupted, last_completed_step=waiting_dsa_worker
        when: POST /admin/after-close-runs/{id}/resume
        then: 响应 200 + status=queued + last_completed_step 保留
        """
        _override_get_db(db_session)
        job_run = await _create_after_close_job_run(
            db_session,
            status="interrupted",
            orchestrator_status="waiting_dsa_worker",
            last_completed_step="waiting_dsa_worker",
        )
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{job_run.id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 200, f"响应体: {response.text}"
        data = response.json()
        assert data["status"] == "queued"
        await db_session.refresh(job_run)
        assert job_run.metadata_json is not None
        meta = json.loads(job_run.metadata_json)
        assert meta["last_completed_step"] == "waiting_dsa_worker"

    @pytest.mark.asyncio
    async def test_resume_not_found_returns_404(
        self, db_session, admin_user
    ) -> None:
        """场景 4.2：任务不存在，返 404。"""
        _override_get_db(db_session)
        fake_id = uuid.uuid4()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{fake_id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 404, f"响应体: {response.text}"

    @pytest.mark.asyncio
    async def test_resume_non_after_close_returns_400(
        self, db_session, admin_user
    ) -> None:
        """场景 4.3：任务非盘后编排，返 400。"""
        _override_get_db(db_session)
        now = datetime.now(UTC)
        other_job = SchedulerJobRun(
            job_name="bars_scheduler",
            business_date="2026-06-25",
            run_key=f"bars_scheduler:test:{uuid.uuid4().hex[:8]}",
            status="failed",
            scheduled_at=now,
            started_at=now,
            heartbeat_at=now,
            lease_expires_at=now,
            metadata_json=json.dumps({"trade_date": "2026-06-25"}),
        )
        db_session.add(other_job)
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{other_job.id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 400, f"响应体: {response.text}"
        assert "非盘后编排" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_resume_idempotent_returns_queued_task(
        self, db_session, admin_user
    ) -> None:
        """场景 5：已 queued 任务再次调用 resume，幂等返回同一任务（不报错、不新建事件）。

        given: after_close 任务 status=queued（前一次 resume 已提交）
        when: POST /admin/after-close-runs/{id}/resume
        then: 响应 200 + status=queued + 幂等消息
        """
        _override_get_db(db_session)
        job_run = await _create_after_close_job_run(
            db_session,
            status="queued",
            orchestrator_status="queued",
            last_completed_step="quality_gate",
        )
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{job_run.id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 200, f"响应体: {response.text}"
        data = response.json()
        assert data["status"] == "queued"
        assert "幂等" in data["message"]

    @pytest.mark.asyncio
    async def test_resume_same_day_active_run_returns_409(
        self, db_session, admin_user
    ) -> None:
        """场景 6：同 trade_date 已有另一 queued/running 任务时返 409 SAME_DAY_ACTIVE_RUN。

        given: 两个同 trade_date 的 after_close 任务，一个 failed，一个 running
        when: POST /admin/after-close-runs/{failed_id}/resume
        then: 响应 409 + error_code=SAME_DAY_ACTIVE_RUN
        """
        _override_get_db(db_session)
        trade_date = date(2026, 6, 25)

        # 第一个任务：failed（可 resume）
        failed_run = await _create_after_close_job_run(
            db_session,
            status="failed",
            orchestrator_status="failed",
            trade_date=trade_date,
            last_completed_step="quality_gate",
        )

        # 第二个任务：running（同日活跃，阻止 resume）
        running_run = await _create_after_close_job_run(
            db_session,
            status="running",
            orchestrator_status="feature_snapshot",
            trade_date=trade_date,
        )
        # 修正 run_key 避免唯一约束冲突
        running_run.run_key = f"after_close_orchestrator:test:running:{uuid.uuid4().hex[:8]}"
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{failed_run.id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 409, f"响应体: {response.text}"
        detail = response.json()["detail"]
        assert detail["error_code"] == "SAME_DAY_ACTIVE_RUN"
        assert detail["conflicting_run_id"] == str(running_run.id)

    @pytest.mark.asyncio
    async def test_resume_writes_manual_resume_event_and_metadata(
        self, db_session, admin_user
    ) -> None:
        """场景 7：resume 写入 manual_resume 事件 + metadata 含 resume_requested_at。

        given: after_close 任务 status=interrupted
        when: POST /admin/after-close-runs/{id}/resume
        then:
          - job_run_event 表有一条 step=manual_resume 事件
          - metadata 含 resume_requested_at（ISO 字符串）
          - worker_instance_id 被清空
          - heartbeat_at / lease_expires_at 被清空
        """
        from sqlalchemy import select as sa_select

        from app.models.job_run_event import JobRunEvent

        _override_get_db(db_session)
        job_run = await _create_after_close_job_run(
            db_session,
            status="interrupted",
            orchestrator_status="feature_snapshot",
            last_completed_step="quality_gate",
        )
        # 设置 worker_instance_id 模拟中断前有 worker
        job_run.worker_instance_id = "worker-abc-123"
        await db_session.flush()

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/v1/admin/after-close-runs/{job_run.id}/resume",
                headers=_auth_headers(admin_user.id),
            )

        assert response.status_code == 200, f"响应体: {response.text}"

        # 验证 metadata 含 resume_requested_at
        await db_session.refresh(job_run)
        meta = json.loads(job_run.metadata_json)
        assert "resume_requested_at" in meta, (
            f"metadata 应含 resume_requested_at: {meta}"
        )
        assert meta["orchestrator_status"] == "queued"

        # 验证 worker_instance_id / heartbeat_at / lease_expires_at 清空
        assert job_run.worker_instance_id is None, (
            f"worker_instance_id 应被清空: {job_run.worker_instance_id}"
        )
        assert job_run.heartbeat_at is None, (
            f"heartbeat_at 应被清空: {job_run.heartbeat_at}"
        )
        assert job_run.lease_expires_at is None, (
            f"lease_expires_at 应被清空: {job_run.lease_expires_at}"
        )
        assert job_run.finished_at is None
        assert job_run.error_message is None
        assert job_run.error_code is None

        # 验证 manual_resume 事件
        event_stmt = sa_select(JobRunEvent).where(
            JobRunEvent.job_run_id == job_run.id,
            JobRunEvent.step == "manual_resume",
        )
        event_result = await db_session.execute(event_stmt)
        events = event_result.scalars().all()
        assert len(events) == 1, (
            f"应写入 1 条 manual_resume 事件，实际 {len(events)}"
        )
        assert events[0].level == "info"


# ============================================================
# Task 8: POST /admin/after-close-runs 创建端点 409 detail 透明化 + 成功文案
# ============================================================


class TestCreateAfterCloseRunEndpoint:
    """POST /admin/after-close-runs 创建端点测试。

    [AfterClose] - 验证第八节"盘后创建错误透明化"修复：
    - 409 响应包含 detail（error_code/after_close_run_id/started_at/...），不再丢失真实原因
    - 成功文案改为"任务已加入队列"
    """

    @pytest.mark.asyncio
    async def test_create_duplicate_returns_409_with_detail(
        self, db_session, admin_user
    ) -> None:
        """场景：同日已有 running 编排任务，create 返 409 + detail 含完整字段。

        given: 当日已有 after_close_orchestrator job_run（status=running, run_key 匹配）
        when: POST /admin/after-close-runs { trade_date }
        then:
          - 响应 409
          - detail.error_code == "DUPLICATE_RUN"
          - detail.after_close_run_id == 已有任务 id
          - detail.started_at / heartbeat_at / last_completed_step / orchestrator_status 透传
          - detail.message 含"当天已有盘后任务正在运行"
        """
        _override_get_db(db_session)
        trade_date = date(2099, 2, 1)
        now = datetime.now(UTC)
        existing = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date=trade_date.isoformat(),
            run_key=f"after_close_orchestrator:{trade_date.isoformat()}",
            status="running",
            scheduled_at=now,
            started_at=now,
            heartbeat_at=now,
            # 租约设为远未来，避免 recover_stale 恢复为 interrupted
            lease_expires_at=datetime(2099, 12, 31, tzinfo=UTC),
            metadata_json=json.dumps({
                "orchestrator_status": "refreshing_daily",
                "trade_date": trade_date.isoformat(),
                "last_completed_step": "refreshing_daily",
            }),
        )
        db_session.add(existing)
        await db_session.flush()

        with patch(
            "app.api.admin_after_close.is_trading_day_async",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = make_asgi_transport(app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/admin/after-close-runs",
                    headers=_auth_headers(admin_user.id),
                    json={"trade_date": trade_date.isoformat()},
                )

        assert response.status_code == 409, f"响应体: {response.text}"
        detail = response.json()["detail"]
        assert detail["error_code"] == "DUPLICATE_RUN"
        assert detail["after_close_run_id"] == str(existing.id)
        assert detail["status"] == "running"
        assert detail["orchestrator_status"] == "refreshing_daily"
        assert detail["last_completed_step"] == "refreshing_daily"
        assert detail["started_at"] is not None
        assert detail["heartbeat_at"] is not None
        assert "当天已有盘后任务正在运行" in detail["message"]

    @pytest.mark.asyncio
    async def test_create_success_message_queued(
        self, db_session, admin_user
    ) -> None:
        """场景：新建成功，message 含"任务已加入队列"（不再说"已创建并启动"）。

        given: 当日无 after_close_orchestrator 任务，is_trading_day=True
        when: POST /admin/after-close-runs { trade_date: 独特未来日期 }
        then: 响应 201 + status=queued + message 含"任务已加入队列"
        """
        _override_get_db(db_session)
        trade_date = date(2099, 3, 1)

        with patch(
            "app.api.admin_after_close.is_trading_day_async",
            new_callable=AsyncMock,
            return_value=True,
        ):
            transport = make_asgi_transport(app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/admin/after-close-runs",
                    headers=_auth_headers(admin_user.id),
                    json={"trade_date": trade_date.isoformat()},
                )

        assert response.status_code == 201, f"响应体: {response.text}"
        data = response.json()
        assert data["status"] == "queued"
        assert "任务已加入队列" in data["message"]
        assert "已创建并启动" not in data["message"]


# ============================================================
# DSA 历史日期拒绝校验（strategy_runs 端点）
# ============================================================


class TestDsaHistoricalDateRejection:
    """DSA 相关端点拒绝历史日期，统一返回 422。

    覆盖：
    - POST /admin/strategies/{strategy_key}/run（dsa_selector）
    """

    @pytest.mark.asyncio
    async def test_trigger_strategy_run_dsa_historical_date_returns_422(
        self, db_session, admin_user
    ) -> None:
        """DSA 手动运行传入历史日期，应 422，无需查策略版本。

        given: 管理员调用 POST /admin/strategies/dsa_selector/run
        when: trade_date 为昨天
        then: 响应 422，detail 含 "DSA 拒绝历史日期运行"
        """
        _override_get_db(db_session)

        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/admin/strategies/dsa_selector/run",
                headers=_auth_headers(admin_user.id),
                json={"trade_date": "2026-06-25", "run_type": "manual"},
            )

        assert response.status_code == 422, f"响应体: {response.text}"
        assert "DSA 拒绝历史日期运行" in response.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
