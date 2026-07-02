"""关键业务集成测试 - 选股查询 + 监控执行全链路。"""
import uuid
from datetime import UTC, date, datetime

import pytest

from app.services.selector_query_service import (
    RunNotFoundError,
    query_published_selector_results,
)


@pytest.mark.asyncio
async def test_selector_unpublished_run_not_queryable(db_session, test_selector_strategy):
    """未发布 run 不可查询。"""
    from app.models.strategy_run import StrategyRun

    version = test_selector_strategy["version"]
    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="manual",
        trade_date=date(2026, 6, 23),
        status="completed",
        input_overrides={},
        idempotency_key=f"test:unpub:{uuid.uuid4().hex[:8]}",
        published_at=None,
    )
    db_session.add(run)
    await db_session.flush()

    with pytest.raises(RunNotFoundError):
        await query_published_selector_results(db_session, run_id=run.id)


@pytest.mark.asyncio
async def test_selector_published_run_returns_results(
    db_session, test_published_run, test_instrument
):
    """已发布 run 返回结果。"""
    from app.models.strategy_run import StrategyResult

    result = StrategyResult(
        run_id=test_published_run.id,
        strategy_version_id=test_published_run.strategy_version_id,
        instrument_id=test_instrument.id,
        trade_date=date(2026, 6, 23),
        payload={"dsa_dir_bars": 40, "offset_mean": 0.01},
    )
    db_session.add(result)
    await db_session.flush()

    page = await query_published_selector_results(
        db_session, run_id=test_published_run.id, page=1, page_size=50
    )
    assert page.source_total >= 1
    assert page.filtered_total >= 1


@pytest.mark.asyncio
async def test_selector_three_level_counts(
    db_session, test_published_run, test_user
):
    """选股三级计数：run_source_total / universe_total / filtered_total。"""
    from app.models.instrument import Instrument
    from app.models.strategy_run import StrategyResult
    from app.models.watchlist import UserWatchlistItem

    # 创建 5 个标的 + 5 条结果
    instrument_ids = []
    for i in range(5):
        inst = Instrument(
            symbol=f"T3LC{uuid.uuid4().hex[:4]}",
            name=f"测试标的{i}",
            market="SZ",
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        instrument_ids.append(inst.id)

        result = StrategyResult(
            run_id=test_published_run.id,
            strategy_version_id=test_published_run.strategy_version_id,
            instrument_id=inst.id,
            trade_date=date(2026, 6, 23),
            payload={"dsa_dir_bars": 40, "offset_mean": 0.01},
        )
        db_session.add(result)

    # 添加 2 个标的到用户自选股
    for inst_id in instrument_ids[:2]:
        item = UserWatchlistItem(
            user_id=test_user.id,
            instrument_id=inst_id,
            source="manual",
            active=True,
        )
        db_session.add(item)

    await db_session.flush()

    page = await query_published_selector_results(
        db_session,
        run_id=test_published_run.id,
        user_id=test_user.id,
        universe="watchlist",
        page=1,
        page_size=50,
    )
    # run_source_total 应为该 run 的总结果数
    assert page.source_total >= 5
    # universe_total 应为自选股范围内的结果数
    assert page.universe_total >= 2
    # filtered_total 应等于 universe_total（无指标过滤）
    assert page.filtered_total == page.universe_total


@pytest.mark.asyncio
async def test_monitor_evaluation_exactly_once(db_session, test_selector_strategy, test_instrument):
    """MonitorEvaluation exactly-once：同一 (version, instrument, bar_time) 只能写入一次。"""
    from app.models.monitor_evaluation import MonitorEvaluation

    version = test_selector_strategy["version"]
    bar_time = datetime(2026, 6, 23, 10, 30, 0, tzinfo=UTC)

    eval1 = MonitorEvaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="SUCCEEDED",
        metrics={"test": 1},
    )
    db_session.add(eval1)
    await db_session.flush()

    # 第二次插入相同唯一键应失败
    from sqlalchemy.exc import IntegrityError

    eval2 = MonitorEvaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="SUCCEEDED",
        metrics={"test": 2},
    )
    db_session.add(eval2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_event_recipient_expansion(
    db_session, test_user, test_instrument, test_selector_strategy, make_user_eligible,
):
    """事件收件人展开：自选该股票的用户应被添加为收件人。"""
    from app.models.strategy_event import StrategyEvent
    from app.models.watchlist import UserWatchlistItem

    version = test_selector_strategy["version"]

    # [eligible_user_service] - 使 test_user 有资格进入监控 universe
    await make_user_eligible(test_user)

    # 创建自选股记录
    item = UserWatchlistItem(
        user_id=test_user.id,
        instrument_id=test_instrument.id,
        source="manual",
        active=True,
    )
    db_session.add(item)
    await db_session.flush()

    # 创建事件
    event = StrategyEvent(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        event_type="bb_upper_cross_up",
        event_key=f"test:{uuid.uuid4().hex[:8]}",
        event_time=datetime.now(UTC),
        schema_version=1,
        payload={"test": True},
    )
    db_session.add(event)
    await db_session.flush()

    # 展开收件人
    from app.services.event_recipient_service import expand_event_recipients
    count = await expand_event_recipients(db_session, event.id)

    assert count >= 1
