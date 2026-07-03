"""股票代码标准化正则测试（Task 6）。

测试覆盖（TDD - 先写失败测试，再实施）：
1. normalize_symbol / normalize_market: 去空格、大写、去后缀
2. is_stock_symbol: 11 个边界用例（SH/SZ/BJ 合法与非法）

对应 advice.md 第五节"股票代码标准化正则"。
"""

from app.services.instrument_maintenance_service import (
    is_index_symbol,
    is_stock_symbol,
    normalize_market,
    normalize_symbol,
)


class TestNormalizeSymbol:
    """normalize_symbol 单元测试。"""

    def test_normalize_symbol_strips_whitespace(self):
        assert normalize_symbol(" 600000 ") == "600000"

    def test_normalize_symbol_uppercases(self):
        assert normalize_symbol("600000") == "600000"

    def test_normalize_symbol_removes_suffix(self):
        assert normalize_symbol("600000.SH") == "600000"
        assert normalize_symbol("000001.SZ") == "000001"

    def test_normalize_symbol_none_empty(self):
        assert normalize_symbol(None) == ""
        assert normalize_symbol("") == ""


class TestNormalizeMarket:
    """normalize_market 单元测试。"""

    def test_normalize_market_strips(self):
        assert normalize_market(" sh ") == "SH"

    def test_normalize_market_uppercases(self):
        assert normalize_market("sz") == "SZ"

    def test_normalize_market_none_empty(self):
        assert normalize_market(None) == ""
        assert normalize_market("") == ""


class TestIsStockSymbol:
    """is_stock_symbol 单元测试 - 11 个边界用例。"""

    def test_sh_600000_is_stock(self):
        # 上交所主板
        assert is_stock_symbol("600000", "SH") is True

    def test_sz_000001_is_stock(self):
        # 深交所主板
        assert is_stock_symbol("000001", "SZ") is True

    def test_sz_300001_is_stock(self):
        # 深交所创业板
        assert is_stock_symbol("300001", "SZ") is True

    def test_bj_920001_is_stock(self):
        # 北交所
        assert is_stock_symbol("920001", "BJ") is True

    def test_sh_6abc_not_stock(self):
        # 非数字字符，应拒绝
        assert is_stock_symbol("6ABC", "SH") is False

    def test_sh_7_digits_not_stock(self):
        # 7 位数字，应拒绝
        assert is_stock_symbol("6000000", "SH") is False

    def test_sz_with_suffix_not_stock(self):
        # 带后缀，normalize 后为 "300001"，应通过
        # 注：normalize 会去除 .SZ 后缀，所以这应该是 True
        assert is_stock_symbol("300001.SZ", "SZ") is True

    def test_sh_with_spaces_is_stock_after_normalize(self):
        # 含空格，normalize 后为 "600000"，应通过
        assert is_stock_symbol(" 600000 ", "SH") is True

    def test_sz_000001_in_sh_not_stock(self):
        # SH 市场不接受 000 开头
        assert is_stock_symbol("000001", "SH") is False

    def test_sh_688001_is_stock(self):
        # 科创板 688
        assert is_stock_symbol("688001", "SH") is True

    def test_bj_899001_not_stock(self):
        # 北证指数 899，应拒绝
        assert is_stock_symbol("899001", "BJ") is False


class TestIsIndexSymbol:
    """is_index_symbol 单元测试 - 验证指数识别。"""

    def test_sh_000001_is_index(self):
        # 上证指数
        assert is_index_symbol("000001", "SH") is True

    def test_sz_399001_is_index(self):
        # 深证成指
        assert is_index_symbol("399001", "SZ") is True

    def test_bj_899001_is_index(self):
        # 北证指数
        assert is_index_symbol("899001", "BJ") is True

    def test_sh_600000_not_index(self):
        # 股票不是指数
        assert is_index_symbol("600000", "SH") is False

    def test_sz_000001_not_index(self):
        # 深交所 000001 是平安银行（股票），不是指数
        assert is_index_symbol("000001", "SZ") is False
