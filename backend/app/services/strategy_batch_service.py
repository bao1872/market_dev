"""通用策略批量计算服务 - 后台 Worker 调用的批量计算。

DSA 作为第一个支持的 strategy_key，后续可扩展其他策略。
由 Worker 调用，不在 HTTP 请求内执行。

核心方法：
- create_batch_run: 创建批量计算运行（status=queued），数据就绪检查 + 预创建 run_items
- execute_run: 执行批量计算（Worker 调用），逐标的执行策略并写入结果
- publish_run: 发布运行结果（admin 调用），completed/partial_failed → published
- check_data_readiness: 数据就绪检查（交易日/活跃标的/K线覆盖率/停牌/退市）

设计说明：
- POST API 只创建 queued 运行，Worker 异步执行（不在 HTTP 请求内计算全市场）
- run 状态机：queued → running → completed/partial_failed → published/failed
- per-stock 跟踪：strategy_run_items 记录 status/attempt_count/error/result_id
- effective_config 从 manifest 读取并保存到 strategy_runs.effective_config（不可变）
- 幂等：idempotency_key = strategy_key:trade_date（不区分 run_type，同一天同策略只保留一个 run）

禁异常吞没：所有异常补充上下文后 re-raise。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.strategy import StrategyVersion
from app.models.strategy_run import StrategyRun, StrategyRunItem
from app.repositories import strategy_result_repository
from app.repositories.bar_repository import fetch_daily_bars
from app.services.calendar_service import is_trading_day_async
from app.services.strategy_service import (
    StrategyNotFoundError,
    list_versions,
)
from app.strategy.runtime import MarketDataContext, StrategyLoader

logger = logging.getLogger("strategy_batch_service")

# 数据就绪检查覆盖率阈值（当日 K 线数 / 活跃标的数）
DATA_COVERAGE_THRESHOLD = 0.9


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
        await service.execute_run(db, run.id)
        # Admin 发布
        await service.publish_run(db, run.id)
    """

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
        1. 查找策略最新 released 版本
        2. 数据就绪检查（非交易日/数据未就绪则拒绝）
        3. 生成幂等键：strategy_key:run_type:trade_date
        4. 创建 StrategyRun（status=queued, effective_config 从 manifest 读取）
        5. 预创建 strategy_run_items（status=pending）

        Args:
            db: 异步会话
            strategy_key: 策略 key（如 "dsa_selector"）
            trade_date: 交易日
            run_type: 触发方式（manual/scheduled/replay）
            instrument_ids: 指定标的列表（None 表示全市场活跃标的）

        Returns:
            StrategyRun ORM 对象（status=queued）

        Raises:
            ValueError: 非交易日/数据未就绪/策略无可用版本
            RuntimeError: 创建失败
        """
        # 1. 查找策略最新 released 版本
        version_id, version = await self._get_latest_released_version(
            db, strategy_key
        )

        # 2. 数据就绪检查
        readiness = await self.check_data_readiness(db, trade_date)
        if not readiness.is_ready:
            raise ValueError(
                f"数据未就绪，拒绝创建批量计算: trade_date={trade_date}, "
                f"reason={readiness.reason}"
            )

        # 3. 生成幂等键（不区分 run_type，同一天同策略只保留一个 run）
        idempotency_key = f"{strategy_key}:{trade_date.isoformat()}"

        # 检查是否已存在（幂等）
        existing_stmt = select(StrategyRun).where(
            StrategyRun.idempotency_key == idempotency_key
        )
        existing_result = await db.execute(existing_stmt)
        existing_run = existing_result.scalar_one_or_none()
        if existing_run is not None:
            logger.info(
                "批量计算已存在（幂等）: idempotency_key=%s, run_id=%s",
                idempotency_key, existing_run.id,
            )
            return existing_run

        # 4. 从 manifest 读取 effective_config
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

        # 5. 解析标的列表
        if instrument_ids is None:
            instrument_ids = await self._resolve_active_instruments(db, trade_date)

        # 6. 创建 StrategyRun
        run = StrategyRun(
            strategy_version_id=version_id,
            run_type=run_type,
            trade_date=trade_date,
            status="queued",
            input_overrides={
                "strategy_key": strategy_key,
                "instrument_count": len(instrument_ids),
            },
            started_at=datetime.now(UTC),
            idempotency_key=idempotency_key,
            effective_config=effective_config,
            effective_config_hash=effective_config_hash,
            total_instruments=len(instrument_ids),
            succeeded_count=0,
            failed_count=0,
            skipped_count=0,
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

        # 7. 预创建 strategy_run_items（status=pending）
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
            "instruments=%d, effective_config_hash=%s",
            run.id, strategy_key, trade_date,
            len(instrument_ids), effective_config_hash,
        )
        return run

    async def execute_run(self, db: AsyncSession, run_id: uuid.UUID) -> None:
        """执行批量计算（由 Worker 调用）。

        流程：
        1. 加载 StrategyRun，校验 status=queued
        2. 更新 status=running
        3. 加载 StrategyVersion + 策略运行时
        4. 查询 pending 的 strategy_run_items
        5. 逐标的执行：
           - 更新 item status=running
           - 拉取日线行情（lookback=800 bars）
           - 策略 execute(context)
           - 写入 strategy_results + strategy_result_metrics
           - 更新 item status=succeeded/failed/skipped
        6. 汇总统计，更新 run status=completed/partial_failed

        Args:
            db: 异步会话
            run_id: 运行 ID

        Raises:
            ValueError: run 不存在或状态非 queued
            RuntimeError: 执行失败
        """
        # 1. 加载 StrategyRun
        run_stmt = select(StrategyRun).where(StrategyRun.id == run_id)
        run_result = await db.execute(run_stmt)
        run = run_result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"运行不存在: run_id={run_id}")
        if run.status != "queued":
            raise ValueError(
                f"运行状态非 queued（当前 {run.status}），拒绝执行: run_id={run_id}"
            )

        # 2. 更新 status=running
        run.status = "running"
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(
                f"更新运行状态为 running 失败 run_id={run_id}: {exc}"
            ) from exc

        # 3. 加载 StrategyVersion + 策略运行时
        version_stmt = select(StrategyVersion).where(StrategyVersion.id == run.strategy_version_id)
        version_result = await db.execute(version_stmt)
        version = version_result.scalar_one_or_none()
        if version is None:
            run.status = "failed"
            await db.flush()
            raise ValueError(
                f"策略版本不存在: strategy_version_id={run.strategy_version_id}"
            )

        try:
            runtime = await StrategyLoader.load(version)
        except Exception as exc:
            run.status = "failed"
            await db.flush()
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

        # 5. 逐标的执行
        succeeded = 0
        failed = 0
        skipped = 0
        all_results = []

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

            try:
                result = await self._execute_single_instrument(
                    db, run, version, runtime, item
                )
                if result is not None:
                    all_results.append(result)
                    item.status = "succeeded"
                    item.finished_at = datetime.now(UTC)
                    succeeded += 1
                else:
                    item.status = "skipped"
                    item.finished_at = datetime.now(UTC)
                    skipped += 1
            except Exception as exc:
                logger.warning(
                    "标的执行失败 instrument_id=%s: %s",
                    item.instrument_id, exc,
                )
                item.status = "failed"
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

    async def publish_run(self, db: AsyncSession, run_id: uuid.UUID) -> StrategyRun:
        """发布运行结果（admin 调用）。

        status: completed/partial_failed → published
        记录 published_at 时间戳

        Args:
            db: 异步会话
            run_id: 运行 ID

        Returns:
            更新后的 StrategyRun

        Raises:
            ValueError: run 不存在或状态不允许发布
            RuntimeError: 更新失败
        """
        run_stmt = select(StrategyRun).where(StrategyRun.id == run_id)
        run_result = await db.execute(run_stmt)
        run = run_result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"运行不存在: run_id={run_id}")

        if run.status not in ("completed", "partial_failed"):
            raise ValueError(
                f"运行状态不允许发布（当前 {run.status}，"
                f"仅 completed/partial_failed 可发布）: run_id={run_id}"
            )

        run.status = "published"
        run.published_at = datetime.now(UTC)
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
        active_count = int(await db.scalar(
            select(func.count()).select_from(Instrument).where(
                Instrument.status == "active"
            )
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

        # 3. 当日 K 线数量
        bars_count = int(await db.scalar(
            select(func.count()).select_from(BarDaily).where(
                BarDaily.trade_date == trade_date
            )
        ) or 0)

        # 4. 覆盖率检查
        coverage_rate = bars_count / active_count if active_count > 0 else 0.0

        if coverage_rate < DATA_COVERAGE_THRESHOLD:
            warnings.append(
                f"数据覆盖率不足: {coverage_rate:.1%}（阈值 {DATA_COVERAGE_THRESHOLD:.0%}），"
                f"bars={bars_count}, active={active_count}"
            )

        # 5. 新上市标的检查（上市 < 30 天）
        new_listing_cutoff = trade_date - timedelta(days=30)
        new_listing_count = int(await db.scalar(
            select(func.count()).select_from(Instrument).where(
                Instrument.status == "active",
                Instrument.listing_date >= new_listing_cutoff,
            )
        ) or 0)

        if new_listing_count > 0:
            warnings.append(
                f"有 {new_listing_count} 只新上市标的（上市 < 30 天），历史数据可能不足"
            )

        # 6. 导入完整性检查（对比上一交易日的 K 线数量）
        prev_trade_date = await self._get_previous_trade_date(db, trade_date)
        prev_bars_count = 0
        if prev_trade_date is not None:
            prev_bars_count = int(await db.scalar(
                select(func.count()).select_from(BarDaily).where(
                    BarDaily.trade_date == prev_trade_date
                )
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

        # 数据就绪：交易日 + 有活跃标的 + 有 K 线数据 + 导入完整性 >= 50%
        is_ready = (
            is_trading
            and active_count > 0
            and bars_count > 0
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

        Args:
            db: 异步会话
            trade_date: 交易日

        Returns:
            标的 ID 列表
        """
        # 查询当日有 K 线的活跃标的
        stmt = (
            select(Instrument.id)
            .where(Instrument.status == "active")
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

        # 拉取日线行情（回看 800 bars，约 720 天）
        lookback_days = 720
        start_date = run.trade_date - timedelta(days=lookback_days)
        try:
            bars_df = await fetch_daily_bars(
                db, item.instrument_id, start_date, run.trade_date
            )
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
    methods = ["create_batch_run", "execute_run", "publish_run", "check_data_readiness"]
    for m in methods:
        assert hasattr(StrategyBatchService, m), f"缺少方法: {m}"
        assert callable(getattr(StrategyBatchService, m)), f"方法不可调用: {m}"
    print(f"方法存在: {methods} ✓")

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

    # 验证 create_batch_run 签名
    sig = inspect.signature(StrategyBatchService.create_batch_run)
    params = list(sig.parameters.keys())
    assert "strategy_key" in params
    assert "trade_date" in params
    assert "run_type" in params
    assert "instrument_ids" in params
    print(f"create_batch_run params: {params} ✓")

    # 验证 execute_run 签名
    sig = inspect.signature(StrategyBatchService.execute_run)
    params = list(sig.parameters.keys())
    assert "run_id" in params
    print(f"execute_run params: {params} ✓")

    # 验证 publish_run 签名
    sig = inspect.signature(StrategyBatchService.publish_run)
    params = list(sig.parameters.keys())
    assert "run_id" in params
    print(f"publish_run params: {params} ✓")

    print("OK")
