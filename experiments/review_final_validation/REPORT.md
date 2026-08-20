# REVIEW-FINAL-STATISTICAL-VALIDATION — REPORT

**STATUS = EXPERIMENTAL EVIDENCE.** This report is an experiment conclusion, not
a project formal document. It is not a PRD / Map / Change / Runbook / production
runtime input / strict PIT product evidence.

Membership semantics for all dynamics / leadership output in this report:

> **RESEARCH PROXY — CURRENT STATIC MEMBERSHIP**

---

## 1. Executive Conclusion

The canonical Review owners (baseline `915b0429`) were executed over the frozen
RAW SOURCE FACTS dataset under a current-static membership research proxy. The
full six-layer chain (Scope Observation, Internal Structure, Member Attribution,
Historical Dynamics, Leadership, Composition) runs and is deterministic on the
frozen data. Current-state canonical facts (price/amount/breadth/concentration)
are produced for the full scope universe. Historical Dynamics and Leadership
validate under the current-static proxy; strict PIT historical product
validation is blocked solely by the absence of historical PIT membership data.

FACT vs PRODUCT INTERPRETATION is kept distinct throughout (see §9).

## 2. Data / Membership Boundary

- Input: `backend/.perfdata/review/review-source-c5c686e-v1/` (RAW SOURCE FACTS)
  + SFS overlay.
- Membership: **current-static** (`board_memberships_current_snapshot`); strict
  PIT membership **unavailable**.
- Scope universe: `industry_l1/l2/l3` + `concept`, full offline-supported set
  (not sampled, not hand-filtered).
- Dates: historical `2026-07-29 … 08-04`; spot `2026-08-10`;
  `07-28`/`08-07` only as T-1.

## 3. Technical Closure Evidence

- Canonical owners called: `compute_scope_observation`, `compute_internal_structure`,
  `compute_member_attribution`, `build_observation_series`+`compute_scope_dynamics_analysis`,
  `compute_member_leadership_contributions`+`build_leadership_snapshot`+`compute_leadership_migration`,
  `compose_canonical_review_scope` (via canonical prep owners).
- No business formula reimplemented; no second/fast/shadow implementation.
- Determinism reconciliation and closure-gate matrix are written to
  `results/determinism_reconciliation.csv` and `results/closure_gate_matrix.json`.

## 4. 2026-08-10 Current-State Statistics

FACT / STATISTICAL DESCRIPTION (full detail in `family_daily_summary.csv` and
`scope_daily_metrics.csv`; 768 scopes, 4 scope families):

| family | scopes | EW median | AW return | advance median | decline median | cap_tilt median | price-norm-HHI median | amount-norm-HHI median |
|---|---|---|---|---|---|---|---|---|
| concept | 389 | 0.01197 | — | 0.728 | 0.262 | −0.00384 | 0.00888 | 0.02862 |
| industry_l1 | 31 | 0.01639 | — | 0.810 | 0.154 | −0.00090 | 0.00592 | 0.01666 |
| industry_l2 | 90 | 0.01650 | — | 0.810 | 0.167 | −0.00161 | 0.01518 | 0.04579 |
| industry_l3 | 257 | 0.01738 | — | 0.825 | 0.143 | −0.00063 | 0.03457 | 0.10367 |

- All 768 scopes computed; composition_readiness = `unavailable_current`
  (leadership/historical_dynamics layers are single-date proxy unavailable; the
  six owners still ran — see readiness + per-layer CSVs).
- No scope was hand-filtered; the full offline-supported universe is included.

## 5. 2026-07-29 → 08-04 Historical Statistics

FACT / STATISTICAL DESCRIPTION (from `family_daily_summary.csv`):

