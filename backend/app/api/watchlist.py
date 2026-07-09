"""用户自选股 API 路由（W1）。

端点：
- GET /watchlist: 当前用户自选列表（user_id 由认证上下文注入）
- POST /watchlist: 加入自选（使用 AccessContext 统一权限模型校验订阅与额度）
- DELETE /watchlist/{instrument_id}: 移除自选（软删除：active=false + removed_at）
- GET /watchlist/monitor-status: 自选股+监控状态聚合查询

设计说明：
- user_id 由认证上下文注入，不接受请求体传入（V1.1 安全约束）
- POST /watchlist 使用 AccessContext 统一权限模型：
  - require_active_subscription: 需有效订阅（admin 豁免），过期/无订阅返回 403
  - require_quota("monitor_limit"): 返回额度值（admin=None 无限制；member=int 限额）
  - 不再自行查询 Subscription 表或判断 admin 角色（单一事实源原则）
- 加入自选即参与当前启用的监控方案（universe_service 聚合 active=true 记录）
- 移除采用软删除（active=false + removed_at），保留历史，支持重新加入
- (user_id, instrument_id) 唯一约束：重复加入返回 409 Conflict
- 重新加入已软删除的记录：恢复 active=true 并清空 removed_at
- monitor-status 端点 metrics 数据源：
  - 来自 StockFeatureSnapshot.summary_payload（盘后 orchestrator 生成）
  - 不再走 MonitorState.payload 或 MonitorSnapshotService 实时 fallback
  - MonitorEvaluation 仅用于展示评估状态（evaluation_status/retry_count/error_code）

套餐权限（plans 表，通过 AccessContext 统一读取）：
- POST /watchlist 及恢复软删除前校验 active count < monitor_limit
- 超限返回 409 {"detail": "监控数量已达上限 N"}
- admin（monitor_limit=None）绕过监控数量限制
- 过期订阅或无订阅返回 403（由 require_active_subscription 校验）
- 降级后已有数量超过额度不删除，只禁止新增
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import NamedTuple
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import WATCHLIST_MONITOR
from app.core.route_utils import get_route_paths
from app.core.time import now_shanghai, shanghai_business_date
from app.db import get_db
from app.models.instrument import Instrument
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_event import StrategyEvent
from app.models.watchlist import UserWatchlistItem
from app.schemas.watchlist import (
    WatchlistAddRequest,
    WatchlistItemResponse,
    WatchlistListResponse,
    WatchlistMonitorStatusItem,
    WatchlistMonitorStatusResponse,
)
from app.services.access_control_service import (
    AccessContext,
    require_active_subscription,
    require_quota,
)
from app.services.calendar_service import (
    get_most_recent_trading_day_async,
    get_previous_trading_day_async,
    is_trading_day_async,
)
from app.services.feature_snapshot_service import has_succeeded_snapshot_run
from app.services.market_status_service import compute_market_session

logger = logging.getLogger("watchlist_api")

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


class _EvalInfo(NamedTuple):
    """MonitorEvaluation 查询行的评估字段（仅用于展示）。"""

    evaluation_status: str
    retry_count: int
    error_code: str | None
    source_bar_time: datetime


async def _check_limit_if_needed(
    db: AsyncSession, user_id: UUID, monitor_limit: int | None
) -> None:
    """校验用户监控数量额度，超限抛 409（admin 跳过）。

    使用 AccessContext 统一权限模型：
    - monitor_limit is None（admin）：跳过额度检查
    - monitor_limit is not None（member）：查询 active count，超限返回 409

    订阅有效性由 require_active_subscription 依赖在路由层校验，本函数只负责额度比较。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        monitor_limit: 额度值（admin=None 无限制；member=int 限额）

    Raises:
        HTTPException 409: 监控数量已达上限
    """
    if monitor_limit is None:
        return  # admin 无限制

    count_stmt = (
        select(func.count(UserWatchlistItem.id))
        .where(
            UserWatchlistItem.user_id == user_id,
            UserWatchlistItem.active.is_(True),
        )
    )
    count_result = await db.execute(count_stmt)
    active_count = count_result.scalar_one()

    if active_count >= monitor_limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"监控数量已达上限 {monitor_limit}",
        )


def _compute_market_status(now_cst: datetime, is_trading_day: bool) -> str:
    """根据当前时间计算市场状态。

    薄包装：委托给 app.services.market_status_service.compute_market_session，
    统一 6 值枚举（NON_TRADING_DAY/PRE_OPEN/MORNING_SESSION/LUNCH_BREAK/AFTERNOON_SESSION/MARKET_CLOSED）。
    """
    return compute_market_session(now_cst, is_trading_day)


async def _resolve_expected_snapshot_trade_date(
    db: AsyncSession,
    today: date,
    market_session: str,
    is_trading_day: bool,
) -> date | None:
    """[Blocker1] - 解析当日应读取的 feature snapshot 交易日。

    规则（与 after_close_orchestrator 生成快照的时点对齐）：
    1. 交易日且未收盘（PRE_OPEN/MORNING_SESSION/LUNCH_BREAK/AFTERNOON_SESSION）：
       返回上一个已完成交易日（盘中读昨日 snapshot）。
    2. 交易日且已收盘（MARKET_CLOSED）：返回 today（orchestrator 应已生成当日快照）。
    3. 非交易日（NON_TRADING_DAY）：返回最近一个交易日（读最近交易日 snapshot）。
    4. 若无法解析最近交易日（trading_calendar 表无记录），返回 None。

    复用 calendar_service.get_previous_trading_day_async /
    get_most_recent_trading_day_async，禁止硬编码周末。

    Args:
        db: 异步数据库会话
        today: 上海业务日期
        market_session: 6 值枚举的市场状态
        is_trading_day: 是否为交易日

    Returns:
        预期的快照交易日；None 表示无法解析（前端展示 NO_SNAPSHOT）。
    """
    if is_trading_day:
        if market_session == "MARKET_CLOSED":
            return today
        # 盘中：读取上一个已完成交易日 snapshot
        return await get_previous_trading_day_async(db, today)
    # 非交易日：读取最近一个交易日 snapshot
    return await get_most_recent_trading_day_async(db, today)


@router.get("", response_model=WatchlistListResponse)
async def list_watchlist(
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
) -> WatchlistListResponse:
    """查询当前用户的自选列表（仅 active=true）。

    user_id 由权限上下文注入，不接受查询参数传入。
    """
    stmt = (
        select(UserWatchlistItem)
        .where(
            UserWatchlistItem.user_id == UUID(ctx.user_id),
            UserWatchlistItem.active.is_(True),
        )
        .order_by(UserWatchlistItem.created_at.desc())
    )
    result = await db.execute(stmt)
    items = result.scalars().all()
    return WatchlistListResponse(
        items=[WatchlistItemResponse.model_validate(item) for item in items],
        total=len(items),
    )


@router.post("", response_model=WatchlistItemResponse, status_code=status.HTTP_201_CREATED)
async def add_to_watchlist(
    payload: WatchlistAddRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
    monitor_limit: int | None = Depends(require_quota("monitor_limit")),
) -> WatchlistItemResponse:
    """加入自选 - 使用 AccessContext 统一权限模型校验订阅与额度。

    权限链：
    - require_active_subscription: 需有效订阅（admin 豁免），过期/无订阅返回 403
    - require_quota("monitor_limit"): 返回额度值（admin=None 无限制；member=int 限额）

    user_id 由权限上下文注入（不接受 body 中的 user_id）。
    若已存在软删除记录，则恢复 active=true 并清空 removed_at（重新加入）。
    若已存在 active 记录，返回 409 Conflict。

    套餐额度：
    - 恢复软删除记录前校验额度（恢复后 active 数量 +1）
    - 新建记录前校验额度
    - admin（monitor_limit=None）绕过额度限制
    - 超限返回 409 {"detail": "监控数量已达上限 N"}
    """
    user_id = UUID(ctx.user_id)

    # 校验股票存在
    inst_stmt = select(Instrument).where(Instrument.id == payload.instrument_id)
    inst_result = await db.execute(inst_stmt)
    if inst_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 instrument_id={payload.instrument_id} 的股票",
        )

    # 查询是否已有记录（含软删除）
    stmt = select(UserWatchlistItem).where(
        UserWatchlistItem.user_id == user_id,
        UserWatchlistItem.instrument_id == payload.instrument_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该股票已在自选列表中",
            )
        # 恢复软删除记录前校验额度（恢复后 active 数量 +1）
        await _check_limit_if_needed(db, user_id, monitor_limit)
        # 恢复软删除记录：重新加入
        existing.active = True
        existing.removed_at = None
        existing.source = payload.source
        await db.commit()
        await db.refresh(existing)
        return WatchlistItemResponse.model_validate(existing)

    # 新建记录前校验额度
    await _check_limit_if_needed(db, user_id, monitor_limit)

    # 新建自选记录
    item = UserWatchlistItem(
        user_id=user_id,
        instrument_id=payload.instrument_id,
        source=payload.source,
        active=True,
    )
    db.add(item)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        # 唯一约束冲突兜底（并发场景）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"加入自选失败（可能已存在）：{e}",
        ) from e
    await db.refresh(item)
    return WatchlistItemResponse.model_validate(item)


@router.get("/monitor-status", response_model=WatchlistMonitorStatusResponse)
async def get_watchlist_monitor_status(
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
) -> WatchlistMonitorStatusResponse:
    """查询当前用户自选股+监控状态聚合数据。

    返回当前用户所有 active 自选股，附带最新 released watchlist_monitor 版本的
    MonitorEvaluation（评估状态）与 StockFeatureSnapshot（指标数据）。

    状态语义：
    - market_session: 市场阶段（NON_TRADING_DAY/PRE_OPEN/MORNING_SESSION/LUNCH_BREAK/AFTERNOON_SESSION/MARKET_CLOSED）
    - calculation_status: 计算状态（SUCCEEDED/WAITING_SNAPSHOT/NO_SNAPSHOT）
      * SUCCEEDED: expected_trade_date 对应的 snapshot 存在，metrics 来自 snapshot.summary_payload
      * WAITING_SNAPSHOT: 交易日已收盘（MARKET_CLOSED）但当日 snapshot 尚未生成
        （orchestrator 未跑或失败；仅在 MARKET_CLOSED 时出现，盘中不出现）
      * NO_SNAPSHOT: 盘中无昨日 snapshot / 非交易日无历史 snapshot / 无法解析交易日
        （盘中 expected_trade_date 为上一交易日，缺失时返回 NO_SNAPSHOT，不是 WAITING_SNAPSHOT）
    - monitor_status: 兼容字段，SUCCEEDED 时回落到 market_session，否则与 calculation_status 一致
    - freshness_seconds: 基于 snapshot.updated_at 的数据新鲜度（秒）
    - last_bar_time: 最新评估对应的 bar 时间（来自 MonitorEvaluation）

    expected_snapshot_trade_date 规则（_resolve_expected_snapshot_trade_date）：
    - 交易日未收盘 → 上一交易日（盘中读昨日 snapshot）
    - 交易日已收盘 → today（读当日 snapshot）
    - 非交易日 → 最近交易日
    - 无法解析 → None（NO_SNAPSHOT）

    metrics 数据源：
    - 来自 StockFeatureSnapshot.summary_payload（_source='feature_snapshot'）
    - 无 snapshot 时 metrics 为空 dict
    """
    # 0. 计算市场状态
    # 统一使用上海业务日期/时间作为唯一事实源，避免服务器本地时区跨日误判
    today = shanghai_business_date()
    is_trading_day = await is_trading_day_async(db, today)
    now_cst = now_shanghai()
    market_status = _compute_market_status(now_cst, is_trading_day)

    # 1. 查找 watchlist_monitor 策略的最新 released 版本 ID
    latest_version_stmt = (
        select(StrategyVersion.id)
        .join(StrategyDefinition, StrategyVersion.strategy_definition_id == StrategyDefinition.id)
        .where(
            StrategyDefinition.strategy_key == WATCHLIST_MONITOR,
            StrategyVersion.status == "released",
        )
        .order_by(StrategyVersion.released_at.desc())
        .limit(1)
    )
    ver_result = await db.execute(latest_version_stmt)
    monitor_version_id = ver_result.scalar_one_or_none()

    # 2. 查询用户 active 自选股 + Instrument 信息
    items_stmt = (
        select(UserWatchlistItem, Instrument)
        .join(Instrument, UserWatchlistItem.instrument_id == Instrument.id)
        .where(
            UserWatchlistItem.user_id == UUID(ctx.user_id),
            UserWatchlistItem.active.is_(True),
        )
        .order_by(UserWatchlistItem.created_at.desc())
    )
    items_result = await db.execute(items_stmt)
    rows = items_result.all()

    # 3. 批量查询当日 feature snapshot（唯一键：instrument_id + expected_trade_date）
    # [RunGate] - publish gate：仅当 expected_trade_date 存在 succeeded run 时才读取 snapshot 行
    # failed/running run 对应的 snapshot 即使存在也不得被读取，避免半成品被显示为 SUCCEEDED
    snapshot_map: dict[UUID, StockFeatureSnapshot] = {}
    expected_trade_date = await _resolve_expected_snapshot_trade_date(
        db, today, market_status, is_trading_day,
    )
    if expected_trade_date is not None and rows:
        # [RunGate] - 先检查是否存在 succeeded run，无则跳过 snapshot 查询
        has_succeeded_run = await has_succeeded_snapshot_run(db, expected_trade_date)
        if has_succeeded_run:
            instrument_ids = [row[1].id for row in rows]
            snapshot_stmt = (
                select(StockFeatureSnapshot)
                .where(
                    StockFeatureSnapshot.instrument_id.in_(instrument_ids),
                    StockFeatureSnapshot.trade_date == expected_trade_date,
                    StockFeatureSnapshot.schema_version == 1,
                )
            )
            snapshot_result = await db.execute(snapshot_stmt)
            for snap in snapshot_result.scalars():
                snapshot_map[snap.instrument_id] = snap

    # 4. 批量查询每个 instrument 的最新 MonitorEvaluation（窗口函数取 rn=1）
    # MonitorEvaluation 仅用于展示评估状态（evaluation_status/retry_count/error_code/source_bar_time），
    # 不再作为 metrics 数据源
    eval_map: dict[UUID, _EvalInfo] = {}
    if monitor_version_id is not None and rows:
        instrument_ids = [row[1].id for row in rows]
        latest_eval_subq = (
            select(
                MonitorEvaluation.id,
                MonitorEvaluation.instrument_id,
                MonitorEvaluation.status.label("evaluation_status"),
                MonitorEvaluation.retry_count,
                MonitorEvaluation.error_code,
                MonitorEvaluation.source_bar_time,
                MonitorEvaluation.next_retry_at,
                func.row_number().over(
                    partition_by=MonitorEvaluation.instrument_id,
                    order_by=MonitorEvaluation.source_bar_time.desc(),
                ).label("rn"),
            )
            .where(
                MonitorEvaluation.strategy_version_id == monitor_version_id,
                MonitorEvaluation.instrument_id.in_(instrument_ids),
            )
            .subquery()
        )
        latest_eval_stmt = select(latest_eval_subq).where(latest_eval_subq.c.rn == 1)
        eval_result = await db.execute(latest_eval_stmt)
        for row in eval_result:
            eval_map[row.instrument_id] = _EvalInfo(
                evaluation_status=row.evaluation_status,
                retry_count=row.retry_count,
                error_code=row.error_code,
                source_bar_time=row.source_bar_time,
            )

    # 4.5. 批量查询每个 instrument 的最新 StrategyEvent
    latest_event_map: dict[UUID, dict] = {}
    if monitor_version_id is not None and rows:
        instrument_ids = [row[1].id for row in rows]
        latest_event_subq = (
            select(
                StrategyEvent.id,
                StrategyEvent.instrument_id,
                StrategyEvent.event_type,
                StrategyEvent.event_time,
                StrategyEvent.payload,
                func.row_number().over(
                    partition_by=StrategyEvent.instrument_id,
                    order_by=StrategyEvent.event_time.desc(),
                ).label("rn"),
            )
            .where(
                StrategyEvent.strategy_version_id == monitor_version_id,
                StrategyEvent.instrument_id.in_(instrument_ids),
            )
            .subquery()
        )
        latest_event_stmt = select(latest_event_subq).where(latest_event_subq.c.rn == 1)
        event_result = await db.execute(latest_event_stmt)
        for row in event_result:
            # Extract boundary from payload if available
            payload = row.payload if row.payload else {}
            boundary = payload.get("boundary") or payload.get("price") or None
            latest_event_map[row.instrument_id] = {
                "event_type": row.event_type,
                "event_time": row.event_time.isoformat() if row.event_time else None,
                "boundary": boundary,
            }

    # 5. 组装响应
    response_items: list[WatchlistMonitorStatusItem] = []
    for watchlist_item, instrument in rows:
        eval_row = eval_map.get(instrument.id)
        snapshot = snapshot_map.get(instrument.id)

        # 从 MonitorEvaluation 获取评估字段（仅用于展示，不影响 metrics）
        if eval_row is not None:
            evaluation_status = eval_row.evaluation_status
            retry_count = eval_row.retry_count
            error_code = eval_row.error_code
            source_bar_time = eval_row.source_bar_time
        else:
            evaluation_status = None
            retry_count = None
            error_code = None
            source_bar_time = None

        # [snapshot-based] - metrics 与 calculation_status 统一来自 feature snapshot
        market_session = market_status
        if snapshot is not None:
            calculation_status = "SUCCEEDED"
            metrics = dict(snapshot.summary_payload or {})
            updated_at = snapshot.updated_at
            # 数据新鲜度基于 snapshot.updated_at
            if updated_at is not None:
                freshness_seconds = int(
                    (now_cst - updated_at.astimezone(ZoneInfo("Asia/Shanghai"))).total_seconds()
                )
            else:
                freshness_seconds = None
        elif (
            expected_trade_date is not None
            and is_trading_day
            and market_status == "MARKET_CLOSED"
        ):
            # 交易日已收盘但 snapshot 缺失 → orchestrator 未生成（等待 after_close 生成）
            calculation_status = "WAITING_SNAPSHOT"
            metrics = {}
            updated_at = None
            freshness_seconds = None
        else:
            # 盘中无昨日 snapshot / 非交易日无历史 snapshot → NO_SNAPSHOT
            calculation_status = "NO_SNAPSHOT"
            metrics = {}
            updated_at = None
            freshness_seconds = None

        # 兼容字段：非 SUCCEEDED 时保留计算语义，SUCCEEDED 时使用市场状态
        if calculation_status != "SUCCEEDED":
            monitor_status = calculation_status
        else:
            monitor_status = market_session

        latest_event = latest_event_map.get(instrument.id)

        response_items.append(
            WatchlistMonitorStatusItem(
                watchlist_item_id=watchlist_item.id,
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                name=instrument.name,
                market=instrument.market,
                watchlist_created_at=watchlist_item.created_at,
                monitor_status=monitor_status,
                market_session=market_session,
                calculation_status=calculation_status,
                freshness_seconds=freshness_seconds,
                last_bar_time=str(source_bar_time) if source_bar_time else None,
                evaluation_status=evaluation_status,
                retry_count=retry_count,
                error_code=error_code,
                source_bar_time=str(source_bar_time) if source_bar_time else None,
                metrics=metrics,
                updated_at=updated_at,
                latest_event=latest_event,
            )
        )

    return WatchlistMonitorStatusResponse(items=response_items)


@router.delete("/{instrument_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_watchlist(
    instrument_id: UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
) -> None:
    """移除自选（软删除：active=false + removed_at）。

    user_id 由权限上下文注入。
    不存在或已移除返回 404。
    """
    stmt = select(UserWatchlistItem).where(
        UserWatchlistItem.user_id == UUID(ctx.user_id),
        UserWatchlistItem.instrument_id == instrument_id,
        UserWatchlistItem.active.is_(True),
    )
    result = await db.execute(stmt)
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="自选记录不存在或已移除",
        )
    item.active = False
    item.removed_at = datetime.now(UTC)
    await db.commit()


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={get_route_paths(router.routes)}")
    print("OK")
