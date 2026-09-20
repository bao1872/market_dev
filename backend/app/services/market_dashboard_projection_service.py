"""Market Dashboard P1-F1A — projection 计算层（只计算，绝不写 DB）。

输入：现有 ``bars_daily`` + 当前 ``MarketBoard`` / ``MarketBoardMembership``
（membership_basis = latest_snapshot_replay）。
输出：与 095 两张 projection table 一一对应的轻量 records（dict-like）：
- ``build_market_records``  每个 display date 恰好一行（list，最多 250）
- ``iter_scope_record_chunks``  按日期 chunk 产出 scope records（generator）

冻结合同：
- 复用已通过 E1A/E1B 验证的现有 primitives（``_compute_stock_facts_long`` /
  ``_aggregate_market_history_long`` / ``_build_membership_long`` /
  ``_aggregate_scope_breadth_long`` / ``_empty_market_breadth``），**不复制 breadth 数学**。
- 本轮**不写 projection DB**：无 INSERT/DELETE/UPSERT/commit，无 scheduler/API/frontend。
- 不累计巨型 list：market 是小 list；scope 以 chunk generator 产出，消费完即释放。
- 完整性合同：每个 active board × 每个 display date 必有一行（空为显式零，不是缺行）。
- 不输出 ratio / equal_weight_index / delta / scope name|type|level / updated_at；
  ``updated_at`` 由数据库 server_default 负责。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.market_dashboard.breadth import WINDOWS, BreadthResult
from app.repositories import bar_repository
from app.services import market_dashboard_service as dashboard_service

# scope projection 每次 JOIN 的日期上限。
# E2 真实 smoke 证明 all-board × 250 一次 JOIN 峰值 RSS ≈ 1.84 GiB；
# 冻结为按日期分块（all active boards × <= 10 dates / 次）。
SCOPE_PROJECTION_DATE_CHUNK = 10


@dataclass(frozen=True)
class ProjectionContext:
    """projection 计算所需的已加载/已计算上下文（不含任何写库能力）。"""

    projection_trade_date: date
    display_dates: list[date]
    stock_facts: pd.DataFrame
    membership_long: pd.DataFrame
    board_ids: list[UUID]
    membership_versions: dict[UUID, str]


def _none_if_nan(value: float | None) -> float | None:
    """NaN / None → None；否则 float（供 DOUBLE PRECISION 写入）。"""
    if value is None:
        return None
    fv = float(value)
    return None if fv != fv else fv  # NaN -> None


def _count_fields(breadth: BreadthResult) -> dict[str, object]:
    """把 BreadthResult 展平成 095 两张表共用的计数字段（无 ratio / index / delta）。"""
    fields: dict[str, object] = {
        "member_count": int(breadth.member_count),
        "valid_return_count": int(breadth.valid_return_count),
        "equal_weight_return": _none_if_nan(breadth.equal_weight_return),
    }
    for k in WINDOWS:
        w = breadth.windows[k]
        fields[f"ma{k}_valid_count"] = int(w.valid_count)
        fields[f"ma{k}_above_count"] = int(w.above_count)
    return fields


async def prepare_projection_context(session: AsyncSession, end_date: date) -> ProjectionContext:
    """一次批量加载 bars + 当前 membership，供 market/scope records 复用。

    固定查询/计算次数（禁止 chunk 重查 DB / 重算 MA）：
    instrument=1, trade-dates=1, bars=1, stock-facts=1, boards=1, memberships=1,
    membership_long=1。
    ``end_date`` 即使是非交易日，也取 ``<= end_date`` 的最后真实交易日作为 projection T。
    """
    instrument_ids = await dashboard_service._query_market_instrument_ids(session)
    load_dates = await dashboard_service._query_recent_trade_dates(
        session,
        end_date,
        dashboard_service.HISTORY_TRADE_DAYS + dashboard_service.MA_WARMUP_DAYS,
    )
    if not load_dates:
        # fail closed：没有真实交易日就没有 projection T；不得用请求日期伪造 T 继续。
        # 一旦 F1B 接上持久化，silent empty 会变成"替换旧 projection 为空"，必须当场拒绝。
        raise ValueError("cannot build market dashboard projection: no trading dates <= end_date")
    display_dates = load_dates[-dashboard_service.HISTORY_TRADE_DAYS :]

    # 唯一一次批量 bars 读取（Dashboard 专用窄读取：仅 4 列长表）
    raw_long = await bar_repository.get_dashboard_daily_facts_source(
        session, instrument_ids, load_dates[0], load_dates[-1]
    )
    # stock-day facts 整批向量化一次；生成后 projection 不再需要 raw_long
    stock_facts = dashboard_service._compute_stock_facts_long(raw_long)
    del raw_long

    boards = await dashboard_service._query_active_boards(session)
    memberships = await dashboard_service._query_board_memberships(session, [b.id for b in boards])
    membership_long = dashboard_service._build_membership_long(memberships)

    return ProjectionContext(
        projection_trade_date=display_dates[-1],
        display_dates=display_dates,
        stock_facts=stock_facts,
        membership_long=membership_long,
        board_ids=[b.id for b in boards],
        membership_versions={b.id: b.membershipVersion for b in boards},
    )


def build_market_records(context: ProjectionContext) -> list[dict[str, object]]:
    """每个 display date 恰好一条 market record（空日期 / 空 facts 也显式零）。

    projection completeness：输出行数恒等于 ``len(context.display_dates)``（最多 250）。
    被复用的 ``_aggregate_market_history_long`` 在 stock_facts 为空时返回 ``[]``，
    故此处必须按 display_dates 逐日补 ``_empty_market_breadth``（不改其既有行为合同）。
    """
    aggregated: dict[date, BreadthResult] = {
        d: breadth
        for d, breadth, _ewr in dashboard_service._aggregate_market_history_long(
            context.stock_facts, context.display_dates
        )
    }
    return [
        {
            "trade_date": d,
            **_count_fields(aggregated.get(d, dashboard_service._empty_market_breadth())),
        }
        for d in context.display_dates
    ]


def iter_scope_record_chunks(
    context: ProjectionContext,
    *,
    chunk_size: int = SCOPE_PROJECTION_DATE_CHUNK,
) -> Iterator[list[dict[str, object]]]:
    """按日期 chunk 产出 scope records（每次 <= active_board_count × chunk_size）。

    - ``chunk_size`` 必须在 ``1..SCOPE_PROJECTION_DATE_CHUNK``；否则立即 ValueError（fail closed）。
    - 每个 display date 恰处理一次（不遗漏、不重复）。
    - 每个 active board × 该 chunk 的每个 date 必有一行；空组合显式零
      （``_empty_market_breadth`` 兜底），使 projection 缺行只表示"builder 没算完"。
    - 生成器逐 chunk yield，调用方消费完一个 chunk 即可释放（不累计巨型 list）。
    """
    # 内存合同（生产，不是测试约定）：单次 scope aggregation 的 dates 必须 <= 上限。
    if not 1 <= chunk_size <= SCOPE_PROJECTION_DATE_CHUNK:
        raise ValueError(
            f"chunk_size must be within 1..{SCOPE_PROJECTION_DATE_CHUNK}, got {chunk_size!r}"
        )
    return _generate_scope_record_chunks(context, chunk_size)


def _generate_scope_record_chunks(
    context: ProjectionContext, chunk_size: int
) -> Iterator[list[dict[str, object]]]:
    """:func:`iter_scope_record_chunks` 的生成器实现（chunk_size 已由调用方校验）。"""
    dates = context.display_dates
    board_ids = context.board_ids
    if not dates or not board_ids:
        return
    for start in range(0, len(dates), chunk_size):
        chunk_dates = dates[start : start + chunk_size]
        agg = dashboard_service._aggregate_scope_breadth_long(
            context.stock_facts, context.membership_long, board_ids, chunk_dates
        )
        chunk_records: list[dict[str, object]] = []
        for d in chunk_dates:  # trade_date ascending
            for bid in board_ids:  # deterministic board order（输入顺序，不依赖 UUID 排序语义）
                breadth = agg.get((bid, d), dashboard_service._empty_market_breadth())
                chunk_records.append(
                    {
                        "board_id": bid,
                        "trade_date": d,
                        "membership_version": context.membership_versions[bid],
                        **_count_fields(breadth),
                    }
                )
        yield chunk_records


if __name__ == "__main__":
    print(f"SCOPE_PROJECTION_DATE_CHUNK={SCOPE_PROJECTION_DATE_CHUNK}")
    print(f"WINDOWS={WINDOWS}")
    print("OK")
