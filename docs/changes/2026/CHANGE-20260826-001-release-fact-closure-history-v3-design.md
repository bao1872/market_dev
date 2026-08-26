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

### A4 — active-squeeze regression 修复（CORRECTION GATE 2）

上一轮 `60eb6729` 把 `squeeze_period_volume_mean` 与 `release_volume_ratio` 都只从
「当前 T 的 SQZ_RELEASE event」读取。但 `缩量挤压` 分支要求 `last_sqz_on == True`
（T 仍 sqzOn），与「T 是 release（sqzOn[T]=False）」互斥 → **`缩量挤压` 成为死分支**（确定 regression）。

修复：把 owner 提升一层。`build_momentum_history` 在 `daily_state[T]` 对每个 T 暴露
`SqueezeVolumeFacts`（**单 owner，禁止下游自行扫描 squeeze 区间**）：

```text
CASE 1 — T 仍 sqzOn[T]:
    squeeze_period_volume_mean = 当前连续 sqzOn 区间均量（含 T）
    release_volume_ratio      = None
CASE 2 — 刚 release (sqzOn[T-1] && sqzOff[T]):
    squeeze_period_volume_mean = T 前连续 sqzOn 区间均量
    release_volume_ratio      = squeeze_mean / volume[T]  (需 volume[T]>0)
CASE 3 — 其他: 两者均 None
```

`SQZ_RELEASE event` 仅投影 CASE 2 的同一 `daily_state[T]` 事实（禁止重算）。
`_build_momentum_dimension` 改为消费 `mh["daily_state"][last_bar_index]`，使
`缩量挤压`(last_sqz_on+mean) 与 `放量释放`(last_sqz_off+ratio) 均可达。
新增 B1 active-squeeze 用例 + B2 `缩量挤压` 可达 consumer 测试验证。

`fp_squeeze_avg_volume`, `fp_release_volume_ratio`, `fp_momentum_volume_relation` all
consume the corrected owner. `momentum_volume_relation` (`vol_divergence`) inherits the
direction fix.

## 2. 8/25 page census — true source trace + JSON null 纠正 (C/D)

The screenshot field "释放量比" is served by the `observationGroups.momentum.release_volume_ratio`
projection, whose canonical source is `ReviewScopeObservationFact.observation_payload`
→ member `release_volume_ratio` → snapshot `fp_release_volume_ratio`. This path does NOT
touch `market_review_metric_observations`.

### 5293/5293 结论已作废（INVALID_JSON_NULL_CHECK）

上一轮使用的 SQL 为：
```sql
count(s.summary_payload->'first_pyramid_flat'->'fp_release_volume_ratio')
```
`->` 在 key 缺失时返回 JSON `null`，而 `count()` 跳过 SQL NULL 但**不跳过 JSON null**，
于是把 5293 个都含 `"fp_release_volume_ratio": null` 的 snapshot 误统计成「非 null」。

正确统计（jsonb_typeof）：
```text
total=5293  key_missing_sql_null=0  json_null=5293  json_number=0  other_type=0
```
→ 8/25 全市场 **0 个** snapshot 有数值型 `fp_release_volume_ratio`（全部 JSON null）。

### 因此「释放量比 暂无事实」是 LEGIT_UNAVAILABLE（非 producer defect）

- 全市场 5293 个 `fp_release_volume_ratio` 全为 JSON null → 每个成员的
  `MemberObservation.release_volume_ratio` = None。
- 全市场 749 个 8/25 scope facts（concept 371 / industry_l1 31 / industry_l2 90 /
  industry_l3 257）的 `momentum.release_volume_ratio` 全部
  `status=unavailable, valid_count=0`，reason =
  `CURRENT_SOURCE_UNAVAILABLE: no member has a consumable exact-T canonical snapshot value`。
- 即：8/25 当天**没有任何成员 snapshot 携带可用的 release ratio**，
  `valid_count=0` 是数据真实状态的必然结果，**不是 producer 计算错误**。

**纠正上一轮错误结论**：此前称「释放量比 on 8/25 为 PRODUCER_DEFECT」是错的，
根因正是无效的 5293 查询。正确分类 = **LEGIT_UNAVAILABLE**。

A 修复保证**未来** ratio 计算正确；但 8/25 历史 snapshot 本身无可用 ratio 值，
页面在 8/25 显示「暂无事实」在语义上正确，无需 reprocess 来「修正」该字段
（reprocess 不会产出原本不存在的数据）。

### 精确 scope 成员枚举（§D）状态

截图对应 scope（n≈16）的 exact members / pit_member_count / 各成员
`fp_release_volume_ratio` / `MemberObservation.release_volume_ratio` 枚举，**需要截图对应的
scope identity（scope_key）**。该信息仅存在于截图/URL，文本无法推断，故本轮标记为
`BLOCKED_NEEDS_SCOPE_KEY`。一旦提供 scope_key，即可按 §D 完成：
current sqzOff count / current-day SQZ_RELEASE event count / source numeric count /
expected valid_count / published valid_count 的逐项核对。
因全市场 source numeric=0，任何 scope 的 expected valid_count 必 = 0 = published，
分类恒为 LEGIT_UNAVAILABLE。

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
  B1 `build_momentum_history` SqueezeVolumeFacts（exactly-one release, no-release-after-
  continued-sqzoff, still-squeezing mean present & ratio None, release-day mean from prior
  squeeze & ratio=mean/vol[T], release-2nd-day both None, no-sqz, event==daily_state
  projection, vol<=0 ratio None but mean exists）.
  B2 consumer（注入 daily_state[T] sentinel → 只转发不重算；缩量挤压分支可达；
  vol_divergence 阈值 ratio=0.50→放量释放, ratio=0.80→not）。
- Regressions: `test_first_pyramid_flatten`, `test_review_observation_prep`,
  `test_review_observation_group_service`, `test_change_20260729_003`,
  `test_review_scope_observation`, `test_review_observation_groups`,
  `test_release_volume_ratio_ssot` → 270 passed.
- `test_review_vectorized_facts` has 4 pre-existing failures confirmed identical on base
  `aedcc766` (unrelated to this change; not a regression).
