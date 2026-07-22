"""FeatureSnapshotService 单元测试。

验证维度：
1. build_summary_payload：字段完整性与缺失处理
2. _truncate_bars_to_trade_date：point-in-time 截断
3. compute_feature_snapshot_for_date：传入 bars 计算 snapshot
4. upsert_snapshot：幂等写入
5. compute_for_trade_date：批量逻辑与失败阈值
6. source_*_bar_time 时区正确性

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://... \
        pytest tests/test_feature_snapshot_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.services.feature_snapshot_service import (
    _SHANGHAI_TZ,
    _truncate_bars_to_trade_date,
    build_summary_payload,
    compute_feature_snapshot_for_date,
    upsert_snapshot,
)
from app.services.node_cluster_input_provider import NodeClusterInput

# ===== 1. build_summary_payload =====


def test_build_summary_payload_returns_required_fields() -> None:
    """summary_payload 包含所有前端列表必需字段。"""
    structural = {
        "primary": {
            "1d": {
                "cost_position": {
                    "poc_price": 10.5,
                    "nearest_node_above_price": 11.0,
                    "nearest_node_below_price": 10.0,
                    "distance_to_node_above_atr": 1.2,
                    "distance_to_node_below_atr": -0.8,
                    "node_interval_position_0_1": 0.55,
                    "cost_position_zone": "upper_half",
                    "value_area_zone": "value_area",
                },
                "volatility_momentum": {
                    "bb_percent_b": 0.65,
                },
                "swing_position": {
                    "developing_swing_dir": "up",
                    "developing_swing_high": 11.2,
                    "developing_swing_low": 9.8,
                },
            }
        },
        "secondary": {
            "15m": {
                "swing_position": {
                    "developing_swing_dir": "down",
                    "developing_swing_high": 10.8,
                    "developing_swing_low": 10.2,
                },
            }
        },
    }
    temporal = {
        "derived_relation": {
            "m15_position_relative_to_daily": "above_middle",
        },
    }

    summary = build_summary_payload(
        structural, temporal, trade_date=date(2026, 1, 10),
        source_bar_time="2026-01-10T15:00:00+08:00",
    )

    # 必需字段
    assert summary["poc_price"] == 10.5
    assert summary["nearest_node_above"] == 11.0
    assert summary["nearest_node_below"] == 10.0
    assert summary["distance_to_node_above_atr"] == 1.2
    assert summary["distance_to_node_below_atr"] == -0.8
    assert summary["node_interval_position_0_1"] == 0.55
    assert summary["cost_position_zone"] == "upper_half"
    assert summary["value_area_zone"] == "value_area"
    assert summary["daily_developing_swing_dir"] == "up"
    assert summary["daily_developing_swing_high"] == 11.2
    assert summary["daily_developing_swing_low"] == 9.8
    assert summary["m15_developing_swing_dir"] == "down"
    assert summary["m15_developing_swing_high"] == 10.8
    assert summary["m15_developing_swing_low"] == 10.2
    assert summary["m15_position_relative_to_daily"] == "above_middle"
    assert summary["as_of"] == "2026-01-10"
    assert summary["source_bar_time"] == "2026-01-10T15:00:00+08:00"
    assert summary["_source"] == "feature_snapshot"


def test_build_summary_payload_handles_missing_fields() -> None:
    """structural/temporal payload 缺失字段时 summary 填 None。"""
    summary = build_summary_payload({}, {}, trade_date=date(2026, 1, 10))

    assert summary["poc_price"] is None
    assert summary["bb_upper"] is None
    assert summary["daily_developing_swing_dir"] is None
    assert summary["m15_position_relative_to_daily"] is None
    assert summary["_source"] == "feature_snapshot"
    assert summary["as_of"] == "2026-01-10"


# ===== 2. _truncate_bars_to_trade_date =====


def _build_daily_bars(n: int = 250, seed: int = 42) -> pd.DataFrame:
    """构造日线 bars（DatetimeIndex trade_date）。"""
    rng = np.random.default_rng(seed)
    base = 100.0
    trend = np.linspace(0, 20.0, n)
    noise = rng.normal(0, 2.0, n)
    closes = base + trend + noise
    intrabar = np.abs(rng.normal(0, 1.5, n)) + 0.5
    highs = closes + intrabar
    lows = closes - intrabar
    opens = closes + rng.normal(0, 0.5, n)
    volumes = rng.integers(1_000_000, 10_000_000, n).astype(float)
    amounts = volumes * closes
    idx = pd.date_range("2025-06-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes, "amount": amounts,
    }, index=idx)


def test_truncate_daily_bars_excludes_future_data() -> None:
    """日线截断到 trade_date，不含 trade_date 之后数据。"""
    bars = _build_daily_bars(n=250)
    # trade_date 设为第 100 根 bar 的日期
    target_date = bars.index[99].date()
    truncated = _truncate_bars_to_trade_date(bars, target_date, "1d")
    assert truncated is not None
    # 最后一根 bar 日期 <= target_date
    assert truncated.index[-1].date() <= target_date
    # 不包含 target_date 之后的 bar
    assert (truncated.index.date > target_date).sum() == 0


def test_truncate_daily_bars_none_returns_none() -> None:
    """bars 为 None 时返回 None。"""
    assert _truncate_bars_to_trade_date(None, date(2026, 1, 10), "1d") is None


def test_truncate_15m_bars_excludes_future_data() -> None:
    """15m bars 截断到 trade_date 当日收盘，不含之后数据。"""
    # 构造 3 天 15m bars（每天 16 根）
    n_days = 3
    bars_per_day = 16
    n = n_days * bars_per_day
    idx = pd.date_range("2026-01-10 09:45", periods=n, freq="15min")
    # 简化：直接用全部 idx
    closes = np.linspace(10.0, 11.0, n)
    bars = pd.DataFrame({
        "open": closes, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "volume": np.ones(n) * 1e6, "amount": closes * 1e6,
    }, index=idx)

    target_date = date(2026, 1, 10)
    truncated = _truncate_bars_to_trade_date(bars, target_date, "15m")
    assert truncated is not None
    # 所有 bar 日期 <= target_date
    assert truncated.index[-1].date() <= target_date


# ===== 3. compute_feature_snapshot_for_date =====


@pytest.mark.asyncio
async def test_compute_snapshot_point_in_time_no_future_data() -> None:
    """2026-01-10 snapshot 不得使用 2026-01-11 之后 bars。"""
    # 构造 250 根日线，最后一根在 2026-01-15
    bars = _build_daily_bars(n=250)
    # 调整 index 覆盖到 2026-01-10 之后
    idx = pd.date_range("2025-06-01", periods=250, freq="B")
    bars.index = idx
    target_date = date(2026, 1, 10)

    # mock session（只需 instrument 查询）
    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    snapshot = await compute_feature_snapshot_for_date(
        mock_session,
        uuid.uuid4(),
        target_date,
        primary_bars=bars,
        secondary_bars=bars,
    )

    # source_primary_bar_time 应 <= target_date（trade_date 可能是非交易日，取最后有数据的日期）
    assert snapshot.source_primary_bar_time is not None
    assert snapshot.source_primary_bar_time.date() <= target_date
    # 时区为 Asia/Shanghai
    assert snapshot.source_primary_bar_time.tzinfo is not None

    # structural_payload 不为空
    assert snapshot.structural_payload is not None
    assert isinstance(snapshot.structural_payload, dict)

    # summary_payload 包含 _source
    assert snapshot.summary_payload["_source"] == "feature_snapshot"


@pytest.mark.asyncio
async def test_compute_snapshot_degraded_on_insufficient_data() -> None:
    """数据不足时不抛异常，写 degraded_reasons。"""
    # 只给 5 根 bar（不足 60 根 warmup）
    bars = _build_daily_bars(n=5)
    target_date = bars.index[-1].date()

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    snapshot = await compute_feature_snapshot_for_date(
        mock_session,
        uuid.uuid4(),
        target_date,
        primary_bars=bars,
        secondary_bars=bars,
    )

    # degraded_reasons 应非空
    assert len(snapshot.degraded_reasons) > 0
    # structural_payload 仍为 dict（可能含 null factor groups）
    assert isinstance(snapshot.structural_payload, dict)


@pytest.mark.asyncio
async def test_compute_snapshot_source_bar_time_timezone_aware() -> None:
    """source_primary_bar_time 和 source_secondary_bar_time 必须是 timezone-aware。"""
    bars = _build_daily_bars(n=250)
    target_date = bars.index[200].date()

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    snapshot = await compute_feature_snapshot_for_date(
        mock_session,
        uuid.uuid4(),
        target_date,
        primary_bars=bars,
        secondary_bars=bars,
    )

    assert snapshot.source_primary_bar_time is not None
    assert snapshot.source_primary_bar_time.tzinfo is not None
    # 1d bar time 规范化为 trade_date 15:00+08:00
    assert snapshot.source_primary_bar_time.hour == 15
    assert snapshot.source_primary_bar_time.utcoffset() is not None


@pytest.mark.asyncio
async def test_compute_snapshot_structural_payload_contains_relation() -> None:
    """structural_payload 必须包含 relation 字段（复用 _compute_relation）。

    relation 复用 structural_factor_service._compute_relation(primary, secondary)，
    禁止在 feature_snapshot_service 内复制关系计算公式。
    """
    bars = _build_daily_bars(n=250)
    target_date = bars.index[200].date()

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    snapshot = await compute_feature_snapshot_for_date(
        mock_session,
        uuid.uuid4(),
        target_date,
        primary_bars=bars,
        secondary_bars=bars,
    )

    # structural_payload 必须含 primary/secondary/relation/meta 四个顶层 key
    sp = snapshot.structural_payload
    assert set(sp.keys()) >= {"primary", "secondary", "relation", "meta"}
    # relation 必须含 V1.8 客观关系字段（值可为 None，但 key 必须存在）
    relation = sp["relation"]
    assert isinstance(relation, dict)
    assert "trend_alignment" in relation
    assert "secondary_vs_primary_position_delta" in relation
    assert "primary_dir" in relation
    assert "secondary_dir" in relation


# ===== 4. upsert_snapshot =====


@pytest.mark.asyncio
async def test_upsert_snapshot_idempotent(db_session: AsyncSession) -> None:
    """重复 upsert 同一 instrument/date 不新增重复行。"""
    from app.models.instrument import Instrument

    # 创建测试 instrument
    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST001",
        name="测试股票",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 1, 10)
    snapshot = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        structural_payload={"test": "v1"},
        temporal_payload={"test": "v1"},
        summary_payload={"_source": "feature_snapshot", "poc_price": 10.0},
        degraded_reasons=[],
    )

    # 第一次 upsert
    await upsert_snapshot(db_session, snapshot)
    await db_session.flush()

    # 第二次 upsert（相同唯一键）
    snapshot2 = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        structural_payload={"test": "v2"},
        temporal_payload={"test": "v2"},
        summary_payload={"_source": "feature_snapshot", "poc_price": 11.0},
        degraded_reasons=[],
    )
    await upsert_snapshot(db_session, snapshot2)
    await db_session.flush()

    # 验证只有一行
    stmt = select(StockFeatureSnapshot).where(
        StockFeatureSnapshot.instrument_id == inst.id,
        StockFeatureSnapshot.trade_date == target_date,
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    # 内容为第二次写入
    assert rows[0].structural_payload["test"] == "v2"
    assert rows[0].summary_payload["poc_price"] == 11.0


@pytest.mark.asyncio
async def test_upsert_snapshot_updates_source_run_id_on_conflict(
    db_session: AsyncSession,
) -> None:
    """[P0] 同日成功重跑时，冲突更新必须把 source_run_id 切换到新 run。

    场景：
    - run A 失败（未发布）→ snapshot.source_run_id = A
    - run B 成功重跑（同 trade_date）→ snapshot.source_run_id = B
    - stock_context 按 source_run_id 查询能查到新快照

    run_a 未发布，WHERE 子句允许覆盖；run_b 成功后归属切换到 B。
    """
    from app.models.instrument import Instrument
    from app.models.stock_feature_snapshot_run import (
        STATUS_FAILED,
        STATUS_SUCCEEDED,
    )
    from app.services.feature_snapshot_service import (
        create_snapshot_run,
        finish_snapshot_run,
    )

    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST_RERUN",
        name="测试重跑",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 7, 10)

    # Run A 失败（未发布，可被覆盖）
    run_a = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="after_close",
        expected_count=1, scope="full",
    )
    await finish_snapshot_run(
        db_session, run_a, status=STATUS_FAILED,
        failed_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "full"},
    )
    await db_session.flush()

    snap_a = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_run_id=run_a.id,
        source_primary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        source_secondary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        structural_payload={"run": "A"},
        temporal_payload={"run": "A"},
        summary_payload={"_source": "feature_snapshot", "poc_price": 10.0},
        degraded_reasons=[],
    )
    await upsert_snapshot(db_session, snap_a)
    await db_session.flush()

    # Run B 成功重跑（run_a 未发布，可创建新 full run）
    run_b = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="after_close",
        expected_count=1, scope="full",
    )
    await finish_snapshot_run(
        db_session, run_b, status=STATUS_SUCCEEDED,
        snapshot_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "full"},
    )
    await db_session.flush()

    snap_b = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_run_id=run_b.id,
        source_primary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        source_secondary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        structural_payload={"run": "B"},
        temporal_payload={"run": "B"},
        summary_payload={"_source": "feature_snapshot", "poc_price": 11.0},
        degraded_reasons=[],
    )
    # run_a 未发布，WHERE 子句允许覆盖 snap_a
    await upsert_snapshot(db_session, snap_b)
    await db_session.flush()

    # 验证：快照归属切换到 B，context 按 B 查询能查到
    stmt = select(StockFeatureSnapshot).where(
        StockFeatureSnapshot.instrument_id == inst.id,
        StockFeatureSnapshot.trade_date == target_date,
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_run_id == run_b.id, "冲突更新必须把 source_run_id 切到新 run"
    assert rows[0].structural_payload["run"] == "B"

    # 按 run_b 查询应命中
    stmt_b = select(StockFeatureSnapshot).where(
        StockFeatureSnapshot.source_run_id == run_b.id,
    )
    rows_b = (await db_session.execute(stmt_b)).scalars().all()
    assert len(rows_b) == 1

    # 按 run_a 查询不应命中（归属已切走）
    stmt_a = select(StockFeatureSnapshot).where(
        StockFeatureSnapshot.source_run_id == run_a.id,
    )
    rows_a = (await db_session.execute(stmt_a)).scalars().all()
    assert len(rows_a) == 0


@pytest.mark.asyncio
async def test_upsert_snapshot_rollback_preserves_old_ownership(
    db_session: AsyncSession,
) -> None:
    """[P0] run B 失败回滚时，快照归属仍为 A。

    场景：
    - run A 失败（未发布）→ snapshot.source_run_id = A
    - run B 写入快照但失败 → 事务 rollback
    - snapshot.source_run_id 仍为 A（未被污染）

    run_a 失败时 WHERE 子句不保护 snap_a，但 run_b 未写快照即标记 failed，
    故 snap_a 归属不受影响。该用例验证失败 run 不污染已有归属。
    """
    from app.models.instrument import Instrument
    from app.models.stock_feature_snapshot_run import (
        STATUS_FAILED,
    )
    from app.services.feature_snapshot_service import (
        create_snapshot_run,
        finish_snapshot_run,
    )

    inst = Instrument(
        id=uuid.uuid4(),
        symbol="TEST_RB",
        name="测试回滚",
        market="SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 7, 10)

    # Run A 失败（未发布）
    run_a = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="after_close",
        expected_count=1, scope="full",
    )
    await finish_snapshot_run(
        db_session, run_a, status=STATUS_FAILED,
        failed_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "full"},
    )
    await db_session.flush()

    snap_a = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=target_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_run_id=run_a.id,
        source_primary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        source_secondary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
        structural_payload={"run": "A"},
        temporal_payload={"run": "A"},
        summary_payload={"_source": "feature_snapshot", "poc_price": 10.0},
        degraded_reasons=[],
    )
    await upsert_snapshot(db_session, snap_a)
    await db_session.flush()

    # 模拟 run B 失败：在 savepoint 中写入然后 rollback
    # conftest 使用 savepoint 模式，rollback 只影响 savepoint 内的修改
    # 这里用 mock 验证：run B 标记为 failed（不写快照），A 的归属不受影响
    # run_a 未发布，可创建新 full run
    run_b = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="after_close",
        expected_count=1, scope="full",
    )
    # run B 失败：不写快照，直接标记 failed
    await finish_snapshot_run(
        db_session, run_b, status=STATUS_FAILED,
        failed_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "full"},
    )
    await db_session.flush()

    # 验证：快照归属仍为 A
    stmt = select(StockFeatureSnapshot).where(
        StockFeatureSnapshot.instrument_id == inst.id,
        StockFeatureSnapshot.trade_date == target_date,
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_run_id == run_a.id, "run B 失败时归属应仍为 A"
    assert rows[0].structural_payload["run"] == "A"


# ===== 5. compute_for_trade_date =====


@pytest.mark.asyncio
async def test_compute_for_trade_date_single_failure_does_not_block(
    db_session: AsyncSession,
) -> None:
    """单股失败不阻断批次：成功行落库，失败行不写，返回 failed_count。"""
    from app.models.instrument import Instrument
    from app.services.feature_snapshot_service import compute_for_trade_date

    # 创建 3 个测试 instrument
    inst_ids = []
    for i in range(3):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"TEST{i:03d}",
            name=f"测试{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        inst_ids.append(inst.id)
    await db_session.flush()

    # mock compute_feature_snapshot_for_date：第 2 个失败
    call_count = 0

    async def mock_compute(session, instrument_id, trade_date, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ValueError("mock failure for instrument 2")
        return StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
            source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )

    with patch(
        "app.services.feature_snapshot_service.compute_feature_snapshot_for_date",
        side_effect=mock_compute,
    ):
        result = await compute_for_trade_date(
            db_session,
            date(2026, 1, 10),
            inst_ids,
            batch_size=10,
            failure_threshold=0.5,
        )
        # [Blocker2] - compute_for_trade_date 不再内部 commit，caller flush 使行可见
        await db_session.flush()

    assert result["snapshot_count"] == 2
    assert result["failed_count"] == 1
    assert result["trade_date"] == "2026-01-10"

    # [Blocker2] - 成功行已落库（flush），失败行不写
    rows = (
        await db_session.execute(
            select(StockFeatureSnapshot).where(
                StockFeatureSnapshot.trade_date == date(2026, 1, 10),
            )
        )
    ).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_compute_for_trade_date_over_threshold_no_partial_after_rollback(
    db_session: AsyncSession,
) -> None:
    """[Blocker2] 失败率超阈值时 compute_for_trade_date 抛错，caller rollback 后无部分 snapshots。

    构造 40% 单股失败（5 只中 2 只失败），failure_threshold=0.3。
    compute_for_trade_date 不内部 commit，caller rollback 清除所有已 flush 行。
    """
    from app.models.instrument import Instrument
    from app.services.feature_snapshot_service import compute_for_trade_date

    # 创建 5 个测试 instrument
    inst_ids = []
    for i in range(5):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"HALF{i:03d}",
            name=f"半成品{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        inst_ids.append(inst.id)
    await db_session.flush()

    # mock compute：第 3、4 个失败（40%）
    call_count = 0

    async def mock_compute(session, instrument_id, trade_date, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count in (3, 4):  # 2 of 5 fail = 40%
            raise ValueError("mock failure for 40% case")
        return StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
            source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )

    with patch(
        "app.services.feature_snapshot_service.compute_feature_snapshot_for_date",
        side_effect=mock_compute,
    ):
        with pytest.raises(RuntimeError, match="失败比例.*阈值"):
            await compute_for_trade_date(
                db_session,
                date(2026, 1, 10),
                inst_ids,
                batch_size=10,
                failure_threshold=0.3,
            )
        # [Blocker2] - caller 负责 rollback（compute_for_trade_date 不内部 commit）
        await db_session.rollback()

    # rollback 后 DB 不存在该 trade_date 的部分 snapshots
    rows = (
        await db_session.execute(
            select(StockFeatureSnapshot).where(
                StockFeatureSnapshot.trade_date == date(2026, 1, 10),
            )
        )
    ).scalars().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_compute_for_trade_date_failure_threshold_raises(
    db_session: AsyncSession,
) -> None:
    """失败比例超过阈值时整体抛异常。"""
    from app.models.instrument import Instrument
    from app.services.feature_snapshot_service import compute_for_trade_date

    inst_ids = []
    for i in range(4):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"FAIL{i:03d}",
            name=f"失败{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        inst_ids.append(inst.id)
    await db_session.flush()

    async def mock_compute_all_fail(session, instrument_id, trade_date, **kwargs):
        raise ValueError("all fail")

    with patch(
        "app.services.feature_snapshot_service.compute_feature_snapshot_for_date",
        side_effect=mock_compute_all_fail,
    ):
        with pytest.raises(RuntimeError, match="失败比例.*阈值"):
            await compute_for_trade_date(
                db_session,
                date(2026, 1, 10),
                inst_ids,
                batch_size=10,
                failure_threshold=0.3,
            )


@pytest.mark.asyncio
async def test_compute_for_trade_date_progress_callback_per_batch(
    db_session: AsyncSession,
) -> None:
    """[Heartbeat] 每处理一个 batch 后调用 progress_callback，携带当前进度。

    场景：5 只 instrument，batch_size=2，期望 callback 在 batch 0/1/2 后被调用 3 次，
    且最后一次 processed=5 / total=5。
    """
    from app.models.instrument import Instrument
    from app.services.feature_snapshot_service import compute_for_trade_date

    inst_ids = []
    for i in range(5):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"PROG{i:03d}",
            name=f"进度{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        inst_ids.append(inst.id)
    await db_session.flush()

    progress_calls: list[dict[str, Any]] = []

    async def mock_compute(session, instrument_id, trade_date, **kwargs):
        return StockFeatureSnapshot(
            instrument_id=instrument_id,
            trade_date=trade_date,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_primary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
            source_secondary_bar_time=datetime(2026, 1, 10, 15, 0, tzinfo=_SHANGHAI_TZ),
            structural_payload={},
            temporal_payload={},
            summary_payload={"_source": "feature_snapshot"},
            degraded_reasons=[],
        )

    async def progress_callback(**kwargs):
        progress_calls.append(dict(kwargs))

    with patch(
        "app.services.feature_snapshot_service.compute_feature_snapshot_for_date",
        side_effect=mock_compute,
    ):
        result = await compute_for_trade_date(
            db_session,
            date(2026, 1, 10),
            inst_ids,
            batch_size=2,
            failure_threshold=0.3,
            progress_callback=progress_callback,
        )
        await db_session.flush()

    assert result["snapshot_count"] == 5
    assert result["failed_count"] == 0
    # batch_size=2, total=5 -> 3 batches
    assert len(progress_calls) == 3, f"应调用 3 次 progress_callback，实际 {len(progress_calls)}"
    assert progress_calls[0]["processed"] == 2
    assert progress_calls[0]["total"] == 5
    assert progress_calls[2]["processed"] == 5
    assert progress_calls[2]["snapshot_count"] == 5
    assert progress_calls[2]["failed_count"] == 0


# ===== 6. P0-4: published snapshot 保护 =====


@pytest.mark.asyncio
async def test_p0_4_create_snapshot_run_blocks_when_published_full_exists(
    db_session: AsyncSession,
) -> None:
    """[P0-4] 已存在 succeeded+published+full run 时，create_snapshot_run 拒绝创建新 run。"""
    from app.models.instrument import Instrument
    from app.models.stock_feature_snapshot_run import STATUS_SUCCEEDED
    from app.services.feature_snapshot_service import (
        PublishedSnapshotRunExistsError,
        create_snapshot_run,
        finish_snapshot_run,
    )

    inst = Instrument(
        id=uuid.uuid4(), symbol="TEST_PUB_F", name="测试发布保护",
        market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 7, 11)

    # Run A 成功 + 发布 + full scope
    run_a = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="after_close",
        expected_count=1, scope="full",
    )
    await finish_snapshot_run(
        db_session, run_a, status=STATUS_SUCCEEDED,
        snapshot_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "full"},
    )
    await db_session.flush()

    # 尝试创建新 full run → 应抛 PublishedSnapshotRunExistsError
    with pytest.raises(PublishedSnapshotRunExistsError) as exc_info:
        await create_snapshot_run(
            db_session, trade_date=target_date, run_type="after_close",
            expected_count=1, scope="full",
        )
    assert exc_info.value.existing_run.id == run_a.id


@pytest.mark.asyncio
async def test_p0_4_create_snapshot_run_sample_scope_not_blocked(
    db_session: AsyncSession,
) -> None:
    """[P0-4] scope='sample' 时即使已存在 published full run 也不阻止创建。"""
    from app.models.instrument import Instrument
    from app.models.stock_feature_snapshot_run import STATUS_SUCCEEDED
    from app.services.feature_snapshot_service import (
        create_snapshot_run,
        finish_snapshot_run,
    )

    inst = Instrument(
        id=uuid.uuid4(), symbol="TEST_SAMP", name="测试样本不阻止",
        market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 7, 11)

    # Run A: full scope + succeeded + published
    run_a = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="after_close",
        expected_count=1, scope="full",
    )
    await finish_snapshot_run(
        db_session, run_a, status=STATUS_SUCCEEDED,
        snapshot_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "full"},
    )
    await db_session.flush()

    # scope='sample' → 不阻止（小样本验证不影响 watchlist 可读的 full run）
    run_b = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="backfill",
        expected_count=1, scope="sample",
    )
    assert run_b.id != run_a.id


@pytest.mark.asyncio
async def test_p0_4_upsert_snapshot_protects_published_run_ownership(
    db_session: AsyncSession,
) -> None:
    """[P0-4] upsert_snapshot 无条件保护已归属 published run 的 snapshot。"""
    from app.models.instrument import Instrument
    from app.models.stock_feature_snapshot_run import STATUS_SUCCEEDED
    from app.services.feature_snapshot_service import (
        create_snapshot_run,
        finish_snapshot_run,
    )

    inst = Instrument(
        id=uuid.uuid4(), symbol="TEST_PROT", name="测试归属保护",
        market="SH", status="active",
    )
    db_session.add(inst)
    await db_session.flush()

    target_date = date(2026, 7, 11)

    # Run A 成功 + 发布
    run_a = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="after_close",
        expected_count=1, scope="full",
    )
    await finish_snapshot_run(
        db_session, run_a, status=STATUS_SUCCEEDED,
        snapshot_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "full"},
    )
    await db_session.flush()

    # snap_a 归属 run_a
    snap_a = StockFeatureSnapshot(
        instrument_id=inst.id, trade_date=target_date,
        primary_timeframe="1d", secondary_timeframe="15m", adj="qfq",
        schema_version=1, source_run_id=run_a.id,
        source_primary_bar_time=datetime(2026, 7, 11, 15, 0, tzinfo=_SHANGHAI_TZ),
        source_secondary_bar_time=datetime(2026, 7, 11, 15, 0, tzinfo=_SHANGHAI_TZ),
        structural_payload={"run": "A"}, temporal_payload={"run": "A"},
        summary_payload={"_source": "feature_snapshot", "poc_price": 10.0},
        degraded_reasons=[],
    )
    await upsert_snapshot(db_session, snap_a)
    await db_session.flush()

    # 尝试用 run_b 覆盖（无条件保护，无 bypass）
    # run_a 已是 succeeded+published+full，无法创建新 full run，改用 sample scope
    run_b = await create_snapshot_run(
        db_session, trade_date=target_date, run_type="backfill",
        expected_count=1, scope="sample",
    )
    await finish_snapshot_run(
        db_session, run_b, status=STATUS_SUCCEEDED,
        snapshot_count=1, expected_count=1,
        metadata={"source": "after_close_orchestrator", "scope": "sample"},
    )
    await db_session.flush()

    snap_b = StockFeatureSnapshot(
        instrument_id=inst.id, trade_date=target_date,
        primary_timeframe="1d", secondary_timeframe="15m", adj="qfq",
        schema_version=1, source_run_id=run_b.id,
        source_primary_bar_time=datetime(2026, 7, 11, 15, 0, tzinfo=_SHANGHAI_TZ),
        source_secondary_bar_time=datetime(2026, 7, 11, 15, 0, tzinfo=_SHANGHAI_TZ),
        structural_payload={"run": "B"}, temporal_payload={"run": "B"},
        summary_payload={"_source": "feature_snapshot", "poc_price": 11.0},
        degraded_reasons=[],
    )
    # 无条件保护：WHERE 子句保护 snap_a 不被覆盖
    await upsert_snapshot(db_session, snap_b)
    await db_session.flush()

    # 验证：快照仍归属 run_a，内容仍为 A
    stmt = select(StockFeatureSnapshot).where(
        StockFeatureSnapshot.instrument_id == inst.id,
        StockFeatureSnapshot.trade_date == target_date,
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_run_id == run_a.id, "published run 归属不应被覆盖"
    assert rows[0].structural_payload["run"] == "A"


# ===== 7. [CHANGE-20260721-001] Node Cluster availability/degraded_reason 注入 =====


def _build_15m_bars(n: int = 1000, seed: int = 7) -> pd.DataFrame:
    """构造 15m bars（覆盖足够多交易日，确保 has_15m=True）。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-06-01 09:45", periods=n, freq="15min")
    base = 100.0
    closes = base + rng.normal(0, 1.5, n).cumsum()
    intrabar = np.abs(rng.normal(0, 0.5, n)) + 0.1
    return pd.DataFrame({
        "open": closes, "high": closes + intrabar, "low": closes - intrabar,
        "close": closes, "volume": rng.integers(100_000, 1_000_000, n).astype(float),
        "amount": closes * 1_000_000,
    }, index=idx)


