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

[instrument-first 事务边界]：
- backfill_instrument_first 不内部 commit，由 main 控制
- 失败比例超阈值的 trade_date 标 run.status='failed'（不抛 RuntimeError）
- 单股失败不阻塞其他股票
- watchlist 只读取 run.status='succeeded' 的 snapshot（Phase 5 run gate）

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://... \
        pytest tests/test_feature_snapshot_backfill.py -v
"""

from __future__ import annotations

import argparse
import uuid
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from scripts.feature_snapshot_backfill import (
    _resolve_run_scope,
    backfill_instrument_first,
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
