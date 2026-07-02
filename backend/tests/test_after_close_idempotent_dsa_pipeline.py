"""盘后编排幂等性集成测试 - 验证 DSA pipeline 的幂等触发。

场景：
1. 构造满足覆盖率的交易日（active A 股 instruments + bars_daily 覆盖率 100%）
2. 第一次触发 _check_daily_coverage_and_trigger_dsa → 创建 queued DSA run（attempt_no=1）
3. 模拟 strategy_batch_worker 领取 + 执行完成 → completed
4. 调用 publish_run → published（自动发布链路）
5. 第二次触发 _check_daily_coverage_and_trigger_dsa（同业务日）
   → 复用已有 published run，不创建新 run（_BLOCKING_STATUSES 包含 published）

约束（遵守 AGENTS.md 硬规则）：
- PostgreSQL 测试库执行（conftest.py 公共 db_session fixture，savepoint 隔离）
- 不 mock eligibility（check_data_readiness / _check_daily_coverage_and_trigger_dsa 真实执行）
- 不使用 SQLite / aiosqlite / 内存数据库
- 不手写 DDL 建表语句（结构来自 Alembic 迁移）
- 普通用户角色名为 member（本测试不涉及用户角色，仅数据层）
- 管理员无套餐（本测试不涉及套餐）
- 禁止修改 DSA/Node Cluster/MACD/布林带公式参数（本测试不触碰算法）
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.constants.strategy_keys import DSA_SELECTOR
from app.models.bar import BarDaily
from app.models.calendar import TradingCalendar
from app.models.instrument import Instrument
from app.models.strategy_run import StrategyRun
from app.services.bars_scheduler_service import BarsSchedulerService
from app.services.strategy_batch_service import StrategyBatchService

# 测试固定交易日（避免依赖今天是否为交易日）
_TRADE_DATE = date(2026, 6, 25)

# 5 只 A 股 active 股票（满足 stock_symbol_sql_filter 正则）
# SH ^6\d{5}$ / SZ ^(00|02|30)\d{4}$
_TEST_SYMBOLS: list[tuple[str, str]] = [
    ("600001", "SH"),
    ("600002", "SH"),
    ("000001", "SZ"),
    ("000002", "SZ"),
    ("300001", "SZ"),
]


async def _seed_trading_calendar(db_session) -> None:
    """插入测试交易日历记录，标记 _TRADE_DATE 为 A 股交易日。

    is_trading_day_async 优先读 trading_calendar 表（status=OPEN/CLOSED 时权威）。
    """
    cal = TradingCalendar(
        trade_date=_TRADE_DATE,
        is_trading_day=True,
        market="A",
        source="MANUAL_OVERRIDE",
        status="OPEN",
    )
    db_session.add(cal)
    await db_session.flush()


async def _seed_active_instruments(db_session) -> list[Instrument]:
    """插入 5 只 active A 股 instruments（满足 stock_symbol_sql_filter 正则）。

    listing_date 设为 2020-01-01，避免触发"新上市标的 < 30 天"警告。
    """
    instruments: list[Instrument] = []
    for symbol, market in _TEST_SYMBOLS:
        inst = Instrument(
            symbol=symbol,
            name=f"测试{symbol}",
            market=market,
            status="active",
            listing_date=date(2020, 1, 1),
        )
        db_session.add(inst)
        instruments.append(inst)
    await db_session.flush()
    return instruments


async def _seed_bars_daily(db_session, instruments: list[Instrument]) -> None:
    """为每只 instrument 插入 _TRADE_DATE 当日 bars_daily 记录（覆盖率 100%）。

    覆盖率 = bars_daily 不同 instrument_id 数 / active A 股 instrument 数 = 5/5 = 100%。
    """
    for inst in instruments:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=_TRADE_DATE,
            open=Decimal("10.00"),
            high=Decimal("10.50"),
            low=Decimal("9.80"),
            close=Decimal("10.20"),
            volume=Decimal("1000000"),
            amount=Decimal("10200000"),
            adj_factor=Decimal("1.0"),
        )
        db_session.add(bar)
    await db_session.flush()


@pytest.mark.asyncio
async def test_after_close_idempotent_dsa_pipeline(
    db_session,
    dsa_selector_strategy,
) -> None:
    """验证盘后 DSA pipeline 幂等性：第二次触发复用已 published run。

    流程：
    1. 构造测试数据（trading_calendar + 5 只 active A 股 + bars_daily 100% 覆盖）
    2. 第一次 _check_daily_coverage_and_trigger_dsa
       → 调用 create_batch_run 创建 queued run（attempt_no=1）
    3. 模拟 strategy_batch_worker 执行完成 → completed
    4. 调用 publish_run → published
    5. 第二次 _check_daily_coverage_and_trigger_dsa（同业务日）
       → create_batch_run 命中 _BLOCKING_STATUSES={published,completed,running,queued}
       → 返回同一 run_id，不创建新 run
    6. 断言：strategy_runs 表中该 (version, trade_date, run_type) 仅 1 条记录
    """
    # ---- 准备：构造满足覆盖率的数据 ----
    await _seed_trading_calendar(db_session)
    instruments = await _seed_active_instruments(db_session)
    await _seed_bars_daily(db_session, instruments)

    version = dsa_selector_strategy["version"]

    bars_service = BarsSchedulerService()
    batch_service = StrategyBatchService()

    # ---- 步骤 1：第一次触发 DSA（模拟 bars 日线完成后的触发）----
    first_dsa_run_id = await bars_service._check_daily_coverage_and_trigger_dsa(
        trade_date=_TRADE_DATE,
        db_session=db_session,
        job_run_id=None,
        result=None,
    )
    # 触发成功应返回 run_id（create_batch_run 内部已 commit savepoint）
    assert first_dsa_run_id is not None, (
        "覆盖率 100% 应触发 DSA run，但返回 None（create_batch_run 未创建）"
    )

    # 验证第一次创建的 run：status=queued, attempt_no=1
    first_run = await db_session.get(StrategyRun, first_dsa_run_id)
    assert first_run is not None, "第一次触发的 run 不存在"
    assert first_run.status == "queued", (
        f"第一次创建的 run 应为 queued，实际 {first_run.status}"
    )
    assert first_run.attempt_no == 1, (
        f"第一次创建的 run attempt_no 应为 1，实际 {first_run.attempt_no}"
    )
    assert first_run.run_type == "scheduled"
    assert first_run.trade_date == _TRADE_DATE

    # ---- 步骤 2：模拟 strategy_batch_worker 领取 + 执行完成 ----
    # 不调用 execute_run（依赖真实策略 manifest 加载，超出本测试范围），
    # 直接将 status 置为 completed 并填充统计字段，模拟 batch 计算完成
    first_run.status = "completed"
    first_run.succeeded_count = len(instruments)
    first_run.failed_count = 0
    first_run.skipped_count = 0
    first_run.finished_at = datetime.now(UTC)
    await db_session.flush()

    # ---- 步骤 3：调用 publish_run（真实链路，验证质量门禁 + 发布）----
    published_run = await batch_service.publish_run(db_session, first_run.id)
    assert published_run.status == "published", (
        f"publish_run 后 status 应为 published，实际 {published_run.status}"
    )
    assert published_run.published_at is not None, "published_at 应非空"
    await db_session.flush()

    # ---- 步骤 4：第二次触发（同业务日，模拟 18:30 兜底）----
    # create_batch_run 内部查询同 (version, trade_date, run_type) 的所有 runs，
    # published 命中 _BLOCKING_STATUSES，直接返回已有 run
    second_dsa_run_id = await bars_service._check_daily_coverage_and_trigger_dsa(
        trade_date=_TRADE_DATE,
        db_session=db_session,
        job_run_id=None,
        result=None,
    )

    # ---- 步骤 5：幂等断言 ----
    assert second_dsa_run_id is not None, (
        "第二次触发不应返回 None（应复用已有 published run）"
    )
    assert second_dsa_run_id == first_dsa_run_id, (
        f"第二次触发应复用同一 run_id（幂等），"
        f"第一次={first_dsa_run_id}, 第二次={second_dsa_run_id}"
    )

    # ---- 步骤 6：DB 层断言：仅 1 条 run 记录 ----
    count_stmt = (
        select(func.count())
        .select_from(StrategyRun)
        .where(
            StrategyRun.strategy_version_id == version.id,
            StrategyRun.trade_date == _TRADE_DATE,
            StrategyRun.run_type == "scheduled",
        )
    )
    run_count = await db_session.scalar(count_stmt)
    assert run_count == 1, (
        f"同 (version, trade_date, run_type) 应仅有 1 条 run 记录（幂等），"
        f"实际 {run_count}"
    )

    # 验证最终 run 状态仍为 published（未被二次触发篡改）
    final_run = await db_session.get(StrategyRun, first_dsa_run_id)
    assert final_run.status == "published", (
        f"二次触发后 run 状态应保持 published，实际 {final_run.status}"
    )
    assert final_run.attempt_no == 1, (
        f"二次触发不应新增 attempt，attempt_no 应保持 1，实际 {final_run.attempt_no}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
