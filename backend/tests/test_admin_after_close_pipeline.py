"""盘后流水线可视化面板 API 测试。

覆盖 /admin/after-close/pipeline/* 四个端点的 8 种后端场景：
1. 无 run → not_started
2. running + 当前 step 正确
3. succeeded after_close + full published snapshot → watchlist_ready=true
4. sample snapshot run → watchlist_ready=false
5. failed run → overall_status=failed + error
6. POST run 幂等
7. events limit=100
8. 非 admin 403

测试策略：
- 复用 conftest client fixture，覆盖 get_db 为测试 session
- 使用 user_factory 创建 admin / 普通用户
- 使用 create_access_token 生成 Authorization header
- mock is_trading_day_async 控制交易日判定
- 注入固定 now 时间，避免测试时市场阶段漂移
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_feature_snapshot_run import (
    RUN_TYPE_AFTER_CLOSE,
    RUN_TYPE_BACKFILL,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.models.user import User
from app.services.after_close_orchestrator import AfterCloseRunStatus

SHANGHAI = ZoneInfo("Asia/Shanghai")

TEST_DATE = date(2026, 6, 24)
TEST_DATE_STR = "2026-06-24"


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token header。"""
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _mock_trading_day(is_trading: bool = True):
    """创建 is_trading_day_async 的 mock 上下文。"""
    return patch(
        "app.services.calendar_service.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=is_trading,
    )


@pytest_asyncio.fixture
async def admin_user(user_factory) -> User:
    """创建 admin 用户。"""
    return await user_factory(roles=["admin"])


@pytest_asyncio.fixture
async def normal_user(user_factory) -> User:
    """创建普通用户（无 admin 角色）。"""
    return await user_factory(roles=["member"])


def _make_after_close_job_run(
    status: str,
    orchestrator_status: str | None = None,
    last_completed_step: str | None = None,
    error_message: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
) -> SchedulerJobRun:
    """构造 after_close_orchestrator 测试任务。"""
    meta: dict[str, object] = {
        "orchestrator_status": orchestrator_status,
        "trade_date": TEST_DATE_STR,
    }
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step

    return SchedulerJobRun(
        id=uuid.uuid4(),
        job_name="after_close_orchestrator",
        business_date=TEST_DATE_STR,
        run_key=f"after_close_orchestrator:{TEST_DATE_STR}",
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        heartbeat_at=heartbeat_at,
        error_message=error_message,
        metadata_json=json.dumps(meta),
    )


def _make_snapshot_run(
    run_type: str,
    status: str,
    scope: str = "full",
    published: bool = False,
    finished_at: datetime | None = None,
) -> StockFeatureSnapshotRun:
    """构造 stock_feature_snapshot_run 测试记录。"""
    published_at: datetime | None = None
    if published and status == STATUS_SUCCEEDED:
        published_at = finished_at or datetime.now(SHANGHAI)
    return StockFeatureSnapshotRun(
        id=uuid.uuid4(),
        trade_date=TEST_DATE,
        run_type=run_type,
        status=status,
        metadata_={"scope": scope},
        snapshot_count=10,
        failed_count=0,
        expected_count=10,
        published_at=published_at,
        finished_at=finished_at,
    )


# ==================== 1. 盘前/盘中无 run → not_started ====================

