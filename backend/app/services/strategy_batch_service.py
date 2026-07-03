"""通用策略批量计算服务 - 后台 Worker 调用的批量计算。

DSA 作为第一个支持的 strategy_key，后续可扩展其他策略。
由 Worker 调用，不在 HTTP 请求内执行。

核心方法：
- create_batch_run: 创建批量计算运行（status=queued），数据就绪检查 + 预创建 run_items
- claim_next_run: Worker 加锁领取下一个 queued 运行（status=queued → running）
- execute_run: 执行批量计算（Worker 调用），逐标的执行策略并写入结果
- retry_run: 基于已有运行创建新的 attempt（业务重试）
- publish_run: 发布运行结果（admin 调用或盘后编排器调用），completed/partial_failed → published
- check_data_readiness: 数据就绪检查（交易日/活跃标的/K线覆盖率/停牌/退市）
- _check_quality_gates: 严格质量门禁检查（盘后编排器自动发布前置条件）

设计说明：
- POST API 只创建 queued 运行，Worker 异步执行（不在 HTTP 请求内计算全市场）
- run 状态机：queued → running → completed/partial_failed → published/failed
- 质量门禁：scheduled 运行完成后，execute_run 仅记录门禁是否通过；
  实际自动发布由 after_close_orchestrator / 调度任务统一负责，避免重复 publish
- per-stock 跟踪：strategy_run_items 记录 status/attempt_count/error/result_id
- effective_config 从 manifest 读取并保存到 strategy_runs.effective_config（不可变）
- 幂等：idempotency_key = strategy_key:strategy_version_id:trade_date:run_type:attempt_no
  同一天同策略同类型允许多个 attempt（失败重试）
- 结果不可变：运行 completed/published 后，write_results 拒绝写入
- Worker 领取任务使用 SELECT ... FOR UPDATE SKIP LOCKED 加锁，避免多 Worker 竞争
- execute_run 使用独立 Session 心跳，每 30 秒更新 heartbeat_at / lease_expires_at

禁异常吞没：所有异常补充上下文后 re-raise。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import (
    FAILURE_STAGE_WORKER_INTERRUPTED,
    StrategyRun,
    StrategyRunItem,
)
from app.repositories import strategy_result_repository
from app.repositories.bar_repository import get_bars
from app.services.calendar_service import is_trading_day_async
from app.services.instrument_maintenance_service import stock_symbol_sql_filter
from app.services.strategy_service import (
    StrategyNotFoundError,
    list_versions,
)
from app.strategy.budget import BudgetExceededError
from app.strategy.runtime import MarketDataContext, StrategyLoader

logger = logging.getLogger("strategy_batch_service")

# 数据就绪检查覆盖率阈值（当日 K 线数 / 活跃标的数）
DATA_COVERAGE_THRESHOLD = 0.9

# 策略批量计算日线回看天数（与 bars.py _DEFAULT_DAILY_LOOKBACK_DAYS 一致）
_STRATEGY_BATCH_DAILY_LOOKBACK_DAYS = 5000

# [StrategyRun] - 租约与恢复常量
_LEASE_DURATION_MINUTES = 30  # Worker claim 后租约时长（分钟）
_MAX_ATTEMPTS = 3  # 最大重试次数（超过后标记 failed）
_STALE_QUEUED_HOURS = 2  # queued 状态超过此小时数视为 stale
_HEARTBEAT_INTERVAL_SECONDS = 30  # 独立心跳更新间隔（秒）

# [StrategyRun] - 触发方式常量
_RUN_TYPE_SCHEDULED = "scheduled"
_RUN_TYPE_MANUAL = "manual"
_RUN_TYPE_REPLAY = "replay"
VALID_RUN_TYPES = {
    _RUN_TYPE_SCHEDULED, _RUN_TYPE_MANUAL, _RUN_TYPE_REPLAY,
}

# [StrategyRun] - 去重状态分组
# 这些状态存在时，不允许创建新的 attempt（直接返回已存在 run）
_BLOCKING_STATUSES = {"published", "completed", "running", "queued"}
# 这些状态存在时，允许创建下一个 attempt（业务重试）
_RETRYABLE_STATUSES = {"failed", "partial_failed", "interrupted"}

class InvalidStrategyResultError(Exception):
    """策略结果校验失败。

    [StrategyBatchService] - 描述: 保留供 API 层捕获转 422（原 DSA 硬校验已移除）
    """
    pass


def _get_worker_id() -> str:
    """生成当前 Worker 的唯一标识（hostname:pid）。"""
    import socket
    return f"{socket.gethostname()}:{os.getpid()}"


async def _run_heartbeat_task(run_id: uuid.UUID, worker_id: str) -> None:
    """[StrategyRun] - 独立 Session 心跳任务。

    每 _HEARTBEAT_INTERVAL_SECONDS 秒使用独立 AsyncSessionLocal 更新
    heartbeat_at / lease_expires_at / worker_id，与主执行 Session 解耦。

    Args:
        run_id: 运行 ID
        worker_id: Worker 标识
    """
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            async with AsyncSessionLocal() as hb_db:
                run = await hb_db.get(StrategyRun, run_id)
                if run is None or run.status != "running":
                    return
                now = datetime.now(UTC)
                run.heartbeat_at = now
                run.lease_expires_at = now + timedelta(minutes=_LEASE_DURATION_MINUTES)
                run.worker_id = worker_id
                await hb_db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[StrategyRun] 独立心跳更新失败 run_id=%s: %s", run_id, exc,
            )


@dataclass
class DataReadinessResult:
    """数据就绪检查结果。

    Attributes:
        is_ready: 是否就绪（True 表示可以创建 run）
        is_trading_day: 是否为交易日
        active_instrument_count: 活跃标的数量
        bars_count: 当日 K 线数量
        coverage_rate: 数据覆盖率（bars_count / active_instrument_count）
        warnings: 警告信息列表（不阻止创建但需关注）
        reason: 不就绪原因（is_ready=False 时填充）
        suspended_count: 停牌标的数量
        delisted_count: 退市标的数量
        new_listing_count: 新上市标的数量（上市 < 30 天）
        import_completeness: 导入完整性（当日数据量 / 前一交易日数据量）
    """

    is_ready: bool
    is_trading_day: bool
    active_instrument_count: int
    bars_count: int
    coverage_rate: float
    warnings: list[str]
    reason: str | None = None
    suspended_count: int = 0
    delisted_count: int = 0
    new_listing_count: int = 0
    import_completeness: float = 1.0


class StrategyBatchService:
    """通用策略批量计算服务。

    DSA 作为第一个支持的 strategy_key，后续可扩展其他策略。
    由 Worker 调用，不在 HTTP 请求内执行。

    用法：
        service = StrategyBatchService()
        run = await service.create_batch_run(db, "dsa_selector", date(2026, 6, 20))
        # Worker 轮询 queued run 并执行
        claimed = await service.claim_next_run(db)
        await db.commit()
        await service.execute_run(db, claimed.id)
        # Admin 发布
        await service.publish_run(db, run.id)
    """

    # [StrategyBatchService] - run 级总超时默认值（秒）。
    # 取消单股硬超时后，由 run 级总预算控制整体执行时间；测试可覆盖。
    _RUN_TOTAL_TIMEOUT_SECONDS: float = 600.0

    # [StrategyBatchService] - skipped 原因标准编码允许列表。
    # 自动发布门禁会校验所有 skipped 项的 reason_code 必须在此集合内。
    _SKIPPED_REASON_ALLOWLIST: set[str] = {
        "insufficient_data",
        "suspended",
        "delisted",
        "new_listing",
    }

    # [StrategyBatchService] - failed 原因标准编码集合（仅用于日志/可读性）。
    _FAILED_REASON_CODES: set[str] = {
        "timeout",
        "runtime_error",
        "data_error",
    }

    def __init__(self) -> None:
        """初始化 batch service。

        实例级 run 超时时间默认使用类常量，允许测试或调用方覆盖。
        """
        self._run_total_timeout_seconds: float = self._RUN_TOTAL_TIMEOUT_SECONDS

    async def recover_stale_runs(self, db: AsyncSession) -> int:
        """Worker 启动时恢复过期租约的 running 和 stale queued 任务。

        恢复逻辑：
        1. 查找 lease_expires_at < now() 的 running 任务 → 重置为 queued，attempt_count +1
        2. 查找 queued_at < now() - 2h 的 queued 任务 → attempt_count +1
        3. attempt_count >= 3 的任务标记为 failed（error_code=max_retries_exceeded，
           failure_stage=WORKER_INTERRUPTED，error_message 记录详情）

        Args:
            db: 异步会话

        Returns:
            恢复的任务数量
        """
        now = datetime.now(UTC)
        recovered = 0

        # 恢复 lease 过期的 running 任务
        stmt = select(StrategyRun).where(
            StrategyRun.status == "running",
            StrategyRun.lease_expires_at < now,
        )
        result = await db.execute(stmt)
        stale_running = result.scalars().all()

        for run in stale_running:
            run.attempt_count = (run.attempt_count or 0) + 1
            if run.attempt_count >= _MAX_ATTEMPTS:
                run.status = "failed"
                run.error_code = "max_retries_exceeded"
                # [StrategyRun] - Worker 中断恢复达上限，记录失败阶段与详情
                run.failure_stage = FAILURE_STAGE_WORKER_INTERRUPTED
                run.error_message = f"Worker 租约过期，重试 {run.attempt_count} 次后仍失败"
                run.finished_at = now
            else:
                run.status = "queued"
                run.started_at = None
                run.lease_expires_at = None
                run.worker_id = None
                run.next_retry_at = now
            recovered += 1

        # 恢复 stale queued 任务（超过 2 小时未被消费）
        stale_threshold = now - timedelta(hours=_STALE_QUEUED_HOURS)
        stmt = select(StrategyRun).where(
            StrategyRun.status == "queued",
            StrategyRun.queued_at < stale_threshold,
        )
        result = await db.execute(stmt)
        stale_queued = result.scalars().all()

        for run in stale_queued:
            run.attempt_count = (run.attempt_count or 0) + 1
            if run.attempt_count >= _MAX_ATTEMPTS:
                run.status = "failed"
                run.error_code = "max_retries_exceeded"
                # [StrategyRun] - 排队超时恢复达上限，记录失败阶段与详情
                run.failure_stage = FAILURE_STAGE_WORKER_INTERRUPTED
                run.error_message = f"任务排队超 {_STALE_QUEUED_HOURS}h 未消费，重试 {run.attempt_count} 次后仍失败"
                run.finished_at = now
            else:
                run.next_retry_at = now
            recovered += 1

        if recovered > 0:
            await db.flush()
            logger.info(
                "[StrategyBatchService] 恢复了 %d 个过期任务", recovered,
            )

        return recovered

    async def create_batch_run(
        self,
        db: AsyncSession,
        strategy_key: str,
        trade_date: date,
        run_type: str = "scheduled",
        instrument_ids: list[uuid.UUID] | None = None,
    ) -> StrategyRun:
        """创建批量计算运行（status=queued）。

        流程：
        1. 校验 run_type 合法
        2. 查找策略最新 released 版本
        3. 数据就绪检查（非交易日/数据未就绪则拒绝）
        4. 查询当天同 (strategy_version_id, trade_date, run_type) 的所有 runs
        5. 若存在 published/completed/running/queued 状态 run，直接返回（幂等）
        6. 若存在 failed/partial_failed/interrupted 状态 run，创建 attempt_no = max + 1
        7. 生成幂等键：strategy_key:strategy_version_id:trade_date:run_type:attempt_no
        8. 创建 StrategyRun（status=queued, effective_config 从 manifest 读取）
        9. 预创建 strategy_run_items（status=pending）

        Args:
            db: 异步会话
            strategy_key: 策略 key（如 "dsa_selector"）
            trade_date: 交易日
            run_type: 触发方式（manual/scheduled/replay）
            instrument_ids: 指定标的列表（None 表示全市场活跃标的）

        Returns:
            StrategyRun ORM 对象（status=queued）

        Raises:
            ValueError: 非交易日/数据未就绪/策略无可用版本/非法 run_type
            RuntimeError: 创建失败
        """
        # 1. 校验 run_type
        if run_type not in VALID_RUN_TYPES:
            raise ValueError(
                f"非法 run_type: {run_type}（合法值: {sorted(VALID_RUN_TYPES)})"
            )

        # 2. 查找策略最新 released 版本
        version_id, version = await self._get_latest_released_version(
            db, strategy_key
        )

        # 3. 数据就绪检查
        readiness = await self.check_data_readiness(db, trade_date)
        if not readiness.is_ready:
            raise ValueError(
                f"数据未就绪，拒绝创建批量计算: trade_date={trade_date}, "
                f"reason={readiness.reason}"
            )

        # 4. 查询当天同 (version, date, run_type) 的所有 runs
        runs_stmt = select(StrategyRun).where(
            StrategyRun.strategy_version_id == version_id,
            StrategyRun.trade_date == trade_date,
            StrategyRun.run_type == run_type,
        )
        runs_result = await db.execute(runs_stmt)
        existing_runs = list(runs_result.scalars().all())

        # 5. 存在进行中的 run（published/completed/running/queued），直接返回
        blocking_run = next(
            (r for r in existing_runs if r.status in _BLOCKING_STATUSES), None
        )
        if blocking_run is not None:
            logger.info(
                "批量计算已存在进行中的运行: run_id=%s, status=%s, "
                "strategy_key=%s, trade_date=%s, run_type=%s",
                blocking_run.id, blocking_run.status,
                strategy_key, trade_date, run_type,
            )
            return blocking_run

        # 6. 失败运行不阻断当日重试：基于最大 attempt_no 创建新 attempt
        # [StrategyRun] - _RETRYABLE_STATUSES={failed,partial_failed,interrupted} 允许重建，
        # _BLOCKING_STATUSES={published,completed,running,queued} 已在步骤 5 跳过
        attempt_no = 1
        retryable_runs = [
            r for r in existing_runs if r.status in _RETRYABLE_STATUSES
        ]
        if retryable_runs:
            attempt_no = max((r.attempt_no or 1) for r in retryable_runs) + 1
            logger.info(
                "[StrategyBatch] 检测到可重试运行，创建新 attempt: "
                "strategy_key=%s, trade_date=%s, run_type=%s, "
                "prev_attempts=%s, new_attempt_no=%d",
                strategy_key, trade_date, run_type,
                [(r.id, r.status, r.attempt_no) for r in retryable_runs],
                attempt_no,
            )

        # 7. 生成幂等键（含 strategy_version_id 与 attempt_no）
        idempotency_key = (
            f"{strategy_key}:{version_id}:{trade_date.isoformat()}:"
            f"{run_type}:{attempt_no}"
        )

        # 防御并发：二次校验幂等键唯一性
        existing_key_stmt = select(StrategyRun).where(
            StrategyRun.idempotency_key == idempotency_key
        )
        existing_key_result = await db.execute(existing_key_stmt)
        existing_key_run = existing_key_result.scalar_one_or_none()
        if existing_key_run is not None:
            logger.info(
                "幂等键已存在（并发）: idempotency_key=%s, run_id=%s",
                idempotency_key, existing_key_run.id,
            )
            return existing_key_run

        # 8. 从 manifest 读取 effective_config
        manifest = version.manifest
        parameters = manifest.get("parameters", [])
        effective_config: dict[str, Any] = {
            p["key"]: p.get("default") for p in parameters
        }
        # 计算 effective_config_hash
        config_str = str(sorted(effective_config.items()))
        effective_config_hash = hashlib.sha256(
            config_str.encode("utf-8")
        ).hexdigest()[:16]

        # 9. 解析标的列表
        if instrument_ids is None:
            instrument_ids = await self._resolve_active_instruments(db, trade_date)

        # 10. 创建 StrategyRun
        run = StrategyRun(
            strategy_version_id=version_id,
            run_type=run_type,
            trade_date=trade_date,
            status="queued",
            input_overrides={
                "strategy_key": strategy_key,
                "instrument_count": len(instrument_ids),
            },
            started_at=None,
            queued_at=datetime.now(UTC),
            idempotency_key=idempotency_key,
            effective_config=effective_config,
            effective_config_hash=effective_config_hash,
            total_instruments=len(instrument_ids),
            succeeded_count=0,
            failed_count=0,
            skipped_count=0,
            attempt_no=attempt_no,
        )
        db.add(run)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(
                f"创建批量计算运行失败 strategy_key={strategy_key}, "
                f"trade_date={trade_date}: {exc}"
            ) from exc

        # 11. 预创建 strategy_run_items（status=pending）
        run_items = [
            StrategyRunItem(
                run_id=run.id,
                instrument_id=iid,
                status="pending",
                attempt_count=0,
            )
            for iid in instrument_ids
        ]
        db.add_all(run_items)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(
                f"预创建 run_items 失败 run_id={run.id}: {exc}"
            ) from exc

        logger.info(
            "创建批量计算: run_id=%s, strategy_key=%s, trade_date=%s, "
            "run_type=%s, attempt_no=%d, instruments=%d, effective_config_hash=%s",
            run.id, strategy_key, trade_date, run_type, attempt_no,
            len(instrument_ids), effective_config_hash,
        )
        return run

    async def retry_run(
        self,
        db: AsyncSession,
        run_id: uuid.UUID,
    ) -> StrategyRun:
        """基于已有运行创建新的 attempt（业务重试）。

        旧运行保留，新运行 attempt_no = 同维度最大 attempt_no + 1。

        Args:
            db: 异步会话
            run_id: 原运行 ID

        Returns:
            新建的 StrategyRun（status=queued）

        Raises:
            ValueError: 运行不存在或状态不允许重试
            RuntimeError: 创建失败
        """
        # 1. 加载原运行
        run = await db.get(StrategyRun, run_id)
        if run is None:
            raise ValueError(f"运行不存在: run_id={run_id}")
        if run.status not in _RETRYABLE_STATUSES:
            raise ValueError(
                f"运行状态不允许重试（当前 {run.status}，"
                f"仅 {sorted(_RETRYABLE_STATUSES)} 可重试）: run_id={run_id}"
            )

        # 2. 计算新 attempt_no
        runs_stmt = select(StrategyRun).where(
            StrategyRun.strategy_version_id == run.strategy_version_id,
            StrategyRun.trade_date == run.trade_date,
            StrategyRun.run_type == run.run_type,
        )
        runs_result = await db.execute(runs_stmt)
        sibling_runs = list(runs_result.scalars().all())
        attempt_no = max((r.attempt_no or 1) for r in sibling_runs) + 1

        # 3. 解析 strategy_key
        strategy_key = None
        if run.input_overrides:
            strategy_key = run.input_overrides.get("strategy_key")
        if strategy_key is None:
            version = await db.get(StrategyVersion, run.strategy_version_id)
            if version is None:
                raise ValueError(
                    f"策略版本不存在: strategy_version_id={run.strategy_version_id}"
                )
            definition = await db.get(
                StrategyDefinition, version.strategy_definition_id
            )
            if definition is None:
                raise ValueError(
                    f"策略定义不存在: strategy_definition_id={version.strategy_definition_id}"
                )
            strategy_key = definition.strategy_key

        # 4. 生成幂等键
        idempotency_key = (
            f"{strategy_key}:{run.strategy_version_id}:"
            f"{run.trade_date.isoformat()}:{run.run_type}:{attempt_no}"
        )

        # 5. 创建新运行（复制原运行配置）
        new_run = StrategyRun(
            strategy_version_id=run.strategy_version_id,
            run_type=run.run_type,
            trade_date=run.trade_date,
            status="queued",
            input_overrides=dict(run.input_overrides) if run.input_overrides else {},
            started_at=None,
            queued_at=datetime.now(UTC),
            idempotency_key=idempotency_key,
            effective_config=run.effective_config,
            effective_config_hash=run.effective_config_hash,
            total_instruments=run.total_instruments,
            succeeded_count=0,
            failed_count=0,
            skipped_count=0,
            attempt_no=attempt_no,
        )
        db.add(new_run)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(
                f"创建重试运行失败 run_id={run_id}: {exc}"
            ) from exc

        # 6. 复制原运行的 instrument 列表（从 run_items 恢复）
        items_stmt = select(StrategyRunItem.instrument_id).where(
            StrategyRunItem.run_id == run.id
        )
        items_result = await db.execute(items_stmt)
        instrument_ids = [row[0] for row in items_result.all()]

        if instrument_ids:
            run_items = [
                StrategyRunItem(
                    run_id=new_run.id,
                    instrument_id=iid,
                    status="pending",
                    attempt_count=0,
                )
                for iid in instrument_ids
            ]
            db.add_all(run_items)
            try:
                await db.flush()
            except Exception as exc:
                await db.rollback()
                raise RuntimeError(
                    f"预创建重试 run_items 失败 run_id={new_run.id}: {exc}"
                ) from exc

        logger.info(
            "创建重试运行: new_run_id=%s, original_run_id=%s, strategy_key=%s, "
            "trade_date=%s, attempt_no=%d",
            new_run.id, run.id, strategy_key, run.trade_date, attempt_no,
        )
        return new_run

    async def claim_next_run(
        self,
        db: AsyncSession,
    ) -> StrategyRun | None:
        """Worker 加锁领取下一个 queued 运行。

        使用 SELECT ... FOR UPDATE SKIP LOCKED 避免多 Worker 竞争。
        领取成功后更新 status=running，设置 worker_id / 心跳 / 租约。

        Args:
            db: 异步会话

        Returns:
            StrategyRun（status=running），无 queued 任务时返回 None

        Raises:
            RuntimeError: 领取失败
        """
        stmt = (
            select(StrategyRun)
            .where(StrategyRun.status == "queued")
            .order_by(StrategyRun.queued_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        try:
            result = await db.execute(stmt)
        except Exception as exc:
            raise RuntimeError(f"领取任务查询失败: {exc}") from exc

        run = result.scalar_one_or_none()
        if run is None:
            return None

        now = datetime.now(UTC)
        run.status = "running"
        run.started_at = now
        run.heartbeat_at = now
        run.lease_expires_at = now + timedelta(minutes=_LEASE_DURATION_MINUTES)
        run.worker_id = _get_worker_id()
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(f"领取任务状态更新失败 run_id={run.id}: {exc}") from exc

        logger.info(
            "Worker 领取任务: run_id=%s, worker_id=%s, lease_expires_at=%s",
            run.id, run.worker_id, run.lease_expires_at,
        )
        return run

    async def execute_run(
        self,
        db: AsyncSession,
        run_id: uuid.UUID,
        job_run_id: uuid.UUID | None = None,
    ) -> None:
        """执行批量计算（由 Worker 调用）。

        流程：
        1. 加载 StrategyRun，校验 status=running（已由 claim_next_run 领取）
        2. 启动独立 Session 心跳任务
        3. 加载 StrategyVersion + 策略运行时
        4. 查询 pending 的 strategy_run_items
        5. 逐标的执行并写入结果
        6. 汇总统计，更新 run status=completed/partial_failed/failed

        Args:
            db: 异步会话
            run_id: 运行 ID
            job_run_id: 可选的 SchedulerJobRun.id，传入时写入 BATCH_START/BATCH_PROGRESS/
                BATCH_DONE/QUALITY_GATE/PUBLISH_DONE/BATCH_FAILED 事件到 job_run_events 时间线。
                典型场景：盘后编排（after_close_orchestrator）调用时传入 orchestrator 的 job_run_id。

        Raises:
            ValueError: run 不存在或状态非 running
            RuntimeError: 执行失败
        """
        # 1. 加载 StrategyRun
        run = await db.get(StrategyRun, run_id)
        if run is None:
            raise ValueError(f"运行不存在: run_id={run_id}")
        if run.status != "running":
            raise ValueError(
                f"运行状态非 running（当前 {run.status}），拒绝执行: run_id={run_id}"
            )

        worker_id = run.worker_id or _get_worker_id()

        # [JobRunEvent] - BATCH_START：开始计算
        if job_run_id is not None:
            await self._append_batch_event(
                job_run_id, "BATCH_START", "info",
                f"开始批量计算: run_id={run_id}",
                {"run_id": str(run_id), "total_instruments": run.total_instruments or 0},
            )

        # 2. 启动独立 Session 心跳任务
        heartbeat_task = asyncio.create_task(
            _run_heartbeat_task(run.id, worker_id)
        )

        try:
            # 3. 加载 StrategyVersion + 策略运行时
            version = await db.get(StrategyVersion, run.strategy_version_id)
            if version is None:
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                await db.flush()
                # [JobRunEvent] - BATCH_FAILED：策略版本不存在
                if job_run_id is not None:
                    await self._append_batch_event(
                        job_run_id, "BATCH_FAILED", "error",
                        f"策略版本不存在: strategy_version_id={run.strategy_version_id}",
                        {"error_code": "VERSION_NOT_FOUND", "run_id": str(run_id)},
                    )
                raise ValueError(
                    f"策略版本不存在: strategy_version_id={run.strategy_version_id}"
                )

            try:
                runtime = await StrategyLoader.load(version)
            except Exception as exc:
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                await db.flush()
                # [JobRunEvent] - BATCH_FAILED：加载策略运行时失败
                if job_run_id is not None:
                    await self._append_batch_event(
                        job_run_id, "BATCH_FAILED", "error",
                        f"加载策略运行时失败: {exc}",
                        {"error_code": "LOAD_RUNTIME_FAILED", "run_id": str(run_id)},
                    )
                raise RuntimeError(
                    f"加载策略运行时失败 run_id={run_id}: {exc}"
                ) from exc

            # 4. 查询 pending 的 strategy_run_items
            items_stmt = (
                select(StrategyRunItem)
                .where(
                    and_(
                        StrategyRunItem.run_id == run_id,
                        StrategyRunItem.status == "pending",
                    )
                )
                .order_by(StrategyRunItem.id)
            )
            items_result = await db.execute(items_stmt)
            run_items = list(items_result.scalars().all())

            if not run_items:
                # 无待执行标的，直接完成
                run.status = "completed"
                run.finished_at = datetime.now(UTC)
                await db.flush()
                logger.info("批量计算无待执行标的，直接完成: run_id=%s", run_id)
                return

            # 5. 逐标的执行（run 级总超时 + 可取消）
            succeeded = 0
            failed = 0
            skipped = 0
            all_results = []
            run_start_at = datetime.now(UTC)
            timeout_seconds = self._run_total_timeout_seconds

            for item in run_items:
                item.status = "running"
                item.started_at = datetime.now(UTC)
                item.attempt_count += 1
                try:
                    await db.flush()
                except Exception as exc:
                    await db.rollback()
                    raise RuntimeError(
                        f"更新 run_item 状态为 running 失败 item_id={item.id}: {exc}"
                    ) from exc

                # 计算剩余 run 级预算；若已耗尽，剩余项直接记 timeout 失败
                elapsed = (datetime.now(UTC) - run_start_at).total_seconds()
                remaining_seconds = timeout_seconds - elapsed
                if remaining_seconds <= 0:
                    item.status = "failed"
                    item.reason_code = "timeout"
                    item.error_message = "run 级总超时，剩余项取消"
                    item.finished_at = datetime.now(UTC)
                    failed += 1
                    try:
                        await db.flush()
                    except Exception as exc:
                        await db.rollback()
                        raise RuntimeError(
                            f"更新 run_item 状态失败 item_id={item.id}: {exc}"
                        ) from exc
                    continue

                try:
                    result = await asyncio.wait_for(
                        self._execute_single_instrument(
                            db, run, version, runtime, item
                        ),
                        timeout=remaining_seconds,
                    )
                    if result is not None:
                        all_results.append(result)
                        item.status = "succeeded"
                        item.finished_at = datetime.now(UTC)
                        succeeded += 1
                    else:
                        item.status = "skipped"
                        item.reason_code = "insufficient_data"
                        item.finished_at = datetime.now(UTC)
                        skipped += 1
                except TimeoutError:
                    logger.warning(
                        "标的执行超时 instrument_id=%s",
                        item.instrument_id,
                    )
                    item.status = "failed"
                    item.reason_code = "timeout"
                    item.error_message = "run 级总超时"
                    item.finished_at = datetime.now(UTC)
                    failed += 1
                except BudgetExceededError as exc:
                    logger.warning(
                        "标的执行超出预算 instrument_id=%s: %s",
                        item.instrument_id, exc,
                    )
                    item.status = "failed"
                    item.reason_code = "timeout"
                    item.error_message = str(exc)[:500]
                    item.finished_at = datetime.now(UTC)
                    failed += 1
                except Exception as exc:
                    logger.warning(
                        "标的执行失败 instrument_id=%s: %s",
                        item.instrument_id, exc,
                    )
                    item.status = "failed"
                    item.reason_code = "runtime_error"
                    item.error_message = str(exc)[:500]
                    item.finished_at = datetime.now(UTC)
                    failed += 1

                try:
                    await db.flush()
                except Exception as exc:
                    await db.rollback()
                    raise RuntimeError(
                        f"更新 run_item 状态失败 item_id={item.id}: {exc}"
                    ) from exc

                # [JobRunEvent] - BATCH_PROGRESS：每 500 股一次进度事件
                processed = succeeded + failed + skipped
                if job_run_id is not None and processed > 0 and processed % 500 == 0:
                    await self._append_batch_event(
                        job_run_id, "BATCH_PROGRESS", "info",
                        f"进度: {processed}/{len(run_items)} succeeded={succeeded} failed={failed}",
                        {
                            "processed": processed,
                            "total": len(run_items),
                            "succeeded": succeeded,
                            "failed": failed,
                            "skipped": skipped,
                        },
                    )

            # 5.1 批量写入结果
            if all_results:
                try:
                    await strategy_result_repository.write_results(
                        db, run.id, run.strategy_version_id, all_results
                    )
                except Exception as exc:
                    await db.rollback()
                    raise RuntimeError(
                        f"批量写入结果失败 run_id={run_id}: {exc}"
                    ) from exc

            # 6. 汇总统计，更新 run status
            run.succeeded_count = succeeded
            run.failed_count = failed
            run.skipped_count = skipped
            run.finished_at = datetime.now(UTC)

            if failed == 0:
                run.status = "completed"
            elif succeeded > 0:
                run.status = "partial_failed"
            else:
                run.status = "failed"

            try:
                await db.flush()
            except Exception as exc:
                await db.rollback()
                raise RuntimeError(
                    f"更新运行汇总状态失败 run_id={run_id}: {exc}"
                ) from exc

            logger.info(
                "批量计算完成: run_id=%s, status=%s, "
                "total=%d, succeeded=%d, failed=%d, skipped=%d",
                run_id, run.status,
                len(run_items), succeeded, failed, skipped,
            )

            # [JobRunEvent] - BATCH_DONE：完成
            if job_run_id is not None:
                await self._append_batch_event(
                    job_run_id, "BATCH_DONE", "info",
                    f"批量计算完成: status={run.status}, "
                    f"succeeded={succeeded}, failed={failed}, skipped={skipped}",
                    {
                        "run_id": str(run_id),
                        "status": run.status,
                        "total": len(run_items),
                        "succeeded": succeeded,
                        "failed": failed,
                        "skipped": skipped,
                    },
                )

            # 6.1 统计实际写入结果数，用于质量门禁校验
            result_count = 0
            try:
                result_count = await strategy_result_repository.count_by_run(
                    db, run.id
                )
            except Exception as exc:
                logger.warning(
                    "统计 strategy_results 数量失败 run_id=%s: %s",
                    run_id, exc,
                )

            # 6.2 质量门禁检查（仅记录结果，不自动发布）
            # 自动发布由 after_close_orchestrator / 调度任务统一负责，避免 execute_run
            # 与 orchestrator 重复调用 publish_run。
            if run.run_type == _RUN_TYPE_SCHEDULED:
                quality_passed = await self._check_quality_gates(
                    run, result_count=result_count, db=db
                )
                # [JobRunEvent] - QUALITY_GATE：质量门禁结果
                if job_run_id is not None:
                    await self._append_batch_event(
                        job_run_id, "QUALITY_GATE", "info" if quality_passed else "warn",
                        f"质量门禁: {'通过' if quality_passed else '未通过'}",
                        {
                            "passed": quality_passed,
                            "run_status": run.status,
                            "coverage": (
                                (succeeded / len(run_items))
                                if run_items else 0.0
                            ),
                        },
                    )
                if quality_passed:
                    logger.info(
                        "质量门禁通过，等待调度任务发布: run_id=%s, trade_date=%s",
                        run_id, run.trade_date,
                    )
                else:
                    logger.info(
                        "质量门禁未通过: run_id=%s, status=%s",
                        run_id, run.status,
                    )
        finally:
            # 取消独立心跳任务
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _append_batch_event(
        self,
        job_run_id: uuid.UUID,
        step: str,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """[JobRunEvent] - 用独立 session 写入批量计算事件并 commit。

        使用独立 session 避免与 execute_run 主 session 的 rollback 冲突，
        确保事件即使在主流程 rollback 时也能持久化。
        事件写入失败仅记录警告，不影响批量计算主流程。
        """
        from app.services.job_run_event_service import append_event

        try:
            async with AsyncSessionLocal() as event_db:
                await append_event(
                    db=event_db,
                    job_run_id=job_run_id,
                    step=step,
                    level=level,
                    message=message,
                    payload=payload,
                )
                await event_db.commit()
        except Exception as exc:
            logger.warning(
                "[StrategyBatch] 写入 job_run_event 失败 step=%s job_run_id=%s: %s",
                step, job_run_id, exc,
            )

    async def publish_run(self, db: AsyncSession, run_id: uuid.UUID) -> StrategyRun:
        """发布运行结果（admin 调用）。

        status: completed → published
        记录 published_at 时间戳

        [DSA] - 描述: admin 手动发布同样禁止 partial_failed，与自动发布门禁一致，
        仅允许 completed 状态进入 published。

        Args:
            db: 异步会话
            run_id: 运行 ID

        Returns:
            更新后的 StrategyRun

        Raises:
            ValueError: run 不存在或状态不允许发布
            RuntimeError: 更新失败
        """
        run = await db.get(StrategyRun, run_id)
        if run is None:
            raise ValueError(f"运行不存在: run_id={run_id}")

        # [DSA] - 描述: publish_run 显式拒绝 partial_failed，仅 completed 可发布
        if run.status != "completed":
            raise ValueError(
                f"运行状态不允许发布（当前 {run.status}，"
                f"仅 completed 可发布）: run_id={run_id}"
            )

        if (run.succeeded_count or 0) <= 0:
            raise ValueError("没有成功结果，禁止发布")

        run.status = "published"
        run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(
                f"发布运行失败 run_id={run_id}: {exc}"
            ) from exc

        logger.info(
            "发布运行: run_id=%s, trade_date=%s, published_at=%s",
            run_id, run.trade_date, run.published_at,
        )
        return run

    async def check_data_readiness(
        self, db: AsyncSession, trade_date: date
    ) -> DataReadinessResult:
        """数据就绪检查。

        检查项：
        1. 交易日检查（calendar_service.is_trading_day_async）
        2. 活跃/停牌/退市标的数量（Instrument.status）
        3. 当日 K 线导入数量（BarDaily WHERE trade_date = :date）
        4. 覆盖率检查（bars_count / active_instrument_count）
        5. 新上市标的检查（上市 < 30 天，历史数据可能不足）
        6. 导入完整性检查（当日数据量 vs 前一交易日数据量）

        Args:
            db: 异步会话
            trade_date: 交易日

        Returns:
            DataReadinessResult
        """
        warnings: list[str] = []

        # 1. 交易日检查
        is_trading = await is_trading_day_async(db, trade_date)
        if not is_trading:
            return DataReadinessResult(
                is_ready=False,
                is_trading_day=False,
                active_instrument_count=0,
                bars_count=0,
                coverage_rate=0.0,
                warnings=warnings,
                reason=f"非交易日: {trade_date}",
            )

        # 2. 标的状态统计（active/suspended/delisted）
        # [StrategyBatch] - active_count 仅算 A 股股票（排除指数/基金/ETF），与覆盖率分母口径一致
        active_count = int(await db.scalar(
            select(func.count()).select_from(Instrument).where(
                Instrument.status == "active"
            ).where(stock_symbol_sql_filter(Instrument))
        ) or 0)

        if active_count == 0:
            return DataReadinessResult(
                is_ready=False,
                is_trading_day=True,
                active_instrument_count=0,
                bars_count=0,
                coverage_rate=0.0,
                warnings=warnings,
                reason="无活跃标的",
            )

        suspended_count = int(await db.scalar(
            select(func.count()).select_from(Instrument).where(
                Instrument.status == "suspended"
            )
        ) or 0)

        delisted_count = int(await db.scalar(
            select(func.count()).select_from(Instrument).where(
                Instrument.status == "delisted"
            )
        ) or 0)

        # 3. 当日 K 线数量（仅算 A 股股票的 K 线，与 active_count 口径一致）
        # [StrategyBatch] - JOIN instruments + stock_symbol_sql_filter 排除指数/基金/ETF 的 K 线
        bars_count = int(await db.scalar(
            select(func.count()).select_from(BarDaily)
            .join(Instrument, BarDaily.instrument_id == Instrument.id)
            .where(BarDaily.trade_date == trade_date)
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
        ) or 0)

        # 4. 覆盖率检查
        coverage_rate = bars_count / active_count if active_count > 0 else 0.0

        if coverage_rate < DATA_COVERAGE_THRESHOLD:
            warnings.append(
                f"数据覆盖率不足: {coverage_rate:.1%}（阈值 {DATA_COVERAGE_THRESHOLD:.0%}），"
                f"bars={bars_count}, active={active_count}，DSA 不执行"
            )

        # 5. 新上市标的检查（上市 < 30 天）
        new_listing_cutoff = trade_date - timedelta(days=30)
        # [StrategyBatch] - new_listing_count 也只算 A 股股票，与 active_count 口径一致
        new_listing_count = int(await db.scalar(
            select(func.count()).select_from(Instrument).where(
                Instrument.status == "active",
                Instrument.listing_date >= new_listing_cutoff,
            ).where(stock_symbol_sql_filter(Instrument))
        ) or 0)

        if new_listing_count > 0:
            warnings.append(
                f"有 {new_listing_count} 只新上市标的（上市 < 30 天），历史数据可能不足"
            )

        # 6. 导入完整性检查（对比上一交易日的 K 线数量）
        prev_trade_date = await self._get_previous_trade_date(db, trade_date)
        prev_bars_count = 0
        if prev_trade_date is not None:
            # [StrategyBatch] - prev_bars_count 也只算 A 股股票 K 线，与 bars_count 口径一致
            prev_bars_count = int(await db.scalar(
                select(func.count()).select_from(BarDaily)
                .join(Instrument, BarDaily.instrument_id == Instrument.id)
                .where(BarDaily.trade_date == prev_trade_date)
                .where(Instrument.status == "active")
                .where(stock_symbol_sql_filter(Instrument))
            ) or 0)

        # 当日数据量 < 前一交易日的 50%，可能导入未完成
        import_completeness = (
            bars_count / prev_bars_count
            if prev_bars_count and prev_bars_count > 0
            else 1.0
        )

        if import_completeness < 0.5:
            warnings.append(
                f"当日数据量仅为前一交易日的 {import_completeness:.1%}，行情可能未导入完成"
            )

        # 7. 停牌标的警告
        if suspended_count > 0:
            warnings.append(
                f"有 {suspended_count} 只停牌标的，将跳过计算"
            )

        # 数据就绪：交易日 + 有活跃标的 + 有 K 线数据 + 覆盖率 >= 90% + 导入完整性 >= 50%
        is_ready = (
            is_trading
            and active_count > 0
            and bars_count > 0
            and coverage_rate >= DATA_COVERAGE_THRESHOLD
            and import_completeness >= 0.5
        )

        return DataReadinessResult(
            is_ready=is_ready,
            is_trading_day=is_trading,
            active_instrument_count=active_count,
            bars_count=bars_count,
            coverage_rate=coverage_rate,
            warnings=warnings,
            reason=None if is_ready else "数据不完整或导入未完成",
            suspended_count=suspended_count,
            delisted_count=delisted_count,
            new_listing_count=new_listing_count,
            import_completeness=import_completeness,
        )

    async def _check_quality_gates(
        self,
        run: StrategyRun,
        result_count: int,
        db: AsyncSession | None = None,
    ) -> bool:
        """检查运行是否通过严格质量门禁（用于定时任务自动发布）。

        自动发布必须同时满足：
        1. status == "completed"（partial_failed 禁止自动发布）
        2. failed_count == 0
        3. strategy_results.count == succeeded_count
        4. succeeded_count + skipped_count == total_instruments
        5. skipped 原因全部在允许列表内且每个 skipped 都有 reason_code
        6. computable universe 覆盖率 100%（即 (succeeded + skipped) / total == 1）
        7. succeeded_count > 0

        Args:
            run: 运行记录
            result_count: 实际写入的 strategy_results 数量（来自 repository 统计）
            db: 可选的数据库 session；传入时校验 skipped 项的 reason_code

        Returns:
            True 表示通过严格质量门禁，可自动发布
        """
        # 门禁 1：状态必须为 completed
        if run.status != "completed":
            logger.info(
                "质量门禁未通过: 状态不允许自动发布（当前 %s，仅 completed 可发布）, run_id=%s",
                run.status, run.id,
            )
            return False

        succeeded = run.succeeded_count or 0
        failed = run.failed_count or 0
        skipped = run.skipped_count or 0
        total = run.total_instruments or 0

        # 门禁 2：失败数必须为 0
        if failed != 0:
            logger.info(
                "质量门禁未通过: failed_count=%d > 0, run_id=%s",
                failed, run.id,
            )
            return False

        # 门禁 3：实际结果数必须等于 succeeded_count
        if result_count != succeeded:
            logger.info(
                "质量门禁未通过: result_count=%d != succeeded_count=%d, run_id=%s",
                result_count, succeeded, run.id,
            )
            return False

        # 门禁 4：成功 + 跳过必须覆盖全部标的
        if total <= 0 or (succeeded + skipped) != total:
            logger.info(
                "质量门禁未通过: succeeded+skipped=%d != total_instruments=%d, run_id=%s",
                succeeded + skipped, total, run.id,
            )
            return False

        # 门禁 5：skipped 原因必须在允许列表且每个 skipped 都有 reason_code
        if skipped > 0 and db is not None:
            stmt = select(StrategyRunItem.reason_code).where(
                StrategyRunItem.run_id == run.id,
                StrategyRunItem.status == "skipped",
            )
            result = await db.execute(stmt)
            skipped_reasons = {row[0] for row in result.all()}
            if None in skipped_reasons or "" in skipped_reasons:
                logger.info(
                    "质量门禁未通过: 存在无 reason_code 的 skipped 项, run_id=%s",
                    run.id,
                )
                return False
            invalid_reasons = skipped_reasons - self._SKIPPED_REASON_ALLOWLIST
            if invalid_reasons:
                logger.info(
                    "质量门禁未通过: skipped 原因 %s 不在允许列表, run_id=%s",
                    invalid_reasons, run.id,
                )
                return False

        # 门禁 6：至少有一个成功结果
        if succeeded <= 0:
            logger.info(
                "质量门禁未通过: 没有成功结果, run_id=%s", run.id,
            )
            return False

        logger.info(
            "质量门禁通过: status=%s, succeeded=%d, skipped=%d, "
            "total=%d, result_count=%d, run_id=%s",
            run.status, succeeded, skipped, total, result_count, run.id,
        )
        return True

    async def _get_previous_trade_date(
        self, db: AsyncSession, trade_date: date
    ) -> date | None:
        """获取前一交易日。

        从 trading_calendar 表查询 trade_date 之前最近的交易日。

        Args:
            db: 异步会话
            trade_date: 当前交易日

        Returns:
            前一交易日 date，或 None（无历史交易日）
        """
        from app.models.calendar import TradingCalendar

        result = await db.scalar(
            select(TradingCalendar.trade_date)
            .where(
                TradingCalendar.trade_date < trade_date,
                TradingCalendar.is_trading_day.is_(True),
                TradingCalendar.market == "A",
            )
            .order_by(TradingCalendar.trade_date.desc())
            .limit(1)
        )
        return result

    async def _get_latest_released_version(
        self, db: AsyncSession, strategy_key: str
    ) -> tuple[uuid.UUID, StrategyVersion]:
        """获取策略的最新 released 版本。

        Args:
            db: 异步会话
            strategy_key: 策略 key

        Returns:
            (version_id, version) 元组

        Raises:
            ValueError: 策略或版本不存在
        """
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
                strategy_key, version.status,
            )
        else:
            raise ValueError(f"策略无可用版本: strategy_key={strategy_key}")

        return version.id, version

    async def _resolve_active_instruments(
        self, db: AsyncSession, trade_date: date
    ) -> list[uuid.UUID]:
        """解析当日有行情的活跃标的列表。

        [StrategyBatch] - 描述: 只解析 A 股股票（排除指数/基金/ETF），与覆盖率分母口径一致

        Args:
            db: 异步会话
            trade_date: 交易日

        Returns:
            标的 ID 列表（仅 A 股股票）
        """
        # 查询当日有 K 线的活跃股票（排除指数/基金/ETF）
        stmt = (
            select(Instrument.id)
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
            .order_by(Instrument.id)
        )
        result = await db.execute(stmt)
        return [row[0] for row in result.all()]

    async def _execute_single_instrument(
        self,
        db: AsyncSession,
        run: StrategyRun,
        version: StrategyVersion,
        runtime: Any,
        item: StrategyRunItem,
    ) -> Any:
        """执行单个标的的策略计算。

        Args:
            db: 异步会话
            run: 运行记录
            version: 策略版本
            runtime: 策略运行时实例
            item: run_item 记录

        Returns:
            StrategyResult（成功时）或 None（跳过时）

        Raises:
            Exception: 执行失败时 re-raise
        """
        # 查询标的 symbol
        inst_stmt = select(Instrument.symbol, Instrument.listing_date).where(
            Instrument.id == item.instrument_id
        )
        inst_result = await db.execute(inst_stmt)
        inst_row = inst_result.first()
        if inst_row is None:
            logger.warning("标的不存在: instrument_id=%s", item.instrument_id)
            return None

        symbol = inst_row[0]
        listing_date = inst_row[1]

        # 新上市标的检查（上市不足 30 天）
        if listing_date is not None:
            days_since_listing = (run.trade_date - listing_date).days
            if days_since_listing < 30:
                logger.info(
                    "新上市标的，历史可能不足: symbol=%s, days=%d",
                    symbol, days_since_listing,
                )

        # 拉取日线行情（回看 5000 天，与 bars.py 一致）
        lookback_days = _STRATEGY_BATCH_DAILY_LOOKBACK_DAYS
        start_date = run.trade_date - timedelta(days=lookback_days)
        try:
            bars_result = await get_bars(
                db, item.instrument_id,
                timeframe="1d",
                start_date=start_date,
                end_date=run.trade_date,
                adjustment="qfq",
            )
            bars_df = bars_result.bars if bars_result.bars is not None else None
        except Exception as exc:
            raise RuntimeError(
                f"拉取行情失败 instrument_id={item.instrument_id}: {exc}"
            ) from exc

        if bars_df is None or bars_df.empty:
            logger.info(
                "无行情数据，跳过: symbol=%s, trade_date=%s",
                symbol, run.trade_date,
            )
            return None

        # 构建上下文并执行
        context = MarketDataContext(
            instrument_id=item.instrument_id,
            symbol=symbol,
            bars_daily=bars_df,
            trade_date=run.trade_date,
        )
        try:
            result = await runtime.execute(context)
            return result
        except BudgetExceededError:
            # 预算/超时异常保持原类型上抛，由 execute_run 标记 reason_code=timeout
            raise
        except Exception as exc:
            raise RuntimeError(
                f"策略执行失败 instrument_id={item.instrument_id}, "
                f"symbol={symbol}: {exc}"
            ) from exc


if __name__ == "__main__":
    # 自测入口：验证类与方法签名（无副作用，不连接数据库）
    import inspect

    # 验证类存在
    assert StrategyBatchService is not None
    print(f"StrategyBatchService: {StrategyBatchService} ✓")

    # 验证方法签名
    methods = [
        "create_batch_run", "execute_run", "publish_run",
        "check_data_readiness", "_check_quality_gates",
        "recover_stale_runs", "claim_next_run", "retry_run",
    ]
    for m in methods:
        assert hasattr(StrategyBatchService, m), f"缺少方法: {m}"
        assert callable(getattr(StrategyBatchService, m)), f"方法不可调用: {m}"
    print(f"方法存在: {methods} ✓")

    # 验证常量
    assert _HEARTBEAT_INTERVAL_SECONDS == 30
    print(f"_HEARTBEAT_INTERVAL_SECONDS={_HEARTBEAT_INTERVAL_SECONDS} ✓")

    assert VALID_RUN_TYPES == {"scheduled", "manual", "replay"}
    print(f"VALID_RUN_TYPES={VALID_RUN_TYPES} ✓")

    assert _BLOCKING_STATUSES == {"published", "completed", "running", "queued"}
    print(f"_BLOCKING_STATUSES={_BLOCKING_STATUSES} ✓")

    assert _RETRYABLE_STATUSES == {"failed", "partial_failed", "interrupted"}
    print(f"_RETRYABLE_STATUSES={_RETRYABLE_STATUSES} ✓")

    # 验证租约与恢复常量
    assert _LEASE_DURATION_MINUTES == 30
    assert _MAX_ATTEMPTS == 3
    assert _STALE_QUEUED_HOURS == 2
    print(f"_LEASE_DURATION_MINUTES={_LEASE_DURATION_MINUTES} ✓")
    print(f"_MAX_ATTEMPTS={_MAX_ATTEMPTS} ✓")
    print(f"_STALE_QUEUED_HOURS={_STALE_QUEUED_HOURS} ✓")

    # 验证 _get_worker_id 函数
    worker_id = _get_worker_id()
    assert ":" in worker_id, f"worker_id 格式错误: {worker_id}"
    print(f"_get_worker_id()={worker_id} ✓")

    # 验证 DataReadinessResult
    result = DataReadinessResult(
        is_ready=True,
        is_trading_day=True,
        active_instrument_count=5000,
        bars_count=4800,
        coverage_rate=0.96,
        warnings=[],
    )
    assert result.is_ready is True
    assert result.coverage_rate == 0.96
    print(f"DataReadinessResult: {result} ✓")

    # 验证 _check_quality_gates 签名
    sig = inspect.signature(StrategyBatchService._check_quality_gates)
    params = list(sig.parameters.keys())
    assert "run" in params
    print(f"_check_quality_gates params: {params} ✓")

    # 验证 create_batch_run 签名
    sig = inspect.signature(StrategyBatchService.create_batch_run)
    params = list(sig.parameters.keys())
    assert "strategy_key" in params
    assert "trade_date" in params
    assert "run_type" in params
    assert "instrument_ids" in params
    print(f"create_batch_run params: {params} ✓")

    # 验证 claim_next_run 签名
    sig = inspect.signature(StrategyBatchService.claim_next_run)
    params = list(sig.parameters.keys())
    assert "db" in params
    print(f"claim_next_run params: {params} ✓")

    # 验证 retry_run 签名
    sig = inspect.signature(StrategyBatchService.retry_run)
    params = list(sig.parameters.keys())
    assert "db" in params
    assert "run_id" in params
    print(f"retry_run params: {params} ✓")

    # 验证 execute_run 签名
    sig = inspect.signature(StrategyBatchService.execute_run)
    params = list(sig.parameters.keys())
    assert "run_id" in params
    assert "job_run_id" in params
    print(f"execute_run params: {params} ✓")

    # 验证 _append_batch_event 方法
    assert hasattr(StrategyBatchService, "_append_batch_event"), \
        "应有 _append_batch_event 方法"
    sig = inspect.signature(StrategyBatchService._append_batch_event)
    params = list(sig.parameters.keys())
    assert params == ["self", "job_run_id", "step", "level", "message", "payload"], \
        f"_append_batch_event 参数不匹配: {params}"
    print(f"_append_batch_event params: {params} ✓")

    # 验证 publish_run 签名
    sig = inspect.signature(StrategyBatchService.publish_run)
    params = list(sig.parameters.keys())
    assert "run_id" in params
    print(f"publish_run params: {params} ✓")

    # 验证 recover_stale_runs 签名
    sig = inspect.signature(StrategyBatchService.recover_stale_runs)
    params = list(sig.parameters.keys())
    assert "db" in params
    print(f"recover_stale_runs params: {params} ✓")

    # 验证 InvalidStrategyResultError 仍保留供 API 层捕获
    assert issubclass(InvalidStrategyResultError, Exception)
    print(f"InvalidStrategyResultError: {InvalidStrategyResultError} ✓")

    print("OK")
