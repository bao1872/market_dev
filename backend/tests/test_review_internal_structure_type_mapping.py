"""TYPE-MAPPING Commit 1 (R0-R1) probe helpers — pure unit tests.

Locks the deterministic building blocks of the internal-structure-type mapping
dataset in ``scripts.review_scope_dynamics_probe``:

  * ``_board_to_family``       — concept / industry_l1/l2/l3 family mapping
  * ``_percentile_sorted``     — numpy-linear deterministic percentile
  * ``_size_bucket_for_count`` — deterministic family-internal size buckets
  * ``_stratified_sample_boards`` — deterministic stratified sample + exclusion
  * ``_hist_pct``              — no-future-leak, mid-rank ECDF, MIN_HIST_OBS=20
  * ``_delta5d``               — exact X[T]-X[T-5], never skips missing
  * ``build_internal_structure_type_row`` — unavailable -> None (never 0),
    transparent current_leader_fraction, 8 research cols, no cs_pct

Pure unit: no DB, no network, no dataset IO.  Synthetic inputs only.
"""

from __future__ import annotations

import pytest

from app.domain.review.analysis.leadership_migration import (
    LeadershipMigrationFacts,
)
from scripts.review_scope_dynamics_probe import (
    _IST_MAPPING_MIN_HIST_OBS,
    _IST_MAPPING_RESEARCH_COLS,
    _board_to_family,
    _delta5d,
    _hist_pct,
    _percentile_sorted,
    _size_bucket_for_count,
    _stratified_sample_boards,
    build_internal_structure_type_row,
)

pytestmark = pytest.mark.pure_unit


# ---------------------------------------------------------------------------
# _board_to_family
# ---------------------------------------------------------------------------


def test_board_to_family_concept():
    assert _board_to_family({"type": "concept", "id": "c1"}) == "concept"


@pytest.mark.parametrize("level,expected", [("L1", "industry_l1"), ("L2", "industry_l2"), ("L3", "industry_l3")])
def test_board_to_family_industry_levels(level, expected):
    assert _board_to_family({"type": "industry", "hierarchy_level": level}) == expected


def test_board_to_family_unknown_type_raises():
    with pytest.raises(ValueError):
        _board_to_family({"type": "market", "id": "m1"})


def test_board_to_family_industry_missing_level_raises():
    with pytest.raises(ValueError):
        _board_to_family({"type": "industry", "id": "i1"})


def test_board_to_family_industry_illegal_level_raises():
    with pytest.raises(ValueError):
        _board_to_family({"type": "industry", "hierarchy_level": "L9"})


# ---------------------------------------------------------------------------
# _percentile_sorted
# ---------------------------------------------------------------------------


