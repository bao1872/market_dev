# S3 — Minimum Sufficient Observation Model Closure

**Branch**: `exp/scope-observation-model-v1`
**S2 FINAL SHA**: `1cbf1c54164f1aa1511a0c48670b8fdc622bacf7` — **S2 external verdict: PASS**
**This round**: `S3 Minimum Sufficient Observation Model Closure` — final semantic closure, NOT a new large data experiment.
**Scope**: semantic disposition only. No re-run of S1/S2, no production DB query, no new indicator, no new statistical method, no product implementation, no Review PRD change.
**Outputs**: `S3_SUMMARY.md`, `s3_observation_model.json`, `s3_component_disposition.json` (only these 3 files).

---

## 0. Frozen S2 verdicts (not re-judged)

| Q | Question | Frozen verdict |
|---|----------|----------------|
| Q1 | State + Breadth | **SUPPORTED** |
| Q2 | Transition incremental information | **PARTIALLY_SUPPORTED** |
| Q3 | Diffusion incremental information | **INCONCLUSIVE** (complete D1/D3/D5 same-day evidence only on 2026-08-10) |
| Q4 | Concentration | **PARTIALLY_SUPPORTED** |
| Q5 | Participation | **PARTIALLY_SUPPORTED** |
| Q6 | Cross-horizon raw axes | **SUPPORTED** |
| Q7 | Chip vs Participation | **INCONCLUSIVE** (chip-like field data gap) |

S2 is frozen PASS. S3 does not re-judge it.

---

## 1. Method

S3 answers **one question**: what is the *minimum but semantically complete* Scope Observation Model?

It does **not** search for best score / best weight / best threshold / predictive power / future return /
opportunity / risk ranking. It only assigns each Observation object one of:

- **CORE** — indispensable semantic primitive
- **EXPLANATORY** — derived/display layer used to explain CORE facts; not a primitive
- **DERIVED_REDUNDANT** — mathematically derivable, or semantically identical with zero added observation info
- **PROVISIONAL** — retained but not yet verified (evidence/verdict inconclusive)
- **UNRESOLVED** — cannot be decided with current real data

**No correlation-based dimension removal.** Per §5, only (A) math-derivable or (B) semantic-identical-with-zero-
added-info qualifies as DERIVED_REDUNDANT. Different semantics stay distinct even if correlated.

---

## 2. Final Minimum Observation Model (A)

```text
SCOPE OBSERVATION
│
├── PRICE  (top-level result fact layer — NOT a Trend score)
│     ├── Return Level                      [CORE]
│     ├── Return Distribution               [CORE]
│     ├── Price Breadth                     [CORE]
│     ├── Signed Return Contribution        [EXPLANATORY]
│     ├── Price Concentration               [CORE]
│     └── Amount Contribution / Concentration [CORE]
│
├── TREND     (slow)    State+Breadth [CORE] · Transition [CORE] · Diffusion [PROVISIONAL]
├── STRUCTURE (medium)  State+Breadth [CORE] · Transition [CORE] · Diffusion [PROVISIONAL]
├── MOMENTUM  (fast)    State+Breadth [CORE] · Transition [CORE] · Diffusion [PROVISIONAL]
│
├── PARTICIPATION
│     ├── Volume participation distribution  [CORE]
│     └── Amount participation distribution  [CORE]
│
├── CONCENTRATION / CONTRIBUTION
│     ├── Price Concentration                [CORE]
│     ├── Amount Concentration               [CORE]
│     ├── Signed Return Contribution         [EXPLANATORY]
│     └── Amount Contribution                [EXPLANATORY]
│
└── CHIP  [UNRESOLVED]  → slot: CHIP / PARTICIPATION RELATION — PENDING DATA
```

No new top-level dimension is added. The above is the minimum set that is semantically complete given
the frozen S2 evidence.

---

## 3. Per-object disposition (B)

