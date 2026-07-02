"""统一图表行情输入服务。

用法:
    from app.services.chart_bars_service import (
        load_chart_bars,
        compute_source_bar_hash,
        compute_source_bar_times,
    )

    df = await load_chart_bars(session, instrument_id, timeframe="1d", count=250)
    bar_times = compute_source_bar_times(df)
    bar_hash = compute_source_bar_hash(df)

事实源:
- app.services.market_data_aggregation_service.MarketDataAggregationService（行情聚合唯一事实源）
- indicator_contract.INDICATOR_BARS["1d"]=250（图表场景日线根数）

约束:
- /bars API 与 indicator_service 必须共用此服务获取日线行情
- 日线固定最近 count 根已完成前复权 Bar（默认 250，图表场景）
- 返回 DataFrame: open/high/low/close/volume/amount/adj_factor 列, DatetimeIndex（naive 上海时间）
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, time as dt_time
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import CHART_BARS_COUNT

logger = logging.getLogger("services.chart_bars_service")

# [chart_bars] - 描述: 图表场景日线默认根数，引用 indicator_contract.CHART_BARS_COUNT 唯一真源
_DEFAULT_CHART_DAILY_COUNT = CHART_BARS_COUNT

# A 股收盘时间 15:00 Asia/Shanghai（_filter_unfinished_daily_bars 保留兼容）
_DAILY_CLOSE_TIME = dt_time(15, 0)


async def load_chart_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str = "1d",
    count: int = _DEFAULT_CHART_DAILY_COUNT,
    adj: str = "qfq",
) -> pd.DataFrame:
    """统一图表行情输入服务。

    /bars API 与 indicator_service 共用此服务获取日线行情，确保数据源一致。
    内部委托 MarketDataAggregationService（行情聚合唯一事实源）处理数据获取、
    Pytdx 兜底、复权、排序、去重、未完成 Bar 过滤；本函数仅做最后的 count 截取。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        timeframe: 周期（当前仅支持 "1d"）
        count: 返回最近 N 根 Bar（默认 250）
        adj: 复权方式 "qfq"（前复权，默认）或 "none"（不复权）

    Returns:
        DataFrame: open/high/low/close/volume/amount/adj_factor 列, DatetimeIndex
        无数据时返回空 DataFrame

    Raises:
        ValueError: timeframe 不在支持列表中，或 adj 非法
    """
    if timeframe != "1d":
        raise ValueError(
            f"load_chart_bars 当前仅支持 1d, got {timeframe!r}"
        )
    if adj not in ("qfq", "none"):
        raise ValueError(f"load_chart_bars adj 只支持 qfq/none, got {adj!r}")

    from app.services.market_data_aggregation_service import (
        MarketDataAggregationService,
    )

    service = MarketDataAggregationService()
    result = await service.get_bars(
        session, instrument_id, timeframe="1d", adj=adj,
    )
    df = result.bars
    if df.empty:
        return df

    # count 截取: 取最近 count 根
    return df.tail(count)


def _filter_unfinished_daily_bars(
    df: pd.DataFrame,
    now: datetime | None = None,
) -> pd.DataFrame:
    """过滤当日未完成日线 Bar。

    如果最新 Bar 的日期是今天且现在未到收盘时间（15:00 Asia/Shanghai），则过滤掉。

    Args:
        df: 日线 DataFrame，index 为 DatetimeIndex
        now: 当前时间（用于测试），None 使用 datetime.now(SHANGHAI_TZ)

    Returns:
        过滤后的 DataFrame
    """
    if df.empty:
        return df
    if now is None:
        now = datetime.now(SHANGHAI_TZ)
    today = now.date()
    latest_date = df.index[-1].date()
    if latest_date == today and now.time() < _DAILY_CLOSE_TIME:
        df = df[df.index.date < today]
    return df


def compute_source_bar_times(df: pd.DataFrame) -> list[str]:
    """计算 source_bar_times（ISO 日期字符串数组）。

    Args:
        df: 行情 DataFrame，index 为 DatetimeIndex

    Returns:
        ISO 日期字符串列表（YYYY-MM-DD），长度等于 DataFrame 行数
    """
    return [idx.strftime("%Y-%m-%d") for idx in df.index]


def compute_source_bar_hash(df: pd.DataFrame) -> str:
    """计算 source_bar_hash（OHLCV 拼接的 SHA256 哈希前 16 字符）。

    拼接格式（每行一个）: date|open|high|low|close|volume|amount
    所有行用换行符连接后计算 SHA256，取 hexdigest 前 16 字符。

    Args:
        df: 行情 DataFrame，含 open/high/low/close/volume/amount 列

    Returns:
        SHA256 hexdigest 前 16 字符；空 DataFrame 返回空字符串
    """
    if df.empty:
        return ""
    parts: list[str] = []
    for idx, row in df.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        parts.append(
            f"{date_str}|{row['open']}|{row['high']}|{row['low']}|"
            f"{row['close']}|{row['volume']}|{row['amount']}"
        )
    joined = "\n".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    # 自测入口：验证函数签名与基础逻辑（不连 DB/网络，无副作用）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 验证函数签名
    sig = inspect.signature(load_chart_bars)
    params = list(sig.parameters.keys())
    assert params == ["session", "instrument_id", "timeframe", "count", "adj"], \
        f"load_chart_bars 参数不匹配: {params}"
    print(f"load_chart_bars params={params} ✓")

    # 2. 验证 compute_source_bar_hash
    sample_df = pd.DataFrame({
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [100000.0],
        "amount": [1020000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-16"]))
    sample_df.index.name = "trade_date"

    h = compute_source_bar_hash(sample_df)
    assert isinstance(h, str) and len(h) == 16, f"hash 应为 16 字符: {h}"
    # 相同输入产生相同 hash
    assert compute_source_bar_hash(sample_df) == h
    # 空数据
    assert compute_source_bar_hash(pd.DataFrame()) == ""
    print(f"compute_source_bar_hash: {h} ✓")

    # 3. 验证 compute_source_bar_times
    times = compute_source_bar_times(sample_df)
    assert times == ["2026-06-16"], f"times 不匹配: {times}"
    assert len(times) == len(sample_df)
    print(f"compute_source_bar_times: {times} ✓")

    # 4. 验证 _filter_unfinished_daily_bars
    # 4.1 收盘前：今日 Bar 被过滤
    today = date.today()
    df_with_today = pd.DataFrame({
        "open": [10.0, 11.0],
        "high": [10.5, 11.5],
        "low": [9.8, 10.8],
        "close": [10.2, 11.2],
        "volume": [100000.0, 110000.0],
        "amount": [1020000.0, 1232000.0],
        "adj_factor": [1.0, 1.0],
    }, index=pd.to_datetime(["2026-06-15", today.isoformat()]))
    df_with_today.index.name = "trade_date"

    fake_before_close = datetime(
        today.year, today.month, today.day, 14, 0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    filtered = _filter_unfinished_daily_bars(df_with_today, now=fake_before_close)
    assert len(filtered) == 1, f"收盘前应过滤今日 Bar: {len(filtered)}"
    assert filtered.index[0].date() != today
    print(f"收盘前过滤今日 Bar: {len(filtered)} ✓")

    # 4.2 收盘后：今日 Bar 保留
    fake_after_close = datetime(
        today.year, today.month, today.day, 16, 0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    kept = _filter_unfinished_daily_bars(df_with_today, now=fake_after_close)
    assert len(kept) == 2, f"收盘后应保留今日 Bar: {len(kept)}"
    print(f"收盘后保留今日 Bar: {len(kept)} ✓")

    # 4.3 非今日数据：不过滤
    df_no_today = pd.DataFrame({
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [100000.0],
        "amount": [1020000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-15"]))
    df_no_today.index.name = "trade_date"
    kept_all = _filter_unfinished_daily_bars(df_no_today, now=fake_before_close)
    assert len(kept_all) == 1, "非今日数据应保留"
    print("非今日数据保留 ✓")

    # 5. 验证空数据
    empty_df = pd.DataFrame()
    assert _filter_unfinished_daily_bars(empty_df) is empty_df
    print("空数据 _filter_unfinished_daily_bars ✓")

    print("OK")
