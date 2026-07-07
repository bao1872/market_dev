"""feature_snapshot_backfill 脚本测试（instrument-first 重构后）。

验证维度：
1. parse_args: 参数解析（默认值 / 自定义值 / --symbols / --limit-instruments / --dry-run / --resume）
2. get_trade_dates_from_bars: 从 bars_daily 查询已有交易日
3. get_latest_bar_date: 查询最新 bars_daily 日期
4. get_existing_instrument_ids: 查询已存在 snapshot 的 instrument_id 集合
5. get_instruments_for_backfill: --symbols / --limit-instruments 过滤
6. load_instrument_bars: 一次性加载 1d + 15m bars
7. backfill_instrument_first:
   - --dry-run 不写库
   - instrument-first 不重复调用 get_bars（mock 断言）
   - --resume 跳过已存在 + succeeded run 的 snapshot
   - 成功创建 succeeded run
   - 失败比例超阈值创建 failed run（不抛异常）
8. main: end=latest 解析、start>end 退出、--symbols 小样本
9. multiprocessing:
   - --workers 参数解析（默认 1，自定义 N）
   - _worker_process_instruments: per-instrument commit、resume 跳过、单股失败不阻塞
   - backfill_instrument_first_parallel: 创建/finish run records、scope 传播、空输入返回

[instrument-first 事务边界]：
- backfill_instrument_first 不内部 commit，由 main 控制
- 失败比例超阈值的 trade_date 标 run.status='failed'（不抛 RuntimeError）
- 单股失败不阻塞其他股票
- watchlist 只读取 run.status='succeeded' 的 snapshot（Phase 5 run gate）

[multiprocessing 事务边界]：
- backfill_instrument_first_parallel 主进程创建/finish run records
- _worker_process_instruments per-instrument commit（被 kill 不丢已完成，resume 可续）
- 单 worker 失败不阻塞其他 workers
- run scope 传播到 metadata_['scope']

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://... \
        pytest tests/test_feature_snapshot_backfill.py -v
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from scripts.feature_snapshot_backfill import (
    _resolve_run_scope,
    _worker_process_instruments,
    backfill_instrument_first,
    backfill_instrument_first_parallel,
    get_existing_instrument_ids,
    get_instruments_for_backfill,
    get_latest_bar_date,
    get_trade_dates_from_bars,
    load_instrument_bars,
    main,
    parse_args,
)

# ===== 1. parse_args =====


def test_parse_args_defaults() -> None:
    """parse_args 默认值：end=latest, batch_size=20, failure_threshold=0.3。

    --symbols / --limit-instruments 默认为 None（不限制）。
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
    assert args.symbols is None
    assert args.limit_instruments is None


