r"""board_filter_helper 单元测试（CHANGE-20260716-007）。

测试内容：
1. 行业一级/二级/三级关键词匹配
2. 行业局部关键词匹配
3. 特殊字符转义（%、_、\）
4. NFKC 规范化
5. 空值/纯空白不生成条件
6. industry+concept AND 语义
7. concept 保持精确匹配
8. 不同关联列（Instrument.id / StrategyRunItem.instrument_id）通用性

测试策略：
- 直接调用 build_board_filter_conditions，断言生成的 SQL condition
- 使用 str(condition) 编译后的 SQL 字符串验证 ilike 模式
- 不依赖数据库（纯单元测试，快速执行）
"""
from __future__ import annotations

from sqlalchemy import Column, String, Table, select
from sqlalchemy.schema import MetaData

from app.repositories.board_filter_helper import (
    _escape_ilike_pattern,
    _normalize_keyword,
    build_board_filter_conditions,
)

# 构造一个测试用的列表达式（模拟 Instrument.id / StrategyRunItem.instrument_id）
_metadata = MetaData()
_test_table = Table(
    "_test_instrument",
    _metadata,
    Column("id", String()),
    Column("name", String()),
)
_test_instrument_id = _test_table.c.id


def _compile_conditions(conditions: list) -> str:
    """编译 conditions 为 SQL 字符串（便于断言）。"""
    if not conditions:
        return ""
    stmt = select(_test_table).where(*conditions)
    return str(
        stmt.compile(compile_kwargs={"literal_binds": True})
    )


# ===== 1. 行业关键词匹配（一级/二级/三级）=====

def test_industry_keyword_matches_first_level() -> None:
    """一级关键词匹配：输入"电子"应生成 ilike '%电子%' 条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "电子", None)
    assert len(conditions) == 1
    sql = _compile_conditions(conditions)
    # SQLAlchemy 编译 ilike 为 LOWER(col) LIKE LOWER(pattern) ESCAPE '\\'
    assert "LIKE" in sql.upper()
    assert "%电子%" in sql


def test_industry_keyword_matches_second_level() -> None:
    """二级关键词匹配：输入"半导体"应生成 ilike '%半导体%' 条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "半导体", None)
    sql = _compile_conditions(conditions)
    assert "%半导体%" in sql


def test_industry_keyword_matches_third_level() -> None:
    """三级关键词匹配：输入"数字芯片"应生成 ilike '%数字芯片%' 条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "数字芯片", None)
    sql = _compile_conditions(conditions)
    assert "%数字芯片%" in sql


def test_industry_keyword_matches_partial() -> None:
    """局部关键词匹配：输入"芯片"应生成 ilike '%芯片%' 条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "芯片", None)
    sql = _compile_conditions(conditions)
    assert "%芯片%" in sql


# ===== 2. 特殊字符转义 =====

def test_industry_keyword_escapes_percent() -> None:
    """% 字符应被转义为 \\%（按字面匹配）。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "50%", None)
    sql = _compile_conditions(conditions)
    # 编译后 SQL 中应含 \%（字面百分号）
    assert "\\%" in sql or "\\\\%" in sql
    # 不应出现未转义的 %keyword% 模式（除两端的 % 占位符外）
    # 即中间的 % 必须被转义


def test_industry_keyword_escapes_underscore() -> None:
    """_ 字符应被转义为 \\_（按字面匹配）。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "stock_name", None)
    sql = _compile_conditions(conditions)
    # 编译后 SQL 中应含 \_
    assert "\\_" in sql or "\\\\_" in sql


