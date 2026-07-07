"""测试 DSA visual_segments.points.time 按 timeframe 序列化（PR #34 RED）。

根因：
    dynamic_swing_anchored_vwap._make_segment 把 segment point time 写死为
    strftime("%Y-%m-%d")；dsa_selector.compute_indicators / compute_dsa_bundle
    anchor 也写死 YYYY-MM-DD。
    15m/1h K 线时间含 THH:MM:SS，前端 normalizeChartTime('15m') 要求 raw 含
    HH:MM 才能产生 canonical key，否则返回 None，dsa_polyline renderer matched=0，
    开关打开也画不出来。

修复目标：
    新增 format_dsa_time(x)：若 Timestamp 含时间部分（hour/minute/second 不全为 0）
    返回 isoformat()（含 T），否则返回 strftime("%Y-%m-%d")。
    _make_segment / compute_dsa_bundle anchor / compute_indicators 全部改用 format_dsa_time。

不变：
    dsa_vwap / dsa_dir / regime_id / visual_segments 数值与方向计算不变。

用法:
    APP_ENV=test backend/.venv/bin/python -m pytest backend/tests/test_dsa_visual_segments_time_format.py -v
"""
from __future__ import annotations

import re
import uuid
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.constants.indicator_contract import DSA_LOOKBACK
from app.strategy.runtime import MarketDataContext
from app.strategy.selectors.dsa_selector import (
    MIN_DIR_BARS,
    DSASelector,
    compute_dsa_bundle,
)
from app.strategy_assets.algorithms.features.atr_rope_event_factor_lab_v4 import (
    ATRRopeConfig,
)
from app.strategy_assets.algorithms.features.dynamic_swing_anchored_vwap import (
    DSAConfig,
)

# ---------------------------------------------------------------------------
# 测试数据工厂
# ---------------------------------------------------------------------------


def _build_synthetic_bars_15m(n: int = 250, seed: int = 7) -> pd.DataFrame:
    """构造 15m 周期合成行情（A 股交易时段 09:30-11:30 / 13:00-15:00）。

    生成 n 根连续 15m bar，时间落在 A 股交易时段内，确保 hour/minute 不全为 0，
    从而触发 format_dsa_time 走 isoformat() 分支。
    """
    np.random.seed(seed)
    # 用 bdate_range 生成工作日，每天 16 根 15m（09:45-11:30 = 8 根，13:00-15:00 = 8 根）
    base = pd.Timestamp("2026-06-01 09:45:00")
    times: list[pd.Timestamp] = []
    cur = base
    while len(times) < n:
        # 09:45-11:30 共 8 根（09:45/10:00/10:15/10:30/10:45/11:00/11:15/11:30）
        # 13:00-15:00 共 8 根（13:00/13:15/.../14:45）
        if cur.weekday() < 5:
            t = cur.time()
            if (
                pd.Timestamp("09:45:00").time() <= t <= pd.Timestamp("11:30:00").time()
                or pd.Timestamp("13:00:00").time() <= t <= pd.Timestamp("15:00:00").time()
            ):
                times.append(cur)
        cur += pd.Timedelta(minutes=15)
        # 跨日：超过 15:00 直接跳到次日 09:45
        if cur.time() > pd.Timestamp("15:00:00").time():
            cur = (cur + pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=9, minutes=45)
            # 跳过周末
            while cur.weekday() >= 5:
                cur += pd.Timedelta(days=1)

    idx = pd.DatetimeIndex(times[:n])

    start_price = 10.0
    daily_returns = np.random.uniform(0.001, 0.005, size=n)
    daily_returns[::9] = -0.003
    close = start_price * np.cumprod(1 + daily_returns)
    close = np.maximum(close, 1.0)
    open_ = close * (1 + np.random.uniform(-0.003, 0.003, size=n))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.001, 0.005, size=n))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.001, 0.005, size=n))
    volume = np.random.uniform(200000, 800000, size=n)
    amount = volume * close

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        index=idx,
    )


