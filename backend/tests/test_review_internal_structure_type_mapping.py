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
    _IST_CANDIDATE_BOOL_COLS,
    _IST_CANDIDATE_CLASSES,
    _IST_CANDIDATE_FLOAT_COLS,
    _IST_CANDIDATE_INT_COLS,
    _IST_CANDIDATE_STR_COLS,
    _IST_CANDIDATE_VARIANTS,
    _IST_MAPPING_MIN_HIST_OBS,
    _IST_MAPPING_RESEARCH_COLS,
    _IST_SELECT_JOINT_HIST_KEYS,
    _IST_SELECT_READINESS_FEATURE_KEYS,
    _IST_THRESHOLD_GRID,
    _aligned_breadth,
    _aligned_tilt,
    _balanced_central_sensitivity,
    _band_classify,
    _board_to_family,
    _candidate_configs,
    _compute_aligned_features,
    _consecutive_runs,
    _delta5d,
    _evaluate_candidate_variant,
    _hist_pct,
    _multi_hit_and_unmatched,
    _nested_variant,
    _numeric_group_stats,
    _pairwise_overlap,
    _percentile_sorted,
    _pick_spread_replay,
    _ready_unmatched_band_distribution,
    _reference_configs,
    _rotate_fragment_partition,
    _select_replay_picks,
    _sign_direction,
    _size_bucket_for_count,
    _spread_stability,
    _stratified_sample_boards,
    _strict_configs,
    _threshold_perturbation,
    _unmatched_stratification,
    _variant_evidence_flags,
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


def test_stratified_sample_coverage_not_forced_to_max():
    """The per-bucket coverage guarantee must NOT force the bucket's max
    member_count candidate (old ``pool[0]`` behavior biased the sample toward
    the bucket upper edge).  With seeded-random coverage, the max candidate
    (``con-19``, the sorted-first of the concept small bucket) is selected for
    some seeds and skipped for others.  Fixed seeds -> deterministic, no flake.
    """
    boards, mems = _synthetic_dataset()
    selected_any = False
    skipped_any = False
    for seed in range(1, 11):
        out = _stratified_sample_boards(
            boards, mems, target_per_family=10, seed=seed
        )
        keys = {c["scope_key"] for c in out["scopes"]}
        if "con-19" in keys:
            selected_any = True
        else:
            skipped_any = True
    assert skipped_any, "max-member board must not be force-picked every time"
    assert selected_any, "max-member board can still appear via the random draw"


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


def test_row_unavailable_snapshot_current_side_preserved():
    """previous unavailable + current ready: transition unavailable, but the
    current-side leader evidence (count/rankable/fraction) is fully known and
    must be preserved; rate metrics stay None; reason is preserved."""
    mf = _mf(
        "unavailable",
        reason="unavailable_snapshot",
        previous_leader_count=None,        # previous side truly unknown
        previous_leader_ids=None,
        current_leader_count=3,
        current_leader_ids=("x", "y", "z"),
        retained_count=None,
        entrant_count=None,
        exit_count=None,
        previous_retention=None,
        jaccard_stability=None,
        migration=None,
    )
    row = build_internal_structure_type_row(
        scope_type="concept",
        scope_key="con-00",
        scope_name="Concept 00",
        trade_date="2026-08-17",
        member_count=20,
        size_bucket="medium",
        foundation=_foundation(),
        migration_facts=mf,
        research={},
    )
    assert row["leadership_status"] == "unavailable"
    assert row["leadership_reason"] == "unavailable_snapshot"  # preserved
    assert row["leadership_previous_leader_count"] is None      # unknown -> None, never 0
    assert row["leadership_previous_rankable_count"] == 20      # snapshot rankable is a real int
    assert row["leadership_current_leader_count"] == 3          # current-side evidence preserved
    assert row["leadership_current_rankable_count"] == 20
    assert row["leadership_retained_count"] is None
    assert row["leadership_entrant_count"] is None
    assert row["leadership_exit_count"] is None
    assert row["leadership_migration"] is None
    assert row["leadership_jaccard_stability"] is None
    assert row["leadership_previous_retention"] is None
    # fraction is derived from current-side evidence only -> 3/20
    assert row["leadership_current_leader_fraction"] == pytest.approx(3 / 20)


