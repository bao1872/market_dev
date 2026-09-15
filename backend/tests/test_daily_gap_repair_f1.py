"""F1 有界 degraded repair 单元测试（CHANGE-CHECK F1-7 / F1-8 / F1-6-unit）。

纯单元测试（PURE_UNIT_TEST=1，不连 DB/网络）：

- F1-7: repair_degraded_factor_inputs 只请求 data-degraded symbols，窗口有界
        (start = min(event_dates) - 30d, end = max(event_dates))；
        非 data-degraded reason 不进 repair。
- F1-8: bulk_insert_raw_daily_repair 使用 ON CONFLICT DO NOTHING，
        既有 bars 不被 overwrite；非法 OHLCV 行被丢弃。
- F1-6 (unit): 非 data-degraded reason → repair 为 no-op（不请求 provider / 不插入）。

provider 网络 I/O 通过注入 raw_fetch spy 验证调用契约，不触达真实 pytdx。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.daily_gap_repair_service import (
    bulk_insert_raw_daily_repair,
    repair_degraded_factor_inputs,
)
from app.services.factor_reconciliation import DegradedFactorInput


def _run(coro):
    """同步运行 async 协程（测试 helper）。"""
    return asyncio.run(coro)


def _valid_raw(datetime_str: str = "2024-02-01"):
    """构造一条合法 raw daily（通过 bulk_insert 校验）。"""
    return {
        "datetime": datetime_str,
        "open": 1.0, "high": 2.0, "low": 0.5,
        "close": 1.5, "volume": 1000.0, "amount": 1500.0,
    }


@pytest.mark.asyncio
async def test_f1_repair_targets_only_data_degraded_with_bounded_window() -> None:
    """F1-7: targeted repair 只请求 data-degraded symbols，窗口有界。"""
    iid1 = uuid.uuid4()
    iid2 = uuid.uuid4()
    iid3 = uuid.uuid4()
    item1 = DegradedFactorInput(
        iid1, "000032", "bars_daily_gap",
        (date(2024, 1, 5), date(2024, 3, 2)),
    )
    item2 = DegradedFactorInput(
        iid2, "001331", "bars_daily_missing_data", (date(2023, 6, 1),),
    )
    item3 = DegradedFactorInput(
        iid3, "ERR99", "unknown_degradation", (date(2023, 1, 1),),
    )

    calls: list[tuple[str, date, date]] = []

    def raw_fetch(symbol: str, start: date, end: date):
        calls.append((symbol, start, end))
        return [_valid_raw()]

    session = MagicMock()
    # session.execute 必须可 await；每个 bulk_insert 调用返回 1 行已插入
    session.execute = AsyncMock(return_value=MagicMock())
    session.execute.return_value.fetchall.return_value = [MagicMock()]
    session.commit = AsyncMock()

    report = await repair_degraded_factor_inputs(
        session, [item1, item2, item3], raw_fetch=raw_fetch,
    )

    fetched_symbols = [c[0] for c in calls]
    assert set(fetched_symbols) == {"000032", "001331"}
    assert "ERR99" not in fetched_symbols  # 非 data-reason 不修

    # 有界窗口：item1 = min(dates) - 30d .. max(dates)
    c1 = [c for c in calls if c[0] == "000032"][0]
    assert c1[1] == date(2024, 1, 5) - timedelta(days=30)
    assert c1[2] == date(2024, 3, 2)
    # 窗口长度 = (max - min) + 30d，而不是无界全量回补
    assert (c1[2] - c1[1]).days == (
        (date(2024, 3, 2) - date(2024, 1, 5)).days + 30
    )

    assert report.attempted_symbols == ["000032", "001331"]
    assert report.repaired_symbols == ["000032", "001331"]
    assert report.inserted_rows == 2  # 每只 1 个插入 group
    assert report.reason == "all_repaired"


@pytest.mark.asyncio
async def test_f1_unknown_reason_not_repaired() -> None:
    """F1-6 (unit): 非 data-degraded reason → repair 为 no-op（不请求 provider / 不插入）。"""
    item = DegradedFactorInput(
        uuid.uuid4(), "ERR99", "unknown_degradation", (date(2023, 1, 1),),
    )
    session = MagicMock()
    report = await repair_degraded_factor_inputs(session, [item])

    assert report.attempted_symbols == []
    assert report.repaired_symbols == []
    assert report.inserted_rows == 0
    assert report.reason == "no_data_degraded_items"
    session.execute.assert_not_called()  # 根本没走 provider / 插入


def test_f1_bulk_insert_uses_on_conflict_do_nothing() -> None:
    """F1-8: 插入缺失历史 raw rows 时，既有 bars 不被 overwrite（ON CONFLICT DO NOTHING）。"""
    from sqlalchemy.dialects import postgresql

    session = MagicMock()
    captured: dict = {}

    async def fake_execute(stmt, *a, **k):
        captured["stmt"] = stmt
        res = MagicMock()
        res.fetchall.return_value = []
        return res

    session.execute = fake_execute
    session.commit = AsyncMock()

    iid = uuid.uuid4()
    inserted = _run(
        bulk_insert_raw_daily_repair(
            session, [(iid, _valid_raw("2024-01-05"))], date(2024, 1, 5)
        )
    )
    # 无真实 DB：fetchall 返回空 → 0 行计入（此处只验证语句语义，不依赖真实 PG）
    assert inserted == 0
    stmt = captured["stmt"]
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in compiled
    assert "DO NOTHING" in compiled
    assert "DO UPDATE" not in compiled  # 绝对不允许 overwrite 既有行


def test_f1_bulk_insert_rejects_bad_raw_rows() -> None:
    """F1-8b: 非法 OHLCV（<=0）行被丢弃，不插入、不触达 DB。"""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock())

    iid = uuid.uuid4()
    bad = _valid_raw("2024-01-05")
    bad["open"] = -1.0  # 非法
    inserted = _run(
        bulk_insert_raw_daily_repair(session, [(iid, bad)], date(2024, 1, 5))
    )
    assert inserted == 0
    session.execute.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
