# REVIEW-FINAL-STATISTICAL-VALIDATION

**STATUS = EXPERIMENTAL EVIDENCE**

THIS DIRECTORY IS EXPERIMENTAL EVIDENCE.

It is NOT:
- PRD
- Map
- Change
- Runbook
- production runtime input
- strict PIT product evidence

---

## 1. Experiment question

Validate the current canonical Review backend (baseline SHA
`915b0429fb71aa9c253c2fe7f405d1ca79a69eb3`) against the frozen RAW SOURCE FACTS
dataset to answer:

- **H1** — Do the canonical Review owners produce a stable, interpretable Scope
  Composition on real historical source facts?
- **H2** — Does Scope Observation / Internal Structure / Member Attribution offer
  information beyond a simple return ranking?
- **H3** — Do the Historical Dynamics and Leadership algorithm chains run
  correctly and deterministically under a current-static membership research
  proxy?
- **H4** — Is the inability to run strict PIT historical validation caused ONLY
  by missing historical PIT membership data (not a backend implementation gap)?

This directory does NOT claim strict PIT validation; it provides
current-static research-proxy evidence for independent Git audit by the user and
ChatGPT.

## 2. Repository / backend SHA

- Branch: `experiments`
- Backend baseline SHA: `915b0429fb71aa9c253c2fe7f405d1ca79a69eb3`
- No production code / tests / docs / rules / frontend / migration changed by
  this experiment.

## 3. Input datasets

- **Base RAW SOURCE FACTS**: `backend/.perfdata/review/review-source-c5c686e-v1/`
  - `parquet/`: bars_daily, first_pyramid_daily_state, first_pyramid_events,
    boards, board_memberships_current_snapshot, instruments, trading_calendar.
  - Read-only. Not modified, not deleted, not committed.
- **SFS overlay**: `backend/.perfdata/review/review-source-c5c686e-v1-sfs-overlay-v1/`
  - `stock_feature_snapshots.parquet` (instrument_id, trade_date, source_run_id,
    summary_payload->first_pyramid_flat) for the 8 target dates.
  - Long-term offline experiment asset. Not committed, not deleted.

The current-static proxy path intentionally keeps current-only snapshot facts
(`first_pyramid_flat`) UNAVAILABLE, matching the canonical current-static
Historical DB batch semantics. The overlay is materialized as a documented data
asset but is NOT fed into the six-layer compute under this proxy.

## 4. Membership semantics

- Historical PIT board membership: **UNAVAILABLE** in the frozen dataset.
- This experiment uses **CURRENT STATIC MEMBERSHIP RESEARCH PROXY** (the current
  board snapshot, `board_memberships_current_snapshot.parquet`).
- Therefore:
  - 08/10 current state = current-state factual validation.
  - Historical Dynamics = `PASS_ON_CURRENT_STATIC_PROXY` only.
  - Leadership = `PASS_ON_CURRENT_STATIC_PROXY` only.
- Prohibited claims in this directory:
  - "strict PIT Dynamics validated"
  - "strict PIT Leadership validated"
  - "broadening / core-led / rotating / fragmenting" as verified product labels.

## 5. Data dates

- Validation window: `2026-07-29`, `2026-07-30`, `2026-07-31`, `2026-08-03`,
  `2026-08-04` (historical).
- Current-state spot check: `2026-08-10`.
- `2026-07-28` used only as T-1 for `2026-07-29`.
- `2026-08-07` used only as T-1 for `2026-08-10`.

## 6. Scope universe

All scopes supported by the frozen dataset, NOT sampled and NOT hand-filtered:

- `industry_l1`, `industry_l2`, `industry_l3`, `concept`.

## 7. Canonical owners used (called, not reimplemented)

- `review_scope_dynamics_probe._build_replay_selection_from_specs` + `_load_capacity_facts`
  (canonical selection-first, memory-bounded fact loading).
- `build_union_fact_context_from_loaded_facts` + `build_prepared_scopes_from_union`
  (canonical preparation owner).
- `compute_scope_observation` (Scope Observation).
- `compute_internal_structure` (Internal Structure).
- `compute_member_attribution` (Member Attribution).
- `compute_member_leadership_contributions` / `build_leadership_snapshot` /
  `compute_leadership_migration` (Leadership).
- `build_observation_series` / `compute_scope_dynamics_analysis` (Historical
  Dynamics in-memory canonical primitives).
