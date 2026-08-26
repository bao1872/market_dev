# CHANGE-20260826-001 — Release Volume Ratio Closure + History-v3 Boundary Design

- Status: `verified_code_pending_acceptance`
- Base: `aedcc76639fc1d82d74ee8f83c0a6a308c5c7182` (correction commit on top)
- No production deploy / no production DB write / no after-close run / no History backfill this round.
- after-close worker remains STOPPED.

## 1. Release-fact semantic closure (P0)

`build_momentum_history` is the **single SSOT owner** of SQZ_RELEASE:

```text
SQZ_RELEASE trigger : sqzOn[T-1] == True AND sqzOff[T] == True
release_volume_ratio : squeeze_period_mean_volume / release_volume[T]   (squeeze mean in numerator)
```

`_build_momentum_dimension` no longer independently searches for the release bar or
computes any ratio. It consumes `release_volume_ratio` AND `squeeze_period_volume_mean`
directly from the SSOT event (additively extended to expose `squeeze_period_volume_mean`).

### A1 — vol_divergence direction fix (was a deploy blocker)

Old code kept `if release_vs_squeeze_vol_ratio > 1.5: vol_divergence = "放量释放"`.
With canonical ratio = `squeeze_mean / release_volume`, the business rule
"release_volume > 1.5 × squeeze_mean" is algebraically equivalent to
`ratio < 1/1.5 ≈ 0.6667`. The old `> 1.5` was numerically inverted (would label a
low-volume release day as 放量释放).

Fix: introduced `RELEASE_VOLUME_RATIO_EXPAND_THRESHOLD = 2.0/3.0` with an explicit
comment on the reciprocal relationship; `vol_divergence = "放量释放"` now requires
`release_vs_squeeze_vol_ratio < RELEASE_VOLUME_RATIO_EXPAND_THRESHOLD`.

### A2 — squeeze_period_volume_mean closure

Old code re-derived the squeeze interval from `sqz_on_list[T]` backward; when T is the
formal release (`sqzOn[T]=False`), the loop stopped at step 1 → `squeeze_period_volume_mean`
was lost on release days. Now supplied by the SSOT event (which already computed
`squeeze_mean` internally and now exposes it). On `vol[T] <= 0` the mean is still exposed
(only the ratio requires `vol[T] > 0`).

### A3 — downstream chain verified

```text
_build_momentum_dimension
  → fp_release_volume_ratio (first_pyramid_flatten.py:942, from mom_cf['release_vs_squeeze_volume_ratio'])
  → observation_prep.py:366 → MemberObservation.release_volume_ratio
  → scope_observation.py:1527 release_volume_ratio_values → _current_only_distribution (1739)
  → API review.py observationGroups.momentum.release_volume_ratio
  → scopeMomentumVolumeContract.parseMomentumObservation → releaseVolumeRatio
  → ScopeMomentumObservation.tsx "释放量比"
```

`fp_squeeze_avg_volume`, `fp_release_volume_ratio`, `fp_momentum_volume_relation` all
consume the corrected owner. `momentum_volume_relation` (`vol_divergence`) inherits the
direction fix.

## 2. 8/25 page census — true source trace (C)

The screenshot field "释放量比" is served by the `observationGroups.momentum.release_volume_ratio`
projection, whose canonical source is `ReviewScopeObservationFact.observation_payload`
→ member `release_volume_ratio` → snapshot `fp_release_volume_ratio`. This path does NOT
touch `market_review_metric_observations`.

Correction to prior report: `market_review_metric_observations = 0` for 8/25 is
**NOT** proven to be the page's data source (PAGE_RELEVANCE: UNKNOWN). The page's real
source (`review_scope_observation_facts` / observationGroups) was confirmed READY
(provided_member_count == pit_member_count).

8/25 evidence:
- 5293 / 5293 8/25 snapshots have non-null `fp_release_volume_ratio` →
  "释放量比" on 8/25 was NOT a `valid_count=0` legit-unavailable; under the old inverted
  formula those values were directionally wrong → **PRODUCER_DEFECT, fixed by A**.
