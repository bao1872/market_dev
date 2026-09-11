"""盘中全市场实时 Snapshot Adapter（Stage B1）。

职责边界（**极其重要**）：

- 只负责「行情事实」：从东方财富 **实时** host 拉完整全市场 A 股快照，归一化为
  :class:`EodSnapshotRow`（raw，不复权），并给出 ``source_host`` / ``captured_at`` /
  ``market_watermark`` 供上层做 freshness 诊断。
- **不做任何复权**：不查 adj_factor、不乘 factor、不查 XDXR。复权坐标由 Stage B2 的
  business-date Adjustment Context 负责。
- **不猜 freshness 秒数**：本层只暴露 watermark，不写「>120s 判死」这类阈值。
  午休 11:30→13:00 没有新 watermark 是合法的，是否判 stale 由 Monitor 按市场阶段决定。

盘中数据源合同（**不可违反**）：

    盘中只允许 push2.eastmoney.com。
    push2delay.eastmoney.com 是延时源，盘中一旦被悄悄用作 fallback，
    通知会延迟十几分钟。它不可用必须 FAIL/degraded，绝不静默回退。

    → 因此 :data:`REALTIME_CLIST_HOSTS` 只含 push2，且本模块不引用
      :data:`~app.services.eod_market_snapshot_provider.EASTMONEY_CLIST_HOSTS`。

分页/failover 复用 EOD 的通用实现
:func:`~app.services.eod_market_snapshot_provider.fetch_a_share_snapshot_batch`
（single-host 一致性、total 漂移检查、完整性 fail-closed、f5 手→股、f13 校验），
本模块不另写一套分页。

用法::

    async with httpx.AsyncClient() as client:
        snap = await fetch_realtime_a_share_snapshot(client)
        rows = snap.by_symbol()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.services.eod_market_snapshot_provider import (
    DEFAULT_PAGE_SIZE,
    EodSnapshotRow,
    SnapshotProviderError,
    fetch_a_share_snapshot_batch,
    normalize_snapshot_rows,
)

logger = logging.getLogger(__name__)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# 盘中唯一允许的 snapshot host。
# 严禁在此加入 push2delay（延时源）：盘中 fallback 到延时源会让通知延迟十几分钟。
REALTIME_CLIST_HOSTS: tuple[str, ...] = (
    "push2.eastmoney.com",
)


@dataclass(frozen=True)
class RealtimeMarketSnapshot:
    """盘中全市场实时快照结果。

    ``rows`` 是最终交给上层的行（可能已被 ``symbols`` 过滤）；
    ``raw_count`` / ``normalized_count`` 始终描述**全市场**拉取结果，
    用于证明「网络层永远是全市场，watchlist 只做内存过滤」。
    """

    source_host: str
    captured_at: datetime
    market_watermark: datetime

    rows: tuple[EodSnapshotRow, ...]

    raw_count: int
    normalized_count: int

    @property
    def dropped_count(self) -> int:
        """归一化丢弃的行数（非法代码 / f13 不符 / 非 A 股）。"""
        return self.raw_count - self.normalized_count

    def by_symbol(self) -> dict[str, EodSnapshotRow]:
        return {row.symbol: row for row in self.rows}


async def fetch_realtime_a_share_snapshot(
    client: httpx.AsyncClient,
    *,
    symbols: set[str] | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int | None = None,
    now: datetime | None = None,
) -> RealtimeMarketSnapshot:
    """拉取盘中全市场 A 股实时快照（raw，不复权）。

    流程：
    1. **先取得完整全市场 snapshot**（网络层永远是全市场，不按 symbol 查询）；
    2. 归一化（f5 手 → 股、f13 分类校验、非法行丢弃）；
    3. 市场级 freshness 最低合同：本轮必须至少存在「captured_at 当天」的有效 f124；
    4. 若传入 ``symbols``，**只在内存里过滤**最终 rows。

    Args:
        client: httpx 异步客户端。
        symbols: 可选的 watchlist 代码集合；只影响返回 rows，不影响网络请求。
        page_size: 单页条数（clist 实测上限 100）。
        max_pages: 最多拉取页数（None 表示不限）。
        now: 抓取时刻（仅测试注入）；默认当前上海时间。

    Returns:
        :class:`RealtimeMarketSnapshot`。

    Raises:
        SnapshotProviderError: 实时 host 不可用（**不回退延时源**），
            或全市场不存在当天 watermark。
    """
    captured_at = now or datetime.now(_SHANGHAI_TZ)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=_SHANGHAI_TZ)
    else:
        captured_at = captured_at.astimezone(_SHANGHAI_TZ)

    batch = await fetch_a_share_snapshot_batch(
        client,
        hosts=REALTIME_CLIST_HOSTS,
        require_eod_watermark=False,
        page_size=page_size,
        max_pages=max_pages,
        captured_at=captured_at,
    )

    # 只归一化一次：normalized_count 用全量，rows 用过滤后的子集
    all_rows = normalize_snapshot_rows(batch.raw_rows)
    normalized_count = len(all_rows)

    # 市场级 freshness 最低合同：
    # 本轮必须至少存在「今天」的有效 f124。
    #
    # 这里刻意**不设** 60s/120s 之类阈值 —— 午休 11:30→13:00 合法没有新 watermark，
    # 拍脑袋阈值会在午休把系统判死。具体 freshness 由 Monitor 按市场阶段决定。
    today_watermarks = [
        row.updated_at
        for row in all_rows
        if row.updated_at is not None
        and row.updated_at.date() == captured_at.date()
    ]

    if not today_watermarks:
        raise SnapshotProviderError(
            "realtime snapshot has no same-day market watermark"
        )

    market_watermark = max(today_watermarks)  # type: ignore[type-var]

    selected_rows = (
        all_rows
        if symbols is None
        else [row for row in all_rows if row.symbol in symbols]
    )

    logger.debug(
        "realtime snapshot host=%s captured_at=%s market_watermark=%s "
        "raw_count=%d normalized_count=%d selected=%d",
        batch.source_host,
        captured_at.isoformat(),
        market_watermark.isoformat(),
        batch.raw_count,
        normalized_count,
        len(selected_rows),
    )

    return RealtimeMarketSnapshot(
        source_host=batch.source_host,
        captured_at=captured_at,
        market_watermark=market_watermark,
        rows=tuple(selected_rows),
        raw_count=batch.raw_count,
        normalized_count=normalized_count,
    )