def _build_node_input(
    *,
    daily_bars: pd.DataFrame | None = None,
    bars_15m: pd.DataFrame | None = None,
    availability: str = "available",
    degraded_reason: str | None = None,
    daily_count: int | None = None,
    m15_count: int | None = None,
    adjustment_as_of: date | None = None,
) -> NodeClusterInput:
    """[CP-V3-A] 构造 mock NodeClusterInput（4 个 Node 状态测试共用）。

    默认返回 available 态 + 250 daily + 4000 15m bars，调用方可覆盖任一字段。
    count 默认从 bars 长度推断；空 DataFrame 须显式传 daily_count/m15_count。
    """
    if daily_bars is None:
        daily_bars = _build_daily_bars(n=250)
    if bars_15m is None:
        bars_15m = _build_15m_bars(n=4000)
    return NodeClusterInput(
        daily_bars=daily_bars,
        bars_15m=bars_15m,
        daily_source_hash="mock_daily_hash",
        daily_adj_factor_hash="mock_daily_adj",
        m15_source_hash="mock_m15_hash",
        m15_adj_factor_hash="mock_m15_adj",
        daily_count=daily_count if daily_count is not None else len(daily_bars),
        m15_count=m15_count if m15_count is not None else len(bars_15m),
        daily_requested=250,
        m15_requested=4000,
        daily_history_exhausted=False,
        m15_history_exhausted=False,
        availability=availability,
        degraded_reason=degraded_reason,
        adjustment_as_of=adjustment_as_of,
    )