- The already-published 8/25 Review payload still carries the OLD (wrong) distribution.
  Correcting the 8/25 page requires a separate deploy + 8/25 reprocess (deferred; not done
  this round, per gate rules NO deploy / NO backfill).

## 3. History finding correction (D)

Prior parity report stated "History rolling 5 fields re-implemented member_fact formula".
This is inaccurate: `compute_first_pyramid_history()` explicitly calls the shared
`compute_ratio` / `compute_percentile` / `compute_price_position_120d` pure functions.
Correct finding:

```text
History recomputes business facts from a SECOND input lineage / bars window / DSA window,
even where the formula owner is shared.
```

Preserved evidence:
- History DSA `lookback=None` vs Core `DSA_LOOKBACK`.
- Real numeric drift: `current_vs_prev_volume/amount_mean_ratio` 13.55%; regime/segment/VWAP
  small real mismatches; enum fields are representation-layer only.

## 4. History-v3 design RTM (E) — design only, no migration this round

Ruling:

```text
review-history-v2 = legacy immutable recompute semantics  (frozen, read-only compatible)
review-history-v3 = canonical Core projection semantics     (new version)
```

Same contract version = same semantic meaning. Mixing new projection semantics into v2
is forbidden. v3 must not silently rewrite v2 semantics.

### v3 field RTM (target)

| v3 field | canonical Core/Artifact source | mapping | rep conversion | availability rule | event source | current-T materializable? | historical rebuild |
|---|---|---|---|---|---|---|---|
| regime_value / strength | Core `first_pyramid_flat` | 1:1 | enum/number mapping | Core(T) ready | — | yes | Core replay |
| dsa_dir_bars / vwap_dev | Core `first_pyramid_flat` | 1:1 | — | Core(T) ready | — | yes | Core replay |
| segment_* | Core `first_pyramid_flat` | 1:1 | — | Core(T) ready | — | yes | Core replay |
| review_volume/amount_ratio/percentile | Core `first_pyramid_flat` (computed once) | 1:1 projection | — | Core(T) ready | — | yes | Core replay |
| price_position_120d | Core `first_pyramid_flat` | 1:1 | — | Core(T) ready | — | yes | Core replay |
| squeeze_release facts | Core SQZ_RELEASE event (build_momentum_history) | projection | — | event present | Core SQZ_OFF | yes | Core replay |
| momentum_diffusion | Core MOMENTUM_DIFFUSION | projection | — | event present | Core ZERO_CROSS_UP/DOWN | yes | Core replay |

### New daily path (target)

```text
Core compute once
  → durable StockFeatureSnapshot / CoreArtifact
    → Review(T) consumes Core(T) + History(<T)
      → History-v3(T) pure materialization
        → NO DSA / SMC / SQZMOM kernel
```

`compute_first_pyramid_history()` becomes **backfill/replay only**; daily AfterClose must
not call a second recompute kernel.

### Event parity (must be completed in v3 RTM)

```text
Core SQZ_OFF            ↔ History SQZ_RELEASE
Core MOMENTUM_DIFFUSION ↔ History ZERO_CROSS_UP / ZERO_CROSS_DOWN
```

Not merely "events exist"; the type-to-type semantic mapping must be verified.

## 5. Tests

- New `test_release_volume_ratio_ssot.py` (B1 SSOT layer + B2 monkeypatch wiring layer):
  SSOT event correctness (exactly-one release, no-release-after-continued-sqzoff, still-
  squeezing, no-sqz, squeeze length, squeeze_period_volume_mean, ratio direction, vol<=0)
  and consumer-forwarding (sentinel injected → only forwards; no recompute; threshold
  semantics ratio=0.50→放量释放, ratio=0.80→not).
- Regressions: `test_first_pyramid_flatten`, `test_review_observation_prep`,
  `test_review_observation_group_service`, `test_change_20260729_003`,
  `test_review_scope_observation`, `test_review_observation_groups`,
  `test_review_vectorized_facts` → 279 passed.
- `test_review_vectorized_facts` has 4 pre-existing failures confirmed identical on base
  `aedcc766` (unrelated to this change; not a regression).
