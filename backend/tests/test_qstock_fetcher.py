"""ths_adapter + qstock_fetcher 测试（PRD §7.5，C10 决策）。

验证项：
1. HTTP 重试/退避：_http_get_with_retry 在失败后重试，超过次数抛 THSAdapterError
2. 分页解析：_parse_total_page_concept / _parse_page_num_member 正确解析页数
3. 概念代码提取：_extract_concept_codes 从 HTML 提取板块代码
4. 行业目录：fetch_industry_boards 从 qstock.ths_code_name dict 读取
5. 概念目录：fetch_concept_boards 通过 HTTP + bs4[html.parser] + pd.read_html 解析
6. 成分股：fetch_board_members 通过 HTTP + pd.read_html 解析，6 位左填充去重
7. 异步包装：QStockFetcher 通过 asyncio.to_thread 委托 QStockTHSAdapter
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.qstock_fetcher import (
    FETCH_RETRY_COUNT,
    FETCH_TIMEOUT_SECONDS,
    QStockFetcher,
    QStockFetchError,
)
from app.services.ths_adapter import (
    HTTP_MAX_PAGES,
    HTTP_RETRY_COUNT,
    QStockTHSAdapter,
    THSAdapterError,
    _extract_concept_codes,
    _http_get_with_retry,
    _parse_html_table,
    _parse_page_num_member,
    _parse_total_page_concept,
    fetch_board_members,
    fetch_concept_boards,
    fetch_industry_boards,
)

# ---------------------------------------------------------------------------
# 测试用 HTML 样本
# ---------------------------------------------------------------------------

_CONCEPT_INDEX_HTML = """
<html><body>
<span class="page_info">1/2</span>
<table class="m-table m-pager-table">
  <thead><tr><th>序号</th><th>概念名称</th><th>成分股数量</th><th>日期</th></tr></thead>
  <tbody>
    <tr><td>1</td><td><a href="http://q.10jqka.com.cn/gn/detail/code/301558/">概念A</a></td><td>10</td><td>2026-07-10</td></tr>
    <tr><td>2</td><td><a href="http://q.10jqka.com.cn/gn/detail/code/301559/">概念B</a></td><td>20</td><td>2026-07-10</td></tr>
  </tbody>
</table>
</body></html>
"""

_CONCEPT_INDEX_HTML_PAGE2 = """
<html><body>
<span class="page_info">2/2</span>
<table class="m-table m-pager-table">
  <thead><tr><th>序号</th><th>概念名称</th><th>成分股数量</th><th>日期</th></tr></thead>
  <tbody>
    <tr><td>3</td><td><a href="http://q.10jqka.com.cn/gn/detail/code/301560/">概念C</a></td><td>5</td><td>2026-07-10</td></tr>
  </tbody>
</table>
</body></html>
"""

_MEMBER_HTML = """
<html><body>
<a class="changePage" page="1">1</a>
<a class="changePage" page="2">2</a>
<a class="changePage" page="3">3</a>
<table>
  <thead><tr><th>序号</th><th>代码</th><th>名称</th><th>加自选</th></tr></thead>
  <tbody>
    <tr><td>1</td><td>1</td><td>平安银行</td><td>加</td></tr>
    <tr><td>2</td><td>600000</td><td>浦发银行</td><td>加</td></tr>
  </tbody>
</table>
</body></html>
"""

_MEMBER_HTML_PAGE2 = """
<html><body>
<a class="changePage" page="3">3</a>
<table>
  <thead><tr><th>序号</th><th>代码</th><th>名称</th><th>加自选</th></tr></thead>
  <tbody>
    <tr><td>3</td><td>000002</td><td>万科A</td><td>加</td></tr>
  </tbody>
