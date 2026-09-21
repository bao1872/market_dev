"""Market Dashboard Scope Explorer（R2，projection-only）。

``GET /v1/market-dashboard/scopes`` —— 行业/概念板块**全集 collection** 探索器。

数据来源（只读；与 F2 read path 同一 SSOT）：
- ``market_dashboard_market_daily``  全市场最近 6 个 projection 交易日 → 全局 T / T-5
- ``market_dashboard_scope_daily``   板块/概念每日投影（CUR = T，PREV = T-5）
- ``market_boards``                  板块目录（name / type / hierarchy_level）

设计边界（R2 冻结）：
- **projection-only**：禁止 bars_daily / instruments / market_board_memberships /
  pandas / F1A compute service。
- **T / T-5 是全局语义**：T 与 PREV 取自全市场 projection 日历（``trade_date DESC LIMIT 6``），
  所有 board 共用同一 T / PREV。禁止 per-board 自行 ``row_number`` 滑窗、禁止向更老日期
  fallback、禁止自然日 -5 / nearest available row。
- board 缺 PREV row → ``previous_*`` / ``*_delta`` = None（**绝不 fallback**）。
- ratio = ``above_count / valid_count``；``valid_count == 0`` → None（**禁止伪造 0%**）。
- delta 单位 = ratio difference（0.12 = +12pp），保持现有 ``round4`` 语义。
- 查询数常数级：1 次全市场日历 + 1 次 explorer（``count(*) OVER()`` 带出 total）；
  仅当分页越界（无行返回）时才补 1 次 count，保证 total 正确。
- search / filter / sort / pagination **全部 SQL server-side**；NULLS LAST（asc/desc 皆然），
  并以 ``board_id ASC`` 作为稳定二级排序（同值分页不漂移）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Float, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from app.models.market_board import MarketBoard
from app.models.market_dashboard import (
    MarketDashboardMarketDaily,
    MarketDashboardScopeDaily,
)

# ---------------------------------------------------------------------------
# 契约常量（validation owner）
# ---------------------------------------------------------------------------
SCOPE_TYPES: tuple[str, ...] = ("industry", "concept")
HIERARCHY_LEVELS: tuple[str, ...] = ("L1", "L2", "L3")
SORT_FIELDS: tuple[str, ...] = (
    "name",
    "member_count",
    "ma5",
    "ma10",
    "ma20",
    "ma50",
    "ma120",
    "ma5_delta",
    "ma10_delta",
)
DIRECTIONS: tuple[str, ...] = ("asc", "desc")

# (min_param, max_param) → min > max 视为非法（422）
RANGE_PAIRS: tuple[tuple[str, str], ...] = (
    ("ma5_min", "ma5_max"),
    ("ma10_min", "ma10_max"),
    ("ma5_delta_min", "ma5_delta_max"),
    ("ma10_delta_min", "ma10_delta_max"),
    ("member_count_min", "member_count_max"),
)

# T 窗口：最近 6 个 projection 交易日 → T = dates[0]，PREV = dates[5]（= T-5）
T_WINDOW: int = 6

MA_WINDOWS: tuple[int, ...] = (5, 10, 20, 50, 120)


def _round4(x: float | None) -> float | None:
    """与 market_dashboard_read_service._round4 完全一致（保持现有 round4 语义）。"""
    return None if x is None else round(x, 4)


# ---------------------------------------------------------------------------
# 纯校验（无 IO，便于纯单元测试）
# ---------------------------------------------------------------------------
def validate_explorer_params(
    *,
    scope_type: str,
    hierarchy_level: str | None,
    sort: str,
    direction: str,
    ranges: dict[str, float | None],
) -> str | None:
    """校验 explorer query 参数；返回错误信息（API 层转 422）或 None。

    与 rankings endpoint 不同：concept + hierarchy_level **不静默忽略**，而是显式 422。
    """
    if scope_type not in SCOPE_TYPES:
        return f"Invalid scope_type: {scope_type}; must be one of: industry, concept"
    if hierarchy_level is not None and hierarchy_level not in HIERARCHY_LEVELS:
        return f"Invalid hierarchy_level: {hierarchy_level}; must be one of: L1, L2, L3"
    if scope_type == "concept" and hierarchy_level is not None:
        return "hierarchy_level 仅对 industry 有效；concept 传入 hierarchy_level 非法"
    if sort not in SORT_FIELDS:
        return f"Invalid sort: {sort}; must be one of: {', '.join(SORT_FIELDS)}"
    if direction not in DIRECTIONS:
        return f"Invalid direction: {direction}; must be one of: asc, desc"
    for lo_key, hi_key in RANGE_PAIRS:
        lo, hi = ranges.get(lo_key), ranges.get(hi_key)
        if lo is not None and hi is not None and lo > hi:
            return f"{lo_key} ({lo}) 不得大于 {hi_key} ({hi})"
    return None


# ---------------------------------------------------------------------------
# 纯派生：T / PREV 解析（无 IO）
# ---------------------------------------------------------------------------
def resolve_t_prev(market_dates: list[date]) -> tuple[date | None, date | None]:
    """从全市场 projection 日历（DESC）解析全局 T / PREV。

    - 无 projection → (None, None)
    - < T_WINDOW 个日期 → (dates[0], None)：previous_trade_date=None，所有 delta=None
    - >= T_WINDOW → (dates[0], dates[T_WINDOW-1]) = 真正 T vs T-5 trading dates
    """
    if not market_dates:
        return None, None
    if len(market_dates) < T_WINDOW:
        return market_dates[0], None
    return market_dates[0], market_dates[T_WINDOW - 1]


# ---------------------------------------------------------------------------
# SQL 派生 expression（filters / ordering 直接使用，避免 integer division）
# ---------------------------------------------------------------------------
def _ratio_expr(above: Any, valid: Any) -> Any:
    """ratio = above / valid（float）；valid <= 0 → NULL（不伪造 0%）。"""
    return case(
        (valid > 0, sa.cast(above, Float) / sa.cast(valid, Float)),
        else_=None,
    )


@dataclass(frozen=True)
class ExplorerExpressions:
    """一次查询内复用的 SQL expression 集合（cur / prev / delta）。"""

    ma: dict[int, Any]
    prev_ma5: Any
    prev_ma10: Any
    ma5_delta: Any
    ma10_delta: Any

    def by_sort(self, sort: str) -> Any | None:
        if sort == "ma5_delta":
            return self.ma5_delta
        if sort == "ma10_delta":
            return self.ma10_delta
        if sort.startswith("ma") and sort[2:].isdigit():
            return self.ma.get(int(sort[2:]))
        return None


def build_expressions(
    cur: Any = MarketDashboardScopeDaily,
    prev: Any = MarketDashboardScopeDaily,
) -> ExplorerExpressions:
    """构造 CUR / PREV 的 ratio 与 delta expression（纯，可编译断言）。"""
    ma = {
        k: _ratio_expr(
            getattr(cur, f"ma{k}_above_count"), getattr(cur, f"ma{k}_valid_count")
        )
        for k in MA_WINDOWS
    }
    prev_ma5 = _ratio_expr(prev.ma5_above_count, prev.ma5_valid_count)
    prev_ma10 = _ratio_expr(prev.ma10_above_count, prev.ma10_valid_count)
    return ExplorerExpressions(
        ma=ma,
        prev_ma5=prev_ma5,
        prev_ma10=prev_ma10,
        # LEFT JOIN 缺 PREV → CUR - NULL = NULL（绝不 fallback）
        ma5_delta=ma[5] - prev_ma5,
        ma10_delta=ma[10] - prev_ma10,
    )


def _range_predicates(expr: Any, lo: float | None, hi: float | None) -> list[Any]:
    preds: list[Any] = []
    if lo is not None:
        preds.append(expr >= lo)
    if hi is not None:
        preds.append(expr <= hi)
    return preds


def build_explorer_select(
    *,
    trade_date: date,
    prev_trade_date: date | None,
    scope_type: str,
    hierarchy_level: str | None,
    q: str | None,
    ranges: dict[str, float | None],
    sort: str,
    direction: str,
    page: int,
    page_size: int,
    with_total: bool = True,
) -> Select:
    """构造单条 explorer SELECT（SQL server-side filter/sort/pagination）。

    ``with_total=True`` 时附带 ``count(*) OVER()``（filter 后、pagination 前总数）。
    """
    cur = aliased(MarketDashboardScopeDaily, name="cur")
    prev = aliased(MarketDashboardScopeDaily, name="prev")
    expr = build_expressions(cur, prev)

    join_cond = (
        and_(prev.board_id == cur.board_id, prev.trade_date == prev_trade_date)
        if prev_trade_date is not None
        else sa.false()
    )

    columns: list[Any] = [
        MarketBoard.id.label("board_id"),
        MarketBoard.name.label("board_name"),
        MarketBoard.type.label("board_type"),
        MarketBoard.hierarchyLevel.label("hierarchy_level"),
        cur.membership_version.label("membership_version"),
        cur.member_count.label("member_count"),
        expr.ma[5].label("ma5"),
        expr.ma[10].label("ma10"),
        expr.ma[20].label("ma20"),
        expr.ma[50].label("ma50"),
        expr.ma[120].label("ma120"),
        expr.prev_ma5.label("previous_ma5"),
        expr.prev_ma10.label("previous_ma10"),
        expr.ma5_delta.label("ma5_delta"),
        expr.ma10_delta.label("ma10_delta"),
    ]
    if with_total:
        columns.append(func.count().over().label("total"))

    where: list[Any] = [
        cur.trade_date == trade_date,
        MarketBoard.type == scope_type,
        MarketBoard.isActive.is_(True),
    ]
    if scope_type == "industry" and hierarchy_level is not None:
        where.append(MarketBoard.hierarchyLevel == hierarchy_level)
    if q:
        # icontains + autoescape：真正的 substring 搜索（自动转义 % / _ 通配符，
        # 避免手写 ILIKE ... ESCAPE '\' 在 standard_conforming_strings 下的转义陷阱）。
        where.append(MarketBoard.name.icontains(q, autoescape=True))
    where.extend(
        _range_predicates(cur.member_count, ranges.get("member_count_min"), ranges.get("member_count_max"))
    )
    where.extend(_range_predicates(expr.ma[5], ranges.get("ma5_min"), ranges.get("ma5_max")))
    where.extend(_range_predicates(expr.ma[10], ranges.get("ma10_min"), ranges.get("ma10_max")))
    where.extend(
        _range_predicates(expr.ma5_delta, ranges.get("ma5_delta_min"), ranges.get("ma5_delta_max"))
    )
    where.extend(
        _range_predicates(expr.ma10_delta, ranges.get("ma10_delta_min"), ranges.get("ma10_delta_max"))
    )

    stmt = (
        select(*columns)
        .select_from(cur)
        .join(MarketBoard, MarketBoard.id == cur.board_id)
        .outerjoin(prev, join_cond)
        .where(*where)
    )

    stmt = stmt.order_by(*_order_by(cur, expr, sort, direction)).offset((page - 1) * page_size).limit(page_size)
    return stmt


def _order_by(cur: Any, expr: ExplorerExpressions, sort: str, direction: str) -> list[Any]:
    """排序：NULLS LAST（asc/desc 皆然）+ board_id ASC 稳定二级排序。"""
    desc = direction == "desc"
    if sort == "name":
        primary = (
            MarketBoard.name.desc().nullslast() if desc else MarketBoard.name.asc().nullslast()
        )
    elif sort == "member_count":
        primary = (
            cur.member_count.desc().nullslast() if desc else cur.member_count.asc().nullslast()
        )
    else:
        target = expr.by_sort(sort)
        if target is None:
            target = expr.ma[5]
        primary = target.desc().nullslast() if desc else target.asc().nullslast()
    return [primary, MarketBoard.id.asc()]


# ---------------------------------------------------------------------------
# DB 读取（只读，最多 2 次 round trip）
# ---------------------------------------------------------------------------
async def _fetch_market_dates(db: AsyncSession, limit: int) -> list[date]:
    stmt = (
        select(MarketDashboardMarketDaily.trade_date)
        .order_by(MarketDashboardMarketDaily.trade_date.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _fetch_explorer_rows(
    db: AsyncSession,
    *,
    trade_date: date,
    prev_trade_date: date | None,
    scope_type: str,
    hierarchy_level: str | None,
    q: str | None,
    ranges: dict[str, float | None],
    sort: str,
    direction: str,
    page: int,
    page_size: int,
) -> tuple[list[Any], int]:
    """返回 (rows, total)。正常路径单查询（count(*) OVER()）即得 total。"""
    kwargs: dict[str, Any] = {
        "trade_date": trade_date,
        "prev_trade_date": prev_trade_date,
        "scope_type": scope_type,
        "hierarchy_level": hierarchy_level,
        "q": q,
        "ranges": ranges,
    }
    stmt = build_explorer_select(
        **kwargs,
        sort=sort,
        direction=direction,
        page=page,
        page_size=page_size,
        with_total=True,
    )
    # build_explorer_select 内部重建了 aliased(cur/prev)；count 需要同一 FROM/WHERE，
    # 故用同一 builder 去掉排序/分页后包成 subquery 计数（仅越界补查时使用）。
    rows = list((await db.execute(stmt)).all())
    if rows:
        return rows, int(rows[0].total)
    count_stmt = build_explorer_select(
        **kwargs,
        sort=sort,
        direction=direction,
        page=1,
        page_size=page_size,
        with_total=False,
    )
    count_stmt = count_stmt.order_by(None).limit(None).offset(None)
    total = int(
        (await db.execute(select(func.count()).select_from(count_stmt.subquery()))).scalar_one()
    )
    return [], total


def _row_to_item(row: Any) -> dict[str, Any]:
    return {
        "board_id": str(row.board_id),
        "board_name": row.board_name,
        "board_type": row.board_type,
        "hierarchy_level": row.hierarchy_level,
        "membership_version": row.membership_version,
        "member_count": int(row.member_count),
        "ma5": _round4(row.ma5),
        "ma10": _round4(row.ma10),
        "ma20": _round4(row.ma20),
        "ma50": _round4(row.ma50),
        "ma120": _round4(row.ma120),
        "previous_ma5": _round4(row.previous_ma5),
        "previous_ma10": _round4(row.previous_ma10),
        "ma5_delta": _round4(row.ma5_delta),
        "ma10_delta": _round4(row.ma10_delta),
    }


async def get_scope_explorer(
    db: AsyncSession,
    *,
    scope_type: str = "industry",
    hierarchy_level: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort: str = "ma5",
    direction: str = "desc",
    ranges: dict[str, float | None] | None = None,
):
    """Scope Explorer 读入口（返回 ScopeExplorerResponse）。

    无任何 market projection → 200 + 空 items（projection_trade_date=None，不伪造日期）。
    """
    from app.schemas.market_dashboard import ScopeExplorerResponse

    ranges = ranges or {}
    dates = await _fetch_market_dates(db, T_WINDOW)
    t, prev_date = resolve_t_prev(dates)
    if t is None:
        return ScopeExplorerResponse(
            projection_trade_date=None,
            previous_trade_date=None,
            total=0,
            page=page,
            page_size=page_size,
            items=[],
        )

    rows, total = await _fetch_explorer_rows(
        db,
        trade_date=t,
        prev_trade_date=prev_date,
        scope_type=scope_type,
        hierarchy_level=hierarchy_level,
        q=q,
        ranges=ranges,
        sort=sort,
        direction=direction,
        page=page,
        page_size=page_size,
    )
    return ScopeExplorerResponse(
        projection_trade_date=t.isoformat(),
        previous_trade_date=prev_date.isoformat() if prev_date is not None else None,
        total=total,
        page=page,
        page_size=page_size,
        items=[_row_to_item(r) for r in rows],
    )