- `compose_canonical_review_scope` (Composition aggregation).

## 8. Statistical definitions

- **member_count**: number of current-static members in the prepared scope.
- **equal_weight_return / amount_weighted_return**: canonical price-layer facts.
- **advance_ratio / decline_ratio / unchanged_ratio / return_dispersion**:
  canonical price-layer breadth facts.
- **capital_tilt**: canonical price-layer capital tilt (aw vs ew).
- **price_raw_hhi / price_normalized_hhi**: canonical price concentration.
- **amount_raw_hhi / amount_normalized_hhi**: canonical amount concentration.
- **cross-section ranking tie-break**: by metric value, then by scope_key
  (deterministic, ascending scope_key).
- **Attribution top-N abs-share**: top-N sum of |canonical contribution|
  (or |tilt_contribution| / |hhi_contribution|) / total |contribution| sum in the
  group, using unrounded canonical values. Empty group or zero total => blank.
- **zero_or_unavailable_count** (attribution): `aw_universe_count` −
  `len(direction.positive)` − `len(direction.negative)`, floored at 0.
- **percentiles**: linear interpolation `pct` between sorted values
  (n=1 => the single value).

## 9. Null / unavailable handling

- Any canonical field that is `None` / unavailable is written as an **empty cell**
  (never silently coerced to 0). Status/reason is preserved in the readiness and
  per-layer columns.
- `unavailable_current` is the valid compose-layer status for the
  single-date-proxy leadership/historical_dynamics layers.
- `STRICT_PIT_HISTORICAL_PRODUCT_VALIDATION = BLOCKED_BY_MEMBERSHIP_DATA`.

## 10. Deterministic ordering

- All rankings / representative selection use deterministic sorts:
  `(metric_value, scope_key)` for cross-section and representative selection;
  member orders follow the canonical owners' own ordering (the owners are
  order-independent; the experiment does not add its own member RNG).
- Representative selection uses the fixed rules in §12 of the task; no
  story-based substitution.

## 11. Output files

- `results/scope_daily_metrics.csv`
- `results/scope_daily_readiness.csv`
- `results/family_daily_summary.csv`
- `results/cross_section_rankings.csv`
- `results/dynamics_statistics.csv`
- `results/leadership_statistics.csv`
- `results/attribution_statistics.csv`
- `results/determinism_reconciliation.csv`
- `results/closure_gate_matrix.json`
- `samples/representative_scope_compositions.json`
- `samples/representative_member_attribution.json`

These CSVs/JSONs are **persistent experiment evidence** explicitly requested by
the user, not the temporary-CSV category referenced in `rules/50`.

## 12. Reproduction command

```bash
cd /Users/zhenbao/Desktop/coding/market_dev
PYTHONPATH=. APP_ENV=test \
  DATABASE_URL="postgresql+psycopg://x:x@localhost:5432/bz_stock_test" \
  REDIS_URL="redis://localhost:6379/0" \
  backend/.venv/bin/python experiments/review_final_validation/run_validation.py
```

(Env vars are only needed to satisfy import-time `Settings`; no DB/Redis
connection is made.)

## 13. Known limitations

- **strict PIT historical membership is unavailable**; all dynamics/leadership
  results are `RESEARCH_PROXY — CURRENT STATIC MEMBERSHIP`.
- Current-only snapshot facts (SFS `first_pyramid_flat`) are intentionally not
  consumed in this proxy path, matching canonical current-static semantics.
- SFS `first_pyramid_flat` coverage is 0 for `2026-07-28`/`2026-07-29`
  (v4 runs predate that payload sub-key); those two dates are only used as
  T-1 / T-1-for-07-29 in this experiment.
- **Dynamics window is bounded to 15 trading days pre-T** (not the full 120) so
  the full 768-scope universe × 6 dates is tractable offline while still
  exercising a real pre-T historical series (velocity/acceleration/persistence)
  under the current-static proxy. This is a documented tractability choice, not
  a substitute for the full 120-day production axis.
- `composition_readiness = unavailable_current` for every scope: the single-date
  proxy intentionally marks leadership and historical_dynamics layers
  `unavailable_current`. The six canonical owners still executed (see the
  per-layer readiness and statistics CSVs).
- The experiment calls canonical owners only; it does not assert product value.
- This is NOT a release/RTM or a claim of backend closure. `BACKEND_CLOSURE` is
  decided by user + ChatGPT after independent Git audit, not by this IDE.