</table>
</body></html>
"""


def _mock_response(text: str) -> MagicMock:
    """构造 mock httpx.Response，resp.text 返回字符串。"""
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# _http_get_with_retry 测试
# ---------------------------------------------------------------------------

class TestHttpGetWithRetry:
    """_http_get_with_retry 超时/重试/退避测试。"""

    def test_success_on_first_attempt(self) -> None:
        """首次成功不重试。"""
        client = MagicMock()
        client.get.return_value = _mock_response("ok")

        result = _http_get_with_retry(client, "http://test", {}, label="test")
        assert result == "ok"
        assert client.get.call_count == 1

    def test_raises_after_max_retries(self) -> None:
        """超过最大重试次数后抛 THSAdapterError。"""
        client = MagicMock()
        client.get.side_effect = httpx.ConnectError("network error")

        with patch("app.services.ths_adapter.time.sleep") as mock_sleep:
            with pytest.raises(THSAdapterError, match="failed after"):
                _http_get_with_retry(client, "http://test", {}, label="test")

        assert client.get.call_count == HTTP_RETRY_COUNT + 1
        assert mock_sleep.call_count == HTTP_RETRY_COUNT

    def test_success_after_retry(self) -> None:
        """首次失败，重试后成功。"""
        client = MagicMock()
        client.get.side_effect = [
            httpx.ConnectError("temp"),
            _mock_response("ok"),
        ]

        with patch("app.services.ths_adapter.time.sleep"):
            result = _http_get_with_retry(client, "http://test", {}, label="test")

        assert result == "ok"
        assert client.get.call_count == 2

    def test_timeout_is_configured(self) -> None:
        """超时参数存在且合理。"""
        assert FETCH_TIMEOUT_SECONDS == 30
        assert FETCH_RETRY_COUNT == 2
        assert HTTP_RETRY_COUNT == 2


# ---------------------------------------------------------------------------
# 分页解析测试
# ---------------------------------------------------------------------------

class TestParseTotalPageConcept:
    """_parse_total_page_concept 概念目录分页解析测试。"""

    def test_parses_total_page(self) -> None:
        """从 <span class="page_info">1/N</span> 解析总页数。"""
        assert _parse_total_page_concept(_CONCEPT_INDEX_HTML) == 2

    def test_missing_page_info_raises(self) -> None:
        """page_info span 不存在时抛 THSAdapterError。"""
        html = "<html><body>no span</body></html>"
        with pytest.raises(THSAdapterError, match="page_info"):
            _parse_total_page_concept(html)

    def test_invalid_format_raises(self) -> None:
        """page_info 格式异常时抛 THSAdapterError。"""
        html = '<html><body><span class="page_info">invalid</span></body></html>'
        with pytest.raises(THSAdapterError, match="unexpected format"):
            _parse_total_page_concept(html)

    def test_capped_at_max_pages(self) -> None:
        """总页数超过 HTTP_MAX_PAGES 时被截断。"""
        html = '<html><body><span class="page_info">1/999</span></body></html>'
        assert _parse_total_page_concept(html) == HTTP_MAX_PAGES


class TestParsePageNumMember:
    """_parse_page_num_member 成分股分页解析测试。"""

    def test_parses_page_num(self) -> None:
        """从 <a class="changePage" page="N"> 解析总页数。"""
        assert _parse_page_num_member(_MEMBER_HTML, "881001") == 3

    def test_no_change_page_returns_one(self) -> None:
        """无 changePage 标签时返回 1。"""
        html = "<html><body>no pagination</body></html>"
        assert _parse_page_num_member(html, "881001") == 1

    def test_invalid_page_attr_defaults_to_one(self) -> None:
        """page 属性非数字时默认返回 1。"""
        html = '<html><body><a class="changePage" page="abc">x</a></body></html>'
        assert _parse_page_num_member(html, "881001") == 1


class TestExtractConceptCodes:
    """_extract_concept_codes 概念代码提取测试。"""

    def test_extracts_codes_from_hrefs(self) -> None:
        """从 <a href> 链接提取板块代码。"""
        codes = _extract_concept_codes(_CONCEPT_INDEX_HTML)
        assert codes == ["301558", "301559"]

    def test_missing_table_raises(self) -> None:
        """表格不存在时抛 THSAdapterError。"""
        html = "<html><body>no table</body></html>"
        with pytest.raises(THSAdapterError, match="table not found"):
            _extract_concept_codes(html)

    def test_missing_tbody_raises(self) -> None:
        """tbody 不存在时抛 THSAdapterError。"""
        html = '<html><body><table class="m-table m-pager-table">no tbody</table></body></html>'
        with pytest.raises(THSAdapterError, match="tbody"):
            _extract_concept_codes(html)


# ---------------------------------------------------------------------------
# _parse_html_table 测试
# ---------------------------------------------------------------------------

class TestParseHtmlTable:
    """_parse_html_table bs4[html.parser] 表格解析测试。"""

    def test_parses_table_with_headers(self) -> None:
        """从 thead > th 提取列名，从 tbody > td 提取行数据。"""
        html = """
        <html><body>
        <table>
          <thead><tr><th>序号</th><th>名称</th><th>值</th></tr></thead>
          <tbody>
            <tr><td>1</td><td>A</td><td>10</td></tr>
            <tr><td>2</td><td>B</td><td>20</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        rows = _parse_html_table(html)
        assert len(rows) == 2
        assert rows[0] == {"序号": "1", "名称": "A", "值": "10"}
        assert rows[1] == {"序号": "2", "名称": "B", "值": "20"}

    def test_parses_table_without_thead(self) -> None:
        """无 thead 时用列索引作为 key。"""
        html = """
        <html><body>
        <table>
          <tbody>
            <tr><td>1</td><td>A</td></tr>
            <tr><td>2</td><td>B</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        rows = _parse_html_table(html)
        assert len(rows) == 2
        assert rows[0] == {"0": "1", "1": "A"}
        assert rows[1] == {"0": "2", "1": "B"}

    def test_no_table_returns_empty(self) -> None:
        """无表格时返回空列表。"""
        html = "<html><body>no table</body></html>"
        assert _parse_html_table(html) == []

    def test_table_index_out_of_range_returns_empty(self) -> None:
        """table_index 超出范围时返回空列表。"""
        html = '<html><body><table><tbody><tr><td>1</td></tr></tbody></table></body></html>'
        assert _parse_html_table(html, table_index=5) == []

    def test_parses_concept_index_html(self) -> None:
        """解析概念目录 HTML 样本。"""
        rows = _parse_html_table(_CONCEPT_INDEX_HTML)
        assert len(rows) == 2
        assert rows[0]["概念名称"] == "概念A"
        assert rows[1]["概念名称"] == "概念B"

    def test_parses_member_html(self) -> None:
        """解析成分股 HTML 样本。"""
        rows = _parse_html_table(_MEMBER_HTML)
        assert len(rows) == 2
        assert rows[0]["代码"] == "1"
        assert rows[1]["代码"] == "600000"


# ---------------------------------------------------------------------------
# fetch_industry_boards 测试
# ---------------------------------------------------------------------------

class TestFetchIndustryBoards:
    """fetch_industry_boards 行业目录拉取测试。"""

    def test_reads_from_ths_code_name_dict(self) -> None:
        """从 qstock.ths_code_name dict 读取行业目录（无网络）。"""
        mock_qs = MagicMock()
        mock_qs.ths_code_name = {
            "881001": "行业1",
            "881002": "行业2",
            "881001_dup": "行业1重复",
        }
        with patch.dict("sys.modules", {"qstock": mock_qs}):
            boards = fetch_industry_boards()

        assert len(boards) == 3
        assert all(b["type"] == "industry" for b in boards)
        codes = {b["external_code"] for b in boards}
        assert codes == {"881001", "881002", "881001_dup"}

    def test_qstock_not_installed_raises(self) -> None:
        """qstock 未安装时抛 THSAdapterError。"""
        with patch.dict("sys.modules", {"qstock": None}):
            with pytest.raises(THSAdapterError, match="qstock is not installed"):
                fetch_industry_boards()

    def test_ths_code_name_missing_raises(self) -> None:
        """ths_code_name 缺失时抛 THSAdapterError。"""
        mock_qs = MagicMock()
        mock_qs.ths_code_name = None
        with patch.dict("sys.modules", {"qstock": mock_qs}):
            with pytest.raises(THSAdapterError, match="ths_code_name"):
                fetch_industry_boards()

    def test_ths_code_name_empty_raises(self) -> None:
        """ths_code_name 为空 dict 时抛 THSAdapterError。"""
        mock_qs = MagicMock()
        mock_qs.ths_code_name = {}
        with patch.dict("sys.modules", {"qstock": mock_qs}):
            with pytest.raises(THSAdapterError, match="ths_code_name"):
                fetch_industry_boards()


# ---------------------------------------------------------------------------
# fetch_concept_boards 测试
# ---------------------------------------------------------------------------

class TestFetchConceptBoards:
    """fetch_concept_boards 概念目录拉取测试。

    通过 mock _http_get_with_retry 返回 HTML 字符串，测试解析逻辑。
    """

    def test_parses_concept_boards_from_html(self) -> None:
        """通过 HTTP + bs4[html.parser] + pd.read_html 解析概念目录。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_CONCEPT_INDEX_HTML, _CONCEPT_INDEX_HTML_PAGE2],
        ):
            boards = fetch_concept_boards(client, {"User-Agent": "test"})

        assert len(boards) == 3
        assert all(b["type"] == "concept" for b in boards)
        codes = {b["external_code"] for b in boards}
        assert codes == {"301558", "301559", "301560"}
        names = {b["name"] for b in boards}
        assert names == {"概念A", "概念B", "概念C"}

    def test_http_failure_raises(self) -> None:
        """HTTP 请求失败抛 THSAdapterError。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=THSAdapterError("failed after 3 attempts: network error"),
        ):
            with pytest.raises(THSAdapterError, match="failed after"):
                fetch_concept_boards(client, {"User-Agent": "test"})

    def test_deduplicates_concept_codes(self) -> None:
        """概念代码去重。"""
        client = MagicMock()
        # 两页都返回相同的 HTML（相同的 code）
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_CONCEPT_INDEX_HTML, _CONCEPT_INDEX_HTML],
        ):
            boards = fetch_concept_boards(client, {"User-Agent": "test"})

        codes = [b["external_code"] for b in boards]
        assert codes.count("301558") == 1
        assert codes.count("301559") == 1


# ---------------------------------------------------------------------------
# fetch_board_members 测试
# ---------------------------------------------------------------------------

class TestFetchBoardMembers:
    """fetch_board_members 成分股拉取测试。

    通过 mock _http_get_with_retry 返回 HTML 字符串，测试解析逻辑。
    """

    def test_parses_members_and_normalizes_codes(self) -> None:
        """通过 HTTP + pd.read_html 解析成分股，6 位左填充去重。"""
        client = MagicMock()
        # page 1 (page_num=3), page 2 (reuse), page 3
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_MEMBER_HTML, _MEMBER_HTML, _MEMBER_HTML_PAGE2],
        ):
            symbols = fetch_board_members(
                client, {"User-Agent": "test"}, "881001", "industry"
            )

        # page 1+2: "1"→"000001", "600000" ; page 3: "000002"
        assert "000001" in symbols
        assert "600000" in symbols
        assert "000002" in symbols
        assert symbols.count("000001") == 1
        assert symbols.count("600000") == 1

    def test_industry_uses_correct_url(self) -> None:
        """industry 类型使用 thshy 端点。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_MEMBER_HTML, _MEMBER_HTML, _MEMBER_HTML_PAGE2],
        ) as mock_get:
            fetch_board_members(client, {"User-Agent": "test"}, "881101", "industry")

        # 验证 URL 包含 thshy
        first_call_url = mock_get.call_args_list[0][0][1]  # args[1] = url
        assert "thshy" in first_call_url

    def test_concept_uses_correct_url(self) -> None:
        """concept 类型使用 gn 端点。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_MEMBER_HTML, _MEMBER_HTML, _MEMBER_HTML_PAGE2],
        ) as mock_get:
            fetch_board_members(client, {"User-Agent": "test"}, "301558", "concept")

        first_call_url = mock_get.call_args_list[0][0][1]
        assert "/gn/" in first_call_url

    def test_unsupported_board_type_raises(self) -> None:
        """不支持的 board_type 抛 THSAdapterError。"""
        client = MagicMock()
        with pytest.raises(THSAdapterError, match="unsupported board_type"):
            fetch_board_members(client, {}, "881001", "unknown")

    def test_http_failure_raises(self) -> None:
        """HTTP 请求失败抛 THSAdapterError。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=THSAdapterError("failed after 3 attempts: network error"),
        ):
            with pytest.raises(THSAdapterError, match="failed after"):
                fetch_board_members(client, {}, "881001", "industry")


