"""Slice 4A1R — Board current-state capability migration parity test (pure-unit).

Runs the OLD Board producer (as a test oracle) and the NEW Review scope
aggregation on the SAME synthetic exact-T ``first_pyramid_flat`` member fixtures
and compares the 9 current-state capability groups field-by-field.

Slice 4A1R corrections locked by this file:

1. RUNTIME SOURCE.  Review must resolve the migrated capabilities from the
   exact-T ``StockFeatureSnapshot.summary_payload.first_pyramid_flat`` carried as
   the current-only fact ``board_first_pyramid_flat`` -- NOT from the History
   ``RawMemberFacts.flat_t`` (``previous_state_to_flat``, partial ``fp_*``
   subset).  ``test_runtime_source_is_exact_t_snapshot_not_history_flat``
   deliberately makes the two sources CONFLICT so any regression back to
   ``flat_t`` turns the suite red.

2. BOARD READY GATE.  ``semantics.trend is None`` -> the member is skipped by the
   Board producer, so it must contribute to NO migrated capability (numerator or
   denominator).  Boundary case A.

3. MISSING-VALUE SEMANTICS.  A board-ready member with a missing
   ``momentum_change`` counts as **flat** (boundary B) and with a missing
   ``volume_badge`` counts as **unknown** (boundary C) -- never dropped before
   the denominator is taken.

Hard gate: Review production code must NOT import BoardAnalysisService.  This
test imports it ONLY as an oracle (allowed by the migration spec).

Parity requirements (per spec):
  count exact | enum exact | mean exact | p25/p50/p75 exact | histogram exact
  latest-event up/down/presence exact | missing / null denominator exact
"""
from __future__ import annotations

import ast
import json
import math
from datetime import date
from pathlib import Path

from app.domain.review.scope_observation import (
    FirstPyramidSemanticAdapter,
    compute_scope_observation,
)
from app.services.board_analysis_service import (
    _change_magnitude,
    _compute_concentration,
    _compute_dispersion,
    compute_board_payload,
)
from app.services.observation_prep import (
    _BOARD_CURRENT_ELIGIBLE_KEY,
    _BOARD_CURRENT_FLAT_KEY,
    _BOARD_CURRENT_SYMBOL_KEY,
    RawMemberFacts,
    build_member_observation,
)


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _member(
    *,
    trend_strength,
    dsa_vwap_dev_pct,
    active_ob_count,
    sqzmom_value,
    volume_ratio20,
    volume_ratio200,
    volume_percentile20,
    volume_percentile200,
    momentum_change,
    volume_badge,
    bos_dir=None,
    choch_dir=None,
    ob_dir=None,
    eqh_freshness=None,
    eql_freshness=None,
    trend_direction="up",
    symbol=None,
):
    """Build one exact-T ``first_pyramid_flat`` element in Board input layout.

    The Board producer builds ``FirstPyramidSemanticAdapter(flat)`` directly on
    each ``flat_list`` element and reads EVERY ``fp_*`` from that element's
    TOP-LEVEL.  ``fp_trend_direction`` drives the Board ready gate: ``None`` means
    the member is counted as missing and skipped entirely.

    ``fp_symbol`` is the instrument symbol consumed by the Board
    ``_compute_concentration`` leader_symbol oracle; it must equal the Review
    ``board_current_symbol`` injected below for the 4A2 leader_symbol parity.
    """
    return {
        "fp_trend_strength": trend_strength,
        "fp_dsa_vwap_dev_pct": dsa_vwap_dev_pct,
        "fp_active_ob_count": active_ob_count,
        "fp_sqzmom_value": sqzmom_value,
        "fp_volume_ratio20": volume_ratio20,
        "fp_volume_ratio200": volume_ratio200,
        "fp_volume_percentile20": volume_percentile20,
        "fp_volume_percentile200": volume_percentile200,
        "fp_momentum_change": momentum_change,
        "fp_volume_badge": volume_badge,
        "fp_trend_direction": trend_direction,
        "fp_latest_bos_direction": bos_dir,
        "fp_latest_choch_direction": choch_dir,
        "fp_latest_ob_direction": ob_dir,
        "fp_latest_eqh_freshness": eqh_freshness,
        "fp_latest_eql_freshness": eql_freshness,
        "fp_symbol": symbol,
    }


