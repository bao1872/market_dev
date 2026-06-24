"""DSA 历史回补服务 - 按股票外层循环，一次计算完整历史。

实现要点：
- 外层循环为股票，不是日期；单只股票只读取一次 K 线。
- 调用 compute_dsa_history（SSOT）一次计算完整历史指标序列。
- 从完整序列中提取目标交易日结果，避免日期内层重复计算。
- 每个目标交易日仍对应一个独立的 StrategyRun（run_type=backfill），保持发布语义不变。
- 支持断点续跑：通过 BackfillInstrumentProgress 记录每只股票状态。
- 支持有界并发：使用 asyncio.Semaphore 控制并发数，CPU 计算通过 asyncio.to_thread 卸载。
- 批量写入：单只股票的全部日期结果一次性插入。

设计约束：
- 禁止在日期内层重新调用 dynamic_swing_anchored_vwap / compute_atr_rope / 交叉检测。
- 禁止覆盖已 published 的 StrategyRun。
- 数据读取包含足够预热数据（warmup_start = start_date - required_history_bars）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.dsa_backfill import BackfillInstrumentProgress, DSABackfillJob
from app.models.instrument import Instrument
from app.models.strategy import StrategyVersion
from app.models.strategy_run import (
    StrategyResult as StrategyResultORM,
)
from app.models.strategy_run import (
    StrategyResultMetric,
    StrategyRun,
)
from app.repositories.bar_repository import get_bars
from app.repositories.strategy_result_repository import _build_payload, _classify_metric_value
from app.services.calendar_service import is_trading_day_async
from app.services.strategy_service import StrategyNotFoundError, list_versions
from app.strategy.runtime import StrategyLoader
from app.strategy.runtime import StrategyResult as RuntimeStrategyResult
from app.strategy.selectors.dsa_selector import MIN_DIR_BARS, DSASelector, compute_dsa_history

logger = logging.getLogger("dsa_backfill_service")

# 默认并发数
_DEFAULT_MAX_WORKERS = 4

# 租约时长（分钟）
_BACKFILL_LEASE_MINUTES = 30

# 独立心跳间隔（秒）
_BACKFILL_HEARTBEAT_SECONDS = 30

# 结果批量写入阈值
_RESULT_FLUSH_BATCH = 5000

# 默认预热 bar 数（覆盖 lookback、ATR Rope regime、rolling window、min_dir_bars）
_DEFAULT_WARMUP_BARS = 1000


async def _write_backfill_results(
    session: AsyncSession,
    strategy_version_id: uuid.UUID,
    results: list[RuntimeStrategyResult],
    per_run_map: dict[date, uuid.UUID],
) -> int:
    """批量写入回补结果到多个 StrategyRun。

    每个 result 按 trade_date 映射到对应 run_id。
    使用 INSERT ... ON CONFLICT (run_id, instrument_id) DO NOTHING 保证幂等。
    """
    if not results:
        return 0

    # 校验所有目标 run 未发布
    run_ids = list(set(per_run_map.values()))
    if run_ids:
        run_stmt = select(StrategyRun.id, StrategyRun.status).where(StrategyRun.id.in_(run_ids))
        run_result = await session.execute(run_stmt)
        for rid, rstatus in run_result.all():
            if rstatus == "published":
                raise ValueError(f"run 已发布，禁止写入: run_id={rid}")

    # 构造 result records
    result_records: list[dict[str, Any]] = []
    for r in results:
        run_id = per_run_map.get(r.trade_date)
        if run_id is None:
            continue
        result_records.append(
            {
                "run_id": run_id,
                "strategy_version_id": strategy_version_id,
                "instrument_id": r.instrument_id,
                "trade_date": r.trade_date,
                "payload": _build_payload(r),
            }
        )

    if not result_records:
        return 0

    try:
        for i in range(0, len(result_records), _RESULT_FLUSH_BATCH):
            batch = result_records[i : i + _RESULT_FLUSH_BATCH]
            stmt = pg_insert(StrategyResultORM).values(batch)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["run_id", "instrument_id"],
            )
            await session.execute(stmt)
    except Exception as exc:
        await session.rollback()
        raise RuntimeError(
            f"批量写入 backfill 结果失败 count={len(result_records)}: {exc}"
        ) from exc

    # 查询刚写入的 result_id
    instrument_ids = {r.instrument_id for r in results}
    id_query = select(
        StrategyResultORM.id, StrategyResultORM.run_id, StrategyResultORM.instrument_id
    ).where(
        and_(
            StrategyResultORM.run_id.in_(run_ids),
            StrategyResultORM.instrument_id.in_(instrument_ids),
        )
    )
    id_result = await session.execute(id_query)
    result_id_map: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {
        (run_id, instrument_id): result_id for result_id, run_id, instrument_id in id_result.all()
    }

    # 批量写入 metrics
    metric_records: list[dict[str, Any]] = []
    for r in results:
        run_id = per_run_map.get(r.trade_date)
        if run_id is None:
            continue
        result_id = result_id_map.get((run_id, r.instrument_id))
        if result_id is None:
            continue
        for key, value in r.metrics.items():
            numeric_val, text_val, bool_val = _classify_metric_value(value)
            metric_records.append(
                {
                    "result_id": result_id,
                    "strategy_version_id": strategy_version_id,
                    "trade_date": r.trade_date,
                    "instrument_id": r.instrument_id,
                    "metric_key": key,
                    "numeric_value": numeric_val,
                    "text_value": text_val,
                    "bool_value": bool_val,
                }
            )

    if metric_records:
        try:
            for i in range(0, len(metric_records), _RESULT_FLUSH_BATCH):
                batch = metric_records[i : i + _RESULT_FLUSH_BATCH]
                metric_stmt = pg_insert(StrategyResultMetric).values(batch)
                metric_stmt = metric_stmt.on_conflict_do_update(
                    index_elements=["result_id", "metric_key"],
                    set_={
                        "numeric_value": metric_stmt.excluded.numeric_value,
                        "text_value": metric_stmt.excluded.text_value,
                        "bool_value": metric_stmt.excluded.bool_value,
                    },
                )
                await session.execute(metric_stmt)
        except Exception as exc:
            await session.rollback()
            raise RuntimeError(
                f"批量写入 backfill 指标失败 count={len(metric_records)}: {exc}"
            ) from exc

    await session.flush()
    return len(result_records)


@dataclass
class BackfillSummary:
    """回补任务摘要。"""

    job_id: uuid.UUID
    status: str
    target_trade_dates: int
    total_stocks: int
    processed_stocks: int
    succeeded_stocks: int
    failed_stocks: int
    selected_result_count: int


@dataclass
class StockBackfillResult:
    """单只股票回补结果。"""

    instrument_id: uuid.UUID
    symbol: str
    status: str
    result_count: int
    error_code: str | None = None
    error_message: str | None = None


def _get_worker_id() -> str:
    """生成当前 Worker 的唯一标识。"""
    return f"{socket.gethostname()}:{os.getpid()}"


def _compute_required_history_bars(config: dict[str, Any]) -> int:
    """根据策略配置计算需要的最小历史 bar 数。"""
    dsa_cfg = config.get("dsa_config") or {}
    rope_cfg = config.get("rope_config") or {}
    lookback = config.get("lookback") or 0
    min_dir_bars = config.get("min_dir_bars") or 50
    atr_len = getattr(dsa_cfg, "atrLen", 50) if hasattr(dsa_cfg, "atrLen") else 50
    rope_regime = (
        getattr(rope_cfg, "regime_lookback", 55) if hasattr(rope_cfg, "regime_lookback") else 55
    )
    rope_length = getattr(rope_cfg, "length", 14) if hasattr(rope_cfg, "length") else 14
    configured_warmup = config.get("configured_backfill_warmup", _DEFAULT_WARMUP_BARS)
    return max(
        lookback,
        atr_len,
        rope_regime,
        rope_length,
        min_dir_bars,
        configured_warmup,
        20,  # rolling window
    )


async def _run_heartbeat_task(job_id: uuid.UUID, worker_id: str) -> None:
    """独立 Session 心跳任务。"""
    while True:
        try:
            await asyncio.sleep(_BACKFILL_HEARTBEAT_SECONDS)
            async with AsyncSessionLocal() as hb_db:
                job = await hb_db.get(DSABackfillJob, job_id)
                if job is None or job.status != "running":
                    return
                now = datetime.now(UTC)
                job.heartbeat_at = now
                job.lease_expires_at = now + timedelta(minutes=_BACKFILL_LEASE_MINUTES)
                job.current_instrument_id = None
                await hb_db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[DSA Backfill] 独立心跳更新失败 job_id=%s: %s", job_id, exc)


def _build_effective_config(manifest: dict[str, Any]) -> dict[str, Any]:
    """从 manifest 构造 effective_config 快照。"""
    parameters = manifest.get("parameters", [])
    return {p["key"]: p.get("default") for p in parameters}


def _compute_effective_config_hash(effective_config: dict[str, Any]) -> str:
    """计算 effective_config 的哈希。"""
    config_str = str(sorted(effective_config.items()))
    return hashlib.sha256(config_str.encode("utf-8")).hexdigest()[:16]


def _run_compute_dsa_history(bars: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """在线程/进程池中执行 compute_dsa_history 的包装函数。"""
    return compute_dsa_history(bars, config)


class DSABackfillService:
    """DSA 历史回补服务。"""

    def __init__(self, max_workers: int = _DEFAULT_MAX_WORKERS) -> None:
        self.max_workers = max_workers

    async def _get_latest_released_version(
        self,
        db: AsyncSession,
        strategy_key: str,
    ) -> tuple[uuid.UUID, StrategyVersion]:
        """获取策略最新 released 版本。"""
        try:
            versions = await list_versions(db, strategy_key)
        except StrategyNotFoundError as e:
            raise ValueError(str(e)) from e

        released = [v for v in versions if v.status == "released"]
        if released:
            version = released[-1]
        elif versions:
            version = versions[-1]
            logger.warning(
                "策略无 released 版本，使用最新版本: strategy_key=%s, status=%s",
                strategy_key,
                version.status,
            )
        else:
            raise ValueError(f"策略无可用版本: strategy_key={strategy_key}")

        return version.id, version

    async def _resolve_active_instruments(
        self,
        db: AsyncSession,
    ) -> list[tuple[uuid.UUID, str]]:
        """解析活跃标的列表（含 symbol）。"""
        stmt = (
            select(Instrument.id, Instrument.symbol)
            .where(Instrument.status == "active")
            .order_by(Instrument.id)
        )
        result = await db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def _get_target_trade_dates(
        self,
        db: AsyncSession,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """获取 start_date 到 end_date 之间的交易日。"""
        dates: list[date] = []
        for d in pd.date_range(start=start_date, end=end_date):
            d_date = d.date()
            if await is_trading_day_async(db, d_date):
                dates.append(d_date)
        return dates

    async def _filter_published_dates(
        self,
        db: AsyncSession,
        version_id: uuid.UUID,
        dates: list[date],
    ) -> list[date]:
        """过滤掉已有 published backfill run 的日期。"""
        if not dates:
            return []
        stmt = select(StrategyRun.trade_date).where(
            and_(
                StrategyRun.strategy_version_id == version_id,
                StrategyRun.run_type == "backfill",
                StrategyRun.trade_date.in_(dates),
                StrategyRun.status == "published",
            )
        )
        result = await db.execute(stmt)
        published_dates = {row[0] for row in result.all()}
        return [d for d in dates if d not in published_dates]

    async def _init_runtime(self, version: StrategyVersion) -> DSASelector:
        """初始化 DSASelector 运行时。"""
        runtime = await StrategyLoader.load(version)
        if not isinstance(runtime, DSASelector):
            raise ValueError(f"策略运行时不是 DSASelector: {type(runtime).__name__}")
        return runtime

    async def create_backfill(
        self,
        db: AsyncSession,
        strategy_key: str,
        start_date: date,
        end_date: date,
        skip_published: bool = True,
        auto_publish: bool = True,
        requested_by: uuid.UUID | None = None,
    ) -> DSABackfillJob:
        """创建 DSA 历史回补任务。

        流程：
        1. 获取策略最新 released 版本
        2. 获取目标交易日
        3. 排除已有 published 批次的日期（skip_published=True 时）
        4. 获取全市场活跃标的
        5. 创建 DSABackfillJob
        6. 为每个目标交易日创建未发布的 StrategyRun（run_type=backfill）
        7. 预创建 BackfillInstrumentProgress 记录

        Args:
            db: 异步会话
            strategy_key: 策略 key
            start_date: 起始日期
            end_date: 结束日期
            skip_published: 是否跳过已有 published 批次的日期
            auto_publish: 完成后是否自动发布
            requested_by: 请求人用户 ID

        Returns:
            DSABackfillJob ORM 对象
        """
        if end_date < start_date:
            raise ValueError("end_date 必须 >= start_date")

        version_id, version = await self._get_latest_released_version(db, strategy_key)

        # 目标交易日
        target_dates = await self._get_target_trade_dates(db, start_date, end_date)
        if not target_dates:
            raise ValueError(f"区间内无交易日: {start_date} ~ {end_date}")

        if skip_published:
            target_dates = await self._filter_published_dates(db, version_id, target_dates)
            if not target_dates:
                raise ValueError(f"区间内所有日期已有 published 批次: {start_date} ~ {end_date}")

        # 活跃标的
        instruments = await self._resolve_active_instruments(db)
        if not instruments:
            raise ValueError("无活跃标的")

        # effective_config
        effective_config = _build_effective_config(version.manifest)
        effective_config_hash = _compute_effective_config_hash(effective_config)

        # 创建父任务
        job = DSABackfillJob(
            strategy_version_id=version_id,
            start_date=start_date,
            end_date=end_date,
            target_trade_dates=target_dates,
            total_stocks=len(instruments),
            processed_stocks=0,
            succeeded_stocks=0,
            failed_stocks=0,
            selected_result_count=0,
            status="queued",
            requested_by=requested_by,
        )
        db.add(job)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(f"创建 DSA backfill job 失败: {exc}") from exc

        # 为每个目标交易日创建 StrategyRun
        date_run_map: dict[date, uuid.UUID] = {}
        for trade_date in target_dates:
            idempotency_key = (
                f"backfill:{strategy_key}:{version_id}:{trade_date.isoformat()}:job={job.id}"
            )
            run = StrategyRun(
                strategy_version_id=version_id,
                run_type="backfill",
                trade_date=trade_date,
                status="queued",
                input_overrides={
                    "strategy_key": strategy_key,
                    "backfill_job_id": str(job.id),
                    "auto_publish": auto_publish,
                },
                queued_at=datetime.now(UTC),
                idempotency_key=idempotency_key,
                effective_config=effective_config,
                effective_config_hash=effective_config_hash,
                total_instruments=len(instruments),
                succeeded_count=0,
                failed_count=0,
                skipped_count=0,
                attempt_no=1,
            )
            db.add(run)
            try:
                await db.flush()
            except Exception as exc:
                await db.rollback()
                raise RuntimeError(
                    f"创建 backfill date run 失败 job_id={job.id}, trade_date={trade_date}: {exc}"
                ) from exc
            date_run_map[trade_date] = run.id

        # 预创建 progress 记录
        progress_records = [
            BackfillInstrumentProgress(
                backfill_job_id=job.id,
                instrument_id=iid,
                status="pending",
                attempt_count=0,
                result_count=0,
            )
            for iid, _symbol in instruments
        ]
        db.add_all(progress_records)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(f"预创建 backfill progress 失败 job_id={job.id}: {exc}") from exc

        logger.info(
            "创建 DSA backfill job=%s, strategy_key=%s, version_id=%s, "
            "dates=%d, stocks=%d, skip_published=%s, auto_publish=%s",
            job.id,
            strategy_key,
            version_id,
            len(target_dates),
            len(instruments),
            skip_published,
            auto_publish,
        )
        return job

    async def execute_backfill(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
    ) -> BackfillSummary:
        """执行 DSA 历史回补任务。

        流程：
        1. 加载 job 并校验状态
        2. 初始化 runtime
        3. 启动独立心跳
        4. 加载待处理股票
        5. 按股票外层循环执行（支持并发）
        6. 汇总统计并更新 job 状态
        7. 若 auto_publish 且通过质量门禁，则逐日期发布

        Args:
            db: 异步会话
            job_id: 回补任务 ID

        Returns:
            BackfillSummary
        """
        job = await db.get(DSABackfillJob, job_id)
        if job is None:
            raise ValueError(f"回补任务不存在: job_id={job_id}")
        if job.status not in ("queued", "running"):
            raise ValueError(f"回补任务状态不允许执行（当前 {job.status}）: job_id={job_id}")

        version = await db.get(StrategyVersion, job.strategy_version_id)
        if version is None:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            await db.flush()
            raise ValueError(f"策略版本不存在: version_id={job.strategy_version_id}")

        try:
            runtime = await self._init_runtime(version)
        except Exception as exc:
            job.status = "failed"
            job.finished_at = datetime.now(UTC)
            await db.flush()
            raise RuntimeError(f"加载策略运行时失败 job_id={job_id}: {exc}") from exc

        # date_run_map
        stmt = select(StrategyRun).where(
            and_(
                StrategyRun.run_type == "backfill",
                StrategyRun.status != "published",
            )
        )
        result = await db.execute(stmt)
        backfill_runs = list(result.scalars().all())
        date_run_map: dict[date, uuid.UUID] = {
            run.trade_date: run.id
            for run in backfill_runs
            if run.trade_date is not None
            and run.input_overrides.get("backfill_job_id") == str(job_id)
        }

        # 从关联的 StrategyRun 读取 auto_publish（不依赖 job 上的额外字段）
        auto_publish = any(
            bool(run.input_overrides.get("auto_publish"))
            for run in backfill_runs
            if run.input_overrides
        )

        # 启动心跳
        worker_id = _get_worker_id()
        heartbeat_task = asyncio.create_task(_run_heartbeat_task(job.id, worker_id))

        try:
            job.status = "running"
            job.started_at = datetime.now(UTC)
            await db.flush()

            # 加载待处理股票（pending / failed / 租约过期的 running）
            pending_items = await self._load_pending_instruments(db, job_id)

            # 并发处理
            results = await self._process_instruments_parallel(
                db, job, runtime, version, pending_items, date_run_map
            )

            # 汇总
            succeeded = sum(1 for r in results if r.status == "succeeded")
            failed = sum(1 for r in results if r.status == "failed")
            skipped = sum(1 for r in results if r.status == "skipped")
            total_results = sum(r.result_count for r in results)

            # 更新已存在统计（加上本次增量）
            existing = await self._load_progress_stats(db, job_id)
            job.succeeded_stocks = existing["succeeded"] + succeeded
            job.failed_stocks = existing["failed"] + failed
            job.processed_stocks = existing["processed"] + len(results)
            job.selected_result_count = existing["results"] + total_results

            if job.status != "cancelled":
                if failed == 0 and skipped == 0:
                    job.status = "completed"
                elif succeeded > 0:
                    job.status = "partial_failed"
                else:
                    job.status = "failed"
            job.finished_at = datetime.now(UTC)
            await db.flush()

            # 同步每个日期 StrategyRun 的统计，用于发布覆盖率门禁
            skipped_count = job.total_stocks - job.processed_stocks
            run_status = job.status if job.status != "published" else "completed"
            for run in backfill_runs:
                run.succeeded_count = job.succeeded_stocks
                run.failed_count = job.failed_stocks
                run.skipped_count = skipped_count
                run.status = run_status
            await db.flush()

            # 自动发布
            if auto_publish and job.status in ("completed", "partial_failed"):
                try:
                    await self.publish_backfill(db, job_id)
                except Exception as exc:
                    logger.warning("backfill 自动发布失败 job_id=%s: %s", job_id, exc)

            return BackfillSummary(
                job_id=job.id,
                status=job.status,
                target_trade_dates=len(job.target_trade_dates),
                total_stocks=job.total_stocks,
                processed_stocks=job.processed_stocks,
                succeeded_stocks=job.succeeded_stocks,
                failed_stocks=job.failed_stocks,
                selected_result_count=job.selected_result_count,
            )
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _load_pending_instruments(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
    ) -> list[tuple[uuid.UUID, str]]:
        """加载待处理股票（pending / failed / 租约过期的 running）。"""
        now = datetime.now(UTC)
        # 加载任务租约
        job = await db.get(DSABackfillJob, job_id)
        lease_expired = job is not None and (
            job.lease_expires_at is None or job.lease_expires_at < now
        )

        conditions: list[Any] = [BackfillInstrumentProgress.status.in_(["pending", "failed"])]
        if lease_expired:
            conditions.append(BackfillInstrumentProgress.status == "running")

        stmt = (
            select(BackfillInstrumentProgress.instrument_id, Instrument.symbol)
            .join(Instrument, BackfillInstrumentProgress.instrument_id == Instrument.id)
            .where(BackfillInstrumentProgress.backfill_job_id == job_id)
            .where(or_(*conditions))
            .order_by(BackfillInstrumentProgress.instrument_id)
        )
        result = await db.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def _load_progress_stats(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
    ) -> dict[str, int]:
        """加载当前进度统计（已完成/失败/结果数）。"""
        stmt = (
            select(
                BackfillInstrumentProgress.status,
                func.count().label("cnt"),
            )
            .where(BackfillInstrumentProgress.backfill_job_id == job_id)
            .group_by(BackfillInstrumentProgress.status)
        )
        result = await db.execute(stmt)
        status_counts = {row[0]: row[1] for row in result.all()}

        result_stmt = select(
            func.coalesce(func.sum(BackfillInstrumentProgress.result_count), 0)
        ).where(BackfillInstrumentProgress.backfill_job_id == job_id)
        total_results = int((await db.execute(result_stmt)).scalar() or 0)

        return {
            "succeeded": int(status_counts.get("succeeded", 0)),
            "failed": int(status_counts.get("failed", 0)),
            "skipped": int(status_counts.get("skipped", 0)),
            "processed": sum(status_counts.values()),
            "results": total_results,
        }

    async def _process_instruments_parallel(
        self,
        db: AsyncSession,
        job: DSABackfillJob,
        runtime: DSASelector,
        version: StrategyVersion,
        instruments: list[tuple[uuid.UUID, str]],
        date_run_map: dict[date, uuid.UUID],
    ) -> list[StockBackfillResult]:
        """并发处理股票列表。"""
        if not instruments:
            return []

        target_dates = job.target_trade_dates
        config = runtime._build_history_config()
        required_bars = _compute_required_history_bars(config)
        warmup_start = job.start_date - timedelta(days=required_bars + 30)
        end_date = job.end_date

        # 使用有界信号量控制并发
        semaphore = asyncio.Semaphore(self.max_workers)

        async def _process_one(iid: uuid.UUID, symbol: str) -> StockBackfillResult:
            async with semaphore:
                try:
                    return await self._process_single_instrument(
                        db,
                        job,
                        version,
                        config,
                        iid,
                        symbol,
                        target_dates,
                        date_run_map,
                        warmup_start,
                        end_date,
                    )
                except Exception as exc:
                    logger.warning(
                        "股票并发处理异常 job_id=%s instrument_id=%s symbol=%s: %s",
                        job.id,
                        iid,
                        symbol,
                        exc,
                    )
                    return StockBackfillResult(
                        instrument_id=iid,
                        symbol=symbol,
                        status="failed",
                        result_count=0,
                        error_code=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )

        tasks = [asyncio.create_task(_process_one(iid, symbol)) for iid, symbol in instruments]
        return await asyncio.gather(*tasks)

    async def _process_single_instrument(
        self,
        db: AsyncSession,
        job: DSABackfillJob,
        version: StrategyVersion,
        config: dict[str, Any],
        instrument_id: uuid.UUID,
        symbol: str,
        target_dates: list[date],
        date_run_map: dict[date, uuid.UUID],
        warmup_start: date,
        end_date: date,
    ) -> StockBackfillResult:
        """处理单只股票。

        1. 更新 progress 为 running
        2. 加载完整历史 K 线（含预热）
        3. 调用 compute_dsa_history
        4. 提取目标日期结果
        5. 批量写入 StrategyResult
        6. 更新 progress 为 succeeded
        """
        # 加载或创建 progress 记录
        progress = await self._get_or_create_progress(db, job.id, instrument_id)
        if progress.status == "succeeded":
            return StockBackfillResult(
                instrument_id=instrument_id,
                symbol=symbol,
                status="succeeded",
                result_count=progress.result_count,
            )

        progress.status = "running"
        progress.started_at = datetime.now(UTC)
        progress.attempt_count += 1
        progress.error_code = None
        progress.error_message = None
        job.current_instrument_id = instrument_id
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(
                f"更新 progress 状态失败 job_id={job.id}, instrument_id={instrument_id}: {exc}"
            ) from exc

        try:
            # 加载完整历史 K 线
            bars_result = await get_bars(
                db,
                instrument_id,
                timeframe="1d",
                start_date=warmup_start,
                end_date=end_date,
                adjustment="qfq",
            )
            bars_df = bars_result.bars if bars_result.bars is not None else None
            if bars_df is None or bars_df.empty or len(bars_df) < 60:
                progress.status = "skipped"
                progress.finished_at = datetime.now(UTC)
                progress.error_message = "insufficient_data"
                await db.flush()
                return StockBackfillResult(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    status="skipped",
                    result_count=0,
                )

            # 计算完整历史指标（在线程池执行 CPU 计算）
            history = await asyncio.to_thread(_run_compute_dsa_history, bars_df, config)
            if history.empty:
                progress.status = "failed"
                progress.finished_at = datetime.now(UTC)
                progress.error_code = "compute_empty"
                progress.error_message = "compute_dsa_history returned empty"
                await db.flush()
                return StockBackfillResult(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    status="failed",
                    result_count=0,
                    error_code="compute_empty",
                )

            # 提取目标日期结果（仅保留选股条件命中：regime_value == 1 且 dsa_dir_bars > min_dir_bars）
            min_dir_bars = int(config.get("min_dir_bars", MIN_DIR_BARS))
            results: list[RuntimeStrategyResult] = []
            for trade_date in target_dates:
                if trade_date not in date_run_map:
                    continue
                rows = history.loc[history.index.date == trade_date]
                if rows.empty:
                    continue
                row = rows.iloc[-1]
                regime_value = int(row["regime_value"]) if pd.notna(row["regime_value"]) else 0
                dsa_dir_bars = int(row["dsa_dir_bars"]) if pd.notna(row["dsa_dir_bars"]) else 0
                if regime_value != 1 or dsa_dir_bars <= min_dir_bars:
                    continue
                metrics = self._history_row_to_metrics(row)
                results.append(
                    RuntimeStrategyResult(
                        instrument_id=instrument_id,
                        strategy_version_id=job.strategy_version_id,
                        trade_date=trade_date,
                        matched=True,
                        metrics=metrics,
                        calculation_id=str(job.id),
                    )
                )

            # 批量写入该股票的全部日期结果
            if results:
                for i in range(0, len(results), _RESULT_FLUSH_BATCH):
                    batch = results[i : i + _RESULT_FLUSH_BATCH]
                    await _write_backfill_results(
                        db,
                        strategy_version_id=job.strategy_version_id,
                        results=batch,
                        per_run_map=date_run_map,
                    )

            progress.status = "succeeded"
            progress.finished_at = datetime.now(UTC)
            progress.result_count = len(results)
            await db.flush()

            return StockBackfillResult(
                instrument_id=instrument_id,
                symbol=symbol,
                status="succeeded",
                result_count=len(results),
            )
        except Exception as exc:
            logger.warning(
                "股票回补失败 job_id=%s instrument_id=%s symbol=%s: %s",
                job.id,
                instrument_id,
                symbol,
                exc,
            )
            progress.status = "failed"
            progress.finished_at = datetime.now(UTC)
            progress.error_code = type(exc).__name__
            progress.error_message = str(exc)[:500]
            await db.flush()
            return StockBackfillResult(
                instrument_id=instrument_id,
                symbol=symbol,
                status="failed",
                result_count=0,
                error_code=type(exc).__name__,
                error_message=str(exc)[:500],
            )

    async def _get_or_create_progress(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
        instrument_id: uuid.UUID,
    ) -> BackfillInstrumentProgress:
        """获取或创建 progress 记录（并发安全）。"""
        stmt = select(BackfillInstrumentProgress).where(
            and_(
                BackfillInstrumentProgress.backfill_job_id == job_id,
                BackfillInstrumentProgress.instrument_id == instrument_id,
            )
        )
        result = await db.execute(stmt)
        progress = result.scalar_one_or_none()
        if progress is None:
            progress = BackfillInstrumentProgress(
                backfill_job_id=job_id,
                instrument_id=instrument_id,
                status="pending",
                attempt_count=0,
                result_count=0,
            )
            db.add(progress)
            try:
                await db.flush()
            except Exception as exc:
                await db.rollback()
                raise RuntimeError(
                    f"创建 progress 失败 job_id={job_id}, instrument_id={instrument_id}: {exc}"
                ) from exc
        return progress

    def _history_row_to_metrics(self, row: pd.Series) -> dict[str, Any]:
        """将 compute_dsa_history 的单行结果转为 StrategyResult.metrics 字典。"""
        from app.strategy.selectors.dsa_selector import _safe_date, _safe_float

        metrics: dict[str, Any] = {
            "dsa_dir_bars": int(row["dsa_dir_bars"]) if pd.notna(row["dsa_dir_bars"]) else 0,
            "vwap_ret_avg": _safe_float(row["vwap_ret_avg"]),
            "vwap_ret_total": _safe_float(row["vwap_ret_total"]),
            "offset_mean": _safe_float(row["offset_mean"]),
            "offset_std": _safe_float(row["offset_std"]),
            "offset_variance_rate": _safe_float(row["offset_variance_rate"]),
            "offset_percentile": _safe_float(row["offset_percentile"]),
            "regime_value": int(row["regime_value"]) if pd.notna(row["regime_value"]) else 0,
            "regime_strength": _safe_float(row["regime_strength"]),
            "offset_rate": _safe_float(row["offset_rate"]),
            "change_pct": _safe_float(row["change_pct"]),
            "touch_rope": bool(row["touch_rope"]) if pd.notna(row["touch_rope"]) else False,
            "touch_vwap": bool(row["touch_vwap"]) if pd.notna(row["touch_vwap"]) else False,
            "rope_dir1_pct": _safe_float(row["rope_dir1_pct"]),
            "rope_dir0_pct": _safe_float(row["rope_dir0_pct"]),
            "rope_dir_neg1_pct": _safe_float(row["rope_dir_neg1_pct"]),
            "cross_up_count": int(row["cross_up_count"]) if pd.notna(row["cross_up_count"]) else 0,
            "cross_down_count": int(row["cross_down_count"])
            if pd.notna(row["cross_down_count"])
            else 0,
            "last_cross_up_date": _safe_date(row["last_cross_up_date"]),
            "last_cross_down_date": _safe_date(row["last_cross_down_date"]),
            "vwap_ret_5": _safe_float(row["vwap_ret_5"]),
            "vwap_ret_10": _safe_float(row["vwap_ret_10"]),
            "vwap_ret_20": _safe_float(row["vwap_ret_20"]),
            "dsa_vwap": _safe_float(row["dsa_vwap"]),
            "dsa_vwap_dev_pct": _safe_float(row["dsa_vwap_dev_pct"]),
            "vol_zscore": _safe_float(row["vol_zscore"]),
            "avg_amount_20d": _safe_float(row["avg_amount_20d"]),
            "rope_cross_up_date": _safe_date(row["rope_cross_up_date"]),
            "rope_cross_down_date": _safe_date(row["rope_cross_down_date"]),
            "rope_cross_up_price": _safe_float(row["rope_cross_up_price"]),
            "rope_cross_down_price": _safe_float(row["rope_cross_down_price"]),
            "rope_cross_up_count": int(row["rope_cross_up_count"])
            if pd.notna(row["rope_cross_up_count"])
            else 0,
            "rope_cross_down_count": int(row["rope_cross_down_count"])
            if pd.notna(row["rope_cross_down_count"])
            else 0,
        }
        return metrics

    async def publish_backfill(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """发布回补任务下所有未发布的日期 StrategyRun。

        质量门禁：
        - 不发布状态为 published 的 run
        - 仅发布 completed/partial_failed 的 run
        - 单日期 run 的股票覆盖率达到 80% 才发布
        """
        job = await db.get(DSABackfillJob, job_id)
        if job is None:
            raise ValueError(f"回补任务不存在: job_id={job_id}")

        published_ids: list[uuid.UUID] = []
        stmt = select(StrategyRun).where(
            and_(
                StrategyRun.run_type == "backfill",
                StrategyRun.status.in_(["completed", "partial_failed"]),
                StrategyRun.input_overrides["backfill_job_id"].astext == str(job_id),
            )
        )
        result = await db.execute(stmt)
        runs = list(result.scalars().all())

        from app.services.strategy_batch_service import StrategyBatchService

        batch_service = StrategyBatchService()
        for run in runs:
            total = run.total_instruments or 0
            succeeded = run.succeeded_count or 0
            coverage = succeeded / total if total > 0 else 0.0
            if coverage < 0.8:
                logger.info(
                    "backfill date run 覆盖率不足，暂不发布 run_id=%s, coverage=%.1f%%",
                    run.id,
                    coverage * 100,
                )
                continue
            try:
                await batch_service.publish_run(db, run.id)
                published_ids.append(run.id)
            except Exception as exc:
                logger.warning("backfill date run 发布失败 run_id=%s: %s", run.id, exc)
                continue

        if published_ids:
            job.status = "published"
            job.finished_at = datetime.now(UTC)
            await db.flush()

        return published_ids

    async def retry_failed(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
    ) -> int:
        """重试失败的股票。

        将 status=failed 的 progress 重置为 pending，并更新 job 统计。
        """
        job = await db.get(DSABackfillJob, job_id)
        if job is None:
            raise ValueError(f"回补任务不存在: job_id={job_id}")
        if job.status not in ("completed", "partial_failed", "failed"):
            raise ValueError(f"回补任务状态不允许重试（当前 {job.status}）: job_id={job_id}")

        stmt = select(BackfillInstrumentProgress).where(
            and_(
                BackfillInstrumentProgress.backfill_job_id == job_id,
                BackfillInstrumentProgress.status == "failed",
            )
        )
        result = await db.execute(stmt)
        failed_progress = list(result.scalars().all())

        for progress in failed_progress:
            progress.status = "pending"
            progress.attempt_count = 0
            progress.error_code = None
            progress.error_message = None
            progress.started_at = None
            progress.finished_at = None

        job.status = "queued"
        job.processed_stocks -= len(failed_progress)
        job.failed_stocks -= len(failed_progress)
        await db.flush()

        logger.info(
            "backfill 重试失败股票: job_id=%s, count=%d",
            job_id,
            len(failed_progress),
        )
        return len(failed_progress)

    async def cancel_backfill(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
    ) -> DSABackfillJob:
        """取消回补任务。

        将 running 的 progress 标记为 failed，job 标记为 cancelled。
        """
        job = await db.get(DSABackfillJob, job_id)
        if job is None:
            raise ValueError(f"回补任务不存在: job_id={job_id}")
        if job.status not in ("queued", "running"):
            raise ValueError(f"回补任务状态不允许取消（当前 {job.status}）: job_id={job_id}")

        stmt = select(BackfillInstrumentProgress).where(
            and_(
                BackfillInstrumentProgress.backfill_job_id == job_id,
                BackfillInstrumentProgress.status.in_(["pending", "running"]),
            )
        )
        result = await db.execute(stmt)
        for progress in result.scalars().all():
            if progress.status == "running":
                progress.status = "failed"
                progress.error_code = "cancelled"
                progress.error_message = "任务被取消"
                progress.finished_at = datetime.now(UTC)
            else:
                progress.status = "skipped"
                progress.finished_at = datetime.now(UTC)

        job.status = "cancelled"
        job.finished_at = datetime.now(UTC)
        await db.flush()
        return job

    async def get_summary(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
    ) -> BackfillSummary:
        """获取回补任务摘要。"""
        job = await db.get(DSABackfillJob, job_id)
        if job is None:
            raise ValueError(f"回补任务不存在: job_id={job_id}")
        return BackfillSummary(
            job_id=job.id,
            status=job.status,
            target_trade_dates=len(job.target_trade_dates),
            total_stocks=job.total_stocks,
            processed_stocks=job.processed_stocks,
            succeeded_stocks=job.succeeded_stocks,
            failed_stocks=job.failed_stocks,
            selected_result_count=job.selected_result_count,
        )


if __name__ == "__main__":
    # 自测入口：验证类与方法签名（无副作用，不连接数据库）

    service = DSABackfillService(max_workers=2)
    assert service.max_workers == 2
    print(f"DSABackfillService max_workers={service.max_workers} ✓")

    methods = [
        "create_backfill",
        "execute_backfill",
        "publish_backfill",
        "retry_failed",
        "cancel_backfill",
        "get_summary",
    ]
    for method in methods:
        assert hasattr(service, method), f"缺少方法: {method}"
        assert callable(getattr(service, method)), f"方法不可调用: {method}"
    print(f"方法存在: {methods} ✓")

    # 验证辅助函数
    assert _get_worker_id()
    assert _compute_required_history_bars({"lookback": 100, "min_dir_bars": 50}) >= 100
    print("辅助函数 ✓")

    print("OK")
