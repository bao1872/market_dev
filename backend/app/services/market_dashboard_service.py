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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

import pandas as pd
from sqlalchemy import desc, distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market_dashboard.breadth import (
    WINDOWS,
    BreadthResult,
    MemberCloses,
    WindowBreadth,
    compute_breadth,
)
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.repositories import bar_repository

# 全市场候选集合允许的市场（不含 status=active 硬过滤，避免把今天 active 倒灌历史）。
ALLOWED_MARKETS: tuple[str, ...] = ("SH", "SZ", "BJ")

# 单日横截面所需的最近市场交易日数量（覆盖 MA120 + 前一日价格需求）。
RECENT_TRADE_DAYS = 120

MEMBERSHIP_BASIS = "latest_snapshot_replay"

# Checkpoint C 历史窗口（探索版简单常量）。
HISTORY_TRADE_DAYS = 250
MA_WARMUP_DAYS = 119  # 额外加载此前 119 个市场交易日，保证显示首日可算 MA120
WIDTH_CHANGE_LAG = 5  # 板块 5 日宽度变化
WATCH_TRADE_DAYS = 10  # 关注板块半月归一化走势窗口


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


# ===========================================================================
# Checkpoint C — 历史情绪序列（一次批量加载后生成截图所需数据）
# ===========================================================================
#
# 结构（禁止 N+1 / 禁止每板块重算股票 MA）：
#   1 次 instrument 查询 → 1 次历史交易日查询 → 1 次 bars 批量
#   → 每只股票的历史 MA/return 只算一次（stock-day facts）
#   → 市场 / 板块 / 关注板块 复用同一份 stock-day facts 聚合
#
# 板块成员语义冻结为 latest MarketBoard + MarketBoardMembership snapshot，
# 所有历史日期统一使用当前最后一次成功 membership（latest_snapshot_replay），
# 不使用 BoardMembershipHistory / resolve_board_membership_at（非 PIT）。


@dataclass(frozen=True)
class MarketHistoryPoint:
    trade_date: date
    breadth: BreadthResult
    equal_weight_index: float | None


@dataclass(frozen=True)
class ScopeBreadthChange:
    scope_key: str
    scope_name: str
    scope_type: str
    hierarchy_level: str
    current_date: date
    previous_date: date
    ma5_current: float | None
    ma5_previous: float | None
    ma5_delta: float | None
    ma10_current: float | None
    ma10_previous: float | None
    ma10_delta: float | None


@dataclass(frozen=True)
class WatchScopePoint:
    trade_date: date
    equal_weight_return: float | None
    normalized_index: float | None


@dataclass(frozen=True)
class WatchScopeSeries:
    scope_key: str
    scope_name: str
    scope_type: str
    hierarchy_level: str
    points: list[WatchScopePoint]


@dataclass(frozen=True)
class DashboardHistory:
    membership_basis: str
    start_date: date
    end_date: date
    market_history: list[MarketHistoryPoint]
    scope_changes: list[ScopeBreadthChange]
    watch_series: list[WatchScopeSeries]


async def _query_recent_trade_dates(
    session: AsyncSession, end_date: date, count: int
) -> list[date]:
    """最近 count 个市场交易日（distinct trade_date <= end_date），升序返回。

    以 bars_daily 真实 distinct trade_date 为准，不使用 calendar 猜测。
    """
    rows = (
        await session.execute(
            select(distinct(BarDaily.trade_date))
            .where(BarDaily.trade_date <= end_date)
            .order_by(desc(BarDaily.trade_date))
            .limit(count)
        )
    ).all()
    dates = [r[0] for r in rows]
    return sorted(dates)


