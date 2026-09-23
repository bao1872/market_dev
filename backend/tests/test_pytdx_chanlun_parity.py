"""PANJI-TDX-PARITY-01 (Task 1) — 合同 T1–T13 与四服务器故障注入。

覆盖（移植 chanlun-pro 的 transport reliability 机制，非复制其 storage / qfq）：
- T3/T5 全 eligible bars server 都获得机会，且同一 operation 内坏 server 不重复
- T4 全部 eligible 失败 → typed PytdxSourceError
- T6 全部 cooldown → 恰好一台 HALF_OPEN，用业务请求自探针；成功即恢复
- T6b half-open 失败 → 重新进入 cooldown
- T7 EWMA 延迟排名优先健康低延迟 server
- T8 bars 失败不污染 xdxr
- T9 4000 根 15m 分页正确（唯一 + 升序）；T9b 无进展 fail-fast
- T10/T11 fresh 成功后缓存末根被重新认证 + dedupe keep-last
- T12 live 分钟线禁止 stale 兜底（fail-closed）

不连接真实网络 / DB。用法：PURE_UNIT_TEST=1 pytest tests/test_pytdx_chanlun_parity.py -q
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from pytdx.errors import TdxConnectionError, TdxFunctionCallError

from app.core import pytdx_adapter as mod
from app.core.pytdx_adapter import (
    CAPABILITY_BARS,
    CAPABILITY_XDXR,
    PytdxAdapter,
    PytdxServerCapability,
    PytdxSourceError,
    _KlineCacheEntry,
)

pytestmark = pytest.mark.pure_unit

_SERVERS4: list[tuple[str, int]] = [
    ("A", 7709), ("B", 7709), ("C", 7709), ("D", 7709),
]


def _caps_all_bars(
    servers: list[tuple[str, int]] | None = None,
) -> tuple[PytdxServerCapability, ...]:
    servers = servers if servers is not None else _SERVERS4
    return tuple(
        PytdxServerCapability(s, bars=True, xdxr=True, quote=True) for s in servers
    )


class _FakeApi:
    """按 host 可配置 connect / bars / xdxr 失败；支持分页与重复页注入。"""

    connect_fail: set[str] = set()
    bars_fail: set[str] = set()
    xdxr_fail: set[str] = set()
    paginate: bool = False
    duplicate_page: bool = False
    connect_log: list[str] = []
    bars_log: list[str] = []
    _base = pd.Timestamp("2026-01-05 09:45")

    def __init__(self, raise_exception: bool = True, auto_retry: bool = False) -> None:
        assert raise_exception is True
        assert auto_retry is False, "auto_retry 必须为 False（PytdxAdapter 是唯一 retry owner）"
        self.host: str | None = None

    def connect(self, host, port, time_out=None):  # noqa: ANN001, ANN201
        self.host = host
        type(self).connect_log.append(host)
        if host in type(self).connect_fail:
            raise TdxConnectionError("connect failed")
        return True

    def get_security_bars(self, cat, market, code, start, count):  # noqa: ANN001, ANN201
        type(self).bars_log.append(self.host)
        if self.host in type(self).bars_fail:
            raise TdxFunctionCallError("calling function error")
        if type(self).duplicate_page:
            n, offset = count, 0
        elif type(self).paginate:
            n, offset = count, start
        else:
            n, offset = 1, 0
        rows = []
        for i in range(n):
            ts = type(self)._base + pd.Timedelta(minutes=15 * (offset + i))
            rows.append(
                {
                    "datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                    "vol": 100.0, "amount": 100.0,
                }
            )
        return rows

    def get_xdxr_info(self, market, code):  # noqa: ANN001, ANN201
        if self.host in type(self).xdxr_fail:
            raise RuntimeError("calling function error")
        return [
            {
                "year": 2026, "month": 1, "day": 5, "category": 1, "name": "除权除息",
                "fenhong": 1.0, "peigujia": 0.0, "songzhuangu": 0.0, "peigu": 0.0,
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
    _FakeApi.bars_log = []
    _FakeApi.paginate = False
    _FakeApi.duplicate_page = False
    # _klines_cache 是类属性，跨用例共享 → 每个用例重置，避免相互污染
    PytdxAdapter._klines_cache = {}
    monkeypatch.setattr(mod, "TdxHq_API", _FakeApi)


def _adapter(servers=None, *, max_retries: int = 1) -> PytdxAdapter:
    servers = servers if servers is not None else list(_SERVERS4)
    return PytdxAdapter(
        servers=servers, capabilities=_caps_all_bars(servers), max_retries=max_retries
    )


# ── T1 / T2：健康首选 / 首次失败换下一台 ────────────────────────────
def test_t1_healthy_first_server_no_failover() -> None:
    adapter = _adapter()

    df = adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert not df.empty
    assert _FakeApi.bars_log == ["A"]     # 只调用首选
    assert _FakeApi.connect_log == ["A"]  # 其余 server 零调用


def test_t2_first_source_failure_rotates_to_next() -> None:
    adapter = _adapter()
    _FakeApi.bars_fail = {"A"}

    df = adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert not df.empty
    assert _FakeApi.bars_log == ["A", "B"]


# ── T3 / T5：全 eligible 都获得机会；坏 server 不重复 ──────────────────
def test_t3_all_eligible_bars_servers_get_a_chance() -> None:
    adapter = _adapter(max_retries=2)  # max_retries 故意 < 4，证明不再截断 coverage
    _FakeApi.bars_fail = {"A", "B", "C"}

    df = adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert not df.empty
    assert _FakeApi.bars_log == ["A", "B", "C", "D"]
    assert adapter.connected_server == ("D", 7709)


def test_t4_all_eligible_fail_is_typed_source_error() -> None:
    adapter = _adapter(max_retries=10)
    _FakeApi.bars_fail = {"A", "B", "C", "D"}

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert _FakeApi.bars_log == ["A", "B", "C", "D"]  # 每台恰好一次
    # [parity RC2] 最终 error 保留真实业务 source failure 作根因（不是合成 "no eligible"）
    assert ei.value.operation == "get_security_bars"
    assert ei.value.server == ("D", 7709)
    assert isinstance(ei.value.cause, TdxFunctionCallError)
    assert "attempted=" in str(ei.value)


def test_t5_failed_server_not_retried_in_same_operation() -> None:
    adapter = _adapter(max_retries=10)
    _FakeApi.bars_fail = {"A", "B", "C", "D"}

    with pytest.raises(PytdxSourceError):
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert len(_FakeApi.bars_log) == 4
    assert _FakeApi.bars_log.count("A") == 1


# ── T6 / T6b：half-open 恢复 / 失败重新 cooldown ──────────────────────
def test_t6_half_open_rediscovers_recovered_server() -> None:
    adapter = _adapter()
    _FakeApi.bars_fail = {"A", "B", "C", "D"}
    with pytest.raises(PytdxSourceError):
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert all(adapter._in_cooldown(s, CAPABILITY_BARS) for s in _SERVERS4)  # noqa: SLF001

    # D（及全池）恢复，但仍在旧 cooldown 窗口内 → half-open 用业务请求自探针重新发现
    _FakeApi.bars_fail = set()
    _FakeApi.bars_log = []
    df = adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert not df.empty
    assert _FakeApi.bars_log == ["A"]  # 恰好一台（retry_after 最早）
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is False  # noqa: SLF001


def test_t6b_half_open_failure_reenters_cooldown() -> None:
    adapter = _adapter()
    _FakeApi.bars_fail = {"A", "B", "C", "D"}
    with pytest.raises(PytdxSourceError):
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    _FakeApi.bars_log = []
    with pytest.raises(PytdxSourceError):
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert _FakeApi.bars_log == ["A"]  # 恰好一次 half-open，不并发多台
    assert adapter._runtime_health_for(("A", 7709)).consecutive_failures == 2  # noqa: SLF001
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is True  # noqa: SLF001


def test_t6c_all_connect_cooldown_half_open_recovery() -> None:
    """[parity RC4] 全部 connect 失败（_connect_health 1800s）时，下一次业务请求仍允许
    exactly one half-open；provider 恢复即回归服务（不被 connect cooldown 卡死 30 分钟）。"""
    adapter = _adapter()
    _FakeApi.connect_fail = {"A", "B", "C", "D"}

    with pytest.raises(PytdxSourceError):
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert _FakeApi.connect_log == ["A", "B", "C", "D"]
    # 全部经 _connect_health 进入 cooldown（非 bars capability cooldown）
    assert all(adapter._in_cooldown(s, CAPABILITY_BARS) for s in _SERVERS4)  # noqa: SLF001

    # provider 恢复（仍在 1800s connect cooldown 窗口内）
    _FakeApi.connect_fail = set()
    _FakeApi.connect_log = []
    _FakeApi.bars_log = []
    df = adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert not df.empty
    assert _FakeApi.connect_log == ["A"]  # exactly one half-open connect
    assert _FakeApi.bars_log == ["A"]     # 业务请求本身即探针
    # half-open connect 成功 → 清除 connect cooldown → 立即回归正常服务
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is False  # noqa: SLF001


def test_t6d_concurrent_half_open_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[parity RC concurrency gate] 当 A/B/C/D 全部处于 connect cooldown、>=8 个请求同时打到
    同一个共享 PytdxAdapter 时，若第一个 HALF_OPEN 业务请求被故意阻塞：

    - 任意时刻最多 1 个 server/请求持有 ``half_open_inflight``
    - 不会并发启动第二台 half-open 探测（无 socket 状态损坏）
    - 第一个 half-open 成功后：清除 connect cooldown、清除 ``half_open_inflight``、
      其余请求可正常复用已恢复的 A（不在同一时刻再 half-open 第二台）
    - 所有线程完成：无 deadlock、无未预期异常

    不要求「恢复后的 A 总共只能被使用一次」——single-flight 的不变式只是：
    **同时最多一个并发 HALF_OPEN owner**。
    """
    adapter = _adapter()

    # 1) A/B/C/D 全部 connect 失败 → 全部进入 connect cooldown
    _FakeApi.connect_fail = {"A", "B", "C", "D"}
    with pytest.raises(PytdxSourceError):
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001
    assert all(  # noqa: SLF001
        adapter._in_cooldown(s, CAPABILITY_BARS) for s in _SERVERS4
    )

    # 2) A 模拟恢复（仍在 connect cooldown 窗口内 → 将被选为 HALF_OPEN）
    _FakeApi.connect_fail = set()
    _FakeApi.connect_log = []
    _FakeApi.bars_log = []

    # 3) 第一次 bars 调用在锁内（half-open 探测中）被阻塞，直到测试显式释放
    probe_entered = threading.Event()
    release_probe = threading.Event()

    class _BlockingFakeApi(_FakeApi):
        def get_security_bars(self, *a, **k):  # noqa: ANN001, ANN002, ANN003
            if not probe_entered.is_set():
                probe_entered.set()
                release_probe.wait(timeout=15)
            return super().get_security_bars(*a, **k)

    monkeypatch.setattr(mod, "TdxHq_API", _BlockingFakeApi)

    results: list[object] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            results.append(adapter._fetch_bars("000001", "d", 1))  # noqa: SLF001
        except BaseException as exc:  # noqa: BLE001 — 记录任何未预期异常
            errors.append(exc)

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(8)]
    for th in threads:
        th.start()

    # 等待第一个请求已进入 HALF_OPEN 但仍在阻塞（持有 _io_lock）
    assert probe_entered.wait(timeout=15)

    # 5) 在第一个 HALF_OPEN 尚未释放期间检查并发不变量
    inflight = [s for s in _SERVERS4 if adapter._runtime_health_for(s).half_open_inflight]  # noqa: SLF001
    assert len(inflight) <= 1, f"half_open_inflight 不应超过 1 台，实际={inflight}"
    # 其他线程被 _io_lock 挡在临界区外，不可能并发连第二台
    assert _FakeApi.connect_log == ["A"], f"并发期间只允许 1 次 half-open 连接，实际={_FakeApi.connect_log}"
    assert _FakeApi.bars_log == [], "并发期间业务请求应仍被阻塞，未返回 bars"

    # 6) 释放第一个 half-open 请求
    release_probe.set()

    for th in threads:
        th.join(timeout=15)
    assert not any(th.is_alive() for th in threads), "存在未结束线程（deadlock）"
    assert not errors, f"并发请求出现未预期异常：{errors}"

    # 8) 其余请求随后正常完成，并复用已恢复的 A
    assert len(results) == 8, f"应有 8 个成功结果，实际={len(results)}"
    assert all(not r.empty for r in results)

    # half_open_inflight 已清除；A 已脱离 cooldown 回归正常服务
    assert all(  # noqa: SLF001
        not adapter._runtime_health_for(s).half_open_inflight for s in _SERVERS4
    )
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is False  # noqa: SLF001
    # 仅第一台 half-open 真正 connect 过 A；其余线程命中已建立连接（幂等复用，不再 connect）
    assert _FakeApi.connect_log == ["A"]


