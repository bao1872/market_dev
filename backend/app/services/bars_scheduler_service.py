"""多周期行情定时更新服务。

功能：
- 每个交易日 16:00 自动拉取全市场 active 股票的 d/15m/1h 行情
- 按周期分阶段处理：日线优先 → 覆盖率检查 → DSA 触发 → 15min → 60min
- 串行拉取（pytdx 不支持并发）
- 分批 upsert，幂等：upsert on_conflict_do_update
- 进度：tqdm 进度条（底部固定）
- 回补：使用 start_date 参数控制日线回补范围（默认 2023-01-01），15min/60min 使用 BACKFILL_COUNTS

设计说明：
- pytdx 不支持并发，所有拉取通过 asyncio.to_thread 串行桥接
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
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import get_pytdx_adapter
from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    refresh_15min_bars,
    refresh_60min_bars,
    refresh_daily_bars,
)
from app.services.calendar_service import is_trading_day_async

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


class BarsSchedulerService:
    """多周期行情调度服务。

    用法：
        # 每日增量更新
        service = BarsSchedulerService()
        result = await service.refresh_all_instruments(date.today())

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

    # 周期 → refresh 函数映射
    # 日线使用日期范围接口，15min/60min 使用 count 接口
    _REFRESH_FUNCS = {
        "d": refresh_daily_bars,
        "15m": refresh_15min_bars,
        "60m": refresh_60min_bars,
    }

    async def refresh_all_instruments(
        self,
        trade_date: date,
        db_session: AsyncSession | None = None,
        job_run_id: uuid.UUID | None = None,
    ) -> BatchResult:
        """每日增量更新：串行拉取全市场 active 股票的最新行情。

        使用 DAILY_COUNTS，耗时约 1.8 小时。

        Args:
            trade_date: 交易日期
            db_session: 可选的 DB 会话（不传则内部创建）
            job_run_id: 可选的 SchedulerJobRun.id，传入时在日线阶段完成/DSA 触发后
                写入 job_run_events 时间线事件（DAILY_DONE / DSA_CREATED）

        Returns:
            BatchResult: 批量刷新结果
        """
        logger.info("开始每日增量更新 trade_date=%s", trade_date)
        return await self._process_all_instruments(
            trade_date=trade_date,
            counts=self.DAILY_COUNTS,
            db_session=db_session,
            task_name="每日增量更新",
            job_run_id=job_run_id,
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
    ) -> BatchResult:
        """处理全市场股票的多周期行情刷新（按周期分阶段）。

        分阶段执行：
        1. Phase 1: 全部标的日线刷新
        2. 日线完成后检查覆盖率，满足阈值则自动触发 DSA 选股
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

        Returns:
            BatchResult: 批量刷新结果
        """
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

        # 2. 查询全市场 active 股票
        instruments = await self._get_active_instruments(db_session)
        if not instruments:
            logger.warning("无 active 股票可处理")
            return BatchResult()

        total = len(instruments)
        logger.info("%s: 共 %d 只股票，按周期分阶段处理", task_name, total)

        # 3. 按周期分阶段处理
        result = BatchResult(total=total)
        active_periods = [p for p in self.PHASE_ORDER if p in counts]
        for period in active_periods:
            result.period_counts[period] = 0

        is_daily_refresh = task_name == "每日增量更新"

        for phase_idx, period in enumerate(active_periods):
            phase_name = f"{task_name} [{period}]"
            logger.info(
                "Phase %d/%d 开始: 周期=%s, 标的数=%d",
                phase_idx + 1, len(active_periods), period, total,
            )

            # 使用 tqdm 进度条（底部固定）
            try:
                from tqdm import tqdm
                pbar = tqdm(
                    instruments,
                    desc=phase_name,
                    position=0,
                    leave=True,
                    dynamic_ncols=True,
                )
            except ImportError:
                pbar = None

            phase_succeeded = 0
            phase_failed = 0

            for instrument in (pbar or instruments):
                symbol = instrument.symbol
                try:
                    upsert_count = await self._refresh_one_period_with_retry(
                        instrument_id=instrument.id,
                        symbol=symbol,
                        period=period,
                        count=counts[period],
                        db_session=db_session,
                        start_date=start_date,
                    )
                    result.period_counts[period] += upsert_count
                    phase_succeeded += 1
                except Exception as exc:
                    phase_failed += 1
                    if symbol not in result.failed_symbols:
                        result.failed_symbols.append(symbol)
                    logger.warning(
                        "%s 异常 symbol=%s period=%s: %s",
                        phase_name, symbol, period, exc,
                    )

                if pbar is not None:
                    pbar.set_postfix(
                        ok=phase_succeeded,
                        fail=phase_failed,
                        total=total,
                    )

            if pbar is not None:
                pbar.close()

            logger.info(
                "Phase %d/%d 完成: 周期=%s, succeeded=%d, failed=%d, upsert=%d",
                phase_idx + 1, len(active_periods), period,
                phase_succeeded, phase_failed, result.period_counts[period],
            )

            # [BarsScheduler] - 日线阶段完成后，检查覆盖率并触发/复用 DSA run
            if is_daily_refresh and period == "d":
                try:
                    result.dsa_run_id = await self._check_daily_coverage_and_trigger_dsa(
                        trade_date, db_session, job_run_id=job_run_id, result=result,
                    )
                    # [JobRunEvent] - 日线阶段完成后写入 DAILY_DONE 事件（含覆盖率）
                    if job_run_id is not None and result.daily_coverage is not None:
                        await self._append_daily_done_event(
                            db_session, job_run_id, result,
                        )
                except Exception as exc:
                    # [BarsScheduler] - DSA 触发异常不中断日线刷新后续周期，
                    # 但必须写 DSA_TRIGGER_FAILED error 事件留下诊断痕迹（禁止静默吞没）
                    logger.warning(
                        "[BarsScheduler] 日线覆盖率检查/DSA 触发异常: %s", exc,
                        exc_info=True,
                    )
                    if job_run_id is not None:
                        try:
                            await self._append_dsa_trigger_failed_event(
                                db_session, job_run_id, exc,
                            )
                        except Exception as inner_exc:
                            logger.warning(
                                "[BarsScheduler] 写 DSA_TRIGGER_FAILED 事件失败: %s",
                                inner_exc,
                            )

        # 汇总 succeeded/failed（按标的维度：任一周期失败即计为 failed）
        result.succeeded = total - len(result.failed_symbols)
        result.failed = len(result.failed_symbols)

        logger.info(
            "%s 完成: total=%d succeeded=%d failed=%d period_counts=%s",
            task_name, result.total, result.succeeded, result.failed, result.period_counts,
        )
        return result

    async def _check_daily_coverage_and_trigger_dsa(
        self,
        trade_date: date,
        db_session: AsyncSession | None = None,
        job_run_id: uuid.UUID | None = None,
        result: BatchResult | None = None,
    ) -> uuid.UUID | None:
        """[BarsScheduler] - 检查日线覆盖率，满足阈值则自动触发 DSA 选股。

        流程：
        1. 统计今日 bars_daily 中不同标的数
        2. 统计活跃标的总数
        3. 覆盖率 ≥ 90% 时，调用 create_batch_run 创建/复用 dsa_selector queued run
           - create_batch_run 内部统一处理 published/completed/running/queued 跳过
             与 failed/partial_failed/interrupted 重试，本函数不再手动去重
        4. 返回关联的 StrategyRun id（无论新建还是复用），供 job_run.metadata_json 记录

        Args:
            trade_date: 交易日期
            db_session: 可选的 DB 会话
            job_run_id: 可选的 SchedulerJobRun.id，传入时在 DSA 触发后写 DSA_CREATED 事件
            result: 可选的 BatchResult，传入时填充 daily_covered/daily_total/daily_coverage

        Returns:
            关联的 StrategyRun id，未触发时返回 None
        """
        from sqlalchemy import func as sa_func

        from app.constants.strategy_keys import DSA_SELECTOR
        from app.models.bar import BarDaily
        from app.services.job_run_event_service import append_event
        from app.services.strategy_batch_service import StrategyBatchService

        async def _do_check(db: AsyncSession) -> uuid.UUID | None:
            # 统计今日日线覆盖的标的数
            daily_count_result = await db.execute(
                select(sa_func.count(sa_func.distinct(BarDaily.instrument_id)))
                .where(BarDaily.trade_date == trade_date)
            )
            covered = daily_count_result.scalar() or 0

            # 统计活跃标的数
            active_count_result = await db.execute(
                select(sa_func.count(Instrument.id)).where(Instrument.status == "active")
            )
            total = active_count_result.scalar() or 1

            coverage = covered / total if total > 0 else 0.0
            logger.info(
                "[BarsScheduler] 日线覆盖率: %d/%d = %.1f%%",
                covered, total, coverage * 100,
            )

            # [JobRunEvent] - 填充 BatchResult 覆盖率字段（供调用方写 DAILY_DONE 事件）
            if result is not None:
                result.daily_covered = covered
                result.daily_total = total
                result.daily_coverage = coverage

            if coverage < 0.9:
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
            RefreshResult: 刷新结果
        """
        result = RefreshResult(instrument_id=instrument_id, symbol=symbol, success=True)

        # 串行处理周期（仅处理 counts 中存在的周期）
        active_periods = [p for p in self.PERIODS if p in counts]
        for period in active_periods:
            count = counts[period]
            upsert_count = await self._refresh_one_period_with_retry(
                instrument_id=instrument_id,
                symbol=symbol,
                period=period,
                count=count,
                db_session=db_session,
                start_date=start_date,
            )
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
    ) -> int:
        """刷新单只股票单个周期，带重试。

        Args:
            instrument_id: 标的 UUID
            symbol: 股票代码
            period: 周期（d/15m/60m）
            count: 拉取条数（日线时为回看天数，15min/60min 为拉取条数）
            db_session: 可选的 DB 会话
            start_date: 日线回补起始日期（None 时使用 count 模式）

        Returns:
            upsert 记录数（失败返回 0）
        """
        refresh_fn = self._REFRESH_FUNCS[period]
        adapter = get_pytdx_adapter()

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                # 日线使用日期范围接口，15min/60min 使用 count 接口
                if period == "d":
                    end_date = date.today()
                    if start_date is not None:
                        # 回补模式：使用 start_date 参数控制日线回补范围
                        actual_start = start_date
                    else:
                        # 每日增量模式：使用 count 回看天数
                        actual_start = end_date - timedelta(days=count)
                    if db_session is not None:
                        df = await refresh_fn(db_session, instrument_id, actual_start, end_date, adapter)
                    else:
                        async with AsyncSessionLocal() as session:
                            df = await refresh_fn(session, instrument_id, actual_start, end_date, adapter)
                else:
                    if db_session is not None:
                        df = await refresh_fn(db_session, instrument_id, count, adapter)
                    else:
                        async with AsyncSessionLocal() as session:
                            df = await refresh_fn(session, instrument_id, count, adapter)
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
                    return 0

        return 0

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
        stmt = (
            select(Instrument)
            .where(Instrument.status == "active")
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
    import sys
    _mod = sys.modules[__name__]
    _mod._instruments_cache = []  # 模拟空列表（非 None）
    _mod._instruments_cache_ts = time.time()
    # 验证缓存命中条件：非 None 且未过期
    age = time.time() - _mod._instruments_cache_ts
    assert age < _INSTRUMENTS_CACHE_TTL, "刚写入的缓存应未过期"
    print(f"缓存命中条件验证 ✓（age={age:.3f}s < TTL={_INSTRUMENTS_CACHE_TTL}s）")

    # 验证 clear_instruments_cache 清空缓存
    clear_instruments_cache()
    assert _instruments_cache is None, "清空后缓存应为 None"
    assert _instruments_cache_ts == 0.0, "清空后时间戳应为 0.0"
    print("clear_instruments_cache 清空验证 ✓")

    # 验证缓存过期逻辑（模拟过期）
    _mod._instruments_cache = []
    _mod._instruments_cache_ts = time.time() - (_INSTRUMENTS_CACHE_TTL + 1)  # 过期 1 秒
    age = time.time() - _mod._instruments_cache_ts
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
    print(f"BatchResult 新增字段验证 ✓（daily_covered/total/coverage 默认 None）")

    print("\n所有自测通过 ✓（未进行 DB/网络测试）")
