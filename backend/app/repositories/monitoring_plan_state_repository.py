"""MonitoringPlanState 仓储 - 监控组合状态仓储（C6）。

[LEGACY] 本模块已从主业务流程中移除，仅保留代码以备参考。

提供：
- get_or_create_state: 获取或创建状态（按 user_id+revision_id+instrument_id 唯一）
- get_state: 查询单条状态
- update_state: 乐观锁更新（lock_version 不匹配时冲突）
- update_state_with_retry: 乐观锁冲突时重读重放（禁止最后写覆盖）
- list_states_by_revision: 查询方案版本下的所有状态
- list_states_by_instrument: 查询股票下的所有状态

设计说明：
- UNIQUE(user_id, revision_id, instrument_id) 保证状态唯一性。
- lock_version 乐观锁：每次更新 +1，WHERE lock_version = ? 不匹配则 0 行受影响。
- 冲突时重读重放：重新读取最新状态，调用方提供的 replay_fn 重新计算并重试。
- 禁异常吞没：所有异常补充上下文后 re-raise。
- 状态机不依赖墙钟：所有时间字段由 event_time 驱动。

Inputs:
    session: AsyncSession
    user_id / revision_id / instrument_id: UUID
    new_state / lock_version: 状态更新参数
    replay_fn: 冲突重放回调（接收最新 state，返回新状态字段）

How to Run:
    python -m app.repositories.monitoring_plan_state_repository    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monitoring_plan_state import MonitoringPlanState

logger = logging.getLogger("monitoring_plan_state_repository")

# 乐观锁冲突最大重试次数
MAX_RETRY = 3


class StateVersionConflictError(Exception):
    """乐观锁版本冲突异常。

    当 update_state 检测到 lock_version 不匹配时抛出。
    调用方可捕获此异常并执行重读重放逻辑。
    """


async def get_or_create_state(
    session: AsyncSession,
    *,
    user_id: UUID,
    monitoring_plan_id: UUID,
    revision_id: UUID,
    instrument_id: UUID,
    initial_status: str = "WAITING_TRIGGER",
) -> MonitoringPlanState:
    """获取或创建监控组合状态。

    使用 ON CONFLICT (user_id, revision_id, instrument_id) DO NOTHING 实现幂等创建。
    若状态已存在则返回现有状态，否则创建新状态。

    Args:
        session: 异步会话
        user_id: 用户 ID
        monitoring_plan_id: 方案 ID
        revision_id: 方案版本 ID
        instrument_id: 股票 ID
        initial_status: 初始状态（默认 WAITING_TRIGGER）

    Returns:
        MonitoringPlanState 对象（已存在或新建）

    Raises:
        Exception: 写入失败时补充上下文后 re-raise
    """
    # 先查询是否存在
    stmt = select(MonitoringPlanState).where(
        MonitoringPlanState.user_id == user_id,
        MonitoringPlanState.revision_id == revision_id,
        MonitoringPlanState.instrument_id == instrument_id,
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 monitoring_plan_state 失败 user_id=%s revision_id=%s instrument_id=%s: %s",
            user_id, revision_id, instrument_id, exc,
        )
        raise

    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    # 不存在则插入（ON CONFLICT DO NOTHING 处理并发创建）
    insert_stmt = (
        pg_insert(MonitoringPlanState)
        .values(
            user_id=user_id,
            monitoring_plan_id=monitoring_plan_id,
            revision_id=revision_id,
            instrument_id=instrument_id,
            status=initial_status,
            confirmed_member_ids=[],
            state_payload={},
            lock_version=0,
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "revision_id", "instrument_id"]
        )
        .returning(MonitoringPlanState)
    )
    try:
        insert_result = await session.execute(insert_stmt)
    except Exception as exc:
        logger.warning(
            "插入 monitoring_plan_state 失败 user_id=%s revision_id=%s instrument_id=%s: %s",
            user_id, revision_id, instrument_id, exc,
        )
        raise

    row = insert_result.scalar_one_or_none()
    if row is not None:
        logger.info(
            "创建 monitoring_plan_state: user_id=%s revision_id=%s instrument_id=%s",
            user_id, revision_id, instrument_id,
        )
        return row

    # 并发场景：另一事务已插入，重新查询
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "重查 monitoring_plan_state 失败 user_id=%s revision_id=%s instrument_id=%s: %s",
            user_id, revision_id, instrument_id, exc,
        )
        raise
    return result.scalar_one()


async def get_state(
    session: AsyncSession,
    state_id: UUID,
) -> MonitoringPlanState | None:
    """查询状态详情。

    Args:
        session: 异步会话
        state_id: 状态 ID

    Returns:
        MonitoringPlanState 对象或 None

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(MonitoringPlanState).where(MonitoringPlanState.id == state_id)
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning("查询 monitoring_plan_state 详情失败 state_id=%s: %s", state_id, exc)
        raise
    return result.scalar_one_or_none()