def test_rc3_programming_error_does_not_poison_servers() -> None:
    """[parity RC3] 业务调用抛编程错误（TypeError）→ 原样上抛；不轮转、不冷却、B/C/D 零调用。"""
    adapter = _adapter()
    calls = {"n": 0}

    def _boom(*_a: object, **_k: object) -> object:
        calls["n"] += 1
        raise TypeError("programming bug, not a source failure")

    with pytest.raises(TypeError):
        adapter._call_with_reconnect("get_security_bars", _boom)  # noqa: SLF001

    assert calls["n"] == 1                       # 只尝试一次，绝不轮转
    assert _FakeApi.bars_log == []               # 业务 API 未被调用
    assert _FakeApi.connect_log == ["A"]         # B/C/D 零 connect
    # 没有任何 server 被标记 bars cooldown
    assert all(  # noqa: SLF001
        not adapter._in_cooldown(s, CAPABILITY_BARS) for s in _SERVERS4
    )


# ── T7：EWMA 延迟排名 ────────────────────────────────────────────────
def test_t7_latency_ranking_prefers_healthy_fast_server() -> None:
    adapter = _adapter()
    health = adapter._runtime_health_for  # noqa: SLF001
    health(("A", 7709)).ewma_latency_ms = 500.0
    health(("B", 7709)).ewma_latency_ms = 5.0
    health(("C", 7709)).ewma_latency_ms = 50.0

    # [RC1] 机械证据：真实 health 值与 score（无 failure penalty 时 score == ewma）
    scores = {s: adapter._runtime_health_for(s).score() for s in _SERVERS4}  # noqa: SLF001

    ranked = adapter._ranked_servers(CAPABILITY_BARS, set(), set())  # noqa: SLF001

    assert scores == {
        ("A", 7709): 500.0,
        ("B", 7709): 5.0,
        ("C", 7709): 50.0,
        ("D", 7709): 10_000.0,  # 未观测
    }
    # 无 failure penalty → 严格按 EWMA 升序；未观测（10000ms）最后
    assert ranked == [("B", 7709), ("C", 7709), ("A", 7709), ("D", 7709)]


