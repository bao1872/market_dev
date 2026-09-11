"""多周期行情定时更新服务。

功能：
- 每个交易日 16:00 自动拉取全市场 active 股票的 d/15m/1h 行情
- 按周期分阶段处理：日线优先 → 覆盖率检查 → DSA 触发 → 15min → 60min
- 默认串行；显式配置后仅将同周期 provider I/O 放入有界 spawn ProcessPool
- 分批 upsert，幂等：upsert on_conflict_do_update
- 进度：tqdm 进度条（底部固定）
- 回补：使用 start_date 参数控制日线回补范围（默认 2023-01-01），15min/60min 使用 BACKFILL_COUNTS

设计说明：
- workers=1 复用 F1B-1 串行路径；workers>1 每次每日刷新复用一个 spawn pool
- d/post-d/15m/60m 严格分阶段，DB prepare/persistence 始终由 parent 串行执行
- 每日增量更新使用小 count（5/50/10），将耗时从约 2h 降至约 1.8h
- 回补使用大 count（500/15000/4000），耗时约 11.1h
- 失败重试 3 次，间隔 5 秒，不中断整体流程
- 日线是 adj_factor 的来源，必须定时刷新，否则前复权会失败
- 周线/月线不存储在 DB，从日线动态合成（convert_kline_frequency），不参与定时刷新
- 1m 不参与定时刷新/回补，仅在指标计算时按需查询
- 日线阶段完成后自动检查覆盖率，≥90% 时触发 DSA 选股（事件驱动）
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import multiprocessing
import pickle
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    import pandas as pd

from app.config import get_settings
from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.core.time import shanghai_business_date
from app.services.adjustment_factor_service import FactorSourceUnavailableError
from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    persist_daily_bars,
    persist_minute_provider_payload,
    prepare_daily_bars,
    refresh_15min_bars,
    refresh_60min_bars,
    refresh_daily_bars,
)
from app.services.bars_fetch_worker import (
    DailyProviderPayload,
    decode_period_bars_result,
    fetch_period_bars_task,
    init_worker,
)
from app.services.calendar_service import is_trading_day_async
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("bars_scheduler_service")

# 进程级内存缓存：active 股票列表（TTL 5 分钟）
# 多 worker 时各进程独立缓存，TTL 5 分钟可接受短暂不一致
_instruments_cache: list[Instrument] | None = None
_instruments_cache_ts: float = 0.0
_INSTRUMENTS_CACHE_TTL = 300  # 秒


def clear_instruments_cache() -> None:
    """清空股票列表内存缓存（供手动失效使用）。

    在 instruments 表发生变更（如新增/删除/状态变更）后调用，
    确保下次查询从 DB 重新加载。
    """
    global _instruments_cache, _instruments_cache_ts
    _instruments_cache = None
    _instruments_cache_ts = 0.0
    logger.info("股票列表内存缓存已清空")


@dataclass(frozen=True)
class FactorProviderHealth:
    """[FACTOR-HEALTH] 复权因子源（pytdx xdxr）健康探测结果。"""

    available: bool
    provider: str
    latency_seconds: float
    error: str | None
    # 探测尝试的服务器数量（默认取前 5 台，避免「前三台挂、第五台可用」的假阴性）。
    server_attempts: int = 0
    # 实际连上的服务器（host, port）；None 表示未连通。绕过 Redis 缓存旁路读取，
    # 反映真实远端连通情况。
    connected_server: tuple[str, int] | None = None
    # 未缓存 XDXR 探测是否成功（强制直连远端，不被 24h Redis 缓存骗过）。
    # None 表示未执行（如 connect 阶段已失败）。
    uncached_xdxr_ok: bool | None = None


async def probe_factor_provider(
    adapter: PytdxAdapter | None = None,
) -> FactorProviderHealth:
    """探测因子源是否可用；**快速失败**，绝不做整套 server list 重试。

    为什么必须在进入全市场因子阶段前探测：``_rebuild_factors_if_needed`` 会对
    5000+ 只股票逐只调 ``get_xdxr_info``。TDX 故障时那等于 5000+ 次 connect 重试，
    会把盘后任务拖死数十小时。探测把「几秒内明确失败」提前到循环之前。

    探测方式：
    1. ``connect()``（用 3 个候选服务器 + ``connect_timeout=1.0``）—— 这是**主信号**，
       必须真实建连，不受 Redis xdxr 缓存影响；
    2. 再调一次 ``get_xdxr_info("600519")`` —— 捕捉「连接成功但 xdxr 调用失败」。
       （该调用可能命中 Redis 24h 缓存，因此它只作为补充信号，不能替代第 1 步。）

    Args:
        adapter: 仅测试注入；默认新建一个独立探测适配器（不复用业务单例，
            避免把探测失败状态留在单例上）。
    """
    from app.core.pytdx_adapter import PYTDX_SERVERS

    # 取前 5 台服务器探测（而非前 3 台）。最坏约 5 秒量级（connect_timeout=1.0），
    # 但能避免「前三台挂、第五台可用」这类被前 3 台假阴性的情况。
    _FACTOR_PROBE_SERVER_LIMIT = 5
    _FACTOR_PROBE_CONNECT_TIMEOUT = 1.0

    probe = adapter or PytdxAdapter(
        servers=PYTDX_SERVERS[:_FACTOR_PROBE_SERVER_LIMIT],
        max_retries=1,
        retry_delay=0.0,
        connect_timeout=_FACTOR_PROBE_CONNECT_TIMEOUT,
    )

    def _run_probe() -> None:
        # connect() 必须真实建连，不受 Redis xdxr 缓存影响 —— 这是主信号。
        probe.connect()
        # 关键：必须走未缓存直连远端的 XDXR，否则会被 24h Redis 命中骗过
        # （缓存命中时 get_xdxr_info 根本不访问远端，函数调用坏了也显示「健康」）。
        df = probe.probe_xdxr_uncached("600519")
        # 空 DataFrame 代表没有事件，不能算失败；只有真正的源/协议/解析异常
        # 才会由 _fetch_xdxr_from_pytdx 抛 PytdxSourceError。
        if df is None:
            raise RuntimeError("uncached XDXR probe returned None")

    started = time.monotonic()
    try:
        await asyncio.to_thread(_run_probe)
        return FactorProviderHealth(
            available=True,
            provider="pytdx",
            latency_seconds=time.monotonic() - started,
            error=None,
            server_attempts=_FACTOR_PROBE_SERVER_LIMIT,
            connected_server=probe.connected_server,
            uncached_xdxr_ok=True,
        )
    except Exception as exc:  # noqa: BLE001 - 探测失败一律视为不可用
        return FactorProviderHealth(
            available=False,
            provider="pytdx",
            latency_seconds=time.monotonic() - started,
            error=f"{type(exc).__name__}: {exc}",
            server_attempts=_FACTOR_PROBE_SERVER_LIMIT,
            connected_server=probe.connected_server,
            uncached_xdxr_ok=False,
        )
    finally:
        try:
            await asyncio.to_thread(probe.disconnect)
        except Exception:  # noqa: BLE001 - 探测连接清理失败不影响结论
            pass


@dataclass
class RefreshResult:
    """单只股票刷新结果。"""

    instrument_id: uuid.UUID
    symbol: str
    success: bool
    error: str | None = None
    upsert_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class BatchResult:
    """批量刷新结果。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    failed_symbols: list[str] = field(default_factory=list)
    period_counts: dict[str, int] = field(default_factory=dict)
    # [BarsScheduler] - 日线阶段触发/复用的 DSA StrategyRun id，供 job_run.metadata_json 记录
    dsa_run_id: uuid.UUID | None = None
    # [JobRunEvent] - 日线覆盖率（日线阶段完成后填充，供 worker 写 DAILY_DONE 事件 payload）
    daily_covered: int | None = None
    daily_total: int | None = None
    daily_coverage: float | None = None
    # [BarsScheduler] - 描述: 跳过原因（如 NON_TRADING_DAY），供上游编排透传到 metadata
    skip_reason: str | None = None
    # [S3.1 CHANGE-20260718-007] - 因子一致性审计结果（日线阶段完成后填充）
    # 包含 total_audited / consistent / needs_rebuild / rebuilt / failed / errors
    factor_audit: dict[str, int] | None = None
    # [EOD-SNAPSHOT] - 盘后全市场快照快速路径可观测指标（诊断用，无 DB migration）
    snapshot_total: int = 0
    snapshot_valid_daily: int = 0
    universe_new: int = 0
    universe_updated: int = 0
    snapshot_upserted: int = 0
    daily_missing_after_snapshot: int = 0
    daily_fallback_attempted: int = 0
    daily_fallback_succeeded: int = 0
    new_instrument_backfilled: int = 0
    # [EOD-SNAPSHOT] fallback 结束后仍未覆盖 T 日的活跃 A 股数（真成功判定）。
    daily_missing_after_fallback: int = 0
    # [EOD-SNAPSHOT] 日线连续性扫描结果（只读诊断；>0 表示历史存在整日/大面积缺口）
    daily_continuity_gaps: int = 0
    # [EOD-SNAPSHOT] 缺口修复模式："market_wide_gap" | "sparse_symbol_gap"
    daily_repair_mode: str | None = None
    daily_mode: str | None = None  # "snapshot" | "legacy_fallback"
    # [FACTOR-HEALTH] 盘后因子源（pytdx xdxr）健康探测结果。
    # available=False 时 _run_post_daily_phase 必须 fail-closed，禁止跑全市场逐股
    # xdxr（否则 5000+ 次连接重试会把盘后拖死）。
    factor_source_available: bool | None = None
    factor_source_error: str | None = None
    factor_source_latency_seconds: float | None = None
    factor_source_connected_server: tuple[str, int] | None = None
    factor_source_uncached_xdxr_ok: bool | None = None


class PoolFatalError(RuntimeError):
    """The process pool cannot safely continue and must fail closed."""


class InstrumentRefreshExhaustedError(RuntimeError):
    """[F1B-2 P1-A] provider retries exhausted for one instrument/period.

    Raised by :meth:`BarsSchedulerService._refresh_one_period_with_retry` after
    ``MAX_RETRIES`` consecutive provider exceptions so callers can record a real
    failure.

    This signal is **deliberately explicit**: failure must never be inferred from
    ``upsert_count == 0``. A legitimate empty provider result is a *successful*
    refresh with 0 rows and must keep counting as ``succeeded``. Raising keeps the
    serial (workers=1) and parallel (workers>1) accounting identical: both turn an
    exhausted instrument into ``failed += 1`` plus an entry in ``failed_symbols``.
    """


@dataclass
class FactorSourceBreaker:
    """运行中因子源（pytdx xdxr）连续失败熔断器。

    用途：``_rebuild_factors_if_needed`` 在 5000+ 只股票循环里调
    ``detect_company_action_change``。若数据源中途掉线，应当**立即熔断**，
    而不是继续对剩余数千只股票逐股重试（那只会产生更多 provider error 并拖死盘后）。

    原则：
    - 业务数据错误（单股 xdxr 计算失败）不计入熔断，单股降级即可；
    - 连续 ``limit`` 次 ``CorporateActionProviderError``（源/连接/协议不可用）
      触发 ``open``，调用方应 raise ``FactorSourceUnavailableError``。
    """

    consecutive_provider_failures: int = 0
    total_provider_failures: int = 0
    limit: int = 3

    def success(self) -> None:
        self.consecutive_provider_failures = 0

    def failure(self) -> None:
        self.consecutive_provider_failures += 1
        self.total_provider_failures += 1

    @property
    def open(self) -> bool:
        return self.consecutive_provider_failures >= self.limit


