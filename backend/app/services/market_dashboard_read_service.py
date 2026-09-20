"""Market Dashboard 只读 read model（PRD：Dashboard 只读投影）。

数据来源（只读，禁止写）：
- ``market_dashboard_market_daily``   全市场每日一行
- ``market_dashboard_scope_daily``    板块/概念每日一行
- ``market_boards``                   板块目录（name/type/hierarchy/membership_version）

设计边界（F2 冻结）：
- 只派生，不重算 bars；不碰 F1A/B/C/D、bar_daily、instruments、MarketBoardMembership。
- 读路径查询数保持常数级（market=1；rankings/scope/compare=2：目录 + 投影窗口）。
- breadth_ratio = above_count / valid_count；valid_count == 0 → None（**禁止伪造 0%**）。
- EW index：first valid displayed point = 100；``I_t = I_(t-1) * (1 + equal_weight_return_t)``；
  equal_weight_return 为 None 时该点 index = None，且打断后续链（不 forward fill）。
- ranking：industry 支持 L1/L2/L3，concept 不分层；delta 用第 t 与第 t-5 个 projection
  交易日行（不是自然日）。
- 所有"未构建/不可用"以 None 或 404 表达，不返回伪造的 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_board import MarketBoard
from app.models.market_dashboard import (
    MarketDashboardMarketDaily,
    MarketDashboardScopeDaily,
)

_WINDOWS: tuple[int, ...] = (5, 10, 20, 50, 120)

# ranking 的 delta 固定取最近 5 个真实交易日的窗口；V1 lookback 仅允许 5。
_RANKING_LOOKBACK: int = 5


# ---------------------------------------------------------------------------
# 行表示（与 ORM 解耦，便于纯单元测试）
# ---------------------------------------------------------------------------
@dataclass
class MarketDailyRow:
    trade_date: date
    ma5_above_count: int
    ma5_valid_count: int
    ma10_above_count: int
    ma10_valid_count: int
    ma20_above_count: int
    ma20_valid_count: int
    ma50_above_count: int
    ma50_valid_count: int
    ma120_above_count: int
    ma120_valid_count: int
    equal_weight_return: float | None


@dataclass
class ScopeDailyRow:
    board_id: UUID
    trade_date: date
    membership_version: str
    ma5_above_count: int
    ma5_valid_count: int
    ma10_above_count: int
    ma10_valid_count: int
    ma20_above_count: int
    ma20_valid_count: int
    ma50_above_count: int
    ma50_valid_count: int
    ma120_above_count: int
    ma120_valid_count: int
    equal_weight_return: float | None


@dataclass
class BoardMeta:
    board_id: UUID
    name: str
    type: str  # industry | concept
    hierarchy_level: str  # L1 | L2 | L3
    membership_version: str
    is_active: bool


# ---------------------------------------------------------------------------
# 纯派生函数（无 IO，便于纯单元测试）
# ---------------------------------------------------------------------------
def _ratio(above: int, valid: int) -> float | None:
    """breadth ratio；valid_count == 0 → None（不伪造 0%）。"""
    if valid <= 0:
        return None
    return above / valid


def _round4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _round2(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def _ew_index(returns: list[float | None], base: float = 100.0) -> list[float | None]:
    """等权指数链。

    - 首个有效点 = base（100）；
    - equal_weight_return 为 None 时该点 = None；
    - 链建立后遇到 None → 该点 None 且**打断后续链**（后续恒为 None，不 forward fill）；
    - 链建立前的 None（前导无数据）仅记 None，不触发打断（首个有效点仍 = base）。
    """
    out: list[float | None] = []
    cur: float | None = None
    based = False
    broken = False
    for r in returns:
        if r is None:
            out.append(None)
            if based:
                broken = True
            continue
        if broken:
            out.append(None)
            continue
        if not based:
            cur = base
            based = True
        else:
            assert cur is not None  # 首个有效点已置 base，此处必为 float
            cur = cur * (1.0 + r)
        out.append(cur)
    return out


def _point(r, idx, *, with_ma120: bool = True) -> dict:
    point = {
        "trade_date": r.trade_date.isoformat(),
        "ma5": _round4(_ratio(r.ma5_above_count, r.ma5_valid_count)),
        "ma10": _round4(_ratio(r.ma10_above_count, r.ma10_valid_count)),
        "ma20": _round4(_ratio(r.ma20_above_count, r.ma20_valid_count)),
        "ma50": _round4(_ratio(r.ma50_above_count, r.ma50_valid_count)),
        "ew_index": _round2(idx),
    }
    if with_ma120:
        point["ma120"] = _round4(_ratio(r.ma120_above_count, r.ma120_valid_count))
    return point


def build_market_view(rows: list[MarketDailyRow]) -> dict:
    """全市场视图：cards（最新）+ series（250 日，含 MA5/10/20/50/120 + EW index）。"""
    if not rows:
        return {
            "projection_trade_date": None,
            "cards": {
                "ma5": None,
                "ma20": None,
                "ma50": None,
                "equal_weight_index": None,
            },
            "series": [],
        }
    ew = _ew_index([r.equal_weight_return for r in rows])
    series = [_point(r, idx) for r, idx in zip(rows, ew, strict=True)]
    last = rows[-1]
    cards = {
        "ma5": _round4(_ratio(last.ma5_above_count, last.ma5_valid_count)),
        "ma20": _round4(_ratio(last.ma20_above_count, last.ma20_valid_count)),
        "ma50": _round4(_ratio(last.ma50_above_count, last.ma50_valid_count)),
        "equal_weight_index": _round2(ew[-1]),
    }
    return {
        "projection_trade_date": last.trade_date.isoformat(),
        "cards": cards,
        "series": series,
    }


def build_scope_view(rows: list[ScopeDailyRow], board: BoardMeta) -> dict | None:
    """单板块视图；无投影行返回 None（API 层转 404，不伪造 0）。"""
    if not rows:
        return None
    ew = _ew_index([r.equal_weight_return for r in rows])
    series = [_point(r, idx) for r, idx in zip(rows, ew, strict=True)]
    last = rows[-1]
    return {
        "projection_trade_date": last.trade_date.isoformat(),
        "metadata": {
            "board_id": str(board.board_id),
            "name": board.name,
            "type": board.type,
            "hierarchy_level": board.hierarchy_level,
            "membership_version": board.membership_version,
        },
        "series": series,
    }


def build_rankings(
    boards: list[BoardMeta],
    scoped: dict[UUID, list[ScopeDailyRow]],
    lookback: int = _RANKING_LOOKBACK,
    limit: int = 10,
) -> dict:
    """行业/概念 ranking。delta = 当前(t) 与 t-lookback 个交易日前(第 t-5) 的 MA 广度差。

    仅对具备完整历史（>= lookback+1 行）的板块计算 delta；历史不足的板块不参与排序。
    """
    items: list[dict] = []
    for b in boards:
        rs = scoped.get(b.board_id, [])
        if len(rs) < lookback + 1:
            continue
        cur = rs[-1]
        prev = rs[-(lookback + 1)]
        c_ma5 = _ratio(cur.ma5_above_count, cur.ma5_valid_count)
        c_ma10 = _ratio(cur.ma10_above_count, cur.ma10_valid_count)
        p_ma5 = _ratio(prev.ma5_above_count, prev.ma5_valid_count)
        p_ma10 = _ratio(prev.ma10_above_count, prev.ma10_valid_count)
        d_ma5 = (c_ma5 - p_ma5) if (c_ma5 is not None and p_ma5 is not None) else None
        d_ma10 = (c_ma10 - p_ma10) if (c_ma10 is not None and p_ma10 is not None) else None
        items.append(
            {
                "board_id": str(b.board_id),
                "board_name": b.name,
                "board_type": b.type,
                "hierarchy_level": b.hierarchy_level,
                "current": {"ma5": _round4(c_ma5), "ma10": _round4(c_ma10)},
                "previous": {"ma5": _round4(p_ma5), "ma10": _round4(p_ma10)},
                "delta": {"ma5": _round4(d_ma5), "ma10": _round4(d_ma10)},
            }
        )
    ranked = [i for i in items if i["delta"]["ma5"] is not None]
    ranked.sort(key=lambda i: i["delta"]["ma5"], reverse=True)
    top = ranked[:limit]
    bottom = list(reversed(ranked[-limit:]))
    return {"top": top, "bottom": bottom, "lookback": lookback}


def build_compare(
    boards: list[BoardMeta],
    scoped: dict[UUID, list[ScopeDailyRow]],
) -> list[dict]:
    """重点板块半月比较：每个 scope 的 EW return 链**独立**归一到 first=100。"""
    out: list[dict] = []
    for b in boards:
        rs = scoped.get(b.board_id, [])
        ew = _ew_index([r.equal_weight_return for r in rs])
        points = [
            {"trade_date": r.trade_date.isoformat(), "ew_index": _round2(idx)}
            for r, idx in zip(rs, ew, strict=True)
        ]
        out.append(
            {
                "board_id": str(b.board_id),
                "board_name": b.name,
                "board_type": b.type,
                "points": points,
            }
        )
    return out


# ---------------------------------------------------------------------------
# DB 读取（只读）
# ---------------------------------------------------------------------------
def _market_row(r: MarketDashboardMarketDaily) -> MarketDailyRow:
    return MarketDailyRow(
        trade_date=r.trade_date,
        ma5_above_count=r.ma5_above_count,
        ma5_valid_count=r.ma5_valid_count,
        ma10_above_count=r.ma10_above_count,
        ma10_valid_count=r.ma10_valid_count,
        ma20_above_count=r.ma20_above_count,
        ma20_valid_count=r.ma20_valid_count,
        ma50_above_count=r.ma50_above_count,
        ma50_valid_count=r.ma50_valid_count,
        ma120_above_count=r.ma120_above_count,
        ma120_valid_count=r.ma120_valid_count,
        equal_weight_return=r.equal_weight_return,
    )


def _scope_row(row) -> ScopeDailyRow:
    return ScopeDailyRow(
        board_id=row.board_id,
        trade_date=row.trade_date,
        membership_version=row.membership_version,
        ma5_above_count=row.ma5_above_count,
        ma5_valid_count=row.ma5_valid_count,
        ma10_above_count=row.ma10_above_count,
        ma10_valid_count=row.ma10_valid_count,
        ma20_above_count=row.ma20_above_count,
        ma20_valid_count=row.ma20_valid_count,
        ma50_above_count=row.ma50_above_count,
        ma50_valid_count=row.ma50_valid_count,
        ma120_above_count=row.ma120_above_count,
        ma120_valid_count=row.ma120_valid_count,
        equal_weight_return=row.equal_weight_return,
    )


async def _fetch_market_rows(db: AsyncSession, days: int) -> list[MarketDailyRow]:
    stmt = (
        select(MarketDashboardMarketDaily)
        .order_by(MarketDashboardMarketDaily.trade_date.asc())
        .limit(days)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [_market_row(r) for r in rows]


async def _fetch_boards(
    db: AsyncSession, scope_type: str, hierarchy_level: str | None
) -> list[BoardMeta]:
    stmt = select(
        MarketBoard.id,
        MarketBoard.name,
        MarketBoard.type,
        MarketBoard.hierarchyLevel,
        MarketBoard.membershipVersion,
        MarketBoard.isActive,
    ).where(MarketBoard.type == scope_type, MarketBoard.isActive.is_(True))
    if scope_type == "industry" and hierarchy_level:
        stmt = stmt.where(MarketBoard.hierarchyLevel == hierarchy_level)
    res = await db.execute(stmt)
    return [
        BoardMeta(
            board_id=row.id,
            name=row.name,
            type=row.type,
            hierarchy_level=row.hierarchyLevel,
            membership_version=row.membershipVersion,
            is_active=row.isActive,
        )
        for row in res.all()
    ]


async def _fetch_boards_by_ids(db: AsyncSession, board_ids: list[UUID]) -> list[BoardMeta]:
    stmt = select(
        MarketBoard.id,
        MarketBoard.name,
        MarketBoard.type,
        MarketBoard.hierarchyLevel,
        MarketBoard.membershipVersion,
        MarketBoard.isActive,
    ).where(MarketBoard.id.in_(board_ids))
    res = await db.execute(stmt)
    return [
        BoardMeta(
            board_id=row.id,
            name=row.name,
            type=row.type,
            hierarchy_level=row.hierarchyLevel,
            membership_version=row.membershipVersion,
            is_active=row.isActive,
        )
        for row in res.all()
    ]


async def _fetch_scope_window(
    db: AsyncSession, board_ids: list[UUID], n: int
) -> dict[UUID, list[ScopeDailyRow]]:
    """每个 board 取最近 n 个交易日的 scope 投影行（一个 SQL，窗口函数下推）。"""
    rn = (
        func.row_number()
        .over(
            partition_by=MarketDashboardScopeDaily.board_id,
            order_by=MarketDashboardScopeDaily.trade_date.desc(),
        )
        .label("rn")
    )
    sub = (
        select(MarketDashboardScopeDaily, rn).where(
            MarketDashboardScopeDaily.board_id.in_(board_ids)
        )
    ).subquery()
    stmt = select(sub).where(sub.c.rn <= n).order_by(sub.c.board_id, sub.c.trade_date.asc())
    grouped: dict[UUID, list[ScopeDailyRow]] = {}
    for row in (await db.execute(stmt)).all():
        grouped.setdefault(row.board_id, []).append(_scope_row(row))
    return grouped


async def _fetch_single_board(db: AsyncSession, board_id: UUID) -> BoardMeta | None:
    stmt = select(
        MarketBoard.id,
        MarketBoard.name,
        MarketBoard.type,
        MarketBoard.hierarchyLevel,
        MarketBoard.membershipVersion,
        MarketBoard.isActive,
    ).where(MarketBoard.id == board_id)
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    return BoardMeta(
        board_id=row.id,
        name=row.name,
        type=row.type,
        hierarchy_level=row.hierarchyLevel,
        membership_version=row.membershipVersion,
        is_active=row.isActive,
    )


# ---------------------------------------------------------------------------
# 公共读入口（返回 pydantic response 或 None）
# ---------------------------------------------------------------------------
async def get_market_dashboard(db: AsyncSession, days: int):
    from app.schemas.market_dashboard import MarketDashboardResponse

    rows = await _fetch_market_rows(db, days)
    if not rows:
        return None
    return MarketDashboardResponse(**build_market_view(rows))


async def get_rankings(
    db: AsyncSession,
    scope_type: str,
    hierarchy_level: str | None,
    lookback: int,
    limit: int,
):
    from app.schemas.market_dashboard import RankingsResponse

    boards = await _fetch_boards(db, scope_type, hierarchy_level)
    if not boards:
        return RankingsResponse(top=[], bottom=[], lookback=lookback)
    scoped = await _fetch_scope_window(db, [b.board_id for b in boards], lookback + 1)
    return RankingsResponse(**build_rankings(boards, scoped, lookback, limit))


async def get_scope_detail(db: AsyncSession, board_id: UUID, days: int):
    from app.schemas.market_dashboard import ScopeDetailResponse

    board = await _fetch_single_board(db, board_id)
    if board is None or not board.is_active:
        return None
    rows = await _fetch_scope_window(db, [board_id], days)
    view = build_scope_view(rows.get(board_id, []), board)
    if view is None:
        return None
    return ScopeDetailResponse(**view)


async def compare_boards(db: AsyncSession, board_ids: list[UUID], days: int):
    from app.schemas.market_dashboard import CompareResponse

    boards = await _fetch_boards_by_ids(db, board_ids)
    # 任一 board 不存在 / 未激活 → 整体不可用（API 层转 404）。
    if len(boards) != len(set(board_ids)) or any(not b.is_active for b in boards):
        return None
    scoped = await _fetch_scope_window(db, board_ids, days)
    return CompareResponse(boards=build_compare(boards, scoped))
