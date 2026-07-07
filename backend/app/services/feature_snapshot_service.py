"""FeatureSnapshotService - 盘后特征快照计算与持久化服务。

核心功能：
1. compute_feature_snapshot_for_date: 为指定 instrument + trade_date 计算 point-in-time 特征快照。
2. upsert_snapshot: 按唯一键幂等写入。
3. compute_for_trade_date: 批量计算多个 instrument 的快照。
4. build_summary_payload: 从完整 payload 抽取前端列表用摘要。

设计原则：
- 复用 structural_factor_service._compute_all_factors_for_bars 和
  temporal_feature_service._compute_daily_context / _compute_m15_response / _compute_derived_relation，
  不复制 DSA/BB/swing/temporal 数学公式。
- point-in-time：1d bars 只用 <= trade_date，15m bars 只用 <= trade_date 当日。
- 单股失败写 degraded_reasons，不抛全局失败。
- 不建 EAV 表，不给 full payload 加 GIN 索引。

用法：
    from app.services.feature_snapshot_service import compute_feature_snapshot_for_date
    snapshot = await compute_feature_snapshot_for_date(
        session, instrument_id, trade_date=date(2026, 1, 10)
    )

模块自测：
    python -m app.services.feature_snapshot_service
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.services.structural_factor_service import _compute_all_factors_for_bars
from app.services.temporal_feature_service import (
    _compute_daily_context,
    _compute_derived_relation,
    _compute_m15_response,
)
from app.strategy_assets.algorithms.features.bollinger_features_plotly import bollinger

logger = logging.getLogger(__name__)

# 常量
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_SCHEMA_VERSION = 1
_PRIMARY_LOOKBACK = 500  # 日线回看天数（与 structural_factor_service 对齐）
_SECONDARY_LOOKBACK = 500  # 15m 回看天数
_BB_WIN = 20
_BB_K = 2.0


# =============================================================================
# 纯函数：point-in-time 截断
# =============================================================================


def _truncate_bars_to_trade_date(
    bars: pd.DataFrame | None,
    trade_date: date,
    timeframe: str,
) -> pd.DataFrame | None:
    """将 bars 截断到 <= trade_date，保证 point-in-time。

    对 1d 和 15m 均按 index.date <= trade_date 截断。
    截断后为空返回 None。

    Args:
        bars: K 线 DataFrame，index 为 DatetimeIndex
        trade_date: 截止交易日
        timeframe: 时间周期（1d / 15m）

    Returns:
        截断后的 DataFrame 或 None
    """
    if bars is None or bars.empty:
        return None
    mask = bars.index.date <= trade_date
    truncated = bars[mask]
    if truncated.empty:
        return None
    return truncated


# =============================================================================
# 纯函数：summary_payload 构建
# =============================================================================


def _safe_get(d: dict, *keys, default: Any = None) -> Any:
    """安全嵌套取值，任一层缺失返回 default。"""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def build_summary_payload(
    structural_payload: dict[str, Any],
    temporal_payload: dict[str, Any],
    trade_date: date,
    source_bar_time: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从 structural/temporal payload 抽取前端列表用摘要。

    从 structural_payload["primary"]["1d"] 提取日线因子，
    从 structural_payload["secondary"]["15m"] 提取 15m 因子，
    从 temporal_payload["derived_relation"] 提取派生关系。

    Args:
        structural_payload: compute_structural_factors 的完整输出
        temporal_payload: compute_temporal_features 的完整输出
        trade_date: 业务交易日
        source_bar_time: 数据源截止时间（ISO 字符串）
        extra: 额外字段（current_price, change_pct, bb_upper/mid/lower 等）

    Returns:
        前端列表用摘要 dict，包含 _source='feature_snapshot'
    """
    extra = extra or {}
    primary_1d = _safe_get(structural_payload, "primary", "1d", default={})
    secondary_15m = _safe_get(structural_payload, "secondary", "15m", default={})
    cost_pos = primary_1d.get("cost_position") or {}
    swing_primary = primary_1d.get("swing_position") or {}
    swing_secondary = secondary_15m.get("swing_position") or {}
    derived = temporal_payload.get("derived_relation") or {}

    return {
        # 额外字段（来自 bars 最后一根 bar）
        "current_price": extra.get("current_price"),
        "change_pct": extra.get("change_pct"),
        "bb_upper": extra.get("bb_upper"),
        "bb_mid": extra.get("bb_mid"),
        "bb_lower": extra.get("bb_lower"),
        # 成本/节点
        "poc_price": cost_pos.get("poc_price"),
        "nearest_node_above": cost_pos.get("nearest_node_above_price"),
        "nearest_node_below": cost_pos.get("nearest_node_below_price"),
        "distance_to_node_above_atr": cost_pos.get("distance_to_node_above_atr"),
        "distance_to_node_below_atr": cost_pos.get("distance_to_node_below_atr"),
        "node_interval_position_0_1": cost_pos.get("node_interval_position_0_1"),
        "cost_position_zone": cost_pos.get("cost_position_zone"),
        "value_area_zone": cost_pos.get("value_area_zone"),
        # 日线 developing swing
        "daily_developing_swing_dir": swing_primary.get("developing_swing_dir"),
        "daily_developing_swing_high": swing_primary.get("developing_swing_high"),
        "daily_developing_swing_low": swing_primary.get("developing_swing_low"),
        # 15m developing swing
        "m15_developing_swing_dir": swing_secondary.get("developing_swing_dir"),
        "m15_developing_swing_high": swing_secondary.get("developing_swing_high"),
        "m15_developing_swing_low": swing_secondary.get("developing_swing_low"),
        # 派生关系
        "m15_position_relative_to_daily": derived.get("m15_position_relative_to_daily"),
        # 元信息
        "as_of": trade_date.isoformat(),
        "source_bar_time": source_bar_time,
        "_source": "feature_snapshot",
    }


