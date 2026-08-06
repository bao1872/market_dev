"""Chip 15m 运行级 refresh 协调器（[CHANGE-20260806-005 / Phase 3 / GAP-08/10]）。

背景：
- 原 `execute_after_close_chip_consensus` 在 compute loop 内**逐股**调用
  `_fetch_chip_bars` → 内部 `refresh_15min_bars`（N+1 次刷新，无有界并发/超时）。
- 目标：刷新与计算分离——先运行级刷新全部标的 15m 数据（有界并发 + 每股超时 +
  逐股 status + 独立 resume），refresh 完成后冻结 cutoff/readiness，compute loop 不再
  refresh（per_stock_refresh_in_compute_loop=0）。

本模块为**独立可 resume 的刷新阶段**，复用 `bar_repository.refresh_15min_bars` 作为
单标的刷新原语，在其上叠加有界并发与超时编排。刷新结果仅代表「已尝试刷新」，实际
readiness 由 compute 阶段消费时用冻结的 run 级 cutoff 判定。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

# 有界并发与每股超时（pytdx 拉取通常很快；超时避免单股挂起拖垮整批）
_DEFAULT_CONCURRENCY = 8
_DEFAULT_PER_STOCK_TIMEOUT = 30.0


@dataclass
class ChipBarsRefreshResult:
    """运行级刷新结果：每股 status + 整体汇总 + 冻结的 run 级 source_cutoff。"""

    refreshed: int = 0
    failed: int = 0
    skipped: int = 0
    # 每股刷新结果（instrument_id -> status）
    per_instrument: dict[str, str] = field(default_factory=dict)
    # 每股失败原因（instrument_id -> reason）
    failed_reasons: dict[str, str] = field(default_factory=dict)
    # 冻结的 run 级 source_cutoff（ISO 字符串；刷新阶段不计算具体 cutoff，
    # 该字段保留占位，真实 cutoff 由 compute 阶段从已刷新 bars 统一判定）。
    source_cutoff: str | None = None

    @property
    def total(self) -> int:
        return self.refreshed + self.failed + self.skipped

    def to_dict(self) -> dict[str, Any]:
        return {
            "refreshed": self.refreshed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "source_cutoff": self.source_cutoff,
        }


async def _refresh_single(
    instrument_id: Any,
    trade_date: date,
    count: int,
    timeout: float,
) -> tuple[str, str | None]:
    """刷新单标的 15m 数据，返回 (status, reason)。

    status ∈ {"refreshed", "failed", "skipped"}；reason 用于失败/跳过原因。
    复用 `bar_repository.refresh_15min_bars`（单标的刷新原语），不重写 pytdx 逻辑。
    """
    from app.db import AsyncSessionLocal
    from app.repositories.bar_repository import refresh_15min_bars

    try:
        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(
                refresh_15min_bars(db, instrument_id, count=count),
                timeout=timeout,
            )
            await db.commit()
        return "refreshed", None
    except TimeoutError:
        return "failed", "M15_REFRESH_FAILED: timeout"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ChipRefresh] 单标的刷新失败: instrument_id=%s, error=%s",
            instrument_id, exc,
        )
        return "failed", f"M15_REFRESH_FAILED: {str(exc)[:200]}"


async def refresh_15m_batch(
    instrument_ids: Sequence[Any],
    trade_date: date,
    *,
    count: int = 4000,
    concurrency: int = _DEFAULT_CONCURRENCY,
    per_stock_timeout: float = _DEFAULT_PER_STOCK_TIMEOUT,
) -> ChipBarsRefreshResult:
    """运行级批量刷新 15m 数据：有界并发 + 每股超时 + 逐股 status。

    [Phase 3 / GAP-08/10] 在 compute loop 之前调用一次，之后 compute 阶段经
    `skip_refresh=True` 读取已刷新 bars，不再逐股 refresh（per_stock_refresh_in_compute_loop=0）。

    Args:
        instrument_ids: 需刷新的标的集合（未刷新的全部）。
        trade_date: 目标交易日。
        count: 每股拉取 15m 根数。
        concurrency: 有界并发上限。
        per_stock_timeout: 单标的刷新超时（秒）。

    Returns:
        ChipBarsRefreshResult：每股 status + 汇总。
    """
    sem = asyncio.Semaphore(concurrency)
    result = ChipBarsRefreshResult()

    async def _bounded(instrument_id: Any) -> None:
        async with sem:
            status, reason = await _refresh_single(
                instrument_id, trade_date, count, per_stock_timeout,
            )
        sid = str(instrument_id)
        result.per_instrument[sid] = status
        if status == "refreshed":
            result.refreshed += 1
        elif status == "failed":
            result.failed += 1
            result.failed_reasons[sid] = reason or "M15_REFRESH_FAILED"
        else:
            result.skipped += 1

    await asyncio.gather(*(_bounded(i) for i in instrument_ids))
    logger.info(
        "[ChipRefresh] 运行级刷新完成: refreshed=%d failed=%d skipped=%d total=%d",
        result.refreshed, result.failed, result.skipped, result.total,
    )
    return result
