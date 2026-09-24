"""[PANJI-MARKET-OVERVIEW] index_facts 纯单元测试（不连 pytdx / 不连 DB）。

覆盖 Reviewer 修正后的合同：
A. 整段同步 pytdx 会话经 asyncio.to_thread 跑一次；只开一次 session、取四条 series。
B. ``get_index_daily_bars`` keyword-only 调用（回归：positionally 传参必须 TypeError）。
C. 880006 严格解码：finite / >= 0 / 整数值；不合法（51.4 / 负数 / NaN）直接 raise。
C2. TDX 最小刻度编码：精确 0.01 为零家数哨兵，归一化为整数 0；其余非整数（0.02 / 0.10 /
   1.01 / NaN / inf）一律 raise（provider 漂移，fail-closed）。
D. 指数收盘必须 finite 且 > 0；<= 0 直接 raise。
E. exact-date merge：四条 series 按日期合并为 MarketIndexFacts（缺字段为 None）。
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

import app.core.pytdx_adapter as pytdx_mod
from app.core.pytdx_adapter import PytdxAdapter
from app.services import market_dashboard_index_facts as idx

_D = date(2026, 9, 18)


def _df(close, open_=None):
    row = {"datetime": pd.Timestamp(_D), "close": close}
    if open_ is not None:
        row["open"] = open_
    return pd.DataFrame([row])


_DATA_OK = {
    "000001": _df(3300.0),
    "399001": _df(12000.0),
    "399006": _df(2500.0),
    "880006": _df(10.0, 3.0),
}


class _FakePytdx:
    """keyword-only 的 get_index_daily_bars，强制调用方显式传参（回归守卫）。"""

    def __init__(self, data: dict):
        self.data = data
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_index_daily_bars(self, *, market, code, start, end):
        self.calls.append((market, code, start, end))
        df = self.data.get(code)
        return df if df is not None else pd.DataFrame(columns=["datetime", "close", "open"])


def _run(monkeypatch, data):
    fake = _FakePytdx(data)
    monkeypatch.setattr(idx, "PytdxAdapter", lambda *a, **k: fake)
    return asyncio.run(idx.fetch_market_index_facts(_D)), fake


# ---------------------------------------------------------------
# A + E. 一次线程、四条 series、exact-merge
# ---------------------------------------------------------------
def test_fetch_runs_once_in_thread_and_merges(monkeypatch):
    facts, fake = _run(monkeypatch, _DATA_OK)
    assert len(fake.calls) == 4
    codes = [c[1] for c in fake.calls]
    assert codes == ["000001", "399001", "399006", "880006"]
    # 只开一次 session：__enter__ 一次
    f = facts[_D]
    assert f.sse_close == pytest.approx(3300.0)
    assert f.szse_close == pytest.approx(12000.0)
    assert f.chinext_close == pytest.approx(2500.0)
    assert f.limit_up_count == 10
    assert f.limit_down_count == 3


# ---------------------------------------------------------------
# B. keyword-only 合同（真实 adapter 方法签名，无网络）
# ---------------------------------------------------------------
def test_get_index_daily_bars_is_keyword_only():
    params = inspect.signature(PytdxAdapter.get_index_daily_bars).parameters
    assert any(p.kind == inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_wrapper_uses_keyword_args(monkeypatch):
    # 若 index_facts 改为 positional 传参，fake 的 keyword-only 方法会抛 TypeError → 失败。
    _, fake = _run(monkeypatch, _DATA_OK)
    for market, code, start, end in fake.calls:
        assert market is not None and code is not None and start is not None and end is not None


# ---------------------------------------------------------------
# C. 880006 严格解码
# ---------------------------------------------------------------
def test_decode_880006_non_integer_raises(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(51.4, 3.0)  # 非整数 → 数据源漂移
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


def test_decode_880006_negative_raises(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(10.0, -3.0)
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


def test_decode_880006_nan_raises(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(float("nan"), 3.0)
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


# ---------------------------------------------------------------
# C2. TDX 零家数最小刻度编码：精确 0.01 → 0；其余非整数一律 raise
# ---------------------------------------------------------------
def test_decode_880006_open_0_01_normalizes_to_zero(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(51.0, 0.01)  # open 零家数 → 0
    facts, _ = _run(monkeypatch, data)
    f = facts[_D]
    assert f.limit_up_count == 51
    assert f.limit_down_count == 0


def test_decode_880006_close_0_01_normalizes_to_zero(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(0.01, 14.0)  # close 零家数 → 0
    facts, _ = _run(monkeypatch, data)
    f = facts[_D]
    assert f.limit_up_count == 0
    assert f.limit_down_count == 14


def test_decode_880006_exact_zero_still_zero(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(0.0, 0.0)
    facts, _ = _run(monkeypatch, data)
    f = facts[_D]
    assert f.limit_up_count == 0
    assert f.limit_down_count == 0


def test_decode_880006_normal_counts_unaffected(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(74.0, 1.0)  # 真实整数值保持不变
    facts, _ = _run(monkeypatch, data)
    f = facts[_D]
    assert f.limit_up_count == 74
    assert f.limit_down_count == 1


def test_decode_880006_rejects_0_02(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(51.0, 0.02)  # 非精确 0.01 → 漂移
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


def test_decode_880006_rejects_0_10(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(0.10, 3.0)
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


def test_decode_880006_rejects_1_01(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(1.01, 3.0)
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


def test_decode_880006_rejects_inf(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = _df(float("inf"), 3.0)
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


# ---------------------------------------------------------------
# D. 指数收盘必须 finite 且 > 0
# ---------------------------------------------------------------
def test_index_close_non_positive_raises(monkeypatch):
    data = dict(_DATA_OK)
    data["000001"] = _df(0.0)  # <= 0 非法
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


def test_index_close_nan_raises(monkeypatch):
    data = dict(_DATA_OK)
    data["000001"] = _df(float("nan"))
    with pytest.raises(ValueError):
        _run(monkeypatch, data)


# ---------------------------------------------------------------
# 缺字段：某 series 当天无数据 → 该日事实字段为 None（不抛）
# ---------------------------------------------------------------
def test_missing_series_yields_none_field(monkeypatch):
    data = dict(_DATA_OK)
    data["880006"] = pd.DataFrame(columns=["datetime", "close", "open"])  # 空
    facts, _ = _run(monkeypatch, data)
    f = facts[_D]
    assert f.limit_up_count is None
    assert f.limit_down_count is None
    assert f.sse_close == pytest.approx(3300.0)
