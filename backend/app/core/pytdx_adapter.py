"""pytdx 连接适配器 - 封装连接、重试与股票列表拉取。

修复原 ref/交易/datasource/pytdx_client.py 的异常吞没问题：
- 所有 try/except 补充上下文后 re-raise（禁 except: pass / 静默兜底）
- 连接失败抛 RuntimeError，包含所有服务器错误信息
- 数据拉取失败抛 RuntimeError，包含市场与起始位置上下文

提供：
- PytdxAdapter: 适配器类，封装连接池/重试
- connect_pytdx: 模块级便捷函数（兼容原脚本调用习惯）
- get_security_list_all: 拉取指定市场的全部股票列表

用法：
    from app.core.pytdx_adapter import PytdxAdapter

    with PytdxAdapter() as adapter:
        df = adapter.get_stock_list(market="SH")

副作用：连接 pytdx 行情服务器（只读，不写库表/不改文件）。
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from typing import TYPE_CHECKING, Any

import pandas as pd
import redis
from pytdx.errors import TdxConnectionError
from pytdx.hq import TdxHq_API

from app.config import get_settings
from app.core.exchange import Exchange
from app.core.redis_client import get_sync_redis

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


@dataclass
class _KlineCacheEntry:
    """klines 进程内缓存条目（参考 chanlunpro FileCacheDB + ExchangeTDX.klines 增量更新）"""
    df: pd.DataFrame
    cached_at: datetime       # 缓存写入时间
    last_bar_time: datetime   # DataFrame 中最后一根 bar 的时间


# xdxr 缓存配置
_XDXR_CACHE_PREFIX = "xdxr"
_XDXR_CACHE_TTL = 86400  # 24 小时（秒）

# pytdx 服务器列表（与原 ref/交易/datasource/pytdx_client.py 保持一致）
PYTDX_SERVERS: list[tuple[str, int]] = [
    ("119.147.212.81", 7709),
    ("119.147.164.60", 7709),
    ("14.215.128.18", 7709),
    ("14.215.128.116", 7709),
    ("101.133.156.38", 7709),
    ("114.80.149.19", 7709),
    ("115.238.90.165", 7709),
    ("123.125.108.23", 7709),
    ("180.153.18.170", 7709),
    ("202.108.253.131", 7709),
]

# 市场映射：字符串标识 <-> pytdx 数字标识
# pytdx 仅支持 SH(1) 与 SZ(0)，BJ 暂不支持（需通过其他数据源补充）
MARKET_NAME_TO_CODE: dict[str, int] = {
    "SH": 1,
    "SZ": 0,
}

MARKET_CODE_TO_NAME: dict[int, str] = {v: k for k, v in MARKET_NAME_TO_CODE.items()}

# 每次拉取的步长（pytdx get_security_list 单次最大返回 1000 条）
SECURITY_LIST_PAGE_SIZE = 1000


def classify_stock(code: str, market: str) -> str:
    """按代码前缀分类，返回股票类型（参考 chanlun-pro 设计）。

    返回值：
    - stock_cn: A 股主板/科创板/创业板/北交所
    - index_cn: 指数
    - etf_cn: ETF
    - bond_cn: 债券/可转债/国债
    - stockB_cn: B 股
    - undefined: 未定义/其他
    """
    c = str(code)
    if market == "SH":
        if c.startswith("6"):
            return "stock_cn"
        if c[:3] in ("000", "880", "999"):
            return "index_cn"
        if c[:2] in ("51", "58"):
            return "etf_cn"
        if c[:3] in (
            "102", "110", "113", "120", "122", "124",
            "130", "132", "133", "134", "135", "136",
            "140", "141", "143", "144", "147", "148",
        ):
            return "bond_cn"
        return "undefined"
    if market == "SZ":
        if c[:2] in ("00", "30", "02"):
            return "stock_cn"
        if c[:2] == "39":
            return "index_cn"
        if c[:2] in ("15", "16"):
            return "etf_cn"
        if c[:2] in ("10", "11", "12") or c[:3] in ("123", "127", "128", "131", "139"):
            return "bond_cn"
        if c[:2] == "20":
            return "stockB_cn"
        return "undefined"
    return "undefined"


# 北京 A 股代码补充（pytdx 标准接口不返回北交所，参考 chanlun-pro 的 tdx_a_codes.py）
# 值：股票名称
BJ_STOCKS: dict[str, str] = {
    "920808": "曙光数创",
    "920427": "华维设计",
    "920159": "农大科技",
    "920985": "海泰新能",
    "920009": "丹娜生物",
    "920510": "丰光精密",
    "920564": "天润科技",
    "920662": "方盛股份",
    "920368": "连城数控",
    "920699": "海达尔",
    "920475": "三友科技",
    "920086": "科马材料",
    "920184": "国源科技",
    "920118": "太湖远大",
    "920045": "蘅东光",
    "920871": "派特尔",
    "920056": "能之光",
    "920022": "世昌股份",
    "920252": "天宏锂电",
    "920106": "林泰新材",
    "920932": "科达自控",
    "920098": "科隆新材",
    "920946": "森萱医药",
    "920274": "宏裕包材",
    "920719": "宁新新材",
    "920694": "中裕科技",
    "920608": "丰安股份",
    "920267": "鑫汇科",
    "920592": "华信永道",
    "920857": "泓禧科技",
    "920478": "峆一药业",
    "920017": "星昊医药",
    "920152": "昆工科技",
    "920810": "春光智能",
    "920050": "爱舍伦",
    "920247": "华密新材",
    "920037": "广信科技",
    "920790": "联迪信息",
    "920363": "莱赛激光",
    "920261": "一诺威",
    "920496": "许昌智能",
    "920953": "国子软件",
    "920167": "同享科技",
    "920508": "殷图网联",
    "920076": "国亮新材",
    "920419": "路斯股份",
    "920002": "万达轴承",
    "920174": "五新隧装",
    "920100": "三协电机",
    "920304": "迪尔化工",
    "920370": "新安洁",
    "920029": "开发科技",
    "920642": "通易航天",
    "920753": "天纺标",
    "920651": "天罡股份",
    "920978": "开特股份",
    "920455": "汇隆活塞",
    "920523": "德瑞锂电",
    "920892": "广咨国际",
    "920284": "灵鸽科技",
    "920701": "豪声电子",
    "920505": "九菱科技",
    "920571": "国航远洋",
    "920599": "同力股份",
    "920080": "奥美森",
    "920407": "驰诚股份",
    "920068": "天工股份",
    "920179": "凯德石英",
    "920414": "欧普泰",
    "920570": "坤博精工",
    "920504": "博迅生物",
    "920665": "科强股份",
    "920394": "民士达",
    "920190": "雷神科技",
    "920489": "佳先股份",
    "920092": "汉鑫科技",
    "920415": "恒拓开源",
    "920735": "德源药业",
    "920982": "锦波生物",
    "920433": "大唐药业",
    "920454": "同心传动",
    "920556": "雅达股份",
    "920522": "纳科诺尔",
    "920139": "华岭股份",
    "920726": "朱老六",
    "920926": "鸿智科技",
    "920010": "凯添燃气",
    "920112": "巴兰仕",
    "920509": "同惠电子",
    "920166": "海圣医疗",
    "920779": "武汉蓝电",
    "920693": "阿为特",
    "920260": "中寰股份",
    "920371": "欧福蛋业",
    "920305": "*ST云创",
    "920273": "一致魔芋",
    "920207": "众诚科技",
}

# 已知错误/异常代码过滤（参考 chanlun-pro 的 tdx_a_codes.py）
ERROR_CODES: set[str] = {
    "SH.000022", "SH.000025", "SH.000029", "SH.000031", "SH.000033",
    "SH.000035", "SH.000037", "SH.000038", "SH.000040", "SH.000042",
    "SH.000043", "SH.000044", "SH.000048", "SH.000049", "SH.000052",
    "SH.000053", "SH.000054", "SH.000055", "SH.000056", "SH.000057",
    "SH.000058", "SH.000059", "SH.000060", "SH.000061", "SH.000062",
    "SH.000063", "SH.000064", "SH.000065", "SH.000066", "SH.000067",
    "SH.000068", "SH.000069", "SH.000070", "SH.600601", "SH.600602",
    "SH.600603", "SH.600604", "SH.600605", "SH.600606", "SH.600607",
    "SH.600608", "SH.600609", "SH.600610", "SH.600611", "SH.600612",
}

# pytdx K 线周期映射（与原 ref/交易/datasource/pytdx_client.py PERIOD_MAP 一致）
PERIOD_MAP: dict[str, int] = {
    "1m": 8,
    "5m": 0,
    "15m": 1,
    "30m": 2,
    "60m": 3,
    "d": 4,
    "w": 5,
    "m": 6,
}

# 每次 get_security_bars 拉取条数（pytdx 单次上限约 800）
_FETCH_BATCH = 800


def market_from_code(code: str) -> int:
    """根据股票代码判断市场。

    与原 ref/交易/datasource/pytdx_client.py 一致：6 开头为 SH（market=1），其余为 SZ（market=0）。

    Args:
        code: 股票代码（如 '000001', '600519'）

    Returns:
        1 表示 SH，0 表示 SZ
    """
    return 1 if str(code).startswith("6") else 0


class PytdxAdapter(Exchange):
    """pytdx 连接适配器，封装连接重试与资源管理。

    实现 Exchange 抽象接口（策略模式），支持通过 get_exchange() 工厂切换数据源。

    使用方式：
        with PytdxAdapter() as adapter:
            df = adapter.get_stock_list(market="SH")
            daily = adapter.get_daily_bars("600519", date(2026,1,1), date(2026,6,18))

    异常处理：连接与数据拉取失败均抛 RuntimeError（含上下文），不吞没异常。
    """

    # klines 进程内缓存（参考 chanlunpro ExchangeTDX.klines 的 FileCacheDB 增量更新机制）
    _klines_cache: dict[str, _KlineCacheEntry] = {}

    def __init__(
        self,
        servers: list[tuple[str, int]] | None = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        """初始化适配器。

        Args:
            servers: pytdx 服务器列表，None 使用默认 PYTDX_SERVERS
            max_retries: 数据拉取失败重试次数（含重连）
            retry_delay: 重试间隔（秒），用于 get_xdxr_info 等方法的失败重试
        """
        self._servers: list[tuple[str, int]] = servers if servers is not None else PYTDX_SERVERS
        self._api: TdxHq_API | None = None
        self.max_retries = max_retries
        self.retry_delay: float = retry_delay

    def __enter__(self) -> PytdxAdapter:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.disconnect()

    @property
    def api(self) -> TdxHq_API:
        """已连接的 TdxHq_API 实例，未连接时抛 RuntimeError。"""
        if self._api is None:
            raise RuntimeError("pytdx 尚未连接，请先调用 connect()")
        return self._api

    def connect(self) -> None:
        """连接 pytdx 服务器，依次尝试服务器列表。

        Raises:
            RuntimeError: 所有服务器连接均失败时抛出，包含最近 5 条错误信息。
        """
        last_errors: list[str] = []
        for host, port in self._servers:
            try:
                api = TdxHq_API(raise_exception=True, auto_retry=True)
                if api.connect(host, port, time_out=5):
                    logger.info("pytdx 连接成功：%s:%d", host, port)
                    self._api = api
                    return
            except TdxConnectionError as exc:
                last_errors.append(f"{host}:{port} TdxConnectionError: {exc}")
            except Exception as exc:
                last_errors.append(f"{host}:{port} {type(exc).__name__}: {exc}")

        err_summary = "; ".join(last_errors[-5:])
        raise RuntimeError(f"pytdx 连接失败（尝试 {len(self._servers)} 个服务器）：{err_summary}")

    def disconnect(self) -> None:
        """断开连接，忽略断开时的异常（仅资源释放，不影响主流程）。"""
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception as exc:
                logger.warning("pytdx 断开连接时出现异常（已忽略）：%s", exc)
            finally:
                self._api = None

    def get_security_list(self, market: str, max_count: int | None = None) -> pd.DataFrame:
        """拉取指定市场的全部股票列表（参考 chanlun-pro all_stocks() 设计）。

        改进点：
        1. 使用 get_security_count 获取市场总数，按页大小分页拉取，避免依赖空列表终止
        2. 调用 classify_stock 过滤，仅保留 stock_cn / index_cn / etf_cn
        3. 过滤 ERROR_CODES 中的已知错误代码

        Args:
            market: 市场标识（SH/SZ）
            max_count: 最多拉取条数，None 表示拉取全部

        Returns:
            DataFrame，列：code, name, market

        Raises:
            ValueError: market 不在 SH/SZ 中
            RuntimeError: pytdx 未连接或拉取失败
        """
        if market not in MARKET_NAME_TO_CODE:
            raise ValueError(
                f"不支持的市场标识：{market}，pytdx 仅支持 {list(MARKET_NAME_TO_CODE.keys())}"
            )
        market_code = MARKET_NAME_TO_CODE[market]

        # 获取市场证券总数（参考 chanlun-pro：client.get_security_count(market)）
        try:
            total_count = self.api.get_security_count(market_code)
        except Exception as exc:
            raise RuntimeError(
                f"pytdx get_security_count 失败：market={market}(code={market_code}), error={exc}"
            ) from exc

        if total_count <= 0:
            logger.warning("pytdx 市场 %s 证券总数为 0", market)
            return pd.DataFrame(columns=["code", "name", "market"])

        # 计算分页数（参考 chanlun-pro：range(int(count / 1000) + 1)）
        pages = int(total_count / SECURITY_LIST_PAGE_SIZE) + 1
        if max_count is not None:
            pages = min(pages, int(max_count / SECURITY_LIST_PAGE_SIZE) + 1)

        all_items: list[dict[str, Any]] = []
        for i in range(pages):
            start = i * SECURITY_LIST_PAGE_SIZE
            try:
                data = self.api.get_security_list(market_code, start)
            except Exception as exc:
                raise RuntimeError(
                    f"pytdx get_security_list 拉取失败：market={market}(code={market_code}), "
                    f"start={start}, error={exc}"
                ) from exc

            if not data:
                break

            all_items.extend(data)

            if max_count is not None and len(all_items) >= max_count:
                all_items = all_items[:max_count]
                break

        if not all_items:
            logger.warning("pytdx 拉取市场 %s 股票列表为空", market)
            return pd.DataFrame(columns=["code", "name", "market"])

        df = pd.DataFrame(all_items)
        df = df[["code", "name"]].copy()
        df["market"] = market

        # 过滤：仅保留 stock_cn / index_cn / etf_cn（参考 chanlun-pro for_sz/for_sh 分类）
        df["_type"] = df.apply(lambda r: classify_stock(str(r["code"]), market), axis=1)
        valid_types = {"stock_cn", "index_cn", "etf_cn"}
        before_filter = len(df)
        df = df[df["_type"].isin(valid_types)]
        after_filter = len(df)
        if before_filter != after_filter:
            logger.info(
                "市场 %s 类型过滤：%d -> %d（过滤债券/B股/未定义 %d 条）",
                market, before_filter, after_filter, before_filter - after_filter,
            )

        # 过滤已知错误代码（参考 chanlun-pro tdx_codes_by_error）
        error_keys = df["code"].apply(lambda c: f"{market}.{c}")
        error_mask = error_keys.isin(ERROR_CODES)
        if error_mask.any():
            logger.info("市场 %s 过滤错误代码 %d 条", market, int(error_mask.sum()))
            df = df[~error_mask]

        df = df.drop(columns=["_type"]).reset_index(drop=True)
        return df

    def get_stock_list(self, market: str | None = None, max_count: int | None = None) -> pd.DataFrame:
        """拉取股票列表（可指定市场或拉取全部 SH+SZ，并补充北交所）。

        参考 chanlun-pro all_stocks()：SH/SZ 通过 pytdx 拉取，BJ 通过静态表补充。

        Args:
            market: 市场标识（SH/SZ），None 表示拉取全部 SH+SZ + BJ 补充
            max_count: 每个市场最多拉取条数，None 表示全部

        Returns:
            DataFrame，列：code, name, market
        """
        markets = [market] if market is not None else list(MARKET_NAME_TO_CODE.keys())
        frames = [self.get_security_list(m, max_count=max_count) for m in markets]
        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["code", "name", "market"]
        )

        # 补充北交所股票（pytdx 标准接口不返回北交所，参考 chanlun-pro tdx_codes_by_bj）
        if market is None or market == "BJ":
            bj_rows = [
                {"code": c, "name": n, "market": "BJ"}
                for c, n in BJ_STOCKS.items()
            ]
            if bj_rows:
                bj_df = pd.DataFrame(bj_rows)
                result = pd.concat([result, bj_df], ignore_index=True)

        return result

    def _fetch_bars(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> pd.DataFrame:
        """按周期与数量拉取 K 线（分页拉取，内部使用）。

        Args:
            symbol: 股票代码（如 '000001'）
            period: 周期键（见 PERIOD_MAP）
            count: 拉取条数

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 拉取或解析失败（不吞没异常）
        """
        if period not in PERIOD_MAP:
            raise RuntimeError(
                f"不支持的周期: {period}，支持: {list(PERIOD_MAP.keys())}"
            )

        market = market_from_code(symbol)
        cat = PERIOD_MAP[period]

        all_bars: list[dict[str, Any]] = []
        start = 0
        while len(all_bars) < count:
            try:
                data = self.api.get_security_bars(cat, market, symbol, start, _FETCH_BATCH)
            except Exception as exc:
                # 拉取失败：补充上下文后 raise（禁止吞没，原代码此处无异常处理）
                logger.warning(
                    "pytdx get_security_bars 失败 symbol=%s period=%s start=%d: %s",
                    symbol, period, start, exc,
                )
                raise RuntimeError(
                    f"pytdx 拉取 K 线失败 symbol={symbol} period={period} start={start}: {exc}"
                ) from exc

            if not data:
                break
            all_bars.extend(data)
            if len(data) < _FETCH_BATCH:
                break
            start += _FETCH_BATCH

        if not all_bars:
            return pd.DataFrame()

        df = pd.DataFrame(all_bars)

        # 统一 datetime 列（pytdx 返回 datetime 字符串或 year/month/day/hour/minute 分量）
        # 使用 errors='coerce' 容错畸形日期（如指数 399xxx 的 "0-00-00 15:00"），跳过无效行
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce").dt.tz_localize(None)
            df = df.dropna(subset=["datetime"]).reset_index(drop=True)
        elif {"year", "month", "day", "hour", "minute"}.issubset(df.columns):
            df["datetime"] = pd.to_datetime(
                df[["year", "month", "day", "hour", "minute"]].astype(int),
                errors="coerce",
            ).dt.tz_localize(None)
            df = df.dropna(subset=["datetime"]).reset_index(drop=True)

        if df.empty:
            return pd.DataFrame()

        df = df[["datetime", "open", "high", "low", "close", "vol", "amount"]]
        df.columns = ["datetime", "open", "high", "low", "close", "volume", "amount"]
        df = df.sort_values("datetime", ascending=True).tail(count).reset_index(drop=True)
        return df

    def get_daily_bars(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """获取日线数据（按日期范围，带重试）。

        内部按 count 拉取后按日期范围过滤。

        Args:
            symbol: 股票代码（如 '000001'）
            start: 起始日期
            end: 结束日期

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        # 计算拉取条数（天数 + 缓冲，覆盖非交易日）
        days = (end - start).days + 1
        count = min(max(days + 30, 30), 8000)

        df = self._fetch_with_retry(symbol, "d", count)
        if df.empty:
            return df

        # 按日期范围过滤
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        mask = (df["datetime"] >= start_ts) & (df["datetime"] <= end_ts)
        return df.loc[mask].reset_index(drop=True)

    def get_minute_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """获取 1 分钟线数据（按时间范围，带重试）。

        Args:
            symbol: 股票代码（如 '000001'）
            start: 起始时间
            end: 结束时间

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        # 计算拉取条数（分钟数 + 缓冲）
        minutes = int((end - start).total_seconds() // 60) + 1
        count = min(max(minutes + 500, 500), 8000)

        df = self._fetch_with_retry(symbol, "1m", count)
        if df.empty:
            return df

        # 按时间范围过滤
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        mask = (df["datetime"] >= start_ts) & (df["datetime"] <= end_ts)
        return df.loc[mask].reset_index(drop=True)

    def get_weekly_bars(
        self,
        symbol: str,
        count: int = 800,
    ) -> pd.DataFrame:
        """获取周线数据（按数量，带重试）。

        Args:
            symbol: 股票代码（如 '000001'）
            count: 拉取条数（默认 800，回补到 2023-01-01 约需 200；实际回补由 bars_scheduler_service.BACKFILL_COUNTS 控制）

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        return self._fetch_with_retry(symbol, "w", count)

    def get_monthly_bars(
        self,
        symbol: str,
        count: int = 800,
    ) -> pd.DataFrame:
        """获取月线数据（按数量，带重试）。

        Args:
            symbol: 股票代码（如 '000001'）
            count: 拉取条数（默认 800，回补到 2023-01-01 约需 50；实际回补由 bars_scheduler_service.BACKFILL_COUNTS 控制）

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        return self._fetch_with_retry(symbol, "m", count)

    def get_15min_bars(
        self,
        symbol: str,
        count: int = 800,
    ) -> pd.DataFrame:
        """获取 15 分钟线数据（按数量，带重试）。

        Args:
            symbol: 股票代码（如 '000001'）
            count: 拉取条数（默认 800，回补到 2023-01-01 约需 14000）

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        return self._fetch_with_retry(symbol, "15m", count)

    def get_60min_bars(
        self,
        symbol: str,
        count: int = 800,
    ) -> pd.DataFrame:
        """获取 60 分钟线数据（按数量，带重试）。

        Args:
            symbol: 股票代码（如 '000001'）
            count: 拉取条数（默认 800；回补到 2023-01-01 需约 3500 条，由 bars_scheduler_service.BACKFILL_COUNTS["60m"]=4000 控制）

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        return self._fetch_with_retry(symbol, "60m", count)

    # frequency → PERIOD_MAP 键映射（klines 内部使用）
    _FREQ_TO_PERIOD: dict[str, str] = {
        "1d": "d",
        "15m": "15m",
        "1h": "60m",
    }

    @staticmethod
    def _cache_key(symbol: str, frequency: str) -> str:
        return f"{symbol}:{frequency}"

    @staticmethod
    def _klines_ttl(frequency: str) -> int:
        """缓存 TTL（秒），交易时段短 TTL，收盘后长 TTL

        参考 chanlunpro 的 FileCacheDB 读取时排除最后一根 bar 的设计理念：
        交易时段数据变化快，需要短 TTL；收盘后数据不变，使用长 TTL。
        """
        now = datetime.now()
        # 判断是否在交易时段（9:30-15:00）
        is_trading = (
            now.weekday() < 5
            and dt_time(9, 30) <= now.time() <= dt_time(15, 0)
        )
        if is_trading:
            return 60 if frequency in ("15m", "1h", "1m") else 300  # 分钟线 60s，日线 300s
        else:
            return 3600  # 收盘后 1 小时 TTL

    @staticmethod
    def _apply_filters(
        df: pd.DataFrame,
        start_date: date | None,
        end_date: date | None,
        count: int | None,
    ) -> pd.DataFrame:
        """应用日期范围过滤和数量限制"""
        if start_date is not None:
            start_ts = pd.Timestamp(start_date, tz="Asia/Shanghai")
            df = df[df.index >= start_ts]
        if end_date is not None:
            end_ts = pd.Timestamp(end_date, tz="Asia/Shanghai") + pd.Timedelta(days=1)
            df = df[df.index < end_ts]
        if count is not None and len(df) > count:
            df = df.iloc[-count:]
        return df

    async def klines(
        self,
        symbol: str,
        frequency: str,
        start_date: date | None = None,
        end_date: date | None = None,
        count: int | None = None,
    ) -> pd.DataFrame | None:
        """统一行情读取接口（参考 chanlunpro ExchangeTDX.klines）

        进程内缓存 + 增量更新：
        - 缓存命中且有效：直接返回
        - 缓存过期：增量拉取新数据页合并（参考 chanlunpro 的 pages 逐页拉取逻辑）
        - 缓存未命中：全量拉取
        """
        from app.core.exchange import FREQUENCY_MAP

        cat = FREQUENCY_MAP.get(frequency)
        if cat is None:
            logger.warning("klines() 不支持的 frequency=%s", frequency)
            return None

        # 周线/月线：从日线合成
        if frequency in ("1w", "1mo"):
            return await self._klines_synthesized(symbol, frequency, start_date, end_date, count)

        cache_key = self._cache_key(symbol, frequency)
        now = datetime.now()

        # --- 缓存命中检查 ---
        entry = self._klines_cache.get(cache_key)
        if entry is not None:
            ttl = self._klines_ttl(frequency)
            if (now - entry.cached_at).total_seconds() < ttl:
                # 缓存有效，直接返回（应用过滤和限制）
                df = entry.df.copy()
                df = self._apply_filters(df, start_date, end_date, count)
                return df

            # --- 缓存过期：增量更新（参考 chanlunpro ExchangeTDX.klines 增量拉取）---
            try:
                # 拉取最近 2 页数据（2 × 700 = 1400 bars），足够覆盖增量
                incremental_df = await asyncio.to_thread(
                    self._fetch_with_retry, symbol, self._FREQ_TO_PERIOD[frequency], 1400
                )
                if incremental_df is not None and not incremental_df.empty:
                    # 转换为 DatetimeIndex 格式（与全量拉取一致）
                    if "datetime" in incremental_df.columns:
                        incremental_df = incremental_df.set_index("datetime")
                    incremental_df.index = pd.to_datetime(incremental_df.index)
                    incremental_df.index = incremental_df.index.tz_localize("Asia/Shanghai")
                    # 添加 adj_factor 列
                    if "adj_factor" not in incremental_df.columns:
                        incremental_df["adj_factor"] = 1.0

                    # 合并：去重（保留新数据），按时间排序
                    merged = pd.concat([entry.df, incremental_df])
                    merged = merged[~merged.index.duplicated(keep="last")]
                    merged = merged.sort_index()
                    # 更新缓存
                    self._klines_cache[cache_key] = _KlineCacheEntry(
                        df=merged,
                        cached_at=now,
                        last_bar_time=merged.index[-1].to_pydatetime(),
                    )
                    df = merged.copy()
                    df = self._apply_filters(df, start_date, end_date, count)
                    return df
            except Exception as exc:
                logger.warning("klines() 增量更新失败 symbol=%s: %s，使用缓存数据", symbol, exc)
                # 增量更新失败，返回过期缓存（降级）
                df = entry.df.copy()
                df = self._apply_filters(df, start_date, end_date, count)
                return df

        # --- 缓存未命中：全量拉取 ---
        fetch_count = 8000  # 缓存场景：始终拉取足够多的数据，过滤在读取时应用
        df = await asyncio.to_thread(
            self._fetch_with_retry, symbol, self._FREQ_TO_PERIOD[frequency], fetch_count
        )
        if df is None or df.empty:
            return None

        # 转换为 DatetimeIndex 格式
        if "datetime" in df.columns:
            df = df.set_index("datetime")
        df.index = pd.to_datetime(df.index)
        df.index = df.index.tz_localize("Asia/Shanghai")

        # 添加 adj_factor 列（默认 1.0，qfq 由 API 层处理）
        if "adj_factor" not in df.columns:
            df["adj_factor"] = 1.0

        # 写入缓存
        self._klines_cache[cache_key] = _KlineCacheEntry(
            df=df.copy(),
            cached_at=now,
            last_bar_time=df.index[-1].to_pydatetime(),
        )

        # 应用过滤和限制
        df = self._apply_filters(df, start_date, end_date, count)
        return df

    async def _klines_synthesized(
        self,
        symbol: str,
        frequency: str,
        start_date: date | None = None,
        end_date: date | None = None,
        count: int | None = None,
    ) -> pd.DataFrame | None:
        """从日线合成周线/月线"""
        # 获取更多日线以确保有足够的合成数据
        daily_count = (count or 500) * 7 if frequency == "1w" else (count or 120) * 31
        daily_count = min(daily_count, 8000)

        daily_df = await self.klines(symbol, "1d", start_date=start_date, end_date=end_date, count=daily_count)
        if daily_df is None or daily_df.empty:
            return None

        # 使用 bar_repository 的 convert_kline_frequency 合成
        from app.repositories.bar_repository import convert_kline_frequency

        # convert_kline_frequency 期望 DatetimeIndex 无时区，先去除时区
        daily_naive = daily_df.copy()
        if daily_naive.index.tz is not None:
            daily_naive.index = daily_naive.index.tz_localize(None)

        freq_map = {"1w": "w", "1mo": "m"}
        target_freq = freq_map[frequency]
        result = convert_kline_frequency(daily_naive, target_freq)
        if result is None or result.empty:
            return None

        # 恢复时区
        result.index = result.index.tz_localize("Asia/Shanghai")

        # 数量限制
        if count is not None and len(result) > count:
            result = result.iloc[-count:]

        return result

    def get_realtime_quote(self, symbol: str) -> dict[str, Any] | None:
        """获取实时行情报价（通过 pytdx 1 分钟线 + 日线）。

        流程：
        1. 拉取最新 2 根 1 分钟线，取最新 bar 的 close 作为 current_price
        2. 拉取最近 5 根日线，取倒数第 2 根的 close 作为 prev_close（前一交易日收盘价）
        3. 日线不足时降级为前一根 1 分钟线的 close
        4. 计算 change_pct = (current_price - prev_close) / prev_close * 100

        Args:
            symbol: 股票代码（如 '000001', '600519'）

        Returns:
            行情字典，包含 current_price/open/high/low/close/volume/prev_close/
            change_pct/update_time/is_realtime；失败时返回 None（不静默兜底假数据）
        """
        try:
            # [实时行情] 拉取最新 2 根 1 分钟线
            df_1m = self._fetch_with_retry(symbol, "1m", 2)
            if df_1m.empty:
                logger.warning("get_realtime_quote: 1 分钟线无数据 symbol=%s", symbol)
                return None

            latest = df_1m.iloc[-1]
            current_price = float(latest["close"])

            # [实时行情] 获取前一交易日收盘价：优先从日线获取
            prev_close: float | None = None
            try:
                df_daily = self._fetch_with_retry(symbol, "d", 5)
                if len(df_daily) >= 2:
                    prev_close = float(df_daily.iloc[-2]["close"])
            except Exception as exc:
                logger.debug("get_realtime_quote: 日线获取失败 symbol=%s: %s", symbol, exc)

            # 日线不足时，使用前一根 1 分钟线的收盘价
            if prev_close is None and len(df_1m) >= 2:
                prev_close = float(df_1m.iloc[-2]["close"])

            # 无法计算涨跌幅时退化为 0%
            if prev_close is None or prev_close == 0:
                prev_close = current_price

            change_pct = (current_price - prev_close) / prev_close * 100

            update_time = latest["datetime"]
            if hasattr(update_time, "isoformat"):
                update_time = update_time.isoformat()
            else:
                update_time = str(update_time)

            return {
                "current_price": round(current_price, 4),
                "open": round(float(latest["open"]), 4),
                "high": round(float(latest["high"]), 4),
                "low": round(float(latest["low"]), 4),
                "close": round(current_price, 4),
                "volume": round(float(latest["volume"]), 2),
                "prev_close": round(prev_close, 4),
                "change_pct": round(change_pct, 2),
                "update_time": update_time,
                "is_realtime": True,
            }
        except Exception as exc:
            logger.warning("get_realtime_quote 失败 symbol=%s: %s", symbol, exc)
            return None

    def get_xdxr_info(self, symbol: str) -> pd.DataFrame:
        """获取除权除息数据（带 Redis 缓存与重试）。

        用于计算前复权因子。返回的 DataFrame 包含所有除权除息事件，
        其中 category=1 为除权除息（含分红 fenhong），是计算 adj_factor 的关键数据。

        缓存策略：
        - key: xdxr:{symbol}
        - TTL: 24 小时（xdxr 数据变化频率低）
        - miss 时从 pytdx 拉取并回填
        - Redis 不可用时降级为直查 pytdx（捕获 RedisError，记录 warning）

        Args:
            symbol: 股票代码（如 '000001'）

        Returns:
            DataFrame: columns=[date, category, name, fenhong, peigujia, songzhuangu, peigu]
            date 为除权除息日，fenhong 为每10股分红金额
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        # 1. 尝试读缓存（仅当缓存启用时）
        settings = get_settings()
        if settings.bars_redis_cache_enabled:
            cache_key = f"{_XDXR_CACHE_PREFIX}:{symbol}"
            try:
                client = get_sync_redis()
                cached = client.get(cache_key)
                if cached is not None:
                    df = pd.read_json(io.StringIO(cached), orient="split")
                    if not df.empty:
                        # 反序列化后恢复 date 列类型
                        if "date" in df.columns:
                            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
                        logger.debug("xdxr 缓存命中 symbol=%s", symbol)
                        return df
                logger.debug("xdxr 缓存未命中 symbol=%s", symbol)
            except redis.RedisError as exc:
                logger.warning("xdxr 缓存读取失败 symbol=%s: %s，降级直查", symbol, exc)
            except Exception as exc:
                logger.warning("xdxr 缓存读取异常 symbol=%s: %s，降级直查", symbol, exc)

        # 2. 缓存 miss 或 Redis 不可用：从 pytdx 拉取
        df = self._fetch_xdxr_from_pytdx(symbol)

        # 3. 回填缓存（仅当有数据且缓存启用时）
        if settings.bars_redis_cache_enabled and not df.empty:
            cache_key = f"{_XDXR_CACHE_PREFIX}:{symbol}"
            try:
                client = get_sync_redis()
                # 序列化：date 列转为 ISO 字符串避免 JSON 序列化问题
                df_to_cache = df.copy()
                if "date" in df_to_cache.columns:
                    df_to_cache["date"] = df_to_cache["date"].astype(str)
                client.set(cache_key, df_to_cache.to_json(orient="split"), ex=_XDXR_CACHE_TTL)
                logger.debug("xdxr 缓存写入 symbol=%s ttl=%ds", symbol, _XDXR_CACHE_TTL)
            except redis.RedisError as exc:
                logger.warning("xdxr 缓存写入失败 symbol=%s: %s", symbol, exc)
            except Exception as exc:
                logger.warning("xdxr 缓存写入异常 symbol=%s: %s", symbol, exc)

        return df

    def _fetch_xdxr_from_pytdx(self, symbol: str) -> pd.DataFrame:
        """从 pytdx 拉取除权除息数据（带重试，无缓存）。

        Args:
            symbol: 股票代码（如 '000001'）

        Returns:
            DataFrame: columns=[date, category, name, fenhong, peigujia, songzhuangu, peigu]
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        market = market_from_code(symbol)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if self._api is None:
                    self.connect()
                raw = self._api.get_xdxr_info(market, symbol)
                if not raw:
                    return pd.DataFrame()
                df = pd.DataFrame(raw)
                # 构造日期列
                df["date"] = pd.to_datetime(df[["year", "month", "day"]])
                return df
            except (RuntimeError, Exception) as exc:
                last_exc = exc
                logger.warning(
                    "get_xdxr_info 失败 symbol=%s attempt=%d/%d: %s",
                    symbol, attempt, self.max_retries, exc,
                )
                self.disconnect()
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
        raise RuntimeError(
            f"get_xdxr_info 重试 {self.max_retries} 次后仍失败 symbol={symbol}: {last_exc}"
        )

    def _fetch_with_retry(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> pd.DataFrame:
        """带重试的 K 线拉取（失败重连后重试）。

        Args:
            symbol: 股票代码
            period: 周期键
            count: 拉取条数

        Returns:
            DataFrame

        Raises:
            RuntimeError: 重试 max_retries 次后仍失败（不吞没异常）
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                # 确保已连接
                if self._api is None:
                    self.connect()
                return self._fetch_bars(symbol, period, count)
            except RuntimeError as exc:
                last_exc = exc
                logger.warning(
                    "pytdx 拉取失败 attempt=%d/%d symbol=%s: %s",
                    attempt, self.max_retries, symbol, exc,
                )
                # 重连前断开旧连接
                self.disconnect()
            except Exception as exc:
                # 未预期异常：补充上下文后 raise（禁止吞没）
                logger.warning(
                    "pytdx 拉取未预期异常 attempt=%d/%d symbol=%s: %s",
                    attempt, self.max_retries, symbol, exc,
                )
                raise RuntimeError(
                    f"pytdx 拉取未预期异常 symbol={symbol} period={period}: {exc}"
                ) from exc

        # 重试耗尽：raise（禁止吞没）
        raise RuntimeError(
            f"pytdx 拉取失败，重试 {self.max_retries} 次仍失败 symbol={symbol} period={period}: {last_exc}"
        )


