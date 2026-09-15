"""Pure-unit contract: ``factor_publication_service.publish_market_aggregation``
consumes the canonical ``degraded_publishable`` evidence.

This is the still-living publication semantic that the retired legacy board producer
used to guard: the production owner does NOT re-derive degraded logic internally —
it consumes the ``degraded_publishable`` bool decided upstream by the board
aggregation layer and only publishes a *partial* batch when that evidence is True
(accept condition ``status == "succeeded" or (status == "partial" and
degraded_publishable)``).

No real DB: the ``AsyncSession`` is mocked and the two DB lookups
(``get_published_snapshot_run_id`` / ``get_publication``) are patched.  Does NOT
import the retired ``board_analysis_service`` and does NOT restore any research
helper.
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.models.board_analysis_snapshot import BoardAnalysisRun
from app.services import factor_publication_service as fps

pytestmark = pytest.mark.pure_unit


def _board_run(
    *,
    status: str,
    trade_date: date,
    source_core_run_id: uuid.UUID,
    failed_count: int = 0,
    succeeded_count: int = 1,
    expected_count: int = 1,
    coverage_ratio: float = 1.0,
) -> BoardAnalysisRun:
    run = BoardAnalysisRun()
    run.status = status
    run.trade_date = trade_date
    run.source_core_run_id = source_core_run_id
    run.failed_count = failed_count
    run.succeeded_count = succeeded_count
    run.expected_count = expected_count
    run.coverage_ratio = coverage_ratio
    return run


@pytest.mark.asyncio
async def test_publish_market_aggregation_consumes_canonical_degraded_publishable() -> None:
    trade_date = date(2026, 8, 14)
    core_run_id = uuid.uuid4()
    agg_run_id = uuid.uuid4()

    session = AsyncMock()
    session.get = AsyncMock(
        return_value=_board_run(
            status="partial",
            trade_date=trade_date,
            source_core_run_id=core_run_id,
        )
    )
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    with patch.object(
        fps, "get_published_snapshot_run_id", new=AsyncMock(return_value=core_run_id)
    ), patch.object(fps, "get_publication", new=AsyncMock(return_value=object())):
        # partial WITHOUT canonical degraded_publishable evidence -> rejected.
        with pytest.raises(ValueError):
            await fps.publish_market_aggregation(
                session,
                trade_date,
                core_run_id,
                agg_run_id,
                "v1",
                degraded_publishable=False,
            )

        # partial WITH canonical degraded_publishable evidence -> accepted
        # (degraded publication; status stays partial, no rewrite to succeeded).
        await fps.publish_market_aggregation(
            session,
            trade_date,
            core_run_id,
            agg_run_id,
            "v1",
            degraded_publishable=True,
        )
        assert session.execute.await_count == 1

        # control: a fully succeeded batch is publishable regardless of the flag.
        session.get = AsyncMock(
            return_value=_board_run(
                status="succeeded",
                trade_date=trade_date,
                source_core_run_id=core_run_id,
                failed_count=0,
                succeeded_count=1,
                expected_count=1,
            )
        )
        await fps.publish_market_aggregation(
            session,
            trade_date,
            core_run_id,
            agg_run_id,
            "v1",
            degraded_publishable=False,
        )
        assert session.execute.await_count == 2