def test_parse_args_custom_values() -> None:
    """parse_args 自定义值 + --symbols + --limit-instruments。"""
    with patch(
        "sys.argv",
        [
            "feature_snapshot_backfill",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
            "--batch-size", "50",
            "--failure-threshold", "0.5",
            "--symbols", "000100,603303",
            "--limit-instruments", "20",
            "--resume",
            "--dry-run",
        ],
    ):
        args = parse_args()
    assert args.start == "2026-01-01"
    assert args.end == "2026-06-30"
    assert args.batch_size == 50
    assert args.failure_threshold == 0.5
    assert args.symbols == ["000100", "603303"]
    assert args.limit_instruments == 20
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
    """get_existing_instrument_ids 返回已存在 snapshot 的 instrument_id 集合。

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

    assert inst1.id in result
    assert inst2.id in result
    assert inst3.id not in result
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_existing_instrument_ids_filters_by_schema_version(db_session) -> None:
    """get_existing_instrument_ids 按 schema_version 严格过滤。"""
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


# ===== 5. get_instruments_for_backfill: --symbols / --limit-instruments =====


@pytest.mark.asyncio
async def test_get_instruments_for_backfill_with_symbols(db_session) -> None:
    """--symbols 过滤：只返回匹配 symbol 的 instrument_id。"""
    inst1 = Instrument(
        id=uuid.uuid4(), symbol="000100", name="TCL", market="SZ", status="active",
    )
    inst2 = Instrument(
        id=uuid.uuid4(), symbol="603303", name="全柴动力", market="SH", status="active",
    )
    inst3 = Instrument(
        id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH", status="active",
    )
    db_session.add_all([inst1, inst2, inst3])
    await db_session.flush()

    result = await get_instruments_for_backfill(
        db_session, symbols=["000100", "603303"],
    )
    assert inst1.id in result
    assert inst2.id in result
    assert inst3.id not in result
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_instruments_for_backfill_with_limit(db_session) -> None:
    """--limit-instruments 限制返回数量。"""
    for i in range(5):
        db_session.add(Instrument(
            id=uuid.uuid4(),
            symbol=f"60000{i}",
            name=f"测试{i}",
            market="SH",
            status="active",
        ))
    await db_session.flush()

    result = await get_instruments_for_backfill(db_session, limit=3)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_get_instruments_for_backfill_no_filter(db_session) -> None:
    """无 symbols/limit 时返回全部 active A 股（6 位数字 symbol）。"""
    db_session.add(Instrument(
        id=uuid.uuid4(), symbol="600000", name="浦发", market="SH", status="active",
    ))
    db_session.add(Instrument(
        id=uuid.uuid4(), symbol="000001", name="平安", market="SZ", status="active",
    ))
    # 非 A 股（非 6 位数字）不应返回
    db_session.add(Instrument(
        id=uuid.uuid4(), symbol="ETF510300", name="沪深ETF", market="SH", status="active",
    ))
    await db_session.flush()

    result = await get_instruments_for_backfill(db_session)
    assert len(result) == 2  # 只返回 6 位数字 symbol 的


# ===== 6. load_instrument_bars: 一次性加载 1d + 15m bars =====


@pytest.mark.asyncio
async def test_load_instrument_bars_returns_dataframes(db_session) -> None:
    """load_instrument_bars 返回 (primary_bars, secondary_bars) 元组。

    通过 mock _fetch_bars_from_db 验证：每只 instrument 每周期只调用一次。
    """
    inst = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试", market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    fake_1d = pd.DataFrame(
        {"close": [10.0, 11.0]},
        index=pd.DatetimeIndex(
            pd.to_datetime(["2026-01-05", "2026-01-06"]), name="trade_date"
        ),
    )
    fake_15m = pd.DataFrame(
        {"close": [10.0, 10.5]},
        index=pd.DatetimeIndex(
            pd.to_datetime(["2026-01-05 09:30", "2026-01-05 09:45"]), name="trade_date"
        ),
    )

    call_count = {"1d": 0, "15m": 0}

    async def _fake_fetch(session, instrument_id, timeframe, adj, trade_date):
        call_count[timeframe] += 1
        if timeframe == "1d":
            return fake_1d
        return fake_15m

    with patch(
        "scripts.feature_snapshot_backfill._fetch_bars_from_db",
        new=_fake_fetch,
    ):
        primary_bars, secondary_bars = await load_instrument_bars(
            db_session, inst.id,
        )

    assert primary_bars is fake_1d
    assert secondary_bars is fake_15m
    # 每周期只调用 1 次
    assert call_count["1d"] == 1
    assert call_count["15m"] == 1


# ===== 7. backfill_instrument_first: --dry-run 不写库 =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_dry_run_no_write(db_session) -> None:
    """--dry-run 模式不调用 compute_feature_snapshot_for_date，不写库。

    返回 dry_run=True + trade_dates / instruments / batches 统计。
    """
    inst = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试", market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    mock_compute = AsyncMock()

    with patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=AsyncMock(return_value=(None, None)),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=mock_compute,
    ), patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=AsyncMock(),
    ):
        result = await backfill_instrument_first(
            db_session,
            trade_dates=[date(2026, 1, 5), date(2026, 1, 6)],
            instruments=[inst.id],
            batch_size=20,
            failure_threshold=0.3,
            resume=False,
            dry_run=True,
        )

    assert result["dry_run"] is True
    assert result["trade_dates"] == 2
    assert result["instruments"] == 1
    assert result["expected_batches"] == 1
    # compute / upsert 不应被调用
    mock_compute.assert_not_called()


# ===== 8. backfill_instrument_first: instrument-first 不重复调用 get_bars =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_no_repeat_get_bars(db_session) -> None:
    """[Phase8 要求 10] instrument-first：每只 instrument 每周期只取一次历史 bars。

    场景：2 个 instrument × 2 个 trade_date。
    要求：load_instrument_bars 每只 instrument 只调用 1 次（共 2 次），
         而不是 4 次（date-first 时会重复）。
    """
    inst1 = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试1", market="SH", status="active",
    )
    inst2 = Instrument(
        id=uuid.uuid4(), symbol="600001", name="测试2", market="SH", status="active",
    )
    db_session.add_all([inst1, inst2])
    await db_session.flush()

    load_calls: list[uuid.UUID] = []

    async def _track_load(session, instrument_id, **kwargs):
        load_calls.append(instrument_id)
        return (None, None)

    async def _fake_compute(session, instrument_id, trade_date, **kwargs):
        # 返回简单 snapshot，验证只调用 4 次（2 instrument × 2 date）
        snap = StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            source_secondary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )
        return snap

    # mock 两个服务层函数（不真的连 MarketDataAggregationService）
    with patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=_track_load,
    ), patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=_fake_compute,
    ), patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ):
        result = await backfill_instrument_first(
            db_session,
            trade_dates=[date(2026, 1, 5), date(2026, 1, 6)],
            instruments=[inst1.id, inst2.id],
            batch_size=20,
            failure_threshold=0.3,
            resume=False,
            dry_run=False,
        )

    # 关键断言：load_instrument_bars 每只 instrument 只调用 1 次（共 2 次）
    assert len(load_calls) == 2, (
        f"instrument-first 应每只 instrument 只调用 1 次 load_bars，实际 {len(load_calls)} 次"
    )
    assert set(load_calls) == {inst1.id, inst2.id}
    # compute 应调用 4 次（2 instrument × 2 date）
    assert result["total_snapshots"] == 4
    assert result["total_failed"] == 0


# ===== 9. backfill_instrument_first: --resume 跳过 published snapshot =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_resume_skips_published(db_session) -> None:
    """[Phase8 要求 11] --resume 跳过 已存在 snapshot 且 succeeded run 的行。

    场景：2 个 instrument × 1 个 trade_date，其中：
    - inst1 已有 snapshot 且 run=succeeded → 跳过
    - inst2 无 snapshot → 计算

    要求：compute 只被调用 1 次（inst2），不为 inst1 重新计算。
    """
    inst1 = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试1", market="SH", status="active",
    )
    inst2 = Instrument(
        id=uuid.uuid4(), symbol="600001", name="测试2", market="SH", status="active",
    )
    db_session.add_all([inst1, inst2])
    await db_session.flush()

    target_date = date(2026, 1, 5)
    # 为 inst1 预置已存在 snapshot
    existing_snap = StockFeatureSnapshot(
        instrument_id=inst1.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(2026, 1, 5, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        source_secondary_bar_time=datetime(2026, 1, 5, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        structural_payload={"old": True},
        temporal_payload={},
        summary_payload={"_source": "feature_snapshot"},
        degraded_reasons=[],
    )
    db_session.add(existing_snap)
    # 预置 succeeded run（resume 才会跳过）
    succeeded_run = StockFeatureSnapshotRun(
        trade_date=target_date,
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="backfill",
        status="succeeded",
        snapshot_count=1,
        failed_count=0,
        failure_rate=0.0,
        started_at=datetime.now(ZoneInfo("UTC")),
        finished_at=datetime.now(ZoneInfo("UTC")),
        published_at=datetime.now(ZoneInfo("UTC")),
    )
    db_session.add(succeeded_run)
    await db_session.flush()

    compute_calls: list[uuid.UUID] = []

    async def _track_compute(session, instrument_id, trade_date, **kwargs):
        compute_calls.append(instrument_id)
        return StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            source_secondary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )

    with patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=AsyncMock(return_value=(None, None)),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=_track_compute,
    ), patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ):
        result = await backfill_instrument_first(
            db_session,
            trade_dates=[target_date],
            instruments=[inst1.id, inst2.id],
            batch_size=20,
            failure_threshold=0.3,
            resume=True,  # 关键：开启 resume
            dry_run=False,
        )

    # inst1 已存在 + succeeded run → 跳过；只 inst2 被计算
    assert len(compute_calls) == 1, (
        f"resume 应跳过 inst1（已存在 + succeeded），只计算 inst2，实际调用 {len(compute_calls)} 次"
    )
    assert compute_calls[0] == inst2.id
    assert result["total_snapshots"] == 1
    assert result["skipped_existing"] == 1


# ===== 10. backfill_instrument_first: 成功创建 succeeded run =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_creates_succeeded_run(db_session) -> None:
    """所有 instrument 计算成功 → 创建 succeeded run（写 published_at）。"""
    inst = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试", market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    async def _fake_compute(session, instrument_id, trade_date, **kwargs):
        return StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            source_secondary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )

    with patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=AsyncMock(return_value=(None, None)),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=_fake_compute,
    ), patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ) as mock_create, patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ) as mock_finish:
        await backfill_instrument_first(
            db_session,
            trade_dates=[date(2026, 1, 5)],
            instruments=[inst.id],
            batch_size=20,
            failure_threshold=0.3,
            resume=False,
            dry_run=False,
        )

    # 应创建 1 个 run（每个 trade_date 1 个）
    assert mock_create.await_count == 1
    # 应 finish 为 succeeded（失败率 0）
    assert mock_finish.await_count == 1
    finish_kwargs = mock_finish.await_args.kwargs
    assert finish_kwargs["status"] == "succeeded"
    assert finish_kwargs["snapshot_count"] == 1
    assert finish_kwargs["failed_count"] == 0


# ===== 11. backfill_instrument_first: 失败比例超阈值创建 failed run =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_creates_failed_run(db_session) -> None:
    """失败比例超阈值 → 创建 failed run（不抛异常，run.status='failed'）。

    场景：3 个 instrument，2 个失败（failure_rate=0.67 > 0.3 阈值）。
    要求：
    1. 不抛 RuntimeError（与 date-first 不同）
    2. run.status='failed'，不写 published_at
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

    async def _flaky_compute(session, instrument_id, trade_date, **kwargs):
        if instrument_id == inst2.id:
            raise ValueError("模拟 inst2 计算失败")
        if instrument_id == inst3.id:
            raise ValueError("模拟 inst3 计算失败")
        # inst1 成功
        return StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            source_secondary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )

    with patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=AsyncMock(return_value=(None, None)),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=_flaky_compute,
    ), patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ) as mock_finish:
        result = await backfill_instrument_first(
            db_session,
            trade_dates=[date(2026, 1, 5)],
            instruments=[inst1.id, inst2.id, inst3.id],
            batch_size=20,
            failure_threshold=0.3,
            resume=False,
            dry_run=False,
        )

    # 不抛异常（与 date-first 不同）
    assert result["total_snapshots"] == 1
    assert result["total_failed"] == 2
    # run.status='failed'（failure_rate=0.67 > 0.3）
    assert mock_finish.await_count == 1
    finish_kwargs = mock_finish.await_args.kwargs
    assert finish_kwargs["status"] == "failed"
    assert finish_kwargs["snapshot_count"] == 1
    assert finish_kwargs["failed_count"] == 2