def test_row_empty_leader_set_legal_zero_preserved():
    """ready-empty -> ready-nonempty: reason=empty_leader_set, legal leader_count=0
    and the set-change facts (retained/entrant/exit) are preserved; rate metrics
    are None (fail-closed)."""
    mf = _mf(
        "unavailable",
        reason="empty_leader_set",
        previous_leader_count=0,
        previous_leader_ids=(),
        current_leader_count=3,
        current_leader_ids=("x", "y", "z"),
        retained_count=0,
        entrant_count=3,
        exit_count=0,
        previous_retention=None,
        jaccard_stability=None,
        migration=None,
    )
    row = build_internal_structure_type_row(
        scope_type="concept",
        scope_key="con-00",
        scope_name="Concept 00",
        trade_date="2026-08-17",
        member_count=20,
        size_bucket="medium",
        foundation=_foundation(),
        migration_facts=mf,
        research={},
    )
    assert row["leadership_status"] == "unavailable"
    assert row["leadership_reason"] == "empty_leader_set"  # preserved
    assert row["leadership_previous_leader_count"] == 0     # legal 0 preserved
    assert row["leadership_current_leader_count"] == 3
    assert row["leadership_retained_count"] == 0
    assert row["leadership_entrant_count"] == 3
    assert row["leadership_exit_count"] == 0
    assert row["leadership_migration"] is None
    assert row["leadership_jaccard_stability"] is None
    assert row["leadership_previous_retention"] is None
    assert row["leadership_current_leader_fraction"] == pytest.approx(3 / 20)


def test_row_truly_unknown_stays_none_not_zero():
    """A genuinely unknown side keeps None in the row (never coerced to 0):
    here BOTH sides are unavailable -> leader evidence is None, and with no
    current-side evidence the fraction is None too."""
    mf = _mf(
        "unavailable",
        reason="unavailable_snapshot",
        previous_leader_count=None,
        current_leader_count=None,
        retained_count=None,
        entrant_count=None,
        exit_count=None,
        previous_retention=None,
        jaccard_stability=None,
        migration=None,
    )
    row = build_internal_structure_type_row(
        scope_type="concept",
        scope_key="con-00",
        scope_name="Concept 00",
        trade_date="2026-08-17",
        member_count=20,
        size_bucket="medium",
        foundation=_foundation(),
        migration_facts=mf,
        research={},
    )
    assert row["leadership_previous_leader_count"] is None
    assert row["leadership_current_leader_count"] is None
    assert row["leadership_retained_count"] is None
    assert row["leadership_entrant_count"] is None
    assert row["leadership_exit_count"] is None
    assert row["leadership_current_leader_fraction"] is None
    assert row["leadership_migration"] is None
    # foundation side unchanged (unavailable foundation -> None + flags False)
    row2 = build_internal_structure_type_row(
        scope_type="concept",
        scope_key="con-00",
        scope_name="Concept 00",
        trade_date="2026-08-17",
        member_count=20,
        size_bucket="medium",
        foundation=_foundation(
            **{
                "breadth.advance_ratio": None,
                "capital_tilt.capital_tilt": None,
                "concentration.price_normalized_hhi": None,
                "concentration.amount_normalized_hhi": None,
            }
        ),
        migration_facts=_mf("ready"),
        research={},
    )
    assert row2["breadth_advance_ratio"] is None
    assert row2["breadth_available"] is False
    assert row2["capital_tilt"] is None
    assert row2["capital_tilt_available"] is False
    assert row2["concentration_price_hhi"] is None
    assert row2["concentration_available"] is False


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


# ---------------------------------------------------------------------------
# TYPE-MAPPING Commit 2 — direction-neutral features + candidate engine
# ---------------------------------------------------------------------------


