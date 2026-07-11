"""行情列表查询服务 - 批量 JOIN 查询，禁止 N+1。

对应 PRD §8.1 行情列表契约 + §9.2 后端改造指引：
- 市场查询在 repository/service 层一次 join/批量加载，禁止 API 层循环调用单股服务。
- 每行一次返回页面所需全部字段。

查询策略（固定 SQL 数量，无逐行查询）：
1. instruments + is_watchlisted + 分页（scope=market 用 EXISTS，scope=watchlist 用 INNER JOIN）
2. count 查询（相同 WHERE 条件）
3. 最新 2 根日线（rn <= 2）批量按 instrument_ids 查询 → latest_price + change_pct
4. 最新 stock_feature_snapshot（rn = 1）批量 → dsa_state + structure_state
5. 最新 strategy_event（rn = 1）批量 → latest_event_title + latest_event_time

总计 5 条固定 SQL，不随 page_size 增长。
"""

from __future__ import annotations

import logging
import unicodedata
from uuid import UUID

from sqlalchemy import ColumnElement, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import now_shanghai, to_shanghai_iso
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.strategy_event import StrategyEvent
from app.models.watchlist import UserWatchlistItem
from app.schemas.market_stocks import MarketStockRow, MarketStocksResponse
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("market_stocks_service")

# 排序字段白名单（防止 SQL 注入）
_SORTABLE_FIELDS = {"symbol", "name"}


def _build_search_conditions(keyword: str | None) -> tuple[list[ColumnElement[bool]], ColumnElement[int]]:
    """构建搜索条件 + 命中优先级排序表达式。

    复用 instruments.py 的搜索逻辑：symbol 完全匹配 → symbol 前缀 → 拼音首字母前缀 → 名称包含。
    """
    conditions: list[ColumnElement[bool]] = [stock_symbol_sql_filter(Instrument)]
    rank_expr: ColumnElement[int] = literal(0)

    if keyword:
        keyword = unicodedata.normalize("NFKC", keyword)
        keyword_lower = keyword.lower()
        rank_expr = case(
            (Instrument.symbol == keyword, 0),
            (Instrument.symbol.ilike(f"{keyword}%"), 1),
            (Instrument.pinyin_initials.like(f"{keyword_lower}%"), 2),
            (Instrument.name.ilike(f"%{keyword}%"), 3),
            else_=4,
        )
        conditions.append(
            or_(
                Instrument.symbol == keyword,
                Instrument.symbol.ilike(f"{keyword}%"),
                Instrument.pinyin_initials.like(f"{keyword_lower}%"),
                Instrument.name.ilike(f"%{keyword}%"),
            )
        )

    return conditions, rank_expr


def _parse_sort(sort: str | None) -> tuple[list[ColumnElement], ColumnElement[int]]:
    """解析 sort=field:direction 参数，返回 (order_by 列表, rank_expr)。

    支持字段：symbol, name；方向：asc, desc。
    非法字段或方向回退到 symbol asc。
    """
    if not sort:
        return [], literal(0)

    parts = sort.split(":")
    field = parts[0].strip().lower()
    direction = parts[1].strip().lower() if len(parts) > 1 else "asc"

    if field not in _SORTABLE_FIELDS:
        return [], literal(0)

    col = getattr(Instrument, field)
    order_col = col.desc() if direction == "desc" else col.asc()
    return [order_col], literal(0)


def _map_dsa_state(swing_dir: object) -> str | None:
    """将 daily_developing_swing_dir 映射为可读状态。"""
    if swing_dir is None:
        return None
    try:
        val = int(swing_dir)
    except (TypeError, ValueError):
        return None
    if val > 0:
        return "上行"
    if val < 0:
        return "下行"
    return "震荡"


