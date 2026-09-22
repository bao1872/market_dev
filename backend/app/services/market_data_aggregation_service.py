"""行情聚合服务 - 统一 OHLCV bar 聚合唯一事实源。

用法:
    service = MarketDataAggregationService()
    result = await service.get_bars(session, instrument_id, timeframe="1d", adj="qfq")
    result.bars  # DataFrame
    result.data_source  # db | hybrid | pytdx | degraded

职责:
- 日线: DB 优先 → Pytdx 补尾 → 复权 → 过滤未完成 bar → 排序去重
- 周线/月线: 从日线动态合成
- 日内(15m/1h): DB 优先 → 交易时段拉 1m 聚合为 partial bar → 复权 → 合并
- 数据源诊断: data_source / as_of / is_partial / last_persisted_bar_time /
  last_live_bar_time / freshness_seconds / degraded / degraded_reason
- Redis 短缓存: TTL 5–15 秒，缓存键含所有影响结果的参数
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from enum import StrEnum
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import (
    INDICATOR_BARS,
    NODE_CLUSTER_LOW_BARS,
)
from app.core.pytdx_adapter import get_pytdx_adapter
from app.core.redis_client import get_sync_redis
from app.core.time import SHANGHAI_TZ, now_shanghai, shanghai_business_date
from app.domain.shared.bar_identity import compute_source_bar_hash
from app.repositories.bar_repository import (
    _get_listing_date,
    _get_symbol,
    _query_15min_bars,
    _query_60min_bars,
    _query_daily_bars,
    _query_minute_bars,
    get_adj_factor_series_batch,
    get_daily_bars_batch,
)
from app.services.adjustment_factor_service import AdjustmentFactorService
from app.services.calendar_service import (
    get_next_authoritative_trading_day_async,
    is_trading_day_async,
)
from app.services.kline_aggregator import aggregate as aggregate_kline
from app.services.market_status_service import (
    MARKET_SESSION_AFTERNOON,
    MARKET_SESSION_MORNING,
    compute_market_session,
)

logger = logging.getLogger("services.market_data_aggregation_service")

# [mdas] - 描述: 支持的周期与复权方式
_ALLOWED_TIMEFRAMES: set[str] = {"1d", "15m", "1h", "1w", "1mo", "1m"}
_ALLOWED_ADJ: set[str] = {"qfq", "none"}


# [PANJI-INTRADAY-DIRECT-SOURCE] 冻结 source ownership：
#   实时分钟行情属于 Provider；历史分钟行情属于 DB。
# 盘后链不再维护 15m/1h（after_close periods=("d",)），因此「DB 优先 + provider 补尾 +
# merge」的 hybrid 模式对**实时分钟展示**已失去逻辑基础——随着时间推移 DB 15m 会越来越旧，
# 页面会同时读到大量旧 DB bar 和一小段 provider bar，无法判断某根 K 线来自哪里。
# 本枚举把「数据来源」从隐式实现细节提升为**显式的调用方参数**（默认 HYBRID，保持既有行为）。
class MarketDataSourcePolicy(StrEnum):
    """get_bars 数据来源策略（source owner 的显式声明）。

    - ``HYBRID``（默认）：DB 优先 + provider 补尾 + merge。日线/周线/月线/1m 以及所有
      既有调用方继续走这条路径，行为不变。
    - ``PROVIDER_DIRECT``：**只对 provider 原生日内周期（15m / 1h）有效**。直接读 provider
      原生 15m/60m，**完全不读 DB 分钟线、不做 DB ∪ provider merge**。
      provider 失败必须明确失败（异常向上传播），**禁止静默 fallback 到 stale DB**。
      复权 owner 不变：provider 原生 bar ``adj_factor=1.0`` 原样进入 MDAS **既有**的
      统一 adjustment pipeline 施加 qfq，分支内**不得**另写一套 qfq（否则二次复权）。
    - ``DB_ONLY``：只读 DB，**绝不访问外部 provider / realtime 尾部**
      （同时强制 ``allow_backfill=False`` 与 ``fresh_intraday_tail=False``）。
      历史 / PIT（回测、as-of replay、历史快照重建）必须使用本策略：
      PIT 路径访问网络会引入未来数据污染。
    """

    HYBRID = "hybrid"
    PROVIDER_DIRECT = "provider_direct"
    DB_ONLY = "db_only"


# PROVIDER_DIRECT 只支持 provider **原生**日内周期（1m/日线/周线/月线禁止）
_PROVIDER_DIRECT_TIMEFRAMES: frozenset[str] = frozenset({"15m", "1h"})

# PROVIDER_DIRECT 未显式给 limit 时的默认拉取条数。
# 引用 indicator_contract 唯一真源，禁止散落硬编码：
#   15m = NODE_CLUSTER_LOW_BARS(4000) —— Node Cluster 完整质量门槛
#   1h  = INDICATOR_BARS["1h"](1200)  —— 1h 指标窗口
_PROVIDER_DIRECT_DEFAULT_COUNT: dict[str, int] = {
    "15m": NODE_CLUSTER_LOW_BARS,
    "1h": INDICATOR_BARS["1h"],
}


def resolve_market_data_source_policy(
    timeframe: str,
    *,
    historical: bool,
) -> MarketDataSourcePolicy:
    """source policy 的**唯一判定点**（唯一真源，禁止复制第二套规则）。

    判定只依赖两个显式输入：周期 + 「本次请求是不是历史读」。
    ``historical`` 必须由调用方**显式**给出（或经 ``resolve_request_source_policy``
    由请求参数派生），**不得**用周期以外的间接特征猜测。

    - 非原生日内周期（1d / 1w / 1mo / 1m）→ ``HYBRID``（行为与历史完全一致）。
    - 15m / 1h + live → ``PROVIDER_DIRECT``：实时分钟行情归 Provider。
    - 15m / 1h + historical → ``DB_ONLY``：历史分钟行情归 DB，
      PIT 路径**绝对禁止访问网络**（联网会把未来数据污染进历史结果）。
    """
    if timeframe not in _PROVIDER_DIRECT_TIMEFRAMES:
        return MarketDataSourcePolicy.HYBRID
    return (
        MarketDataSourcePolicy.DB_ONLY
        if historical
        else MarketDataSourcePolicy.PROVIDER_DIRECT
    )


def is_historical_market_data_request(
    *,
    adjustment_as_of: date | None,
    end_date: date | None = None,
    today: date | None = None,
) -> bool:
    """「本次请求是不是历史 / PIT 请求」的**唯一语义 owner**。

    [P1-chart-snapshot-historical] 这个判断以前只活在 ``resolve_request_source_policy``
    内部，于是**只有 bars 半边**拿到了历史语义；同一次历史请求里的
    Node / indicator 取数仍各自假定 live，把当前 provider 行情混进历史结果
    （表面能算出结果、不报错，是最危险的一类污染）。

    现在把「请求语义」这件事抽成独立函数：**任何**需要按历史/实时分流的调用方
    （bars 展示、chart-snapshot 展示、indicator 内部日线/分钟/Node 输入）
    都必须从这里取同一个答案，禁止各自重新推断。

    历史判据（任一成立即 historical）：
    - 显式 ``adjustment_as_of``（as-of 复权锚点 ⇒ 历史/PIT 请求，最强信号）；
    - ``end_date`` 早于今天（回溯窗口 ⇒ 历史请求）。

    **刻意不把 ``completed_only=True`` 视为 historical**：实时 Node 输入也传
    completed_only=True，但它仍然是 live，必须走 provider_direct 的已完成 bars。
    """
    effective_today = today if today is not None else now_shanghai().date()
    return adjustment_as_of is not None or (
        end_date is not None and end_date < effective_today
    )


def resolve_request_source_policy(
    timeframe: str,
    *,
    adjustment_as_of: date | None,
    end_date: date | None = None,
    today: date | None = None,
) -> MarketDataSourcePolicy:
    """把**一次请求**的参数映射为 source policy（读链唯一入口）。

    ``/bars`` 与 ``/chart-snapshot`` 两条展示读链必须共用本函数 —— 否则
    「15m/1h 该读 Provider 还是 DB」会出现第二个判定 owner，两个入口随后漂移。

    历史语义本身由 ``is_historical_market_data_request`` 判定（单一 owner），
    本函数只负责把它翻译成周期相关的 policy。
    """
    return resolve_market_data_source_policy(
        timeframe,
        historical=is_historical_market_data_request(
            adjustment_as_of=adjustment_as_of, end_date=end_date, today=today,
        ),
    )


# [P0-2 2026-08-04] 单次 get_bars 在典型路径下的 repository 级读操作数：
# bars 查询 1 次 + 复权因子 1 次（qfq）+ 预期最后完成日 1 次 = 3 次。
# 仅用于 get_bars_batch 日内回退路径的近似诊断（与真实 SQL 数仍有出入，
# 不作为精确 SQL 计数），批读模式使用精确的 3/2 常数值。
_SINGLE_GET_BARS_REPOSITORY_QUERIES: int = 3

# [mdas] - 描述: 默认回看范围（与 bars.py / indicator_service.py 保持一致）
_DEFAULT_DAILY_LOOKBACK_DAYS: int = 5000
_DEFAULT_INTRADAY_LOOKBACK_DAYS: int = 180

# [CP-V3-A] - 描述: 日内周期每交易日 bar 根数（用于 limit 驱动的回看天数计算）
# A 股交易日 4 小时：15m=16 根，1h=4 根，1m=240 根
_BARS_PER_DAY: dict[str, int] = {"15m": 16, "1h": 4, "1m": 240}

# [USER-FIX-3 / C] - 描述: 允许 fresh_intraday_tail 的周期集合（**刻意只有 15m**）
# 本轮需求只针对 15m。不趁机扩大范围：1m/1h 的实时尾部合同保持不变
# （它们仍按原样返回 realtime 尾部，由各自调用方处理）。
_FRESH_TAIL_TIMEFRAMES: frozenset[str] = frozenset({"15m"})

# [CP-V3-A] - 描述: 日内回看安全边界（最大回看天数，约 20 年，防止无限扩大查询）
_MAX_INTRADAY_LOOKBACK_DAYS: int = 5000

# [CP-V3-A] - 描述: limit 驱动回看的额外 buffer 天数（确保交易日充足）
_LIMIT_LOOKBACK_BUFFER_DAYS: int = 10

# [mdas] - 描述: A 股收盘时间，日线 bar 完成边界
_DAILY_CLOSE_TIME: dt_time = dt_time(15, 0)

# [mdas] - 描述: Redis 短缓存 TTL 范围（秒）
_MIN_CACHE_TTL: int = 5
_MAX_CACHE_TTL: int = 15
_REDIS_CACHE_PREFIX: str = "mdas"

# [CP-V3-A] - 描述: 行情数据契约版本 v3（count-aware：含 requested_count/actual_count/
#   coverage_start/coverage_end/history_exhausted，支持 availability 三态状态机）
# [CP-V3-A2] - 描述: v4 — 迭代回补（backfill_rounds/coverage_reason）+ history_exhausted
#   语义修正（intraday: 基于 _fetch_intraday_with_backfill 真实 earliest bar 判定，
#   不再"单次查询 < limit 即 True"）。bump v3→v4 自动隔离旧缓存（旧 v3 缓存的
#   history_exhausted 语义不准确，必须失效）
# [CHANGE-20260730-P0] v4→v5：修复 Redis 序列化遗漏 latest_daily_quote
# [PANJI-INTRADAY-DIRECT-SOURCE] v5→v6：新增 MarketDataSourcePolicy。
#   cache_key 加入 source_policy：同一 (instrument, timeframe, limit, ...) 下
#   hybrid / provider_direct / db_only 的结果**必须**是不同缓存条目，
#   否则 15m 的 provider_direct 请求会命中旧 hybrid（=DB）缓存，
#   表现为「代码改了但看起来还是 DB 数据」。
#   又因 data_source / completed_through / is_partial 等诊断字段的语义在本轮发生变化
#   （provider_direct 不再有 DB persisted bar），一并 bump 让全部旧缓存自动失效。
# 旧缓存因 cache_key 含 contract_version，自动失效；无需全局 flush
_MARKET_DATA_CONTRACT_VERSION: str = "v6"

# [mdas] - 描述: 1m → 15m/1h 聚合频率映射
_TARGET_FREQ: dict[str, str] = {"15m": "15min", "1h": "60min"}

# [mdas] - 描述: 标准行情列
_BAR_COLUMNS: list[str] = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]


class VerificationExternalFetchBlockedError(RuntimeError):
    """verification_replay 模式下禁止外部网络行情源（pytdx），DB 数据不足时 fail-closed。

    携带精确缺口诊断，便于定位是哪个条件让 DB bars 被判为不可用。
    """

    def __init__(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        reason: str | None = None,
        db_row_count: int | None = None,
        completed_through: str | None = None,
        required_end: str | None = None,
        available_end: str | None = None,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.reason = reason
        self.db_row_count = db_row_count
        self.completed_through = completed_through
        self.required_end = required_end
        self.available_end = available_end
        msg = (
            "verification_replay 禁止外部行情源，DB 数据不足（fail-closed）: "
            f"symbol={symbol}, timeframe={timeframe}, reason={reason}, "
            f"db_row_count={db_row_count}, completed_through={completed_through}, "
            f"required_end={required_end}, available_end={available_end}"
        )
        super().__init__(msg)


def _market_data_mode() -> str:
    """读取 MARKET_DATA_MODE（verification_replay / production），缓存避免重复 get_settings。"""
    try:
        from app.config import get_settings

        return get_settings().market_data_mode
    except Exception:
        return "production"


def _is_verification_replay() -> bool:
    return _market_data_mode() == "verification_replay"


@dataclass
class BarAggregationResult:
    """行情聚合结果，包含 bars DataFrame 与数据源诊断字段。

    CHANGE-20260717-002 扩展（v2 契约）：
    - warmup_bars_full: 含 warmup 的完整计算集（warmup_bars=0 时为 None）
    - market_data_contract_version: 契约版本常量 "v2"
    - source_bar_hash: bars 的 OHLCV SHA256 前 16 字符（跨调用方一致性校验）
    - adj_factor_hash: 因子序列 SHA256 前 16 字符（adj=none 时为空串）
    - adjustment_as_of: 回显复权锚点（None=最新）
    - completed_through: 最新已完成 bar 时间（不含 partial/realtime）

    [CP-V3-A] v3 契约扩展（count-aware，支持 availability 三态状态机）：
    - requested_count: 调用方请求的 limit 值（None=未指定）
    - actual_count: 实际返回的 bars 数量
    - coverage_start: bars 最早时间（None=空数据）
    - coverage_end: bars 最晚时间（None=空数据）
    - history_exhausted: DB/上游历史是否不足（True=真实历史不够；
      False=历史足够但可能因系统回看窗口未取满——后者为 INPUT_CONTRACT_VIOLATION）

    [CP-V3-A2] v4 契约扩展（迭代回补，修正 history_exhausted 语义）：
    - backfill_rounds: 日内迭代回补的实际查询轮数（1=单次满足；>1=多轮扩展；
      日线=0，日线不需要迭代回补）
    - coverage_reason: 覆盖率原因诊断：
      * "no_limit" — 未指定 limit，单次查询
      * "met_after_N_rounds" — N 轮后满足 required_count
      * "history_exhausted_empty_query" — 查询返回空，已到 listing date 之前
      * "history_exhausted_no_progress" — 连续扩展无新数据，DB 真实历史不足
      * "max_rounds_reached" — 达到最大轮数仍不足（INPUT_CONTRACT_VIOLATION 风险）
      * "daily_no_backfill" — 日线路径不经过迭代回补
    - history_exhausted 语义修正（intraday）：基于 _fetch_intraday_with_backfill
      真实 earliest bar 判定，不再"单次查询 < limit 即 True"
    """

    bars: pd.DataFrame
    data_source: str
    as_of: datetime
    is_partial: bool
    last_persisted_bar_time: pd.Timestamp | None
    last_live_bar_time: pd.Timestamp | None
    freshness_seconds: float
    degraded: bool
    degraded_reason: str | None
    cache_hit: bool = False
    warmup_bars_full: pd.DataFrame | None = None
    market_data_contract_version: str = _MARKET_DATA_CONTRACT_VERSION
    source_bar_hash: str = ""
    adj_factor_hash: str = ""
    adjustment_as_of: date | None = None
    completed_through: pd.Timestamp | None = None
    # [CP-V3-A] count-aware 字段
    requested_count: int | None = None
    actual_count: int = 0
    coverage_start: pd.Timestamp | None = None
    coverage_end: pd.Timestamp | None = None
    history_exhausted: bool = False
    # [CP-V3-A2] 迭代回补诊断字段
    backfill_rounds: int = 0
    coverage_reason: str = ""
    # [CHANGE-20260724-004] 当日行情事实（quote 与展示周期解耦）
    # 无论 timeframe 是 1d/15m/1h/1w/1mo，latest_daily_quote 始终包含当日日线 OHLC
    # 前端 quote 卡片从此字段派生，不使用展示周期 bar 的 OHLC
    latest_daily_quote: dict | None = None


# ===== 交易时间判断 =====


def _is_trading_hours(now: datetime | None = None) -> bool:
    """判断当前是否在 A 股实时交易时段（上午盘/下午盘，午休不算）。

    复用 market_status_service.compute_market_session，与 /market/status 口径一致，
    不再自行写 9:30-15:00 连续判断。
    """
    if now is None:
        now = now_shanghai()
    # 这里只做 weekday 快速判断；节假日场景由调用方按需使用 is_trading_day_async
    is_trading_day = now.weekday() < 5
    session_name = compute_market_session(now, is_trading_day)
    return session_name in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON)


async def _is_trading_hours_async(now: datetime | None = None) -> bool:
    """异步包装（生产代码使用），支持同步/异步两种 patch 形态。"""
    result = _is_trading_hours(now)
    return result


# ===== 日线最后一个已完成 bar 边界 =====


async def _expected_last_completed_daily_bar(
    session: AsyncSession,
    now: datetime | None = None,
) -> date:
    """计算当前最后一个已完成日线的交易日。

    规则:
    - 今天是交易日且已过收盘时间 -> 今天
    - 否则往前找最近一个交易日
    """
    if now is None:
        now = now_shanghai()
    today = now.date()
    if await is_trading_day_async(session, today) and now.time() >= _DAILY_CLOSE_TIME:
        return today

    prev = today - timedelta(days=1)
    for _ in range(90):
        if await is_trading_day_async(session, prev):
            return prev
        prev -= timedelta(days=1)
    return prev


async def _call_expected_last_completed_daily_bar(
    session: AsyncSession,
    now: datetime,
) -> date:
    """调用 _expected_last_completed_daily_bar，兼容同步/异步 patch。"""
    fn = _expected_last_completed_daily_bar
    if asyncio.iscoroutinefunction(fn):
        return await fn(session, now)
    return fn(session, now)  # type: ignore[return-value]


# ===== 日期范围解析 =====


def _resolve_date_range(
    timeframe: str,
    start_date: date | datetime | None,
    end_date: date | datetime | None,
    *,
    limit: int | None = None,
) -> tuple[date, date] | tuple[datetime, datetime]:
    """解析查询范围。

    [CP-V3-A] count-aware 回补：当 limit 指定且 timeframe 为日内周期时，
    自动根据 limit 和每交易日 bar 根数计算所需最小回看天数，与
    _DEFAULT_INTRADAY_LOOKBACK_DAYS 取较大值，确保 actual_count 达到 limit。
    安全边界：最大不超过 _MAX_INTRADAY_LOOKBACK_DAYS（约 20 年）。
    """
    # [mdas] - 描述: 统一使用上海业务日期，避免服务器本地时区跨日误判
    today = shanghai_business_date()
    if timeframe in ("1d", "1w", "1mo"):
        if isinstance(end_date, date):
            end = end_date
        elif isinstance(end_date, datetime):
            end = end_date.date()
        else:
            end = today
        if isinstance(start_date, date):
            start = start_date
        elif isinstance(start_date, datetime):
            start = start_date.date()
        else:
            start = end - timedelta(days=_DEFAULT_DAILY_LOOKBACK_DAYS)
        return start, end

    # 15m / 1h / 1m
    if isinstance(end_date, datetime):
        end = end_date
    else:
        end = datetime.combine(end_date or today, datetime.max.time())
    if isinstance(start_date, datetime):
        start = start_date
    else:
        # [CP-V3-A] count-aware：limit 驱动的回看天数计算
        lookback_days = _DEFAULT_INTRADAY_LOOKBACK_DAYS
        if start_date is None and limit is not None:
            bars_per_day = _BARS_PER_DAY.get(timeframe, 16)
            min_days_needed = (
                math.ceil(limit / bars_per_day) + _LIMIT_LOOKBACK_BUFFER_DAYS
            )
            lookback_days = min(
                max(_DEFAULT_INTRADAY_LOOKBACK_DAYS, min_days_needed),
                _MAX_INTRADAY_LOOKBACK_DAYS,
            )
        start = datetime.combine(
            start_date or (end.date() - timedelta(days=lookback_days)),
            datetime.min.time(),
        )
    return start, end


# [CP-V3-A3] - 描述: 日内迭代回补参数（修正长期停牌边界）
# 迭代回补循环：查询→不足→向前扩展→满足 required_count 或确认 history_exhausted
_MAX_BACKFILL_ROUNDS: int = 10  # 最大查询轮数（防止死循环）
_BACKFILL_EXPAND_INITIAL_DAYS: int = 90  # 首轮扩展步长（约 60 交易日）
# [CP-V3-A3] A 股最早上市日（1990-12-19 上交所），listing_date 为 NULL 时的安全下界
_MIN_A_SHARE_DATE: date = date(1990, 1, 1)


async def _fetch_intraday_with_backfill(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    initial_start: datetime,
    end: datetime,
    query_fn: Any,
    *,
    required_count: int | None = None,
    listing_date: date | None = None,
) -> tuple[pd.DataFrame, int, bool, str]:
    """[CP-V3-A3] 日内迭代回补查询（修正长期停牌边界）。

    正确算法（PRD V3.3 §1.2 + DEVELOP §1.2 + PROMPT.md §1）：
      query range → 去重排序 → actual >= required_count：结束
      → 已到 instrument listing_date / DB 真实最早 bar：history_exhausted=True
      → no_progress 只表示本轮无新数据，必须继续扩大步长（90→180→360→... 有界递增）
      → 达到 max_rounds 仍未到达真实历史边界且不足 required_count：
        history_exhausted=False → INPUT_CONTRACT_VIOLATION（不得 degraded）

    [CP-V3-A3] 修正要点（PROMPT.md §1）：
      1. 只有查询已到达 listing_date 或 _MIN_A_SHARE_DATE，才允许 history_exhausted=True
      2. no_progress 不再等同于 history_exhausted（长期停牌股票更早可能有大量历史）
      3. 达到 max_rounds 但未到达真实历史边界 → INPUT_CONTRACT_VIOLATION，不是 degraded
      4. 按 trade_time 去重排序，最终只截取最近 required_count

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        timeframe: 日内周期（15m/1h/1m）
        initial_start: 初始查询起始时间（来自 _resolve_date_range 估算）
        end: 查询结束时间
        query_fn: 查询函数（_query_15min_bars / _query_60min_bars / _query_minute_bars）
        required_count: 需要的最小 bar 数（None=无 limit 要求，单次查询）
        listing_date: instrument 上市日期（None 时使用 _MIN_A_SHARE_DATE 安全下界）

    Returns:
        (bars_df, backfill_rounds, history_exhausted, coverage_reason)
        - bars_df: 合并去重后的 DataFrame（可能 > required_count，由调用方 tail(limit)）
        - backfill_rounds: 实际查询轮数
        - history_exhausted: True=真实历史不足（已到 listing date）；False=未到边界
        - coverage_reason: 诊断原因字符串
    """
    # 无 limit 要求 → 单次查询
    if required_count is None:
        bars_df = await query_fn(session, instrument_id, initial_start, end)
        return bars_df, 1, False, "no_limit"

    # [CP-V3-A3] 确定历史下界：listing_date 或 _MIN_A_SHARE_DATE
    # 防御性类型转换：DB 驱动或测试 fixture 可能返回 str，统一转为 date
    _raw_listing = listing_date if listing_date is not None else _MIN_A_SHARE_DATE
    if isinstance(_raw_listing, str):
        try:
            history_boundary_date = date.fromisoformat(_raw_listing[:10])
        except ValueError:
            history_boundary_date = _MIN_A_SHARE_DATE
    elif isinstance(_raw_listing, datetime):
        history_boundary_date = _raw_listing.date()
    else:
        history_boundary_date = _raw_listing
    history_boundary = datetime.combine(
        history_boundary_date, datetime.min.time()
    )
    # 统一为 naive 比较（current_start 可能带 tzinfo，如测试场景）
    _boundary_naive = history_boundary.replace(tzinfo=None) if history_boundary.tzinfo else history_boundary

    bars_df = pd.DataFrame()
    current_start = initial_start
    rounds = 0
    expand_step_days = _BACKFILL_EXPAND_INITIAL_DAYS

    while rounds < _MAX_BACKFILL_ROUNDS:
        rounds += 1
        new_bars = await query_fn(session, instrument_id, current_start, end)

        if not new_bars.empty:
            if bars_df.empty:
                bars_df = new_bars
            else:
                bars_df = _merge_bars(bars_df, new_bars)

        current_count = len(bars_df)

        # 满足要求 → 返回（history_exhausted=False）
        if current_count >= required_count:
            return bars_df, rounds, False, f"met_after_{rounds}_rounds"

        # [CP-V3-A3] 已到达历史下界（listing_date / 最早 A 股日期）
        # 此时仍不足 required_count → 真实历史不足
        _current_start_naive = current_start.replace(tzinfo=None) if current_start.tzinfo else current_start
        if _current_start_naive <= _boundary_naive:
            if new_bars.empty and rounds > 1:
                return bars_df, rounds, True, "history_exhausted_at_listing_date"
            # 查询有数据但不足，且已到 listing_date → 真实历史不足
            if current_count < required_count:
                return bars_df, rounds, True, "history_exhausted_at_listing_date"

        # [CP-V3-A3] no_progress 不再设置 history_exhausted=True
        # 只表示本轮扩展无新数据，继续扩大步长

        # 向前扩展 start（有界递增：90→180→360→720→...）
        new_start = current_start - timedelta(days=expand_step_days)
        if new_start >= current_start:
            # 无法继续扩展（datetime 下限）
            return bars_df, rounds, True, "history_exhausted_cannot_expand"
        # 不超过历史下界（统一 naive 比较）
        _new_start_naive = new_start.replace(tzinfo=None) if new_start.tzinfo else new_start
        if _new_start_naive < _boundary_naive:
            new_start = history_boundary
        current_start = new_start
        # 下一轮步长翻倍（有界递增）
        expand_step_days = min(expand_step_days * 2, 720)

    # [CP-V3-A3] 达到 max_rounds 仍不足
    # 若已到达历史下界 → 真实历史不足；否则 → INPUT_CONTRACT_VIOLATION
    _final_start_naive = current_start.replace(tzinfo=None) if current_start.tzinfo else current_start
    if _final_start_naive <= _boundary_naive:
        return bars_df, rounds, True, f"history_exhausted_max_rounds_{rounds}"
    return bars_df, rounds, False, f"max_rounds_reached_{rounds}"


# ===== 实时源拉取（Pytdx 直调，不走 DB） =====


async def fetch_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """从 Pytdx 拉取日线数据（DB 有缺口时补尾）。"""
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    adapter = get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(adapter.get_daily_bars, symbol, start_date, end_date)
    except Exception as exc:
        logger.warning("Pytdx 拉取日线失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if raw_df.empty:
        return raw_df

    raw_df = raw_df.copy()
    raw_df = raw_df.set_index("datetime")
    # [mdas-dedup] - pytdx 日线 datetime 为 15:00（收盘时刻），DB trade_date 为 00:00（午夜）。
    # 规范化到午夜，使 _merge_bars 的 index.duplicated() 能按交易日正确去重，
    # 避免"同日 00:00 和 15:00 两根错误日线"（CHANGE-20260717-002 验收发现）。
    raw_df.index = raw_df.index.normalize()
    raw_df.index.name = "trade_date"
    if "adj_factor" not in raw_df.columns:
        raw_df["adj_factor"] = 1.0
    return raw_df


async def fetch_minute_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
) -> pd.DataFrame:
    """从 Pytdx 拉取 1 分钟线数据（实时聚合用，不写库）。"""
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    adapter = get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(adapter.get_minute_bars, symbol, start_time, end_time)
    except Exception as exc:
        logger.warning("Pytdx 拉取 1m 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if raw_df.empty:
        return raw_df

    raw_df = raw_df.copy()
    raw_df = raw_df.set_index("datetime")
    raw_df.index.name = "trade_time"
    if "adj_factor" not in raw_df.columns:
        raw_df["adj_factor"] = 1.0
    return raw_df


# [P0-4] 原生周期 Pytdx 拉取（冻结行情周期合同）
# 唯一允许的生产链：
#   1m  = DB 1m  + Pytdx 1m
#   15m = DB 15m + Pytdx 原生 15m
#   1h  = DB 60m + Pytdx 原生 60m
#   1d  = DB 日线 + Pytdx 日线
#   1w/1mo = 合并完成的日线序列 → 周线/月线
# 禁止生产展示链使用 1m→15m / 1m→60m / 1m→1d 聚合（CHANGE-20260724-003）


async def fetch_15min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 16,
) -> pd.DataFrame:
    """从 Pytdx 拉取原生 15 分钟线数据（实时尾部补尾，不写库）。

    [P0-4] 冻结行情周期合同：15m 实时尾部必须使用 Pytdx 原生 15m，
    禁止从 1m 聚合（CHANGE-20260724-003）。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        count: 拉取条数（默认 16 = 1 交易日 15m bar 数）

    Returns:
        DataFrame indexed by trade_time，columns=[open,high,low,close,volume,amount,adj_factor]
        空数据时返回空 DataFrame
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    adapter = get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(adapter.get_15min_bars, symbol, count)
    except Exception as exc:
        logger.warning("Pytdx 拉取原生 15m 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if raw_df.empty:
        return raw_df

    raw_df = raw_df.copy()
    raw_df = raw_df.set_index("datetime")
    raw_df.index.name = "trade_time"
    if "adj_factor" not in raw_df.columns:
        raw_df["adj_factor"] = 1.0
    return raw_df


async def fetch_60min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 4,
) -> pd.DataFrame:
    """从 Pytdx 拉取原生 60 分钟线数据（实时尾部补尾，不写库）。

    [P0-4] 冻结行情周期合同：1h 实时尾部必须使用 Pytdx 原生 60m，
    禁止从 1m 聚合（CHANGE-20260724-003）。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        count: 拉取条数（默认 4 = 1 交易日 60m bar 数）

    Returns:
        DataFrame indexed by trade_time，columns=[open,high,low,close,volume,amount,adj_factor]
        空数据时返回空 DataFrame
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    adapter = get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(adapter.get_60min_bars, symbol, count)
    except Exception as exc:
        logger.warning("Pytdx 拉取原生 60m 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if raw_df.empty:
        return raw_df

    raw_df = raw_df.copy()
    raw_df = raw_df.set_index("datetime")
    raw_df.index.name = "trade_time"
    if "adj_factor" not in raw_df.columns:
        raw_df["adj_factor"] = 1.0
    return raw_df


async def fetch_today_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    today: date,
) -> pd.DataFrame:
    """从 Pytdx 拉取今日日线（partial daily，实时尾部补尾，不写库）。

    [P0-4] 冻结行情周期合同：1d 实时尾部必须使用 Pytdx 原生日线，
    禁止从 1m 聚合（CHANGE-20260724-003）。

    Pytdx 日线在交易时段内为 partial bar（随实时成交更新 OHLCV），
    收盘后为完整日线。与 DB 日线按 trade_date 去重时保留 Pytdx 版本（最新）。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        today: 今日日期（上海时区）

    Returns:
        DataFrame indexed by trade_date，columns=[open,high,low,close,volume,amount,adj_factor]
        空数据时返回空 DataFrame
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    adapter = get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(
            adapter.get_daily_bars, symbol, today, today
        )
    except Exception as exc:
        logger.warning("Pytdx 拉取今日日线条失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if raw_df.empty:
        return raw_df

    raw_df = raw_df.copy()
    raw_df = raw_df.set_index("datetime")
    # 规范化到午夜，与 DB 日线表 trade_date 对齐
    raw_df.index = raw_df.index.normalize()
    raw_df.index.name = "trade_date"
    if "adj_factor" not in raw_df.columns:
        raw_df["adj_factor"] = 1.0
    return raw_df


async def _fetch_provider_intraday_direct(
    *,
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    required_count: int | None,
    end: date | datetime | None = None,
    completed_only: bool = False,
    now: datetime | None = None,
) -> pd.DataFrame:
    """[PANJI-INTRADAY-DIRECT-SOURCE] PROVIDER_DIRECT 取数（纯 provider，不读 DB）。

    只调用 provider 原生周期抓取（``fetch_15min_bars`` / ``fetch_60min_bars``）：
    - **不调用** ``_query_15min_bars`` / ``_query_60min_bars``（不读 DB 分钟线）；
    - **不调用** ``_merge_bars(db_bars, provider_bars)``（不混合来源）；
    - provider 抛错时**不捕获**，异常向上传播（禁止静默 fallback 到 stale DB）。

    复权 owner 不变：本函数返回 provider 原生 bars（``adj_factor=1.0``），
    由 ``get_bars`` 中**既有**的统一 adjustment pipeline 施加 qfq，本函数内不复权。

    ``completed_only`` 语义：15m 的 completed owner 是 ``_filter_unfinished_15m_bars``
    （MDAS 唯一 owner，本函数委托它，不另写第二套 cutoff 规则）。1h 仓库内没有对应的
    forming-bar owner，本函数不新建规则；生产 1h 路径使用 ``completed_only=False``
    （Node 只用 15m），因此不受影响。

    Args:
        session: 异步 DB 会话（仅用于 symbol 解析）
        instrument_id: 标的 UUID
        timeframe: 仅 15m / 1h
        required_count: 请求条数；None 时取 ``_PROVIDER_DIRECT_DEFAULT_COUNT``
        end: 时间视界上界（防御性裁剪，禁止返回 end 之后的 bar）
        completed_only: 是否剔除 still-forming 15m bar
        now: 当前时间（completed 判定用）

    Returns:
        DataFrame（可能为空），index=trade_time，columns=[open,high,low,close,volume,amount,adj_factor]

    Raises:
        ValueError: timeframe 不是 provider 原生日内周期
        PytdxSourceError: provider 重连耗尽后仍失败（不吞没）
    """
    if timeframe not in _PROVIDER_DIRECT_TIMEFRAMES:
        raise ValueError(
            f"provider_direct 只支持 provider 原生日内周期 "
            f"{sorted(_PROVIDER_DIRECT_TIMEFRAMES)}, got {timeframe!r}"
        )

    if required_count is None:
        required_count = _PROVIDER_DIRECT_DEFAULT_COUNT[timeframe]

    if timeframe == "15m":
        bars = await fetch_15min_bars(session, instrument_id, count=required_count)
    else:  # 1h
        bars = await fetch_60min_bars(session, instrument_id, count=required_count)

    if bars is None or bars.empty:
        return pd.DataFrame()

    bars = bars.sort_index()
    bars = bars[~bars.index.duplicated(keep="last")]

    if completed_only and timeframe == "15m":
        bars = _filter_unfinished_15m_bars(bars, now)

    # 防御性时间视界裁剪：调用方显式给出 end 时不得返回 end 之后的 bar。
    # live 链 end = 当日 23:59（no-op）；关键是杜绝任何 PIT 请求经 provider_direct 读到未来 bar。
    if end is not None and not bars.empty:
        _cutoff = (
            pd.Timestamp(end)
            if isinstance(end, datetime)
            else pd.Timestamp(datetime.combine(end, datetime.max.time()))
        )
        bars = _bars_not_after(bars, _cutoff)

    if len(bars) > required_count:
        bars = bars.tail(required_count)

    return bars


# ===== 数据合并与聚合 =====


def _merge_bars(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并两个 DataFrame，去重保留最新，按时间排序。"""
    if existing.empty:
        return new.copy()
    if new.empty:
        return existing.copy()

    merged = pd.concat([existing, new])
    merged = merged[~merged.index.duplicated(keep="last")]
    merged = merged.sort_index()
    return merged


def _aggregate_minute_to_target(minute_df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """[非生产工具] 将 1 分钟线聚合为 15m/1h。

    [P0-4] 冻结行情周期合同后，生产链禁止使用 1m→15m / 1m→60m 聚合
    （CHANGE-20260724-003）。此函数仅保留为非生产工具（__main__ 自测、
    研究脚本），无生产调用方。生产链请使用 fetch_15min_bars / fetch_60min_bars
    拉 Pytdx 原生周期。
    """
    freq = _TARGET_FREQ[timeframe]
    agg = minute_df.resample(freq, closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
        "adj_factor": "last",
    })
    agg = agg.dropna(subset=["close"])
    return agg


def _aggregate_minute_to_daily(
    minute_df: pd.DataFrame,
    adj_factor: float | None = None,
) -> pd.DataFrame:
    """[非生产工具] 将当日 1 分钟线聚合成一根 partial daily bar。

    [P0-4] 冻结行情周期合同后，生产链禁止使用 1m→1d 聚合
    （CHANGE-20260724-003）。此函数仅保留为非生产工具（__main__ 自测、
    研究脚本），无生产调用方。生产链请使用 fetch_today_daily_bars 拉
    Pytdx 原生日线。

    规则：
    - open = 第一根 1m open
    - high = max(high)
    - low = min(low)
    - close = 最后一根 1m close
    - volume/amount = sum
    - adj_factor = 最后一根 adj_factor（或传入的复权因子）
    - index = 最后一根 1m 时间（表示 last_live_bar_time）

    调用方需保证 minute_df 只含已完成 1m bar。

    Args:
        minute_df: 已完成 1m bar DataFrame
        adj_factor: 若指定，对 open/high/low/close 应用该复权因子；
                    用于 qfq 场景，保证 partial daily bar 与复权后的历史日线连续。
    """
    if minute_df.empty:
        return minute_df
    # [mdas] - partial daily 的索引使用 pd.Timestamp(date)，与 DB 日线表（trade_date）保持一致，
    # 同时保证索引类型为 DatetimeIndex，避免与 date 对象混合导致 sort_index 失败。
    trade_date = minute_df.index[-1].date() if hasattr(minute_df.index[-1], "date") else pd.Timestamp(minute_df.index[-1]).date()
    factor = adj_factor if adj_factor is not None else float(minute_df["adj_factor"].iloc[-1])
    partial = pd.DataFrame({
        "open": [float(minute_df["open"].iloc[0]) * factor],
        "high": [float(minute_df["high"].max()) * factor],
        "low": [float(minute_df["low"].min()) * factor],
        "close": [float(minute_df["close"].iloc[-1]) * factor],
        "volume": [float(minute_df["volume"].sum())],
        "amount": [float(minute_df["amount"].sum())],
        "adj_factor": [factor],
    }, index=[pd.Timestamp(trade_date)])
    partial.index.name = "trade_date"
    return partial


def _filter_unfinished_daily_bars(
    df: pd.DataFrame,
    now: datetime | None = None,
) -> pd.DataFrame:
    """过滤当日未完成日线 Bar。"""
    if df.empty:
        return df
    if now is None:
        now = now_shanghai()
    today = now.date()
    latest_date = df.index[-1].date()
    if latest_date == today and now.time() < _DAILY_CLOSE_TIME:
        df = df[df.index.date < today]
    return df


def _apply_intraday_qfq(
    adj_service: Any,
    bars_df: pd.DataFrame,
    factor_df: pd.DataFrame,
    *,
    adj: str,
    adjustment_as_of: date | None,
) -> tuple[pd.DataFrame, str | None]:
    """日内 bars 的 qfq 施加 —— **[复权唯一 owner，禁止复制第二套]**。

    [PANJI-INTRADAY-DIRECT-SOURCE] 改变 source **不改变 adjustment owner**：
    provider 原生 bar（``adj_factor=1.0``，未复权）与 hybrid 的 DB 分钟线
    **必须**经同一份实现施加前复权。因此 provider_direct 与 hybrid 两条分支
    都调用本函数（而不是各自内联一段 qfq）——否则一旦两边漂移就会二次复权，
    或出现「provider_direct 忘记复权却仍声明 adj=qfq」。

    传入 ``adj_service``（``AdjustmentFactorService`` 实例）而不是在模块级新建：
    ``get_bars`` 内的调用方复用同一实例（含会话级缓存语义），不得另建。

    Args:
        adj_service: ``AdjustmentFactorService`` 实例（由调用方提供）
        bars_df: 待复权的日内 bars（index=trade_time）
        factor_df: 权威日线复权因子序列
        adj: 复权方式；非 "qfq" 时原样返回
        adjustment_as_of: 复权锚点

    Returns:
        ``(bars_df, degraded_reason)``。``degraded_reason`` 非 None 表示复权失败
        （调用方据此置 degraded / data_source="degraded"），bars 保持未复权原值。
    """
    if adj != "qfq" or bars_df.empty or factor_df.empty:
        return bars_df, None
    try:
        return (
            adj_service.apply_qfq(
                bars_df, factor_df, as_of=adjustment_as_of, intraday=True,
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - 复权失败必须降级而非抛给调用方
        return bars_df, f"qfq failed: {exc}"


def _finalize_bars(
    df: pd.DataFrame,
    timeframe: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    """排序、去重、过滤未完成 bar。"""
    if df.empty:
        return df
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if timeframe == "1d":
        df = _filter_unfinished_daily_bars(df, now)
    return df


# ===== 15m completed 语义（唯一 owner） =====


def _filter_unfinished_15m_bars(
    bars: pd.DataFrame,
    now: datetime | None = None,
) -> pd.DataFrame:
    """丢弃仍处于 forming 状态的 15m bar。

    [USER-FIX-3 / C corrective] 本函数是 15m completed 语义的**唯一 owner**。
    MDAS 在**实时尾部合并之前**调用它（见 get_bars 日内分支），从而保证
    ``completed_only=True`` 的返回结果**本身**全部为已完成 bar：
    forming bar 不会进入 ``tail(limit)`` / ``source_bar_hash`` / ``actual_count``
    / coverage，因此不会挤占 limit 窗口槽位（4000 → 3999）。

    ``NodeClusterInputProvider._filter_unfinished_15m_bars`` 仍会再调用一次作为
    defensive guard（委托到本函数）；过滤是幂等的，正常路径下为 no-op。

    语义（与仓库冻结的 15m 合同一致）：
    - right-label：trade_time = bar **结束**时间；
    - cutoff = ``floor(now, 15min)``，丢弃 end > cutoff 的 bar；
    - A 股 15m 网格（09:30 / 11:30 / 13:00 / 15:00）均为 15 分钟整数倍，
      ``floor("15min")`` 在午休空洞处同样正确（DB 不存午休伪 bar），
      无需在代码内硬编码交易时段，也不会拼出跨午休 bar；
    - 时区：bars 索引多为 tz-naive（本地时间），now 来自 ``now_shanghai()``
      是 tz-aware，比较前先归一化，避免 tz-naive/tz-aware 冲突；
    - fail-closed：now 不可用时丢弃最后一根 bar——宁可少算，
      绝不把未完成 bar 计入 completed 合同。

    Examples:
        now=10:07 → cutoff=10:00 → 保留 09:45 / 10:00，丢弃 10:15
        now=13:07 → cutoff=13:00 → 丢弃 13:15（13:00–13:15 forming）
        now=13:16 → cutoff=13:15 → 保留 13:15
    """
    if bars is None or bars.empty:
        return bars
    if now is None:
        return bars.iloc[:-1] if len(bars) > 0 else bars
    return _bars_not_after(bars, pd.Timestamp(now).floor("15min"))


def _bars_not_after(bars: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    """返回 index <= cutoff 的 bars（自动归一化 tz-naive / tz-aware 再比较）。

    bars 索引多为 tz-naive（本地时间），而 cutoff 可能来自 ``now_shanghai()``
    （tz-aware）；直接比较会触发 tz-naive/tz-aware 冲突，因此先归一化。
    """
    if bars is None or bars.empty:
        return bars
    idx = bars.index
    idx_tz = getattr(idx, "tz", None)
    cutoff_tz = cutoff.tzinfo
    if idx_tz is None and cutoff_tz is not None:
        cutoff = cutoff.tz_localize(None)
    elif idx_tz is not None and cutoff_tz is None:
        cutoff = cutoff.tz_localize(idx_tz)
    elif (
        idx_tz is not None
        and cutoff_tz is not None
        and str(idx_tz) != str(cutoff_tz)
    ):
        cutoff = cutoff.tz_convert(idx_tz)
    return bars[bars.index <= cutoff]


def _resolve_realtime_horizon(
    end: date | datetime,
    now: datetime,
) -> tuple[bool, pd.Timestamp]:
    """解析「实时尾部」的可用性与时间视界（point-in-time 合同）。

    [USER-FIX-3 / C] 关键原则：**「允许 fresh tail」≠「无视调用方请求的时间截止点」**。

    四链共享 Provider（NodeClusterInputProvider）同时服务盘中 Monitor 与
    FeatureSnapshot 历史快照；后者的点是 ``end_date=trade_date``（过去日期）。
    若仍无条件拉取 current realtime 尾部，会把**今天**的 15m 混进历史快照 ——
    典型 future leakage，而且不会报错（算法会算出一个看起来合理的 Profile）。

    规则（仅用于 15m fresh-tail 路径）：
    - ``end`` 的日期 < 今天 → 历史 point-in-time：**禁止访问 realtime source**
      （不做网络 I/O；历史 replay / snapshot 必须与外部源隔离且可复现）；
    - 否则允许拉取，但结果必须裁到 ``end``（覆盖同一交易日内的精确 datetime 请求，
      例如 end=今天 09:45 时不得包含 10:00 的 bar）。

    Args:
        end: 已归一化的请求结束时间（日内周期为 datetime）
        now: 当前上海时间

    Returns:
        (realtime_allowed, horizon)：horizon 为允许进入结果的最大 timestamp。
    """
    end_ts = pd.Timestamp(end)
    if end_ts.date() < now.date():
        return False, end_ts
    return True, end_ts


# ===== 复权因子哈希（跨调用方一致性校验） =====


def _compute_adj_factor_hash(factor_df: pd.DataFrame) -> str:
    """计算复权因子序列哈希（trade_date|adj_factor 拼接的 SHA256 前 16 字符）。

    用于跨调用方（bars API / indicator / feature snapshot）的因子一致性校验。
    与 compute_source_bar_hash 配对：source_bar_hash 校验 OHLCV，adj_factor_hash 校验因子。

    Args:
        factor_df: 复权因子 DataFrame，columns=[trade_date, adj_factor]

    Returns:
        SHA256 hexdigest 前 16 字符；空 DataFrame 返回空字符串
    """
    if factor_df is None or factor_df.empty:
        return ""
    parts: list[str] = []
    for _, row in factor_df.iterrows():
        td = row["trade_date"]
        td_str = td.strftime("%Y-%m-%d") if hasattr(td, "strftime") else str(td)
        parts.append(f"{td_str}|{row['adj_factor']}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ===== Redis 短缓存 =====


def _cache_key(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    include_realtime: bool,
    completed_only: bool,
    start_date: date | datetime | None,
    end_date: date | datetime | None,
    limit: int | None,
    warmup_bars: int,
    adjustment_as_of: date | None,
    allow_backfill: bool = True,
    fresh_intraday_tail: bool = False,
    source_policy: MarketDataSourcePolicy = MarketDataSourcePolicy.HYBRID,
) -> str:
    """构建缓存键，包含所有影响结果的参数 + 契约版本（自动隔离新旧缓存）。

    [PANJI-INTRADAY-DIRECT-SOURCE] ``source_policy`` 必须参与缓存键：不同 source policy
    会返回**不同数据**（hybrid = DB∪provider 合并；provider_direct = 纯 provider；
    db_only = 纯 DB），若不隔离则 provider_direct 会命中旧 hybrid 缓存。
    """
    start = start_date.isoformat() if start_date is not None else "_"
    end = end_date.isoformat() if end_date is not None else "_"
    as_of_str = adjustment_as_of.isoformat() if adjustment_as_of is not None else "_"
    limit_str = str(limit) if limit is not None else "_"
    return (
        f"{_REDIS_CACHE_PREFIX}:"
        f"{instrument_id}:{timeframe}:{adj}:{include_realtime}:{completed_only}:"
        f"{start}:{end}:{limit_str}:{warmup_bars}:{as_of_str}:"
        f"{int(allow_backfill)}:{int(fresh_intraday_tail)}:"
        f"{source_policy.value}:"
        f"{_MARKET_DATA_CONTRACT_VERSION}"
    )


def _serialize_result(result: BarAggregationResult) -> str:
    """将结果序列化为 JSON 字符串。"""
    def _df_to_payload(df: pd.DataFrame) -> dict[str, Any]:
        if df.empty:
            return {"index": [], "columns": list(df.columns), "data": []}
        payload = df.to_dict(orient="split")
        payload["index"] = [idx.isoformat() for idx in df.index]
        return payload

    payload = {
        "bars": _df_to_payload(result.bars),
        "warmup_bars_full": (
            _df_to_payload(result.warmup_bars_full)
            if result.warmup_bars_full is not None
            else None
        ),
        "data_source": result.data_source,
        "as_of": result.as_of.isoformat(),
        "is_partial": result.is_partial,
        "last_persisted_bar_time": (
            result.last_persisted_bar_time.isoformat()
            if result.last_persisted_bar_time is not None
            else None
        ),
        "last_live_bar_time": (
            result.last_live_bar_time.isoformat()
            if result.last_live_bar_time is not None
            else None
        ),
        "freshness_seconds": result.freshness_seconds,
        "degraded": result.degraded,
        "degraded_reason": result.degraded_reason,
        "market_data_contract_version": result.market_data_contract_version,
        "source_bar_hash": result.source_bar_hash,
        "adj_factor_hash": result.adj_factor_hash,
        "adjustment_as_of": (
            result.adjustment_as_of.isoformat()
            if result.adjustment_as_of is not None
            else None
        ),
        "completed_through": (
            result.completed_through.isoformat()
            if result.completed_through is not None
            else None
        ),
        # [CP-V3-A] count-aware 字段
        "requested_count": result.requested_count,
        "actual_count": result.actual_count,
        "coverage_start": (
            result.coverage_start.isoformat()
            if result.coverage_start is not None
            else None
        ),
        "coverage_end": (
            result.coverage_end.isoformat()
            if result.coverage_end is not None
            else None
        ),
        "history_exhausted": result.history_exhausted,
        # [CP-V3-A2] 迭代回补诊断字段
        "backfill_rounds": result.backfill_rounds,
        "coverage_reason": result.coverage_reason,
        # [CHANGE-20260730-P0] 修复：序列化 latest_daily_quote（v5 契约）
        # 旧 v4 缓存因版本不匹配自动失效；新缓存完整保留 latest_daily_quote
        "latest_daily_quote": result.latest_daily_quote,
    }
    return json.dumps(payload)


def _deserialize_result(raw: str) -> BarAggregationResult | None:
    """从 JSON 字符串反序列化结果。"""
    def _payload_to_df(payload_df: dict[str, Any] | None) -> pd.DataFrame:
        if payload_df is None:
            return pd.DataFrame()
        index = pd.to_datetime(payload_df["index"])
        df = pd.DataFrame(
            payload_df["data"],
            index=index,
            columns=payload_df["columns"],
        )
        for col in df.columns:
            if col in _BAR_COLUMNS:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    try:
        payload = json.loads(raw)
        bars = _payload_to_df(payload["bars"])
        warmup = (
            _payload_to_df(payload.get("warmup_bars_full"))
            if payload.get("warmup_bars_full") is not None
            else None
        )

        return BarAggregationResult(
            bars=bars,
            data_source=payload["data_source"],
            as_of=datetime.fromisoformat(payload["as_of"]),
            is_partial=payload["is_partial"],
            last_persisted_bar_time=(
                pd.Timestamp(payload["last_persisted_bar_time"])
                if payload["last_persisted_bar_time"] is not None
                else None
            ),
            last_live_bar_time=(
                pd.Timestamp(payload["last_live_bar_time"])
                if payload["last_live_bar_time"] is not None
                else None
            ),
            freshness_seconds=payload["freshness_seconds"],
            degraded=payload["degraded"],
            degraded_reason=payload["degraded_reason"],
            cache_hit=payload.get("cache_hit", False),
            warmup_bars_full=warmup,
            market_data_contract_version=payload.get(
                "market_data_contract_version", _MARKET_DATA_CONTRACT_VERSION
            ),
            source_bar_hash=payload.get("source_bar_hash", ""),
            adj_factor_hash=payload.get("adj_factor_hash", ""),
            adjustment_as_of=(
                date.fromisoformat(payload["adjustment_as_of"])
                if payload.get("adjustment_as_of") is not None
                else None
            ),
            completed_through=(
                pd.Timestamp(payload["completed_through"])
                if payload.get("completed_through") is not None
                else None
            ),
            # [CP-V3-A] count-aware 字段（向后兼容：旧缓存无这些字段时用默认值）
            requested_count=payload.get("requested_count"),
            actual_count=payload.get("actual_count", len(bars)),
            coverage_start=(
                pd.Timestamp(payload["coverage_start"])
                if payload.get("coverage_start") is not None
                else None
            ),
            coverage_end=(
                pd.Timestamp(payload["coverage_end"])
                if payload.get("coverage_end") is not None
                else None
            ),
            history_exhausted=payload.get("history_exhausted", False),
            # [CP-V3-A2] 迭代回补诊断字段（向后兼容：旧缓存无这些字段时用默认值）
            backfill_rounds=payload.get("backfill_rounds", 0),
            coverage_reason=payload.get("coverage_reason", ""),
            # [CHANGE-20260730-P0] 修复：反序列化 latest_daily_quote（v5 契约）
            # 旧 v4 缓存因 cache_key 版本不匹配不会命中；新缓存 latest_daily_quote 完整保留
            latest_daily_quote=payload.get("latest_daily_quote"),
        )
    except Exception as exc:
        logger.warning("MDAS 缓存反序列化失败: %s", exc)
        return None


def _cache_get(cache_key: str) -> BarAggregationResult | None:
    """从 Redis 读取缓存结果。"""
    from app.config import get_settings

    settings = get_settings()
    if not settings.bars_redis_cache_enabled:
        return None
    try:
        client = get_sync_redis()
        raw = client.get(cache_key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return _deserialize_result(raw)
    except Exception as exc:
        logger.warning("MDAS 缓存读取失败: %s", exc)
        return None


def _cache_set(
    cache_key: str,
    result: BarAggregationResult,
    ttl: int | None = None,
) -> None:
    """写入 Redis 缓存。"""
    from app.config import get_settings

    settings = get_settings()
    if not settings.bars_redis_cache_enabled:
        return
    if ttl is None:
        ttl = random.randint(_MIN_CACHE_TTL, _MAX_CACHE_TTL)
    try:
        client = get_sync_redis()
        client.set(cache_key, _serialize_result(result), ex=ttl)
    except Exception as exc:
        logger.warning("MDAS 缓存写入失败: %s", exc)


# ===== 主服务 =====


async def _build_daily_aggregation(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    daily_df: pd.DataFrame,
    factor_df: pd.DataFrame,
    expected: Any,
    now: datetime,
    *,
    timeframe: str,
    adj: str,
    include_realtime: bool,
    completed_only: bool,
    start: date,
    end: date,
    limit: int | None,
    warmup_bars: int,
    adjustment_as_of: date | None,
    allow_backfill: bool = True,
) -> BarAggregationResult:
    """[CHANGE-20260804-FS] 从预批量读取的 daily_df / factor_df 构造日线类聚合结果。

    allow_backfill: [CHANGE-20260816-003] 与单股 get_bars 对齐的 strict DB-only 开关
    （batch 默认 True）。False 时 DB historical daily 数据不足也绝不调用 external
    daily provider（fetch_daily_bars zero calls），按现有 MDAS contract fail-closed，
    避免 backfill 偷偷启动第二个 pytdx source connection。

    复用 get_bars 日线路径的完整 bars/复权/诊断合同，但不触发任何 per-instrument
    DB 查询（bars 与 adj_factor 已由调用方批量读取）。仅在 need_tail（今日 partial
    daily 缺失）时按需 fetch_daily_bars，属罕见路径，不改变主路径的批量性质。
    """
    bars_df = pd.DataFrame()
    data_source = "db"
    is_partial = False
    last_persisted_bar_time: pd.Timestamp | None = None
    last_live_bar_time: pd.Timestamp | None = None
    degraded = False
    degraded_reason: str | None = None

    adj_factor_hash = _compute_adj_factor_hash(factor_df)

    backfill_rounds = 0
    coverage_reason = "daily_no_backfill"

    if not daily_df.empty:
        last_persisted_bar_time = pd.Timestamp(daily_df.index[-1])

    # [CHANGE-20260805-CP4A / P0-01] 历史点回放补尾判断统一为「预期最后完成日」与「请求 end」
    # 的较小者（与单股 get_bars 路径一致）。生产 now==处理日，min 取 now_expected，行为不变；
    # 回放时请求 end 在过去（如 07-30），min 取历史 end，DB 已完整覆盖则不触发 pytdx。
    # 这比仅 verification_replay 特判更通用，也避免回放误判触发外部行情源。
    now_expected = await _call_expected_last_completed_daily_bar(session, now) \
        if expected is None else expected
    effective_expected = min(now_expected, end)

    need_tail = daily_df.empty or daily_df.index[-1].date() < effective_expected  # type: ignore[attr-defined]
    # [CHANGE-20260816-003] strict DB-only：allow_backfill=False 时绝不调用 external
    # daily provider（与单股 get_bars 合同一致），避免 backfill 启动第二个 pytdx 连接。
    if need_tail and allow_backfill:
        if _is_verification_replay():
            # 验证回放禁止外部行情源，DB 数据不足 → fail-closed 并输出精确缺口
            available_end = (
                pd.Timestamp(daily_df.index[-1]).date().isoformat()
                if not daily_df.empty
                else None
            )
            raise VerificationExternalFetchBlockedError(
                timeframe=timeframe,
                reason="DB 日线未覆盖到请求 end（verification_replay 禁止 pytdx）",
                db_row_count=len(daily_df) if not daily_df.empty else 0,
                completed_through=available_end,
                required_end=end.isoformat(),
                available_end=available_end,
            )
        try:
            tail_df = await fetch_daily_bars(session, instrument_id, start, end)  # type: ignore[arg-type]
            if not tail_df.empty:
                daily_df = _merge_bars(daily_df, tail_df)
                data_source = "hybrid"
                last_live_bar_time = pd.Timestamp(tail_df.index[-1])
        except Exception as exc:
            degraded = True
            degraded_reason = f"pytdx daily fallback failed: {exc}"
            data_source = "degraded"

    if timeframe in ("1w", "1mo") and include_realtime and _is_trading_hours(now):
        try:
            is_trading_day = await is_trading_day_async(session, now.date())
            session_name = compute_market_session(now, is_trading_day)
            if session_name in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON):
                partial_daily = await fetch_today_daily_bars(session, instrument_id, now.date())
                if not partial_daily.empty:
                    daily_df = _merge_bars(daily_df, partial_daily)
                    if data_source == "db":
                        data_source = "hybrid"
                    is_partial = True
                    last_live_bar_time = pd.Timestamp(partial_daily.index[-1])
                else:
                    degraded = True
                    degraded_reason = "realtime_1d_empty_in_trading_hours"
                    if data_source != "degraded":
                        data_source = "degraded"
        except Exception as exc:
            logger.warning(
                "1w/1mo partial daily 合并失败 instrument_id=%s: %s", instrument_id, exc,
            )
            degraded = True
            degraded_reason = f"pytdx partial daily failed: {exc}"
            data_source = "degraded"

    if adj == "qfq" and not daily_df.empty and not factor_df.empty:
        try:
            daily_df = AdjustmentFactorService().apply_qfq(
                daily_df, factor_df, as_of=adjustment_as_of, intraday=False
            )
        except Exception as exc:
            degraded = True
            degraded_reason = f"qfq failed: {exc}"
            data_source = "degraded"

    # [FS-CONTRACT 2026-08-04] 与单股 get_bars 对齐：qfq 之后统一 finalize
    # （排序 + 去重 + 1d 过滤未完成 bar），不维护两份近似实现。
    if not daily_df.empty:
        daily_df = _finalize_bars(daily_df, timeframe, now)

    if timeframe == "1w":
        bars_df = aggregate_kline(daily_df, "1w") if not daily_df.empty else daily_df
    elif timeframe == "1mo":
        bars_df = aggregate_kline(daily_df, "1mo") if not daily_df.empty else daily_df
    else:
        bars_df = daily_df

    completed_through = last_persisted_bar_time
    pre_limit_count = len(bars_df) if not bars_df.empty else 0
    bars_df_full = bars_df

    warmup_bars_full: pd.DataFrame | None = None
    if warmup_bars > 0 and not bars_df.empty:
        full_count = (limit or 0) + warmup_bars
        warmup_bars_full = (
            bars_df.tail(full_count) if full_count <= len(bars_df) else bars_df
        )
    if limit is not None and not bars_df.empty:
        bars_df = bars_df.tail(limit)

    source_bar_hash = (
        compute_source_bar_hash(bars_df, timeframe) if not bars_df.empty else ""
    )

    actual_count = len(bars_df) if not bars_df.empty else 0
    coverage_start: pd.Timestamp | None = (
        pd.Timestamp(bars_df.index[0]) if not bars_df.empty else None
    )
    coverage_end: pd.Timestamp | None = (
        pd.Timestamp(bars_df.index[-1]) if not bars_df.empty else None
    )
    history_exhausted = limit is not None and pre_limit_count < limit

    latest_daily_quote: dict | None = None
    try:
        if timeframe in ("1w", "1mo"):
            _qdf = daily_df
        else:
            _qdf = bars_df_full
        if _qdf is not None and not _qdf.empty:
            _latest = _qdf.iloc[-1]
            _prev_close = float(_qdf.iloc[-2]["close"]) if len(_qdf) >= 2 else None
            _cp = float(_latest["close"])
            latest_daily_quote = {
                "open": float(_latest["open"]),
                "high": float(_latest["high"]),
                "low": float(_latest["low"]),
                "close": _cp,
                "volume": float(_latest["volume"]),
                "amount": float(_latest["amount"]) if "amount" in _qdf.columns else 0.0,
                "prev_close": _prev_close,
                "change_pct": (
                    (_cp - _prev_close) / _prev_close * 100
                    if _prev_close and _prev_close != 0 else 0.0
                ),
            }
    except Exception as exc:
        logger.warning("latest_daily_quote 聚合失败: %s", exc)
        latest_daily_quote = None

    return BarAggregationResult(
        bars=bars_df,
        data_source=data_source,
        as_of=now,
        is_partial=is_partial,
        last_persisted_bar_time=last_persisted_bar_time,
        last_live_bar_time=last_live_bar_time,
        freshness_seconds=0.0,
        degraded=degraded,
        degraded_reason=degraded_reason,
        warmup_bars_full=warmup_bars_full,
        source_bar_hash=source_bar_hash,
        adj_factor_hash=adj_factor_hash,
        adjustment_as_of=adjustment_as_of,
        completed_through=completed_through,
        requested_count=limit,
        actual_count=actual_count,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        history_exhausted=history_exhausted,
        backfill_rounds=backfill_rounds,
        coverage_reason=coverage_reason,
        latest_daily_quote=latest_daily_quote,
    )


# ---------------------------------------------------------------------------
# Canonical completed 1m qfq（realtime SMC 权威输入，PART 4）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalCompletedQfqMinute:
    """单根**已完成** 1m bar 的不可变快照（qfq 坐标）。"""

    bar_time: str  # timezone-aware ISO8601，如 2026-09-14T09:31:00+08:00
    session_key: str  # 上海交易日 YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if not isinstance(self.bar_time, str) or not self.bar_time:
            raise ValueError("bar_time must be non-empty str")
        raw = datetime.fromisoformat(self.bar_time)
        if raw.tzinfo is None:
            raise ValueError("bar_time must be timezone-aware")
        if not isinstance(self.session_key, str) or not self.session_key:
            raise ValueError("session_key must be non-empty str")
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite number")

        # session invariant：session_key 必须是 bar_time 的上海日期，且 label 必须 canonical
        parsed = _to_shanghai_aware_timestamp(self.bar_time)
        if self.session_key != parsed.date().isoformat():
            raise ValueError("session_key must equal Shanghai date of bar_time")
        if not _is_canonical_a_share_1m_label(parsed):
            raise ValueError("bar_time outside canonical A-share 1m labels")


@dataclass(frozen=True)
class CanonicalCompletedQfqMinutes:
    """MDAS 权威产出的 completed qfq 1m 输入 + proof。

    proof-bearing：一旦 ``qfq_proven`` 为 True，其对应 bars 必须不可再被原地修改，
    因此 bars 是 **tuple row 快照**（禁止持有 DataFrame）。
    """

    bars: tuple[CanonicalCompletedQfqMinute, ...]
    qfq_proven: bool
    qfq_reason: str
    sequence_proven: bool
    sequence_reason: str
    source_bar_hash: str
    adj_factor_hash: str
    latest_completed_bar_time: str | None

    def __post_init__(self) -> None:
        if type(self.qfq_proven) is not bool:
            raise ValueError("qfq_proven must be bool")
        if type(self.sequence_proven) is not bool:
            raise ValueError("sequence_proven must be bool")
        for name, value in (
            ("qfq_reason", self.qfq_reason),
            ("sequence_reason", self.sequence_reason),
            ("source_bar_hash", self.source_bar_hash),
            ("adj_factor_hash", self.adj_factor_hash),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{name} must be str")
        if not isinstance(self.bars, tuple):
            raise ValueError("bars must be tuple")
        for i, row in enumerate(self.bars):
            if not isinstance(row, CanonicalCompletedQfqMinute):
                raise ValueError(f"bars[{i}] must be CanonicalCompletedQfqMinute")
        if self.latest_completed_bar_time is not None:
            if not isinstance(self.latest_completed_bar_time, str) or not self.latest_completed_bar_time:
                raise ValueError("latest_completed_bar_time must be None or non-empty str")
            parsed = datetime.fromisoformat(self.latest_completed_bar_time)
            if parsed.tzinfo is None:
                raise ValueError("latest_completed_bar_time must be timezone-aware")

        # 自洽：latest 必须是最后一根；空 bars 必须 latest=None
        if self.bars:
            if self.latest_completed_bar_time != self.bars[-1].bar_time:
                raise ValueError("latest_completed_bar_time must equal last bar_time")
        elif self.latest_completed_bar_time is not None:
            raise ValueError("empty bars require latest_completed_bar_time=None")

        # proven 前置条件（不在 DTO 内重算 qfq proof）
        if self.sequence_proven and not self.bars:
            raise ValueError("sequence_proven=True requires non-empty bars")
        if self.qfq_proven:
            if not self.bars:
                raise ValueError("qfq_proven=True requires non-empty bars")
            if not self.adj_factor_hash:
                raise ValueError("qfq_proven=True requires adj_factor_hash")


def _to_shanghai_aware_timestamp(value: Any) -> pd.Timestamp:
    """**唯一** Shanghai-aware 归一化入口（naive 视为上海本地墙钟）。

    分钟 DB index 是 naive 上海墙钟（bar_repository `tz_localize(None)`），
    persisted cursor 是 timezone-aware；两者必须先经本函数归一化才能比较。
    """
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(SHANGHAI_TZ)
    else:
        ts = ts.tz_convert(SHANGHAI_TZ)
    return ts


def _minute_index_to_aware_iso(value: Any) -> str:
    """分钟 index → timezone-aware ISO8601。"""
    return _to_shanghai_aware_timestamp(value).isoformat()


def _is_canonical_a_share_1m_label(value: Any) -> bool:
    """A 股 canonical 1m right-label 合同：09:31–11:30 与 13:01–15:00（整分钟）。"""
    ts = _to_shanghai_aware_timestamp(value)
    if ts.second != 0 or ts.microsecond != 0:
        return False
    minute = ts.hour * 60 + ts.minute
    return (9 * 60 + 31 <= minute <= 11 * 60 + 30) or (13 * 60 + 1 <= minute <= 15 * 60)


class MarketDataAggregationService:
    """行情聚合统一入口。"""

    # ------------------------------------------------------------------
    # PART 4：canonical completed 1m qfq（realtime SMC 权威输入）
    # ------------------------------------------------------------------

    async def get_completed_qfq_minutes_for_monitor(
        self,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        *,
        after_bar_time: datetime | None,
        target_epoch: str,
    ) -> CanonicalCompletedQfqMinutes:
        """返回 canonical completed 1m qfq bars + qfq / sequence proof。

        复用唯一行情出口 ``get_bars``（DB + realtime tail → qfq → ``_finalize_bars()``），
        **禁止二次 ``iloc[:-1]``**：未完成 bar 已由 ``_finalize_bars`` 过滤。
        qfq / sequence proof 由本 owner 产出，caller 不得传 bool。
        """
        business_date = shanghai_business_date()
        if after_bar_time is None:
            start_dt: datetime = datetime.combine(
                business_date, dt_time(9, 30), tzinfo=SHANGHAI_TZ
            )
        else:
            # persisted correctness state 已规定 cursor 必须 aware；naive 一律拒绝，不猜时区。
            if not isinstance(after_bar_time, datetime) or after_bar_time.tzinfo is None:
                raise ValueError("after_bar_time must be timezone-aware datetime")
            start_dt = after_bar_time

        now = now_shanghai()
        result = await self.get_bars(
            session=session,
            instrument_id=instrument_id,
            timeframe="1m",
            adj="qfq",
            include_realtime=True,
            completed_only=False,
            start_date=start_dt,
            end_date=now,
        )

        cursor_ts: pd.Timestamp | None = None
        if after_bar_time is not None:
            cursor_ts = _to_shanghai_aware_timestamp(after_bar_time)

        # completed 权威：right-label 语义 —— 当前 10:02:37 → 10:02 已完成、10:03 仍在形成。
        # 按 timestamp 排除 forming bar（不依赖 iloc[:-1]，不会误删最后一根 completed bar）。
        completion_cutoff = _to_shanghai_aware_timestamp(now).floor("min")

        selected: list[tuple[pd.Timestamp, Any]] = []
        bars_df = result.bars
        if bars_df is not None and not bars_df.empty:
            for idx, row in bars_df.iterrows():
                idx_ts = _to_shanghai_aware_timestamp(idx)
                if idx_ts > completion_cutoff:
                    continue  # forming bar
                if cursor_ts is not None and idx_ts <= cursor_ts:
                    continue  # 不重复消费 cursor
                selected.append((idx_ts, row))

        if not selected:
            # 正常运行状态（本轮无新 completed bar）→ NO-OP，不是 degraded / 异常
            return CanonicalCompletedQfqMinutes(
                bars=(),
                qfq_proven=False,
                qfq_reason="no new completed bars",
                sequence_proven=False,
                sequence_reason="no new completed bars",
                source_bar_hash="",
                adj_factor_hash=str(getattr(result, "adj_factor_hash", "") or ""),
                latest_completed_bar_time=None,
            )

        selected_df = pd.DataFrame(
            [row for _, row in selected],
            index=pd.DatetimeIndex([idx_ts for idx_ts, _ in selected]),
        )
        rows = [
            CanonicalCompletedQfqMinute(
                bar_time=idx_ts.isoformat(),
                session_key=idx_ts.date().isoformat(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
            for idx_ts, row in selected
        ]

        # proof 必须绑定 SMC 实际消费的 canonical rows（不能证明 A、消费 B）
        qfq_proven, qfq_reason = self._prove_completed_qfq(
            result=result,
            bars_df=selected_df,
            instrument_id=instrument_id,
            target_epoch=target_epoch,
            business_date=business_date,
        )
        sequence_proven, sequence_reason = await self._prove_completed_minute_sequence(
            session,
            after_bar_time=after_bar_time,
            bars=tuple(rows),
        )

        canonical_source_hash = (
            compute_source_bar_hash(selected_df, "1m") if not selected_df.empty else ""
        )

        return CanonicalCompletedQfqMinutes(
            bars=tuple(rows),
            qfq_proven=qfq_proven,
            qfq_reason=qfq_reason,
            sequence_proven=sequence_proven,
            sequence_reason=sequence_reason,
            source_bar_hash=canonical_source_hash,
            adj_factor_hash=str(getattr(result, "adj_factor_hash", "") or ""),
            latest_completed_bar_time=rows[-1].bar_time if rows else None,
        )

    def _prove_completed_qfq(
        self,
        *,
        result: Any,
        bars_df: pd.DataFrame | None,
        instrument_id: uuid.UUID,
        target_epoch: str,
        business_date: date,
    ) -> tuple[bool, str]:
        """qfq 坐标可证明性（fail closed：任一条件不成立 → False）。"""
        if result.degraded:
            return False, f"mdas degraded: {result.degraded_reason}"
        if not result.adj_factor_hash:
            return False, "adj_factor_hash empty"
        if bars_df is None or bars_df.empty:
            return False, "no completed bars"
        if "adj_factor" not in bars_df.columns:
            return False, "adj_factor column missing"
        for value in bars_df["adj_factor"]:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return False, "adj_factor not numeric"
            if not math.isfinite(numeric) or numeric <= 0:
                return False, "adj_factor not finite/positive"

        schedule = AdjustmentFactorService().get_corporate_action_schedule_state(instrument_id)
        if schedule is None:
            return False, "corporate action schedule unavailable (freshness unproven)"
        epoch_date = pd.Timestamp(target_epoch).date()
        if schedule.scanned_as_of < epoch_date:
            return False, "corporate action schedule stale for epoch"
        if schedule.next_event_date is not None and schedule.next_event_date <= business_date:
            return False, "known corporate action reached effective date"
        return True, ""

    async def _prove_minute_edge(
        self,
        session: AsyncSession,
        *,
        prev_dt: datetime,
        prev_session_key: str,
        curr_dt: datetime,
        curr_session_key: str,
    ) -> tuple[bool, str]:
        """**唯一**相邻边规则 owner：same-day +1 / 唯一午休 jump / 跨日 authoritative。"""
        prev_aware = _to_shanghai_aware_timestamp(prev_dt).to_pydatetime()
        curr_aware = _to_shanghai_aware_timestamp(curr_dt).to_pydatetime()

        # 先锁 label 合法性：11:59/12:00/13:00/09:30 等不得因"恰好 +1 分钟"被证明
        if not _is_canonical_a_share_1m_label(prev_aware):
            return False, "previous minute label outside canonical session"
        if not _is_canonical_a_share_1m_label(curr_aware):
            return False, "current minute label outside canonical session"

        if curr_aware <= prev_aware:
            return False, "minute edge not increasing"

        prev_mod = prev_aware.hour * 60 + prev_aware.minute
        curr_mod = curr_aware.hour * 60 + curr_aware.minute

        if prev_session_key == curr_session_key:
            if prev_mod == 11 * 60 + 30 and curr_mod == 13 * 60 + 1:
                return True, ""  # 唯一合法午休 jump
            if curr_mod == prev_mod + 1:
                return True, ""
            return False, "non-contiguous minute progression"

        if (prev_aware.hour, prev_aware.minute) != (15, 0) or (
            curr_aware.hour,
            curr_aware.minute,
        ) != (9, 31):
            return False, "illegal cross-day jump"

        next_day = await get_next_authoritative_trading_day_async(
            session, date.fromisoformat(prev_session_key)
        )
        if next_day is None:
            return False, "next authoritative trading day unprovable"
        if next_day.isoformat() != curr_session_key:
            return False, "cross-day target is not the authoritative next trading day"
        return True, ""

    async def _prove_completed_minute_sequence(
        self,
        session: AsyncSession,
        *,
        after_bar_time: datetime | None,
        bars: tuple[CanonicalCompletedQfqMinute, ...],
    ) -> tuple[bool, str]:
        """证明 completed 1m 序列连续（冻结 right-label contract：09:31–11:30 / 13:01–15:00）。"""
        if not bars:
            return False, "no completed bars"
        try:
            parsed = [datetime.fromisoformat(row.bar_time) for row in bars]
        except ValueError:
            return False, "bar_time not ISO datetime"

        if after_bar_time is None:
            first_dt = parsed[0]
            if (first_dt.hour, first_dt.minute) != (9, 31):
                return False, "bootstrap does not start from session open"
        else:
            # 核心：cursor → first bar 必须被证明连续（否则允许跨缺口 crossing）
            cursor = _to_shanghai_aware_timestamp(after_bar_time).to_pydatetime()
            ok, reason = await self._prove_minute_edge(
                session,
                prev_dt=cursor,
                prev_session_key=cursor.date().isoformat(),
                curr_dt=parsed[0],
                curr_session_key=bars[0].session_key,
            )
            if not ok:
                return False, f"cursor-to-first-bar gap: {reason}"

        for i in range(1, len(bars)):
            ok, reason = await self._prove_minute_edge(
                session,
                prev_dt=parsed[i - 1],
                prev_session_key=bars[i - 1].session_key,
                curr_dt=parsed[i],
                curr_session_key=bars[i].session_key,
            )
            if not ok:
                return False, reason
        return True, ""

    async def get_bars_batch(
        self,
        session: AsyncSession,
        instrument_ids: Sequence[uuid.UUID],
        *,
        _diag_sink: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[uuid.UUID, BarAggregationResult | Exception]:
        """[CHANGE-20260804-FS] 批量获取同一行情合同的多个标的（数据库级批读）。

        与旧实现（循环逐股调用 get_bars → N 次 bars 查询 + N 次复权因子查询）不同，
        本方法对整批标的只发起：

        * 1 次 bars_daily 批量查询（get_daily_bars_batch，IN 子句）；
        * 1 次 adj_factor 批量查询（get_adj_factor_series_batch，IN 子句）；
        * 1 次共享的「预期最后完成日」计算（按 now 日期，与标的无关）。

        随后在内存按 instrument_id 分组，逐股复用同一套 bars/复权/诊断构造合同
        （_build_daily_aggregation），按标的隔离失败。这把 N×2 的 DB 往返降到约 3 次。

        仅支持日线类周期（1d/1w/1mo）；其余周期（日内）退回逐股 get_bars 以保证合同一致。
        返回值保留输入顺序，便于批任务稳定产生进度和 metrics。
        """
        if not instrument_ids:
            return {}

        timeframe = str(kwargs.get("timeframe", "1d")).lower()
        adj = kwargs.get("adj", "none")
        include_realtime = kwargs.get("include_realtime", True)
        completed_only = kwargs.get("completed_only", False)
        # [FS-CONTRACT 2026-08-04] 与单股 get_bars 对齐：completed_only 强制不含实时，
        # 否则批读与单股合同不一致（单股在 1306-1308 强制 include_realtime=False）。
        if completed_only:
            include_realtime = False
        start_date = kwargs.get("start_date")
        end_date = kwargs.get("end_date")
        limit = kwargs.get("limit")
        warmup_bars = kwargs.get("warmup_bars", 0)
        adjustment_as_of = kwargs.get("adjustment_as_of")
        # [CHANGE-20260816-003] batch 与单股 get_bars 对齐：strict DB-only 开关透传
        allow_backfill = kwargs.get("allow_backfill", True)

        now = now_shanghai()
        start, end = _resolve_date_range(timeframe, start_date, end_date, limit=limit)

        # 日内周期不在批读范围内：退回逐股 get_bars（合同一致，行为不变）
        if timeframe not in ("1d", "1w", "1mo"):
            if _diag_sink is not None:
                _diag_sink.update(
                    {
                        "read_mode": "per_symbol_fallback",
                        "symbol_count": len(instrument_ids),
                        "fallback_symbol_count": len(instrument_ids),
                        "read_operation_count": len(instrument_ids),
                        "repository_query_count": len(instrument_ids)
                        * _SINGLE_GET_BARS_REPOSITORY_QUERIES,
                    }
                )
            results: dict[uuid.UUID, BarAggregationResult | Exception] = {}
            for instrument_id in instrument_ids:
                try:
                    results[instrument_id] = await self.get_bars(
                        session, instrument_id, **kwargs
                    )
                except Exception as exc:
                    results[instrument_id] = exc
            return results

        # [CHANGE-20260804-FS] 数据库级批读：bars + 复权因子各 1 次 SQL
        bars_by_id = await get_daily_bars_batch(session, list(instrument_ids), start, end)

        factor_by_id: dict[uuid.UUID, pd.DataFrame] | None = None
        if adj == "qfq":
            fetch_as_of = None if include_realtime else adjustment_as_of
            factor_by_id = await get_adj_factor_series_batch(
                session, list(instrument_ids), as_of=fetch_as_of
            )

        # 共享的「预期最后完成日」（按 now 日期，与标的无关，整批只算 1 次）
        expected = await _call_expected_last_completed_daily_bar(session, now)

        # [P0-2 2026-08-04] 批读模式真实诊断：整批只发起 bars + 复权因子 + 预期完成日
        # 共 3 次 repository 级查询（adj=none 时 2 次），与标的数量无关。
        if _diag_sink is not None:
            _diag_sink.update(
                {
                    "read_mode": "batch",
                    "symbol_count": len(instrument_ids),
                    "fallback_symbol_count": 0,
                    "read_operation_count": 1,
                    "repository_query_count": 3 if adj == "qfq" else 2,
                }
            )

        results = {}
        for instrument_id in instrument_ids:
            try:
                daily_df = bars_by_id.get(instrument_id, pd.DataFrame())
                factor_df = (
                    factor_by_id.get(instrument_id, pd.DataFrame())
                    if factor_by_id is not None
                    else pd.DataFrame()
                )
                results[instrument_id] = await _build_daily_aggregation(
                    session,
                    instrument_id,
                    daily_df,
                    factor_df,
                    expected,
                    now,
                    timeframe=timeframe,
                    adj=adj,
                    include_realtime=include_realtime,
                    completed_only=completed_only,
                    start=start,
                    end=end,
                    limit=limit,
                    warmup_bars=warmup_bars,
                    adjustment_as_of=adjustment_as_of,
                    allow_backfill=allow_backfill,
                )
            except Exception as exc:
                results[instrument_id] = exc
        return results

    async def get_bars(
        self,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        timeframe: str = "1d",
        adj: str = "none",
        include_realtime: bool = True,
        completed_only: bool = False,
        start_date: date | datetime | None = None,
        end_date: date | datetime | None = None,
        limit: int | None = None,
        warmup_bars: int = 0,
        adjustment_as_of: date | None = None,
        allow_backfill: bool = True,
        fresh_intraday_tail: bool = False,
        source_policy: MarketDataSourcePolicy = MarketDataSourcePolicy.HYBRID,
    ) -> BarAggregationResult:
        """获取行情聚合结果（v2 契约，CHANGE-20260717-002）。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            timeframe: 1d | 15m | 1h | 1w | 1mo | 1m
            adj: qfq | none
            include_realtime: 交易时段是否补充实时 1m 数据
            completed_only: 只返回已完成 bar（默认 True 时强制 include_realtime=False；
                仅当 fresh_intraday_tail=True 且周期为 15m 时例外，见下）
            start_date: 起始日期/时间（可选）
            end_date: 结束日期/时间（可选）
            limit: 返回最近 N 根（服务端截取，保证 source_bar_hash 稳定）
            warmup_bars: 额外预热根数（>0 时返回 warmup_bars_full 含完整计算集）
            adjustment_as_of: 复权锚点（None=最新；date=point-in-time，禁止未来除权事件泄漏）
            allow_backfill: [CHANGE-20260808] 是否允许外部 provider 回补（default True）。
                False = strict DB-only：DB 有 completed qfq bars 则返回，DB 无则返回空
                （由 caller 标 skipped），绝不调用 external provider / realtime / 15m。
                production history replay / canary 必须使用 strict DB-only。
            fresh_intraday_tail: [USER-FIX-3 / C] 显式 opt-in：在 completed_only=True
                语义下，仍允许读取**当日已完成的** 15m 实时尾部（Pytdx 原生周期，
                不回写 DB）。默认 False ⇒ 保持「completed_only 强制不含实时」的既有
                行为，既有的 15m consumer 完全不受影响。
                本参数**只对 15m 有效**（1m/1h/日/周/月线不生效，见
                _FRESH_TAIL_TIMEFRAMES）。
                completed 语义由 MDAS 自身保证：实时尾部在合并之前先经
                _filter_unfinished_15m_bars 剔除 forming bar，因此返回结果
                **本身**全部为已完成 bar —— forming 不会进入 limit /
                source_bar_hash / actual_count / coverage。
            source_policy: [PANJI-INTRADAY-DIRECT-SOURCE] 数据来源策略（唯一 source owner）。
                默认 HYBRID（既有行为不变）。PROVIDER_DIRECT 只对 15m/1h 生效：
                纯 provider 取数、不读 DB 分钟线、不 merge、失败不 fallback。
                DB_ONLY：只读 DB，强制禁用外部 provider / realtime / fresh tail，
                历史与 PIT 路径必须使用。

        Returns:
            BarAggregationResult（含 bars、warmup_bars_full、hash、contract_version 等诊断字段）
        """
        now = now_shanghai()
        as_of = now
        timeframe = timeframe.lower()
        if timeframe not in _ALLOWED_TIMEFRAMES:
            raise ValueError(
                f"timeframe 只支持 {sorted(_ALLOWED_TIMEFRAMES)}, got {timeframe!r}"
            )
        if adj not in _ALLOWED_ADJ:
            raise ValueError(f"adj 只支持 qfq/none, got {adj!r}")

        # [PANJI-INTRADAY-DIRECT-SOURCE] source ownership 校验与归一。
        # 必须在 completed_only 互斥判定与 _cache_key 之前完成——allow_backfill /
        # fresh_intraday_tail 参与缓存键，先归一才能保证同一策略只有一个缓存条目。
        if (
            source_policy == MarketDataSourcePolicy.PROVIDER_DIRECT
            and timeframe not in _PROVIDER_DIRECT_TIMEFRAMES
        ):
            raise ValueError(
                f"provider_direct 只支持 provider 原生日内周期 "
                f"{sorted(_PROVIDER_DIRECT_TIMEFRAMES)}, got {timeframe!r}"
            )
        if source_policy == MarketDataSourcePolicy.DB_ONLY:
            # 严格 DB-only：历史 / PIT 路径禁止访问网络。
            # 显式覆写调用方参数（而不是新增第二套分支），使 daily 的 need_tail 回补
            # 与 intraday 的 fresh tail 都自然失效，行为等价于既有 strict DB-only。
            allow_backfill = False
            fresh_intraday_tail = False

        # [mdas] - completed_only 与 include_realtime 互斥：completed_only 强制不含实时
        # [USER-FIX-3 / C] 唯一的例外：调用方显式 fresh_intraday_tail=True 且周期为
        # 15m 时，允许合并「当日已完成的实时尾部」。未显式开启时行为完全不变。
        if completed_only and not (
            fresh_intraday_tail and timeframe in _FRESH_TAIL_TIMEFRAMES
        ):
            include_realtime = False

        # [mdas] - 先查 Redis 短缓存（参数 + source policy + 契约版本）
        cache_key = _cache_key(
            instrument_id, timeframe, adj, include_realtime, completed_only,
            start_date, end_date, limit, warmup_bars, adjustment_as_of,
            allow_backfill, fresh_intraday_tail, source_policy,
        )
        cached = _cache_get(cache_key)
        if cached is not None:
            cached.cache_hit = True
            cached.freshness_seconds = (now - cached.as_of).total_seconds()
            return cached

        start, end = _resolve_date_range(timeframe, start_date, end_date, limit=limit)

        bars_df = pd.DataFrame()
        data_source = "db"
        is_partial = False
        last_persisted_bar_time: pd.Timestamp | None = None
        last_live_bar_time: pd.Timestamp | None = None
        degraded = False
        degraded_reason: str | None = None

        # [mdas] - 获取复权因子序列（在数据查询前，因子序列用于 qfq 和 adj_factor_hash）
        # include_realtime=True 时取全量因子（含今日，用于 partial daily qfq）
        # include_realtime=False 时按 as_of 过滤（历史可复现，无未来泄漏）
        _adj_service = AdjustmentFactorService()
        factor_df = pd.DataFrame()
        if adj == "qfq":
            fetch_as_of = None if include_realtime else adjustment_as_of
            try:
                factor_df = await _adj_service.get_factor_series(
                    session, instrument_id, as_of=fetch_as_of
                )
            except Exception as exc:
                degraded = True
                degraded_reason = f"adj_factor_unavailable: {exc}"
                data_source = "degraded"
        adj_factor_hash = _compute_adj_factor_hash(factor_df)

        # [CP-V3-A2] 迭代回补诊断字段默认值（日线/周线/月线路径不经过 backfill）。
        # intraday 路径会通过 _fetch_intraday_with_backfill 覆盖这些值；
        # daily 路径保持默认值（backfill_rounds=0, coverage_reason="daily_no_backfill"）。
        backfill_rounds = 0
        intraday_history_exhausted = False
        coverage_reason = "daily_no_backfill"
        # [CHANGE-20260724-004] daily_df 默认空 DataFrame，供 latest_daily_quote 在
        # intraday 路径安全引用（daily 分支会覆盖此值）
        daily_df = pd.DataFrame()

        # ============================================================
        # 日线 / 周线 / 月线
        # ============================================================
        if timeframe in ("1d", "1w", "1mo"):
            daily_df = await _query_daily_bars(session, instrument_id, start, end)  # type: ignore[arg-type]
            if not daily_df.empty:
                last_persisted_bar_time = pd.Timestamp(daily_df.index[-1])

            # [CHANGE-20260805-CP4A / P0-01] 历史点回放补尾判断必须取「预期最后完成日」与
            # 「请求 end」的较小者：历史回放目标为过去某日时，不得拿真实 today 作为补尾基准
            # （否则 DB 已完整覆盖到请求日仍会误触发 pytdx）。end 未提供（实时查询）时保持
            # 原 expected 语义不变。此逻辑对单股/批量两条路径统一。
            now_expected = await _call_expected_last_completed_daily_bar(session, now)
            expected = min(now_expected, end) if end is not None else now_expected
            need_tail = daily_df.empty or daily_df.index[-1].date() < expected

            # [CHANGE-20260808] strict DB-only：allow_backfill=False 时绝不调用外部
            # provider 回补（external provider / realtime / 15m 均 zero calls）。
            # 仅返回 DB 已有 completed bars；DB 无数据则返回空，由 caller 标 skipped。
            if need_tail and allow_backfill:
                try:
                    tail_df = await fetch_daily_bars(
                        session, instrument_id, start, end  # type: ignore[arg-type]
                    )
                    if not tail_df.empty:
                        daily_df = _merge_bars(daily_df, tail_df)
                        data_source = "hybrid"
                        last_live_bar_time = pd.Timestamp(tail_df.index[-1])
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"pytdx daily fallback failed: {exc}"
                    data_source = "degraded"

            # [P0-2] 1w/1mo 必须先合并今日 partial daily 再聚合（CHANGE-20260724-003）
            # 执行顺序：DB 日线 + Pytdx 回补日线 → 合并今日 partial daily → qfq 一次 → 聚合 1w/1mo
            # 不得先聚合后补今日，否则周/月线永远缺少当日数据。
            # 1d 的 partial daily 在 _finalize_bars 之后单独补（避免被 _filter_unfinished_daily_bars 误删）。
            if timeframe in ("1w", "1mo") and include_realtime and _is_trading_hours(now):
                try:
                    is_trading_day = await is_trading_day_async(session, now.date())
                    session_name = compute_market_session(now, is_trading_day)
                    if session_name in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON):
                        partial_daily = await fetch_today_daily_bars(
                            session, instrument_id, now.date()
                        )
                        if not partial_daily.empty:
                            daily_df = _merge_bars(daily_df, partial_daily)
                            if data_source == "db":
                                data_source = "hybrid"
                            is_partial = True
                            last_live_bar_time = pd.Timestamp(partial_daily.index[-1])
                        else:
                            # [P0-6] 实时日线本应存在但返回空 → stale
                            degraded = True
                            degraded_reason = "realtime_1d_empty_in_trading_hours"
                            if data_source != "degraded":
                                data_source = "degraded"
                except Exception as exc:
                    logger.warning(
                        "1w/1mo partial daily 合并失败 instrument_id=%s: %s",
                        instrument_id, exc,
                    )
                    degraded = True
                    degraded_reason = f"pytdx partial daily failed: {exc}"
                    data_source = "degraded"

            # [mdas] - qfq 应用在合成前（"日线完成复权后再聚合"）
            # 含 1w/1mo 已合并的今日 partial daily，统一复权一次
            if adj == "qfq" and not daily_df.empty and not factor_df.empty:
                try:
                    daily_df = _adj_service.apply_qfq(
                        daily_df, factor_df, as_of=adjustment_as_of, intraday=False
                    )
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"qfq failed: {exc}"
                    data_source = "degraded"

            # [mdas] - 周线/月线从已复权日线合成（委托 kline_aggregator）
            # 此时 daily_df 已包含今日 partial daily 并完成 qfq，聚合后的当前 1w/1mo bar 包含当日数据
            if timeframe == "1w":
                bars_df = aggregate_kline(daily_df, "1w") if not daily_df.empty else daily_df
            elif timeframe == "1mo":
                bars_df = aggregate_kline(daily_df, "1mo") if not daily_df.empty else daily_df
            else:
                bars_df = daily_df

        # ============================================================
        # [PANJI-INTRADAY-DIRECT-SOURCE] 个股实时分钟（provider 直取）
        # ============================================================
        # 实时分钟行情属于 Provider：不读 DB 分钟线、不做 DB ∪ provider merge。
        # provider 失败直接向上抛（禁止静默 fallback 到 stale DB —— 那只是掩盖问题，
        # 且会继续返回越来越旧的 DB 15m）。复权仍由下方既有统一 pipeline 施加。
        elif source_policy == MarketDataSourcePolicy.PROVIDER_DIRECT:
            bars_df = await _fetch_provider_intraday_direct(
                session=session,
                instrument_id=instrument_id,
                timeframe=timeframe,
                required_count=limit,
                end=end,
                completed_only=completed_only,
                now=now,
            )
            data_source = "provider_direct"
            # provider 只能给「最近 required_count 根」；不足即真实历史耗尽，
            # 禁止拿 DB 再补（§23 语义：actual_count < requested + history_exhausted）。
            intraday_history_exhausted = (
                limit is not None and len(bars_df) < limit
            )
            backfill_rounds = 1
            coverage_reason = "provider_direct"
            # provider_direct 不读 DB ⇒ 不存在 persisted bar（completed_through 保持 None）。
            if not bars_df.empty:
                last_live_bar_time = pd.Timestamp(bars_df.index[-1])
            # 交易时段内、允许实时尾部时，provider 末根即正在形成的当前 bar。
            is_partial = bool(include_realtime and _is_trading_hours(now))

            # [复权唯一 owner] provider 原生 bar 的 adj_factor=1.0（未复权），必须经
            # **同一份** qfq 实现施加前复权（与 hybrid 分支共用 _apply_intraday_qfq），
            # 不得在本分支内联一套 qfq（否则二次复权 / 或声明 adj=qfq 却返回未复权）。
            bars_df, _qfq_err = _apply_intraday_qfq(
                _adj_service, bars_df, factor_df,
                adj=adj, adjustment_as_of=adjustment_as_of,
            )
            if _qfq_err is not None:
                degraded = True
                degraded_reason = _qfq_err
                data_source = "degraded"

        # ============================================================
        # 日内周期（含 1m 原始分钟线）
        # ============================================================
        else:
            # [CP-V3-A3] 日内迭代回补：受控循环查询直到满足 required_count 或确认
            # history_exhausted（基于 listing_date 边界，而非 no_progress）。
            # 修正 CP-V3-A2 的 no_progress 误判：长期停牌股票更早可能有大量历史。
            # mypy: _query_15min_bars/_query_minute_bars 有可选 limit 参数，
            # _query_60min_bars 没有，三者签名不完全一致，统一用 Any
            _intraday_query_fn: Any
            if timeframe == "15m":
                _intraday_query_fn = _query_15min_bars
            elif timeframe == "1h":
                _intraday_query_fn = _query_60min_bars
            else:  # 1m
                _intraday_query_fn = _query_minute_bars

            # [CP-V3-A3] 获取 listing_date 作为历史下界
            _listing_date = await _get_listing_date(session, instrument_id)

            (
                bars_df,
                backfill_rounds,
                intraday_history_exhausted,
                coverage_reason,
            ) = await _fetch_intraday_with_backfill(
                session,
                instrument_id,
                timeframe,
                start,  # type: ignore[arg-type]
                end,  # type: ignore[arg-type]
                _intraday_query_fn,
                required_count=limit,
                listing_date=_listing_date,
            )

            if not bars_df.empty:
                last_persisted_bar_time = pd.Timestamp(bars_df.index[-1])

            # [USER-FIX-3 / C] point-in-time 时间视界（四链共享 Provider 的历史快照路径）
            # 「允许 fresh tail」≠「无视调用方请求的时间截止点」：
            #   requested end day < today  → 历史 point-in-time，禁止访问 realtime source；
            #   同日但 end 早于当前        → 允许拉取，但结果必须裁到该视界。
            _fresh_tail_path = (
                timeframe == "15m" and completed_only and fresh_intraday_tail
            )
            realtime_allowed = True
            realtime_horizon: pd.Timestamp | None = None
            if _fresh_tail_path:
                realtime_allowed, realtime_horizon = _resolve_realtime_horizon(end, now)

            if (
                include_realtime
                # [PANJI-INTRADAY-DIRECT-SOURCE] DB_ONLY 绝不访问外部 provider：
                # 历史 / PIT 路径的实时尾部=网络访问=未来数据污染风险。
                and source_policy != MarketDataSourcePolicy.DB_ONLY
                and _is_trading_hours(now)
                and realtime_allowed
            ):
                # [P0-4] 冻结行情周期合同：15m/1h 实时尾部使用 Pytdx 原生周期，
                # 禁止从 1m 聚合（CHANGE-20260724-003）
                try:
                    if timeframe == "1m":
                        # 1m 仍使用原生 1m 拉取（带时间范围）
                        now_cst = now if now.tzinfo else now.replace(tzinfo=SHANGHAI_TZ)
                        live_start = now_cst.replace(hour=9, minute=30, second=0, microsecond=0)
                        live_end = now_cst
                        live_agg = await fetch_minute_bars(
                            session, instrument_id, live_start, live_end
                        )
                    elif timeframe == "15m":
                        # 15m 使用 Pytdx 原生 15m（按 count 拉取今日 bar）
                        live_agg = await fetch_15min_bars(session, instrument_id, count=16)
                    else:  # 1h
                        # 1h 使用 Pytdx 原生 60m（按 count 拉取今日 bar）
                        live_agg = await fetch_60min_bars(session, instrument_id, count=4)

                    # [USER-FIX-3 / C corrective] completed_only 的语义必须在返回结果
                    # **本身**成立：先剔除 forming bar，再做 merge / limit / hash /
                    # actual_count / coverage。否则 forming bar 会占用 limit 窗口槽位
                    # （4000 → 3999，把成熟股票误判成 INPUT_CONTRACT_VIOLATION），
                    # 并使 source_bar_hash 描述的数据集 ≠ 实际被算法消费的数据集。
                    live_raw_empty = live_agg.empty
                    if timeframe == "15m" and completed_only:
                        live_agg = _filter_unfinished_15m_bars(live_agg, now)
                    if realtime_horizon is not None:
                        # [USER-FIX-3 / C] 时间视界裁剪：同一交易日内的精确 datetime
                        # point-in-time 请求（如 end=今天 09:45）不得包含 10:00 的 bar。
                        # 最终生效上界 = min(floor(now,15m), requested end)。
                        live_agg = _bars_not_after(live_agg, realtime_horizon)

                    if not live_agg.empty:
                        # 按时间戳合并：实时尾部覆盖 DB 同时间戳 bar
                        bars_df = _merge_bars(bars_df, live_agg)
                        if data_source == "db":
                            data_source = "hybrid"
                        is_partial = True
                        last_live_bar_time = pd.Timestamp(live_agg.index[-1])
                    elif live_raw_empty:
                        # [P0-6] 实时目标周期按当前市场阶段本应存在但返回空 → stale
                        # 禁止静默返回普通 db 状态
                        degraded = True
                        degraded_reason = f"realtime_{timeframe}_empty_in_trading_hours"
                        if data_source != "degraded":
                            data_source = "degraded"
                    # else：原始实时非空、但过滤后为空（当前 15m 槽位刚开始、
                    # 尚无任何已完成 bar）→ 属正常情况，不得标 stale。
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"pytdx realtime fallback failed: {exc}"
                    data_source = "degraded"

            # [mdas] - qfq 应用（日内按交易日映射同一权威日线因子）
            # [PANJI-INTRADAY-DIRECT-SOURCE] 与 provider_direct 分支共用唯一 owner
            # _apply_intraday_qfq，禁止在此内联第二套 qfq。
            bars_df, _qfq_err = _apply_intraday_qfq(
                _adj_service, bars_df, factor_df,
                adj=adj, adjustment_as_of=adjustment_as_of,
            )
            if _qfq_err is not None:
                degraded = True
                degraded_reason = _qfq_err
                data_source = "degraded"

        # [mdas] - 排序、去重、过滤未完成 bar
        bars_df = _finalize_bars(bars_df, timeframe, now)

        # [mdas] - 若 Pytdx 数据被过滤掉，同步 last_live_bar_time
        if last_live_bar_time is not None and not bars_df.empty:
            if last_live_bar_time not in bars_df.index:
                last_live_bar_time = None
        elif bars_df.empty:
            last_live_bar_time = None

        # [P0-4] 1d 交易时段合成今日 partial daily bar（不写库，仅响应）
        # 放在 _finalize_bars 之后，避免被过滤未完成日线逻辑误删
        # [P0-4] 冻结行情周期合同：1d 实时尾部使用 Pytdx 原生日线，
        # 禁止从 1m 聚合（CHANGE-20260724-003）
        if timeframe == "1d" and include_realtime:
            try:
                is_trading_day = await is_trading_day_async(session, now.date())
                session_name = compute_market_session(now, is_trading_day)
                if session_name in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON):
                    # 使用 Pytdx 原生日线拉取今日 partial daily
                    partial_daily = await fetch_today_daily_bars(
                        session, instrument_id, now.date()
                    )
                    if not partial_daily.empty:
                        # [mdas] - partial daily 统一走 apply_qfq
                        # 保证 partial bar 与复权后的历史日线连续（"复权一次"原则）
                        if adj == "qfq" and not factor_df.empty:
                            try:
                                partial_daily = _adj_service.apply_qfq(
                                    partial_daily, factor_df,
                                    as_of=adjustment_as_of, intraday=False,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "partial daily qfq 失败 instrument_id=%s: %s",
                                    instrument_id, exc,
                                )
                        bars_df = _merge_bars(bars_df, partial_daily)
                        if data_source == "db":
                            data_source = "hybrid"
                        is_partial = True
                        # last_live_bar_time 保留完整 datetime，便于前端展示精确到分钟
                        last_live_bar_time = pd.Timestamp(partial_daily.index[-1])
                    else:
                        # [P0-6] 实时日线本应存在但返回空 → stale
                        degraded = True
                        degraded_reason = "realtime_1d_empty_in_trading_hours"
                        if data_source != "degraded":
                            data_source = "degraded"
            except Exception as exc:
                logger.warning(
                    "1d partial daily 合成失败 instrument_id=%s: %s",
                    instrument_id, exc,
                )
                degraded = True
                degraded_reason = f"pytdx partial daily failed: {exc}"
                data_source = "degraded"

        # [mdas] - completed_through = 最新已完成 DB bar 时间（不含 partial/realtime）
        completed_through = last_persisted_bar_time

        # [CP-V3-A] 记录 limit 截取前的原始数量（用于 history_exhausted 判定）
        pre_limit_count = len(bars_df) if not bars_df.empty else 0

        # [CHANGE-20260724-004] 保存 limit 截断前的完整 DataFrame
        # 用于 latest_daily_quote 聚合（1m/15m/1h 按交易日聚合当日 OHLC）
        # 禁止为 quote 增加第二次 MDAS/Pytdx/Repository 行情读取
        bars_df_full = bars_df

        # [mdas] - limit / warmup 截取（在 hash 计算前，保证相同 limit 下 hash 稳定）
        warmup_bars_full: pd.DataFrame | None = None
        if warmup_bars > 0 and not bars_df.empty:
            full_count = (limit or 0) + warmup_bars
            warmup_bars_full = (
                bars_df.tail(full_count) if full_count <= len(bars_df) else bars_df
            )
        if limit is not None and not bars_df.empty:
            bars_df = bars_df.tail(limit)

        # [mdas] - source_bar_hash 在 limit 截取后计算（跨调用方一致性校验）
        source_bar_hash = (
            compute_source_bar_hash(bars_df, timeframe) if not bars_df.empty else ""
        )

        # [CP-V3-A] count-aware 诊断字段
        actual_count = len(bars_df) if not bars_df.empty else 0
        coverage_start: pd.Timestamp | None = (
            pd.Timestamp(bars_df.index[0]) if not bars_df.empty else None
        )
        coverage_end: pd.Timestamp | None = (
            pd.Timestamp(bars_df.index[-1]) if not bars_df.empty else None
        )
        # [CP-V3-A2] history_exhausted 语义修正：
        # - intraday 路径：使用 _fetch_intraday_with_backfill 的判定（基于真实 earliest
        #   bar / empty query / no_progress，而非"单次查询 < limit"）。旧逻辑
        #   (CP-V3-A) 会在节假日/停牌/查询窗口不足时误判 True，把
        #   INPUT_CONTRACT_VIOLATION（系统回看窗口不足）当成真实历史不足（degraded）。
        # - daily 路径：保持 "pre_limit_count < limit" 判定（日线不经过迭代回补，
        #   DB 历史不足即真实历史不足）。
        if timeframe in ("1d", "1w", "1mo"):
            history_exhausted = (
                limit is not None and pre_limit_count < limit
            )
        else:
            history_exhausted = intraday_history_exhausted

        # [CHANGE-20260724-004] latest_daily_quote: 当日行情事实
        # quote 与展示周期完全解耦——单次 MDAS 读取，禁止第二次 Pytdx/Repository 查询
        # - 1d: bars_df_full 即合并今日 partial 后的日线，直接取末根
        # - 1w/1mo: daily_df 已合并今日 partial daily（聚合前），取末根日线
        # - 1m/15m/1h: 从 bars_df_full 按最新交易日聚合 open/high/low/close/volume/amount
        # 缺失时返回 None，前端 quote=null 且 freshness=unavailable
        latest_daily_quote: dict | None = None
        try:
            if timeframe in ("1w", "1mo"):
                # 1w/1mo: 使用聚合前 daily_df（已含今日 partial + qfq）
                _qdf = daily_df
                if _qdf is not None and not _qdf.empty:
                    _latest = _qdf.iloc[-1]
                    _prev_close = (
                        float(_qdf.iloc[-2]["close"])
                        if len(_qdf) >= 2 else None
                    )
                    _cp = float(_latest["close"])
                    latest_daily_quote = {
                        "open": float(_latest["open"]),
                        "high": float(_latest["high"]),
                        "low": float(_latest["low"]),
                        "close": _cp,
                        "volume": float(_latest["volume"]),
                        "amount": float(_latest["amount"]) if "amount" in _qdf.columns else 0.0,
                        "prev_close": _prev_close,
                        "change_pct": (
                            (_cp - _prev_close) / _prev_close * 100
                            if _prev_close and _prev_close != 0 else 0.0
                        ),
                    }
            elif timeframe == "1d":
                # 1d: bars_df_full 是合并 partial daily 后的日线 DataFrame
                _qdf = bars_df_full
                if _qdf is not None and not _qdf.empty:
                    _latest = _qdf.iloc[-1]
                    _prev_close = (
                        float(_qdf.iloc[-2]["close"])
                        if len(_qdf) >= 2 else None
                    )
                    _cp = float(_latest["close"])
                    latest_daily_quote = {
                        "open": float(_latest["open"]),
                        "high": float(_latest["high"]),
                        "low": float(_latest["low"]),
                        "close": _cp,
                        "volume": float(_latest["volume"]),
                        "amount": float(_latest["amount"]) if "amount" in _qdf.columns else 0.0,
                        "prev_close": _prev_close,
                        "change_pct": (
                            (_cp - _prev_close) / _prev_close * 100
                            if _prev_close and _prev_close != 0 else 0.0
                        ),
                    }
            else:
                # 1m/15m/1h: 从已加载的目标周期 bars_df_full 按最新交易日聚合
                # 禁止调用 fetch_today_daily_bars / _query_daily_bars
                if bars_df_full is not None and not bars_df_full.empty:
                    _latest_date = bars_df_full.index[-1].date()
                    _today_mask = bars_df_full.index.date == _latest_date
                    _today_bars = bars_df_full[_today_mask]
                    if not _today_bars.empty:
                        _prev_bars = bars_df_full[~_today_mask]
                        _prev_close = (
                            float(_prev_bars.iloc[-1]["close"])
                            if not _prev_bars.empty else None
                        )
                        _cp = float(_today_bars.iloc[-1]["close"])
                        latest_daily_quote = {
                            "open": float(_today_bars.iloc[0]["open"]),
                            "high": float(_today_bars["high"].max()),
                            "low": float(_today_bars["low"].min()),
                            "close": _cp,
                            "volume": float(_today_bars["volume"].sum()),
                            "amount": float(_today_bars["amount"].sum()) if "amount" in _today_bars.columns else 0.0,
                            "prev_close": _prev_close,
                            "change_pct": (
                                (_cp - _prev_close) / _prev_close * 100
                                if _prev_close and _prev_close != 0 else 0.0
                            ),
                        }
        except Exception as exc:
            logger.warning("latest_daily_quote 聚合失败: %s", exc)
            latest_daily_quote = None

        result = BarAggregationResult(
            bars=bars_df,
            data_source=data_source,
            as_of=as_of,
            is_partial=is_partial,
            last_persisted_bar_time=last_persisted_bar_time,
            last_live_bar_time=last_live_bar_time,
            freshness_seconds=0.0,
            degraded=degraded,
            degraded_reason=degraded_reason,
            warmup_bars_full=warmup_bars_full,
            source_bar_hash=source_bar_hash,
            adj_factor_hash=adj_factor_hash,
            adjustment_as_of=adjustment_as_of,
            completed_through=completed_through,
            # [CP-V3-A] count-aware 字段
            requested_count=limit,
            actual_count=actual_count,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            history_exhausted=history_exhausted,
            # [CP-V3-A2] 迭代回补诊断字段
            backfill_rounds=backfill_rounds,
            coverage_reason=coverage_reason,
            # [CHANGE-20260724-004] 当日行情事实（quote 唯一真源）
            latest_daily_quote=latest_daily_quote,
        )

        _cache_set(cache_key, result)
        return result


if __name__ == "__main__":
    # 自测入口：验证数据结构、缓存序列化、合并逻辑（不连 DB/网络）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 验证 BarAggregationResult 字段
    sample_df = pd.DataFrame({
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [100000.0],
        "amount": [1000000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-18"]))
    sample_df.index.name = "trade_date"

    result = BarAggregationResult(
        bars=sample_df,
        data_source="db",
        as_of=now_shanghai(),
        is_partial=False,
        last_persisted_bar_time=pd.Timestamp("2026-06-18"),
        last_live_bar_time=None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
    )
    assert result.data_source == "db"
    assert not result.degraded
    print("BarAggregationResult 构造 ✓")

    # 2. 验证缓存序列化/反序列化
    serialized = _serialize_result(result)
    restored = _deserialize_result(serialized)
    assert restored is not None
    assert restored.data_source == result.data_source
    assert len(restored.bars) == len(result.bars)
    assert restored.bars.index[0] == result.bars.index[0]
    print("缓存序列化/反序列化 ✓")

    # 3. 验证 _merge_bars
    df1 = pd.DataFrame({
        "open": [10.0], "high": [10.5], "low": [9.8],
        "close": [10.2], "volume": [100.0], "amount": [1000.0], "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-17"]))
    df2 = pd.DataFrame({
        "open": [11.0], "high": [11.5], "low": [10.8],
        "close": [11.2], "volume": [200.0], "amount": [2000.0], "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-18"]))
    merged = _merge_bars(df1, df2)
    assert len(merged) == 2
    assert merged.index[-1] == pd.Timestamp("2026-06-18")
    print("_merge_bars ✓")

    # 4. 验证 1m -> 15m 聚合
    minute_df = pd.DataFrame({
        "open": [10.0, 10.02, 10.03, 10.04, 10.05],
        "high": [10.02, 10.03, 10.04, 10.05, 10.06],
        "low": [9.99, 10.01, 10.02, 10.03, 10.04],
        "close": [10.02, 10.03, 10.04, 10.05, 10.06],
        "volume": [100.0, 100.0, 100.0, 100.0, 100.0],
        "amount": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "adj_factor": [1.0] * 5,
    }, index=pd.date_range("2026-06-18 09:45:00", periods=5, freq="1min"))
    minute_df.index.name = "trade_time"
    agg15 = _aggregate_minute_to_target(minute_df, "15m")
    assert len(agg15) == 1
    assert agg15.index[0] == pd.Timestamp("2026-06-18 09:45:00")
    assert agg15.iloc[0]["close"] == 10.06
    print("1m -> 15m 聚合 ✓")

    # 5. 验证 get_bars 签名（v2 契约 + 后续扩展）
    # [PANJI-INTRADAY-DIRECT-SOURCE] 修正本自检的既有漂移：此前 expected_params 缺少
    # allow_backfill / fresh_intraday_tail（CHANGE-20260808、USER-FIX-3 后未同步），
    # 早已与实际签名不一致。此处补齐为**真实**签名，并加入 source_policy。
    sig = inspect.signature(MarketDataAggregationService.get_bars)
    params = list(sig.parameters.keys())
    expected_params = [
        "self", "session", "instrument_id", "timeframe", "adj",
        "include_realtime", "completed_only", "start_date", "end_date",
        "limit", "warmup_bars", "adjustment_as_of",
        "allow_backfill", "fresh_intraday_tail", "source_policy",
    ]
    assert params == expected_params, f"get_bars 参数不匹配: {params}"
    assert (
        sig.parameters["source_policy"].default
        is MarketDataSourcePolicy.HYBRID
    ), "source_policy 默认必须是 HYBRID（不得改变既有调用方行为）"
    print(f"get_bars params={params} ✓ (source_policy default=HYBRID)")

    # 6. 验证契约版本字段默认值（CHANGE-20260730-P0：v5 契约）
    assert result.market_data_contract_version == _MARKET_DATA_CONTRACT_VERSION, \
        f"contract_version 应为 {_MARKET_DATA_CONTRACT_VERSION!r}, got {result.market_data_contract_version}"
    assert result.source_bar_hash == "", \
        f"source_bar_hash 默认应为空串, got {result.source_bar_hash!r}"
    assert result.adj_factor_hash == "", \
        f"adj_factor_hash 默认应为空串, got {result.adj_factor_hash!r}"
    assert result.adjustment_as_of is None, \
        f"adjustment_as_of 默认应为 None, got {result.adjustment_as_of!r}"
    assert result.completed_through is None, \
        f"completed_through 默认应为 None, got {result.completed_through!r}"
    assert result.warmup_bars_full is None, \
        f"warmup_bars_full 默认应为 None, got {result.warmup_bars_full!r}"
    print(f"契约字段默认值 ✓ (version={_MARKET_DATA_CONTRACT_VERSION})")

    # 7. 验证 _compute_adj_factor_hash
    factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17"]),
        "adj_factor": [0.5, 1.0],
    })
    h = _compute_adj_factor_hash(factor_df)
    assert len(h) == 16, f"adj_factor_hash 应为 16 字符, got {len(h)}"
    assert _compute_adj_factor_hash(pd.DataFrame()) == "", "空因子 hash 应为空串"
    print(f"_compute_adj_factor_hash ✓ (hash={h})")

    # 8. [CHANGE-20260730-P0] 验证 latest_daily_quote 序列化/反序列化完整保留
    result_with_quote = BarAggregationResult(
        bars=sample_df,
        data_source="db",
        as_of=now_shanghai(),
        is_partial=False,
        last_persisted_bar_time=pd.Timestamp("2026-06-18"),
        last_live_bar_time=None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
        latest_daily_quote={
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000.0, "amount": 1000000.0,
            "prev_close": 9.9, "change_pct": 3.03,
        },
    )
    serialized_q = _serialize_result(result_with_quote)
    restored_q = _deserialize_result(serialized_q)
    assert restored_q is not None
    assert restored_q.latest_daily_quote == result_with_quote.latest_daily_quote, \
        "latest_daily_quote 在 Redis 序列化/反序列化后必须完整保留"
    print("latest_daily_quote 序列化/反序列化 ✓")

    # 9. 验证 _cache_key 含契约版本 + allow_backfill（12 参数）
    ck = _cache_key(
        uuid.UUID("00000000-0000-0000-0000-000000000001"), "1d", "qfq", True, False,
        None, None, 4000, 1000, None,
    )
    expected_suffix = f":{_MARKET_DATA_CONTRACT_VERSION}"
    assert ck.endswith(expected_suffix), \
        f"缓存键应含契约版本后缀 {expected_suffix!r}, got {ck}"
    print(f"_cache_key 含契约版本 ✓ (suffix={expected_suffix})")
    # allow_backfill 不同 → 缓存键不同（strict DB-only 与可回补结果隔离）
    ck_bf = _cache_key(
        uuid.UUID("00000000-0000-0000-0000-000000000001"), "1d", "qfq", True, False,
        None, None, 4000, 1000, None, allow_backfill=False,
    )
    assert ck_bf != ck, "allow_backfill 必须参与缓存键隔离"
    print("_cache_key 隔离 allow_backfill ✓")

    # 10. [PANJI-INTRADAY-DIRECT-SOURCE] 验证 _cache_key 隔离 source policy
    # 同一参数下 hybrid / provider_direct / db_only 必须是三个不同缓存条目，
    # 否则 provider_direct 会命中旧 hybrid（=DB）缓存，表现为「代码改了但仍是 DB 数据」。
    _iid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    _base_kw = {
        "include_realtime": True, "completed_only": False,
        "start_date": None, "end_date": None,
        "limit": 4000, "warmup_bars": 0, "adjustment_as_of": None,
    }
    ck_hybrid = _cache_key(
        _iid, "15m", "qfq", source_policy=MarketDataSourcePolicy.HYBRID, **_base_kw,
    )
    ck_direct = _cache_key(
        _iid, "15m", "qfq",
        source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT, **_base_kw,
    )
    ck_dbonly = _cache_key(
        _iid, "15m", "qfq", source_policy=MarketDataSourcePolicy.DB_ONLY, **_base_kw,
    )
    assert len({ck_hybrid, ck_direct, ck_dbonly}) == 3, (
        "source_policy 必须参与缓存键隔离: "
        f"hybrid={ck_hybrid!r} direct={ck_direct!r} db_only={ck_dbonly!r}"
    )
    for _ck in (ck_hybrid, ck_direct, ck_dbonly):
        assert _ck.endswith(f":{_MARKET_DATA_CONTRACT_VERSION}"), _ck
    assert MarketDataSourcePolicy.PROVIDER_DIRECT.value == "provider_direct"
    assert MarketDataSourcePolicy.DB_ONLY.value == "db_only"
    print("_cache_key 隔离 source_policy ✓ (hybrid/provider_direct/db_only 互不命中)")

    # 11. [PANJI-INTRADAY-DIRECT-SOURCE] provider_direct 周期约束
    assert _PROVIDER_DIRECT_TIMEFRAMES == frozenset({"15m", "1h"})
    assert _PROVIDER_DIRECT_DEFAULT_COUNT["15m"] == NODE_CLUSTER_LOW_BARS == 4000
    assert _PROVIDER_DIRECT_DEFAULT_COUNT["1h"] == INDICATOR_BARS["1h"] == 1200
    print("provider_direct 周期/默认条数合同 ✓ (15m=4000, 1h=1200)")

    print("OK")
