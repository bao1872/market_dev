"""feature_snapshot_backfill 脚本测试。

验证维度：
1. parse_args: 参数解析（默认值 / 自定义值 / --dry-run / --resume）
2. get_trade_dates_from_bars: 从 bars_daily 查询已有交易日
3. get_latest_bar_date: 查询最新 bars_daily 日期
4. count_existing_snapshots: 查询某交易日已存在 snapshot 数（--resume 用）
5. backfill_single_date: --dry-run 不写库
6. backfill_single_date: 正常模式调用 compute_for_trade_date
7. main: 端到端 dry-run 流程，不写库
8. main: 单日失败不阻塞其他日

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
    count_existing_snapshots,
    get_latest_bar_date,
    get_trade_dates_from_bars,
    main,
    parse_args,
)

# ===== 1. parse_args =====


def test_parse_args_defaults() -> None:
    """parse_args 默认值：end=latest, batch_size=20, commit_every=500, failure_threshold=0.3。"""
    with patch(
        "sys.argv",
        ["feature_snapshot_backfill", "--start", "2026-01-01"],
    ):
        args = parse_args()
    assert args.start == "2026-01-01"
    assert args.end == "latest"
    assert args.batch_size == 20
    assert args.commit_every == 500
    assert args.failure_threshold == 0.3
    assert args.resume is False
    assert args.dry_run is False


def test_parse_args_custom_values() -> None:
    """parse_args 自定义值。"""
    with patch(
        "sys.argv",
        [
            "feature_snapshot_backfill",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
            "--batch-size", "50",
            "--commit-every", "100",
            "--failure-threshold", "0.5",
            "--resume",
            "--dry-run",
        ],
    ):
        args = parse_args()
    assert args.start == "2026-01-01"
    assert args.end == "2026-06-30"
    assert args.batch_size == 50
    assert args.commit_every == 100
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

    # 插入 3 个交易日 bars_daily
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


# ===== 4. count_existing_snapshots =====


@pytest.mark.asyncio
async def test_count_existing_snapshots(db_session) -> None:
    """count_existing_snapshots 返回某交易日已存在的 snapshot 数。"""
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST003",
        name="测试股票3",
        market="SH",
        status="active",
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
        structural_payload={"test": "v1"},
        temporal_payload={},
        summary_payload={"_source": "feature_snapshot"},
        degraded_reasons=[],
    )
    db_session.add(snap)
    await db_session.flush()

    result = await count_existing_snapshots(db_session, target_date)
    assert result == 1

    # 查询无 snapshot 的日期
    result_empty = await count_existing_snapshots(db_session, date(2026, 2, 1))
    assert result_empty == 0


# ===== 5. backfill_single_date: --dry-run =====


@pytest.mark.asyncio
async def test_backfill_single_date_dry_run_no_write(db_session) -> None:
    """--dry-run 模式不调用 compute_for_trade_date，不写库。"""
    # 准备 instrument
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
            commit_every=500,
            failure_threshold=0.3,
            resume=False,
            dry_run=True,
        )

    assert result["dry_run"] is True
    assert result["total_instruments"] == 1
    assert result["trade_date"] == "2026-01-10"
    # compute_for_trade_date 不应被调用
    mock_compute.assert_not_called()


# ===== 6. backfill_single_date: 正常模式 =====


@pytest.mark.asyncio
async def test_backfill_single_date_normal_calls_compute(db_session) -> None:
    """正常模式调用 compute_for_trade_date 并返回统计。"""
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
            commit_every=500,
            failure_threshold=0.3,
            resume=False,
            dry_run=False,
        )

    assert result == expected_result
    mock_compute.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_single_date_resume_logs_existing(db_session) -> None:
    """--resume 模式查询并记录已存在 snapshot 数，但仍调用 compute（upsert 覆盖）。"""
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST006",
        name="测试股票6",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 1, 10)
    # 预置一条已存在 snapshot
    existing_snap = StockFeatureSnapshot(
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
    db_session.add(existing_snap)
    await db_session.flush()

    mock_compute = AsyncMock(return_value={
        "snapshot_count": 1, "failed_count": 0,
    })

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
            commit_every=500,
            failure_threshold=0.3,
            resume=True,
            dry_run=False,
        )

    # resume 模式下仍调用 compute（因为 upsert 会覆盖）
    mock_compute.assert_awaited_once()
    assert result["snapshot_count"] == 1


# ===== 7. main: 端到端 dry-run =====


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
        commit_every=500,
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


# ===== 8. main: 单日失败不阻塞其他日 =====


@pytest.mark.asyncio
async def test_main_single_date_failure_does_not_block(db_session) -> None:
    """单日回补失败不阻塞其他日，main 继续执行并在汇总中列出 error_dates。"""
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
            raise RuntimeError("模拟 trade_date=2026-01-05 失败")
        return {"snapshot_count": 1, "failed_count": 0}

    args = argparse.Namespace(
        start="2026-01-01",
        end="2026-01-31",
        batch_size=20,
        commit_every=500,
        failure_threshold=0.3,
        resume=False,
        dry_run=False,
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
        new=_mock_compute,
    ):
        # main 捕获单日失败，不抛异常
        await main(args)

    # 两次都被调用（单日失败不阻塞其他日）
    assert call_count == 2


# ===== 9. main: end=latest 解析 =====


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
        commit_every=500,
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


# ===== 10. main: start > end 应退出 =====


@pytest.mark.asyncio
async def test_main_start_after_end_exits(db_session) -> None:
    """start > end 时 main 应 sys.exit(1)。"""
    args = argparse.Namespace(
        start="2026-06-30",
        end="2026-01-01",
        batch_size=20,
        commit_every=500,
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
