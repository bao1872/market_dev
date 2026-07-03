"""DSA 全量特征计算与严格发布门禁集成测试。

覆盖：
- 每个 eligible 且行情足够股票都有 StrategyResult（不丢弃 matched=false/负特征/空头）
- 单股超时记 failed 并填充 reason_code
- run 级总超时/取消机制
- 严格自动发布门禁（通过 _check_quality_gates 与 execute_run 联动）
- skipped 原因必须在 allowlist 内
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.models.strategy_run import StrategyRun, StrategyRunItem
from app.repositories import strategy_result_repository
from app.services.strategy_batch_service import StrategyBatchService
from app.strategy.budget import BudgetExceededError
from app.strategy.runtime import MarketDataContext, StrategyResult, StrategyRuntime


def _fake_bars() -> pd.DataFrame:
    """构造足够长度的日线行情 DataFrame，供 batch service 通过数据就绪检查。"""
    n = 100
    idx = pd.date_range("2024-01-01", periods=n)
    close = 10.0 + pd.Series(range(n), index=idx) * 0.05
    return pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": 1e6,
            "amount": 1e8,
        },
        index=idx,
    )


class FakeRuntime(StrategyRuntime):
    """用于测试的伪 DSA runtime。

    通过类属性 _behaviors 控制每个 instrument_id 的行为：
    - "success": 正常返回结果（含负特征与 dir=-1）
    - "timeout": 抛出 BudgetExceededError（模拟单股超时）
    - "no_data": 返回 None（模拟无行情数据，应 skipped）
    """

    kind = "selector"
    _behaviors: dict[uuid.UUID, str] = {}

    def __init__(self) -> None:
        self._version_id: uuid.UUID | None = None

    async def initialize(self, version) -> None:
        self._version_id = version.id

    async def execute(self, context: MarketDataContext) -> StrategyResult | None:
        behavior = self._behaviors.get(context.instrument_id, "success")
        if behavior == "no_data":
            return None
        if behavior == "timeout":
            raise BudgetExceededError("模拟单股超时", timeout_ms=10)
        return StrategyResult(
            instrument_id=context.instrument_id,
            strategy_version_id=self._version_id or uuid.uuid4(),
            trade_date=context.trade_date,
            matched=True,
            metrics={
                "dsa_dir_bars": -5,
                "dsa_dir": -1,
                "offset_mean": -0.02,
            },
            calculation_id=uuid.uuid4().hex,
        )


async def _create_run_with_items(
    db_session,
    test_selector_strategy,
    instrument_factory,
    behaviors: list[str],
):
    """创建 StrategyRun 并预创建 StrategyRunItem（behaviors 对应 FakeRuntime 行为）。"""
    version = test_selector_strategy["version"]
    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=date(2026, 6, 24),
        status="running",
        input_overrides={"strategy_key": "dsa_selector"},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        total_instruments=len(behaviors),
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()

    instruments = []
    FakeRuntime._behaviors = {}
    items = []
    for i, behavior in enumerate(behaviors):
        inst = await instrument_factory(symbol=f"T{i:03d}", name=f"Test {i}")
        instruments.append(inst)
        FakeRuntime._behaviors[inst.id] = behavior
        item = StrategyRunItem(
            run_id=run.id,
            instrument_id=inst.id,
            status="pending",
            attempt_count=0,
        )
        db_session.add(item)
        items.append(item)
    await db_session.flush()
    return run, items, instruments


@pytest.mark.asyncio
async def test_full_feature_no_result_dropping(
    db_session, test_selector_strategy, instrument_factory
):
    """行情足够股票均写入 StrategyResult，负特征/空头/matched=True 不丢弃。"""
    run, _, _ = await _create_run_with_items(
        db_session, test_selector_strategy, instrument_factory, ["success", "success", "success"]
    )

    service = StrategyBatchService()
    with patch(
        "app.services.strategy_batch_service.StrategyLoader.load",
        new=AsyncMock(return_value=FakeRuntime()),
    ):
        with patch(
            "app.services.strategy_batch_service.get_bars",
            new=AsyncMock(return_value=SimpleNamespace(bars=_fake_bars())),
        ):
            with patch(
                "app.services.strategy_batch_service._run_heartbeat_task",
                new=AsyncMock(),
            ):
                await service.execute_run(db_session, run.id)

    await db_session.refresh(run)
    assert run.status == "completed"
    assert run.succeeded_count == 3
    assert run.failed_count == 0
    assert run.skipped_count == 0

    result_count = await strategy_result_repository.count_by_run(db_session, run.id)
    assert result_count == 3

    results = await strategy_result_repository.query_results(
        db_session, run_id=run.id, limit=100
    )
    for r in results.items:
        # 负特征与空头必须保留
        assert r.payload.get("dsa_dir_bars") == -5
        assert r.payload.get("dsa_dir") == -1
        assert r.payload.get("offset_mean") == -0.02


@pytest.mark.asyncio
async def test_single_stock_timeout_marks_failed_with_reason_code(
    db_session, test_selector_strategy, instrument_factory
):
    """单股超时记 failed 而非 skipped，并填充 reason_code=timeout。"""
    run, items, instruments = await _create_run_with_items(
        db_session, test_selector_strategy, instrument_factory, ["success", "timeout"]
    )
    timeout_inst = instruments[1]

    service = StrategyBatchService()
    with patch(
        "app.services.strategy_batch_service.StrategyLoader.load",
        new=AsyncMock(return_value=FakeRuntime()),
    ):
        with patch(
            "app.services.strategy_batch_service.get_bars",
            new=AsyncMock(return_value=SimpleNamespace(bars=_fake_bars())),
        ):
            with patch(
                "app.services.strategy_batch_service._run_heartbeat_task",
                new=AsyncMock(),
            ):
                await service.execute_run(db_session, run.id)

    await db_session.refresh(run)
    assert run.status == "partial_failed"
    assert run.succeeded_count == 1
    assert run.failed_count == 1

    failed_item = next(i for i in items if i.instrument_id == timeout_inst.id)
    assert failed_item.status == "failed"
    assert failed_item.reason_code == "timeout"


@pytest.mark.asyncio
async def test_insufficient_data_marks_skipped_with_reason_code(
    db_session, test_selector_strategy, instrument_factory
):
    """无行情数据股票记 skipped 并填充 allowlist 内的 reason_code。"""
    run, items, instruments = await _create_run_with_items(
        db_session, test_selector_strategy, instrument_factory, ["success", "no_data"]
    )
    no_data_inst = instruments[1]

    service = StrategyBatchService()
    with patch(
        "app.services.strategy_batch_service.StrategyLoader.load",
        new=AsyncMock(return_value=FakeRuntime()),
    ):
        with patch(
            "app.services.strategy_batch_service.get_bars",
            new=AsyncMock(return_value=SimpleNamespace(bars=_fake_bars())),
        ):
            with patch(
                "app.services.strategy_batch_service._run_heartbeat_task",
                new=AsyncMock(),
            ):
                await service.execute_run(db_session, run.id)

    await db_session.refresh(run)
    assert run.status == "completed"
    assert run.succeeded_count == 1
    assert run.skipped_count == 1

    skipped_item = next(i for i in items if i.instrument_id == no_data_inst.id)
    assert skipped_item.status == "skipped"
    assert skipped_item.reason_code == "insufficient_data"


@pytest.mark.asyncio
async def test_auto_publish_blocked_when_failed_exists(
    db_session, test_selector_strategy, instrument_factory
):
    """存在 failed 项时自动发布门禁拒绝，运行状态为 partial_failed。"""
    run, _, _ = await _create_run_with_items(
        db_session, test_selector_strategy, instrument_factory, ["success", "timeout"]
    )

    service = StrategyBatchService()
    with patch(
        "app.services.strategy_batch_service.StrategyLoader.load",
        new=AsyncMock(return_value=FakeRuntime()),
    ):
        with patch(
            "app.services.strategy_batch_service.get_bars",
            new=AsyncMock(return_value=SimpleNamespace(bars=_fake_bars())),
        ):
            with patch(
                "app.services.strategy_batch_service._run_heartbeat_task",
                new=AsyncMock(),
            ):
                await service.execute_run(db_session, run.id)

    await db_session.refresh(run)
    assert run.status == "partial_failed"
    assert run.failed_count == 1

    # 自动发布门禁应拒绝
    assert await service._check_quality_gates(run, result_count=1) is False


@pytest.mark.asyncio
async def test_run_level_total_timeout_cancels_remaining(
    db_session, test_selector_strategy, instrument_factory
):
    """run 级总超时后剩余 pending/running 项应被标记失败。"""
    run, _, _ = await _create_run_with_items(
        db_session, test_selector_strategy, instrument_factory, ["success", "success", "success"]
    )

    service = StrategyBatchService()
    service._run_total_timeout_seconds = 0.05

    async def _slow_execute(*args, **kwargs):
        await asyncio.sleep(10)
        return None

    with patch(
        "app.services.strategy_batch_service.StrategyLoader.load",
        new=AsyncMock(return_value=FakeRuntime()),
    ):
        with patch(
            "app.services.strategy_batch_service.get_bars",
            new=AsyncMock(return_value=SimpleNamespace(bars=_fake_bars())),
        ):
            with patch.object(FakeRuntime, "execute", new=_slow_execute):
                with patch(
                    "app.services.strategy_batch_service._run_heartbeat_task",
                    new=AsyncMock(),
                ):
                    await service.execute_run(db_session, run.id)

    await db_session.refresh(run)
    # 总超时后所有项应被标记为失败（或至少没有 pending/running）
    non_terminal_items = await db_session.execute(
        select(StrategyRunItem).where(
            StrategyRunItem.run_id == run.id,
            StrategyRunItem.status.in_(["pending", "running"]),
        )
    )
    assert non_terminal_items.scalars().all() == []
