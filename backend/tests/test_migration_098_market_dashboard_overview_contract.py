"""Migration 098 contract — Market Dashboard overview additive columns（纯单元）。

与 095 纯单元测试同策略：不连库，仅做源码级 / ORM 级契约检查：
- revision 链：098 / down 097_split_market_review_capability；
- ``_NEW_COLUMNS`` 精确等于约定的 11 个 additive 列；
- 全部可空、且 upgrade 不添加任何 CHECK 约束（NULL 与真实 0 必须可区分）；
- ORM ``MarketDashboardMarketDaily`` 一一包含这些可空列；
- upgrade 不含误导性的「幂等跳过」注释（op.add_column 不做存在性检查）。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import sqlalchemy as sa

from app.models import market_dashboard as md

_MIGRATION_FILE = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "098_market_dashboard_overview_metrics.py"
)

_EXPECTED_COLUMNS = (
    "advance_count",
    "decline_count",
    "flat_count",
    "change_valid_count",
    "turnover_amount",
    "turnover_valid_count",
    "limit_up_count",
    "limit_down_count",
    "sse_close",
    "szse_close",
    "chinext_close",
)

_TABLE = "market_dashboard_market_daily"


def _migration_source() -> str:
    return _MIGRATION_FILE.read_text(encoding="utf-8")


def _load_migration():
    spec = importlib.util.spec_from_file_location("m098_contract", _MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_body(src: str) -> str:
    return src[src.index("def upgrade") : src.index("def downgrade")]


# ---------------------------------------------------------------
# A. revision 链
# ---------------------------------------------------------------
def test_migration_revision_chain():
    src = _migration_source()
    assert re.search(
        r'\brevision\b\s*(?::[^=]+)?=\s*"098_market_dashboard_overview_metrics"', src
    ), "revision 必须为 098_market_dashboard_overview_metrics"
    assert re.search(
        r'\bdown_revision\b\s*(?::[^=]+)?=\s*"097_split_market_review_capability"', src
    ), "down_revision 必须为 097_split_market_review_capability"


def test_migration_module_constants():
    m = _load_migration()
    assert m.revision == "098_market_dashboard_overview_metrics"
    assert m.down_revision == "097_split_market_review_capability"
    assert callable(getattr(m, "upgrade", None))
    assert callable(getattr(m, "downgrade", None))


# ---------------------------------------------------------------
# B. _NEW_COLUMNS 精确 + 全可空 + 无 CHECK
# ---------------------------------------------------------------
def test_new_columns_exact():
    m = _load_migration()
    names = {c[0] for c in m._NEW_COLUMNS}
    assert names == set(_EXPECTED_COLUMNS), f"列不一致: {names ^ set(_EXPECTED_COLUMNS)}"


def test_new_columns_nullable_and_no_check():
    m = _load_migration()
    up = _upgrade_body(_migration_source())
    int_cols = {
        "advance_count", "decline_count", "flat_count", "change_valid_count",
        "turnover_valid_count", "limit_up_count", "limit_down_count",
    }
    float_cols = {"turnover_amount", "sse_close", "szse_close", "chinext_close"}
    for name, col_type in m._NEW_COLUMNS:
        if name in int_cols:
            assert isinstance(col_type, sa.Integer), f"{name} 应为 Integer"
        elif name in float_cols:
            assert isinstance(col_type, sa.Float), f"{name} 应为 Float"
    # upgrade 用模板 sa.Column(name, col_type, nullable=True) 逐列添加（循环变量，无字面量列名）
    assert "nullable=True" in up, "upgrade 必须 nullable=True"
    # 全部可空、且 upgrade 不添加任何 CHECK 约束（NULL 与真实 0 必须可区分）
    assert "CheckConstraint" not in up
    assert "op.create_check_constraint" not in up
    assert "sa.CheckConstraint" not in up


def test_no_misleading_idempotency_comment():
    up = _upgrade_body(_migration_source())
    assert "若已存在" not in up, "op.add_column 不做存在性检查，不得声称幂等跳过"


# ---------------------------------------------------------------
# C. ORM 表一一对齐
# ---------------------------------------------------------------
def test_orm_model_has_additive_columns():
    cols = {c.name for c in md.MarketDashboardMarketDaily.__table__.columns}
    for name in _EXPECTED_COLUMNS:
        assert name in cols, f"ORM 缺列 {name}"
    for name in _EXPECTED_COLUMNS:
        col = md.MarketDashboardMarketDaily.__table__.columns[name]
        assert col.nullable is True, f"ORM 列 {name} 必须 nullable"


def test_orm_model_uses_float_for_amount_and_index():
    amount = md.MarketDashboardMarketDaily.__table__.columns["turnover_amount"]
    sse = md.MarketDashboardMarketDaily.__table__.columns["sse_close"]
    assert isinstance(amount.type, sa.Float)
    assert isinstance(sse.type, sa.Float)