# ===== 12. main: end=latest 解析 =====


@pytest.mark.asyncio
async def test_main_end_latest_resolves_to_max_bar_date(db_session) -> None:
    """main(--end=latest) 解析为 bars_daily 最大 trade_date。"""
    inst = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试", market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    for d in [date(2026, 1, 5), date(2026, 1, 8)]:
        bar = BarDaily(
            instrument_id=inst.id,
            trade_date=d,
            open=10.0, high=11.0, low=9.5, close=10.5,
            volume=1_000_000.0, amount=10_500_000.0, adj_factor=1.0,
        )
        db_session.add(bar)
    await db_session.flush()

    args = argparse.Namespace(
        start="2026-01-01",
        end="latest",
        batch_size=20,
        failure_threshold=0.3,
        symbols=None,
        limit_instruments=None,
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
    ):
        # dry-run 应正常完成；若 end=latest 解析失败会 sys.exit(1)
        await main(args)


# ===== 13. main: start > end 应退出 =====


@pytest.mark.asyncio
async def test_main_start_after_end_exits(db_session) -> None:
    """start > end 时 main 应 sys.exit(1)。"""
    args = argparse.Namespace(
        start="2026-06-30",
        end="2026-01-01",
        batch_size=20,
        failure_threshold=0.3,
        symbols=None,
        limit_instruments=None,
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


# ===== 14. main: --symbols 小样本 =====


@pytest.mark.asyncio
async def test_main_symbols_small_sample(db_session) -> None:
    """main(--symbols) 只处理指定 symbol 的 instrument（小样本验证）。"""
    inst1 = Instrument(
        id=uuid.uuid4(), symbol="000100", name="TCL", market="SZ", status="active",
    )
    inst2 = Instrument(
        id=uuid.uuid4(), symbol="603303", name="全柴动力", market="SH", status="active",
    )
    # 干扰项：不应被处理
    inst3 = Instrument(
        id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH", status="active",
    )
    db_session.add_all([inst1, inst2, inst3])
    await db_session.flush()

    for d in [date(2026, 1, 5)]:
        for inst in [inst1, inst2, inst3]:
            bar = BarDaily(
                instrument_id=inst.id,
                trade_date=d,
                open=10.0, high=11.0, low=9.5, close=10.5,
                volume=1_000_000.0, amount=10_500_000.0, adj_factor=1.0,
            )
            db_session.add(bar)
    await db_session.flush()

    args = argparse.Namespace(
        start="2026-01-01",
        end="2026-01-31",
        batch_size=20,
        failure_threshold=0.3,
        symbols=["000100", "603303"],
        limit_instruments=None,
        resume=False,
        dry_run=True,  # dry-run 验证只统计 2 个 instrument
    )

    class _FakeCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    with patch(
        "scripts.feature_snapshot_backfill.AsyncSessionLocal",
        return_value=_FakeCtx(),
    ):
        # dry-run 应正常完成，且只处理 2 个 instrument（不含 inst3）
        await main(args)

    # 重新查询 instrument 表验证 symbols 过滤生效（dry-run 不应改动 DB）
    from sqlalchemy import select
    result = await db_session.execute(select(Instrument.symbol))
    symbols_in_db = set(result.scalars().all())
    assert {"000100", "603303", "600519"}.issubset(symbols_in_db)


# ===== Blocker Fix: scope 区分 full / sample =====


def test_resolve_run_scope_returns_sample_when_symbols_set() -> None:
    """[Blocker] --symbols 启用时 scope='sample'。"""
    assert _resolve_run_scope(symbols=["000100", "603303"], limit_instruments=None) == "sample"


def test_resolve_run_scope_returns_sample_when_limit_instruments_set() -> None:
    """[Blocker] --limit-instruments 启用时 scope='sample'。"""
    assert _resolve_run_scope(symbols=None, limit_instruments=20) == "sample"


def test_resolve_run_scope_returns_full_when_no_filter() -> None:
    """[Blocker] 无 --symbols 且无 --limit-instruments 时 scope='full'。"""
    assert _resolve_run_scope(symbols=None, limit_instruments=None) == "full"


@pytest.mark.asyncio
async def test_backfill_instrument_first_propagates_sample_scope_to_run_metadata(
    db_session,
) -> None:
    """[Blocker] backfill_instrument_first(scope='sample') → create_snapshot_run + finish_snapshot_run 收到 scope。

    验证：
    - create_snapshot_run 被调用时含 scope='sample' kwarg
    - finish_snapshot_run 被调用时 metadata 含 'scope': 'sample'
    防止小样本验证产生的 run 污染 watchlist SUCCEEDED 状态。
    """
    inst = Instrument(
        id=uuid.uuid4(), symbol="600000", name="测试", market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    async def _fake_compute(session, instrument_id, trade_date, **kwargs):
        return StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            source_secondary_bar_time=datetime(
                trade_date.year, trade_date.month, trade_date.day,
                15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )

    with patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=AsyncMock(return_value=(None, None)),
    ), patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=_fake_compute,
    ), patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ) as mock_create, patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ) as mock_finish:
        await backfill_instrument_first(
            db_session,
            trade_dates=[date(2026, 1, 5)],
            instruments=[inst.id],
            batch_size=20,
            scope="sample",  # 关键：传入 sample scope
        )

    # 验证 create_snapshot_run 收到 scope='sample' kwarg
    assert mock_create.called, "create_snapshot_run 应被调用"
    create_kwargs = mock_create.call_args.kwargs
    assert create_kwargs.get("scope") == "sample", (
        f"create_snapshot_run 应收到 scope='sample'，实际 kwargs: {create_kwargs}"
    )

    # 验证 finish_snapshot_run 收到的 metadata 含 scope='sample'
    assert mock_finish.called, "finish_snapshot_run 应被调用"
    finish_kwargs = mock_finish.call_args.kwargs
    finish_metadata = finish_kwargs.get("metadata", {})
    assert finish_metadata.get("scope") == "sample", (
        f"finish_snapshot_run 的 metadata 应含 scope='sample'，实际: {finish_metadata}"
    )


