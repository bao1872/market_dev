"""监控批量执行服务：基于评估表的监控执行（查询→占位→计算→检测→事件→合并通知）。

执行模式：
- 自选股模式：合并所有用户自选股去重 + watchlist_monitor 策略执行
- 基于 monitor_evaluations 表实现 exactly-once 语义：
  INSERT ON CONFLICT DO NOTHING 确保同一 (策略版本, 股票, bar时间) 只计算一次

事件通知：周期结束后按用户合并为一张飞书卡片通知，每个用户只收到自己自选股的事件。

用法：
    from app.services.monitor_batch_service import MonitorBatchService
    service = MonitorBatchService()
    result = await service.execute_monitor_cycle(db)

模块自测：
    python -m app.services.monitor_batch_service
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import WATCHLIST_MONITOR
from app.models.instrument import Instrument
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.monitor_state import MonitorState as MonitorStateORM
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_event import StrategyEvent
from app.models.watchlist import UserWatchlistItem
from app.repositories import monitor_state_repository, strategy_event_repository
from app.repositories.bar_repository import get_bars
from app.strategy.monitors.watchlist_monitor import WatchlistMonitor
from app.strategy.runtime import MarketDataContext, MonitorState

logger = logging.getLogger("monitor_batch_service")

# 事件冷却窗口（秒）：同一 instrument_id + event_type + boundary 在此时间内不重复写入
_EVENT_COOLDOWN_SECONDS = 600

# [eval_recovery] - 评估租约与重试常量
_LEASE_DURATION_SECONDS = 300  # 租约时长（秒）
_MAX_RETRIES = 5  # 最大重试次数
_RETRY_BACKOFF_BASE_SECONDS = 30  # 重试退避基数（秒），实际退避 = 30 * 2^retry_count

# 行情回看参数
_DAILY_LOOKBACK_DAYS = 370  # 约250个交易日（参考脚本 bars=250）
_15MIN_LOOKBACK_DAYS = 800
_MINUTE_LOOKBACK_BARS = 2

# 北京时间
_CST = ZoneInfo("Asia/Shanghai")

# 事件类型 → emoji 映射（与旧版 monitoring.py 一致）
_EVENT_EMOJI: dict[str, str] = {
    "bb_upper_touch": "🔴",
    "bb_mid_touch": "🟠",
    "bb_lower_touch": "🟢",
    "node_cluster_touch": "🟣",
}

# 事件类型 → 中文标签
_EVENT_TYPE_LABEL: dict[str, str] = {
    "bb_upper_touch": "布林上轨穿越",
    "bb_mid_touch": "布林中轨穿越",
    "bb_lower_touch": "布林下轨穿越",
    "node_cluster_touch": "节点集群穿越",
}

# 事件类型 → 严重级别
_EVENT_SEVERITY: dict[str, str] = {
    "bb_upper_touch": "danger",
    "bb_mid_touch": "warn",
    "bb_lower_touch": "info",
    "node_cluster_touch": "warn",
}

# 严重级别 → 飞书卡片 header 颜色
_SEVERITY_TEMPLATE: dict[str, str] = {
    "danger": "red",
    "warn": "orange",
    "info": "green",
}

# 严重级别排序（danger > warn > info）
_SEVERITY_ORDER: dict[str, int] = {"danger": 3, "warn": 2, "info": 1}


@dataclass
class MonitorCycleResult:
    """单轮监控执行结果。"""

    total_instruments: int = 0
    total_states_computed: int = 0
    total_events_detected: int = 0
    total_events_written: int = 0  # after cooldown filter
    total_notifications_created: int = 0
    errors: list[str] = field(default_factory=list)


class MonitorBatchService:
    """监控批量执行服务 - 基于评估表的监控执行。

    执行模式：
    - 自选股模式：合并所有用户自选股去重 + watchlist_monitor 策略执行
    - 基于 monitor_evaluations 表实现 exactly-once 语义

    事件通知：周期结束后按用户合并为一张飞书卡片通知，每个用户只收到自己自选股的事件。

    用法：
        service = MonitorBatchService()
        result = await service.execute_monitor_cycle(db)
    """

    async def execute_monitor_cycle(self, db: AsyncSession) -> MonitorCycleResult:
        """执行单轮监控周期（基于评估表）。

        Steps:
        1. 获取 watchlist_monitor 策略的最新 released 版本
        2. 获取所有活跃自选股（去重，排除指数）
        3. 逐标的：获取最新已完成 1m bar → INSERT 评估占位 → 执行算法 → 保存结果
        4. 收集所有事件，按用户合并为一张飞书卡片通知
        5. 返回 MonitorCycleResult

        Args:
            db: 异步会话

        Returns:
            MonitorCycleResult 含各项计数和错误列表
        """
        result = MonitorCycleResult()

        # 1. 获取 watchlist_monitor 策略的最新 released 版本
        strategy_version = await self._get_watchlist_monitor_version(db)
        if strategy_version is None:
            logger.warning("watchlist_monitor 无 released 版本，跳过监控周期")
            return result

        logger.info(
            "watchlist_monitor 版本: version_id=%s version=%s",
            strategy_version.id, strategy_version.version,
        )

        # 2. 获取所有活跃自选股（去重，排除指数）+ 用户映射（通知用）
        instrument_ids, instrument_user_map, instrument_extra_info = (
            await self._resolve_watchlist_instruments(db)
        )
        if not instrument_ids:
            logger.info("无用户自选股，跳过监控周期")
            return result

        result.total_instruments = len(instrument_ids)
        logger.info("监控标的数: %d（合并所有用户自选股去重）", result.total_instruments)

        # 3. 逐标的执行，收集所有写入的事件
        all_written_events: list[StrategyEvent] = []
        for instrument_id in instrument_ids:
            try:
                events = await self._process_instrument_evaluation(
                    db, instrument_id, strategy_version, result,
                )
                all_written_events.extend(events)
            except Exception as exc:
                err_msg = (
                    f"[monitor_batch] 标的处理失败 "
                    f"instrument_id={instrument_id}: {exc}"
                )
                logger.warning(err_msg)
                result.errors.append(err_msg)

        # 4. 扩展事件接收人：为每个写入的事件匹配自选股用户
        total_recipients = 0
        if all_written_events:
            from app.services.event_recipient_service import expand_event_recipients

            for event in all_written_events:
                try:
                    count = await expand_event_recipients(db, event.id)
                    total_recipients += count
                except Exception as exc:
                    logger.warning(
                        "扩展事件接收人失败 event_id=%s: %s", event.id, exc,
                    )

        # 5. 合并通知：按用户自选股归属，每个用户一张飞书卡片
        if all_written_events:
            await self._send_merged_notification(
                db, all_written_events, instrument_user_map, instrument_extra_info, result,
            )

        logger.info(
            "监控周期完成: instruments=%d states=%d events_detected=%d "
            "events_written=%d recipients=%d notifications=%d errors=%d",
            result.total_instruments, result.total_states_computed,
            result.total_events_detected, result.total_events_written,
            total_recipients, result.total_notifications_created, len(result.errors),
        )
        return result

    async def _get_watchlist_monitor_version(
        self, db: AsyncSession,
    ) -> StrategyVersion | None:
        """获取 watchlist_monitor 策略的最新 released 版本。

        仅查询 strategy_key='watchlist_monitor' 的策略定义，
        取其最新 released 版本。不再遍历所有 kind='monitor' 策略。

        Returns:
            StrategyVersion 或 None（无 released 版本时）
        """
        # 查询 strategy_key='watchlist_monitor' 的策略定义
        def_stmt = (
            select(StrategyDefinition)
            .where(StrategyDefinition.strategy_key == WATCHLIST_MONITOR)
        )
        def_result = await db.execute(def_stmt)
        defn = def_result.scalar_one_or_none()

        if defn is None:
            return None

        # 取最新 released 版本
        ver_stmt = (
            select(StrategyVersion)
            .where(
                StrategyVersion.strategy_definition_id == defn.id,
                StrategyVersion.status == "released",
            )
            .order_by(StrategyVersion.released_at.desc())
            .limit(1)
        )
        ver_result = await db.execute(ver_stmt)
        return ver_result.scalar_one_or_none()

    async def _resolve_watchlist_instruments(
        self, db: AsyncSession,
    ) -> tuple[list[uuid.UUID], dict[uuid.UUID, list[uuid.UUID]], dict[uuid.UUID, dict]]:
        """合并所有用户自选股去重，返回标的列表、用户映射及附加信息。

        过滤条件：
        1. 仅取 active=True 的自选记录（排除已软删除的）
        2. 排除指数类标的（symbol 以 '000' 开头且 market=SH，或以 '399' 开头且 market=SZ）

        Returns:
            (instrument_ids, instrument_user_map, instrument_extra_info) 三元组:
            - instrument_ids: 去重后的标的 ID 列表
            - instrument_user_map: {instrument_id: [user_id, ...], ...} 标的与用户映射（通知用）
            - instrument_extra_info: {instrument_id: {priority, weighted_score, ...}, ...} 附加信息
        """
        stmt = (
            select(
                UserWatchlistItem.instrument_id,
                UserWatchlistItem.user_id,
            )
            .where(UserWatchlistItem.active.is_(True))
        )
        result = await db.execute(stmt)
        rows = result.all()

        # 收集所有 instrument_id，批量查询排除指数
        instrument_ids_set = {row[0] for row in rows}
        index_ids: set[uuid.UUID] = set()
        if instrument_ids_set:
            inst_stmt = select(Instrument.id, Instrument.symbol, Instrument.market).where(
                Instrument.id.in_(instrument_ids_set),
            )
            inst_result = await db.execute(inst_stmt)
            for row in inst_result.all():
                # 指数类标的：SH市场000开头 / SZ市场399开头
                sym = row[1] or ""
                mkt = row[2] or ""
                if (mkt == "SH" and sym.startswith("000")) or (mkt == "SZ" and sym.startswith("399")):
                    index_ids.add(row[0])

        instrument_user_map: dict[uuid.UUID, list[uuid.UUID]] = {}
        for instrument_id, user_id in rows:
            if instrument_id in index_ids:
                continue
            if instrument_id not in instrument_user_map:
                instrument_user_map[instrument_id] = []
            instrument_user_map[instrument_id].append(user_id)

        # 去重后的标的 ID 列表
        instrument_ids = list(instrument_user_map.keys())

        # [monitor_batch] - 附加信息: 当前项目无 stock_pools / stop_loss_predictions 模型，
        # instrument_extra_info 暂为空字典。待数据源接入后在此处填充 priority、weighted_score、
        # hype_logic、total_market_cap、pred_sell_reg 等字段。
        instrument_extra_info: dict[uuid.UUID, dict] = {
            inst_id: {} for inst_id in instrument_ids
        }

        return instrument_ids, instrument_user_map, instrument_extra_info

    async def _process_instrument_evaluation(
        self,
        db: AsyncSession,
        instrument_id: uuid.UUID,
        strategy_version: StrategyVersion,
        result: MonitorCycleResult,
    ) -> list[StrategyEvent]:
        """处理单个标的的监控周期（基于评估表）。

        流程：
        1. 获取最新已完成 1m bar 的 source_bar_time
        2. INSERT 评估占位（ON CONFLICT DO NOTHING），冲突则跳过（exactly-once）
        3. 拉取行情 → 执行 WatchlistMonitor → 保存结果
        4. 更新 MonitorState + 写入 StrategyEvent
        5. 更新 MonitorEvaluation 为 SUCCEEDED（最后一步，确保 State/Event 写入成功后再标记）

        Args:
            db: 异步会话
            instrument_id: 标的 UUID
            strategy_version: watchlist_monitor 策略版本
            result: 累计结果

        Returns:
            本标的写入的 StrategyEvent 列表
        """
        # 查询标的 symbol 和 name
        symbol, inst_name = await self._get_instrument_info(db, instrument_id)
        if symbol is None:
            logger.warning("标的不存在: instrument_id=%s", instrument_id)
            return []

        # a. 获取最新已完成 1m bar
        now = datetime.now(UTC)
        today = now.date()
        try:
            bars_minute_result = await get_bars(
                db, instrument_id,
                timeframe="1m",
                start_date=today,
                end_date=today,
                adjustment="qfq",
                skip_upsert=True,
                completed_only=True,
            )
            bars_minute = bars_minute_result.bars
        except Exception as exc:
            logger.warning("1m行情拉取失败 %s: %s", symbol, exc)
            return []

        if bars_minute is None or bars_minute.empty:
            logger.debug("无已完成 1m bar: instrument_id=%s symbol=%s", instrument_id, symbol)
            return []

        # source_bar_time: 最新已完成 1m bar 的整分钟时间戳
        last_ts = bars_minute.index[-1]
        if hasattr(last_ts, "floor"):
            source_bar_time = last_ts.floor("1min").to_pydatetime()
        elif hasattr(last_ts, "to_pydatetime"):
            raw_dt = last_ts.to_pydatetime()
            source_bar_time = raw_dt.replace(second=0, microsecond=0)
        else:
            source_bar_time = now.replace(second=0, microsecond=0)

        # b. [eval_recovery] INSERT 评估占位（含租约/心跳），冲突时按状态判断是否可重入
        evaluation_id: uuid.UUID | None = None
        now_cst = datetime.now(_CST)
        try:
            # 先尝试 INSERT 新记录（含租约和心跳）
            insert_stmt = (
                pg_insert(MonitorEvaluation)
                .values(
                    strategy_version_id=strategy_version.id,
                    instrument_id=instrument_id,
                    source_bar_time=source_bar_time,
                    status="PENDING",
                    lease_expires_at=now_cst + timedelta(seconds=_LEASE_DURATION_SECONDS),
                    heartbeat_at=now_cst,
                    retry_count=0,
                )
                .on_conflict_do_nothing(
                    index_elements=["strategy_version_id", "instrument_id", "source_bar_time"],
                )
                .returning(MonitorEvaluation.id)
            )
            insert_result = await db.execute(insert_stmt)
            row = insert_result.scalar_one_or_none()

            if row is not None:
                # 新插入成功，获得租约
                evaluation_id = row
            else:
                # UNIQUE 冲突，查询已有记录判断是否可重入
                existing_stmt = select(MonitorEvaluation).where(
                    MonitorEvaluation.strategy_version_id == strategy_version.id,
                    MonitorEvaluation.instrument_id == instrument_id,
                    MonitorEvaluation.source_bar_time == source_bar_time,
                )
                existing_result = await db.execute(existing_stmt)
                existing = existing_result.scalar_one_or_none()

                if existing is None:
                    # 极端情况：INSERT 冲突但查不到，跳过
                    logger.debug(
                        "评估冲突但查不到记录，跳过: instrument_id=%s source_bar_time=%s",
                        instrument_id, source_bar_time,
                    )
                    return []

                if existing.status == "SUCCEEDED":
                    # 已成功完成，跳过
                    logger.debug(
                        "评估已成功（exactly-once 跳过）: instrument_id=%s source_bar_time=%s",
                        instrument_id, source_bar_time,
                    )
                    return []

                if existing.status == "DEAD":
                    # 已达最大重试次数，跳过
                    logger.debug(
                        "评估已死亡（DEAD 跳过）: instrument_id=%s source_bar_time=%s",
                        instrument_id, source_bar_time,
                    )
                    return []

                if existing.status == "PENDING" and existing.lease_expires_at is not None and existing.lease_expires_at < now_cst:
                    # PENDING + 租约过期：重新认领
                    existing.retry_count += 1
                    if existing.retry_count >= _MAX_RETRIES:
                        existing.status = "DEAD"
                        logger.warning(
                            "[eval_recovery] PENDING 评估租约过期且达最大重试次数，标记 DEAD: "
                            "evaluation_id=%s instrument_id=%s retry_count=%d",
                            existing.id, instrument_id, existing.retry_count,
                        )
                        return []
                    existing.lease_expires_at = now_cst + timedelta(seconds=_LEASE_DURATION_SECONDS)
                    existing.heartbeat_at = now_cst
                    evaluation_id = existing.id
                    logger.info(
                        "[eval_recovery] 重新认领过期 PENDING 评估: evaluation_id=%s "
                        "instrument_id=%s retry_count=%d",
                        evaluation_id, instrument_id, existing.retry_count,
                    )

                elif existing.status == "FAILED" and existing.retry_count < _MAX_RETRIES:
                    # FAILED + 未达最大重试：检查退避时间
                    next_retry = existing.next_retry_at
                    if next_retry is not None and next_retry <= now_cst:
                        existing.retry_count += 1
                        if existing.retry_count >= _MAX_RETRIES:
                            existing.status = "DEAD"
                            logger.warning(
                                "[eval_recovery] FAILED 评估达最大重试次数，标记 DEAD: "
                                "evaluation_id=%s instrument_id=%s retry_count=%d",
                                existing.id, instrument_id, existing.retry_count,
                            )
                            return []
                        existing.status = "PENDING"
                        existing.lease_expires_at = now_cst + timedelta(seconds=_LEASE_DURATION_SECONDS)
                        existing.heartbeat_at = now_cst
                        evaluation_id = existing.id
                        logger.info(
                            "[eval_recovery] 重试 FAILED 评估: evaluation_id=%s "
                            "instrument_id=%s retry_count=%d",
                            evaluation_id, instrument_id, existing.retry_count,
                        )
                    else:
                        # 还在退避期内，跳过
                        logger.debug(
                            "FAILED 评估仍在退避期: evaluation_id=%s next_retry_at=%s",
                            existing.id, next_retry,
                        )
                        return []

                elif existing.status == "PENDING" and existing.lease_expires_at is not None and existing.lease_expires_at >= now_cst:
                    # PENDING + 租约未过期：其他 worker 正在处理，跳过
                    logger.debug(
                        "PENDING 评估租约未过期（其他 worker 处理中）: evaluation_id=%s",
                        existing.id,
                    )
                    return []

                else:
                    # 其他状态组合（如 FAILED + retry_count >= MAX），跳过
                    logger.debug(
                        "评估状态不可重入: evaluation_id=%s status=%s retry_count=%d",
                        existing.id, existing.status, existing.retry_count,
                    )
                    return []

        except Exception as exc:
            logger.warning(
                "INSERT 评估占位失败 instrument_id=%s source_bar_time=%s: %s",
                instrument_id, source_bar_time, exc,
            )
            return []

        # c. 拉取行情
        bars_daily_result = await get_bars(
            db, instrument_id,
            timeframe="1d",
            start_date=today - timedelta(days=_DAILY_LOOKBACK_DAYS),
            end_date=today,
            adjustment="qfq",
        )
        bars_daily = bars_daily_result.bars

        bars_15min = pd.DataFrame()
        try:
            bars_15min_result = await get_bars(
                db, instrument_id,
                timeframe="15m",
                start_date=today - timedelta(days=_15MIN_LOOKBACK_DAYS),
                end_date=today,
                adjustment="qfq",
            )
            bars_15min = bars_15min_result.bars
        except Exception as exc:
            logger.warning("15min行情拉取失败 %s: %s", symbol, exc)

        # [MonitorBatchService] - 心跳: 行情数据拉取完成
        await self.update_heartbeat(db, evaluation_id)
        await db.flush()

        # d. 构建 MarketDataContext
        context = MarketDataContext(
            instrument_id=instrument_id,
            symbol=symbol,
            bars_daily=bars_daily,
            bars_15min=bars_15min if not bars_15min.empty else None,
            bars_minute=bars_minute if not bars_minute.empty else None,
            trade_date=today,
            bar_time=source_bar_time,
        )

        # e. 执行 WatchlistMonitor 算法
        try:
            runtime = WatchlistMonitor()
            await runtime.initialize(strategy_version)
        except Exception as exc:
            await self._mark_evaluation_failed(
                db, evaluation_id, f"WatchlistMonitor 初始化失败: {exc}",
            )
            logger.warning(
                "WatchlistMonitor 初始化失败 instrument_id=%s: %s", instrument_id, exc,
            )
            return []

        # calculate_state
        try:
            curr_state = await runtime.calculate_state(context)
        except Exception as exc:
            await self._mark_evaluation_failed(
                db, evaluation_id, f"calculate_state 失败: {exc}",
            )
            logger.warning(
                "calculate_state 失败 instrument_id=%s: %s", instrument_id, exc,
            )
            return []

        result.total_states_computed += 1

        # [MonitorBatchService] - 心跳: 指标计算完成
        await self.update_heartbeat(db, evaluation_id)
        await db.flush()

        # 获取 prev_state
        prev_state_orm = await monitor_state_repository.get_state(
            db, instrument_id=instrument_id, strategy_version_id=strategy_version.id,
        )
        prev_state = self._orm_to_runtime_state(prev_state_orm) if prev_state_orm else None

        # detect_events
        event_drafts: list[Any] = []
        try:
            event_drafts = await runtime.detect_events(context, prev_state, curr_state)
        except Exception as exc:
            logger.warning(
                "detect_events 失败 instrument_id=%s: %s", instrument_id, exc,
            )

        result.total_events_detected += len(event_drafts)

        # f. upsert MonitorState
        try:
            await monitor_state_repository.upsert_state(
                db,
                instrument_id=instrument_id,
                strategy_version_id=strategy_version.id,
                payload=curr_state.state,
                bar_time=curr_state.updated_at or now,
                calculation_id=curr_state.calculation_id or str(uuid.uuid4()),
                state_schema_version=curr_state.state_version,
            )
        except Exception as exc:
            logger.warning(
                "upsert monitor_state 失败 instrument_id=%s version_id=%s: %s",
                instrument_id, strategy_version.id, exc,
            )

        # g. 对每个检测到的事件：冷却检查 → 写入
        written_events: list[StrategyEvent] = []
        for draft in event_drafts:
            # 冷却检查
            in_cooldown = await self._check_event_cooldown(
                db, instrument_id, draft.event_type, draft.logical_entity,
            )
            if in_cooldown:
                logger.debug(
                    "事件冷却中，跳过: instrument_id=%s event_type=%s logical_entity=%s",
                    instrument_id, draft.event_type, draft.logical_entity,
                )
                continue

            # 写入事件
            try:
                event_orm = await strategy_event_repository.write_event(
                    db,
                    event_key=draft.dedupe_key,
                    strategy_version_id=strategy_version.id,
                    instrument_id=instrument_id,
                    event_type=draft.event_type,
                    event_time=draft.event_time,
                    payload=draft.payload,
                    logical_entity_id=draft.logical_entity,
                )
            except Exception as exc:
                logger.warning(
                    "写入 strategy_event 失败 instrument_id=%s event_type=%s: %s",
                    instrument_id, draft.event_type, exc,
                )
                continue

            if event_orm is None:
                # 幂等跳过（event_key 已存在）
                continue

            result.total_events_written += 1
            written_events.append(event_orm)

        # h. 保存结果：更新 MonitorEvaluation 为 SUCCEEDED（放在最后，确保 State/Event 写入成功后再标记）
        metrics_output: dict[str, Any] = {
            "state": curr_state.state,
            "events_detected": len(event_drafts),
        }
        try:
            # [eval_recovery] 成功时清除租约，设置最终心跳
            now_cst_final = datetime.now(_CST)
            await db.execute(
                pg_insert(MonitorEvaluation)
                .values(
                    id=evaluation_id,
                    strategy_version_id=strategy_version.id,
                    instrument_id=instrument_id,
                    source_bar_time=source_bar_time,
                    status="SUCCEEDED",
                    metrics=metrics_output,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "status": "SUCCEEDED",
                        "metrics": metrics_output,
                        "lease_expires_at": None,
                        "heartbeat_at": now_cst_final,
                    },
                )
            )
        except Exception as exc:
            logger.warning(
                "更新 MonitorEvaluation 为 SUCCEEDED 失败 evaluation_id=%s: %s",
                evaluation_id, exc,
            )

        return written_events

    async def _mark_evaluation_failed(
        self,
        db: AsyncSession,
        evaluation_id: uuid.UUID,
        error_message: str,
    ) -> None:
        """将 MonitorEvaluation 标记为 FAILED，含重试计数与指数退避。

        [eval_recovery] 失败处理逻辑：
        1. retry_count += 1
        2. 若 retry_count >= MAX_RETRIES，标记为 DEAD
        3. 否则计算指数退避时间（30 * 2^retry_count 秒），设置 next_retry_at

        Args:
            db: 异步会话
            evaluation_id: 评估记录 ID
            error_message: 错误信息
        """
        now_cst = datetime.now(_CST)

        # 查询当前评估记录
        stmt = select(MonitorEvaluation).where(MonitorEvaluation.id == evaluation_id)
        result = await db.execute(stmt)
        evaluation = result.scalar_one_or_none()
        if evaluation is None:
            logger.warning("标记 FAILED 时评估记录不存在: evaluation_id=%s", evaluation_id)
            return

        new_retry_count = evaluation.retry_count + 1
        if new_retry_count >= _MAX_RETRIES:
            # 达最大重试次数，标记 DEAD
            evaluation.status = "DEAD"
            evaluation.retry_count = new_retry_count
            evaluation.error_code = error_message[:500] if error_message else None
            logger.warning(
                "[eval_recovery] 评估达最大重试次数，标记 DEAD: evaluation_id=%s "
                "retry_count=%d error=%s",
                evaluation_id, new_retry_count, error_message[:200],
            )
        else:
            # 计算指数退避: 30s, 60s, 120s, 240s
            backoff_seconds = _RETRY_BACKOFF_BASE_SECONDS * (2 ** new_retry_count)
            evaluation.status = "FAILED"
            evaluation.retry_count = new_retry_count
            evaluation.error_code = error_message[:500] if error_message else None
            evaluation.next_retry_at = now_cst + timedelta(seconds=backoff_seconds)
            evaluation.lease_expires_at = None
            logger.info(
                "[eval_recovery] 评估标记 FAILED（退避 %ds）: evaluation_id=%s "
                "retry_count=%d next_retry_at=%s",
                backoff_seconds, evaluation_id, new_retry_count, evaluation.next_retry_at,
            )

    async def update_heartbeat(self, db: AsyncSession, evaluation_id: uuid.UUID) -> None:
        """更新评估记录的心跳和租约过期时间。

        [eval_recovery] 执行期间每 60 秒调用一次，防止其他 worker 误认领。

        Args:
            db: 异步会话
            evaluation_id: 评估记录 ID
        """
        now_cst = datetime.now(_CST)
        stmt = select(MonitorEvaluation).where(MonitorEvaluation.id == evaluation_id)
        result = await db.execute(stmt)
        evaluation = result.scalar_one_or_none()
        if evaluation is None:
            return
        evaluation.heartbeat_at = now_cst
        evaluation.lease_expires_at = now_cst + timedelta(seconds=_LEASE_DURATION_SECONDS)

    async def recover_stale_evaluations(self, db: AsyncSession) -> int:
        """Worker 启动时恢复过期租约的 PENDING 评估。

        [eval_recovery] 查找所有 PENDING 且租约已过期的评估记录：
        - retry_count += 1
        - 若 retry_count >= MAX_RETRIES，标记为 DEAD
        - 否则清除租约，设置 next_retry_at 为当前时间（立即可重试）

        Args:
            db: 异步会话

        Returns:
            恢复的评估记录数（不含标记为 DEAD 的）
        """
        now_cst = datetime.now(_CST)
        stmt = select(MonitorEvaluation).where(
            MonitorEvaluation.status == "PENDING",
            MonitorEvaluation.lease_expires_at < now_cst,
        )
        result = await db.execute(stmt)
        stale_evals = list(result.scalars().all())

        recovered = 0
        for eval_obj in stale_evals:
            eval_obj.retry_count += 1
            if eval_obj.retry_count >= _MAX_RETRIES:
                eval_obj.status = "DEAD"
                logger.warning(
                    "[eval_recovery] 启动恢复：PENDING 评估达最大重试次数，标记 DEAD: "
                    "evaluation_id=%s retry_count=%d",
                    eval_obj.id, eval_obj.retry_count,
                )
            else:
                eval_obj.lease_expires_at = None
                eval_obj.next_retry_at = now_cst
                eval_obj.heartbeat_at = None
                recovered += 1
                logger.info(
                    "[eval_recovery] 启动恢复：PENDING 评估租约过期，重置可重试: "
                    "evaluation_id=%s retry_count=%d",
                    eval_obj.id, eval_obj.retry_count,
                )

        if stale_evals:
            logger.info(
                "[eval_recovery] 启动恢复完成: stale=%d recovered=%d dead=%d",
                len(stale_evals), recovered, len(stale_evals) - recovered,
            )
        return recovered

    async def _check_event_cooldown(
        self,
        db: AsyncSession,
        instrument_id: uuid.UUID,
        event_type: str,
        logical_entity: str,
    ) -> bool:
        """检查事件是否在冷却期内。

        查询 strategy_events 表：同一 instrument_id + event_type + logical_entity
        在最近 _EVENT_COOLDOWN_SECONDS 秒内是否已有记录。

        Args:
            db: 异步会话
            instrument_id: 标的 ID
            event_type: 事件类型
            logical_entity: 逻辑实体标识

        Returns:
            True 表示在冷却期内（应跳过），False 表示不在冷却期
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=_EVENT_COOLDOWN_SECONDS)
        stmt = (
            select(func.count())
            .select_from(StrategyEvent)
            .where(
                StrategyEvent.instrument_id == instrument_id,
                StrategyEvent.event_type == event_type,
                StrategyEvent.logical_entity_id == logical_entity,
                StrategyEvent.event_time >= cutoff,
            )
        )
        count = await db.scalar(stmt)
        return (count or 0) > 0

    async def _get_instrument_info(
        self, db: AsyncSession, instrument_id: uuid.UUID,
    ) -> tuple[str | None, str | None]:
        """查询标的代码和名称。

        Args:
            db: 异步会话
            instrument_id: 标的 UUID

        Returns:
            (symbol, name) 元组，标的不存在时均为 None
        """
        stmt = select(Instrument.symbol, Instrument.name).where(
            Instrument.id == instrument_id,
        )
        row = await db.execute(stmt)
        result = row.first()
        if result is None:
            return None, None
        return result[0], result[1]

    async def _compute_change_pct(
        self,
        db: AsyncSession,
        instrument_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, float]:
        """计算涨跌幅映射（与旧版 monitoring.py compute_daily_change_pct 完全一致）。

        从 pytdx 拉日线最后2根bar：prev_close=倒数第2根收盘，cur_close=倒数第1根收盘。
        pytdx 盘中最后一根是当日bar（含实时价），所以涨跌幅是实时的。

        Args:
            db: 异步会话
            instrument_ids: 标的 ID 列表

        Returns:
            {instrument_id: change_pct} 映射
        """
        from app.core.pytdx_adapter import get_pytdx_adapter

        # 批量查询 symbol
        if not instrument_ids:
            return {}
        stmt = select(Instrument.id, Instrument.symbol).where(
            Instrument.id.in_(instrument_ids),
        )
        rows = await db.execute(stmt)
        id_symbol_map: dict[uuid.UUID, str] = {r[0]: r[1] for r in rows.all()}

        pytdx = get_pytdx_adapter()
        today = datetime.now(UTC).date()
        change_pct_map: dict[uuid.UUID, float] = {}
        for inst_id, symbol in id_symbol_map.items():
            try:
                df = await asyncio.to_thread(
                    pytdx.get_daily_bars, symbol,
                    today - timedelta(days=10), today,
                )
                if df is not None and len(df) >= 2:
                    prev_close = float(df.iloc[-2]["close"])
                    cur_close = float(df.iloc[-1]["close"])
                    if prev_close > 0:
                        change_pct_map[inst_id] = round(
                            (cur_close - prev_close) / prev_close * 100, 2,
                        )
            except Exception as exc:
                logger.debug("涨跌幅计算失败 symbol=%s: %s", symbol, exc)
        return change_pct_map

    def _build_merged_card_dto(
        self,
        user_events: list[StrategyEvent],
        total_instruments: int,
        instrument_info_cache: dict[uuid.UUID, tuple[str, str]],
        change_pct_map: dict[uuid.UUID, float] | None = None,
        instrument_extra_info: dict[uuid.UUID, dict] | None = None,
    ) -> Any:
        """按旧版 monitoring.py 的 generate_monitoring_card() 格式构建合并通知 DTO。

        卡片结构：
        1. Header: "BB+节点监控 HH:MM"（北京时间），颜色由最严重事件级别决定
        2. 概览行: "自选股 N 只 | 触发 M 只\\n上轨 X | 中轨 Y | 下轨 Z | 节点 W"
        3. 逐股票详情（用 hr 分隔）: 股票标题 + hype_logic + 止损预测 + 信号详情 + BB上下文 + BB快照
        4. 数据时间 note: 事件触发时间（北京时间）

        Args:
            user_events: 该用户相关的事件列表
            total_instruments: 该用户自选股总数
            instrument_info_cache: instrument_id → (symbol, name) 缓存
            change_pct_map: instrument_id → 涨跌幅映射
            instrument_extra_info: instrument_id → {priority, weighted_score, hype_logic,
                total_market_cap, pred_sell_reg, pred_sell_cls, pred_buy_reg, pred_buy_cls} 附加信息

        Returns:
            NotificationMessageDTO 实例
        """
        from app.schemas.notification import NotificationMessageDTO

        # 按标的分组
        instrument_events: dict[uuid.UUID, list[StrategyEvent]] = {}
        for ev in user_events:
            instrument_events.setdefault(ev.instrument_id, []).append(ev)

        triggered_count = len(instrument_events)

        # 全局最严重级别决定 header 颜色
        max_sev = "info"
        for ev in user_events:
            sev = _EVENT_SEVERITY.get(ev.event_type, "info")
            if _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(max_sev, 0):
                max_sev = sev

        # 概览统计
        trigger_counts: dict[str, int] = {
            "bb_upper_touch": 0, "bb_mid_touch": 0,
            "bb_lower_touch": 0, "node_cluster_touch": 0,
        }
        for ev in user_events:
            if ev.event_type in trigger_counts:
                trigger_counts[ev.event_type] += 1

        # 卡片标题时间：最早事件的触发时间（北京时间），不使用当前时间
        earliest_event = min(user_events, key=lambda e: e.event_time)
        header_time_cst = earliest_event.event_time.astimezone(_CST)
        header_time = header_time_cst.strftime("%H:%M")

        elements: list[dict[str, Any]] = []

        # 概览行
        overview = (
            f"自选股 {total_instruments} 只 | 触发 {triggered_count} 只\n"
            f"上轨 {trigger_counts['bb_upper_touch']} | "
            f"中轨 {trigger_counts['bb_mid_touch']} | "
            f"下轨 {trigger_counts['bb_lower_touch']} | "
            f"节点 {trigger_counts['node_cluster_touch']}"
        )
        elements.append({"tag": "markdown", "content": overview})

        # 逐股票详情
        for idx, (inst_id, events) in enumerate(instrument_events.items()):
            info = instrument_info_cache.get(inst_id)
            symbol = info[0] if info else str(inst_id)[:8]
            name = info[1] if info else symbol

            # 分隔线
            if idx > 0:
                elements.append({"tag": "hr"})

            # 股票标题（与参考脚本 generate_monitoring_card 格式对齐）
            extra_info = (instrument_extra_info or {}).get(inst_id, {})
            priority = extra_info.get("priority", "")
            score = extra_info.get("weighted_score", 0)
            market_cap = extra_info.get("total_market_cap")

            title_parts = [f"**{name} {symbol}**"]
            if priority:
                title_parts.append(f"  {priority}")
            if score:
                title_parts.append(f"  {score}分")
            change_pct = (change_pct_map or {}).get(inst_id)
            if change_pct is not None:
                change_str = f"+{change_pct:.2f}" if change_pct > 0 else f"{change_pct:.2f}"
                title_parts.append(f"\n涨跌 {change_str}%")
            if market_cap:
                title_parts.append(f"  市值 {market_cap:.0f}亿")
            title_md = "".join(title_parts)
            elements.append({"tag": "markdown", "content": title_md})

            # hype_logic 显示（与参考脚本对齐）
            hype_logic = extra_info.get("hype_logic", "")
            if hype_logic:
                elements.append({"tag": "markdown", "content": f"💡 {hype_logic}"})

            # 止损预测（与参考脚本对齐）
            pred_sell_reg = extra_info.get("pred_sell_reg")
            pred_sell_cls = extra_info.get("pred_sell_cls")
            pred_buy_reg = extra_info.get("pred_buy_reg")
            pred_buy_cls = extra_info.get("pred_buy_cls")
            if any(v is not None for v in [pred_sell_reg, pred_sell_cls, pred_buy_reg, pred_buy_cls]):
                pred_lines = ["止损预测:"]
                if pred_sell_reg is not None:
                    pred_lines.append(f"  卖出(回归): {pred_sell_reg:.3f}")
                if pred_sell_cls is not None:
                    pred_lines.append(f"  卖出(分类): {pred_sell_cls:.3f}")
                if pred_buy_reg is not None:
                    pred_lines.append(f"  买入(回归): {pred_buy_reg:.3f}")
                if pred_buy_cls is not None:
                    pred_lines.append(f"  买入(分类): {pred_buy_cls:.3f}")
                elements.append({"tag": "markdown", "content": "\n".join(pred_lines)})

            # 信号详情
            for ev in events:
                emoji = _EVENT_EMOJI.get(ev.event_type, "📌")
                event_label = _EVENT_TYPE_LABEL.get(ev.event_type, ev.event_type)
                payload = ev.payload or {}
                current_price = payload.get("price") or payload.get("current_price")
                boundary = payload.get("boundary")
                dev_pct = payload.get("dev_pct")

                sig_lines = [f"{emoji} {event_label}"]
                # 触发时间（北京时间）
                if ev.event_time is not None:
                    ev_time_cst = ev.event_time.astimezone(_CST)
                    sig_lines.append(f"  触发时间: {ev_time_cst.strftime('%Y-%m-%d %H:%M')}")
                if current_price is not None:
                    sig_lines.append(f"  现价: {current_price:.2f}")

                if boundary is not None:
                    boundary_label = {
                        "bb_upper_touch": "上轨",
                        "bb_mid_touch": "中轨",
                        "bb_lower_touch": "下轨",
                        "node_cluster_touch": "节点",
                    }.get(ev.event_type, "边界")
                    dev_str = f"{dev_pct:+.2f}%" if dev_pct is not None else "-"
                    sig_lines.append(
                        f"  {boundary_label}: {boundary:.2f}  偏离: {dev_str}"
                    )

                # BB上下文（仅BB事件）
                if ev.event_type in ("bb_upper_touch", "bb_mid_touch", "bb_lower_touch"):
                    bb_upper = payload.get("bb_upper")
                    bb_mid = payload.get("bb_mid")
                    bb_lower = payload.get("bb_lower")
                    if bb_upper is not None:
                        sig_lines.append(f"  上轨: {bb_upper:.2f}")
                    if bb_mid is not None:
                        sig_lines.append(f"  中轨: {bb_mid:.2f}")
                    if bb_lower is not None:
                        sig_lines.append(f"  下轨: {bb_lower:.2f}")

                elements.append({"tag": "markdown", "content": "\n".join(sig_lines)})

            # BB快照
            snapshot = events[0].snapshot if events else {}
            bb_snap = snapshot.get("bb_snapshot") if snapshot else None
            if bb_snap:
                snap_upper = bb_snap.get("bb_upper")
                snap_mid = bb_snap.get("bb_mid")
                snap_lower = bb_snap.get("bb_lower")
                snap_lines = [f"  BB: 上{snap_upper:.2f} 中{snap_mid:.2f} 下{snap_lower:.2f}"]
                bb_width = bb_snap.get("bb_width")
                if bb_width is not None:
                    snap_lines.append(
                        f"  宽度: {bb_width:.4f}  位置: {bb_snap.get('bb_pos', '-')}"
                    )
                elements.append({"tag": "markdown", "content": "\n".join(snap_lines)})

        # 数据时间 note: 最早事件的 event_time（北京时间）
        data_time_cst = earliest_event.event_time.astimezone(_CST)
        data_time_str = data_time_cst.strftime("%Y-%m-%d %H:%M")
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"数据时间: {data_time_str}"}],
        })

        # 构建 DTO
        dto = NotificationMessageDTO(
            message_type="MONITOR_MEMBER_EVENT",
            template_key="monitor_merged_event",
            template_version="2.0.0",
            title=f"BB+节点监控 {header_time}",
            summary=(
                f"自选股 {total_instruments} 只 | 触发 {triggered_count} 只 | "
                f"上轨 {trigger_counts['bb_upper_touch']} | "
                f"中轨 {trigger_counts['bb_mid_touch']} | "
                f"下轨 {trigger_counts['bb_lower_touch']} | "
                f"节点 {trigger_counts['node_cluster_touch']}"
            ),
            facts=[],
            timeline=[],
            items=elements,
            resource_refs={
                "event_ids": [str(ev.id) for ev in user_events],
                "event_types": list({ev.event_type for ev in user_events}),
                "header_severity": max_sev,
            },
            data_time=data_time_cst.strftime("%Y-%m-%d %H:%M"),
        )
        return dto

    async def _send_merged_notification(
        self,
        db: AsyncSession,
        all_events: list[StrategyEvent],
        instrument_user_map: dict[uuid.UUID, list[uuid.UUID]],
        instrument_extra_info: dict[uuid.UUID, dict],
        result: MonitorCycleResult,
    ) -> None:
        """按用户自选股归属合并通知，每个用户一张飞书卡片。

        通知通过 Outbox 管道投递：
        create_message → write_outbox(notification.message.created) → Delivery Worker → deliver_message

        Args:
            db: 异步会话
            all_events: 本周期所有写入的事件
            instrument_user_map: instrument_id → [user_ids] 映射
            instrument_extra_info: instrument_id → {priority, weighted_score, ...} 附加信息
            result: 累计结果
        """
        from app.services.notification_service import create_message
        from app.services.outbox_relay import write_outbox

        # 构建 instrument_id → events 映射
        instrument_events: dict[uuid.UUID, list[StrategyEvent]] = {}
        for ev in all_events:
            instrument_events.setdefault(ev.instrument_id, []).append(ev)

        # 批量查询所有涉及的 instrument 信息，避免逐条查询
        involved_ids = list(instrument_events.keys())
        instrument_info_cache: dict[uuid.UUID, tuple[str, str]] = {}
        if involved_ids:
            stmt = select(Instrument.id, Instrument.symbol, Instrument.name).where(
                Instrument.id.in_(involved_ids),
            )
            rows = await db.execute(stmt)
            for row in rows.all():
                instrument_info_cache[row[0]] = (row[1], row[2])

        # 计算涨跌幅映射（当日收盘 vs 前日收盘，盘中用最新价）
        change_pct_map = await self._compute_change_pct(db, involved_ids)

        # 构建 user_id → 相关事件列表 + 自选股总数
        user_events_map: dict[uuid.UUID, list[StrategyEvent]] = {}
        user_instrument_count: dict[uuid.UUID, int] = {}
        for inst_id, user_ids in instrument_user_map.items():
            events = instrument_events.get(inst_id, [])
            for uid in user_ids:
                user_instrument_count[uid] = user_instrument_count.get(uid, 0) + 1
                if events:
                    user_events_map.setdefault(uid, []).extend(events)

        # 对每个用户创建通知消息并写入 Outbox（由 Delivery Worker 异步投递）
        for user_id, user_events in user_events_map.items():
            total_inst = user_instrument_count.get(user_id, 0)
            try:
                dto = self._build_merged_card_dto(
                    user_events, total_inst, instrument_info_cache,
                    change_pct_map=change_pct_map,
                    instrument_extra_info=instrument_extra_info,
                )
            except Exception as exc:
                logger.warning(
                    "构建合并卡片失败 user_id=%s: %s", user_id, exc,
                )
                continue

            # 创建消息
            try:
                message = await create_message(
                    db=db,
                    user_id=user_id,
                    message_dto=dto,
                    source_type="monitor_event",
                    source_id=user_events[0].id,
                )
            except Exception as exc:
                logger.warning(
                    "创建合并通知消息失败 user_id=%s: %s", user_id, exc,
                )
                continue

            # 写入 Outbox：Delivery Worker 将异步投递到用户活跃渠道
            try:
                await write_outbox(
                    db=db,
                    event_type="notification.message.created",
                    payload={
                        "message_id": str(message.id),
                        "user_id": str(user_id),
                    },
                    aggregate_type="notification_message",
                    aggregate_id=message.id,
                )
                result.total_notifications_created += 1
            except Exception as exc:
                logger.warning(
                    "写入通知 Outbox 失败 user_id=%s message_id=%s: %s",
                    user_id, message.id, exc,
                )

        # 卡片投递完成后，为每只触发股票渲染 PNG 并发送图片
        # TODO: [monitor_batch] _send_chart_images 直接调用 adapter.send_image()，
        # 绕过 Outbox 管道。待 Outbox 管道支持图片投递后，应改为写入 Outbox 由
        # Delivery Worker 统一处理，与卡片通知保持一致的投递语义。
        await self._send_chart_images(
            db, instrument_events, instrument_info_cache, instrument_user_map,
        )

    async def _send_chart_images(
        self,
        db: AsyncSession,
        instrument_events: dict[uuid.UUID, list[StrategyEvent]],
        instrument_info_cache: dict[uuid.UUID, tuple[str, str]],
        instrument_user_map: dict[uuid.UUID, list[uuid.UUID]],
    ) -> None:
        """为每只触发股票渲染 PNG 行情图并通过飞书发送。

        图片渲染失败不阻塞通知流程，临时 PNG 文件发送后清理。

        Args:
            db: 异步会话
            instrument_events: instrument_id → events 映射
            instrument_info_cache: instrument_id → (symbol, name) 缓存
            instrument_user_map: instrument_id → [user_ids] 映射
        """
        from app.models.notification import NotificationChannel
        from app.services.channel_adapter import get_adapter

        for inst_id, _events in instrument_events.items():
            info = instrument_info_cache.get(inst_id)
            if not info:
                continue
            symbol, stock_name = info

            png_path: str | None = None
            try:
                png_path = await self._render_instrument_chart(db, inst_id, symbol, stock_name)
                if png_path is None:
                    continue

                # 获取持有该标的的用户列表
                user_ids = instrument_user_map.get(inst_id, [])
                for uid in user_ids:
                    # 查询用户的飞书平台应用渠道
                    ch_stmt = select(NotificationChannel).where(
                        NotificationChannel.user_id == uid,
                        NotificationChannel.status == "active",
                        NotificationChannel.adapter_type == "feishu_platform_app",
                    )
                    ch_result = await db.execute(ch_stmt)
                    channels = list(ch_result.scalars().all())

                    for channel in channels:
                        try:
                            adapter = get_adapter("feishu_platform_app")
                            await adapter.send_image(png_path, channel.target_config)
                            logger.info(
                                "PNG 行情图推送成功: symbol=%s user_id=%s channel=%s",
                                symbol, uid, channel.adapter_type,
                            )
                        except Exception as exc:
                            logger.warning(
                                "PNG 行情图推送失败: symbol=%s user_id=%s: %s",
                                symbol, uid, exc,
                            )
            except Exception as exc:
                logger.warning("PNG 渲染/推送失败: %s: %s", symbol, exc)
            finally:
                # 清理临时文件
                if png_path and os.path.isfile(png_path):
                    try:
                        os.unlink(png_path)
                    except OSError:
                        pass

    async def _render_instrument_chart(
        self,
        db: AsyncSession,
        instrument_id: uuid.UUID,
        symbol: str,
        stock_name: str,
    ) -> str | None:
        """渲染单只股票的行情 PNG 图。

        重新获取行情数据并计算布林带和筹码分布，调用 render_monitoring_chart 渲染。
        仅在有事件时才执行，频率很低，因此重新获取数据是可接受的。

        Args:
            db: 异步会话
            instrument_id: 标的 UUID
            symbol: 股票代码
            stock_name: 股票名称

        Returns:
            PNG 文件路径，失败返回 None
        """
        from app.services.monitor_chart_renderer import (
            _load_bollinger_module,
            render_monitoring_chart,
        )

        # 获取日线行情
        now = datetime.now(UTC)
        today = now.date()
        bars_daily_result = await get_bars(
            db, instrument_id,
            timeframe="1d",
            start_date=today - timedelta(days=_DAILY_LOOKBACK_DAYS),
            end_date=today,
            adjustment="qfq",
        )
        bars_daily = bars_daily_result.bars
        if bars_daily.empty or len(bars_daily) < 20:
            logger.debug("日线行情不足，跳过 PNG 渲染: symbol=%s bars=%d", symbol, len(bars_daily))
            return None

        # 计算布林带
        try:
            bb_module = _load_bollinger_module()
        except (FileNotFoundError, ImportError) as exc:
            logger.warning("bollinger features 模块不可用，跳过 PNG 渲染: %s", exc)
            return None

        try:
            bb_result = bb_module.bollinger(bars_daily, win=20, k=2.0)
            # bollinger() 返回 tuple (bb_mid, bb_upper, bb_lower)
            if isinstance(bb_result, tuple):
                bb_mid, bb_upper, bb_lower = bb_result
            else:
                bb_mid = bb_result["bb_mid"]
                bb_upper = bb_result["bb_upper"]
                bb_lower = bb_result["bb_lower"]
        except Exception as exc:
            logger.warning("布林带计算失败 symbol=%s: %s", symbol, exc)
            return None

        # 计算筹码分布（可选，失败时 profile=None）
        profile = None
        try:
            profile = await self._compute_volume_profile(bars_daily, instrument_id, db)
        except Exception as exc:
            logger.debug("筹码分布计算失败 symbol=%s（不影响 PNG 渲染）: %s", symbol, exc)

        # 渲染 PNG
        return await render_monitoring_chart(
            df=bars_daily,
            bb_mid=bb_mid,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            profile=profile,
            symbol=symbol,
            stock_name=stock_name,
        )

    async def _compute_volume_profile(
        self,
        bars_daily: pd.DataFrame,
        instrument_id: uuid.UUID,
        db: AsyncSession,
    ) -> Any:
        """计算筹码分布（Volume Profile）。

        Args:
            bars_daily: 日线行情
            instrument_id: 标的 UUID
            db: 异步会话

        Returns:
            VolumeProfileResult 对象，失败返回 None
        """
        from app.strategy._plotly_mock import ensure_plotly_mock
        from app.strategy_assets.algorithms.features.luxalgo_volume_profile_pytdx_15m_aligned import (
            VolumeProfileConfig,
            compute_volume_profile as vp_compute,
        )

        ensure_plotly_mock()

        # 获取 15min 行情
        now = datetime.now(UTC)
        today = now.date()
        try:
            bars_15min_result = await get_bars(
                db, instrument_id,
                timeframe="15m",
                start_date=today - timedelta(days=_15MIN_LOOKBACK_DAYS),
                end_date=today,
            )
            bars_15min = bars_15min_result.bars
        except Exception:
            return None

        if bars_15min.empty:
            return None

        cfg = VolumeProfileConfig(
            peaks_show="peaks",
            profile_lookback_length=360,
            profile_number_of_rows=100,
            value_area_threshold=0.70,
            peaks_detection_percent=0.05,
            troughs_show="none",
            troughs_detection_percent=0.07,
            volume_node_threshold=0.01,
            highest_n_volume_nodes=0,
            lowest_n_volume_nodes=0,
        )

        # 日线数据需要 datetime 列供 compute_volume_profile 对齐时间
        # DB 日线的日期在 index（trade_date）中，需转为 datetime 列
        if "datetime" not in bars_daily.columns:
            bars_daily = bars_daily.copy()
            bars_daily["datetime"] = pd.to_datetime(bars_daily.index)

        return vp_compute(
            df=bars_daily,
            cfg=cfg,
            profile_df=bars_15min,
            main_period="day",
        )

    @staticmethod
    def _orm_to_runtime_state(orm: MonitorStateORM) -> MonitorState:
        """将 MonitorState ORM 转换为 runtime.MonitorState。

        Args:
            orm: MonitorState ORM 对象

        Returns:
            runtime.MonitorState 数据类实例
        """
        return MonitorState(
            instrument_id=orm.instrument_id,
            strategy_version_id=orm.strategy_version_id,
            state=orm.payload,
            state_version=orm.state_schema_version,
            updated_at=orm.bar_time,
            calculation_id=orm.calculation_id,
        )