def _build_synthetic_bars_1d(n: int = 250, seed: int = 42) -> pd.DataFrame:
    """构造 1d 周期合成行情（与 test_dsa_visual_segments 共享口径）。"""
    np.random.seed(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    daily_returns = np.random.uniform(0.003, 0.008, size=n)
    daily_returns[::7] = -0.002
    close = 10.0 * np.cumprod(1 + daily_returns)
    close = np.maximum(close, 1.0)
    open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.001, 0.01, size=n))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.001, 0.01, size=n))
    volume = np.random.uniform(500000, 2000000, size=n)
    amount = volume * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        index=idx,
    )


def _build_config(lookback: int | None = DSA_LOOKBACK) -> dict:
    return {
        "dsa_config": DSAConfig(),
        "rope_config": ATRRopeConfig(regime_lookback=55),
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": lookback,
    }


def _make_mock_version(lookback: int = DSA_LOOKBACK) -> MagicMock:
    version = MagicMock()
    version.id = uuid.uuid4()
    version.manifest = {
        "strategy_id": "dsa_selector",
        "kind": "selector",
        "version": "1.1.0",
        "parameters": [{"key": "algorithm.lookback", "type": "integer", "default": lookback}],
        "resource_budget": {"target_ms_per_instrument": 5000},
    }
    return version


# 正则：15m/1h 时间含 "T" 或 " " 后接 HH:MM
_INTRADAY_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
# 正则：1d 仅日期
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# 测试 1: compute_dsa_bundle 15m visual_segments.points.time 含 HH:MM
# ---------------------------------------------------------------------------


class TestComputeDsaBundleIntradayTimeFormat:
    """15m/1h 下 compute_dsa_bundle 返回的 visual_segments.points.time 含 HH:MM。"""

    def test_15m_visual_segments_points_time_contains_hhmm(self):
        """15m 行情下 visual_segments.points.time 必须含 HH:MM（或 T）。"""
        bars = _build_synthetic_bars_15m(n=DSA_LOOKBACK)
        bundle = compute_dsa_bundle(bars, _build_config())
        segs = bundle["visual_segments"]
        assert len(segs) > 0, "15m 250 根行情应产生 segment"

        all_times: list[str] = []
        for seg in segs:
            for pt in seg["points"]:
                t = pt["time"]
                assert isinstance(t, str), f"point.time 必须为字符串，实际 {type(t)}"
                all_times.append(t)

        assert len(all_times) > 0, "visual_segments.points 至少有一个点"
        non_match = [t for t in all_times if not _INTRADAY_TIME_RE.match(t)]
        assert not non_match, (
            f"15m segment point time 必须含 HH:MM（[T ]\\d{2}:\\d{2}），"
            f"但发现纯日期格式: {non_match[:3]}"
        )

    def test_15m_anchor_time_contains_hhmm(self):
        """15m 行情下 anchor.time 必须含 HH:MM（或 T）。"""
        bars = _build_synthetic_bars_15m(n=DSA_LOOKBACK)
        bundle = compute_dsa_bundle(bars, _build_config())
        anchor_times = bundle["anchor"]["time"]
        assert len(anchor_times) > 0, "15m 250 根行情应产生 anchor"
        non_match = [t for t in anchor_times if not _INTRADAY_TIME_RE.match(t)]
        assert not non_match, (
            f"15m anchor.time 必须含 HH:MM（[T ]\\d{2}:\\d{2}），"
            f"但发现纯日期格式: {non_match[:3]}"
        )

    def test_15m_factor_time_is_intraday_index(self):
        """15m 行情下 factor_time 保留原始 intraday DatetimeIndex。"""
        bars = _build_synthetic_bars_15m(n=DSA_LOOKBACK)
        bundle = compute_dsa_bundle(bars, _build_config())
        factor_time = bundle["factor_time"]
        # 至少有一个 Timestamp 含非零 hour/minute
        has_intraday = any(
            (ts.hour != 0 or ts.minute != 0) for ts in factor_time
        )
        assert has_intraday, "15m factor_time 必须含 intraday 时间（hour/minute 不全 0）"


