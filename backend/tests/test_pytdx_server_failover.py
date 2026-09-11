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

import threading
from pathlib import Path
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


def _fake_api_factory(
    *,
    connect_ok: dict[str, bool],
    api_fail_hosts: set[str],
    connect_calls: list[str],
    operation_calls: list[str],
):
    """按 host 配置的 FakeApi：可分别控制 TCP 可达性与 API 是否源失败。

    TCP 不可达（connect 返回 False）与「TCP 可达但 API 源失败」是两类不同的故障，
    本工厂用于区分并断言二者都不会导致回退到已证明有问题的 host。
    """

    class FakeApi:
        def __init__(self, *, raise_exception: bool, auto_retry: bool) -> None:
            assert raise_exception is True
            assert auto_retry is False, "auto_retry 必须为 False（PytdxAdapter 是唯一 retry owner）"
            self.host: str | None = None

        def connect(self, host: str, port: int, time_out: float) -> bool:
            connect_calls.append(host)
            self.host = host
            return connect_ok.get(host, False)

        def disconnect(self) -> None:
            return None

        def get_security_bars(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            operation_calls.append(self.host)
            if self.host in api_fail_hosts:
                raise RuntimeError("calling function error")
            return [dict(_BAR_ROW)]

    return FakeApi


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
# 边界：本次 retry cycle 内不得重用已 source-failed 的 host
# ---------------------------------------------------------------------------


def test_source_failed_host_not_reused_when_others_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A API 源失败，B/C TCP 全断线 → 绝不能绕回 A 再发一次 API。"""
    connect_attempts: list[str] = []
    operation_hosts: list[str] = []

    monkeypatch.setattr(
        pytdx_adapter_module,
        "TdxHq_API",
        _fake_api_factory(
            connect_ok={"server-a": True, "server-b": False, "server-c": False},
            api_fail_hosts={"server-a"},
            connect_calls=connect_attempts,
            operation_calls=operation_hosts,
        ),
    )

    servers = [("server-a", 7709), ("server-b", 7709), ("server-c", 7709)]
    adapter = PytdxAdapter(servers=servers, max_retries=5, retry_delay=0)

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_bars("000001", "d", 10)

    # A 上只真正发过一次 API
    assert operation_hosts == ["server-a"]
    # 第二轮只尝试 B / C 的 TCP，绝不再次 connect A
    assert connect_attempts == ["server-a", "server-b", "server-c"]

    assert ei.value.operation == "get_security_bars"
    assert ei.value.cause is not None
    assert ei.value.cause.operation == "connect"


def test_rotates_a_then_b_then_c(monkeypatch: pytest.MonkeyPatch) -> None:
    """A/B 连续 API 失败 → 严格 A → B → C，不得出现 A,B,A 或 A,A,B。"""
    connect_attempts: list[str] = []
    operation_hosts: list[str] = []

    monkeypatch.setattr(
        pytdx_adapter_module,
        "TdxHq_API",
        _fake_api_factory(
            connect_ok={"server-a": True, "server-b": True, "server-c": True},
            api_fail_hosts={"server-a", "server-b"},
            connect_calls=connect_attempts,
            operation_calls=operation_hosts,
        ),
    )

    servers = [("server-a", 7709), ("server-b", 7709), ("server-c", 7709)]
    adapter = PytdxAdapter(servers=servers, max_retries=3, retry_delay=0)

    df = adapter._fetch_bars("000001", "d", 10)

    assert not df.empty
    assert operation_hosts == ["server-a", "server-b", "server-c"]
    assert connect_attempts == ["server-a", "server-b", "server-c"]
    assert adapter.connected_server == ("server-c", 7709)


def test_all_hosts_source_failure_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A/B/C 全部 API 源失败时，即使 max_retries=10，API 也只调用 3 次（不得绕回 A）。"""
    connect_attempts: list[str] = []
    operation_hosts: list[str] = []

    monkeypatch.setattr(
        pytdx_adapter_module,
        "TdxHq_API",
        _fake_api_factory(
            connect_ok={"server-a": True, "server-b": True, "server-c": True},
            api_fail_hosts={"server-a", "server-b", "server-c"},
            connect_calls=connect_attempts,
            operation_calls=operation_hosts,
        ),
    )

    servers = [("server-a", 7709), ("server-b", 7709), ("server-c", 7709)]
    adapter = PytdxAdapter(servers=servers, max_retries=10, retry_delay=0)

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_bars("000001", "d", 10)

    # 3 次，而不是 max_retries=10 下的 A→B→C→A→...
    assert operation_hosts == ["server-a", "server-b", "server-c"]
    assert connect_attempts == ["server-a", "server-b", "server-c"]

    assert ei.value.operation == "get_security_bars"
    assert ei.value.cause is not None
    assert ei.value.cause.operation == "connect"


# ---------------------------------------------------------------------------
# 并发：单例 adapter 共享 socket 的 excluded-host 竞态（确定性调度，非靠时间运气）
# ---------------------------------------------------------------------------


def test_concurrent_thread_cannot_reuse_excluded_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """别的线程重新连上 A 后，T1 绝不能在自己已 excluded 的 A 上再次执行 API。

    旧实现里 ``if self._api is None: _connect_excluding(...)`` 与 ``call(self.api)``
    不在同一临界区：T1 判定 A source failure → 锁内 disconnect → 锁外 sleep 期间，
    T2 可能连上 A 并留下共享 socket；T1 恢复后看到 ``_api is not None`` 就直接复用 A。

    本测试用 Event 精确调度（monkeypatch time.sleep），不依赖调度器运气：

        T1 attempt1：A 上 bars 失败 → excluded={A} → 锁内 disconnect → 锁外 sleep
        ↓ sleep 钩子放行 T2
        T2：B 连不上 → 环形绕回 A → quotes 在 A 成功 → 留下共享 socket=A
        ↓
        T1 attempt2：必须识别 A ∈ excluded → 改连 B
    """
    bar_hosts: list[str] = []
    quote_hosts: list[str] = []
    connect_ok = {"server-a": True, "server-b": True}

    class FakeApi:
        def __init__(self, *, raise_exception: bool, auto_retry: bool) -> None:
            assert auto_retry is False, "auto_retry 必须为 False"
            self.host: str | None = None

        def connect(self, host: str, port: int, time_out: float) -> bool:
            self.host = host
            return connect_ok.get(host, False)

        def disconnect(self) -> None:
            return None

        def get_security_bars(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            bar_hosts.append(self.host or "?")
            if self.host == "server-a":
                raise RuntimeError("calling function error")
            return [dict(_BAR_ROW)]

        def get_security_quotes(self, requests: Any) -> list[dict[str, Any]]:
            quote_hosts.append(self.host or "?")
            return [dict(_QUOTE_ROW)]

    monkeypatch.setattr(pytdx_adapter_module, "TdxHq_API", FakeApi)

    t1_waiting = threading.Event()
    t2_done = threading.Event()

    class _TimeShim:
        """只替换 time.sleep，其余属性转发给真实 time 模块。"""

        def __init__(self, real: Any) -> None:
            self._real = real

        def sleep(self, seconds: float) -> None:
            # T1 在两次 attempt 之间等待（锁已释放）：此刻让 T2 抢占并建立共享 socket
            t1_waiting.set()
            assert t2_done.wait(timeout=10), "T2 未能在超时内完成"

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    monkeypatch.setattr(
        pytdx_adapter_module, "time", _TimeShim(pytdx_adapter_module.time)
    )

    adapter = PytdxAdapter(servers=_SERVERS, max_retries=2, retry_delay=0)

    errors: list[BaseException] = []
    t2_errors: list[BaseException] = []

    def _t1() -> None:
        try:
            adapter._fetch_bars("000001", "d", 10)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _t2() -> None:
        try:
            # connect_ok["server-b"]=False 期间：扫描从 index1(B) 失败 → 绕回 index0(A)
            adapter.get_security_quotes(["000001"])
        except BaseException as exc:  # noqa: BLE001
            t2_errors.append(exc)

    t1 = threading.Thread(target=_t1, name="t1-bars")
    t1.start()

    # 等 T1 完成 attempt1 并进入 sleep（临界区已退出）
    assert t1_waiting.wait(timeout=10), "T1 未能在超时内进入 retry 等待"

    connect_ok["server-b"] = False
    t2 = threading.Thread(target=_t2, name="t2-quotes")
    t2.start()
    t2.join(timeout=10)
    connect_ok["server-b"] = True
    t2_done.set()

    t1.join(timeout=15)
    assert not t1.is_alive(), "T1 未能在超时内结束"
    assert not errors, f"T1 不应抛异常：{errors}"
    assert not t2_errors, f"T2 不应抛异常：{t2_errors}"

    # T2 确实在 A 上成功，并留下了共享 socket=A
    assert quote_hosts == ["server-a"], f"T2 应在 A 上成功，实际 {quote_hosts}"
    # 核心断言：T1 只在 A 上执行过一次 bars，不得复用自己已 excluded 的 A
    assert bar_hosts.count("server-a") == 1, (
        f"T1 不得在已 excluded 的 A 上重复执行：{bar_hosts}"
    )
    assert bar_hosts == ["server-a", "server-b"], f"T1 应 A→B，实际 {bar_hosts}"


def test_failover_tests_pin_explicit_server_list() -> None:
    """防守：本文件不得依赖真实默认 server list。

    上一轮曾因 patch 不完整落到真实 pytdx 服务器；这里把两条约定固化下来：
      1. 本文件不得引用 adapter 模块的真实默认 server list 常量；
      2. 每次 adapter 构造都必须显式指定 servers=。

    注意：下面的检测字符串一律用拼接构造，避免守卫自身源码被扫描命中。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    # 拼接构造：避免本守卫自身的断言被扫描命中
    real_list_marker = "PYTDX" + "_SERVERS"
    assert real_list_marker not in src, "不得使用真实默认 server list"

    # 拼接构造，避免本守卫自身被下面的扫描命中
    marker = "Pytdx" + "Adapter("
    constructions = 0
    idx = 0
    while True:
        idx = src.find(marker, idx)
        if idx == -1:
            break
        constructions += 1
        segment = src[idx: idx + 200]
        assert "servers=" in segment, (
            "本文件构造 adapter 必须显式指定 servers=，禁止回落到真实 server list"
        )
        idx += len(marker)
    assert constructions >= 1, "未找到 adapter 构造，守卫本身失效"


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