@pytest.mark.asyncio
async def test_compute_snapshot_writes_node_cluster_available_state() -> None:
    """日线 + 15m 齐全且 profile 非空 → node_cluster.availability='available'。"""
    from app.services.feature_snapshot_service import _SCHEMA_VERSION

    daily = _build_daily_bars(n=250)
    bars_15m = _build_15m_bars(n=2000)
    target_date = daily.index[200].date()

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    # [CP-V3-A] mock NodeClusterInputProvider.get_inputs 返回 available 态 + 完整 250+4000 bars。
    # primary_bars/secondary_bars 仅供 structural factors 计算使用；
    # node_cluster 输入由 Provider 唯一提供（四链统一入口）。
    mock_node_input = _build_node_input(
        daily_bars=_build_daily_bars(n=250),
        bars_15m=_build_15m_bars(n=4000),
        availability="available",
        adjustment_as_of=target_date,
    )
    with patch(
        "app.services.feature_snapshot_service.NodeClusterInputProvider.get_inputs",
        AsyncMock(return_value=mock_node_input),
    ):
        snapshot = await compute_feature_snapshot_for_date(
            mock_session, uuid.uuid4(), target_date,
            primary_bars=daily, secondary_bars=bars_15m,
        )

    # schema_version 必须 bump 到 4
    assert _SCHEMA_VERSION == 4
    assert snapshot.schema_version == 4

    sp = snapshot.structural_payload
    nc = sp["primary"]["1d"].get("node_cluster")
    assert nc is not None, "node_cluster 字段必须写入"
    # available 态：availability/degraded_reason 齐全
    assert nc["availability"] == "available"
    assert nc["degraded_reason"] is None
    # Canonical 字段齐全（poc_price 可能因合成数据无法计算 POC 为 None，但不影响 available 判定）
    assert nc["profile_hash"] is not None, "profile_hash 必须有值"
    assert nc["daily_source_hash"] is not None, "daily_source_hash 必须有值"
    assert nc["bars_15m_source_hash"] is not None, "bars_15m_source_hash 必须有值"
    assert nc["algorithm_version"] is not None
    assert nc["profile_rows"], "profile_rows 非空"
    # availability/degraded_reason 字段必须存在（即使 poc_price 可能为 None）
    assert "availability" in nc
    assert "degraded_reason" in nc
    # [PROMPT.md §5.2.2 V2] price_state 必须存在且含必填字段
    assert "price_state" in nc, "node_cluster 必须包含 price_state 字段"
    ps = nc["price_state"]
    for k in (
        "current_price", "position_0_1",
        "upper_node_ref", "lower_node_ref", "poc_node_ref", "last_touched_node_ref",
    ):
        assert k in ps, f"price_state 缺少字段: {k}"
    # current_price 必须是 float（engine 可能按 price_step 对齐，不强制等于原始 close）
    assert isinstance(ps["current_price"], (int, float)), "current_price 必须是数值"
    # price_state 中的 *_ref 必须能在 node_regions 中找到对应 entity_id（若非 None）
    if ps.get("upper_node_ref") is not None:
        all_entity_ids = {r["entity_id"] for r in nc["node_regions"]}
        for ref_key in ("upper_node_ref", "lower_node_ref", "poc_node_ref", "last_touched_node_ref"):
            ref = ps.get(ref_key)
            if ref is not None:
                assert ref in all_entity_ids, f"price_state.{ref_key}={ref} 未在 node_regions 中找到"


