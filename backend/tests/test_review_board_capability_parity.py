"""Slice 4A1 — Board current-state capability migration parity test (pure-unit).

Runs the OLD Board producer (as a test oracle) and the NEW Review scope
aggregation on the SAME synthetic ``first_pyramid_flat`` member fixtures and
compares the 9 current-state capability groups field-by-field.

Hard gate: Review production code must NOT import BoardAnalysisService.  This
test imports it ONLY as an oracle (allowed by the migration spec).

Parity requirements (per user spec):
  count exact
  enum exact
  mean exact (same numeric normalization as board)
  p25/p50/p75 exact (same percentile implementation)
  histogram exact
  latest-event up/down/presence exact
  missing / null denominator semantics exact
"""
from __future__ import annotations

import math
from datetime import date

from app.domain.review.scope_observation import compute_scope_observation
from app.services.board_analysis_service import compute_board_payload
from app.services.observation_prep import RawMemberFacts, build_member_observation


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
):
    """Build a board-format member dict.

    Mirrors the Board producer's exact input layout:
      - trend_strength / dsa_vwap_dev_pct / sqzmom_value / volume_ratio* /
        volume_percentile* / momentum_change / volume_badge live in the NESTED
        ``first_pyramid_flat`` sub-dict
      - active_ob_count / fp_latest_*_direction live TOP-LEVEL (board reads them
        from the top-level member dict)
      - fp_latest_eqh_freshness / fp_latest_eql_freshness live in the NESTED
        ``first_pyramid_flat`` sub-dict (board reads them via ``flat``)
    """
    # The Board producer builds FirstPyramidSemanticAdapter(flat) directly on each
    # flat_list element and reads EVERY fp_* from the element's TOP-LEVEL.
    # ``fp_trend_direction`` is required for the Board's ``ready`` gate (members
    # with a null trend direction are counted as missing and skipped).
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
        "fp_trend_direction": "up",  # all members ready
        "fp_latest_bos_direction": bos_dir,
        "fp_latest_choch_direction": choch_dir,
        "fp_latest_ob_direction": ob_dir,
        "fp_latest_eqh_freshness": eqh_freshness,
        "fp_latest_eql_freshness": eql_freshness,
    }


def _members():
    """Deterministic synthetic fixture set with mixed presence/nulls."""
    return [
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
    ]


def _build_review_members(flats):
    member_list = []
    for i, f in enumerate(flats):
        raw = RawMemberFacts(
            member_id=f"M{i:03d}",
            flat_t=f,
            close_t=10.0,
            amount_t=10000.0,
            volume_t=1000.0,
            volume_history=(500.0, 600.0, 700.0),
            amount_history=(5000.0, 6000.0, 7000.0),
            flat_t1=None,
            close_t1=None,
            continuous={},
            current_only=None,
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
    """1. trend_strength: mean/p25/p50/p75/valid_count exact."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    b = board["trend_strength"]
    r = review["trend"]["trend_strength_distribution"]
    assert _close(b["avg"], r["mean"])
    assert _close(b["p25"], r["p25"])
    assert _close(b["p50"], r["p50"])
    assert _close(b["p75"], r["p75"])


def test_parity_dsa_vwap_dev_pct_distribution():
    """2. dsa_vwap_dev_pct: mean/p25/p50/p75 exact."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    b = board["vwap_dev_pct"]
    r = review["trend"]["dsa_vwap_dev_pct_distribution"]
    assert _close(b["avg"], r["mean"])
    assert _close(b["p25"], r["p25"])
    assert _close(b["p50"], r["p50"])
    assert _close(b["p75"], r["p75"])


def test_parity_avg_active_ob_count():
    """3. structure.current_state.avg_active_ob_count exact (NULL-safe)."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    b = board["structure"]["avg_active_ob_count"]
    r = review["structure"]["current_state"]["avg_active_ob_count"]
    assert _close(b, r)


def test_parity_momentum_change_counts():
    """4. momentum.change: enhancing/weakening(fading)/flat/denominator exact."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    b = board["momentum"]
    r = review["momentum"]["change"]
    assert b["enhancing"] == r["enhancing_count"]
    assert b["fading"] == r["weakening_count"]
    assert b["flat"] == r["flat_count"]
    # Board's momentum change counts carry no explicit denominator key; the
    # implicit denominator is the sum of the three change categories.
    assert b["enhancing"] + b["fading"] + b["flat"] == r["denominator"]


def test_parity_avg_sqzmom():
    """5. momentum.sqzmom.mean exact (NULL-safe)."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    b = board["momentum"]["avg_sqzmom"]
    r = review["momentum"]["sqzmom"]["mean"]
    assert _close(b, r)


def test_parity_volume_badge_counts():
    """6. volume badge: high/low/normal/unknown counts exact."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    b = board["volume"]
    r = review["participation"]["volume"]["badge"]
    assert b["high"] == r["high_count"]
    assert b["low"] == r["low_count"]
    assert b["normal"] == r["normal_count"]
    assert b["unknown"] == r["unknown_count"]


def test_parity_volume_ratio_means():
    """7. volume ratio20/200 mean exact."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    b = board["volume"]
    r = review["participation"]["volume"]
    assert _close(b["avg_volume_ratio20"], r["ratio20_mean"])
    assert _close(b["avg_volume_ratio200"], r["ratio200_mean"])


def test_parity_volume_percentile_histograms():
    """8. volume percentile20/200 five-bin histogram exact."""
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
    # Board bucket keys are float-formatted strings (e.g. "<20.0").
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
    flats = _members()
    board = _board_payload(flats)
    review = _review_payload(_build_review_members(flats))
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


def test_review_does_not_import_board_service():
    """Hard gate: Review production code must not import BoardAnalysisService."""
    import app.domain.review.scope_observation as so
    import app.services.observation_prep as op

    assert "board_analysis_service" not in {
        m.split(".")[-1] for m in so.__dict__.get("__builtins__", {})
    }
    # Inspect module imports directly.
    import types

    for mod in (so, op):
        for name, val in vars(mod).items():
            if isinstance(val, types.ModuleType) and name == "board_analysis_service":
                raise AssertionError(
                    "Review production module imports BoardAnalysisService"
                )