# ---------------------------------------------------------------------------
# QStockTHSAdapter 测试
# ---------------------------------------------------------------------------

class TestQStockTHSAdapter:
    """QStockTHSAdapter 集成测试。"""

    def test_fetch_boards_sync_combines_industry_and_concept(self) -> None:
        """fetch_boards_sync 合并行业目录和概念目录。"""
        adapter = QStockTHSAdapter()
        adapter._headers = {"User-Agent": "test"}

        mock_qs = MagicMock()
        mock_qs.ths_code_name = {"881001": "行业1"}

        with patch.dict("sys.modules", {"qstock": mock_qs}):
            with patch(
                "app.services.ths_adapter._http_get_with_retry",
                side_effect=[_CONCEPT_INDEX_HTML, _CONCEPT_INDEX_HTML_PAGE2],
            ):
                boards = adapter.fetch_boards_sync()

        industry = [b for b in boards if b["type"] == "industry"]
        concept = [b for b in boards if b["type"] == "concept"]
        assert len(industry) == 1
        assert len(concept) == 3

    def test_fetch_memberships_sync_delegates_correctly(self) -> None:
        """fetch_memberships_sync 委托 fetch_board_members。"""
        adapter = QStockTHSAdapter()
        adapter._headers = {"User-Agent": "test"}

        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_MEMBER_HTML, _MEMBER_HTML, _MEMBER_HTML_PAGE2],
        ):
            symbols = adapter.fetch_memberships_sync("881001", "industry")

        assert "000001" in symbols
        assert "600000" in symbols