async def get_market_stocks(
    db: AsyncSession,
    user_id: UUID,
    scope: str,
    query: str | None,
    page: int,
    page_size: int,
    sort: str | None,
) -> MarketStocksResponse:
    """查询行情列表（服务端分页 + 批量加载，禁止 N+1）。

    Args:
        db: 异步数据库会话
        user_id: 当前用户 ID（用于自选关联）
        scope: market | watchlist
        query: 搜索关键词（代码/名称/拼音首字母）
        page: 页码（从 1 开始）
        page_size: 每页大小
        sort: 排序字段:方向（如 symbol:asc）

    Returns:
        MarketStocksResponse 分页响应
    """
    search_conditions, rank_expr = _build_search_conditions(query)
    sort_cols, _ = _parse_sort(sort)
    offset = (page - 1) * page_size

    # ===== Query 1: instruments + is_watchlisted + 分页 =====
    if scope == "watchlist":
        # watchlist scope: INNER JOIN，仅返回当前用户 active 自选
        base_stmt = (
            select(
                Instrument.id,
                Instrument.symbol,
                Instrument.name,
                Instrument.market,
                literal(True).label("is_watchlisted"),
            )
            .join(
                UserWatchlistItem,
                (
                    (UserWatchlistItem.instrument_id == Instrument.id)
                    & (UserWatchlistItem.user_id == user_id)
                    & (UserWatchlistItem.active.is_(True))
                ),
            )
        )
        for cond in search_conditions:
            base_stmt = base_stmt.where(cond)
    else:
        # market scope: 全市场 A 股，EXISTS 标记自选
        watched_exists = (
            select(1)
            .where(
                UserWatchlistItem.instrument_id == Instrument.id,
                UserWatchlistItem.user_id == user_id,
                UserWatchlistItem.active.is_(True),
            )
            .exists()
        )
        base_stmt = select(
            Instrument.id,
            Instrument.symbol,
            Instrument.name,
            Instrument.market,
            watched_exists.label("is_watchlisted"),
        )
        for cond in search_conditions:
            base_stmt = base_stmt.where(cond)

    # 排序：有搜索关键词时按命中优先级，否则按 sort 参数（默认 symbol asc）
    if query:
        base_stmt = base_stmt.order_by(rank_expr, Instrument.symbol)
    elif sort_cols:
        base_stmt = base_stmt.order_by(*sort_cols, Instrument.symbol)
    else:
        base_stmt = base_stmt.order_by(Instrument.symbol)

    base_stmt = base_stmt.offset(offset).limit(page_size)
    base_result = await db.execute(base_stmt)
    base_rows = base_result.all()

    if not base_rows:
        return MarketStocksResponse(
            items=[], page=page, page_size=page_size, total=0, as_of=to_shanghai_iso(now_shanghai())
        )

    instrument_ids = [row.id for row in base_rows]
    id_to_row = {row.id: row for row in base_rows}

    # ===== Query 2: count =====
    count_stmt = select(func.count()).select_from(Instrument)
    if scope == "watchlist":
        count_stmt = (
            select(func.count())
            .select_from(Instrument)
            .join(
                UserWatchlistItem,
                (
                    (UserWatchlistItem.instrument_id == Instrument.id)
                    & (UserWatchlistItem.user_id == user_id)
                    & (UserWatchlistItem.active.is_(True))
                ),
            )
        )
    for cond in search_conditions:
        count_stmt = count_stmt.where(cond)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    # ===== Query 3: 最新 2 根日线（批量） =====
    bars_subq = (
        select(
            BarDaily.instrument_id,
            BarDaily.trade_date,
            BarDaily.close,
            func.row_number()
            .over(
                partition_by=BarDaily.instrument_id,
                order_by=BarDaily.trade_date.desc(),
            )
            .label("rn"),
        )
        .where(BarDaily.instrument_id.in_(instrument_ids))
        .subquery()
    )
    bars_stmt = select(bars_subq).where(bars_subq.c.rn <= 2)
    bars_result = await db.execute(bars_stmt)

    price_map: dict[UUID, tuple[float | None, float | None]] = {}
    for bar_row in bars_result:
        inst_id = bar_row.instrument_id
        close_val = float(bar_row.close) if bar_row.close is not None else None
        existing = price_map.get(inst_id)
        if existing is None:
            # rn=1 (latest)
            price_map[inst_id] = (close_val, None)
        else:
            # rn=2 (previous)
            latest, _ = existing
            price_map[inst_id] = (latest, close_val)

    # ===== Query 4: 最新 feature snapshot（批量） =====
    snap_subq = (
        select(
            StockFeatureSnapshot.instrument_id,
            StockFeatureSnapshot.summary_payload,
            func.row_number()
            .over(
                partition_by=StockFeatureSnapshot.instrument_id,
                order_by=StockFeatureSnapshot.trade_date.desc(),
            )
            .label("rn"),
        )
        .where(
            StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            StockFeatureSnapshot.schema_version == 1,
        )
        .subquery()
    )
    snap_stmt = select(snap_subq).where(snap_subq.c.rn == 1)
    snap_result = await db.execute(snap_stmt)

    state_map: dict[UUID, tuple[str | None, str | None]] = {}
    for snap_row in snap_result:
        payload = snap_row.summary_payload or {}
        dsa_state = _map_dsa_state(payload.get("daily_developing_swing_dir"))
        structure_state = payload.get("cost_position_zone")
        state_map[snap_row.instrument_id] = (
            dsa_state,
            str(structure_state) if structure_state else None,
        )

    # ===== Query 5: 最新 strategy_event（批量） =====
    event_subq = (
        select(
            StrategyEvent.instrument_id,
            StrategyEvent.event_type,
            StrategyEvent.event_time,
            func.row_number()
            .over(
                partition_by=StrategyEvent.instrument_id,
                order_by=StrategyEvent.event_time.desc(),
            )
            .label("rn"),
        )
        .where(StrategyEvent.instrument_id.in_(instrument_ids))
        .subquery()
    )
    event_stmt = select(event_subq).where(event_subq.c.rn == 1)
    event_result = await db.execute(event_stmt)

    event_map: dict[UUID, tuple[str | None, str | None]] = {}
    for ev_row in event_result:
        event_map[ev_row.instrument_id] = (
            ev_row.event_type,
            ev_row.event_time.isoformat() if ev_row.event_time else None,
        )

    # ===== 组装响应 =====
    items: list[MarketStockRow] = []
    for inst_id in instrument_ids:
        base = id_to_row[inst_id]
        latest_price, prev_close = price_map.get(inst_id, (None, None))

        change_pct: float | None = None
        if latest_price is not None and prev_close is not None and prev_close != 0:
            change_pct = round((latest_price - prev_close) / prev_close * 100, 2)

        dsa_state, structure_state = state_map.get(inst_id, (None, None))
        event_title, event_time = event_map.get(inst_id, (None, None))

        items.append(
            MarketStockRow(
                instrument_id=inst_id,
                symbol=base.symbol,
                name=base.name,
                latest_price=latest_price,
                change_pct=change_pct,
                industry=None,
                concepts=[],
                dsa_state=dsa_state,
                structure_state=structure_state,
                latest_event_title=event_title,
                latest_event_time=event_time,
                is_watchlisted=base.is_watchlisted,
            )
        )

    return MarketStocksResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        as_of=to_shanghai_iso(now_shanghai()),
    )
