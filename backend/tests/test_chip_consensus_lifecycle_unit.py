"""Pure-unit terminal-state contracts for chip consensus execution."""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.services.after_close_chip_consensus_service import (
    execute_after_close_chip_consensus,
)


@pytest.mark.asyncio
async def test_all_legal_skips_report_skipped() -> None:
    instruments = [uuid.uuid4(), uuid.uuid4()]
    daily = pd.DataFrame({"close": [1.0]})
    m15 = pd.DataFrame({"close": [1.0] * 10})
    upsert = AsyncMock()

    with patch(
        "app.services.after_close_chip_consensus_service._fetch_chip_bars",
        new=AsyncMock(return_value=(daily, m15)),
    ), patch(
        "app.services.after_close_chip_consensus_service._upsert_chip_snapshot",
        new=upsert,
    ):
        result = await execute_after_close_chip_consensus(
            uuid.uuid4(), date(2026, 7, 31), uuid.uuid4(),
            instrument_ids=instruments,
            worker_id="worker:test",
            lease_epoch=1,
        )

    assert result["status"] == "skipped"
    assert result["skipped_count"] == 2
    assert result["failed_count"] == 0
    assert upsert.await_count == 2


@pytest.mark.asyncio
async def test_skip_persistence_failure_is_not_silently_swallowed() -> None:
    daily = pd.DataFrame({"close": [1.0]})
    m15 = pd.DataFrame({"close": [1.0] * 10})

    with patch(
        "app.services.after_close_chip_consensus_service._fetch_chip_bars",
        new=AsyncMock(return_value=(daily, m15)),
    ), patch(
        "app.services.after_close_chip_consensus_service._upsert_chip_snapshot",
        new=AsyncMock(side_effect=RuntimeError("write rejected")),
    ):
        result = await execute_after_close_chip_consensus(
            uuid.uuid4(), date(2026, 7, 31), uuid.uuid4(),
            instrument_ids=[uuid.uuid4()],
            worker_id="worker:test",
            lease_epoch=1,
        )

    assert result["status"] == "failed"
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 1
    assert "persistence failed" in result["failed_instruments"][0]["error"]


@pytest.mark.asyncio
async def test_mixed_skip_and_system_failure_report_partial() -> None:
    daily = pd.DataFrame({"close": [1.0]})
    short_m15 = pd.DataFrame({"close": [1.0] * 10})
    fetch = AsyncMock(side_effect=[(daily, short_m15), (None, None)])

    with patch(
        "app.services.after_close_chip_consensus_service._fetch_chip_bars",
        new=fetch,
    ), patch(
        "app.services.after_close_chip_consensus_service._upsert_chip_snapshot",
        new=AsyncMock(),
    ):
        result = await execute_after_close_chip_consensus(
            uuid.uuid4(), date(2026, 7, 31), uuid.uuid4(),
            instrument_ids=[uuid.uuid4(), uuid.uuid4()],
            worker_id="worker:test",
            lease_epoch=1,
        )

    assert result["status"] == "partial"
    assert result["skipped_count"] == 1
    assert result["failed_count"] == 1