def _mk_scope_row(ew, adv, dec, tilt=0.1, frac=0.5) -> dict:
    """Minimal mapping-style row consumed by ``_compute_aligned_features``."""
    return {
        "breadth_ew_return": ew,
        "breadth_advance_ratio": adv,
        "breadth_decline_ratio": dec,
        "capital_tilt": tilt,
        "leadership_current_leader_fraction": frac,
    }


def _mk_scope(ew_series, adv_series, dec_series, tilt=None, frac=None) -> list[dict]:
    n = len(ew_series)
    tilt = tilt if tilt is not None else [0.1] * n
    frac = frac if frac is not None else [0.5] * n
    return [
        _mk_scope_row(ew_series[i], adv_series[i], dec_series[i], tilt[i], frac[i])
        for i in range(n)
    ]


def test_sign_direction():
    assert _sign_direction(0.5) == 1
    assert _sign_direction(-0.5) == -1
    assert _sign_direction(0) is None
    assert _sign_direction(None) is None
    assert _sign_direction(float("nan")) is None
    assert _sign_direction(float("inf")) is None


def test_aligned_breadth_direction():
    # up day -> advance ratio; down day -> decline ratio
    assert _aligned_breadth(0.01, 0.6, 0.3) == pytest.approx(0.6)
    assert _aligned_breadth(-0.01, 0.6, 0.3) == pytest.approx(0.3)
    # zero / missing EW -> aligned unavailable (direction-neutral guard)
    assert _aligned_breadth(0.0, 0.6, 0.3) is None
    assert _aligned_breadth(None, 0.6, 0.3) is None
    # source None stays None
    assert _aligned_breadth(0.01, None, 0.3) is None


def test_aligned_tilt_sign():
    assert _aligned_tilt(0.1, 0.02) == pytest.approx(0.1)
    assert _aligned_tilt(0.1, -0.02) == pytest.approx(-0.1)
    assert _aligned_tilt(None, 0.02) is None
    assert _aligned_tilt(0.1, 0.0) is None
    assert _aligned_tilt(0.1, None) is None


def test_compute_aligned_features_length_and_direction():
    rows = _mk_scope(
        [0.01] * 25, [0.6] * 25, [0.3] * 25, tilt=[0.1] * 25
    )
    aligned = _compute_aligned_features(rows)
    assert len(aligned) == 25
    assert aligned[0]["aligned_breadth"] == pytest.approx(0.6)
    assert aligned[0]["aligned_tilt"] == pytest.approx(0.1)
    # every hist_pct is a prefix-only ECDF value in [0, 1]
    for a in aligned:
        assert a["aligned_breadth_hist_pct"] is None or 0.0 <= a["aligned_breadth_hist_pct"] <= 1.0
        assert a["aligned_tilt_hist_pct"] is None or 0.0 <= a["aligned_tilt_hist_pct"] <= 1.0


def test_compute_aligned_features_zero_ew_unavailable():
    rows = _mk_scope([0.01] * 24 + [0.0], [0.6] * 25, [0.3] * 25)
    aligned = _compute_aligned_features(rows)
    assert aligned[-1]["aligned_breadth"] is None
    assert aligned[-1]["aligned_tilt"] is None


def test_aligned_hist_pct_no_future_leak():
    """hist_pct at index i must not depend on values at index > i (prefix-only)."""
    # First 20 up-day observations identical; divergence only at index 20.
    adv_a = [0.5] * 20 + [0.9] + [0.5] * 4
    adv_b = [0.5] * 20 + [0.1] + [0.5] * 4
    fa = _compute_aligned_features(_mk_scope([0.01] * 25, adv_a, [0.3] * 25))
    fb = _compute_aligned_features(_mk_scope([0.01] * 25, adv_b, [0.3] * 25))
    # index 19: 20-obs prefix is identical in both -> identical hist_pct/delta5d
    assert fa[19]["aligned_breadth_hist_pct"] == fb[19]["aligned_breadth_hist_pct"]
    assert fa[19]["aligned_breadth_delta5d"] == fb[19]["aligned_breadth_delta5d"]
    # index 20: the divergent value is now inside the prefix -> must differ
    assert fa[20]["aligned_breadth_hist_pct"] != fb[20]["aligned_breadth_hist_pct"]
    assert fa[20]["aligned_breadth_delta5d"] != fb[20]["aligned_breadth_delta5d"]


