"""bar_repository.get_recent_bars 集成测试（SubTask 1.4）。

测试内容：
1. 日线：插入 300 根 bar，断言 get_recent_bars(period="1d", limit=250) 返回恰好 250 根（升序）
2. 不足时返回实际可用数量（如插入 100 根，limit=250 返回 100 根）
3. 15m 周期：插入 4000 根 bar，断言 limit=NODE_CLUSTER_LOW_BARS 返回恰好 4000 根（升序）
4. 1m 周期：插入 5 根 bar，断言 limit=2 返回恰好 2 根（升序，最新两根）
5. 非法 period / limit 抛出 ValueError

测试约束：
- 使用 conftest.py 的 db_session / test_instrument fixtures（事务回滚，无副作用）
- 不连接真实行情源（pytdx），仅验证 DB 查询逻辑
- limit=250 / 4000 / 2 来自 indicator_contract 受控参数（与 NODE_CLUSTER_PRIMARY_BARS /
  NODE_CLUSTER_LOW_BARS / NODE_CLUSTER_MINUTE_BARS 一致）
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import indicator_contract as IC  # noqa: N812
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
# 3. 15m 周期：limit=NODE_CLUSTER_LOW_BARS 返回恰好 4000 根（升序）
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_15min_returns_exactly_node_cluster_low_bars(
    db_session: AsyncSession, test_instrument,
):
    """插入 4000 根 15m bar，断言 limit=NODE_CLUSTER_LOW_BARS 返回恰好 4000 根（升序）。"""
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

    assert len(df) == IC.NODE_CLUSTER_LOW_BARS == 4000
    assert df.index.is_monotonic_increasing
    # 最后一根是最新插入的
    assert df.index[-1] == pd.Timestamp(base_ts)


# ============================================================
# 3b. 15m 周期：插入 5000 根，limit=4000 只返回最新 4000 根（丢弃最旧 1000 根）
#     C10 Step 6: 断言首尾时间，验证 DESC+LIMIT+reverse 语义
# ============================================================


@pytest.mark.asyncio
async def test_get_recent_bars_15min_limit_drops_oldest(
    db_session: AsyncSession, test_instrument,
):
    """插入 5000 根 15m bar，limit=4000 只返回最新 4000 根。

    C10 Step 6: 验证 SQL DESC + LIMIT + reverse-to-ASC 语义：
    - 返回行数 == 4000（不超过 limit，丢弃最旧 1000 根）
    - index 升序（最早在前，最新在后）
    - 首尾时间正确：第一根是第 1001 根（ts_1000），最后一根是第 5000 根（ts_4999）
    - 不包含最旧的 1000 根（ts_0 不在结果中）
    """
    inst_id = test_instrument.id
    base_ts = datetime(2026, 6, 27, 15, 0)
    total = 5000
    limit = IC.NODE_CLUSTER_LOW_BARS  # 4000
    # 生成 5000 根连续 15m bar，close 编码序号便于验证
    rows = []
    for i in range(total):
        ts = base_ts - timedelta(minutes=15 * (total - 1 - i))
        rows.append(Bar15Min(
            instrument_id=inst_id,
            trade_time=ts,
            open=Decimal("10.00"),
            high=Decimal("10.50"),
            low=Decimal("9.80"),
            close=Decimal(f"{i}.00"),  # close == 序号，便于验证
            volume=Decimal("100000"),
            amount=Decimal("1000000"),
            adj_factor=Decimal("1.0"),
        ))
    db_session.add_all(rows)
    await db_session.flush()

    df = await get_recent_bars(db_session, inst_id, period="15m", limit=limit)

    # 1. 只返回最新 4000 根
    assert len(df) == limit == 4000
    # 2. 升序
    assert df.index.is_monotonic_increasing
    # 3. 首尾时间断言：第一根是 ts[1000]，最后一根是 ts[4999]
    expected_first_ts = pd.Timestamp(
        base_ts - timedelta(minutes=15 * (total - 1 - 1000))
    )
    expected_last_ts = pd.Timestamp(base_ts)
    assert df.index[0] == expected_first_ts, (
        f"首根时间错误: expected {expected_first_ts}, got {df.index[0]}"
    )
    assert df.index[-1] == expected_last_ts, (
        f"末根时间错误: expected {expected_last_ts}, got {df.index[-1]}"
    )
    # 4. 验证丢弃了最旧 1000 根：close=0..999 不在结果中
    result_closes = set(df["close"].astype(float).astype(int))
    assert 0 not in result_closes, "最旧根（close=0）不应在结果中"
    assert 999 not in result_closes, "第 1000 根（close=999）不应在结果中"
    assert 1000 in result_closes, "第 1001 根（close=1000）应在结果中"
    assert 4999 in result_closes, "最新根（close=4999）应在结果中"


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
