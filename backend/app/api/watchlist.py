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
- monitor-status 端点 JOIN Instrument + MonitorState(最新 released watchlist_monitor 版本) + MonitorEvaluation(最新评估记录)

套餐权限（plans 表，通过 AccessContext 统一读取）：
- POST /watchlist 及恢复软删除前校验 active count < monitor_limit
- 超限返回 409 {"detail": "监控数量已达上限 N"}
- admin（monitor_limit=None）绕过监控数量限制
- 过期订阅或无订阅返回 403（由 require_active_subscription 校验）
- 降级后已有数量超过额度不删除，只禁止新增
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import WATCHLIST_MONITOR
from app.core.time import now_shanghai, shanghai_business_date
from app.db import get_db
from app.models.instrument import Instrument
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.monitor_state import MonitorState
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
from app.services.calendar_service import is_trading_day_async
from app.services.market_status_service import TRADING_SESSIONS, compute_market_session
from app.services.monitor_snapshot_service import MonitorSnapshot, MonitorSnapshotService

logger = logging.getLogger("watchlist_api")

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


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


# 盘中交易时段内数据延迟判定阈值（秒）
_IN_TRADING_STALE_SECONDS = 180


def _compute_calculation_status(
    now_cst: datetime,
    market_session: str,
    eval_row: object | None,
    ms: MonitorState | None,
) -> str:
    """根据评估记录与市场状态计算 calculation_status。

    优先级：FAILED > STALE > SUCCEEDED > WAITING_FIRST_RUN
    - 盘后/非交易日/盘前/午休不判定 STALE，避免 30 分钟规则误报
    - 盘中交易时段内使用 180 秒阈值判定数据延迟
    - market_session 为 6 值枚举；TRADING_SESSIONS={MORNING_SESSION,AFTERNOON_SESSION}
    """
    if eval_row is None:
        # 无评估记录：按 MonitorState 新鲜度判定（若存在）
        # 盘中无 MonitorState → WAITING_FIRST_RUN；其他时段或数据新鲜 → SUCCEEDED
        if ms is not None and ms.updated_at and market_session in TRADING_SESSIONS:
            updated_at_cst = ms.updated_at.astimezone(ZoneInfo("Asia/Shanghai"))
            age_seconds = (now_cst - updated_at_cst).total_seconds()
            if age_seconds > _IN_TRADING_STALE_SECONDS:
                return "STALE"
            return "SUCCEEDED"
        return "WAITING_FIRST_RUN" if market_session in TRADING_SESSIONS else "SUCCEEDED"

    evaluation_status = eval_row.evaluation_status

    if evaluation_status in ("FAILED", "DEAD"):
        return "FAILED"

    if evaluation_status == "PENDING":
        # PENDING 且租约过期 → STALE
        if eval_row.next_retry_at and now_cst > eval_row.next_retry_at.astimezone(ZoneInfo("Asia/Shanghai")):
            return "STALE"
        return "SUCCEEDED"

    if evaluation_status == "SUCCEEDED":
        # 仅在盘中交易时段根据 MonitorState.updated_at 判定 STALE
        if market_session in TRADING_SESSIONS and ms is not None and ms.updated_at:
            updated_at_cst = ms.updated_at.astimezone(ZoneInfo("Asia/Shanghai"))
            age_seconds = (now_cst - updated_at_cst).total_seconds()
            if age_seconds > _IN_TRADING_STALE_SECONDS:
                return "STALE"
        return "SUCCEEDED"

    # 其他未知状态默认 SUCCEEDED
    return "SUCCEEDED"


def _flatten_node_metrics(metrics: dict | None) -> dict:
    """将 metrics 中的节点对象转为扁平字段，适合前端直接使用。

    输入: {"upper_node": {"price_mid": 72.35, "price_low": 72.10, "price_high": 72.60}, ...}
    输出: {"upper_node_price": 72.35, "upper_node_low": 72.10, "upper_node_high": 72.60, ...}
    """
    if not metrics:
        return {}

    flat = {}
    # Fields that are node objects (have price_mid/price_low/price_high)
    node_keys = ["upper_node", "lower_node", "last_touched_node"]
    for key in node_keys:
        val = metrics.get(key)
        if isinstance(val, dict):
            flat[f"{key}_price"] = val.get("price_mid") or val.get("price") or None
            flat[f"{key}_low"] = val.get("price_low") or None
            flat[f"{key}_high"] = val.get("price_high") or None
        elif val is not None:
            # Already a number, pass through
            flat[f"{key}_price"] = val

    # poc_price: may be object or number
    poc = metrics.get("poc_price")
    if isinstance(poc, dict):
        flat["poc_price"] = poc.get("price_mid") or poc.get("price") or None
    elif poc is not None:
        flat["poc_price"] = poc

    # Copy non-node fields as-is (bb_upper, bb_mid, bb_lower, current_price, etc.)
    skip_keys = set(node_keys) | {"poc_price"}
    for k, v in metrics.items():
        if k not in skip_keys:
            flat[k] = v

    return flat


