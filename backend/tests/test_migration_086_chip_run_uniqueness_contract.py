"""Migration 086 contract tests — chip_consensus_runs 数据库级唯一约束。

[Corrective-3.1 §1.1] 验证 086_chip_consensus_run_uniqueness 迁移的正确性：

- revision / down_revision 链正确（085 → 086）
- upgrade 必须先做重复 preflight：存在重复则明确报错、不修改历史行
- 无重复时创建硬唯一约束
- downgrade 只删约束，不修改业务数据
- 约束名称与 ORM 模型 ChipConsensusRun 的 UniqueConstraint 完全一致

**注意**：本测试为纯文件级静态契约检查，**不连接 PostgreSQL**，可在
PURE_UNIT_TEST=1 下完整运行（postgres connections = 0）。真实数据库的
重复 preflight / 约束创建 / 并发幂等将在阶段 4（隔离 PG 集成）验证。

测试策略：
- 读取迁移文件源码，断言 preflight / raise / create_unique_constraint 逻辑存在
- 读取 ORM 模型源码，断言约束名称与列顺序一致
- 用 compile 校验 086 模块可正常导入且 revision 常量正确
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "086_chip_consensus_run_uniqueness.py"
)
_MODEL_FILE = (
    Path(__file__).parent.parent
    / "app"
    / "models"
    / "chip_consensus_run.py"
)

_CONSTRAINT_NAME = "uq_chip_consensus_runs_date_core_algo"
_UNIQUE_COLUMNS = ["trade_date", "source_core_run_id", "algorithm_version"]


# ============================================================
# 文件存在性 & revision 链
# ============================================================


def test_migration_file_exists():
    assert _MIGRATION_FILE.exists(), f"迁移文件不存在: {_MIGRATION_FILE}"


def test_model_file_exists():
    assert _MODEL_FILE.exists(), f"模型文件不存在: {_MODEL_FILE}"


def test_migration_revision_chain():
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert 'revision: str = "086_chip_consensus_run_uniqueness"' in source, (
        "revision 必须为 086_chip_consensus_run_uniqueness"
    )
    assert 'down_revision: str | None = "085_board_definition_identity_contract"' in source, (
        "down_revision 必须为 085_board_definition_identity_contract"
    )


# ============================================================
# upgrade 逻辑：重复 preflight（不修改历史行）
# ============================================================


def test_upgrade_does_not_modify_history_rows():
    """upgrade 不得修改任何历史业务记录（不得出现 UPDATE/DELETE/status='cancelled'）。

    修复前把重复行置 cancelled，但 cancelled 不改变唯一键三列，重复组依然存在，
    唯一约束仍无法创建。正确做法是不碰历史行，只做 preflight + 建约束。
    """
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "status = 'cancelled'" not in source, (
        "不得把重复行置 cancelled 来伪造去重"
    )
    assert "UPDATE chip_consensus_runs" not in source, (
        "upgrade 不得 UPDATE chip_consensus_runs 历史行"
    )
    assert "DELETE FROM chip_consensus_runs" not in source, (
        "upgrade 不得 DELETE chip_consensus_runs 历史行"
    )


def test_upgrade_has_duplicate_preflight_check():
    """upgrade 必须先做重复 preflight 检查（只读 SELECT 检测重复组）。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "HAVING COUNT(*) > 1" in source, "缺少重复组检测（HAVING COUNT > 1）"
    assert "_duplicate_groups_exist" in source, "缺少 _duplicate_groups_exist 检查函数"


def test_upgrade_raises_on_duplicate_with_detail():
    """存在重复时 upgrade 必须明确报错并输出重复组详情，且事务回滚。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "RAISE" in source.upper() or "raise Exception" in source, (
        "存在重复时必须明确报错"
    )
    assert "_raise_duplicate_error" in source, "缺少 _raise_duplicate_error"
    # 报错信息应提示数据对账而非自动处理
    assert "数据对账" in source, "报错信息应引导人工数据对账"


def test_upgrade_creates_constraint_only_when_no_duplicate():
    """无重复时才创建唯一约束（create_unique_constraint 在 preflight 之后）。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "create_unique_constraint" in source, "缺少 create_unique_constraint"
    # preflight 检查必须在建约束之前
    preflight_pos = source.index("_duplicate_groups_exist")
    create_pos = source.index("create_unique_constraint")
    assert preflight_pos < create_pos, "重复 preflight 必须在 create_unique_constraint 之前"


# ============================================================
# downgrade 逻辑：只删约束
# ============================================================


def test_downgrade_only_drops_constraint():
    """downgrade 只删除约束，不修改业务数据。"""
    source = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "drop_constraint" in source, "downgrade 必须 drop_constraint"
    # downgrade 内不得有 UPDATE/DELETE 业务数据
    down_start = source.index("def downgrade")
    down_body = source[down_start:]
    assert "UPDATE chip_consensus_runs" not in down_body, (
        "downgrade 不得修改 chip_consensus_runs"
    )
    assert "DELETE FROM chip_consensus_runs" not in down_body, (
        "downgrade 不得删除 chip_consensus_runs"
    )


# ============================================================
# 约束名称与 ORM 模型一致
# ============================================================


def test_constraint_name_matches_orm_model():
    """迁移约束名必须与 ORM 模型 ChipConsensusRun 的 UniqueConstraint 一致。"""
    migration_source = _MIGRATION_FILE.read_text(encoding="utf-8")
    model_source = _MODEL_FILE.read_text(encoding="utf-8")
    assert _CONSTRAINT_NAME in migration_source, (
        f"迁移缺少约束名 {_CONSTRAINT_NAME}"
    )
    assert _CONSTRAINT_NAME in model_source, (
        f"ORM 模型缺少约束名 {_CONSTRAINT_NAME}"
    )


def test_unique_columns_match_orm_model():
    """迁移唯一约束的列必须与 ORM 模型 UniqueConstraint 的列顺序一致。"""
    model_source = _MODEL_FILE.read_text(encoding="utf-8")
    # 在模型的 UniqueConstraint(...) 中提取列名
    tree = ast.parse(model_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            func_name = getattr(func, "attr", getattr(func, "id", ""))
            if func_name == "UniqueConstraint":
                args = [a for a in node.args if isinstance(a, ast.Constant)]
                cols = [a.value for a in args if isinstance(a.value, str)]
                if _CONSTRAINT_NAME in cols or "trade_date" in cols:
                    assert cols[:3] == _UNIQUE_COLUMNS, (
                        f"ORM 唯一约束列顺序应为 {_UNIQUE_COLUMNS}，实际 {cols[:3]}"
                    )
                    return
    pytest.fail("未在 ORM 模型中找到 chip 唯一约束定义")


# ============================================================
# 模块可导入 & 常量正确
# ============================================================


def test_migration_module_imports_and_constants():
    """迁移模块可在不连库情况下导入，且 revision / 约束常量正确。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "m086_contract_check", _MIGRATION_FILE,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    assert module.revision == "086_chip_consensus_run_uniqueness"
    assert module.down_revision == "085_board_definition_identity_contract"
    assert module._CONSTRAINT == _CONSTRAINT_NAME
    assert list(module._UNIQUE_COLUMNS) == _UNIQUE_COLUMNS
    # upgrade / downgrade 函数存在
    assert callable(getattr(module, "upgrade", None))
    assert callable(getattr(module, "downgrade", None))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