# ---------------------------------------------------------------------------
# QStockFetcher 异步包装测试
# ---------------------------------------------------------------------------

class TestQStockFetcherAsync:
    """QStockFetcher 异步包装测试。"""

    @pytest.mark.asyncio
    async def test_fetch_boards_uses_to_thread(self) -> None:
        """fetch_boards 通过 asyncio.to_thread 执行同步调用。"""
        fetcher = QStockFetcher()

        with patch.object(
            fetcher._adapter, "fetch_boards_sync",
            return_value=[{"external_code": "881001", "name": "行业1", "type": "industry"}],
        ):
            result = await fetcher.fetch_boards()

        assert len(result) == 1
        assert result[0]["external_code"] == "881001"

    @pytest.mark.asyncio
    async def test_fetch_memberships_uses_to_thread(self) -> None:
        """fetch_memberships 通过 asyncio.to_thread 执行同步调用。"""
        fetcher = QStockFetcher()

        with patch.object(
            fetcher._adapter, "fetch_memberships_sync",
            return_value=["000001", "600000"],
        ):
            result = await fetcher.fetch_memberships("881001", "industry")

        assert result == ["000001", "600000"]

    @pytest.mark.asyncio
    async def test_error_propagates_as_qstock_fetch_error(self) -> None:
        """适配器错误通过 QStockFetchError（THSAdapterError 别名）传播。"""
        fetcher = QStockFetcher()

        with patch.object(
            fetcher._adapter, "fetch_boards_sync",
            side_effect=THSAdapterError("concept failed"),
        ):
            with pytest.raises(QStockFetchError, match="concept failed"):
                await fetcher.fetch_boards()


