"""[CP-V3-A3] MDAS 长期停牌边界修正测试。

验证 PROMPT.md §1 要求：
1. 停牌超过180天但更早存在4000根历史 → 必须最终取得4000（no_progress 不终止）
2. 真正新上市 → history_exhausted=True（到达 listing_date）
3. 数据源缺口 → no_progress 不等于 history_exhausted，继续扩展
4. 达到安全上限仍未到达真实历史边界 → INPUT_CONTRACT_VIOLATION（history_exhausted=False）

测试策略：
- mock query_fn 模拟不同 DB 响应
- 不依赖真实 DB 数据
- 验证 history_exhausted / coverage_reason / bars_count
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.market_data_aggregation_service import (
    _fetch_intraday_with_backfill,
)

_TZ = ZoneInfo("Asia/Shanghai")


def _make_bars(start: datetime, count: int, freq_minutes: int = 15) -> pd.DataFrame:
    """生成 count 根 15m bar，从 start 开始向前（更早）排列。"""
    times = pd.date_range(
        end=start, periods=count, freq=f"{freq_minutes}min", tz=_TZ
    )
    return pd.DataFrame(
        {
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.2,
            "volume": 1000,
            "amount": 10000.0,
            "adj_factor": 1.0,
        },
        index=times,
    )


# =============================================================================
# 测试 1: 停牌超过180天但更早存在4000根历史 → 必须取得4000
# =============================================================================


@pytest.mark.asyncio
async def test_a3_long_suspension_with_earlier_history() -> None:
    """[CP-V3-A3] 停牌>180天但更早有4000根 → no_progress 不终止，最终取得4000。

    场景：
    - 股票 listing_date=2020-01-01
    - 最近 200 天停牌（无 bar）
    - 更早有 4000+ 根 15m bar
    - 初始查询窗口（90天）→ 返回 0 根（停牌期间）
    - 第 2 轮扩展 90 天 → 仍 0 根（停牌期间）
    - 第 3 轮扩展 180 天 → 返回 4000 根

    旧逻辑（CP-V3-A2）：no_progress_count=2 → history_exhausted=True → degraded
    新逻辑（CP-V3-A3）：继续扩展 → 取得 4000 → history_exhausted=False
    """
    instrument_id = uuid.uuid4()
    listing_date = date(2020, 1, 1)
    now = datetime.now(_TZ)
    initial_start = now - timedelta(days=90)
    end = now

    # 更早的 4000 根 bar（在停牌期之前）
    early_bars = _make_bars(
        start=now - timedelta(days=400), count=4000
    )

    call_count = 0

    async def mock_query_fn(session, instr_id, start, end_dt):
        nonlocal call_count
        call_count += 1
        # 前 2 轮查询窗口在停牌期间 → 返回空
        if call_count <= 2:
            return pd.DataFrame()
        # 第 3 轮扩展到停牌期之前 → 返回 4000 根
        return early_bars.copy()

    bars_df, rounds, history_exhausted, reason = await _fetch_intraday_with_backfill(
        session=None,  # mock_query_fn 不使用 session
        instrument_id=instrument_id,
        timeframe="15m",
        initial_start=initial_start,
        end=end,
        query_fn=mock_query_fn,
        required_count=4000,
        listing_date=listing_date,
    )

    assert len(bars_df) >= 4000, (
        f"停牌>180天但更早有历史时必须取得4000，实际={len(bars_df)}，"
        f"rounds={rounds}, reason={reason}"
    )
    assert not history_exhausted, (
        f"no_progress 不应导致 history_exhausted=True，reason={reason}"
    )
    assert "met_after" in reason, f"应满足 required_count，reason={reason}"


# =============================================================================
# 测试 2: 真正新上市 → history_exhausted=True
# =============================================================================


@pytest.mark.asyncio
async def test_a3_truly_new_listing() -> None:
    """[CP-V3-A3] 真正新上市股票 → 到达 listing_date 仍不足 → history_exhausted=True。

    场景：
    - 股票 listing_date=2025-06-01（上市约 1.5 个月）
    - DB 中只有 500 根 15m bar
    - 扩展到 listing_date 仍不足 4000 → history_exhausted=True
    """
    instrument_id = uuid.uuid4()
    listing_date = date(2025, 6, 1)
    now = datetime.now(_TZ)
    initial_start = now - timedelta(days=90)
    end = now

    # 只有 500 根 bar
    available_bars = _make_bars(start=now - timedelta(days=30), count=500)

    async def mock_query_fn(session, instr_id, start, end_dt):
        # 返回可用 bars 的子集（在查询窗口内的）
        if start.date() <= listing_date:
            return available_bars.copy()
        mask = available_bars.index >= pd.Timestamp(start)
        filtered = available_bars[mask]
        return filtered.copy() if not filtered.empty else pd.DataFrame()

    bars_df, rounds, history_exhausted, reason = await _fetch_intraday_with_backfill(
        session=None,
        instrument_id=instrument_id,
        timeframe="15m",
        initial_start=initial_start,
        end=end,
        query_fn=mock_query_fn,
        required_count=4000,
        listing_date=listing_date,
    )

    assert history_exhausted, (
        f"新上市到达 listing_date 仍不足应 history_exhausted=True，reason={reason}"
    )
    assert "history_exhausted" in reason, (
        f"原因应包含 history_exhausted，实际={reason}"
    )
    assert len(bars_df) <= 500, (
        f"新上市不足4000时 bars 不应超过可用量，实际={len(bars_df)}"
    )


# =============================================================================
# 测试 3: 数据源缺口 → no_progress 不等于 history_exhausted
# =============================================================================


@pytest.mark.asyncio
async def test_a3_data_source_gap_no_progress_not_exhausted() -> None:
    """[CP-V3-A3] 数据源缺口（中间有段无数据）→ no_progress 不终止。

    场景：
    - 股票 listing_date=2019-01-01
    - 最近 90 天有 2000 根
    - 90~270 天前无数据（数据缺口）
    - 270+ 天前有 2000+ 根
    - 扩展穿过缺口后取得 4000

    旧逻辑：no_progress_count=2 在缺口处终止 → history_exhausted=True
    新逻辑：继续扩展穿过缺口 → 取得 4000
    """
    instrument_id = uuid.uuid4()
    listing_date = date(2019, 1, 1)
    now = datetime.now(_TZ)
    initial_start = now - timedelta(days=90)
    end = now

    recent_bars = _make_bars(start=now - timedelta(days=60), count=2000)
    older_bars = _make_bars(start=now - timedelta(days=400), count=2000)

    call_count = 0

    async def mock_query_fn(session, instr_id, start, end_dt):
        nonlocal call_count
        call_count += 1
        result_parts = []
        # 最近窗口有数据
        mask1 = recent_bars.index >= pd.Timestamp(start)
        filtered1 = recent_bars[mask1]
        if not filtered1.empty:
            result_parts.append(filtered1)
        # 更早窗口（扩展后）有数据
        mask2 = older_bars.index >= pd.Timestamp(start)
        filtered2 = older_bars[mask2]
        if not filtered2.empty:
            result_parts.append(filtered2)
        if not result_parts:
            return pd.DataFrame()
        return pd.concat(result_parts)

    bars_df, rounds, history_exhausted, reason = await _fetch_intraday_with_backfill(
        session=None,
        instrument_id=instrument_id,
        timeframe="15m",
        initial_start=initial_start,
        end=end,
        query_fn=mock_query_fn,
        required_count=4000,
        listing_date=listing_date,
    )

    assert len(bars_df) >= 4000, (
        f"穿过数据缺口后应取得4000，实际={len(bars_df)}，rounds={rounds}，reason={reason}"
    )
    assert not history_exhausted, (
        f"数据缺口不应导致 history_exhausted=True，reason={reason}"
    )


# =============================================================================
# 测试 4: 达到安全上限未到真实历史边界 → INPUT_CONTRACT_VIOLATION
# =============================================================================


@pytest.mark.asyncio
async def test_a3_max_rounds_not_at_boundary_input_contract_violation() -> None:
    """[CP-V3-A3] 达到 max_rounds 但未到达 listing_date → history_exhausted=False。

    场景：
    - 股票 listing_date=2010-01-01（很早上市）
    - DB 中有数据但分散在极长时间范围
    - 每轮只返回少量 bar（模拟稀疏数据）
    - 达到 _MAX_BACKFILL_ROUNDS 仍不足 4000
    - 但 current_start 仍未到 listing_date → INPUT_CONTRACT_VIOLATION
    """
    instrument_id = uuid.uuid4()
    listing_date = date(2010, 1, 1)
    now = datetime.now(_TZ)
    initial_start = now - timedelta(days=90)
    end = now

    # 每轮只返回 100 根（模拟稀疏数据，max_rounds=10 → 最多 1000 根 < 4000）
    async def mock_query_fn(session, instr_id, start, end_dt):
        return _make_bars(start=pd.Timestamp(start), count=100)

    bars_df, rounds, history_exhausted, reason = await _fetch_intraday_with_backfill(
        session=None,
        instrument_id=instrument_id,
        timeframe="15m",
        initial_start=initial_start,
        end=end,
        query_fn=mock_query_fn,
        required_count=4000,
        listing_date=listing_date,
    )

    assert not history_exhausted, (
        f"未到 listing_date 不应 history_exhausted=True，reason={reason}"
    )
    assert "max_rounds_reached" in reason, (
        f"应返回 max_rounds_reached，实际={reason}"
    )
    # 调用方（NodeClusterInputProvider）会将 history_exhausted=False + count<4000
    # 判定为 INPUT_CONTRACT_VIOLATION
    assert len(bars_df) < 4000, "max_rounds 不足时 bars 应 < 4000"


# =============================================================================
# 测试 5: 到达 listing_date 且有数据但不足 → history_exhausted=True
# =============================================================================


@pytest.mark.asyncio
async def test_a3_at_listing_date_insufficient_history_exhausted() -> None:
    """[CP-V3-A3] 到达 listing_date 有数据但不足4000 → history_exhausted=True。

    场景：
    - 股票 listing_date=2024-06-01（上市约1年）
    - DB 中有 2000 根 15m bar
    - 扩展到 listing_date 后仍只有 2000 → history_exhausted=True
    """
    instrument_id = uuid.uuid4()
    listing_date = date(2024, 6, 1)
    now = datetime.now(_TZ)
    initial_start = now - timedelta(days=90)
    end = now

    available_bars = _make_bars(start=now - timedelta(days=300), count=2000)

    async def mock_query_fn(session, instr_id, start, end_dt):
        # 统一为 naive 比较，避免 tz-aware vs tz-naive 冲突
        start_ts = pd.Timestamp(start)
        if start_ts.tzinfo is not None:
            start_ts = start_ts.tz_localize(None)
        idx_naive = available_bars.index.tz_localize(None) if available_bars.index.tz else available_bars.index
        mask = idx_naive >= start_ts
        filtered = available_bars[mask]
        return filtered.copy() if not filtered.empty else pd.DataFrame()

    bars_df, rounds, history_exhausted, reason = await _fetch_intraday_with_backfill(
        session=None,
        instrument_id=instrument_id,
        timeframe="15m",
        initial_start=initial_start,
        end=end,
        query_fn=mock_query_fn,
        required_count=4000,
        listing_date=listing_date,
    )

    assert history_exhausted, (
        f"到 listing_date 仍不足应 history_exhausted=True，reason={reason}"
    )
    assert "history_exhausted" in reason, f"原因应含 history_exhausted，实际={reason}"