def _members():
    """Deterministic fixture set: 4 plain members + boundary cases A / B / C.

    A. ``fp_trend_direction=None`` with every other ``fp_*`` populated
       -> Board skips it (missing); Review must skip it too.
    B. board-ready with ``fp_momentum_change=None`` -> Board flat +1.
    C. board-ready with ``fp_volume_badge=None``    -> Board unknown +1.
    """
    flats = [
        _member(
            trend_strength=0.80,
            dsa_vwap_dev_pct=-1.20,
            active_ob_count=3,
            sqzmom_value=0.55,
            volume_ratio20=1.30,
            volume_ratio200=0.90,
            volume_percentile20=82.0,
            volume_percentile200=45.0,
            momentum_change="enhancing",
            volume_badge="high",
            bos_dir="up",
            choch_dir="down",
            ob_dir="up",
            eqh_freshness=2.0,
            eql_freshness=None,
        ),
        _member(
            trend_strength=0.40,
            dsa_vwap_dev_pct=0.50,
            active_ob_count=1,
            sqzmom_value=-0.10,
            volume_ratio20=0.70,
            volume_ratio200=1.10,
            volume_percentile20=35.0,
            volume_percentile200=70.0,
            momentum_change="weakening",
            volume_badge="low",
            bos_dir="down",
            choch_dir=None,
            ob_dir="down",
            eqh_freshness=None,
            eql_freshness=5.0,
        ),
        _member(
            trend_strength=None,
            dsa_vwap_dev_pct=2.00,
            active_ob_count=2,
            sqzmom_value=None,
            volume_ratio20=None,
            volume_ratio200=1.50,
            volume_percentile20=55.0,
            volume_percentile200=10.0,
            momentum_change="flat",
            volume_badge="normal",
            bos_dir=None,
            choch_dir="up",
            ob_dir=None,
            eqh_freshness=None,
            eql_freshness=None,
        ),
        _member(
            trend_strength=0.60,
            dsa_vwap_dev_pct=None,
            active_ob_count=None,
            sqzmom_value=0.20,
            volume_ratio20=1.05,
            volume_ratio200=None,
            volume_percentile20=15.0,
            volume_percentile200=92.0,
            momentum_change="enhancing",
            volume_badge="unknown",
            bos_dir="up",
            choch_dir="up",
            ob_dir="up",
            eqh_freshness=1.0,
            eql_freshness=3.0,
        ),
        # ---- Boundary A: NOT board-ready (trend_direction None), everything else
        # populated with distinctive values.  If Review forgets the ready gate,
        # these values leak into the distributions and parity breaks.
        _member(
            trend_strength=99.0,
            dsa_vwap_dev_pct=99.0,
            active_ob_count=99,
            sqzmom_value=99.0,
            volume_ratio20=99.0,
            volume_ratio200=99.0,
            volume_percentile20=99.0,
            volume_percentile200=99.0,
            momentum_change="enhancing",
            volume_badge="high",
            bos_dir="up",
            choch_dir="up",
            ob_dir="up",
            eqh_freshness=1.0,
            eql_freshness=1.0,
            trend_direction=None,
        ),
        # ---- Boundary B: board-ready, momentum_change missing -> flat.
        _member(
            trend_strength=0.50,
            dsa_vwap_dev_pct=0.10,
            active_ob_count=4,
            sqzmom_value=0.30,
            volume_ratio20=1.10,
            volume_ratio200=1.20,
            volume_percentile20=61.0,
            volume_percentile200=25.0,
            momentum_change=None,
            volume_badge="normal",
            bos_dir=None,
            choch_dir=None,
            ob_dir=None,
        ),
        # ---- Boundary C: board-ready, volume_badge missing -> unknown.
        _member(
            trend_strength=0.30,
            dsa_vwap_dev_pct=-0.40,
            active_ob_count=0,
            sqzmom_value=-0.25,
            volume_ratio20=0.95,
            volume_ratio200=0.85,
            volume_percentile20=5.0,
            volume_percentile200=99.5,
            momentum_change="flat",
            volume_badge=None,
            bos_dir="down",
            choch_dir="down",
            ob_dir="down",
            eqh_freshness=7.0,
            eql_freshness=None,
        ),
    ]
    # 4A2 — Board ``leader_symbol`` oracle reads ``fp_symbol``; Review reads the
    # same value from ``board_current_symbol`` (instrument meta).  Pin them equal
    # so the concentration leader_symbol parity test is meaningful.
    for i, f in enumerate(flats):
        f["fp_symbol"] = f"M{i:03d}"
    return flats


def _history_flat_subset(flat):
    """Simulate History ``previous_state_to_flat`` output for the same member.

    ``previous_state_to_flat`` emits only a PARTIAL ``fp_*`` subset -- notably it
    does NOT emit fp_trend_strength / fp_dsa_vwap_dev_pct / fp_active_ob_count /
    fp_sqzmom_value / fp_volume_badge / fp_volume_ratio200 /
    fp_volume_percentile200 / fp_latest_eq*_freshness.
    """
    return {
        "fp_trend_direction": flat.get("fp_trend_direction"),
        "fp_momentum_change": flat.get("fp_momentum_change"),
        "fp_volume_ratio20": flat.get("fp_volume_ratio20"),
        "fp_volume_percentile20": flat.get("fp_volume_percentile20"),
        "fp_latest_bos_direction": flat.get("fp_latest_bos_direction"),
        "fp_latest_choch_direction": flat.get("fp_latest_choch_direction"),
        "fp_latest_ob_direction": flat.get("fp_latest_ob_direction"),
    }


