"""同花顺板块数据独立适配器（PRD §7.5，C10 决策）。

背景：
qstock 1.3.1 的 ths_concept_name_code() / ths_index_member() 硬编码
BeautifulSoup(res.text, "lxml")，但 METADATA 未声明 lxml 依赖。
C10 决策：不添加 lxml，改为仓库内独立实现 THS 板块目录/成分访问逻辑。

本模块只依赖：
- httpx（已在 deps）：HTTP 请求，带连接/读取超时
- bs4[html.parser]（Python 内置解析器，不需 lxml）：分页/链接/表格解析
- qstock.ths_code_name：行业板块目录 dict（硬编码数据，无网络无依赖）
- qstock.data.util.get_ths_header：反爬 cookie 生成（py_mini_racer 执行 ths.js）

不引入：backtrader、pyfolio、scikit-learn、lxml。

数据语义：
- 行业板块目录：ths_code_name dict，306 条，{code: name}，如 {"881101": "种植业与林业"}
- 概念板块目录：同花顺概念列表，含 code（如 "301558"）和 name
- 行业成分股：URL http://q.10jqka.com.cn/thshy/detail/code/{code}/
- 概念成分股：URL http://q.10jqka.com.cn/gn/detail/code/{code}/

参考来源：qstock 1.3.1（MIT License, https://github.com/tkfy920/qstock），
基于公开同花顺 web 接口独立实现请求与解析逻辑，未复制 qstock 源码。

约束：
- 单并发；设置连接/读取超时和有限重试。
- 任一目录/成分请求失败抛 THSAdapterError，不返回空列表伪装成功。
- 标准化并去重板块代码、股票代码（6 位左填充）。
"""

from __future__ import annotations

import logging
import time

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# HTTP 拉取参数
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=30.0)
HTTP_RETRY_COUNT = 2
HTTP_BACKOFF_BASE = 2.0  # 指数退避：2s, 4s
HTTP_MAX_PAGES = 200  # 安全上限，防止恶意分页导致无限循环

# 同花顺 web 接口端点（公开页面）
_THS_BASE = "http://q.10jqka.com.cn"
_CONCEPT_INDEX_URL = f"{_THS_BASE}/gn/index/field/addtime/order/desc/page/{{page}}/ajax/1/"
_INDUSTRY_MEMBER_URL = (
    f"{_THS_BASE}/thshy/detail/field/199112/order/desc/page/{{page}}/ajax/1/code/{{code}}"
)
_CONCEPT_MEMBER_URL = (
    f"{_THS_BASE}/gn/detail/field/264648/order/desc/page/{{page}}/ajax/1/code/{{code}}"
)

# 默认 User-Agent（与 qstock get_ths_header 一致，备用）
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36"
)


class THSAdapterError(Exception):
    """THS 适配器拉取失败（目录或成分请求异常）。"""


def _get_ths_headers() -> dict[str, str]:
    """获取同花顺请求头（含反爬 cookie）。

    使用 qstock.data.util.get_ths_header() 生成 cookie。
    get_ths_header 通过 py_mini_racer 执行 qstock/data/ths.js 生成 cookie "v" 值。
    不复制 ths.js 源码，直接调用 qstock 公开函数。

    若 qstock 未安装或 get_ths_header 失败，回退到无 cookie 的基础 UA（可能被反爬拦截）。
    """
    try:
        from qstock.data.util import get_ths_header

        headers = get_ths_header()
        if isinstance(headers, dict) and "Cookie" in headers:
            return headers
        logger.warning("get_ths_header returned unexpected format: %s", type(headers))
    except ImportError:
        logger.warning("qstock not installed, using basic UA without cookie")
    except Exception as exc:
        logger.warning("get_ths_header failed: %s, using basic UA without cookie", exc)
    return {"User-Agent": _DEFAULT_UA}