if __name__ == "__main__":
    # 自测入口：验证 MonitorBatchService 可实例化、MonitorCycleResult 可构造（无副作用）
    # 1. 验证 MonitorCycleResult
    r = MonitorCycleResult()
    assert r.total_instruments == 0
    assert r.errors == []
    r2 = MonitorCycleResult(
        total_instruments=5,
        total_states_computed=10,
        total_events_detected=3,
        total_events_written=2,
        total_notifications_created=1,
        errors=["err1"],
    )
    assert r2.total_instruments == 5
    assert r2.total_events_written == 2
    assert len(r2.errors) == 1
    print(f"MonitorCycleResult: {r2} ✓")

    # 2. 验证 MonitorBatchService 可实例化
    service = MonitorBatchService()
    assert hasattr(service, "execute_monitor_cycle")
    assert callable(service.execute_monitor_cycle)
    assert hasattr(service, "_get_watchlist_monitor_version")
    assert hasattr(service, "_process_instrument_evaluation")
    assert hasattr(service, "_mark_evaluation_failed")
    assert hasattr(service, "update_heartbeat")
    assert hasattr(service, "recover_stale_evaluations")
    print(f"MonitorBatchService: {service} ✓")

    # 2b. 验证 [eval_recovery] 常量
    assert _LEASE_DURATION_SECONDS == 300
    assert _MAX_RETRIES == 5
    assert _RETRY_BACKOFF_BASE_SECONDS == 30
    # 验证指数退避序列: 30*2^1=60, 30*2^2=120, 30*2^3=240, 30*2^4=480
    assert _RETRY_BACKOFF_BASE_SECONDS * (2 ** 1) == 60
    assert _RETRY_BACKOFF_BASE_SECONDS * (2 ** 2) == 120
    assert _RETRY_BACKOFF_BASE_SECONDS * (2 ** 3) == 240
    print("[eval_recovery] 常量与退避序列 ✓")

    # 3. 验证 _orm_to_runtime_state 方法
    class _FakeORM:
        instrument_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        strategy_version_id = uuid.UUID("87654321-4321-8765-4321-876543218765")
        payload = {"current_price": 100.0, "direction": "up"}
        state_schema_version = 1
        bar_time = datetime(2026, 6, 23, 10, 30, 0, tzinfo=UTC)
        calculation_id = "calc-001"

    fake_orm = _FakeORM()
    runtime_state = MonitorBatchService._orm_to_runtime_state(fake_orm)
    assert runtime_state.instrument_id == fake_orm.instrument_id
    assert runtime_state.state == {"current_price": 100.0, "direction": "up"}
    assert runtime_state.state_version == 1
    print(f"_orm_to_runtime_state: {runtime_state} ✓")

    # 4. 验证 _build_merged_card_dto 方法（无 instrument_extra_info，向后兼容）
    class _FakeEvent:
        def __init__(self, event_type, instrument_id, payload, event_time, snapshot=None):
            self.id = uuid.uuid4()
            self.event_type = event_type
            self.instrument_id = instrument_id
            self.payload = payload
            self.event_time = event_time
            self.snapshot = snapshot or {}

    inst_id_1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
    inst_id_2 = uuid.UUID("22222222-2222-2222-2222-222222222222")
    ev_time = datetime(2026, 6, 23, 10, 30, 0, tzinfo=UTC)

    fake_events = [
        _FakeEvent(
            "bb_upper_touch", inst_id_1,
            {"price": 25.50, "boundary": 24.80, "dev_pct": 2.82,
             "bb_upper": 24.80, "bb_mid": 22.00, "bb_lower": 19.20},
            ev_time,
            {"bb_snapshot": {"bb_upper": 24.80, "bb_mid": 22.00, "bb_lower": 19.20,
                             "bb_width": 0.2245, "bb_pos": 0.85}},
        ),
        _FakeEvent(
            "node_cluster_touch", inst_id_2,
            {"price": 15.30, "boundary": 15.00, "dev_pct": 2.00},
            ev_time,
        ),
    ]
    info_cache = {
        inst_id_1: ("000001", "平安银行"),
        inst_id_2: ("600519", "贵州茅台"),
    }
    # 无 instrument_extra_info 时向后兼容
    dto = service._build_merged_card_dto(fake_events, 5, info_cache)
    assert dto.message_type == "MONITOR_MEMBER_EVENT"
    assert dto.template_key == "monitor_merged_event"
    assert "BB+节点监控" in dto.title
    assert "自选股 5 只" in dto.summary
    assert "触发 2 只" in dto.summary
    assert len(dto.items) > 0
    # 验证概览行
    assert dto.items[0]["tag"] == "markdown"
    assert "自选股 5 只" in dto.items[0]["content"]
    # 验证 data_time 使用 event_time（北京时间）
    assert "2026-06-23" in dto.data_time
    print(f"_build_merged_card_dto (无extra_info): title={dto.title} items_count={len(dto.items)} ✓")

    # 4b. 验证 _build_merged_card_dto 方法（含 instrument_extra_info，含 priority/score/market_cap/hype_logic/止损预测）
    extra_info_with_data = {
        inst_id_1: {
            "priority": "S",
            "weighted_score": 85.5,
            "total_market_cap": 1200.0,
            "hype_logic": "AI芯片龙头，业绩超预期",
            "pred_sell_reg": 0.876,
            "pred_sell_cls": 0.912,
            "pred_buy_reg": 0.234,
            "pred_buy_cls": 0.156,
        },
        inst_id_2: {
            "priority": "A",
            "weighted_score": 72.0,
            "total_market_cap": None,
            "hype_logic": "",
            "pred_sell_reg": None,
            "pred_sell_cls": None,
            "pred_buy_reg": None,
            "pred_buy_cls": None,
        },
    }
    dto2 = service._build_merged_card_dto(
        fake_events, 5, info_cache,
        change_pct_map={inst_id_1: 2.35, inst_id_2: -1.08},
        instrument_extra_info=extra_info_with_data,
    )
    assert dto2.message_type == "MONITOR_MEMBER_EVENT"
    # 验证标题含 priority 和 score
    title_item = dto2.items[1]  # 概览后第一个股票标题
    assert "S" in title_item["content"], f"expected 'S' in title, got: {title_item['content']}"
    assert "85.5分" in title_item["content"], f"expected '85.5分' in title, got: {title_item['content']}"
    assert "市值 1200亿" in title_item["content"], f"expected '市值 1200亿' in title, got: {title_item['content']}"
    # 验证 hype_logic 显示
    hype_item = dto2.items[2]
    assert "💡" in hype_item["content"], f"expected '💡' in hype_logic, got: {hype_item['content']}"
    # 验证止损预测显示
    pred_item = dto2.items[3]
    assert "止损预测" in pred_item["content"], f"expected '止损预测' in pred, got: {pred_item['content']}"
    assert "卖出(回归): 0.876" in pred_item["content"]
    print(f"_build_merged_card_dto (含extra_info): title={dto2.title} items_count={len(dto2.items)} ✓")

    # 5. 验证常量映射
    assert _EVENT_EMOJI["bb_upper_touch"] == "🔴"
    assert _EVENT_TYPE_LABEL["bb_mid_touch"] == "布林中轨穿越"
    assert _EVENT_SEVERITY["bb_lower_touch"] == "info"
    assert _SEVERITY_TEMPLATE["danger"] == "red"
    assert _SEVERITY_ORDER["warn"] == 2
    print("常量映射 ✓")

    print("OK")
