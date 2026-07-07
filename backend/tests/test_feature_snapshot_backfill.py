"""feature_snapshot_backfill 脚本测试。

验证维度：
1. parse_args: 参数解析（默认值 / 自定义值 / --dry-run / --resume）
2. get_trade_dates_from_bars: 从 bars_daily 查询已有交易日
3. get_latest_bar_date: 查询最新 bars_daily 日期
4. get_existing_instrument_ids: [Blocker3] 查询已存在 snapshot 的 instrument_id 集合
5. backfill_single_date: --dry-run 输出 missing 数量，不写库
6. backfill_single_date: 正常模式调用 compute_for_trade_date
7. backfill_single_date: --resume 真正跳过已存在 instrument（不重新计算）
8. backfill_single_date: --resume 无已存在时调用 compute 处理全部
9. main: 端到端 dry-run 流程，不写库
10. main: 单日失败 rollback 不阻塞其他日

[Blocker2] 事务边界：
- backfill_single_date 不内部 commit，由 main 控制 commit/rollback
- 单日 RuntimeError → rollback 该日所有半成品 → 继续下一日

[Blocker3] resume 真正跳过：
- 按完整唯一键 (instrument_id, trade_date, tf, adj, schema_version) 查询已存在
- 只对 missing instrument 调用 compute_for_trade_date
- dry-run 输出 missing_instruments 数量

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://... \
        pytest tests/test_feature_snapshot_backfill.py -v
"""

from __future__ import annotations

import argparse
import uuid
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from scripts.feature_snapshot_backfill import (
    backfill_single_date,
    get_existing_instrument_ids,
    get_latest_bar_date,
    get_trade_dates_from_bars,
    main,
    parse_args,
)

# ===== 1. parse_args =====


def test_parse_args_defaults() -> None:
    """parse_args 默认值：end=latest, batch_size=20, failure_threshold=0.3。

    [Blocker2] commit_every 参数已移除（compute_for_trade_date 不再内部 commit）。
    """
    with patch(
        "sys.argv",
        ["feature_snapshot_backfill", "--start", "2026-01-01"],
    ):
        args = parse_args()
    assert args.start == "2026-01-01"
    assert args.end == "latest"
    assert args.batch_size == 20
    assert args.failure_threshold == 0.3
    assert args.resume is False
    assert args.dry_run is False
    # [Blocker2] commit_every 已移除
    assert not hasattr(args, "commit_every")


def test_parse_args_custom_values() -> None:
    """parse_args 自定义值。"""
    with patch(
        "sys.argv",
        [
            "feature_snapshot_backfill",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
            "--batch-size", "50",
            "--failure-threshold", "0.5",
            "--resume",
            "--dry-run",
        ],
    ):
        args = parse_args()
    assert args.start == "2026-01-01"
    assert args.end == "2026-06-30"
    assert args.batch_size == 50
    assert args.failure_threshold == 0.5
    assert args.resume is True
    assert args.dry_run is True


def test_parse_args_missing_start_fails() -> None:
    """parse_args 缺少 --start 应 SystemExit。"""
    with patch("sys.argv", ["feature_snapshot_backfill"]), \
        pytest.raises(SystemExit):
        parse_args()


# ===== 2. get_trade_dates_from_bars =====


@pytest.mark.asyncio
async def test_get_trade_dates_from_bars(db_session) -> None:
    """get_trade_dates_from_bars 返回升序交易日列表。"""
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST001",
        name="测试股票",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    for d in [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=d,
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000.0,
            amount=10_500_000.0,
            adj_factor=1.0,
        )
        db_session.add(bar)
    await db_session.flush()

    result = await get_trade_dates_from_bars(
        db_session, date(2026, 1, 1), date(2026, 1, 31),
    )
    assert result == [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]


@pytest.mark.asyncio
async def test_get_trade_dates_from_bars_empty(db_session) -> None:
    """无 bars_daily 数据时返回空列表。"""
    result = await get_trade_dates_from_bars(
        db_session, date(2026, 1, 1), date(2026, 1, 31),
    )
    assert result == []


# ===== 3. get_latest_bar_date =====


@pytest.mark.asyncio
async def test_get_latest_bar_date(db_session) -> None:
    """get_latest_bar_date 返回 bars_daily 最新 trade_date。"""
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST002",
        name="测试股票2",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    for d in [date(2026, 1, 5), date(2026, 1, 8), date(2026, 1, 6)]:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=d,
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000.0,
            amount=10_500_000.0,
            adj_factor=1.0,
        )
        db_session.add(bar)
    await db_session.flush()

    result = await get_latest_bar_date(db_session)
    assert result == date(2026, 1, 8)