# =============================================================================
# multiprocessing 测试
# =============================================================================


def _make_snapshot(
    instrument_id: uuid.UUID, trade_date: date,
) -> StockFeatureSnapshot:
    """测试辅助：构造 StockFeatureSnapshot。"""
    return StockFeatureSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(
            trade_date.year, trade_date.month, trade_date.day,
            15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        source_secondary_bar_time=datetime(
            trade_date.year, trade_date.month, trade_date.day,
            15, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        structural_payload={},
        temporal_payload={},
        summary_payload={"_source": "feature_snapshot"},
        degraded_reasons=[],
    )


class _FakeWorkerSession:
    """worker 测试用的 fake async session（支持 commit/rollback + async ctx）。"""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self) -> _FakeWorkerSession:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeWorkerEngine:
    """worker 测试用的 fake async engine。"""

    async def dispose(self) -> None:
        pass


def _patch_worker_deps(
    fake_session: _FakeWorkerSession,
    fake_load,
    fake_compute,
    fake_upsert,
):
    """返回 patch context，统一处理 worker 内部 import 的依赖。"""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch(
        "sqlalchemy.ext.asyncio.create_async_engine",
        return_value=_FakeWorkerEngine(),
    ))
    # async_sessionmaker(*args, **kwargs) → factory; factory() → session
    stack.enter_context(patch(
        "sqlalchemy.ext.asyncio.async_sessionmaker",
        return_value=lambda *a, **kw: fake_session,
    ))
    stack.enter_context(patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=fake_load,
    ))
    stack.enter_context(patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=fake_compute,
    ))
    stack.enter_context(patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=fake_upsert,
    ))
    return stack


# ===== 15. parse_args: --workers =====


def test_parse_args_workers_default_is_1() -> None:
    """--workers 默认为 1（单进程模式）。"""
    with patch("sys.argv", ["feature_snapshot_backfill", "--start", "2026-01-01"]):
        args = parse_args()
    assert args.workers == 1


