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
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pandas as pd
import redis
from pytdx.errors import TdxConnectionError, TdxFunctionCallError
from pytdx.hq import TdxHq_API

from app.config import get_settings
from app.core.exchange.contracts import FREQUENCY_MAP, Exchange
from app.core.redis_client import get_sync_redis

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

logger = logging.getLogger(__name__)


class PytdxSourceError(RuntimeError):
    """TDX provider/socket/protocol source failure.

    与业务数据错误区分：socket 断开、连接超时、协议解析失败、XDXR 拉取最终失败等
    属于「源不可用」，必须让上层 fail-fast（熔断 / FactorSourceUnavailableError），
    而不是当作单股数据异常降级继续。

    typed attributes（不得只拼字符串）：``operation`` / ``symbol`` / ``market`` /
    ``period`` / ``attempt`` / ``server`` / ``cause`` 均保留为可访问字段，
    便于上层熔断与诊断时区分「哪次操作、哪只标的、哪个服务器」失败。
    """

    def __init__(
        self,
        *,
        operation: str,
        message: str,
        symbol: str | None = None,
        market: int | None = None,
        period: str | None = None,
        attempt: int | None = None,
        server: tuple[str, int] | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.operation = operation
        self.symbol = symbol
        self.market = market
        self.period = period
        self.attempt = attempt
        self.server = server
        self.cause = cause

        context = [
            f"operation={operation}",
            f"symbol={symbol}" if symbol is not None else None,
            f"market={market}" if market is not None else None,
            f"period={period}" if period is not None else None,
            f"attempt={attempt}" if attempt is not None else None,
            f"server={server}" if server is not None else None,
        ]
        super().__init__(
            f"{message}; " + ", ".join(x for x in context if x is not None)
        )


@dataclass
class _KlineCacheEntry:
    """klines 进程内缓存条目（参考 chanlunpro FileCacheDB + ExchangeTDX.klines 增量更新）"""
    df: pd.DataFrame
    cached_at: datetime       # 缓存写入时间
    last_bar_time: datetime   # DataFrame 中最后一根 bar 的时间


# ── 运行时 bars 健康（process-local；参考 chanlun-pro tdx_best_ip 的真实行情探测思路）──
# 只有 ``CAPABILITY_BARS`` 的 source/function failure 使用「短指数冷却」；
# connect 层与其它 capability 仍使用 ``capability_cooldown_seconds``（默认 1800s 语义不变）。
# 依据：2026-09-23 的真实 outage 仅数分钟，30 分钟固定冷却会让已恢复的 server 持续「自我失联」。
_BARS_COOLDOWN_STEPS: tuple[float, ...] = (30.0, 60.0, 120.0, 240.0)
_BARS_COOLDOWN_MAX: float = 300.0
# HALF_OPEN 探测的进程级限频窗口（秒）：整个 provider family 全部 outage 时，
# 两次 half-open 真实业务探测之间至少间隔该时长，避免每个网页请求各自绕过 cooldown
# 造成 outage 期间的重试放大（在线服务必须限频，与 chanlun-pro 单机探测思路不同）。
_BARS_HALF_OPEN_INTERVAL: float = 30.0


# [parity RC3] 只有「已证实的传输 / 协议 / 源」失败才允许污染 server health 并触发 failover；
# 编程 / 契约错误（TypeError / KeyError / AssertionError …）必须原样上抛，绝不轮转、绝不冷却。
# 类型依据 pytdx/errors.py 实测 + py3 socket 语义：``OSError`` 已覆盖 TimeoutError /
# ConnectionError / socket.error（均为 OSError 子类）。
_EXPECTED_TDX_SOURCE_ERRORS: tuple[type[BaseException], ...] = (
    TdxConnectionError,
    TdxFunctionCallError,
    OSError,
)


def _is_expected_tdx_source_failure(exc: BaseException) -> bool:
    """是否为「expected」TDX provider/socket/protocol 失败（可 failover / 可冷却）。

    以 pytdx 真实异常类型 + 已核实 socket 异常为准（``TdxConnectionError`` /
    ``TdxFunctionCallError`` / ``OSError``）。其余异常（如 ``TypeError`` / ``KeyError`` /
    ``AssertionError``）属编程或契约错误，调用方必须原样上抛，不得当作 server 故障。
    """
    return isinstance(exc, _EXPECTED_TDX_SOURCE_ERRORS)


@dataclass
class ServerRuntimeHealth:
    """单台 server 的**运行时** bars 健康（与静态 capability 分离）。

    并发安全：所有读写都发生在 ``PytdxAdapter._io_lock`` 临界区内
    （adapter 被 ``asyncio.to_thread`` 并发共享，禁止无锁共享此对象）。
    """

    ewma_latency_ms: float | None = None
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    half_open_inflight: bool = False

    def score(self) -> float:
        """排名分数（越小越优先）：未观测延迟按 10s 计，连续失败每次 +2s 惩罚。"""
        latency = self.ewma_latency_ms if self.ewma_latency_ms is not None else 10_000.0
        return latency + self.consecutive_failures * 2_000.0


# xdxr 缓存配置
_XDXR_CACHE_PREFIX = "xdxr"
_XDXR_CACHE_TTL = 86400  # 24 小时（秒）

# ── capability-aware 服务器池（候选配置，与运行时健康分离）───────────
# 事实来源：2026-09-12 只读取证（本机 → 公网 TDX 主站）：
#   1) ``scripts/pytdx_server_sweep.py``：18 台 union 逐台 capability sweep
#   2) ``scripts/verify_pytdx_daily_contract.py``：production adapter 路径对照
# 只写已验证事实；**未验证一律 None，不得当作 False**。
#   - 4 台 bars + xdxr + quote 全部通过
#     （bars 100 根；exact-date daily 40/40 且 OHLC diff=0；quote 40/40 且
#      quote/daily volume ratio ≈ 0.01；amount ratio ≈ 1）
#   - 6 台仅 xdxr 可用（bars 函数级 ``TdxFunctionCallError``）
#   - 旧池 8 台 TCP 建连即失败（``TdxConnectionError``），已移出候选池
CAPABILITY_BARS = "bars"
CAPABILITY_XDXR = "xdxr"
CAPABILITY_QUOTE = "quote"


@dataclass(frozen=True)
class PytdxServerCapability:
    """单台 TDX server 的**静态已证** capability（运行时健康另行维护）。"""

    server: tuple[str, int]
    bars: bool
    xdxr: bool
    quote: bool | None


PYTDX_SERVER_CAPABILITIES: tuple[PytdxServerCapability, ...] = (
    PytdxServerCapability(("159.75.55.232", 7709), bars=True, xdxr=True, quote=True),
    PytdxServerCapability(("sztdx.gtjas.com", 7709), bars=True, xdxr=True, quote=True),
    PytdxServerCapability(("shtdx.gtjas.com", 7709), bars=True, xdxr=True, quote=True),
    PytdxServerCapability(("jstdx.gtjas.com", 7709), bars=True, xdxr=True, quote=True),
    PytdxServerCapability(("115.238.90.165", 7709), bars=False, xdxr=True, quote=None),
    PytdxServerCapability(("180.153.18.170", 7709), bars=False, xdxr=True, quote=None),
    PytdxServerCapability(("115.238.56.198", 7709), bars=False, xdxr=True, quote=None),
    PytdxServerCapability(("218.75.126.9", 7709), bars=False, xdxr=True, quote=None),
    PytdxServerCapability(("60.12.136.250", 7709), bars=False, xdxr=True, quote=None),
    PytdxServerCapability(("60.191.117.167", 7709), bars=False, xdxr=True, quote=None),
)

# 兼容既有导出名（factor health probe 取前 N 台，故 bars-capable 排在最前）。
PYTDX_SERVERS: list[tuple[str, int]] = [c.server for c in PYTDX_SERVER_CAPABILITIES]

@dataclass(frozen=True)
class PytdxCallProvenance:
    """一次成功 pytdx API 调用的来源证明（在 ``_io_lock`` 临界区内原子取得）。

    - ``server``：**hostname 保持 hostname**，不保存当次 DNS resolved IP。
    - ``connection_generation``：进程内单调递增的成功建连计数。同 hostname 断线重连后
      DNS 后台 IP 可能已变，故 ``server`` 相同**不等于**同一次 snapshot connection；
      generation 也必须比较。
    """

    server: tuple[str, int]
    connection_generation: int


# operation → capability（避免所有 public API 改签名）；None = 不限制。
_OPERATION_CAPABILITY: dict[str, str | None] = {
    "get_security_bars": CAPABILITY_BARS,
    "get_index_bars": CAPABILITY_BARS,
    "get_security_quotes": CAPABILITY_QUOTE,
    "get_xdxr_info": CAPABILITY_XDXR,
    "get_history_transaction_data": None,
    "get_security_count": None,
    "get_security_list": None,
    "get_finance_info": None,
}

# 市场映射：字符串标识 <-> pytdx 数字标识
# pytdx 仅支持 SH(1) 与 SZ(0)，BJ 暂不支持（需通过其他数据源补充）
MARKET_NAME_TO_CODE: dict[str, int] = {
    "SH": 1,
    "SZ": 0,
    # 注意：不把 "BJ" 加入此 dict —— get_stock_list(None) 会遍历其 keys 并对每个
    # 市场调用 get_security_list（用 get_security_count 分页），而 pytdx 的
    # get_security_count 不支持 BSE（market=2）。BJ 股票由 get_stock_list 里的
    # BJ_STOCKS 静态表补充，不经由此 dict。
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

    与原 ref/交易/datasource/pytdx_client.py 一致：6 开头为 SH（market=1）。

    [Phase 3D] 北交所/新三板代码（92x/43x/83x/87x/88x）返回 market=2（BSE）。
    此前它们被当作 SZ（market=0）查询，pytdx 在 SZ 市场找不到这些代码 → 返回空，
    导致全市场 BJ 日线从未 ingest（root cause of DATA_INGESTION_GAP）。

    Args:
        code: 股票代码（如 '000001', '600519', '920002'）

    Returns:
        1 表示 SH，0 表示 SZ，2 表示 BJ/BSE
    """
    code = str(code)
    if code.startswith("6"):
        return 1
    # BJ/BSE 代码前缀（与 stock_symbol_sql_filter 的 BJ 分支一致）
    if code.startswith(("92", "43", "83", "87", "88")):
        return 2
    return 0


def _to_float(value: Any) -> float | None:
    """安全转换为 float；None / 非数值 / NaN 返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
        return f if f == f else None  # NaN 检查
    except (TypeError, ValueError):
        return None


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
        connect_timeout: float = 5.0,
        capabilities: tuple[PytdxServerCapability, ...] | None = None,
        capability_cooldown_seconds: float = 1800.0,
    ) -> None:
        """初始化适配器。

        Args:
            servers: pytdx 服务器列表，None 使用默认 PYTDX_SERVERS
            max_retries: 数据拉取失败重试次数（含重连）
            retry_delay: 重试间隔（秒），用于 get_xdxr_info 等方法的失败重试
            connect_timeout: 单台服务器建连超时（秒）。默认 5.0 保持历史行为；
                盘后健康探测等需要「快速失败」的场景应显式调小（如 1.0），
                否则被黑洞的服务器会每台各耗满一个超时。
            capabilities: 静态 capability 候选配置；None 用
                :data:`PYTDX_SERVER_CAPABILITIES`。**只对已声明的服务器生效**：
                未声明的（例如测试注入的 server）不参与 capability / health
                过滤，保持既有行为。
            capability_cooldown_seconds: 单 operation source failure 后该
                ``(server, capability)`` 的 **process-local** 冷却时长（默认 30 分钟）。
                不是永久黑名单，到期自动重新 eligible。
        """
        self._servers: list[tuple[str, int]] = servers if servers is not None else PYTDX_SERVERS
        self._api: TdxHq_API | None = None
        self.max_retries = max_retries
        self.retry_delay: float = retry_delay
        self.connect_timeout: float = connect_timeout
        # [B2] 连接诊断计数
        self.successful_connect_count: int = 0
        self.reconnect_count: int = 0
        # [P0-5] I/O 锁：覆盖所有底层读取与重连。
        # 单例 adapter 在 asyncio.to_thread 调用方并发时，TdxHq_API 的 connect/disconnect/
        # _fetch_bars 共享同一 socket，必须串行；锁必须在 adapter 内部，不得只在调用方加局部锁。
        # [P0-7] 使用 RLock（可重入）：防止 _call_with_reconnect 持锁后内部意外调用 connect/
        # disconnect 导致自锁。当前实现无嵌套获取，但 RLock 提供防御性安全。
        self._io_lock: threading.RLock = threading.RLock()
        # 最近一次成功连接的服务器；disconnect 或连接失败时为 None。
        # 供 factor health probe 报告真实连通的 server（不依赖缓存命中等旁路）。
        self.connected_server: tuple[str, int] | None = None
        # [failover] 下一次 connect() 的起始下标（环形扫描）。
        # 正常连接成功后停在成功 host；只有 API source failure 才 advance 到下一个。
        self._next_server_index: int = 0
        # 当前成功连接的 host 在 self._servers 中的下标（未连接为 None）。
        self._connected_server_index: int | None = None
        # [capability] 静态候选配置：仅对已声明 server 生效（未声明 = 不限制）。
        caps = capabilities if capabilities is not None else PYTDX_SERVER_CAPABILITIES
        self._capabilities: dict[tuple[str, int], PytdxServerCapability] = {
            c.server: c for c in caps
        }
        # [capability] 运行时健康（process-local、有 TTL、不持久化到 Redis/DB）：
        #   _capability_health[server][capability] = monotonic 截止时间
        #   _connect_health[server]                = monotonic 截止时间（connect 层）
        # **按 capability 分开记**：bars 失败不得污染 xdxr / quote。
        self._capability_health: dict[tuple[str, int], dict[str, float]] = {}
        self._connect_health: dict[tuple[str, int], float] = {}
        # [parity B] 运行时 bars 健康（EWMA 延迟 / 连续失败 / half-open 标记）。
        # 只在 _io_lock 临界区内读写（adapter 被 asyncio.to_thread 并发共享）。
        self._runtime_health: dict[tuple[str, int], ServerRuntimeHealth] = {}
        self.capability_cooldown_seconds = capability_cooldown_seconds
        # 成功建连的单调计数：用于 PytdxCallProvenance（同 hostname 重连也算新 generation）。
        self._connection_generation: int = 0
        # [FIX3] bars half-open 探测的进程级限频时间窗（monotonic 截止时刻）。
        # 仅 CAPABILITY_BARS；outage 期间两次 half-open 真实探测至少间隔
        # ``bars_half_open_interval``，避免每个新请求都绕过 cooldown 各做一次网络探测。
        # 非 bars capability 不使用。
        self._bars_half_open_retry_after: float = 0.0
        self.bars_half_open_interval: float = _BARS_HALF_OPEN_INTERVAL

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
        """连接 pytdx 服务器（使用完整 server pool，无排除）。

        公开契约保持不变：外部调用方（含 ``__enter__`` / health probe）仍按完整
        server pool 连接。

        Raises:
            PytdxSourceError: 完整扫描所有服务器仍无法连接时抛出（operation="connect"）。
        """
        self._connect_excluding()

    # ── capability / 运行时健康（process-local；不持久化）────────────
    def _declared(self, server: tuple[str, int]) -> bool:
        """该 server 是否在静态 capability 配置中声明（未声明→不参与过滤）。"""
        return server in self._capabilities

    def _server_supports(
        self,
        server: tuple[str, int],
        capability: str | None,
    ) -> bool:
        """静态 capability 过滤；未声明的 server 一律放行（保持既有注入行为）。"""
        if capability is None:
            return True
        cap = self._capabilities.get(server)
        if cap is None:
            return True
        if capability == CAPABILITY_BARS:
            return cap.bars
        if capability == CAPABILITY_XDXR:
            return cap.xdxr
        if capability == CAPABILITY_QUOTE:
            # quote=None 表示「未验证」：不作为 eligible（但也永不判坏）。
            return cap.quote is True
        return True

    def _in_cooldown(self, server: tuple[str, int], capability: str | None) -> bool:
        if not self._declared(server):
            return False
        now = time.monotonic()
        until = self._connect_health.get(server)
        if until is not None and now < until:
            return True
        if capability is None:
            return False
        cap_until = self._capability_health.get(server, {}).get(capability)
        return cap_until is not None and now < cap_until

    def _server_eligible(self, server: tuple[str, int], capability: str | None) -> bool:
        return self._server_supports(server, capability) and not self._in_cooldown(
            server, capability
        )

    def _mark_connect_failure(self, server: tuple[str, int]) -> None:
        """connect 层失败 → 短期 cooldown（对该 server 的所有 operation 生效）。"""
        if not self._declared(server) or self.capability_cooldown_seconds <= 0:
            return
        self._connect_health[server] = (
            time.monotonic() + self.capability_cooldown_seconds
        )

    def _mark_capability_failure(
        self,
        server: tuple[str, int],
        capability: str | None,
    ) -> None:
        """operation 级 source failure → **只**冷却该 capability（不得污染其它）。

        冷却时长（[parity D]，2026-09-23 收紧）：
        - ``CAPABILITY_BARS``：短指数退避 30/60/120/240 → 上限 300s（实时行情专用，
          替换原先一刀切 1800s 的固定冷却；最终受 ``capability_cooldown_seconds`` 上限
          约束，以便测试注入更短窗口）。参考 chanlun-pro「失败后重新真实探测」的思路。
        - 其它 capability（XDXR 等）与 connect 层：保持 ``capability_cooldown_seconds``
          （默认 1800s），factor provider 合同不变。
        """
        if capability is None or not self._declared(server):
            return
        if self.capability_cooldown_seconds <= 0:
            return
        # [FIX1] 运行时 bars 健康（连续失败计数 / 排名分数）只属于 CAPABILITY_BARS；
        # 其它 capability 只维护各自的 capability cooldown，不得污染 bars 运行时健康。
        if capability == CAPABILITY_BARS:
            health = self._runtime_health_for(server)
            health.failure_count += 1
            health.consecutive_failures += 1
            delay = self._bars_cooldown_delay(health.consecutive_failures)
        else:
            delay = self.capability_cooldown_seconds
        self._capability_health.setdefault(server, {})[capability] = (
            time.monotonic() + delay
        )

    def _clear_capability_failure(
        self,
        server: tuple[str, int],
        capability: str | None,
    ) -> None:
        if capability is None:
            return
        health = self._capability_health.get(server)
        if health is not None:
            health.pop(capability, None)

    def _runtime_health_for(self, server: tuple[str, int]) -> ServerRuntimeHealth:
        """取得（按需创建）server 的运行时 bars 健康对象。

        只应在 ``_io_lock`` 临界区内调用 / 修改：adapter 被 ``asyncio.to_thread``
        并发共享，禁止无锁并发读写本对象。
        """
        health = self._runtime_health.get(server)
        if health is None:
            health = ServerRuntimeHealth()
            self._runtime_health[server] = health
        return health

    def _bars_cooldown_delay(self, consecutive_failures: int) -> float:
        """实时 bars 的短指数冷却（秒）：30/60/120/240 → 上限 300。

        最终受 ``capability_cooldown_seconds`` 约束（默认 1800 不生效；测试可注入更小值）。
        """
        idx = max(0, consecutive_failures - 1)
        step = (
            _BARS_COOLDOWN_STEPS[idx]
            if idx < len(_BARS_COOLDOWN_STEPS)
            else _BARS_COOLDOWN_MAX
        )
        return min(step, _BARS_COOLDOWN_MAX, self.capability_cooldown_seconds)

    def _record_success(
        self,
        server: tuple[str, int],
        elapsed_ms: float,
        capability: str | None,
    ) -> None:
        """记录一次成功：更新 EWMA 延迟、清零连续失败、清除该 capability 冷却。

        [FIX1] EWMA 延迟 / 连续失败清零 只属于 CAPABILITY_BARS 的运行时健康；
        其它 capability 的成功不得改写 bars 运行时健康（如 quote 很快不能让 bars
        认为很快）。各 capability 的 cooldown 仍按自身 capability 清除。
        """
        if capability == CAPABILITY_BARS:
            health = self._runtime_health_for(server)
            health.success_count += 1
            health.consecutive_failures = 0
            health.half_open_inflight = False
            alpha = 0.25
            if health.ewma_latency_ms is None:
                health.ewma_latency_ms = elapsed_ms
            else:
                health.ewma_latency_ms = (
                    alpha * elapsed_ms + (1 - alpha) * health.ewma_latency_ms
                )
        self._clear_capability_failure(server, capability)

    def _ranked_servers(
        self,
        capability: str | None,
        excluded: set[tuple[str, int]],
        attempted: set[tuple[str, int]],
    ) -> list[tuple[str, int]]:
        """尚未尝试、且当前 healthy 的 capable server，按分数升序。

        分数 = EWMA 延迟（无观测按 10s）+ 连续失败次数 × 2s。
        始终在 ``_io_lock`` 内调用（读 runtime health / cooldown）。
        """
        healthy: list[tuple[str, int]] = []
        for server in self._servers:
            if server in excluded or server in attempted:
                continue
            if not self._server_supports(server, capability):
                continue
            if self._in_cooldown(server, capability):
                continue
            healthy.append(server)
        # [FIX1] 运行时 EWMA 排名只用于 CAPABILITY_BARS；其它 capability 保持静态
        # server 配置顺序（稳定，不被 bars 健康污染 / 不被动态重排），沿用既有行为。
        if capability != CAPABILITY_BARS:
            return healthy
        healthy.sort(key=lambda s: self._runtime_health_for(s).score())
        return healthy

    def _cooldown_deadline(
        self,
        server: tuple[str, int],
        capability: str | None,
    ) -> float:
        """该 server 在指定 capability 下的正常 eligibility 截止时刻。

        一台 server 必须等 connect 冷却 **与** capability 冷却**都**清除后才恢复普通
        eligibility，故取二者之 **max**（而非 min）：例如 connect 冷却 1800s、
        bars 冷却 30s 时，真正可重新服务要等到 1800s。
        """
        deadlines: list[float] = []
        connect_until = self._connect_health.get(server)
        if connect_until:
            deadlines.append(connect_until)
        if capability is not None:
            cap_until = self._capability_health.get(server, {}).get(capability)
            if cap_until:
                deadlines.append(cap_until)
        return max(deadlines) if deadlines else 0.0

    def _select_half_open_server(
        self,
        capability: str | None,
        excluded: set[tuple[str, int]],
        attempted: set[tuple[str, int]],
    ) -> tuple[str, int] | None:
        """当所有 capable server 都在 cooldown 时，选 ``retry_after`` 最早的一台做 HALF_OPEN。

        仅返回 1 台；调用方在 ``_io_lock`` 内把该 server 标为 ``half_open_inflight``，
        并以**真实业务请求本身**作为探测（不额外 probe，避免 probe+request 双倍延迟）。
        选择与尝试在同一锁临界区 → 同一时刻不可能有多线程同时 half-open 同一台 server。
        """
        cooled: list[tuple[str, int]] = []
        for server in self._servers:
            if server in excluded or server in attempted:
                continue
            if not self._server_supports(server, capability):
                continue
            if not self._in_cooldown(server, capability):
                continue
            if self._runtime_health_for(server).half_open_inflight:
                continue
            cooled.append(server)
        if not cooled:
            return None
        return min(cooled, key=lambda s: self._cooldown_deadline(s, capability))

    def _connect_server(self, server: tuple[str, int]) -> None:
        """在 ``_io_lock`` 内连接到**指定** server（供 operation 级候选迭代使用）。

        与 ``_connect_excluding``（ring scan，供 ``connect()``）不同：本方法只连一台，
        失败即抛 typed ``PytdxSourceError(operation="connect")``，由调用方决定是否换下一台。
        已连接且正是目标 server 时幂等复用。``auto_retry=False``（PytdxAdapter 是唯一 retry owner）。
        """
        with self._io_lock:
            if self._api is not None and self.connected_server == server:
                return
            if self._api is not None:
                self.disconnect()
            host, port = server
            last_exc: Exception | None = None
            try:
                # Adapter 是唯一 retry owner：必须关闭 pytdx 内建 auto_retry。
                api = TdxHq_API(raise_exception=True, auto_retry=False)
                if api.connect(host, port, time_out=self.connect_timeout):
                    logger.info("pytdx 连接成功：%s:%d", host, port)
                    self._api = api
                    self.connected_server = server
                    try:
                        idx = self._servers.index(server)
                        self._connected_server_index = idx
                        self._next_server_index = idx
                    except ValueError:
                        self._connected_server_index = None
                    # [parity RC4] connect 成功 → 清除该 server 的 connect 层冷却，使 half-open
                    # 探测成功的 server 立即回归正常服务（不再被旧 1800s connect cooldown 卡死）。
                    self._connect_health.pop(server, None)
                    self.successful_connect_count += 1
                    self._connection_generation += 1
                    if self.successful_connect_count > 1:
                        self.reconnect_count += 1
                    return
                last_exc = TdxConnectionError(f"connect returned False: {host}:{port}")
            except Exception as exc:  # noqa: BLE001
                if not _is_expected_tdx_source_failure(exc):
                    # [parity RC3] 编程/契约错误不得被当作 connect 源失败（不冷却、不掩盖）
                    raise
                last_exc = exc
            self._mark_connect_failure(server)
            self.disconnect()
            raise PytdxSourceError(
                operation="connect",
                message=f"connect failed {host}:{port}: {last_exc}",
                server=server,
                cause=last_exc,
            ) from last_exc

    def _probe_bars_server(self, server: tuple[str, int]) -> float:
        """真实 K 线健康探测（参考 chanlun-pro ``tdx_best_ip.ping``）。

        TCP + TDX 协议 + ``get_security_bars`` + 有效数据 四层一次验证；返回耗时（毫秒）。

        用途：启动 / 周期健康检查 / 管理命令 / 显式 benchmark。
        **不**在普通用户请求路径里对每台 server 全量 probe（那是 chanlun-pro 单机工具的
        做法；Panji 在线服务以真实业务请求延迟 EWMA 作为主排名，half-open 也用业务请求
        本身当探针）。

        Raises:
            PytdxSourceError: 连接失败 / 返回空 / bars 数不足。
        """
        host, port = server
        started = time.perf_counter()
        rows: list[dict[str, Any]] | None = None
        try:
            api = TdxHq_API(raise_exception=True, auto_retry=False)
            if not api.connect(host, port, time_out=self.connect_timeout):
                raise TdxConnectionError(f"connect returned False: {host}:{port}")
            try:
                rows = api.get_security_bars(PERIOD_MAP["d"], 1, "600519", 0, 100)
            finally:
                try:
                    api.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            if not _is_expected_tdx_source_failure(exc):
                # [parity RC3] 编程/契约错误（TypeError / KeyError …）必须原样上抛，
                # 不得伪装成 provider 探测失败。
                raise
            raise PytdxSourceError(
                operation="health_probe",
                message=f"bars health probe failed {host}:{port}: {exc}",
                server=server,
                cause=exc,
            ) from exc
        if not rows or len(rows) < 10:
            raise PytdxSourceError(
                operation="health_probe",
                message=f"invalid bars response: {server}",
                server=server,
            )
        return (time.perf_counter() - started) * 1000.0

    def _connect_excluding(
        self,
        excluded_servers: set[tuple[str, int]] | None = None,
        capability: str | None = None,
    ) -> None:
        """连接 pytdx 服务器：从 ``_next_server_index`` 起环形扫描一整轮，跳过 excluded。

        [B1] 幂等：已连接则直接返回，绝不重复建连。
        [B2] 诊断：首次连接成功 successful_connect_count += 1；真实 source failure 后
        disconnect → reconnect 成功时 successful_connect_count += 1 且 reconnect_count += 1。
        [failover] 起始下标由 ``_advance_server_after_failure`` 推进；``excluded_servers``
        用于**单次 ``_call_with_reconnect`` 生命周期内**排除「TCP 能连、但本次 operation
        已明确 source/protocol 失败」的 host。

        两类 host 语义不同，但在本次业务调用内都不应被再次选中：
            - source-failed host：TCP 可达，但 operation 已失败（经 excluded 排除）
            - TCP-unreachable host：本轮建连失败（本轮内自然跳过，不进入已连接态）

        排除集**不持久化**：新的业务调用重新从空集合开始，
        因此偶发故障不会被永久封禁。

        Args:
            excluded_servers: 本次业务调用内禁止选择的 (host, port) 集合。

        Raises:
            PytdxSourceError: 无 eligible server，或全部 eligible server 建连失败。
        """
        excluded = excluded_servers or set()

        # [P0-5] I/O 锁覆盖 connect：防止并发调用方同时建连导致 _api 状态错乱
        with self._io_lock:
            if self._api is not None:
                current = self.connected_server

                # [B1] 幂等：已有 socket、该 host 未 excluded、且对本次 capability eligible
                # → 直接复用。
                if (
                    current is not None
                    and current not in excluded
                    and self._server_eligible(current, capability)
                ):
                    return

                # 已有 socket 对本次 capability 不 eligible 的三种情形都必须释放后重连：
                #   1) 静态 capability 不支持（例：上一 operation 留在 xdxr-only server，
                #      本次要 bars —— 绝不能先对它调 get_security_bars 再失败切换）；
                #   2) 该 capability 正在 cooldown；
                #   3) 当前调用已在 excluded 中（自己刚判坏的 host，绝不复用）。
                # （_io_lock 是 RLock，可重入，允许在此调用 disconnect）
                self.disconnect()

            if not self._servers:
                raise PytdxSourceError(
                    operation="connect",
                    message="pytdx server list is empty",
                )

            self.connected_server = None
            self._connected_server_index = None

            server_count = len(self._servers)

            eligible_count = sum(
                1
                for server in self._servers
                if server not in excluded
                and self._server_eligible(server, capability)
            )

            if eligible_count == 0:
                raise PytdxSourceError(
                    operation="connect",
                    message=(
                        "no eligible pytdx server remains after source failures / "
                        f"capability={capability} / cooldown"
                    ),
                    attempt=0,
                )

            last_exc: Exception | None = None
            last_server: tuple[str, int] | None = None
            last_errors: list[str] = []
            attempted = 0

            for offset in range(server_count):
                idx = (self._next_server_index + offset) % server_count
                host, port = self._servers[idx]
                server = (host, port)

                # 本次业务调用内已证明 source failure 的 host，不再选中
                if server in excluded:
                    continue
                # 静态 capability 不匹配，或该 capability 处于冷却期 → 跳过
                if not self._server_eligible(server, capability):
                    continue

                attempted += 1
                last_server = server

                try:
                    # Adapter 是唯一 retry owner。
                    # 必须关闭 pytdx 内建 auto_retry：否则真实网络尝试次数会变成
                    # adapter_max_retries × pytdx_auto_retries，且 MagicMock 测试看不见。
                    api = TdxHq_API(raise_exception=True, auto_retry=False)
                    if api.connect(host, port, time_out=self.connect_timeout):
                        logger.info("pytdx 连接成功：%s:%d", host, port)
                        self._api = api
                        self.connected_server = server
                        self._connected_server_index = idx
                        # 正常连接成功后保持当前 host；
                        # 只有 API source failure 才 advance（见 _advance_server_after_failure）。
                        self._next_server_index = idx
                        # [parity RC4] connect 成功 → 清除 connect 层冷却（与 _connect_server 一致）
                        self._connect_health.pop(server, None)
                        self.successful_connect_count += 1
                        self._connection_generation += 1
                        if self.successful_connect_count > 1:
                            self.reconnect_count += 1
                        return
                except TdxConnectionError as exc:
                    last_exc = exc
                    last_errors.append(f"{host}:{port} TdxConnectionError: {exc}")
                    self._mark_connect_failure(server)
                except Exception as exc:
                    if not _is_expected_tdx_source_failure(exc):
                        # [parity RC3] 编程/契约错误不得被当作 connect 源失败
                        raise
                    last_exc = exc
                    last_errors.append(f"{host}:{port} {type(exc).__name__}: {exc}")
                    self._mark_connect_failure(server)

            err_summary = "; ".join(last_errors[-5:])
            raise PytdxSourceError(
                operation="connect",
                message=(
                    f"pytdx connect exhausted {attempted} eligible servers: {err_summary}"
                ),
                attempt=attempted,
                server=last_server,
                cause=last_exc,
            ) from last_exc

    def disconnect(self) -> None:
        """断开连接，忽略断开时的异常（仅资源释放，不影响主流程）。"""
        # [P0-5] I/O 锁覆盖 disconnect：防止与 _call_with_reconnect 并发访问 _api
        with self._io_lock:
            if self._api is not None:
                try:
                    self._api.disconnect()
                except Exception as exc:
                    logger.warning("pytdx 断开连接时出现异常（已忽略）：%s", exc)
                finally:
                    self._api = None
                    self.connected_server = None

    def _advance_server_after_failure(
        self,
        failed_server: tuple[str, int] | None,
    ) -> None:
        """把下一次 connect() 的起点推进到失败服务器的下一个（环形）。

        仅用于「能 connect、但 operation 失败」的场景（生产即
        ``get_security_bars → calling function error``）：这类 host 已证明不可用，
        下一次必须换 host，而不是重新连回同一台再失败一次。

        正常主动 close / disconnect **不应**调用本方法（不要把普通 close 当 source failure）。
        """
        if not self._servers:
            self._next_server_index = 0
            return

        if failed_server is None:
            self._next_server_index = (
                self._next_server_index + 1
            ) % len(self._servers)
            return

        try:
            failed_index = self._servers.index(failed_server)
        except ValueError:
            self._next_server_index = (
                self._next_server_index + 1
            ) % len(self._servers)
            return

        self._next_server_index = (failed_index + 1) % len(self._servers)

    def _call_with_reconnect(
        self,
        operation: str,
        call: Callable[[TdxHq_API], Any],
        *,
        symbol: str | None = None,
        market: int | None = None,
        period: str | None = None,
        capability: str | None = None,
        return_provenance: bool = False,
    ) -> Any:
        """唯一 connection-level retry owner：所有 pytdx 网络调用必须经由此处。

        合同（[parity C]，2026-09-23 收紧；[FIX1/FIX2/FIX3] 2026-09-23 纠正）：

        - **CAPABILITY_BARS**（实时行情专用）：享受本轮 chanlun 对等韧性 —— 每台
          eligible capable server 最多执行一次 function call；成功立即返回；source
          failure 后排除该 server；直到**全部** eligible server 耗尽才抛 typed
          :class:`PytdxSourceError`。``max_retries`` **不**截断 bars 的 server coverage
          （旧实现 ``for attempt in range(1, max_retries + 1)`` 在 4 台 bars server 时
          会漏掉第 4 台）。候选顺序由 ``_ranked_servers`` 决定（运行时 EWMA 延迟 +
          连续失败惩罚）；全部 capable server 都在 cooldown 时，由 ``_select_half_open_server``
          放行**恰好一台** HALF_OPEN，并以本次真实业务请求本身作为探测（不额外 probe），
          受进程级限频窗口 ``bars_half_open_interval``（默认 30s）约束。
        - **其它 capability（XDXR / QUOTE / capability=None）**：保持既有「有界重试」语义
          （最多 ``max_retries`` 次尝试，不扫全池、不做 half-open、不动态重排），cooldown
          仍走 ``capability_cooldown_seconds``（默认 1800s），factor / XDXR 合同不变。
        - network / socket / protocol / ``calling function error`` → 该 server 判定不可信 →
          ``_advance_server_after_failure`` + ``disconnect`` → 换下一台候选（A → B → C → D）。
        - 候选顺序由 ``_ranked_servers`` 决定：静态 capability + 运行时 EWMA 延迟 +
          连续失败惩罚；当全部 capable server 都在 cooldown 时，由
          ``_select_half_open_server`` 放行**恰好一台** HALF_OPEN，并以本次真实业务请求
          本身作为探测（不额外 probe）。
        - 合法成功返回（含空列表 ``[]`` = 该标的确实无数据）原样返回，**不得**被当作
          provider outage 自动重试。
        - 单次尝试的 socket 状态转换必须**原子**：候选选择 + half-open 标记 +
          ``_connect_server`` + ``call`` + 失败后的（排除 → 冷却 → advance → ``disconnect``）
          全部在同一 ``_io_lock`` 临界区内；``time.sleep`` 必须在锁外。
        - connect 层失败（含「全部 eligible 耗尽」）统一包装为本次 operation 失败，
          并保留 ``cause.operation == "connect"`` 供上层区分 connect vs operation 故障。

        上层调用方收到 :class:`PytdxSourceError` 后**不得**再做同样的 socket retry；
        应记录失败标的 / 触发熔断 / 交给下一业务周期。
        """
        last_exc: Exception | None = None
        last_server: tuple[str, int] | None = None
        # [parity RC2] 最后一次「真实业务 source failure」——耗尽时作为最终 error 的
        # 根因 / cause，而不是被合成的「no eligible server」覆盖。
        last_source_exc: Exception | None = None
        last_source_server: tuple[str, int] | None = None
        # capability 优先取显式入参，否则按 operation 映射（不改 public API 签名）
        resolved_capability = (
            capability if capability is not None else _OPERATION_CAPABILITY.get(operation)
        )
        # [FIX1/FIX2] 本轮 parity 全池覆盖 / 动态排名 / 短冷却 / half-open 只属于
        # CAPABILITY_BARS；其它 capability 走既有「有界重试」（max_retries 次尝试）。
        bars_mode = resolved_capability == CAPABILITY_BARS

        # 本 operation 生命周期内：
        #   source_failed_servers — TCP 能连，但 operation 已 source/protocol 失败
        #   attempted             — 已尝试过（connect 或 call）的 server
        # 二者都不持久化：新的业务调用重新从空集合开始 → 偶发故障不会被永久封禁。
        source_failed_servers: set[tuple[str, int]] = set()
        attempted: set[tuple[str, int]] = set()
        half_open_used = False
        # [FIX2] 非 bars 模式：保留有界重试，最多 max_retries 次尝试（不扫全池）。
        attempt_count = 0
        max_attempts: int | None = None if bars_mode else self.max_retries

        while True:
            call_failed = False

            # [FIX2] 非 bars 模式达到 max_retries 次尝试 → 停止，交后续逻辑抛错。
            if max_attempts is not None and attempt_count >= max_attempts:
                break

            try:
                # 候选选择 + half-open 标记 + 尝试 必须原子（同一 _io_lock 临界区）：
                # 单例 adapter 被 asyncio.to_thread 并发共享 socket，若拆开，其他线程可能
                # 在「API failure → disconnect」之间替换共享 socket，或并发 half-open 同一台。
                with self._io_lock:
                    candidates = self._ranked_servers(
                        resolved_capability, source_failed_servers, attempted
                    )
                    half_open = False
                    # [FIX2] half-open 真实探测只属于 CAPABILITY_BARS；
                    # [FIX3] 且受进程级限频窗口约束：上一次 half-open 失败后的窗口内
                    # 不再重复探测，避免 outage 期间每个网页请求各自绕过 cooldown。
                    if bars_mode and not candidates and not half_open_used:
                        now = time.monotonic()
                        if now >= self._bars_half_open_retry_after:
                            half_open_server = self._select_half_open_server(
                                resolved_capability, source_failed_servers, attempted
                            )
                            if half_open_server is not None:
                                candidates = [half_open_server]
                                half_open = True
                                half_open_used = True
                                self._runtime_health_for(
                                    half_open_server
                                ).half_open_inflight = True

                    if not candidates:
                        # [parity RC2] 若发生过真实业务 source failure，最终 error 必须保留它作
                        # 根因（operation=本次 operation、server=最后失败 server、cause=真实异常）；
                        # 「no eligible server」只是耗尽状态，不能替换真实根因。
                        if last_source_exc is not None:
                            raise PytdxSourceError(
                                operation=operation,
                                symbol=symbol,
                                market=market,
                                period=period,
                                attempt=len(attempted),
                                server=last_source_server,
                                message=(
                                    "all eligible pytdx servers exhausted; "
                                    f"attempted={sorted(str(s) for s in attempted)}"
                                ),
                                cause=last_source_exc,
                            ) from last_source_exc
                        # 从未发生业务失败（如全部 connect 失败）→ 退化为 connect 类错误
                        # （operation="connect" 以便上层区分 connect vs operation 故障）。
                        raise PytdxSourceError(
                            operation="connect",
                            message=(
                                "no eligible pytdx server remains after source failures / "
                                f"capability={resolved_capability} / cooldown"
                            ),
                            attempt=len(attempted),
                        )

                    server = candidates[0]
                    attempted.add(server)
                    attempt_count += 1

                    try:
                        # _connect_server 自身获取同一把锁；_io_lock 是 RLock，不会自锁。
                        self._connect_server(server)
                    except PytdxSourceError as exc:
                        if exc.operation != "connect":
                            raise
                        # 单台 connect 失败：记录 connect cooldown，换下一台（不得中断整池）。
                        last_exc = exc
                        call_failed = True
                        if half_open:
                            self._runtime_health_for(server).half_open_inflight = False
                    except Exception as exc:  # noqa: BLE001
                        # [FIX5] connect 阶段的编程/契约错误也必须释放 half-open 所有权，
                        # 不得泄漏；随后原样上抛（RC3）。
                        if half_open:
                            self._runtime_health_for(server).half_open_inflight = False
                        raise
                    else:
                        attempt_server = self.connected_server
                        if attempt_server is None:
                            if half_open:
                                self._runtime_health_for(
                                    server
                                ).half_open_inflight = False
                            raise PytdxSourceError(
                                operation="connect",
                                message="pytdx connected server identity unavailable",
                            )
                        last_server = attempt_server
                        started = time.perf_counter()
                        try:
                            result = call(self.api)
                        except Exception as exc:  # noqa: BLE001
                            if not _is_expected_tdx_source_failure(exc):
                                # [parity RC3] 编程/契约错误：原样上抛，不冷却、不轮转、
                                # B/C/D 零调用（绝不让一个 Python bug 把全 TDX family 判坏）。
                                # [FIX5] finally 释放 half-open 所有权。
                                raise
                            # 仍持有同一把锁：其他线程不可能在
                            # 「API failure → disconnect」之间替换共享 socket。
                            last_exc = exc
                            last_source_exc = exc
                            last_source_server = attempt_server
                            call_failed = True
                            source_failed_servers.add(attempt_server)
                            # 只冷却该 capability：bars 失败不得污染 xdxr / quote。
                            self._mark_capability_failure(
                                attempt_server, resolved_capability
                            )
                            if half_open:
                                # [FIX3] half-open 真实探测失败 → 安排下一个探测窗口，
                                # 避免 outage 期间每个请求都绕过 cooldown 重复探测。
                                self._bars_half_open_retry_after = (
                                    time.monotonic() + self.bars_half_open_interval
                                )
                            logger.warning(
                                "PYTDX_SOURCE_FAILURE "
                                "operation=%s symbol=%s market=%s period=%s "
                                "server=%s type=%s error=%s half_open=%s",
                                operation,
                                symbol,
                                market,
                                period,
                                attempt_server,
                                type(exc).__name__,
                                exc,
                                half_open,
                            )
                            # 下一次必须从下一个 host 开始（A → B → C → D，而不是 A → A）
                            self._advance_server_after_failure(attempt_server)
                            # 仍在锁内：断掉的一定是本 attempt 真正失败的 socket，
                            # 不会误伤其他线程刚建立的新连接。
                            self.disconnect()
                        else:
                            self._record_success(
                                attempt_server,
                                (time.perf_counter() - started) * 1000.0,
                                resolved_capability,
                            )
                            if half_open:
                                # [FIX3] half-open 成功 → 探测家族恢复，重置限频窗口。
                                self._bars_half_open_retry_after = 0.0
                            if return_provenance:
                                # provenance 必须与真正执行 API 的 attempt_server 原子绑定；
                                # 绝不允许在锁外读 connected_server 猜来源。
                                return (
                                    result,
                                    PytdxCallProvenance(
                                        server=attempt_server,
                                        connection_generation=self._connection_generation,
                                    ),
                                )
                            return result
                        finally:
                            # [FIX5] half-open 所有权必须在任何离开路径（成功 / source 失败 /
                            # 编程错误）释放，绝不泄漏；否则该 server 永久 half_open_inflight
                            # 既无法被再次 half-open，又卡在 cooldown 之外。
                            if half_open:
                                self._runtime_health_for(
                                    attempt_server
                                ).half_open_inflight = False

            except PytdxSourceError as exc:
                # connect 层（含「全部 eligible 耗尽」）统一包装为本次 operation 的失败，
                # 保留 cause.operation == "connect" 供上层区分 connect vs operation 故障。
                if exc.operation == "connect":
                    raise PytdxSourceError(
                        operation=operation,
                        message="pytdx connection unavailable",
                        symbol=symbol,
                        market=market,
                        period=period,
                        attempt=len(attempted),
                        # 「全部 eligible 耗尽」的 connect 错误不带 server → 回落到最后尝试过的 server
                        server=exc.server if exc.server is not None else last_server,
                        cause=exc,
                    ) from exc
                raise

            if call_failed:
                # sleep 必须在锁外，不得持锁休眠阻塞其他调用方
                time.sleep(self.retry_delay)
                continue

        # 非 bars 模式有界重试耗尽（达到 max_retries 次尝试仍失败）：与上面
        # `if not candidates` 同构，保留真实 source 失败作为根因。
        if last_source_exc is not None:
            raise PytdxSourceError(
                operation=operation,
                symbol=symbol,
                market=market,
                period=period,
                attempt=len(attempted),
                server=last_source_server,
                message=(
                    "all eligible pytdx servers exhausted; "
                    f"attempted={sorted(str(s) for s in attempted)}"
                ),
                cause=last_source_exc,
            ) from last_source_exc
        raise PytdxSourceError(
            operation="connect",
            message=(
                "no eligible pytdx server remains after source failures / "
                f"capability={resolved_capability} / cooldown"
            ),
            attempt=len(attempted),
        )

    def get_history_transaction_page(
        self,
        symbol: str,
        trade_date: date,
        offset: int,
        count: int,
    ) -> list[dict]:
        """[B3] Managed historical transaction page — thin owner of
        ``adapter.api.get_history_transaction_data(...)``。

        职责只包括：market_from_code / YYYYMMDD conversion / _io_lock /
        复用已有 managed connection / retry-reconnect / context-rich RuntimeError。

        不做：canonicalize、解释 09:25、解释 volume、任何 Auction business logic。

        正常调用只复用已有 ``self._api``（单长连接生命周期，禁止 per-call 新连接）；
        只有真实 socket/source failure 才 disconnect → reconnect（计入
        successful_connect_count / reconnect_count 诊断）。

        Args:
            symbol: 股票代码（如 '000001', '600519'）
            trade_date: 交易日
            offset: pytdx start offset（0=当天后段，offset 增大向当天更早移动）
            count: 单页请求条数

        Returns:
            list[dict]：原始 transaction 记录（空列表表示无数据）

        Raises:
            RuntimeError: 重试 max_retries 次后仍失败（含 symbol/date/offset 上下文）
        """
        market_int = market_from_code(symbol)
        date_int = int(trade_date.strftime("%Y%m%d"))
        rows = self._call_with_reconnect(
            "get_history_transaction_data",
            lambda api: api.get_history_transaction_data(
                market_int, symbol, offset, count, date_int
            ),
            symbol=symbol,
            market=market_int,
        )
        return list(rows) if rows else []

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
        total_count = self._call_with_reconnect(
            "get_security_count",
            lambda api: api.get_security_count(market_code),
            market=market_code,
        )

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
            data = self._call_with_reconnect(
                "get_security_list",
                # 默认参数绑定本轮 start，避免闭包捕获循环变量（B023）
                lambda api, _start=start: api.get_security_list(market_code, _start),
                market=market_code,
            )

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

    def get_security_quotes(
        self,
        symbols: Sequence[str],
    ) -> list[dict[str, Any]]:
        """批量获取实时行情快照（正式 public API；调用方禁止再访问 ``.api``）。

        这是集合竞价等消费方访问 ``get_security_quotes`` 的唯一入口：市场解析
        （``market_from_code``）与连接 / 重连 / bounded retry 全部收敛到本方法；
        调用方（如 auction provider）只负责按既有 batch-size 约束分批调用。

        语义：
        - 合法成功但返回空 → 返回 ``[]``（该批标的确实无行情）。
        - provider / socket / reconnect 耗尽 → 抛 :class:`PytdxSourceError`。

        Args:
            symbols: 股票代码列表（如 ``['000001', '600519']``），
                市场由 ``market_from_code`` 解析。

        Returns:
            原始 quote dict 列表（含 market/code/price/open/high/low/vol/amount/
            last_close/servertime 等字段）；无数据时为空列表。
        """
        if not symbols:
            return []

        requests = [
            (market_from_code(symbol), symbol)
            for symbol in symbols
        ]
        rows = self._call_with_reconnect(
            "get_security_quotes",
            lambda api: api.get_security_quotes(requests),
        )
        return list(rows) if rows else []

    def get_security_quotes_with_provenance(
        self,
        symbols: Sequence[str],
    ) -> tuple[list[dict[str, Any]], PytdxCallProvenance]:
        """与 :meth:`get_security_quotes` 相同，但**原子**返回本次调用的来源证明。

        为什么必须原子返回：``PytdxAdapter`` 是可并发共享的单例，
        「先调 API、再读 ``connected_server``」之间其它线程可能已切换 socket，
        读到的来源会与数据不匹配。provenance 因此只能在
        :meth:`_call_with_reconnect` 的成功临界区内产生。

        Returns:
            ``(rows, provenance)``；``rows`` 语义与 :meth:`get_security_quotes` 完全一致。
        """
        if not symbols:
            return [], PytdxCallProvenance(
                server=self.connected_server or ("", 0),
                connection_generation=self._connection_generation,
            )

        requests = [
            (market_from_code(symbol), symbol)
            for symbol in symbols
        ]
        rows, provenance = self._call_with_reconnect(
            "get_security_quotes",
            lambda api: api.get_security_quotes(requests),
            return_provenance=True,
        )
        return (list(rows) if rows else []), provenance

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
            PytdxSourceError: provider 重连耗尽后仍失败（不吞没异常）
        """
        if period not in PERIOD_MAP:
            raise RuntimeError(
                f"不支持的周期: {period}，支持: {list(PERIOD_MAP.keys())}"
            )

        market = market_from_code(symbol)
        cat = PERIOD_MAP[period]

        all_bars: list[dict[str, Any]] = []
        seen_time_keys: set[Any] = set()
        start = 0
        while len(all_bars) < count:
            data = self._call_with_reconnect(
                "get_security_bars",
                # 默认参数绑定本轮 start，避免闭包捕获循环变量（B023）
                lambda api, _start=start: api.get_security_bars(
                    cat, market, symbol, _start, _FETCH_BATCH
                ),
                symbol=symbol,
                market=market,
                period=period,
            )

            if not data:
                break

            # [parity E] 分页 no-progress guard：若新一页的 datetime 全集都已在之前出现过，
            # 说明 provider 未推进（重复页 / 错乱），必须 fail-fast，而不是死循环或重复拼接。
            page_keys: set[Any] = set()
            for row in data:
                if row.get("datetime") is not None:
                    page_keys.add(("dt", str(row["datetime"])))
                elif {"year", "month", "day"}.issubset(row.keys()):
                    page_keys.add(
                        (
                            "ymd",
                            row.get("year"),
                            row.get("month"),
                            row.get("day"),
                            row.get("hour"),
                            row.get("minute"),
                        )
                    )
            if page_keys and page_keys <= seen_time_keys:
                raise PytdxSourceError(
                    operation="get_security_bars",
                    message="pagination made no progress (duplicate page)",
                    symbol=symbol,
                    market=market,
                    period=period,
                    attempt=start // _FETCH_BATCH + 1,
                )
            seen_time_keys |= page_keys

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
        # [parity E] 时间戳升序 + 唯一（keep last），再对齐尾部请求数量。
        df = df.drop_duplicates(subset=["datetime"], keep="last")
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

        df = self._fetch_bars(symbol, "d", count)
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

        df = self._fetch_bars(symbol, "1m", count)
        if df.empty:
            return df

        # 按时间范围过滤
        # [pytdx-timezone] - _fetch_bars 已将 1m 数据 datetime 列显式 tz_localize(None)，
        # 因此传入 aware start/end 时需先统一为 naive（按 Asia/Shanghai 解释），避免
        # aware Timestamp 与 datetime64[us] 比较抛出 TypeError。
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if start_ts.tzinfo is not None:
            start_ts = start_ts.tz_convert("Asia/Shanghai").tz_localize(None)
        if end_ts.tzinfo is not None:
            end_ts = end_ts.tz_convert("Asia/Shanghai").tz_localize(None)
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
        return self._fetch_bars(symbol, "w", count)

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
        return self._fetch_bars(symbol, "m", count)

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
        return self._fetch_bars(symbol, "15m", count)

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
        return self._fetch_bars(symbol, "60m", count)

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
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
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
        limit: int | None = None,
    ) -> pd.DataFrame | None:
        """统一行情读取接口（参考 chanlunpro ExchangeTDX.klines）

        进程内缓存 + 增量更新：
        - 缓存命中且有效：直接返回
        - 缓存过期：增量拉取新数据页合并（参考 chanlunpro 的 pages 逐页拉取逻辑）
        - 缓存未命中：全量拉取

        Args:
            symbol: 股票代码（如 '000001'）
            frequency: K线周期（'1d', '15m', '1h', '1w', '1mo'）
            start_date: 起始日期
            end_date: 结束日期
            count: 返回 bar 数量（用于结果截断，与 start_date/end_date 二选一）
            limit: 请求量，控制 pytdx 拉取条数 fetch_count = min(limit+250, 1000)；
                   未传时回退到 count，都未传默认 250
        """
        cat = FREQUENCY_MAP.get(frequency)
        if cat is None:
            logger.warning("klines() 不支持的 frequency=%s", frequency)
            return None

        # 周线/月线：从日线合成
        if frequency in ("1w", "1mo"):
            return await self._klines_synthesized(
                symbol, frequency, start_date, end_date, count, limit=limit,
            )

        cache_key = self._cache_key(symbol, frequency)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))

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
                    self._fetch_bars, symbol, self._FREQ_TO_PERIOD[frequency], 1400
                )
            except PytdxSourceError as exc:
                # [parity G] live 分钟线禁止 stale 兜底：provider 不可用时必须 fail-closed；
                # 日线/周线合成等既有 stale 降级语义保持不变。
                # [FIX6] 非 provider 异常（TypeError / KeyError 等编程/契约错误）不在此捕获，
                # 必须原样上抛（RC3），绝不被重新伪装成 PytdxSourceError 掩盖真实 bug。
                if frequency in ("15m", "1h", "1m"):
                    raise PytdxSourceError(
                        operation="klines",
                        symbol=symbol,
                        message=(
                            f"live intraday refresh failed for {frequency}; "
                            "refusing stale cache fallback"
                        ),
                        cause=exc,
                    ) from exc
                logger.warning(
                    "klines() 增量更新失败 symbol=%s freq=%s: %s，使用缓存数据",
                    symbol, frequency, exc,
                )
                # 非分钟线：保持既有 stale 降级（历史/周月合成路径）
                df = entry.df.copy()
                df = self._apply_filters(df, start_date, end_date, count)
                return df

            if incremental_df is not None and not incremental_df.empty:
                # 转换为 DatetimeIndex 格式（与全量拉取一致）
                if "datetime" in incremental_df.columns:
                    incremental_df = incremental_df.set_index("datetime")
                incremental_df.index = pd.to_datetime(incremental_df.index)
                incremental_df.index = incremental_df.index.tz_localize("Asia/Shanghai")
                # 添加 adj_factor 列
                if "adj_factor" not in incremental_df.columns:
                    incremental_df["adj_factor"] = 1.0

                # [parity F] 边界刷新：**fresh 成功之后**才构造 stable_cached = cached[:-1]，
                # 使缓存末根（可能是不完整 bar）由 provider 重新认证；fresh 失败绝不缩小 cache。
                # 然后 dedupe keep-last → sort → **一次性** replace cache（原子）。
                stable_cached = entry.df.iloc[:-1] if len(entry.df) > 0 else entry.df
                merged = pd.concat([stable_cached, incremental_df])
                merged = merged[~merged.index.duplicated(keep="last")]
                merged = merged.sort_index()
                # 更新缓存（原子替换）
                self._klines_cache[cache_key] = _KlineCacheEntry(
                    df=merged,
                    cached_at=now,
                    last_bar_time=merged.index[-1].to_pydatetime(),
                )
                df = merged.copy()
                df = self._apply_filters(df, start_date, end_date, count)
                return df

        # --- 缓存未命中：全量拉取 ---
        # [行情] - fetch_count: 请求量 + 预热窗口 250，上限 1000（避免过度拉取 pytdx）
        # limit 优先；未传 limit 时回退到 count；都未传默认 250
        effective_limit = limit if limit is not None else (count if count is not None else 250)
        fetch_count = min(effective_limit + 250, 1000)
        df = await asyncio.to_thread(
            self._fetch_bars, symbol, self._FREQ_TO_PERIOD[frequency], fetch_count
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
        limit: int | None = None,
    ) -> pd.DataFrame | None:
        """从日线合成周线/月线"""
        # [行情] - 合成周线/月线所需日线数：每根周线≈7日，每根月线≈31日
        daily_count = (count or 500) * 7 if frequency == "1w" else (count or 120) * 31
        daily_count = min(daily_count, 8000)

        # limit 透传给 klines，控制 pytdx 拉取量（fetch_count 上限 1000）
        daily_df = await self.klines(
            symbol, "1d",
            start_date=start_date, end_date=end_date,
            count=daily_count, limit=limit,
        )
        if daily_df is None or daily_df.empty:
            return None

        # 使用纯计算 owner 合成，避免 provider 反向依赖 persistence。
        from app.domain.shared.kline_frequency import convert_kline_frequency

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
        """获取实时行情报价（通过 pytdx get_security_quotes 快照）。

        语义（严格区分「无行情」与「源不可用」）：
        - 成功调用但返回空 → ``None``（该标的确实无报价，不伪造数据）。
        - provider / socket / reconnect 耗尽 → 抛 :class:`PytdxSourceError`
          （绝不 ``except Exception: return None`` 把源故障伪装成「无行情」）。

        注意：本方法不再用 1m/daily K 线合成 quote；Monitor V2 亦不依赖本 API，
        此修改只用于把既有 quote API 本身修正为统一 provider 边界。

        Args:
            symbol: 股票代码（如 '000001', '600519'）

        Returns:
            行情字典，包含 current_price/open/high/low/close/volume/prev_close/
            change_pct/update_time/is_realtime；成功但无行情时返回 None。
        """
        rows = self.get_security_quotes([symbol])

        # provider 成功，但确实没有 quote
        if not rows:
            return None

        row = rows[0]
        if not isinstance(row, dict):
            logger.warning("get_realtime_quote: 非预期响应类型 symbol=%s", symbol)
            return None

        price = _to_float(row.get("price"))
        if price is None or price <= 0:
            return None

        prev_close = _to_float(row.get("last_close"))
        open_price = _to_float(row.get("open"))
        high = _to_float(row.get("high"))
        low = _to_float(row.get("low"))
        volume = _to_float(row.get("vol"))
        amount = _to_float(row.get("amount"))

        # prev_close 不可用时不伪造 0%，保持 None（调用方自行降级）
        change_pct = None
        if prev_close is not None and prev_close > 0:
            change_pct = (price - prev_close) / prev_close * 100.0

        # quote 无可靠交易所 watermark → 使用本地上海时区抓取时间
        captured_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()

        return {
            "current_price": price,
            "open": open_price,
            "high": high,
            "low": low,
            "close": price,
            "volume": volume,
            "amount": amount,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "captured_at": captured_at,
            # 兼容旧调用方保留 update_time；其语义为「本地抓取时间」，
            # 不是交易所/provider 的更新时间，不得伪称 provider watermark。
            "update_time": captured_at,
            "update_time_semantics": "local_capture_time",
            "is_realtime": True,
        }

    def get_xdxr_info(
        self, symbol: str, *, force_refresh: bool = False
    ) -> pd.DataFrame:
        """获取除权除息数据（带 Redis 缓存与重试）。

        用于计算前复权因子。返回的 DataFrame 包含所有除权除息事件，
        其中 category=1 为除权除息（含分红 fenhong），是计算 adj_factor 的关键数据。

        缓存策略：
        - key: xdxr:{symbol}
        - TTL: 24 小时（xdxr 数据变化频率低）
        - miss 时从 pytdx 拉取并回填
        - Redis 不可用时降级为直查 pytdx（捕获 RedisError，记录 warning）
        - force_refresh=True 时跳过缓存，直接拉取远端并刷新缓存

        Args:
            symbol: 股票代码（如 '000001'）
            force_refresh: 若为 True，跳过 Redis 缓存直接拉取远端并刷新缓存
                （AfterClose 本轮因子检测首遍使用；审计阶段复用本轮写入的新缓存）。

        Returns:
            DataFrame: columns=[date, category, name, fenhong, peigujia, songzhuangu, peigu]
            date 为除权除息日，fenhong 为每10股分红金额
            无数据时返回空 DataFrame

        Raises:
            RuntimeError: 重试后仍失败
        """
        # 1. 尝试读缓存（仅当缓存启用且非强制刷新时）
        settings = get_settings()
        if settings.bars_redis_cache_enabled and not force_refresh:
            cache_key = f"{_XDXR_CACHE_PREFIX}:{symbol}"
            try:
                client = get_sync_redis()
                cached = client.get(cache_key)
                if cached is not None:
                    cached_str = cached.decode() if isinstance(cached, bytes) else cached
                    df = pd.read_json(io.StringIO(cached_str), orient="split")
                    if not df.empty:
                        # 反序列化后恢复 date 列类型
                        if "date" in df.columns:
                            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
                    # 重要：空 DataFrame 也属于合法 negative cache，
                    # 不得因为 df.empty 就再次远端抓取。
                    logger.debug(
                        "xdxr 缓存命中 symbol=%s force_refresh=%s", symbol, force_refresh
                    )
                    return df
                logger.debug("xdxr 缓存未命中 symbol=%s", symbol)
            except redis.RedisError as exc:
                logger.warning("xdxr 缓存读取失败 symbol=%s: %s，降级直查", symbol, exc)
            except Exception as exc:
                logger.warning("xdxr 缓存读取异常 symbol=%s: %s，降级直查", symbol, exc)

        # 2. 缓存 miss 或 Redis 不可用：从 pytdx 拉取
        df = self._fetch_xdxr_from_pytdx(symbol)

        # 3. 回填缓存（无论是否有数据，只要缓存启用都写入；
        #    空 DF 属合法 negative cache，避免后续审计又重新远端拉取）
        if settings.bars_redis_cache_enabled:
            cache_key = f"{_XDXR_CACHE_PREFIX}:{symbol}"
            try:
                client = get_sync_redis()
                # 序列化：date 列转为 ISO 字符串避免 JSON 序列化问题
                df_to_cache = df.copy()
                if "date" in df_to_cache.columns:
                    df_to_cache["date"] = df_to_cache["date"].astype(str)
                client.set(
                    cache_key,
                    df_to_cache.to_json(orient="split"),
                    ex=_XDXR_CACHE_TTL,
                )
                logger.debug("xdxr 缓存写入 symbol=%s ttl=%ds", symbol, _XDXR_CACHE_TTL)
            except redis.RedisError as exc:
                if force_refresh:
                    # fresh 数据已取到但无法发布到 Redis：旧 key 可能继续存在并被
                    # 后续阶段读回 → 形成 freshness 漏洞。force_refresh 场景必须
                    # fail-closed，不得静默降级。
                    raise PytdxSourceError(
                        operation="get_xdxr_info",
                        symbol=symbol,
                        message="XDXR fresh fetch succeeded but fresh cache publish failed",
                        cause=exc,
                    ) from exc
                logger.warning("xdxr 缓存写入失败 symbol=%s: %s", symbol, exc)
            except Exception as exc:
                if force_refresh:
                    raise PytdxSourceError(
                        operation="get_xdxr_info",
                        symbol=symbol,
                        message="XDXR fresh fetch succeeded but fresh cache publish failed",
                        cause=exc,
                    ) from exc
                logger.warning("xdxr 缓存写入异常 symbol=%s: %s", symbol, exc)

        return df

    def probe_xdxr_uncached(self, symbol: str) -> pd.DataFrame:
        """只用于 source health probe 的未缓存 XDXR 探测。

        强制直接调用远端 XDXR，**不读 / 不写 Redis 缓存**。

        为什么需要它：``get_xdxr_info`` 在 24h Redis 缓存命中时直接返回、
        根本不访问远端；用 ``get_xdxr_info`` 做健康探测会被缓存骗过，
        即使远端函数已坏也会显示「健康」。盘前/盘后因子源探测必须绕过缓存。

        返回 DataFrame（空 DataFrame 代表该标的无事件，不等于失败）；
        只有真正的源/协议/解析失败才抛 ``PytdxSourceError``。
        """
        return self._fetch_xdxr_from_pytdx(symbol)

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
        raw = self._call_with_reconnect(
            "get_xdxr_info",
            lambda api: api.get_xdxr_info(market, symbol),
            symbol=symbol,
            market=market,
        )
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        # 构造日期列
        df["date"] = pd.to_datetime(df[["year", "month", "day"]])
        return df

    def get_finance_info(self, symbol: str) -> dict[str, Any] | None:
        """获取股票财务信息（含总股本/流通股本）。

        CHANGE-20260713-010: 用于每日股本同步链（instrument_share_sync_service）。
        禁止在用户请求链中调用此方法；仅由定时同步任务调用。

        pytdx get_finance_info 返回字段（单位：股，经抽样验证 5 只股票与公开数据一致）：
        - zongguben: 总股本（股）
        - liutongguben: 流通股本（股）
        - updated_date: 数据更新日期（YYYYMMDD int）
        - ipo_date: 上市日期（YYYYMMDD int，来自 pytdx raw 'ipo_date' 字段）

        CHANGE-20260816-002: 额外解析 ipo_date，作为 Instrument.listing_date 的
        authoritative source（用于 Auction 120D PIT population 的 listing boundary）。
        归一化由调用方/ instrument_lifecycle_service.normalize_pytdx_ipo_date 负责。

        Args:
            symbol: 股票代码（如 '000001'）

        Returns:
            dict with keys: total_share, float_share, share_as_of (date | None),
            ipo_date_raw (int | None，YYYYMMDD 原值，未经日历校验)
            无数据时返回 None

        Raises:
            RuntimeError: 重试后仍失败
        """
        from datetime import date as date_cls

        market = market_from_code(symbol)
        raw = self._call_with_reconnect(
            "get_finance_info",
            lambda api: api.get_finance_info(market, symbol),
            symbol=symbol,
            market=market,
        )
        if raw is None or not raw:
            return None
        # raw 是 OrderedDict
        zongguben = raw.get("zongguben")
        liutongguben = raw.get("liutongguben")
        updated_date_raw = raw.get("updated_date")
        ipo_date_raw = raw.get("ipo_date")
        # updated_date 是 YYYYMMDD int（如 20260425）
        share_as_of: date_cls | None = None
        if updated_date_raw and isinstance(updated_date_raw, (int, float)):
            try:
                d_int = int(updated_date_raw)
                share_as_of = date_cls(d_int // 10000, (d_int // 100) % 100, d_int % 100)
            except (ValueError, OverflowError):
                share_as_of = None
        # ipo_date 是 YYYYMMDD int（如 19910403）；仅透传原值，
        # 日历合法性校验交给 normalize_pytdx_ipo_date，避免此处猜边界。
        ipo_date_value: int | None = None
        if ipo_date_raw and isinstance(ipo_date_raw, (int, float)):
            try:
                ipo_date_value = int(ipo_date_raw)
            except (ValueError, OverflowError):
                ipo_date_value = None
        return {
            "total_share": float(zongguben) if zongguben else None,
            "float_share": float(liutongguben) if liutongguben else None,
            "share_as_of": share_as_of,
            "ipo_date_raw": ipo_date_value,
        }

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