def test_evaluate_candidate_variant():
    feats = {"a": 0.9, "b": 0.1, "c": None}
    assert _evaluate_candidate_variant(feats, (("a", ">=", 0.8),), {"HIGH": 0.8})
    assert not _evaluate_candidate_variant(feats, (("a", ">=", 0.95),), {"HIGH": 0.95})
    assert not _evaluate_candidate_variant(feats, (("a", ">=", 0.8), ("b", "<=", 0.05)), {"HIGH": 0.8})
    # None feature -> condition not met (fail-closed), never a hit
    assert not _evaluate_candidate_variant(feats, (("c", ">=", 0.0),), {})
    # unknown feature / unsupported op -> hard error
    with pytest.raises(KeyError):
        _evaluate_candidate_variant(feats, (("zz", ">=", 0.0),), {})
    with pytest.raises(ValueError):
        _evaluate_candidate_variant(feats, (("a", "==", 0.9),), {})


def test_candidate_configs_four_classes_no_balanced_and_deterministic():
    c1 = _candidate_configs()
    c2 = _candidate_configs()
    assert c1 == c2  # deterministic
    classes = {c["candidate"] for c in c1}
    assert classes == set(_IST_CANDIDATE_CLASSES)
    assert "Balanced" not in classes
    # every config threshold slot comes from the grid
    for c in c1:
        assert all(v in _IST_THRESHOLD_GRID[s] for s, v in c["thresholds"].items())
    # every variant from the table appears (sweep covers all slots)
    variant_labels = {c["variant"] for c in c1}
    assert variant_labels == {"A", "B", "C"}


def test_reference_and_strict_configs_use_fixed_sets():
    refs = _reference_configs()
    strict = _strict_configs()
    assert len(refs) == len(_IST_CANDIDATE_VARIANTS) == len(strict)
    for c in refs:
        assert set(c["thresholds"]) == {"HIGH", "LOW", "MID"}
    # strict thresholds are the tightest end of the grid (no freeze decision)
    assert strict[0]["thresholds"]["HIGH"] == 0.90
    assert strict[0]["thresholds"]["LOW"] == 0.10


def test_no_formal_internal_structure_type_fields():
    cols = (
        _IST_CANDIDATE_STR_COLS
        + _IST_CANDIDATE_INT_COLS
        + _IST_CANDIDATE_BOOL_COLS
        + _IST_CANDIDATE_FLOAT_COLS
    )
    # candidate bool columns are all research_candidate_* flags
    for c in _IST_CANDIDATE_BOOL_COLS:
        assert c.startswith("research_candidate_")
    # no formal type column and no Balanced class column anywhere
    assert not any("internal_structure_type" in c for c in cols)
    assert not any("balanced" in c.lower() for c in cols)
    assert not any("balanced" in cl.lower() for cl in _IST_CANDIDATE_CLASSES)


def test_consecutive_runs():
    assert _consecutive_runs([True, True, False, True]) == [2, 1]
    assert _consecutive_runs([False, False]) == []
    assert _consecutive_runs([True]) == [1]
    assert _consecutive_runs([]) == []


def test_pairwise_overlap_jaccard():
    rows = [
        {"scope_key": "s1", "trade_date": "2026-08-17", "kA": True, "kB": False},
        {"scope_key": "s2", "trade_date": "2026-08-17", "kA": True, "kB": True},
        {"scope_key": "s3", "trade_date": "2026-08-17", "kA": True, "kB": True},
        {"scope_key": "s4", "trade_date": "2026-08-17", "kA": False, "kB": True},
        {"scope_key": "s5", "trade_date": "2026-08-17", "kA": False, "kB": False},
    ]
    ov = _pairwise_overlap(rows, "kA", "kB")
    assert ov["a_count"] == 3
    assert ov["b_count"] == 3
    assert ov["intersection"] == 2
    assert ov["union"] == 4
    assert ov["jaccard"] == pytest.approx(0.5)


