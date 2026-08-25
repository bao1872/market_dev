# S2 — Short-window Scope Observation Structure Experiment (EXACT-T1 & SAME-DAY EVIDENCE CLOSURE)

**Branch**: `exp/scope-observation-model-v1`
**Baseline**: `SCOPE_S1_FINAL_CLEAN_SHA = f7683d7c1feeec4e878a034a26f6d178cea5dfce` (S1 = PASS)
**Prior S2**: `SCOPE_S2_FINAL_AUDIT_SHA = db6befc12d434922089fea925b28cd841db536a6` (external verdict: PARTIAL / ONE MORE DATA-INTEGRITY CLOSURE)
**This round**: `S2 EXACT-T1 & SAME-DAY EVIDENCE CLOSURE` — last S2 data/method closure. **not S3.**
**Final SHA**: recorded in git log of this commit.
**Date window**: 2026-08-03 .. 2026-08-10 (6 trading days: 08-03/04/05/06/07/10; weekend 08-08/09 excluded).
**End date source**: `max(trade_date)` of `first_pyramid_history_daily_state` = **2026-08-10** (DB fact, not assumed).

---

## 0. Process Deviation Note (§20)

> **PROCESS_DEVIATION** (NOT automatic DATA_INVALID):
> The previous S2 run (`a43e962`) deviated from the resource contract:
>   - `work_mem` increased to **256MB** (contract: ≤64MB)
>   - `statement_timeout` increased to **600s** (contract: ≤300s)
>   - **439K member-state rows entered Python** (contract: DB-native aggregation first, compact)
> This correction restores the contract: work_mem≤64MB, statement_timeout≤300s,
> max_parallel_workers_per_gather=0, and Python only consumes scope-day tables +
> compact evidence. **Not repeated this round.**

---

## 1. Objective (§0)

Validate the **Scope Observation Grammar** structure (not long-term stability):
- Axes: TREND / STRUCTURE / MOMENTUM / CHIP-PARTICIPATION
- Observation attributes: STATE+BREADTH / TRANSITION / DIFFUSION
- Scope properties: CONCENTRATION / PARTICIPATION
- **Price Facts** (result-fact layer, not a Trend score): RETURN LEVEL / DISTRIBUTION / BREADTH / CONCENTRATION

**Out of scope**: scoring, weighting, prediction, future return, opportunity/risk, ranking, signal,
Minimum Sufficient Set, Observation Collision final model, formal Architecture A/B, formal Review PRD rewrite.

**Cannot answer**: long-history stability — industry/concept PIT membership pre-2026-08-01 is unavailable.

---

## 2. What was retained from prior S2 (raw facts, unchanged)

The following bottom-layer facts were **kept** (not recomputed, not re-derived):
- per-trade-date PIT membership (`pit_member_count`, `fp_row_count`, `definition_effective_from`, `membership_version`)
- membership member-SET diff (`membership_changed`, `membership_added_count`, `membership_removed_count`)
- canonical T-1 (strict, no LAG across missing days)
- common-valid Transition denominator (`transition_denominator`)
- State/Breadth categorical facts (regime/swing/internal/momentum/alignment ratios)
- Diffusion raw delta (Δ1/3/5)
- Participation percentile facts (vol/amt ratio20, percentile20/200)
- **Q7 = INCONCLUSIVE** (chip field data gap, unchanged)

---

## 3. What was corrected (conclusion layer)

Prior S2 conclusion layer was unreliable; each item fixed:

