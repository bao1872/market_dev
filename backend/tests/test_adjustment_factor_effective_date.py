"""Stage B2A — 公司行为「生效日」语义 + future-event 防泄漏（纯单元）。

锁定三条不变量：

1. ``effective corporate action = category == 1 AND event_date <= effective_as_of``
   —— 项目里只有一份过滤逻辑（``filter_effective_corporate_actions``）。
2. canonical calculator **默认 cutoff = 最新 raw bar 日期**：future XDXR 绝不提前
   进入 factor（否则最新几根 bar 会被未生效的除权改写）。
3. fingerprint 与 factor 共用同一 effective 集合：future event 不改变 fingerprint，
   到生效日才第一次进入 —— 否则真正生效时 fingerprint 不变，rebuild 永不触发。

不连 DB / 网络 / pytdx。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.adjustment_factor_calculator import (
    calculate_adjustment_factor_series,
    corporate_action_fingerprint,
    filter_effective_corporate_actions,
)
from app.services.adjustment_factor_service import AdjustmentFactorService

pytestmark = pytest.mark.pure_unit


# =============================================================================
# helpers
# =============================================================================


def _raw_df(dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": pd.to_datetime(dates),
        "close": closes,
    })


def _xdxr_df(events: list[dict]) -> pd.DataFrame:
    rows = []
    for e in events:
        rows.append({
            "date": pd.Timestamp(e["date"]),
            "category": e.get("category", 1),
            "fenhong": e.get("fenhong", 0.0),
            "songzhuangu": e.get("songzhuangu", 0.0),
            "peigu": e.get("peigu", 0.0),
            "peigujia": e.get("peigujia", 0.0),
        })
    return pd.DataFrame(rows, columns=[
        "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
    ])


def _factor_df(max_trade_date: str) -> pd.DataFrame:
    """模拟 canonical factor series（只关心最大 trade_date 作为 cutoff）。"""
    return pd.DataFrame({
        "trade_date": pd.to_datetime([max_trade_date]),
        "adj_factor": [1.0],
    })


class _FixedAdapter:
    """get_xdxr_info 返回固定 xdxr（模拟 TDX provider）。"""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def get_xdxr_info(self, symbol: str, **kwargs: object) -> pd.DataFrame:
        return self._df


# =============================================================================
# filter_effective_corporate_actions（唯一 effective 过滤）
# =============================================================================


def test_filter_effective_excludes_future_and_non_category1() -> None:
    events = _xdxr_df([
        {"date": "2026-09-01", "songzhuangu": 10},
        {"date": "2026-09-18", "songzhuangu": 10},          # future
        {"date": "2026-09-05", "category": 2, "fenhong": 1.0},  # 非除权除息
    ])

    out = filter_effective_corporate_actions(events, effective_as_of=date(2026, 9, 12))

    assert list(out["date"]) == [pd.Timestamp("2026-09-01")]


def test_filter_effective_includes_event_on_cutoff_day() -> None:
    events = _xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}])
    out = filter_effective_corporate_actions(events, effective_as_of=date(2026, 9, 12))
    assert list(out["date"]) == [pd.Timestamp("2026-09-12")]


def test_filter_effective_none_and_empty_return_empty() -> None:
    assert filter_effective_corporate_actions(None, effective_as_of=date(2026, 9, 12)).empty
    empty = pd.DataFrame(columns=["date", "category"])
    assert filter_effective_corporate_actions(empty, effective_as_of=date(2026, 9, 12)).empty


def test_filter_effective_missing_columns_returns_empty() -> None:
    df = pd.DataFrame({"foo": [1, 2]})
    assert filter_effective_corporate_actions(df, effective_as_of=date(2026, 9, 12)).empty


def test_filter_effective_tolerates_string_category() -> None:
    df = _xdxr_df([{"date": "2026-09-01", "songzhuangu": 10}])
    df["category"] = df["category"].astype(str)  # 数据源可能给字符串
    out = filter_effective_corporate_actions(df, effective_as_of=date(2026, 9, 12))
    assert list(out["date"]) == [pd.Timestamp("2026-09-01")]


# =============================================================================
# A. future event 默认不影响 canonical factor
# =============================================================================


def test_a_future_event_does_not_affect_default_factors() -> None:
    """raw 最新=9/12，事件=9/18（future）→ 默认 cutoff=9/12 → 全 1.0。"""
    raw = _raw_df(["2026-09-10", "2026-09-11", "2026-09-12"], [20.0, 20.0, 20.0])
    xdxr = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}])

    factors = calculate_adjustment_factor_series(raw, xdxr)

    assert factors == [1.0, 1.0, 1.0]


# =============================================================================
# B. 显式 business date 可纳入“今天事件”（即使今天还没有 raw bar）
# =============================================================================


def test_b_explicit_business_date_includes_today_event() -> None:
    """raw 只到 9/11，事件=9/12 10送10，effective_as_of=9/12 → 9/11 factor=0.5。"""
    raw = _raw_df(["2026-09-10", "2026-09-11"], [20.0, 20.0])
    xdxr = _xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}])

    factors = calculate_adjustment_factor_series(
        raw, xdxr, effective_as_of=date(2026, 9, 12),
    )

    # preclose = (20×10 - 0) / (10 + 0 + 10) = 200/20 = 10 → event_factor = 10/20 = 0.5
    assert abs(factors[0] - 0.5) < 1e-10
    assert abs(factors[1] - 0.5) < 1e-10


def test_b_default_cutoff_would_exclude_same_event() -> None:
    """同一场景不传 effective_as_of → 默认 cutoff=9/11 → 事件被排除（对照）。"""
    raw = _raw_df(["2026-09-10", "2026-09-11"], [20.0, 20.0])
    xdxr = _xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}])

    factors = calculate_adjustment_factor_series(raw, xdxr)

    assert factors == [1.0, 1.0]


# =============================================================================
# C. 显式 cutoff 在事件之前 → 事件被排除
# =============================================================================


def test_c_explicit_cutoff_before_event_excludes_it() -> None:
    raw = _raw_df(["2026-09-10", "2026-09-11"], [20.0, 20.0])
    xdxr = _xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}])

    factors = calculate_adjustment_factor_series(
        raw, xdxr, effective_as_of=date(2026, 9, 11),
    )

    assert factors == [1.0, 1.0]


# =============================================================================
# D. past + future 混合：只纳入 past
# =============================================================================


def test_d_past_and_future_events_only_past_applied() -> None:
    raw = _raw_df(
        ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-11"],
        [10.0, 10.0, 10.0, 10.0],
    )
    xdxr = _xdxr_df([
        {"date": "2026-09-01", "songzhuangu": 10},   # 已生效
        {"date": "2026-09-18", "songzhuangu": 10},   # future
    ])

    factors = calculate_adjustment_factor_series(
        raw, xdxr, effective_as_of=date(2026, 9, 12),
    )

    # 只有 9/01 生效（0.5）；9/18 绝不累积（否则 8/31 会是 0.25）
    assert abs(factors[0] - 0.5) < 1e-10
    assert abs(factors[1] - 1.0) < 1e-10
    assert abs(factors[2] - 1.0) < 1e-10
    assert abs(factors[3] - 1.0) < 1e-10


# =============================================================================
# E / F / G. fingerprint 生效日语义
# =============================================================================


def test_e_fingerprint_excludes_future_event() -> None:
    events = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}])

    fp, earliest = corporate_action_fingerprint(events, effective_as_of=date(2026, 9, 12))

    assert fp == ""
    assert earliest is None


def test_f_fingerprint_includes_event_on_effective_date() -> None:
    events = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}])

    fp, earliest = corporate_action_fingerprint(events, effective_as_of=date(2026, 9, 18))

    assert fp != ""
    assert earliest == date(2026, 9, 18)


def test_g_future_event_value_change_does_not_change_fingerprint_before_effective() -> None:
    """9/18 songzhuangu 10→20：cutoff=9/12 时 fingerprint 必须完全不变，9/18 才变。"""
    e10 = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}])
    e20 = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 20}])

    fp10_before, _ = corporate_action_fingerprint(e10, effective_as_of=date(2026, 9, 12))
    fp20_before, _ = corporate_action_fingerprint(e20, effective_as_of=date(2026, 9, 12))
    assert fp10_before == fp20_before == ""

    fp10_after, _ = corporate_action_fingerprint(e10, effective_as_of=date(2026, 9, 18))
    fp20_after, _ = corporate_action_fingerprint(e20, effective_as_of=date(2026, 9, 18))
    assert fp10_after != fp20_after != ""


def test_fingerprint_format_matches_legacy_formula() -> None:
    """字符串格式必须与历史实现一致（避免无意义地洗掉已存 Redis fingerprint）。"""
    events = _xdxr_df([
        {"date": "2026-09-01", "fenhong": 1.5, "songzhuangu": 10},
        {"date": "2026-09-05", "peigu": 3, "peigujia": 4.2},
    ])

    fp, earliest = corporate_action_fingerprint(events, effective_as_of=date(2026, 9, 30))

    legacy_parts = [
        f"{row['date']}|{row.get('fenhong', 0)}|{row.get('songzhuangu', 0)}|"
        f"{row.get('peigu', 0)}|{row.get('peigujia', 0)}"
        for _, row in events.sort_values("date").iterrows()
    ]
    expected = hashlib.sha256("\n".join(legacy_parts).encode("utf-8")).hexdigest()[:16]

    assert fp == expected
    assert len(fp) == 16
    assert earliest == date(2026, 9, 1)


# =============================================================================
# detect_company_action_change 集成：生效日前 / 生效日
# =============================================================================


def _wire_detect(monkeypatch: pytest.MonkeyPatch, service: AdjustmentFactorService):
    stored: dict[uuid.UUID, str] = {}
    monkeypatch.setattr(service, "_get_stored_fingerprint", lambda i: stored.get(i))
    monkeypatch.setattr(
        service, "_store_fingerprint", lambda i, fp: stored.__setitem__(i, fp)
    )
    return stored


@pytest.mark.asyncio
async def test_detect_future_event_does_not_trigger_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """latest factor=9/12，future XDXR=9/18 → 不得返回 earliest=9/18。"""
    service = AdjustmentFactorService()
    iid = uuid.uuid4()
    adapter = _FixedAdapter(_xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}]))
    monkeypatch.setattr(
        service, "get_factor_series", AsyncMock(return_value=_factor_df("2026-09-12"))
    )
    stored = _wire_detect(monkeypatch, service)

    result = await service.detect_company_action_change(
        None, iid, "600519", adapter, force_refresh=True,  # type: ignore[arg-type]
    )

    assert result is None
    # 存的是「截至 9/12 的有效集合」= 空
    assert stored[iid] == ""


@pytest.mark.asyncio
async def test_detect_event_enters_fingerprint_on_effective_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """factor coverage 进入 9/18 后，事件第一次进入 fingerprint → earliest=9/18。"""
    service = AdjustmentFactorService()
    iid = uuid.uuid4()
    adapter = _FixedAdapter(_xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}]))
    monkeypatch.setattr(
        service, "get_factor_series", AsyncMock(return_value=_factor_df("2026-09-18"))
    )
    stored = _wire_detect(monkeypatch, service)

    result = await service.detect_company_action_change(
        None, iid, "600519", adapter, force_refresh=True,  # type: ignore[arg-type]
    )

    assert result == date(2026, 9, 18)
    assert stored[iid] != ""


@pytest.mark.asyncio
async def test_detect_explicit_effective_as_of_skips_db_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 effective_as_of 时不得再查 DB cutoff（point-in-time 语义）。"""
    service = AdjustmentFactorService()
    iid = uuid.uuid4()
    adapter = _FixedAdapter(_xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}]))
    monkeypatch.setattr(
        service,
        "get_factor_series",
        AsyncMock(side_effect=AssertionError("显式 effective_as_of 不应读取 DB cutoff")),
    )
    stored = _wire_detect(monkeypatch, service)

    result = await service.detect_company_action_change(
        None, iid, "600519", adapter,  # type: ignore[arg-type]
        force_refresh=True,
        effective_as_of=date(2026, 9, 12),
    )

    assert result == date(2026, 9, 12)
    assert stored[iid] != ""