def test_parse_args_workers_custom() -> None:
    """--workers 4 启用 multiprocessing 并行模式。"""
    with patch("sys.argv", [
        "feature_snapshot_backfill",
        "--start", "2026-01-01",
        "--workers", "4",
    ]):
        args = parse_args()
    assert args.workers == 4


# ===== 16. _worker_process_instruments: per-instrument commit =====


def test_worker_process_instruments_per_instrument_commit() -> None:
    """_worker_process_instruments: per-date commit 全成功（resume 安全）。

    场景：2 instruments × 2 trade_dates。
    要求：
    - commit 调用 4 次（每 instrument × date 一次，per-date commit 策略）
    - 4 个 snapshot 全部成功
    - failed=0
    """
    inst1_id = uuid.uuid4()
    inst2_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5), date(2026, 1, 6)]
    fake_session = _FakeWorkerSession()

    async def _fake_load(db, instrument_id, **kwargs):
        return (None, None)

    async def _fake_compute(db, instrument_id, trade_date, **kwargs):
        return _make_snapshot(instrument_id, trade_date)

    async def _fake_upsert(db, snapshot):
        pass

    with _patch_worker_deps(fake_session, _fake_load, _fake_compute, _fake_upsert):
        result = _worker_process_instruments(
            instrument_ids=[inst1_id, inst2_id],
            trade_dates=trade_dates,
            db_url="postgresql+psycopg://user:pass@localhost/db",
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            resume=False,
            existing_per_date_str={},
            worker_id=0,
        )

    # per-date commit：2 instruments × 2 dates = 4 commits
    assert fake_session.commits == 4, (
        f"per-date commit 应调用 4 次，实际 {fake_session.commits} 次"
    )
    # 4 snapshots total (2 instruments × 2 dates)
    total_success = sum(stats["success"] for stats in result.values())
    assert total_success == 4
    assert all(stats["failed"] == 0 for stats in result.values())


# ===== 17. _worker_process_instruments: resume 跳过已存在 =====


def test_worker_process_instruments_resume_skips_existing() -> None:
    """_worker_process_instruments: resume 跳过已存在的 (instrument, date) 组合。

    场景：2 instruments × 1 trade_date，inst1 已存在。
    要求：只计算 inst2，inst1 计入 skipped。
    """
    inst1_id = uuid.uuid4()
    inst2_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5)]
    fake_session = _FakeWorkerSession()

    compute_calls: list[tuple[uuid.UUID, date]] = []

    async def _fake_load(db, instrument_id, **kwargs):
        return (None, None)

    async def _fake_compute(db, instrument_id, trade_date, **kwargs):
        compute_calls.append((instrument_id, trade_date))
        return _make_snapshot(instrument_id, trade_date)

    async def _fake_upsert(db, snapshot):
        pass

    existing = {"2026-01-05": {str(inst1_id)}}

    with _patch_worker_deps(fake_session, _fake_load, _fake_compute, _fake_upsert):
        result = _worker_process_instruments(
            instrument_ids=[inst1_id, inst2_id],
            trade_dates=trade_dates,
            db_url="postgresql+psycopg://user:pass@localhost/db",
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            resume=True,
            existing_per_date_str=existing,
            worker_id=0,
        )

    assert len(compute_calls) == 1, (
        f"resume 应跳过 inst1，只计算 inst2，实际调用 {len(compute_calls)} 次"
    )
    assert compute_calls[0] == (inst2_id, date(2026, 1, 5))
    assert result["2026-01-05"]["skipped"] == 1
    assert result["2026-01-05"]["success"] == 1


# ===== 18. _worker_process_instruments: 单 instrument 失败不阻塞其他 =====


def test_worker_process_instruments_single_failure_doesnt_block() -> None:
    """_worker_process_instruments: 单 instrument 失败触发 rollback，不阻塞其他。

    场景：2 instruments × 1 trade_date，inst1 load 失败。
    要求：
    - inst1 失败（failed=1），inst2 成功（success=1）
    - inst1 触发 rollback
    - inst2 仍能正常 commit
    """
    inst1_id = uuid.uuid4()
    inst2_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5)]
    fake_session = _FakeWorkerSession()

    async def _fake_load(db, instrument_id, **kwargs):
        if instrument_id == inst1_id:
            raise ValueError("模拟 inst1 加载失败")
        return (None, None)

    async def _fake_compute(db, instrument_id, trade_date, **kwargs):
        return _make_snapshot(instrument_id, trade_date)

    async def _fake_upsert(db, snapshot):
        pass

    with _patch_worker_deps(fake_session, _fake_load, _fake_compute, _fake_upsert):
        result = _worker_process_instruments(
            instrument_ids=[inst1_id, inst2_id],
            trade_dates=trade_dates,
            db_url="postgresql+psycopg://user:pass@localhost/db",
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            resume=False,
            existing_per_date_str={},
            worker_id=0,
        )

    # inst1 失败（load 抛异常 → rollback），inst2 成功
    assert result["2026-01-05"]["failed"] == 1
    assert result["2026-01-05"]["success"] == 1
    # inst1 触发 rollback
    assert fake_session.rollbacks == 1
    # inst2 仍能 commit
    assert fake_session.commits == 1


# ===== 19. backfill_instrument_first_parallel: 空输入返回 =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_parallel_empty_inputs() -> None:
    """backfill_instrument_first_parallel: 空 trade_dates 或 instruments 返回 0。"""
    fake_db = MagicMock()
    fake_db.commit = AsyncMock()

    # 空 trade_dates
    result = await backfill_instrument_first_parallel(
        fake_db,
        trade_dates=[],
        instruments=[uuid.uuid4()],
        workers=4,
        db_url="postgresql+psycopg://user:pass@localhost/db",
    )
    assert result["total_snapshots"] == 0

    # 空 instruments
    result = await backfill_instrument_first_parallel(
        fake_db,
        trade_dates=[date(2026, 1, 5)],
        instruments=[],
        workers=4,
        db_url="postgresql+psycopg://user:pass@localhost/db",
    )
    assert result["total_snapshots"] == 0