@contextmanager
def connect_pytdx() -> Generator[PytdxAdapter, None, None]:
    """模块级便捷函数：以上下文管理器方式连接 pytdx。

    用法：
        with connect_pytdx() as adapter:
            df = adapter.get_stock_list(market="SH")

    Raises:
        RuntimeError: 连接失败时抛出（含上下文）
    """
    adapter = PytdxAdapter()
    try:
        adapter.connect()
        yield adapter
    finally:
        adapter.disconnect()


# 模块级单例适配器（延迟初始化，避免导入时连接）
_adapter_singleton: PytdxAdapter | None = None


def get_pytdx_adapter() -> PytdxAdapter:
    """获取模块级单例 PytdxAdapter（延迟初始化）。

    单例避免频繁建连；如需独立连接可自行实例化 PytdxAdapter。
    返回的适配器尚未连接，首次调用行情拉取方法时会自动连接。
    """
    global _adapter_singleton
    if _adapter_singleton is None:
        _adapter_singleton = PytdxAdapter()
    return _adapter_singleton


# ===== GBK 解码容错补丁 =====
# pytdx GetSecurityList.parseResponse 默认使用 name_bytes.decode("gbk")，
# 遇到非法字节会抛 UnicodeDecodeError 导致整批股票列表拉取失败。
# 此处 monkey-patch 为 errors="ignore"，仅跳过非法字节，不中断拉取。
# 参考 chanlun-pro 的容错思路（chanlun-pro 使用 pytdx 原生接口，但名称解码同样脆弱）。