def _build_review_members(flats, *, history_flats=None, eligible=None):
    """Build Review MemberObservations the way real runtime does.

    ``flat_t`` gets the History projection (partial), and the exact-T Board flat
    is delivered through ``current_only[_BOARD_CURRENT_FLAT_KEY]`` exactly as the
    production loader does.  The Board valid_for_market_aggregation eligibility
    gate (``Instrument.status == "active"``) is carried under
    ``current_only[_BOARD_CURRENT_ELIGIBLE_KEY]``; when ``eligible`` is None all
    members are eligible, otherwise it is a per-member bool list.
    """
    member_list = []
    for i, f in enumerate(flats):
        hist = (
            history_flats[i] if history_flats is not None else _history_flat_subset(f)
        )
        is_eligible = True if eligible is None else bool(eligible[i])
        raw = RawMemberFacts(
            member_id=f"M{i:03d}",
            flat_t=hist,
            close_t=10.0,
            amount_t=10000.0,
            volume_t=1000.0,
            volume_history=(500.0, 600.0, 700.0),
            amount_history=(5000.0, 6000.0, 7000.0),
            flat_t1=None,
            close_t1=None,
            continuous={},
            current_only={
                _BOARD_CURRENT_ELIGIBLE_KEY: is_eligible,
                _BOARD_CURRENT_FLAT_KEY: f,
                # Symbol parity: prefer the flat's fp_symbol (if the test pinned
                # one), else fall back to the deterministic M{i:03d} so Review and
                # the Board oracle read the SAME symbol.
                _BOARD_CURRENT_SYMBOL_KEY: f.get("fp_symbol") or f"M{i:03d}",
            },
        )
        member_list.append(build_member_observation(raw))
    return member_list


def _board_payload(flats):
    return compute_board_payload(flats)


def _review_payload(member_list):
    return compute_scope_observation(
        scope_type="industry",
        scope_key="ind_test",
        trade_date=date(2026, 8, 20),
        pit_member_ids=[f"M{i:03d}" for i in range(len(member_list))],
        members=member_list,
        event_coverage_member_ids=[f"M{i:03d}" for i in range(len(member_list))],
    )


def _both():
    flats = _members()
    return _board_payload(flats), _review_payload(_build_review_members(flats))


# --------------------------------------------------------------------------- #
# Comparison helpers
# --------------------------------------------------------------------------- #
def _close(a, b, tol=1e-9):
    if a is None or b is None:
        return a == b
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


# --------------------------------------------------------------------------- #
# Parity tests (9 capability groups)
# --------------------------------------------------------------------------- #
def test_parity_trend_strength_distribution():
    """1. trend_strength: mean/p25/p50/p75 exact."""
    board, review = _both()
    b = board["trend_strength"]
    r = review["trend"]["trend_strength_distribution"]
    assert _close(b["avg"], r["mean"])
    assert _close(b["p25"], r["p25"])
    assert _close(b["p50"], r["p50"])
    assert _close(b["p75"], r["p75"])


def test_parity_dsa_vwap_dev_pct_distribution():
    """2. dsa_vwap_dev_pct: mean/p25/p50/p75 exact."""
    board, review = _both()
    b = board["vwap_dev_pct"]
    r = review["trend"]["dsa_vwap_dev_pct_distribution"]
    assert _close(b["avg"], r["mean"])
    assert _close(b["p25"], r["p25"])
    assert _close(b["p50"], r["p50"])
    assert _close(b["p75"], r["p75"])


def test_parity_mean_active_orderblock_count():
    """3. structure.current_state.mean_active_orderblock_count exact (NULL-safe).

    Review names this key ``mean_active_orderblock_count`` (not the board's
    ``avg_active_ob_count``) so it cannot collide with the PRD v2.3 whole-payload
    invariant that Review's own removed ``active_ob_count`` fact stays absent.
    The VALUE must still be exactly the board's.
    """
    board, review = _both()
    b = board["structure"]["avg_active_ob_count"]
    r = review["structure"]["current_state"]["mean_active_orderblock_count"]
    assert _close(b, r)


def test_parity_momentum_change_counts():
    """4. momentum.change: enhancing/weakening(fading)/flat/denominator exact."""
    board, review = _both()
    b = board["momentum"]
    r = review["momentum"]["change"]
    assert b["enhancing"] == r["enhancing_count"]
    assert b["fading"] == r["weakening_count"]
    assert b["flat"] == r["flat_count"]
    # Board carries no explicit denominator key; its implicit denominator is the
    # board-ready count, because the trailing ``else`` sends every ready member
    # without a recognized momentum_change into flat.
    assert b["enhancing"] + b["fading"] + b["flat"] == r["denominator"]
    assert r["denominator"] == review["trend"]["board_ready_member_count"]


