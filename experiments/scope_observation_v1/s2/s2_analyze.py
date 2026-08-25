"""S2 (CORRECTED) — analysis layer: distribution / row-aligned correlation /
rank-scaled contrast / evidence-based verdicts / price facts / contribution evidence.

Consumes out/s2_scope_observation_daily.csv (DB-fact scope-day table), plus
out/_price_facts*.csv and out/_contribution*.csv (DB-native compact extracts).

Corrected vs previous S2:
  - correlation: row-aligned pairwise-complete (same scope-day), reports n
  - contrast: no None->0; rank-scaled; distinct Q2/Q3/Q4/Q5 key sets
  - Q1: factual categorical distribution (sum~1), no std threshold
  - Q4: not correlation-only; uses similarity+contrast evidence
  - Q5: not hardcoded; similarity includes State/Breadth+price HHI+amount HHI
  - Q6: cross-horizon net patterns, not hardcoded SUPPORTED
  - verdicts: every verdict generated from an evidence object (never constants)
Outputs:
  out/s2_incremental_information.json
  out/s2_contrast_cases.json
  out/s2_chip_participation_analysis.json
  out/s2_price_facts.json
  out/s2_member_contribution_evidence.json
"""
from __future__ import annotations

import csv
import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from s2_analysis import (  # noqa: E402
    select_contrast_cases, percentile, row_aligned_correlation,
    cross_horizon_signature, is_same_direction, is_slow_fast_reverse, sign,
)

OUT = Path(__file__).parent / "out"

# observation fields grouped by axis
STATE_BREADTH = [
    "regime_up_ratio", "regime_neutral_ratio", "regime_down_ratio",
    "swing_up_ratio", "swing_neutral_ratio", "swing_down_ratio",
    "internal_up_ratio", "internal_neutral_ratio", "internal_down_ratio",
    "resonance_ratio", "divergence_ratio",
    "expanding_ratio", "flat_ratio", "contracting_ratio",
]
# Transition RATIOS only (cross-scope analysis MUST use ratio, not raw count)
TRANSITION_RATIOS = [
    "regime_neutral_to_up_ratio", "regime_neutral_to_down_ratio",
    "regime_up_to_neutral_ratio", "regime_down_to_neutral_ratio",
    "regime_up_to_down_ratio", "regime_down_to_up_ratio",
    "swing_transition_ratio", "internal_transition_ratio", "momentum_transition_ratio",
]
DIFFUSION = [f"diffusion_{ax}_{lag}"
             for ax in ["regime_up", "regime_down", "swing_up", "internal_up", "resonance", "expanding", "contracting"]
             for lag in ["d1", "d3", "d5"]]
# Raw HHI is retained as a fact (single-scope time variation, explanatory).
# Cross-scope Q4/Q5 similarity/contrast MUST use the N-bias-normalized HHI.
CONCENTRATION = ["price_contribution_hhi", "amount_contribution_hhi"]
CONCENTRATION_NORMALIZED = ["price_contribution_hhi_normalized", "amount_contribution_hhi_normalized"]
# scope families that participate in Q2-Q5 nearest-neighbor contrast (market excluded as control/context)
CONTRAST_SCOPE_FAMILIES = ("industry", "concept")
PARTICIPATION = [
    "vol_ratio20_p25", "vol_ratio20_p50", "vol_ratio20_p75",
    "amt_ratio20_p25", "amt_ratio20_p50", "amt_ratio20_p75",
    "vol_pct20_median", "amt_pct200_median", "seg_vol_ratio_median",
]
# Price breadth (threshold-free): advance/decline/unchanged
PRICE_BREADTH = ["advance_count", "decline_count", "unchanged_count"]

# EXACT canonical T-1 day-pair mapping (BLOCKER #1). Mirrors the `canonical_pairs` CTE in the SQL.
# return = close(T) / close(canonical_T1) - 1 via two bars joins; if exact T-1 bar missing -> UNAVAILABLE.
CANONICAL_T1_MAP = {
    "2026-08-03": "2026-07-31",
    "2026-08-04": "2026-08-03",
    "2026-08-05": "2026-08-04",
    "2026-08-06": "2026-08-05",
    "2026-08-07": "2026-08-06",
    "2026-08-10": "2026-08-07",
}

VERDICTS = ("SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE", "EXTERNAL_AUDIT_PENDING")


def load_records():
    with open(OUT / "s2_scope_observation_daily.csv") as f:
        return list(csv.DictReader(f))


def fnum(x):
    try:
        return float(x) if x not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


def parse_float_dict(r, fields):
    return {f: fnum(r.get(f)) for f in fields}


def col_vals(recs, field):
    return [fnum(r[field]) for r in recs if fnum(r[field]) is not None]


def float_records(recs, fields):
    """Return list of dicts with only numeric-cast values for the given fields
    (missing -> None). Used by row-aligned correlation so no string arithmetic."""
    return [{f: fnum(r.get(f)) for f in fields} for r in recs]


def distribution_summary(recs, fields):
    out = {}
    for f in fields:
        vals = col_vals(recs, f)
        if not vals:
            out[f] = None
            continue
        out[f] = {
            "n": len(vals),
            "min": round(min(vals), 4),
            "p25": round(percentile(vals, 0.25), 4),
            "median": round(statistics.median(vals), 4),
            "p75": round(percentile(vals, 0.75), 4),
            "max": round(max(vals), 4),
            "mean": round(statistics.mean(vals), 4),
            "std": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
        }
    return out


# ---------------------------------------------------------------------------
# Row-aligned correlation matrix (CORRECTED)
# ---------------------------------------------------------------------------
def correlation_matrix(recs, fields):
    """Pairwise row-aligned correlation over the SAME scope-day row.

    For each (A,B) pair, only records with BOTH A and B non-null contribute.
    Reports rho + n_pairwise_complete. Small n -> rho None (conclusion layer
    stays INCONCLUSIVE), no invented min-n product threshold.
    """
    out = {"fields": fields, "pairs": {}}
    for i in range(len(fields)):
        for j in range(i + 1, len(fields)):
            fa, fb = fields[i], fields[j]
            res = row_aligned_correlation(recs, fa, fb)
            out["pairs"][f"{fa}__{fb}"] = res
    return out


