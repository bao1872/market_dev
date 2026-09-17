from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.after_close_run_contract import (
    AFTER_CLOSE_JOB_NAME,
    AfterCloseRunStatus,
    parse_after_close_metadata,
    update_after_close_status,
)


def _job(metadata: str | None) -> SchedulerJobRun:
    return SchedulerJobRun(
        id=uuid.uuid4(),
        job_name=AFTER_CLOSE_JOB_NAME,
        business_date="2026-09-17",
        run_key=f"after-close-contract:{uuid.uuid4()}",
        status="running",
        metadata_json=metadata,
    )


def test_parse_after_close_metadata_preserves_fail_open_read_compatibility() -> None:
    assert parse_after_close_metadata(_job(None)) == {}
    assert parse_after_close_metadata(_job("not-json")) == {}
    assert parse_after_close_metadata(_job('{"trade_date":"2026-09-17"}')) == {
        "trade_date": "2026-09-17",
    }


@pytest.mark.asyncio
async def test_update_after_close_status_preserves_metadata_and_appends_event() -> None:
    dsa_run_id = uuid.uuid4()
    job_run = _job(
        json.dumps(
            {
                "trade_date": "2026-09-17",
                "orchestrator_status": "computing_features",
                "preserved": {"value": 1},
            }
        )
    )
    db = AsyncMock()
    db.add = Mock()

    await update_after_close_status(
        db,
        job_run,
        AfterCloseRunStatus.QUEUED,
        dsa_run_id=dsa_run_id,
        extra={"dsa_recovery_count": 2, "trade_date": "ignored"},
    )

    metadata = json.loads(job_run.metadata_json)
    assert metadata == {
        "trade_date": "2026-09-17",
        "orchestrator_status": "queued",
        "preserved": {"value": 1},
        "dsa_run_id": str(dsa_run_id),
        "dsa_recovery_count": 2,
    }
    assert db.add.call_count == 1
    event = db.add.call_args.args[0]
    assert event.step == "queued"
    assert event.level == "info"
    assert event.message == "编排状态切换: queued"
    assert event.payload == {"orchestrator_status": "queued"}
    assert db.flush.await_count == 3