def test_parity_avg_sqzmom():
    """5. momentum.sqzmom.mean exact (NULL-safe)."""
    board, review = _both()
    b = board["momentum"]["avg_sqzmom"]
    r = review["momentum"]["sqzmom"]["mean"]
    assert _close(b, r)


def test_parity_volume_badge_counts():
    """6. volume badge: high/low/normal/unknown counts exact."""
    board, review = _both()
    b = board["volume"]
    r = review["participation"]["volume"]["badge"]
    assert b["high"] == r["high_count"]
    assert b["low"] == r["low_count"]
    assert b["normal"] == r["normal_count"]
    assert b["unknown"] == r["unknown_count"]
    # The four categories partition the board-ready universe exactly.
    assert (
        r["high_count"] + r["low_count"] + r["normal_count"] + r["unknown_count"]
        == review["trend"]["board_ready_member_count"]
    )


def test_parity_volume_ratio_means():
    """7. volume ratio20/200 mean exact."""
    board, review = _both()
    b = board["volume"]
    r = review["participation"]["volume"]
    assert _close(b["avg_volume_ratio20"], r["ratio20_mean"])
    assert _close(b["avg_volume_ratio200"], r["ratio200_mean"])


def test_parity_volume_percentile_histograms():
    """8. volume percentile20/200 five-bin histogram exact."""
    board, review = _both()
    board_to_review = {
        "<20.0": "lt20",
        "[20.0,40.0)": "20_40",
        "[40.0,60.0)": "40_60",
        "[60.0,80.0)": "60_80",
        ">=80.0": "gte80",
    }
    b20 = board["volume"]["percentile_20_dist"]
    r20 = review["participation"]["volume"]["percentile20_histogram"]
    for bk, rk in board_to_review.items():
        assert b20[bk] == r20[rk], f"percentile20 {bk} != {rk}"
    b200 = board["volume"]["percentile_200_dist"]
    r200 = review["participation"]["volume"]["percentile200_histogram"]
    for bk, rk in board_to_review.items():
        assert b200[bk] == r200[rk], f"percentile200 {bk} != {rk}"


def test_parity_latest_event_state():
    """9. latest-event up/down/presence exact (board payload: structure_events)."""
    board, review = _both()
    b = board["structure_events"]
    r = review["structure"]["current_state"]["latest_events"]
    assert b["bos_up"] == r["bos"]["up"]
    assert b["bos_down"] == r["bos"]["down"]
    assert b["choch_up"] == r["choch"]["up"]
    assert b["choch_down"] == r["choch"]["down"]
    assert b["ob_up"] == r["ob"]["up"]
    assert b["ob_down"] == r["ob"]["down"]
    assert b["eqh_present"] == r["eqh"]
    assert b["eql_present"] == r["eql"]


# --------------------------------------------------------------------------- #
# Slice 4A1R — ready gate / missing-value / runtime-source regression gates
# --------------------------------------------------------------------------- #
def test_board_ready_gate_excludes_non_ready_member():
    """Boundary A: trend_direction=None member is excluded from every group.

    The non-ready fixture carries distinctive ``99.0`` values; if the ready gate
    were missing they would pollute the distributions.  Board's own ``ready``
    count is the authoritative denominator.
    """
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    ready = review["trend"]["board_ready_member_count"]
    # One fixture is deliberately not board-ready.
    assert ready == len(flats) - 1
    assert board["ready_members"] == ready
    assert board["missing_members"] == 1
    assert review["structure"]["current_state"]["board_ready_member_count"] == ready
    # The 99.0 outlier must not reach any distribution.
    assert review["trend"]["trend_strength_distribution"]["p75"] < 1.0
    assert review["trend"]["dsa_vwap_dev_pct_distribution"]["p75"] < 10.0
    assert review["participation"]["volume"]["ratio20_mean"] < 10.0
    assert review["participation"]["volume"]["ratio200_mean"] < 10.0


def test_ready_member_with_missing_momentum_change_counts_as_flat():
    """Boundary B: ready + momentum_change None -> flat (NOT dropped)."""
    flats = _members()
    board_before = _board_payload(flats)
    review_before = _review_payload(_build_review_members(flats))
    # Add one more ready member whose momentum_change is missing.
    extra = _member(
        trend_strength=0.45,
        dsa_vwap_dev_pct=0.05,
        active_ob_count=2,
        sqzmom_value=0.11,
        volume_ratio20=1.0,
        volume_ratio200=1.0,
        volume_percentile20=50.0,
        volume_percentile200=50.0,
        momentum_change=None,
        volume_badge="normal",
    )
    flats2 = [*flats, extra]
    board_after = _board_payload(flats2)
    review_after = _review_payload(_build_review_members(flats2))
    assert board_after["momentum"]["flat"] == board_before["momentum"]["flat"] + 1
    assert (
        review_after["momentum"]["change"]["flat_count"]
        == review_before["momentum"]["change"]["flat_count"] + 1
    )
    # Denominator grows with the ready universe, not with valid-value count.
    assert (
        review_after["momentum"]["change"]["denominator"]
        == review_before["momentum"]["change"]["denominator"] + 1
    )
    assert board_after["momentum"]["flat"] == review_after["momentum"]["change"][
        "flat_count"
    ]


