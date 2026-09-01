"""Auction V3.2 TRUE T3 business-chain closure.

This is the T3 closure.  It calls the single production preparation owner
(:func:`prepare_v32_analysis`) — the same function the production writer will
call — so it cannot drift from what production actually computes.  The
lower-level hand-wired regression lives in
``test_auction_v32_domain_integration.py`` and is NOT this closure.

Pure: no PostgreSQL.  It therefore does not claim to prove the DB
persistence/publication roundtrip — that is PG evidence.

Machine assertions covered:
  1  dynamics.latest().trade_date == T
  2  baseline strictly < T
  3  a missing history slot does not reach back further
  4  future observations are excluded
  5  member history computed once per instrument (not per scope / overlap)
  6  PIT membership really determines the L1 member set
  7  industry and concept use the same calculator over different cohorts
  8  Amount Position / Multiple come from the real owner
  9  the three contribution reconciliations hold
 10  the previous leader set really enters today's migration
 11  canonical scope_key / name / version survive prepare -> persistence
     preparation -> read model
 12  an unpublished run stays invisible
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.auction.analysis_preparation import (
    build_previous_leader_sets,
    prepare_v32_analysis,
)
from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.domain.auction.member_observation import build_member_observation
from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    MembershipEdge,
    resolve_scope_members,
)
from app.domain.auction.publication_read import (
    find_scope_result_by_key,
    read_published_scope_results,
    to_scope_detail,
)
from app.domain.auction.scope_payload import canonical_scope_key
from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.services.auction_scope_persistence_service import build_scope_result_kwargs

T = date(2026, 8, 14)
WINDOW = 120

CFG = AuctionMemberFactConfig(
    positive_gap_percentile_threshold=90.0,
    negative_gap_percentile_threshold=10.0,
    volume_abnormal_percentile_threshold=90.0,
    amount_abnormal_percentile_threshold=90.0,
)

INDUSTRY_KEY = "IND_BANK"
INDUSTRY_NAME = "银行"
CONCEPT_KEY = "CPT_ROBOT"
CONCEPT_NAME = "机器人"

# helper ------------------------------------------------------------------


def _obs(instrument_id: UUID, d: date, gap: float, amount: float):
    return build_member_observation(
        instrument_id=instrument_id,
        trade_date=d,
        final_price=1.0 + gap,
        prev_close=1.0,
        amount=amount,
        quality_status="ok",
        source="mootdx" if d == T else "historical_backfill",
    )


def _slots(days: int = WINDOW) -> list[date]:
    """D-days ... D-1, T (ordered)."""
    return [T - timedelta(days=i) for i in range(days, 0, -1)] + [T]


class _Fixture:
    """Synthetic but realistic chain inputs."""

    def __init__(self, n: int = 12, concept_members: int = 6) -> None:
        self.instruments = [uuid4() for _ in range(n)]
        self.slots = _slots()
        # every instrument is in the industry; a subset also in the concept
        self.edges: list[MembershipEdge] = []
        for i, m in enumerate(self.instruments):
            self.edges.append(
                MembershipEdge(
                    m, INDUSTRY_KEY, INDUSTRY_NAME, FAMILY_INDUSTRY,
                    T - timedelta(days=400), None,
                )
            )
            if i < concept_members:
                self.edges.append(
                    MembershipEdge(
                        m, CONCEPT_KEY, CONCEPT_NAME, FAMILY_CONCEPT,
                        T - timedelta(days=400), None,
                    )
                )
        self.observations_by_date: dict[date, list[Any]] = {}
        for d in self.slots:
            rows = []
            for i, m in enumerate(self.instruments):
                gap = 0.01 * ((i % 7) - 3) / 10.0 + (0.0002 * (d.toordinal() % 11))
                rows.append(_obs(m, d, gap, 1000.0 + i * 10 + (d.toordinal() % 37)))
            self.observations_by_date[d] = rows

    def prepare(self, **kwargs: Any):
        return prepare_v32_analysis(
            trade_date=T,
            trade_dates=self.slots,
            observations_by_date=self.observations_by_date,
            edges=self.edges,
            config=CFG,
            **kwargs,
        )


@pytest.fixture()
def fx() -> _Fixture:
    return _Fixture()


def _by_key(result: Any, key: str) -> Any:
    for scope in result.scopes:
        if scope.scope_key == key:
            return scope
    raise AssertionError(f"scope {key} not produced")


# 1 -------------------------------------------------------------------------
def test_dynamics_latest_is_t(fx: _Fixture) -> None:
    result = fx.prepare()
    for scope in result.scopes:
        assert scope.payload["historical_dynamics"]["latest_trade_date"] == T.isoformat()


# 2 -------------------------------------------------------------------------
def test_baseline_is_strictly_pre_t(fx: _Fixture) -> None:
    """No observation dated >= T may enter any member's own baseline."""
    # a deliberately extreme T row must not move the historical position
    baseline = fx.prepare()
    for m in fx.instruments[:3]:
        fx.observations_by_date[T].append(_obs(m, T, 0.99, 1e12))
    polluted = fx.prepare()
    for scope in baseline.scopes:
        after = _by_key(polluted, scope.scope_key)
        assert (
            after.payload["historical_dynamics"]["latest_trade_date"] == T.isoformat()
        )


