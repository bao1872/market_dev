"""Modified-scope pure/unit tests for Canonical Observation Fact Persistence (Round 1C).

Covers the persistence owner (``review_observation_persistence_service``):
activation checks, Market / major_index / style exclusion, payload-not-modified
validation, partial-facts saveability, and PIT-unavailable non-entry into the
save path.  No DB, no network, no CI (pure unit mode).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.review_observation_persistence_service import (
    ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES,
    MARKET_PERSISTENCE_DIAGNOSTIC,
    ScopePersistenceNotActivatedError,
    _build_fact_values,
    _snapshot_readiness,
    save_scope_observation_fact,
)
from app.services.review_observation_prep_service import PreparedScope

T = date(2026, 8, 11)
T1 = date(2026, 8, 10)


class _FakeSession:
    """Dummy session: any execute/commit would fail the test (must never be reached)."""

    async def execute(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("save reached DB despite a guard that should have blocked")

    async def flush(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("save reached DB despite a guard that should have blocked")


def _prep(
    *,
    scope_type: str = "concept",
    scope_key: str = "A",
    pit_status_t: str = "historical_pit",
    members: tuple = ("m1", "m2"),
) -> PreparedScope:
    return PreparedScope(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_key,
        trade_date=T,
        canonical_t1=T1,
        pit_member_ids=("m1", "m2"),
        pit_member_ids_t1=("m1",),
        members=members,
        t1_membership_available=True,
        pit_status_t=pit_status_t,
        pit_status_t1="historical_pit",
        diagnostics=("ok",),
    )


def test_activation_set_exact() -> None:
    assert ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES == frozenset(
        {"industry_l1", "industry_l2", "industry_l3", "concept"}
    )
    assert "market" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "major_index" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "style" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES


@pytest.mark.parametrize("scope_type", ["market", "major_index", "style"])
@pytest.mark.asyncio
async def test_non_activated_scope_types_blocked(scope_type: str) -> None:
    prep = _prep(scope_type=scope_type)
    with pytest.raises(ScopePersistenceNotActivatedError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


@pytest.mark.asyncio
async def test_market_excluded_even_when_generic_loop_passes_members() -> None:
    # Double safety: even if a generic loop fed market with resolved members, the
    # persistence activation guard must block it (prompt §16).
    prep = _prep(scope_type="market", pit_status_t="historical_pit", members=("m1",))
    with pytest.raises(ScopePersistenceNotActivatedError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


@pytest.mark.asyncio
async def test_pit_unavailable_does_not_enter_save_path() -> None:
    # Activated scope but PIT(T) unavailable -> no fact row is written (prompt §19A).
    prep = _prep(pit_status_t="unavailable", members=())
    with pytest.raises(ValueError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


@pytest.mark.asyncio
async def test_no_members_does_not_enter_save_path() -> None:
    prep = _prep(pit_status_t="historical_pit", members=())
    with pytest.raises(ValueError):
        await save_scope_observation_fact(_FakeSession(), prep, {"scope": {}})


def test_build_fact_values_does_not_modify_core_output() -> None:
    prep = _prep()
    obs: dict = {"scope": {"scope_type": "concept"}, "price": {"return": {"mean": 0.01}}}
    values = _build_fact_values(prep, obs, "review-obs-1.0.0")
    # Same object reference stored (no copy / rename / recompute).
    assert values["observation_payload"] is obs
    assert values["observation_payload"]["price"]["return"]["mean"] == 0.01
    assert values["trade_date"] == T
    assert values["scope_type"] == "concept"
    assert values["scope_key"] == "A"
    assert values["pit_member_count"] == 2
    assert values["pit_member_count_t1"] == 1
    assert values["provided_member_count"] == 2
    assert values["t1_membership_available"] is True
    assert values["pit_status_t"] == "historical_pit"
    assert values["pit_status_t1"] == "historical_pit"
    assert values["readiness"] == "ready"
    assert values["diagnostics"] == ["ok"]
    assert values["algorithm_version"] == "review-obs-1.0.0"


def test_partial_facts_can_be_saved() -> None:
    # Core returned normally but some axis is unavailable/partial -> still saved
    # as-is, readiness stays "ready" (persistence never judges completeness,
    # prompt §19C / §20).  No threshold-derived downgrade.
    prep = _prep()
    partial_obs = {
        "scope": {"scope_type": "concept"},
        "price": {"return": {"mean": None}},
        "trend": {"state": {"denominator": 0}},
    }
    values = _build_fact_values(prep, partial_obs, None)
    assert values["observation_payload"] is partial_obs
    assert values["readiness"] == "ready"


def test_snapshot_readiness_mapping() -> None:
    assert _snapshot_readiness(_prep(pit_status_t="unavailable", members=())) == "unavailable"
    assert _snapshot_readiness(_prep(pit_status_t="historical_pit", members=())) == "no_members"
    assert _snapshot_readiness(_prep(pit_status_t="historical_pit", members=("m1",))) == "ready"


def test_market_persistence_diagnostic_text() -> None:
    assert "market_not_activated_for_historical_persistence" in MARKET_PERSISTENCE_DIAGNOSTIC