# ---------------------------------------------------------------------------
# 测试 2: DSASelector.compute_indicators 15m time 含 HH:MM
# ---------------------------------------------------------------------------


class TestComputeIndicatorsIntradayTimeFormat:
    """15m/1h 下 DSASelector.compute_indicators 返回的 time 数组含 HH:MM。"""

    @pytest.mark.asyncio
    async def test_15m_compute_indicators_time_contains_hhmm(self):
        """15m 行情下 compute_indicators.time 必须含 HH:MM（或 T）。"""
        bars = _build_synthetic_bars_15m(n=DSA_LOOKBACK)
        selector = DSASelector()
        await selector.initialize(_make_mock_version())
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="000100",
            bars_daily=bars,
            trade_date=date(2026, 6, 18),
        )
        result = await selector.compute_indicators(ctx)
        assert len(result["time"]) > 0, "15m compute_indicators.time 不应为空"
        non_match = [t for t in result["time"] if not _INTRADAY_TIME_RE.match(t)]
        assert not non_match, (
            f"15m compute_indicators.time 必须含 HH:MM，"
            f"但发现纯日期格式: {non_match[:3]}"
        )

    @pytest.mark.asyncio
    async def test_15m_compute_indicators_visual_segments_time_contains_hhmm(self):
        """15m 行情下 compute_indicators.visual_segments.points.time 含 HH:MM。"""
        bars = _build_synthetic_bars_15m(n=DSA_LOOKBACK)
        selector = DSASelector()
        await selector.initialize(_make_mock_version())
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="000100",
            bars_daily=bars,
            trade_date=date(2026, 6, 18),
        )
        result = await selector.compute_indicators(ctx)
        segs = result["visual_segments"]
        assert len(segs) > 0, "15m visual_segments 不应为空"
        all_times = [pt["time"] for seg in segs for pt in seg["points"]]
        non_match = [t for t in all_times if not _INTRADAY_TIME_RE.match(t)]
        assert not non_match, (
            f"15m visual_segments.points.time 必须含 HH:MM，"
            f"但发现纯日期格式: {non_match[:3]}"
        )


# ---------------------------------------------------------------------------
# 测试 3: 1d 仍返回日期格式
# ---------------------------------------------------------------------------


class TestComputeDsaBundleDailyTimeFormat:
    """1d 下 compute_dsa_bundle / compute_indicators 仍返回 YYYY-MM-DD 日期格式。"""

    def test_1d_visual_segments_points_time_is_date_only(self):
        """1d 行情下 visual_segments.points.time 必须为 YYYY-MM-DD。"""
        bars = _build_synthetic_bars_1d(n=DSA_LOOKBACK)
        bundle = compute_dsa_bundle(bars, _build_config())
        segs = bundle["visual_segments"]
        assert len(segs) > 0, "1d 250 根行情应产生 segment"
        all_times = [pt["time"] for seg in segs for pt in seg["points"]]
        non_match = [t for t in all_times if not _DATE_ONLY_RE.match(t)]
        assert not non_match, (
            f"1d visual_segments.points.time 必须为 YYYY-MM-DD，"
            f"但发现带时间格式: {non_match[:3]}"
        )

    def test_1d_anchor_time_is_date_only(self):
        """1d 行情下 anchor.time 必须为 YYYY-MM-DD。"""
        bars = _build_synthetic_bars_1d(n=DSA_LOOKBACK)
        bundle = compute_dsa_bundle(bars, _build_config())
        anchor_times = bundle["anchor"]["time"]
        assert len(anchor_times) > 0, "1d 250 根行情应产生 anchor"
        non_match = [t for t in anchor_times if not _DATE_ONLY_RE.match(t)]
        assert not non_match, (
            f"1d anchor.time 必须为 YYYY-MM-DD，但发现带时间格式: {non_match[:3]}"
        )

    @pytest.mark.asyncio
    async def test_1d_compute_indicators_time_is_date_only(self):
        """1d 行情下 compute_indicators.time 必须为 YYYY-MM-DD。"""
        bars = _build_synthetic_bars_1d(n=DSA_LOOKBACK)
        selector = DSASelector()
        await selector.initialize(_make_mock_version())
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=bars,
            trade_date=date(2026, 6, 18),
        )
        result = await selector.compute_indicators(ctx)
        assert len(result["time"]) > 0, "1d compute_indicators.time 不应为空"
        non_match = [t for t in result["time"] if not _DATE_ONLY_RE.match(t)]
        assert not non_match, (
            f"1d compute_indicators.time 必须为 YYYY-MM-DD，"
            f"但发现带时间格式: {non_match[:3]}"
        )


