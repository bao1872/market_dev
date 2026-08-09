"""[CHANGE-20260808] load_day_fact_maps previous-bar PG contract 测试。

验证（用户 §1）：
1. instrument A：T bar + T-1 bar → review_return_1d 正确（(close_T - close_T-1)/close_T-1*100）
2. instrument B：T bar + previous bar >400 calendar days ago（长期停牌）→ 仍能取到 previous
   （previous bar 无自然日下界，DISTINCT ON 取最近一根）
3. 有 current FP state 但 target_date 无 current bar → review_return_1d unavailable（不冒充）

这是 PG 集成测试，需在验证 DB（PANJI_REMOTE_VERIFY_DB_TEST=1）运行，
不得连接 bz_stock。运行：
    cd backend
    PANJI_REMOTE_VERIFY_DB_TEST=1 .venv/bin/python -m pytest \
        tests/test_pg_review_previous_bar.py -v
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

# 仅在 verify DB 环境运行
pytestmark = pytest.mark.pg


async def _insert_bar(
    session, instrument_id: uuid.UUID, trade_date: date,
    close: float, volume: float = 1000.0, amount: float = 10000.0,
) -> None:
    """向 bars_daily 写入一条真实 bar（测试 fixture，verify DB 内）。"""
    from sqlalchemy import text

    await session.execute(
        text(
            """INSERT INTO bars_daily
               (instrument_id, trade_date, open, high, low, close, volume, amount, qfq_close)
               VALUES (:iid, :td, :o, :h, :l, :c, :v, :a, :c)
               ON CONFLICT DO NOTHING"""
        ),
        {
            "iid": instrument_id, "td": trade_date,
            "o": close - 0.05, "h": close + 0.1, "l": close - 0.1,
            "c": close, "v": volume, "a": amount,
        },
    )


@pytest.fixture
def _cleanup():
    yield


@pytest.mark.asyncio
async def test_previous_bar_t_plus_t_minus_1():
    """instrument A：T bar + T-1 bar → return_1d 正确。"""
    from sqlalchemy import delete

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.instrument import Instrument
    from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
    from app.services.review_scope_service import load_day_fact_maps

    iid = uuid.uuid4()
    target = date(2026, 8, 4)
    async with AsyncSessionLocal() as session:
        # 清理（仅本次测试数据）
        await session.execute(
            delete(Instrument).where(Instrument.id == iid),
        )
        await session.commit()
        # 真实 Instrument + HistoryRun 满足 FK（先独立提交父记录）
        session.add(Instrument(
            id=iid, symbol=f"PGVB{iid.hex[:8]}", name="verify-prev",
            market="SH", status="active",
        ))
        run = FirstPyramidHistoryRun(
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            parameter_hash="p", output_bars=250, scope="all_a_share", status="running",
        )
        session.add(run)
        await session.flush()
        src_run = run.id
        await session.commit()
        await session.execute(
            delete(FirstPyramidHistoryDailyState).where(
                FirstPyramidHistoryDailyState.instrument_id == iid,
            )
        )
        await _insert_bar(session, iid, target, close=10.0)          # T
        await _insert_bar(session, iid, target - timedelta(days=1), close=9.5)  # T-1
        # 写入 current FP state（含 history_contract_version + source_history_run_id）
        session.add(FirstPyramidHistoryDailyState(
            instrument_id=iid, trade_date=target,
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            input_hash="h", history_contract_version="review-history-v2",
            source_history_run_id=src_run,
            state_payload={
                "history_contract_version": "review-history-v2",
                "regime_value": 1,
                "swing_bias": 1,
                "internal_bias": 1,
            },
        ))
        await session.commit()

        facts = await load_day_fact_maps(
            session, trade_date=target, instrument_ids=[iid],
        )
        fact = facts[iid]
        # (10 - 9.5) / 9.5 * 100 ≈ 5.26
        assert fact["review_return_1d"] is not None
        assert abs(fact["review_return_1d"] - ((10.0 - 9.5) / 9.5 * 100.0)) < 1e-6
        assert fact["review_amount"] == 10000.0
        # 清理
        await session.execute(
            delete(FirstPyramidHistoryDailyState).where(
                FirstPyramidHistoryDailyState.instrument_id == iid,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_previous_bar_suspended_over_400_days():
    """instrument B：T bar + previous bar >400 自然日 → 仍取到 previous（停牌安全）。"""
    from sqlalchemy import delete

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.instrument import Instrument
    from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
    from app.services.review_scope_service import load_day_fact_maps

    iid = uuid.uuid4()
    target = date(2026, 8, 4)
    # 停牌 >400 自然日：previous bar 在 target 前 500 天
    prev_date = target - timedelta(days=500)
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(FirstPyramidHistoryDailyState).where(
                FirstPyramidHistoryDailyState.instrument_id == iid,
            )
        )
        await session.execute(
            delete(Instrument).where(Instrument.id == iid),
        )
        await session.commit()
        session.add(Instrument(
            id=iid, symbol=f"PGVB{iid.hex[:8]}", name="verify-prev",
            market="SH", status="active",
        ))
        run = FirstPyramidHistoryRun(
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            parameter_hash="p", output_bars=250, scope="all_a_share", status="running",
        )
        session.add(run)
        await session.flush()
        src_run = run.id
        await session.commit()
        await _insert_bar(session, iid, target, close=12.0)          # T
        await _insert_bar(session, iid, prev_date, close=10.0)       # previous（>400 天前）
        session.add(FirstPyramidHistoryDailyState(
            instrument_id=iid, trade_date=target,
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            input_hash="h", history_contract_version="review-history-v2",
            source_history_run_id=src_run,
            state_payload={
                "history_contract_version": "review-history-v2",
                "regime_value": 1,
                "swing_bias": 1,
                "internal_bias": 1,
            },
        ))
        await session.commit()

        facts = await load_day_fact_maps(
            session, trade_date=target, instrument_ids=[iid],
        )
        fact = facts[iid]
        # (12 - 10) / 10 * 100 = 20（previous bar >400 天前仍被取到）
        assert fact["review_return_1d"] is not None
        assert abs(fact["review_return_1d"] - 20.0) < 1e-6
        await session.execute(
            delete(FirstPyramidHistoryDailyState).where(
                FirstPyramidHistoryDailyState.instrument_id == iid,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_no_current_bar_return_1d_unavailable():
    """有 current FP state 但 target_date 无 current bar → return_1d unavailable（不冒充）。"""
    from sqlalchemy import delete

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.instrument import Instrument
    from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
    from app.services.review_scope_service import load_day_fact_maps

    iid = uuid.uuid4()
    target = date(2026, 8, 4)
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(FirstPyramidHistoryDailyState).where(
                FirstPyramidHistoryDailyState.instrument_id == iid,
            )
        )
        await session.execute(
            delete(Instrument).where(Instrument.id == iid),
        )
        await session.commit()
        session.add(Instrument(
            id=iid, symbol=f"PGVB{iid.hex[:8]}", name="verify-prev",
            market="SH", status="active",
        ))
        run = FirstPyramidHistoryRun(
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            parameter_hash="p", output_bars=250, scope="all_a_share", status="running",
        )
        session.add(run)
        await session.flush()
        src_run = run.id
        await session.commit()
        # 只有 previous bar，无 current bar（target 无当日 bar）
        await _insert_bar(session, iid, target - timedelta(days=1), close=9.5)
        session.add(FirstPyramidHistoryDailyState(
            instrument_id=iid, trade_date=target,
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            input_hash="h", history_contract_version="review-history-v2",
            source_history_run_id=src_run,
            state_payload={
                "history_contract_version": "review-history-v2",
                "regime_value": 1,
            },
        ))
        await session.commit()

        facts = await load_day_fact_maps(
            session, trade_date=target, instrument_ids=[iid],
        )
        fact = facts[iid]
        # target 无 current bar → return_1d unavailable（不得拿更早 bar 冒充）
        assert fact["review_return_1d"] is None
        await session.execute(
            delete(FirstPyramidHistoryDailyState).where(
                FirstPyramidHistoryDailyState.instrument_id == iid,
            )
        )
        await session.commit()
