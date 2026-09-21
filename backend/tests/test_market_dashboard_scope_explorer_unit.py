"""R2 Scope Explorer —— 纯单元合同（validation / 全局 T-T5 解析 / SQL 形态 / source guard）。

不连库：只验证纯函数与**编译后的 SQL 形态**：
- projection-only（不出现 bars_daily / instruments / market_board_memberships）
- 全局 T / PREV 语义（PREV 绑定到全市场 projection 日历的 T-5，不是 per-board 滑窗）
- NULLS LAST（asc/desc 皆然）+ board_id ASC 稳定二级排序
- count(*) OVER()（filter 后、pagination 前 total）
- LEFT JOIN 缺 PREV → 表达式为 NULL（不 fallback）
"""

from __future__ import annotations

import inspect
import pathlib
import re
from datetime import date

import pytest
from sqlalchemy.dialects import postgresql

from app.services import market_dashboard_scope_explorer_service as svc

_FORBIDDEN_TABLES = ("bars_daily", "instruments", "market_board_memberships")


def _sql(**overrides) -> str:
    kwargs = {
        "trade_date": date(2026, 9, 10),
        "prev_trade_date": date(2026, 9, 5),
        "scope_type": "industry",
        "hierarchy_level": None,
        "q": None,
        "ranges": {},
        "sort": "ma5",
        "direction": "desc",
        "page": 1,
        "page_size": 20,
    }
    kwargs.update(overrides)
    stmt = svc.build_explorer_select(**kwargs)
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


# ===========================================================================
# A. validation（→ 422 owner）
# ===========================================================================
def test_validate_rejects_invalid_scope_type() -> None:
    err = svc.validate_explorer_params(
        scope_type="board", hierarchy_level=None, sort="ma5", direction="desc", ranges={}
    )
    assert err is not None and "scope_type" in err


def test_validate_rejects_invalid_hierarchy_level() -> None:
    err = svc.validate_explorer_params(
        scope_type="industry", hierarchy_level="L4", sort="ma5", direction="desc", ranges={}
    )
    assert err is not None and "hierarchy_level" in err


def test_validate_rejects_concept_with_hierarchy_level() -> None:
    """concept + hierarchy_level 必须显式 422（不静默忽略，区别于 rankings）。"""
    err = svc.validate_explorer_params(
        scope_type="concept", hierarchy_level="L1", sort="ma5", direction="desc", ranges={}
    )
    assert err is not None and "hierarchy_level" in err


def test_validate_rejects_invalid_sort_and_direction() -> None:
    assert svc.validate_explorer_params(
        scope_type="industry", hierarchy_level=None, sort="bogus", direction="asc", ranges={}
    )
    assert svc.validate_explorer_params(
        scope_type="industry", hierarchy_level=None, sort="ma5", direction="sideways", ranges={}
    )


@pytest.mark.parametrize(("lo", "hi"), svc.RANGE_PAIRS)
def test_validate_rejects_min_greater_than_max(lo: str, hi: str) -> None:
    err = svc.validate_explorer_params(
        scope_type="industry",
        hierarchy_level=None,
        sort="ma5",
        direction="desc",
        ranges={lo: 1.0, hi: 0.0},
    )
    assert err is not None and lo in err


def test_validate_accepts_valid_params() -> None:
    assert (
        svc.validate_explorer_params(
            scope_type="industry",
            hierarchy_level="L1",
            sort="ma5_delta",
            direction="asc",
            ranges={"ma5_min": 0.1, "ma5_max": 0.9},
        )
        is None
    )


# ===========================================================================
# B. 全局 T / PREV 解析（纯）
# ===========================================================================
def test_resolve_t_prev_without_projection() -> None:
    assert svc.resolve_t_prev([]) == (None, None)


def test_resolve_t_prev_below_window_has_no_previous() -> None:
    dates = [date(2026, 9, 10 - i) for i in range(3)]
    assert svc.resolve_t_prev(dates) == (dates[0], None)