# ── T8：bars 失败不污染 xdxr ─────────────────────────────────────────
def test_t8_bars_failure_does_not_poison_xdxr() -> None:
    adapter = _adapter(servers=[("A", 7709)])
    _FakeApi.bars_fail = {"A"}

    with pytest.raises(PytdxSourceError):
        adapter._fetch_bars("000001", "d", 1)  # noqa: SLF001

    assert adapter._in_cooldown(("A", 7709), CAPABILITY_BARS) is True  # noqa: SLF001
    assert adapter._in_cooldown(("A", 7709), CAPABILITY_XDXR) is False  # noqa: SLF001

    frame = adapter._fetch_xdxr_from_pytdx("600519")  # noqa: SLF001
    assert frame is not None and not frame.empty


# ── T9 / T9b：4000 根分页 / 无进展 fail-fast ─────────────────────────
def test_t9_4000_bars_paginated_sorted_unique() -> None:
    adapter = _adapter(servers=[("A", 7709)])
    _FakeApi.paginate = True

    df = adapter._fetch_bars("000001", "15m", 4000)  # noqa: SLF001

    assert len(df) == 4000
    assert df["datetime"].is_monotonic_increasing
    assert df["datetime"].is_unique
    assert _FakeApi.bars_log == ["A"] * 5  # 4000 / 800 = 5 页