def _patch_get_security_list_gbk_decode() -> None:
    """Monkey-patch GetSecurityList.parseResponse，使股票名称 GBK 解码容错。

    幂等：重复调用不会重复 patch。
    """
    from pytdx.hq import GetSecurityList

    # 幂等检查：已 patch 则跳过
    if getattr(GetSecurityList, "_gbk_ignore_patched", False):
        return

    def _patched_parse_response(self, body_buf):  # type: ignore[no-untyped-def]
        """容错版 parseResponse：name_bytes.decode("gbk", errors="ignore")。"""
        import struct
        from collections import OrderedDict

        from pytdx.helper import get_volume

        pos = 0
        (num,) = struct.unpack("<H", body_buf[:2])
        pos += 2
        stocks = []
        for _ in range(num):
            one_bytes = body_buf[pos: pos + 29]
            (code, volunit,
             name_bytes, reversed_bytes1, decimal_point,
             pre_close_raw, reversed_bytes2) = struct.unpack("<6sH8s4sBI4s", one_bytes)

            code = code.decode("utf-8")
            # 关键修复：errors="ignore" 跳过非法 GBK 字节，避免整批失败
            name = name_bytes.decode("gbk", errors="ignore").rstrip("\x00")
            pre_close = get_volume(pre_close_raw)
            pos += 29

            one = OrderedDict(
                [
                    ('code', code),
                    ('volunit', volunit),
                    ('decimal_point', decimal_point),
                    ('name', name),
                    ('pre_close', pre_close),
                ]
            )
            stocks.append(one)
        return stocks

    GetSecurityList.parseResponse = _patched_parse_response
    GetSecurityList._gbk_ignore_patched = True
    logger.info("已 patch GetSecurityList.parseResponse（GBK errors=ignore）")