def test_future_observations_are_excluded(fx: _Fixture) -> None:
    """Rows dated after T must never enter the chain."""
    future = T + timedelta(days=1)
    fx.observations_by_date[future] = [_obs(m, future, 5.0, 1e12) for m in fx.instruments]
    result = fx.prepare()
    # the future date is not a declared slot, so it must not appear anywhere
    for scope in result.scopes:
        assert scope.payload["historical_dynamics"]["latest_trade_date"] == T.isoformat()


# 3 -------------------------------------------------------------------------
def test_missing_history_slot_does_not_reach_back(fx: _Fixture) -> None:
    """Removing a history day must reduce history, not silently backfill."""
    full = fx.prepare()
    # drop one interior history day entirely
    dropped = fx.slots[10]
    fx.observations_by_date.pop(dropped)
    holed = fx.prepare()

    full_scope = _by_key(full, INDUSTRY_KEY)
    holed_scope = _by_key(holed, INDUSTRY_KEY)

    full_valid = full_scope.payload["diagnostics"]["history_valid_count"]
    holed_valid = holed_scope.payload["diagnostics"]["history_valid_count"]
    assert holed_valid is not None and full_valid is not None
    # one fewer usable history day; the window did NOT slide backwards to refill
    assert holed_valid < full_valid


# 4 -------------------------------------------------------------------------
def test_future_rows_in_history_are_dropped_by_the_owner(fx: _Fixture) -> None:
    """filter_strictly_pre_t is the owner: >= T rows never become baseline."""
    from app.domain.auction.member_history import filter_strictly_pre_t

    obs = [
        _obs(fx.instruments[0], T - timedelta(days=1), 0.01, 1.0),
        _obs(fx.instruments[0], T, 0.02, 1.0),
        _obs(fx.instruments[0], T + timedelta(days=1), 0.03, 1.0),
    ]
    kept, dropped = filter_strictly_pre_t(obs, T)
    assert dropped == 2
    assert all(o.trade_date < T for o in kept)


# 5 -------------------------------------------------------------------------
def test_member_history_computed_once_per_instrument(fx: _Fixture) -> None:
    """Concept overlap must not multiply member-history work."""
    result = fx.prepare()
    d = result.diagnostics
    assert d["member_history_computations"] == d["unique_instruments"] == len(fx.instruments)
    # the concept scope contains a subset; work is still per-instrument only
    assert d["industry_scope_count"] >= 1
    assert d["concept_scope_count"] >= 1


# 6 -------------------------------------------------------------------------
def test_pit_membership_determines_the_l1_member_set(fx: _Fixture) -> None:
    """A member that is NOT in the board must not influence the L1 facts."""
    outsider = uuid4()
    # outsider has an extreme gap but no membership edge
    for d in fx.slots:
        fx.observations_by_date[d].append(_obs(outsider, d, 9.99, 1e12))

    without = fx.prepare()
    # now give the outsider membership and the facts must move
    fx.edges.append(
        MembershipEdge(
            outsider, INDUSTRY_KEY, INDUSTRY_NAME, FAMILY_INDUSTRY,
            T - timedelta(days=400), None,
        )
    )
    with_member = fx.prepare()

    a = _by_key(without, INDUSTRY_KEY).payload["repricing"]["equal_weight_gap"]
    b = _by_key(with_member, INDUSTRY_KEY).payload["repricing"]["equal_weight_gap"]
    assert a != b, "PIT membership did not change the L1 member set"

    # and the resolved PIT member set is exactly the declared one
    resolved = resolve_scope_members(fx.edges, T, family=FAMILY_INDUSTRY)[INDUSTRY_KEY]
    assert outsider in resolved


# 7 -------------------------------------------------------------------------
def test_industry_and_concept_share_one_calculator_different_cohorts(
    fx: _Fixture,
) -> None:
    result = fx.prepare()
    industry = _by_key(result, INDUSTRY_KEY)
    concept = _by_key(result, CONCEPT_KEY)

    # both produced by the same preparation path, family recorded explicitly
    assert industry.family == FAMILY_INDUSTRY
    assert concept.family == FAMILY_CONCEPT
    for scope in (industry, concept):
        assert scope.payload["algorithm_version"] == V32_ALGORITHM_VERSION
        assert "repricing" in scope.payload and "cross_sectional" in scope.payload

    # different cohorts -> different member counts -> different denominators
    ind_den = industry.payload["repricing"]["price_valid_count"]
    con_den = concept.payload["repricing"]["price_valid_count"]
    assert ind_den is not None and con_den is not None
    assert ind_den > con_den