def test_multi_hit_and_unmatched_no_else():
    """Unmatched is a pure complement (hit_count==0); it must never be emitted
    as an implicit Balanced class."""
    rows = [
        {"scope_key": "s1", "kA": True, "kB": True, "kC": False, "kD": False},   # multi
        {"scope_key": "s2", "kA": True, "kB": False, "kC": False, "kD": False},  # single
        {"scope_key": "s3", "kA": False, "kB": False, "kC": False, "kD": False},  # unmatched
        {"scope_key": "s4", "kA": False, "kB": True, "kC": True, "kD": True},    # multi
        {"scope_key": "s5", "kA": False, "kB": False, "kC": False, "kD": False},  # unmatched
    ]
    out = _multi_hit_and_unmatched(rows, ("kA", "kB", "kC", "kD"))
    assert out["multi_hit_count"] == 2
    assert out["unmatched_count"] == 2
    assert out["multi_hit_rate"] == pytest.approx(0.4)
    assert out["unmatched_rate"] == pytest.approx(0.4)
    # complement math: single + multi + unmatched == total
    assert (5 - out["multi_hit_count"] - out["unmatched_count"]) == 1  # s2 single
    assert "Balanced" not in out


def test_select_replay_picks_conflict_requires_multi_hit_flag():
    """The conflict bucket must be a true multi-class conflict (explicit flag),
    never a bare ref hit."""
    rows = [
        # s1: ref hit + true multi-hit conflict flag
        {"scope_key": "s1", "trade_date": "2026-08-17",
         "ref": True, "strict": True, "vA": True, "vB": True, "vC": True,
         "multi": True},
        # s2: ref hit but single-class (no conflict flag) -> boundary only
        {"scope_key": "s2", "trade_date": "2026-08-18",
         "ref": True, "strict": False, "vA": True, "vB": False, "vC": False,
         "multi": False},
        # s3: not a ref hit at all
        {"scope_key": "s3", "trade_date": "2026-08-19",
         "ref": False, "strict": False, "vA": False, "vB": False, "vC": False,
         "multi": False},
    ]
    picks = _select_replay_picks(
        rows,
        ref_key="ref",
        strict_key="strict",
        variant_keys=("vA", "vB", "vC"),
        multi_hit_key="multi",
    )
    # high_evidence: hits ref AND all variants
    assert [x["scope_key"] for x in picks["high_evidence"]] == ["s1"]
    # boundary: ref hit at reference but not at strict
    assert [x["scope_key"] for x in picks["boundary"]] == ["s2"]
    # conflict: only the row carrying the explicit multi-hit flag
    assert [x["scope_key"] for x in picks["conflict"]] == ["s1"]


# ---------------------------------------------------------------------------
# TYPE-MAPPING Commit 2B — candidate selection + conflict resolution helpers
# ---------------------------------------------------------------------------


def test_band_classify():
    assert _band_classify(0.39) == "low"
    assert _band_classify(0.40) == "mid"
    assert _band_classify(0.60) == "mid"
    assert _band_classify(0.61) == "high"
    assert _band_classify(None) is None
    assert _band_classify(float("nan")) is None


def test_threshold_perturbation():
    out = _threshold_perturbation([0.10, 0.12, 0.30])
    assert out["min"] == pytest.approx(0.10)
    assert out["max"] == pytest.approx(0.30)
    assert out["range"] == pytest.approx(0.20)
    assert out["count"] == 3
    assert _threshold_perturbation([])["count"] == 0