# ===== 20. backfill_instrument_first_parallel: 创建 + finalize run records =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_parallel_creates_and_finalizes_run() -> None:
    """backfill_instrument_first_parallel: 创建 run records + finalize succeeded。

    使用 ThreadPoolExecutor 替代 ProcessPoolExecutor（in-process，可 mock worker）。
    场景：2 instruments × 1 trade_date，全部成功。
    """
    inst1_id = uuid.uuid4()
    inst2_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5)]

    fake_db = MagicMock()
    fake_db.commit = AsyncMock()

    def _fake_worker(instrument_ids, trade_dates, *args, **kwargs):
        return {
            td.isoformat(): {
                "success": len(instrument_ids),
                "failed": 0,
                "skipped": 0,
            }
            for td in trade_dates
        }

    with patch(
        "scripts.feature_snapshot_backfill._worker_process_instruments",
        new=_fake_worker,
    ), patch(
        "concurrent.futures.ProcessPoolExecutor",
        concurrent.futures.ThreadPoolExecutor,
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ) as mock_create, patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ) as mock_finish, patch(
        "scripts.feature_snapshot_backfill._get_succeeded_trade_dates",
        new=AsyncMock(return_value=set()),
    ), patch(
        "scripts.feature_snapshot_backfill.get_existing_instrument_ids",
        new=AsyncMock(return_value=set()),
    ):
        result = await backfill_instrument_first_parallel(
            fake_db,
            trade_dates=trade_dates,
            instruments=[inst1_id, inst2_id],
            workers=2,
            failure_threshold=0.3,
            resume=False,
            db_url="postgresql+psycopg://user:pass@localhost/db",
        )

    # 创建 1 个 run record（每个 trade_date 1 个）
    assert mock_create.await_count == 1
    # finish 为 succeeded（failure_rate=0）
    assert mock_finish.await_count == 1
    finish_kwargs = mock_finish.await_args.kwargs
    assert finish_kwargs["status"] == "succeeded"
    assert finish_kwargs["snapshot_count"] == 2
    assert finish_kwargs["failed_count"] == 0
    assert result["total_snapshots"] == 2


# ===== 21. backfill_instrument_first_parallel: 高失败率标 failed =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_parallel_high_failure_marks_failed() -> None:
    """backfill_instrument_first_parallel: failure_rate > 阈值 → run.status='failed'。

    场景：3 instruments，1 成功 2 失败（failure_rate=0.67 > 0.3）。
    使用 workers=1 确保 3 instruments 在同一 chunk，fake_worker 返回固定 1+2。
    """
    inst1_id = uuid.uuid4()
    inst2_id = uuid.uuid4()
    inst3_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5)]

    fake_db = MagicMock()
    fake_db.commit = AsyncMock()

    def _fake_worker(instrument_ids, trade_dates, *args, **kwargs):
        return {
            td.isoformat(): {"success": 1, "failed": 2, "skipped": 0}
            for td in trade_dates
        }

    with patch(
        "scripts.feature_snapshot_backfill._worker_process_instruments",
        new=_fake_worker,
    ), patch(
        "concurrent.futures.ProcessPoolExecutor",
        concurrent.futures.ThreadPoolExecutor,
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ) as mock_finish, patch(
        "scripts.feature_snapshot_backfill._get_succeeded_trade_dates",
        new=AsyncMock(return_value=set()),
    ), patch(
        "scripts.feature_snapshot_backfill.get_existing_instrument_ids",
        new=AsyncMock(return_value=set()),
    ):
        await backfill_instrument_first_parallel(
            fake_db,
            trade_dates=trade_dates,
            instruments=[inst1_id, inst2_id, inst3_id],
            workers=1,  # 单 chunk 确保 fake_worker 固定 1+2 生效
            failure_threshold=0.3,
            resume=False,
            db_url="postgresql+psycopg://user:pass@localhost/db",
        )

    assert mock_finish.await_count == 1
    finish_kwargs = mock_finish.await_args.kwargs
    assert finish_kwargs["status"] == "failed"
    assert finish_kwargs["snapshot_count"] == 1
    assert finish_kwargs["failed_count"] == 2


# ===== 22. backfill_instrument_first_parallel: scope 传播到 run metadata =====


@pytest.mark.asyncio
async def test_backfill_instrument_first_parallel_propagates_scope() -> None:
    """backfill_instrument_first_parallel: scope='sample' 传播到 create/finish metadata。

    防止小样本验证产生的 run 污染 watchlist SUCCEEDED 状态。
    """
    inst1_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5)]

    fake_db = MagicMock()
    fake_db.commit = AsyncMock()

    def _fake_worker(instrument_ids, trade_dates, *args, **kwargs):
        return {
            td.isoformat(): {
                "success": len(instrument_ids),
                "failed": 0,
                "skipped": 0,
            }
            for td in trade_dates
        }

    with patch(
        "scripts.feature_snapshot_backfill._worker_process_instruments",
        new=_fake_worker,
    ), patch(
        "concurrent.futures.ProcessPoolExecutor",
        concurrent.futures.ThreadPoolExecutor,
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ) as mock_create, patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ) as mock_finish, patch(
        "scripts.feature_snapshot_backfill._get_succeeded_trade_dates",
        new=AsyncMock(return_value=set()),
    ), patch(
        "scripts.feature_snapshot_backfill.get_existing_instrument_ids",
        new=AsyncMock(return_value=set()),
    ):
        await backfill_instrument_first_parallel(
            fake_db,
            trade_dates=trade_dates,
            instruments=[inst1_id],
            workers=2,
            scope="sample",  # 关键：传入 sample scope
            db_url="postgresql+psycopg://user:pass@localhost/db",
        )

    # create_snapshot_run 收到 scope='sample'
    create_kwargs = mock_create.call_args.kwargs
    assert create_kwargs.get("scope") == "sample", (
        f"create_snapshot_run 应收到 scope='sample'，实际: {create_kwargs}"
    )
    # finish_snapshot_run 的 metadata 含 scope='sample'
    finish_kwargs = mock_finish.call_args.kwargs
    finish_metadata = finish_kwargs.get("metadata", {})
    assert finish_metadata.get("scope") == "sample", (
        f"finish_snapshot_run metadata 应含 scope='sample'，实际: {finish_metadata}"
    )