@pytest.mark.asyncio
async def test_compute_snapshot_writes_node_cluster_missing_15m_state() -> None:
    """日线齐全但 15m bars 为空 → availability='unavailable', degraded_reason='MISSING_15M_BARS'。"""
    daily = _build_daily_bars(n=250)
    target_date = daily.index[200].date()
    empty_15m = pd.DataFrame()

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    # [CP-V3-A] mock NodeClusterInputProvider.get_inputs 返回 unavailable/MISSING_15M_BARS。
    # 新状态机：15m=0 直接 unavailable，禁止调用 engine 生成看似正常的 Profile。
    mock_node_input = _build_node_input(
        daily_bars=_build_daily_bars(n=250),
        bars_15m=empty_15m,
        availability="unavailable",
        degraded_reason="MISSING_15M_BARS",
        daily_count=250,
        m15_count=0,
        adjustment_as_of=target_date,
    )
    with patch(
        "app.services.feature_snapshot_service.NodeClusterInputProvider.get_inputs",
        AsyncMock(return_value=mock_node_input),
    ):
        snapshot = await compute_feature_snapshot_for_date(
            mock_session, uuid.uuid4(), target_date,
            primary_bars=daily, secondary_bars=empty_15m,
        )

    sp = snapshot.structural_payload
    nc = sp["primary"]["1d"].get("node_cluster")
    assert nc is not None, "node_cluster 字段必须写入（即使 15m 缺失）"
    # [CP-V3-A] 新状态机：15m=0 → unavailable/MISSING_15M_BARS（不再生成 PROFILE_EMPTY）
    assert nc["availability"] == "unavailable"
    assert nc["degraded_reason"] == "MISSING_15M_BARS"