def test_ready_member_with_missing_volume_badge_counts_as_unknown():
    """Boundary C: ready + volume_badge None -> unknown (NOT dropped)."""
    flats = _members()
    board_before = _board_payload(flats)
    review_before = _review_payload(_build_review_members(flats))
    extra = _member(
        trend_strength=0.45,
        dsa_vwap_dev_pct=0.05,
        active_ob_count=2,
        sqzmom_value=0.11,
        volume_ratio20=1.0,
        volume_ratio200=1.0,
        volume_percentile20=50.0,
        volume_percentile200=50.0,
        momentum_change="flat",
        volume_badge=None,
    )
    flats2 = [*flats, extra]
    board_after = _board_payload(flats2)
    review_after = _review_payload(_build_review_members(flats2))
    assert board_after["volume"]["unknown"] == board_before["volume"]["unknown"] + 1
    assert (
        review_after["participation"]["volume"]["badge"]["unknown_count"]
        == review_before["participation"]["volume"]["badge"]["unknown_count"] + 1
    )
    assert (
        board_after["volume"]["unknown"]
        == review_after["participation"]["volume"]["badge"]["unknown_count"]
    )


def test_runtime_source_is_exact_t_snapshot_not_history_flat():
    """RUNTIME-SOURCE REGRESSION GATE.

    ``flat_t`` is given a CONFLICTING History-shaped projection (partial ``fp_*``,
    different values).  Review must report the exact-T snapshot values, so any
    regression that reads the migrated facts back from ``flat_t`` fails here.
    """
    flats = _members()
    # History flat: partial + deliberately conflicting on the keys it does carry.
    conflicting_history = []
    for f in flats:
        hist = _history_flat_subset(f)
        hist["fp_volume_ratio20"] = 42.0
        hist["fp_volume_percentile20"] = 5.0
        hist["fp_momentum_change"] = "weakening"
        conflicting_history.append(hist)

    board = _board_payload(flats)
    review = _review_payload(
        _build_review_members(flats, history_flats=conflicting_history)
    )
    # Snapshot wins on every migrated capability -> full board parity holds.
    assert _close(board["volume"]["avg_volume_ratio20"], review["participation"]["volume"]["ratio20_mean"])
    assert _close(board["volume"]["avg_volume_ratio200"], review["participation"]["volume"]["ratio200_mean"])
    assert board["momentum"]["enhancing"] == review["momentum"]["change"]["enhancing_count"]
    assert board["momentum"]["fading"] == review["momentum"]["change"]["weakening_count"]
    assert board["momentum"]["flat"] == review["momentum"]["change"]["flat_count"]
    b20 = board["volume"]["percentile_20_dist"]
    r20 = review["participation"]["volume"]["percentile20_histogram"]
    assert b20["<20.0"] == r20["lt20"]
    assert b20["[60.0,80.0)"] == r20["60_80"]
    # History-only value 42.0 must never appear.
    assert review["participation"]["volume"]["ratio20_mean"] < 10.0


def test_migrated_capabilities_unavailable_without_exact_t_snapshot():
    """No exact-T snapshot -> capability unavailable, NEVER History-derived.

    ``current_only`` is empty while ``flat_t`` carries a full Board-shaped flat.
    Review must NOT fall back to it: nothing is board-ready and every migrated
    statistic reports empty rather than a History-derived value.
    """
    flats = _members()
    member_list = []
    for i, f in enumerate(flats):
        raw = RawMemberFacts(
            member_id=f"M{i:03d}",
            flat_t=f,  # full board-shaped flat available here on purpose
            close_t=10.0,
            amount_t=10000.0,
            volume_t=1000.0,
            volume_history=(500.0, 600.0, 700.0),
            amount_history=(5000.0, 6000.0, 7000.0),
            flat_t1=None,
            close_t1=None,
            continuous={},
            current_only=None,  # exact-T snapshot absent
        )
        member_list.append(build_member_observation(raw))
    review = _review_payload(member_list)
    assert review["trend"]["board_ready_member_count"] == 0
    assert review["trend"]["trend_strength_distribution"]["valid_count"] == 0
    assert review["trend"]["trend_strength_distribution"]["mean"] is None
    assert (
        review["structure"]["current_state"]["mean_active_orderblock_count"] is None
    )
    assert review["momentum"]["change"]["denominator"] == 0
    assert review["momentum"]["sqzmom"]["mean"] is None
    badge = review["participation"]["volume"]["badge"]
    assert badge["high_count"] == 0
    assert badge["low_count"] == 0
    assert badge["normal_count"] == 0
    assert badge["unknown_count"] == 0
    assert review["participation"]["volume"]["ratio20_mean"] is None