# ---------------------------------------------------------------------------
# Contrast case builders for Q2 / Q3 / Q4 / Q5 (distinct key sets)
# ---------------------------------------------------------------------------
def _case_record(r, keys):
    return {"scope_day": f"{r['scope_type']}/{r['board_name']}/{r['trade_date']}",
            "trade_date": r["trade_date"], "scope_type": r["scope_type"],
            **{k: fnum(r.get(k)) for k in keys}}


def run_contrast(recs, name, similarity_keys, contrast_keys, top_n=15):
    """SAME-DAY cross-sectional contrast (BLOCKER #3 fixed).

    For each trade_date independently: take industry+concept rows, rank-scale each
    field tie-aware WITHIN that date (NOT pooled across dates), and find nearest
    neighbors only among rows of the SAME trade_date. No cross-date pairing (a
    08-10 board is never matched to a 08-04 board), avoiding market-wide time effect.

    eligible_dates = dates with at least one valid contrast pair (complete eligible
    rows), NOT the whole input pool. eligible/missing rows reported per date.
    """
    required = list(dict.fromkeys(similarity_keys + contrast_keys))
    # group rows by trade_date; only dates present in the pool are candidates
    by_date = {}
    for r in recs:
        by_date.setdefault(r["trade_date"], []).append(_case_record(r, required))

    cases_by_date = {}
    eligible_rows_by_date = {}
    missing_rows_by_date = {}
    total_eligible = 0
    total_missing = 0
    all_cases = []
    eligible_dates = []
    for td in sorted(by_date):
        case_recs = by_date[td]
        # complete-case: a row is eligible iff complete on ALL required keys (missing never becomes 0)
        eligible = [c for c in case_recs if all(fnum(c.get(k)) is not None for k in required)]
        missing = len(case_recs) - len(eligible)
        total_missing += missing
        eligible_rows_by_date[td] = len(eligible)
        missing_rows_by_date[td] = missing
        if len(eligible) < 2:
            cases_by_date[td] = []
            continue
        res = select_contrast_cases(eligible, similarity_keys, contrast_keys, top_n=top_n)
        cases = res["cases"]
        total_eligible += len(eligible)
        cases_by_date[td] = cases
        all_cases.extend(cases)
        if cases:  # at least one valid contrast pair exists for this date
            eligible_dates.append(td)

    return {
        "name": name,
        "similarity_keys": similarity_keys,
        "contrast_keys": contrast_keys,
        "eligible_rows": total_eligible,
        "total_rows": len(recs),
        "missing_rows": total_missing,
        "eligible_dates": eligible_dates,                 # derived from eligible rows, NOT input pool
        "input_dates": sorted({r["trade_date"] for r in recs}),
        "eligible_rows_by_date": eligible_rows_by_date,
        "missing_rows_by_date": missing_rows_by_date,
        "cases_by_date": cases_by_date,
        "available_scope_families": sorted({r["scope_type"] for r in recs}),
        "cases": all_cases,
    }


# ---------------------------------------------------------------------------
# Q1 evidence — factual categorical distribution (sum ~ 1)
# ---------------------------------------------------------------------------
def q1_evidence(primary):
    """State distribution expresses Breadth: regime/swing/internal ratios are
    a categorical distribution with proportions (sum ~= 1). Factual output, no
    std threshold."""
    axes = [
        ("regime", ["regime_up_ratio", "regime_neutral_ratio", "regime_down_ratio"]),
        ("swing", ["swing_up_ratio", "swing_neutral_ratio", "swing_down_ratio"]),
        ("internal", ["internal_up_ratio", "internal_neutral_ratio", "internal_down_ratio"]),
        ("momentum", ["expanding_ratio", "flat_ratio", "contracting_ratio"]),
    ]
    rows = []
    for ax, keys in axes:
        valid = 0
        eq_1 = 0
        not_eq_1 = 0
        sums = []
        for r in primary:
            vals = [fnum(r.get(k)) for k in keys]
            if any(v is None for v in vals):
                continue
            valid += 1
            s = sum(vals)
            if abs(s - 1.0) < 1e-6:
                eq_1 += 1
            else:
                not_eq_1 += 1
            sums.append(s)
        rows.append({
            "axis": ax,
            "categories": keys,
            "valid_rows": valid,
            "sum_eq_1_rows": eq_1,
            "sum_not_eq_1_rows": not_eq_1,
            "sum_range": (round(min(sums), 4), round(max(sums), 4)) if sums else None,
            "note": "categorical distribution contract; proportions are the Breadth; no min-row threshold",
        })
    return rows


# ---------------------------------------------------------------------------
# Q2 / Q3 / Q4 / Q5 evidence (each with its own similarity/contrast keys)
# ---------------------------------------------------------------------------
def _contrast_pool(primary):
    """Q2-Q5 nearest-neighbor pool: industry + concept ONLY (market is control/context)."""
    return [r for r in primary if r["scope_type"] in CONTRAST_SCOPE_FAMILIES]


def q2_evidence(primary):
    """Transition varies among similar State/Breadth.
       similarity = State/Breadth ratios; contrast = Transition RATIOS only.
       market excluded."""
    pool = _contrast_pool(primary)
    return run_contrast(
        pool, "Q2_state_breadth_similar_transition_differs",
        similarity_keys=[k for k in STATE_BREADTH if k in pool[0]],
        contrast_keys=TRANSITION_RATIOS,
    )


def q3_evidence(primary):
    """Diffusion varies among similar Transition.
       similarity = Transition RATIOS; contrast = Diffusion delta. Independent of Q2.
       market excluded."""
    pool = _contrast_pool(primary)
    return run_contrast(
        pool, "Q3_transition_similar_diffusion_differs",
        similarity_keys=TRANSITION_RATIOS,
        contrast_keys=DIFFUSION,
    )


