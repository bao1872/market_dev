"""[EOD-SNAPSHOT] 东方财富全市场收盘快照 provider 单元测试（纯单元，无网络）。

覆盖 fail-closed 契约：
1. 分页（>1 页）拉取完整；
2. 中间页网络失败 → 整体 fail，禁止返回半截市场；
3. total 与实拉行数不一致 → fail-closed；
4. 非法 payload / 非法 total → fail-closed；
5. 网络抖动重试后成功；
6. SH / SZ / BJ 分类，含新北交所 920xxx；
7. f13 身份校验 fail-closed（沪/深同码碰撞、f13 与市场不符、f13 缺失）；
8. f124 → Asia/Shanghai trade_date，非目标日不得通过；
9. 价格字段不缩放（fltt=2 已是元）；volume=手 / amount=元 同样不缩放；
10. 停牌/空价行仍归一化（由落库层判废），不得抛异常；
11. Eastmoney 历史 kline 必须 fqt=0 且不缩放价格；北交所 secid 前缀为 0；
12. 当日收盘窗口守卫 can_use_same_day_eod_snapshot（盘中/过去日必须拒绝）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services import eod_market_snapshot_provider as prov

_SH = ZoneInfo("Asia/Shanghai")


def _ts(year: int, month: int, day: int, hour: int = 15, minute: int = 0) -> int:
    """构造 Asia/Shanghai 某时刻的秒级时间戳（不硬编码 epoch）。"""
    return int(datetime(year, month, day, hour, minute, tzinfo=_SH).timestamp())


TARGET_DAY = date(2026, 9, 11)
TARGET_TS = _ts(2026, 9, 11)


def _row(
    code: str,
    *,
    name: str = "测试股",
    f13: int = 0,
    close: Any = 12.34,       # 元（fltt=2 下东财已直接返回元，不再缩放）
    high: Any = 13.00,
    low: Any = 12.00,
    open_: Any = 12.50,
    volume: Any = 100000,     # 手（东财 f5；parse 时 ×100 转股）
    amount: Any = 123456789,  # 元
    prev_close: Any = 12.20,
    f124: Any = TARGET_TS,
) -> dict[str, Any]:
    return {
        "f12": code,
        "f14": name,
        "f13": f13,
        "f2": close,
        "f15": high,
        "f16": low,
        "f17": open_,
        "f5": volume,
        "f6": amount,
        "f18": prev_close,
        "f124": f124,
    }


def _page(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {"data": {"total": total, "diff": rows}}


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://example.invalid")
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError("boom", request=req, response=resp)

    def json(self) -> Any:
        return self._payload


class _ScriptedClient:
    """按调用顺序返回 payload 或抛异常；用尽后重复最后一项。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.urls: list[str] = []

    async def get(self, url: str, params: Any = None, timeout: Any = None) -> _FakeResponse:
        self.urls.append(url)
        self.calls.append(params or {})
        idx = min(len(self.calls) - 1, len(self._script) - 1)
        item = self._script[idx]
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


