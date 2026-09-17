from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.beta_application_notification_contract import (
    BETA_APPLICATION_ADMIN_EVENT as CONTRACT_EVENT,
)
from app.services.beta_application_notification_contract import (
    build_beta_application_dto as contract_builder,
)
from app.services.beta_application_notifier import (
    BETA_APPLICATION_ADMIN_EVENT as COMPATIBILITY_EVENT,
)
from app.services.beta_application_notifier import (
    build_beta_application_dto as compatibility_builder,
)
from app.services.outbox_relay import write_outbox as relay_compatibility_writer
from app.services.outbox_writer import write_outbox


def test_notification_compatibility_exports_have_single_owners() -> None:
    assert COMPATIBILITY_EVENT == CONTRACT_EVENT
    assert compatibility_builder is contract_builder
    assert relay_compatibility_writer is write_outbox


@pytest.mark.asyncio
async def test_outbox_writer_preserves_caller_owned_transaction() -> None:
    db = AsyncMock()
    db.add = Mock()
    aggregate_id = uuid.uuid4()

    row = await write_outbox(
        db,
        event_type="test.created",
        payload={"value": 1},
        aggregate_type="test",
        aggregate_id=aggregate_id,
        headers={"trace": "abc"},
    )

    assert row.event_type == "test.created"
    assert row.aggregate_id == aggregate_id
    assert row.payload == {"value": 1}
    assert row.headers == {"trace": "abc"}
    assert row.status == "pending"
    assert row.retry_count == 0
    db.add.assert_called_once_with(row)
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "aggregate_type"),
    [("", "test"), ("test.created", "")],
)
async def test_outbox_writer_rejects_missing_identity(
    event_type: str,
    aggregate_type: str,
) -> None:
    db = AsyncMock()
    with pytest.raises(ValueError):
        await write_outbox(db, event_type, {}, aggregate_type)
    db.flush.assert_not_awaited()