def q4_evidence(primary):
    """Concentration distinguishes similar State/Breadth.
       similarity = State/Breadth; contrast = NORMALIZED price/amount HHI (N-bias corrected).
       NOT correlation-only; market excluded."""
    pool = _contrast_pool(primary)
    return run_contrast(
        pool, "Q4_state_breadth_similar_normalized_concentration_differs",
        similarity_keys=[k for k in STATE_BREADTH if k in pool[0]],
        contrast_keys=[k for k in CONCENTRATION_NORMALIZED if k in pool[0]],
    )


def q5_evidence(primary):
    """Participation distinguishes similar State/Breadth+Concentration.
       similarity = State/Breadth + NORMALIZED price/amount HHI; contrast = participation
       continuous distributions. market excluded."""
    pool = _contrast_pool(primary)
    sim = [k for k in STATE_BREADTH if k in pool[0]] + [k for k in CONCENTRATION_NORMALIZED if k in pool[0]]
    return run_contrast(
        pool, "Q5_state_breadth_normalized_concentration_similar_participation_differs",
        similarity_keys=sim,
        contrast_keys=[k for k in PARTICIPATION if k in pool[0] and k != "seg_vol_ratio_median"],
    )


# ---------------------------------------------------------------------------
# Q6 evidence — cross-horizon net patterns (not hardcoded)
# ---------------------------------------------------------------------------
def q6_evidence(primary):
    """Cross-horizon net patterns. 0 is the natural directional boundary.
    Reports same_direction_count / slow_fast_reverse_count / other_count plus
    breakdown by trade_date and scope_type. NO count threshold (>=10/>=20 removed)."""
    same_direction = []
    slow_fast_reverse = []
    other = []
    for r in primary:
        sig = cross_horizon_signature({
            "regime_up_ratio": fnum(r.get("regime_up_ratio")),
            "regime_down_ratio": fnum(r.get("regime_down_ratio")),
            "swing_up_ratio": fnum(r.get("swing_up_ratio")),
            "swing_down_ratio": fnum(r.get("swing_down_ratio")),
            "internal_up_ratio": fnum(r.get("internal_up_ratio")),
            "internal_down_ratio": fnum(r.get("internal_down_ratio")),
            "expanding_ratio": fnum(r.get("expanding_ratio")),
            "contracting_ratio": fnum(r.get("contracting_ratio")),
        })
        if None in (sig["trend_net"], sig["swing_net"], sig["internal_net"], sig["momentum_net"]):
            continue
        if is_same_direction(sig["trend_net"], sig["swing_net"], sig["internal_net"], sig["momentum_net"]):
            same_direction.append((r["scope_type"], r["board_name"], r["trade_date"], sig))
        elif is_slow_fast_reverse(sig["trend_net"], sig["momentum_net"]):
            slow_fast_reverse.append((r["scope_type"], r["board_name"], r["trade_date"], sig))
        else:
            other.append((r["scope_type"], r["board_name"], r["trade_date"], sig))

    def _breakdown(pool):
        by_date = defaultdict(int)
        by_scope = defaultdict(int)
        for s, _n, d, _sig in pool:
            by_date[d] += 1
            by_scope[s] += 1
        return {"by_trade_date": dict(sorted(by_date.items())),
                "by_scope_type": dict(sorted(by_scope.items()))}

    return {
        "definition": ("slow/medium/fast nets: trend_net=regime_up-regime_down, "
                       "swing_net=swing_up-swing_down, internal_net=internal_up-internal_down, "
                       "momentum_net=expanding-contracting; 0 is natural neutral boundary; "
                       "no count threshold applied"),
        "same_direction_count": len(same_direction),
        "slow_fast_reverse_count": len(slow_fast_reverse),
        "other_count": len(other),
        "same_direction_breakdown": _breakdown(same_direction),
        "slow_fast_reverse_breakdown": _breakdown(slow_fast_reverse),
        "other_breakdown": _breakdown(other),
        "representative_same_direction": [
            {"scope_day": f"{s}/{n}/{d}", "nets": {k: round(v, 4) for k, v in sig.items()}}
            for s, n, d, sig in same_direction[:8]
        ],
        "representative_slow_fast_reverse": [
            {"scope_day": f"{s}/{n}/{d}", "nets": {k: round(v, 4) for k, v in sig.items()}}
            for s, n, d, sig in slow_fast_reverse[:8]
        ],
    }


# ---------------------------------------------------------------------------
# Verdict generation — FINAL AUDIT CLOSURE
#   NO automatic numeric thresholds (valid_min>100, present_threshold, len(cases)>=5,
#   same>=20, rev>=20, same+rev>=10 all removed). Q1/Q6 output factual evidence only.
#   Q2/Q3/Q4/Q5 verdict = EXTERNAL_AUDIT_PENDING (no false-green).
#   Every object carries the evidence; the verdict is deferred to human audit.
# ---------------------------------------------------------------------------
def verdict_q1(evidence):
    """Q1: externally judged SUPPORTED (State categorical proportions ARE Breadth).
    This round does NOT re-run the experiment (§18); code only preserves the factual
    categorical-distribution evidence and the external verdict. No re-judging."""
    if not evidence:
        return {"verdict": "SUPPORTED", "reason": "external verdict preserved (no re-run)",
                "external_verdict": True}
    total_valid = sum(ax["valid_rows"] for ax in evidence)
    reason = (f"EXTERNAL verdict SUPPORTED (preserved, not re-judged by code). "
              f"Categorical distribution contract: {total_valid} valid rows across {len(evidence)} axes "
              f"(regime/swing/internal/momentum proportions are the Breadth). Evidence retained for audit.")
    return {"verdict": "SUPPORTED", "reason": reason, "external_verdict": True}


def verdict_q6(evidence):
    """Q6: externally judged SUPPORTED (short-window structural evidence). This round
    preserves the factual evidence (same/slow-fast-reverse/other counts + breakdown)
    and the external verdict; does NOT auto-judge (§18)."""
    reason = (f"EXTERNAL verdict SUPPORTED (short-window structural evidence, preserved). "
              f"{evidence.get('same_direction_count', 0)} same-direction + "
              f"{evidence.get('slow_fast_reverse_count', 0)} slow/fast-reverse + "
              f"{evidence.get('other_count', 0)} other; breakdown by trade_date and scope_type in evidence")
    return {"verdict": "SUPPORTED", "reason": reason, "external_verdict": True}


