"""PHASE-5.5-LEADERSHIP-INTEGRATION-CORRECTION — real vertical-slice test.

This test closes the integration hole that the previous ``_fake_leadership()`` dict
test masked: it runs the REAL chain

    compute_scope_leadership_batch()        # single family-batch owner
        -> LeadershipMigrationFacts          # domain dataclass (NOT a dict)
        -> serialize_leadership_migration()  # single application serialization boundary
        -> compose_canonical_review_scope()  # requires Mapping (layer.get("status"))

and asserts the composition leadership layer is a real serialized dict whose
entrant/exit reflect the real T-1 -> T migration, and that Member Attribution
consumes the SAME dataclass.

NO fake leadership dict.  NO panji-verify / PG / remote DB / profiler.

The only two monkeypatched boundaries are the genuine IO boundaries:
  * get_previous_trading_day_async   (calendar)
  * prepare_current_scope_observations_batch (neutral fact loader)
The canonical EW owner ``compute_scope_observation`` is exercised for real
(only ``price.equal_weight_return`` is consumed, which depends solely on the
prepared members' price_candidate / return_1d).
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.domain.review.analysis.leadership_migration import (
    LeadershipMigrationFacts,
    serialize_leadership_migration,
)
from app.domain.review.canonical_composition import compose_canonical_review_scope
from app.domain.review.review_capability import ScopeCapability
from app.domain.review.scope_observation import MemberObservation
from app.services import review_leadership_service as ls
from app.services.review_observation_prep_service import PreparedScope

pytestmark = pytest.mark.pure_unit


def _member(member_id: str, amount: float, return_1d: float) -> MemberObservation:
    return MemberObservation(
        member_id=member_id,
        price_candidate=True,
        return_1d=return_1d,
        amount=amount,
        trend=None,
        swing=None,
        internal=None,
        momentum=None,
    )


def _prep(scope_type: str, scope_key: str, td: date, members: list[MemberObservation]) -> PreparedScope:
    member_ids = tuple(m.member_id for m in members)
    return PreparedScope(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_key,
        trade_date=td,
        canonical_t1=None,
        pit_member_ids=member_ids,
        pit_member_ids_t1=member_ids,
        members=tuple(members),
        t1_membership_available=True,
        pit_status_t="READY",
        pit_status_t1="READY",
        diagnostics=(),
        event_coverage_member_ids=(),
    )


class _ScopeSpec:
    def __init__(self, scope_type: str, scope_key: str, member_ids: list[str]) -> None:
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.scope_name = scope_key
        self.member_ids = tuple(member_ids)


T1 = date(2024, 1, 2)
T = date(2024, 1, 3)
SCOPE_KEY = "sw_electronics"


def _build_fixture_series() -> dict[str, list[PreparedScope]]:
    # T-1 leader set = {A, B, C}; T leader set = {B, C, D} -> entrant D, exit A
    t1_members = [_member("A", 30.0, 0.02), _member("B", 30.0, 0.01), _member("C", 40.0, -0.01)]
    t_members = [_member("B", 30.0, 0.01), _member("C", 40.0, -0.01), _member("D", 50.0, 0.03)]
    return {
        SCOPE_KEY: [
            _prep("industry_l2", SCOPE_KEY, T1, t1_members),
            _prep("industry_l2", SCOPE_KEY, T, t_members),
        ]
    }


async def test_real_t1_batch_to_composition_serialization():
    series = _build_fixture_series()
    spec = _ScopeSpec("industry_l2", SCOPE_KEY, ["A", "B", "C", "D"])

    async def fake_prev_day(session, ref_date, **_kw):
        return T1

    async def fake_prep(session, trade_date, specs, *, trade_dates=None, **_kw):
        # return series keyed by scope_key (single-date unwrap avoided because
        # we pass two trade_dates -> dict[str, list[PreparedScope]])
        return {s.scope_key: series[s.scope_key] for s in specs}

    with pytest.MonkeyPatch().context() as mp:
        # get_previous_trading_day_async is imported locally inside the batch fn,
        # so patch the canonical owner module it resolves to.
        mp.setattr(
            "app.services.calendar_service.get_previous_trading_day_async",
            fake_prev_day,
        )
        mp.setattr(ls, "prepare_current_scope_observations_batch", fake_prep)

        batch_result = await ls.compute_scope_leadership_batch(
            None, T, [spec], source_core_run_id=uuid.uuid4()
        )

    # 1) batch returns the domain dataclass (NOT a dict)
    assert SCOPE_KEY in batch_result
    facts = batch_result[SCOPE_KEY]
    assert isinstance(facts, LeadershipMigrationFacts)
    assert facts.status == "ready"

    # 2) real T-1 -> T migration reflected: recompute expected leader sets with
    #    the SAME canonical owners the batch uses, and assert the batch's
    #    migration equals the canonical T-1 vs T set-difference (proves the batch
    #    wired the real previous snapshot, not a faked migration).
    from app.domain.review.analysis.leadership_migration import (
        compute_leadership_migration,
    )

    t1_prep = series[SCOPE_KEY][0]
    t_prep = series[SCOPE_KEY][1]
    exp_prev = ls._build_snapshot(t1_prep)
    exp_curr = ls._build_snapshot(t_prep)
    exp_migration = compute_leadership_migration(
        previous_snapshot=exp_prev, current_snapshot=exp_curr,
    )
    assert facts.previous_leader_ids == exp_migration.previous_leader_ids
    assert facts.current_leader_ids == exp_migration.current_leader_ids
    assert facts.entrant_ids == exp_migration.entrant_ids
    assert facts.exit_ids == exp_migration.exit_ids
    assert facts.retained_count == exp_migration.retained_count
    # and it is a genuine T-1 vs T difference (not empty / not faked)
    assert set(facts.exit_ids) | set(facts.entrant_ids)  # non-empty transition

    # 3) single serialization boundary -> dict for Composition
    leadership_layer = serialize_leadership_migration(facts)
    assert isinstance(leadership_layer, dict)
    assert leadership_layer["status"] == "ready"
    assert leadership_layer["entrant_ids"] == ["D"]
    assert leadership_layer["exit_ids"] == ["A"]

    # 4) REAL compose consumes the serialized dict (would AttributeError on dataclass)
    capability = ScopeCapability(
        scope_type="industry_l2",
        scope_name=SCOPE_KEY,
        persistence_activated=True,
        current_membership_available=True,
        historical_membership_available=True,
        historical_dynamics_runtime_wired=True,
        leadership_runtime_wired=True,
        member_attribution_available=True,
    )
    composition = compose_canonical_review_scope(
        scope_type="industry_l2",
        scope_key=SCOPE_KEY,
        trade_date=T.isoformat(),
        capability=capability,
        scope_observation={"status": "ready", "price": {}},
        historical_dynamics={"status": "ready"},
        internal_structure_facts={"status": "ready"},
        leadership=leadership_layer,
        member_attribution={"status": "ready"},
    )
    assert composition["leadership"]["status"] == "ready"
    assert composition["leadership"]["entrant_ids"] == ["D"]
    assert composition["leadership"]["exit_ids"] == ["A"]

    # 5) Member Attribution consumes the SAME dataclass (not the serialized dict)
    from app.domain.review.analysis.member_attribution import compute_member_attribution

    members = series[SCOPE_KEY][1].members
    attr = compute_member_attribution(
        members=list(members),
        observation={},
        leadership_migration=facts,
    )
    leadership_check = attr["reconciliation"]["checks"]["leadership"]
    assert leadership_check["resolved"] == "matched"
    assert leadership_check["entrant_ids_match_canonical"] is True
    assert leadership_check["exit_ids_match_canonical"] is True


async def test_real_t1_batch_honest_unavailable_when_t1_missing():
    # T-1 not present in the prepared series -> honest unavailable migration
    t_members = [_member("B", 30.0, 0.01), _member("C", 40.0, -0.01), _member("D", 50.0, 0.03)]
    series = {SCOPE_KEY: [_prep("industry_l2", SCOPE_KEY, T, t_members)]}
    spec = _ScopeSpec("industry_l2", SCOPE_KEY, ["B", "C", "D"])

    async def fake_prev_day(session, ref_date, **_kw):
        return T1

    async def fake_prep(session, trade_date, specs, *, trade_dates=None, **_kw):
        return {s.scope_key: series[s.scope_key] for s in specs}

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            "app.services.calendar_service.get_previous_trading_day_async",
            fake_prev_day,
        )
        mp.setattr(ls, "prepare_current_scope_observations_batch", fake_prep)

        batch_result = await ls.compute_scope_leadership_batch(
            None, T, [spec], source_core_run_id=uuid.uuid4()
        )

    facts = batch_result[SCOPE_KEY]
    assert isinstance(facts, LeadershipMigrationFacts)
    # honest: T-1 missing -> migration NOT faked ready
    assert facts.status == "unavailable"
    layer = serialize_leadership_migration(facts)
    assert layer["status"] == "unavailable"