@pytest.mark.asyncio
async def test_get_latest_bar_date_empty(db_session) -> None:
    """bars_daily 无数据时返回 None。"""
    result = await get_latest_bar_date(db_session)
    assert result is None


# ===== 4. get_existing_instrument_ids =====


@pytest.mark.asyncio
async def test_get_existing_instrument_ids_returns_set(db_session) -> None:
    """[Blocker3] get_existing_instrument_ids 返回已存在 snapshot 的 instrument_id 集合。

    按完整唯一键 (instrument_id, trade_date, primary_timeframe, secondary_timeframe,
    adj, schema_version) 过滤。
    """
    inst1 = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试1", market="SH", status="active",
    )
    inst2 = Instrument(
        id=uuid.uuid4(), symbol="600001", name="测试2", market="SH", status="active",
    )
    inst3 = Instrument(
        id=uuid.uuid4(), symbol="600002", name="测试3", market="SH", status="active",
    )
    db_session.add_all([inst1, inst2, inst3])
    await db_session.flush()

    target_date = date(2026, 1, 10)
    # 只为 inst1 和 inst2 预置 snapshot（inst3 缺失）
    for inst_id in [inst1.id, inst2.id]:
        snap = StockFeatureSnapshot(
            instrument_id=inst_id,
            trade_date=target_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            structural_payload={"test": "v1"},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )
        db_session.add(snap)
    await db_session.flush()

    result = await get_existing_instrument_ids(
        db_session, target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
    )

    # inst1 和 inst2 在结果中，inst3 不在
    assert inst1.id in result
    assert inst2.id in result
    assert inst3.id not in result
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_existing_instrument_ids_filters_by_schema_version(db_session) -> None:
    """[Blocker3] get_existing_instrument_ids 按 schema_version 严格过滤。

    schema_version=1 的 snapshot 不应被 schema_version=2 的查询返回。
    """
    inst = Instrument(
        id=uuid.uuid4(), symbol="600003", name="测试", market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 1, 10)
    snap = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,  # 已存在的是 v1
        source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        structural_payload={"test": "v1"},
        temporal_payload={},
        summary_payload={"_source": "feature_snapshot"},
        degraded_reasons=[],
    )
    db_session.add(snap)
    await db_session.flush()

    # 查询 schema_version=2 → 不应返回 inst
    result_v2 = await get_existing_instrument_ids(
        db_session, target_date, schema_version=2,
    )
    assert inst.id not in result_v2
    assert len(result_v2) == 0

    # 查询 schema_version=1 → 应返回 inst
    result_v1 = await get_existing_instrument_ids(
        db_session, target_date, schema_version=1,
    )
    assert inst.id in result_v1


# ===== 5. backfill_single_date: --dry-run =====


@pytest.mark.asyncio
async def test_backfill_single_date_dry_run_no_write(db_session) -> None:
    """--dry-run 模式不调用 compute_for_trade_date，不写库。

    [Blocker3] dry-run 应输出 missing_instruments 数量。
    """
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST004",
        name="测试股票4",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    mock_compute = AsyncMock(return_value={"snapshot_count": 0, "failed_count": 0})

    with patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=mock_compute,
    ):
        result = await backfill_single_date(
            db_session,
            date(2026, 1, 10),
            batch_size=20,
            failure_threshold=0.3,
            resume=False,
            dry_run=True,
        )

    assert result["dry_run"] is True
    assert result["total_instruments"] == 1
    assert result["missing_instruments"] == 1
    assert result["trade_date"] == "2026-01-10"
    # compute_for_trade_date 不应被调用
    mock_compute.assert_not_called()


# ===== 6. backfill_single_date: 正常模式 =====


@pytest.mark.asyncio
async def test_backfill_single_date_normal_calls_compute(db_session) -> None:
    """正常模式调用 compute_for_trade_date 并返回统计。

    [Blocker2] backfill_single_date 不内部 commit，由 caller 控制。
    """
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST005",
        name="测试股票5",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    expected_result = {
        "snapshot_count": 1,
        "failed_count": 0,
        "schema_version": 1,
        "trade_date": "2026-01-10",
    }
    mock_compute = AsyncMock(return_value=expected_result)

    with patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=mock_compute,
    ):
        result = await backfill_single_date(
            db_session,
            date(2026, 1, 10),
            batch_size=20,
            failure_threshold=0.3,
            resume=False,
            dry_run=False,
        )

    assert result == expected_result
    mock_compute.assert_awaited_once()


# ===== 7. backfill_single_date: --resume 真正跳过 =====