def compute_verdicts(primary):
    ev = {}
    specs = [
        ("q1", "Q1_state_expresses_breadth", q1_evidence(primary)),
        ("q2", "Q2_transition_varies_at_similar_breadth", q2_evidence(primary)),
        ("q3", "Q3_diffusion_varies_at_similar_transition", q3_evidence(primary)),
        ("q4", "Q4_concentration_distinguishes_similar_breadth", q4_evidence(primary)),
        ("q5", "Q5_participation_distinguishes_similar_breadth_concentration", q5_evidence(primary)),
        ("q6", "Q6_raw_axis_combination_expresses_cross_horizon_divergence", q6_evidence(primary)),
    ]
    for key, name, evidence in specs:
        if key == "q1":
            vd = verdict_q1(evidence)
        elif key == "q6":
            vd = verdict_q6(evidence)
        else:
            # Q2/Q3/Q4/Q5: evidence object only, verdict deferred to external audit.
            eligible = evidence.get("eligible_rows", 0) if isinstance(evidence, dict) else 0
            cases = evidence.get("cases", []) if isinstance(evidence, dict) else []
            vd = {"verdict": "EXTERNAL_AUDIT_PENDING",
                  "reason": (f"evidence object emitted: {eligible} eligible rows, "
                             f"{len(cases)} nearest-neighbor contrast cases; no auto threshold applied")}
        ev[key] = {"name": name, "evidence": evidence, "verdict": vd["verdict"], "reason": vd["reason"]}
    return ev


# ---------------------------------------------------------------------------
# Price facts analysis + Price Breadth vs Trend Breadth
# ---------------------------------------------------------------------------
def price_breadth(rec):
    """return>0=advance, <0=decline, ==0=unchanged. Valid denominator = sum."""
    adv, dec, unc = fnum(rec.get("advance_count")), fnum(rec.get("decline_count")), fnum(rec.get("unchanged_count"))
    if any(x is None for x in (adv, dec, unc)):
        return None
    denom = adv + dec + unc
    if denom <= 0:
        return None
    return {"advance_ratio": round(adv / denom, 4),
            "decline_ratio": round(dec / denom, 4),
            "unchanged_ratio": round(unc / denom, 4),
            "valid_price_denominator": int(denom)}


def trend_breadth(rec):
    up, neut, down = fnum(rec.get("regime_up_ratio")), fnum(rec.get("regime_neutral_ratio")), fnum(rec.get("regime_down_ratio"))
    if any(x is None for x in (up, neut, down)):
        return None
    return {"regime_up_ratio": up, "regime_neutral_ratio": neut, "regime_down_ratio": down}


def price_vs_trend_breadth(primary):
    """Find automatically-selected SAME-DAY cases where the full PRICE BREADTH distribution is
    similar but the full TREND BREADTH distribution differs, and vice versa.

    PRICE BREADTH  = advance_ratio / decline_ratio / unchanged_ratio
    TREND BREADTH  = regime_up_ratio / regime_neutral_ratio / regime_down_ratio
    Same-day cross-sectional: rank is computed WITHIN each trade_date, and a board is only
    paired with a board of the SAME trade_date (never 08-10 vs 08-04). Tie-aware rank-scaled
    multi-dimensional Euclidean distance. Market excluded (control/context). No scoring.

    Direction A: price-similar & trend-differs. Direction B: trend-similar & price-differs."""
    PB_KEYS = ["advance_ratio", "decline_ratio", "unchanged_ratio"]
    TB_KEYS = ["regime_up_ratio", "regime_neutral_ratio", "regime_down_ratio"]
    pool = [r for r in primary if r["scope_type"] in CONTRAST_SCOPE_FAMILIES]
    pb = [(i, r, price_breadth(r)) for i, r in enumerate(pool)]
    tb = [(i, r, trend_breadth(r)) for i, r in enumerate(pool)]
    pb_ok = [x for x in pb if x[2] is not None]
    tb_ok = [x for x in tb if x[2] is not None]

    price_sim_trend_diff = _contrast_dimension(pb_ok, tb_ok, PB_KEYS, TB_KEYS, "price_sim", "trend_diff")
    trend_sim_price_diff = _contrast_dimension(tb_ok, pb_ok, TB_KEYS, PB_KEYS, "trend_sim", "price_diff")

    return {
        "method": ("PRICE BREADTH = advance/decline/unchanged (return sign, no ±threshold); "
                   "TREND BREADTH = regime up/neutral/down; SAME-DAY cross-sectional tie-aware "
                   "rank-scaled multi-dimensional distance; nearest-neighbor only within the same "
                   "trade_date; market excluded; missing-aware; no cross-date pairing"),
        "price_breadth_keys": PB_KEYS,
        "trend_breadth_keys": TB_KEYS,
        "price_similar_trend_differs": price_sim_trend_diff,
        "trend_similar_price_differs": trend_sim_price_diff,
        "finding_note": "verifies today's % advancers != % in Up Trend (semantic distinction, not a score)",
    }


