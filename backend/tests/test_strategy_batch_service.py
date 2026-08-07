"""StrategyBatchService 核心行为测试。

测试重点：
- run 级总超时可配置
- 超时预算耗尽后 reason_code 明确
- partial_failed 不可发布
- 历史数据不足标的在创建时即标记 skipped
- execute_run 保留 create_batch_run 预置的 skipped_count
- computable universe 通过严格质量门禁

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://user:pass@host:port/dbname_test \
        pytest backend/tests/test_strategy_batch_service.py -q
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyRun, StrategyRunItem
from app.services.strategy_batch_service import DataReadinessResult, StrategyBatchService
from app.strategy.runtime import StrategyResult


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _create_dsa_selector(
    db_session: AsyncSession,
    outputs: list[dict[str, Any]] | None = None,
) -> StrategyVersion:
    """创建 dsa_selector 策略定义与 released 版本。"""
    definition = StrategyDefinition(
        strategy_key="dsa_selector",
        kind="selector",
        display_name="趋势选股",
    )
    db_session.add(definition)
    await db_session.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "strategy_id": "dsa_selector",
            "parameters": [],
            "outputs": outputs or [
                {"key": "dsa_dir_bars", "type": "numeric", "filterable": True, "sortable": True},
                {"key": "offset_mean", "type": "numeric", "filterable": True, "sortable": True},
            ],
        },
        build_hash=f"test_hash_{uuid.uuid4().hex[:16]}",
        released_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    return version


@pytest.fixture
def mock_data_readiness(monkeypatch):
    """绕过数据就绪检查，让 create_batch_run 测试聚焦在 item 创建逻辑。"""
    async def _ready(*args, **kwargs):
        return DataReadinessResult(
            is_ready=True,
            is_trading_day=True,
            active_instrument_count=2,
            bars_count=2,
            coverage_rate=1.0,
            warnings=[],
        )

    monkeypatch.setattr(StrategyBatchService, "check_data_readiness", _ready)


async def _create_instrument_with_bars(
    db_session: AsyncSession,
    symbol: str,
    market: str = "SZ",
    bar_count: int = 0,
    end_date: date = date(2026, 7, 3),
) -> Instrument:
    """创建标的并在 bars_daily 中写入指定条数历史数据。"""
    instrument = Instrument(
        symbol=symbol,
        name=f"测试-{symbol}",
        market=market,
        status="active",
        listing_date=date(2020, 1, 1),
    )
    db_session.add(instrument)
    await db_session.flush()

    bars = []
    for i in range(bar_count):
        trade_date = end_date - timedelta(days=i)
        bars.append(BarDaily(
            instrument_id=instrument.id,
            trade_date=trade_date,
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            volume=1000,
            amount=10000.0,
        ))
    if bars:
        db_session.add_all(bars)
        await db_session.flush()

    return instrument


def test_run_total_timeout_default_is_7200_seconds() -> None:
    """默认 run 级总超时应为 7200 秒，与 after_close_orchestrator 对齐。"""
    service = StrategyBatchService()
    assert service._RUN_TOTAL_TIMEOUT_SECONDS == 7200.0
    assert service._run_total_timeout_seconds == 7200.0


def test_run_total_timeout_is_configurable_via_env(monkeypatch) -> None:
    """STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS 环境变量可覆盖默认超时。"""
    monkeypatch.setenv("STRATEGY_RUN_TOTAL_TIMEOUT_SECONDS", "1234.5")
    service = StrategyBatchService()
    assert service._run_total_timeout_seconds == 1234.5


@pytest.mark.asyncio
async def test_partial_failed_not_publishable(
    db_session: AsyncSession,
) -> None:
    """publish_run 必须拒绝 partial_failed 状态。"""
    version = await _create_dsa_selector(db_session)
    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=date(2026, 7, 3),
        status="partial_failed",
        input_overrides={"strategy_key": "dsa_selector"},
        idempotency_key=f"test-partial:{uuid.uuid4()}",
        total_instruments=10,
        succeeded_count=5,
        failed_count=5,
        skipped_count=0,
    )
    db_session.add(run)
    await db_session.flush()

    service = StrategyBatchService()
    with pytest.raises(ValueError) as exc_info:
        await service.publish_run(db_session, run.id)
    assert "partial_failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_insufficient_history_marked_skipped(
    db_session: AsyncSession,
) -> None:
    """历史 bars 不足标的应在 create_batch_run 时标记 skipped/insufficient_history。"""
    await _create_dsa_selector(db_session)

    # 一只历史足够，一只历史不足
    enough_inst = await _create_instrument_with_bars(
        db_session, "000001", bar_count=100,
    )
    low_inst = await _create_instrument_with_bars(
        db_session, "000002", bar_count=10,
    )

    trade_date = date(2026, 7, 3)

    # 直接调用底层分类方法验证；create_batch_run 集成测试见下文
    service = StrategyBatchService()
    computable, insufficient = await service._classify_computable_universe(
        db_session, trade_date, [enough_inst.id, low_inst.id],
    )

    assert enough_inst.id in computable
    assert low_inst.id in insufficient
    assert low_inst.id not in computable


@pytest.mark.asyncio
async def test_create_batch_run_skips_insufficient_history(
    db_session: AsyncSession,
    mock_data_readiness: None,
) -> None:
    """create_batch_run 预创建时，历史不足标的直接 status=skipped。"""
    await _create_dsa_selector(db_session)

    trade_date = date(2026, 7, 3)
    # 创建一只历史足够、一只历史不足的标的
    enough_inst = await _create_instrument_with_bars(
        db_session, "000001", bar_count=100, end_date=trade_date,
    )
    low_inst = await _create_instrument_with_bars(
        db_session, "000002", bar_count=10, end_date=trade_date,
    )

    service = StrategyBatchService()
    run = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[enough_inst.id, low_inst.id],
    )

    items_result = await db_session.execute(
        select(StrategyRunItem).where(StrategyRunItem.run_id == run.id)
    )
    items = list(items_result.scalars().all())
    by_inst = {item.instrument_id: item for item in items}

    assert len(items) == 2
    assert by_inst[enough_inst.id].status == "pending"
    assert by_inst[low_inst.id].status == "skipped"
    assert by_inst[low_inst.id].reason_code == "insufficient_history"
    assert run.total_instruments == 2
    assert run.skipped_count == 1


@pytest.mark.asyncio
async def test_run_timeout_budget_exhausted_reason_code(
    db_session: AsyncSession,
    mock_data_readiness: None,
) -> None:
    """run 级总超时预算耗尽后，剩余 pending 项 reason_code=run_timeout_budget_exhausted。"""
    await _create_dsa_selector(db_session)

    trade_date = date(2026, 7, 3)
    # 创建两只历史足够的标的
    inst1 = await _create_instrument_with_bars(
        db_session, "000001", bar_count=100, end_date=trade_date,
    )
    inst2 = await _create_instrument_with_bars(
        db_session, "000002", bar_count=100, end_date=trade_date,
    )

    service = StrategyBatchService()
    run = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst1.id, inst2.id],
    )

    # 领取任务
    claimed = await service.claim_next_run(db_session)
    assert claimed is not None
    assert claimed.id == run.id

    # 模拟单股执行极慢，并将 run 总超时设为极短
    service._run_total_timeout_seconds = 0.001

    async def _slow_execute(*args, **kwargs):
        await asyncio.sleep(1.0)
        return None

    original_execute = service._execute_single_instrument
    service._execute_single_instrument = _slow_execute  # type: ignore[method-assign]

    try:
        await service.execute_run(db_session, run.id)
    finally:
        service._execute_single_instrument = original_execute  # type: ignore[method-assign]

    await db_session.refresh(run)
    items_result = await db_session.execute(
        select(StrategyRunItem).where(StrategyRunItem.run_id == run.id)
    )
    items = list(items_result.scalars().all())

    # 至少有一只标的是 run_timeout_budget_exhausted
    timeout_items = [
        item for item in items
        if item.reason_code == "run_timeout_budget_exhausted"
    ]
    item_summary = [
        (i.status, i.reason_code, i.error_message) for i in items
    ]
    assert len(timeout_items) >= 1, (
        f"期望至少 1 个 run_timeout_budget_exhausted，实际 items={item_summary}"
    )
    for item in timeout_items:
        assert item.status == "failed"
        assert item.error_message is not None
        assert "总超时" in item.error_message


@pytest.mark.asyncio
async def test_computable_universe_passes_quality_gate(
    db_session: AsyncSession,
    mock_data_readiness: None,
) -> None:
    """succeeded + skipped == total 且 skipped reason 合法时 quality gate 通过。"""
    await _create_dsa_selector(db_session)

    trade_date = date(2026, 7, 3)
    enough_inst = await _create_instrument_with_bars(
        db_session, "000001", bar_count=100, end_date=trade_date,
    )
    low_inst = await _create_instrument_with_bars(
        db_session, "000002", bar_count=10, end_date=trade_date,
    )

    service = StrategyBatchService()
    run = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[enough_inst.id, low_inst.id],
    )

    # 手动将 run 设为 completed，并补全结果数与统计
    run.status = "completed"
    run.succeeded_count = 1
    run.skipped_count = 1
    run.failed_count = 0
    run.finished_at = datetime.now(UTC)
    await db_session.flush()

    passed = await service._check_quality_gates(run, result_count=1, db=db_session)
    assert passed is True


@pytest.mark.asyncio
async def test_execute_run_preserves_pre_skipped_count(
    db_session: AsyncSession,
    mock_data_readiness: None,
) -> None:
    """execute_run 必须保留 create_batch_run 预置的 insufficient_history skipped_count。

    历史不足标的在 create_batch_run 时即被标记 skipped，不应在 execute_run 后被覆盖为 0。
    否则质量门禁 succeeded + skipped == total 会失败，导致 completed run 无法发布。
    """
    await _create_dsa_selector(db_session)

    trade_date = date(2026, 7, 3)
    # 一只历史足够，一只历史不足
    enough_inst = await _create_instrument_with_bars(
        db_session, "000001", bar_count=100, end_date=trade_date,
    )
    low_inst = await _create_instrument_with_bars(
        db_session, "000002", bar_count=10, end_date=trade_date,
    )

    service = StrategyBatchService()
    run = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[enough_inst.id, low_inst.id],
    )

    # 领取任务
    claimed = await service.claim_next_run(db_session)
    assert claimed is not None
    assert claimed.id == run.id

    # mock 单股执行：对 pending 项返回成功结果
    async def _mock_execute(
        db: AsyncSession,
        run_obj: StrategyRun,
        version: StrategyVersion,
        runtime: Any,
        item: StrategyRunItem,
    ) -> StrategyResult:
        assert run_obj.trade_date is not None
        return StrategyResult(
            instrument_id=item.instrument_id,
            strategy_version_id=run_obj.strategy_version_id,
            trade_date=run_obj.trade_date,
            matched=True,
            metrics={"dsa_dir_bars": 100},
            calculation_id=f"test-{item.instrument_id}",
        )

    original_execute = service._execute_single_instrument
    service._execute_single_instrument = _mock_execute  # type: ignore[method-assign, assignment]

    try:
        await service.execute_run(db_session, run.id)
    finally:
        service._execute_single_instrument = original_execute  # type: ignore[method-assign, assignment]

    await db_session.refresh(run)

    assert run.succeeded_count == 1
    assert run.skipped_count == 1
    assert run.failed_count == 0
    assert run.status == "completed"

    # 质量门禁应通过
    passed = await service._check_quality_gates(run, result_count=1, db=db_session)
    assert passed is True


@pytest.mark.asyncio
async def test_create_batch_run_source_core_identity_isolates_runs(
    db_session: AsyncSession,
    mock_data_readiness: None,
) -> None:
    """[required compatibility projection identity] 同 trade_date 传不同 source_core_run_id
    必须创建不同 projection run（不复用旧 run），否则同日 Core A→Core B 会错误复用 Core A
    的 published projection run。

    - 传 source_core_run_id=A 创建 run1；
    - 传 source_core_run_id=B 创建 run2（different identity，不得返回 run1）；
    - 两者 input_overrides 持久化各自 source_core_run_id + requirement。
    """
    import uuid

    await _create_dsa_selector(db_session)
    trade_date = date(2026, 7, 3)
    inst = await _create_instrument_with_bars(
        db_session, "000001", bar_count=100, end_date=trade_date,
    )

    service = StrategyBatchService()
    core_a = uuid.uuid4()
    core_b = uuid.uuid4()

    run_a = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst.id],
        source_core_run_id=core_a,
        requirement="required_compatibility",
    )
    run_b = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst.id],
        source_core_run_id=core_b,
        requirement="required_compatibility",
    )

    # 同 trade_date 不同 source_core → 必须创建独立 run（不复用）
    assert run_a.id != run_b.id
    assert run_a.input_overrides["source_core_run_id"] == str(core_a)
    assert run_b.input_overrides["source_core_run_id"] == str(core_b)
    assert run_a.input_overrides["requirement"] == "required_compatibility"
    assert run_b.input_overrides["requirement"] == "required_compatibility"

    # 幂等：同一 source_core 再次创建 → 复用（同 idempotency key）
    run_a2 = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst.id],
        source_core_run_id=core_a,
        requirement="required_compatibility",
    )
    assert run_a2.id == run_a.id


@pytest.mark.asyncio
async def test_create_batch_run_force_restart_new_attempt_for_published(
    db_session: AsyncSession,
    mock_data_readiness: None,
) -> None:
    """[granular restart new attempt] force_restart=True 时，published 的 compatibility run
    也创建 attempt_no + 1（保留旧 run，不原地修改），而 queued/running 仍复用。

    合同（Invariant 2）：
    - run1(published) + force_restart → run2(attempt 2)：新 ID、旧 run1 保留（status 不变）。
    - run2(queued) + force_restart → **复用 run2**（active run 仍复用，不得新建）。
    - 将 run2 推进为 published 后再次 force_restart → run3(attempt 3)：新 ID、旧 run2 保留。
    """
    import uuid

    await _create_dsa_selector(db_session)
    trade_date = date(2026, 7, 4)
    inst = await _create_instrument_with_bars(
        db_session, "000002", bar_count=100, end_date=trade_date,
    )
    service = StrategyBatchService()
    core = uuid.uuid4()

    run1 = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst.id],
        source_core_run_id=core,
        requirement="required_compatibility",
    )
    # 模拟正常链已完成并发布
    run1.status = "published"
    await db_session.flush()

    # force_restart → published run1 不再阻塞，创建 attempt_no+1
    run2 = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst.id],
        source_core_run_id=core,
        requirement="required_compatibility",
        force_restart=True,
    )
    assert run2.id != run1.id, "force_restart 应创建新 attempt"
    assert run2.attempt_no == (run1.attempt_no or 1) + 1
    # 旧 run 保留（不原地修改 status）
    assert run1.status == "published"

    # run2 当前为 queued（active run）：force_restart → 复用 run2，不新建
    run2_again = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst.id],
        source_core_run_id=core,
        requirement="required_compatibility",
        force_restart=True,
    )
    assert run2_again.id == run2.id, "queued active run 应被复用（不得新建）"

    # 将 run2 推进为 published 后再次 force_restart → attempt_no+1（run3）
    run2.status = "published"
    await db_session.flush()
    run3 = await service.create_batch_run(
        db_session,
        strategy_key="dsa_selector",
        trade_date=trade_date,
        run_type="scheduled",
        instrument_ids=[inst.id],
        source_core_run_id=core,
        requirement="required_compatibility",
        force_restart=True,
    )
    assert run3.id != run2.id, "published run2 + force_restart 应创建新 attempt"
    assert run3.attempt_no == (run2.attempt_no or 1) + 1
    # 旧 run2 保留（不原地修改 status）
    assert run2.status == "published"


if __name__ == "__main__":
    # 纯逻辑入口：仅验证默认常量（不连 DB）
    test_run_total_timeout_default_is_7200_seconds()
    print("默认超时 7200 秒 ✓")
    print("OK")
