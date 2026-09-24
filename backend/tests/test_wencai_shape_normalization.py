"""[WENCAI-STREAM-QUERY-SHAPE-CORRECTION-01] 新协议数组形态归一化测试。

背景（真实缺陷）：stream-query 以 `list[str]` 返回
`所属概念` / `所属同花顺行业`；旧归一化按字符串处理，`str(list)` 会把
`['A','B']` 当成**一个**业务名 → 实测产生 5578 股 / 5560 个虚假"概念"，
`membership_count = 11156 = 5578×2`。

本文件用**新协议的真实值形态**锁定：
- list 元素即独立概念 / 有序行业层级
- 旧字符串形态保持兼容
- dict / 嵌套 / 数字等一律 fail closed（禁止静默接受）
- 端到端：概念与 L1/L2/L3 行业父子链正确，且不出现 Python 列表字符串
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.wencai_board_provider import (
    MAX_CONCEPTS_PER_STOCK,
    WencaiConceptLimitError,
    WencaiParseError,
    _build_board_snapshot,
    _make_external_code,
    _normalize_concepts,
    _normalize_industry,
)


# =============================================================================
# 概念归一化
# =============================================================================


class TestNormalizeConceptsNewShape:
    def test_list_shape_dedupes_stably(self) -> None:
        assert _normalize_concepts(["机器人", "人工智能", "机器人"]) == ["机器人", "人工智能"]

    def test_list_shape_single_element(self) -> None:
        assert _normalize_concepts(["人工智能"]) == ["人工智能"]

    def test_list_shape_empty(self) -> None:
        assert _normalize_concepts([]) == []

    def test_list_shape_trims_and_nfkc(self) -> None:
        assert _normalize_concepts([" 人工智能 ", "ＡＩ"]) == ["人工智能", "AI"]

    def test_legacy_string_shape_preserved(self) -> None:
        assert _normalize_concepts("机器人;人工智能") == ["机器人", "人工智能"]

    def test_legacy_empty_string(self) -> None:
        assert _normalize_concepts("") == []
        assert _normalize_concepts(None) == []

    @pytest.mark.parametrize(
        "bad",
        [
            ["人工智能", {"nested": "dict"}],
            ["人工智能", ["nested", "list"]],
            ["人工智能", 123],
            {"key": "value"},
            42,
        ],
    )
    def test_unexpected_shape_fails_closed(self, bad: object) -> None:
        """禁止 str(list) 降级；容器/非字符串元素一律 WencaiParseError。"""
        with pytest.raises(WencaiParseError):
            _normalize_concepts(bad)

    def test_none_element_fails_closed(self) -> None:
        """[RC1] 合同是 list[str]，不是 list[str | None] → 元素 None 必须 fail closed。

        live 证据中 None 元素为 0；未观察到就不应扩大输入合同。
        """
        with pytest.raises(WencaiParseError):
            _normalize_concepts(["人工智能", None])

    def test_empty_string_element_still_ignored(self) -> None:
        """空字符串元素保持忽略（不影响其它元素语义）。"""
        assert _normalize_concepts(["人工智能", "", "机器人"]) == ["人工智能", "机器人"]

    def test_top_level_none_preserved_as_empty(self) -> None:
        """整个字段缺失/旧协议空值：仍返回 []（兼容性行为保留）。"""
        assert _normalize_concepts(None) == []

    def test_no_stringified_list_leaks(self) -> None:
        """核心防回归：结果绝不能是列表的文本表示。"""
        result = _normalize_concepts(["机器人", "人工智能"])
        for name in result:
            assert "['" not in name and "']" not in name
            assert not name.startswith("[")
        assert len(result) == 2


# =============================================================================
# 行业归一化
# =============================================================================


class TestNormalizeIndustryNewShape:
    def test_ordered_hierarchy_list(self) -> None:
        assert (
            _normalize_industry(["电子", "半导体", "集成电路"])
            == "电子-半导体-集成电路"
        )

    def test_list_single_level(self) -> None:
        assert _normalize_industry(["银行"]) == "银行"

    def test_legacy_dash_string_unchanged(self) -> None:
        assert _normalize_industry("电子-半导体-集成电路") == "电子-半导体-集成电路"

    def test_legacy_slash_string(self) -> None:
        assert _normalize_industry("电子/半导体") == "电子-半导体"

    def test_empty_inputs(self) -> None:
        assert _normalize_industry("") == ""
        assert _normalize_industry(None) == ""

    @pytest.mark.parametrize(
        "bad",
        [
            ["电子", {"nested": "x"}],
            ["电子", ["nested"]],
            ["电子", 7],
            {"a": 1},
            3,
        ],
    )
    def test_unexpected_shape_fails_closed(self, bad: object) -> None:
        with pytest.raises(WencaiParseError):
            _normalize_industry(bad)

    def test_empty_level_fails_closed(self) -> None:
        """空层级会让层级顺序产生歧义 → 禁止忽略后继续。"""
        with pytest.raises(WencaiParseError):
            _normalize_industry(["电子", "", "集成电路"])

    def test_industry_depth_limit_still_enforced(self) -> None:
        """深度上限仍由 _split_industry_path 的 WencaiIndustryDepthError 把关。"""
        from app.services.wencai_board_provider import WencaiIndustryDepthError

        with pytest.raises(WencaiIndustryDepthError):
            _build_board_snapshot(
                pd.DataFrame([{
                    "股票代码": "600000.SH",
                    "股票简称": "测试",
                    "所属概念": ["人工智能"],
                    "所属同花顺行业": ["一", "二", "三", "四"],
                }]),
                pd,
            )


# =============================================================================
# 端到端：真实新协议行形态
# =============================================================================


def _rows_stream_query_shape() -> pd.DataFrame:
    """与 live stream-query 一致的形态（两个字段均为 list[str]）。"""
    return pd.DataFrame([
        {
            "股票代码": "600000.SH",
            "股票简称": "测试A",
            "所属概念": ["机器人", "人工智能"],
            "所属同花顺行业": ["电子", "半导体", "集成电路"],
        },
        {
            "股票代码": "600001.SH",
            "股票简称": "测试B",
            "所属概念": ["机器人", "算力"],
            "所属同花顺行业": ["电子", "半导体", "分立器件"],
        },
        {
            "股票代码": "600002.SH",
            "股票简称": "测试C",
            "所属概念": ["人工智能"],
            "所属同花顺行业": ["银行", "国有银行", "国有大型银行"],
        },
    ])


class TestEndToEndStreamQueryShape:
    def test_concepts_become_separate_boards(self) -> None:
        snap = _build_board_snapshot(_rows_stream_query_shape(), pd)
        concept_names = {b["name"] for b in snap.boards if b["type"] == "concept"}
        assert concept_names == {"机器人", "人工智能", "算力"}

    def test_concept_memberships_are_separate(self) -> None:
        snap = _build_board_snapshot(_rows_stream_query_shape(), pd)
        concept_relations = sum(
            len(v) for k, v in snap.memberships.items() if k[1] == "concept"
        )
        # 2 + 2 + 1 = 5 个独立概念成员关系（不是 3 个"整串概念"）
        assert concept_relations == 5
        assert snap.memberships[(_make_external_code("concept", "机器人"), "concept")] == [
            "600000", "600001",
        ]

    def test_industry_hierarchy_boards_and_parents(self) -> None:
        snap = _build_board_snapshot(_rows_stream_query_shape(), pd)
        by_name = {b["name"]: b for b in snap.boards if b["type"] == "industry"}

        assert set(by_name) == {
            "电子", "电子-半导体", "电子-半导体-集成电路", "电子-半导体-分立器件",
            "银行", "银行-国有银行", "银行-国有银行-国有大型银行",
        }
        l1 = by_name["电子"]["external_code"]
        l2 = by_name["电子-半导体"]["external_code"]
        assert by_name["电子-半导体"]["parent_external_code"] == l1
        assert by_name["电子-半导体-集成电路"]["parent_external_code"] == l2
        assert by_name["电子-半导体-分立器件"]["parent_external_code"] == l2
        assert by_name["电子-半导体"]["hierarchy_level"] == "L2"
        assert by_name["电子-半导体-集成电路"]["hierarchy_level"] == "L3"
        assert by_name["银行"]["hierarchy_level"] == "L1"
        assert "parent_external_code" not in by_name["电子"]

    def test_stock_is_member_of_every_industry_level(self) -> None:
        snap = _build_board_snapshot(_rows_stream_query_shape(), pd)
        for level in ("电子", "电子-半导体", "电子-半导体-集成电路"):
            code = _make_external_code("industry", level)
            assert "600000" in snap.memberships[(code, "industry")], level

    def test_no_python_list_representation_in_board_names(self) -> None:
        """防回归断言：板块名不得出现 Python 列表文本。"""
        snap = _build_board_snapshot(_rows_stream_query_shape(), pd)
        for board in snap.boards:
            name = board["name"]
            assert not name.startswith("[")
            assert "['" not in name
            assert "']" not in name
            assert '", "' not in name

    def test_concept_limit_still_enforced_with_list_shape(self) -> None:
        rows = [{
            "股票代码": "600000.SH",
            "股票简称": "测试",
            "所属概念": [f"概念{i}" for i in range(MAX_CONCEPTS_PER_STOCK + 1)],
            "所属同花顺行业": ["电子", "半导体", "集成电路"],
        }]
        with pytest.raises(WencaiConceptLimitError):
            _build_board_snapshot(pd.DataFrame(rows), pd)