# =============================================================================
# Blocker Fix 测试（multiprocessing 事务与统计安全）
# =============================================================================


# ===== Blocker 1: worker future 异常 → chunk 计入 failed =====


@pytest.mark.asyncio
async def test_backfill_parallel_worker_exception_counts_as_failed() -> None:
    """[Blocker 1] worker future 异常时，该 chunk 的 instruments × trade_dates 全部计 failed。

    场景：1 chunk 含 2 instruments × 1 trade_date，worker 抛 RuntimeError。
    要求：run.status='failed'，failed_count=2，snapshot_count=0。
    """
    inst1_id = uuid.uuid4()
    inst2_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5)]

    fake_db = MagicMock()
    fake_db.commit = AsyncMock()

    def _exploding_worker(*args, **kwargs):
        raise RuntimeError("worker 进程崩溃")

    with patch(
        "scripts.feature_snapshot_backfill._worker_process_instruments",
        new=_exploding_worker,
    ), patch(
        "concurrent.futures.ProcessPoolExecutor",
        concurrent.futures.ThreadPoolExecutor,
    ), patch(
        "scripts.feature_snapshot_backfill.create_snapshot_run",
        new=AsyncMock(),
    ), patch(
        "scripts.feature_snapshot_backfill.finish_snapshot_run",
        new=AsyncMock(),
    ) as mock_finish, patch(
        "scripts.feature_snapshot_backfill._get_succeeded_trade_dates",
        new=AsyncMock(return_value=set()),
    ), patch(
        "scripts.feature_snapshot_backfill.get_existing_instrument_ids",
        new=AsyncMock(return_value=set()),
    ):
        await backfill_instrument_first_parallel(
            fake_db,
            trade_dates=trade_dates,
            instruments=[inst1_id, inst2_id],
            workers=1,  # 单 chunk 确保 2 instruments 都在爆炸 worker 内
            failure_threshold=0.3,
            resume=False,
            db_url="postgresql+psycopg://user:pass@localhost/db",
        )

    assert mock_finish.await_count == 1
    finish_kwargs = mock_finish.await_args.kwargs
    # worker 崩溃 → 2 instruments 全部 failed
    assert finish_kwargs["status"] == "failed", (
        f"worker 异常应标 failed，实际: {finish_kwargs['status']}"
    )
    assert finish_kwargs["failed_count"] == 2, (
        f"2 instruments × 1 date 应计 failed=2，实际: {finish_kwargs['failed_count']}"
    )
    assert finish_kwargs["snapshot_count"] == 0


# ===== Blocker 2: commit 失败 → success 不增加，failed 增加 =====


def test_worker_commit_failure_doesnt_count_as_success() -> None:
    """[Blocker 2] per-date commit 失败时 success 不增加，failed 增加。

    场景：1 instrument × 1 trade_date，upsert 成功但 commit 抛异常。
    要求：success=0，failed=1，rollback 被调用。
    """
    inst1_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5)]
    fake_session = _FakeWorkerSession()

    # 让 commit 抛异常
    async def _failing_commit() -> None:
        fake_session.commits += 1
        raise RuntimeError("commit 失败")

    fake_session.commit = _failing_commit  # type: ignore[method-assign]

    async def _fake_load(db, instrument_id, **kwargs):
        return (None, None)

    async def _fake_compute(db, instrument_id, trade_date, **kwargs):
        return _make_snapshot(instrument_id, trade_date)

    async def _fake_upsert(db, snapshot):
        pass

    with _patch_worker_deps(fake_session, _fake_load, _fake_compute, _fake_upsert):
        result = _worker_process_instruments(
            instrument_ids=[inst1_id],
            trade_dates=trade_dates,
            db_url="postgresql+psycopg://user:pass@localhost/db",
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            resume=False,
            existing_per_date_str={},
            worker_id=0,
        )

    # commit 被尝试但失败
    assert fake_session.commits == 1
    # success 不增加（commit 失败 → DB 无写入）
    assert result["2026-01-05"]["success"] == 0, (
        f"commit 失败时 success 应为 0，实际: {result['2026-01-05']['success']}"
    )
    # failed 增加
    assert result["2026-01-05"]["failed"] == 1
    # rollback 被调用（清理事务）
    assert fake_session.rollbacks == 1


# ===== Blocker 3: upsert 异常 → rollback，后续 date 继续 =====


def test_worker_upsert_exception_rollback_continues() -> None:
    """[Blocker 3] per-date upsert 异常 → rollback，后续 trade_date 仍可成功。

    场景：1 instrument × 2 trade_dates，date1 upsert 抛异常。
    要求：
    - date1 failed=1, success=0（upsert 异常 → rollback）
    - date2 success=1, failed=0（rollback 后继续，per-date 独立事务）
    - rollback 调用 1 次（date1），commit 调用 1 次（date2）
    """
    inst1_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5), date(2026, 1, 6)]
    fake_session = _FakeWorkerSession()

    upsert_calls: list[date] = []

    async def _fake_load(db, instrument_id, **kwargs):
        return (None, None)

    async def _fake_compute(db, instrument_id, trade_date, **kwargs):
        return _make_snapshot(instrument_id, trade_date)

    async def _fake_upsert(db, snapshot):
        upsert_calls.append(snapshot.trade_date)
        if snapshot.trade_date == date(2026, 1, 5):
            raise RuntimeError("第一个 date upsert 失败")

    with _patch_worker_deps(fake_session, _fake_load, _fake_compute, _fake_upsert):
        result = _worker_process_instruments(
            instrument_ids=[inst1_id],
            trade_dates=trade_dates,
            db_url="postgresql+psycopg://user:pass@localhost/db",
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            resume=False,
            existing_per_date_str={},
            worker_id=0,
        )

    # date1 失败（upsert 异常 → rollback）
    assert result["2026-01-05"]["failed"] == 1
    assert result["2026-01-05"]["success"] == 0
    # date2 成功（rollback 后继续，per-date 独立事务）
    assert result["2026-01-06"]["success"] == 1, (
        "date1 异常 rollback 后，date2 应继续成功"
    )
    assert result["2026-01-06"]["failed"] == 0
    # rollback 1 次（date1），commit 1 次（date2）
    assert fake_session.rollbacks == 1
    assert fake_session.commits == 1
    # upsert 被调用 2 次（date1 + date2）
    assert len(upsert_calls) == 2


