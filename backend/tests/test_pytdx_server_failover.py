"""PytdxAdapter 服务器 failover 测试（Stage A fix）。

上一版 ``_install_api()`` 每次 reconnect 都装回**同一个** mock server，只能证明
「断线后调用了 reconnect」，不能证明「失败服务器被避开、下一次真的换 host」。
本文件用**多 host Fake API** 真正证明：

    A: get_security_bars → calling function error
        ↓ advance
    B: get_security_bars → 成功

并且通过 Fake API 构造参数断言 **pytdx 内建 auto_retry 已被关闭**
（PytdxAdapter 必须是唯一 retry owner；否则真实网络尝试次数会是
adapter_retries × pytdx_auto_retries，而 MagicMock 测试看不见）。

不连接网络 / 不连 DB。
用法：PURE_UNIT_TEST=1 pytest tests/test_pytdx_server_failover.py -q
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core import pytdx_adapter as pytdx_adapter_module
from app.core.pytdx_adapter import PytdxAdapter, PytdxSourceError

pytestmark = pytest.mark.pure_unit

_BAR_ROW: dict[str, Any] = {
    "datetime": "2026-09-11 15:00:00",
    "open": 10.0,
    "high": 10.5,
    "low": 9.8,
    "close": 10.2,
    "vol": 1000.0,
    "amount": 10200.0,
}

_QUOTE_ROW: dict[str, Any] = {
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

_SERVERS = [("server-a", 7709), ("server-b", 7709)]


def _make_fake_api(created: list[Any], *, failing_host: str):
    """构造 FakeApi 类：failing_host 上的 operation 一律源失败，其他 host 成功。"""

    class FakeApi:
        def __init__(self, *, raise_exception: bool, auto_retry: bool) -> None:
            # 唯一 retry owner 契约：禁止 pytdx 自己再做隐藏 auto retry
            assert raise_exception is True, "raise_exception 必须为 True"
            assert auto_retry is False, "auto_retry 必须为 False（PytdxAdapter 是唯一 retry owner）"
            self.host: str | None = None
            created.append(self)

        def connect(self, host: str, port: int, time_out: float) -> bool:
            self.host = host
            return True

        def disconnect(self) -> None:
            return None

        def get_security_bars(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if self.host == failing_host:
                raise RuntimeError("calling function error")
            return [dict(_BAR_ROW)]

        def get_security_quotes(self, requests: Any) -> list[dict[str, Any]]:
            if self.host == failing_host:
                raise RuntimeError("calling function error")
            return [dict(_QUOTE_ROW)]

    return FakeApi


# ---------------------------------------------------------------------------
# P0-1 — source failure 必须换 host（A → B，而不是 A → A）
# ---------------------------------------------------------------------------


def test_source_error_rotates_to_next_server(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    monkeypatch.setattr(
        pytdx_adapter_module, "TdxHq_API", _make_fake_api(created, failing_host="server-a")
    )

    adapter = PytdxAdapter(servers=_SERVERS, max_retries=2, retry_delay=0)
    df = adapter._fetch_bars("000001", "d", 10)

    assert not df.empty
    # 失败后必须落到下一个 host，而不是重连同一台
    assert adapter.connected_server == ("server-b", 7709)
    assert [api.host for api in created] == ["server-a", "server-b"]


def test_normal_disconnect_does_not_rotate(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常主动 close/disconnect 不是 source failure，不应推进 server index。"""
    hosts: list[str] = []

    class FakeApi:
        def __init__(self, *, raise_exception: bool, auto_retry: bool) -> None:
            self.host: str | None = None

        def connect(self, host: str, port: int, time_out: float) -> bool:
            self.host = host
            hosts.append(host)
            return True

        def disconnect(self) -> None:
            return None

    monkeypatch.setattr(pytdx_adapter_module, "TdxHq_API", FakeApi)

    adapter = PytdxAdapter(servers=_SERVERS)
    adapter.connect()
    assert adapter.connected_server == ("server-a", 7709)
    adapter.disconnect()
    adapter.connect()
    # 仍从 server-a 开始（未 rotate）
    assert adapter.connected_server == ("server-a", 7709)
    assert hosts == ["server-a", "server-a"]


