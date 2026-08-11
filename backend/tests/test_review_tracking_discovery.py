"""[REVIEW-V2-B3] Discovery tracking additive contract 单元测试。

验证 Discovery 追踪的 additive correction：
- create_tracking 允许 tracking_type='discovery'，且必须提供 discovery_id
- Discovery target 持久化 discovery_id，不退化成 scope target
- Scope target 仍保持 scope_type/scope_key，不带 discovery_id
- 非法 tracking_type 被拒绝
- _tracking_to_dto 回传 discoveryId（Discovery logical identity 可无歧义返回）

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_review_tracking_discovery.py -v -p no:cacheprovider
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.api.review import _tracking_to_dto
from app.schemas.review import (
    ReviewTrackingCreateRequest,
    ReviewTrackingResponse,
)
from app.services.review_tracking_service import TrackingError, create_tracking


class _FakeSession:
    """最小 AsyncSession：捕获 add 的对象与 flush。"""

    def __init__(self) -> None:
        self.added: list = []
        self.flushed = 0

    def add(self, obj):  # noqa: ANN001
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1


def _tracking_orm(**overrides):
    """构造 MarketReviewTracking 最小 ORM 替身（用于 DTO 映射）。"""
    t = MagicMock()
    t.id = uuid.uuid4()
    t.user_id = uuid.uuid4()
    t.source_signal_id = None
    t.tracking_type = overrides.get("tracking_type", "scope")
    t.scope_type = overrides.get("scope_type")
    t.scope_key = overrides.get("scope_key")
    t.instrument_id = None
    t.discovery_id = overrides.get("discovery_id")
    t.status = "active"
    t.confirmation_conditions = {}
    t.invalidation_conditions = {}
    t.note = None
    t.created_at = MagicMock()
    t.created_at.isoformat.return_value = "2026-08-11T00:00:00+00:00"
    t.closed_at = None
    return t


async def _create(**kwargs):
    session = _FakeSession()
    uid = uuid.uuid4()
    tracking = await create_tracking(
        session,
        user_id=uid,
        tracking_type=kwargs.get("tracking_type", "discovery"),
        scope_type=kwargs.get("scope_type"),
        scope_key=kwargs.get("scope_key"),
        discovery_id=kwargs.get("discovery_id"),
        idempotency_key=kwargs.get("idempotency_key", "idem-1"),
    )
    return session, tracking


async def test_discovery_tracking_requires_discovery_id():
    with pytest.raises(TrackingError, match="discovery"):
        await _create(tracking_type="discovery", scope_type="industry_l1", scope_key="k")


async def test_discovery_tracking_persists_discovery_identity():
    session, tracking = await _create(
        tracking_type="discovery",
        discovery_id="abc123def456",
        scope_type="industry_l1",
        scope_key="l1-tech",
    )
    assert tracking.tracking_type == "discovery"
    assert tracking.discovery_id == "abc123def456"
    # scope 仅作 evaluation context，身份以 discovery_id 为准
    assert tracking.scope_type == "industry_l1"
    assert session.added[0] is tracking


async def test_scope_tracking_stays_scope_target():
    session, tracking = await _create(
        tracking_type="scope",
        scope_type="style",
        scope_key="growth",
        discovery_id=None,
    )
    assert tracking.tracking_type == "scope"
    assert tracking.discovery_id is None
    assert tracking.scope_type == "style"
    assert tracking.scope_key == "growth"


async def test_invalid_tracking_type_rejected():
    with pytest.raises(TrackingError, match="非法 tracking_type"):
        await _create(tracking_type="bogus")


async def test_scope_tracking_requires_scope_key():
    with pytest.raises(TrackingError, match="scope"):
        await _create(tracking_type="scope", scope_type="style", scope_key=None)


def test_tracking_dto_returns_discovery_id():
    orm = _tracking_orm(
        tracking_type="discovery", discovery_id="abc123def456",
    )
    dto = _tracking_to_dto(orm)
    assert isinstance(dto, ReviewTrackingResponse)
    assert dto.discoveryId == "abc123def456"
    assert dto.trackingType == "discovery"


def test_scope_tracking_dto_has_no_discovery_id():
    orm = _tracking_orm(tracking_type="scope", scope_type="style", scope_key="growth")
    dto = _tracking_to_dto(orm)
    assert dto.discoveryId is None
    assert dto.scopeKey == "growth"


def test_create_request_schema_accepts_discovery_id():
    req = ReviewTrackingCreateRequest(
        tracking_type="discovery",
        discovery_id="abc123def456",
        scope_type="industry_l1",
        scope_key="l1-tech",
        idempotency_key="idem-1",
    )
    assert req.discovery_id == "abc123def456"
