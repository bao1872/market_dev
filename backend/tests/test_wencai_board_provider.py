"""问财板块数据源测试（PRD §7.5 重构：pywencai 唯一数据源）。

验证项：
1. 规范化函数：股票代码、概念拆分、行业路径、external_code 生成
2. 哈希冲突检测
3. BoardSnapshot 构建（从 DataFrame）
4. 主表选择逻辑

注：真实问财拉取测试不进入 CI，只在部署后执行一次。
"""

from __future__ import annotations

import hashlib
import unicodedata

import pandas as pd
import pytest

from app.services.wencai_board_provider import (
    MAX_CONCEPTS_PER_STOCK,
    WencaiHashCollisionError,
    WencaiParseError,
    _build_board_snapshot,
    _detect_hash_collision,
    _make_external_code,
    _match_column,
    _normalize_concepts,
    _normalize_industry,
    _normalize_name,
    _normalize_stock_code,
    _select_primary_dataframe,
    get_provider_info,
)

# =============================================================================
# 1. 股票代码规范化
# =============================================================================


class TestNormalizeStockCode:
    """股票代码规范化测试。"""

    def test_sh_suffix(self) -> None:
        assert _normalize_stock_code("600000.SH") == "600000"

    def test_sz_suffix(self) -> None:
        assert _normalize_stock_code("000001.SZ") == "000001"

    def test_bj_suffix(self) -> None:
        assert _normalize_stock_code("688981.BJ") == "688981"

    def test_pure_six_digits(self) -> None:
        assert _normalize_stock_code("600000") == "600000"

    def test_preserve_leading_zeros(self) -> None:
        assert _normalize_stock_code("000001.SZ") == "000001"
        assert _normalize_stock_code("000001") == "000001"

    def test_none_returns_none(self) -> None:
        assert _normalize_stock_code(None) is None

    def test_empty_returns_none(self) -> None:
        assert _normalize_stock_code("") is None
        assert _normalize_stock_code("   ") is None

    def test_invalid_format_returns_none(self) -> None:
        assert _normalize_stock_code("ABC123") is None
        assert _normalize_stock_code("12345") is None
        assert _normalize_stock_code("1234567") is None

    def test_embedded_code(self) -> None:
        """代码可能嵌入在更长的字符串中。"""
        assert _normalize_stock_code("股票代码: 600000.SH") == "600000"


# =============================================================================
# 2. 名称规范化
# =============================================================================


class TestNormalizeName:
    """名称规范化测试（NFKC + trim）。"""

    def test_basic(self) -> None:
        assert _normalize_name("银行") == "银行"

    def test_trim(self) -> None:
        assert _normalize_name("  银行  ") == "银行"

    def test_nfkc_fullwidth_to_halfwidth(self) -> None:
        # 全角字母/数字 → 半角
        fullwidth = "ＡＢＣ１２３"
        expected = unicodedata.normalize("NFKC", fullwidth)
        assert _normalize_name(fullwidth) == expected

    def test_none_returns_empty(self) -> None:
        assert _normalize_name(None) == ""


# =============================================================================
# 3. 概念规范化
# =============================================================================


class TestNormalizeConcepts:
    """概念列表规范化测试。"""

    def test_single_concept(self) -> None:
        assert _normalize_concepts("人工智能") == ["人工智能"]

    def test_multiple_concepts(self) -> None:
        result = _normalize_concepts("人工智能;芯片;半导体")
        assert result == ["人工智能", "芯片", "半导体"]

    def test_dedup(self) -> None:
        result = _normalize_concepts("人工智能;芯片;人工智能")
        assert result == ["人工智能", "芯片"]

    def test_trim_parts(self) -> None:
        result = _normalize_concepts("  人工智能 ;  芯片  ")
        assert result == ["人工智能", "芯片"]

    def test_empty_string(self) -> None:
        assert _normalize_concepts("") == []

    def test_none(self) -> None:
        assert _normalize_concepts(None) == []

    def test_nfkc(self) -> None:
        result = _normalize_concepts("ＡＩ;芯片")
        expected = [unicodedata.normalize("NFKC", "ＡＩ"), "芯片"]
        assert result == expected


# =============================================================================
# 4. 行业规范化
# =============================================================================


class TestNormalizeIndustry:
    """行业路径规范化测试。"""

    def test_single_level(self) -> None:
        assert _normalize_industry("银行") == "银行"

    def test_two_levels_dash(self) -> None:
        assert _normalize_industry("金融-银行") == "金融-银行"

    def test_three_levels_dash(self) -> None:
        assert _normalize_industry("金融-银行-国有银行") == "金融-银行-国有银行"

    def test_slash_separator_normalized_to_dash(self) -> None:
        assert _normalize_industry("金融/银行/国有银行") == "金融-银行-国有银行"

    def test_trim_parts(self) -> None:
        assert _normalize_industry("  金融 -  银行  ") == "金融-银行"

    def test_empty_parts_removed(self) -> None:
        assert _normalize_industry("金融--银行") == "金融-银行"

    def test_empty_string(self) -> None:
        assert _normalize_industry("") == ""

    def test_none(self) -> None:
        assert _normalize_industry(None) == ""