| Object | Disposition | Basis |
|--------|-------------|-------|
| PRICE · Return Level | **CORE** | equal_weight_return_mean is the scope-day headline return fact |
| PRICE · return_median | **DERIVED_REDUNDANT** → merged into return_p50 | identical fact (same values in S2: median=0.0077) |
| PRICE · Return Distribution (P25/P50/P75) | **CORE** | one distribution object; P10/P90 are EXPLANATORY tail |
| PRICE · Price Breadth | **CORE** | advance/decline/unchanged (return sign, no threshold) |
| PRICE · Signed Return Contribution | **EXPLANATORY** | top contributors explain who pushed/dragged; not a primitive |
| PRICE · Price Concentration | **CORE** | raw + normalized price HHI (two views, not averaged) |
| PRICE · Amount Contribution/Concentration | **CORE** | amount share + amount HHI |
| TREND/STRUCTURE/MOMENTUM · State+Breadth | **CORE** | State categorical proportions ARE Breadth (Q1 SUPPORTED) |
| TREND/STRUCTURE/MOMENTUM · Transition | **CORE** | transition RATIOS are the cross-scope core expression (Q2) |
| … · Transition raw counts | **EXPLANATORY** | audit-only; ratio is cross-scope core |
| TREND/STRUCTURE/MOMENTUM · Diffusion | **PROVISIONAL** | Q3 INCONCLUSIVE; retain D1/D3/D5, no horizon optimization |
| PARTICIPATION · Volume distribution | **CORE** | threshold-free vol_ratio20 distribution (Q5) |
| PARTICIPATION · Amount distribution | **CORE** | threshold-free amt_ratio20 distribution (Q5) |
| CHIP | **UNRESOLVED** | real data gap (fp_segment_volume_ratio NULL) |

---

## 4. What was deleted / merged, and why (C)

1. **`return_median` merged into `return_p50`** — mathematically/semantically the same fact (identical values in
   S2 evidence). One product field.
2. **No separate Breadth Score** — State categorical distribution already *is* Breadth (Q1 SUPPORTED). Designing a
   distinct Breadth Score would be redundant.
3. **Transition → ratio-only for cross-scope** — raw transition counts retained only as EXPLANATORY/audit evidence;
   all cross-scope analysis uses ratios. This was already the S2 convention and is preserved.
4. **No Concentration Score** — raw HHI is kept for single-scope time variation; normalized HHI is for cross-scope
   comparison. They are never averaged; no aggregate Concentration Score.
5. **Top contributors are EXPLANATORY, not primitives** — top_positive/negative/abs_price/amount are display layers.
6. **No Participation Score** — volume/amount ratio distributions are threshold-free; P25/P50/P75 describe one
   distribution object (not three dimensions); no active/high/low, no >1/>1.5 cut, no synthetic Participation Score.
7. **Distinct semantics kept apart (NOT merged)** — Price Breadth ≠ Trend State/Breadth; signed contribution vs abs
   price share vs amount share are three separate meanings; price concentration vs amount concentration are separate.
   These are not merged even if correlated, per §5/§9.

---

## 5. Deferred due to insufficient data (D)

- **Diffusion (all axes)**: disposition = PROVISIONAL. Full D1/D3/D5 same-day evidence exists only on 2026-08-10;
  historical PIT board data is too short. We neither delete Diffusion nor claim it is verified. Which horizon
  (D1/D3/D5) matters is explicitly **not decided**.
- **CHIP**: disposition = UNRESOLVED. `fp_segment_volume_ratio` is NULL for all valid state rows in the window.
  We do **not** force Chip==Participation or Chip!=Participation. The model keeps an explicit unresolved slot
  `CHIP / PARTICIPATION RELATION — PENDING DATA`. This does not block the observation-model closure.
- **Long-history stability**: all conclusions are short-window; long-run stability is not established.

---

## 6. Final relationships among the layers (E)

- **PRICE** is the **topmost result-fact layer**; it is what actually happened (return level/distribution/breadth)
  and the concentration/contribution that produced it. It is **not** a Trend score.
- **Price Breadth ≠ Trend State/Breadth.** Price breadth is about today's realized return sign distribution;
  Trend state+breadth is about the regime distribution. S2's price-vs-trend contrast (same-day, 0 cross-date)
  demonstrated boards with identical price breadth can have very different trend breadth (contrast up to 1.55),
  so the two are semantically distinct.
- **TREND / STRUCTURE / MOMENTUM** are the three **horizon axes**; each carries the same observation grammar:
  State+Breadth (CORE), Transition (CORE, ratio), Diffusion (PROVISIONAL).
- **PARTICIPATION** sits alongside the axes: it describes how activity (volume/amount) is distributed across
  members, threshold-free, and is a distinct dimension from price/amount concentration.
- **CONCENTRATION / CONTRIBUTION** is the family that explains the price result: who pushed/dragged (signed
  contribution, explanatory) and whether the move is concentrated in price or in amount. The three meanings
  (signed contribution / abs price concentration / amount concentration) are kept distinct and never merged.
- **CHIP** is an unresolved open slot pending real chip-like data.

---

## 7. P/Q/U/C/V scoring judgment (F)

**Judgment: NOT_NEEDED** (confidence: HIGH).

Criterion asked: *is there information that can ONLY be expressed through P/Q/U/C/V scores and that the
observation model above cannot express?*