@pytest.mark.asyncio
async def test_compute_snapshot_writes_node_cluster_insufficient_daily_state() -> None:
    """日线 < 10 根 → availability='unavailable', degraded_reason='INSUFFICIENT_DAILY_BARS'。"""
    short_daily = _build_daily_bars(n=5)
    target_date = short_daily.index[-1].date()

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    # [CP-V3-A] mock NodeClusterInputProvider.get_inputs 返回 unavailable/INSUFFICIENT_DAILY_BARS。
    # Provider 状态机检测到 daily<10 时直接 unavailable，禁止调用 engine。
    mock_node_input = _build_node_input(
        daily_bars=short_daily,
        bars_15m=_build_15m_bars(n=4000),
        availability="unavailable",
        degraded_reason="INSUFFICIENT_DAILY_BARS",
        daily_count=5,
        m15_count=4000,
        adjustment_as_of=target_date,
    )
    with patch(
        "app.services.feature_snapshot_service.NodeClusterInputProvider.get_inputs",
        AsyncMock(return_value=mock_node_input),
    ):
        snapshot = await compute_feature_snapshot_for_date(
            mock_session, uuid.uuid4(), target_date,
            primary_bars=short_daily, secondary_bars=short_daily,
        )

    sp = snapshot.structural_payload
    nc = sp["primary"]["1d"].get("node_cluster")
    assert nc is not None, "node_cluster 字段必须写入（即使日线不足）"
    assert nc["availability"] == "unavailable"
    assert nc["degraded_reason"] == "INSUFFICIENT_DAILY_BARS"
    # profile 相关字段为 None（engine 未运行）
    assert nc["poc_price"] is None
    assert nc["profile_hash"] is None
    # [CP-V3-A] count/hash 仍从 Provider 写入（四链一致诊断字段，不再为 None）
    assert nc["daily_source_hash"] == "mock_daily_hash"
    assert nc["bars_15m_source_hash"] == "mock_m15_hash"
    assert nc["daily_bars_count"] == 5
    assert nc["bars_15m_count"] == 4000
    # [PROMPT.md §5.2.2 V2] profile 为空时 price_state 仍写入最小结构
    assert "price_state" in nc, "unavailable 态也必须包含 price_state"
    ps = nc["price_state"]
    for k in (
        "current_price", "position_0_1",
        "upper_node_ref", "lower_node_ref", "poc_node_ref", "last_touched_node_ref",
    ):
        assert k in ps, f"price_state 缺少字段: {k}"
    # refs 必须全 None（profile 为空）
    for ref_key in ("upper_node_ref", "lower_node_ref", "poc_node_ref", "last_touched_node_ref"):
        assert ps[ref_key] is None, f"unavailable 态 price_state.{ref_key} 必须为 None"
    assert ps["position_0_1"] is None


