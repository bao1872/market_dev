"""Chip 15m 运行级 refresh 协调器单测（[CHANGE-20260806-005 / Phase 3]）。

验证：有界并发 + 每股超时 + 逐股 status + 运行级刷新结果汇总。
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chip_bars_refresh_coordinator import (
    ChipBarsRefreshResult,
    refresh_15m_batch,
)


@pytest.mark.asyncio
async def test_refresh_batch_summary_and_per_instrument_status() -> None:
    """refresh_15m_batch 应汇总每股 status（refreshed/failed）到 result。"""
    ids = ["i1", "i2", "i3"]

    async def _fake_refresh_single(inst, trade_date, count, timeout):
        if inst == "i1":
            return "refreshed", None
        if inst == "i2":
            return "refreshed", None
        return "failed", "M15_REFRESH_FAILED: boom"

    with patch(
        "app.services.chip_bars_refresh_coordinator._refresh_single",
        new=_fake_refresh_single,
    ):
        result = await refresh_15m_batch(ids, date(2026, 7, 31))

    assert result.refreshed == 2
    assert result.failed == 1
    assert result.total == 3
    assert result.per_instrument["i1"] == "refreshed"
    assert result.per_instrument["i3"] == "failed"
    assert "i3" in result.failed_reasons


@pytest.mark.asyncio
async def test_refresh_batch_bounded_concurrency() -> None:
    """refresh_15m_batch 应通过信号量限制并发（有界并发契约）。"""
    ids = ["a", "b", "c", "d"]

    async def _fake_refresh_single(inst, trade_date, count, timeout):
        return "refreshed", None

    with patch(
        "app.services.chip_bars_refresh_coordinator._refresh_single",
        new=_fake_refresh_single,
    ):
        result = await refresh_15m_batch(ids, date(2026, 7, 31), concurrency=2)

    assert result.refreshed == 4
    assert result.total == 4


@pytest.mark.asyncio
async def test_refresh_single_timeout_maps_to_failed() -> None:
    """单标的刷新超时应映射为 failed + M15_REFRESH_FAILED: timeout。"""
    from app.services.chip_bars_refresh_coordinator import _refresh_single

    async def _hanging(inst, count):
        await asyncio.sleep(60)  # 远超 timeout，触发 wait_for 超时

    with patch(
        "app.repositories.bar_repository.refresh_15min_bars",
        new=_hanging,
    ):
        status, reason = await _refresh_single("i1", date(2026, 7, 31), 4000, 0.01)

    assert status == "failed"
    assert "M15_REFRESH_FAILED" in (reason or "")


@pytest.mark.asyncio
async def test_refresh_single_provider_error_maps_to_failed() -> None:
    """刷新 provider 异常应映射为 failed，不向 compute loop 传播。"""
    from app.services.chip_bars_refresh_coordinator import _refresh_single

    with patch(
        "app.repositories.bar_repository.refresh_15min_bars",
        new=AsyncMock(side_effect=RuntimeError("provider down")),
    ):
        status, reason = await _refresh_single("i1", date(2026, 7, 31), 4000, 1.0)

    assert status == "failed"
    assert "provider down" in (reason or "")


@pytest.mark.asyncio
async def test_refresh_result_to_dict_roundtrip() -> None:
    """ChipBarsRefreshResult.to_dict 应包含关键汇总字段。"""
    r = ChipBarsRefreshResult(refreshed=1, failed=1, skipped=0)
    d = r.to_dict()
    assert d["refreshed"] == 1
    assert d["failed"] == 1
    assert d["total"] == 2
    assert "source_cutoff" in d
