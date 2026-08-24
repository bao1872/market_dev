"""Slice 4A3 — Board Event Freshness migration parity test (pure-unit).

Migrates ``pyramid_v2.freshness`` from the legacy Board producer into the
Unified Review canonical top-level ``freshness`` section with 100% Board
semantics (NO formula change).

Hard gate: Review PRODUCTION code must NOT import BoardAnalysisService.  This
test imports the Board producer ONLY as an oracle (allowed by the migration
spec): the constants (``_EVENT_DIMENSION_MAP`` / ``_DIMENSION_WINDOW`` /
``_event_dimension``) and a pure extraction of its in-memory aggregation loop.

Covered parity (field-level, exact):

- empty ready universe
- event today / days_ago == 5 / == 10 / == 20 (INCLUSIVE calendar-day bounds)
- future event ignored
- CHoCH -> trend
- BOS / OB_* / EQH / EQL -> structure
- SQZ_* / ZERO_CROSS* / MOMENTUM_DIFFUSION -> momentum
- node / NODE* -> chip
- unknown event -> structure
- per-dimension weighted_sum / density / event_count parity
- overall decay_weighted_density parity
- inactive / non-ready member excluded from the scope denominator
- union-level freshness loader query count is constant (one batch read), never
  per-scope (no N+1)
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import (
    _DIMENSION_WINDOW,
    _EVENT_DIMENSION_MAP,
    _event_dimension,
    compute_freshness,
    compute_scope_observation,
)
from app.domain.review.scope_observation import MemberObservation
from app.services.board_analysis_service import (
    _DIMENSION_WINDOW as BOARD_DIMENSION_WINDOW,
)
from app.services.board_analysis_service import (
    _EVENT_DIMENSION_MAP as BOARD_EVENT_DIMENSION_MAP,
)
from app.services.board_analysis_service import _event_dimension as board_event_dimension

T = date(2026, 8, 11)


# --------------------------------------------------------------------------- #
# Board pure extraction oracle
# --------------------------------------------------------------------------- #
def _board_freshness_pure(
    *,
    trade_date: date,
    events: list[tuple[str, str]],
    instrument_count: int,
) -> dict:
    """Pure in-memory extraction of ``board_analysis_service._compute_freshness_density``
    (the loop after the DB fetch), using the Board's OWN constants + mapper so the
    parity proof is against the real Board semantics, not a re-declared copy."""
    out: dict = {
        "today_count": 0,
        "last_5d_count": 0,
        "last_10d_count": 0,
        "last_20d_count": 0,
        "instrument_count": instrument_count,
        "by_dimension": {
            dim: {
                "window_days": BOARD_DIMENSION_WINDOW[dim],
                "event_count": 0,
                "weighted_sum": 0.0,
                "density": 0.0,
            }
            for dim in ("trend", "structure", "momentum", "chip")
        },
        "decay_weighted_density": 0.0,
    }
    if not instrument_count:
        return out
    inst_count = instrument_count
    for etype, etime in events:
        if not etime:
            continue
        try:
            ev_date = date.fromisoformat(etime[:10])
        except ValueError:
            continue
        days_ago = (trade_date - ev_date).days
        if days_ago < 0:
            continue
        out["last_20d_count"] += 1
        if days_ago <= 5:
            out["last_5d_count"] += 1
        if days_ago <= 10:
            out["last_10d_count"] += 1
        if days_ago == 0:
            out["today_count"] += 1
        dim = board_event_dimension(etype)
        d = out["by_dimension"][dim]
        d["event_count"] += 1
        window = d["window_days"]
        w = max(0.0, 1.0 - days_ago / window) if window > 0 else 1.0
        d["weighted_sum"] = round(d["weighted_sum"] + w, 6)
    total_weighted = 0.0
    for d in out["by_dimension"].values():
        d["density"] = (
            round(d["weighted_sum"] / inst_count, 6) if inst_count > 0 else 0.0
        )
        total_weighted += d["weighted_sum"]
    out["decay_weighted_density"] = (
        round(total_weighted / 4 / inst_count, 6) if inst_count > 0 else 0.0
    )
    return out


def _iso(ago_days: int) -> str:
    """ISO event_time for an event ``ago_days`` calendar days before T."""
    return (T - timedelta(days=ago_days)).isoformat() + "T10:00:00"


def _assert_full_parity(review: dict, board: dict) -> None:
    assert review == board


# --------------------------------------------------------------------------- #
# Constants / taxonomy parity
# --------------------------------------------------------------------------- #
def test_event_dimension_map_matches_board_exactly() -> None:
    assert _EVENT_DIMENSION_MAP == BOARD_EVENT_DIMENSION_MAP
    assert _DIMENSION_WINDOW == BOARD_DIMENSION_WINDOW


def test_event_dimension_mapper_parity_for_all_families() -> None:
    types = [
        "CHoCH",
        "BOS", "OB_CREATED", "OB_ENTERED", "OB_MITIGATED", "EQH", "EQL",
        "SQZ_RELEASE", "SQZ_OFF", "MOMENTUM_DIFFUSION",
        "ZERO_CROSS_UP", "ZERO_CROSS_DOWN", "node_cluster_touch",
        "NODE_CREATED", "UNKNOWN_TYPE", "", None,
    ]
    for t in types:
        assert _event_dimension(t) == board_event_dimension(t), (
            f"dimension mismatch for event_type={t!r}: "
            f"review={_event_dimension(t)!r} board={board_event_dimension(t)!r}"
        )


# --------------------------------------------------------------------------- #
# Core parity cases
# --------------------------------------------------------------------------- #
def test_empty_ready_universe_parity() -> None:
    review = compute_freshness(trade_date=T, events=[], instrument_count=0)
    board = _board_freshness_pure(
        trade_date=T, events=[], instrument_count=0
    )
    _assert_full_parity(review, board)
    assert review["instrument_count"] == 0


def test_event_today_parity() -> None:
    events = [("BOS", _iso(0))]
    review = compute_freshness(trade_date=T, events=events, instrument_count=3)
    board = _board_freshness_pure(
        trade_date=T, events=events, instrument_count=3
    )
    _assert_full_parity(review, board)
    assert review["today_count"] == 1
    assert review["last_5d_count"] == 1
    assert review["last_20d_count"] == 1


def test_days_ago_inclusive_boundaries_parity() -> None:
    # days_ago 5 / 10 / 20 are INCLUSIVE calendar days (never <, never trading days).
    #
    # IMPORTANT: this pure ``compute_freshness`` does NOT bound ``days_ago`` to the
    # 20-day window on the high side -- exactly like the Board in-memory loop, which
    # only skips FUTURE events (``days_ago < 0``).  In BOTH producers the ``T-20..T``
    # window is enforced UPSTREAM by the loader SQL bound ``event_time >= T-20``, so
    # a ``days_ago > 20`` event never reaches the loop in production.  We therefore
    # only feed in-window values here; the SQL lower bound is pinned by the loader
    # test (``_load_batch_freshness_events`` uses ``timedelta(days=20)``).
    for ago in (5, 6, 10, 11, 20):
        events = [("BOS", _iso(ago))]
        review = compute_freshness(trade_date=T, events=events, instrument_count=1)
        board = _board_freshness_pure(
            trade_date=T, events=events, instrument_count=1
        )
        _assert_full_parity(review, board)
        # last_20d: INCLUSIVE 0..20 (T-20 still counts; within the loader window).
        assert (review["last_20d_count"] == 1) == (ago <= 20), f"days_ago={ago} 20d"
        # last_10d / last_5d: INCLUSIVE bounds, never trading days.
        assert (review["last_10d_count"] == 1) == (ago <= 10), f"days_ago={ago} 10d"
        assert (review["last_5d_count"] == 1) == (ago <= 5), f"days_ago={ago} 5d"


def test_days_ago_20_chip_weight_zero_parity() -> None:
    # At days_ago == 20 the chip window (20) yields weight 0.0; the event still
    # counts toward event_count / last_20d_count (calendar-inclusive semantics).
    events = [("node_cluster_touch", _iso(20))]
    review = compute_freshness(trade_date=T, events=events, instrument_count=2)
    board = _board_freshness_pure(
        trade_date=T, events=events, instrument_count=2
    )
    _assert_full_parity(review, board)
    assert review["by_dimension"]["chip"]["event_count"] == 1
    assert review["by_dimension"]["chip"]["weighted_sum"] == 0.0
    assert review["last_20d_count"] == 1


def test_future_event_ignored_parity() -> None:
    events = [("BOS", _iso(-1)), ("BOS", _iso(0))]
    review = compute_freshness(trade_date=T, events=events, instrument_count=1)
    board = _board_freshness_pure(
        trade_date=T, events=events, instrument_count=1
    )
    _assert_full_parity(review, board)
    assert review["last_20d_count"] == 1
    assert review["today_count"] == 1


def test_full_composite_stream_parity() -> None:
    events = [
        ("CHoCH", _iso(0)),
        ("BOS", _iso(1)),
        ("OB_CREATED", _iso(2)),
        ("EQH", _iso(3)),
        ("EQL", _iso(4)),
        ("SQZ_RELEASE", _iso(5)),
        ("SQZ_OFF", _iso(6)),
        ("MOMENTUM_DIFFUSION", _iso(7)),
        ("ZERO_CROSS_UP", _iso(8)),
        ("node_cluster_touch", _iso(9)),
        ("UNKNOWN_THING", _iso(10)),
        ("CHoCH", _iso(20)),
    ]
    for instrument_count in (1, 3):
        review = compute_freshness(
            trade_date=T, events=events, instrument_count=instrument_count
        )
        board = _board_freshness_pure(
            trade_date=T, events=events, instrument_count=instrument_count
        )
        _assert_full_parity(review, board)
        # dimension mapping spot checks (parity already proven by _assert_full_parity)
        assert review["by_dimension"]["trend"]["event_count"] == 2       # 2x CHoCH (T & T-20)
        assert review["by_dimension"]["structure"]["event_count"] == 5    # BOS/OB_CREATED/EQH/EQL/UNKNOWN
        assert review["by_dimension"]["momentum"]["event_count"] == 4     # SQZ x2 + MOMENTUM_DIFFUSION + ZERO_CROSS
        assert review["by_dimension"]["chip"]["event_count"] == 1         # node
        # per-dimension weighted_sum / density must be finite and consistent
        for dim in ("trend", "structure", "momentum", "chip"):
            ws = review["by_dimension"][dim]["weighted_sum"]
            den = review["by_dimension"][dim]["density"]
            assert den == round(ws / instrument_count, 6)


# --------------------------------------------------------------------------- #
# Canonical scope integration
# --------------------------------------------------------------------------- #
def _member(
    mid: str,
    *,
    board_ready: bool = True,
) -> MemberObservation:
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=0.01,
        amount=100.0,
        trend=Direction.UP if board_ready else Direction.SIDEWAYS,
        swing=Direction.SIDEWAYS,
        internal=Direction.DOWN,
        momentum=MomentumDirection.FLAT,
        board_current_ready=board_ready,
        trend_strength=1.0 if board_ready else None,
        current_dsa_vwap_dev_pct=0.5 if board_ready else None,
        active_ob_count=1 if board_ready else None,
        current_sqzmom_val=0.5 if board_ready else None,
        current_vol_ratio20=1.0 if board_ready else None,
        current_vol_ratio200=1.0 if board_ready else None,
        current_vol_pct20=50.0 if board_ready else None,
        current_vol_pct200=50.0 if board_ready else None,
    )


def test_scope_observation_freshness_section_instrument_count_is_board_ready() -> None:
    members = [_member("m1", board_ready=True), _member("m2", board_ready=False)]
    freshness_events = [("BOS", _iso(0)), ("CHoCH", _iso(3))]
    obs = compute_scope_observation(
        scope_type="concept",
        scope_key="A",
        trade_date=T,
        pit_member_ids=["m1", "m2"],
        members=members,
        event_coverage_member_ids=None,
        freshness_events=freshness_events,
    )
    assert "freshness" in obs
    freshness = obs["freshness"]
    # inactive / non-ready member excluded from the denominator (Board ready-gate).
    assert freshness["instrument_count"] == 1
    expected = compute_freshness(
        trade_date=T, events=freshness_events, instrument_count=1
    )
    _assert_full_parity(freshness, expected)


def test_scope_observation_no_freshness_history_empty_skeleton() -> None:
    members = [_member("m1", board_ready=True), _member("m2", board_ready=False)]
    obs = compute_scope_observation(
        scope_type="concept",
        scope_key="A",
        trade_date=T,
        pit_member_ids=["m1", "m2"],
        members=members,
        event_coverage_member_ids=None,
        freshness_events=None,
    )
    freshness = obs["freshness"]
    assert freshness["instrument_count"] == 1
    assert freshness["today_count"] == 0
    assert freshness["last_5d_count"] == 0
    assert freshness["last_10d_count"] == 0
    assert freshness["last_20d_count"] == 0
    assert freshness["decay_weighted_density"] == 0.0
    for dim in ("trend", "structure", "momentum", "chip"):
        assert freshness["by_dimension"][dim]["event_count"] == 0
        assert freshness["by_dimension"][dim]["weighted_sum"] == 0.0
        assert freshness["by_dimension"][dim]["density"] == 0.0


# --------------------------------------------------------------------------- #
# Union-level batch read (no N+1)
# --------------------------------------------------------------------------- #
class _DummySession:
    """Any unexpected DB access fails the test."""

    async def execute(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("unexpected DB access in union query-count test")


@pytest.mark.asyncio
async def test_freshness_union_loader_query_count_constant_vs_scope_count() -> None:
    import app.services.review_observation_prep_service as prep_mod

    freshness_calls = {"n": 0}

    async def _fake_calendar(session, trade_dates):
        return {d: d - timedelta(days=1) for d in trade_dates}

    async def _fake_states(session, instrument_ids, trade_dates, t1_by_date):
        return {}

    async def _fake_bars(session, instrument_ids, trade_dates):
        return {}

    async def _fake_events(session, instrument_ids, trade_dates):
        return {}

    async def _fake_freshness(session, instrument_ids, trade_date):
        freshness_calls["n"] += 1
        return {
            str(i): [("CHoCH", trade_date.isoformat() + "T10:00:00")]
            for i in instrument_ids
        }

    monkey = pytest.MonkeyPatch()
    monkey.setattr(prep_mod, "_load_batch_calendar", _fake_calendar)
    monkey.setattr(prep_mod, "_load_batch_states", _fake_states)
    monkey.setattr(prep_mod, "_load_batch_bars", _fake_bars)
    monkey.setattr(prep_mod, "_load_batch_events", _fake_events)
    monkey.setattr(prep_mod, "_load_batch_freshness_events", _fake_freshness)
    try:
        u1, u2, u3 = "aaaaaaaa-0000-0000-0000-000000000001", \
                     "aaaaaaaa-0000-0000-0000-000000000002", \
                     "aaaaaaaa-0000-0000-0000-000000000003"
        t_dates = [T - timedelta(days=1), T]
        ctx = await prep_mod.prepare_union_fact_context(
            _DummySession(), t_dates, [u1, u2, u3]
        )
        # ONE union-level batch read regardless of scope count.
        assert freshness_calls["n"] == 1
        assert set(ctx.freshness_events_by_member) == {u1, u2, u3}

        # Two scopes sharing the union members: the pure core slices the shared
        # context, so the freshness loader is NOT called per scope (still 1).
        specs = [
            prep_mod.ScopeReplaySpec(
                scope_type="concept", scope_key="A", scope_name="A",
                member_ids=(u1, u2),
            ),
            prep_mod.ScopeReplaySpec(
                scope_type="concept", scope_key="B", scope_name="B",
                member_ids=(u2, u3),
            ),
        ]
        prepared = prep_mod.build_prepared_scopes_from_union(
            trade_dates=t_dates,
            scope_specs=specs,
            union_ctx=ctx,
        )
        # The pure union core slices the shared context: the freshness loader is
        # STILL called exactly once even across multiple scopes (no N+1).
        assert freshness_calls["n"] == 1
        assert len(prepared["A"]) == 2
        assert len(prepared["B"]) == 2
        # Every PreparedScope carries a freshness stream attribute (empty here
        # because no current-only facts mark members board-ready).
        for scope_key in ("A", "B"):
            for ps in prepared[scope_key]:
                assert ps.freshness_events == ()
    finally:
        monkey.undo()
