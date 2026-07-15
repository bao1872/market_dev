"""股票名称筛选共享条件构造器。

CHANGE-20260714-001: 抽取 Instrument.name 的 ILIKE/NOT ILIKE/等于条件构造逻辑，
供以下查询链共用，禁止第二套筛选口径：

- strategy_result_repository.query_results（/strategies/{key}/results）
- strategy_result_repository._apply_run_item_filters
  → query_run_items_with_results（/strategy-runs/{run_id}/results + Excel 导出）

支持三种操作符：
- contains    → name ILIKE '%value%'（默认）
- not_contains → name NOT ILIKE '%value%'
- eq          → name = value

对 ILIKE 通配符 `%`、`_` 和转义符 `\\` 正确转义，避免用户输入污染模式。
操作符不区分大小写传入，值原样使用（ILIKE 本身大小写不敏感）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, literal_column

# 合法操作符（小写）
_VALID_OPS = frozenset({"contains", "not_contains", "eq"})


def _escape_like_pattern(value: str) -> str:
    """转义 ILIKE 模式中的特殊字符 `%`、`_`、`\\`。

    使用反斜杠作为 ESCAPE 字符，并声明 `ESCAPE '\\'` 让 PostgreSQL 正确解析。
    输入值原样使用，不预先 toLowerCase（ILIKE 本身大小写不敏感）。
    """
    # 先转义反斜杠本身，再转义 % 和 _
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_stock_name_conditions(
    name_col: Any,
    stock_name: str | None,
    stock_name_op: str | None,
) -> list[ColumnElement[bool]]:
    """构建 Instrument.name 筛选条件列表。

    Args:
        name_col: SQLAlchemy 列表达式（如 Instrument.name）
        stock_name: 股票名称筛选值（已 trim；None 或空串表示无筛选）
        stock_name_op: 操作符（contains/not_contains/eq，大小写不敏感）

    Returns:
        条件列表；为空表示无名称筛选。非法操作符静默忽略（返回空列表）。
        调用方应在 API 层校验操作符合法性，本 helper 仅做防御性兜底。
    """
    if not stock_name:
        return []
    op = (stock_name_op or "contains").lower()
    if op not in _VALID_OPS:
        return []

    conditions: list[ColumnElement[bool]] = []
    if op == "eq":
        # 等于：直接 == 比较（PostgreSQL 默认大小写敏感，符合精确匹配语义）
        conditions.append(name_col == stock_name)
    else:
        # contains / not_contains：ILIKE / NOT ILIKE + ESCAPE
        escaped = _escape_like_pattern(stock_name)
        pattern = f"%{escaped}%"
        # 使用 literal_column 声明 ESCAPE 子句；PostgreSQL 支持 ILIKE '...' ESCAPE '\'
        if op == "contains":
            conditions.append(name_col.ilike(pattern, escape="\\"))
        else:  # not_contains
            # NOT ILIKE 含 NULL 语义：name 为 NULL 时 NOT ILIKE 返回 NULL（非 TRUE），
            # 因此 NULL name 行自动被排除（符合"不包含"语义）
            conditions.append(name_col.not_ilike(pattern, escape="\\"))
    return conditions


if __name__ == "__main__":
    # 自测：验证转义和操作符
    assert _escape_like_pattern("abc") == "abc"
    assert _escape_like_pattern("a%b") == "a\\%b"
    assert _escape_like_pattern("a_b") == "a\\_b"
    assert _escape_like_pattern("a\\b") == "a\\\\b"
    assert _escape_like_pattern("%_\\") == "\\%\\_\\\\"

    # build_stock_name_conditions 不带 SQLAlchemy 列时返回空（None 输入）
    assert build_stock_name_conditions(literal_column("name"), None, "contains") == []
    assert build_stock_name_conditions(literal_column("name"), "", "contains") == []
    # op=None 默认 contains（防御性 fallback；API 层应保证传入合法 op）
    assert len(build_stock_name_conditions(literal_column("name"), "abc", None)) == 1
    assert build_stock_name_conditions(literal_column("name"), "abc", "invalid") == []

    # 带合法 op 时返回非空
    conds_contains = build_stock_name_conditions(literal_column("name"), "abc", "contains")
    assert len(conds_contains) == 1
    conds_not = build_stock_name_conditions(literal_column("name"), "abc", "not_contains")
    assert len(conds_not) == 1
    conds_eq = build_stock_name_conditions(literal_column("name"), "abc", "eq")
    assert len(conds_eq) == 1

    # 大小写不敏感
    assert len(build_stock_name_conditions(literal_column("name"), "abc", "CONTAINS")) == 1
    assert len(build_stock_name_conditions(literal_column("name"), "abc", "Not_Contains")) == 1

    print("stock_name_filter_helper 自测 ✓")