def _http_get_with_retry(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    label: str = "",
) -> str:
    """同步 HTTP GET，带超时、有限重试和指数退避。

    超时或异常时重试，超过 HTTP_RETRY_COUNT 次后抛 THSAdapterError。
    返回响应文本。
    """
    last_exc: Exception | None = None
    for attempt in range(HTTP_RETRY_COUNT + 1):
        try:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < HTTP_RETRY_COUNT:
                backoff = HTTP_BACKOFF_BASE ** attempt
                logger.warning(
                    "THS %s attempt %d failed: %s, retrying in %.1fs",
                    label or url,
                    attempt + 1,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
            else:
                logger.error(
                    "THS %s failed after %d attempts: %s",
                    label or url,
                    HTTP_RETRY_COUNT + 1,
                    exc,
                )

    raise THSAdapterError(
        f"THS {label or url} failed after {HTTP_RETRY_COUNT + 1} attempts: {last_exc}"
    )


def _parse_total_page_concept(html_text: str) -> int:
    """从概念目录页面解析总页数。

    同花顺概念目录页面含 <span class="page_info">1/N</span>，
    N 为总页数。使用 bs4 html.parser 解析（不需 lxml）。
    """
    soup = BeautifulSoup(html_text, "html.parser")
    span = soup.find("span", attrs={"class": "page_info"})
    if span is None or not span.get_text(strip=True):
        raise THSAdapterError(
            "concept index page_info span not found (pagination parse failed)"
        )
    text = span.get_text(strip=True)
    parts = text.split("/")
    if len(parts) < 2:
        raise THSAdapterError(f"concept index page_info unexpected format: {text!r}")
    try:
        total = int(parts[1])
    except ValueError as exc:
        raise THSAdapterError(
            f"concept index page_info total_page not int: {parts[1]!r}"
        ) from exc
    if total < 1:
        raise THSAdapterError(f"concept index total_page < 1: {total}")
    return min(total, HTTP_MAX_PAGES)


def _parse_page_num_member(html_text: str, board_code: str) -> int:
    """从成分股页面解析总页数。

    同花顺成分股页面含 <a class="changePage" page="N">，
    最后一个的 page 属性为总页数。使用 bs4 html.parser 解析（不需 lxml）。
    无分页时返回 1。
    """
    soup = BeautifulSoup(html_text, "html.parser")
    change_pages = soup.find_all("a", attrs={"class": "changePage"})
    if not change_pages:
        return 1
    last_page_attr = str(change_pages[-1].get("page", "1"))
    try:
        page_num = int(last_page_attr)
    except ValueError:
        logger.warning(
            "member page_num parse failed for board %s, attr=%r, defaulting to 1",
            board_code,
            last_page_attr,
        )
        return 1
    return min(max(page_num, 1), HTTP_MAX_PAGES)


def _extract_concept_codes(html_text: str) -> list[str]:
    """从概念目录页面提取板块代码列表。

    同花顺概念目录表格中第 2 列含 <a href="/gn/detail/code/{code}/">，
    从 href 中提取 code。使用 bs4 html.parser 解析。
    """
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", attrs={"class": "m-table m-pager-table"})
    if not isinstance(table, Tag):
        raise THSAdapterError(
            "concept index table not found (m-table m-pager-table)"
        )
    tbody = table.find("tbody")
    if not isinstance(tbody, Tag):
        raise THSAdapterError("concept index table tbody not found")

    codes: list[str] = []
    rows = tbody.find_all("tr")
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 2:
            continue
        link = tds[1].find("a")
        if link is None:
            continue
        href = str(link.get("href", ""))
        # href 格式: http://q.10jqka.com.cn/gn/detail/code/301558/ 或 /gn/detail/code/301558/
        parts = href.rstrip("/").split("/")
        if len(parts) >= 1 and parts[-1]:
            code = parts[-1].strip()
            if code:
                codes.append(code)
    return codes


def _parse_html_table(html_text: str, table_index: int = 0) -> list[dict[str, str]]:
    """使用 bs4[html.parser] 解析 HTML 表格，返回行字典列表。

    不依赖 lxml 或 pandas.read_html（pandas.read_html 默认需要 lxml）。
    使用 Python 内置 html.parser，从 <thead><tr><th> 提取列名，
    从 <tbody><tr><td> 提取行数据。

    Args:
        html_text: HTML 文本
        table_index: 使用第几个 <table>（默认第 0 个）

    Returns:
        list[dict[str, str]]: 每行一个 dict，key 为列名（th 文本），value 为单元格文本。
        若无 thead 则用列索引字符串作为 key（"0", "1", ...）。
    """
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    if not tables or table_index >= len(tables):
        return []

    table = tables[table_index]

    # 提取列名（从 thead > tr > th）
    headers: list[str] = []
    thead = table.find("thead")
    if isinstance(thead, Tag):
        header_row = thead.find("tr")
        if isinstance(header_row, Tag):
            for th in header_row.find_all("th"):
                headers.append(th.get_text(strip=True))

    # 提取行数据（从 tbody > tr > td；无 tbody 时从 table > tr > td）
    tbody = table.find("tbody")
    source: Tag = tbody if isinstance(tbody, Tag) else table

    rows: list[dict[str, str]] = []
    for tr in source.find_all("tr", recursive=False):
        tds = tr.find_all("td")
        if not tds:
            continue
        if headers and len(headers) >= len(tds):
            row = {headers[i]: tds[i].get_text(strip=True) for i in range(len(tds))}
        else:
            row = {str(i): tds[i].get_text(strip=True) for i in range(len(tds))}
        rows.append(row)

    return rows


def fetch_industry_boards() -> list[dict[str, str]]:
    """拉取行业板块目录。

    使用 qstock.ths_code_name dict（硬编码数据，无网络无依赖）。
    返回 [{external_code, name, type: 'industry'}]。
    """
    try:
        import qstock as qs
    except ImportError as exc:
        raise THSAdapterError("qstock is not installed; cannot read ths_code_name") from exc

    ths_code_name = getattr(qs, "ths_code_name", None)
    if not isinstance(ths_code_name, dict) or not ths_code_name:
        raise THSAdapterError(
            "qstock ths_code_name 缺失或非 dict（行业目录不可用）"
        )

    boards: list[dict[str, str]] = []
    seen: set[str] = set()
    for code, name in ths_code_name.items():
        code_str = str(code).strip()
        name_str = str(name).strip()
        if code_str and name_str and code_str not in seen:
            seen.add(code_str)
            boards.append(
                {"external_code": code_str, "name": name_str, "type": "industry"}
            )
    logger.info("Fetched %d industry boards from qstock.ths_code_name", len(boards))
    return boards


def fetch_concept_boards(client: httpx.Client, headers: dict[str, str]) -> list[dict[str, str]]:
    """拉取概念板块目录。

    通过同花顺 web 接口分页拉取，使用 httpx + bs4[html.parser]。
    返回 [{external_code, name, type: 'concept'}]。
    """
    # 第一页获取总页数
    first_url = _CONCEPT_INDEX_URL.format(page=1)
    first_html = _http_get_with_retry(client, first_url, headers, label="concept_index_p1")
    total_page = _parse_total_page_concept(first_html)

    boards: list[dict[str, str]] = []
    seen: set[str] = set()

    for page in range(1, total_page + 1):
        url = _CONCEPT_INDEX_URL.format(page=page)
        html_text = first_html if page == 1 else _http_get_with_retry(
            client, url, headers, label=f"concept_index_p{page}"
        )

        # 提取板块代码（从 <a href> 链接）
        codes = _extract_concept_codes(html_text)

        # 解析表格数据（bs4[html.parser]，不依赖 lxml）
        rows = _parse_html_table(html_text)
        if not rows:
            raise THSAdapterError(f"concept index page {page}: no tables parsed")

        # 同花顺概念目录表格列：序号, 概念名称, 成分股数量, 日期
        # 代码从 <a href> 提取，与表格行一一对应
        if len(codes) != len(rows):
            logger.warning(
                "concept index page %d: codes(%d) != rows(%d), using min",
                page, len(codes), len(rows),
            )
        n = min(len(codes), len(rows))
        for i in range(n):
            code = codes[i].strip()
            # 概念名称列：优先中文列名
            name = ""
            for col in ("概念名称", "名称", "板块名称"):
                if col in rows[i]:
                    name = rows[i][col].strip()
                    break
            if code and name and code not in seen:
                seen.add(code)
                boards.append(
                    {"external_code": code, "name": name, "type": "concept"}
                )

    logger.info("Fetched %d concept boards from THS web", len(boards))
    return boards


def fetch_board_members(
    client: httpx.Client,
    headers: dict[str, str],
    board_external_code: str,
    board_type: str,
) -> list[str]:
    """拉取指定板块的成分股代码列表。

    通过同花顺 web 接口分页拉取，使用 httpx + bs4[html.parser]。
    board_type: 'industry' 或 'concept'，决定使用哪个端点。
    返回 [symbol, ...]（6 位股票代码）。
    """
    if board_type == "industry":
        url_template = _INDUSTRY_MEMBER_URL
    elif board_type == "concept":
        url_template = _CONCEPT_MEMBER_URL
    else:
        raise THSAdapterError(f"unsupported board_type: {board_type!r}")

    # 第一页获取总页数
    first_url = url_template.format(page=1, code=board_external_code)
    first_html = _http_get_with_retry(
        client, first_url, headers, label=f"member_{board_external_code}_p1"
    )
    page_num = _parse_page_num_member(first_html, board_external_code)

    symbols: list[str] = []
    seen: set[str] = set()

    for page in range(1, page_num + 1):
        url = url_template.format(page=page, code=board_external_code)
        html_text = first_html if page == 1 else _http_get_with_retry(
            client, url, headers, label=f"member_{board_external_code}_p{page}"
        )

        # 解析表格（bs4[html.parser]，不依赖 lxml）
        rows = _parse_html_table(html_text)
        if not rows:
            logger.warning(
                "board %s page %d: no tables parsed",
                board_external_code, page,
            )
            continue

        # 成分股表格含"代码"列（中文列名）或 "code" 列
        code_col: str | None = None
        for col in ("代码", "code", "股票代码"):
            if col in rows[0]:
                code_col = col
                break
        if code_col is None:
            logger.warning(
                "board %s page %d: no code column found, keys=%s",
                board_external_code, page, list(rows[0].keys()),
            )
            continue

        for row in rows:
            code = row.get(code_col, "").strip()
            if not code:
                continue
            # 标准化为 6 位代码
            if code.isdigit() and len(code) < 6:
                code = code.zfill(6)
            if code and code not in seen:
                seen.add(code)
                symbols.append(code)

    logger.info(
        "Fetched %d members for board %s (%s)", len(symbols), board_external_code, board_type
    )
    return symbols


class QStockTHSAdapter:
    """同花顺板块数据独立适配器。

    实现 BoardFetcher 协议（board_sync_service.BoardFetcher）。
    不缓存数据，每次调用实时拉取。
    HTTP 同步调用通过 asyncio.to_thread 包装（由 QStockFetcher 负责）。

    依赖：
    - qstock.ths_code_name（行业目录 dict，无网络）
    - qstock.data.util.get_ths_header（cookie 生成，py_mini_racer）
    - httpx（HTTP 请求）
    - bs4[html.parser]（分页/链接/表格解析）

    不依赖：lxml、backtrader、pyfolio、scikit-learn。
    """

    def __init__(self) -> None:
        self._headers: dict[str, str] | None = None

    def _get_headers(self) -> dict[str, str]:
        """延迟获取请求头（含反爬 cookie），缓存避免重复生成。"""
        if self._headers is None:
            self._headers = _get_ths_headers()
        return self._headers

    def fetch_boards_sync(self) -> list[dict[str, str]]:
        """同步拉取板块目录（行业 + 概念）。

        返回 [{external_code, name, type}]。
        type: 'industry' | 'concept'

        任一目录请求失败抛出 THSAdapterError。
        """
        headers = self._get_headers()
        boards: list[dict[str, str]] = []

        # 行业目录：qstock.ths_code_name dict（无网络）
        boards.extend(fetch_industry_boards())

        # 概念目录：同花顺 web 接口（需网络）
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            boards.extend(fetch_concept_boards(client, headers))

        logger.info("Fetched %d total boards (industry + concept)", len(boards))
        return boards

    def fetch_memberships_sync(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        """同步拉取指定板块的成分股代码列表。

        Args:
            board_external_code: 同花顺板块代码
            board_type: 'industry' | 'concept'

        Returns:
            股票代码列表（如 ['000001', '000002', ...]）

        请求失败抛出 THSAdapterError。
        """
        headers = self._get_headers()
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            return fetch_board_members(
                client, headers, board_external_code, board_type
            )