def test_industry_keyword_escapes_backslash() -> None:
    """\\ 字符应被转义为 \\\\（按字面匹配）。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "path\\to", None)
    sql = _compile_conditions(conditions)
    # 反斜杠自身被转义
    assert "\\\\" in sql


def test_escape_ilike_pattern_helper() -> None:
    """_escape_ilike_pattern 辅助函数直接测试。"""
    # % 转义
    assert _escape_ilike_pattern("50%") == "50\\%"
    # _ 转义
    assert _escape_ilike_pattern("a_b") == "a\\_b"
    # \ 转义（先转义反斜杠自身）
    assert _escape_ilike_pattern("a\\b") == "a\\\\b"
    # 组合
    assert _escape_ilike_pattern("a\\b%c_d") == "a\\\\b\\%c\\_d"
    # 无特殊字符
    assert _escape_ilike_pattern("电子") == "电子"


# ===== 3. NFKC 规范化 =====

def test_normalize_keyword_nfkc_fullwidth() -> None:
    """NFKC 规范化：全角字符转半角。"""
    # 全角 A → 半角 A
    assert _normalize_keyword("Ａ") == "A"
    # 全角数字 1 → 半角 1
    assert _normalize_keyword("１") == "1"


def test_normalize_keyword_trim() -> None:
    """trim 去除首尾空白。"""
    assert _normalize_keyword("  电子  ") == "电子"
    assert _normalize_keyword("\t半导体\n") == "半导体"


def test_normalize_keyword_empty() -> None:
    """空值返回空串。"""
    assert _normalize_keyword("") == ""
    assert _normalize_keyword(None) == ""  # type: ignore[arg-type]


def test_industry_keyword_nfkc_applied() -> None:
    """行业关键词应经过 NFKC 规范化后匹配。"""
    # 全角"电子"应规范化为半角"电子"（中文不受 NFKC 影响，但英文/数字会）
    conditions = build_board_filter_conditions(_test_instrument_id, "Ａ股", None)
    sql = _compile_conditions(conditions)
    # NFKC 后 "Ａ股" → "A股"
    assert "%A股%" in sql


# ===== 4. 空值/纯空白不生成条件 =====

def test_industry_empty_no_condition() -> None:
    """空字符串不生成 industry 条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "", None)
    assert len(conditions) == 0


def test_industry_none_no_condition() -> None:
    """None 不生成 industry 条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, None, None)
    assert len(conditions) == 0


def test_industry_whitespace_only_no_condition() -> None:
    """纯空白不生成 industry 条件（trim 后为空）。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "   ", None)
    assert len(conditions) == 0


def test_concept_empty_no_condition() -> None:
    """空字符串不生成 concept 条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, None, "")
    assert len(conditions) == 0


# ===== 5. industry+concept AND 语义 =====

def test_industry_and_concept_both_present() -> None:
    """industry + concept 同时提供时应生成 2 个条件（AND 语义）。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "电子", "光刻机")
    assert len(conditions) == 2
    sql = _compile_conditions(conditions)
    # 两个条件都应出现在 SQL 中
    assert "%电子%" in sql
    # concept 是精确匹配（==），生成 = 而非 ILIKE
    assert "光刻机" in sql


def test_industry_only() -> None:
    """仅提供 industry 时生成 1 个条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "电子", None)
    assert len(conditions) == 1


def test_concept_only() -> None:
    """仅提供 concept 时生成 1 个条件。"""
    conditions = build_board_filter_conditions(_test_instrument_id, None, "光刻机")
    assert len(conditions) == 1


# ===== 6. concept 保持精确匹配 =====

def test_concept_uses_exact_match_not_ilike() -> None:
    """concept 应使用 == 精确匹配，不使用 ilike。"""
    conditions = build_board_filter_conditions(_test_instrument_id, None, "光刻机")
    sql = _compile_conditions(conditions)
    # concept 生成 = 而非 LOWER(...) LIKE LOWER(...)
    assert "LIKE" not in sql.upper()
    assert "光刻机" in sql


def test_concept_with_special_chars_not_escaped() -> None:
    """concept 精确匹配：特殊字符不转义（== 不需要 escape）。"""
    conditions = build_board_filter_conditions(_test_instrument_id, None, "50%off")
    sql = _compile_conditions(conditions)
    # 应直接出现在 = 子句中（参数化），不转义
    assert "50%off" in sql


# ===== 7. 不同关联列通用性 =====

def test_different_instrument_id_column() -> None:
    """build_board_filter_conditions 应支持任意列表达式。"""
    # 模拟 StrategyRunItem.instrument_id
    other_col = _test_table.c.name
    conditions = build_board_filter_conditions(other_col, "电子", None)
    assert len(conditions) == 1
    sql = _compile_conditions(conditions)
    assert "%电子%" in sql


# ===== 8. ilike escape 子句 =====

def test_ilike_uses_escape_clause() -> None:
    """ilike 应使用 escape='\\' 子句（PostgreSQL/SQLite 标准）。"""
    conditions = build_board_filter_conditions(_test_instrument_id, "50%", None)
    sql = _compile_conditions(conditions)
    # escape 子句应出现在 SQL 中
    assert "escape" in sql.lower() or "ESCAPE" in sql