async def get_state_by_key(
    session: AsyncSession,
    *,
    user_id: UUID,
    revision_id: UUID,
    instrument_id: UUID,
) -> MonitoringPlanState | None:
    """按唯一键查询状态。

    Args:
        session: 异步会话
        user_id: 用户 ID
        revision_id: 方案版本 ID
        instrument_id: 股票 ID

    Returns:
        MonitoringPlanState 对象或 None
    """
    stmt = select(MonitoringPlanState).where(
        MonitoringPlanState.user_id == user_id,
        MonitoringPlanState.revision_id == revision_id,
        MonitoringPlanState.instrument_id == instrument_id,
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "按 key 查询 monitoring_plan_state 失败 user_id=%s revision_id=%s instrument_id=%s: %s",
            user_id, revision_id, instrument_id, exc,
        )
        raise
    return result.scalar_one_or_none()


async def update_state(
    session: AsyncSession,
    *,
    state_id: UUID,
    expected_lock_version: int,
    new_status: str | None = None,
    window_started_at: datetime | None = None,
    window_deadline_at: datetime | None = None,
    cooldown_until: datetime | None = None,
    confirmed_member_ids: list[UUID] | None = None,
    vetoed_by_member_id: UUID | None = None,
    state_payload: dict[str, Any] | None = None,
    clear_window: bool = False,
    clear_cooldown: bool = False,
    clear_vetoed: bool = False,
) -> MonitoringPlanState:
    """乐观锁更新状态。

    使用 WHERE lock_version = expected_lock_version 实现乐观锁：
    - 匹配则更新（lock_version +1）
    - 不匹配则 0 行受影响，抛出 StateVersionConflictError

    None 字段表示不更新该字段；clear_* 参数表示将该字段置为 NULL。

    Args:
        session: 异步会话
        state_id: 状态 ID
        expected_lock_version: 期望的 lock_version（乐观锁）
        new_status: 新状态（None 表示不更新）
        window_started_at: 窗口开始时间（None 表示不更新）
        window_deadline_at: 窗口截止时间（None 表示不更新）
        cooldown_until: 冷却截止时间（None 表示不更新）
        confirmed_member_ids: 已确认成员 ID 列表（None 表示不更新）
        vetoed_by_member_id: 否决成员 ID（None 表示不更新）
        state_payload: 状态附加信息（None 表示不更新）
        clear_window: 是否清空 window_started_at/window_deadline_at
        clear_cooldown: 是否清空 cooldown_until
        clear_vetoed: 是否清空 vetoed_by_member_id

    Returns:
        更新后的 MonitoringPlanState 对象

    Raises:
        StateVersionConflictError: lock_version 不匹配（乐观锁冲突）
        Exception: 更新失败时补充上下文后 re-raise
    """
    from sqlalchemy import update

    # 构建更新字段
    set_values: dict[str, Any] = {"lock_version": expected_lock_version + 1}
    if new_status is not None:
        set_values["status"] = new_status
    if window_started_at is not None:
        set_values["window_started_at"] = window_started_at
    if window_deadline_at is not None:
        set_values["window_deadline_at"] = window_deadline_at
    if cooldown_until is not None:
        set_values["cooldown_until"] = cooldown_until
    if confirmed_member_ids is not None:
        set_values["confirmed_member_ids"] = confirmed_member_ids
    if vetoed_by_member_id is not None:
        set_values["vetoed_by_member_id"] = vetoed_by_member_id
    if state_payload is not None:
        set_values["state_payload"] = state_payload
    if clear_window:
        set_values["window_started_at"] = None
        set_values["window_deadline_at"] = None
    if clear_cooldown:
        set_values["cooldown_until"] = None
    if clear_vetoed:
        set_values["vetoed_by_member_id"] = None

    stmt = (
        update(MonitoringPlanState)
        .where(
            MonitoringPlanState.id == state_id,
            MonitoringPlanState.lock_version == expected_lock_version,
        )
        .values(**set_values)
        .returning(MonitoringPlanState)
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "更新 monitoring_plan_state 失败 state_id=%s lock_version=%s: %s",
            state_id, expected_lock_version, exc,
        )
        raise

    row = result.scalar_one_or_none()
    if row is None:
        # 0 行受影响：lock_version 不匹配
        raise StateVersionConflictError(
            f"乐观锁冲突: state_id={state_id} expected_lock_version={expected_lock_version}"
        )

    logger.info(
        "更新 monitoring_plan_state: state_id=%s status=%s lock_version=%s->%s",
        state_id, row.status, expected_lock_version, row.lock_version,
    )
    return row