**Answer: No.** Every P/Q/U/C/V score is an aggregation over the CORE observation facts already enumerated
(return level/distribution/breadth, state+breadth, transition ratios, price/amount concentration, participation
distributions). The observation model expresses all underlying semantics directly; P/Q/U/C/V add **no new
information**.

**Explicit distinction**:
- **summary presentation** — UI at-a-glance scores/summaries MAY remain as a *presentation convenience*. This is
  NOT part of the underlying observation model.
- **underlying observation model** — P/Q/U/C/V are **NOT** needed as the first-layer observation model. The CORE
  objects above are the observation model.

We do **not** retain P/Q/U/C/V just because "aggregation is convenient."

---

## 8. Which conclusions rest only on short-window evidence (G)

All of the following rest on the 6-day window `2026-08-03 .. 2026-08-10` plus exact-T1 same-day contrasts, and
are therefore **short-window structural evidence only**:

- Q1 State categorical proportions ARE Breadth (SUPPORTED) — structural/representational, but sample is 6 days.
- Q2 Transition ratios vary among similar State/Breadth (PARTIALLY_SUPPORTED) — 75 same-day cases over 08-04..08-10.
- Q3 Diffusion (INCONCLUSIVE) — full D1/D3/D5 only on 2026-08-10.
- Q4 Concentration distinguishes similar State/Breadth (PARTIALLY_SUPPORTED) — 90 same-day cases.
- Q5 Participation distinguishes similar State/Breadth+Concentration (PARTIALLY_SUPPORTED) — 90 same-day cases.
- Q6 Raw axes express cross-horizon divergence (SUPPORTED) — 765 same + 2678 slow/fast-reverse scope-days.
- Price Breadth ≠ Trend State/Breadth (structural distinction) — same-day contrast over 6 days.
- Signed contribution sum == equal_weight_return_mean (DB-native identity) — this is a mathematical identity, but
  the *magnitudes/distribution* are 6-day evidence.

The **model structure** (which objects are CORE / EXPLANATORY / PROVISIONAL / UNRESOLVED) is a semantic
disposition that follows from the frozen verdicts; the **numerical values and stability** of each fact are only
short-window.

---

## 9. What to re-validate if long-history PIT data becomes available (H)

Only these questions are worth re-validating (no new research-question list is generated):

1. **Diffusion incremental information (Q3)** — re-run on long history so D1/D3/D5 same-day evidence is not limited
   to a single date; decide only then whether Diffusion is CORE or foldable.
2. **Chip vs Participation relation (Q7)** — once a real chip-like field is populated, resolve the `CHIP /
   PARTICIPATION RELATION — PENDING DATA` slot (UNRESOLVED → decided).
3. **Long-horizon stability of Transition ratios and Concentration/Participation distributions** — confirm the
   Q2/Q4/Q5 PARTIALLY_SUPPORTED findings are not artifacts of one short window.

No other object's disposition needs revisiting on new data unless its verdict changes.

---

## 10. Tests (§15)

This round is semantic closure; no large test suite. Only minimal static contract checks were performed:

- [x] No score / weight / threshold introduced anywhere in the S3 model files
- [x] No new top-level dimension beyond the allowed set (PRICE / TREND / STRUCTURE / MOMENTUM / PARTICIPATION /
      CONCENTRATION_CONTRIBUTION / CHIP)
- [x] Diffusion is marked **PROVISIONAL** on all three axes
- [x] CHIP is marked **UNRESOLVED**
- [x] Formal Review PRD untouched (no modification in this commit)

No testing framework was created.

---

## 11. Constraints honored

- **No production DB query** — S3 is a pure semantic disposition over frozen S2 evidence.
- **No re-run of S1/S2** — S2 frozen PASS, not re-judged.
- **No new indicator / no new statistical method** — only existing evidence is classified.
- **dev unchanged** (verified at `6fc7384...`, not modified, not rebased).
- **No Review PRD modification.**
- **No product implementation** (frontend / API / schema / Alembic / production tables / orchestrator / Review
  pipeline rewrite).
- **No new testing framework.**

---

## 12. STOP

S3 Minimum Sufficient Observation Model Closure complete. The final minimal model keeps the semantic core
(Price facts, State+Breadth, Transition ratios, Price/Amount concentration, Participation distributions),
marks Diffusion PROVISIONAL and Chip UNRESOLVED, and concludes **P/Q/U/C/V scoring is NOT_NEEDED** as the first-layer
observation model (it adds no information beyond the CORE objects; any retention is summary presentation only).
Await user direction — do not begin product implementation.