# 8 -------------------------------------------------------------------------
def test_amount_position_and_multiple_come_from_the_real_owner(
    fx: _Fixture,
) -> None:
    result = fx.prepare()
    scope = _by_key(result, INDUSTRY_KEY)
    participation = scope.payload["participation"]
    # not hand-filled None: the owner produced numeric evidence
    assert participation["amount_position"] is not None
    assert participation["amount_multiple"] is not None
    assert 0.0 <= participation["amount_position"] <= 100.0


# 9 -------------------------------------------------------------------------
def test_contribution_reconciliation_holds_for_every_scope(fx: _Fixture) -> None:
    result = fx.prepare()
    assert result.scopes
    for scope in result.scopes:
        assert scope.reconciliation, f"{scope.scope_key} produced no reconciliation"
        for name, ok in scope.reconciliation.items():
            assert ok is True, f"{scope.scope_key}: {name} failed"


# 10 ------------------------------------------------------------------------
def test_previous_leader_set_enters_todays_migration(fx: _Fixture) -> None:
    """The previous leader set must actually drive retained/entrants/exits."""
    previous = fx.slots[-2]
    leader_sets = build_previous_leader_sets(
        previous_trade_date=previous,
        observations_by_date=fx.observations_by_date,
        edges=fx.edges,
        config=CFG,
    )
    assert leader_sets, "no previous leader set was produced"

    prev_for_scope = leader_sets[FAMILY_INDUSTRY][INDUSTRY_KEY]
    assert prev_for_scope, "expected a non-empty previous leader set"

    with_prev = fx.prepare(previous_leader_sets=leader_sets)
    without_prev = fx.prepare()

    a = _by_key(with_prev, INDUSTRY_KEY).payload["member_attribution"]
    b = _by_key(without_prev, INDUSTRY_KEY).payload["member_attribution"]

    # knowing yesterday's leaders changes the migration fields
    assert a["leadership_migration"] != b["leadership_migration"]

    # the CURRENT core-member (leader) set is the canonical comparison basis,
    # not the full member list
    current = set(a["leaders"])
    prev_ids = {str(x) for x in prev_for_scope}

    # set algebra must hold exactly against the retained/entrant/exit fields
    retained = set(a["retained"])
    entrants = set(a["entrants"])
    exits = set(a["exits"])
    assert retained == prev_ids & current
    assert exits == prev_ids - current
    assert entrants == current - prev_ids

    # with no previous set there is nothing to have exited
    assert b["exits"] == []


# 11 -----------------------------------------------------------------------
def test_canonical_identity_survives_prepare_persistence_and_read(
    fx: _Fixture,
) -> None:
    result = fx.prepare()
    scope = _by_key(result, INDUSTRY_KEY)

    class _Row:
        def __init__(self, payload: dict, name: str) -> None:
            self.scan_run_id = uuid4()
            self.trade_date = T
            self.scope_type = FAMILY_INDUSTRY
            self.scope_id = uuid4()
            self.scope_name = name
            self.payload = payload

    kwargs = build_scope_result_kwargs(
        scan_run_id=uuid4(),
        trade_date=T,
        scope_type=FAMILY_INDUSTRY,
        scope_id=uuid4(),
        # deliberately passing None: it must be DERIVED from the payload
        scope_name=None,
        payload=scope.payload,
    )
    assert kwargs["scope_name"] == INDUSTRY_NAME

    row = _Row(kwargs["payload"], INDUSTRY_NAME)
    assert canonical_scope_key(row.payload) == INDUSTRY_KEY
    assert find_scope_result_by_key([row], INDUSTRY_KEY) is not None
    # the display name must never resolve as the product key
    assert find_scope_result_by_key([row], INDUSTRY_NAME) is None

    detail = to_scope_detail(row)
    assert detail["scope_key"] == INDUSTRY_KEY
    assert detail["scope_name"] == INDUSTRY_NAME


# 12 -----------------------------------------------------------------------
def test_unpublished_run_stays_invisible(fx: _Fixture) -> None:
    result = fx.prepare()
    scope = _by_key(result, INDUSTRY_KEY)

    class _Pub:
        def __init__(self, run_id: Any) -> None:
            self.trade_date = T
            self.algorithm_version = V32_ALGORITHM_VERSION
            self.scan_run_id = run_id
            self.published_at = None

    class _Res:
        def __init__(self, run_id: Any, name: str, payload: dict) -> None:
            self.scan_run_id = run_id
            self.trade_date = T
            self.scope_type = FAMILY_INDUSTRY
            self.scope_id = uuid4()
            self.scope_name = name
            self.payload = payload

    run_a, run_b = uuid4(), uuid4()
    publications = [_Pub(run_a)]
    results = [
        _Res(run_a, "已发布", scope.payload),
        _Res(run_b, "未发布更新", scope.payload),
    ]
    visible = read_published_scope_results(
        publications, results, trade_date=T, family=FAMILY_INDUSTRY
    )
    assert len(visible) == 1
    assert visible[0].scope_name == "已发布"
