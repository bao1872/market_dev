"""C6 监控组合状态仓储乐观锁测试 - Task 25.3。

测试内容：
1. 乐观锁冲突：lock_version 不匹配时抛出 StateVersionConflictError
2. 重读重放：冲突时重新读取最新状态并重试
3. update_state 字段更新逻辑（含 clear_* 参数）
4. update_state_with_retry 重试逻辑

测试策略：
- 使用 unittest.mock 模拟 AsyncSession
- 测试仓储层的乐观锁逻辑，不依赖真实数据库
- 覆盖主逻辑 + 边界条件
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.monitoring_plan_state import MonitoringPlanState
from app.repositories.monitoring_plan_state_repository import (
    MAX_RETRY,
    StateVersionConflictError,
    get_or_create_state,
    get_state,
    update_state,
    update_state_with_retry,
)


def _make_state(
    *,
    state_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    monitoring_plan_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    instrument_id: uuid.UUID | None = None,
    status: str = "WAITING_TRIGGER",
    lock_version: int = 0,
    confirmed_member_ids: list[uuid.UUID] | None = None,
    window_started_at: datetime | None = None,
    window_deadline_at: datetime | None = None,
    cooldown_until: datetime | None = None,
    vetoed_by_member_id: uuid.UUID | None = None,
) -> MonitoringPlanState:
    """构造测试用 MonitoringPlanState 实例。"""
    return MonitoringPlanState(
        id=state_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        monitoring_plan_id=monitoring_plan_id or uuid.uuid4(),
        revision_id=revision_id or uuid.uuid4(),
        instrument_id=instrument_id or uuid.uuid4(),
        status=status,
        window_started_at=window_started_at,
        window_deadline_at=window_deadline_at,
        cooldown_until=cooldown_until,
        confirmed_member_ids=confirmed_member_ids or [],
        vetoed_by_member_id=vetoed_by_member_id,
        state_payload={},
        lock_version=lock_version,
    )


def _make_mock_session() -> MagicMock:
    """构造 mock AsyncSession。"""
    session = MagicMock()
    session.execute = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_get_or_create_state_returns_existing() -> None:
    """测试 get_or_create_state 返回已存在的状态。"""
    user_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    instrument_id = uuid.uuid4()
    existing_state = _make_state(
        user_id=user_id,
        monitoring_plan_id=plan_id,
        revision_id=revision_id,
        instrument_id=instrument_id,
        lock_version=5,
    )

    session = _make_mock_session()
    # 模拟查询返回已存在的状态
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing_state
    session.execute.return_value = mock_result

    state = await get_or_create_state(
        session,
        user_id=user_id,
        monitoring_plan_id=plan_id,
        revision_id=revision_id,
        instrument_id=instrument_id,
    )

    assert state.id == existing_state.id
    assert state.lock_version == 5
    # 不应执行 insert（已存在）
    assert session.execute.call_count == 1  # 仅查询


@pytest.mark.asyncio
async def test_get_or_create_state_creates_new() -> None:
    """测试 get_or_create_state 创建新状态。"""
    user_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    instrument_id = uuid.uuid4()
    new_state = _make_state(
        user_id=user_id,
        monitoring_plan_id=plan_id,
        revision_id=revision_id,
        instrument_id=instrument_id,
        lock_version=0,
    )

    session = _make_mock_session()
    # 第一次查询返回 None（不存在）
    select_result = MagicMock()
    select_result.scalar_one_or_none.return_value = None
    # insert 返回新状态
    insert_result = MagicMock()
    insert_result.scalar_one_or_none.return_value = new_state
    session.execute.side_effect = [select_result, insert_result]

    state = await get_or_create_state(
        session,
        user_id=user_id,
        monitoring_plan_id=plan_id,
        revision_id=revision_id,
        instrument_id=instrument_id,
    )

    assert state.id == new_state.id
    assert state.lock_version == 0
    assert state.status == "WAITING_TRIGGER"
    assert session.execute.call_count == 2  # 查询 + insert


@pytest.mark.asyncio
async def test_update_state_success() -> None:
    """测试 update_state 成功更新（lock_version 匹配）。"""
    state_id = uuid.uuid4()
    updated_state = _make_state(
        state_id=state_id, status="WAITING_CONFIRM", lock_version=1
    )

    session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = updated_state
    session.execute.return_value = mock_result

    result = await update_state(
        session,
        state_id=state_id,
        expected_lock_version=0,
        new_status="WAITING_CONFIRM",
        window_started_at=datetime(2026, 6, 18, 10, 30, 0),
    )

    assert result.status == "WAITING_CONFIRM"
    assert result.lock_version == 1


@pytest.mark.asyncio
async def test_update_state_version_conflict() -> None:
    """测试 update_state 乐观锁冲突（lock_version 不匹配）。"""
    state_id = uuid.uuid4()

    session = _make_mock_session()
    # 模拟 update 返回 0 行（lock_version 不匹配）
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    with pytest.raises(StateVersionConflictError) as exc_info:
        await update_state(
            session,
            state_id=state_id,
            expected_lock_version=99,  # 错误的版本号
            new_status="WAITING_CONFIRM",
        )

    assert "乐观锁冲突" in str(exc_info.value)
    assert "expected_lock_version=99" in str(exc_info.value)


@pytest.mark.asyncio
async def test_update_state_clear_fields() -> None:
    """测试 update_state 清空字段（clear_window/clear_cooldown/clear_vetoed）。"""
    state_id = uuid.uuid4()
    cleared_state = _make_state(
        state_id=state_id,
        status="WAITING_TRIGGER",
        lock_version=2,
        window_started_at=None,
        window_deadline_at=None,
        cooldown_until=None,
        vetoed_by_member_id=None,
    )

    session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cleared_state
    session.execute.return_value = mock_result

    result = await update_state(
        session,
        state_id=state_id,
        expected_lock_version=1,
        new_status="WAITING_TRIGGER",
        clear_window=True,
        clear_cooldown=True,
        clear_vetoed=True,
    )

    assert result.window_started_at is None
    assert result.window_deadline_at is None
    assert result.cooldown_until is None
    assert result.vetoed_by_member_id is None


@pytest.mark.asyncio
async def test_update_state_with_retry_success_on_first_attempt() -> None:
    """测试 update_state_with_retry 首次成功（无冲突）。"""
    state_id = uuid.uuid4()
    current_state = _make_state(state_id=state_id, lock_version=0)
    updated_state = _make_state(state_id=state_id, status="CONFIRMED", lock_version=1)

    session = _make_mock_session()
    # get_state 返回当前状态
    get_result = MagicMock()
    get_result.scalar_one_or_none.return_value = current_state
    # update_state 返回更新后的状态
    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = updated_state
    session.execute.side_effect = [get_result, update_result]

    async def replay_fn(current: MonitoringPlanState) -> dict:
        return {"new_status": "CONFIRMED"}

    result = await update_state_with_retry(
        session,
        state_id=state_id,
        expected_lock_version=0,
        replay_fn=replay_fn,
    )

    assert result.status == "CONFIRMED"
    assert result.lock_version == 1


@pytest.mark.asyncio
async def test_update_state_with_retry_replays_on_conflict() -> None:
    """测试 update_state_with_retry 冲突时重读重放成功。"""
    state_id = uuid.uuid4()
    # 第一次读取：lock_version=0（过期的）
    state_v0 = _make_state(state_id=state_id, lock_version=0)
    # 第二次读取（重试）：lock_version=1（其他事务已更新）
    state_v1 = _make_state(state_id=state_id, lock_version=1, status="WAITING_CONFIRM")
    # 最终更新成功
    state_v2 = _make_state(state_id=state_id, lock_version=2, status="CONFIRMED")

    session = _make_mock_session()
    # 第一次 get_state 返回 v0
    get_result_1 = MagicMock()
    get_result_1.scalar_one_or_none.return_value = state_v0
    # 第一次 update_state 返回 None（冲突）
    update_result_1 = MagicMock()
    update_result_1.scalar_one_or_none.return_value = None
    # 第二次 get_state 返回 v1（重读）
    get_result_2 = MagicMock()
    get_result_2.scalar_one_or_none.return_value = state_v1
    # 第二次 update_state 返回 v2（成功）
    update_result_2 = MagicMock()
    update_result_2.scalar_one_or_none.return_value = state_v2

    session.execute.side_effect = [
        get_result_1, update_result_1,  # 第一次尝试
        get_result_2, update_result_2,  # 第二次尝试（重试）
    ]

    replay_count = {"count": 0}

    async def replay_fn(current: MonitoringPlanState) -> dict:
        replay_count["count"] += 1
        return {"new_status": "CONFIRMED"}

    result = await update_state_with_retry(
        session,
        state_id=state_id,
        expected_lock_version=0,  # 过期的版本号
        replay_fn=replay_fn,
    )

    assert result.status == "CONFIRMED"
    assert result.lock_version == 2
    assert replay_count["count"] == 2  # replay_fn 调用两次


@pytest.mark.asyncio
async def test_update_state_with_retry_exhausted() -> None:
    """测试 update_state_with_retry 重试耗尽后抛出异常。"""
    state_id = uuid.uuid4()
    current_state = _make_state(state_id=state_id, lock_version=0)

    session = _make_mock_session()
    # 每次都返回冲突
    get_result = MagicMock()
    get_result.scalar_one_or_none.return_value = current_state
    update_result = MagicMock()
    update_result.scalar_one_or_none.return_value = None  # 冲突

    # 交替返回 get_result 和 update_result
    side_effects = []
    for _ in range(MAX_RETRY):
        side_effects.append(get_result)
        side_effects.append(update_result)
    session.execute.side_effect = side_effects

    async def replay_fn(current: MonitoringPlanState) -> dict:
        return {"new_status": "CONFIRMED"}

    with pytest.raises(StateVersionConflictError) as exc_info:
        await update_state_with_retry(
            session,
            state_id=state_id,
            expected_lock_version=0,
            replay_fn=replay_fn,
        )

    assert "重试" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_state_returns_none_for_nonexistent() -> None:
    """测试 get_state 查询不存在的状态返回 None。"""
    state_id = uuid.uuid4()

    session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    result = await get_state(session, state_id)
    assert result is None


@pytest.mark.asyncio
async def test_get_state_returns_state() -> None:
    """测试 get_state 查询存在的状态。"""
    state_id = uuid.uuid4()
    state = _make_state(state_id=state_id, status="WAITING_TRIGGER")

    session = _make_mock_session()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = state
    session.execute.return_value = mock_result

    result = await get_state(session, state_id)
    assert result is not None
    assert result.id == state_id
    assert result.status == "WAITING_TRIGGER"


def test_max_retry_constant() -> None:
    """测试 MAX_RETRY 常量为正数。"""
    assert MAX_RETRY > 0
    assert MAX_RETRY == 3


def test_state_version_conflict_error_is_exception() -> None:
    """测试 StateVersionConflictError 是 Exception 子类。"""
    assert issubclass(StateVersionConflictError, Exception)

    err = StateVersionConflictError("test message")
    assert "test message" in str(err)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
