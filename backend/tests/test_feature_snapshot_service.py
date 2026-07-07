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


# ===== 5. compute_for_trade_date =====


@pytest.mark.asyncio
async def test_compute_for_trade_date_single_failure_does_not_block(
    db_session: AsyncSession,
) -> None:
    """单股失败不阻断批次。"""
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
            commit_every=100,
            failure_threshold=0.5,
        )

    assert result["snapshot_count"] == 2
    assert result["failed_count"] == 1
    assert result["trade_date"] == "2026-01-10"


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
                commit_every=100,
                failure_threshold=0.3,
            )
