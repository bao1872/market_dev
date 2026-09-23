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

import asyncio
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pytdx.errors import TdxConnectionError, TdxFunctionCallError

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
    bars_call_log: list = []
    xdxr_call_log: list = []
    quotes_call_log: list = []
    quotes_fail: set = set()
    history_fail: set = set()
    history_call_log: list = []

    def __init__(self, raise_exception: bool = True, auto_retry: bool = False) -> None:
        self.host: str | None = None

    def connect(self, host, port, time_out=None):  # noqa: ANN001, ANN201
        self.host = host
        type(self).connect_log.append(host)
        if host in type(self).connect_fail:
            raise TdxConnectionError("connect failed")
        return True

    def get_security_bars(self, cat, market, code, start, count):  # noqa: ANN001, ANN201
        type(self).bars_call_log.append(self.host)
        if self.host in type(self).bars_fail:
            raise TdxFunctionCallError("calling function error")
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
        type(self).xdxr_call_log.append(self.host)
        if self.host in type(self).xdxr_fail:
            raise TdxFunctionCallError("calling function error")
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
        type(self).quotes_call_log.append(self.host)
        if self.host in type(self).quotes_fail:
            raise TdxFunctionCallError("calling function error")
        return [{"market": m, "code": c, "price": 1.0} for m, c in requests]

    def get_history_transaction_data(self, market, code, offset, count, date):  # noqa: ANN001, ANN201
        type(self).history_call_log.append(self.host)
        if self.host in type(self).history_fail:
            raise TdxFunctionCallError("calling function error")
        return [{"time": "14:57", "price": 1.0, "vol": 100, "amount": 100.0, "buy_or_sell": 0}]

    def disconnect(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeApi.connect_fail = set()
    _FakeApi.bars_fail = set()
    _FakeApi.xdxr_fail = set()
    _FakeApi.quotes_fail = set()
    _FakeApi.history_fail = set()
    _FakeApi.connect_log = []
    _FakeApi.bars_call_log = []
    _FakeApi.xdxr_call_log = []
    _FakeApi.quotes_call_log = []
    _FakeApi.history_call_log = []
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

    # 冷却期内：全部 capable server 均 cooldown → 允许一次 half-open（业务请求自探针），
    # 但 provider 仍坏 → 再次 typed error。[parity RC2] 根因保留真实 bars 失败
    # （而不是被合成 "no eligible" 覆盖）。
    with pytest.raises(mod.PytdxSourceError) as ei:
        _bars(adapter)
    assert isinstance(ei.value.cause, TdxFunctionCallError)

    # TTL 过期 + provider 恢复 → 重新 eligible
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

    # 新合同（[parity D] half-open）：全部 capable server 都在 cooldown 时，允许
    # **恰好一次** HALF_OPEN，并用本次真实业务请求本身作为探测；失败后重新进入 cooldown。
    # （旧合同「cooldown 内不再建连」已被有意的语义升级取代，见 test_pytdx_chanlun_parity.py。）
    before = len(_FakeApi.connect_log)
    with pytest.raises(mod.PytdxSourceError) as ei:
        _bars(adapter)
    assert "no eligible" in str(ei.value.cause)
    assert len(_FakeApi.connect_log) == before + 1  # exactly one half-open probe
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is True  # noqa: SLF001


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


# ── A1–A3. 已有 socket 不得绕过 capability 过滤（跨 operation）──────
def test_a1_existing_xdxr_only_socket_switches_for_bars() -> None:
    """先 XDXR 连到 xdxr-only server A；随后 bars 必须直接切 B，不得对 A 调 bars。"""
    caps = _caps(("A", False, True, None), ("B", True, True, None))
    adapter = PytdxAdapter(servers=[("A", 7709), ("B", 7709)], capabilities=caps, max_retries=1)

    _xdxr(adapter)
    assert adapter.connected_server == ("A", 7709)  # 环形起点 → A

    _FakeApi.bars_call_log = []
    _bars(adapter)

    assert "A" not in _FakeApi.bars_call_log
    assert "B" in _FakeApi.bars_call_log
    assert adapter.connected_server == ("B", 7709)


def test_a2_existing_socket_with_bars_cooldown_switches() -> None:
    """A 已持有 socket，但其 bars 处于 cooldown → bars 必须直接切 B。"""
    caps = _caps(("A", True, True, None), ("B", True, True, None))
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709)], capabilities=caps, max_retries=1,
        capability_cooldown_seconds=1800,
    )
    _xdxr(adapter)
    assert adapter.connected_server == ("A", 7709)
    # 模拟 A 的 bars 已被其它路径证伪并进入冷却
    adapter._mark_capability_failure(("A", 7709), CAPABILITY_BARS)  # noqa: SLF001

    _FakeApi.bars_call_log = []
    _bars(adapter)

    assert "A" not in _FakeApi.bars_call_log
    assert "B" in _FakeApi.bars_call_log


