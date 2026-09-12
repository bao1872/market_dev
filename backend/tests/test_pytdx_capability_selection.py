"""pytdx capability-aware server selection 纯单元测试。

覆盖契约：
1. 静态 capability 过滤：bars 不选 bars=False 的 server
2. **capability 隔离**：bars 失败只冷却 bars，不污染 xdxr
3. failover：bars A 失败 → bars B 成功，且不再重试 A
4. cooldown：冷却期内跳过该 capability；TTL 过期后重新 eligible
5. connect 失败 → 短期 cooldown，不每次都等 timeout
6. hostname 保持 hostname（不被当次解析出的 IP 覆盖）
7. `quote=None`（未验证）既不被选中，也**不得被判成坏**（不是 False）
8. 未声明 server（测试注入）不参与过滤 → 保持既有行为
"""

from __future__ import annotations

import time
from datetime import date

import pytest
from pytdx.errors import TdxConnectionError

from app.core import pytdx_adapter as mod
from app.core.pytdx_adapter import (
    CAPABILITY_BARS,
    CAPABILITY_QUOTE,
    CAPABILITY_XDXR,
    PYTDX_SERVER_CAPABILITIES,
    PYTDX_SERVERS,
    PytdxAdapter,
    PytdxServerCapability,
)

pytestmark = pytest.mark.pure_unit

T = date(2026, 9, 11)


class _FakeApi:
    connect_fail: set = set()
    bars_fail: set = set()
    xdxr_fail: set = set()
    connect_log: list = []

    def __init__(self, raise_exception: bool = True, auto_retry: bool = False) -> None:
        self.host: str | None = None

    def connect(self, host, port, time_out=None):  # noqa: ANN001, ANN201
        self.host = host
        type(self).connect_log.append(host)
        if host in type(self).connect_fail:
            raise TdxConnectionError("connect failed")
        return True

    def get_security_bars(self, cat, market, code, start, count):  # noqa: ANN001, ANN201
        if self.host in type(self).bars_fail:
            raise RuntimeError("calling function error")
        return [
            {
                "datetime": "2026-09-11 15:00",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "vol": 100.0,
                "amount": 100.0,
            }
        ]

    def get_xdxr_info(self, market, code):  # noqa: ANN001, ANN201
        if self.host in type(self).xdxr_fail:
            raise RuntimeError("calling function error")
        # 真实 pytdx xdxr payload 是 year/month/day 分量（adapter 据此构造 date 列）
        return [
            {
                "year": 2026,
                "month": 1,
                "day": 5,
                "category": 1,
                "name": "除权除息",
                "fenhong": 1.0,
                "peigujia": 0.0,
                "songzhuangu": 0.0,
                "peigu": 0.0,
            }
        ]

    def get_security_quotes(self, requests):  # noqa: ANN001, ANN201
        return [{"market": m, "code": c, "price": 1.0} for m, c in requests]

    def disconnect(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeApi.connect_fail = set()
    _FakeApi.bars_fail = set()
    _FakeApi.xdxr_fail = set()
    _FakeApi.connect_log = []
    monkeypatch.setattr(mod, "TdxHq_API", _FakeApi)


def _caps(*entries: tuple[str, bool, bool, bool | None]) -> tuple:
    return tuple(
        PytdxServerCapability((host, 7709), bars=b, xdxr=x, quote=q)
        for host, b, x, q in entries
    )


def _bars(adapter: PytdxAdapter) -> object:
    return adapter.get_daily_bars("600519", T, T)


def _xdxr(adapter: PytdxAdapter):  # noqa: ANN202
    return adapter._fetch_xdxr_from_pytdx("600519")  # noqa: SLF001


# ── 1. 静态 capability 过滤 ──────────────────────────────────────────
def test_bars_never_selects_non_bars_server() -> None:
    caps = _caps(
        ("A", False, True, None),   # xdxr only
        ("B", True, True, True),    # bars ok
    )
    adapter = PytdxAdapter(servers=[("A", 7709), ("B", 7709)], capabilities=caps, max_retries=1)

    _bars(adapter)

    assert "A" not in _FakeApi.connect_log
    assert "B" in _FakeApi.connect_log


# ── 2. capability 隔离：bars 失败不污染 xdxr ─────────────────────────
def test_bars_failure_does_not_pollute_xdxr() -> None:
    caps = _caps(("A", True, True, True))
    adapter = PytdxAdapter(
        servers=[("A", 7709)], capabilities=caps, max_retries=1,
        capability_cooldown_seconds=1800,
    )
    _FakeApi.bars_fail = {"A"}

    with pytest.raises(mod.PytdxSourceError):
        _bars(adapter)

    # bars 已冷却
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is True  # noqa: SLF001
    # 但 xdxr 未被污染，仍可成功
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_XDXR) is False  # noqa: SLF001
    frame = _xdxr(adapter)
    assert frame is not None and not frame.empty