# =============================================================================
# 核心：计算单股单日 snapshot
# =============================================================================


async def compute_feature_snapshot_for_date(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    trade_date: date,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
    *,
    primary_bars: pd.DataFrame | None = None,
    secondary_bars: pd.DataFrame | None = None,
) -> StockFeatureSnapshot:
    """为指定 instrument + trade_date 计算 point-in-time 特征快照。

    内部复用现有算法，不复制公式：
    - structural_factor_service._compute_all_factors_for_bars
    - temporal_feature_service._compute_daily_context / _compute_m15_response / _compute_derived_relation
    - bollinger_features_plotly.bollinger（BB 绝对值）

    point-in-time：
    - 1d bars 只用 <= trade_date
    - 15m bars 只用 <= trade_date 当日
    - 禁止使用 trade_date 之后数据

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        trade_date: 业务交易日
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 副周期（默认 15m）
        adj: 复权方式（默认 qfq）
        primary_bars: 预加载的日线 bars（可选，不传则从 DB 获取）
        secondary_bars: 预加载的 15m bars（可选，不传则从 DB 获取）

    Returns:
        StockFeatureSnapshot ORM 对象（未写入 DB）
    """
    degraded_reasons: list[str] = []
    warmup_notes: list[str] = []

    # 获取 K 线（如果未预加载）
    if primary_bars is None:
        primary_bars = await _fetch_bars_from_db(
            session, instrument_id, primary_timeframe, adj, trade_date,
        )
    if secondary_bars is None:
        secondary_bars = await _fetch_bars_from_db(
            session, instrument_id, secondary_timeframe, adj, trade_date,
        )

    # point-in-time 截断
    df_1d = _truncate_bars_to_trade_date(primary_bars, trade_date, primary_timeframe)
    df_15m = _truncate_bars_to_trade_date(secondary_bars, trade_date, secondary_timeframe)

    # 数据不足检查（与 _fetch_bars 的 60 根 warmup 对齐）
    if df_1d is None:
        degraded_reasons.append(f"{primary_timeframe}: no bars <= {trade_date}")
    elif len(df_1d) < 60:
        degraded_reasons.append(
            f"{primary_timeframe}: insufficient bars ({len(df_1d)} < 60)"
        )
    if df_15m is None:
        degraded_reasons.append(f"{secondary_timeframe}: no bars <= {trade_date}")
    elif len(df_15m) < 60:
        degraded_reasons.append(
            f"{secondary_timeframe}: insufficient bars ({len(df_15m)} < 60)"
        )

    # 计算 structural factors
    primary_factors = _compute_all_factors_for_bars(
        df_1d, primary_timeframe, degraded_reasons, warmup_notes
    )
    secondary_factors = _compute_all_factors_for_bars(
        df_15m, secondary_timeframe, degraded_reasons, warmup_notes
    )

    # 计算 temporal features（复用内部函数）
    daily_context = _compute_daily_context(
        primary_factors, df_1d, degraded_reasons, warmup_notes
    )
    m15_response = _compute_m15_response(
        secondary_factors, df_15m, degraded_reasons, warmup_notes
    )
    derived_relation = _compute_derived_relation(
        daily_context, m15_response, degraded_reasons
    )

    # 构造 structural_payload（与 compute_structural_factors 输出格式对齐）
    structural_payload: dict[str, Any] = {
        "primary": {primary_timeframe: primary_factors},
        "secondary": {secondary_timeframe: secondary_factors},
        "meta": {
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }

    # 构造 temporal_payload
    temporal_payload: dict[str, Any] = {
        "daily_context": daily_context,
        "m15_response": m15_response,
        "derived_relation": derived_relation,
        "meta": {
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }

    # 提取额外字段（current_price, change_pct, BB 绝对值）
    extra = _extract_extra_fields(df_1d)

    # source_bar_time
    source_primary = _normalize_primary_bar_time(df_1d, trade_date)
    source_secondary = _normalize_secondary_bar_time(df_15m)
    source_bar_time_str = (
        source_secondary.isoformat() if source_secondary
        else (source_primary.isoformat() if source_primary else None)
    )

    # 构造 summary_payload
    summary_payload = build_summary_payload(
        structural_payload, temporal_payload, trade_date,
        source_bar_time=source_bar_time_str, extra=extra,
    )

    return StockFeatureSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date,
        primary_timeframe=primary_timeframe,
        secondary_timeframe=secondary_timeframe,
        adj=adj,
        schema_version=_SCHEMA_VERSION,
        source_primary_bar_time=source_primary,
        source_secondary_bar_time=source_secondary,
        structural_payload=structural_payload,
        temporal_payload=temporal_payload,
        summary_payload=summary_payload,
        degraded_reasons=degraded_reasons,
    )


async def _fetch_bars_from_db(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    trade_date: date,
) -> pd.DataFrame | None:
    """从 DB 获取 K 线数据（通过 MarketDataAggregationService）。

    使用 include_realtime=False 只获取已完成 bar。
    失败时返回 None 并由调用方写入 degraded_reasons。
    """
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    try:
        service = MarketDataAggregationService()
        result = await service.get_bars(
            session,
            instrument_id,
            timeframe=timeframe,
            adj=adj,
            include_realtime=False,
        )
        bars = result.bars
        if bars is None or bars.empty:
            return None
        return bars
    except Exception as exc:
        logger.warning(
            "get_bars 失败 instrument_id=%s timeframe=%s: %s",
            instrument_id, timeframe, exc,
        )
        return None


def _extract_extra_fields(df_1d: pd.DataFrame | None) -> dict[str, Any]:
    """从日线 bars 最后一根提取 current_price, change_pct, BB 绝对值。

    BB 使用 bollinger(bars, 20, 2.0) 计算，与 structural_factor_service 一致。
    """
    extra: dict[str, Any] = {
        "current_price": None,
        "change_pct": None,
        "bb_upper": None,
        "bb_mid": None,
        "bb_lower": None,
    }
    if df_1d is None or df_1d.empty or len(df_1d) < 2:
        return extra

    closes = df_1d["close"].to_numpy(dtype=float)
    current_price = float(closes[-1])
    prev_close = float(closes[-2])
    extra["current_price"] = current_price
    if prev_close > 0:
        extra["change_pct"] = round(
            (current_price - prev_close) / prev_close * 100, 4
        )

    # BB 绝对值（需要 >= 20 根 bar）
    if len(df_1d) >= _BB_WIN + 1:
        try:
            mid, upper, lower = bollinger(df_1d, _BB_WIN, _BB_K)
            extra["bb_upper"] = float(upper.iloc[-1]) if pd.notna(upper.iloc[-1]) else None
            extra["bb_mid"] = float(mid.iloc[-1]) if pd.notna(mid.iloc[-1]) else None
            extra["bb_lower"] = float(lower.iloc[-1]) if pd.notna(lower.iloc[-1]) else None
        except Exception:
            pass

    return extra


def _normalize_primary_bar_time(
    df_1d: pd.DataFrame | None,
    trade_date: date,
) -> datetime | None:
    """将 1d 最后一根 bar 的日期规范化为 trade_date 15:00+08:00。

    规范化规则：
    - 如果 df_1d 有数据，取最后一根 bar 的实际日期。
    - 将该日期转换为 Asia/Shanghai 15:00:00。
    - 如果 df_1d 为空，使用 trade_date。
    """
    if df_1d is not None and not df_1d.empty:
        last_date = df_1d.index[-1].date()
    else:
        last_date = trade_date
    return datetime(
        last_date.year, last_date.month, last_date.day,
        15, 0, 0, tzinfo=_SHANGHAI_TZ,
    )


def _normalize_secondary_bar_time(
    df_15m: pd.DataFrame | None,
) -> datetime | None:
    """取 15m 最后一根 bar 的实际 trade_time，确保 timezone-aware。

    规范化规则：
    - 如果 df_15m 有数据，取最后一根 bar 的 trade_time。
    - 如果 trade_time 是 naive，加上 Asia/Shanghai 时区。
    - 如果 df_15m 为空，返回 None。
    """
    if df_15m is None or df_15m.empty:
        return None
    last_ts = df_15m.index[-1]
    ts = pd.Timestamp(last_ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize(_SHANGHAI_TZ)
    else:
        ts = ts.astimezone(_SHANGHAI_TZ)
    return ts.to_pydatetime()


# =============================================================================
# upsert：幂等写入
# =============================================================================


async def upsert_snapshot(
    session: AsyncSession,
    snapshot: StockFeatureSnapshot,
) -> StockFeatureSnapshot:
    """按唯一键幂等 upsert snapshot。

    存在则更新 payload/source_bar_time/updated_at，不存在则 insert。
    使用 PostgreSQL INSERT ... ON CONFLICT DO UPDATE。

    Args:
        session: 异步 DB 会话
        snapshot: 待写入的 StockFeatureSnapshot 对象

    Returns:
        写入后的 ORM 对象
    """
    stmt = pg_insert(StockFeatureSnapshot).values(
        instrument_id=snapshot.instrument_id,
        trade_date=snapshot.trade_date,
        primary_timeframe=snapshot.primary_timeframe,
        secondary_timeframe=snapshot.secondary_timeframe,
        adj=snapshot.adj,
        schema_version=snapshot.schema_version,
        source_primary_bar_time=snapshot.source_primary_bar_time,
        source_secondary_bar_time=snapshot.source_secondary_bar_time,
        structural_payload=snapshot.structural_payload,
        temporal_payload=snapshot.temporal_payload,
        summary_payload=snapshot.summary_payload,
        degraded_reasons=snapshot.degraded_reasons,
    )

    update_cols = {
        "source_primary_bar_time": stmt.excluded.source_primary_bar_time,
        "source_secondary_bar_time": stmt.excluded.source_secondary_bar_time,
        "structural_payload": stmt.excluded.structural_payload,
        "temporal_payload": stmt.excluded.temporal_payload,
        "summary_payload": stmt.excluded.summary_payload,
        "degraded_reasons": stmt.excluded.degraded_reasons,
        "updated_at": func.now(),
    }

    stmt = stmt.on_conflict_do_update(
        constraint="uq_feature_snapshot_instrument_date_tf_adj_schema",
        set_=update_cols,
    )
    await session.execute(stmt)
    await session.flush()

    # 返回传入的 snapshot（upsert 已在 DB 层完成，不重新查询避免 identity map 返回旧值）
    return snapshot


# =============================================================================
# 批量计算
# =============================================================================


async def compute_for_trade_date(
    session: AsyncSession,
    trade_date: date,
    instrument_ids: Sequence[uuid.UUID],
    batch_size: int = 20,
    commit_every: int = 500,
    failure_threshold: float = 0.3,
) -> dict[str, Any]:
    """为给定 instrument 列表批量计算并写入快照。

    - 按 batch_size 分批遍历
    - 单股失败记录，不阻塞其他股票
    - 每满 commit_every rows 执行一次 commit
    - 失败比例超过 failure_threshold 时整体抛异常

    Args:
        session: 异步 DB 会话
        trade_date: 交易日
        instrument_ids: 标的 ID 列表
        batch_size: 每批 instrument 数（默认 20）
        commit_every: 每 N rows commit 一次（默认 500）
        failure_threshold: 失败比例阈值（默认 0.3）

    Returns:
        统计信息 dict：snapshot_count, failed_count, schema_version, trade_date

    Raises:
        RuntimeError: 失败比例超过 failure_threshold
    """
    total = len(instrument_ids)
    snapshot_count = 0
    failed_count = 0
    rows_since_commit = 0

    for i in range(0, total, batch_size):
        batch = instrument_ids[i : i + batch_size]
        for instrument_id in batch:
            try:
                snapshot = await compute_feature_snapshot_for_date(
                    session, instrument_id, trade_date,
                )
                await upsert_snapshot(session, snapshot)
                snapshot_count += 1
                rows_since_commit += 1
            except Exception as exc:
                failed_count += 1
                logger.error(
                    "snapshot 计算失败 instrument_id=%s trade_date=%s: %s",
                    instrument_id, trade_date, exc, exc_info=True,
                )

            if rows_since_commit >= commit_every:
                await session.commit()
                rows_since_commit = 0

    # 提交剩余
    if rows_since_commit > 0:
        await session.commit()

    # 检查失败阈值
    if total > 0:
        failure_rate = failed_count / total
        if failure_rate > failure_threshold:
            raise RuntimeError(
                f"feature_snapshot 失败比例 {failure_rate:.1%} 超过阈值 {failure_threshold:.0%} "
                f"(failed={failed_count}, total={total})"
            )

    logger.info(
        "feature_snapshot 批量完成 trade_date=%s snapshot_count=%d failed_count=%d",
        trade_date, snapshot_count, failed_count,
    )

    return {
        "snapshot_count": snapshot_count,
        "failed_count": failed_count,
        "schema_version": _SCHEMA_VERSION,
        "trade_date": trade_date.isoformat(),
    }


# =============================================================================
# 辅助：获取需要快照的 instrument 列表
# =============================================================================


async def get_active_a_share_instruments(
    session: AsyncSession,
) -> list[uuid.UUID]:
    """获取所有活跃 A 股股票的 instrument_id 列表。

    与 BarsCoverageService 口径一致：
    - status='active'
    - symbol 匹配 A 股股票代码（6 位数字，排除指数/基金/ETF）
    """
    from app.models.instrument import Instrument

    stmt = select(Instrument.id).where(
        Instrument.status == "active",
        Instrument.symbol.op("~")(r"^\d{6}$"),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯函数自测（不连 DB）
    print("feature_snapshot_service 自测...")

    # _truncate_bars_to_trade_date
    idx = pd.date_range("2026-01-01", periods=10, freq="B")
    bars = pd.DataFrame({"close": range(10)}, index=idx)
    truncated = _truncate_bars_to_trade_date(bars, date(2026, 1, 7), "1d")
    assert truncated is not None
    assert truncated.index[-1].date() <= date(2026, 1, 7)
    print(f"_truncate_bars: {len(truncated)} bars (expect <= 5)")

    # build_summary_payload
    summary = build_summary_payload({}, {}, date(2026, 1, 10))
    assert summary["_source"] == "feature_snapshot"
    assert summary["poc_price"] is None
    print(f"build_summary_payload: {len(summary)} fields")

    print("OK")