@pytest.mark.asyncio
async def test_backfill_single_date_resume_skips_existing(db_session) -> None:
    """[Blocker3] --resume 模式真正跳过已存在 instrument，不重新计算。

    场景：3 个 active instrument，其中 1 个已存在 snapshot。
    resume=True 时只对剩余 2 个调用 compute_for_trade_date。
    """
    inst1 = Instrument(
        id=uuid.uuid4(), symbol="600010", name="测试1", market="SH", status="active",
    )
    inst2 = Instrument(
        id=uuid.uuid4(), symbol="600011", name="测试2", market="SH", status="active",
    )
    inst3 = Instrument(
        id=uuid.uuid4(), symbol="600012", name="测试3", market="SH", status="active",
    )
    db_session.add_all([inst1, inst2, inst3])
    await db_session.flush()

    target_date = date(2026, 1, 10)
    # 为 inst1 预置已存在 snapshot
    existing_snap = StockFeatureSnapshot(
        instrument_id=inst1.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        structural_payload={"old": True},
        temporal_payload={},
        summary_payload={"_source": "feature_snapshot"},
        degraded_reasons=[],
    )
    db_session.add(existing_snap)
    await db_session.flush()

    captured_instruments: list = []

    async def _capture_compute(*args, **kwargs):
        # capture 第二个位置参数（instrument_ids）
        captured_instruments.extend(args[2] if len(args) > 2 else kwargs.get("instrument_ids", []))
        return {"snapshot_count": 2, "failed_count": 0}

    with patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst1.id, inst2.id, inst3.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=_capture_compute,
    ):
        result = await backfill_single_date(
            db_session,
            target_date,
            batch_size=20,
            failure_threshold=0.3,
            resume=True,
            dry_run=False,
        )

    # [Blocker3] 验证：只对 missing instruments 调用 compute（inst2 和 inst3）
    assert inst1.id not in captured_instruments, (
        f"inst1 已存在 snapshot，不应被重新计算: {captured_instruments}"
    )
    assert inst2.id in captured_instruments
    assert inst3.id in captured_instruments
    assert len(captured_instruments) == 2

    # 结果应反映实际处理的数量
    assert result["snapshot_count"] == 2


@pytest.mark.asyncio
async def test_backfill_single_date_resume_no_existing_calls_compute_with_all(db_session) -> None:
    """[Blocker3] --resume 模式无已存在 snapshot 时调用 compute 处理全部 instrument。"""
    inst1 = Instrument(
        id=uuid.uuid4(), symbol="600020", name="测试1", market="SH", status="active",
    )
    inst2 = Instrument(
        id=uuid.uuid4(), symbol="600021", name="测试2", market="SH", status="active",
    )
    db_session.add_all([inst1, inst2])
    await db_session.flush()

    captured_instruments: list = []

    async def _capture_compute(*args, **kwargs):
        captured_instruments.extend(args[2] if len(args) > 2 else kwargs.get("instrument_ids", []))
        return {"snapshot_count": 2, "failed_count": 0}

    with patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst1.id, inst2.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=_capture_compute,
    ):
        await backfill_single_date(
            db_session,
            date(2026, 1, 10),
            batch_size=20,
            failure_threshold=0.3,
            resume=True,
            dry_run=False,
        )

    # 无已存在 snapshot，应处理全部
    assert inst1.id in captured_instruments
    assert inst2.id in captured_instruments
    assert len(captured_instruments) == 2


@pytest.mark.asyncio
async def test_backfill_single_date_resume_all_existing_skips_compute(db_session) -> None:
    """[Blocker3] --resume 模式全部已存在时不调用 compute。"""
    inst = Instrument(
        id=uuid.uuid4(), symbol="600030", name="测试", market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 1, 10)
    snap = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        structural_payload={"old": True},
        temporal_payload={},
        summary_payload={"_source": "feature_snapshot"},
        degraded_reasons=[],
    )
    db_session.add(snap)
    await db_session.flush()

    mock_compute = AsyncMock(return_value={"snapshot_count": 0, "failed_count": 0})

    with patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=mock_compute,
    ):
        result = await backfill_single_date(
            db_session,
            target_date,
            batch_size=20,
            failure_threshold=0.3,
            resume=True,
            dry_run=False,
        )

    # 全部已存在，不应调用 compute
    mock_compute.assert_not_called()
    assert result["snapshot_count"] == 0
    assert result["skipped_existing"] == 1


# ===== 8. main: 端到端 dry-run =====