def test_spread_stability():
    out = _spread_stability({"a": 0.05, "b": 0.10, "c": 0.15})
    assert out["spread"] == pytest.approx(0.10)
    assert out["max"] == pytest.approx(0.15)
    assert out["min"] == pytest.approx(0.05)
    # constant group -> cv 0
    out2 = _spread_stability({"a": 0.1, "b": 0.1})
    assert out2["spread"] == pytest.approx(0.0)
    assert out2["cv"] == pytest.approx(0.0)


def test_nested_variant():
    rows = [
        {"scope_key": "s1", "trade_date": "2026-08-17", "vA": True, "vB": True, "vC": True},
        {"scope_key": "s2", "trade_date": "2026-08-17", "vA": True, "vB": True, "vC": False},
        {"scope_key": "s3", "trade_date": "2026-08-17", "vA": True, "vB": False, "vC": False},
        {"scope_key": "s4", "trade_date": "2026-08-17", "vA": False, "vB": False, "vC": False},
    ]
    nested = _nested_variant(rows, ("vA", "vB", "vC"))
    # strict superset chain: vC={s1} < vB={s1,s2} < vA={s1,s2,s3}
    # first strict superset in key order wins: vC->vA, vB->vA
    assert nested["vC"] == "vA"
    assert nested["vB"] == "vA"
    assert nested["vA"] is None


def test_variant_evidence_flags():
    # zero hits -> fact flag, fixed research status, no verdict
    out = _variant_evidence_flags({"hit_count": 0, "hit_rate": 0.0})
    assert out["evidence_flags"]["zero_reference_hits"] is True
    assert out["research_review_status"] == "REQUIRES_SEMANTIC_REVIEW"
    assert "suggestion" not in out
    # nested_under is a FACT, never a REJECT verdict
    out2 = _variant_evidence_flags(
        {"hit_count": 100, "hit_rate": 0.2, "nested_under": "A",
         "one_day_only_rate": 0.5, "median_run": 3, "multi_hit_involving": 10}
    )
    assert out2["evidence_flags"]["nested_under"] == "A"
    assert out2["evidence_flags"]["rare_reference_hit"] is False
    assert out2["evidence_flags"]["high_overlap"] is False
    assert out2["research_review_status"] == "REQUIRES_SEMANTIC_REVIEW"
    # rare + one-day-heavy + high-overlap warnings still never decide
    out3 = _variant_evidence_flags(
        {"hit_count": 3, "hit_rate": 0.006, "one_day_only_rate": 0.98,
         "median_run": 1, "multi_hit_involving": 3}
    )
    assert out3["evidence_flags"]["rare_reference_hit"] is True
    assert out3["evidence_flags"]["one_day_heavy"] is True
    assert out3["evidence_flags"]["high_overlap"] is True
    assert out3["research_review_status"] == "REQUIRES_SEMANTIC_REVIEW"
    # broad hit-rate warning
    out4 = _variant_evidence_flags(
        {"hit_count": 500, "hit_rate": 0.30, "one_day_only_rate": 0.5,
         "median_run": 4, "multi_hit_involving": 0}
    )
    assert out4["evidence_flags"]["broad_reference_hit"] is True


def test_balanced_central_sensitivity():
    rows = [
        {"scope_type": "concept", "size_bucket": "large", "b": 0.5, "h": 0.5, "m": 0.5, "t": 0.5},
        {"scope_type": "industry_l1", "size_bucket": "small", "b": 0.5, "h": 0.5, "m": 0.5, "t": 0.65},
        {"scope_type": "concept", "size_bucket": "large", "b": 0.45, "h": 0.55, "m": 0.48, "t": 0.52},
        {"scope_type": "industry_l1", "size_bucket": "medium", "b": 0.68, "h": 0.5, "m": 0.5, "t": 0.5},
    ]
    out = _balanced_central_sensitivity(rows, ("b", "h", "m", "t"))
    assert out["total_ready"] == 4
    # p40-60 (width .10): rows 1,3 all central; rows 2,4 exactly 3 central
    w10 = out["widths"]["p40-60"]
    assert w10["four_of_four"]["count"] == 2
    assert w10["four_of_four"]["rate"] == pytest.approx(0.5)
    assert w10["exactly_three"]["count"] == 2
    # p35-65 (width .15): row 2 joins 4-of-4; row 4 still exactly 3
    w15 = out["widths"]["p35-65"]
    assert w15["four_of_four"]["count"] == 3
    assert w15["exactly_three"]["count"] == 1
    # p30-70 (width .20): all four 4-of-4, family×size distribution populated
    w20 = out["widths"]["p30-70"]
    assert w20["four_of_four"]["count"] == 4
    assert w20["exactly_three"]["count"] == 0
    assert w20["four_of_four"]["family_size_distribution"]["concept|large"] == 2
    assert w20["four_of_four"]["family_size_distribution"]["industry_l1|small"] == 1


