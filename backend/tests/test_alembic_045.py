"""Alembic 045 迁移测试 (Task 2, SubTask 2.8).

TDD 红灯阶段：先写失败测试，再实现迁移代码。

验证 045_beta_applications 迁移：
- beta_applications 表存在且包含所有 spec 要求字段
- 索引：status/submitted_at/ip_hash/phone/wechat
- 迁移文件 revision 链正确（down_revision=044_plan_contract_fields）
- downgrade 能正确回滚（DROP TABLE）

测试策略：
- 使用 conftest.db_session（PostgreSQL 测试库，已 alembic upgrade head）
- 通过 information_schema 校验列与索引存在
- 读取迁移文件校验 revision 链
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text


_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "045_beta_applications.py"
)


# ============================================================
# 迁移文件结构测试（无需 DB）
# ============================================================


def test_migration_file_exists():
    """迁移文件 045_beta_applications.py 必须存在。"""
    assert _MIGRATION_FILE.exists(), f"迁移文件不存在: {_MIGRATION_FILE}"


def test_migration_revision_chain():
    """迁移文件 revision 链：revision=045_beta_applications, down_revision=044_plan_contract_fields。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "045_beta_applications"' in source, (
        "revision 必须为 045_beta_applications"
    )
    assert 'down_revision: str | None = "044_plan_contract_fields"' in source, (
        "down_revision 必须为 044_plan_contract_fields"
    )


def test_migration_creates_beta_applications_table():
    """迁移文件必须包含 create_table('beta_applications')。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "create_table" in source, "缺少 create_table 操作"
    assert "beta_applications" in source, "缺少 beta_applications 表创建"


def test_migration_downgrade_drops_table():
    """downgrade 必须能正确回滚（DROP TABLE beta_applications）。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "drop_table" in source.lower() or "DROP TABLE" in source, (
        "downgrade 必须包含 drop_table 操作"
    )
    assert "beta_applications" in source, "downgrade 必须删除 beta_applications 表"


def test_migration_creates_indexes():
    """迁移文件必须创建 status/submitted_at/ip_hash/phone/wechat 索引。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    # 检查 create_index 调用
    assert "create_index" in source, "缺少 create_index 操作"
    for col in ["status", "submitted_at", "ip_hash", "phone", "wechat"]:
        assert col in source, f"迁移文件缺少 {col} 索引相关内容"


# ============================================================
# 表结构测试（需要 PostgreSQL 测试库，已 alembic upgrade head）
# ============================================================


@pytest.mark.asyncio
async def test_beta_applications_table_exists(db_session):
    """beta_applications 表必须存在。"""
    result = await db_session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name='beta_applications'"
        )
    )
    row = result.first()
    assert row is not None, "beta_applications 表不存在"
    assert row[0] == "beta_applications"


@pytest.mark.asyncio
async def test_beta_applications_has_all_required_columns(db_session):
    """beta_applications 必须包含 spec 要求的所有字段。"""
    required_columns = [
        "id",
        "wechat",
        "phone",
        "watch_stock_count",
        "reason_code",
        "reason_other",
        "status",
        "source",
        "admin_note",
        "handled_by",
        "handled_at",
        "submitted_at",
        "updated_at",
        "ip_hash",
        "feishu_delivery_status",
        "feishu_delivered_at",
        "feishu_last_error",
    ]
    result = await db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='beta_applications' ORDER BY column_name"
        )
    )
    actual_columns = {row[0] for row in result.all()}
    for col in required_columns:
        assert col in actual_columns, f"beta_applications 缺少列: {col}"


@pytest.mark.asyncio
async def test_beta_applications_id_is_uuid(db_session):
    """id 列必须为 UUID 类型。"""
    result = await db_session.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='beta_applications' AND column_name='id'"
        )
    )
    row = result.first()
    assert row is not None, "beta_applications 缺少 id 列"
    assert row[0] == "uuid", f"id 列类型应为 uuid，实际 {row[0]}"


@pytest.mark.asyncio
async def test_beta_applications_watch_stock_count_is_integer(db_session):
    """watch_stock_count 列必须为 integer 类型。"""
    result = await db_session.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='beta_applications' AND column_name='watch_stock_count'"
        )
    )
    row = result.first()
    assert row is not None, "beta_applications 缺少 watch_stock_count 列"
    assert row[0] == "integer", f"watch_stock_count 应为 integer，实际 {row[0]}"


@pytest.mark.asyncio
async def test_beta_applications_has_status_check_constraint(db_session):
    """status 列应有 CHECK 约束（限定 new/contacted/approved/rejected/converted）。"""
    result = await db_session.execute(
        text(
            "SELECT constraint_name, check_clause "
            "FROM information_schema.check_constraints "
            "WHERE constraint_name LIKE 'beta_applications_%status%' "
            "OR (check_clause LIKE '%new%' AND check_clause LIKE '%contacted%' "
            "    AND check_clause LIKE '%approved%' AND constraint_name LIKE 'beta_applications%')"
        )
    )
    rows = result.all()
    assert len(rows) > 0, "beta_applications 缺少 status CHECK 约束"


@pytest.mark.asyncio
async def test_beta_applications_has_reason_code_check_constraint(db_session):
    """reason_code 列应有 CHECK 约束（限定 busy/too_many/forget/quant/other）。"""
    result = await db_session.execute(
        text(
            "SELECT constraint_name, check_clause "
            "FROM information_schema.check_constraints "
            "WHERE constraint_name LIKE 'beta_applications_%reason%' "
            "OR (check_clause LIKE '%busy%' AND check_clause LIKE '%too_many%' "
            "    AND check_clause LIKE '%quant%' AND constraint_name LIKE 'beta_applications%')"
        )
    )
    rows = result.all()
    assert len(rows) > 0, "beta_applications 缺少 reason_code CHECK 约束"


@pytest.mark.asyncio
async def test_beta_applications_has_index_on_status(db_session):
    """beta_applications 必须有 status 列的索引。"""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename='beta_applications' "
            "AND indexdef LIKE '%status%'"
        )
    )
    rows = result.all()
    assert len(rows) > 0, "beta_applications 缺少 status 索引"


@pytest.mark.asyncio
async def test_beta_applications_has_index_on_submitted_at(db_session):
    """beta_applications 必须有 submitted_at 列的索引。"""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename='beta_applications' "
            "AND indexdef LIKE '%submitted_at%'"
        )
    )
    rows = result.all()
    assert len(rows) > 0, "beta_applications 缺少 submitted_at 索引"


@pytest.mark.asyncio
async def test_beta_applications_has_index_on_ip_hash(db_session):
    """beta_applications 必须有 ip_hash 列的索引。"""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename='beta_applications' "
            "AND indexdef LIKE '%ip_hash%'"
        )
    )
    rows = result.all()
    assert len(rows) > 0, "beta_applications 缺少 ip_hash 索引"


@pytest.mark.asyncio
async def test_beta_applications_has_index_on_phone(db_session):
    """beta_applications 必须有 phone 列的索引。"""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename='beta_applications' "
            "AND indexdef LIKE '%phone%'"
        )
    )
    rows = result.all()
    assert len(rows) > 0, "beta_applications 缺少 phone 索引"


@pytest.mark.asyncio
async def test_beta_applications_has_index_on_wechat(db_session):
    """beta_applications 必须有 wechat 列的索引。"""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename='beta_applications' "
            "AND indexdef LIKE '%wechat%'"
        )
    )
    rows = result.all()
    assert len(rows) > 0, "beta_applications 缺少 wechat 索引"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