@pytest.mark.asyncio
async def test_main_dry_run_no_write(db_session) -> None:
    """main(--dry-run) 不调用 compute_for_trade_date。"""
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST007",
        name="测试股票7",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    # 在测试库预置 bars_daily
    for d in [date(2026, 1, 5), date(2026, 1, 6)]:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=d,
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000.0,
            amount=10_500_000.0,
            adj_factor=1.0,
        )
        db_session.add(bar)
    await db_session.flush()

    mock_compute = AsyncMock()

    args = argparse.Namespace(
        start="2026-01-01",
        end="2026-01-31",
        batch_size=20,
        failure_threshold=0.3,
        resume=False,
        dry_run=True,
    )

    # 使用测试库 session 替换 AsyncSessionLocal
    class _FakeCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    with patch(
        "scripts.feature_snapshot_backfill.AsyncSessionLocal",
        return_value=_FakeCtx(),
    ), patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=mock_compute,
    ):
        await main(args)

    # dry-run 不应调用 compute
    mock_compute.assert_not_called()


# ===== 9. main: 单日失败 rollback 不阻塞其他日 =====


@pytest.mark.asyncio
async def test_main_single_date_failure_rolls_back_and_continues(db_session) -> None:
    """[Blocker2] 单日 RuntimeError → rollback 该日半成品 → 继续下一日。

    场景：2 个交易日，第一天 compute 抛 RuntimeError，第二天成功。
    要求：
    1. 第一天调用 db.rollback()
    2. 第二天继续执行（不阻塞）
    3. 第二天调用 db.commit()
    """
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST008",
        name="测试股票8",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    for d in [date(2026, 1, 5), date(2026, 1, 6)]:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=d,
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000.0,
            amount=10_500_000.0,
            adj_factor=1.0,
        )
        db_session.add(bar)
    await db_session.flush()

    # 第一次调用失败，第二次成功
    call_count = 0

    async def _mock_compute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("模拟 trade_date=2026-01-05 失败比例超阈值")
        return {"snapshot_count": 1, "failed_count": 0}

    args = argparse.Namespace(
        start="2026-01-01",
        end="2026-01-31",
        batch_size=20,
        failure_threshold=0.3,
        resume=False,
        dry_run=False,
    )

    # 记录 commit / rollback 调用
    commit_count = 0
    rollback_count = 0
    original_commit = db_session.commit
    original_rollback = db_session.rollback

    async def _fake_commit():
        nonlocal commit_count
        commit_count += 1
        await original_commit()

    async def _fake_rollback():
        nonlocal rollback_count
        rollback_count += 1
        await original_rollback()

    class _FakeCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    with patch(
        "scripts.feature_snapshot_backfill.AsyncSessionLocal",
        return_value=_FakeCtx(),
    ), patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=_mock_compute,
    ), patch.object(
        db_session, "commit", new=_fake_commit,
    ), patch.object(
        db_session, "rollback", new=_fake_rollback,
    ):
        await main(args)

    # 两次都被调用（单日失败不阻塞其他日）
    assert call_count == 2
    # [Blocker2] 第一次失败应 rollback，第二次成功应 commit
    assert rollback_count == 1, f"第一天失败应 rollback 1 次，实际 {rollback_count}"
    assert commit_count == 1, f"第二天成功应 commit 1 次，实际 {commit_count}"


# ===== 10. main: end=latest 解析 =====


@pytest.mark.asyncio
async def test_main_end_latest_resolves_to_max_bar_date(db_session) -> None:
    """main(--end=latest) 解析为 bars_daily 最大 trade_date。"""
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST009",
        name="测试股票9",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    # bars_daily 中最大日期为 2026-01-08
    for d in [date(2026, 1, 5), date(2026, 1, 8)]:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=d,
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000.0,
            amount=10_500_000.0,
            adj_factor=1.0,
        )
        db_session.add(bar)
    await db_session.flush()

    args = argparse.Namespace(
        start="2026-01-01",
        end="latest",
        batch_size=20,
        failure_threshold=0.3,
        resume=False,
        dry_run=True,
    )

    class _FakeCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    with patch(
        "scripts.feature_snapshot_backfill.AsyncSessionLocal",
        return_value=_FakeCtx(),
    ), patch(
        "scripts.feature_snapshot_backfill.get_active_a_share_instruments",
        new=AsyncMock(return_value=[inst.id]),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_for_trade_date",
        new=AsyncMock(),
    ):
        await main(args)

    # main 应正常完成（如果 end=latest 解析失败会 sys.exit(1)）


# ===== 11. main: start > end 应退出 =====


@pytest.mark.asyncio
async def test_main_start_after_end_exits(db_session) -> None:
    """start > end 时 main 应 sys.exit(1)。"""
    args = argparse.Namespace(
        start="2026-06-30",
        end="2026-01-01",
        batch_size=20,
        failure_threshold=0.3,
        resume=False,
        dry_run=True,
    )

    class _FakeCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    with patch(
        "scripts.feature_snapshot_backfill.AsyncSessionLocal",
        return_value=_FakeCtx(),
    ), pytest.raises(SystemExit) as exc_info:
        await main(args)

    assert exc_info.value.code == 1