class _HostScriptedClient:
    """按 **(host, page)** 返回 payload；未登记的组合默认 raise 以便暴露意外请求。

    用于验证「一个 snapshot 只来自一个 host」与 host 级 failover：
    host 名从 url 里提取，page 从 params['pn'] 提取。
    """

    def __init__(self, script: dict[tuple[str, int], Any]) -> None:
        self._script = dict(script)
        self.calls: list[tuple[str, int]] = []

    @staticmethod
    def _host_of(url: str) -> str:
        return url.split("//", 1)[-1].split("/", 1)[0]

    async def get(self, url: str, params: Any = None, timeout: Any = None) -> _FakeResponse:
        host = self._host_of(url)
        page = int((params or {}).get("pn", 1))
        self.calls.append((host, page))
        key = (host, page)
        if key not in self._script:
            raise httpx.ConnectError(f"{host} page{page} not scripted")
        item = self._script[key]
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避不真实 sleep，避免测试变慢。"""
    monkeypatch.setattr(prov, "_PAGE_RETRY_BASE_DELAY", 0.0)


# =========================================================================
# 1. 分页
# =========================================================================


@pytest.mark.asyncio
async def test_fetch_snapshot_paginates_beyond_one_page() -> None:
    """total=450 / page_size=200 → 必须请求 3 页并返回 450 行。"""
    p1 = [_row(f"600{i:03d}") for i in range(200)]
    p2 = [_row(f"601{i:03d}") for i in range(200)]
    p3 = [_row(f"602{i:03d}") for i in range(50)]
    client = _ScriptedClient([_page(p1, 450), _page(p2, 450), _page(p3, 450)])

    rows = await prov.fetch_full_a_share_snapshot(client, page_size=200)  # type: ignore[arg-type]

    assert len(rows) == 450
    assert len(client.calls) == 3
    # 页码递增
    assert [c["pn"] for c in client.calls] == [1, 2, 3]
    # 分页参数必须带全市场 A 股过滤，不得退化为单市场
    assert all(c["fs"] == prov.A_SHARE_FILTER for c in client.calls)


@pytest.mark.asyncio
async def test_fetch_snapshot_exact_multiple_stops_without_extra_page() -> None:
    """total 恰为 page_size 整数倍时，取满即停，不多拉一页。"""
    p1 = [_row(f"600{i:03d}") for i in range(200)]
    p2 = [_row(f"601{i:03d}") for i in range(200)]
    client = _ScriptedClient([_page(p1, 400), _page(p2, 400)])

    rows = await prov.fetch_full_a_share_snapshot(client, page_size=200)  # type: ignore[arg-type]

    assert len(rows) == 400
    assert len(client.calls) == 2


# =========================================================================
# 2. 中间页失败 / 不完整 → fail-closed
# =========================================================================


@pytest.mark.asyncio
async def test_middle_page_failure_fails_closed_not_partial() -> None:
    """第 2 页持续网络失败必须抛错，绝不能返回只剩第 1 页的「半截市场」。"""
    p1 = [_row(f"600{i:03d}") for i in range(200)]
    client = _ScriptedClient(
        [_page(p1, 450), httpx.ConnectError("page2 down")]
    )

    with pytest.raises(prov.SnapshotProviderError, match="第 2 页"):
        await prov.fetch_full_a_share_snapshot(client, page_size=200)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_total_mismatch_fails_closed() -> None:
    """声明 total=1000 但只拉到 200 行后就空页 → 必须 fail-closed。"""
    p1 = [_row(f"600{i:03d}") for i in range(200)]
    client = _ScriptedClient([_page(p1, 1000), _page([], 1000)])

    with pytest.raises(prov.SnapshotProviderError, match="incomplete snapshot host="):
        await prov.fetch_full_a_share_snapshot(client, page_size=200)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_total_drift_fails_closed() -> None:
    """分页期间 total 变化（数据在流动）→ 该 host 整体作废，必须 fail-closed。"""
    p1 = [_row(f"600{i:03d}") for i in range(100)]
    client = _HostScriptedClient(
        {
            (host, 1): _page(p1, 300)
            for host in prov.EASTMONEY_CLIST_HOSTS
        }
        | {
            (host, 2): _page(p1, 250)
            for host in prov.EASTMONEY_CLIST_HOSTS
        }
    )

    with pytest.raises(prov.SnapshotProviderError, match="total changed during pagination"):
        await prov.fetch_full_a_share_snapshot(client, page_size=100)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_invalid_total_fails_closed() -> None:
    """total<=0 视为非法，必须 fail-closed。"""
    client = _ScriptedClient([_page([_row("600519")], 0)])

    with pytest.raises(prov.SnapshotProviderError, match="invalid total host="):
        await prov.fetch_full_a_share_snapshot(client, page_size=200)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_invalid_payload_fails_closed() -> None:
    """data 非 dict（例如接口改版 / 限流返回）必须 fail-closed。"""
    client = _ScriptedClient([{"data": None}])

    with pytest.raises(prov.SnapshotProviderError, match="invalid snapshot payload host="):
        await prov.fetch_full_a_share_snapshot(client, page_size=200)  # type: ignore[arg-type]


# =========================================================================
# 2b. 单 host 一致性 + host 级 failover
# =========================================================================


@pytest.mark.asyncio
async def test_all_pages_come_from_single_host() -> None:
    """整批分页必须来自同一个 host（禁止 hostA page1 + hostB page2 拼接）。"""
    host_a = prov.EASTMONEY_CLIST_HOSTS[0]
    p1 = [_row(f"600{i:03d}") for i in range(100)]
    p2 = [_row(f"601{i:03d}") for i in range(50)]
    client = _HostScriptedClient(
        {
            (host_a, 1): _page(p1, 150),
            (host_a, 2): _page(p2, 150),
        }
    )

    rows = await prov.fetch_full_a_share_snapshot(client, page_size=100)  # type: ignore[arg-type]

    assert len(rows) == 150
    assert {h for h, _ in client.calls} == {host_a}
    assert [p for _, p in client.calls] == [1, 2]


@pytest.mark.asyncio
async def test_host_failover_restarts_from_page_one_not_mixed() -> None:
    """hostA 第 2 页失败 → 整个 hostA 作废，hostB 必须**从第 1 页重新开始**。

    若实现是「hostA page1 + hostB page2」，结果里会混进两个 host 的市场切片，
    这是必须禁止的（延时源与实时源混拼）。
    """
    host_a, host_b = prov.EASTMONEY_CLIST_HOSTS[0], prov.EASTMONEY_CLIST_HOSTS[1]
    a1 = [_row(f"600{i:03d}") for i in range(100)]
    b1 = [_row(f"600{i:03d}") for i in range(100)]
    b2 = [_row(f"601{i:03d}") for i in range(50)]
    client = _HostScriptedClient(
        {
            (host_a, 1): _page(a1, 150),
            (host_a, 2): httpx.ConnectError("hostA page2 down"),
            (host_b, 1): _page(b1, 150),
            (host_b, 2): _page(b2, 150),
        }
    )

    rows = await prov.fetch_full_a_share_snapshot(client, page_size=100)  # type: ignore[arg-type]

    assert len(rows) == 150
    # hostB 必须从 page1 开始（不是 page2）
    assert (host_b, 1) in client.calls
    assert (host_b, 2) in client.calls
    # 最终结果只含 hostB 的行
    assert [r["f12"] for r in rows][100:] == [r["f12"] for r in b2]


@pytest.mark.asyncio
async def test_host_failover_all_hosts_failed_reports_each_host() -> None:
    """所有 host 都失败 → 报错信息必须逐 host 列出，便于诊断。"""
    client = _ScriptedClient([httpx.ConnectError("all down")])

    with pytest.raises(prov.SnapshotProviderError, match="all snapshot hosts failed"):
        await prov.fetch_full_a_share_snapshot(client, page_size=100)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_http_status_error_retries_then_succeeds() -> None:
    """单次 5xx 抖动后成功：必须重试而不是立刻判死。"""
    p1 = [_row("600519")]
    client = _ScriptedClient([_page(p1, 1)])

    async def flaky_get(url: str, params: Any = None, timeout: Any = None) -> _FakeResponse:
        client.calls.append(params or {})
        if len(client.calls) == 1:
            req = httpx.Request("GET", "https://example.invalid")
            resp = httpx.Response(503, request=req)
            raise httpx.HTTPStatusError("503", request=req, response=resp)
        return _FakeResponse(_page(p1, 1))

    client.get = flaky_get  # type: ignore[method-assign]

    rows = await prov.fetch_full_a_share_snapshot(client, page_size=200)  # type: ignore[arg-type]

    assert len(rows) == 1
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_host_failover_switches_to_next_candidate() -> None:
    """首个主机被拒时必须自动切换候选主机，而不是整体失败。

    实测：push2.eastmoney.com 在部分网络会直接断开连接（RemoteProtocolError），
    而 push2delay.eastmoney.com 可用；二者为同一 API。
    """
    assert len(prov.EASTMONEY_CLIST_HOSTS) >= 2
    primary, secondary = prov.EASTMONEY_CLIST_HOSTS[0], prov.EASTMONEY_CLIST_HOSTS[1]
    hits: list[str] = []

    async def fake_get(url: str, params: Any = None, timeout: Any = None) -> _FakeResponse:
        hits.append(url)
        if primary in url:
            raise httpx.ConnectError("primary blocked")
        return _FakeResponse(_page([_row("600519")], 1))

    client = _ScriptedClient([])
    client.get = fake_get  # type: ignore[method-assign]

    rows = await prov.fetch_full_a_share_snapshot(client, page_size=100)  # type: ignore[arg-type]

    assert len(rows) == 1
    assert secondary in hits[0] or secondary in hits[-1]


def test_default_page_size_respects_measured_api_cap() -> None:
    """clist 的 pz 实测上限为 100；默认页大小必须与之对齐（否则被静默截断）。"""
    assert prov.DEFAULT_PAGE_SIZE == 100


def test_parse_row_does_not_scale_prices() -> None:
    """回归防护：确认不存在任何 /100 缩放。"""
    row = prov.parse_eod_snapshot_row(_row("600519", f13=1, close=100.55))
    assert row is not None
    assert row.close == Decimal("100.55")


# =========================================================================
# 3. 市场分类
# =========================================================================


@pytest.mark.parametrize(
    ("symbol", "f13", "expected"),
    [
        ("600519", 1, "SH"),
        ("688981", 1, "SH"),      # 科创板
        ("000001", 0, "SZ"),      # 深市平安银行
        ("300750", 0, "SZ"),      # 创业板
        ("002594", 0, "SZ"),      # 中小板
        ("920001", 0, "BJ"),      # 新北交所（静态 BJ_STOCKS 之外）
        ("430047", 0, "BJ"),
        ("830799", 0, "BJ"),
        ("870204", 0, "BJ"),
        ("889999", 0, "BJ"),
    ],
)
def test_classify_a_share_market(symbol: str, f13: int, expected: str) -> None:
    assert prov.classify_a_share_market(symbol, f13) == expected


def test_classify_rejects_sh_index_code_collision() -> None:
    """上证指数 000001（f13=1）与平安银行 000001（f13=0）同码。

    若不拒绝，沪市指数会被当作深市股票写坏 000001 的行情。
    """
    with pytest.raises(ValueError, match="market identity mismatch"):
        prov.classify_a_share_market("000001", 1)
    # 深市 f13=0 时正常放行
    assert prov.classify_a_share_market("000001", 0) == "SZ"


@pytest.mark.parametrize(
    ("symbol", "f13"),
    [
        ("600519", 0),   # 沪市股票必须 f13=1
        ("688981", 0),   # 科创板同样 f13=1
        ("000001", 1),   # 深市股票必须 f13=0
        ("300750", 1),
        ("920001", 1),   # 北交所 f13 实测为 0（不是 2）
        ("920001", 2),
        ("430047", 2),
    ],
)
def test_classify_rejects_f13_mismatch(symbol: str, f13: int) -> None:
    """f13 与代码前缀不符时必须拒绝，不能猜。"""
    with pytest.raises(ValueError, match="market identity mismatch"):
        prov.classify_a_share_market(symbol, f13)


@pytest.mark.parametrize("bad_f13", [None, "", "x", "-", [], {}])
def test_classify_rejects_missing_or_invalid_f13(bad_f13: Any) -> None:
    """f13 缺失/不可解析 → fail-closed（外部源身份冲突时宁愿丢弃）。"""
    with pytest.raises(ValueError, match="market identity unknown"):
        prov.classify_a_share_market("600519", bad_f13)


def test_classify_rejects_non_stock_by_project_rule() -> None:
    """前缀+f13 都通过但项目规则判定非股票时（如位数不足的北交所代码）必须拒绝。"""
    with pytest.raises(ValueError, match="not A-share stock by project rule"):
        prov.classify_a_share_market("92001", 0)
    with pytest.raises(ValueError, match="not A-share stock by project rule"):
        prov.classify_a_share_market("60051", 1)


@pytest.mark.parametrize("symbol", ["399001", "510300", "159919", "900001", "200001", "123456", ""])
def test_classify_rejects_non_stock(symbol: str) -> None:
    with pytest.raises(ValueError):
        prov.classify_a_share_market(symbol, 0)


# =========================================================================
# 4. trade_date（fail-closed）
# =========================================================================


def test_snapshot_trade_date_uses_asia_shanghai() -> None:
    """f124 必须按 Asia/Shanghai 解释，而非 UTC。

    CST 2026-09-11 03:00 在 UTC 下是 09-10 19:00；若误用 UTC 会得到 09-10。
    """
    ts = _ts(2026, 9, 11, 3, 0)
    assert prov.snapshot_trade_date(ts) == date(2026, 9, 11)


def test_snapshot_trade_date_non_target_day_is_different() -> None:
    """昨日 15:00 的时间戳必须给出昨日，而不是被强行标成今天。"""
    assert prov.snapshot_trade_date(_ts(2026, 9, 10)) == date(2026, 9, 10)


@pytest.mark.parametrize("bad", [None, "", "-", "abc", 0, -1, [], {}])
def test_snapshot_trade_date_fail_closed(bad: Any) -> None:
    assert prov.snapshot_trade_date(bad) is None


# =========================================================================
# 5. 行归一化 / 单位
# =========================================================================


def test_parse_row_keeps_price_in_yuan_and_converts_lots_to_shares() -> None:
    """价格不得缩放（fltt=2 下已是元）；volume 手→股 ×100；amount=元 不缩放。

    历史教训：
    - 曾误按「分」把价格 ÷100，被外部实测推翻（见 test_eod_external_ab）。
    - 曾把 volume 直接当「手」入库，被 A/B 实测推翻：``bars_daily.volume``
      canonical 单位是**股**（600519 2026-09-09 DB=3_222_611 股，东财=32_226 手）。
      因此 f5 必须 ×100 转股，否则全市场成交量缩小 100 倍。
    """
    row = prov.parse_eod_snapshot_row(_row("600519", f13=1))
    assert row is not None
    assert row.market == "SH"
    assert row.trade_date == TARGET_DAY
    assert row.close == Decimal("12.34")
    assert row.open == Decimal("12.50")
    assert row.high == Decimal("13.00")
    assert row.low == Decimal("12.00")
    assert row.previous_close == Decimal("12.20")
    # f5=手 → ×100 转股；amount=元 不缩放
    assert row.volume == Decimal("100000") * prov.SHARES_PER_LOT
    assert row.amount == Decimal("123456789")


def test_parse_row_accepts_new_bj_stock_beyond_static_list() -> None:
    """920xxx 新北交所股票必须被接纳（静态 BJ_STOCKS 不能作为权威池）。"""
    row = prov.parse_eod_snapshot_row(_row("920819", name="某北交所新股", f13=0))
    assert row is not None
    assert row.market == "BJ"


def test_parse_row_rejects_index_and_etf() -> None:
    assert prov.parse_eod_snapshot_row(_row("399001", f13=0)) is None
    assert prov.parse_eod_snapshot_row(_row("510300", f13=1)) is None
    assert prov.parse_eod_snapshot_row(_row("000001", f13=1)) is None


def test_parse_row_suspended_stock_keeps_row_with_none_prices() -> None:
    """停牌 / 空价：归一化不抛异常，字段为 None（由落库层判废，不伪造 K 线）。"""
    row = prov.parse_eod_snapshot_row(
        _row("600519", f13=1, close="-", open_="-", high="-", low="-", volume="-", amount="-")
    )
    assert row is not None
    assert row.close is None
    assert row.volume is None
    assert row.trade_date == TARGET_DAY


def test_normalize_rows_drops_invalid_and_keeps_order() -> None:
    rows = prov.normalize_snapshot_rows(
        [
            _row("600519", f13=1),
            _row("399001", f13=0),
            _row("920001", f13=0),
            {"f12": ""},
        ]
    )
    assert [r.symbol for r in rows] == ["600519", "920001"]


# =========================================================================
# 6. 历史 kline（不复权）
# =========================================================================


@pytest.mark.asyncio
async def test_eastmoney_kline_requests_fqt_zero_and_parses() -> None:
    """历史 kline 必须显式 fqt=0（不复权），且价格不做 fltt 缩放。"""
    captured: dict[str, Any] = {}

    async def fake_get(url: str, params: Any = None, timeout: Any = None) -> _FakeResponse:
        captured["url"] = url
        captured["params"] = params or {}
        return _FakeResponse(
            {"data": {"klines": ["2026-09-10,10.00,10.50,10.80,9.90,12345,6789012"]}}
        )

    client = _ScriptedClient([])
    client.get = fake_get  # type: ignore[method-assign]

    recs = await prov.fetch_eastmoney_daily_kline(
        client, "000001", "SZ", date(2026, 9, 10), date(2026, 9, 11)  # type: ignore[arg-type]
    )

    assert captured["url"].endswith(prov._KLINE_PATH)  # noqa: SLF001
    assert captured["params"]["fqt"] == "0"
    assert captured["params"]["klt"] == "101"
    assert captured["params"]["secid"] == "0.000001"
    assert recs == [
        {
            "datetime": "2026-09-10",
            "open": 10.00,
            "close": 10.50,
            "high": 10.80,
            "low": 9.90,
            # f56 是「手」→ ×100 转股（canonical 单位）
            "volume": 12345.0 * float(prov.SHARES_PER_LOT),
            "amount": 6789012.0,
        }
    ]


@pytest.mark.asyncio
async def test_eastmoney_kline_empty_is_error_not_silent_success() -> None:
    """无数据必须抛错（不得当作「成功但空」）。"""

    async def fake_get(url: str, params: Any = None, timeout: Any = None) -> _FakeResponse:
        return _FakeResponse({"data": {"klines": []}})

    client = _ScriptedClient([])
    client.get = fake_get  # type: ignore[method-assign]

    with pytest.raises(prov.SnapshotProviderError, match="无数据"):
        await prov.fetch_eastmoney_daily_kline(
            client, "920819", "BJ", date(2026, 9, 1), date(2026, 9, 11)  # type: ignore[arg-type]
        )


def test_eastmoney_secid_market_prefixes() -> None:
    assert prov._eastmoney_secid("600519", "SH") == "1.600519"
    assert prov._eastmoney_secid("000001", "SZ") == "0.000001"


def test_eastmoney_secid_bj_is_zero_prefix() -> None:
    """北交所在东财统一行情下与深市同为市场 0。

    回归防护：曾写成 "2.920xxx"，导致北交所历史 K 线永远取不到数据。
    """
    assert prov._EM_SECID_MARKET["BJ"] == "0"
    assert prov._eastmoney_secid("920819", "BJ") == "0.920819"
    assert prov._eastmoney_secid("920002", "BJ") == "0.920002"


def test_eastmoney_secid_rejects_unknown_market() -> None:
    with pytest.raises(ValueError, match="unsupported market"):
        prov._eastmoney_secid("920819", "US")


# =========================================================================
# 7. 当日收盘窗口守卫（禁止盘中快照被当成收盘日线）
# =========================================================================


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 9, 11, 13, 0, tzinfo=_SH), False),   # 盘中 13:00
        (datetime(2026, 9, 11, 14, 59, tzinfo=_SH), False),  # 收盘前 1 分钟
        (datetime(2026, 9, 11, 15, 5, tzinfo=_SH), True),    # 结算余量之后
        (datetime(2026, 9, 11, 16, 0, tzinfo=_SH), True),
        (datetime(2026, 9, 12, 9, 0, tzinfo=_SH), False),    # 次日：不能用今日快照
        (datetime(2026, 9, 10, 16, 0, tzinfo=_SH), False),   # 过去日：不能用今日快照
    ],
)
def test_can_use_same_day_eod_snapshot(when: datetime, expected: bool) -> None:
    assert prov.can_use_same_day_eod_snapshot(date(2026, 9, 11), now=when) is expected


def test_can_use_same_day_eod_snapshot_treats_naive_as_shanghai() -> None:
    """naive datetime 必须按上海时区解释，不得当成 UTC（否则边界日会判错）。"""
    naive_intraday = datetime(2026, 9, 11, 13, 0)   # naive
    assert prov.can_use_same_day_eod_snapshot(date(2026, 9, 11), now=naive_intraday) is False
    naive_after_close = datetime(2026, 9, 11, 16, 0)
    assert prov.can_use_same_day_eod_snapshot(date(2026, 9, 11), now=naive_after_close) is True


def test_can_use_same_day_eod_snapshot_converts_utc_input() -> None:
    """UTC 输入必须换算到上海时区后再比较（03:00 UTC = 11:00 CST → 拒绝）。"""
    from datetime import UTC

    utc_1100 = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)
    assert prov.can_use_same_day_eod_snapshot(date(2026, 9, 11), now=utc_1100) is False
    utc_1600 = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)  # = 16:00 CST
    assert prov.can_use_same_day_eod_snapshot(date(2026, 9, 11), now=utc_1600) is True


# =========================================================================
# 8. updated_at / trade_date 投影
# =========================================================================


def test_snapshot_updated_at_keeps_full_time_not_just_date() -> None:
    """必须保留完整时间：只存日期就无法判断是否已收盘（watermark 依赖它）。"""
    ts = _ts(2026, 9, 11, 14, 50)
    got = prov.snapshot_updated_at(ts)
    assert got is not None
    assert got.hour == 14 and got.minute == 50
    assert got.tzinfo is not None
    assert prov.snapshot_trade_date(ts) == date(2026, 9, 11)


@pytest.mark.parametrize("bad", [None, "", "abc", 0, -1, "0"])
def test_snapshot_updated_at_fail_closed_on_bad_input(bad: Any) -> None:
    assert prov.snapshot_updated_at(bad) is None
    assert prov.snapshot_trade_date(bad) is None


def test_eod_snapshot_row_trade_date_is_projection_of_updated_at() -> None:
    """DTO 的 trade_date 必须由 updated_at 派生，避免两份时间字段漂移。"""
    row = prov.parse_eod_snapshot_row(_row("600519", f13=1, f124=_ts(2026, 9, 11, 15, 3)))
    assert row is not None
    assert row.trade_date == date(2026, 9, 11)
    assert row.updated_at is not None and row.updated_at.hour == 15


def test_eod_snapshot_row_trade_date_none_when_timestamp_missing() -> None:
    """f124 缺失 → updated_at/trade_date 均为 None（由落库层判废，不伪造 K 线）。"""
    row = prov.parse_eod_snapshot_row(_row("600519", f13=1, f124=None))
    assert row is not None
    assert row.updated_at is None
    assert row.trade_date is None


# =========================================================================
# 9. market watermark（数据自身必须证明已收盘）
# =========================================================================


def test_watermark_accepts_at_or_after_close() -> None:
    rows = [_row("600519", f13=1, f124=_ts(2026, 9, 11, 15, 0))]
    wm = prov.validate_snapshot_market_watermark(rows, date(2026, 9, 11))
    assert wm.time() >= prov._MARKET_CLOSE_TIME  # noqa: SLF001


def test_watermark_rejects_intraday_even_when_wall_clock_past_1505() -> None:
    """延时源在 15:05 仍只给 14:50 → 必须拒绝；本机时间到了不算数。"""
    rows = [_row("600519", f13=1, f124=_ts(2026, 9, 11, 14, 50))]
    with pytest.raises(prov.SnapshotProviderError, match="snapshot is not final"):
        prov.validate_snapshot_market_watermark(rows, date(2026, 9, 11))


def test_watermark_rejects_when_no_target_date_timestamps() -> None:
    """全是老时间戳（例如全市场都还是上一交易日）→ 必须拒绝。"""
    rows = [_row("600519", f13=1, f124=_ts(2026, 9, 10, 15, 0))]
    with pytest.raises(prov.SnapshotProviderError, match="no valid timestamps"):
        prov.validate_snapshot_market_watermark(rows, date(2026, 9, 11))


def test_watermark_uses_max_across_rows_not_first() -> None:
    """watermark 取全体最大值：少量滞后行不得把整批判为盘中。"""
    rows = [
        _row("600519", f13=1, f124=_ts(2026, 9, 11, 14, 50)),
        _row("000001", f124=_ts(2026, 9, 11, 15, 2)),
    ]
    wm = prov.validate_snapshot_market_watermark(rows, date(2026, 9, 11))
    assert wm.minute == 2


@pytest.mark.asyncio
async def test_snapshot_failover_rejects_host_with_stale_watermark() -> None:
    """hostA 数据只到 14:50 → 整批作废并换 hostB；hostB 达标才返回。"""
    host_a, host_b = prov.EASTMONEY_CLIST_HOSTS[0], prov.EASTMONEY_CLIST_HOSTS[1]
    stale = [_row("600519", f13=1, f124=_ts(2026, 9, 11, 14, 50))]
    fresh = [_row("600519", f13=1, f124=_ts(2026, 9, 11, 15, 2))]
    client = _HostScriptedClient(
        {
            (host_a, 1): _page(stale, 1),
            (host_b, 1): _page(fresh, 1),
        }
    )

    rows = await prov.fetch_full_a_share_snapshot(
        client,  # type: ignore[arg-type]
        expected_trade_date=date(2026, 9, 11),
        page_size=100,
    )

    assert len(rows) == 1
    assert rows[0]["f124"] == fresh[0]["f124"]
    assert (host_a, 1) in client.calls and (host_b, 1) in client.calls


@pytest.mark.asyncio
async def test_snapshot_all_hosts_stale_watermark_fails_closed() -> None:
    """所有 host 都只到 14:50 → 整体 fail-closed，绝不能把盘中价当日线。"""
    stale = [_row("600519", f13=1, f124=_ts(2026, 9, 11, 14, 50))]
    client = _HostScriptedClient(
        {(_h, 1): _page(stale, 1) for _h in prov.EASTMONEY_CLIST_HOSTS}
    )

    with pytest.raises(prov.SnapshotProviderError, match="not final"):
        await prov.fetch_full_a_share_snapshot(
            client,  # type: ignore[arg-type]
            expected_trade_date=date(2026, 9, 11),
            page_size=100,
        )


# =========================================================================
# 13. 非法参数必须在任何网络请求之前 fail-fast（zero network I/O）
# =========================================================================


class _NoNetworkClient:
    """任何 get 调用都视为违规；用于证明参数错误不会触发网络。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def get(self, url: str, params: Any = None, timeout: Any = None) -> Any:
        self.calls.append((url, params))
        raise AssertionError("invalid parameter must fail before network access")


@pytest.mark.asyncio
async def test_eod_watermark_requires_trade_date_before_any_network() -> None:
    """require_eod_watermark=True 但 expected_trade_date=None 属于调用侧参数错误：
    必须在任何 host / 网络请求之前 fail-fast（否则会白白拉 ~53 页 × N hosts）。"""
    client = _NoNetworkClient()

    with pytest.raises(
        prov.SnapshotProviderError,
        match="expected_trade_date required for EOD watermark validation",
    ):
        await prov.fetch_a_share_snapshot_batch(
            client,  # type: ignore[arg-type]
            hosts=("host-a", "host-b"),
            require_eod_watermark=True,
            expected_trade_date=None,
        )

    assert client.calls == []


@pytest.mark.asyncio
async def test_empty_host_list_fails_before_any_network() -> None:
    """空 host 列表同样属于参数错误：zero network I/O。"""
    client = _NoNetworkClient()

    with pytest.raises(prov.SnapshotProviderError, match="snapshot host list is empty"):
        await prov.fetch_a_share_snapshot_batch(
            client,  # type: ignore[arg-type]
            hosts=(),
        )

    assert client.calls == []