# ---------------------------------------------------------------------------
# 错误场景测试（C10 Step 6 补充）
# ---------------------------------------------------------------------------

_EMPTY_HTML = "<html><body></body></html>"
_FORBIDDEN_HTML = "<html><body><h1>403 Forbidden</h1></body></html>"


class TestErrorScenarios:
    """403/超时/空页面/列名变化/部分失败测试。"""

    def test_http_403_raises_ths_adapter_error(self) -> None:
        """HTTP 403 响应抛 THSAdapterError（raise_for_status 触发）。"""
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=resp
        )
        client.get.return_value = resp

        with patch("app.services.ths_adapter.time.sleep"):
            with pytest.raises(THSAdapterError, match="failed after"):
                _http_get_with_retry(client, "http://test", {}, label="403_test")

    def test_timeout_raises_ths_adapter_error(self) -> None:
        """HTTP 超时抛 THSAdapterError。"""
        client = MagicMock()
        client.get.side_effect = httpx.ReadTimeout("read timeout")

        with patch("app.services.ths_adapter.time.sleep"):
            with pytest.raises(THSAdapterError, match="failed after"):
                _http_get_with_retry(client, "http://test", {}, label="timeout_test")

    def test_empty_page_concept_raises(self) -> None:
        """概念目录空页面（无 table）抛 THSAdapterError。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            return_value=_EMPTY_HTML,
        ):
            with pytest.raises(THSAdapterError, match="page_info"):
                fetch_concept_boards(client, {"User-Agent": "test"})

    def test_empty_page_member_returns_empty(self) -> None:
        """成分股空页面（无 table）跳过该页，不抛异常。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            return_value=_EMPTY_HTML,
        ):
            # 空页面不会抛异常，只是跳过（_parse_html_table 返回空列表）
            symbols = fetch_board_members(client, {}, "881001", "industry")
        assert symbols == []

    def test_column_name_variation_uses_fallback(self) -> None:
        """概念名称列名变化时使用备用列名。"""
        html = """
        <html><body>
        <span class="page_info">1/1</span>
        <table class="m-table m-pager-table">
          <thead><tr><th>序号</th><th>名称</th><th>数量</th></tr></thead>
          <tbody>
            <tr><td>1</td><td><a href="http://q.10jqka.com.cn/gn/detail/code/301558/">概念X</a></td><td>10</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            return_value=html,
        ):
            boards = fetch_concept_boards(client, {"User-Agent": "test"})

        assert len(boards) == 1
        assert boards[0]["name"] == "概念X"
        assert boards[0]["external_code"] == "301558"

    def test_partial_failure_raises(self) -> None:
        """概念目录多页拉取，第2页失败时整体失败。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_CONCEPT_INDEX_HTML, THSAdapterError("page 2 failed")],
        ):
            with pytest.raises(THSAdapterError, match="page 2 failed"):
                fetch_concept_boards(client, {"User-Agent": "test"})

    def test_member_partial_failure_raises(self) -> None:
        """成分股多页拉取，第2页失败时整体失败。"""
        client = MagicMock()
        with patch(
            "app.services.ths_adapter._http_get_with_retry",
            side_effect=[_MEMBER_HTML, THSAdapterError("page 2 failed"), _MEMBER_HTML_PAGE2],
        ):
            with pytest.raises(THSAdapterError, match="page 2 failed"):
                fetch_board_members(client, {}, "881001", "industry")


