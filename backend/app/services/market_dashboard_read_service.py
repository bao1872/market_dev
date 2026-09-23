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
from app.services.market_dashboard_scope_explorer_service import (
    T_WINDOW,
    resolve_t_prev,
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
    # [PANJI-MARKET-OVERVIEW] 快照/轨迹 additive 字段（DB 可空；缺则 None）
    advance_count: int | None = None
    decline_count: int | None = None
    flat_count: int | None = None
    change_valid_count: int | None = None
    turnover_amount: float | None = None
    turnover_valid_count: int | None = None
    limit_up_count: int | None = None
    limit_down_count: int | None = None
    sse_close: float | None = None
    szse_close: float | None = None
    chinext_close: float | None = None


@dataclass
class ScopeDailyRow:
    board_id: UUID
    trade_date: date
    membership_version: str
    # [R3C0] 该 projection row 冻结的成分数（与 breadth / membership_version 同一 projection owner）；
    # 绝不来自 market_board_memberships / instruments / bars_daily。
    member_count: int
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
    """等权指数链（与 F1A _rebase_index 同一合同）。

    - 第一个 displayed date：equal_weight_return 有效 -> base（100）；为 None -> None；
    - 之后：仅当 prev 与当前 return 都有效 -> prev * (1 + r)；否则 -> None；
    - 一旦某点为 None（首个或任意后续 displayed date），其后恒为 None
      （不 forward fill、不在 null 之后重新 rebasing）。
    """
    out: list[float | None] = []
    prev: float | None = None
    for i, r in enumerate(returns):
        if i == 0:
            # 首个 displayed date：有效 -> base，无效（None）-> None
            idx = base if r is not None else None
        else:
            # 仅当 prev 与当前 return 都有效才接链；否则 None（含前导 None 后恒 None）
            idx = prev * (1.0 + r) if (prev is not None and r is not None) else None
        out.append(idx)
        prev = idx
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


def _market_point(r: MarketDailyRow, idx, *, with_ma120: bool = True) -> dict:
    """[PANJI-MARKET-OVERVIEW] 市场轨迹点：在核心 MA/EW 基础上叠加快照/轨迹 additive 字段。

    与 _point 分离：scope/compare 视图复用 _point（其 row/schema 不含 overview 字段）。
    """
    point = _point(r, idx, with_ma120=with_ma120)
    point.update(
        {
            "sse_close": _round2(r.sse_close),
            "szse_close": _round2(r.szse_close),
            "chinext_close": _round2(r.chinext_close),
            "advance_count": r.advance_count,
            "decline_count": r.decline_count,
            "flat_count": r.flat_count,
            "turnover_amount": r.turnover_amount,
            "limit_up_count": r.limit_up_count,
            "limit_down_count": r.limit_down_count,
        }
    )
    return point


def build_market_view(rows: list[MarketDailyRow]) -> dict:
    """全市场视图：cards（最新）+ series（250 日，含 MA5/10/20/50/120 + EW index + [PANJI-MARKET-OVERVIEW] 快照/轨迹）。"""
    if not rows:
        return {
            "projection_trade_date": None,
            "cards": {
                "ma5": None,
                "ma20": None,
                "ma50": None,
                "equal_weight_index": None,
                # [PANJI-MARKET-OVERVIEW] 空态所有新字段均为 None（不伪造 0）
                "sse_close": None, "sse_change_pct": None,
                "szse_close": None, "szse_change_pct": None,
                "chinext_close": None, "chinext_change_pct": None,
                "advance_count": None, "decline_count": None, "flat_count": None,
                "turnover_amount": None, "limit_up_count": None, "limit_down_count": None,
            },
            "series": [],
        }
    ew = _ew_index([r.equal_weight_return for r in rows])
    series = [_market_point(r, idx) for r, idx in zip(rows, ew, strict=True)]

    # [PANJI-MARKET-OVERVIEW] 指数 rebasing（首有效显示点=100；分别归一；read-time 派生，不持久化）
    for raw_key, rebased_key in (
        ("sse_close", "sse_rebased"),
        ("szse_close", "szse_rebased"),
        ("chinext_close", "chinext_rebased"),
    ):
        first = next((p[raw_key] for p in series if p.get(raw_key) is not None), None)
        for p in series:
            raw = p.get(raw_key)
            p[rebased_key] = (raw / first * 100.0) if (first is not None and raw is not None) else None

    last = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None

    def _chg(cur, before):
        if cur is None or before is None or before == 0:
            return None
        return round(cur / before - 1.0, 4)

    cards = {
        "ma5": _round4(_ratio(last.ma5_above_count, last.ma5_valid_count)),
        "ma20": _round4(_ratio(last.ma20_above_count, last.ma20_valid_count)),
        "ma50": _round4(_ratio(last.ma50_above_count, last.ma50_valid_count)),
        "equal_weight_index": _round2(ew[-1]),
        # [PANJI-MARKET-OVERVIEW] 快照 6 卡
        "sse_close": _round2(last.sse_close),
        "sse_change_pct": _chg(last.sse_close, prev.sse_close if prev else None),
        "szse_close": _round2(last.szse_close),
        "szse_change_pct": _chg(last.szse_close, prev.szse_close if prev else None),
        "chinext_close": _round2(last.chinext_close),
        "chinext_change_pct": _chg(last.chinext_close, prev.chinext_close if prev else None),
        "advance_count": last.advance_count,
        "decline_count": last.decline_count,
        "flat_count": last.flat_count,
        "turnover_amount": last.turnover_amount,
        "limit_up_count": last.limit_up_count,
        "limit_down_count": last.limit_down_count,
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
            # [R3C0] 取**最新 projection row** 的 member_count（rows[-1]），
            # 不从 MarketBoard / membership 表推算（早期 row 绝不覆盖 latest）。
            "member_count": last.member_count,
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
    """重点板块半月比较：每个 scope 的 EW return 链**独立**归一到 first=100。

    仅负责 chart points；比较矩阵由 build_compare_snapshot 独立计算（逻辑分离）。
    """
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


def resolve_compare_dates(
    market_dates: list[date],
) -> tuple[date | None, date | None]:
    """比较矩阵全局 T / T-5 —— 与 R2 Scope Explorer **同一** resolve_t_prev 实现。

    - 无 projection → (None, None)
    - < T_WINDOW 个日期 → (latest, None)：previous_trade_date=None，所有 delta=None
    - >= T_WINDOW → (latest, T-5 trading date)
    所有 basket board 共用 SAME T / SAME PREV（禁止 per-board 滑窗 / nearest available / 自然日-5）。
    """
    return resolve_t_prev(market_dates)


def build_compare_snapshot(
    cur: ScopeDailyRow | None,
    prev: ScopeDailyRow | None,
) -> dict:
    """[R3D0] 比较矩阵快照（精确 T / T-5 全局口径，纯函数）。

    - current(T) 缺失 → 全部 MA / member_count / delta = None（但 points 仍由 build_compare 提供）。
    - previous(T-5) 缺失 → delta = None（**绝不 fallback**）。
    - ratio = above_count / valid_count；valid_count <= 0 → None（**禁止伪造 0%**）。
    - delta = ratio(T) - ratio(PREV)；任一端 ratio 为 None → delta = None。
    """
    if cur is None:
        return {
            "member_count": None,
            "ma5": None,
            "ma10": None,
            "ma20": None,
            "ma50": None,
            "ma120": None,
            "ma5_delta": None,
            "ma10_delta": None,
        }
    c_ma5 = _ratio(cur.ma5_above_count, cur.ma5_valid_count)
    c_ma10 = _ratio(cur.ma10_above_count, cur.ma10_valid_count)
    c_ma20 = _ratio(cur.ma20_above_count, cur.ma20_valid_count)
    c_ma50 = _ratio(cur.ma50_above_count, cur.ma50_valid_count)
    c_ma120 = _ratio(cur.ma120_above_count, cur.ma120_valid_count)
    if prev is None:
        d_ma5 = d_ma10 = None
    else:
        p_ma5 = _ratio(prev.ma5_above_count, prev.ma5_valid_count)
        p_ma10 = _ratio(prev.ma10_above_count, prev.ma10_valid_count)
        d_ma5 = (c_ma5 - p_ma5) if (c_ma5 is not None and p_ma5 is not None) else None
        d_ma10 = (c_ma10 - p_ma10) if (c_ma10 is not None and p_ma10 is not None) else None
    return {
        "member_count": cur.member_count,
        "ma5": _round4(c_ma5),
        "ma10": _round4(c_ma10),
        "ma20": _round4(c_ma20),
        "ma50": _round4(c_ma50),
        "ma120": _round4(c_ma120),
        "ma5_delta": _round4(d_ma5),
        "ma10_delta": _round4(d_ma10),
    }


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
        # [PANJI-MARKET-OVERVIEW]
        advance_count=r.advance_count,
        decline_count=r.decline_count,
        flat_count=r.flat_count,
        change_valid_count=r.change_valid_count,
        turnover_amount=r.turnover_amount,
        turnover_valid_count=r.turnover_valid_count,
        limit_up_count=r.limit_up_count,
        limit_down_count=r.limit_down_count,
        sse_close=r.sse_close,
        szse_close=r.szse_close,
        chinext_close=r.chinext_close,
    )


def _scope_row(row) -> ScopeDailyRow:
    return ScopeDailyRow(
        board_id=row.board_id,
        trade_date=row.trade_date,
        membership_version=row.membership_version,
        member_count=row.member_count,
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
    # 取最近 days 天：按 trade_date 倒序取前 days 行，再反转回时间升序，
    # 保证最终 DTO 的 series 仍按时间递增（一个 SQL，不额外往返）。
    stmt = (
        select(MarketDashboardMarketDaily)
        .order_by(MarketDashboardMarketDaily.trade_date.desc())
        .limit(days)
    )
    rows = (await db.execute(stmt)).scalars().all()
    rows = list(reversed(rows))
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


async def _fetch_market_dates(db: AsyncSession, limit: int) -> list[date]:
    """全市场 projection 交易日历（DESC，取最近 limit 个）；用于解析全局 T / T-5。"""
    stmt = (
        select(MarketDashboardMarketDaily.trade_date)
        .order_by(MarketDashboardMarketDaily.trade_date.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _fetch_compare_snapshot_rows(
    db: AsyncSession, board_ids: list[UUID], t: date | None, prev: date | None
) -> dict[UUID, dict[str, ScopeDailyRow]]:
    """精确 T / PREV 当前/前期 projection 行（一个 SQL；t=None 时整体不可用）。

    返回 snapshot[board_id] = {"current": <T 行>, "previous": <PREV 行>}，缺失项不写入（None）。
    禁止 per-board 查询；禁止向更老日期 fallback。
    """
    if t is None:
        return {}
    dates = [t, prev] if prev is not None else [t]
    stmt = select(MarketDashboardScopeDaily).where(
        MarketDashboardScopeDaily.board_id.in_(board_ids),
        MarketDashboardScopeDaily.trade_date.in_(dates),
    )
    snap: dict[UUID, dict[str, ScopeDailyRow]] = {}
    for row in (await db.execute(stmt)).scalars().all():
        entry = snap.setdefault(row.board_id, {})
        if row.trade_date == t:
            entry["current"] = _scope_row(row)
        elif prev is not None and row.trade_date == prev:
            entry["previous"] = _scope_row(row)
    return snap


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
    # [R3D0-N] 响应顺序严格按请求 board_ids 顺序（basket 已禁止 duplicate，不重做去重语义）。
    board_by_id = {b.board_id: b for b in boards}
    ordered = [board_by_id[i] for i in board_ids]

    # 全局 T / T-5（与 R2 Explorer 完全同语义；所有 board 共用同一 T/PREV）。
    market_dates = await _fetch_market_dates(db, T_WINDOW)
    t, prev = resolve_compare_dates(market_dates)

    scoped = await _fetch_scope_window(db, board_ids, days)  # Q2 chart window
    snapshot = await _fetch_compare_snapshot_rows(db, board_ids, t, prev)  # Q4 精确 T/PREV 行

    ew_boards = build_compare(ordered, scoped)  # chart points（独立 rebasing）
    out: list[dict] = []
    for ew in ew_boards:
        bid = UUID(ew["board_id"])
        snap = snapshot.get(bid, {})
        # 矩阵与 chart 逻辑分离：snapshot 仅补 matrix 字段，不动 points。
        ew.update(build_compare_snapshot(snap.get("current"), snap.get("previous")))
        out.append(ew)
    return CompareResponse(
        projection_trade_date=t.isoformat() if t else None,
        previous_trade_date=prev.isoformat() if prev else None,
        boards=out,
    )
