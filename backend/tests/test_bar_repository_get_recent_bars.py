"""bar_repository.get_recent_bars 集成测试（SubTask 1.4）。

测试内容：
1. 日线：插入 300 根 bar，断言 get_recent_bars(period="1d", limit=250) 返回恰好 250 根（升序）
2. 不足时返回实际可用数量（如插入 100 根，limit=250 返回 100 根）
3. 15m 周期：插入 4000 根 bar，断言 limit=3600 返回恰好 3600 根（升序）
4. 1m 周期：插入 5 根 bar，断言 limit=2 返回恰好 2 根（升序，最新两根）
5. 非法 period / limit 抛出 ValueError

测试约束：
- 使用 conftest.py 的 db_session / test_instrument fixtures（事务回滚，无副作用）
- 不连接真实行情源（pytdx），仅验证 DB 查询逻辑
- limit=250 / 3600 / 2 来自 indicator_contract 受控参数（与 NODE_CLUSTER_PRIMARY_BARS /
  NODE_CLUSTER_LOW_BARS / NODE_CLUSTER_MINUTE_BARS 一致）
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import indicator_contract as IC
from app.models.bar import Bar15Min, BarDaily, BarMinute
from app.repositories.bar_repository import get_recent_bars

# ============================================================
# 1. 日线：limit=250 返回恰好 250 根（升序）
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_daily_returns_exactly_250(
    db_session: AsyncSession, test_instrument,
):
    """插入 300 根日线，断言 get_recent_bars(period='1d', limit=250) 返回恰好 250 根。

    验证：
        - 返回行数 == 250（不超过 limit）
        - index 升序（最早在前，最新在后）
        - 最后一根是最新插入的 bar
    """
    inst_id = test_instrument.id
    base_date = date(2026, 6, 27)
    # 插入 300 根日线
    for i in range(300):
        d = base_date - timedelta(days=299 - i)
        db_session.add(BarDaily(
            instrument_id=inst_id,
            trade_date=d,
            open=Decimal("10.00"),
            high=Decimal("10.50"),
            low=Decimal("9.80"),
            close=Decimal(f"{10.0 + i * 0.01:.2f}"),
            volume=Decimal("100000"),
            amount=Decimal("1000000"),
            adj_factor=Decimal("1.0"),
        ))
    await db_session.flush()

    df = await get_recent_bars(db_session, inst_id, period="1d", limit=IC.NODE_CLUSTER_PRIMARY_BARS)

    assert len(df) == IC.NODE_CLUSTER_PRIMARY_BARS == 250
    # 升序：第一根最早，最后一根最新
    assert df.index.is_monotonic_increasing
    # 最后一根应当是最新日期
    assert df.index[-1].date() == base_date


# ============================================================
# 2. 不足时返回实际可用数量
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_daily_returns_actual_when_insufficient(
    db_session: AsyncSession, test_instrument,
):
    """插入 100 根日线，limit=250 时返回实际可用 100 根。"""
    inst_id = test_instrument.id
    base_date = date(2026, 6, 27)
    for i in range(100):
        d = base_date - timedelta(days=99 - i)
        db_session.add(BarDaily(
            instrument_id=inst_id,
            trade_date=d,
            open=Decimal("10.00"),
            high=Decimal("10.50"),
            low=Decimal("9.80"),
            close=Decimal("10.00"),
            volume=Decimal("100000"),
            amount=Decimal("1000000"),
            adj_factor=Decimal("1.0"),
        ))
    await db_session.flush()

    df = await get_recent_bars(db_session, inst_id, period="1d", limit=IC.NODE_CLUSTER_PRIMARY_BARS)

    assert len(df) == 100  # 不足 250，返回实际 100 根
    assert df.index.is_monotonic_increasing


# ============================================================
# 3. 15m 周期：limit=3600 返回恰好 3600 根（升序）
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_15min_returns_exactly_3600(
    db_session: AsyncSession, test_instrument,
):
    """插入 4000 根 15m bar，断言 limit=3600 返回恰好 3600 根（升序）。"""
    inst_id = test_instrument.id
    # 4000 根 15m bar ≈ 250 个交易日 × 16 根/天，从 2026-06-27 09:30 倒推
    base_ts = datetime(2026, 6, 27, 15, 0)
    # 每根 15 分钟，生成 4000 根（不考虑交易日历，纯连续时间序列用于测试）
    rows = []
    for i in range(4000):
        ts = base_ts - timedelta(minutes=15 * (3999 - i))
        rows.append(Bar15Min(
            instrument_id=inst_id,
            trade_time=ts,
            open=Decimal("10.00"),
            high=Decimal("10.50"),
            low=Decimal("9.80"),
            close=Decimal(f"{10.0 + (i % 100) * 0.01:.2f}"),
            volume=Decimal("100000"),
            amount=Decimal("1000000"),
            adj_factor=Decimal("1.0"),
        ))
    db_session.add_all(rows)
    await db_session.flush()

    df = await get_recent_bars(db_session, inst_id, period="15m", limit=IC.NODE_CLUSTER_LOW_BARS)

    assert len(df) == IC.NODE_CLUSTER_LOW_BARS == 3600
    assert df.index.is_monotonic_increasing
    # 最后一根是最新插入的
    assert df.index[-1] == pd.Timestamp(base_ts)


# ============================================================
# 4. 1m 周期：limit=2 返回恰好 2 根（最新两根）
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_1min_returns_exactly_2(
    db_session: AsyncSession, test_instrument,
):
    """插入 5 根 1m bar，断言 limit=2 返回恰好 2 根（最新两根，升序）。"""
    inst_id = test_instrument.id
    base_ts = datetime(2026, 6, 27, 10, 4)  # 10:04
    for i in range(5):
        ts = base_ts - timedelta(minutes=4 - i)  # 10:00, 10:01, 10:02, 10:03, 10:04
        db_session.add(BarMinute(
            instrument_id=inst_id,
            trade_time=ts,
            open=Decimal("10.00"),
            high=Decimal("10.50"),
            low=Decimal("9.80"),
            close=Decimal(f"{10.0 + i * 0.01:.2f}"),
            volume=Decimal("100000"),
            amount=Decimal("1000000"),
            adj_factor=Decimal("1.0"),
        ))
    await db_session.flush()

    df = await get_recent_bars(db_session, inst_id, period="1m", limit=IC.NODE_CLUSTER_MINUTE_BARS)

    assert len(df) == IC.NODE_CLUSTER_MINUTE_BARS == 2
    assert df.index.is_monotonic_increasing
    # 最新两根：10:03, 10:04
    assert df.index[0] == pd.Timestamp(datetime(2026, 6, 27, 10, 3))
    assert df.index[-1] == pd.Timestamp(datetime(2026, 6, 27, 10, 4))


# ============================================================
# 5. 无数据时返回空 DataFrame
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_empty_when_no_data(
    db_session: AsyncSession, test_instrument,
):
    """无数据时返回空 DataFrame（不抛异常，不拉 pytdx）。"""
    inst_id = test_instrument.id
    df = await get_recent_bars(db_session, inst_id, period="1d", limit=250)
    assert df.empty


# ============================================================
# 6. 非法 period / limit 抛出 ValueError
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_invalid_period_raises(
    db_session: AsyncSession, test_instrument,
):
    """非法 period 抛出 ValueError。"""
    with pytest.raises(ValueError, match="不支持的 period"):
        await get_recent_bars(db_session, test_instrument.id, period="1h", limit=10)


@pytest.mark.asyncio
async def test_get_recent_bars_invalid_limit_raises(
    db_session: AsyncSession, test_instrument,
):
    """limit <= 0 抛出 ValueError。"""
    with pytest.raises(ValueError, match="limit 必须 > 0"):
        await get_recent_bars(db_session, test_instrument.id, period="1d", limit=0)
    with pytest.raises(ValueError, match="limit 必须 > 0"):
        await get_recent_bars(db_session, test_instrument.id, period="1d", limit=-1)