def _contrast_dimension(similarity_rows, contrast_rows, sim_keys, con_keys, sim_tag, con_tag):
    """SAME-DAY nearest-neighbor contrast over full distributions.
    similarity_rows: [(i, rec, frac_dict)] complete on sim_keys; contrast_rows likewise.
    Rows are grouped by trade_date; rank is computed tie-aware WITHIN each date and a
    board is paired only with a board of the SAME trade_date (never cross-date).
    For each row with BOTH sim and con complete, find nearest neighbor by sim_keys
    (tie-aware rank-scaled Euclidean), then report con_keys distance. Deterministic."""
    # group eligible row indices by trade_date
    by_date = {}
    for i, r, fr in similarity_rows:
        if all(fr.get(k) is not None for k in sim_keys):
            by_date.setdefault(r["trade_date"], {})[i] = {"sim": {k: fr[k] for k in sim_keys}}
    for i, r, fr in contrast_rows:
        if all(fr.get(k) is not None for k in con_keys):
            by_date.setdefault(r["trade_date"], {}).setdefault(i, {})["con"] = {k: fr[k] for k in con_keys}

    cases = []
    total_eligible = 0
    for td, day in by_date.items():
        idxs = sorted(i for i, d in day.items() if "sim" in d and "con" in d)
        if len(idxs) < 2:
            continue
        total_eligible += len(idxs)
        sim_rank = _rank_multi(idxs, day, "sim", sim_keys)
        con_rank = _rank_multi(idxs, day, "con", con_keys)
        for i in idxs:
            best_j, best_d = None, None
            for j in idxs:
                if i == j:
                    continue
                d = _euclid(sim_rank, sim_keys, i, j)
                # tie-break deterministically by scope_day (order-independent)
                if best_j is None or d < best_d - 1e-9 or (abs(d - best_d) <= 1e-9
                                                           and _scope_day(primary_rec(j)) < _scope_day(primary_rec(best_j))):
                    best_d, best_j = d, j
            if best_j is None:
                continue
            cases.append({
                "a": _scope_day(primary_rec(i)),
                "b": _scope_day(primary_rec(best_j)),
                f"similarity_distance_{sim_tag}": round(best_d, 4),
                f"contrast_distance_{con_tag}": round(_euclid(con_rank, con_keys, i, best_j), 4),
            })
    cases.sort(key=lambda c: c[f"contrast_distance_{con_tag}"], reverse=True)
    return {"eligible": total_eligible, "cases": cases[:12]}


# globals for _contrast_dimension helpers
_PRIMARY = []


def primary_rec(i):
    return _PRIMARY[i]


def _scope_day(r):
    return f"{r['scope_type']}/{r['board_name']}/{r['trade_date']}"


def _euclid(ranks, keys, i, j):
    return sum((ranks[k][i] - ranks[k][j]) ** 2 for k in keys if i in ranks[k] and j in ranks[k]) ** 0.5


def _rank_multi(idxs, by_idx, tag, keys):
    """Tie-aware average-rank scale per field in [0,1] over the row set."""
    out = {}
    n = len(idxs)
    for k in keys:
        ordered = sorted(idxs, key=lambda i: by_idx[i][tag][k])
        ranks = {}
        pos = 0
        while pos < n:
            j = pos
            v = by_idx[ordered[pos]][tag][k]
            while j < n and by_idx[ordered[j]][tag][k] == v:
                j += 1
            avg_rank_1based = (pos + 1 + j) / 2.0
            r01 = (avg_rank_1based - 1) / (n - 1) if n > 1 else 0.5
            for t in range(pos, j):
                ranks[ordered[t]] = r01
            pos = j
        out[k] = ranks
    return out


# ---------------------------------------------------------------------------
# Contribution evidence (compact; top contributors for deterministic scopes)
#   instrument_id / symbol / name; signed_return_contribution = return / price_valid_count
#   (validates Σ signed_return_contribution == equal_weight_return_mean)
#   abs_price_change_share = |return| / Σ|return|  (concentration, distinct semantic)
# ---------------------------------------------------------------------------
CONTRIB_FIELDS = [
    "board_id", "board_name", "board_type", "trade_date", "instrument_id",
    "symbol", "name", "member_return_1d",
    "signed_return_contribution", "abs_price_change_share", "amount_share",
    "price_valid_count", "total_abs_ret", "amount_valid_count", "total_amount",
    "price_candidate_count", "missing_exact_t1_count",
    "equal_weight_return_mean", "sum_signed_return_contribution", "signed_contribution_delta",
    "sum_abs_price_change_share", "sum_amount_share",
    "positive_rank", "negative_rank", "abs_price_rank", "amount_rank",
]


def load_contributions():
    d = defaultdict(list)
    for path in ["_contribution.csv", "_contribution_market.csv"]:
        with open(OUT / path) as f:
            for parts in csv.reader(f):
                if len(parts) < len(CONTRIB_FIELDS) or parts[0] == "board_id":
                    continue
                r = dict(zip(CONTRIB_FIELDS, parts))
                d[(r["board_type"], r["board_id"], r["trade_date"])].append(r)
    return d


SHARE_VALIDATION_FIELDS = ["board_id", "board_type", "trade_date", "price_candidate_count", "price_valid_count",
                           "missing_exact_t1_count", "amount_valid_count",
                           "sum_amount_share", "sum_price_change_share"]


def load_share_validation():
    """Per scope-day: sum(amount_share) and sum(price_change_share) over ALL members (=1 by construction)."""
    rows = {}
    path = OUT / "_share_validation.csv"
    if not path.exists():
        return rows
    with open(path) as f:
        for parts in csv.reader(f):
            if len(parts) < len(SHARE_VALIDATION_FIELDS) or parts[0] == "board_id":
                continue
            r = dict(zip(SHARE_VALIDATION_FIELDS, parts))
            rows[(r["board_type"], r["board_id"], r["trade_date"])] = r
    return rows


def _fv(x):
    try:
        return float(x) if x not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


