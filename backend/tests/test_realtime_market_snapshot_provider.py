"""[RT-SNAPSHOT] 盘中全市场实时 Snapshot Adapter 单元测试（纯单元，无网络）。

覆盖 Stage B1 合同：
A. 盘中 10:05 的 f124 必须合法返回（不得被 EOD 的 >=15:00 watermark 拒绝）；
B. 盘中只允许 push2.eastmoney.com，push2delay 绝不能出现；
C. 实时 host 失败必须 fail，不得 fallback 到延时源；
D. 全市场没有「当天」有效 f124 → fail；
E. 个别老时间戳（停牌/未刷新）不影响全市场 batch，且不伪造成今天；
F. watchlist 过滤发生在**全市场 fetch 之后**（网络永远全市场）；
G. 单位合同：f5 手→股、f6 元、f2 元不缩放；
H. single-host 快照契约：中间页失败不返回 partial；第二个 host 必须从 page 1 重拉。

不做：复权（B2）、freshness 秒数阈值（Monitor 按市场阶段决定）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services import eod_market_snapshot_provider as eod_prov
from app.services import realtime_market_snapshot_provider as rp

_SH = ZoneInfo("Asia/Shanghai")

TODAY = date(2026, 9, 12)
INTRADAY_TS = int(datetime(2026, 9, 12, 10, 5, tzinfo=_SH).timestamp())   # 今天 10:05
YESTERDAY_TS = int(datetime(2026, 9, 11, 15, 0, tzinfo=_SH).timestamp())  # 昨天 15:00

NOW = datetime(2026, 9, 12, 10, 5, tzinfo=_SH)

pytestmark = pytest.mark.pure_unit


def _row(
    code: str,
    *,
    name: str = "测试股",
    f13: int | None = None,
    close: Any = 12.34,      # 元（fltt=2 下东财已返回元）
    high: Any = 13.00,
    low: Any = 12.00,
    open_: Any = 12.50,
    volume: Any = 100,       # 手（f5；归一化后 ×100 转股）
    amount: Any = 123456.0,  # 元（f6）
    prev_close: Any = 12.20,
    f124: Any = INTRADAY_TS,
) -> dict[str, Any]:
    if f13 is None:
        f13 = 1 if code.startswith("6") else 0
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
    """按 **(host, page)** 返回 payload；未登记组合直接抛错以暴露意外请求。"""

    def __init__(self, script: dict[tuple[str, int], Any]) -> None:
        self._script = dict(script)
        self.calls: list[tuple[str, int]] = []
        self.urls: list[str] = []

    @staticmethod
    def _host_of(url: str) -> str:
        return url.split("//", 1)[-1].split("/", 1)[0]

    async def get(self, url: str, params: Any = None, timeout: Any = None) -> _FakeResponse:
        host = self._host_of(url)
        page = int((params or {}).get("pn", 1))
        self.urls.append(url)
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
    """分页重试不真实 sleep。"""
    monkeypatch.setattr(eod_prov, "_PAGE_RETRY_BASE_DELAY", 0.0)


# =========================================================================
# A. 盘中 watermark 合法（不要求 >= 15:00）
# =========================================================================


@pytest.mark.asyncio
async def test_a_intraday_watermark_is_accepted() -> None:
    """f124=今天 10:05 必须合法返回，不得被 EOD 收盘 watermark 拒绝。"""
    rows = [_row("600519"), _row("000001")]
    client = _ScriptedClient([_page(rows, 2)])

    snap = await rp.fetch_realtime_a_share_snapshot(client, now=NOW)  # type: ignore[arg-type]

    assert len(snap.rows) == 2
    assert snap.market_watermark == datetime(2026, 9, 12, 10, 5, tzinfo=_SH)
    assert snap.captured_at == NOW


# =========================================================================
# B / C. 盘中 host 白名单与禁止延时源 fallback
# =========================================================================


@pytest.mark.asyncio
async def test_b_realtime_only_uses_push2_host() -> None:
    rows = [_row("600519"), _row("000001")]
    client = _ScriptedClient([_page(rows, 2)])

    snap = await rp.fetch_realtime_a_share_snapshot(client, now=NOW)  # type: ignore[arg-type]

    assert snap.source_host == "push2.eastmoney.com"
    assert client.urls == ["https://push2.eastmoney.com/api/qt/clist/get"]
    assert all("push2delay" not in u for u in client.urls)
    # 常量层面也不得混入延时源
    assert "push2delay.eastmoney.com" not in rp.REALTIME_CLIST_HOSTS


@pytest.mark.asyncio
async def test_c_realtime_host_failure_does_not_fallback_to_delayed() -> None:
    """实时 host 不可用 → fail；绝不偷偷改用 push2delay。"""
    client = _ScriptedClient([httpx.ConnectError("push2 down")])

    with pytest.raises(eod_prov.SnapshotProviderError):
        await rp.fetch_realtime_a_share_snapshot(client, now=NOW)  # type: ignore[arg-type]

    assert client.urls, "必须至少尝试过一次实时 host"
    assert all("push2.eastmoney.com" in u for u in client.urls)
    assert all("push2delay" not in u for u in client.urls)


# =========================================================================
# D. 没有当天 watermark → fail
# =========================================================================


@pytest.mark.asyncio
async def test_d_no_same_day_watermark_fails() -> None:
    """全市场 f124 全是昨天（现在是今天 10:05）→ 必须 fail。"""
    rows = [_row("600519", f124=YESTERDAY_TS), _row("000001", f124=YESTERDAY_TS)]
    client = _ScriptedClient([_page(rows, 2)])

    with pytest.raises(
        eod_prov.SnapshotProviderError, match="no same-day market watermark"
    ):
        await rp.fetch_realtime_a_share_snapshot(client, now=NOW)  # type: ignore[arg-type]


# =========================================================================
# E. 个别老时间戳不影响全市场 batch（市场级 freshness != 单股 freshness）
# =========================================================================


@pytest.mark.asyncio
async def test_e_individual_stale_row_does_not_fail_batch() -> None:
    """600519 今天 / 000001 昨天 → batch PASS；000001 的 updated_at 不得被伪造成今天。"""
    rows = [_row("600519", f124=INTRADAY_TS), _row("000001", f124=YESTERDAY_TS)]
    client = _ScriptedClient([_page(rows, 2)])

    snap = await rp.fetch_realtime_a_share_snapshot(client, now=NOW)  # type: ignore[arg-type]

    assert snap.market_watermark == datetime(2026, 9, 12, 10, 5, tzinfo=_SH)
    by = snap.by_symbol()
    assert by["600519"].updated_at is not None
    assert by["600519"].updated_at.date() == TODAY
    # 停牌/老时间戳保留原值，不伪造
    assert by["000001"].updated_at is not None
    assert by["000001"].updated_at.date() == date(2026, 9, 11)


@pytest.mark.asyncio
async def test_e_watchlist_keeps_stale_row_as_is() -> None:
    """watchlist 命中停牌股时，row 仍保留且时间戳仍是昨天。"""
    rows = [_row("600519", f124=INTRADAY_TS), _row("000001", f124=YESTERDAY_TS)]
    client = _ScriptedClient([_page(rows, 2)])

    snap = await rp.fetch_realtime_a_share_snapshot(
        client, symbols={"000001"}, now=NOW  # type: ignore[arg-type]
    )

    assert len(snap.rows) == 1
    assert snap.rows[0].symbol == "000001"
    assert snap.rows[0].updated_at is not None
    assert snap.rows[0].updated_at.date() == date(2026, 9, 11)


# =========================================================================
# F. watchlist 过滤在全市场 fetch 之后
# =========================================================================


@pytest.mark.asyncio
async def test_f_watchlist_filter_happens_after_full_fetch() -> None:
    """网络 total=3（全市场），symbols={'600519'} → rows=1，raw/normalized 仍为 3。"""
    rows = [_row("600519"), _row("000001"), _row("300750")]
    client = _ScriptedClient([_page(rows, 3)])

    snap = await rp.fetch_realtime_a_share_snapshot(
        client, symbols={"600519"}, now=NOW  # type: ignore[arg-type]
    )

    assert [r.symbol for r in snap.rows] == ["600519"]
    # 证明不是 per-symbol 网络查询
    assert snap.raw_count == 3
    assert snap.normalized_count == 3
    assert len(client.calls) == 1
    assert client.calls[0]["pz"] == 100  # 全市场分页，未退化为按 symbol 查询


# =========================================================================
# G. 单位合同
# =========================================================================


@pytest.mark.asyncio
async def test_g_units_contract() -> None:
    """f5=100 手 → volume=10_000 股；f6 元；f2 元不缩放。"""
    rows = [_row("600519", close=12.34, volume=100, amount=123456.0, prev_close=12.20)]
    client = _ScriptedClient([_page(rows, 1)])

    snap = await rp.fetch_realtime_a_share_snapshot(client, now=NOW)  # type: ignore[arg-type]

    row = snap.rows[0]
    assert row.volume == Decimal("10000")      # 手 → 股
    assert row.close == Decimal("12.34")       # 元，不缩放
    assert row.amount == Decimal("123456")
    assert row.previous_close == Decimal("12.20")


# =========================================================================
# H. single-host 快照契约
# =========================================================================


@pytest.mark.asyncio
async def test_h_middle_page_failure_is_not_partial() -> None:
    client = _ScriptedClient(
        [_page([_row("600519"), _row("000001")], 4), httpx.ConnectError("page2 down")]
    )

    with pytest.raises(eod_prov.SnapshotProviderError):
        await rp.fetch_realtime_a_share_snapshot(client, page_size=2, now=NOW)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_h_second_realtime_host_restarts_from_page_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若未来加入第二个实时 host：失败后必须从 page 1 重新拉，不得续页拼接。"""
    primary, secondary = "push2.eastmoney.com", "push2mirror.eastmoney.com"
    monkeypatch.setattr(rp, "REALTIME_CLIST_HOSTS", (primary, secondary))

    client = _HostScriptedClient(
        {
            (primary, 1): _page([_row("600519"), _row("000001")], 4),
            (primary, 2): httpx.ConnectError("page2 down"),
            (secondary, 1): _page([_row("600519"), _row("000001")], 4),
            (secondary, 2): _page([_row("300750"), _row("002594")], 4),
        }
    )

    snap = await rp.fetch_realtime_a_share_snapshot(client, page_size=2, now=NOW)  # type: ignore[arg-type]

    assert snap.source_host == secondary
    assert (primary, 2) in client.calls, "必须确实在 primary 第 2 页失败过"
    assert (secondary, 1) in client.calls, "换 host 后必须从第 1 页重拉"
    assert snap.raw_count == 4