@pytest.mark.asyncio
async def test_pipeline_not_started_pre_market(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """交易日盘前（09:00）无 after_close run → overall_status=not_started。"""
    now = datetime(2026, 6, 24, 9, 0, tzinfo=SHANGHAI)

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            "/admin/after-close/pipeline",
            params={"trade_date": TEST_DATE_STR},
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trade_date"] == TEST_DATE_STR
    assert data["overall_status"] == "not_started"
    assert data["watchlist_ready"] is False
    assert data["after_close_run"] is None
    assert all(step["status"] == "pending" for step in data["steps"])


# ==================== 1b. 收盘后超过阈值无 run → blocked ====================

@pytest.mark.asyncio
async def test_pipeline_blocked_after_close(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """交易日收盘后（16:05）超过 30 分钟阈值无 after_close run → overall_status=blocked。"""
    now = datetime(2026, 6, 24, 16, 5, tzinfo=SHANGHAI)

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            "/admin/after-close/pipeline",
            params={"trade_date": TEST_DATE_STR},
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["trade_date"] == TEST_DATE_STR
    assert data["overall_status"] == "blocked"
    assert data["watchlist_ready"] is False
    assert data["after_close_run"] is None
    assert all(step["status"] == "pending" for step in data["steps"])


# ==================== 1c. latest 在交易日不回退历史 run ====================

@pytest.mark.asyncio
async def test_pipeline_latest_no_fallback_on_trading_day(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """交易日收盘后 today 无 run 但昨天有 succeeded → latest 必须返回 today 的 blocked，不得返回昨天。"""
    today = datetime(2026, 6, 24, 16, 5, tzinfo=SHANGHAI)
    yesterday_str = "2026-06-23"
    yesterday_finished = today - timedelta(days=1, minutes=-10)

    # 昨天有 succeeded after_close run + full published snapshot
    yesterday_meta = {
        "orchestrator_status": AfterCloseRunStatus.SUCCEEDED.value,
        "trade_date": yesterday_str,
        "last_completed_step": AfterCloseRunStatus.SUCCEEDED.value,
    }
    yesterday_run = SchedulerJobRun(
        id=uuid.uuid4(),
        job_name="after_close_orchestrator",
        business_date=yesterday_str,
        run_key=f"after_close_orchestrator:{yesterday_str}",
        status=STATUS_SUCCEEDED,
        started_at=yesterday_finished - timedelta(minutes=30),
        finished_at=yesterday_finished,
        metadata_json=json.dumps(yesterday_meta),
    )
    db_session.add(yesterday_run)

    yesterday_snapshot = StockFeatureSnapshotRun(
        id=uuid.uuid4(),
        trade_date=date(2026, 6, 23),
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_SUCCEEDED,
        metadata_={"scope": "full"},
        snapshot_count=10,
        failed_count=0,
        expected_count=10,
        published_at=yesterday_finished,
        finished_at=yesterday_finished,
    )
    db_session.add(yesterday_snapshot)
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=today,
    ):
        resp = await client.get(
            "/admin/after-close/pipeline/latest",
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 关键断言：latest 必须返回 today（2026-06-24），不得回退到昨天
    assert data["trade_date"] == "2026-06-24"
    assert data["overall_status"] == "blocked"
    assert data["watchlist_ready"] is False
    assert data["after_close_run"] is None


# ==================== 2. running → 当前 step 正确 ====================


@pytest.mark.asyncio
async def test_pipeline_running_current_step(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """after_close running 且处于 feature_snapshot 步骤 → 当前 step 正确。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    started_at = now - timedelta(minutes=10)

    job_run = _make_after_close_job_run(
        status=STATUS_RUNNING,
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        last_completed_step=AfterCloseRunStatus.QUALITY_GATE.value,
        started_at=started_at,
        heartbeat_at=now - timedelta(seconds=30),
    )
    db_session.add(job_run)
    await db_session.flush()

    # 写入 feature_snapshot 步骤事件
    db_session.add(
        JobRunEvent(
            job_run_id=job_run.id,
            step=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
            level="info",
            message="开始 feature snapshot",
            payload={"snapshot_count": 10},
            created_at=started_at + timedelta(minutes=5),
        )
    )
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            f"/admin/after-close/pipeline?trade_date={TEST_DATE_STR}",
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["overall_status"] == "running"
    assert data["after_close_run"]["status"] == STATUS_RUNNING
    assert data["after_close_run"]["orchestrator_status"] == AfterCloseRunStatus.FEATURE_SNAPSHOT.value

    steps = {step["step"]: step for step in data["steps"]}
    assert steps[AfterCloseRunStatus.FEATURE_SNAPSHOT.value]["status"] == "running"
    assert steps[AfterCloseRunStatus.REFRESHING_DAILY.value]["status"] == "completed"
    assert steps[AfterCloseRunStatus.PUBLISHING.value]["status"] == "pending"
    assert steps["watchlist_ready"]["status"] == "pending"


# ==================== 3. succeeded + full published → watchlist_ready=true ====================


@pytest.mark.asyncio
async def test_pipeline_succeeded_watchlist_ready(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """after_close succeeded + full published snapshot → watchlist_ready=true。"""
    now = datetime(2026, 6, 24, 20, 0, tzinfo=SHANGHAI)
    finished_at = now - timedelta(minutes=10)

    job_run = _make_after_close_job_run(
        status=STATUS_SUCCEEDED,
        orchestrator_status=AfterCloseRunStatus.SUCCEEDED.value,
        last_completed_step=AfterCloseRunStatus.SUCCEEDED.value,
        started_at=finished_at - timedelta(minutes=30),
        finished_at=finished_at,
    )
    db_session.add(job_run)

    snapshot_run = _make_snapshot_run(
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_SUCCEEDED,
        scope="full",
        published=True,
        finished_at=finished_at,
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            "/admin/after-close/pipeline/latest",
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["overall_status"] == "succeeded"
    assert data["watchlist_ready"] is True
    assert data["watchlist_reason"].startswith("after_close 已 succeeded")
    assert data["feature_snapshot_run"]["scope"] == "full"
    assert data["feature_snapshot_run"]["published_at"] is not None

    steps = {step["step"]: step for step in data["steps"]}
    assert all(step["status"] == "completed" for step in data["steps"])
    assert steps["watchlist_ready"]["status"] == "completed"


# ==================== 4. sample snapshot run → watchlist_ready=false ====================


@pytest.mark.asyncio
async def test_pipeline_sample_snapshot_not_readable(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """sample scope snapshot run 不得让 watchlist_ready=true。"""
    now = datetime(2026, 6, 24, 20, 0, tzinfo=SHANGHAI)
    finished_at = now - timedelta(minutes=10)

    job_run = _make_after_close_job_run(
        status=STATUS_SUCCEEDED,
        orchestrator_status=AfterCloseRunStatus.SUCCEEDED.value,
        last_completed_step=AfterCloseRunStatus.SUCCEEDED.value,
        started_at=finished_at - timedelta(minutes=30),
        finished_at=finished_at,
    )
    db_session.add(job_run)

    snapshot_run = _make_snapshot_run(
        run_type=RUN_TYPE_BACKFILL,
        status=STATUS_SUCCEEDED,
        scope="sample",
        published=True,
        finished_at=finished_at,
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            "/admin/after-close/pipeline",
            params={"trade_date": TEST_DATE_STR},
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["watchlist_ready"] is False
    assert data["feature_snapshot_run"]["scope"] == "sample"
    assert "非 full，不可读" in data["watchlist_reason"]

    steps = {step["step"]: step for step in data["steps"]}
    assert steps["watchlist_ready"]["status"] == "pending"


# ==================== 4b. watchlist_ready=true 时 feature_snapshot_run 优先显示 full run ====================

@pytest.mark.asyncio
async def test_pipeline_full_snapshot_preferred_over_sample(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """同一 trade_date 同时存在 full succeeded published 和后来的 sample succeeded published，
    watchlist_ready=true 时页面摘要应显示 full run，不显示 sample 作为主 run。"""
    now = datetime(2026, 6, 24, 20, 0, tzinfo=SHANGHAI)
    full_finished = now - timedelta(minutes=20)
    sample_finished = now - timedelta(minutes=5)  # sample 更晚创建

    job_run = _make_after_close_job_run(
        status=STATUS_SUCCEEDED,
        orchestrator_status=AfterCloseRunStatus.SUCCEEDED.value,
        last_completed_step=AfterCloseRunStatus.SUCCEEDED.value,
        started_at=full_finished - timedelta(minutes=30),
        finished_at=full_finished,
    )
    db_session.add(job_run)

    # full run（先创建）
    full_run = _make_snapshot_run(
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_SUCCEEDED,
        scope="full",
        published=True,
        finished_at=full_finished,
    )
    db_session.add(full_run)

    # sample run（后创建，created_at 更新）
    sample_run = _make_snapshot_run(
        run_type=RUN_TYPE_BACKFILL,
        status=STATUS_SUCCEEDED,
        scope="sample",
        published=True,
        finished_at=sample_finished,
    )
    db_session.add(sample_run)
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            f"/admin/after-close/pipeline?trade_date={TEST_DATE_STR}",
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["watchlist_ready"] is True
    # 关键断言：feature_snapshot_run 必须是 full run，不是 sample
    assert data["feature_snapshot_run"]["scope"] == "full"
    assert data["feature_snapshot_run"]["run_id"] == str(full_run.id)


# ==================== 5. failed run → overall_status=failed + error ====================


@pytest.mark.asyncio
async def test_pipeline_failed_with_error(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """after_close failed → overall_status=failed，并暴露 failed step + error。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    failed_at = now - timedelta(minutes=5)

    job_run = _make_after_close_job_run(
        status=STATUS_FAILED,
        orchestrator_status=AfterCloseRunStatus.FAILED.value,
        last_completed_step=AfterCloseRunStatus.REFRESHING_DAILY.value,
        error_message="daily bars download failed",
        started_at=failed_at - timedelta(minutes=20),
        finished_at=failed_at,
    )
    db_session.add(job_run)
    await db_session.flush()

    db_session.add(
        JobRunEvent(
            job_run_id=job_run.id,
            step=AfterCloseRunStatus.CHECKING_COVERAGE.value,
            level="error",
            message="daily bars download failed",
            payload={"step": AfterCloseRunStatus.CHECKING_COVERAGE.value, "coverage": 0.1},
            created_at=failed_at,
        )
    )
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            "/admin/after-close/pipeline",
            params={"trade_date": TEST_DATE_STR},
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["overall_status"] == "failed"
    assert data["after_close_run"]["error_message"] == "daily bars download failed"

    steps = {step["step"]: step for step in data["steps"]}
    assert steps[AfterCloseRunStatus.CHECKING_COVERAGE.value]["status"] == "failed"
    assert steps[AfterCloseRunStatus.CHECKING_COVERAGE.value]["error_message"] == "daily bars download failed"
    assert steps[AfterCloseRunStatus.REFRESHING_DAILY.value]["status"] == "completed"


# ==================== 6. POST run 幂等 ====================


@pytest.mark.asyncio
async def test_post_pipeline_run_idempotent(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """同 trade_date 已有 queued/running/succeeded 时 POST 返回 existing，不重复创建。"""
    # 使用真实当前时间，避免 recover_stale_scheduler_job_runs 将测试任务标记为 interrupted
    now = datetime.now(SHANGHAI)

    job_run = _make_after_close_job_run(
        status=STATUS_RUNNING,
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        started_at=now - timedelta(minutes=10),
        heartbeat_at=now - timedelta(seconds=30),
    )
    db_session.add(job_run)
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.post(
            "/admin/after-close/pipeline/run",
            json={"trade_date": TEST_DATE_STR},
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["job_run_id"] == str(job_run.id)
    assert data["is_new"] is False
    assert data["status"] == STATUS_RUNNING


# ==================== 7. events limit=100 ====================


@pytest.mark.asyncio
async def test_pipeline_events_limit_100(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
):
    """事件日志最多返回 100 条。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)

    job_run = _make_after_close_job_run(
        status=STATUS_RUNNING,
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - timedelta(seconds=30),
    )
    db_session.add(job_run)
    await db_session.flush()

    # 写入 120 条事件
    base_time = now - timedelta(minutes=30)
    for i in range(120):
        db_session.add(
            JobRunEvent(
                job_run_id=job_run.id,
                step=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
                level="info",
                message=f"event {i}",
                created_at=base_time + timedelta(seconds=i),
            )
        )
    await db_session.flush()

    with _mock_trading_day(is_trading=True), patch(
        "app.services.after_close_pipeline_service.now_shanghai",
        return_value=now,
    ):
        resp = await client.get(
            "/admin/after-close/pipeline",
            params={"trade_date": TEST_DATE_STR},
            headers=_auth_headers(admin_user.id),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["events"]) == 100


# ==================== 8. 非 admin 403 ====================


@pytest.mark.asyncio
async def test_pipeline_non_admin_forbidden(
    client: AsyncClient,
    normal_user: User,
):
    """普通用户访问 admin after-close pipeline 端点返回 403。"""
    with _mock_trading_day(is_trading=True):
        resp = await client.get(
            "/admin/after-close/pipeline/latest",
            headers=_auth_headers(normal_user.id),
        )

    assert resp.status_code == 403, resp.text


if __name__ == "__main__":
    # 自测入口：验证常量与导入（不连 DB）
    assert AfterCloseRunStatus.FEATURE_SNAPSHOT.value in [
        "refreshing_daily",
        "checking_coverage",
        "creating_dsa",
        "waiting_dsa_worker",
        "quality_gate",
        "feature_snapshot",
        "publishing",
        "watchlist_ready",
    ]
    assert STATUS_SUCCEEDED != STATUS_FAILED
    print("test_admin_after_close_pipeline 自测通过")