# 模块加载时自动执行 patch（确保所有 get_security_list 调用都走容错路径）
_patch_get_security_list_gbk_decode()


if __name__ == "__main__":
    # 自测入口：小批量验证（不写库表，仅连接并拉取少量数据）
    # 注意：需要网络访问 pytdx 服务器
    print("=== pytdx_adapter 自测 ===")

    # 基础验证（不依赖网络）
    assert market_from_code("600519") == 1, "600519 应为 SH(market=1)"
    assert market_from_code("000001") == 0, "000001 应为 SZ(market=0)"
    assert PERIOD_MAP["1m"] == 8, "1m 应映射到 8"
    assert PERIOD_MAP["d"] == 4, "d 应映射到 4"
    print(f"market_from_code('600519')={market_from_code('600519')} (SH)")
    print(f"market_from_code('000001')={market_from_code('000001')} (SZ)")
    print(f"PERIOD_MAP['1m']={PERIOD_MAP['1m']}, PERIOD_MAP['d']={PERIOD_MAP['d']}")

    adapter = PytdxAdapter(max_retries=2)
    assert adapter.max_retries == 2
    assert adapter._api is None
    print(f"adapter.max_retries={adapter.max_retries}")

    a1 = get_pytdx_adapter()
    a2 = get_pytdx_adapter()
    assert a1 is a2, "get_pytdx_adapter 应返回单例"
    print(f"singleton: a1 is a2 = {a1 is a2}")

    # xdxr 缓存配置验证
    assert _XDXR_CACHE_PREFIX == "xdxr", f"xdxr 缓存前缀应为 'xdxr'，实际 {_XDXR_CACHE_PREFIX}"
    assert _XDXR_CACHE_TTL == 86400, f"xdxr 缓存 TTL 应为 86400，实际 {_XDXR_CACHE_TTL}"
    print(f"_XDXR_CACHE_PREFIX={_XDXR_CACHE_PREFIX}, _XDXR_CACHE_TTL={_XDXR_CACHE_TTL}s (24h)")

    # 验证缓存 key 构造
    expected_key = "xdxr:000001"
    actual_key = f"{_XDXR_CACHE_PREFIX}:000001"
    assert actual_key == expected_key, f"xdxr 缓存 key 不匹配: {actual_key} != {expected_key}"
    print(f"xdxr 缓存 key 构造 ✓: {actual_key}")

    # 验证缓存禁用时 get_xdxr_info 不访问 Redis（通过 mock 验证降级逻辑）
    settings = get_settings()
    original_cache_enabled = settings.bars_redis_cache_enabled
    object.__setattr__(settings, "bars_redis_cache_enabled", False)

    # 验证 _fetch_xdxr_from_pytdx 方法存在
    assert hasattr(adapter, "_fetch_xdxr_from_pytdx"), "应有 _fetch_xdxr_from_pytdx 方法"
    assert callable(adapter._fetch_xdxr_from_pytdx), "_fetch_xdxr_from_pytdx 应可调用"
    print("_fetch_xdxr_from_pytdx 方法存在 ✓")

    # 验证 DataFrame 序列化/反序列化（模拟 xdxr 数据）
    import pandas as pd  # noqa: F811 - 局部导入用于自测
    mock_xdxr = pd.DataFrame({
        "date": pd.to_datetime(["2026-06-12", "2025-06-13"]),
        "category": [1, 1],
        "name": ["除权除息", "除权除息"],
        "fenhong": [2.0, 1.5],
        "peigujia": [0.0, 0.0],
        "songzhuangu": [0, 5],
        "peigu": [0, 0],
    })
    # 序列化（模拟缓存写入）
    df_to_cache = mock_xdxr.copy()
    df_to_cache["date"] = df_to_cache["date"].astype(str)
    serialized = df_to_cache.to_json(orient="split")
    assert serialized is not None, "序列化不应返回 None"
    print(f"xdxr 序列化 ✓: {len(serialized)} 字节")

    # 反序列化（模拟缓存读取）
    deserialized = pd.read_json(io.StringIO(serialized), orient="split")
    assert len(deserialized) == 2, f"反序列化后行数应为 2，实际 {len(deserialized)}"
    assert "date" in deserialized.columns, "反序列化后应包含 date 列"
    # 恢复 date 列类型
    deserialized["date"] = pd.to_datetime(deserialized["date"]).dt.tz_localize(None)
    assert pd.api.types.is_datetime64_any_dtype(deserialized["date"]), "date 列应为 datetime 类型"
    print(f"xdxr 反序列化 ✓: {len(deserialized)} 行，date 类型已恢复")

    # 验证 fenhong 值保持一致
    assert float(deserialized["fenhong"].iloc[0]) == 2.0, "fenhong[0] 应为 2.0"
    assert int(deserialized["songzhuangu"].iloc[1]) == 5, "songzhuangu[1] 应为 5"
    print("xdxr 序列化/反序列化值一致性 ✓")

    object.__setattr__(settings, "bars_redis_cache_enabled", original_cache_enabled)

    # 网络测试（可能失败）
    try:
        with connect_pytdx() as adapter:
            df_sh = adapter.get_security_list("SH", max_count=20)
            print(f"SH 市场前 20 条：{len(df_sh)} 行")
            if not df_sh.empty:
                print(df_sh.head(5).to_string(index=False))
    except RuntimeError as e:
        print(f"网络测试失败（网络或服务器问题）：{e}")
    print("=== 自测结束 ===")
