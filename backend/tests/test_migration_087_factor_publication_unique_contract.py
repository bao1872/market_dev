"""Migration 087 contract tests — factor_publications partial unique index。

[CHANGE-20260806-CP4A-Amendment] immutable publication history 需要同一 scope/date/kind 允许
历史 superseded 行与当前行并存，只有当前有效 pointer（superseded_by IS NULL）唯一。普通 UNIQUE
约束禁止新旧两行共存，与 supersede 模型冲突。本测试静态验证 Migration 087：

- revision / down_revision 链正确（086 → 087）
- upgrade：drop 普通 UNIQUE 约束 → 创建同名 partial unique index（postgresql_where）
- downgrade：drop partial index → 恢复普通 UNIQUE 约束 → 还原列
- 约束/索引名与 ORM 模型 FactorPublication 一致
- upgrade 不得修改任何 factor_publications 历史业务行

**注意**：本测试为纯文件级静态契约检查，**不连接 PostgreSQL**，可在 PURE_UNIT_TEST=1 下
完整运行（postgres connections = 0）。真实 upgrade→downgrade→upgrade→duplicate-upgrade
在阶段 4（隔离 PG 集成）验证。

测试策略：
- 读取迁移文件源码，断言 drop_constraint / create_index(postgresql_where) 逻辑存在且顺序正确
- 读取 ORM 模型源码，断言 partial index 名与列一致
- 用 compile 校验 087 模块可正常导入且 revision 常量正确
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "087_stock_core_atomic_publication.py"
)
_MODEL_FILE = (
    Path(__file__).parent.parent
    / "app"
    / "models"
    / "factor_publication.py"
)

_UQ_NAME = "uq_factor_publications_scope_date_kind"
_UQ_COLUMNS = ["scope_type", "scope_key", "trade_date", "publication_kind"]
_NEW_COLUMNS = [
    "superseded_by", "superseded_at", "publish_worker_id", "publish_lease_epoch",
]


# ============================================================
# 文件存在性 & revision 链
# ============================================================


def test_migration_file_exists():
    assert _MIGRATION_FILE.exists(), f"迁移文件不存在: {_MIGRATION_FILE}"


def test_model_file_exists():
    assert _MODEL_FILE.exists(), f"模型文件不存在: {_MODEL_FILE}"


def test_migration_revision_chain():
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision = "087_stock_core_atomic_publication"' in source, (
        "revision 必须为 087_stock_core_atomic_publication"
    )
    assert 'down_revision = "086_chip_consensus_run_uniqueness"' in source, (
        "down_revision 必须为 086_chip_consensus_run_uniqueness"
    )


# ============================================================
# upgrade：不修改历史行 + 唯一性改为 partial unique index
# ============================================================


def test_upgrade_does_not_modify_history_rows():
    """upgrade 不得修改任何 factor_publications 历史业务记录。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    up_start = source.index("def upgrade")
    up_body = source[up_start:source.index("def downgrade")]
    assert "UPDATE factor_publications" not in up_body, (
        "upgrade 不得 UPDATE factor_publications 历史行"
    )
    assert "DELETE FROM factor_publications" not in up_body, (
        "upgrade 不得 DELETE factor_publications 历史行"
    )


def test_upgrade_drops_plain_unique_constraint():
    """upgrade 必须 drop 073 建的普通 UNIQUE 约束。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    up_start = source.index("def upgrade")
    up_body = source[up_start:source.index("def downgrade")]
    assert "drop_constraint" in up_body, "upgrade 必须 drop 普通 UNIQUE 约束"
    assert _UQ_NAME in up_body, f"drop 的约束名必须是 {_UQ_NAME}"


def test_upgrade_creates_partial_unique_index():
    """upgrade 必须创建同名 partial unique index（postgresql_where）。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    up_start = source.index("def upgrade")
    up_body = source[up_start:source.index("def downgrade")]
    assert "create_index" in up_body, "upgrade 必须 create_index"
    assert "unique=True" in up_body, "index 必须 unique"
    assert "postgresql_where=sa.text(\"superseded_by IS NULL\")" in up_body, (
        "partial unique index 必须带 postgresql_where（superseded_by IS NULL）"
    )