- **2026-07-29**: EW median concept 0.0127, industry_l1 0.0163, l2 0.0143, l3 0.0146 (positive day).
- **2026-07-30**: all families negative (EW median ≈ −0.0164 … −0.0165 concept/l2/l3); a down day.
- **2026-07-31**: all families positive (concept 0.0267, l1 0.0194, l2 0.0195, l3 0.0187).
- **2026-08-03**: positive (concept 0.0121, l1 0.0151, l2 0.0133, l3 0.0135).
- **2026-08-04**: mixed (concept 0.0204, l1 0.0110, l2 0.0113, l3 0.0091).
- Amount-normalized HHI increases with granularity (l1 < l2 < l3 < concept is
  NOT monotonic; l3 and concept have higher member-level concentration).

These are **statistical descriptions of the canonical facts**, not product labels.

## 6. Leadership Migration Research Proxy

FACT / STATISTICAL DESCRIPTION (from `leadership_statistics.csv`,
**RESEARCH PROXY — CURRENT STATIC MEMBERSHIP**):

- Leadership computed for 1,534 (scope,date) rows across the window (768 scopes
  on 07-29 and 08-10, plus T-1-to-T where T-1 is in-window).
- 08-10 examples: PPP概念 (prev 14 → curr 14, retained 4, entrant 10, exit 10,
  migration 0.833); AI PC (prev 3 → curr 4, retained 2, migration 0.6);
  玉米 (prev 2 → curr 5, migration 1.0).
- Migration is a real, non-degenerate signal under the current-static proxy.
- **Not strict PIT product evidence.** T-1 membership is also current-static.

## 7. Member Attribution

FACT / STATISTICAL DESCRIPTION (from `attribution_statistics.csv` +
`representative_member_attribution.json`):

- Direction positive/negative counts and top-1/3/5/10 absolute-contribution
  shares are computed per scope/date using unrounded canonical contributions.
- 08-10 examples: PPP概念 direction pos 173 / neg 23, top-1 share 0.058;
  AI PC pos 20 / neg 43, top-1 share 0.591; 玉米 pos 27 / neg 3.
- Capital-tilt and price/amount-HHI contribution shares are reported.
- Reconciliation status fields are carried from the canonical reconciliation
  block (violation/skipped/tolerance/checks).

## 8. Product Information Assessment

The experiment provides **factual + statistical** evidence only. It does NOT
assert any directional product label. In particular this experiment does not
auto-generate "opportunity / risk / strong / weak / bullish / bearish /
Broadening / Core-led / Rotating / Fragmenting" as verified validation labels.

## 9. Limitations / Deferred Data Readiness

- Strict PIT membership unavailable => Historical Dynamics and Leadership are
  `PASS_ON_CURRENT_STATIC_PROXY` only, never "strict PIT validated".
- Current-only snapshot facts intentionally not consumed in the proxy path.
- SFS `first_pyramid_flat` coverage is 0 for `07-28`/`07-29`.
- These are data-readiness limitations, not backend implementation gaps (H4).

## 10. Final Verdict

```
CURRENT_STATE_CANONICAL_FACTS              = PASS
FULL_SIX_LAYER_COMPOSITION_EXECUTION       = PASS
MEMBER_ATTRIBUTION                          = PASS
DETERMINISM                                = PASS
RECONCILIATION                             = PASS
HISTORICAL_DYNAMICS_ALGORITHM              = PASS_ON_CURRENT_STATIC_PROXY
LEADERSHIP_MIGRATION_ALGORITHM             = PASS_ON_CURRENT_STATIC_PROXY
STRICT_PIT_HISTORICAL_PRODUCT_VALIDATION   = BLOCKED_BY_MEMBERSHIP_DATA
BACKEND_CLOSURE_CANDIDATE                  = NO
```

`BACKEND_CLOSURE` (CLOSED/OPEN) is **not** declared by this IDE. The final
product / experiment acceptance is decided by the user and by ChatGPT after an
independent Git audit of this evidence. The IDE only offers
`BACKEND_CLOSURE_CANDIDATE = NO` here because this experiment validates the
current-static proxy only and does not establish a full six-layer composition
with leadership/historical_dynamics layers marked `ready` in the single-date
proxy path, nor strict PIT product readiness.