@dataclass(frozen=True)
class _InstrumentItem:
    order: int
    instrument_id: uuid.UUID
    symbol: str


@dataclass
class _ParallelPhaseResult:
    succeeded: int = 0
    failed: int = 0
    upsert_count: int = 0
    failed_symbols: list[str] = field(default_factory=list)
    distinct_child_pids: set[int] = field(default_factory=set)
    submitted: int = 0
    attempts_completed: int = 0
    terminal_completed: int = 0
    max_inflight_observed: int = 0
    child_max_rss_kb: float = 0.0
    elapsed_seconds: float = 0.0


class BarsSchedulerService:
    """多周期行情调度服务。

    用法：
        # 每日增量更新
        service = BarsSchedulerService()
        result = await service.refresh_all_instruments(shanghai_business_date())

        # 历史回补
        result = await service.backfill_all_instruments(date(2023, 1, 1))
    """

    # 3 个周期（日线 + 日内周期；周线/月线从日线动态合成，不参与定时刷新）
    PERIODS = ["d", "15m", "60m"]

    # 每日增量更新的 count（只拉最新数据，减少拉取量）
    # 日线 count 表示回看天数，15min/60min 表示拉取条数
    DAILY_COUNTS: dict[str, int] = {"d": 5, "15m": 50, "60m": 10}

    # 回补的 count（回补到 2023-01-01 所需拉取量）
    # 日线回补使用 start_date 参数控制范围，count 不用于日线；15min/60min 使用 count
    BACKFILL_COUNTS: dict[str, int] = {"d": 500, "15m": 15000, "60m": 4000}

    # 失败重试
    MAX_RETRIES = 3
    RETRY_DELAY = 5  # 秒
    MAX_INFLIGHT_MULTIPLIER = 2

    # [EOD-SNAPSHOT] 每日增量更新 d 阶段是否走全市场收盘快照快速路径的**类级默认值**。
    # 生产默认开启；只验证 provider 重试语义 / 需要 legacy 逐股路径的测试可通过
    # monkeypatch 本属性（或构造参数 use_eod_snapshot=False）关闭。
    USE_EOD_SNAPSHOT_DEFAULT = True

    # 周期 → refresh 函数映射
    # 日线使用日期范围接口，15min/60min 使用 count 接口
    _REFRESH_FUNCS: dict[str, Callable[..., Awaitable[pd.DataFrame]]] = {
        "d": refresh_daily_bars,
        "15m": refresh_15min_bars,
        "60m": refresh_60min_bars,
    }

    def __init__(
        self,
        *,
        fetch_processes: int | None = None,
        adapter_spec: dict[str, Any] | None = None,
        use_eod_snapshot: bool | None = None,
    ) -> None:
        configured = (
            get_settings().bars_fetch_processes
            if fetch_processes is None
            else fetch_processes
        )
        if configured < 1 or configured > 8:
            raise ValueError("fetch_processes must be between 1 and 8")
        self.fetch_processes = configured
        self.adapter_spec = adapter_spec
        # [EOD-SNAPSHOT] 每日增量更新的 d 阶段是否走全市场收盘快照快速路径。
        # 关闭时退回逐股 provider 路径（用于 provider 不可用的运维场景与
        # 只验证 provider 重试语义的测试）。
        self.use_eod_snapshot = (
            self.USE_EOD_SNAPSHOT_DEFAULT
            if use_eod_snapshot is None
            else use_eod_snapshot
        )
        self._reset_process_metrics()

    def _reset_process_metrics(self) -> None:
        self.last_process_metrics: dict[str, Any] = {
            "workers": self.fetch_processes,
            "pool_creations": 0,
            "max_inflight_observed": 0,
            "distinct_child_pids": set(),
            "periods": {},
        }

    def _create_process_pool(self) -> ProcessPoolExecutor:
        context = multiprocessing.get_context("spawn")
        return ProcessPoolExecutor(
            max_workers=self.fetch_processes,
            mp_context=context,
            initializer=init_worker,
            initargs=(self.adapter_spec,),
        )

    async def refresh_all_instruments(
        self,
        trade_date: date,
        db_session: AsyncSession | None = None,
        job_run_id: uuid.UUID | None = None,
        *,
        trigger_dsa: bool = True,
        periods: tuple[str, ...] | None = None,
    ) -> BatchResult:
        """每日增量更新：默认串行，可配置有界 provider ProcessPool。

        使用 DAILY_COUNTS，耗时约 1.8 小时。

        Args:
            trade_date: 交易日期
            db_session: 可选的 DB 会话（不传则内部创建）
            job_run_id: 可选的 SchedulerJobRun.id，传入时在日线阶段完成/DSA 触发后
                写入 job_run_events 时间线事件（DAILY_DONE / DSA_CREATED）
            trigger_dsa: [Phase8A] True=日线完成后触发DSA（16:00独立路径）；
                False=仅刷新行情+计算覆盖率，不触发DSA（orchestrator调用时使用，
                DSA由orchestrator在computing_features步骤创建并inline claim）
            periods: 显式限定刷新周期；None（默认）= DAILY_COUNTS 的全部周期
                （d + 15m + 60m），与历史行为完全一致。
                盘后编排（after_close_orchestrator）只传 ``("d",)``：当前盘后 Core
                是 daily-only，15m/60m 不应在盘后主链刷新（它们的更新能力保留给
                独立 bars scheduler 与手工更新，不是删除）。

        Returns:
            BatchResult: 批量刷新结果
        """
        self._reset_process_metrics()
        logger.info(
            "开始每日增量更新 trade_date=%s trigger_dsa=%s periods=%s",
            trade_date,
            trigger_dsa,
            periods if periods is not None else tuple(self.DAILY_COUNTS),
        )
        return await self._process_all_instruments(
            trade_date=trade_date,
            counts=self.DAILY_COUNTS,
            db_session=db_session,
            task_name="每日增量更新",
            job_run_id=job_run_id,
            trigger_dsa=trigger_dsa,
            requested_periods=periods,
        )

    async def backfill_all_instruments(
        self,
        start_date: date = date(2023, 1, 1),
        db_session: AsyncSession | None = None,
    ) -> BatchResult:
        """历史回补：串行拉取全市场历史数据。

        使用 BACKFILL_COUNTS，耗时约 11.1 小时。
        日线回补范围由 start_date 参数控制（默认 2023-01-01），
        15min/60min 仍使用 BACKFILL_COUNTS 中的 count。

        Args:
            start_date: 日线回补起始日期（默认 2023-01-01），真正控制日线回补范围
            db_session: 可选的 DB 会话（不传则内部创建）

        Returns:
            BatchResult: 批量刷新结果
        """
        logger.info("开始历史回补 start_date=%s", start_date)
        return await self._process_all_instruments(
            trade_date=start_date,
            counts=self.BACKFILL_COUNTS,
            db_session=db_session,
            task_name="历史回补",
            start_date=start_date,
        )

    # [BarsScheduler] - 分阶段处理顺序：日线优先，便于尽早触发 DSA
    PHASE_ORDER = ["d", "15m", "60m"]

    async def _process_all_instruments(
        self,
        trade_date: date,
        counts: dict[str, int],
        db_session: AsyncSession | None,
        task_name: str,
        start_date: date | None = None,
        job_run_id: uuid.UUID | None = None,
        *,
        trigger_dsa: bool = True,
        requested_periods: tuple[str, ...] | None = None,
    ) -> BatchResult:
        """处理全市场股票的多周期行情刷新（按周期分阶段）。

        分阶段执行：
        1. Phase 1: 全部标的日线刷新
        2. 日线完成后检查覆盖率，满足阈值则自动触发 DSA 选股（trigger_dsa=True 时）
        3. Phase 2: 全部标的 15min 刷新
        4. Phase 3: 全部标的 60min 刷新

        Args:
            trade_date: 交易日期
            counts: 各周期的拉取条数
            db_session: 可选的 DB 会话
            task_name: 任务名称（用于日志）
            start_date: 日线回补起始日期（仅回补模式使用，None 时用 count 模式）
            job_run_id: 可选的 SchedulerJobRun.id，传入时在日线阶段完成/DSA 触发后
                写入 job_run_events 时间线事件
            trigger_dsa: [Phase8A] False 时仅计算覆盖率不触发DSA（orchestrator调用）
            requested_periods: 显式限定周期；None=counts 的全部周期（历史行为）

        Returns:
            BatchResult: 批量刷新结果

        Raises:
            ValueError: ``requested_periods`` 含未知周期或为空（禁止静默 no-op）。
            DailyContinuityBlockedError: 日线连续性硬门禁未通过。
            FactorSourceUnavailableError: 因子源不可用（盘后 fail-closed）。
        """
        # 0. periods 参数校验：禁止「periods=("foo",) → 什么都没跑 → 全部计为成功」。
        if requested_periods is not None:
            unknown = set(requested_periods) - set(self.PHASE_ORDER)
            if unknown:
                raise ValueError(f"unsupported refresh periods: {sorted(unknown)}")
            if not requested_periods:
                raise ValueError("requested_periods must not be empty")

        # 1. 交易日检查（仅对每日增量更新，回补不检查）
        if task_name == "每日增量更新":
            if db_session is not None:
                is_trading = await is_trading_day_async(db_session, trade_date)
            else:
                async with AsyncSessionLocal() as session:
                    is_trading = await is_trading_day_async(session, trade_date)
            if not is_trading:
                logger.info("非交易日，跳过 %s trade_date=%s", task_name, trade_date)
                # [BarsScheduler] - 非交易日返回带 skip_reason 的空结果，供上游编排透传到 metadata
                return BatchResult(skip_reason="NON_TRADING_DAY")

        # 2. 确定运行模式与周期集合（必须在读取 universe 之前，因为
        #    「快照发现新股」是 universe 为空时唯一能自愈的路径）。
        is_daily_refresh = task_name == "每日增量更新"
        active_periods = [
            p
            for p in self.PHASE_ORDER
            if p in counts and (requested_periods is None or p in requested_periods)
        ]
        # 快照发现仅在「每日增量 + 启用快照 + 本轮真的要跑 d」时成立。
        snapshot_discovery_enabled = (
            is_daily_refresh and self.use_eod_snapshot and "d" in active_periods
        )

        # 3. 查询全市场 active 股票
        instruments = await self._get_active_instruments(db_session)
        if not instruments:
            if not snapshot_discovery_enabled:
                logger.warning("无 active 股票可处理")
                return BatchResult()
            # 旧 universe 为空：唯一可行路径是快照发现。继续执行 d 阶段，
            # 由快照失败时的 fail-closed 分支决定结果，绝不用空列表伪装成功。
            logger.warning(
                "[EOD-SNAPSHOT] active universe 为空，改由全市场快照发现新股"
            )

        total = len(instruments)
        logger.info("%s: 共 %d 只股票，按周期分阶段处理", task_name, total)

        result = BatchResult(total=total)
        for period in active_periods:
            result.period_counts[period] = 0

        # legacy daily provider 的目标结束日（仅 period="d" 使用）：
        # - 每日增量（start_date is None）：目标交易日 trade_date。重跑历史 T 时必须
        #   拉 T，禁止把 end_date 悄悄写成今天（那会把历史 T 的日线污染成今天的）。
        # - 历史回补（start_date 给定）：今天，保持「补到当前」的历史语义不变。
        legacy_daily_end_date = (
            shanghai_business_date() if start_date is not None else trade_date
        )

        parallel_enabled = is_daily_refresh and self.fetch_processes > 1
        # [LAZY-POOL] ProcessPool 延迟创建：snapshot-only 的盘后 daily 刷新
        # （periods=("d",) 且快照成功）完全不进入 parallel 分支，因此不应创建 pool。
        # 只有真的要走 _run_parallel_period 时才创建。
        process_pool: ProcessPoolExecutor | None = None

        def ensure_process_pool() -> ProcessPoolExecutor:
            nonlocal process_pool
            if process_pool is None:
                process_pool = self._create_process_pool()
                self.last_process_metrics["pool_creations"] += 1
            return process_pool

        items = [
            _InstrumentItem(i, instrument.id, instrument.symbol)
            for i, instrument in enumerate(instruments)
        ]

        try:
            for phase_idx, period in enumerate(active_periods):
                phase_name = f"{task_name} [{period}]"
                logger.info(
                    "Phase %d/%d 开始: period=%s instruments=%d workers=%d",
                    phase_idx + 1,
                    len(active_periods),
                    period,
                    total,
                    self.fetch_processes if parallel_enabled else 1,
                )

                # [EOD-SNAPSHOT] 每日增量模式：日线阶段优先走全市场快照快速路径。
                # 成功则跳过逐股取数；失败（SnapshotProviderError）退回 legacy 逐股路径。
                ran_fast_path = False
                if snapshot_discovery_enabled and period == "d":
                    from app.services.eod_market_snapshot_provider import (
                        SnapshotProviderError,
                        can_use_same_day_eod_snapshot,
                    )

                    if not can_use_same_day_eod_snapshot(trade_date):
                        # 过去交易日重跑 / 盘中提前触发：实时快照不是 T 日终值，
                        # 必须走 legacy（historical）路径，禁止拿今天的快照写历史日线。
                        result.daily_mode = "legacy_fallback"
                        logger.warning(
                            "[EOD-SNAPSHOT] trade_date=%s 不在当日收盘窗口"
                            "（>=15:05 且为当天），跳过快照，改用 historical daily 路径",
                            trade_date,
                        )
                    else:
                        try:
                            await self._refresh_daily_from_market_snapshot(
                                trade_date, db_session, job_run_id, result,
                            )
                            result.daily_mode = "snapshot"
                            # universe 可能新增 → 清空缓存并重新读取，供后续 post-daily
                            # 阶段与 15m/60m 阶段使用；items/result.total 必须同步重建，
                            # 否则后续阶段与最终统计仍按旧 universe 口径。
                            clear_instruments_cache()
                            instruments = await self._get_active_instruments(db_session)
                            total = len(instruments)
                            result.total = total
                            items = [
                                _InstrumentItem(i, instrument.id, instrument.symbol)
                                for i, instrument in enumerate(instruments)
                            ]
                            ran_fast_path = True
                        except SnapshotProviderError as exc:
                            logger.warning(
                                "[BarsScheduler] 快照 provider 失败，退回 legacy daily 路径: %s",
                                exc,
                            )
                            result.daily_mode = "legacy_fallback"

                if not ran_fast_path:
                    if snapshot_discovery_enabled and period == "d" and not instruments:
                        # 旧 universe 为空 + 快照不可用/失败：既不能发现新股，也没有
                        # 可逐股刷新的标的。禁止返回空 BatchResult 伪装成功。
                        raise RuntimeError(
                            "EMPTY_UNIVERSE_AND_SNAPSHOT_UNAVAILABLE"
                        )
                    if not parallel_enabled:
                        phase_succeeded, phase_failed = await self._run_serial_period(
                            instruments,
                            period=period,
                            count=counts[period],
                            db_session=db_session,
                            start_date=start_date,
                            end_date=legacy_daily_end_date,
                            result=result,
                            phase_name=phase_name,
                        )
                    else:
                        phase = await self._run_parallel_period(
                            ensure_process_pool(),
                            items,
                            period=period,
                            count=counts[period],
                            db_session=db_session,
                            start_date=start_date,
                            end_date=legacy_daily_end_date,
                        )
                        phase_succeeded = phase.succeeded
                        phase_failed = phase.failed
                        result.period_counts[period] += phase.upsert_count
                        result.failed_symbols.extend(phase.failed_symbols)
                        self._record_parallel_metrics(period, phase)

                    logger.info(
                        "Phase %d/%d 完成: period=%s succeeded=%d failed=%d upsert=%d",
                        phase_idx + 1,
                        len(active_periods),
                        period,
                        phase_succeeded,
                        phase_failed,
                        result.period_counts[period],
                    )
                if is_daily_refresh and period == "d":
                    # [HARD GATE] 日线连续性。无论 daily 来自 snapshot、legacy 逐股还是
                    # 未来的 provider，都必须过同一门禁，然后才允许进入
                    # 因子重建 / 审计 / DSA / Core。
                    from app.services.eod_daily_refresh_service import (
                        DailyContinuityBlockedError,
                    )

                    gaps = await self._scan_daily_continuity_gate(
                        trade_date, db_session, result
                    )
                    if gaps:
                        raise DailyContinuityBlockedError(gaps)

                    await self._run_post_daily_phase(
                        trade_date,
                        instruments,
                        db_session,
                        job_run_id,
                        result,
                        trigger_dsa=trigger_dsa,
                    )
        finally:
            if process_pool is not None:
                await asyncio.to_thread(
                    process_pool.shutdown, wait=True, cancel_futures=True
                )

        # 汇总 succeeded/failed（按标的维度：任一周期失败即计为 failed）
        failed_set = set(result.failed_symbols)
        result.failed_symbols = [item.symbol for item in items if item.symbol in failed_set]
        result.succeeded = total - len(result.failed_symbols)
        result.failed = len(result.failed_symbols)

        logger.info(
            "%s 完成: total=%d succeeded=%d failed=%d period_counts=%s",
            task_name, result.total, result.succeeded, result.failed, result.period_counts,
        )
        return result

    async def _run_serial_period(
        self,
        instruments: list[Instrument],
        *,
        period: str,
        count: int,
        db_session: AsyncSession | None,
        start_date: date | None,
        end_date: date,
        result: BatchResult,
        phase_name: str,
    ) -> tuple[int, int]:
        """F1B-1 serial fallback; no ProcessPool is created for workers=1/backfill."""
        succeeded = 0
        failed = 0
        for instrument in instruments:
            try:
                upsert_count = await self._refresh_one_period_with_retry(
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    period=period,
                    count=count,
                    db_session=db_session,
                    start_date=start_date,
                    end_date=end_date,
                )
                result.period_counts[period] += upsert_count
                succeeded += 1
            except Exception as exc:
                failed += 1
                result.failed_symbols.append(instrument.symbol)
                logger.warning(
                    "%s 异常 symbol=%s period=%s: %s",
                    phase_name,
                    instrument.symbol,
                    period,
                    exc,
                )
        return succeeded, failed

    @staticmethod
    def _build_provider_request(
        item: _InstrumentItem,
        *,
        period: str,
        count: int,
        start_date: date | None,
        end_date: date,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "instrument_id": str(item.instrument_id),
            "symbol": item.symbol,
            "period": period,
            "count": count,
        }
        if period == "d":
            # end_date 由调用方显式传入（每日增量=trade_date，回补=今天）；
            # 禁止在此处写死今天，否则重跑历史 T 会拉到今天的日线。
            request["start_date"] = (
                start_date if start_date is not None else end_date - timedelta(days=count)
            )
            request["end_date"] = end_date
        return request

    @staticmethod
    def _is_pool_fatal(exc: BaseException) -> bool:
        if isinstance(
            exc,
            (BrokenProcessPool, pickle.PicklingError, BrokenPipeError, EOFError),
        ):
            return True
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "cannot pickle",
                "can't pickle",
                "process pool is not usable",
                "process in the process pool was terminated abruptly",
            )
        )

    async def _persist_provider_result(
        self,
        item: _InstrumentItem,
        raw_result: dict[str, Any],
        db_session: AsyncSession | None,
    ) -> int:
        try:
            payload = decode_period_bars_result(raw_result)
        except (KeyError, TypeError, ValueError) as exc:
            raise PoolFatalError(f"child payload decode failed: {exc}") from exc
        if payload.instrument_id != str(item.instrument_id) or payload.symbol != item.symbol:
            raise PoolFatalError("child payload identity mismatch")

        async def _persist(session: AsyncSession) -> int:
            if isinstance(payload, DailyProviderPayload):
                prepared = prepare_daily_bars(payload)
                return await persist_daily_bars(
                    session, item.instrument_id, prepared, symbol=item.symbol
                )
            return await persist_minute_provider_payload(
                session, item.instrument_id, payload
            )

        if db_session is not None:
            return await _persist(db_session)
        async with AsyncSessionLocal() as session:
            return await _persist(session)

    async def _run_parallel_period(
        self,
        pool: ProcessPoolExecutor,
        items: list[_InstrumentItem],
        *,
        period: str,
        count: int,
        db_session: AsyncSession | None,
        start_date: date | None,
        end_date: date,
    ) -> _ParallelPhaseResult:
        """Bounded provider dispatcher; persistence remains serial in this parent."""
        max_inflight = self.fetch_processes * self.MAX_INFLIGHT_MULTIPLIER
        phase_started = time.monotonic()
        phase = _ParallelPhaseResult()
        retry_heap: list[tuple[float, int, int, _InstrumentItem]] = [
            (0.0, item.order, 1, item) for item in items
        ]
        heapq.heapify(retry_heap)
        inflight: dict[
            asyncio.Future[dict[str, Any]],
            tuple[Future[dict[str, Any]], _InstrumentItem, int],
        ] = {}

        def schedule_retry_or_fail(item: _InstrumentItem, attempt: int, exc: BaseException) -> None:
            if attempt < self.MAX_RETRIES:
                heapq.heappush(
                    retry_heap,
                    (time.monotonic() + self.RETRY_DELAY, item.order, attempt + 1, item),
                )
                logger.warning(
                    "parallel retry queued symbol=%s period=%s attempt=%d/%d error=%s",
                    item.symbol,
                    period,
                    attempt,
                    self.MAX_RETRIES,
                    exc,
                )
            else:
                phase.failed += 1
                phase.terminal_completed += 1
                phase.failed_symbols.append(item.symbol)
                logger.warning(
                    "parallel retry exhausted symbol=%s period=%s attempts=%d error=%s",
                    item.symbol,
                    period,
                    attempt,
                    exc,
                )

        try:
            while retry_heap or inflight:
                now = time.monotonic()
                while (
                    retry_heap
                    and len(inflight) < max_inflight
                    and retry_heap[0][0] <= now
                ):
                    _, _, attempt, item = heapq.heappop(retry_heap)
                    request = self._build_provider_request(
                        item,
                        period=period,
                        count=count,
                        start_date=start_date,
                        end_date=end_date,
                    )
                    try:
                        concurrent_future = pool.submit(fetch_period_bars_task, request)
                    except Exception as exc:
                        if self._is_pool_fatal(exc):
                            raise PoolFatalError(
                                f"process pool submission failed: {exc}"
                            ) from exc
                        schedule_retry_or_fail(item, attempt, exc)
                        continue
                    wrapped = asyncio.wrap_future(concurrent_future)
                    inflight[wrapped] = (concurrent_future, item, attempt)
                    phase.submitted += 1
                    phase.max_inflight_observed = max(
                        phase.max_inflight_observed, len(inflight)
                    )

                if not inflight:
                    if retry_heap:
                        await asyncio.sleep(
                            max(0.0, retry_heap[0][0] - time.monotonic())
                        )
                    continue

                timeout = None
                if retry_heap and len(inflight) < max_inflight:
                    timeout = max(0.0, retry_heap[0][0] - time.monotonic())
                done, _ = await asyncio.wait(
                    inflight,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for wrapped in done:
                    _, item, attempt = inflight.pop(wrapped)
                    phase.attempts_completed += 1
                    try:
                        raw_result = wrapped.result()
                    except Exception as exc:
                        if self._is_pool_fatal(exc):
                            raise PoolFatalError(
                                f"process pool failed during {period}: {exc}"
                            ) from exc
                        schedule_retry_or_fail(item, attempt, exc)
                        continue

                    try:
                        child_pid = int(raw_result["pid"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise PoolFatalError(
                            f"child payload PID is invalid: {exc}"
                        ) from exc
                    phase.distinct_child_pids.add(child_pid)
                    phase.child_max_rss_kb = max(
                        phase.child_max_rss_kb,
                        float(raw_result.get("child_max_rss_kb", 0.0)),
                    )
                    try:
                        upsert_count = await self._persist_provider_result(
                            item, raw_result, db_session
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if isinstance(exc, PoolFatalError):
                            raise
                        schedule_retry_or_fail(item, attempt, exc)
                        continue

                    phase.upsert_count += upsert_count
                    phase.succeeded += 1
                    phase.terminal_completed += 1

                logger.info(
                    "parallel progress period=%s workers=%d total=%d submitted=%d "
                    "completed=%d succeeded=%d failed=%d in_flight=%d elapsed=%.2fs",
                    period,
                    self.fetch_processes,
                    len(items),
                    phase.submitted,
                    phase.terminal_completed,
                    phase.succeeded,
                    phase.failed,
                    len(inflight),
                    time.monotonic() - phase_started,
                )
        except asyncio.CancelledError:
            for wrapped, (concurrent_future, _, _) in inflight.items():
                wrapped.cancel()
                concurrent_future.cancel()
            raise
        except BaseException:
            for wrapped, (concurrent_future, _, _) in inflight.items():
                wrapped.cancel()
                concurrent_future.cancel()
            raise

        phase.elapsed_seconds = time.monotonic() - phase_started
        return phase

    def _record_parallel_metrics(
        self, period: str, phase: _ParallelPhaseResult
    ) -> None:
        period_metrics = {
            "submitted": phase.submitted,
            "attempts_completed": phase.attempts_completed,
            "completed": phase.terminal_completed,
            "succeeded": phase.succeeded,
            "failed": phase.failed,
            "max_inflight_observed": phase.max_inflight_observed,
            "distinct_child_pids": set(phase.distinct_child_pids),
            "child_max_rss_kb": phase.child_max_rss_kb,
            "elapsed_seconds": phase.elapsed_seconds,
        }
        self.last_process_metrics["periods"][period] = period_metrics
        self.last_process_metrics["max_inflight_observed"] = max(
            self.last_process_metrics["max_inflight_observed"],
            phase.max_inflight_observed,
        )
        self.last_process_metrics["distinct_child_pids"].update(
            phase.distinct_child_pids
        )

    async def _scan_daily_continuity_gate(
        self,
        trade_date: date,
        db_session: AsyncSession | None,
        result: BatchResult,
    ) -> list[Any]:
        """[HARD GATE] 扫描最近 N 个交易日的日线覆盖，返回所有缺口（升序）。

        只检测与记录（``result.daily_continuity_gaps``）；是否阻断由调用方决定 ——
        当前策略是「有缺口即 raise ``DailyContinuityBlockedError``」。

        为什么必须逐交易日扫描而不是看 ``max(trade_date)``：若 09-10 整日缺失、
        09-11 正常，``max`` 仍是 09-11，整日空洞会被完全掩盖。
        """
        from app.services.eod_daily_refresh_service import scan_daily_continuity

        own_session = db_session is None
        session = AsyncSessionLocal() if own_session else db_session
        try:
            gaps = await scan_daily_continuity(session, trade_date)
        finally:
            if own_session:
                await session.close()

        result.daily_continuity_gaps = len(gaps)
        if gaps:
            logger.error(
                "[DAILY-CONTINUITY-GATE] 阻断盘后 Core：%d 个交易日覆盖率不足（through=%s）: %s",
                len(gaps),
                trade_date,
                ", ".join(
                    f"{g.trade_date}(cov={g.coverage:.1%}, missing={g.missing_count}"
                    f"{',TOTAL_GAP' if g.is_total_gap else ''})"
                    for g in gaps
                ),
            )
        return list(gaps)

    async def _run_post_daily_phase(
        self,
        trade_date: date,
        instruments: list[Instrument],
        db_session: AsyncSession | None,
        job_run_id: uuid.UUID | None,
        result: BatchResult,
        *,
        trigger_dsa: bool,
    ) -> None:
        """Run the existing post-d sequence only after all d persistence is terminal.

        Raises:
            FactorSourceUnavailableError: 因子源健康探测失败（fail-closed，
                禁止跑 5000+ 逐股 xdxr）。
        """
        # [FACTOR-HEALTH] 因子源健康门禁：必须先于全市场逐股 xdxr 阶段。
        health = await probe_factor_provider()
        result.factor_source_available = health.available
        result.factor_source_error = health.error
        result.factor_source_latency_seconds = health.latency_seconds
        result.factor_source_connected_server = health.connected_server
        result.factor_source_uncached_xdxr_ok = health.uncached_xdxr_ok
        if not health.available:
            raise FactorSourceUnavailableError(
                "FACTOR_SOURCE_UNAVAILABLE: pytdx xdxr unavailable; "
                "qfq freshness cannot be proven "
                f"(provider={health.provider}, "
                f"latency={health.latency_seconds:.2f}s, error={health.error})"
            )
        logger.info(
            "[FACTOR-HEALTH] 因子源可用 provider=%s latency=%.2fs "
            "connected_server=%s uncached_xdxr_ok=%s",
            health.provider,
            health.latency_seconds,
            health.connected_server,
            health.uncached_xdxr_ok,
        )

        try:
            rebuild_result = await self._rebuild_factors_if_needed(
                trade_date, instruments, db_session, job_run_id=job_run_id
            )
            logger.info(
                "[BarsScheduler] 因子重建完成 checked=%d changed=%d rebuilt=%d failed=%d",
                rebuild_result["checked"],
                rebuild_result["changed"],
                rebuild_result["rebuilt"],
                rebuild_result["failed"],
            )
        except FactorSourceUnavailableError:
            # 熔断器触发：运行中源掉线，必须向上传播 fail-closed，禁止后续 DSA/Core。
            raise
        except Exception as exc:
            logger.warning("[BarsScheduler] 因子重建阶段异常（不阻断后续）: %s", exc, exc_info=True)

        try:
            audit_result = await self._audit_and_rebuild_factors(
                trade_date, instruments, db_session, job_run_id=job_run_id
            )
            result.factor_audit = audit_result
            logger.info(
                "[BarsScheduler] 因子审计完成 audited=%d consistent=%d "
                "needs_rebuild=%d rebuilt=%d failed=%d errors=%d",
                audit_result["total_audited"],
                audit_result["consistent"],
                audit_result["needs_rebuild"],
                audit_result["rebuilt"],
                audit_result["failed"],
                audit_result["errors"],
            )
        except FactorSourceUnavailableError:
            # 审计发现 provider outage 比例超阈值：必须向上传播 fail-closed。
            raise
        except Exception as exc:
            logger.warning("[BarsScheduler] 因子审计阶段异常（不阻断后续）: %s", exc, exc_info=True)

        try:
            result.dsa_run_id = await self._check_daily_coverage_and_trigger_dsa(
                trade_date,
                db_session,
                job_run_id=job_run_id,
                result=result,
                trigger_dsa=trigger_dsa,
            )
            if job_run_id is not None and result.daily_coverage is not None:
                await self._append_daily_done_event(db_session, job_run_id, result)
        except Exception as exc:
            logger.warning(
                "[BarsScheduler] 日线覆盖率检查/DSA 触发异常: %s",
                exc,
                exc_info=True,
            )
            if job_run_id is not None:
                try:
                    await self._append_dsa_trigger_failed_event(
                        db_session, job_run_id, exc
                    )
                except Exception as inner_exc:
                    logger.warning(
                        "[BarsScheduler] 写 DSA_TRIGGER_FAILED 事件失败: %s",
                        inner_exc,
                    )

    async def refresh_raw_daily_only(
        self,
        trade_date: date,
        *,
        job_run_id: uuid.UUID | None = None,
    ) -> BatchResult:
        """[EOD-SNAPSHOT] 只跑到 raw daily 完成即停止的入口（灾难修复 / 单日 raw 补数）。

        流程：snapshot → 同步 universe → raw daily upsert → sparse fallback → 连续性扫描
        → **STOP**。

        **不触发**因子重建 / DSA / Core / Review。用于「只想把某日 raw 日线补上，
        暂不跑依赖因子的盘后 Core」的场景（如 09-11 单独补 raw daily）。

        09-11 这类正常盘后 **优先用 Eastmoney 全市场快照**（单次请求覆盖全市场），
        而不是 THS 5000 次逐股请求。若 snapshot 当前网络不可用，调用方应改用
        :func:`repair_market_wide_daily_gap`（THS）作为 disaster repair。

        Returns:
            BatchResult（含 snapshot / universe / upsert / 连续性指标）。
        """
        result = BatchResult(total=0)
        async with AsyncSessionLocal() as db_session:
            await self._refresh_daily_from_market_snapshot(
                trade_date, db_session, job_run_id, result
            )
        return result

    async def _refresh_daily_from_market_snapshot(
        self,
        trade_date: date,
        db_session: AsyncSession | None,
        job_run_id: uuid.UUID | None,
        result: BatchResult,
    ) -> None:
        """[EOD-SNAPSHOT] 每日日线阶段的全市场快照快速路径 owner。

        流程（详见 eod_daily_refresh_service / eod_market_snapshot_provider）：
        1. 拉全市场 A 股收盘快照（东方财富，不复权）。
        1.5 规模防护：快照覆盖比例异常小 → fail-closed，防止 fallback storm。
        2. 先同步 instrument universe（新股发现必须先于行情覆盖率）。
        3. 批量落当日 raw 日线（conflict 保留 adj_factor）。
        4. 集合差找缺口；仅 sparse_symbol_gap 走历史 fallback（pytdx / Eastmoney
           fqt=0）；market_wide_gap 本轮只报告不逐股回补。
        5. 对新股历史补齐（listing_date 或 2023-01-01 起）。
        6. 回补后重新求集合差 → ``daily_missing_after_fallback``（真成功判定）。
        7. 日线连续性只读扫描 → ``daily_continuity_gaps``（可发现 09-10 整日空洞）。

        所有指标写入传入的 result（snapshot_total / universe_new / ...）。
        失败时抛 SnapshotProviderError，由 _process_all_instruments 退回 legacy 逐股路径。

        调用前置条件：``can_use_same_day_eod_snapshot(trade_date)`` 为 True
        （由 _process_all_instruments 保证），因此这里不需要再判断盘中。
        """
        import httpx

        from app.models.instrument import Instrument
        from app.services.eod_daily_refresh_service import (
            DailyGap,
            _PytdxBreaker,
            backfill_new_instruments,
            check_snapshot_universe_sanity,
            count_active_a_share_instruments,
            fill_missing_daily_instruments,
            find_missing_daily_instruments,
            plan_daily_repair,
            sync_instruments_from_eod_snapshot,
            upsert_raw_daily_snapshot,
        )
        from app.services.eod_market_snapshot_provider import (
            SnapshotProviderError,
            fetch_full_a_share_snapshot,
            normalize_snapshot_rows,
        )
        from app.services.instrument_maintenance_service import stock_symbol_sql_filter

        own_session = db_session is None
        session = AsyncSessionLocal() if own_session else db_session
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                try:
                    # expected_trade_date：由 provider 校验「数据 watermark 已过 15:00」，
                    # 不达标的 host 会被拒绝并尝试下一个；全部不达标则整体 fail-closed。
                    raw_rows = await fetch_full_a_share_snapshot(
                        client, expected_trade_date=trade_date
                    )
                except Exception as exc:
                    raise SnapshotProviderError(f"snapshot 拉取失败: {exc}") from exc

            rows = normalize_snapshot_rows(raw_rows)
            result.snapshot_total = len(raw_rows)
            result.snapshot_valid_daily = len(rows)
            if not rows:
                # 归一化后一行都不剩说明筛选/解析契约被破坏，禁止静默当成功。
                raise SnapshotProviderError(
                    f"snapshot 归一化后无有效 A 股行 raw={len(raw_rows)}"
                )
            logger.info(
                "[EOD-SNAPSHOT] 快照拉取 raw=%d valid=%d", len(raw_rows), len(rows)
            )

            # 1.5 规模防护（落库前）：接口筛选失效 → 只返回几百只 → 若继续，
            # 集合差会把几千只全部推入 historical fallback（fallback storm）。
            existing_count = await count_active_a_share_instruments(session)
            check_snapshot_universe_sanity(
                [r.symbol for r in rows], existing_count
            )

            # 2. 同步 universe（新股发现必须先于行情覆盖率）
            sync = await sync_instruments_from_eod_snapshot(session, rows, trade_date)
            result.universe_new = len(sync.new_symbols)
            result.universe_updated = len(sync.updated_symbols)

            # 重新读取 active A 股 universe（含可能的新股）
            active = (
                await session.execute(
                    select(Instrument)
                    .where(Instrument.status == "active")
                    .where(stock_symbol_sql_filter(Instrument))
                )
            ).scalars().all()
            id_by_symbol = {i.symbol: i.id for i in active}
            eligible = len(active)

            pairs = [
                (id_by_symbol[r.symbol], r)
                for r in rows
                if r.symbol in id_by_symbol and r.market in ("SH", "SZ", "BJ")
            ]
            upserted = await upsert_raw_daily_snapshot(session, trade_date, pairs)
            result.snapshot_upserted = upserted
            # 快照路径也必须计入当日 d 阶段的分周期统计，否则 period_counts["d"]
            # 会错误地保持 0（看起来像「一只都没刷」）。
            result.period_counts["d"] = result.period_counts.get("d", 0) + upserted
            logger.info("[EOD-SNAPSHOT] 当日 raw 日线 upsert=%d", upserted)

            # 4. 缺口：只对 snapshot 后仍未覆盖的活跃 A 股走历史源。
            #    先分类：market_wide_gap 逐股回补等于重新制造数小时任务，本轮只报告。
            breaker = _PytdxBreaker()
            missing = await find_missing_daily_instruments(session, trade_date)
            result.daily_missing_after_snapshot = len(missing)
            if missing:
                covered = max(eligible - len(missing), 0)
                t_gap = DailyGap(
                    trade_date=trade_date,
                    covered=covered,
                    eligible=eligible,
                    coverage=(covered / eligible) if eligible else 0.0,
                    missing_count=len(missing),
                    is_total_gap=eligible > 0 and covered == 0,
                )
                plan = plan_daily_repair(t_gap)
                result.daily_repair_mode = plan.mode
                if plan.mode == "market_wide_gap":
                    logger.warning(
                        "[EOD-SNAPSHOT] T=%s 缺口为 market-wide"
                        "（missing=%d/%d ratio=%.3f），本轮只报告，不做逐股回补",
                        trade_date, len(missing), eligible, plan.missing_ratio,
                    )
                else:
                    result.daily_fallback_attempted = len(missing)
                    ok = await fill_missing_daily_instruments(
                        session, missing, trade_date, breaker=breaker
                    )
                    result.daily_fallback_succeeded = ok
                    result.period_counts["d"] = result.period_counts.get("d", 0) + ok
                    logger.info(
                        "[EOD-SNAPSHOT] 缺口 fallback attempted=%d succeeded=%d",
                        len(missing), ok,
                    )

            # 4.5 真成功判定：回补后仍缺 T 日的数量
            still_missing = await find_missing_daily_instruments(session, trade_date)
            result.daily_missing_after_fallback = len(still_missing)

            # 5. 新股历史补齐（每只按其 listing_date 或 2023-01-01 起）
            if sync.new_instruments:
                backfilled = await backfill_new_instruments(
                    session, sync.new_instruments, trade_date, breaker=breaker
                )
                result.new_instrument_backfilled = backfilled
                logger.info("[EOD-SNAPSHOT] 新股历史补齐=%d", backfilled)

            # 7. 日线连续性扫描 **不在此处**：连续性已经是硬门禁，统一由
            #    _process_all_instruments 在 daily 阶段落库完成后调用
            #    _scan_daily_continuity_gate（对 snapshot / legacy / 未来 provider 一视同仁）。
        finally:
            if own_session:
                await session.close()

    async def _check_daily_coverage_and_trigger_dsa(
        self,
        trade_date: date,
        db_session: AsyncSession | None = None,
        job_run_id: uuid.UUID | None = None,
        result: BatchResult | None = None,
        *,
        trigger_dsa: bool = True,
    ) -> uuid.UUID | None:
        """[BarsScheduler] - 检查日线覆盖率，满足阈值则自动触发 DSA 选股。

        流程：
        1. 统计今日 bars_daily 中不同标的数
        2. 统计活跃标的总数
        3. 覆盖率 ≥ 90% 时：
           - trigger_dsa=True：调用 create_batch_run 创建/复用 dsa_selector queued run
           - trigger_dsa=False：[Phase8A] 仅记录覆盖率，不创建DSA（orchestrator调用）
        4. 返回关联的 StrategyRun id（trigger_dsa=False 时恒为 None）

        Args:
            trade_date: 交易日期
            db_session: 可选的 DB 会话
            job_run_id: 可选的 SchedulerJobRun.id，传入时在 DSA 触发后写 DSA_CREATED 事件
            result: 可选的 BatchResult，传入时填充 daily_covered/daily_total/daily_coverage
            trigger_dsa: [Phase8A] False 时仅计算覆盖率不创建DSA run

        Returns:
            关联的 StrategyRun id，未触发时返回 None
        """
        from app.constants.strategy_keys import DSA_SELECTOR
        from app.services.job_run_event_service import append_event
        from app.services.strategy_batch_service import StrategyBatchService

        async def _do_check(db: AsyncSession) -> uuid.UUID | None:
            # [BarsScheduler] - 复用 BarsCoverageService 统一 SQL，禁止复制覆盖率查询
            from app.services.bars_coverage_service import BarsCoverageService

            coverage_result = await BarsCoverageService.compute_daily_coverage(db, trade_date)
            covered = coverage_result["covered"]
            total = coverage_result["total"]
            coverage = coverage_result["coverage"]
            coverage_raw = coverage_result["coverage_raw"]
            logger.info(
                "[BarsScheduler] 日线覆盖率: %d/%d = %.1f%% (raw=%.6f)",
                covered, total, coverage * 100, coverage_raw,
            )

            # [JobRunEvent] - 填充 BatchResult 覆盖率字段（供调用方写 DAILY_DONE 事件）
            if result is not None:
                result.daily_covered = covered
                result.daily_total = total
                result.daily_coverage = coverage

            # 覆盖率门禁使用原始值，避免四舍五入边缘误判
            if coverage_raw < 0.9:
                # [BarsScheduler] - 覆盖率不足阈值，写 COVERAGE_INSUFFICIENT warn 事件
                logger.warning(
                    "[BarsScheduler] 日线覆盖率不足 %.1f%%（covered=%d/total=%d），暂不触发 DSA",
                    coverage * 100, covered, total,
                )
                if job_run_id is not None:
                    await append_event(
                        db=db,
                        job_run_id=job_run_id,
                        step="COVERAGE_INSUFFICIENT",
                        level="warn",
                        message=(
                            f"日线覆盖率不足 {coverage:.1%}（{covered}/{total}），暂不触发 DSA"
                        ),
                        payload={
                            "covered": covered,
                            "total": total,
                            "coverage": coverage,
                            "threshold": 0.9,
                        },
                    )
                    await db.commit()
                return None

            # [Phase8A] trigger_dsa=False：仅计算覆盖率，不创建DSA run
            # orchestrator调用时使用，DSA由orchestrator在computing_features步骤创建
            if not trigger_dsa:
                logger.info(
                    "[BarsScheduler] 日线覆盖率达标但 trigger_dsa=False，跳过 DSA 创建: "
                    "covered=%d/total=%d, coverage=%.1f%%",
                    covered, total, coverage * 100,
                )
                return None

            # 触发 DSA run（create_batch_run 内部统一去重/重试）
            # create_batch_run 内部 _BLOCKING_STATUSES 跳过，_RETRYABLE_STATUSES 重建 attempt
            batch_service = StrategyBatchService()
            run = await batch_service.create_batch_run(
                db=db,
                strategy_key=DSA_SELECTOR,
                trade_date=trade_date,
                run_type="scheduled",
            )
            await db.commit()
            logger.info(
                "[BarsScheduler] 日线覆盖率达标，已自动触发/复用 DSA 选股: "
                "run_id=%s, attempt_no=%d, covered=%d/total=%d",
                run.id, run.attempt_no, covered, total,
            )

            # [JobRunEvent] - DSA 触发后写入 DSA_CREATED 事件（含覆盖率与 attempt_no）
            if job_run_id is not None:
                await append_event(
                    db=db,
                    job_run_id=job_run_id,
                    step="DSA_CREATED",
                    level="info",
                    message=f"DSA 选股已触发: run_id={run.id}, attempt_no={run.attempt_no}",
                    payload={
                        "run_id": str(run.id),
                        "attempt_no": run.attempt_no,
                        "coverage": coverage,
                        "covered": covered,
                        "total": total,
                    },
                )
                await db.commit()

            return run.id

        if db_session is not None:
            return await _do_check(db_session)
        else:
            async with AsyncSessionLocal() as session:
                return await _do_check(session)

    async def _rebuild_factors_if_needed(
        self,
        trade_date: date,
        instruments: list[Instrument],
        db_session: AsyncSession | None = None,
        job_run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """[CHANGE-20260717-002 SSOT] - 公司行为变化时重建复权因子序列。

        在日线刷新完成后、覆盖率门禁/DSA 触发前执行，保证 DSA 和 snapshot
        使用的因子序列已包含最新公司行为（禁止未来除权事件泄漏到 point-in-time 回算）。

        流程：
        1. 遍历 active A 股，detect_company_action_change 检查 xdxr fingerprint
        2. fingerprint 变化的股票调用 rebuild_factor_series 重建完整因子序列
           （从最早受影响日期重算，原子 upsert，禁止只更新最新 5 根）
        3. 重建成功后精确失效该股票 MDAS 缓存（service 内部完成）
        4. 单股失败不阻断（MDAS 会标记 degraded）：rebuild 失败时回滚 fingerprint，
           保证下次运行重新检测重建

        Args:
            trade_date: 交易日期
            instruments: active A 股列表（复用调用方已查询的缓存，避免重复查询）
            db_session: 可选的 DB 会话（None 时内部新建单一 session 复用）
            job_run_id: 可选的 SchedulerJobRun.id，传入时写 REBUILDING_FACTORS 事件

        Returns:
            dict: [PROMPT.md §5.4.2 V2] 生产审计字段（_rebuild 阶段）
                - trade_date: 业务交易日
                - checked: 检查的股票数
                - changed: fingerprint 变化的股票数
                - rebuilt: 重建成功的股票数
                - failed: 重建失败的股票数
                - failed_symbols: 失败股票代码列表
        """
        from app.services.adjustment_factor_service import (
            AdjustmentFactorService,
            CorporateActionProviderError,
        )
        from app.services.job_run_event_service import append_event

        adj_service = AdjustmentFactorService()
        adapter = get_pytdx_adapter()

        total = len(instruments)
        result: dict[str, Any] = {
            "trade_date": trade_date.isoformat(),  # [PROMPT.md §5.4.2 V2]
            "checked": 0,
            "changed": 0,
            "rebuilt": 0,
            "failed": 0,
            "failed_symbols": [],
        }

        if total == 0:
            logger.warning("[BarsScheduler] _rebuild_factors_if_needed: 无 active 股票")
            return result

        logger.info(
            "[BarsScheduler] 开始因子重建检查 trade_date=%s total=%d", trade_date, total,
        )

        # 写开始事件
        if job_run_id is not None:
            try:
                async def _write_start(db: AsyncSession) -> None:
                    await append_event(
                        db=db, job_run_id=job_run_id,
                        step="REBUILDING_FACTORS", level="info",
                        message=f"开始因子重建检查: total={total}",
                        payload={"total": total, "trade_date": trade_date.isoformat()},
                    )
                    await db.commit()
                if db_session is not None:
                    await _write_start(db_session)
                else:
                    async with AsyncSessionLocal() as session:
                        await _write_start(session)
            except Exception as exc:
                logger.warning(
                    "[BarsScheduler] 写 REBUILDING_FACTORS start 事件失败: %s", exc,
                )

        # tqdm 进度条
        try:
            from tqdm import tqdm
            pbar = tqdm(
                instruments, desc="因子重建检查", position=0, leave=True, dynamic_ncols=True,
            )
        except ImportError:
            pbar = None

        # 使用单一 session 遍历（db_session 为 None 时新建复用，减少连接开销）
        # detect 不写 DB（仅 pytdx + Redis），rebuild 写 DB（commit per stock）
        if db_session is not None:
            session = db_session
            should_close = False
        else:
            session = AsyncSessionLocal()
            should_close = True

        breaker = FactorSourceBreaker()
        try:
            for instrument in (pbar or instruments):
                symbol = instrument.symbol
                result["checked"] += 1
                try:
                    # 1. detect：检查 xdxr fingerprint 是否变化
                    #    （detect 内部已存储新 fingerprint，rebuild 失败时需回滚）
                    earliest = await adj_service.detect_company_action_change(
                        session, instrument.id, symbol, adapter,
                    )
                    if earliest is None:
                        breaker.success()
                        continue  # 无变化，跳过重建

                    # 2. rebuild：从最早受影响日期重算完整因子序列
                    result["changed"] += 1
                    await adj_service.rebuild_factor_series(
                        session, instrument.id, symbol, earliest, adapter,
                    )
                    await session.commit()
                    result["rebuilt"] += 1
                    breaker.success()
                except CorporateActionProviderError as exc:
                    # 源/连接/协议不可用：计入熔断，连续 limit 次则整体 fail-closed。
                    breaker.failure()
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    logger.error(
                        "XDXR provider failure symbol=%s consecutive=%d total=%d: %s",
                        symbol,
                        breaker.consecutive_provider_failures,
                        breaker.total_provider_failures,
                        exc,
                    )
                    if breaker.open:
                        raise FactorSourceUnavailableError(
                            "FACTOR_SOURCE_LOST_DURING_RUN: "
                            f"consecutive_failures={breaker.consecutive_provider_failures}, "
                            f"last_symbol={symbol}, error={exc}"
                        ) from exc
                    continue
                except Exception as exc:
                    # 单股业务数据失败（非源不可用）不阻断：rollback 保持 session 可用
                    # 回滚 fingerprint：detect 已存新值，rebuild 失败需删除，
                    # 保证下次运行重新检测重建（避免因子永久停留在旧值）
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    adj_service._delete_fingerprint(instrument.id)
                    result["failed"] += 1
                    if symbol not in result["failed_symbols"]:
                        result["failed_symbols"].append(symbol)
                    logger.warning(
                        "[BarsScheduler] 因子重建失败 symbol=%s: %s", symbol, exc,
                    )

                if pbar is not None:
                    pbar.set_postfix(
                        checked=result["checked"],
                        changed=result["changed"],
                        rebuilt=result["rebuilt"],
                        failed=result["failed"],
                    )
        finally:
            if should_close:
                await session.close()

        if pbar is not None:
            pbar.close()

        logger.info(
            "[BarsScheduler] 因子重建检查完成: checked=%d changed=%d rebuilt=%d failed=%d",
            result["checked"], result["changed"], result["rebuilt"], result["failed"],
        )

        # 写完成事件
        if job_run_id is not None:
            try:
                async def _write_done(db: AsyncSession) -> None:
                    await append_event(
                        db=db, job_run_id=job_run_id,
                        step="REBUILDING_FACTORS", level="info",
                        message=(
                            f"因子重建检查完成: checked={result['checked']}, "
                            f"changed={result['changed']}, rebuilt={result['rebuilt']}, "
                            f"failed={result['failed']}"
                        ),
                        payload={
                            "checked": result["checked"],
                            "changed": result["changed"],
                            "rebuilt": result["rebuilt"],
                            "failed": result["failed"],
                            "failed_symbols": result["failed_symbols"][:100],
                        },
                    )
                    await db.commit()
                if db_session is not None:
                    await _write_done(db_session)
                else:
                    async with AsyncSessionLocal() as session:
                        await _write_done(session)
            except Exception as exc:
                logger.warning(
                    "[BarsScheduler] 写 REBUILDING_FACTORS done 事件失败: %s", exc,
                )

        return result

    async def _audit_and_rebuild_factors(
        self,
        trade_date: date,
        instruments: list[Instrument],
        db_session: AsyncSession | None = None,
        job_run_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """[S3.1 CHANGE-20260718-007] - 因子一致性审计 + 串行重建。

        在 _rebuild_factors_if_needed 之后、_check_daily_coverage_and_trigger_dsa 之前
        执行，审计 _rebuild_factors_if_needed 可能漏掉的不一致股票（legacy 错误、
        fingerprint 未变但存量错误、1.0 伪装成功等），立即串行重建。

        流程：
        1. FactorReconciliationTask.dry_run: 全市场只读审计 → ReconciliationPlan
        2. 若 needs_rebuild > 0: rebuild_batch 串行重建（每只股票独立事务）
        3. 写 FACTOR_AUDIT job_run_event（start + done，含 before/after hash 摘要）

        软失败：审计/重建异常不阻断 DSA（DSA 可基于旧因子 degraded 运行），
        但失败清单写入事件留下诊断痕迹。调用方已 try/except 包裹。

        Args:
            trade_date: 交易日期
            instruments: active A 股列表（复用调用方缓存，本方法未直接使用，
                仅供日志记录总数；dry_run 内部独立查询 active 股票保证一致性）
            db_session: 可选的 DB 会话（None 时内部新建）
            job_run_id: 可选的 SchedulerJobRun.id，传入时写 FACTOR_AUDIT 事件

        Returns:
            dict: [PROMPT.md §5.4.2 V2] 完整生产审计字段
                - trade_date: 业务交易日
                - total_audited: 审计的股票数
                - consistent: 因子一致的数量
                - needs_rebuild: 需要重建的数量
                - audit_rebuilt: 审计阶段实际重建成功数（与 rebuilt 同义，便于区分 _rebuild 阶段）
                - rebuilt: 重建成功数（兼容旧字段，等于 audit_rebuilt）
                - failed: 重建失败数
                - errors: 审计/重建异常计数
                - failed_symbols: 失败股票代码列表（便于生产验证定位）
        """
        from app.services.factor_reconciliation import FactorReconciliationTask
        from app.services.job_run_event_service import append_event

        total = len(instruments)
        # [PROMPT.md §5.4.2 V2] 生产审计字段完整集合
        summary: dict[str, Any] = {
            "trade_date": trade_date.isoformat(),
            "total_audited": 0,
            "consistent": 0,
            "needs_rebuild": 0,
            "audit_rebuilt": 0,  # V2: 区分 _rebuild_factors_if_needed.rebuilt 与本阶段 rebuilt
            "rebuilt": 0,        # 向后兼容：等于 audit_rebuilt
            "failed": 0,
            "errors": 0,
            "failed_symbols": [],  # V2: 失败股票代码列表
        }

        if total == 0:
            logger.warning("[BarsScheduler] _audit_and_rebuild_factors: 无 active 股票")
            return summary

        logger.info(
            "[BarsScheduler] 开始因子一致性审计 trade_date=%s total=%d",
            trade_date, total,
        )

        # 写开始事件
        if job_run_id is not None:
            try:
                async def _write_audit_start(db: AsyncSession) -> None:
                    await append_event(
                        db=db, job_run_id=job_run_id,
                        step="FACTOR_AUDIT", level="info",
                        message=f"开始因子一致性审计: total={total}",
                        payload={"total": total, "trade_date": trade_date.isoformat()},
                    )
                    await db.commit()
                if db_session is not None:
                    await _write_audit_start(db_session)
                else:
                    async with AsyncSessionLocal() as session:
                        await _write_audit_start(session)
            except Exception as exc:
                logger.warning(
                    "[BarsScheduler] 写 FACTOR_AUDIT start 事件失败: %s", exc,
                )

        task = FactorReconciliationTask()

        # =========================================================================
        # Phase 1: dry-run 全市场审计
        # =========================================================================
        try:
            if db_session is not None:
                plan = await task.dry_run(
                    db_session, batch_size=50, max_mismatches=20,
                )
            else:
                async with AsyncSessionLocal() as session:
                    plan = await task.dry_run(
                        session, batch_size=50, max_mismatches=20,
                    )
        except Exception as exc:
            logger.error(
                "[BarsScheduler] 因子审计 dry_run 失败: %s", exc, exc_info=True,
            )
            summary["errors"] = total  # 无法审计，全部计为 error
            await self._write_audit_done_event(
                db_session, job_run_id, summary,
                error=f"dry_run_failed: {type(exc).__name__}: {exc}",
            )
            return summary

        summary["total_audited"] = plan.total_audited
        summary["consistent"] = plan.consistent_count
        summary["needs_rebuild"] = plan.needs_rebuild_count
        summary["errors"] = plan.error_count

        # [FACTOR-HEALTH] 审计后 fail-closed：即使 dry_run 没有抛异常，也不得把
        # 「全市场 provider 错误」当作普通软失败继续跑 Core。
        audited = summary["total_audited"]
        errors = summary["errors"]
        if audited <= 0:
            raise RuntimeError("FACTOR_AUDIT_EMPTY")
        provider_failure_ratio = errors / max(audited, 1)
        if provider_failure_ratio > 0.01:
            raise FactorSourceUnavailableError(
                f"FACTOR_AUDIT_UNHEALTHY: errors={errors}/{audited} "
                f"(ratio={provider_failure_ratio:.4f})"
            )

        logger.info(
            "[BarsScheduler] 因子审计 dry_run 完成: audited=%d consistent=%d "
            "needs_rebuild=%d errors=%d",
            plan.total_audited, plan.consistent_count,
            plan.needs_rebuild_count, plan.error_count,
        )

        if plan.needs_rebuild_count > 0:
            logger.warning(
                "[BarsScheduler] 发现 %d 只不一致股票: %s",
                plan.needs_rebuild_count,
                [i.symbol for i in plan.items[:20]],
            )

        # =========================================================================
        # Phase 2: 串行重建不一致股票
        # =========================================================================
        if plan.needs_rebuild_count > 0:
            try:
                if db_session is not None:
                    report = await task.rebuild_batch(
                        db_session, plan, batch_size=10,
                    )
                else:
                    async with AsyncSessionLocal() as session:
                        report = await task.rebuild_batch(
                            session, plan, batch_size=10,
                        )
                summary["rebuilt"] = report.success_count
                summary["audit_rebuilt"] = report.success_count  # [PROMPT.md §5.4.2 V2]
                summary["failed"] = report.failure_count
                # [PROMPT.md §5.4.2 V2] 失败股票代码列表（便于生产验证定位）
                failed_symbols_list = [r.symbol for r in report.results if not r.success]
                summary["failed_symbols"] = failed_symbols_list

                logger.info(
                    "[BarsScheduler] 因子重建完成: total=%d success=%d failure=%d",
                    report.total_planned, report.success_count, report.failure_count,
                )

                # 收集失败清单用于事件
                failed_list = [
                    {
                        "symbol": r.symbol,
                        "error_code": r.error_code,
                        "before_hash": r.before_hash,
                        "after_hash": r.after_hash,
                    }
                    for r in report.results if not r.success
                ]
                success_before_after = [
                    {
                        "symbol": r.symbol,
                        "before_hash": r.before_hash,
                        "after_hash": r.after_hash,
                    }
                    for r in report.results if r.success
                ]
                await self._write_audit_done_event(
                    db_session, job_run_id, summary,
                    needs_rebuild_symbols=[i.symbol for i in plan.items],
                    failed_list=failed_list,
                    success_before_after=success_before_after,
                )
            except Exception as exc:
                logger.error(
                    "[BarsScheduler] 因子重建 rebuild_batch 失败: %s",
                    exc, exc_info=True,
                )
                summary["failed"] = plan.needs_rebuild_count  # 全部计为失败
                # [PROMPT.md §5.4.2 V2] 失败股票代码列表（重建异常时所有 needs_rebuild 都视为失败）
                summary["failed_symbols"] = [i.symbol for i in plan.items]
                await self._write_audit_done_event(
                    db_session, job_run_id, summary,
                    error=f"rebuild_batch_failed: {type(exc).__name__}: {exc}",
                    needs_rebuild_symbols=[i.symbol for i in plan.items],
                )
        else:
            await self._write_audit_done_event(db_session, job_run_id, summary)

        # [FACTOR-HEALTH] 重建阶段若仍有失败（单股业务错误），不得静默继续 Core。
        if summary["failed"] > 0:
            raise RuntimeError(
                f"FACTOR_REBUILD_INCOMPLETE: failed={summary['failed']}"
            )

        return summary

    async def _write_audit_done_event(
        self,
        db_session: AsyncSession | None,
        job_run_id: uuid.UUID | None,
        summary: dict[str, Any],
        *,
        error: str | None = None,
        needs_rebuild_symbols: list[str] | None = None,
        failed_list: list[dict[str, Any]] | None = None,
        success_before_after: list[dict[str, Any]] | None = None,
    ) -> None:
        """[S3.1] - 写入 FACTOR_AUDIT_DONE 事件（含 before/after hash 摘要）。"""
        if job_run_id is None:
            return
        from app.services.job_run_event_service import append_event

        payload: dict[str, Any] = dict(summary)
        if error:
            payload["error"] = error
            level = "error"
        elif summary.get("failed", 0) > 0:
            level = "warn"
        else:
            level = "info"
        if needs_rebuild_symbols:
            payload["needs_rebuild_symbols"] = needs_rebuild_symbols[:50]
        if failed_list:
            payload["failed_list"] = failed_list[:50]
        if success_before_after:
            # 只记录前 20 个 before/after hash，避免事件过大
            payload["success_before_after_sample"] = success_before_after[:20]

        message = (
            f"因子审计完成: audited={summary['total_audited']} "
            f"consistent={summary['consistent']} "
            f"needs_rebuild={summary['needs_rebuild']} "
            f"rebuilt={summary['rebuilt']} failed={summary['failed']} "
            f"errors={summary['errors']}"
        )
        if error:
            message += f" error={error}"

        try:
            async def _write_done(db: AsyncSession) -> None:
                await append_event(
                    db=db, job_run_id=job_run_id,
                    step="FACTOR_AUDIT", level=level,
                    message=message, payload=payload,
                )
                await db.commit()
            if db_session is not None:
                await _write_done(db_session)
            else:
                async with AsyncSessionLocal() as session:
                    await _write_done(session)
        except Exception as exc:
            logger.warning(
                "[BarsScheduler] 写 FACTOR_AUDIT done 事件失败: %s", exc,
            )

    async def _append_daily_done_event(
        self,
        db_session: AsyncSession | None,
        job_run_id: uuid.UUID,
        result: BatchResult,
    ) -> None:
        """[JobRunEvent] - 写入 DAILY_DONE 事件（日线阶段完成，含覆盖率）。

        db_session 为 None 时内部创建独立 session；事件写入后 commit 持久化。
        """
        from app.services.job_run_event_service import append_event

        covered = result.daily_covered or 0
        total = result.daily_total or 0
        coverage = result.daily_coverage or 0.0

        async def _do_write(db: AsyncSession) -> None:
            await append_event(
                db=db,
                job_run_id=job_run_id,
                step="DAILY_DONE",
                level="info",
                message=f"日线覆盖 {covered}/{total} = {coverage:.1%}",
                payload={
                    "covered": covered,
                    "total": total,
                    "coverage": coverage,
                },
            )
            await db.commit()

        if db_session is not None:
            await _do_write(db_session)
        else:
            async with AsyncSessionLocal() as session:
                await _do_write(session)

    async def _append_dsa_trigger_failed_event(
        self,
        db_session: AsyncSession | None,
        job_run_id: uuid.UUID,
        exc: Exception,
    ) -> None:
        """[JobRunEvent] - 写入 DSA_TRIGGER_FAILED error 事件（DSA 触发异常诊断）。

        DSA 触发失败不中断日线刷新后续周期（15min/60min），但需留下诊断痕迹：
        - step=DSA_TRIGGER_FAILED, level=error
        - payload 含 error_type / message，便于前端时间线展示与告警

        db_session 为 None 时内部创建独立 session；事件写入后 commit 持久化。
        """
        import traceback as tb_mod

        from app.services.job_run_event_service import append_event

        async def _do_write(db: AsyncSession) -> None:
            await append_event(
                db=db,
                job_run_id=job_run_id,
                step="DSA_TRIGGER_FAILED",
                level="error",
                message=f"DSA 触发失败: {exc}",
                payload={
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                    "traceback": tb_mod.format_exc()[:4000],
                },
            )
            await db.commit()

        if db_session is not None:
            await _do_write(db_session)
        else:
            async with AsyncSessionLocal() as session:
                await _do_write(session)

    async def refresh_one_instrument(
        self,
        instrument_id: uuid.UUID,
        symbol: str,
        counts: dict[str, int],
        db_session: AsyncSession | None = None,
        start_date: date | None = None,
    ) -> RefreshResult:
        """串行刷新单只股票的 3 个周期行情。

        Args:
            instrument_id: 标的 UUID
            symbol: 股票代码
            counts: 各周期的拉取条数
            db_session: 可选的 DB 会话
            start_date: 日线回补起始日期（None 时使用 count 模式）

        Returns:
            RefreshResult: 刷新结果（任一周期重试耗尽 → success=False + error）

        Note:
            [F1B-2 P1-A] 重试耗尽由 ``InstrumentRefreshExhaustedError`` 显式信号化，
            本函数转换为 RefreshResult.success=False，不再静默上报 success=True + 0 行。
        """
        result = RefreshResult(instrument_id=instrument_id, symbol=symbol, success=True)

        # 串行处理周期（仅处理 counts 中存在的周期）
        active_periods = [p for p in self.PERIODS if p in counts]
        for period in active_periods:
            count = counts[period]
            try:
                upsert_count = await self._refresh_one_period_with_retry(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    period=period,
                    count=count,
                    db_session=db_session,
                    start_date=start_date,
                )
            except InstrumentRefreshExhaustedError as exc:
                result.success = False
                result.error = str(exc)
                logger.error(
                    "refresh_one_instrument 失败 symbol=%s period=%s: %s",
                    symbol, period, exc,
                )
                break
            result.upsert_counts[period] = upsert_count

        return result

    async def _refresh_one_period_with_retry(
        self,
        instrument_id: uuid.UUID,
        symbol: str,
        period: str,
        count: int,
        db_session: AsyncSession | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        """刷新单只股票单个周期，带重试。

        Args:
            instrument_id: 标的 UUID
            symbol: 股票代码
            period: 周期（d/15m/60m）
            count: 拉取条数（日线时为回看天数，15min/60min 为拉取条数）
            db_session: 可选的 DB 会话
            start_date: 日线回补起始日期（None 时使用 count 模式）
            end_date: 日线目标结束日；None 时回退到今天（仅供未传的旧调用点）。

        Returns:
            upsert 记录数（合法空 provider 返回 0，仍视为成功）

        Raises:
            InstrumentRefreshExhaustedError: 连续 MAX_RETRIES 次 provider 异常后抛出。
                调用方据此计 failed；**禁止**用 upsert 记录数 == 0 推断失败。
        """
        refresh_fn = self._REFRESH_FUNCS[period]
        adapter = get_pytdx_adapter()
        request = self._build_provider_request(
            _InstrumentItem(0, instrument_id, symbol),
            period=period,
            count=count,
            start_date=start_date,
            end_date=end_date if end_date is not None else shanghai_business_date(),
        )

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                # 日线使用日期范围接口，15min/60min 使用 count 接口
                if period == "d":
                    actual_start = request["start_date"]
                    end_date = request["end_date"]
                    if db_session is not None:
                        df = await refresh_fn(db_session, instrument_id, actual_start, end_date, adapter)
                    else:
                        async with AsyncSessionLocal() as session:
                            df = await refresh_fn(session, instrument_id, actual_start, end_date, adapter)
                else:
                    if db_session is not None:
                        df = await refresh_fn(
                            db_session, instrument_id, request["count"], adapter
                        )
                    else:
                        async with AsyncSessionLocal() as session:
                            df = await refresh_fn(
                                session, instrument_id, request["count"], adapter
                            )
                return 0 if df.empty else len(df)
            except Exception as exc:
                if attempt < self.MAX_RETRIES:
                    logger.warning(
                        "拉取失败 symbol=%s period=%s attempt=%d/%d: %s，%ds 后重试",
                        symbol, period, attempt, self.MAX_RETRIES, exc, self.RETRY_DELAY,
                    )
                    await asyncio.sleep(self.RETRY_DELAY)
                else:
                    logger.warning(
                        "拉取失败 symbol=%s period=%s attempt=%d/%d: %s，放弃",
                        symbol, period, attempt, self.MAX_RETRIES, exc,
                    )
                    raise InstrumentRefreshExhaustedError(
                        f"symbol={symbol} period={period} 连续 {self.MAX_RETRIES} 次拉取失败: {exc}"
                    ) from exc

        # 防御：MAX_RETRIES <= 0 等异常配置下不得静默返回 0（0 是合法空结果）
        raise InstrumentRefreshExhaustedError(
            f"symbol={symbol} period={period} 未执行任何拉取（MAX_RETRIES={self.MAX_RETRIES}）"
        )

    async def _get_active_instruments(
        self,
        db_session: AsyncSession | None = None,
    ) -> list[Instrument]:
        """查询全市场 active 股票（带进程级内存缓存，TTL 5 分钟）。

        缓存命中时直接返回，避免重复查询 DB。
        缓存失效条件：
        - TTL 过期（5 分钟）
        - 调用 clear_instruments_cache() 手动清空

        Args:
            db_session: 可选的 DB 会话

        Returns:
            Instrument 列表
        """
        global _instruments_cache, _instruments_cache_ts

        # 1. 检查缓存是否命中
        now_ts = time.time()
        if (
            _instruments_cache is not None
            and (now_ts - _instruments_cache_ts) < _INSTRUMENTS_CACHE_TTL
        ):
            logger.debug(
                "股票列表内存缓存命中，共 %d 只（age=%.0fs）",
                len(_instruments_cache),
                now_ts - _instruments_cache_ts,
            )
            return _instruments_cache

        # 2. 缓存 miss：查询 DB
        # [BarsScheduler] - 仅查询 A 股股票（排除指数/基金/ETF），与覆盖率分母口径一致
        # 原因：pytdx 不向指数/基金/ETF 写入 bars_daily，刷新这些标的浪费时间且拉低覆盖率
        stmt = (
            select(Instrument)
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
            .order_by(Instrument.symbol)
        )

        if db_session is not None:
            result = await db_session.execute(stmt)
            instruments = list(result.scalars().all())
        else:
            async with AsyncSessionLocal() as session:
                result = await session.execute(stmt)
                instruments = list(result.scalars().all())

        # 3. 更新缓存
        _instruments_cache = instruments
        _instruments_cache_ts = time.time()
        logger.info("股票列表缓存刷新，共 %d 只", len(instruments))
        return instruments

    async def run_retention_cleanup(
        self,
        dry_run: bool = False,
    ) -> list:
        """执行保留策略清理（当前未配置自动调度，需手动调用或后续添加定时任务）。

        Args:
            dry_run: True 时只统计不删除（用于预检）

        Returns:
            各表的清理结果列表（RetentionResult）
        """
        from app.services.bars_retention import apply_retention_policy

        async with AsyncSessionLocal() as session:
            return await apply_retention_policy(session, dry_run=dry_run)


if __name__ == "__main__":
    # 自测入口：验证类定义和函数签名（不连 DB，无副作用）
    import inspect

    service = BarsSchedulerService()

    # 1. 验证常量
    assert service.PERIODS == ["d", "15m", "60m"], \
        f"PERIODS 不匹配: {service.PERIODS}"
    print(f"PERIODS={service.PERIODS}")

    assert service.PHASE_ORDER == ["d", "15m", "60m"], \
        f"PHASE_ORDER 不匹配: {service.PHASE_ORDER}"
    print(f"PHASE_ORDER={service.PHASE_ORDER}")

    assert service.DAILY_COUNTS == {"d": 5, "15m": 50, "60m": 10}, \
        f"DAILY_COUNTS 不匹配: {service.DAILY_COUNTS}"
    print(f"DAILY_COUNTS={service.DAILY_COUNTS}")

    assert service.BACKFILL_COUNTS == {"d": 500, "15m": 15000, "60m": 4000}, \
        f"BACKFILL_COUNTS 不匹配: {service.BACKFILL_COUNTS}"
    print(f"BACKFILL_COUNTS={service.BACKFILL_COUNTS}")

    # 2. 验证方法签名
    sig = inspect.signature(service.refresh_all_instruments)
    params = list(sig.parameters.keys())
    assert params == ["trade_date", "db_session", "job_run_id"], \
        f"refresh_all_instruments 参数不匹配: {params}"
    print(f"refresh_all_instruments params={params}")

    sig = inspect.signature(service.backfill_all_instruments)
    params = list(sig.parameters.keys())
    assert params == ["start_date", "db_session"], \
        f"backfill_all_instruments 参数不匹配: {params}"
    print(f"backfill_all_instruments params={params}")

    sig = inspect.signature(service.refresh_one_instrument)
    params = list(sig.parameters.keys())
    assert params == ["instrument_id", "symbol", "counts", "db_session", "start_date"], \
        f"refresh_one_instrument 参数不匹配: {params}"
    print(f"refresh_one_instrument params={params}")

    sig = inspect.signature(service._refresh_one_period_with_retry)
    params = list(sig.parameters.keys())
    assert params == ["instrument_id", "symbol", "period", "count", "db_session", "start_date"], \
        f"_refresh_one_period_with_retry 参数不匹配: {params}"
    print(f"_refresh_one_period_with_retry params={params}")

    sig = inspect.signature(service._process_all_instruments)
    params = list(sig.parameters.keys())
    assert params == ["trade_date", "counts", "db_session", "task_name", "start_date", "job_run_id"], \
        f"_process_all_instruments 参数不匹配: {params}"
    print(f"_process_all_instruments params={params}")

    # 3. 验证 refresh 函数映射
    assert set(service._REFRESH_FUNCS.keys()) == set(service.PERIODS), \
        f"_REFRESH_FUNCS keys 不匹配 PERIODS: {service._REFRESH_FUNCS.keys()}"
    print(f"_REFRESH_FUNCS keys={list(service._REFRESH_FUNCS.keys())}")

    # 4. 验证 dataclass
    result = RefreshResult(
        instrument_id=uuid.uuid4(),
        symbol="000001",
        success=True,
    )
    assert result.upsert_counts == {}
    print(f"RefreshResult: {result}")

    batch = BatchResult(total=10, succeeded=8, failed=2)
    assert batch.period_counts == {}
    print(f"BatchResult: {batch}")

    # 5. 验证股票列表内存缓存逻辑
    assert _INSTRUMENTS_CACHE_TTL == 300, f"缓存 TTL 应为 300，实际 {_INSTRUMENTS_CACHE_TTL}"
    print(f"_INSTRUMENTS_CACHE_TTL={_INSTRUMENTS_CACHE_TTL}s (5 分钟)")

    # 验证 clear_instruments_cache 函数存在且可调用
    assert callable(clear_instruments_cache), "clear_instruments_cache 应可调用"
    print("clear_instruments_cache 函数存在 ✓")

    # 验证缓存初始状态为空
    assert _instruments_cache is None, "初始缓存应为 None"
    assert _instruments_cache_ts == 0.0, "初始缓存时间戳应为 0.0"
    print("缓存初始状态为空 ✓")

    # 模拟缓存填充与命中（不连 DB，直接操作模块级变量）
    _instruments_cache = []  # 模拟空列表（非 None）
    _instruments_cache_ts = time.time()
    # 验证缓存命中条件：非 None 且未过期
    age = time.time() - _instruments_cache_ts
    assert age < _INSTRUMENTS_CACHE_TTL, "刚写入的缓存应未过期"
    print(f"缓存命中条件验证 ✓（age={age:.3f}s < TTL={_INSTRUMENTS_CACHE_TTL}s）")

    # 验证 clear_instruments_cache 清空缓存
    clear_instruments_cache()
    assert _instruments_cache is None, "清空后缓存应为 None"
    assert _instruments_cache_ts == 0.0, "清空后时间戳应为 0.0"
    print("clear_instruments_cache 清空验证 ✓")

    # 验证缓存过期逻辑（模拟过期）
    _instruments_cache = []
    _instruments_cache_ts = time.time() - (_INSTRUMENTS_CACHE_TTL + 1)  # 过期 1 秒
    age = time.time() - _instruments_cache_ts
    assert age > _INSTRUMENTS_CACHE_TTL, "模拟过期后 age 应大于 TTL"
    print(f"缓存过期条件验证 ✓（age={age:.0f}s > TTL={_INSTRUMENTS_CACHE_TTL}s)")

    # 清理测试数据
    clear_instruments_cache()

    # 6. 验证 run_retention_cleanup 方法
    assert hasattr(service, "run_retention_cleanup"), "应有 run_retention_cleanup 方法"
    assert callable(service.run_retention_cleanup), "run_retention_cleanup 应可调用"
    sig = inspect.signature(service.run_retention_cleanup)
    params = list(sig.parameters.keys())
    assert params == ["dry_run"], f"run_retention_cleanup 参数应为 [dry_run]，实际 {params}"
    assert sig.parameters["dry_run"].default is False, "dry_run 默认应为 False"
    print("run_retention_cleanup 方法验证 ✓")

    # 7. 验证 _check_daily_coverage_and_trigger_dsa 方法
    assert hasattr(service, "_check_daily_coverage_and_trigger_dsa"), \
        "应有 _check_daily_coverage_and_trigger_dsa 方法"
    assert callable(service._check_daily_coverage_and_trigger_dsa), \
        "_check_daily_coverage_and_trigger_dsa 应可调用"
    sig = inspect.signature(service._check_daily_coverage_and_trigger_dsa)
    params = list(sig.parameters.keys())
    assert params == ["trade_date", "db_session", "job_run_id", "result"], \
        f"_check_daily_coverage_and_trigger_dsa 参数应为 [trade_date, db_session, job_run_id, result]，实际 {params}"
    print("_check_daily_coverage_and_trigger_dsa 方法验证 ✓")

    # 验证 _append_daily_done_event 方法
    assert hasattr(service, "_append_daily_done_event"), \
        "应有 _append_daily_done_event 方法"
    sig = inspect.signature(service._append_daily_done_event)
    params = list(sig.parameters.keys())
    assert params == ["db_session", "job_run_id", "result"], \
        f"_append_daily_done_event 参数应为 [db_session, job_run_id, result]，实际 {params}"
    print("_append_daily_done_event 方法验证 ✓")

    # 验证 _append_dsa_trigger_failed_event 方法（Phase 3 新增）
    assert hasattr(service, "_append_dsa_trigger_failed_event"), \
        "应有 _append_dsa_trigger_failed_event 方法"
    sig = inspect.signature(service._append_dsa_trigger_failed_event)
    params = list(sig.parameters.keys())
    assert params == ["db_session", "job_run_id", "exc"], \
        f"_append_dsa_trigger_failed_event 参数应为 [db_session, job_run_id, exc]，实际 {params}"
    print("_append_dsa_trigger_failed_event 方法验证 ✓")

    # 验证 BatchResult 新增字段
    batch = BatchResult(total=10, succeeded=8, failed=2)
    assert batch.daily_covered is None
    assert batch.daily_total is None
    assert batch.daily_coverage is None
    print("BatchResult 新增字段验证 ✓（daily_covered/total/coverage 默认 None）")

    print("\n所有自测通过 ✓（未进行 DB/网络测试）")
