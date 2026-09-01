"""KPI-1 contract tests: slot fail-closed, previous-leader isolation and the
three-state previous semantics, plus complete version binding.

These are the Gate B blockers that the T3 chain alone did not pin down.  They
share the chain fixture with ``test_auction_v32_production_chain.py`` so the
inputs under test are the same ones production preparation consumes.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from app.domain.auction.analysis_preparation import (
    PREVIOUS_COMPUTED_EMPTY,
    PREVIOUS_NONEMPTY,
    PREVIOUS_UNAVAILABLE,
    build_previous_leader_sets,
    canonicalize_trade_slots,
    prepare_v32_analysis,
)
from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    resolve_scope_members,
)
from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.services.auction_scope_persistence_service import build_scope_result_kwargs
from tests.test_auction_v32_production_chain import (
    CFG,
    CONCEPT_KEY,
    INDUSTRY_KEY,
    T,
    _by_key,
    _Fixture,
    _obs,
)


@pytest.fixture()
def fx() -> _Fixture:
    return _Fixture()


# ---------------------------------------------------------------------------
# KPI-1A trade slots
# ---------------------------------------------------------------------------
def test_slot_contract_rejects_future_slot(fx: _Fixture) -> None:
    """A T+1 slot must fail closed, never be silently dropped."""
    future = T + timedelta(days=1)
    with pytest.raises(ValueError, match="after T"):
        canonicalize_trade_slots(list(fx.slots) + [future], T)


def test_slot_contract_rejects_missing_t(fx: _Fixture) -> None:
    with pytest.raises(ValueError, match="must contain T"):
        canonicalize_trade_slots([d for d in fx.slots if d != T], T)


def test_slot_contract_canonicalises_unordered_duplicates(fx: _Fixture) -> None:
    messy = list(reversed(fx.slots)) + list(fx.slots[:5]) + [T, T]
    first = canonicalize_trade_slots(messy, T)
    second = canonicalize_trade_slots(list(reversed(messy)), T)
    assert first == second, "slot canonicalisation must be deterministic"
    assert first.trade_dates[-1] == T
    assert first.trade_dates == tuple(sorted(set(first.trade_dates)))


def test_prepare_fails_closed_on_future_slot_and_observation(
    fx: _Fixture,
) -> None:
    """Future data in BOTH trade_dates and observations must fail closed."""
    future = T + timedelta(days=1)
    fx.observations_by_date[future] = [
        _obs(m, future, 5.0, 1e12) for m in fx.instruments
    ]
    # the future date is in BOTH the declared slots and the observations
    with pytest.raises(ValueError, match="after T"):
        prepare_v32_analysis(
            trade_date=T,
            trade_dates=list(fx.slots) + [future],
            observations_by_date=fx.observations_by_date,
            edges=fx.edges,
            config=CFG,
        )


def test_missing_history_day_is_not_backfilled(fx: _Fixture) -> None:
    """Dropping a slot lowers the pre-T count; no earlier day is pulled in."""
    full = fx.prepare()
    holed_slot = fx.slots[5]
    holed = prepare_v32_analysis(
        trade_date=T,
        trade_dates=[d for d in fx.slots if d != holed_slot],
        observations_by_date=fx.observations_by_date,
        edges=fx.edges,
        config=CFG,
    )
    assert holed.diagnostics["pre_t_slot_count"] == (
        full.diagnostics["pre_t_slot_count"] - 1
    )


# ---------------------------------------------------------------------------
# KPI-1B previous leader scope isolation
# ---------------------------------------------------------------------------
def test_previous_concept_leaders_exclude_outsiders(fx: _Fixture) -> None:
    """An outsider with extreme gap+amount must not become a past leader."""
    outsider = fx.instruments[8]  # NOT a concept member (only 0..5 are)
    concept_members = {
        str(m)
        for m in resolve_scope_members(fx.edges, T, family=FAMILY_CONCEPT)[CONCEPT_KEY]
    }
    assert str(outsider) not in concept_members

    prev_day = fx.slots[-2]
    fx.observations_by_date[prev_day] = [
        (o if o.instrument_id != outsider else _obs(outsider, prev_day, 9.99, 1e12))
        for o in fx.observations_by_date[prev_day]
    ]

    leader_sets = build_previous_leader_sets(
        previous_trade_date=prev_day,
        observations_by_date=fx.observations_by_date,
        edges=fx.edges,
        config=CFG,
    )
    prev_concept = leader_sets[FAMILY_CONCEPT][CONCEPT_KEY]

    assert str(outsider) not in {str(x) for x in prev_concept}
    assert {str(x) for x in prev_concept} <= concept_members, (
        "previous concept leaders must be a subset of the previous PIT members"
    )


# ---------------------------------------------------------------------------
# KPI-1C three-state previous semantics
# ---------------------------------------------------------------------------
def test_previous_unavailable_yields_none_migration(fx: _Fixture) -> None:
    result = fx.prepare()  # no previous_leader_sets supplied
    scope = _by_key(result, INDUSTRY_KEY)
    diag = scope.payload["diagnostics"]
    attribution = scope.payload["member_attribution"]

    assert diag["previous_leader_status"] == PREVIOUS_UNAVAILABLE
    assert diag["previous_leader_count"] is None
    assert attribution["jaccard"] is None
    assert attribution["leadership_migration"] is None
    assert attribution["leaders"], "today's leaders must still be computed"


def test_previous_computed_empty_yields_migration_one(fx: _Fixture) -> None:
    """An explicitly EMPTY previous set is not the same as unavailable."""
    empty = {FAMILY_INDUSTRY: {INDUSTRY_KEY: frozenset()}}
    result = fx.prepare(previous_leader_sets=empty)
    scope = _by_key(result, INDUSTRY_KEY)
    diag = scope.payload["diagnostics"]
    attribution = scope.payload["member_attribution"]

    assert diag["previous_leader_status"] == PREVIOUS_COMPUTED_EMPTY
    assert diag["previous_leader_count"] == 0
    assert attribution["leaders"], "expected a non-empty current leader set"
    assert attribution["jaccard"] == 0.0
    assert attribution["leadership_migration"] == 1.0


def test_previous_nonempty_yields_exact_set_algebra(fx: _Fixture) -> None:
    prev_day = fx.slots[-2]
    leader_sets = build_previous_leader_sets(
        previous_trade_date=prev_day,
        observations_by_date=fx.observations_by_date,
        edges=fx.edges,
        config=CFG,
    )
    prev = {str(x) for x in leader_sets[FAMILY_INDUSTRY][INDUSTRY_KEY]}
    assert prev

    result = fx.prepare(previous_leader_sets=leader_sets)
    scope = _by_key(result, INDUSTRY_KEY)
    diag = scope.payload["diagnostics"]
    attribution = scope.payload["member_attribution"]

    assert diag["previous_leader_status"] == PREVIOUS_NONEMPTY
    current = set(attribution["leaders"])
    retained = set(attribution["retained"])
    entrants = set(attribution["entrants"])
    exits = set(attribution["exits"])

    assert retained == prev & current
    assert exits == prev - current
    assert entrants == current - prev

    union = prev | current
    assert attribution["jaccard"] == pytest.approx(len(prev & current) / len(union))
    assert attribution["leadership_migration"] == pytest.approx(
        1.0 - len(prev & current) / len(union)
    )


# ---------------------------------------------------------------------------
# KPI-1E complete version binding
# ---------------------------------------------------------------------------
def test_scan_run_version_is_not_caller_nominated(fx: _Fixture) -> None:
    result = fx.prepare()
    scope = _by_key(result, INDUSTRY_KEY)

    with pytest.raises(TypeError):
        build_scope_result_kwargs(
            scan_run_id=uuid4(),
            trade_date=T,
            scope_type=FAMILY_INDUSTRY,
            scope_id=uuid4(),
            scope_name=None,
            payload=scope.payload,
            algorithm_version="auction-v999",  # type: ignore[call-arg]
        )
    assert scope.payload["algorithm_version"] == V32_ALGORITHM_VERSION


def test_non_v32_payload_fails_before_persistence(fx: _Fixture) -> None:
    """A payload carrying a foreign algorithm version must be rejected."""
    result = fx.prepare()
    scope = _by_key(result, INDUSTRY_KEY)
    tampered = dict(scope.payload)
    tampered["algorithm_version"] = "auction-v999"

    with pytest.raises(ValueError, match="algorithm_version"):
        build_scope_result_kwargs(
            scan_run_id=uuid4(),
            trade_date=T,
            scope_type=FAMILY_INDUSTRY,
            scope_id=uuid4(),
            scope_name=None,
            payload=tampered,
        )