def test_upgrade_drop_before_create_order():
    """必须先 drop 旧约束，再创建 partial unique index。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    up_start = source.index("def upgrade")
    up_body = source[up_start:source.index("def downgrade")]
    drop_pos = up_body.index("drop_constraint")
    create_pos = up_body.index("create_index")
    assert drop_pos < create_pos, "必须先 drop 旧约束再创建 partial unique index"


def test_upgrade_adds_new_columns():
    """upgrade 必须添加 supersede / fencing 列。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    up_start = source.index("def upgrade")
    up_body = source[up_start:source.index("def downgrade")]
    for col in _NEW_COLUMNS:
        assert col in up_body, f"upgrade 缺少列 {col}"


# ============================================================
# downgrade：还原 partial unique index 为普通 UNIQUE 约束
# ============================================================


def test_downgrade_restores_plain_unique_constraint():
    """downgrade 必须 drop partial index 并恢复普通 UNIQUE 约束。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    down_start = source.index("def downgrade")
    down_body = source[down_start:]
    assert "drop_index" in down_body, "downgrade 必须 drop partial unique index"
    assert "create_unique_constraint" in down_body, (
        "downgrade 必须恢复普通 UNIQUE 约束"
    )
    assert _UQ_NAME in down_body, f"恢复的约束名必须是 {_UQ_NAME}"


def test_downgrade_drops_audit_table_and_columns():
    """downgrade 必须 drop audit 表并还原新增列。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    down_start = source.index("def downgrade")
    down_body = source[down_start:]
    assert "drop_table" in down_body, "downgrade 必须 drop audit 表"
    assert "drop_column" in down_body, "downgrade 必须 drop 新增列"


def test_downgrade_does_not_modify_history_rows():
    """downgrade 不得修改任何 factor_publications 历史业务记录。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    down_start = source.index("def downgrade")
    down_body = source[down_start:]
    assert "UPDATE factor_publications" not in down_body, (
        "downgrade 不得 UPDATE factor_publications"
    )
    assert "DELETE FROM factor_publications" not in down_body, (
        "downgrade 不得 DELETE factor_publications"
    )


# ============================================================
# 索引名/列与 ORM 模型一致
# ============================================================


def test_index_name_matches_orm_model():
    """迁移 partial unique index 名必须与 ORM 模型 FactorPublication 一致。"""
    migration_source = _MIGRATION_FILE.read_text(encoding="utf-8")
    model_source = _MODEL_FILE.read_text(encoding="utf-8")
    assert _UQ_NAME in migration_source, f"迁移缺少索引名 {_UQ_NAME}"
    assert _UQ_NAME in model_source, f"ORM 模型缺少索引名 {_UQ_NAME}"


def test_orm_model_has_no_plain_unique_constraint():
    """ORM 模型不得再定义普通 UniqueConstraint（必须改为 partial unique index）。"""
    model_source = _MODEL_FILE.read_text(encoding="utf-8")
    tree = ast.parse(model_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            func_name = getattr(func, "attr", getattr(func, "id", ""))
            if func_name == "UniqueConstraint":
                pytest.fail(
                    "ORM 模型 FactorPublication 不应再有普通 UniqueConstraint，"
                    "应改为 partial unique Index"
                )
    # 模型必须用 Index + postgresql_where
    assert "postgresql_where=text(\"superseded_by IS NULL\")" in model_source, (
        "ORM 模型必须用 partial unique Index（postgresql_where）"
    )


def test_unique_columns_match_orm_model():
    """迁移 partial unique index 的列必须与 ORM 模型 Index 列顺序一致。"""
    model_source = _MODEL_FILE.read_text(encoding="utf-8")
    tree = ast.parse(model_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            func_name = getattr(func, "attr", getattr(func, "id", ""))
            if func_name == "Index" and _UQ_NAME in [
                a.value for a in node.args if isinstance(a, ast.Constant)
            ]:
                cols = [
                    a.value for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and a.value != _UQ_NAME
                ]
                assert cols == _UQ_COLUMNS, (
                    f"ORM partial index 列顺序应为 {_UQ_COLUMNS}，实际 {cols}"
                )
                return
    pytest.fail("未在 ORM 模型中找到 factor_publications partial unique index")


# ============================================================
# 模块可导入 & 常量正确
# ============================================================


def test_migration_module_imports_and_constants():
    """迁移模块可在不连库情况下导入，且 revision 常量正确。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m087_contract_check", _MIGRATION_FILE,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    assert module.revision == "087_stock_core_atomic_publication"
    assert module.down_revision == "086_chip_consensus_run_uniqueness"
    assert callable(getattr(module, "upgrade", None))
    assert callable(getattr(module, "downgrade", None))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
