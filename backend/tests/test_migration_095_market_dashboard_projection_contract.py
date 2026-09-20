"""Migration 095 contract — Market Dashboard read projection schema（纯单元部分）。

本文件只做**不连库**的契约检查：
- 095 迁移源码级结构（revision 链、建表、PK/FK/index、CHECK、downgrade 顺序）；
- ORM（app/models/market_dashboard.py）与迁移列/约束一一对齐；
- ratio / normalized_index / delta / scope name/type/level 等派生列不存在。

真实 PG introspection（建表、PK、FK CASCADE、index、CHECK 实际存在）在
``test_migration_095_market_dashboard_projection_pg.py``，用 postgres marker 隔离。

注意：本文件**不得**出现 conftest 的 PG 源码标志（否则整模块会被判为 postgres 并在
PURE_UNIT 下跳过），因此这里不导入任何 DB session 工厂。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa

# 复用仓库既有 manifest loader（scripts/verify，纯 stdlib，无 app 依赖）
_VERIFY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "verify"
if str(_VERIFY_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFY_DIR))

from evidence_manifest import load_evidence_manifest  # noqa: E402

_MIGRATION_FILE = (
    Path(__file__).parent.parent / "alembic" / "versions" / "095_market_dashboard_projection.py"
)
_MODEL_FILE = Path(__file__).parent.parent / "app" / "models" / "market_dashboard.py"
_PG_TEST_FILE = Path(__file__).parent / "test_migration_095_market_dashboard_projection_pg.py"

_WINDOWS = (5, 10, 20, 50, 120)

_MARKET_TABLE = "market_dashboard_market_daily"
_SCOPE_TABLE = "market_dashboard_scope_daily"

# 两张投影表共用的计数列
_COUNT_COLUMNS = frozenset(
    {"member_count", "valid_return_count", "equal_weight_return"}
    | {f"ma{k}_{suffix}_count" for k in _WINDOWS for suffix in ("valid", "above")}
)

_EXPECTED_MARKET_COLUMNS = _COUNT_COLUMNS | {"trade_date", "updated_at"}
_EXPECTED_SCOPE_COLUMNS = _COUNT_COLUMNS | {
    "board_id",
    "trade_date",
    "membership_version",
    "updated_at",
}

_EXPECTED_MARKET_CHECK_NAMES = frozenset(
    {
        f"{_MARKET_TABLE}_member_count_check",
        f"{_MARKET_TABLE}_valid_return_count_check",
        f"{_MARKET_TABLE}_valid_return_le_member_check",
    }
    | {f"{_MARKET_TABLE}_ma{k}_counts_check" for k in _WINDOWS}
)
_EXPECTED_SCOPE_CHECK_NAMES = frozenset(
    {
        f"{_SCOPE_TABLE}_member_count_check",
        f"{_SCOPE_TABLE}_valid_return_count_check",
        f"{_SCOPE_TABLE}_valid_return_le_member_check",
    }
    | {f"{_SCOPE_TABLE}_ma{k}_counts_check" for k in _WINDOWS}
)

_FORBIDDEN_COLUMNS = frozenset(
    {
        "ratio",
        "normalized_index",
        "scope_name",
        "scope_type",
        "hierarchy_level",
    }
)


# ============================================================
# helpers
# ============================================================
def _migration_source() -> str:
    return _MIGRATION_FILE.read_text(encoding="utf-8")


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m095_contract", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_body(src: str) -> str:
    return src[src.index("def upgrade") : src.index("def downgrade")]


def _downgrade_body(src: str) -> str:
    return src[src.index("def downgrade") :]


# ============================================================
# A. 迁移文件与 revision 链
# ============================================================
def test_migration_file_exists() -> None:
    assert _MIGRATION_FILE.exists(), f"迁移文件不存在: {_MIGRATION_FILE}"


def test_migration_revision_chain() -> None:
    src = _migration_source()
    assert re.search(r'\brevision\b\s*(?::[^=]+)?=\s*"095_market_dashboard_projection"', src), (
        "revision 必须为 095_market_dashboard_projection"
    )
    assert re.search(r'\bdown_revision\b\s*(?::[^=]+)?=\s*"094_invite_code_ciphertext"', src), (
        "down_revision 必须为 094_invite_code_ciphertext（真实 head，不得猜）"
    )


def test_migration_module_imports_and_constants() -> None:
    module = _load_migration()
    assert module.revision == "095_market_dashboard_projection"
    assert module.down_revision == "094_invite_code_ciphertext"
    assert callable(getattr(module, "upgrade", None))
    assert callable(getattr(module, "downgrade", None))


# ============================================================
# B. upgrade 创建两张表；downgrade 顺序（先 scope 后 market）
# ============================================================
def test_upgrade_creates_both_tables() -> None:
    up = _upgrade_body(_migration_source())
    assert f'op.create_table(\n        "{_MARKET_TABLE}"' in up or (
        f'"{_MARKET_TABLE}"' in up and "op.create_table" in up
    ), "upgrade 必须 create_table market table"
    assert f'"{_SCOPE_TABLE}"' in up, "upgrade 必须 create_table scope table"
    assert up.count("op.create_table") == 2, "upgrade 应恰好创建两张表"


def test_downgrade_order_scope_before_market() -> None:
    dn = _downgrade_body(_migration_source())
    idx_scope = dn.index(f'op.drop_table("{_SCOPE_TABLE}")')
    idx_market = dn.index(f'op.drop_table("{_MARKET_TABLE}")')
    assert idx_scope < idx_market, "downgrade 必须先 drop scope table 再 drop market table"


# ============================================================
# C. 列集合（复用迁移 helper 得到精确列，再与期望对齐）
# ============================================================
def test_migration_count_columns_exact() -> None:
    module = _load_migration()
    cols = {c.name for c in module._count_columns()}
    assert cols == _COUNT_COLUMNS, f"计数列不一致: {cols ^ _COUNT_COLUMNS}"


def test_migration_equal_weight_return_nullable_float() -> None:
    module = _load_migration()
    by_name = {c.name: c for c in module._count_columns()}
    col = by_name["equal_weight_return"]
    assert col.nullable is True, "equal_weight_return 必须 nullable"
    assert isinstance(col.type, sa.Float), "equal_weight_return 必须为浮点（double precision）"


def test_migration_market_columns() -> None:
    module = _load_migration()
    up = _upgrade_body(_migration_source())
    # market 表 = 计数列 + trade_date(PK) + updated_at
    assert f'"{_MARKET_TABLE}"' in up
    assert 'sa.Column("trade_date", sa.Date(), primary_key=True' in up, (
        "market 表 trade_date 必须为 PRIMARY KEY"
    )
    assert 'sa.Column(\n            "updated_at"' in up or '"updated_at"' in up
    assert {c.name for c in module._count_columns()} | {
        "trade_date",
        "updated_at",
    } == _EXPECTED_MARKET_COLUMNS


def test_migration_scope_columns() -> None:
    module = _load_migration()
    up = _upgrade_body(_migration_source())
    assert '"membership_version"' in up, "scope 表必须保存 membership_version"
    assert "sa.String(128)" in up, "membership_version 必须为 VARCHAR(128)"
    assert {c.name for c in module._count_columns()} | {
        "board_id",
        "trade_date",
        "membership_version",
        "updated_at",
    } == _EXPECTED_SCOPE_COLUMNS


# ============================================================
# D. scope PK / FK / index
# ============================================================
def test_scope_primary_key_source() -> None:
    up = _upgrade_body(_migration_source())
    assert 'sa.PrimaryKeyConstraint("board_id", "trade_date")' in up, (
        "scope PK 必须为 (board_id, trade_date)"
    )


def test_scope_foreign_key_cascade_source() -> None:
    up = _upgrade_body(_migration_source())
    assert re.search(
        r'sa\.ForeignKeyConstraint\(\s*\["board_id"\],\s*\["market_boards\.id"\],\s*ondelete="CASCADE"',
        up,
    ), "scope FK board_id -> market_boards.id ON DELETE CASCADE 必须存在"


def test_scope_trade_date_index_source() -> None:
    up = _upgrade_body(_migration_source())
    assert re.search(
        r'op\.create_index\(\s*"ix_market_dashboard_scope_daily_trade_date"\s*,\s*'
        r'"market_dashboard_scope_daily"\s*,\s*\["trade_date"\]',
        up,
    ), "scope 表必须有 trade_date 索引"


# ============================================================
# E. CHECK 约束（counts 非负 + above<=valid<=member + valid_return<=member）
# ============================================================
def _assert_counts_checks(checks, expected_names: frozenset[str]) -> None:
    by_name = {c.name: str(c.sqltext) for c in checks}
    assert set(by_name) == set(expected_names), (
        f"CHECK 名称不一致: {set(by_name) ^ set(expected_names)}"
    )
    joined = " ".join(by_name.values())
    for token in ("member_count >= 0", "valid_return_count >= 0"):
        assert token in joined, f"缺少 CHECK: {token}"
    assert "valid_return_count <= member_count" in joined, "缺少 CHECK: valid_return <= member"
    for k in _WINDOWS:
        assert f"ma{k}_above_count <= ma{k}_valid_count" in joined, (
            f"缺少 CHECK: ma{k}_above <= ma{k}_valid"
        )
        assert f"ma{k}_valid_count <= member_count" in joined, f"缺少 CHECK: ma{k}_valid <= member"


def test_migration_market_check_constraints() -> None:
    module = _load_migration()
    _assert_counts_checks(module._count_checks(_MARKET_TABLE), _EXPECTED_MARKET_CHECK_NAMES)


def test_migration_scope_check_constraints() -> None:
    module = _load_migration()
    _assert_counts_checks(module._count_checks(_SCOPE_TABLE), _EXPECTED_SCOPE_CHECK_NAMES)


# ============================================================
# F. 禁止列（派生值/已有 SSOT）不存在
# ============================================================
def test_no_forbidden_columns_in_migration() -> None:
    module = _load_migration()
    all_cols = {c.name for c in module._count_columns()} | {
        "trade_date",
        "board_id",
        "membership_version",
        "updated_at",
    }
    for name in all_cols:
        assert name not in _FORBIDDEN_COLUMNS, f"禁止列存在: {name}"
        assert not name.endswith("_delta"), f"禁止 delta 列: {name}"
        assert "ratio" not in name, f"禁止 ratio 派生列: {name}"


# ============================================================
# H. ORM 与迁移对齐（不连库）
# ============================================================
def _model_classes() -> tuple[object, object]:
    from app.models.market_dashboard import (
        MarketDashboardMarketDaily,
        MarketDashboardScopeDaily,
    )

    return MarketDashboardMarketDaily, MarketDashboardScopeDaily


def test_orm_market_columns_match_migration() -> None:
    module = _load_migration()
    market_cls, _ = _model_classes()
    migration_cols = {c.name for c in module._count_columns()} | {
        "trade_date",
        "updated_at",
    }
    orm_cols = {c.name for c in market_cls.__table__.columns}  # type: ignore[attr-defined]
    assert orm_cols == _EXPECTED_MARKET_COLUMNS, (
        f"market ORM 列不一致: {orm_cols ^ _EXPECTED_MARKET_COLUMNS}"
    )
    assert orm_cols == migration_cols, "market ORM 列必须与迁移列一致"


def test_orm_scope_columns_match_migration() -> None:
    module = _load_migration()
    _, scope_cls = _model_classes()
    migration_cols = {c.name for c in module._count_columns()} | {
        "board_id",
        "trade_date",
        "membership_version",
        "updated_at",
    }
    orm_cols = {c.name for c in scope_cls.__table__.columns}  # type: ignore[attr-defined]
    assert orm_cols == _EXPECTED_SCOPE_COLUMNS, (
        f"scope ORM 列不一致: {orm_cols ^ _EXPECTED_SCOPE_COLUMNS}"
    )
    assert orm_cols == migration_cols, "scope ORM 列必须与迁移列一致"


def test_orm_primary_keys() -> None:
    market_cls, scope_cls = _model_classes()
    assert [c.name for c in market_cls.__table__.primary_key] == ["trade_date"]  # type: ignore[attr-defined]
    assert [c.name for c in scope_cls.__table__.primary_key] == ["board_id", "trade_date"]  # type: ignore[attr-defined]


def test_orm_scope_foreign_key_cascade() -> None:
    _, scope_cls = _model_classes()
    board_id = scope_cls.__table__.columns["board_id"]  # type: ignore[attr-defined]
    fks = list(board_id.foreign_keys)
    assert fks, "scope.board_id 必须有外键"
    fk = fks[0]
    assert fk.target_fullname == "market_boards.id", f"FK 目标错误: {fk.target_fullname}"
    assert fk.ondelete == "CASCADE", f"FK ondelete 必须 CASCADE，实际 {fk.ondelete}"


def test_orm_scope_trade_date_index() -> None:
    _, scope_cls = _model_classes()
    index_names = {ix.name for ix in scope_cls.__table__.indexes}  # type: ignore[attr-defined]
    assert "ix_market_dashboard_scope_daily_trade_date" in index_names, (
        f"scope ORM 缺少 trade_date 索引: {index_names}"
    )


def test_orm_check_constraints_present() -> None:
    market_cls, scope_cls = _model_classes()
    for cls, expected in (
        (market_cls, _EXPECTED_MARKET_CHECK_NAMES),
        (scope_cls, _EXPECTED_SCOPE_CHECK_NAMES),
    ):
        names = {
            c.name
            for c in cls.__table__.constraints  # type: ignore[attr-defined]
            if isinstance(c, sa.CheckConstraint)
        }
        assert names == set(expected), f"{cls.__name__} CHECK 不一致: {names ^ set(expected)}"


def test_orm_equal_weight_return_nullable() -> None:
    market_cls, scope_cls = _model_classes()
    for cls in (market_cls, scope_cls):
        col = cls.__table__.columns["equal_weight_return"]  # type: ignore[attr-defined]
        assert col.nullable is True, f"{cls.__name__}.equal_weight_return 必须 nullable"


def test_orm_no_forbidden_columns() -> None:
    market_cls, scope_cls = _model_classes()
    for cls in (market_cls, scope_cls):
        for col in cls.__table__.columns:  # type: ignore[attr-defined]
            assert col.name not in _FORBIDDEN_COLUMNS, f"禁止列: {col.name}"
            assert not col.name.endswith("_delta"), f"禁止 delta 列: {col.name}"


def test_model_file_does_not_touch_market_board_or_bar() -> None:
    """不得污染 MarketBoard / BarDaily（只新建独立 model 文件）。"""
    src = _MODEL_FILE.read_text(encoding="utf-8")
    assert "class MarketBoard" not in src, "不得在投影 model 内重定义/污染 MarketBoard"
    assert "class BarDaily" not in src, "不得在投影 model 内定义/污染 BarDaily"


def test_manifest_registers_projection_pg_contract() -> None:
    """F0-FIX1 回归：095 PG 契约必须登记进 closed manifest registry。

    用仓库既有 loader（而非 json.loads）校验 manifest 整体合法性（顶层键、contract 键、
    selector 文件真实存在），再断言 095 这一条登记精确。
    """
    manifest = load_evidence_manifest(
        _VERIFY_DIR / "evidence_manifest.json",
        repo_root=Path(__file__).resolve().parents[2],
    )
    by_id = {c.contract_id: c for c in manifest.contracts}
    contract = by_id.get("market_dashboard_projection_schema_095")
    assert contract is not None, "095 PG contract 必须登记进 evidence_manifest.json"
    assert contract.required is True, "095 PG contract 必须 required=true"
    assert "targeted-pg" in contract.gates, "095 PG contract 必须注册到 targeted-pg gate"
    assert contract.test_selectors == (
        "tests/test_migration_095_market_dashboard_projection_pg.py",
    ), f"selector 必须精确为该 PG 文件: {contract.test_selectors}"


def test_pg_contract_uses_bind_safe_regclass_casts() -> None:
    """F0-FIX2 回归：bind 参数紧邻 PostgreSQL cast（:tbl::regclass）在真实 PG 上
    被 asyncpg 解析为 syntax error at or near ":"。锁死该已知坑（真实 remote 失败，非风格）。

    只针对 ``:bind::type`` 组合；字面 ``confrelid::regclass::text``（无 bind）合法，必须保留。
    """
    src = _PG_TEST_FILE.read_text(encoding="utf-8")
    assert ":tbl::regclass" not in src, "禁止 bind 参数紧邻 ::cast"
    assert src.count("CAST(:tbl AS regclass)") == 4, "必须恰好 4 处 bind-safe 转换"
    # 字面 confrelid::regclass::text（无 bind 参数）必须原样保留
    assert "confrelid::regclass::text" in src


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "--tb=short"])