def test_t9b_pagination_no_progress_raises() -> None:
    adapter = _adapter(servers=[("A", 7709)])
    _FakeApi.paginate = True
    _FakeApi.duplicate_page = True

    with pytest.raises(PytdxSourceError) as ei:
        adapter._fetch_bars("000001", "15m", 4000)  # noqa: SLF001

    assert "no progress" in str(ei.value)


# ── T10 / T11：边界末根重新认证 + dedupe keep-last ───────────────────
def _seed_cache(
    adapter: PytdxAdapter,
    symbol: str,
    freq: str,
    times: list[str],
    closes: list[float],
    *,
    hours_ago: int = 10,
) -> None:
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz="Asia/Shanghai") for t in times])
    df = pd.DataFrame(
        {
            "open": closes, "high": closes, "low": closes, "close": closes,
            "volume": [1.0] * len(times), "amount": [1.0] * len(times),
            "adj_factor": [1.0] * len(times),
        },
        index=idx,
    )
    adapter._klines_cache[f"{symbol}:{freq}"] = _KlineCacheEntry(  # noqa: SLF001
        df=df,
        cached_at=datetime.now(tz=ZoneInfo("Asia/Shanghai")) - timedelta(hours=hours_ago),
        last_bar_time=idx[-1].to_pydatetime(),
    )