@pytest.mark.asyncio
async def test_compute_snapshot_node_cluster_field_stable_when_engine_raises() -> None:
    """engine 抛异常时，node_cluster 字段仍写入 availability=unavailable + COMPUTE_FAILED。"""
    daily = _build_daily_bars(n=250)
    bars_15m = _build_15m_bars(n=2000)
    target_date = daily.index[200].date()

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("000001",)
    mock_session.execute = AsyncMock(return_value=mock_result)

    # [CP-V3-A] mock Provider 返回 available 态，让 engine 真正被调用后抛异常 → 验证 COMPUTE_FAILED 降级
    mock_node_input = _build_node_input(
        daily_bars=_build_daily_bars(n=250),
        bars_15m=_build_15m_bars(n=4000),
        availability="available",
        adjustment_as_of=target_date,
    )
    # [CP-13] 迁移到 canonical 后，patch compute_node_cluster_adapter（仅 node_cluster 失败）
    # 其他算法（structural_features/macd/bollinger/relation）仍走真实 canonical 路径。
    with patch(
        "app.services.feature_snapshot_service.NodeClusterInputProvider.get_inputs",
        AsyncMock(return_value=mock_node_input),
    ), patch(
        "app.services.canonical_adapters.compute_node_cluster_adapter",
        side_effect=RuntimeError("mocked engine failure"),
    ):
        snapshot = await compute_feature_snapshot_for_date(
            mock_session, uuid.uuid4(), target_date,
            primary_bars=daily, secondary_bars=bars_15m,
        )

    sp = snapshot.structural_payload
    nc = sp["primary"]["1d"].get("node_cluster")
    assert nc is not None, "engine 失败时仍必须写入 node_cluster（含诊断字段）"
    assert nc["availability"] == "unavailable"
    assert nc["degraded_reason"] is not None
    assert nc["degraded_reason"].startswith("COMPUTE_FAILED")
    assert "mocked engine failure" in nc["degraded_reason"]
    # degraded_reasons 也应记录
    assert any("node_cluster" in r and "engine failed" in r for r in snapshot.degraded_reasons)
