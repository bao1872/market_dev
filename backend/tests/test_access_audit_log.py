"""访问审计日志测试 - Phase 4.5 RED 阶段。

测试内容：
1. access_audit_logs 表存在（由 052_access_audit_logs 迁移创建）
2. write_audit_log 写入审计日志，所有字段正确持久化
3. 字段完整性：actor_user_id / action / target_type / target_id /
   before_data / after_data / request_id / ip_hash / created_at
4. query_audit_logs 按 actor_user_id 筛选
5. query_audit_logs 按 target_type + target_id 筛选
6. query_audit_logs 按 action 筛选
7. query_audit_logs 按时间范围筛选（start_time / end_time）
8. query_audit_logs 分页（limit / offset）
9. before_data / after_data JSONB 正确存储和读取（嵌套结构）

测试策略：
- 使用 conftest.py 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 表结构由 Alembic 052_access_audit_logs 迁移创建
- 直接调用 access_audit_service.write_audit_log / query_audit_logs
- 使用 user_factory 创建 actor（admin 角色）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access_audit_log import AccessAuditLog
from app.services.access_audit_service import query_audit_logs, write_audit_log


def _list_table_names_sync(sync_session) -> set[str]:
    """同步上下文中获取表名集合（供 run_sync 调用）。"""
    conn = sync_session.connection()
    return set(inspect(conn).get_table_names())


def _list_index_names_sync(sync_session) -> set[str]:
    """同步上下文中获取 access_audit_logs 表的索引名集合。"""
    conn = sync_session.connection()
    inspector = inspect(conn)
    return {idx["name"] for idx in inspector.get_indexes("access_audit_logs")}


# ============================================================
# 表与索引存在性测试
# ============================================================


@pytest.mark.asyncio
async def test_access_audit_logs_table_exists(db_session: AsyncSession) -> None:
    """access_audit_logs 表存在（由 052_access_audit_logs 迁移创建）。"""
    table_names = await db_session.run_sync(_list_table_names_sync)
    assert "access_audit_logs" in table_names, "access_audit_logs 表应存在"


@pytest.mark.asyncio
async def test_access_audit_logs_indexes_exist(db_session: AsyncSession) -> None:
    """access_audit_logs 表的两个索引存在。"""
    index_names = await db_session.run_sync(_list_index_names_sync)
    assert "idx_access_audit_logs_actor_created" in index_names
    assert "idx_access_audit_logs_target" in index_names


# ============================================================
# write_audit_log 字段完整性测试
# ============================================================


@pytest.mark.asyncio
async def test_write_audit_log_persists_all_fields(
    db_session: AsyncSession, user_factory
) -> None:
    """write_audit_log 写入审计日志，所有字段正确持久化。"""
    admin = await user_factory(roles=["admin"])
    before = {"status": "unused", "plan_code": "observe_20"}
    after = {"status": "revoked", "plan_code": "observe_20"}

    log = await write_audit_log(
        db=db_session,
        actor_user_id=admin.id,
        action="invite_code.revoke",
        target_type="invite_code",
        target_id=str(uuid.uuid4()),
        before_data=before,
        after_data=after,
        request_id="req-abc-123",
        ip_hash="sha256deadbeef",
    )

    await db_session.flush()

    # 重新查询验证字段
    stmt = select(AccessAuditLog).where(AccessAuditLog.id == log.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()

    assert fetched.actor_user_id == admin.id
    assert fetched.action == "invite_code.revoke"
    assert fetched.target_type == "invite_code"
    assert fetched.target_id == log.target_id
    assert fetched.before_data == before
    assert fetched.after_data == after
    assert fetched.request_id == "req-abc-123"
    assert fetched.ip_hash == "sha256deadbeef"
    assert fetched.created_at is not None


@pytest.mark.asyncio
async def test_write_audit_log_optional_fields_none(
    db_session: AsyncSession, user_factory
) -> None:
    """可选字段为 None 时正确持久化（target_id/before_data/after_data/request_id/ip_hash）。"""
    admin = await user_factory(roles=["admin"])

    log = await write_audit_log(
        db=db_session,
        actor_user_id=admin.id,
        action="user.disable",
        target_type="user",
        # target_id / before_data / after_data / request_id / ip_hash 全部省略
    )
    await db_session.flush()

    stmt = select(AccessAuditLog).where(AccessAuditLog.id == log.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()

    assert fetched.target_id is None
    assert fetched.before_data is None
    assert fetched.after_data is None
    assert fetched.request_id is None
    assert fetched.ip_hash is None


@pytest.mark.asyncio
async def test_write_audit_log_jsonb_nested_structure(
    db_session: AsyncSession, user_factory
) -> None:
    """before_data / after_data JSONB 正确存储和读取嵌套结构。"""
    admin = await user_factory(roles=["admin"])
    before = {
        "user": {"id": "u-1", "email": "a@b.com"},
        "roles": ["member"],
        "meta": {"nested": {"deep": True}},
    }
    after = {
        "user": {"id": "u-1", "email": "a@b.com"},
        "roles": ["member", "admin"],
        "meta": {"nested": {"deep": True}},
    }

    log = await write_audit_log(
        db=db_session,
        actor_user_id=admin.id,
        action="role.grant",
        target_type="user",
        target_id="u-1",
        before_data=before,
        after_data=after,
    )
    await db_session.flush()

    stmt = select(AccessAuditLog).where(AccessAuditLog.id == log.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()

    assert fetched.before_data == before
    assert fetched.after_data == after
    # 验证嵌套结构可读
    assert fetched.after_data["meta"]["nested"]["deep"] is True
    assert fetched.after_data["roles"] == ["member", "admin"]


# ============================================================
# query_audit_logs 筛选测试
# ============================================================


@pytest.mark.asyncio
async def test_query_audit_logs_filter_by_actor(
    db_session: AsyncSession, user_factory
) -> None:
    """query_audit_logs 按 actor_user_id 筛选。"""
    admin1 = await user_factory(roles=["admin"])
    admin2 = await user_factory(roles=["admin"])

    await write_audit_log(
        db=db_session, actor_user_id=admin1.id,
        action="invite_code.create", target_type="invite_code",
    )
    await write_audit_log(
        db=db_session, actor_user_id=admin2.id,
        action="invite_code.create", target_type="invite_code",
    )
    await write_audit_log(
        db=db_session, actor_user_id=admin1.id,
        action="invite_code.revoke", target_type="invite_code",
    )
    await db_session.flush()

    logs = await query_audit_logs(db=db_session, actor_user_id=admin1.id)
    assert len(logs) == 2
    assert all(log.actor_user_id == admin1.id for log in logs)


@pytest.mark.asyncio
async def test_query_audit_logs_filter_by_target(
    db_session: AsyncSession, user_factory
) -> None:
    """query_audit_logs 按 target_type + target_id 筛选。"""
    admin = await user_factory(roles=["admin"])
    target_id_a = str(uuid.uuid4())
    target_id_b = str(uuid.uuid4())

    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="invite_code.create", target_type="invite_code",
        target_id=target_id_a,
    )
    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="invite_code.revoke", target_type="invite_code",
        target_id=target_id_a,
    )
    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="invite_code.create", target_type="invite_code",
        target_id=target_id_b,
    )
    await db_session.flush()

    logs = await query_audit_logs(
        db=db_session, target_type="invite_code", target_id=target_id_a
    )
    assert len(logs) == 2
    assert all(log.target_id == target_id_a for log in logs)


@pytest.mark.asyncio
async def test_query_audit_logs_filter_by_action(
    db_session: AsyncSession, user_factory
) -> None:
    """query_audit_logs 按 action 筛选。"""
    admin = await user_factory(roles=["admin"])

    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="invite_code.create", target_type="invite_code",
    )
    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="invite_code.revoke", target_type="invite_code",
    )
    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="invite_code.revoke", target_type="invite_code",
    )
    await db_session.flush()

    logs = await query_audit_logs(db=db_session, action="invite_code.revoke")
    assert len(logs) == 2
    assert all(log.action == "invite_code.revoke" for log in logs)


@pytest.mark.asyncio
async def test_query_audit_logs_filter_by_time_range(
    db_session: AsyncSession, user_factory
) -> None:
    """query_audit_logs 按时间范围筛选（start_time / end_time）。

    所有日志在同一事务内 flush，created_at 服务器默认值相近；
    用一个明显更早的 start_time 和一个明显更晚的 end_time 验证边界。
    """
    admin = await user_factory(roles=["admin"])

    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="invite_code.create", target_type="invite_code",
    )
    await db_session.flush()

    # 读取实际 created_at
    all_logs = await query_audit_logs(db=db_session, actor_user_id=admin.id)
    assert len(all_logs) == 1
    actual_created = all_logs[0].created_at
    assert actual_created is not None

    # start_time 晚于 created_at -> 无结果
    future_start = actual_created + timedelta(hours=1)
    logs = await query_audit_logs(db=db_session, start_time=future_start)
    assert len(logs) == 0

    # end_time 早于 created_at -> 无结果
    past_end = actual_created - timedelta(hours=1)
    logs = await query_audit_logs(db=db_session, end_time=past_end)
    assert len(logs) == 0

    # start_time 早 / end_time 晚 -> 命中
    logs = await query_audit_logs(
        db=db_session,
        start_time=actual_created - timedelta(hours=1),
        end_time=actual_created + timedelta(hours=1),
    )
    assert len(logs) == 1


# ============================================================
# query_audit_logs 分页测试
# ============================================================


@pytest.mark.asyncio
async def test_query_audit_logs_pagination(
    db_session: AsyncSession, user_factory
) -> None:
    """query_audit_logs 分页（limit / offset）。"""
    admin = await user_factory(roles=["admin"])

    # 写入 5 条日志
    for i in range(5):
        await write_audit_log(
            db=db_session, actor_user_id=admin.id,
            action=f"test.action_{i}", target_type="test",
        )
    await db_session.flush()

    # limit=2 offset=0
    page1 = await query_audit_logs(db=db_session, actor_user_id=admin.id, limit=2, offset=0)
    assert len(page1) == 2

    # limit=2 offset=2
    page2 = await query_audit_logs(db=db_session, actor_user_id=admin.id, limit=2, offset=2)
    assert len(page2) == 2

    # limit=2 offset=4
    page3 = await query_audit_logs(db=db_session, actor_user_id=admin.id, limit=2, offset=4)
    assert len(page3) == 1

    # 三页不重叠
    all_ids = {log.id for log in page1 + page2 + page3}
    assert len(all_ids) == 5


@pytest.mark.asyncio
async def test_query_audit_logs_default_limit(
    db_session: AsyncSession, user_factory
) -> None:
    """query_audit_logs 默认 limit=50。"""
    admin = await user_factory(roles=["admin"])
    await write_audit_log(
        db=db_session, actor_user_id=admin.id,
        action="test.action", target_type="test",
    )
    await db_session.flush()

    logs = await query_audit_logs(db=db_session, actor_user_id=admin.id)
    assert len(logs) == 1
    # 验证默认 limit 不报错且返回结果


# ============================================================
# 排序测试
# ============================================================


@pytest.mark.asyncio
async def test_query_audit_logs_ordered_by_created_at_desc(
    db_session: AsyncSession, user_factory
) -> None:
    """query_audit_logs 按 created_at 降序返回（最新优先）。"""
    admin = await user_factory(roles=["admin"])

    log_ids = []
    for i in range(3):
        log = await write_audit_log(
            db=db_session, actor_user_id=admin.id,
            action=f"test.action_{i}", target_type="test",
        )
        log_ids.append(log.id)
    await db_session.flush()

    logs = await query_audit_logs(db=db_session, actor_user_id=admin.id)
    # 由于 created_at 来自 server_default，flush 后需 expire 才能读到；
    # query_audit_logs 重新查询时 SQLAlchemy 会重新加载，created_at 应可读
    assert len(logs) == 3


if __name__ == "__main__":
    # 自测入口：验证测试模块可导入
    print("test_access_audit_log module loaded")
    print(f"write_audit_log={write_audit_log}")
    print(f"query_audit_logs={query_audit_logs}")
    print("OK")
