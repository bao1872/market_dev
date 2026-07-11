"""行情列表查询服务 - 批量 JOIN 查询，禁止 N+1。

对应 PRD §8.1 行情列表契约 + §9.2 后端改造指引：
- 市场查询在 repository/service 层一次 join/批量加载，禁止 API 层循环调用单股服务。
- 每行一次返回页面所需全部字段。

查询策略（固定 SQL 数量，无逐行查询）：
1. instruments + is_watchlisted + 分页（scope=market 用 EXISTS，scope=watchlist 用 INNER JOIN）
2. count 查询（相同 WHERE 条件，含 industry/concept EXISTS 子查询）
3. 最新 2 根日线（rn <= 2）批量按 instrument_ids 查询 → latest_price + change_pct
4. 最新 stock_feature_snapshot（rn = 1）批量 → dsa_state + structure_state
5. 最新 stock_state_event（rn = 1）批量 → latest_event_title + latest_event_time
6. boards_as_of — MAX(market_boards.updated_at) 标量查询
7. 板块归属批量查询 → industry + concepts
8. price_as_of — MAX(bar_daily.trade_date) 全局标量（不随分页变化）
9. state_as_of — MAX(stock_feature_snapshot.created_at) 全局标量（不随分页变化）

总计 9 条固定 SQL，不随 page_size 增长。
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, Integer, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import to_shanghai_iso
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_state_event import StockStateEvent
from app.models.watchlist import UserWatchlistItem
from app.schemas.market_stocks import MarketStockRow, MarketStocksResponse
from app.services.board_sync_service import get_instrument_boards_batch
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("market_stocks_service")

# 排序字段白名单（防止 SQL 注入）
_SORTABLE_FIELDS = {"name", "symbol", "change_pct", "dsa_state", "latest_event_time"}
_SORT_DIRECTIONS = {"asc", "desc"}


@dataclass(frozen=True)
class SortSpec:
    """排序规格：字段 + 方向。"""

    field: str
    direction: str


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


def _parse_sort(sort: str | None) -> SortSpec | None:
    """解析 sort=field:direction 参数，返回 SortSpec 或 None。

    支持字段：name, symbol, change_pct, dsa_state, latest_event_time。
    方向：asc, desc（默认 asc）。
    非法字段或方向抛出 ValueError（由 API 层转为 422）。
    """
    if not sort:
        return None

    parts = sort.split(":")
    field = parts[0].strip().lower()
    direction = parts[1].strip().lower() if len(parts) > 1 else "asc"

    if field not in _SORTABLE_FIELDS:
        raise ValueError(
            f"Invalid sort field '{field}'. Allowed: {', '.join(sorted(_SORTABLE_FIELDS))}"
        )
    if direction not in _SORT_DIRECTIONS:
        raise ValueError(f"Invalid sort direction '{direction}'. Allowed: asc, desc")

    return SortSpec(field=field, direction=direction)


def _build_sort_expression(field: str) -> ColumnElement:
    """构建排序标量表达式（用于 change_pct/dsa_state/latest_event_time）。

    name/symbol 直接使用 Instrument 列，不经过此函数。
    """
    if field == "change_pct":
        latest_close = (
            select(BarDaily.close)
            .where(BarDaily.instrument_id == Instrument.id)
            .order_by(BarDaily.trade_date.desc())
            .limit(1)
            .scalar_subquery()
        )
        prev_close = (
            select(BarDaily.close)
            .where(BarDaily.instrument_id == Instrument.id)
            .order_by(BarDaily.trade_date.desc())
            .offset(1)
            .limit(1)
            .scalar_subquery()
        )
        return case(
            (
                (prev_close.isnot(None)) & (prev_close != 0),
                (latest_close - prev_close) / prev_close * 100.0,
            ),
            else_=None,
        )

    if field == "dsa_state":
        return (
            select(
                cast(
                    StockFeatureSnapshot.summary_payload["daily_developing_swing_dir"].astext,
                    Integer,
                )
            )
            .where(StockFeatureSnapshot.instrument_id == Instrument.id)
            .order_by(StockFeatureSnapshot.trade_date.desc())
            .limit(1)
            .scalar_subquery()
        )

    if field == "latest_event_time":
        return (
            select(func.max(StockStateEvent.occurred_at))
            .where(StockStateEvent.instrument_id == Instrument.id)
            .scalar_subquery()
        )

    # 不应到达此处（_parse_sort 已校验白名单）
    raise ValueError(f"Unsupported sort field: {field}")


def _build_order_by(
    sort_spec: SortSpec | None,
    has_query: bool,
    rank_expr: ColumnElement[int],
) -> list[ColumnElement]:
    """构建 ORDER BY 列表。

    - 搜索模式（has_query=True）：按命中优先级排序，忽略 sort_spec。
    - 无 sort_spec：默认 symbol asc。
    - name/symbol：直接使用 Instrument 列。
    - change_pct/dsa_state/latest_event_time：使用标量子查询表达式。
    """
    if has_query:
        return [rank_expr, Instrument.symbol.asc()]

    if sort_spec is None:
        return [Instrument.symbol.asc()]

    field = sort_spec.field
    direction = sort_spec.direction

    if field in ("name", "symbol"):
        col = getattr(Instrument, field)
        order_col = col.desc().nullslast() if direction == "desc" else col.asc().nullslast()
        return [order_col, Instrument.symbol.asc()]

    sort_expr = _build_sort_expression(field)
    order_col = (
        sort_expr.desc().nullslast() if direction == "desc" else sort_expr.asc().nullslast()
    )
    return [order_col, Instrument.symbol.asc()]


def _map_dsa_state(swing_dir: object) -> str | None:
    """将 daily_developing_swing_dir 映射为可读状态。"""
    if swing_dir is None:
        return None
    if isinstance(swing_dir, bool) or not isinstance(swing_dir, (int, float, str)):
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


def _build_state_filter(state: str | None) -> ColumnElement[bool] | None:
    """构建状态筛选条件（Phase 4 实现）。

    使用标量子查询取最新 snapshot 的 daily_developing_swing_dir，
    按状态码过滤：up → > 0, down → < 0, sideways → == 0。

    与 _build_sort_expression(dsa_state) 使用相同的子查询模式，
    确保 filter 和 sort 口径一致。
    """
    if state is None:
        return None

    swing_dir_subq = (
        select(
            cast(
                StockFeatureSnapshot.summary_payload["daily_developing_swing_dir"].astext,
                Integer,
            )
        )
        .where(StockFeatureSnapshot.instrument_id == Instrument.id)
        .order_by(StockFeatureSnapshot.trade_date.desc())
        .limit(1)
        .scalar_subquery()
    )

    if state == "up":
        return swing_dir_subq > 0
    if state == "down":
        return swing_dir_subq < 0
    if state == "sideways":
        return swing_dir_subq == 0
    return None


def _build_board_filter_conditions(
    industry: str | None, concept: str | None
) -> list[ColumnElement[bool]]:
    """构建行业/概念筛选 EXISTS 条件（PRD §7.5）。

    使用 SQL EXISTS 子查询，避免先加载全量 UUID 再 IN 的 N+1 模式。
    industry/concept 参数为板块名称，通过 market_boards.name 匹配。
    """
    conditions: list[ColumnElement[bool]] = []
    if industry:
        industry_exists = (
            select(1)
            .select_from(MarketBoardMembership)
            .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
            .where(
                MarketBoardMembership.instrumentId == Instrument.id,
                MarketBoard.type == "industry",
                MarketBoard.name == industry,
            )
            .exists()
        )
        conditions.append(industry_exists)
    if concept:
        concept_exists = (
            select(1)
            .select_from(MarketBoardMembership)
            .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
            .where(
                MarketBoardMembership.instrumentId == Instrument.id,
                MarketBoard.type == "concept",
                MarketBoard.name == concept,
            )
            .exists()
        )
        conditions.append(concept_exists)
    return conditions


async def get_market_stocks(
    db: AsyncSession,
    user_id: UUID,
    scope: str,
    query: str | None,
    page: int,
    page_size: int,
    sort: str | None,
    state: str | None = None,
    industry: str | None = None,
    concept: str | None = None,
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
        state: 状态筛选（Phase 4）：up/down/sideways
        industry: 行业筛选（板块名称，qstock 同步后可用）
        concept: 概念筛选（板块名称，qstock 同步后可用）

    Returns:
        MarketStocksResponse 分页响应
    """
    search_conditions, rank_expr = _build_search_conditions(query)
    sort_spec = _parse_sort(sort)
    state_cond = _build_state_filter(state)
    board_conditions = _build_board_filter_conditions(industry, concept)
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
        if state_cond is not None:
            base_stmt = base_stmt.where(state_cond)
        for cond in board_conditions:
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
        if state_cond is not None:
            base_stmt = base_stmt.where(state_cond)
        for cond in board_conditions:
            base_stmt = base_stmt.where(cond)

    # 排序：有搜索关键词时按命中优先级，否则按 sort 参数（默认 symbol asc）
    order_by_cols = _build_order_by(sort_spec, has_query=bool(query), rank_expr=rank_expr)
    base_stmt = base_stmt.order_by(*order_by_cols)

    base_stmt = base_stmt.offset(offset).limit(page_size)
    base_result = await db.execute(base_stmt)
    base_rows = base_result.all()

    # 空页边界：page 超出总页数时 base_rows 为空，但仍需返回真实 total 和全局 as_of
    if not base_rows:
        # 仍执行 count 查询获取真实 total
        count_stmt_empty = select(func.count()).select_from(Instrument)
        if scope == "watchlist":
            count_stmt_empty = (
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
            count_stmt_empty = count_stmt_empty.where(cond)
        for cond in board_conditions:
            count_stmt_empty = count_stmt_empty.where(cond)
        if state_cond is not None:
            count_stmt_empty = count_stmt_empty.where(state_cond)
        real_total = await db.scalar(count_stmt_empty) or 0

        # 全局 as_of 标量查询（不随分页变化）
        empty_price_as_of = await db.scalar(select(func.max(BarDaily.trade_date)))
        empty_state_as_of = await db.scalar(
            select(func.max(StockFeatureSnapshot.created_at)).where(
                StockFeatureSnapshot.schema_version == 1
            )
        )
        empty_boards_as_of = await db.scalar(
            select(func.max(MarketBoard.updatedAt))
        )

        return MarketStocksResponse(
            items=[],
            page=page,
            page_size=page_size,
            total=real_total,
            price_as_of=empty_price_as_of.isoformat() if empty_price_as_of else None,
            state_as_of=to_shanghai_iso(empty_state_as_of) if empty_state_as_of else None,
            boards_as_of=to_shanghai_iso(empty_boards_as_of) if empty_boards_as_of else None,
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
    if state_cond is not None:
        count_stmt = count_stmt.where(state_cond)
    for cond in board_conditions:
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
            StockFeatureSnapshot.created_at,
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

    # ===== Query 5: 最新 stock_state_event（批量） =====
    event_subq = (
        select(
            StockStateEvent.instrument_id,
            StockStateEvent.title,
            StockStateEvent.occurred_at,
            func.row_number()
            .over(
                partition_by=StockStateEvent.instrument_id,
                order_by=StockStateEvent.occurred_at.desc(),
            )
            .label("rn"),
        )
        .where(StockStateEvent.instrument_id.in_(instrument_ids))
        .subquery()
    )
    event_stmt = select(event_subq).where(event_subq.c.rn == 1)
    event_result = await db.execute(event_stmt)

    event_map: dict[UUID, tuple[str | None, str | None]] = {}
    for ev_row in event_result:
        event_map[ev_row.instrument_id] = (
            ev_row.title,
            ev_row.occurred_at.isoformat() if ev_row.occurred_at else None,
        )

    # ===== Query 7: 板块归属（批量，industry/concepts） =====
    boards_map = await get_instrument_boards_batch(db, instrument_ids)

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

        # 板块归属：industry 取首个行业，concepts 取全部概念
        inst_boards = boards_map.get(inst_id, [])
        industry_name = next(
            (b["name"] for b in inst_boards if b["type"] == "industry"), None
        )
        concept_names = [b["name"] for b in inst_boards if b["type"] == "concept"]

        items.append(
            MarketStockRow(
                instrument_id=inst_id,
                symbol=base.symbol,
                name=base.name,
                latest_price=latest_price,
                change_pct=change_pct,
                industry=industry_name,
                concepts=concept_names,
                dsa_state=dsa_state,
                structure_state=structure_state,
                latest_event_title=event_title,
                latest_event_time=event_time,
                is_watchlisted=base.is_watchlisted,
            )
        )

    # ===== Query 6: boards_as_of — 最近一次板块同步时间 =====
    boards_as_of_dt: datetime | None = await db.scalar(
        select(func.max(MarketBoard.updatedAt))
    )

    # ===== Query 8: price_as_of — 全局最新日线 trade_date（不随分页变化） =====
    price_as_of_date: date | None = await db.scalar(select(func.max(BarDaily.trade_date)))

    # ===== Query 9: state_as_of — 全局最新特征快照 created_at（不随分页变化） =====
    state_as_of_dt: datetime | None = await db.scalar(
        select(func.max(StockFeatureSnapshot.created_at)).where(
            StockFeatureSnapshot.schema_version == 1
        )
    )

    return MarketStocksResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        price_as_of=price_as_of_date.isoformat() if price_as_of_date else None,
        state_as_of=to_shanghai_iso(state_as_of_dt) if state_as_of_dt else None,
        boards_as_of=to_shanghai_iso(boards_as_of_dt) if boards_as_of_dt else None,
    )