def test_percentile_sorted_extremes_and_single():
    assert _percentile_sorted([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert _percentile_sorted([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0
    assert _percentile_sorted([7.5], 0.5) == 7.5


def test_percentile_sorted_linear_interpolation():
    # pos = (4-1)*0.25 = 0.75 -> 10*(1-0.75) + 20*0.75 = 17.5
    assert _percentile_sorted([10.0, 20.0, 30.0, 40.0], 0.25) == pytest.approx(17.5)
    assert _percentile_sorted([1.0, 2.0, 3.0], 0.5) == 2.0  # median


def test_percentile_sorted_invalid_inputs():
    with pytest.raises(ValueError):
        _percentile_sorted([], 0.5)
    with pytest.raises(ValueError):
        _percentile_sorted([1.0, 2.0], 1.5)


# ---------------------------------------------------------------------------
# _size_bucket_for_count
# ---------------------------------------------------------------------------


def test_size_bucket_boundaries():
    assert _size_bucket_for_count(5, 10.0, 20.0) == "small"
    assert _size_bucket_for_count(10, 10.0, 20.0) == "small"  # == small_upper
    assert _size_bucket_for_count(11, 10.0, 20.0) == "medium"
    assert _size_bucket_for_count(20, 10.0, 20.0) == "medium"  # == medium_upper
    assert _size_bucket_for_count(21, 10.0, 20.0) == "large"


def test_size_bucket_zero_raises():
    with pytest.raises(ValueError):
        _size_bucket_for_count(0, 10.0, 20.0)
    with pytest.raises(ValueError):
        _size_bucket_for_count(-3, 10.0, 20.0)


# ---------------------------------------------------------------------------
# _stratified_sample_boards
# ---------------------------------------------------------------------------


def _board(bid: str, btype: str, level: str | None = None, name: str | None = None) -> dict:
    b: dict = {"id": bid, "type": btype, "name": name or bid}
    if level is not None:
        b["hierarchy_level"] = level
    return b


def _memberships(boards: list[dict], counts: dict[str, int]) -> list[dict]:
    rows: list[dict] = []
    for b in boards:
        for k in range(counts.get(str(b["id"]), 0)):
            rows.append({"board_id": b["id"], "instrument_id": f"ins-{b['id']}-{k}"})
    return rows


def _synthetic_dataset():
    """Synthetic boards across the four families, each with distinct buckets + zeros."""
    boards: list[dict] = []
    counts: dict[str, int] = {}
    # concept: 20 small (5/12), 8 medium (25), 5 large (60/100), 2 zero
    for k in range(10):
        bid = f"con-{k:02d}"
        boards.append(_board(bid, "concept"))
        counts[bid] = 5
    for k in range(10):
        bid = f"con-{10 + k:02d}"
        boards.append(_board(bid, "concept"))
        counts[bid] = 12
    for k in range(8):
        bid = f"con-med-{k}"
        boards.append(_board(bid, "concept"))
        counts[bid] = 25
    for k in range(3):
        bid = f"con-large-{k}"
        boards.append(_board(bid, "concept"))
        counts[bid] = 60
    boards.append(_board("con-large", "concept"))
    counts["con-large"] = 100
    boards.append(_board("con-zero-1", "concept"))
    counts["con-zero-1"] = 0
    boards.append(_board("con-zero-2", "concept"))
    counts["con-zero-2"] = 0
    # industry_l1: one bucket (small) with 8 boards + 1 zero
    for k in range(8):
        bid = f"l1-{k:02d}"
        boards.append(_board(bid, "industry", "L1"))
        counts[bid] = 4
    boards.append(_board("l1-zero", "industry", "L1"))
    counts["l1-zero"] = 0
    # industry_l2: 5 small (6), 2 medium (15), 2 large (30), 1 zero
    for k in range(5):
        bid = f"l2-{k:02d}"
        boards.append(_board(bid, "industry", "L2"))
        counts[bid] = 6
    for k in range(2):
        bid = f"l2-med-{k}"
        boards.append(_board(bid, "industry", "L2"))
        counts[bid] = 15
    for k in range(2):
        bid = f"l2-large-{k}"
        boards.append(_board(bid, "industry", "L2"))
        counts[bid] = 30
    boards.append(_board("l2-zero", "industry", "L2"))
    counts["l2-zero"] = 0
    # industry_l3: 4 small, 2 medium, 1 large, 1 zero
    for k in range(4):
        bid = f"l3-{k:02d}"
        boards.append(_board(bid, "industry", "L3"))
        counts[bid] = 5
    for k in range(2):
        bid = f"l3-med-{k}"
        boards.append(_board(bid, "industry", "L3"))
        counts[bid] = 25
    boards.append(_board("l3-large", "industry", "L3"))
    counts["l3-large"] = 90
    boards.append(_board("l3-zero", "industry", "L3"))
    counts["l3-zero"] = 0
    return boards, _memberships(boards, counts)


def test_stratified_sample_determinism():
    boards, mems = _synthetic_dataset()
    s1 = _stratified_sample_boards(boards, mems, target_per_family=10, seed=20260817)
    s2 = _stratified_sample_boards(boards, mems, target_per_family=10, seed=20260817)
    assert [c["scope_key"] for c in s1["scopes"]] == [c["scope_key"] for c in s2["scopes"]]


def test_stratified_sample_seed_sensitivity():
    boards, mems = _synthetic_dataset()
    s1 = _stratified_sample_boards(boards, mems, target_per_family=10, seed=1)
    s2 = _stratified_sample_boards(boards, mems, target_per_family=10, seed=2)
    # 30 small-bucket concept candidates -> different seed almost surely differs
    assert {c["scope_key"] for c in s1["scopes"]} != {c["scope_key"] for c in s2["scopes"]}


def test_stratified_sample_bucket_coverage_and_exclusion():
    boards, mems = _synthetic_dataset()
    out = _stratified_sample_boards(boards, mems, target_per_family=10, seed=20260817)

    # every non-empty bucket must be covered in the selected sample
    selected_by_family: dict[str, set[str]] = {}
    for c in out["scopes"]:
        selected_by_family.setdefault(c["scope_family"], set()).add(c["size_bucket"])
    assert selected_by_family["concept"] == {"small", "medium", "large"}
    assert selected_by_family["industry_l1"] == {"small"}
    assert selected_by_family["industry_l2"] == {"small", "medium", "large"}
    assert selected_by_family["industry_l3"] == {"small", "medium", "large"}

    # zero-member boards excluded and reported
    assert out["excluded_zero_member_count"]["total"] == 5
    assert out["excluded_zero_member_count"]["by_family"] == {
        "concept": 2,
        "industry_l1": 1,
        "industry_l2": 1,
        "industry_l3": 1,
    }

    # each family <= target_per_family
    fam_counts: dict[str, int] = {}
    for c in out["scopes"]:
        fam_counts[c["scope_family"]] = fam_counts.get(c["scope_family"], 0) + 1
    for fam in ("concept", "industry_l1", "industry_l2", "industry_l3"):
        assert fam in fam_counts
        assert fam_counts[fam] <= 10

    # no selected scope has zero members
    assert all(c["member_count"] > 0 for c in out["scopes"])
    # family cutpoints deterministic and present for all families
    assert set(out["family_cutpoints"]) == {
        "concept", "industry_l1", "industry_l2", "industry_l3"
    }


# ---------------------------------------------------------------------------
# _hist_pct
# ---------------------------------------------------------------------------


def test_hist_pct_no_future_leak():
    series = [float(x) for x in range(1, 22)]  # 21 valid values (1..21)
    # at i=20: prefix = 21 values, x=21.0, below=20, equal=1
    assert _hist_pct(series, 20) == pytest.approx((20 + 0.5) / 21)
    # appending a future value must not change the result at i=20
    series.append(999.0)
    assert _hist_pct(series, 20) == pytest.approx((20 + 0.5) / 21)


def test_hist_pct_tie_mid_rank():
    series = [5.0] * 20  # all ties
    # at i=19: below=0, equal=20 -> (0 + 20/2)/20 = 0.5
    assert _hist_pct(series, 19) == pytest.approx(0.5)
    # output in [0,1]
    assert 0.0 <= _hist_pct(series, 19) <= 1.0


def test_hist_pct_min_obs_gate():
    # fewer than MIN_HIST_OBS valid observations -> None
    assert _IST_MAPPING_MIN_HIST_OBS == 20
    series = [float(x) for x in range(10)]
    assert _hist_pct(series, 9) is None


def test_hist_pct_current_invalid():
    series = [float(x) for x in range(20)] + [None]
    assert _hist_pct(series, 20) is None  # 20 valid before but current None
    series[20] = float("nan")
    assert _hist_pct(series, 20) is None  # non-finite current


def test_hist_pct_ignores_none_in_prefix():
    # None inside the prefix is dropped from valid but the window stays
    series = [float(x) for x in range(20)]
    series[5] = None  # 19 valid -> still below MIN_HIST_OBS at i=19
    assert _hist_pct(series, 19) is None


# ---------------------------------------------------------------------------
# _delta5d
# ---------------------------------------------------------------------------


def test_delta5d_exact_index():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0]
    assert _delta5d(series, 10) == 11.0 - 6.0  # X[10] - X[5]
    assert _delta5d(series, 5) == 6.0 - 1.0  # X[5] - X[0]


def test_delta5d_too_short():
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _delta5d(series, 4) is None  # i < 5


def test_delta5d_endpoint_unavailable():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, None, 7.0, 8.0, 9.0, 10.0, 11.0]
    assert _delta5d(series, 10) is None  # X[5] unavailable
    series[10] = None
    assert _delta5d(series, 10) is None  # X[T] unavailable


def test_delta5d_does_not_skip_missing():
    # X[T-5] is None, but X[T-4] is valid -> still None (never skip to 5th valid)
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, None, 8.0, 9.0, 10.0, 11.0, 12.0]
    assert series[6] is None
    assert _delta5d(series, 11) is None  # X[11] - X[6] = None


def test_delta5d_non_finite_endpoint():
    series = [float("nan")] + [float(x) for x in range(1, 11)]
    assert _delta5d(series, 5) is None  # X[0] non-finite


# ---------------------------------------------------------------------------
# build_internal_structure_type_row
# ---------------------------------------------------------------------------


def _foundation(**overrides):
    base = {
        "breadth": {
            "equal_weight_return": 0.01,
            "advance_ratio": 0.6,
            "decline_ratio": 0.3,
            "unchanged_ratio": 0.1,
            "return_dispersion": 0.05,
        },
        "capital_tilt": {
            "equal_weight_return": 0.01,
            "amount_weighted_return": 0.02,
            "capital_tilt": 0.01,
        },
        "concentration": {
            "price_normalized_hhi": 0.2,
            "amount_normalized_hhi": 0.3,
        },
    }
    for key, val in overrides.items():
        section, field = key.split(".")
        base[section][field] = val
    return base


def _mf(status: str, **kw) -> LeadershipMigrationFacts:
    defaults = dict(
        trade_date="2026-08-17",
        status=status,
        reason=None,
        coverage=1.0,
        previous_direction=1,
        current_direction=1,
        previous_rankable_count=20,
        current_rankable_count=20,
        previous_leader_count=4,
        current_leader_count=5,
        retained_count=3,
        entrant_count=2,
        exit_count=1,
        previous_retention=0.75,
        jaccard_stability=0.5,
        migration=0.1,
        previous_leader_ids=("a", "b", "c", "d"),
        current_leader_ids=("a", "b", "c", "e"),
        entrant_ids=("e",),
        exit_ids=("d",),
    )
    defaults.update(kw)
    return LeadershipMigrationFacts(**defaults)


def _research():
    return {
        "advance_ratio_hist_pct": 0.5,
        "advance_ratio_delta5d": 0.01,
        "decline_ratio_hist_pct": 0.4,
        "decline_ratio_delta5d": -0.01,
        "price_hhi_hist_pct": 0.6,
        "price_hhi_delta5d": 0.02,
        "migration_hist_pct": 0.3,
        "migration_delta5d": -0.02,
    }


def test_row_ready_fraction_derived():
    row = build_internal_structure_type_row(
        scope_type="concept",
        scope_key="con-00",
        scope_name="Concept 00",
        trade_date="2026-08-17",
        member_count=20,
        size_bucket="medium",
        foundation=_foundation(),
        migration_facts=_mf("ready"),
        research=_research(),
    )
    # fraction = current_leader_count / current_rankable_count = 5 / 20
    assert row["leadership_current_leader_fraction"] == pytest.approx(5 / 20)
    assert row["leadership_migration"] == 0.1
    assert row["leadership_jaccard_stability"] == 0.5
    assert row["leadership_previous_retention"] == 0.75
    assert row["leadership_current_rankable_count"] == 20
    assert row["leadership_current_leader_count"] == 5
    assert row["leadership_status"] == "ready"
    assert row["breadth_available"] is True
    assert row["capital_tilt_available"] is True
    assert row["concentration_available"] is True


def test_row_unavailable_never_zero():
    row = build_internal_structure_type_row(
        scope_type="concept",
        scope_key="con-00",
        scope_name="Concept 00",
        trade_date="2026-08-17",
        member_count=20,
        size_bucket="medium",
        foundation=_foundation(
            **{
                "breadth.advance_ratio": None,
                "breadth.decline_ratio": None,
                "capital_tilt.capital_tilt": None,
                "concentration.price_normalized_hhi": None,
                "concentration.amount_normalized_hhi": None,
            }
        ),
        migration_facts=_mf("unavailable"),
        research={},
    )
    # leadership side: unavailable -> None (never 0 / never a derived value)
    assert row["leadership_status"] == "unavailable"
    assert row["leadership_migration"] is None
    assert row["leadership_jaccard_stability"] is None
    assert row["leadership_previous_retention"] is None
    assert row["leadership_current_leader_fraction"] is None
    assert row["leadership_current_leader_count"] is None
    assert row["leadership_current_rankable_count"] is None
    assert row["leadership_retained_count"] is None
    assert row["leadership_entrant_count"] is None
    assert row["leadership_exit_count"] is None
    assert row["leadership_reason"] is None
    # foundation side: unavailable -> None + available flags False
    assert row["breadth_advance_ratio"] is None
    assert row["breadth_available"] is False
    assert row["capital_tilt"] is None
    assert row["capital_tilt_available"] is False
    assert row["concentration_price_hhi"] is None
    assert row["concentration_available"] is False
    # research: missing keys -> None
    assert row["advance_ratio_hist_pct"] is None
    assert row["advance_ratio_delta5d"] is None
    assert row["migration_hist_pct"] is None


def test_row_research_cols_and_no_cs_pct():
    row = build_internal_structure_type_row(
        scope_type="concept",
        scope_key="con-00",
        scope_name="Concept 00",
        trade_date="2026-08-17",
        member_count=20,
        size_bucket="medium",
        foundation=_foundation(),
        migration_facts=_mf("ready"),
        research=_research(),
    )
    for key in _IST_MAPPING_RESEARCH_COLS:
        assert key in row
    assert len(_IST_MAPPING_RESEARCH_COLS) == 8
    # both breadth directions present, no cross-sectional cs_pct
    assert "advance_ratio_hist_pct" in row and "decline_ratio_hist_pct" in row
    assert "advance_ratio_delta5d" in row and "decline_ratio_delta5d" in row
    assert not any("cs_pct" in k for k in row)