# ---------------------------------------------------------------------------
# P0-1 / P0-2 — 全 server pool 连接失败必须 bounded（不得 servers × max_retries）
# ---------------------------------------------------------------------------


def test_total_connect_outage_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个 server 全部 connect 失败时，即使 max_retries=3，TCP connect 只允许 2 次。"""
    attempts: list[str] = []

    class DeadApi:
        def __init__(self, *, raise_exception: bool, auto_retry: bool) -> None:
            assert auto_retry is False
            self.host: str | None = None

        def connect(self, host: str, port: int, time_out: float) -> bool:
            attempts.append(host)
            self.host = host
            return False

        def disconnect(self) -> None:
            return None

    monkeypatch.setattr(pytdx_adapter_module, "TdxHq_API", DeadApi)

    adapter = PytdxAdapter(servers=_SERVERS, max_retries=3, retry_delay=0)

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_bars("000001", "d", 10)

    # 完整 pool 失败一轮即停止：2 次，而不是 2 × 3 = 6
    assert attempts == ["server-a", "server-b"]

    err = ei.value
    assert err.operation == "get_security_bars"
    assert err.cause is not None
    assert err.cause.operation == "connect"


def test_total_connect_outage_with_raising_connect_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect 抛异常（而非返回 False）时同样 bounded。"""
    attempts: list[str] = []

    class RaisingApi:
        def __init__(self, *, raise_exception: bool, auto_retry: bool) -> None:
            self.host: str | None = None

        def connect(self, host: str, port: int, time_out: float) -> bool:
            attempts.append(host)
            self.host = host
            raise RuntimeError("tcp refused")

        def disconnect(self) -> None:
            return None

    monkeypatch.setattr(pytdx_adapter_module, "TdxHq_API", RaisingApi)

    adapter = PytdxAdapter(servers=_SERVERS, max_retries=3, retry_delay=0)

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_bars("000001", "d", 10)

    assert attempts == ["server-a", "server-b"]
    assert ei.value.operation == "get_security_bars"
    assert ei.value.cause is not None
    assert ei.value.cause.operation == "connect"


# ---------------------------------------------------------------------------
# P1 — auction 必须记录实际成功的 server
# ---------------------------------------------------------------------------


def test_auction_source_server_is_actual_connected_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 上 quotes 源失败 → failover 到 B 成功 → source_server 必须是 server-b。"""
    from app.services.auction_quote_provider import MootdxAuctionQuoteProvider

    created: list[Any] = []
    monkeypatch.setattr(
        pytdx_adapter_module,
        "TdxHq_API",
        _make_fake_api(created, failing_host="server-a"),
    )

    adapter = PytdxAdapter(servers=_SERVERS, max_retries=2, retry_delay=0)
    provider = MootdxAuctionQuoteProvider()
    provider._adapter = adapter
    provider._connected = False  # 触发 _ensure_connected → adapter.connect()

    results = provider.fetch_auction_quotes([("000001", "SZ")])

    assert len(results) == 1
    assert results[0].symbol == "000001"
    assert results[0].quality_status == "ok"
    # 不得再用 servers[0] 伪造
    assert results[0].source_server == "server-b:7709"
    assert [api.host for api in created] == ["server-a", "server-b"]


def test_auction_source_server_prefers_connected_server_over_list_head() -> None:
    """即使 adapter 停在非首台 server，也必须以 connected_server 为准。"""
    from app.services.auction_quote_provider import MootdxAuctionQuoteProvider

    provider = MootdxAuctionQuoteProvider()
    fake_adapter = MagicMock()
    fake_adapter._servers = [("server-a", 7709), ("server-b", 7709)]
    fake_adapter.connected_server = ("server-b", 7709)
    fake_adapter.get_security_quotes.return_value = [dict(_QUOTE_ROW)]
    provider._adapter = fake_adapter
    provider._connected = True

    results = provider.fetch_auction_quotes([("000001", "SZ")])

    assert results[0].source_server == "server-b:7709"