# ---------------------------------------------------------------------------
# 测试 4: 15m visual_segments.points.time 与 source_bar_times canonical 可匹配
# ---------------------------------------------------------------------------


class TestIntradayCanonicalAlignment:
    """15m visual_segments.points.time 与 source_bar_times canonical 可匹配。

    模拟前端 normalizeChartTime('15m') 行为：raw 含 "YYYY-MM-DD HH:MM" 才返回 canonical。
    若 segment time 仍是 YYYY-MM-DD（无 HH:MM），canonical 为 None，matched=0。
    """

    def test_15m_segment_times_match_source_bar_times(self):
        """15m visual_segments.points.time 与 source_bar_times 至少 50% 匹配。"""
        bars = _build_synthetic_bars_15m(n=DSA_LOOKBACK)
        bundle = compute_dsa_bundle(bars, _build_config())
        # 模拟 indicator_service 计算 source_bar_times 的方式
        # （使用 format_dsa_time 后，15m 应与 segment time 同口径）
        source_canonical: set[str] = set()
        for ts in bars.index:
            ts = pd.Timestamp(ts)
            if ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0:
                key = ts.strftime("%Y-%m-%d")
            else:
                key = ts.isoformat()
            # 提取 YYYY-MM-DD HH:MM
            m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", key)
            source_canonical.add(m.group(1) + " " + m.group(2) if m else key)

        segs = bundle["visual_segments"]
        matched = 0
        total = 0
        for seg in segs:
            for pt in seg["points"]:
                total += 1
                m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", pt["time"])
                if m:
                    canonical = m.group(1) + " " + m.group(2)
                    if canonical in source_canonical:
                        matched += 1

        assert total > 0, "visual_segments.points 至少有一个点"
        ratio = matched / total
        assert ratio > 0.5, (
            f"15m segment times 与 source_bar_times 匹配率应 > 0.5，"
            f"实际 {ratio:.3f} (matched={matched}, total={total})"
        )


# ---------------------------------------------------------------------------
# 模块自测入口
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import asyncio

    async def _self_test():
        bars = _build_synthetic_bars_15m(n=DSA_LOOKBACK)
        bundle = compute_dsa_bundle(bars, _build_config())
        print(f"15m factor_time 前 3: {list(bundle['factor_time'][:3])}")
        print(f"15m anchor.time 前 3: {bundle['anchor']['time'][:3]}")
        seg0 = bundle["visual_segments"][0]
        print(f"15m segment 0 direction={seg0['direction']}, points={len(seg0['points'])}")
        print(f"15m segment 0 first 3 points.time: {[p['time'] for p in seg0['points'][:3]]}")

        selector = DSASelector()
        await selector.initialize(_make_mock_version())
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="000100",
            bars_daily=bars,
            trade_date=date(2026, 6, 18),
        )
        result = await selector.compute_indicators(ctx)
        print(f"compute_indicators.time 前 3: {result['time'][:3]}")

        # 1d 对照
        bars_1d = _build_synthetic_bars_1d(n=DSA_LOOKBACK)
        bundle_1d = compute_dsa_bundle(bars_1d, _build_config())
        seg0_1d = bundle_1d["visual_segments"][0]
        print(f"1d segment 0 first 3 points.time: {[p['time'] for p in seg0_1d['points'][:3]]}")

    asyncio.run(_self_test())