def test_rotate_fragment_partition():
    rows = [
        {"scope_key": "s1", "trade_date": "2026-08-17", "R": True, "F": False},
        {"scope_key": "s2", "trade_date": "2026-08-17", "R": False, "F": True},
        {"scope_key": "s3", "trade_date": "2026-08-17", "R": True, "F": True},
        {"scope_key": "s4", "trade_date": "2026-08-17", "R": False, "F": False},
        {"scope_key": "s5", "trade_date": "2026-08-17", "R": True, "F": True},
    ]
    part = _rotate_fragment_partition(rows, "R", "F")
    assert part["rotating_only_count"] == 1
    assert part["fragmenting_only_count"] == 1
    assert part["overlap_count"] == 2
    assert part["neither_count"] == 1
    assert all(bool(r["R"]) and bool(r["F"]) for r in part["overlap"])


def test_numeric_group_stats():
    rows = [
        {"v": 1.0}, {"v": 2.0}, {"v": 3.0}, {"v": None}, {"v": 4.0},
    ]
    out = _numeric_group_stats(rows, "v")
    assert out["count"] == 4
    assert out["mean"] == pytest.approx(2.5)
    assert out["median"] == pytest.approx(2.5)
    assert out["min"] == pytest.approx(1.0)
    assert out["max"] == pytest.approx(4.0)
    assert _numeric_group_stats([{"v": None}], "v")["count"] == 0


def test_unmatched_stratification():
    keys = ("a", "b")
    rows = [
        {"a": 0.5, "b": 0.5},     # ready
        {"a": 0.5, "b": None},    # warmup/unavailable
        {"a": None, "b": 0.5},    # warmup/unavailable
        {"a": 0.3, "b": 0.7},     # ready
    ]
    out = _unmatched_stratification(rows, keys)
    assert out["all_features_ready_count"] == 2
    assert out["warmup_unavailable_count"] == 2
    assert out["all_features_ready_rate"] == pytest.approx(0.5)


def test_ready_unmatched_band_distribution():
    keys = ("b", "h", "m", "t")
    rows = [
        {"b": 0.5, "h": 0.5, "m": 0.5, "t": 0.5},  # all mid
        {"b": 0.5, "h": 0.5, "m": 0.5, "t": 0.5},  # all mid
        {"b": 0.1, "h": 0.9, "m": 0.2, "t": 0.8},  # mixed
        {"b": None, "h": 0.5, "m": 0.5, "t": 0.5},  # skipped (None)
    ]
    out = _ready_unmatched_band_distribution(rows, keys)
    assert out["total_ready"] == 3
    assert out["all_mid_count"] == 2
    assert out["all_mid_rate"] == pytest.approx(2 / 3)
    assert out["top_band"] == "mid-mid-mid-mid"
    assert out["band_count"] == 2


def test_pick_spread_replay_across_scopes():
    rows = []
    for sk in ("z-scope", "a-scope"):
        for i in range(6):
            rows.append({"scope_key": sk, "trade_date": f"2026-08-{i+1:02d}"})
    picked = _pick_spread_replay(rows, 5)
    assert len(picked) == 5
    scopes = {p["scope_key"] for p in picked}
    assert scopes == {"z-scope", "a-scope"}  # spread across scopes
    assert _pick_spread_replay([], 5) == []

