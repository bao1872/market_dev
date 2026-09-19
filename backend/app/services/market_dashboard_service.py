"""Market Dashboard Checkpoint B — 真实数据接入 + 单交易日横截面计算。

给定目标交易日 T，一次批量读取真实数据，计算：
① 全 A 股市场：MA5/10/20/50/120 线上占比 + equal_weight_return
② 当前最新板块（industry L1/L2/L3 + concept）的同一组指标

探索阶段：
- 不接前端、不建表、不做历史时间序列、不做排名/5日变化/归一化曲线。
- 板块成员语义冻结为 latest MarketBoard + MarketBoardMembership snapshot，
  不使用 BoardMembershipHistory / resolve_board_membership_at（非 PIT）。
- qfq 坐标 = raw_close * adj_factor（标准 qfq 的 anchor_factor 在
  Close>MA 与 Close_T/Close_{T-1}-1 中抵消，数学等价，非近似）。

性能边界（禁止 N+1）：
- 1 次市场 instrument 查询
- 1 次最近交易日查询
- 1 次 get_daily_bars_batch
- 最多 2 次板块/成员查询
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

import pandas as pd
from sqlalchemy import desc, distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market_dashboard.breadth import BreadthResult, MemberCloses, compute_breadth
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.repositories import bar_repository

# 全市场候选集合允许的市场（不含 status=active 硬过滤，避免把今天 active 倒灌历史）。
ALLOWED_MARKETS: tuple[str, ...] = ("SH", "SZ", "BJ")

# 单日横截面所需的最近市场交易日数量（覆盖 MA120 + 前一日价格需求）。
RECENT_TRADE_DAYS = 120

MEMBERSHIP_BASIS = "latest_snapshot_replay"


def adjusted_close_coordinate(close, adj_factor) -> float | None:
    """qfq 坐标 = raw_close * adj_factor。

    close 与 adj_factor 必须各自：非 None、finite、严格 > 0；任一不满足返回 None。
    严禁 adj_factor 缺失时 fallback 1.0。
    """
    def _ok(v) -> bool:
        if v is None:
            return False
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False
        return fv > 0 and fv == fv and fv not in (float("inf"), float("-inf"))

    if not (_ok(close) and _ok(adj_factor)):
        return None
    return float(close) * float(adj_factor)


def build_member_closes(trade_date: date, df: pd.DataFrame) -> MemberCloses | None:
    """从一只股票的 bars DataFrame 构造 exact-T 的 MemberCloses。

    Exact-T 硬合同：若最后一根 bar 的交易日 != trade_date，返回 None，
    该股票不进入 T 日任何 MA denominator，也不产生 T 日收益。
    （不得把 < T 的旧 close 冒充 T。）
    """
    if df is None or len(df) == 0:
        return None
    df = df.sort_index()
    last_date = df.index[-1]
    if isinstance(last_date, datetime):
        last_date = last_date.date()
    if last_date != trade_date:
        return None

    closes: list[float | None] = []
    close_col = df["close"] if "close" in df.columns else None
    factor_col = df["adj_factor"] if "adj_factor" in df.columns else None
    if close_col is None or factor_col is None:
        return None
    for c, f in zip(close_col.tolist(), factor_col.tolist(), strict=False):
        closes.append(adjusted_close_coordinate(c, f))
    return MemberCloses(member_id="", closes=closes)


@dataclass(frozen=True)
class DashboardScopeSnapshot:
    scope_type: str
    scope_key: str
    scope_name: str
    hierarchy_level: str
    breadth: BreadthResult


@dataclass(frozen=True)
class DashboardDailySnapshot:
    trade_date: date
    membership_basis: str
    market: DashboardScopeSnapshot
    scopes: list[DashboardScopeSnapshot]


# ---------------------------------------------------------------------------
# 数据加载 helper（均最小化查询；测试可整体 monkeypatch，无需真实 DB）
# ---------------------------------------------------------------------------

async def _query_market_instrument_ids(session: AsyncSession) -> list[UUID]:
    """全市场候选集合：SH/SZ/BJ。不使用 status=active 作为硬过滤。"""
    rows = (
        await session.execute(
            select(Instrument.id).where(Instrument.market.in_(ALLOWED_MARKETS))
        )
    ).all()
    return [r[0] for r in rows]


async def _query_recent_start_date(session: AsyncSession, trade_date: date) -> date:
    """最近 120 个市场交易日中最早的一个（作为批量 bars 的 start_date）。"""
    rows = (
        await session.execute(
            select(distinct(BarDaily.trade_date))
            .where(BarDaily.trade_date <= trade_date)
            .order_by(desc(BarDaily.trade_date))
            .limit(RECENT_TRADE_DAYS)
        )
    ).all()
    dates = [r[0] for r in rows]
    if not dates:
        return trade_date
    return min(dates)


async def _query_active_boards(session: AsyncSession) -> list[MarketBoard]:
    """当前最新板块快照：只取 isActive 的 industry/concept 板块。"""
    return list(
        (
            await session.execute(
                select(MarketBoard).where(MarketBoard.isActive.is_(True))
            )
        ).scalars().all()
    )


async def _query_board_memberships(
    session: AsyncSession, board_ids: list[UUID]
) -> dict[UUID, list[UUID]]:
    """一次性查询所有板块成员关系，返回 board_id -> [instrument_id]。"""
    result: dict[UUID, list[UUID]] = {bid: [] for bid in board_ids}
    if not board_ids:
        return result
    rows = (
        await session.execute(
            select(MarketBoardMembership.boardId, MarketBoardMembership.instrumentId)
            .where(MarketBoardMembership.boardId.in_(board_ids))
        )
    ).all()
    for board_id, instrument_id in rows:
        result.setdefault(board_id, []).append(instrument_id)
    return result


async def build_daily_dashboard_snapshot(
    session: AsyncSession,
    trade_date: date,
) -> DashboardDailySnapshot:
    """计算 T 日全市场 + 各板块的横截面宽度。

    结构：1 次 instrument 查询 → 1 次交易日查询 → 1 次 bars 批量 →
    复用同一份 member 映射 → 全市场 / 各板块共用（不重复查 bars）。
    """
    t = trade_date
    instrument_ids = await _query_market_instrument_ids(session)
    start_date = await _query_recent_start_date(session, t)

    # 唯一一次批量 bars 读取（已含 close + adj_factor）
    bars = await bar_repository.get_daily_bars_batch(session, instrument_ids, start_date, t)

    # 构造 exact-T 的 member 映射（非 exact-T 成员直接剔除）
    member_map: dict[UUID, MemberCloses] = {}
    for iid, df in bars.items():
        mc = build_member_closes(t, df)
        if mc is not None:
            member_map[iid] = mc

    # 全市场宽度
    market_breadth = compute_breadth(list(member_map.values()))
    market_snapshot = DashboardScopeSnapshot(
        scope_type="market",
        scope_key="ALL",
        scope_name="全市场",
        hierarchy_level="ALL",
        breadth=market_breadth,
    )

    # 各板块宽度（复用同一份 member_map，不重复查 bars）
    boards = await _query_active_boards(session)
    memberships = await _query_board_memberships(session, [b.id for b in boards])
    scopes: list[DashboardScopeSnapshot] = []
    for board in boards:
        bm_ids = memberships.get(board.id, [])
        scope_members = [member_map[iid] for iid in bm_ids if iid in member_map]
        scope_breadth = compute_breadth(scope_members)
        scopes.append(
            DashboardScopeSnapshot(
                scope_type=board.type,
                scope_key=str(board.id),
                scope_name=board.name,
                hierarchy_level=board.hierarchyLevel,
                breadth=scope_breadth,
            )
        )

    return DashboardDailySnapshot(
        trade_date=t,
        membership_basis=MEMBERSHIP_BASIS,
        market=market_snapshot,
        scopes=scopes,
    )