def test_a3_existing_quote_unknown_socket_switches_for_quote() -> None:
    """A 因 XDXR 已连上但 quote=None；随后 quote 必须直接切 B，A 的 quote 0 次调用。"""
    caps = _caps(("A", True, True, None), ("B", True, True, True))
    adapter = PytdxAdapter(servers=[("A", 7709), ("B", 7709)], capabilities=caps, max_retries=1)

    _xdxr(adapter)
    assert adapter.connected_server == ("A", 7709)

    _FakeApi.quotes_call_log = []
    rows = adapter.get_security_quotes(["600519"])

    assert rows
    assert "A" not in _FakeApi.quotes_call_log
    assert "B" in _FakeApi.quotes_call_log
    assert adapter.connected_server == ("B", 7709)


# ── 静态配置事实快照（防止误改回旧池）────────────────────────────────
def test_capability_config_snapshot() -> None:
    bars_servers = [c.server for c in PYTDX_SERVER_CAPABILITIES if c.bars]
    assert len(PYTDX_SERVER_CAPABILITIES) == 10
    assert len(bars_servers) == 4
    # 已证实 TCP 建连失败的旧池 IP 不得回到候选池
    for dead in ("119.147.212.81", "14.215.128.18", "202.108.253.131", "123.125.108.23"):
        assert (dead, 7709) not in [c.server for c in PYTDX_SERVER_CAPABILITIES]


# ════════════════════════════════════════════════════════════════════════
# PANJI-TDX-PARITY-01-FIX1：bars 运行时健康必须只属于 BARS
# ════════════════════════════════════════════════════════════════════════

# FIX1-A：XDXR 失败不得改写 bars 运行时计数器 / 分数
def test_xdxr_failure_does_not_alter_bars_runtime_health() -> None:
    caps = _caps(("A", True, True, True))
    adapter = PytdxAdapter(
        servers=[("A", 7709)], capabilities=caps, max_retries=1,
        capability_cooldown_seconds=1800,
    )
    health_before = adapter._runtime_health_for(("A", 7709))  # noqa: SLF001
    assert health_before.failure_count == 0
    assert health_before.consecutive_failures == 0

    _FakeApi.xdxr_fail = {"A"}
    with pytest.raises(mod.PytdxSourceError):
        _xdxr(adapter)

    health_after = adapter._runtime_health_for(("A", 7709))  # noqa: SLF001
    # XDXR 失败只冷却 XDXR capability，绝不动 bars 运行时健康
    assert health_after.failure_count == 0
    assert health_after.consecutive_failures == 0


# FIX1-B：quote 成功不得改写 bars EWMA 延迟
def test_quote_success_does_not_alter_bars_ewma() -> None:
    caps = _caps(("A", True, True, True))
    adapter = PytdxAdapter(servers=[("A", 7709)], capabilities=caps, max_retries=1)
    health = adapter._runtime_health_for(("A", 7709))  # noqa: SLF001
    assert health.ewma_latency_ms is None

    adapter.get_security_quotes(["600519"])
    # quote 成功不得污染 bars 运行时健康（bars 不能「以为」A 很快）
    assert health.ewma_latency_ms is None

    # 对照：bars 成功应当更新 EWMA
    _FakeApi.bars_fail = set()
    _bars(adapter)
    assert health.ewma_latency_ms is not None


# FIX1-C：bars 失败不得改变 XDXR 的 server 选择顺序（保持静态稳定顺序）
def test_bars_failure_does_not_alter_xdxr_ranking() -> None:
    caps = _caps(("A", True, True, True), ("B", False, True, True))
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709)], capabilities=caps, max_retries=1,
        capability_cooldown_seconds=1800,
    )
    # 制造 bars 失败（污染 bars 健康）
    _FakeApi.bars_fail = {"A"}
    with pytest.raises(mod.PytdxSourceError):
        _bars(adapter)

    # XDXR 排序应仍按静态配置顺序 [A, B]，不被 bars 健康/分数影响
    ranked = adapter._ranked_servers(CAPABILITY_XDXR, set(), set())  # noqa: SLF001
    assert ranked == [("A", 7709), ("B", 7709)]


