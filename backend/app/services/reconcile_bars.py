"""行情数据对账机制。

对比 DB 数据与 pytdx 源数据，检测 3 类差异：
1. DB 缺失：pytdx 有数据，但 DB 无（需补写）
2. DB 多余：DB 中有数据，但 pytdx 无（需删除）
3. 值不一致：同一 (instrument_id, trade_date) 的 close 不同（超过 0.01 容差）

设计原则：
- 仅检测不修复（避免误删/误写，由人工决定）
- 对账过程不修改任何 DB 数据
- 批量对账默认抽样 10 只股票（避免全量 8000+ 耗时过长）

Inputs:
    session: AsyncSession
    instrument_id: UUID
    symbol: 股票代码
    period: 周期（d/15m/60m/w/m）
    start_date/end_date: 对账日期范围

Outputs:
    ReconcileResult: db_count, source_count, missing/extra/mismatch counts

How to Run:
    python -m app.services.reconcile_bars    # 自测：验证对账逻辑
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMinute, BarMonthly, BarWeekly
from app.models.instrument import Instrument

logger = logging.getLogger("reconcile_bars")

# 值不一致容差（close 差异超过此值视为不一致）
_MISMATCH_TOLERANCE = 0.01

# 不一致详情最大保留条数
_MAX_MISMATCH_DETAILS = 20

# 批量对账默认抽样数量
_DEFAULT_BATCH_SAMPLE_SIZE = 10

# 批量对账默认天数
_DEFAULT_BATCH_DAYS = 30

# 15min/60min count 上限与 bars_scheduler_service.BACKFILL_COUNTS 对齐
# 15min: 15000（覆盖 2023-01-01 至今约 14000 条）
# 60min: 4000（覆盖 2023-01-01 至今约 3500 条）
_15MIN_COUNT_LIMIT = 15000
_60MIN_COUNT_LIMIT = 4000

# period -> (Model, 时间字段名, 是否日线类)
_PERIOD_CONFIG: dict[str, tuple[type, str, bool]] = {
    "d": (BarDaily, "trade_date", True),
    "w": (BarWeekly, "trade_date", True),
    "m": (BarMonthly, "trade_date", True),
    "15m": (Bar15Min, "trade_time", False),
    "60m": (Bar60Min, "trade_time", False),
    "minute": (BarMinute, "trade_time", False),
}


@dataclass
class ReconcileResult:
    """对账结果。

    Attributes:
        instrument_id: 标的 UUID
        symbol: 股票代码
        period: 周期（d/15m/60m/w/m）
        db_count: DB 中的记录数
        source_count: pytdx 源数据记录数
        missing_count: DB 缺失数（pytdx 有 DB 无）
        extra_count: DB 多余数（DB 有 pytdx 无）
        mismatch_count: 值不一致数（close 差异 > 0.01）
        mismatches: 不一致详情列表（最多 20 条）
    """

    instrument_id: uuid.UUID
    symbol: str
    period: str
    db_count: int
    source_count: int
    missing_count: int
    extra_count: int
    mismatch_count: int
    mismatches: list[dict] = field(default_factory=list)


async def _query_db_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    model_cls: type,
    time_col: str,
    start: date | datetime,
    end: date | datetime,
) -> pd.DataFrame:
    """从 DB 查询指定周期的行情数据（用于对账）。

    Returns:
        DataFrame: index=时间列, columns=[close]（仅 close 用于对账）
    """
    time_column = getattr(model_cls, time_col)
    close_column = model_cls.close

    try:
        result = await session.execute(
            select(time_column, close_column)
            .where(model_cls.instrument_id == instrument_id)
            .where(time_column >= start)
            .where(time_column <= end)
            .order_by(time_column)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning(
            "对账查询 DB 失败 instrument_id=%s table=%s: %s",
            instrument_id, model_cls.__tablename__, exc,
        )
        raise

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[time_col, "close"])
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.set_index(time_col)
    # Decimal -> float（便于对比）
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


async def _fetch_source_bars(
    symbol: str,
    period: str,
    start: date | datetime,
    end: date | datetime,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """从 pytdx 拉取源数据（用于对账）。

    Returns:
        DataFrame: index=datetime, columns=[close]（仅 close 用于对账）
    """
    pytdx = adapter or get_pytdx_adapter()

    try:
        if period == "d":
            raw_df = await asyncio.to_thread(
                pytdx.get_daily_bars, symbol, start, end
            )
        elif period == "minute":
            raw_df = await asyncio.to_thread(
                pytdx.get_minute_bars, symbol, start, end
            )
        elif period == "w":
            # 周线按 count 拉取，估算条数
            days = (end - start).days if isinstance(end, date) else 30
            count = min(max(days // 7 + 10, 10), 800)
            raw_df = await asyncio.to_thread(pytdx.get_weekly_bars, symbol, count)
        elif period == "m":
            days = (end - start).days if isinstance(end, date) else 30
            count = min(max(days // 30 + 5, 5), 800)
            raw_df = await asyncio.to_thread(pytdx.get_monthly_bars, symbol, count)
        elif period == "15m":
            # 15min 按时间范围拉取（通过 count 估算）
            if isinstance(start, datetime):
                minutes = int((end - start).total_seconds() // 60)
                count = min(max(minutes // 15 + 100, 100), _15MIN_COUNT_LIMIT)
            else:
                count = 800
            raw_df = await asyncio.to_thread(pytdx.get_15min_bars, symbol, count)
        elif period == "60m":
            if isinstance(start, datetime):
                minutes = int((end - start).total_seconds() // 60)
                count = min(max(minutes // 60 + 50, 50), _60MIN_COUNT_LIMIT)
            else:
                count = 800
            raw_df = await asyncio.to_thread(pytdx.get_60min_bars, symbol, count)
        else:
            logger.error("不支持的周期 period=%s", period)
            return pd.DataFrame()
    except Exception as exc:
        logger.warning("对账拉取 pytdx 数据失败 symbol=%s period=%s: %s", symbol, period, exc)
        raise

    if raw_df.empty:
        return pd.DataFrame()

    # 提取 datetime 和 close 列
    if "datetime" not in raw_df.columns or "close" not in raw_df.columns:
        logger.warning(
            "pytdx 数据缺少必要列 symbol=%s period=%s columns=%s",
            symbol, period, raw_df.columns.tolist(),
        )
        return pd.DataFrame()

    df = raw_df[["datetime", "close"]].copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def _compare_bars(
    db_df: pd.DataFrame,
    source_df: pd.DataFrame,
    tolerance: float = _MISMATCH_TOLERANCE,
) -> tuple[int, int, int, list[dict]]:
    """对比 DB 与源数据，返回 (missing, extra, mismatch, mismatches)。

    Args:
        db_df: DB 数据，index=时间, columns=[close]
        source_df: 源数据，index=时间, columns=[close]
        tolerance: close 差异容差

    Returns:
        (missing_count, extra_count, mismatch_count, mismatches)
    """
    db_keys = set(db_df.index) if not db_df.empty else set()
    source_keys = set(source_df.index) if not source_df.empty else set()

    # DB 缺失：source 有 DB 无
    missing_keys = source_keys - db_keys
    missing_count = len(missing_keys)

    # DB 多余：DB 有 source 无
    extra_keys = db_keys - source_keys
    extra_count = len(extra_keys)

    # 值不一致：同 key 的 close 差异 > tolerance
    common_keys = db_keys & source_keys
    mismatch_count = 0
    mismatches: list[dict] = []

    if common_keys:
        # 对齐索引后比较 close
        db_aligned = db_df.loc[list(common_keys), "close"]
        source_aligned = source_df.loc[list(common_keys), "close"]

        for idx in common_keys:
            db_close = db_aligned.loc[idx]
            src_close = source_aligned.loc[idx]

            # 跳过 NaN
            if pd.isna(db_close) or pd.isna(src_close):
                continue

            diff = abs(float(db_close) - float(src_close))
            if diff > tolerance:
                mismatch_count += 1
                if len(mismatches) < _MAX_MISMATCH_DETAILS:
                    mismatches.append({
                        "timestamp": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                        "db_close": float(db_close),
                        "source_close": float(src_close),
                        "diff": round(diff, 4),
                    })

    return missing_count, extra_count, mismatch_count, mismatches


async def reconcile_instrument(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    symbol: str,
    period: str,
    start_date: date | datetime,
    end_date: date | datetime,
    adapter: PytdxAdapter | None = None,
) -> ReconcileResult:
    """对账单只股票单周期数据。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        symbol: 股票代码（如 '000001'）
        period: 周期（d/15m/60m/w/m/minute）
        start_date: 起始日期/时间
        end_date: 结束日期/时间
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        ReconcileResult: 对账结果（不修改任何 DB 数据）

    Raises:
        ValueError: period 不支持
        Exception: DB 查询或 pytdx 拉取失败时 re-raise
    """
    if period not in _PERIOD_CONFIG:
        raise ValueError(
            f"不支持的周期 period={period}，支持: {list(_PERIOD_CONFIG.keys())}"
        )

    model_cls, time_col, _is_daily = _PERIOD_CONFIG[period]

    # 1. 查询 DB 数据
    db_df = await _query_db_bars(
        session, instrument_id, model_cls, time_col, start_date, end_date
    )

    # 2. 拉取 pytdx 源数据
    source_df = await _fetch_source_bars(
        symbol, period, start_date, end_date, adapter
    )

    # 3. 对比
    missing_count, extra_count, mismatch_count, mismatches = _compare_bars(
        db_df, source_df
    )

    result = ReconcileResult(
        instrument_id=instrument_id,
        symbol=symbol,
        period=period,
        db_count=len(db_df),
        source_count=len(source_df),
        missing_count=missing_count,
        extra_count=extra_count,
        mismatch_count=mismatch_count,
        mismatches=mismatches,
    )

    logger.info(
        "对账完成 symbol=%s period=%s db=%d source=%d missing=%d extra=%d mismatch=%d",
        symbol, period, result.db_count, result.source_count,
        missing_count, extra_count, mismatch_count,
    )

    return result


async def reconcile_batch(
    session: AsyncSession,
    symbols: list[str] | None = None,
    period: str = "d",
    days: int = _DEFAULT_BATCH_DAYS,
    sample_size: int = _DEFAULT_BATCH_SAMPLE_SIZE,
    adapter: PytdxAdapter | None = None,
) -> list[ReconcileResult]:
    """批量对账（默认最近 30 天日线，抽样 10 只股票）。

    Args:
        session: 异步会话
        symbols: 指定股票代码列表；None 时从 DB 抽样 sample_size 只
        period: 周期（默认 d）
        days: 对账天数（默认 30）
        sample_size: 抽样数量（默认 10）
        adapter: pytdx 适配器

    Returns:
        各股票的对账结果列表
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    # 确定对账股票列表
    if symbols is None:
        # 从 DB 抽样 active 股票
        try:
            from app.services.instrument_maintenance_service import stock_symbol_sql_filter
            # [ReconcileBars] - 描述: 抽样只取 A 股股票，与覆盖率口径一致
            result = await session.execute(
                select(Instrument.id, Instrument.symbol)
                .where(Instrument.status == "active")
                .where(stock_symbol_sql_filter(Instrument))
                .order_by(Instrument.symbol)
                .limit(sample_size)
            )
            rows = result.all()
            symbols = [row[1] for row in rows]
            instrument_ids = [row[0] for row in rows]
        except Exception as exc:
            logger.error("对账批量查询股票列表失败: %s", exc)
            raise
    else:
        # 查询指定 symbols 的 instrument_id
        try:
            result = await session.execute(
                select(Instrument.id, Instrument.symbol)
                .where(Instrument.symbol.in_(symbols))
            )
            rows = result.all()
            # 保持输入顺序
            id_map = {row[1]: row[0] for row in rows}
            instrument_ids = [id_map[s] for s in symbols if s in id_map]
            symbols = [s for s in symbols if s in id_map]
        except Exception as exc:
            logger.error("对账批量查询指定 symbols 失败: %s", exc)
            raise

    if not symbols:
        logger.warning("对账批量无可用股票")
        return []

    results: list[ReconcileResult] = []
    for instrument_id, symbol in zip(instrument_ids, symbols, strict=True):
        try:
            result = await reconcile_instrument(
                session, instrument_id, symbol, period,
                start_date, end_date, adapter,
            )
            results.append(result)
        except Exception as exc:
            logger.error(
                "对账单只股票失败 symbol=%s period=%s: %s",
                symbol, period, exc,
            )
            # 记录失败结果（不计入成功对账）
            results.append(ReconcileResult(
                instrument_id=instrument_id,
                symbol=symbol,
                period=period,
                db_count=0,
                source_count=0,
                missing_count=0,
                extra_count=0,
                mismatch_count=0,
                mismatches=[],
            ))

    return results