# =============================================================================
# 5. external_code 生成
# =============================================================================


class TestMakeExternalCode:
    """external_code 生成测试。"""

    def test_concept_prefix(self) -> None:
        code = _make_external_code("concept", "人工智能")
        expected_hash = hashlib.sha256("人工智能".encode()).hexdigest()[:24]
        assert code == f"wc:c:{expected_hash}"

    def test_industry_prefix(self) -> None:
        code = _make_external_code("industry", "金融-银行")
        expected_hash = hashlib.sha256("金融-银行".encode()).hexdigest()[:24]
        assert code == f"wc:i:{expected_hash}"

    def test_stable(self) -> None:
        """相同输入产生相同 external_code。"""
        code1 = _make_external_code("concept", "芯片")
        code2 = _make_external_code("concept", "芯片")
        assert code1 == code2

    def test_different_names_different_codes(self) -> None:
        code1 = _make_external_code("concept", "人工智能")
        code2 = _make_external_code("concept", "芯片")
        assert code1 != code2

    def test_different_types_different_codes(self) -> None:
        """相同名称但不同类型产生不同 external_code。"""
        code1 = _make_external_code("concept", "银行")
        code2 = _make_external_code("industry", "银行")
        assert code1 != code2

    def test_hash_length_24(self) -> None:
        code = _make_external_code("concept", "test")
        hash_part = code.split(":")[2]
        assert len(hash_part) == 24


# =============================================================================
# 6. 哈希冲突检测
# =============================================================================


class TestHashCollision:
    """哈希冲突检测测试。"""

    def test_no_collision(self) -> None:
        name_to_code = {"A": "wc:c:aaa", "B": "wc:c:bbb"}
        code_to_names = {"wc:c:aaa": ["A"], "wc:c:bbb": ["B"]}
        # 不抛异常
        _detect_hash_collision(name_to_code, code_to_names)

    def test_collision_raises(self) -> None:
        name_to_code = {"A": "wc:c:xxx", "B": "wc:c:xxx"}
        code_to_names = {"wc:c:xxx": ["A", "B"]}
        with pytest.raises(WencaiHashCollisionError, match="哈希冲突"):
            _detect_hash_collision(name_to_code, code_to_names)


# =============================================================================
# 7. 列匹配
# =============================================================================


class TestMatchColumn:
    """列名匹配测试。"""

    def test_exact_match(self) -> None:
        assert _match_column(["股票代码", "股票简称"], ("股票代码",)) == "股票代码"

    def test_partial_match(self) -> None:
        assert _match_column(["股票代码[日期]", "股票简称"], ("股票代码",)) == "股票代码[日期]"

    def test_multiple_patterns(self) -> None:
        assert _match_column(["同花顺行业分类"], ("所属同花顺行业", "同花顺行业",)) == "同花顺行业分类"

    def test_no_match(self) -> None:
        assert _match_column(["价格", "市值"], ("股票代码",)) is None


# =============================================================================
# 8. BoardSnapshot 构建
# =============================================================================


def _make_test_dataframe(
    rows: int = 100,
    concepts_per_stock: int = 5,
) -> pd.DataFrame:
    """构造测试用 DataFrame（模拟问财返回格式）。"""
    data = []
    for i in range(rows):
        code = f"{600000 + i:06d}.SH"
        name = f"测试股{i}"
        concepts = ";".join(f"概念{j}" for j in range(concepts_per_stock))
        industry = f"金融-银行-子类{i % 3}"
        data.append({
            "股票代码": code,
            "股票简称": name,
            "所属概念": concepts,
            "所属同花顺行业": industry,
        })
    return pd.DataFrame(data)


