"""PytdxAdapter 统一 provider 边界（Stage A）纯单元测试。

覆盖 PytdxAdapter 成为唯一 socket owner 后的合同：

A. ``get_security_bars`` 首次 "calling function error" → disconnect → reconnect → 成功
B. 永久 source error → bounded calls → typed ``PytdxSourceError``（含 operation/symbol/period/server）
C. 成功 empty bars → 空 DataFrame，**不得**抛 source error（legitimate empty）
D. ``get_security_quotes`` 首次 source failure → reconnect → 成功
E. ``get_security_quotes`` 永久失败 → ``PytdxSourceError``
F. ``get_realtime_quote`` 合法 empty → ``None``；source failure → ``PytdxSourceError``
G. auction provider 只调用 ``adapter.get_security_quotes``（不再访问 ``.api``）
H. XDXR：empty = legitimate empty；source failure = typed error
I. 无嵌套重试放大：一次业务调用的底层 API 调用次数 == ``max_retries``（不存在 3×3）

不连接网络 / 不连 Redis / 不连 DB。
用法：PURE_UNIT_TEST=1 pytest tests/test_pytdx_source_boundary.py -q
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.pytdx_adapter import PytdxAdapter, PytdxSourceError

pytestmark = pytest.mark.pure_unit

_SERVER = ("127.0.0.1", 7709)


def _install_api(adapter: PytdxAdapter, api: MagicMock) -> None:
    """把 adapter 置于「已连接」态，并让 reconnect 复用同一 mock api。

    绕过真实 socket：一旦 ``disconnect`` 把 ``_api`` 置空，下一轮 ``connect``
    会重新注入同一 mock，从而可断言底层 API 的真实调用次数。
    """
    adapter._api = api  # type: ignore[assignment]
    adapter.connected_server = _SERVER

    def _connect(*_args: object, **_kwargs: object) -> None:
        # 忽略 excluded_servers：本文件故意复用同一 mock server，
        # 以便断言「底层 API 调用次数」这一契约（failover 由
        # tests/test_pytdx_server_failover.py 用多 host Fake API 覆盖）。
        adapter._api = api  # type: ignore[assignment]
        adapter.connected_server = _SERVER

    def _disconnect() -> None:
        adapter._api = None
        adapter.connected_server = None

    adapter.connect = _connect  # type: ignore[method-assign]
    # retry owner 走 _connect_excluding：必须一并 patch，否则会落到真实网络
    adapter._connect_excluding = _connect  # type: ignore[method-assign]
    adapter.disconnect = _disconnect  # type: ignore[method-assign]


def _bar(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "datetime": "2026-09-11 15:00:00",
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "vol": 1000.0,
        "amount": 10200.0,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# A / B / C — get_security_bars
# ---------------------------------------------------------------------------


def test_a_bars_reconnects_after_source_error() -> None:
    adapter = PytdxAdapter(max_retries=3, retry_delay=0)
    api = MagicMock()
    api.get_security_bars.side_effect = [
        RuntimeError("calling function error"),
        [_bar()],
    ]
    _install_api(adapter, api)

    df = adapter._fetch_bars("000001", "d", 10)

    assert api.get_security_bars.call_count == 2  # 1 失败 + 1 重连成功
    assert not df.empty
    assert list(df.columns) == [
        "datetime", "open", "high", "low", "close", "volume", "amount",
    ]


def test_b_permanent_error_is_bounded_and_typed() -> None:
    adapter = PytdxAdapter(max_retries=3, retry_delay=0)
    api = MagicMock()
    api.get_security_bars.side_effect = RuntimeError("calling function error")
    _install_api(adapter, api)

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_bars("000610", "d", 10)

    # bounded：恰好 max_retries，绝不放大
    assert api.get_security_bars.call_count == 3

    err = ei.value
    assert err.operation == "get_security_bars"
    assert err.symbol == "000610"
    assert err.period == "d"
    assert err.attempt == 3
    assert err.server == _SERVER
    assert err.cause is not None
    assert "operation=get_security_bars" in str(err)
    assert "symbol=000610" in str(err)


def test_c_success_empty_bars_is_legitimate_empty() -> None:
    adapter = PytdxAdapter(max_retries=3, retry_delay=0)
    api = MagicMock()
    api.get_security_bars.return_value = []
    _install_api(adapter, api)

    df = adapter._fetch_bars("000001", "d", 10)

    assert df.empty
    assert api.get_security_bars.call_count == 1  # 合法空不触发重试


# ---------------------------------------------------------------------------
# D / E — get_security_quotes public API
# ---------------------------------------------------------------------------


def test_d_quotes_reconnects_after_source_error() -> None:
    adapter = PytdxAdapter(max_retries=3, retry_delay=0)
    api = MagicMock()
    api.get_security_quotes.side_effect = [
        RuntimeError("socket dropped"),
        [{"market": 0, "code": "000001", "price": 10.0}],
    ]
    _install_api(adapter, api)

    rows = adapter.get_security_quotes(["000001"])

    assert api.get_security_quotes.call_count == 2
    assert rows and rows[0]["code"] == "000001"


def test_e_quotes_permanent_error_raises() -> None:
    adapter = PytdxAdapter(max_retries=2, retry_delay=0)
    api = MagicMock()
    api.get_security_quotes.side_effect = RuntimeError("calling function error")
    _install_api(adapter, api)

    with pytest.raises(PytdxSourceError) as ei:
        adapter.get_security_quotes(["000001"])

    assert api.get_security_quotes.call_count == 2
    assert ei.value.operation == "get_security_quotes"


def test_e_empty_symbols_returns_empty_without_call() -> None:
    adapter = PytdxAdapter(max_retries=1, retry_delay=0)
    api = MagicMock()
    _install_api(adapter, api)

    assert adapter.get_security_quotes([]) == []
    api.get_security_quotes.assert_not_called()


# ---------------------------------------------------------------------------
# F — get_realtime_quote（不再用 1m 伪装）
# ---------------------------------------------------------------------------


def test_f_realtime_quote_empty_returns_none() -> None:
    adapter = PytdxAdapter(max_retries=2, retry_delay=0)
    api = MagicMock()
    api.get_security_quotes.return_value = []
    _install_api(adapter, api)

    assert adapter.get_realtime_quote("000001") is None


def test_f_realtime_quote_source_failure_raises() -> None:
    adapter = PytdxAdapter(max_retries=2, retry_delay=0)
    api = MagicMock()
    api.get_security_quotes.side_effect = RuntimeError("calling function error")
    _install_api(adapter, api)

    with pytest.raises(PytdxSourceError):
        adapter.get_realtime_quote("000001")


def test_f_realtime_quote_success_maps_fields() -> None:
    adapter = PytdxAdapter(max_retries=1, retry_delay=0)
    api = MagicMock()
    api.get_security_quotes.return_value = [
        {
            "market": 0,
            "code": "000001",
            "price": 10.5,
            "open": 10.0,
            "high": 10.8,
            "low": 9.9,
            "last_close": 10.0,
            "vol": 1234.0,
            "amount": 12957.0,
            "servertime": "14:59:59",
        }
    ]
    _install_api(adapter, api)

    quote = adapter.get_realtime_quote("000001")

    assert quote is not None
    assert quote["current_price"] == 10.5
    assert quote["close"] == 10.5
    assert quote["prev_close"] == 10.0
    assert quote["amount"] == 12957.0
    assert quote["is_realtime"] is True
    assert quote["change_pct"] == pytest.approx(5.0, abs=0.01)
    # captured_at / update_time 为本地上海时区抓取时间，语义必须显式标注
    assert quote["captured_at"].startswith("2026-")
    assert quote["update_time"] == quote["captured_at"]
    assert quote["update_time_semantics"] == "local_capture_time"
    # 不再用 1m/daily 伪装 quote
    api.get_security_bars.assert_not_called()


def test_f_realtime_quote_prev_close_unavailable_yields_none_pct() -> None:
    """prev_close 缺失/为 0 时 change_pct 必须是 None，不得伪造 0%。"""
    adapter = PytdxAdapter(max_retries=1, retry_delay=0)
    api = MagicMock()
    api.get_security_quotes.return_value = [
        {"market": 0, "code": "000001", "price": 10.5, "last_close": 0.0}
    ]
    _install_api(adapter, api)

    quote = adapter.get_realtime_quote("000001")

    assert quote is not None
    assert quote["change_pct"] is None


def test_f_realtime_quote_non_positive_price_returns_none() -> None:
    adapter = PytdxAdapter(max_retries=1, retry_delay=0)
    api = MagicMock()
    api.get_security_quotes.return_value = [
        {"market": 0, "code": "000001", "price": 0.0}
    ]
    _install_api(adapter, api)

    assert adapter.get_realtime_quote("000001") is None


# ---------------------------------------------------------------------------
# G — auction provider 只走 public API
# ---------------------------------------------------------------------------


def test_g_auction_provider_uses_public_api_only() -> None:
    from app.services.auction_quote_provider import MootdxAuctionQuoteProvider

    provider = MootdxAuctionQuoteProvider()
    fake_adapter = MagicMock()
    fake_adapter._servers = [("127.0.0.1", 7709)]
    fake_adapter.connected_server = ("127.0.0.1", 7709)
    fake_adapter.get_security_quotes.return_value = [
        {
            "market": 0,
            "code": "000001",
            "price": 10.0,
            "open": 9.9,
            "high": 10.1,
            "low": 9.8,
            "last_close": 9.5,
            "vol": 1000.0,
            "amount": 10000.0,
            "servertime": "9:25:5",
        }
    ]
    provider._adapter = fake_adapter
    provider._connected = True

    results = provider.fetch_auction_quotes([("000001", "SZ")])

    fake_adapter.get_security_quotes.assert_called_once_with(["000001"])
    assert len(results) == 1
    assert results[0].symbol == "000001"
    assert results[0].quality_status == "ok"


# ---------------------------------------------------------------------------
# H — XDXR empty vs source failure
# ---------------------------------------------------------------------------


def test_h_xdxr_empty_is_legitimate_empty() -> None:
    adapter = PytdxAdapter(max_retries=2, retry_delay=0)
    api = MagicMock()
    api.get_xdxr_info.return_value = []
    _install_api(adapter, api)

    df = adapter._fetch_xdxr_from_pytdx("600519")

    assert df.empty
    assert api.get_xdxr_info.call_count == 1


def test_h_xdxr_source_failure_is_typed() -> None:
    adapter = PytdxAdapter(max_retries=2, retry_delay=0)
    api = MagicMock()
    api.get_xdxr_info.side_effect = RuntimeError("calling function error")
    _install_api(adapter, api)

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_xdxr_from_pytdx("600519")

    assert ei.value.operation == "get_xdxr_info"
    assert api.get_xdxr_info.call_count == 2


# ---------------------------------------------------------------------------
# I — 无嵌套重试放大
# ---------------------------------------------------------------------------


def test_i_no_nested_retry_amplification() -> None:
    """一次业务调用（get_daily_bars）的底层 API 调用次数 == max_retries，无 3×3。"""
    adapter = PytdxAdapter(max_retries=3, retry_delay=0)
    api = MagicMock()
    api.get_security_bars.side_effect = RuntimeError("boom")
    _install_api(adapter, api)

    with pytest.raises(PytdxSourceError):
        adapter.get_daily_bars("000001", date(2026, 9, 1), date(2026, 9, 10))

    assert api.get_security_bars.call_count == 3  # 而非 9