def _snapshot_to_metrics(snapshot: MonitorSnapshot) -> dict:
    """将 MonitorSnapshotService 输出映射为 _flatten_node_metrics 输入格式。

    复用 MonitorSnapshotService 的计算结果，不复制 BB/VN 算法。
    只读 fallback：不生成事件、不写 MonitorState。
    """
    return {
        "current_price": snapshot.current_price,
        "bb_upper": snapshot.range_upper,
        "bb_mid": snapshot.range_center,
        "bb_lower": snapshot.range_lower,
        "upper_node": snapshot.upper_volume_zone,
        "lower_node": snapshot.lower_volume_zone,
        "poc_price": snapshot.most_traded_price,
        "position_0_1": snapshot.range_position,
        "previous_close": snapshot.previous_close,
        "change_pct": snapshot.change_pct,
        "_source": "fallback_snapshot",
    }


def _is_payload_valid(payload: dict | None) -> bool:
    """判定 MonitorState.payload 是否包含有效的监控指标字段。

    关键字段缺失、为 None 或 NaN 时视为无效，应触发 fallback。
    """
    if not payload:
        return False

    required_fields = [
        "current_price",
        "bb_upper",
        "bb_mid",
        "bb_lower",
        "upper_node",
        "lower_node",
        "poc_price",
        "position_0_1",
    ]
    for field in required_fields:
        value = payload.get(field)
        if value is None:
            return False
        try:
            if isinstance(value, float) and math.isnan(value):
                return False
        except (TypeError, ValueError):
            return False
    return True


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
    MonitorState 与 MonitorEvaluation。

    状态语义：
    - market_session: 市场阶段（NON_TRADING_DAY/PRE_OPEN/MORNING_SESSION/LUNCH_BREAK/AFTERNOON_SESSION/MARKET_CLOSED）
    - calculation_status: 计算状态（SUCCEEDED/FAILED/STALE/WAITING_FIRST_RUN）
    - monitor_status: 兼容字段，由 calculation_status 推导，SUCCEEDED 时回落到 market_session
    - freshness_seconds: 基于 MonitorState.updated_at 的数据新鲜度（秒）
    - last_bar_time: 最新评估对应的 bar 时间

    STALE 判定：仅在盘中交易时段使用 180 秒阈值；盘后/非交易日不因 30 分钟规则误判。
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

    # 3. 若有 released 版本，批量查询所有相关 MonitorState
    monitor_states_map: dict[UUID, MonitorState] = {}
    if monitor_version_id is not None and rows:
        instrument_ids = [row[1].id for row in rows]
        states_stmt = (
            select(MonitorState)
            .where(
                MonitorState.strategy_version_id == monitor_version_id,
                MonitorState.instrument_id.in_(instrument_ids),
            )
        )
        states_result = await db.execute(states_stmt)
        for state in states_result.scalars():
            monitor_states_map[state.instrument_id] = state

    # 4. 批量查询每个 instrument 的最新 MonitorEvaluation（窗口函数取 rn=1）
    eval_map: dict[UUID, object] = {}
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
            eval_map[row.instrument_id] = row

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
        ms = monitor_states_map.get(instrument.id)
        eval_row = eval_map.get(instrument.id)

        # 从 MonitorEvaluation 获取评估字段
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

        # [monitor_status] - 拆分为 market_session 与 calculation_status
        market_session = market_status
        calculation_status = _compute_calculation_status(
            now_cst, market_session, eval_row, ms,
        )
        # 兼容字段：FAILED/STALE/WAITING_FIRST_RUN 保留计算语义，SUCCEEDED 时使用市场状态
        if calculation_status in ("FAILED", "STALE", "WAITING_FIRST_RUN"):
            monitor_status = calculation_status
        else:
            monitor_status = market_session

        # 数据新鲜度
        freshness_seconds = None
        if ms is not None and ms.updated_at:
            freshness_seconds = int(
                (now_cst - ms.updated_at.astimezone(ZoneInfo("Asia/Shanghai"))).total_seconds()
            )

        # metrics 仍从 MonitorState.payload 获取（价格/指标数据），节点对象扁平化
        # [Bugfix] - 描述: MonitorState 不存在或 payload 无效时，基于已有 bars fallback 计算
        # 复用 MonitorSnapshotService.get_snapshot（只读，不生成事件/不写 MonitorState）
        fallback_error_code: str | None = None
        if ms is not None and _is_payload_valid(ms.payload):
            metrics = _flatten_node_metrics(ms.payload)
            updated_at = ms.updated_at
        else:
            try:
                snapshot = await MonitorSnapshotService().get_snapshot(
                    db, str(instrument.id), timeframe="1d"
                )
                metrics = _flatten_node_metrics(_snapshot_to_metrics(snapshot))
                updated_at = None
            except Exception as exc:
                # 单行降级：不阻断整个自选列表，记录 error 并返回空 metrics
                logger.exception(
                    "[watchlist.monitor-status] fallback 计算失败 instrument_id=%s: %s",
                    instrument.id,
                    exc,
                )
                metrics = {}
                updated_at = None
                fallback_error_code = "FALLBACK_FAILED"
        if fallback_error_code:
            error_code = fallback_error_code
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
    print(f"router.routes={[r.path for r in router.routes]}")
    print("OK")