# ── 3. failover：A 失败 → B 成功，且不再重试 A ──────────────────────
def test_bars_failover_to_next_server_without_retrying_failed() -> None:
    caps = _caps(("A", True, True, None), ("B", True, True, None))
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709)], capabilities=caps, max_retries=3,
        capability_cooldown_seconds=1800,
    )
    _FakeApi.bars_fail = {"A"}

    df = _bars(adapter)

    assert df is not None and not df.empty
    assert _FakeApi.connect_log.count("A") == 1  # 失败后不再回到 A


# ── 4. cooldown：期内跳过、TTL 过期后恢复 ───────────────────────────
def test_cooldown_skips_then_expires() -> None:
    caps = _caps(("A", True, True, None))
    adapter = PytdxAdapter(
        servers=[("A", 7709)], capabilities=caps, max_retries=1,
        capability_cooldown_seconds=0.05,
    )
    _FakeApi.bars_fail = {"A"}

    with pytest.raises(mod.PytdxSourceError):
        _bars(adapter)

    # 冷却期内：无 eligible server（wrapper 会把 message 换成 "connection unavailable"，
    # 「no eligible」保留在 cause 上）
    with pytest.raises(mod.PytdxSourceError) as ei:
        _bars(adapter)
    assert "no eligible" in str(ei.value.cause)

    # TTL 过期 → 重新 eligible
    time.sleep(0.06)
    _FakeApi.bars_fail = set()
    df = _bars(adapter)
    assert df is not None and not df.empty


# ── 5. connect 失败 → 短期 cooldown ─────────────────────────────────
def test_connect_failure_marks_short_cooldown() -> None:
    caps = _caps(("A", True, True, None))
    adapter = PytdxAdapter(
        servers=[("A", 7709)], capabilities=caps, max_retries=1,
        capability_cooldown_seconds=1800,
    )
    _FakeApi.connect_fail = {"A"}

    with pytest.raises(mod.PytdxSourceError):
        _bars(adapter)

    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is True  # noqa: SLF001

    # 第二次调用：A 处于 cooldown → 不再发起建连（不重复等 timeout）
    before = len(_FakeApi.connect_log)
    with pytest.raises(mod.PytdxSourceError) as ei:
        _bars(adapter)
    assert "no eligible" in str(ei.value.cause)
    assert len(_FakeApi.connect_log) == before


# ── 6. hostname 保持 hostname ───────────────────────────────────────
def test_hostname_is_preserved_in_configuration() -> None:
    servers = [c.server for c in PYTDX_SERVER_CAPABILITIES]
    assert ("sztdx.gtjas.com", 7709) in servers
    assert ("shtdx.gtjas.com", 7709) in servers
    assert ("jstdx.gtjas.com", 7709) in servers
    assert PYTDX_SERVERS[0] == ("159.75.55.232", 7709)

    caps = _caps(("sztdx.gtjas.com", True, True, True))
    adapter = PytdxAdapter(servers=[("sztdx.gtjas.com", 7709)], capabilities=caps, max_retries=1)
    _bars(adapter)
    assert adapter.connected_server == ("sztdx.gtjas.com", 7709)


# ── 7. quote=None：不选中，但也不是 False ───────────────────────────
def test_quote_unverified_not_selected_and_not_marked_bad() -> None:
    caps = _caps(("A", True, True, None))
    adapter = PytdxAdapter(servers=[("A", 7709)], capabilities=caps, max_retries=1)

    cap = adapter._capabilities[("A", 7709)]  # noqa: SLF001
    assert cap.quote is None  # 未验证 ≠ False
    assert adapter._server_supports(("A", 7709), CAPABILITY_QUOTE) is False  # noqa: SLF001
    assert adapter._server_supports(("A", 7709), CAPABILITY_BARS) is True  # noqa: SLF001


# ── 8. 未声明 server 不参与过滤（保持既有注入行为）──────────────────
def test_undeclared_servers_keep_legacy_behaviour() -> None:
    adapter = PytdxAdapter(servers=[("injected.example", 7709)], max_retries=1)
    _FakeApi.bars_fail = {"injected.example"}

    with pytest.raises(mod.PytdxSourceError):
        _bars(adapter)

    # 未声明 → 不进入 cooldown（既有 retry/rotate 语义不变）
    assert adapter._in_cooldown(("injected.example", 7709), CAPABILITY_BARS) is False  # noqa: SLF001


# ── 静态配置事实快照（防止误改回旧池）────────────────────────────────
def test_capability_config_snapshot() -> None:
    bars_servers = [c.server for c in PYTDX_SERVER_CAPABILITIES if c.bars]
    assert len(PYTDX_SERVER_CAPABILITIES) == 10
    assert len(bars_servers) == 4
    # 已证实 TCP 建连失败的旧池 IP 不得回到候选池
    for dead in ("119.147.212.81", "14.215.128.18", "202.108.253.131", "123.125.108.23"):
        assert (dead, 7709) not in [c.server for c in PYTDX_SERVER_CAPABILITIES]
