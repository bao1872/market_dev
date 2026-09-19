"""Market Dashboard 宽度（breadth）数学核心 — 纯函数，无数据库 / 无 IO。

本模块是 Dashboard 四张图的共同数学基础：
① 中长周期宽度（MA20/50/120 线上占比）+ 全市场等权指数
② 短期宽度（MA5/10 线上占比）
③ 行业/概念宽度 5 日变化排名
④ 关注板块归一化走势（等权日收益累计，起点=100）

冻结合同（不可破坏）：
- 窗口固定 5/10/20/50/120，各窗口分母独立。
- 单窗口可用性 = 最近 k 个 close 全部非 None、finite、> 0。
- close_T == MA_k 不算 above（严格大于）。
- NULL / NaN / inf / 数据不足一律不可用，绝不当作 0 进入分母。
- valid_count == 0 时 ratio 为 None（不是 0）。
- equal_weight_return 只用最后两个有效 close；无有效成员时为 None。
- 纯计算层不查询 BarDaily / Instrument / MarketBoard / membership / 交易日历。
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

WINDOWS: tuple[int, ...] = (5, 10, 20, 50, 120)


def _valid_price(value: float | None) -> bool:
    """价格可用性：非 None、数值有限、严格为正。NaN/inf/非数值均不可用。"""
    if value is None:
        return False
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(fv) and fv > 0


@dataclass(frozen=True)
class MemberCloses:
    """单个成员的有序 qfq completed 日线收盘价（升序，最后一个元素代表 T）。

    允许长度不足 MA120；允许元素为 None / NaN / inf —— 由本核心判定可用性。
    """

    member_id: str
    closes: Sequence[float | None]


@dataclass(frozen=True)
class WindowBreadth:
    """单个 MA 窗口的 scope 宽度。ratio = above_count / valid_count。"""

    window: int
    valid_count: int
    above_count: int
    ratio: float | None


@dataclass(frozen=True)
class BreadthResult:
    """scope 级输出（最小字段集）。"""

    member_count: int
    valid_return_count: int
    equal_weight_return: float | None
    windows: dict[int, WindowBreadth]


def _member_window_valid(closes: Sequence[float | None], window: int) -> bool:
    """成员在窗口 k 可用：至少 k 个 close，且最近 k 个全部有效。"""
    if len(closes) < window:
        return False
    return all(_valid_price(c) for c in closes[-window:])


def _member_above(closes: Sequence[float | None], window: int) -> bool:
    """close_T 严格大于最近 k 个有效 close 的简单平均（调用前须已确认窗口可用）。"""
    tail = [float(c) for c in closes[-window:]]
    ma = sum(tail) / window
    return tail[-1] > ma


def _member_return(closes: Sequence[float | None]) -> float | None:
    """成员 T 日收益 r = C_T / C_{T-1} - 1；最后两个 close 任一无效则 None。"""
    if len(closes) < 2:
        return None
    if not (_valid_price(closes[-1]) and _valid_price(closes[-2])):
        return None
    return float(closes[-1]) / float(closes[-2]) - 1.0


def compute_breadth(members: Sequence[MemberCloses]) -> BreadthResult:
    """对一组成员计算五个 MA 窗口的线上占比与 scope 等权日收益。

    各窗口分母独立：成员能算 MA5 但不能算 MA120 时，只进入 MA5 分母。
    """
    member_list = list(members)

    windows: dict[int, WindowBreadth] = {}
    for window in WINDOWS:
        valid_count = 0
        above_count = 0
        for member in member_list:
            if _member_window_valid(member.closes, window):
                valid_count += 1
                if _member_above(member.closes, window):
                    above_count += 1
        windows[window] = WindowBreadth(
            window=window,
            valid_count=valid_count,
            above_count=above_count,
            ratio=(above_count / valid_count) if valid_count else None,
        )

    returns = [r for m in member_list if (r := _member_return(m.closes)) is not None]
    equal_weight_return = (sum(returns) / len(returns)) if returns else None

    return BreadthResult(
        member_count=len(member_list),
        valid_return_count=len(returns),
        equal_weight_return=equal_weight_return,
        windows=windows,
    )