def test_active_ob_count_uses_board_safe_int_semantics():
    """``fp_active_ob_count`` must use the Board ``_safe_int`` integer parse."""
    flats = [
        _member(
            trend_strength=0.5,
            dsa_vwap_dev_pct=0.0,
            active_ob_count="3",  # str -> int("3") == 3 in board
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="flat",
            volume_badge="normal",
        ),
        _member(
            trend_strength=0.5,
            dsa_vwap_dev_pct=0.0,
            active_ob_count="abc",  # unparsable -> None in board
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="flat",
            volume_badge="normal",
        ),
    ]
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    assert _close(
        board["structure"]["avg_active_ob_count"],
        review["structure"]["current_state"]["mean_active_orderblock_count"],
    )


# --------------------------------------------------------------------------- #
# Hard gate: no BoardAnalysisService import in Review production code
# --------------------------------------------------------------------------- #
_REVIEW_PROD_MODULES = (
    "app/domain/review/scope_observation.py",
    "app/services/observation_prep.py",
    "app/services/review_observation_prep_service.py",
)


def test_review_production_code_does_not_import_board_service():
    """AST/source import gate (stronger than runtime attribute inspection).

    Board producer may only be imported by tests, as a parity oracle.
    """
    backend_root = Path(__file__).resolve().parents[1]
    offenders = []
    for rel in _REVIEW_PROD_MODULES:
        path = backend_root / rel
        assert path.exists(), f"missing review production module: {rel}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "board_analysis_service" in alias.name:
                        offenders.append(f"{rel}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "board_analysis_service" in module:
                    offenders.append(f"{rel}: from {module} import ...")
    assert not offenders, "Review production code imports BoardAnalysisService: " + str(
        offenders
    )


# --------------------------------------------------------------------------- #
# Slice 4A1R2 — Board valid_for_market_aggregation eligibility gate
# --------------------------------------------------------------------------- #
def test_board_eligibility_gate_excludes_non_active_member():
    """A PIT member with a full snapshot + trend but ``Instrument.status != active``
    must be excluded from EVERY migrated Board capability universe (numerator AND
    denominator), exactly like the legacy Board ``valid_for_market_aggregation``
    pre-filter.  The Board oracle (``compute_board_payload``) only sees
    already-filtered ``flats`` so it does NOT replicate this gate — the parity
    test must therefore assert the Review-side universe shrink directly.
    """
    flats = _members()  # all have trend present
    # Member 0 is NOT active -> must be dropped from the migrated capability universe.
    member_list = _build_review_members(flats, eligible=[False] + [True] * (len(flats) - 1))
    review = _review_payload(member_list)

    # Board oracle baseline (unfiltered) for comparison.
    board = _board_payload(flats)

    # Review ready count must be one less than the unfiltered Board baseline.
    assert review["trend"]["board_ready_member_count"] == board["ready_members"] - 1
    # Denominator for every migrated capability reflects the shrunk universe.
    assert review["momentum"]["change"]["denominator"] == board["ready_members"] - 1


def test_board_eligibility_gate_all_ineligible_yields_empty_universe():
    """If NO member passes ``Instrument.status == active``, the migrated Board
    capability universe is empty (zero ready, zero denominators) — never a
    fallback to the unfiltered PIT set.
    """
    flats = _members()
    member_list = _build_review_members(flats, eligible=[False] * len(flats))
    review = _review_payload(member_list)
    assert review["trend"]["board_ready_member_count"] == 0
    assert review["momentum"]["change"]["denominator"] == 0
    assert review["participation"]["volume"]["badge"]["unknown_count"] == 0


# --------------------------------------------------------------------------- #
# Slice 4A2 — Board technical-state distribution (concentration / dispersion)
# --------------------------------------------------------------------------- #
def _board_technical_oracle(flats, eligible=None):
    """Faithful Board producer oracle for pyramid_v2 concentration / dispersion.

    Mirrors Board's ``_build_instrument_results``: keep only members that pass
    the Board eligibility (``eligible`` here, default all-active) AND the Board
    ready gate (``FirstPyramidSemanticAdapter(flat).trend is not None``), in the
    SAME encounter order as the ``flats`` list.  The magnitude is Board's
    ``_change_magnitude`` (``|fp_trend_strength|`` > ``|fp_dsa_vwap_dev_pct|`` > 0)
    and the symbol is ``fp_symbol``.  Production Review code MUST NOT call this —
    it is a test-only oracle.  Returns ``(concentration, dispersion)`` dicts.
    """
    instrument_results = []
    for i, f in enumerate(flats):
        if eligible is not None and not eligible[i]:
            continue
        if FirstPyramidSemanticAdapter(f).trend is None:
            continue
        instrument_results.append(
            {
                "change_magnitude": _change_magnitude(f),
                "symbol": f.get("fp_symbol") or f"M{i:03d}",
            }
        )
    return (
        _compute_concentration(instrument_results),
        _compute_dispersion(instrument_results),
    )


def _review_technical_state(flats, eligible=None):
    member_list = _build_review_members(flats, eligible=eligible)
    return _review_payload(member_list)["structure"]["current_state"][
        "technical_state"
    ]


def test_technical_concentration_dispersion_parity_normal_multimember():
    """Normal multi-member board: Review technical_state.concentration /
    dispersion must equal the legacy Board producers EXACTLY (hard gate).

    ``Review technical_state.concentration == OLD Board concentration`` and
    ``== OLD Board dispersion``, field-by-field, after both round(..., 6).
    """
    flats = _members()
    b_conc, b_disp = _board_technical_oracle(flats)
    review = _review_technical_state(flats)

    # Distinct from Review's price/amount concentration (must NOT be reused).
    assert "concentration" in review
    assert "dispersion" in review
    assert review["concentration"] == b_conc
    assert review["dispersion"] == b_disp
    # Leader symbol preserved (instrument symbol, not member UUID).
    assert review["concentration"]["leader_symbol"] == b_conc["leader_symbol"]


def test_technical_magnitude_prefers_trend_strength_over_larger_vwap():
    """Magnitude priority is STRICT: ``|trend_strength|`` wins even when the
    ``|dsa_vwap_dev_pct|`` is far larger.  Board uses the same priority, so the
    resulting concentration/dispersion must still match field-by-field.
    """
    flats = [
        _member(
            trend_strength=0.1,  # tiny trend strength
            dsa_vwap_dev_pct=9.0,  # huge vwap deviation, must be IGNORED
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="up",
            volume_badge="normal",
            symbol="A",
        ),
        _member(
            trend_strength=0.5,
            dsa_vwap_dev_pct=0.2,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="up",
            volume_badge="normal",
            symbol="B",
        ),
    ]
    b_conc, b_disp = _board_technical_oracle(flats)
    review = _review_technical_state(flats)
    # Both magnitudes equal the respective |trend_strength|, not vwap.
    assert b_conc["leader_magnitude"] == 0.5
    assert review["concentration"] == b_conc
    assert review["dispersion"] == b_disp


def test_technical_magnitude_both_missing_yields_zero():
    """Both magnitude inputs missing -> magnitude ``0.0`` (never None), so a
    board-ready member still contributes to concentration/dispersion with a
    zero weight; parity with Board (which also returns 0.0 for missing inputs).
    """
    flats = [
        _member(
            trend_strength=None,
            dsa_vwap_dev_pct=None,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="flat",
            volume_badge="normal",
            symbol="A",
        )
        for _ in range(3)
    ]
    b_conc, b_disp = _board_technical_oracle(flats)
    review = _review_technical_state(flats)
    assert b_conc["hhi"] == 0.0
    assert review["concentration"] == b_conc
    assert review["dispersion"] == b_disp


def test_technical_magnitude_abs_of_negative():
    """Negative trend_strength must be taken absolute, matching Board."""
    flats = [
        _member(
            trend_strength=-0.8,
            dsa_vwap_dev_pct=None,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="down",
            volume_badge="normal",
            symbol="A",
        ),
        _member(
            trend_strength=0.3,
            dsa_vwap_dev_pct=None,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="up",
            volume_badge="normal",
            symbol="B",
        ),
    ]
    b_conc, b_disp = _board_technical_oracle(flats)
    review = _review_technical_state(flats)
    assert b_conc["leader_magnitude"] == 0.8
    assert review["concentration"] == b_conc
    assert review["dispersion"] == b_disp


def test_technical_leader_tie_keeps_first_encountered_member():
    """Two members with identical max magnitude: leader stays the FIRST
    encountered (legacy Board ``max`` semantics) — NOT a (magnitude, symbol)
    re-tiebreak.  Parity with Board must hold and the symbol is the first one's.
    """
    flats = [
        _member(
            trend_strength=0.5,
            dsa_vwap_dev_pct=None,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="up",
            volume_badge="normal",
            symbol="FIRST",
        ),
        _member(
            trend_strength=0.5,  # identical magnitude, lexicographically-smaller symbol
            dsa_vwap_dev_pct=None,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="up",
            volume_badge="normal",
            symbol="SECOND",
        ),
    ]
    b_conc, _ = _board_technical_oracle(flats)
    review = _review_technical_state(flats)
    # Both Board and Review must pick the FIRST encountered member's symbol.
    assert b_conc["leader_symbol"] == "FIRST"
    assert review["concentration"]["leader_symbol"] == "FIRST"
    assert review["concentration"] == b_conc


def test_technical_single_member():
    """Single board-ready member -> concentration leader/median = that magnitude,
    dispersion std = 0.0 and cv = 0.0 (population semantics, NOT Review _stdev
    None-for-n<2).  Parity with Board.
    """
    flats = [
        _member(
            trend_strength=0.42,
            dsa_vwap_dev_pct=None,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="up",
            volume_badge="normal",
            symbol="A",
        )
    ]
    b_conc, b_disp = _board_technical_oracle(flats)
    review = _review_technical_state(flats)
    assert b_disp["std"] == 0.0
    assert b_disp["cv"] == 0.0
    assert review["concentration"] == b_conc
    assert review["dispersion"] == b_disp


def test_technical_all_zero_magnitude():
    """All magnitudes 0 but universe non-empty -> count > 0, hhi 0.0,
    leader/median 0.0, gap 0.0; dispersion mean/std 0.0, cv None.  Distinct from
    the empty-universe case.  Parity with Board.
    """
    flats = [
        _member(
            trend_strength=0.0,
            dsa_vwap_dev_pct=None,
            active_ob_count=0,
            sqzmom_value=0.0,
            volume_ratio20=1.0,
            volume_ratio200=1.0,
            volume_percentile20=50.0,
            volume_percentile200=50.0,
            momentum_change="flat",
            volume_badge="normal",
            symbol="A",
        )
        for _ in range(3)
    ]
    b_conc, b_disp = _board_technical_oracle(flats)
    review = _review_technical_state(flats)
    assert b_conc["count"] == 3
    assert b_conc["hhi"] == 0.0
    assert b_disp["mean"] == 0.0
    assert b_disp["cv"] is None
    assert review["concentration"] == b_conc
    assert review["dispersion"] == b_disp


def test_technical_empty_board_ready_universe():
    """No board-ready member -> concentration all-zero/Nones (count 0), dispersion
    all None (count 0).  Distinct from the all-zero-magnitude case.
    """
    flats = _members()
    review = _review_technical_state(flats, eligible=[False] * len(flats))
    assert review["concentration"]["count"] == 0
    assert review["concentration"]["leader_symbol"] is None
    assert review["concentration"]["hhi"] == 0.0
    assert review["concentration"]["top3_contribution"]["denominator"] == 0.0
    assert review["dispersion"]["count"] == 0
    assert review["dispersion"]["mean"] is None
    assert review["dispersion"]["std"] is None
    assert review["dispersion"]["range"] is None


def test_technical_inactive_and_non_ready_members_excluded():
    """Eligibility + ready gate must shrink the technical universe exactly like
    the other migrated capabilities: an inactive (but otherwise board-ready)
    member drops out of concentration/dispersion, while a trend-missing member
    (member 0) is already excluded by the Board ready gate on both sides.
    """
    flats = _members()  # member 0 has trend None -> not board-ready on both sides
    # Make member 1 (board-ready) inactive -> only it is added to the Board-side
    # exclusion that the eligibility gate produces on the Review side.
    eligible = [True] + [False] + [True] * (len(flats) - 2)
    b_conc, b_disp = _board_technical_oracle(flats, eligible=eligible)
    review = _review_technical_state(flats, eligible=eligible)
    # Member 1 dropped by eligibility; member 0 excluded by ready gate on both.
    assert review["concentration"]["count"] == b_conc["count"]
    assert review["dispersion"]["count"] == b_disp["count"]
    assert review["concentration"] == b_conc
    assert review["dispersion"] == b_disp
    # Sanity: one fewer than the unfiltered Board baseline (member 1 dropped).
    b_all, _ = _board_technical_oracle(flats)
    assert b_conc["count"] == b_all["count"] - 1


def test_technical_state_survives_json_roundtrip_persistence():
    """Persistence boundary (observation_payload -> JSONB -> observation_payload)
    must preserve every concentration / dispersion field exactly, including the
    nested numerator/denominator and the round(..., 6) float precision.  This is
    the pure stand-in for the ReviewScopeObservationFact JSONB write/read path;
    the canonical persistence is exercised by the PG verification plan.
    """
    flats = _members()
    review = _review_payload(_build_review_members(flats))[
        "structure"
    ]["current_state"]["technical_state"]

    # Full key set present.
    assert set(review["concentration"].keys()) == {
        "top3_contribution",
        "top5_contribution",
        "hhi",
        "leader_median_gap",
        "leader_symbol",
        "leader_magnitude",
        "median_magnitude",
        "count",
    }
    assert set(review["dispersion"].keys()) == {
        "count",
        "mean",
        "std",
        "cv",
        "p25",
        "p50",
        "p75",
        "iqr",
        "min",
        "max",
        "range",
    }
    # JSONB round-trip stability (the persistence contract).
    restored = json.loads(json.dumps(review))
    assert restored == review
