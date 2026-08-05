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
import time
import unicodedata
from pickle import PicklingError
from queue import Empty

import pandas as pd
import pytest

from app.services import wencai_board_provider as wencai_provider
from app.services.wencai_board_provider import (
    MAX_CONCEPTS_PER_STOCK,
    WencaiConceptLimitError,
    WencaiFetchError,
    WencaiHashCollisionError,
    WencaiParseError,
    _build_board_snapshot,
    _detect_hash_collision,
    _df_content_hash,
    _fetch_with_terminable_subprocess,
    _make_external_code,
    _match_column,
    _normalize_concepts,
    _normalize_industry,
    _normalize_name,
    _normalize_stock_code,
    _select_primary_dataframe,
    _subprocess_fetch_worker,
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

        # [Commit A §6.2] 行业拆分为 L1/L2/L3 层级，每股挂到每一级。
        # 示例行业 "金融-银行-子类{i%3}" 为 3 级 → 每股 3 条行业关系。
        industry_memberships = sum(
            len(v) for k, v in snapshot.memberships.items() if k[1] == "industry"
        )
        assert industry_memberships == 50 * 3

    def test_industry_hierarchy_levels(self) -> None:
        """行业拆分为 L1/L2/L3，三级的 parent 关系正确。"""
        df = _make_test_dataframe(rows=10, concepts_per_stock=1)
        snapshot = _build_board_snapshot(df, pd)

        industry_boards = [b for b in snapshot.boards if b["type"] == "industry"]
        # 每个行业路径 3 级 → 10 行全部同一路径 "金融-银行-子类N" 家族
        # 只产生一组 L1/L2/L3（金融 / 金融-银行 / 金融-银行-子类x）
        levels = {b["hierarchy_level"] for b in industry_boards}
        assert levels == {"L1", "L2", "L3"}

        # L2 的 parent 是 L1，L3 的 parent 是 L2
        by_level: dict[str, dict] = {b["hierarchy_level"]: b for b in industry_boards}
        l1, l2, l3 = by_level["L1"], by_level["L2"], by_level["L3"]
        assert l2["parent_external_code"] == l1["external_code"]
        assert l3["parent_external_code"] == l2["external_code"]
        # L1 无父级（省略 key，等价语义为 None）
        assert l1.get("parent_external_code") is None

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
        """[PRD Alignment Pass P0-3] 超过 MAX_CONCEPTS_PER_STOCK 的概念禁止静默截断，必须门禁失败。"""
        too_many = ";".join(f"概念{i}" for i in range(MAX_CONCEPTS_PER_STOCK + 10))
        df = pd.DataFrame([{
            "股票代码": "600000.SH",
            "股票简称": "测试",
            "所属概念": too_many,
            "所属同花顺行业": "科技-软件",
        }])
        # 静默截断已移除：超限必须抛 WencaiConceptLimitError
        with pytest.raises(WencaiConceptLimitError):
            _build_board_snapshot(df, pd)

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

        # 行业：科技-软件（2 级）→ L1"科技" + L2"科技-软件" = 2 个（两股相同路径，去重）
        industry_boards = [b for b in snapshot.boards if b["type"] == "industry"]
        assert len(industry_boards) == 2


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

    def test_no_qualified_table_raises(self) -> None:
        """所有表都缺必需字段时禁止静默降级，直接失败。"""
        df_bad = pd.DataFrame([{"股票代码": "600000.SH", "股票简称": "测试"}])
        df_bad2 = pd.DataFrame([{"价格": 1.0, "市值": 100}])
        with pytest.raises(WencaiParseError, match="缺少必需字段.*静默降级"):
            _select_primary_dataframe({"t1": df_bad, "t2": df_bad2}, pd)

    def test_equal_size_qualified_hash_conflict_raises(self) -> None:
        """多张同等合格且行数最大的表内容 hash 冲突 → 失败。"""
        df_a = _make_test_dataframe(rows=10, concepts_per_stock=2)
        df_b = _make_test_dataframe(rows=10, concepts_per_stock=3)  # 内容不同
        with pytest.raises(WencaiHashCollisionError, match="hash 冲突"):
            _select_primary_dataframe({"t1": df_a, "t2": df_b}, pd)

    def test_equal_size_identical_tables_ok(self) -> None:
        """多张同等合格但内容相同（同一 DataFrame 重复引用）→ 不冲突，选其一。"""
        df = _make_test_dataframe(rows=10)
        result = _select_primary_dataframe({"t1": df, "t2": df}, pd)
        assert len(result) == 10

    def test_content_hash_row_order_invariant(self) -> None:
        """同行不同顺序 hash 相同（_df_content_hash 对规范化行排序）。

        [Commit A 修正 2026-08-05] 同一批数据即使行序不同，内容 hash 必须一致，
        否则多表哈希冲突检测会误判"内容不同"。
        """
        df = _make_test_dataframe(rows=20, concepts_per_stock=3)
        reordered = df.iloc[::-1].reset_index(drop=True)  # 反转行序
        assert not reordered.equals(df)  # 行序确实不同，测试才有意义
        assert _df_content_hash(df) == _df_content_hash(reordered)

    def test_content_hash_different_content_differs(self) -> None:
        """内容确实不同时 hash 必须不同（防止行序无关退化为恒等）。"""
        df_a = _make_test_dataframe(rows=10, concepts_per_stock=2)
        df_b = _make_test_dataframe(rows=10, concepts_per_stock=3)
        assert _df_content_hash(df_a) != _df_content_hash(df_b)


# =============================================================================
# 9.1 _df_content_hash 行关系语义（整行 tuple 排序）
# =============================================================================


class TestDfContentHashRowRelationships:
    """_df_content_hash 必须保留整行字段关系，禁止拆散列值排序。"""

    @staticmethod
    def _swap_df() -> tuple[pd.DataFrame, pd.DataFrame]:
        """构造两批行序相同、但股票↔概念/行业对应关系互换的 DataFrame。"""
        data1 = pd.DataFrame([
            {"股票代码": "600000.SH", "股票简称": "测试1", "所属概念": "概念A", "所属同花顺行业": "行业X"},
            {"股票代码": "600001.SH", "股票简称": "测试2", "所属概念": "概念B", "所属同花顺行业": "行业Y"},
        ])
        # 行序不变，仅把 600000/600001 与 概念A/概念B 的对应关系对调
        data2 = pd.DataFrame([
            {"股票代码": "600001.SH", "股票简称": "测试2", "所属概念": "概念B", "所属同花顺行业": "行业X"},
            {"股票代码": "600000.SH", "股票简称": "测试1", "所属概念": "概念A", "所属同花顺行业": "行业Y"},
        ])
        return data1, data2

    def test_same_content_different_row_order_same_hash(self) -> None:
        """同内容不同行序 hash 相同（整行 tuple 排序）。"""
        df = _make_test_dataframe(rows=20, concepts_per_stock=3)
        reordered = df.iloc[::-1].reset_index(drop=True)
        assert not reordered.equals(df)
        assert _df_content_hash(df) == _df_content_hash(reordered)

    def test_relationship_swap_changes_hash(self) -> None:
        """股票与行业/概念对应关系互换时 hash 必须不同。"""
        data1, data2 = self._swap_df()
        # 两份数据行序相同、单元格值集合相同，仅行内对应关系不同
        assert data1.equals(data1) and data2.equals(data2)
        assert _df_content_hash(data1) != _df_content_hash(data2)

    def test_single_field_change_changes_hash(self) -> None:
        """某字段变化 hash 必须不同。"""
        df_a = pd.DataFrame([
            {"股票代码": "600000.SH", "股票简称": "测试1", "所属概念": "概念A", "所属同花顺行业": "行业X"},
        ])
        df_b = df_a.copy()
        df_b.loc[0, "所属概念"] = "概念B"  # 仅改一个单元格
        assert _df_content_hash(df_a) != _df_content_hash(df_b)

    def test_duplicate_rows_semantics(self) -> None:
        """重复行语义明确：重复行数量变化必须改变 hash。

        相同股票/概念/行业出现两次（重复行）与出现一次，语义不同，hash 必须不同。
        """
        df_single = pd.DataFrame([
            {"股票代码": "600000.SH", "股票简称": "测试1", "所属概念": "概念A", "所属同花顺行业": "行业X"},
        ])
        df_dup = pd.DataFrame([
            {"股票代码": "600000.SH", "股票简称": "测试1", "所属概念": "概念A", "所属同花顺行业": "行业X"},
            {"股票代码": "600000.SH", "股票简称": "测试1", "所属概念": "概念A", "所属同花顺行业": "行业X"},
        ])
        assert _df_content_hash(df_single) != _df_content_hash(df_dup)


# =============================================================================
# 9.2 pywencai 可终止子进程（生命周期加固）
# =============================================================================


class _FakeProc:
    """模拟 multiprocessing.Process，可编程存活/退出/terminate/kill。"""

    def __init__(self, is_alive: bool = True, exitcode: int | None = None,
                 stubborn: bool = False) -> None:
        self._alive = is_alive
        self.exitcode = exitcode
        self.pid = 1234
        # stubborn=True 模拟 terminate 无效（进程仍存活），必须 kill
        self.stubborn = stubborn
        self.terminated = 0
        self.killed = 0
        self.joined: list[float | None] = []

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminated += 1
        if not self.stubborn:
            self._alive = False

    def kill(self) -> None:
        self.killed += 1
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        self.joined.append(timeout)


class _FakeQueue:
    """模拟 multiprocessing.Queue，可编程 get_nowait/put/close/join_thread。"""

    def __init__(self, get_nowait_results: list | None = None,
                 put_ok_raises: Exception | None = None) -> None:
        self._get_results = list(get_nowait_results or [])
        self.put_items: list = []
        self.put_ok_raises = put_ok_raises
        self.closed = False
        self.joined = False

    def get_nowait(self):
        if not self._get_results:
            raise Empty
        return self._get_results.pop(0)

    def put(self, item) -> None:
        # 模拟"结果不可 pickle"：ok 结果 put 抛异常，error 结果可正常 put
        if self.put_ok_raises is not None and item[0] == "ok":
            raise self.put_ok_raises
        self.put_items.append(item)

    def close(self) -> None:
        self.closed = True

    def join_thread(self) -> None:
        self.joined = True


class _FakeCtx:
    """模拟 multiprocessing 上下文，注入假 Queue/Process。"""

    def __init__(self, queue: _FakeQueue, proc: _FakeProc) -> None:
        self._queue = queue
        self._proc = proc

    def Queue(self) -> _FakeQueue:  # noqa: N802 - 镜像 multiprocessing 上下文 API
        return self._queue

    def Process(self, target, args, daemon) -> _FakeProc:  # noqa: N802 - 镜像 multiprocessing 上下文 API
        return self._proc


class TestSubprocessWorker:
    """_subprocess_fetch_worker 结果/错误/不可 pickle 回传。"""

    def test_worker_ok(self, monkeypatch) -> None:
        q = _FakeQueue()
        monkeypatch.setattr(wencai_provider, "_fetch_wencai_sync", lambda: {"data": 1})
        _subprocess_fetch_worker(q)
        assert q.put_items == [("ok", {"data": 1})]

    def test_worker_error(self, monkeypatch) -> None:
        q = _FakeQueue()

        def boom() -> None:
            raise ValueError("boom")

        monkeypatch.setattr(wencai_provider, "_fetch_wencai_sync", boom)
        _subprocess_fetch_worker(q)
        assert q.put_items[0][0] == "error"
        assert q.put_items[0][1] == "ValueError"
        assert "boom" in q.put_items[0][2]

    def test_worker_unpicklable_result(self, monkeypatch) -> None:
        """结果不可 pickle 时回传结构化错误，而非让父进程只能靠超时兜底。"""
        q = _FakeQueue(put_ok_raises=PicklingError("can't pickle"))
        monkeypatch.setattr(wencai_provider, "_fetch_wencai_sync", lambda: object())
        _subprocess_fetch_worker(q)
        assert q.put_items[0][0] == "error"
        assert q.put_items[0][1] == "PicklingError"


class TestFetchTerminableSubprocess:
    """_fetch_with_terminable_subprocess 轮询/超时/异常退出/资源释放。"""

    @staticmethod
    def _freeze_time(monkeypatch) -> None:
        monkeypatch.setattr(time, "monotonic", lambda: 0.0)

    @pytest.mark.asyncio
    async def test_ok(self, monkeypatch) -> None:
        q = _FakeQueue(get_nowait_results=[("ok", {"data": 1})])
        proc = _FakeProc(is_alive=True)
        monkeypatch.setattr(wencai_provider, "PROVIDER_TIMEOUT_SECONDS", 100)
        self._freeze_time(monkeypatch)
        result = await _fetch_with_terminable_subprocess(_FakeCtx(q, proc))
        assert result == {"data": 1}
        # 队列资源释放：close + join_thread
        assert q.closed is True
        assert q.joined is True

    @pytest.mark.asyncio
    async def test_error_item(self, monkeypatch) -> None:
        q = _FakeQueue(get_nowait_results=[("error", "ValueError", "bad")])
        proc = _FakeProc(is_alive=True)
        monkeypatch.setattr(wencai_provider, "PROVIDER_TIMEOUT_SECONDS", 100)
        self._freeze_time(monkeypatch)
        with pytest.raises(WencaiFetchError, match="问财拉取失败"):
            await _fetch_with_terminable_subprocess(_FakeCtx(q, proc))

    @pytest.mark.asyncio
    async def test_timeout_terminates_and_kills(self, monkeypatch) -> None:
        """超时：terminate 无效（仍存活）时必须 kill 兜底。"""
        q = _FakeQueue(get_nowait_results=[])  # 永远 Empty
        proc = _FakeProc(is_alive=True, stubborn=True)  # terminate 无效 → kill
        monkeypatch.setattr(wencai_provider, "PROVIDER_TIMEOUT_SECONDS", 0.0)
        self._freeze_time(monkeypatch)
        with pytest.raises(WencaiFetchError, match="超时"):
            await _fetch_with_terminable_subprocess(_FakeCtx(q, proc))
        assert proc.terminated >= 1
        assert proc.killed >= 1

    @pytest.mark.asyncio
    async def test_abnormal_exit_detected(self, monkeypatch) -> None:
        """子进程已退出但队列仍空 → 识别为异常终止。"""
        q = _FakeQueue(get_nowait_results=[])
        proc = _FakeProc(is_alive=False, exitcode=1)
        monkeypatch.setattr(wencai_provider, "PROVIDER_TIMEOUT_SECONDS", 100)
        self._freeze_time(monkeypatch)
        with pytest.raises(WencaiFetchError, match="异常退出"):
            await _fetch_with_terminable_subprocess(_FakeCtx(q, proc))


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