# ════════════════════════════════════════════════════════════════════════
# PANJI-TDX-PARITY-01-FIX2：全池覆盖 / half-open 不得扩散到其它 capability
# ════════════════════════════════════════════════════════════════════════

# FIX2-bars：4 eligible server + max_retries=3 → A/B/C/D 全部得到机会
def test_bars_covers_all_eligible_servers_regardless_of_max_retries() -> None:
    caps = _caps(
        ("A", True, True, None),
        ("B", True, True, None),
        ("C", True, True, None),
        ("D", True, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    _FakeApi.bars_fail = {"A", "B", "C", "D"}
    with pytest.raises(mod.PytdxSourceError):
        _bars(adapter)
    # full-pool coverage：max_retries=3 不再截断 bars 覆盖到第 4 台
    assert len({*_FakeApi.connect_log}) == 4
    for h in ("A", "B", "C", "D"):
        assert h in _FakeApi.connect_log


# FIX2-xdxr：max_retries=3 → 至多 3 次 operation 尝试（不扫全池）
def test_xdxr_respects_max_retries_bounded_attempts() -> None:
    caps = _caps(
        ("A", False, True, None),
        ("B", False, True, None),
        ("C", False, True, None),
        ("D", False, True, None),
        ("E", False, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709), ("E", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    _FakeApi.xdxr_fail = {"A", "B", "C", "D", "E"}
    with pytest.raises(mod.PytdxSourceError):
        _xdxr(adapter)
    # [RC2] max_retries=3 限制的是「业务 operation 调用次数」（不是 TCP connect 次数）
    assert len(_FakeApi.xdxr_call_log) <= 3
    assert "D" not in _FakeApi.xdxr_call_log
    assert "E" not in _FakeApi.xdxr_call_log


# FIX2-quote：保持既有有界重试语义
def test_quote_respects_max_retries_bounded_attempts() -> None:
    caps = _caps(
        ("A", True, True, True),
        ("B", True, True, True),
        ("C", True, True, True),
        ("D", True, True, True),
        ("E", True, True, True),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709), ("E", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    _FakeApi.quotes_fail = {"A", "B", "C", "D", "E"}
    with pytest.raises(mod.PytdxSourceError):
        adapter.get_security_quotes(["600519"])
    # [RC2] max_retries 限制业务调用次数
    assert len(_FakeApi.quotes_call_log) <= 3
    assert "D" not in _FakeApi.quotes_call_log
    assert "E" not in _FakeApi.quotes_call_log


# FIX2-history（capability=None）：保持既有有界重试语义
def test_history_transaction_respects_max_retries_bounded() -> None:
    caps = _caps(
        ("A", True, True, None),
        ("B", True, True, None),
        ("C", True, True, None),
        ("D", True, True, None),
        ("E", True, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709), ("E", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    _FakeApi.history_fail = {"A", "B", "C", "D", "E"}
    with pytest.raises(mod.PytdxSourceError):
        adapter.get_history_transaction_page("000001", date(2026, 9, 11), 0, 100)
    # [RC2] max_retries 限制业务调用次数
    assert len(_FakeApi.history_call_log) <= 3
    assert "D" not in _FakeApi.history_call_log
    assert "E" not in _FakeApi.history_call_log


# ════════════════════════════════════════════════════════════════════════
# PANJI-TDX-PARITY-01-FIX3：half-open 必须跨请求限频（家族级探测窗口）
# ════════════════════════════════════════════════════════════════════════

# FIX3：outage 窗口内的 20 个顺序请求 → 只有 1 次 half-open 真实网络探测
def test_half_open_rate_limited_across_requests_during_outage() -> None:
    caps = _caps(
        ("A", True, True, None),
        ("B", True, True, None),
        ("C", True, True, None),
        ("D", True, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    # 模拟 outage 已发生：所有 bars server 已处于 cooldown
    now = time.monotonic()
    for h in ("A", "B", "C", "D"):
        adapter._capability_health.setdefault((h, 7709), {})[CAPABILITY_BARS] = now + 1800  # noqa: SLF001
    # 进程级 half-open 限频窗口设很大，使 20 个快速请求都落在同一窗口内
    adapter.bars_half_open_interval = 100.0
    _FakeApi.bars_fail = {"A", "B", "C", "D"}

    for _ in range(20):
        with pytest.raises(mod.PytdxSourceError):
            _bars(adapter)

    # 整个 outage 窗口内只有 1 次 half-open 真实探测（其余 19 次快速失败，0 网络调用）
    assert len(_FakeApi.connect_log) == 1


# ════════════════════════════════════════════════════════════════════════
# PANJI-TDX-PARITY-01-FIX4：cooldown 截止 = max(connect, capability)，不是 min
# ════════════════════════════════════════════════════════════════════════

# FIX4：connect 冷却 1800s + bars 冷却 30s → 正常 eligibility 截止 = 1800s
def test_cooldown_deadline_is_max_of_connect_and_capability() -> None:
    adapter = PytdxAdapter(servers=[("A", 7709)], max_retries=1, capability_cooldown_seconds=1800)
    server = ("A", 7709)
    now = time.monotonic()
    adapter._connect_health[server] = now + 1800  # noqa: SLF001
    adapter._capability_health.setdefault(server, {})[CAPABILITY_BARS] = now + 30  # noqa: SLF001

    # 正常 eligibility 必须等两者都清除 → 取 max = now + 1800
    assert adapter._cooldown_deadline(server, CAPABILITY_BARS) == pytest.approx(now + 1800, rel=1e-6)  # noqa: SLF001


# ════════════════════════════════════════════════════════════════════════
# PANJI-TDX-PARITY-01-FIX5：half-open 所有权在任何离开路径都必须释放
# ════════════════════════════════════════════════════════════════════════

# FIX5：half-open 路径遇编程错误（TypeError）→ 原样抛出、half_open_inflight 释放、
#       不新增 cooldown、不 failover
def test_half_open_releases_ownership_on_programming_error() -> None:
    caps = _caps(("A", True, True, None), ("B", True, True, None))
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709)], capabilities=caps, max_retries=3,
        capability_cooldown_seconds=1800,
    )
    # 全部 bars 已 cooldown → 走 half-open；业务回调抛 TypeError
    now = time.monotonic()
    for h in ("A", "B"):
        adapter._capability_health.setdefault((h, 7709), {})[CAPABILITY_BARS] = now + 1800  # noqa: SLF001
    adapter.bars_half_open_interval = 100.0

    state = {"n": 0}

    def boom(api):  # noqa: ANN001, ANN202
        state["n"] += 1
        raise TypeError("bug")

    with pytest.raises(TypeError):
        adapter._call_with_reconnect("get_security_bars", boom, capability=CAPABILITY_BARS)  # noqa: SLF001

    # 仅 1 次业务调用（half-open 探测本身）
    assert state["n"] == 1
    # half-open 所有权已释放
    for h in ("A", "B"):
        assert adapter._runtime_health_for((h, 7709)).half_open_inflight is False  # noqa: SLF001
    # 不因编程错误新增 bars cooldown（pre-seeded 的 cooldown 保持不变，未被改写）
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is True  # noqa: SLF001
    assert adapter._in_cooldown(("B", 7709), CAPABILITY_BARS) is True  # noqa: SLF001


# ════════════════════════════════════════════════════════════════════════
# PANJI-TDX-PARITY-01-FIX6：klines() 不得把编程错误重新伪装成 provider outage
# ════════════════════════════════════════════════════════════════════════

# FIX6：live 分钟线增量刷新遇编程错误（TypeError）→ 原样抛出，不得包成 PytdxSourceError
def test_klines_live_programming_error_not_reclassified() -> None:
    adapter = PytdxAdapter(servers=[("A", 7709)], max_retries=1)

    # 注入一个已过期缓存，触发增量刷新路径（catch 分支所在处）
    cache_key = adapter._cache_key("600519", "15m")  # noqa: SLF001
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    adapter._klines_cache[cache_key] = mod._KlineCacheEntry(  # noqa: SLF001
        df=pd.DataFrame({"datetime": [pd.Timestamp("2026-09-11 15:00")], "close": [1.0]}),
        cached_at=now - timedelta(days=1),
        last_bar_time=pd.Timestamp("2026-09-11 15:00"),
    )

    # 让 _fetch_bars 抛编程错误（模拟代码 bug）
    def boom(*_a, **_k):  # noqa: ANN202
        raise TypeError("bug in fetch")

    adapter._fetch_bars = boom  # noqa: SLF001

    # 必须原样抛出 TypeError，而不是被重新包装成 PytdxSourceError
    with pytest.raises(TypeError):
        asyncio.run(adapter.klines("600519", "15m"))


# ════════════════════════════════════════════════════════════════════════
# PANJI-TDX-PARITY-01-FIX2 纠正（reviewer 第二轮 P0）
# ════════════════════════════════════════════════════════════════════════

# RC1：half-open 的「30 秒限频」必须覆盖 CONNECT failure（不仅是 business call 失败）。
#       所有 bars server 处于 CONNECT cooldown + half-open connect 仍抛 TdxConnectionError
#       → 20 个顺序请求里只有 1 次 half-open 网络探测，后 19 个快速失败、0 网络调用。
def test_half_open_connect_failure_rate_limited_across_requests() -> None:
    caps = _caps(
        ("A", True, True, None),
        ("B", True, True, None),
        ("C", True, True, None),
        ("D", True, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    # 所有 bars server 处于 CONNECT cooldown（模拟 outage：连不上的场景）
    now = time.monotonic()
    for h in ("A", "B", "C", "D"):
        adapter._connect_health[(h, 7709)] = now + 1800  # noqa: SLF001
    # 进程级 half-open 限频窗口设很大，使 20 个快速请求都落在同一窗口内
    adapter.bars_half_open_interval = 100.0
    # half-open 候选的 connect 也继续抛连接错误
    _FakeApi.connect_fail = {"A", "B", "C", "D"}

    for _ in range(20):
        with pytest.raises(mod.PytdxSourceError):
            _bars(adapter)

    # 整个 outage 窗口内只有 1 次 half-open 真实探测（connect 失败也必须推进窗口）
    assert len(_FakeApi.connect_log) == 1
    # 无任何 half-open 所有权泄漏
    for h in ("A", "B", "C", "D"):
        assert adapter._runtime_health_for((h, 7709)).half_open_inflight is False  # noqa: SLF001


# RC2 #2：XDXR —— A/B/C connect 失败、D 健康 → D 必须在第一次业务 attempt 就得到机会
def test_xdxr_connect_failure_does_not_consume_business_attempt() -> None:
    caps = _caps(
        ("A", False, True, None),
        ("B", False, True, None),
        ("C", False, True, None),
        ("D", False, True, None),
        ("E", False, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709), ("E", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    # A/B/C TCP 连不上、D/E 健康；业务 call 不失败
    _FakeApi.connect_fail = {"A", "B", "C"}
    _FakeApi.xdxr_fail = set()
    frame = _xdxr(adapter)
    assert frame is not None and not frame.empty
    # 只有 D（首个健康 server）拿到真正的 XDXR business call；
    # A/B/C 仅 connect 失败，不得占用业务重试名额（否则 D 永远没机会）
    assert _FakeApi.xdxr_call_log == ["D"]


# RC2 #3：QUOTE —— 同 XDXR 的 connect 失败不占用业务重试名额
def test_quote_connect_failure_does_not_consume_business_attempt() -> None:
    caps = _caps(
        ("A", True, True, True),
        ("B", True, True, True),
        ("C", True, True, True),
        ("D", True, True, True),
        ("E", True, True, True),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709), ("E", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    _FakeApi.connect_fail = {"A", "B", "C"}
    _FakeApi.quotes_fail = set()
    result = adapter.get_security_quotes(["600519"])
    assert result is not None
    assert _FakeApi.quotes_call_log == ["D"]


# RC2 #4：history / capability=None —— 同 connect 失败不占用业务重试名额
def test_history_connect_failure_does_not_consume_business_attempt() -> None:
    caps = _caps(
        ("A", True, True, None),
        ("B", True, True, None),
        ("C", True, True, None),
        ("D", True, True, None),
        ("E", True, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709), ("E", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    _FakeApi.connect_fail = {"A", "B", "C"}
    _FakeApi.history_fail = set()
    result = adapter.get_history_transaction_page("000001", date(2026, 9, 11), 0, 100)
    assert result is not None
    assert _FakeApi.history_call_log == ["D"]


# RC3 #6：非 bars 全部 connect 失败 → 外层 operation 是业务 operation，
#       不得泄漏裸 operation="connect"；cause.operation == "connect"。
def test_nonbars_all_connect_fail_outer_operation_preserved() -> None:
    caps = _caps(
        ("A", False, True, None),
        ("B", False, True, None),
        ("C", False, True, None),
    )
    adapter = PytdxAdapter(
        servers=[("A", 7709), ("B", 7709), ("C", 7709)],
        capabilities=caps, max_retries=3, capability_cooldown_seconds=1800,
    )
    _FakeApi.connect_fail = {"A", "B", "C"}
    with pytest.raises(mod.PytdxSourceError) as ei:
        _xdxr(adapter)
    # 外层 operation 是业务 operation，不是裸 "connect"
    assert ei.value.operation != "connect"
    # cause.operation == "connect"（RC3 public taxonomy）
    assert ei.value.cause is not None and ei.value.cause.operation == "connect"