@pytest.mark.asyncio
async def test_detect_without_factor_baseline_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 canonical factor 基线 → 有效集合为空，不得凭空放行 future event。"""
    service = AdjustmentFactorService()
    iid = uuid.uuid4()
    adapter = _FixedAdapter(_xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}]))
    monkeypatch.setattr(
        service,
        "get_factor_series",
        AsyncMock(return_value=pd.DataFrame(columns=["trade_date", "adj_factor"])),
    )
    stored = _wire_detect(monkeypatch, service)

    result = await service.detect_company_action_change(
        None, iid, "600519", adapter, force_refresh=True,  # type: ignore[arg-type]
    )

    assert result is None
    assert stored[iid] == ""


@pytest.mark.asyncio
async def test_detect_empty_xdxr_clears_old_fingerprint_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stored fingerprint 非空 + fresh XDXR 为空 → 必须安全返回 None（不得 UnboundLocalError）。

    回归：旧实现在此场景下 `cutoff` 未绑定，logger 引用它 → UnboundLocalError。
    """
    import logging

    service = AdjustmentFactorService()
    iid = uuid.uuid4()
    adapter = _FixedAdapter(pd.DataFrame())

    # 空 XDXR 不得读取 factor DB
    monkeypatch.setattr(
        service,
        "get_factor_series",
        AsyncMock(side_effect=AssertionError("empty XDXR must not read factor cutoff")),
    )
    stored: dict[uuid.UUID, str] = {iid: "0123456789abcdef"}
    monkeypatch.setattr(service, "_get_stored_fingerprint", lambda i: stored.get(i))
    monkeypatch.setattr(
        service, "_store_fingerprint", lambda i, fp: stored.__setitem__(i, fp)
    )

    caplog.set_level(logging.INFO, logger="services.adjustment_factor_service")

    result = await service.detect_company_action_change(
        None, iid, "600519", adapter, force_refresh=True,  # type: ignore[arg-type]
    )

    assert result is None
    assert stored[iid] == ""          # 空 XDXR → effective fingerprint 为空串
    # cutoff 已定义（无显式值且未读 DB → None），日志可安全记录
    assert "cutoff=None" in caplog.text


@pytest.mark.asyncio
async def test_detect_empty_xdxr_with_explicit_cutoff_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """显式 effective_as_of + 空 XDXR + 旧 fingerprint 非空 → 安全返回 None，cutoff 可记录。"""
    import logging

    service = AdjustmentFactorService()
    iid = uuid.uuid4()
    adapter = _FixedAdapter(pd.DataFrame())

    monkeypatch.setattr(
        service,
        "get_factor_series",
        AsyncMock(side_effect=AssertionError("empty XDXR must not read factor cutoff")),
    )
    stored: dict[uuid.UUID, str] = {iid: "0123456789abcdef"}
    monkeypatch.setattr(service, "_get_stored_fingerprint", lambda i: stored.get(i))
    monkeypatch.setattr(
        service, "_store_fingerprint", lambda i, fp: stored.__setitem__(i, fp)
    )

    caplog.set_level(logging.INFO, logger="services.adjustment_factor_service")

    result = await service.detect_company_action_change(
        None, iid, "600519", adapter,  # type: ignore[arg-type]
        force_refresh=True,
        effective_as_of=date(2026, 9, 12),
    )

    assert result is None
    assert stored[iid] == ""
    assert "cutoff=2026-09-12" in caplog.text