| # | Prior defect | Correction |
|---|---|---|
| 1 | Transition main output was raw counts, not normalized by `transition_denominator` | Added explicit `*_ratio = count / transition_denominator` (NULL when denom NULL/0). Cross-scope analysis now uses RATIOS only. Raw counts kept as explanatory facts. |
| 2 | `correlation_matrix` dropped NULL per-column then zipped → misaligned scope-days | Rewrote as **row-aligned pairwise-complete**: for each record, if A and B both available → append (A,B). Reports `rho` + `n_pairwise_complete`. No self-invented min-n threshold; small n → rho None (conclusion INCONCLUSIVE). |
| 3 | contrast selector converted missing value `None → 0` | Forbidden. A pair participates only if BOTH rows are complete on that test's required fields. Missing → candidate unavailable. |
| 4 | contrast mixed Transition raw counts with Diffusion ratio (different scales) | All analytical dimensions rank-scaled before distance. Transition counts and Diffusion ratios never mixed on one axis. Q2 uses Transition RATIOS; Q3 uses Diffusion delta. |
| 5 | Q2/Q3/Q5/Q6 verdicts hard-coded | Every verdict derived from an evidence object (`verdict_from_evidence` / `verdict_q1` / `verdict_q6`). No `q2=PARTIALLY_SUPPORTED` constants. |
| 6 | Q4 used `|rho|<0.5` directly → PARTIAL | Q4 now uses similarity(State/Breadth)+contrast(price/amount HHI) evidence, NOT correlation-only. |
| 7 | Q1 used `std>0.05` human threshold | Q1 now outputs factual categorical distribution (sum≈1, proportions present). No std threshold. |
| 8 | `regime_strength_median` computed `median(regime_value)` (field semantics wrong) | Fixed SQL to read `state_payload->>'regime_strength'`. Real values (e.g. -0.0039) now reported. |
| 9 | Price HHI 08-03 return lacked real T-1=07-31 | Bar window now includes 07-31; 08-03 return = close(08-03)/close(07-31)-1. |
| 10 | **EXACT-T1 (BLOCKER #1)**: `inst_ret` used instrument-level `LAG(close)` over a bar window; a stock missing the exact T-1 bar would LAG to an older bar and fabricate a multi-day return labeled 1D. | All 6 return-bearing SQL files now use an explicit `canonical_pairs(T→T1)` CTE + **two bars joins** (`bt` at T, `bp` at exact T-1): `return = bt.close/bp.close - 1`. Missing exact T-1 bar → return **UNAVAILABLE**, never falls back to an older bar. `price_candidate_count / price_valid_count / missing_exact_t1_count` diagnostics added. |
| 11 | **Signed contribution sum==mean was computed over the top-N subset** (false claim). | `equal_weight_return_mean / sum_signed_return_contribution / signed_contribution_delta` are now **DB-native aggregates over the FULL price-valid universe**, not the top-N output rows. |
| 12 | **top_positive/top_negative were sliced from an abs-top-40** (real global tops could be missed). | Four **independent TRUE global ranks** over the full universe: `positive_rank` (signed>0 DESC), `negative_rank` (signed<0 ASC), `abs_price_rank`, `amount_rank`; output keeps `any rank <= 10`. |
| 13 | **price/amount scope counts fell back via `COALESCE(price_valid_count, amount_valid_count)`**. | Scope-level `price_valid_count` and `amount_valid_count` come from **independent** price_tot / amount_tot joins; never fallback into each other's semantics. |
| 14 | **Q2-Q5 (and Price-vs-Trend) paired boards across different trade_dates**, mixing market-wide time effect. | **SAME-DAY cross-sectional**: rank tie-aware WITHIN each trade_date; a board pairs only with a board of the **same trade_date** (never 08-10 vs 08-04). `eligible_dates` derived from eligible rows, not the input pool. |
| 15 | `available_dates` came from the whole input pool (could overstate Q3 coverage). | Replaced with `input_dates / eligible_dates / eligible_rows_by_date / missing_rows_by_date`. Q3 full D1/D3/D5 only available on **2026-08-10** — honestly reported. |

---

## 4. Transition Ratio correction (§3)

New columns in `s2_scope_observation_daily.csv` (count/transition_denominator, NULL when denom NULL/0):
```
regime_neutral_to_up_ratio, regime_neutral_to_down_ratio,
regime_up_to_neutral_ratio, regime_down_to_neutral_ratio,
regime_up_to_down_ratio,   regime_down_to_up_ratio,
swing_transition_ratio, internal_transition_ratio, momentum_transition_ratio
```
- Raw counts retained as explanatory facts.
- **All cross-scope analysis uses ratio, never raw count.**

---

## 5. Correlation correction (§4)

`correlation_matrix` now returns `pairs: {A__B: {rho, n_pairwise_complete}}`, row-aligned.
- Example: `regime_up_ratio__regime_down_ratio` → rho=-0.3703, n=3858.
- Auxiliary only; never used to mechanically claim independence/redundancy.

---

## 6. Contrast correction (§5) + Q2/Q3/Q4/Q5 separate (§12)

Contrast selector is deterministic, **tie-aware rank-scaled** (average-rank/midrank: equal raw values → equal
rank), missing-aware (missing never becomes 0), **SAME-DAY cross-sectional** (rank computed independently
within each trade_date; a board pairs only with a board of the **same trade_date** — never cross-date), and
**excludes the market control** from nearest-neighbor pairs (industry + concept only; market is control/context).
**Distinct experiments**:

| Q | Question | Similarity keys | Contrast keys |
|---|----------|-----------------|---------------|
| Q2 | Transition varies among similar State/Breadth? | State/Breadth ratios | **Transition RATIOS only** |
| Q3 | Diffusion varies among similar Transition? | Transition RATIOS | Diffusion delta (independent of Q2) |
| Q4 | Concentration distinguishes similar State/Breadth? | State/Breadth | **NORMALIZED** price/amount HHI (N-bias corrected) |
| Q5 | Participation distinguishes similar State/Breadth+Concentration? | State/Breadth + **NORMALIZED** price/amount HHI | Participation continuous distributions |

Contrast distance is rank-scaled, tie-aware (equal values share a rank), never raw count.

---

## 7. Q1 — external SUPPORTED preserved (§18)

Q1 = "State distribution already expresses Breadth?" is a semantic/representational relationship.
State categories (up/neutral/down proportions) **are** Breadth proportions.
**This round does NOT re-run the Q1 experiment.** The external verdict **SUPPORTED** is preserved
(`verdict_q1` returns `SUPPORTED` with `external_verdict: True`; code does not re-judge). The factual
categorical-distribution evidence is retained for audit:
- regime: valid=3859, sum_eq_1=3547, sum_not_eq_1=312 (6-decimal ratio rounding artifact, sum_range=[1.0,1.0])
- swing: sum_eq_1=3739, sum_not_eq_1=120; internal & momentum: sum_eq_1=3859, sum_not_eq_1=0
- **No minimum-row threshold.** If all valid rows sum to 1 the fact holds — reported as fact.

---

## 8. Q6 — external SUPPORTED preserved, factual evidence kept (§18)

Q6 = "Raw-axis combination expresses cross-horizon divergence without score?"
This round does **NOT auto-judge Q6**. The external verdict **SUPPORTED (short-window structural
evidence)** is preserved; the factual evidence is retained:
```
trend_net    = regime_up_ratio - regime_down_ratio        (slow: TREND)
swing_net    = swing_up_ratio - swing_down_ratio          (medium: STRUCTURE)
internal_net = internal_up_ratio - internal_down_ratio    (medium: STRUCTURE)
momentum_net = expanding_ratio - contracting_ratio        (fast: MOMENTUM)
```
0 is a natural directional boundary, not a strong/weak threshold.
- Same-direction scope-days: **765** (concept 516 / industry 248 / market 1)
- slow/fast reverse scope-days: **2678** (concept 1616 / industry 1057 / market 5)
- other scope-days: **416**
- Breakdown by trade_date and scope_type in `s2_incremental_information.json`.
- **No count threshold (>=10/>=20 removed)**. Verdict = **SUPPORTED (external)**.

---

## 9. regime_strength_median (§9)

SQL fixed to read `state_payload->>'regime_strength'`. Verified field exists and is populated
(5277 rows on 08-03). Now reports real regime_strength median (range -0.0098 .. 0.0158),
NOT median(regime_value). Field is a descriptor only, not a Trend score.

---

## 10. PRICE FACTS (§5/§6/§10)

New result-fact layer per scope-day (Price is a **result fact layer**, not a Trend score):

**RETURN LEVEL**
- `equal_weight_return_mean`, `return_median`

**RETURN DISTRIBUTION**
- `return_p25`, `return_p50`, `return_p75` (evidence: `return_p10`, `return_p90`)

**PRICE BREADTH** (threshold-free)
- `advance_ratio` (return>0), `decline_ratio` (return<0), `unchanged_ratio` (return==0)
- No ±1%/±2% thresholds. `advance+decline+unchanged = valid price denominator`.

**PRICE CONCENTRATION (N-bias corrected)**
- `price_contribution_hhi` (raw) + `price_contribution_hhi_normalized = (HHI - 1/N)/(1 - 1/N)`
- `amount_contribution_hhi` (raw) + `amount_contribution_hhi_normalized`
- Raw HHI kept for single-scope time variation; **normalized HHI used for cross-scope Q4/Q5**.
- N = that metric's valid-member count (N<=1 -> normalized NULL). Never average price/amount HHI.

**Universe separation (§5)**
- PRICE valid universe = `PIT(T) ∩ valid ∩ close(T) ∩ close(exact T-1)` → `price_valid_count`
- PRICE candidate = `PIT(T) ∩ valid ∩ close(T)` → `price_candidate_count`
- `missing_exact_t1_count = price_candidate_count - price_valid_count` (audits suspend / missing bar impact)
- AMOUNT valid universe = `PIT(T) ∩ valid ∩ amount(T) non-null` (**no T-1 return required**) → `amount_valid_count`
- The two universes are SEPARATE. Amount HHI/share depend only on amount-valid members.
- (In this window the two coincide numerically because every bar has both close and amount.)

**Exact canonical return contract (BLOCKER #1)**: `member_return_1d = bt.close/bp.close - 1` where `bt`
is the bar at T and `bp` is the bar at the **exact canonical T-1** (explicit `canonical_pairs(T→T1)` CTE).
If the exact T-1 bar is missing → return **UNAVAILABLE** (no LAG to an older bar, no fabricated multi-day
return). For 08-03, canonical T-1 = **07-31**. This is **not** membership backfill.
**Impact this window**: `missing_exact_t1_count = 0` for all scope-days (data is complete), so Price Facts
are **unchanged** (0 scope-days changed / 3882 unchanged) — the exact-T1 is a contract hardening, not a
data change in this complete window.

---

## 11. Price Breadth vs Trend Breadth (§16) — full distributions, SAME-DAY

Full-distribution contrast (tie-aware rank-scaled multi-dimensional distance), NOT single advance/up field:
- **PRICE BREADTH** = advance_ratio / decline_ratio / unchanged_ratio
- **TREND BREADTH** = regime_up_ratio / regime_neutral_ratio / regime_down_ratio
- **SAME-DAY**: rank computed within each trade_date; a board pairs only with a board of the same
  trade_date (no more `08-10 vs 08-04` cross-date case).
- Direction A: price-similar & trend-differs. Direction B: trend-similar & price-differs.
- 3853 eligible rows each direction; market excluded. Example case (same-day):
  `industry/基础化工-化学制品-氟化工/2026-08-06` vs `industry/农林牧渔-养殖业-水产养殖/2026-08-06`
  (similarity_distance_price_sim=0.0, contrast_distance_trend_diff=1.5533).
Goal: verify **today's % advancers ≠ % in Up Trend**. No scoring.

---

## 12. Individual contribution facts (§6/§7/§8/§9)

**Signed return contribution**: `signed_return_contribution_i = member_return_1d / price_valid_count`.
Distinguishes who lifted / dragged the board. **Full-universe signed validation (BLOCKER #2)** is
computed **DB-native over the FULL price-valid universe** (NOT the top-N output rows):
`equal_weight_return_mean = AVG(return)`, `sum_signed_return_contribution = SUM(return/price_valid_count)`,
`signed_contribution_delta = sum_signed - mean`. Verified: `delta = 0.0` (max_abs = 0.0 over 6
representative scopes) — **Σ signed over full universe == equal_weight_return_mean**, proven, not claimed.

**Abs price-change share** (concentration, distinct semantic): `abs_price_change_share_i = |return_i| / Σ|return|`.
**Amount share**: `amount_share_i = member_amount / Σamount` over **amount-valid members only**.
- `sum(amount_share) = 1.0` (3876 scope-days) and `sum(abs_price_change_share) = 1.0` — validated DB-native.
- **Four independent TRUE global ranks over the full universe (BLOCKER #3 / §7)**:
  `positive_rank` (signed>0 DESC), `negative_rank` (signed<0 ASC), `abs_price_rank`, `amount_rank`.
  Output keeps `any rank <= 10`. A positive contributor is a genuine global top even when >40 larger
  negative abs moves exist (no longer sliced from an abs-top-40).
- **Scope counts independent (§8)**: `price_valid_count` / `amount_valid_count` come from separate
  price_tot / amount_tot joins; never `COALESCE` into each other.
- Evidence splits **top_positive_return_contributors** / **top_negative_return_contributors** (by signed)
  + **top_abs_price_change_contributors** + **top_amount_contributors**, each with instrument_id/symbol/name,
  plus scope-level `price_valid_count / amount_valid_count / price_candidate_count / missing_exact_t1_count /
  equal_weight_return_mean / sum_signed_return_contribution / signed_contribution_delta /
  sum_abs_price_change_share / sum_amount_share`.
- TopN is evidence display, NOT a primitive. `review_amount_ratio20` belongs to Participation, NOT amount contribution.
- Full member rows are process-only (gitignored `out/_contribution*.csv`); only compact evidence committed.

---

## 13. S2 Core Questions — same-day evidence, Q1/Q6 external SUPPORTED (§12/§13/§17/§18)

**No automatic verdict thresholds** (valid_min>100, present_threshold, len(cases)>=5, same>=20, rev>=20,
same+rev>=10 all removed). Q2/Q3/Q4/Q5 produce **same-day evidence objects only** with
`input_dates / eligible_dates / eligible_rows_by_date / missing_rows_by_date / cases_by_date`
(eligible_dates derived from eligible rows, NOT the input pool). **Q1/Q6 verdict = SUPPORTED (external,
preserved, not re-judged)**; **Q2-Q5 verdict = EXTERNAL_AUDIT_PENDING** — no false-green.

| Q | Question | Verdict | Same-day Evidence |
|---|----------|---------|----------|
| Q1 | State distribution already expresses Breadth? | **SUPPORTED (external)** | categorical contract: valid=3859/axis; regime eq1=3547/noteq1=312, swing eq1=3739/noteq1=120, internal & momentum eq1=3859/noteq1=0. Not re-run. |
| Q2 | Transition varies among similar State/Breadth? | **EXTERNAL_AUDIT_PENDING** | 75 same-day cases / eligible dates 08-04..08-10 (08-03 first obs day, no transition), Transition RATIOS only |
| Q3 | Diffusion varies among similar Transition? | **EXTERNAL_AUDIT_PENDING** | **15 cases, eligible_dates = [2026-08-10] only** — full D1/D3/D5 unavailable on early dates (honestly reported, not supplemented) |
| Q4 | Concentration distinguishes similar State/Breadth? | **EXTERNAL_AUDIT_PENDING** | 90 same-day cases / all 6 dates, contrast = NORMALIZED price/amount HHI |
| Q5 | Participation distinguishes similar State/Breadth+Concentration? | **EXTERNAL_AUDIT_PENDING** | 90 same-day cases / all 6 dates, similarity incl NORMALIZED HHI |
| Q6 | Raw-axis combination expresses cross-horizon divergence without score? | **SUPPORTED (external)** | 765 same + 2678 slow/fast-reverse + 416 other; by-date & by-scope breakdown. Not re-judged. |
| Q7 | Chip/Participation overlap (strong/partial/distinct)? | **INCONCLUSIVE** | `fp_segment_volume_ratio` NULL for ALL valid state rows — data gap, not method failure |

**All Q2-Q5 nearest-neighbor pairs are SAME-DATE (verified 0 cross-date pairs). No SUPPORTED auto-generated
for Q2-Q5; verdict deferred to external human audit. Q3 full D1/D3/D5 evidence only available on 2026-08-10.**

---

## 13b. Market control semantics (§8)

Market has **no board PIT membership**; the market axis SQL now reports:
- `market_universe_count` = ALL `first_pyramid_history_daily_state` rows that day (no valid filter)
- `fp_valid_count` = subset with `valid_for_market_aggregation = true`
- (08-03 universe=5277, fp_valid=5276; 08-07 universe=5277, fp_valid=5277 — distinct semantics)
- `pit_member_count` is **None** for market (not set to valid count).
- Valid-set change is recorded as `market_valid_universe_changed`, **not** `membership_changed`.
- Market does **not** participate in Q2-Q5 nearest-neighbor contrast (industry + concept only);
  it is retained as control/context.

---

## 14. Tests (§19)

`tests/test_s2_contracts.py` — **81 passed** (prior + FINAL AUDIT CLOSURE + EXACT-T1 & SAME-DAY regressions). No new framework.

EXACT-T1 & SAME-DAY CLOSURE added regression tests (§19 #1-#20):
1. instrument missing exact T-1 bar → return UNAVAILABLE (SQL has two-bar join + LEFT JOIN bp; no fallback)
2. instrument cannot fall back to an older bar (no `LAG(close)` / no `LAG(` in any return SQL)
3. 08-03 exact T-1 = 07-31 (`CANONICAL_T1_MAP`)
4. 08-10 exact T-1 = 08-07
5. price_candidate_count == price_valid_count + missing_exact_t1_count (daily CSV identity, 0 violations)
6. amount_valid universe does not require T-1 (amount_universe filters `amount IS NOT NULL`, not ret)
7. full-universe signed contribution sum == equal_weight_return_mean (delta ≈ 0 in evidence)
8. top-N subset NOT used for signed sum validation (SQL aggregates over full price universe)
9. positive_rank finds global top positive (independent window, not abs-top-40 slice)
10. negative_rank finds global top negative
11. price_valid_count / amount_valid_count never fallback into each other (no COALESCE)
12. Q2 pairs always same trade_date
13. Q3 pairs always same trade_date
14. Q4 pairs always same trade_date
15. Q5 pairs always same trade_date
16. Price-vs-Trend pairs always same trade_date
17. rank scaling performed independently per date (run_contrast groups by trade_date)
18. eligible_dates derived from eligible rows, not input pool
19. Q3 unavailable early dates remain unavailable (only 08-10 eligible)
20. no auto verdict restored (Q2-Q5 EXTERNAL_AUDIT_PENDING; Q1/Q6 external SUPPORTED preserved)

Run: `python3 -m pytest tests/test_s2_contracts.py -q` → **81 passed**. No new testing framework.

---

## 15. Outputs (§20/§22)

- `out/s2_scope_observation_daily.csv` — 3882 scope-day rows (regenerated locally; gitignored by root `*.csv`). Audited via `s2_daily_evidence_manifest.json`.
- `out/s2_daily_evidence_manifest.json` — auditability manifest: sha256, row_count=3882, unique_key_count=3882, duplicate_key_count=0, columns, trade_dates, scope_type_counts, per_date_row_counts, key null counts (**price_candidate_count / price_valid_count / missing_exact_t1_count / amount_valid_count** added), **signed_validation** (max_abs_signed_contribution_delta = 0.0), and 18 deterministic sample rows (per trade_date: lexicographically-smallest board_id for industry + concept + market).
- `out/s2_incremental_information.json` — distribution, row-aligned correlation, HHI evidence (raw+normalized), **same-day Q2-Q5 contrast** (eligible_dates / eligible_rows_by_date / missing_rows_by_date / cases_by_date), verdicts (Q1/Q6 SUPPORTED external, Q2-Q5 EXTERNAL_AUDIT_PENDING)
- `out/s2_contrast_cases.json` — Q2/Q3/Q4/Q5 same-day tie-aware rank-scaled contrast experiments
- `out/s2_chip_participation_analysis.json` — Q7 INCONCLUSIVE (unchanged)
- `out/s2_price_facts.json` — return level/distribution/breadth + universe counts (price_candidate/price_valid/missing_exact_t1/amount_valid) + same-day price-vs-trend breadth (full distribution)
- `out/s2_member_contribution_evidence.json` — top positive/negative return contributors + top abs-price + top amount contributors (with symbol/name) + full-universe signed validation (equal_weight_return_mean / sum_signed / signed_contribution_delta) + share-sum validation
- `sql/*.sql` — read-only DB-native aggregation (work_mem≤64MB, statement_timeout≤300s, max_parallel_workers_per_gather=0)
- `s2_analysis.py` / `s2_build.py` / `s2_analyze.py` — corrected pipeline
- `tests/test_s2_contracts.py` — 81 contracts

---

## 16. Resource usage (§21)

- work_mem ≤ 64MB, statement_timeout ≤ 300s, max_parallel_workers_per_gather = 0 (restored contract)
- MemAvailable at run time: **5616 MiB** (> 3 GiB gate)
- DB-native aggregation first; Python consumed only scope-day tables + compact evidence.
- No 439K member-state pull into Python. Contribution files are process-only (gitignored).
- **Contribution SQL optimization**: the exact-T1 + four-rank contribution query needed `MATERIALIZED`
  on `mem_bar` / `price_tot` / `amount_tot` / `cand_tot` / `full_contrib` to avoid re-planning the
  dual bar-join and bring it back under the 300s statement_timeout (was >300s; now completes in contract).

---

## 17. Constraints honored

- **Production DB READ ONLY** (SELECT aggregation only; no INSERT/UPDATE/DDL). Zero production DB writes.
- `dev` unchanged (verified at `6fc7384...`, not modified, not rebased).
- No recompute of State/Breadth (consumed as-is from `first_pyramid_history_daily_state`).
- No historical membership backfill. Exact T-1 uses a real trading-day pair mapping; 08-03 → 07-31.
- No entry into S3.
- No formal Review PRD modification.
- No new testing framework.

---

## 18. STOP

S2 EXACT-T1 & SAME-DAY EVIDENCE CLOSURE complete — the last S2 data/method closure. Exact canonical
T-1 implemented (no LAG), signed contribution validated DB-native over the full universe, four TRUE
global contribution ranks, Q2-Q5 same-day cross-sectional (0 cross-date pairs). Q2-Q5 verdicts deferred
to **EXTERNAL_AUDIT_PENDING** (no auto verdict). Q1/Q6 external **SUPPORTED** preserved. Q7 INCONCLUSIVE
preserved. **Do NOT enter S3.** Await user direction.