if __name__ == "__main__":
    # 自测入口：验证对账逻辑（无副作用，不连 DB/pytdx）
    print("===== Phase 5.3 reconcile_bars 自测 =====")

    # 1. 验证 ReconcileResult 数据类
    result = ReconcileResult(
        instrument_id=uuid.uuid4(),
        symbol="000001",
        period="d",
        db_count=30,
        source_count=30,
        missing_count=0,
        extra_count=0,
        mismatch_count=0,
        mismatches=[],
    )
    assert result.symbol == "000001"
    assert result.db_count == 30
    assert result.missing_count == 0
    print(f"✓ ReconcileResult 数据类验证通过: {result}")

    # 2. 验证 _compare_bars 逻辑 - 完全一致
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    db_df = pd.DataFrame({"close": [10.0, 10.5, 10.2, 10.8, 10.6]}, index=dates)
    source_df = pd.DataFrame({"close": [10.0, 10.5, 10.2, 10.8, 10.6]}, index=dates)

    missing, extra, mismatch, mismatches = _compare_bars(db_df, source_df)
    assert missing == 0, f"完全一致时 missing 应为 0，实际 {missing}"
    assert extra == 0, f"完全一致时 extra 应为 0，实际 {extra}"
    assert mismatch == 0, f"完全一致时 mismatch 应为 0，实际 {mismatch}"
    print("✓ 完全一致对账通过（missing=0, extra=0, mismatch=0）")

    # 3. 验证 _compare_bars 逻辑 - DB 缺失
    db_df_missing = db_df.iloc[:3].copy()  # DB 只有前 3 条
    missing, extra, mismatch, mismatches = _compare_bars(db_df_missing, source_df)
    assert missing == 2, f"DB 缺失 2 条，实际 missing={missing}"
    assert extra == 0, f"无多余，实际 extra={extra}"
    print(f"✓ DB 缺失对账通过（missing={missing}）")

    # 4. 验证 _compare_bars 逻辑 - DB 多余
    db_df_extra = pd.concat([db_df, pd.DataFrame(
        {"close": [11.0]},
        index=pd.to_datetime(["2024-01-06"]),
    )])
    missing, extra, mismatch, mismatches = _compare_bars(db_df_extra, source_df)
    assert missing == 0, f"无缺失，实际 missing={missing}"
    assert extra == 1, f"DB 多余 1 条，实际 extra={extra}"
    print(f"✓ DB 多余对账通过（extra={extra}）")

    # 5. 验证 _compare_bars 逻辑 - 值不一致
    source_df_mismatch = source_df.copy()
    source_df_mismatch.loc[dates[0], "close"] = 10.5  # 差异 0.5 > 0.01
    source_df_mismatch.loc[dates[1], "close"] = 10.51  # 差异 0.01 = tolerance，不算
    missing, extra, mismatch, mismatches = _compare_bars(db_df, source_df_mismatch)
    assert mismatch == 1, f"值不一致 1 条（差异 0.5），实际 mismatch={mismatch}"
    assert len(mismatches) == 1, f"mismatches 应有 1 条，实际 {len(mismatches)}"
    assert mismatches[0]["diff"] == 0.5, f"diff 应为 0.5，实际 {mismatches[0]['diff']}"
    print(f"✓ 值不一致对账通过（mismatch={mismatch}, diff={mismatches[0]['diff']}）")

    # 6. 验证容差边界（差异 = 0.01 不算不一致）
    source_df_boundary = source_df.copy()
    source_df_boundary.loc[dates[0], "close"] = 10.01  # 差异 0.01 = tolerance
    missing, extra, mismatch, mismatches = _compare_bars(db_df, source_df_boundary)
    assert mismatch == 0, f"差异 = tolerance 不应算不一致，实际 mismatch={mismatch}"
    print("✓ 容差边界验证通过（差异 = 0.01 不算不一致）")

    # 7. 验证空数据
    empty_df = pd.DataFrame()
    missing, extra, mismatch, mismatches = _compare_bars(empty_df, source_df)
    assert missing == 5, f"DB 空时 missing 应为 5，实际 {missing}"
    assert extra == 0
    print(f"✓ 空数据对账通过（missing={missing}）")

    # 8. 验证 _PERIOD_CONFIG
    assert "d" in _PERIOD_CONFIG
    assert "15m" in _PERIOD_CONFIG
    assert "60m" in _PERIOD_CONFIG
    assert "w" in _PERIOD_CONFIG
    assert "m" in _PERIOD_CONFIG
    assert "minute" in _PERIOD_CONFIG
    assert _PERIOD_CONFIG["d"] == (BarDaily, "trade_date", True)
    assert _PERIOD_CONFIG["15m"] == (Bar15Min, "trade_time", False)
    print("✓ _PERIOD_CONFIG 配置验证通过（6 个周期）")

    # 9. 验证 reconcile_instrument 签名
    import inspect
    sig = inspect.signature(reconcile_instrument)
    params = list(sig.parameters.keys())
    expected = ["session", "instrument_id", "symbol", "period", "start_date", "end_date", "adapter"]
    assert params == expected, f"reconcile_instrument 参数应为 {expected}，实际 {params}"
    print(f"✓ reconcile_instrument 签名验证通过: {params}")

    # 10. 验证 reconcile_batch 签名
    sig = inspect.signature(reconcile_batch)
    params = list(sig.parameters.keys())
    expected = ["session", "symbols", "period", "days", "sample_size", "adapter"]
    assert params == expected, f"reconcile_batch 参数应为 {expected}，实际 {params}"
    assert sig.parameters["period"].default == "d", "period 默认应为 'd'"
    assert sig.parameters["days"].default == _DEFAULT_BATCH_DAYS, f"days 默认应为 {_DEFAULT_BATCH_DAYS}"
    assert sig.parameters["sample_size"].default == _DEFAULT_BATCH_SAMPLE_SIZE, \
        f"sample_size 默认应为 {_DEFAULT_BATCH_SAMPLE_SIZE}"
    print(f"✓ reconcile_batch 签名验证通过: {params}")

    print("\n所有自测通过 ✓（未进行 DB/pytdx 测试）")
