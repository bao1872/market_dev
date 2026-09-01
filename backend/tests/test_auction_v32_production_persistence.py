"""Production-path EXECUTION contract for V3.2 persistence.

These tests actually call ``await persist_v32_scope_results(...)`` through a
fake AsyncSession, rather than inspecting helper signatures.  Signature-only
tests produced a false green: the production function still passed a stale
``algorithm_version=`` argument that ``build_scan_run_kwargs`` no longer
accepts, and a whole suite (541 tests) stayed green without ever executing it.

Only the session and the formal publication owner are faked; the real
``AuctionScanRun`` / ``AuctionScopeResult`` models, the payload parser, the
version binding and the scope-name single owner all execute for real.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.auction.coverage import ScanCoverage
from app.domain.auction.scope_payload import build_scope_payload
from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.models.auction import AuctionScanRun, AuctionScopeResult
from app.services import auction_scope_persistence_service as persistence

_T = date(2026, 8, 14)
_SCOPE_KEY = "IND_BANK"
_SCOPE_NAME = "银行"


class FakeAsyncSession:
    """Minimal async session that records side effects."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flush_count = 0
        self.commit_count = 0
        self.rollback_count = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1
        # emulate PK materialisation so children can reference run.id
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


def _payload() -> dict[str, Any]:
    return build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": _SCOPE_KEY, "scope_name": _SCOPE_NAME},
        repricing={"equal_weight_gap": 0.012, "price_valid_count": 20},
        historical_dynamics={"position": 70.0},
        participation={"total_auction_amount": 1_000_000.0},
        cross_sectional={"repricing": {"equal_weight_gap": 80.0}},
        member_attribution={"leaders": [], "jaccard": None},
    )


def _coverage() -> ScanCoverage:
    return ScanCoverage(
        eligible_count=100,
        valid_count=80,
        price_ready_count=80,
        amount_ready_count=80,
        both_ready_count=80,
        missing_count=20,
        coverage_ratio=0.8,
        missing_reasons=("missing_current_auction_quote",),
    )


@pytest.fixture()
def session() -> FakeAsyncSession:
    return FakeAsyncSession()


@pytest.fixture()
def publish_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the formal publication owner and record its invocations."""
    calls: list[dict[str, Any]] = []

    async def _fake_publish(session: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(persistence, "publish_auction_analysis", _fake_publish)
    return calls


async def test_production_persist_path_executes(
    session: FakeAsyncSession, publish_calls: list[dict[str, Any]]
) -> None:
    """The real production function must run end to end."""
    run_id = await _persist(session)

    assert isinstance(run_id, UUID)

    runs = [o for o in session.added if isinstance(o, AuctionScanRun)]
    results = [o for o in session.added if isinstance(o, AuctionScopeResult)]
    assert len(runs) == 1, "exactly one AuctionScanRun must be added"
    assert len(results) == 1, "each prepared scope must produce one result row"


async def test_scan_run_carries_the_v32_algorithm_version(
    session: FakeAsyncSession, publish_calls: list[dict[str, Any]]
) -> None:
    await _persist(session)
    run = next(o for o in session.added if isinstance(o, AuctionScanRun))
    assert run.algorithm_version == V32_ALGORITHM_VERSION


async def test_publication_owner_is_called_once_with_the_run(
    session: FakeAsyncSession, publish_calls: list[dict[str, Any]]
) -> None:
    run_id = await _persist(session)
    assert len(publish_calls) == 1
    call = publish_calls[0]
    assert call["scan_run_id"] == run_id
    assert call["truth_status"] == "verified"


async def test_persist_does_not_own_the_transaction(
    session: FakeAsyncSession, publish_calls: list[dict[str, Any]]
) -> None:
    await _persist(session)
    assert session.commit_count == 0, "the orchestrator must own the commit"
    assert session.flush_count >= 1, "ids must be materialised before children"


async def test_scope_result_name_is_derived_from_the_payload(
    session: FakeAsyncSession, publish_calls: list[dict[str, Any]]
) -> None:
    await _persist(session)
    result = next(o for o in session.added if isinstance(o, AuctionScopeResult))
    assert result.scope_name == _SCOPE_NAME


async def test_non_v32_payload_fails_before_anything_is_added(
    session: FakeAsyncSession, publish_calls: list[dict[str, Any]]
) -> None:
    """A foreign algorithm version must be rejected before any write."""
    tampered = dict(_payload())
    tampered["algorithm_version"] = "auction-v999"

    with pytest.raises(ValueError, match="algorithm_version"):
        await _persist(session, payload=tampered)

    assert session.added == [], "nothing may be added when validation fails"
    assert publish_calls == []


async def test_scope_name_drift_is_rejected_before_write(
    session: FakeAsyncSession, publish_calls: list[dict[str, Any]]
) -> None:
    with pytest.raises(ValueError, match="drift"):
        await _persist(session, scope_name="旧名字")
    assert session.added == []


async def _persist(
    session: FakeAsyncSession,
    *,
    payload: dict[str, Any] | None = None,
    scope_name: str | None = None,
) -> UUID:
    return await persistence.persist_v32_scope_results(
        session,  # type: ignore[arg-type]
        trade_date=_T,
        scope_results=[
            {
                "scope_type": "industry",
                "scope_id": uuid4(),
                "scope_name": scope_name,
                "payload": payload if payload is not None else _payload(),
            }
        ],
        capture_run_id=uuid4(),
        test_namespace="production",
        coverage=_coverage(),
        truth_status="verified",
    )