def test_t10_t11_boundary_bar_replaced_and_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(servers=[("A", 7709)])
    # cached 末根 t3（可能是不完整 bar，close=9.9）必须由 fresh 重新认证
    _seed_cache(
        adapter, "000001", "1d",
        ["2026-01-01 15:00", "2026-01-02 15:00", "2026-01-05 15:00"],
        [10.0, 10.5, 9.9],
    )
    fresh = pd.DataFrame(
        {
            "datetime": ["2026-01-05 15:00", "2026-01-06 15:00"],
            "open": [9.95, 10.1], "high": [9.95, 10.2], "low": [9.9, 10.0],
            "close": [9.95, 10.15], "volume": [2.0, 3.0], "amount": [2.0, 3.0],
        }
    )
    monkeypatch.setattr(adapter, "_fetch_bars", lambda *a, **k: fresh)

    result = asyncio.run(adapter.klines("000001", "1d"))

    assert list(result.index.strftime("%Y-%m-%d")) == [
        "2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"
    ]
    # t3 由 fresh 重新认证（9.95），缓存的旧 9.9 被丢弃（keep-last）
    assert float(result.loc[pd.Timestamp("2026-01-05 15:00", tz="Asia/Shanghai"), "close"]) == 9.95


# ── T12：live 分钟线禁止 stale 兜底 ──────────────────────────────────
def test_t12_live_minute_refresh_failure_refuses_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(servers=[("A", 7709)])
    _seed_cache(
        adapter, "000001", "15m",
        ["2026-01-05 09:45", "2026-01-05 10:00"],
        [10.0, 10.1],
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise PytdxSourceError(operation="get_security_bars", message="provider down")

    monkeypatch.setattr(adapter, "_fetch_bars", _boom)

    with pytest.raises(PytdxSourceError) as ei:
        asyncio.run(adapter.klines("000001", "15m"))

    assert "refusing stale" in str(ei.value)


# T13（historical/PIT DB_ONLY 零网络）由 MDAS source-policy 测试覆盖，不在本文件重复。