class TestBuildBoardSnapshot:
    """BoardSnapshot 构建测试。"""

    def test_basic_snapshot(self) -> None:
        df = _make_test_dataframe(rows=100, concepts_per_stock=5)
        snapshot = _build_board_snapshot(df, pd)

        assert snapshot.raw_rows == 100
        assert snapshot.board_count > 0
        assert snapshot.membership_count > 0

    def test_concepts_split(self) -> None:
        df = _make_test_dataframe(rows=10, concepts_per_stock=3)
        snapshot = _build_board_snapshot(df, pd)

        # 10 行 × 3 概念 = 30 概念关系
        concept_memberships = sum(
            len(v) for k, v in snapshot.memberships.items() if k[1] == "concept"
        )
        assert concept_memberships == 30

    def test_industry_one_per_stock(self) -> None:
        df = _make_test_dataframe(rows=50, concepts_per_stock=2)
        snapshot = _build_board_snapshot(df, pd)

        # 每股恰好一个行业 → 行业关系数 = 股票数
        industry_memberships = sum(
            len(v) for k, v in snapshot.memberships.items() if k[1] == "industry"
        )
        assert industry_memberships == 50

    def test_concepts_deduped_per_stock(self) -> None:
        """同一股票的重复概念去重。"""
        df = pd.DataFrame([{
            "股票代码": "600000.SH",
            "股票简称": "测试",
            "所属概念": "AI;AI;芯片;芯片",
            "所属同花顺行业": "科技-软件",
        }])
        snapshot = _build_board_snapshot(df, pd)

        for k, v in snapshot.memberships.items():
            if k[1] == "concept":
                # 每个概念关系列表中 600000 只出现一次
                assert v.count("600000") == 1

    def test_concepts_over_limit_truncated(self) -> None:
        """超过 MAX_CONCEPTS_PER_STOCK 的概念截断。"""
        too_many = ";".join(f"概念{i}" for i in range(MAX_CONCEPTS_PER_STOCK + 10))
        df = pd.DataFrame([{
            "股票代码": "600000.SH",
            "股票简称": "测试",
            "所属概念": too_many,
            "所属同花顺行业": "科技-软件",
        }])
        snapshot = _build_board_snapshot(df, pd)

        # 该股票的概念关系数应 ≤ MAX_CONCEPTS_PER_STOCK
        stock_concept_count = sum(
            1 for k, v in snapshot.memberships.items()
            if k[1] == "concept" and "600000" in v
        )
        assert stock_concept_count <= MAX_CONCEPTS_PER_STOCK

    def test_unresolved_symbols_recorded(self) -> None:
        """无效股票代码记录到 unresolved_symbols。"""
        df = pd.DataFrame([
            {"股票代码": "INVALID", "股票简称": "无效", "所属概念": "概念", "所属同花顺行业": "行业"},
            {"股票代码": "600000.SH", "股票简称": "有效", "所属概念": "概念", "所属同花顺行业": "行业"},
        ])
        snapshot = _build_board_snapshot(df, pd)

        assert len(snapshot.unresolved_symbols) == 1
        # 脱敏：截断到20字符
        assert len(snapshot.unresolved_symbols[0]) <= 20

    def test_missing_required_field_raises(self) -> None:
        """缺少必需字段抛 WencaiParseError。"""
        df = pd.DataFrame([{
            "股票代码": "600000.SH",
            "股票简称": "测试",
            # 缺少 所属概念 和 所属同花顺行业
        }])
        with pytest.raises(WencaiParseError, match="缺少必需字段"):
            _build_board_snapshot(df, pd)

    def test_boards_deduplicated(self) -> None:
        """相同概念/行业在不同股票中出现时只创建一个 board。"""
        df = pd.DataFrame([
            {"股票代码": "600000.SH", "股票简称": "A", "所属概念": "AI;芯片", "所属同花顺行业": "科技-软件"},
            {"股票代码": "600001.SH", "股票简称": "B", "所属概念": "AI;半导体", "所属同花顺行业": "科技-软件"},
        ])
        snapshot = _build_board_snapshot(df, pd)

        # 概念：AI, 芯片, 半导体 = 3 个
        concept_boards = [b for b in snapshot.boards if b["type"] == "concept"]
        assert len(concept_boards) == 3

        # 行业：科技-软件 = 1 个（两股相同行业）
        industry_boards = [b for b in snapshot.boards if b["type"] == "industry"]
        assert len(industry_boards) == 1


# =============================================================================
# 9. 主表选择
# =============================================================================


class TestSelectPrimaryDataframe:
    """主表选择逻辑测试。"""

    def test_single_dataframe(self) -> None:
        df = _make_test_dataframe(rows=50)
        result = _select_primary_dataframe(df, pd)
        assert len(result) == 50

    def test_select_largest_with_required_fields(self) -> None:
        """从多个 DataFrame 中选择包含必需字段且行数最大的。"""
        df_large = _make_test_dataframe(rows=100)
        df_small = _make_test_dataframe(rows=10)
        result = _select_primary_dataframe({"table1": df_large, "table2": df_small}, pd)
        assert len(result) == 100

    def test_no_dataframe_raises(self) -> None:
        with pytest.raises(WencaiParseError, match="未返回可保存的表格数据"):
            _select_primary_dataframe({"data": [1, 2, 3]}, pd)

    def test_nested_structure(self) -> None:
        """嵌套 dict/list 结构中提取 DataFrame。"""
        df = _make_test_dataframe(rows=30)
        result = _select_primary_dataframe(
            {"result": {"data": [df, {"sub": "value"}]}},
            pd,
        )
        assert len(result) == 30


# =============================================================================
# 10. provider 元信息
# =============================================================================


class TestGetProviderInfo:
    """provider 元信息测试。"""

    def test_provider_info(self) -> None:
        info = get_provider_info()
        assert info["source"] == "wencai"
        assert "query" in info
        assert "max_retries" in info
        assert info["max_retries"] == 3