def _compute_stock_daily_facts(df: pd.DataFrame) -> pd.DataFrame:
    """对单只股票计算全部历史 stock-day facts（每只股票只调用一次）。

    沿用 adjusted_close_coordinate（raw_close * adj_factor），任何 close / adj_factor
    为 None/NaN/inf/<=0 即 unavailable，禁止 fallback。
    - ret: 该股票自身前一根实际 bar 的收益（C_t/C_{t-1}-1），不 calendar reindex
    - 每个窗口 k: ma_k（最近 k 个有效 adjusted close 均值），above_k（close_T > ma_k）
    停牌日没有实际 bar → 该日无 stock-day fact（不 forward fill）。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.sort_index().copy()
    df.index = pd.to_datetime(df.index).date  # 用 python date 索引，便于 exact-T 聚合
    df["adj_close"] = df.apply(
        lambda r: adjusted_close_coordinate(r["close"], r["adj_factor"]), axis=1
    )
    # fill_method=None：禁止 pct_change 对中间缺失/invalid 坐标做前向填充，
    # 保证 invalid 坐标不 fallback、也不跨无效前一 bar 计算收益。
    df["ret"] = df["adj_close"].pct_change(fill_method=None)
    for k in WINDOWS:
        ma = df["adj_close"].rolling(k, min_periods=k).mean()
        df[f"ma{k}"] = ma
        df[f"above{k}"] = (df["adj_close"] > ma).where(ma.notna())
    return df


def _aggregate_breadth_for_date(
    target_date: date,
    stock_facts: dict[UUID, pd.DataFrame],
    member_ids: list[UUID] | None = None,
) -> BreadthResult:
    """聚合某日的 scope 宽度，只纳入当日真实存在 stock-day fact 的股票（exact-T）。"""
    ids = member_ids if member_ids is not None else list(stock_facts.keys())
    valid_counts = dict.fromkeys(WINDOWS, 0)
    above_counts = dict.fromkeys(WINDOWS, 0)
    returns: list[float] = []
    member_count = 0
    for iid in ids:
        f = stock_facts.get(iid)
        if f is None or len(f) == 0:
            continue
        row = f[f.index == target_date]
        if len(row) == 0:
            continue  # exact-T：当日无真实 bar，不进入分母、不产生收益
        member_count += 1
        r = row.iloc[0]
        for k in WINDOWS:
            av = r[f"above{k}"]
            if pd.notna(av):
                valid_counts[k] += 1
                if bool(av):
                    above_counts[k] += 1
        ret = r["ret"]
        if pd.notna(ret):
            returns.append(float(ret))
    windows = {
        k: (WindowBreadth(k, valid_counts[k], above_counts[k],
                          (above_counts[k] / valid_counts[k]) if valid_counts[k] else None))
        for k in WINDOWS
    }
    ewr = (sum(returns) / len(returns)) if returns else None
    return BreadthResult(
        member_count=member_count,
        valid_return_count=len(returns),
        equal_weight_return=ewr,
        windows=windows,
    )


def _delta(a: float | None, b: float | None) -> float | None:
    return a - b if (a is not None and b is not None) else None


def _rebase_index(points: list[tuple[float | None]]) -> list[float | None]:
    """从等权日收益序列累计归一化指数。

    首点=100（若当日收益有效）；之后严格链式 prev*(1+r)。
    None 不 fallback 0；一旦链路中断（prev 或当日收益为 None），后续恒为 None（fail-closed）。
    """
    result: list[float | None] = []
    prev: float | None = None
    first = True
    for (ewr,) in points:
        if first:
            idx = 100.0 if ewr is not None else None
        else:
            idx = prev * (1 + ewr) if (prev is not None and ewr is not None) else None
        result.append(idx)
        prev = idx
        first = False
    return result


async def build_dashboard_history(
    session: AsyncSession,
    end_date: date,
    watch_board_ids: Sequence[UUID] = (),
) -> DashboardHistory:
    """生成截图所需的 Dashboard 历史数据（一次批量加载，stock facts 只算一次）。"""
    instrument_ids = await _query_market_instrument_ids(session)
    load_dates = await _query_recent_trade_dates(
        session, end_date, HISTORY_TRADE_DAYS + MA_WARMUP_DAYS
    )
    if not load_dates:
        return DashboardHistory(
            membership_basis=MEMBERSHIP_BASIS,
            start_date=end_date, end_date=end_date,
            market_history=[], scope_changes=[], watch_series=[],
        )
    display_dates = load_dates[-HISTORY_TRADE_DAYS:]

    # 唯一一次批量 bars 读取
    bars = await bar_repository.get_daily_bars_batch(
        session, instrument_ids, load_dates[0], load_dates[-1]
    )
    # 每只股票历史事实只计算一次
    stock_facts: dict[UUID, pd.DataFrame] = {
        iid: _compute_stock_daily_facts(df)
        for iid, df in bars.items()
        if df is not None and len(df) > 0
    }

    # ① + ② 市场历史曲线 + 全市场等权指数
    market_history: list[MarketHistoryPoint] = []
    prev_index: float | None = None
    first_date = True
    for d in display_dates:
        br = _aggregate_breadth_for_date(d, stock_facts)
        ewr = br.equal_weight_return
        if first_date:
            idx = 100.0 if ewr is not None else None
        else:
            idx = prev_index * (1 + ewr) if (prev_index is not None and ewr is not None) else None
        market_history.append(
            MarketHistoryPoint(trade_date=d, breadth=br, equal_weight_index=idx)
        )
        prev_index = idx
        first_date = False

    # ③ 板块 5 日宽度变化（仅 T 与 T-5 两个截面）
    boards = await _query_active_boards(session)
    memberships = await _query_board_memberships(session, [b.id for b in boards])
    scope_changes: list[ScopeBreadthChange] = []
    if len(display_dates) >= WIDTH_CHANGE_LAG + 1:
        t_now = display_dates[-1]
        t_prev = display_dates[-(WIDTH_CHANGE_LAG + 1)]
        for board in boards:
            mids = memberships.get(board.id, [])
            b_now = _aggregate_breadth_for_date(t_now, stock_facts, mids)
            b_prev = _aggregate_breadth_for_date(t_prev, stock_facts, mids)
            scope_changes.append(
                ScopeBreadthChange(
                    scope_key=str(board.id),
                    scope_name=board.name,
                    scope_type=board.type,
                    hierarchy_level=board.hierarchyLevel,
                    current_date=t_now,
                    previous_date=t_prev,
                    ma5_current=b_now.windows[5].ratio,
                    ma5_previous=b_prev.windows[5].ratio,
                    ma5_delta=_delta(b_now.windows[5].ratio, b_prev.windows[5].ratio),
                    ma10_current=b_now.windows[10].ratio,
                    ma10_previous=b_prev.windows[10].ratio,
                    ma10_delta=_delta(b_now.windows[10].ratio, b_prev.windows[10].ratio),
                )
            )

    # ④ 关注板块最近约半月归一化走势（仅用户指定的当前 active board）
    watch_dates = display_dates[-WATCH_TRADE_DAYS:]
    board_by_id = {b.id: b for b in boards}
    watch_series: list[WatchScopeSeries] = []
    for wid in watch_board_ids:
        board = board_by_id.get(wid)
        if board is None:
            continue
        mids = memberships.get(board.id, [])
        ewrs = [
            (_aggregate_breadth_for_date(d, stock_facts, mids).equal_weight_return,)
            for d in watch_dates
        ]
        indices = _rebase_index(ewrs)
        watch_series.append(
            WatchScopeSeries(
                scope_key=str(board.id),
                scope_name=board.name,
                scope_type=board.type,
                hierarchy_level=board.hierarchyLevel,
                points=[
                    WatchScopePoint(
                        trade_date=d, equal_weight_return=ewr[0], normalized_index=idx
                    )
                    for d, ewr, idx in zip(watch_dates, ewrs, indices, strict=False)
                ],
            )
        )

    return DashboardHistory(
        membership_basis=MEMBERSHIP_BASIS,
        start_date=display_dates[0],
        end_date=display_dates[-1],
        market_history=market_history,
        scope_changes=scope_changes,
        watch_series=watch_series,
    )