# ===== Blocker 4: worker pool_size=1, max_overflow=0 =====


def test_worker_pool_config_size_1_overflow_0() -> None:
    """[Blocker 4] worker create_async_engine 使用 pool_size=1, max_overflow=0。

    每个 worker 只需要 1 个 session，避免连接池过大（4 workers × 15 = 60 连接）。
    """
    inst1_id = uuid.uuid4()
    fake_session = _FakeWorkerSession()

    captured_kwargs: dict[str, Any] = {}

    class _FakeEngine:
        async def dispose(self) -> None:
            pass

    def _capture_engine(*args: Any, **kwargs: Any) -> _FakeEngine:
        captured_kwargs.update(kwargs)
        return _FakeEngine()

    async def _fake_load(db, instrument_id, **kwargs):
        return (None, None)

    async def _fake_compute(db, instrument_id, trade_date, **kwargs):
        return _make_snapshot(instrument_id, trade_date)

    async def _fake_upsert(db, snapshot):
        pass

    with patch(
        "sqlalchemy.ext.asyncio.create_async_engine",
        side_effect=_capture_engine,
    ), patch(
        "sqlalchemy.ext.asyncio.async_sessionmaker",
        return_value=lambda *a, **kw: fake_session,
    ), patch(
        "scripts.feature_snapshot_backfill.load_instrument_bars",
        new=_fake_load,
    ), patch(
        "scripts.feature_snapshot_backfill.compute_feature_snapshot_for_date",
        new=_fake_compute,
    ), patch(
        "scripts.feature_snapshot_backfill.upsert_snapshot",
        new=_fake_upsert,
    ):
        _worker_process_instruments(
            instrument_ids=[inst1_id],
            trade_dates=[date(2026, 1, 5)],
            db_url="postgresql+psycopg://user:pass@localhost/db",
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            resume=False,
            existing_per_date_str={},
            worker_id=0,
        )

    assert captured_kwargs.get("pool_size") == 1, (
        f"pool_size 应为 1，实际: {captured_kwargs.get('pool_size')}"
    )
    assert captured_kwargs.get("max_overflow") == 0, (
        f"max_overflow 应为 0，实际: {captured_kwargs.get('max_overflow')}"
    )
    assert captured_kwargs.get("pool_pre_ping") is True


# ===== Blocker 5: workers 参数保护 =====


def test_parse_args_workers_zero_rejected() -> None:
    """[Blocker 5] --workers 0 被 argparse 拒绝（SystemExit）。"""
    with patch("sys.argv", [
        "feature_snapshot_backfill",
        "--start", "2026-01-01",
        "--workers", "0",
    ]):
        with pytest.raises(SystemExit):
            parse_args()


def test_parse_args_workers_negative_rejected() -> None:
    """[Blocker 5] --workers -1 被 argparse 拒绝（SystemExit）。"""
    with patch("sys.argv", [
        "feature_snapshot_backfill",
        "--start", "2026-01-01",
        "--workers", "-1",
    ]):
        with pytest.raises(SystemExit):
            parse_args()


def test_parse_args_workers_cap_to_cpu_count() -> None:
    """[Blocker 5] --workers > os.cpu_count() 时 cap 到 cpu_count 并 warning。

    防止用户误设过大值导致进程调度抖动。
    """
    cpu = os.cpu_count() or 1
    large = cpu + 10
    with patch("sys.argv", [
        "feature_snapshot_backfill",
        "--start", "2026-01-01",
        "--workers", str(large),
    ]):
        args = parse_args()
    assert args.workers <= cpu, (
        f"workers={args.workers} 应被 cap 到 cpu_count={cpu}"
    )


# ===== 回归测试：per-date commit 全成功 =====


def test_worker_per_date_commit_all_succeed() -> None:
    """[回归] per-date commit 全成功：2 instruments × 2 dates = 4 commits。

    验证 per-date commit 策略下的事务边界。
    """
    inst1_id = uuid.uuid4()
    inst2_id = uuid.uuid4()
    trade_dates = [date(2026, 1, 5), date(2026, 1, 6)]
    fake_session = _FakeWorkerSession()

    async def _fake_load(db, instrument_id, **kwargs):
        return (None, None)

    async def _fake_compute(db, instrument_id, trade_date, **kwargs):
        return _make_snapshot(instrument_id, trade_date)

    async def _fake_upsert(db, snapshot):
        pass

    with _patch_worker_deps(fake_session, _fake_load, _fake_compute, _fake_upsert):
        result = _worker_process_instruments(
            instrument_ids=[inst1_id, inst2_id],
            trade_dates=trade_dates,
            db_url="postgresql+psycopg://user:pass@localhost/db",
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            resume=False,
            existing_per_date_str={},
            worker_id=0,
        )

    # per-date commit：2 instruments × 2 dates = 4 commits
    assert fake_session.commits == 4, (
        f"per-date commit 应调用 4 次，实际 {fake_session.commits} 次"
    )
    # 4 snapshots 全部成功
    total_success = sum(stats["success"] for stats in result.values())
    assert total_success == 4
    assert all(stats["failed"] == 0 for stats in result.values())