def test_resolve_t_prev_exactly_window_uses_t_minus_5() -> None:
    dates = [date(2026, 9, 10 - i) for i in range(6)]
    t, prev = svc.resolve_t_prev(dates)
    assert t == dates[0]
    assert prev == dates[5]
    # 明确不是 T-1，也不是更老的日期
    assert prev != dates[1]


def test_resolve_t_prev_ignores_dates_beyond_window() -> None:
    dates = [date(2026, 9, 10 - i) for i in range(9)]
    _t, prev = svc.resolve_t_prev(dates)
    assert prev == dates[5]


# ===========================================================================
# C. SQL 形态
# ===========================================================================
@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("sort", svc.SORT_FIELDS)
def test_nulls_last_for_every_sort_and_direction(sort: str, direction: str) -> None:
    sql = _sql(sort=sort, direction=direction)
    order_by = sql.split("ORDER BY", 1)[1]
    assert "NULLS LAST" in order_by, f"{sort}/{direction} 必须 NULLS LAST"
    # 稳定二级排序：board_id ASC
    assert "market_boards.id ASC" in order_by


def test_forbidden_tables_absent_from_sql() -> None:
    sql = _sql(prev_trade_date=None, scope_type="concept")
    for bad in _FORBIDDEN_TABLES:
        assert bad not in sql, f"explorer SQL 不得触碰 {bad}"


def test_prev_row_is_bound_to_global_prev_date() -> None:
    """PREV 必须绑定到全局 T-5 日期；不得 per-board row_number/滑窗。"""
    sql = _sql(prev_trade_date=date(2026, 9, 5))
    assert "LEFT OUTER JOIN market_dashboard_scope_daily AS prev" in sql
    assert "prev.board_id = cur.board_id" in sql
    assert "prev.trade_date = '2026-09-05'" in sql
    assert "row_number" not in sql.lower()


def test_missing_prev_date_uses_false_join_condition() -> None:
    """< T_WINDOW 个 market dates → PREV=None → join 恒不成立（列全 NULL，不 fallback）。"""
    sql = _sql(prev_trade_date=None)
    assert "false" in sql.lower()
    assert "prev.trade_date =" not in sql


def test_total_comes_from_window_count_over() -> None:
    sql = _sql()
    assert "count(*) OVER ()" in sql


def test_ratio_uses_case_guard_against_zero_valid_count() -> None:
    sql = _sql()
    # valid_count <= 0 → NULL（禁止伪造 0%），且必须浮点除法（非 integer division）
    assert "CASE WHEN (cur.ma5_valid_count > 0)" in sql
    assert "CAST(cur.ma5_above_count AS FLOAT)" in sql
    assert "CAST(cur.ma5_valid_count AS FLOAT)" in sql


def test_q_is_escaped_substring_search() -> None:
    sql = _sql(q="银%_行")
    assert "ILIKE" in sql
    assert "ESCAPE" in sql
    # % / _ 通配符被转义（escape char '/'），不得以裸通配符进入 LIKE 模式。
    # 注：literal_binds 会把 '%' 渲染为 '%%'（paramstyle 转义），故按转义片段断言。
    assert "银/" in sql and "/%" in sql and "/_行" in sql
    assert "'银%_行'" not in sql


def test_range_filters_apply_to_sql_expressions() -> None:
    sql = _sql(ranges={"ma5_min": 0.1, "ma5_max": 0.9, "member_count_min": 5.0})
    where = sql.split("WHERE", 1)[1]
    assert ">=" in where and "<=" in where


# ===========================================================================
# D. source guard（projection-only）
# ===========================================================================
def test_service_only_imports_allowed_models_and_no_forbidden_imports() -> None:
    src = pathlib.Path(inspect.getfile(svc)).read_text()
    imports = "\n".join(
        line.strip() for line in src.splitlines() if line.strip().startswith(("import ", "from "))
    )
    model_imports = set(re.findall(r"from app\.models\.(\w+) import", src))
    assert model_imports == {"market_board", "market_dashboard"}, model_imports
    for bad in ("bars", "instrument", "membership", "pandas"):
        assert bad not in imports.lower(), f"service 不得 import {bad}"