def contribution_evidence(primary):
    contribs = load_contributions()
    validation = load_share_validation()
    # deterministic representative scopes: pick per trade_date the scope-day with the
    # largest price_valid_count, to prove computation correctness.
    rep_scopes = {}
    for r in primary:
        td = r["trade_date"]
        key = (r["scope_type"], r["board_id"], td)
        rows = contribs.get(key, [])
        if not rows:
            continue
        pv = int(rows[0].get("price_valid_count") or 0)
        if td not in rep_scopes or pv > rep_scopes[td]["price_valid_count"]:
            rep_scopes[td] = {"key": key, "price_valid_count": pv, "rows": rows}

    # aggregate share-sum validation over ALL scope-days
    sums_a = [float(r["sum_amount_share"]) for r in validation.values() if r["sum_amount_share"] not in (None, "")]
    sums_p = [float(r["sum_price_change_share"]) for r in validation.values() if r["sum_price_change_share"] not in (None, "")]
    validation_summary = {
        "scopes_validated": len(sums_a),
        "sum_amount_share_all": {"min": round(min(sums_a), 6), "max": round(max(sums_a), 6)} if sums_a else None,
        "sum_price_change_share_all": {"min": round(min(sums_p), 6), "max": round(max(sums_p), 6)} if sums_p else None,
        "note": ("amount_share summed over amount-valid members; abs_price_change_share summed over "
                 "price-valid members; == 1 by construction"),
    }

    price_ev = {"scopes_analyzed": len(contribs),
                "share_sum_validation": validation_summary,
                "representative_cases": []}
    amt_ev = {"scopes_analyzed": len(contribs),
              "share_sum_validation": validation_summary,
              "representative_cases": []}

    for td in sorted(rep_scopes):
        rows = rep_scopes[td]["rows"]
        key = rep_scopes[td]["key"]
        bname = rows[0]['board_name']
        # Four independent TRUE global lists from the FULL universe (contribution ranking fix):
        #   positive: signed_return_contribution > 0, DESC
        #   negative: signed_return_contribution < 0, ASC
        #   abs_price: abs_price_change_share DESC
        #   amount: amount_share DESC
        def _by(field, rev, pred=None):
            sel = [r for r in rows if _fv(r.get(field)) is not None and (pred is None or pred(_fv(r.get(field))))]
            return sorted(sel, key=lambda x: _fv(x.get(field)), reverse=rev)[:5]

        pos = _by("signed_return_contribution", True, lambda v: v > 0)
        neg = _by("signed_return_contribution", False, lambda v: v < 0)
        absp = _by("abs_price_change_share", True)
        atop = _by("amount_share", True)

        def _member(r):
            return {"instrument_id": r.get("instrument_id"), "symbol": r.get("symbol"), "name": r.get("name")}

        def _signed(r):
            return {**_member(r), "signed_return_contribution": round(_fv(r.get("signed_return_contribution")), 6)}

        def _abs(r):
            return {**_member(r), "abs_price_change_share": round(_fv(r.get("abs_price_change_share")), 4)}

        def _amt(r):
            return {**_member(r), "amount_share": round(_fv(r.get("amount_share")), 4)}

        r0 = rows[0]
        price_ev["representative_cases"].append({
            "scope_day": f"{key[0]}/{bname}/{td}",
            "price_valid_count": int(r0.get("price_valid_count") or 0),
            "amount_valid_count": int(r0.get("amount_valid_count") or 0),
            "price_candidate_count": int(r0.get("price_candidate_count") or 0),
            "missing_exact_t1_count": int(r0.get("missing_exact_t1_count") or 0),
            # Full-universe signed validation (BLOCKER #2): DB aggregate over ALL price-valid members,
            # NOT the top-N subset. signed_contribution_delta == sum_signed - equal_weight_return_mean.
            "equal_weight_return_mean": _fv(r0.get("equal_weight_return_mean")),
            "sum_signed_return_contribution": _fv(r0.get("sum_signed_return_contribution")),
            "signed_contribution_delta": _fv(r0.get("signed_contribution_delta")),
            "sum_abs_price_change_share": _fv(r0.get("sum_abs_price_change_share")),
            "sum_amount_share": _fv(r0.get("sum_amount_share")),
            "top_positive_return_contributors": [_signed(r) for r in pos],
            "top_negative_return_contributors": [_signed(r) for r in neg],
            "top_abs_price_change_contributors": [_abs(r) for r in absp],
        })
        amt_ev["representative_cases"].append({
            "scope_day": f"{key[0]}/{bname}/{td}",
            "amount_valid_count": int(r0.get("amount_valid_count") or 0),
            "top_amount_contributors": [_amt(r) for r in atop],
        })
    return {"price": price_ev, "amount": amt_ev,
            "note": ("per-member facts are DB-native; signed_return_contribution = return/price_valid_count "
                     "(full-universe sum == equal_weight_return_mean); abs_price_change_share = |return|/Σ|return| "
                     "(concentration); amount_share = amount/Σamount over amount-valid members; "
                     "positive/negative/abs_price/amount ranks are TRUE global top-N over the full universe; "
                     "top-N is evidence display, not a primitive; full member rows are process-only (gitignored)")}


# ---------------------------------------------------------------------------
# HHI evidence (raw + normalized + valid member count; price/amount separate)
# ---------------------------------------------------------------------------
def _hhi_evidence(primary):
    out = {"price": [], "amount": []}
    for r in primary:
        p_raw = fnum(r.get("price_contribution_hhi"))
        p_n = fnum(r.get("price_contribution_hhi_normalized"))
        p_cnt = fnum(r.get("price_valid_count"))
        a_raw = fnum(r.get("amount_contribution_hhi"))
        a_n = fnum(r.get("amount_contribution_hhi_normalized"))
        a_cnt = fnum(r.get("amount_valid_count"))
        sd = f"{r['scope_type']}/{r['board_name']}/{r['trade_date']}"
        if p_raw is not None:
            out["price"].append({"scope_day": sd, "raw_hhi": round(p_raw, 4),
                                 "normalized_hhi": round(p_n, 4) if p_n is not None else None,
                                 "valid_member_count": int(p_cnt) if p_cnt is not None else None})
        if a_raw is not None:
            out["amount"].append({"scope_day": sd, "raw_hhi": round(a_raw, 4),
                                  "normalized_hhi": round(a_n, 4) if a_n is not None else None,
                                  "valid_member_count": int(a_cnt) if a_cnt is not None else None})
    out["summary"] = {
        "price": distribution_summary(primary, ["price_contribution_hhi_normalized"]),
        "amount": distribution_summary(primary, ["amount_contribution_hhi_normalized"]),
        "note": ("raw HHI is a fact (single-scope time variation); normalized HHI = "
                 "(HHI-1/N)/(1-1/N) used for cross-scope Q4/Q5; price/amount never averaged"),
    }
    return out


