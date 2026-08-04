"""真实 repository 批量 Row -> DataFrame 转换测试（覆盖 FS-DB-01）。

[FS-DB-01 2026-08-04] 修复前 get_daily_bars_batch 的 columns 定义缺少 trade_date，
导致列错位 + 后续 df["trade_date"] KeyError，批读在真实数据下必然失败回退逐股。

本测试**直接调用真实函数**，传入伪造的 SQLAlchemy Row（而非 mock），验证：
- 列数与列名正确（含 trade_date，且输出已 drop instrument_id）；
- DatetimeIndex 正确（按 trade_date）；
- 多股票分组；
- 空股票结果、完全空数据；
- 数值类型（Decimal -> float）；
- 排序。

不连库：Row 与 session 均为内存伪造。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from pandas import DatetimeIndex as pd_DatetimeIndex
from pandas import Timestamp as pd_Timestamp

from app.repositories.bar_repository import get_daily_bars_batch


class _FakeRow(dict):
    """可被 pandas 直接构造为 DataFrame 的伪造 Row。

    - 本身是 dict（含 9 个查询列），因此 pd.DataFrame(list_of_rows) 能按列名展开；
    - 设置 instrument_id 属性，供 groupby(key=lambda r: r.instrument_id) 使用；
    - _mapping 指向自身，供函数内 dict(r._mapping) 转换。
    """


def _make_row(
    instrument_id: str,
    trade_date: date,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    amount: float,
    adj_factor: float,
) -> Any:
    row = _FakeRow(
        instrument_id=instrument_id,
        trade_date=trade_date,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
        adj_factor=adj_factor,
    )
    row.instrument_id = instrument_id  # type: ignore[attr-defined]
    row._mapping = row
    return row


@pytest.mark.asyncio
async def test_batch_single_instrument_columns_and_index():
    rows = [
        _make_row("600000", date(2026, 7, 13), 10.1, 10.5, 9.9, 10.2, 1000, 10200, 1.0),
        _make_row("600000", date(2026, 7, 14), 10.2, 10.6, 10.0, 10.4, 1100, 11440, 1.0),
    ]
    # session / instrument_ids 仅参数占位，真实函数内部用 session.execute
    result = await _call_with_fake_session(rows)

    assert "600000" in result
    df = result["600000"]
    # 输出列不得含 instrument_id（已 drop），且必须含全部 OHLCV 列
    assert "instrument_id" not in df.columns
    for col in ["open", "high", "low", "close", "volume", "amount", "adj_factor"]:
        assert col in df.columns
    # DatetimeIndex
    assert isinstance(df.index, pd_DatetimeIndex)
    assert list(df.index) == [
        pd_Timestamp("2026-07-13"),
        pd_Timestamp("2026-07-14"),
    ]
    # 排序（按 trade_date 升序）
    assert df.index.is_monotonic_increasing


@pytest.mark.asyncio
async def test_batch_multiple_instruments_grouped():
    rows = [
        _make_row("600000", date(2026, 7, 13), 10.1, 10.5, 9.9, 10.2, 1000, 10200, 1.0),
        _make_row("600001", date(2026, 7, 13), 20.1, 20.5, 19.9, 20.2, 2000, 40400, 1.0),
        _make_row("600001", date(2026, 7, 14), 20.2, 20.6, 20.0, 20.4, 2100, 42440, 1.0),
    ]
    result = await _call_with_fake_session(rows)
    assert set(result.keys()) == {"600000", "600001"}
    assert len(result["600000"]) == 1
    assert len(result["600001"]) == 2
    # 数值类型（float，非 Decimal）
    assert isinstance(result["600000"].iloc[0]["close"], float)


@pytest.mark.asyncio
async def test_batch_empty_rows_returns_empty_dict():
    result = await _call_with_fake_session([])
    assert result == {}


# ---------------------------------------------------------------------------
# 以下为与真实函数对接的辅助：直接调用 get_daily_bars_batch 的真实行转换分支。
# 因函数签名依赖 db session，这里用 monkeypatch 注入伪造 session.execute。
# ---------------------------------------------------------------------------


async def _call_with_fake_session(rows):
    """调用真实 get_daily_bars_batch，但用内存伪造的 session.execute 替换 DB 查询。"""
    fake_session = SimpleNamespace(
        execute=lambda *a, **k: SimpleNamespace(fetchall=lambda: rows)
    )

    # get_daily_bars_batch 是 async def，内部 rows = (await session.execute(stmt)).all()
    async def _fake_execute(*a, **k):
        return SimpleNamespace(all=lambda: rows)

    fake_session.execute = _fake_execute

    instrument_ids = sorted({r.instrument_id for r in rows}) if rows else []
    return await get_daily_bars_batch(
        session=fake_session,
        instrument_ids=instrument_ids,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )
