"""Alembic 升级测试 - plan_contract 字段迁移（SubTask 4.14）。

验证 044_plan_contract_fields 迁移：
- invite_codes 表新增 plan_code/monitor_limit/grant_months 列
- 旧数据回填：invite_codes → plan_code='observe_20', monitor_limit=20, grant_months=1
- 迁移文件 revision 链正确（down_revision=043_rename_dsa_selector_display_name）

注意：memberships 表的列与回填测试已移除，因 049_subscriptions_table 已删除 memberships 表。
迁移文件内容测试（test_migration_contains_backfill_for_memberships）仍保留，校验 044 源码。

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库，已 alembic upgrade head）
- 通过 information_schema 校验列存在
- 插入旧风格记录（plan_code=NULL）后手动执行回填 SQL，验证默认映射
- 读取迁移文件校验 revision 链与回填语句
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from app.constants.plan_codes import DEFAULT_PLAN_CODE

_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "044_plan_contract_fields.py"
)


@pytest.mark.asyncio
async def test_invite_codes_has_plan_code_column(db_session):
    """invite_codes 表必须包含 plan_code 列（String/Text，nullable）。"""
    result = await db_session.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='invite_codes' AND column_name='plan_code'"
        )
    )
    row = result.first()
    assert row is not None, "invite_codes 缺少 plan_code 列"
    assert row[0] == "plan_code"
    assert row[1] in ("character varying", "text")


@pytest.mark.asyncio
async def test_invite_codes_has_monitor_limit_column(db_session):
    """invite_codes 表必须包含 monitor_limit 列（Integer）。"""
    result = await db_session.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='invite_codes' AND column_name='monitor_limit'"
        )
    )
    row = result.first()
    assert row is not None, "invite_codes 缺少 monitor_limit 列"
    assert row[0] == "monitor_limit"
    assert row[1] == "integer"


@pytest.mark.asyncio
async def test_invite_codes_has_grant_months_column(db_session):
    """invite_codes 表必须包含 grant_months 列（Integer）。"""
    result = await db_session.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='invite_codes' AND column_name='grant_months'"
        )
    )
    row = result.first()
    assert row is not None, "invite_codes 缺少 grant_months 列"
    assert row[0] == "grant_months"
    assert row[1] == "integer"


def test_migration_file_exists():
    """迁移文件 044_plan_contract_fields.py 必须存在。"""
    assert _MIGRATION_FILE.exists(), f"迁移文件不存在: {_MIGRATION_FILE}"


def test_migration_revision_chain():
    """迁移文件 revision 链：down_revision 必须为 043_rename_dsa_selector_display_name。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "044_plan_contract_fields"' in source, (
        "revision 必须为 044_plan_contract_fields"
    )
    assert 'down_revision: str | None = "043_rename_dsa_selector_display_name"' in source, (
        "down_revision 必须为 043_rename_dsa_selector_display_name"
    )


def test_migration_contains_backfill_for_invite_codes():
    """迁移文件必须包含 invite_codes 旧数据回填 SQL（plan_code=observe_20, monitor_limit=20, grant_months=1）。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "UPDATE invite_codes" in source, "缺少 invite_codes 回填语句"
    assert "observe_20" in source, "回填必须使用 observe_20"
    assert "grant_months" in source, "回填必须设置 grant_months"


def test_migration_contains_backfill_for_memberships():
    """迁移文件必须包含 memberships 旧数据回填 SQL（plan_code=observe_20, monitor_limit=20）。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "UPDATE memberships" in source, "缺少 memberships 回填语句"
    assert "observe_20" in source


def test_migration_contains_downgrade_drop_columns():
    """downgrade 必须能正确回滚（DROP COLUMN）。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "drop_column" in source.lower() or "DROP COLUMN" in source, (
        "downgrade 必须包含 drop_column 操作"
    )


@pytest.mark.asyncio
async def test_backfill_invite_codes_old_data_default_mapping(db_session):
    """旧 invite_codes 记录（plan_code=NULL）回填后应为 observe_20/grant_months=1/monitor_limit=20。

    模拟流程：插入旧风格记录 → 执行回填 UPDATE → 验证字段值。
    """
    import uuid
    from datetime import UTC, datetime

    # 插入一条旧风格 invite_code（不设置 plan_code/monitor_limit/grant_months）
    old_invite_id = uuid.uuid4()
    admin_user_id = uuid.uuid4()
    # 先插入一个临时 user 作为 created_by
    await db_session.execute(
        text(
            "INSERT INTO users (id, email, password_hash, status, timezone) "
            "VALUES (:uid, :email, 'hash', 'active', 'Asia/Shanghai')"
        ),
        {"uid": admin_user_id, "email": f"alembic_test_{uuid.uuid4().hex[:8]}@test.com"},
    )
    await db_session.execute(
        text(
            "INSERT INTO invite_codes (id, code_hash, status, grant_days, created_by, created_at) "
            "VALUES (:id, :hash, 'unused', 30, :created_by, :now)"
        ),
        {
            "id": old_invite_id,
            "hash": f"old_hash_{uuid.uuid4().hex}",
            "created_by": admin_user_id,
            "now": datetime.now(UTC),
        },
    )
    await db_session.flush()

    # 执行回填（模拟迁移中的 UPDATE 语句）
    await db_session.execute(
        text(
            "UPDATE invite_codes SET plan_code='observe_20', monitor_limit=20, grant_months=1 "
            "WHERE plan_code IS NULL"
        )
    )
    await db_session.flush()

    # 验证回填结果
    result = await db_session.execute(
        text(
            "SELECT plan_code, monitor_limit, grant_months FROM invite_codes WHERE id=:id"
        ),
        {"id": old_invite_id},
    )
    row = result.first()
    assert row is not None, "回填后查询不到记录"
    assert row[0] == DEFAULT_PLAN_CODE, f"plan_code 应为 {DEFAULT_PLAN_CODE}，实际 {row[0]}"
    assert row[1] == 20, f"monitor_limit 应为 20，实际 {row[1]}"
    assert row[2] == 1, f"grant_months 应为 1，实际 {row[2]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