async def update_state_with_retry(
    session: AsyncSession,
    *,
    state_id: UUID,
    expected_lock_version: int,
    replay_fn: Callable[[MonitoringPlanState], Awaitable[dict[str, Any]]],
) -> MonitoringPlanState:
    """乐观锁冲突时重读重放。

    流程：
    1. 使用 expected_lock_version 尝试更新
    2. 冲突（StateVersionConflictError）时重新读取最新状态
    3. 调用 replay_fn 重新计算新状态字段
    4. 用最新 lock_version 重试更新
    5. 最多重试 MAX_RETRY 次

    replay_fn 接收最新 state，返回更新字段字典（key 为 update_state 的参数名）。

    Args:
        session: 异步会话
        state_id: 状态 ID
        expected_lock_version: 期望的 lock_version（首次尝试）
        replay_fn: 重放回调，接收最新 state，返回更新字段字典

    Returns:
        更新后的 MonitoringPlanState 对象

    Raises:
        StateVersionConflictError: 重试 MAX_RETRY 次后仍冲突
        Exception: 其他失败时补充上下文后 re-raise
    """
    current_lock_version = expected_lock_version
    last_error: Exception | None = None

    for attempt in range(MAX_RETRY):
        try:
            # 调用 replay_fn 计算更新字段（首次使用传入的 expected_lock_version）
            if attempt == 0:
                # 首次尝试：调用方已计算好字段，直接更新
                # 但为统一流程，仍调用 replay_fn
                current_state = await get_state(session, state_id)
                if current_state is None:
                    raise ValueError(f"状态不存在: state_id={state_id}")
                update_fields = await replay_fn(current_state)
                current_lock_version = current_state.lock_version
            else:
                # 重试：重新读取最新状态
                current_state = await get_state(session, state_id)
                if current_state is None:
                    raise ValueError(f"状态不存在: state_id={state_id}")
                update_fields = await replay_fn(current_state)
                current_lock_version = current_state.lock_version

            return await update_state(
                session,
                state_id=state_id,
                expected_lock_version=current_lock_version,
                **update_fields,
            )
        except StateVersionConflictError as e:
            last_error = e
            logger.warning(
                "乐观锁冲突，重试 attempt=%s/%s state_id=%s",
                attempt + 1, MAX_RETRY, state_id,
            )
            continue

    raise StateVersionConflictError(
        f"重试 {MAX_RETRY} 次后仍冲突: state_id={state_id} last_error={last_error}"
    )


async def list_states_by_revision(
    session: AsyncSession,
    revision_id: UUID,
    *,
    status_filter: str | None = None,
) -> list[MonitoringPlanState]:
    """查询方案版本下的所有状态。

    Args:
        session: 异步会话
        revision_id: 方案版本 ID
        status_filter: 可选状态过滤

    Returns:
        MonitoringPlanState 列表（按 updated_at 倒序）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(MonitoringPlanState).where(
        MonitoringPlanState.revision_id == revision_id
    )
    if status_filter is not None:
        stmt = stmt.where(MonitoringPlanState.status == status_filter)
    stmt = stmt.order_by(MonitoringPlanState.updated_at.desc())
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 monitoring_plan_state 列表失败 revision_id=%s: %s",
            revision_id, exc,
        )
        raise
    return list(result.scalars().all())


async def list_states_by_instrument(
    session: AsyncSession,
    instrument_id: UUID,
    *,
    user_id: UUID | None = None,
) -> list[MonitoringPlanState]:
    """查询股票下的所有状态。

    Args:
        session: 异步会话
        instrument_id: 股票 ID
        user_id: 可选用户过滤

    Returns:
        MonitoringPlanState 列表（按 updated_at 倒序）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(MonitoringPlanState).where(
        MonitoringPlanState.instrument_id == instrument_id
    )
    if user_id is not None:
        stmt = stmt.where(MonitoringPlanState.user_id == user_id)
    stmt = stmt.order_by(MonitoringPlanState.updated_at.desc())
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 monitoring_plan_state 列表失败 instrument_id=%s: %s",
            instrument_id, exc,
        )
        raise
    return list(result.scalars().all())


if __name__ == "__main__":
    # 自测入口：验证函数签名与可调用性（不连 DB，无副作用）
    import inspect

    for fn in (
        get_or_create_state, get_state, get_state_by_key, update_state,
        update_state_with_retry, list_states_by_revision, list_states_by_instrument,
    ):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        print(f"{fn.__name__} params={list(inspect.signature(fn).parameters.keys())}")

    # 验证异常类
    assert issubclass(StateVersionConflictError, Exception)
    assert MAX_RETRY > 0
    print(f"MAX_RETRY={MAX_RETRY}")
    print("OK")