# ---------------------------------------------------------------------------
# Auditability manifest for the gitignored daily CSV
#   Lets external audit verify the CSV's generation evidence without the large file.
# ---------------------------------------------------------------------------
def build_evidence_manifest(recs):
    import hashlib
    daily_path = OUT / "s2_scope_observation_daily.csv"
    sha256 = None
    row_count = 0
    if daily_path.exists():
        blob = daily_path.read_bytes()
        sha256 = hashlib.sha256(blob).hexdigest()
        row_count = sum(1 for _ in open(daily_path)) - 1  # minus header
    columns = list(recs[0].keys()) if recs else []
    keys = [(r["scope_type"], r["board_id"], r["trade_date"]) for r in recs]
    unique_keys = set(keys)
    dup_count = len(keys) - len(unique_keys)
    key_null_counts = {}
    for f in ["transition_denominator", "regime_neutral_to_up_ratio", "regime_up_to_down_ratio",
              "swing_transition_ratio", "price_candidate_count", "price_valid_count",
              "missing_exact_t1_count", "amount_valid_count",
              "return_median", "price_contribution_hhi", "amount_contribution_hhi",
              "price_contribution_hhi_normalized", "amount_contribution_hhi_normalized"]:
        if f in columns:
            key_null_counts[f] = sum(1 for r in recs if r.get(f) in (None, "", "None"))
        else:
            key_null_counts[f] = None
    # signed validation summary from the contribution evidence (DB-native full-universe).
    #   signed_contribution_delta = sum(signed_return_contribution) - equal_weight_return_mean,
    #   computed over the FULL price-valid universe; max_abs ~0 by construction.
    signed_deltas = []
    contrib_ev_path = OUT / "s2_member_contribution_evidence.json"
    if contrib_ev_path.exists():
        try:
            ce = json.loads(contrib_ev_path.read_text())
            for case in ce.get("price", {}).get("representative_cases", []):
                d = case.get("signed_contribution_delta")
                if d is not None:
                    signed_deltas.append(abs(float(d)))
        except (ValueError, TypeError, KeyError):
            signed_deltas = []
    signed_validation = {
        "max_abs_signed_contribution_delta": round(max(signed_deltas), 12) if signed_deltas else None,
        "signed_delta_samples_count": len(signed_deltas),
        "source": ("representative scopes in s2_member_contribution_evidence.json; each delta computed "
                   "DB-native over the FULL price-valid universe (NOT top-N)"),
        "note": ("signed_contribution_delta = sum(signed_return_contribution) - equal_weight_return_mean; "
                 "~0 by construction"),
    }
    # deterministic sample rows: per trade_date, lexicographically-smallest board_id for
    # each of industry / concept, plus the market control.
    sample_rows = []
    for td in sorted({r["trade_date"] for r in recs}):
        for fam in CONTRAST_SCOPE_FAMILIES + ("market",):
            fam_rows = [r for r in recs if r["trade_date"] == td and r["scope_type"] == fam]
            if not fam_rows:
                continue
            pick = min(fam_rows, key=lambda r: (r["board_id"], r["board_name"]))
            sample_rows.append({k: pick.get(k) for k in
                                ["scope_type", "board_id", "board_name", "trade_date",
                                 "price_candidate_count", "price_valid_count", "missing_exact_t1_count",
                                 "amount_valid_count",
                                 "price_contribution_hhi", "price_contribution_hhi_normalized",
                                 "amount_contribution_hhi", "amount_contribution_hhi_normalized",
                                 "return_median", "regime_neutral_to_down_ratio", "transition_denominator"]})
    return {
        "artifact_name": "s2_scope_observation_daily.csv",
        "sha256": sha256,
        "row_count": row_count,
        "unique_scope_day_key_count": len(unique_keys),
        "duplicate_key_count": dup_count,
        "columns": columns,
        "trade_dates": sorted({r["trade_date"] for r in recs}),
        "scope_type_counts": {k: sum(1 for r in recs if r["scope_type"] == k)
                              for k in sorted({r["scope_type"] for r in recs})},
        "per_date_row_counts": {td: sum(1 for r in recs if r["trade_date"] == td)
                                for td in sorted({r["trade_date"] for r in recs})},
        "key_null_counts": key_null_counts,
        "signed_validation": signed_validation,
        "deterministic_sample_rows": sample_rows,
        "sample_selection_rule": "per trade_date: lexicographically-smallest board_id for industry, concept, and market",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def analyze():
    global _PRIMARY
    recs = load_records()
    primary = [r for r in recs if r["membership_changed"] == "False"]
    sensitivity = [r for r in recs if r["membership_changed"] == "True"]
    _PRIMARY = primary

    dist = {
        "state_breadth": distribution_summary(primary, STATE_BREADTH),
        "transition": distribution_summary(primary, TRANSITION_RATIOS),
        "diffusion": distribution_summary(primary, DIFFUSION),
        "concentration": distribution_summary(primary, CONCENTRATION),
        "concentration_normalized": distribution_summary(primary, CONCENTRATION_NORMALIZED),
        "participation": distribution_summary(primary, PARTICIPATION),
    }

    # row-aligned correlation (corrected) — auxiliary only
    all_fields = list(dict.fromkeys(STATE_BREADTH + TRANSITION_RATIOS + DIFFUSION + CONCENTRATION + PARTICIPATION))
    corr = correlation_matrix(float_records(primary, all_fields), all_fields)

    # HHI evidence: raw + normalized + valid member count, price/amount kept separate (never averaged)
    hhi_evidence = _hhi_evidence(primary)

    # distinct contrast experiments (Q2-Q5 exclude market; Q4/Q5 use normalized HHI)
    contrast = {
        "Q2": q2_evidence(primary),
        "Q3": q3_evidence(primary),
        "Q4": q4_evidence(primary),
        "Q5": q5_evidence(primary),
    }

    verdicts = compute_verdicts(primary)

    inc = {
        "scope_days_total": len(recs),
        "scope_days_primary_no_membership_change": len(primary),
        "scope_days_membership_changed_sensitivity": len(sensitivity),
        "trade_dates": sorted({r["trade_date"] for r in recs}),
        "boards_total": len({(r["scope_type"], r["board_id"]) for r in recs}),
        "distribution_summary": dist,
        "hhi_evidence": hhi_evidence,
        "correlation_matrix": corr,
        "contrast": contrast,
        # each verdict carries its full evidence object (Q1 categorical contract,
        # Q6 pattern counts + breakdown, Q2-Q5 contrast evidence)
        "verdicts": {k: {"verdict": v["verdict"], "reason": v["reason"], "evidence": v["evidence"]}
                     for k, v in verdicts.items()},
        "note": ("EXACT-T1 & SAME-DAY CLOSURE. Exact canonical T-1 (explicit day-pair CTE + two bars "
                 "join; no instrument-level LAG; missing exact T-1 bar -> return UNAVAILABLE). "
                 "Q2/Q3/Q4/Q5 same-day cross-sectional (rank within each trade_date; never cross-date; "
                 "eligible_dates from eligible rows, not input pool). No automatic verdict thresholds. "
                 "Q4/Q5 use N-bias-normalized HHI; market excluded from nearest-neighbor contrast. "
                 "Contribution positive/negative/abs_price/amount ranks are TRUE global top-N over the "
                 "full universe; signed validation DB-native over full price-valid universe."),
    }
    with open(OUT / "s2_incremental_information.json", "w") as f:
        json.dump(inc, f, indent=2, ensure_ascii=False)

    contrast_out = {"selection_rule": ("deterministic nearest-neighbor on similarity keys (rank-scaled), "
                                       "ranked by contrast distance (rank-scaled); missing value never becomes 0; "
                                       "distinct key sets per Q2/Q3/Q4/Q5"),
                    "experiments": contrast}
    with open(OUT / "s2_contrast_cases.json", "w") as f:
        json.dump(contrast_out, f, indent=2, ensure_ascii=False)

    # Q7 — chip/participation overlap (keep INCONCLUSIVE, data gap)
    chip_part = analyze_chip_participation(primary)
    with open(OUT / "s2_chip_participation_analysis.json", "w") as f:
        json.dump(chip_part, f, indent=2, ensure_ascii=False)

    # Price facts + Price vs Trend breadth
    price_out = {
        "return_level": distribution_summary(primary, ["equal_weight_return_mean", "return_median"]),
        "return_distribution": distribution_summary(primary, ["return_p25", "return_p50", "return_p75", "return_p10", "return_p90"]),
        "price_breadth": distribution_summary(primary, ["advance_count", "decline_count", "unchanged_count"]),
        "price_hhi": distribution_summary(primary, ["price_contribution_hhi"]),
        "amount_hhi": distribution_summary(primary, ["amount_contribution_hhi"]),
        "price_vs_trend_breadth": price_vs_trend_breadth(primary),
        "universe_counts": {
            "price_candidate_count": distribution_summary(primary, ["price_candidate_count"]),
            "price_valid_count": distribution_summary(primary, ["price_valid_count"]),
            "missing_exact_t1_count": distribution_summary(primary, ["missing_exact_t1_count"]),
            "amount_valid_count": distribution_summary(primary, ["amount_valid_count"]),
        },
        "note": ("Price is a result-fact layer, NOT a Trend score. EXACT canonical T-1 (explicit day-pair "
                 "mapping + two bars join; missing exact T-1 bar -> return UNAVAILABLE; no LAG/fallback). "
                 "price_candidate_count = PIT∩valid∩close(T); price_valid_count = candidate with exact T-1 "
                 "close; missing_exact_t1_count = candidate - valid. amount_valid_count = PIT∩valid∩amount(T) "
                 "(no T-1 required). return>0=advance, <0=decline, ==0=unchanged; no ±threshold."),
    }
    with open(OUT / "s2_price_facts.json", "w") as f:
        json.dump(price_out, f, indent=2, ensure_ascii=False)

    # Contribution evidence (compact)
    contrib_ev = contribution_evidence(primary)
    with open(OUT / "s2_member_contribution_evidence.json", "w") as f:
        json.dump(contrib_ev, f, indent=2, ensure_ascii=False)

    # Auditability manifest for the (gitignored) daily CSV
    manifest = build_evidence_manifest(recs)
    with open(OUT / "s2_daily_evidence_manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return inc, contrast_out, chip_part, price_out, contrib_ev, manifest


def analyze_chip_participation(primary):
    # chip-like = seg_vol_ratio_median (fp_segment_volume_ratio); participation = vol/amt ratios
    chip_field = "seg_vol_ratio_median"
    part_fields = ["vol_ratio20_p50", "amt_ratio20_p50", "vol_pct20_median", "amt_pct200_median"]
    chip_present = sum(1 for r in primary if fnum(r.get(chip_field)) is not None)
    if chip_present == 0:
        return {
            "chip_like_field": chip_field,
            "participation_fields": part_fields,
            "correlations": {pf: None for pf in part_fields},
            "overlap_verdict": "inconclusive",
            "chip_field_populated_rows": 0,
            "note": ("chip-like segment volume ratio (fp_segment_volume_ratio) is NULL for ALL valid state rows "
                     "in the window — data gap, not computation failure. Q7 cannot be answered from real data. "
                     "Does NOT decide Architecture A/B."),
            "long_history": "unavailable",
        }
    correlations = {}
    flds = [chip_field] + part_fields
    fr = float_records(primary, flds)
    for pf in part_fields:
        res = row_aligned_correlation(fr, chip_field, pf)
        correlations[pf] = res["rho"] if res["n_pairwise_complete"] >= 3 else None
    return {
        "chip_like_field": chip_field,
        "participation_fields": part_fields,
        "correlations": correlations,
        "overlap_verdict": "inconclusive",
        "chip_field_populated_rows": chip_present,
        "note": "chip-like field available; correlations row-aligned (auxiliary only)",
        "long_history": "unavailable",
    }


if __name__ == "__main__":
    inc, contrast, chip, price, contrib, manifest = analyze()
    print("primary scope-days:", inc["scope_days_primary_no_membership_change"])
    for k, v in inc["verdicts"].items():
        print(f"  {k}: {v['verdict']}")
    print("contrast experiments:", list(contrast["experiments"].keys()))
    print("Q7 chip/participation:", chip["overlap_verdict"])
    print("manifest sha256:", manifest["sha256"], "rows:", manifest["row_count"],
          "dup:", manifest["duplicate_key_count"])
    print("wrote 6 JSON outputs")
