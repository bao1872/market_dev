"""Columnar EW Source pure-unit tests (M5-B1, PERF-OOM-M5B1).

Scope A-L gate coverage (explicit):
    A. exact T1 outside analysis axis (R > D on first day).
    B. non-consecutive analysis axis.
    C. missing T close → NaN close_t → price_candidate=False, return invalid.
    D. missing T1 close → NaN close_t1 → return invalid.
    E. zero T1 close → denom epsilon gate fails.
    F. abs(T1) <= 1e-12 → denom epsilon gate fails.
    G. non-finite inputs (inf / nan) → price_candidate / base_valid False.
    H. overflow / non-finite raw return (huge ratio + invalid) → finite(raw) mask.
    I. EW empty member universe (no valid returns on a date/scope) → None/NaN.
    J. EW exact parity with canonical _return_distribution reducer.
    K. sparse membership: deterministic order, dedupe per-scope, unknown ids ignored.
    L. minimal ObservationSeries + Dynamics smoke parity vs canonical
       compute_exact_return / _return_distribution manually-built oracle path.

PURE_UNIT_TEST=1 safe: no DB, no network, no orchestrator.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np
import pytest

pytestmark = pytest.mark.pure_unit


# Canonical oracles — these are the ONLY reference implementations the tests
# are allowed to compare against.  No full MemberObservation rebuild.
from app.services.observation_prep import compute_exact_return
from app.domain.review.scope_observation import _return_distribution

# Columnar core under test.
from app.services.review_historical_ew_columnar import (
    build_required_bar_axis,
    compute_return_matrix,
    build_scope_member_indices,
    compute_scope_ew_matrix,
    build_scope_dynamics_from_ew,
)


# ---------------------------------------------------------------------------
# Helpers: tiny deterministic member ids & date generators.
# ---------------------------------------------------------------------------
M0, M1, M2, M3 = "M0", "M1", "M2", "M3"


def _dates(*iso: str) -> list[date]:
    return [date.fromisoformat(s) for s in iso]


def _build_close_from_rows(
    member_ids: list[str],
    rows: list[tuple[date, dict[str, float]]],
) -> np.ndarray:
    """Construct [R, M] close matrix with NaN fill from explicit rows."""
    mat = np.full((len(rows), len(member_ids)), np.nan, dtype=np.float64)
    col = {m: i for i, m in enumerate(member_ids)}
    for r, (_d, values) in enumerate(rows):
        for m, v in values.items():
            c = col.get(m)
            if c is None:
                continue
            if v is None:
                # treat explicit None as unavailable (NaN)
                continue
            mat[r, c] = float(v)
    return mat


# ===========================================================================
# A — exact T1 outside analysis axis
# ===========================================================================
def test_a_exact_t1_outside_analysis_axis() -> None:
    """First analysis day T1 must be present in required_bar_dates even if it
    is not itself an analysis day."""
    # axis: D0..D2 (3 days).  D0's T-1 is PRE which is NOT in the axis.
    d0, d1, d2 = _dates("2026-02-24", "2026-02-25", "2026-02-26")
    pre = date.fromisoformat("2026-02-23")
    analysis_dates = [d0, d1, d2]
    t1_by_date = {d0: pre, d1: d0, d2: d1}

    required_bar_dates, t_idx, t1_idx = build_required_bar_axis(
        analysis_dates, t1_by_date,
    )

    # Must carry PRE even though it is not in analysis_dates.
    assert pre in required_bar_dates
    assert len(required_bar_dates) == 4  # R = D + 1 in this simple case

    # t_idx maps analysis days in order.
    assert t_idx.tolist() == [
        required_bar_dates.index(d0),
        required_bar_dates.index(d1),
        required_bar_dates.index(d2),
    ]
    # t1_idx for D0 points to PRE row, not -1.
    assert t1_idx[0] == required_bar_dates.index(pre)
    assert t1_idx[1] == required_bar_dates.index(d0)
    assert t1_idx[2] == required_bar_dates.index(d1)

    # --- Now compute real return matrix and verify D0 return IS computable.
    member_ids = [M0]
    # Close values: PRE=100, D0=110, D1=121, D2=133.1
    rows = [
        (pre, {M0: 100.0}),
        (d0, {M0: 110.0}),
        (d1, {M0: 121.0}),
        (d2, {M0: 133.1}),
    ]
    close = _build_close_from_rows(member_ids, rows)
    out = compute_return_matrix(close, t_idx, t1_idx)

    expected_returns = [
        110.0 / 100.0 - 1.0,   # 0.1
        121.0 / 110.0 - 1.0,   # 0.1
        133.1 / 121.0 - 1.0,   # 0.1
    ]
    assert out["return_valid"][:, 0].tolist() == [True, True, True]
    for t in range(3):
        assert math.isclose(out["return_1d"][t, 0], expected_returns[t], rel_tol=0.0, abs_tol=0.0)
        # Oracle check — compute_exact_return on the same (T, T1) scalar pair.
        oracle = compute_exact_return(rows[t_idx[t]][1][M0], rows[t1_idx[t]][1][M0])
        assert math.isclose(out["return_1d"][t, 0], oracle, rel_tol=0.0, abs_tol=0.0)


# ===========================================================================
# B — non-consecutive analysis axis (R != D+1 pattern)
# ===========================================================================
def test_b_non_consecutive_analysis_axis() -> None:
    """If the analysis axis has multiple gaps requiring off-axis T1s, the R set
    still includes every exact T1, and indices are correct."""
    # Analysis axis is D0, D2, D4 (skipping D1, D3).  All T-1s are off-axis.
    d0, d1, d2, d3, d4 = _dates(
        "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06",
    )
    analysis_dates = [d0, d2, d4]
    t1_by_date = {d0: d1, d2: d3, d4: None}  # T1s can be in any calendar order.

    required_bar_dates, t_idx, t1_idx = build_required_bar_axis(
        analysis_dates, t1_by_date,
    )
    expected_set = {d0, d2, d4, d1, d3}  # T1 of d4 is None, not included.
    assert set(required_bar_dates) == expected_set
    assert len(required_bar_dates) == 5

    assert t1_idx[2] == -1  # d4's T1 None maps to -1.
    # Non-consecutive T→idx still land on correct rows.
    assert required_bar_dates[t_idx[0]] == d0
    assert required_bar_dates[t_idx[1]] == d2
    assert required_bar_dates[t_idx[2]] == d4
    assert required_bar_dates[t1_idx[0]] == d1
    assert required_bar_dates[t1_idx[1]] == d3


# ===========================================================================
# C — missing T close
# ===========================================================================
def test_c_missing_t_close() -> None:
    """close_t unavailable → price_candidate False, return invalid."""
    d0, d1 = _dates("2026-02-24", "2026-02-25")
    analysis_dates = [d1]
    t1_by_date = {d1: d0}
    required, t_idx, t1_idx = build_required_bar_axis(analysis_dates, t1_by_date)

    member_ids = [M0, M1]
    # M0 close_t missing, close_t1 available; M1 both available.
    rows = [(d0, {M0: 100.0, M1: 50.0}), (d1, {M0: None, M1: 60.0})]
    close = _build_close_from_rows(member_ids, rows)
    out = compute_return_matrix(close, t_idx, t1_idx)

    assert bool(out["price_candidate"][0, 0]) is False
    assert bool(out["price_candidate"][0, 1]) is True
    assert bool(out["return_valid"][0, 0]) is False
    assert np.isnan(out["return_1d"][0, 0])
    assert not np.isnan(out["return_1d"][0, 1])
    assert math.isclose(out["return_1d"][0, 1], compute_exact_return(60.0, 50.0))


# ===========================================================================
# D — missing T1 close
# ===========================================================================
def test_d_missing_t1_close() -> None:
    """close_t1 NaN → base_valid False, return invalid (price_candidate may be True)."""
    d0, d1 = _dates("2026-02-24", "2026-02-25")
    analysis_dates = [d1]
    t1_by_date = {d1: d0}
    required, t_idx, t1_idx = build_required_bar_axis(analysis_dates, t1_by_date)

    member_ids = [M0]
    rows = [(d0, {M0: None}), (d1, {M0: 110.0})]
    close = _build_close_from_rows(member_ids, rows)
    out = compute_return_matrix(close, t_idx, t1_idx)

    assert bool(out["price_candidate"][0, 0]) is True
    assert bool(out["return_valid"][0, 0]) is False
    assert np.isnan(out["return_1d"][0, 0])
    assert compute_exact_return(110.0, None) is None  # scalar oracle matches


# ===========================================================================
# E — zero T1
# ===========================================================================
def test_e_zero_t1() -> None:
    """close_t1 == 0 exactly → denominator epsilon gate fails."""
    d0, d1 = _dates("2026-02-24", "2026-02-25")
    analysis_dates = [d1]
    t1_by_date = {d1: d0}
    required, t_idx, t1_idx = build_required_bar_axis(analysis_dates, t1_by_date)

    member_ids = [M0]
    rows = [(d0, {M0: 0.0}), (d1, {M0: 1.0})]
    close = _build_close_from_rows(member_ids, rows)
    out = compute_return_matrix(close, t_idx, t1_idx)

    assert bool(out["return_valid"][0, 0]) is False
    assert np.isnan(out["return_1d"][0, 0])
    assert compute_exact_return(1.0, 0.0) is None


# ===========================================================================
# F — abs(T1) <= 1e-12
# ===========================================================================
@pytest.mark.parametrize("small", [1e-13, 5e-14, -1e-13])
def test_f_small_t1_epsilon_gate(small: float) -> None:
    d0, d1 = _dates("2026-02-24", "2026-02-25")
    analysis_dates = [d1]
    t1_by_date = {d1: d0}
    required, t_idx, t1_idx = build_required_bar_axis(analysis_dates, t1_by_date)

    member_ids = [M0]
    rows = [(d0, {M0: small}), (d1, {M0: 100.0})]
    close = _build_close_from_rows(member_ids, rows)
    out = compute_return_matrix(close, t_idx, t1_idx)

    assert bool(out["return_valid"][0, 0]) is False
    assert np.isnan(out["return_1d"][0, 0])
    assert compute_exact_return(100.0, small) is None

    # Boundary on the other side must still PASS.
    rows_ok = [(d0, {M0: 2e-12}), (d1, {M0: 100.0})]
    close_ok = _build_close_from_rows(member_ids, rows_ok)
    out_ok = compute_return_matrix(close_ok, t_idx, t1_idx)
    assert bool(out_ok["return_valid"][0, 0]) is True
    oracle = compute_exact_return(100.0, 2e-12)
    assert oracle is not None
    assert math.isclose(out_ok["return_1d"][0, 0], oracle)


# ===========================================================================
# G — non-finite inputs (NaN / ±inf)
# ===========================================================================
@pytest.mark.parametrize(
    ("ct", "ct1"),
    [
        (float("inf"), 100.0),
        (float("-inf"), 100.0),
        (float("nan"), 100.0),
        (100.0, float("inf")),
        (100.0, float("-inf")),
        (100.0, float("nan")),
        (float("nan"), float("nan")),
    ],
)
def test_g_non_finite_inputs(ct: float, ct1: float) -> None:
    d0, d1 = _dates("2026-02-24", "2026-02-25")
    analysis_dates = [d1]
    t1_by_date = {d1: d0}
    required, t_idx, t1_idx = build_required_bar_axis(analysis_dates, t1_by_date)

    member_ids = [M0]
    rows = [(d0, {M0: ct1}), (d1, {M0: ct})]
    close = _build_close_from_rows(member_ids, rows)
    out = compute_return_matrix(close, t_idx, t1_idx)

    # price_candidate is independent: finite(close_t).
    expected_pc = math.isfinite(ct)
    assert bool(out["price_candidate"][0, 0]) is expected_pc

    # return_valid must be False for all these parametrize.
    assert bool(out["return_valid"][0, 0]) is False
    assert np.isnan(out["return_1d"][0, 0])
    # scalar oracle parity.
    assert compute_exact_return(ct if math.isfinite(ct) else None,
                               ct1 if math.isfinite(ct1) else None) is None


# ===========================================================================
# H — overflow / non-finite raw return  (Layer-1 *effective* parity contract)
# ===========================================================================
def test_h_non_finite_raw_return() -> None:
    """Effective member-return contribution parity gate: overflow → inf.

    Layer-1 parity is NOT ``compute_exact_return()`` raw intermediate
    identity.  It is defined as::

        scalar_raw          = compute_exact_return(close_t, close_t1)
        scalar_effective_valid = isfinite(scalar_raw)

        columnar.return_valid == scalar_effective_valid

        and where valid:
            columnar.return_1d == scalar_raw

        where invalid:
            columnar.return_1d = NaN

    This test proves:

    1. scalar ``compute_exact_return`` itself produces a non-finite value on
       overflow (so the raw intermediate is *not* unavailable).
    2. the canonical downstream scalar path would drop it anyway via
       ``_finite_or_none(m.return_1d)`` in ``compute_scope_observation``.
    3. columnar ``return_valid`` matches ``scalar_effective_valid`` (both
       False).
    4. columnar ``return_1d`` is NaN (unavailable) in this branch, matching
       the *effective* scalar universe contribution, not the raw inf
       intermediate.
    """
    from app.domain.review.scope_observation import _finite_or_none

    member_ids = [M0]
    # Base case: T1 = 1.5e-12 (just above epsilon gate) and T = MAX so that
    # raw_return = T / T1 - 1 overflows to +inf with finite inputs.
    close_T1 = 1.5e-12
    close_T = float(np.finfo(np.float64).max)

    # --- Scalar oracle first.
    scalar_raw = compute_exact_return(close_T, close_T1)
    # (1) Prove scalar raw intermediate IS non-finite.
    assert scalar_raw is not None
    assert math.isinf(scalar_raw), (
        "assumption failure: expected overflow on MAX / 1.5e-12"
    )
    # (2) Prove canonical scope_observation downstream WOULD drop it via the
    #     same _finite_or_none wrapper used in compute_scope_observation.
    scalar_effective_drop = _finite_or_none(scalar_raw)
    assert scalar_effective_drop is None, (
        "_finite_or_none must convert inf return_1d → None (unavailable)"
    )
    scalar_effective_valid = scalar_effective_drop is not None
    assert scalar_effective_valid is False

    # --- Columnar.
    close = np.array([[close_T1], [close_T]], dtype=np.float64)
    out = compute_return_matrix(
        close,
        np.array([1], dtype=np.int32),
        np.array([0], dtype=np.int32),
    )
    # Inputs are both finite and T1 passes epsilon → base_valid True.
    assert bool(out["price_candidate"][0, 0]) is True
    # (3) Exact semantic parity on the effective contribution:
    assert bool(out["return_valid"][0, 0]) is scalar_effective_valid
    # (4) Columnar unavailable = NaN.  This differs from scalar raw inf, but
    # matches effective contribution (None).  That is the correct Layer-1
    # owner because Historical Dynamics only sees finite EW numerators.
    assert np.isnan(out["return_1d"][0, 0])


# ===========================================================================
# I — EW empty universe → None/NaN
# ===========================================================================
def test_i_ew_empty_universe_is_nan() -> None:
    # Two scopes across three dates: on D1 every valid return is filtered out.
    member_ids = [M0, M1]
    # return_1d: row 0 has NaN/NaN, row 1 has NaN/NaN, row 2 has .10/.20
    r1d = np.array(
        [[np.nan, np.nan],
         [np.nan, np.nan],
         [0.10, 0.20]],
        dtype=np.float64,
    )
    scope_idx = build_scope_member_indices(
        member_ids,
        {"sA": [M0, M1], "sB": [M0]},
    )
    scope_keys, ew = compute_scope_ew_matrix(r1d, scope_idx)
    assert scope_keys == ["sA", "sB"]
    assert ew.shape == (3, 2)
    # Dates 0 and 1 have empty finite universe → NaN.
    assert np.isnan(ew[0, 0]) and np.isnan(ew[0, 1])
    assert np.isnan(ew[1, 0]) and np.isnan(ew[1, 1])
    # Date 2 is valid.
    assert not np.isnan(ew[2, 0])
    assert not np.isnan(ew[2, 1])

    # Genuinely empty scope (zero declared members) → all rows NaN.
    # (Unknown members now fail-closed; use an explicit empty membership list
    # to represent the legitimately-empty-scoped case.)
    empty_idx = build_scope_member_indices(member_ids, {"empty": []})
    skeys, ew2 = compute_scope_ew_matrix(r1d, empty_idx)
    assert ew2.shape == (3, 1)
    assert np.isnan(ew2).all()


# ===========================================================================
# J — EW exact parity with canonical reducer
# ===========================================================================
def test_j_ew_exact_parity_with_canonical_reducer() -> None:
    """Build a realistic D×M return matrix with NaN patterns, compare every
    (date, scope) result columnar → canonical scalar _return_distribution."""
    rng = np.random.default_rng(12345)
    M_count = 20
    D_count = 30
    member_ids = [f"m{i}" for i in range(M_count)]

    r1d = rng.standard_normal((D_count, M_count)).astype(np.float64)
    # Randomly sprinkle 35% NaNs (unavailable returns).
    mask = rng.random((D_count, M_count)) < 0.35
    r1d[mask] = np.nan
    # And inject a few inf values (should be filtered out because finite()).
    r1d[2, 3] = np.inf
    r1d[5, 11] = -np.inf

    # 6 scopes with varying overlap.
    memberships: dict[str, list[str]] = {
        "all": list(member_ids),
        "low": member_ids[:6],
        "high": member_ids[-6:],
        "odds": member_ids[1::2],
        "one": [member_ids[0]],
        "sparse": [member_ids[2], member_ids[7], member_ids[15]],
    }
    scope_idx = build_scope_member_indices(member_ids, memberships)
    scope_keys, ew = compute_scope_ew_matrix(r1d, scope_idx)

    for s_idx, key in enumerate(scope_keys):
        cols = scope_idx[key]
        for t in range(D_count):
            row = r1d[t, cols]
            values = [float(v) for v in row.tolist() if math.isfinite(float(v))]
            if len(values) == 0:
                assert np.isnan(ew[t, s_idx])
            else:
                expected = _return_distribution(values)["mean"]
                # Exact bit parity — no tolerance downgrade.
                assert float(ew[t, s_idx]) == float(expected), (
                    f"scope={key} date={t} mismatch: "
                    f"columnar={ew[t, s_idx]!r} vs canonical={expected!r}"
                )


# ===========================================================================
# K — sparse membership determinism + fail-closed on unknown members
# ===========================================================================
def test_k_sparse_membership_deterministic() -> None:
    member_ids = [M3, M0, M1, M2]  # intentionally non-alphabetical
    memberships = {
        # Duplicates should collapse to first occurrence, no error.
        "s1": [M0, M2, M0, M1, M2],
        # Pure-known set, non-alphabetical ordering preserved.
        "s2": [M3, M0],
        # Genuinely empty membership (zero scoped instruments).
        "s3": [],
    }
    idx = build_scope_member_indices(member_ids, memberships)

    # member_to_col order follows member_ids (not alphabetical).
    expected_col = {M3: 0, M0: 1, M1: 2, M2: 3}
    # s1: duplicates collapsed to first occurrence.
    assert idx["s1"].tolist() == [expected_col[M0], expected_col[M2], expected_col[M1]]
    # s2: known-member ordering preserved exactly.
    assert idx["s2"].tolist() == [expected_col[M3], expected_col[M0]]
    # s3: genuinely empty scope returns empty int32.
    assert idx["s3"].tolist() == []
    assert idx["s1"].dtype == np.int32
    assert idx["s2"].dtype == np.int32
    assert idx["s3"].dtype == np.int32


def test_k_unknown_member_must_fail_closed() -> None:
    """Fail-closed: a scope referencing a member absent from member_ids must
    raise ValueError, never silently drop a denominator contribution.

    Any single unknown id in the membership list is enough — no silent
    filtering, no partial mapping.  This protects against the classic
    performance-path regression "fast, but secretly undercounted stocks".
    """
    member_ids = [M0, M1, M2]
    # Membership with one unknown member.
    with pytest.raises(ValueError, match="unknown member"):
        build_scope_member_indices(
            member_ids,
            {"sA": [M0, "BOGUS_MEMBER_1234", M1]},
        )
    # All-unknown scope must also raise (it would silently become an empty
    # scope otherwise).
    with pytest.raises(ValueError, match="unknown member"):
        build_scope_member_indices(
            member_ids,
            {"sB": ["unknown_X", "unknown_Y"]},
        )
    # Unknown member position doesn't matter — last member, second-to-last,
    # or interspersed all fail the same way.
    for bad in ([M0, M1, "LAST_BAD"],
                ["FIRST_BAD", M0, M1],
                [M0, "MID_BAD", M1]):
        with pytest.raises(ValueError, match="unknown member"):
            build_scope_member_indices(member_ids, {"sX": bad})
    # Sanity: without any unknown members the same scope succeeds — proves
    # the failure is specifically triggered by unresolved ids, not by scope
    # size or structure.
    good = build_scope_member_indices(
        member_ids, {"sA": [M0, M1, M2]},
    )
    assert good["sA"].tolist() == [0, 1, 2]


# ===========================================================================
# L — minimal ObservationSeries + Dynamics smoke parity
# ===========================================================================
def test_l_minimal_observation_series_and_dynamics_smoke() -> None:
    """Build a 4-member, 120-date synthetic world with one industry_l1 scope.

    Steps:
      1. Columnar EW core produces ew_values (length D).
      2. build_scope_dynamics_from_ew() runs ObservationSeries + Dynamics.
      3. Build the SAME ew_values via scalar oracle (compute_exact_return per
         member + _return_distribution) and run the same Dynamics owners.
      4. Assert semantic equality: primitive points availability/value,
         dynamics structs (statuses/counts are compared structurally).

    We do NOT rebuild MemberObservation — the scalar oracle here is literally
    compute_exact_return + _return_distribution on the same numbers, which is
    the explicit P0-L contract for M5-B1.
    """
    rng = np.random.default_rng(42)
    D = 120
    member_ids = [M0, M1, M2, M3]
    SCOPEM = [M0, M1, M3]  # scope membership excludes M2

    # Build D + 1 required dates (consecutive trading days synthetic).
    base = date.fromisoformat("2026-02-24")
    # We need D analysis dates + 1 off-axis PRE.
    cal: list[date] = [date.fromordinal(base.toordinal() + i) for i in range(D + 1)]
    analysis_dates = cal[1:]  # D=120
    t1_by_date = {analysis_dates[i]: cal[i] for i in range(D)}

    required, t_idx, t1_idx = build_required_bar_axis(analysis_dates, t1_by_date)
    assert len(required) == D + 1

    # Generate a [R, M] close series with slow drift + small random walk.
    R = len(required)
    close0 = np.array([100.0, 50.0, 200.0, 80.0], dtype=np.float64)
    log_ret = rng.normal(0.0, 0.01, size=(R - 1, len(member_ids)))
    close_mat = np.zeros((R, len(member_ids)), dtype=np.float64)
    close_mat[0] = close0
    for r in range(1, R):
        close_mat[r] = close_mat[r - 1] * np.exp(log_ret[r - 1])

    # Sprinkle 5% missing close values deterministically.
    rng2 = np.random.default_rng(7)
    missing_mask = rng2.random(close_mat.shape) < 0.05
    close_mat[missing_mask] = np.nan

    # --- (1) Columnar path.
    r_out = compute_return_matrix(close_mat, t_idx, t1_idx)
    scope_idx = build_scope_member_indices(member_ids, {"industry_A": SCOPEM})
    skeys, ew_mat = compute_scope_ew_matrix(r_out["return_1d"], scope_idx)
    assert skeys == ["industry_A"]
    ew_col_list: list[float | None] = [
        (None if np.isnan(ew_mat[t, 0]) else float(ew_mat[t, 0]))
        for t in range(D)
    ]

    col_res = build_scope_dynamics_from_ew(
        scope_type="industry_l1",
        scope_key="industry_A",
        analysis_dates=analysis_dates,
        ew_values=ew_col_list,
    )

    # --- (2) Scalar oracle (same exact math, no MemberObservation).
    col = {m: i for i, m in enumerate(member_ids)}
    ew_scalar_list: list[float | None] = []
    for ti, d in enumerate(analysis_dates):
        r_t = int(t_idx[ti])
        r_t1 = int(t1_idx[ti])
        member_returns: list[float] = []
        for m in SCOPEM:
            c = col[m]
            ct = float(close_mat[r_t, c])
            if r_t1 == -1:
                ct1 = None
            else:
                ct1_val = float(close_mat[r_t1, c])
                ct1 = ct1_val if math.isfinite(ct1_val) else None
            ct_clean = ct if math.isfinite(ct) else None
            raw = compute_exact_return(ct_clean, ct1)
            # Mimic scope_observation's _finite_or_none wrapper on raw.
            if raw is not None and not math.isfinite(float(raw)):
                raw = None
            if raw is not None:
                member_returns.append(float(raw))
        if len(member_returns) == 0:
            ew_scalar_list.append(None)
        else:
            ew_scalar_list.append(float(_return_distribution(member_returns)["mean"]))

    scalar_res = build_scope_dynamics_from_ew(
        scope_type="industry_l1",
        scope_key="industry_A",
        analysis_dates=analysis_dates,
        ew_values=ew_scalar_list,
    )

    # --- (3) Semantic parity — EW primitive series first.
    col_prim = col_res["observation_series"]["primitives"]["equal_weight_return"]
    scl_prim = scalar_res["observation_series"]["primitives"]["equal_weight_return"]
    assert col_prim["key"] == scl_prim["key"]
    assert col_prim["l1_path"] == scl_prim["l1_path"]
    assert len(col_prim["points"]) == len(scl_prim["points"]) == D
    for pcol, psca in zip(col_prim["points"], scl_prim["points"]):
        assert pcol["trade_date"] == psca["trade_date"]
        assert pcol["readiness"] == psca["readiness"]
        assert pcol["available"] == psca["available"]
        if pcol["available"]:
            assert float(pcol["value"]) == float(psca["value"]), (
                f"value parity: col={pcol['value']!r} scalar={psca['value']!r} at {pcol['trade_date']}"
            )
        else:
            assert pcol["value"] is None
            assert psca["value"] is None

    # --- (4) Structural Dynamics parity.
    col_dyn = col_res["dynamics"]
    scl_dyn = scalar_res["dynamics"]
    assert col_dyn["primitive_key"] == scl_dyn["primitive_key"] == "equal_weight_return"

    # Compare historical_dynamics length — actual keys (no _series suffix).
    col_hist = col_dyn["historical_dynamics"]
    scl_hist = scl_dyn["historical_dynamics"]
    for section in ("position", "ema5", "ema20", "velocity", "signal",
                    "acceleration", "persistence"):
        assert len(col_hist[section]) == len(scl_hist[section])
    # Compare dynamics_phase top-level: it's a list of per-date phase facts.
    col_phase = col_dyn["dynamics_phase"]
    scl_phase = scl_dyn["dynamics_phase"]
    assert isinstance(col_phase, list)
    assert isinstance(scl_phase, list)
    assert len(col_phase) == len(scl_phase)

    # Exact structural semantic parity: since both sides were produced from the
    # SAME ew_values list (we just proved that above), the downstream Dynamics
    # results MUST be dict-deep equal.  If they differ, the minimal snapshot
    # builder has introduced drift.
    _assert_deep_equal(col_dyn, scl_dyn, path="dynamics")


# ---------------------------------------------------------------------------
# Deep-equality helper for L — tolerates NaN identity via np.isnan both sides.
# ---------------------------------------------------------------------------
def _assert_deep_equal(a: Any, b: Any, path: str) -> None:
    if isinstance(a, dict):
        assert isinstance(b, dict), f"dict/non-dict at {path}"
        assert a.keys() == b.keys(), f"key diff at {path}: {a.keys() ^ b.keys()}"
        for k in a:
            _assert_deep_equal(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert isinstance(b, list), f"list/non-list at {path}"
        assert len(a) == len(b), f"len diff at {path}: {len(a)} vs {len(b)}"
        for i, (xa, xb) in enumerate(zip(a, b)):
            _assert_deep_equal(xa, xb, f"{path}[{i}]")
    elif isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return
        if math.isinf(a) and math.isinf(b):
            assert (a > 0) == (b > 0), f"inf sign at {path}"
            return
        assert a == b, f"float diff at {path}: {a!r} vs {b!r}"
    else:
        assert a == b, f"ne at {path}: {a!r} vs {b!r}"