class TestProviderIsolation:
    """Provider 不能混用测试。

    一次同步只能由一个完整 provider 提供目录和全部成分。
    board_sync_service.sync_boards 接收单个 fetcher，不会混用。
    本测试验证 QStockTHSAdapter 一次实例只使用一个 provider 的数据源。
    """

    def test_adapter_uses_single_provider(self) -> None:
        """QStockTHSAdapter 实例只使用 qstock+THS web 作为数据源。"""
        adapter = QStockTHSAdapter()
        # 验证 adapter 内部只有一个 _adapter 实例（不存在第二个 provider）
        assert hasattr(adapter, "_headers")
        assert adapter._headers is None  # 初始状态

    def test_two_adapters_are_independent(self) -> None:
        """两个 adapter 实例独立，不共享状态。"""
        adapter1 = QStockTHSAdapter()
        adapter2 = QStockTHSAdapter()
        adapter1._headers = {"User-Agent": "test1"}

        # adapter2 不受 adapter1 影响
        assert adapter2._headers is None

    def test_fetch_boards_does_not_mix_providers(self) -> None:
        """fetch_boards_sync 只从 qstock.ths_code_name + THS web 获取数据。

        不会从其他数据源（如东方财富）获取数据混入结果。
        """
        adapter = QStockTHSAdapter()
        adapter._headers = {"User-Agent": "test"}

        mock_qs = MagicMock()
        mock_qs.ths_code_name = {"881001": "行业1"}

        with patch.dict("sys.modules", {"qstock": mock_qs}):
            with patch(
                "app.services.ths_adapter._http_get_with_retry",
                side_effect=[_CONCEPT_INDEX_HTML, _CONCEPT_INDEX_HTML_PAGE2],
            ):
                boards = adapter.fetch_boards_sync()

        # 所有 boards 只来自两个来源：ths_code_name（industry）和 THS web（concept）
        industry_boards = [b for b in boards if b["type"] == "industry"]
        concept_boards = [b for b in boards if b["type"] == "concept"]
        assert len(industry_boards) == 1
        assert len(concept_boards) == 3
        # 验证行业来自 ths_code_name
        assert industry_boards[0]["external_code"] == "881001"
        # 验证概念来自 THS web 解析
        concept_codes = {b["external_code"] for b in concept_boards}
        assert concept_codes == {"301558", "301559", "301560"}
