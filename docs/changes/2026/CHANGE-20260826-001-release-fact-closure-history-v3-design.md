# CHANGE-20260826-001 — Release Volume Ratio Closure + History-v3 Boundary Design

- Status: `verified_code_pending_acceptance`
- Base: `946ef94fe3d40b14b9bb059e3672d484d6fdb66f`
- No production deploy / no production DB write / no after-close run / no History backfill this round.
- after-close worker remains STOPPED.
- 8/25 release ratio 历史结论冻结为 `LEGACY_SNAPSHOT_INCOMPLETE_REQUIRES_REPROCESS`
  （不再跑 86-stock production forensic；交由未来 deploy+reprocess 验收）。

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

### A5 — Squeeze event identity 与 volume availability 解耦（CORRECTION GATE 3 / PHASE A）

确定 bug：`seg_start` 原仅在 `vol_arr is not None` 时计算，但 SQZ_RELEASE event 路径
无条件引用 `seg_start` → `vol_arr=None` 时 NameError（event 路径崩溃）。

修复：`build_momentum_history` 先完全根据 `sqz_on_list` / `sqz_off_list` 解析 squeeze window
身份（`squeeze_start_index` / `squeeze_length`），**该逻辑不依赖 volume**；volume 仅用于
可选的量能事实（CASE 1/2 的 mean/ratio）。`vol_arr=None` 时 event 仍合法生成，身份恒存在，
量能事实为 None，无异常。

### A6 — finite volume contract（PHASE A 收尾，本轮冻结项）

代码注释与测试合同规定 release volume 必须 `finite && > 0`，但原实现仅 `vol_arr[i] > 0`
且 squeeze 区间仅 `~np.isnan(...)` 过滤，导致 `+inf`/`-inf` 会通过 `> 0` 并被算入均量
（如 `volume[T]=+inf` → `ratio = mean/inf = 0`，甚至误判为「放量释放」），与「非有限量能应
unavailable」不一致。

修复（`sqzmom_lb.build_momentum_history`）：
```python
# squeeze 区间过滤
valid = seg[np.isfinite(seg)]
# release volume 门
if np.isfinite(vol_arr[i]) and vol_arr[i] > 0:
    release_vol_ratio = squeeze_mean / float(vol_arr[i])
```
非有限 volume → event identity 保留、volume fact unavailable。

新增 B1 测试（`test_release_volume_ratio_ssot.py`）：
1. `+inf`/`-inf` squeeze 成员 → `np.isfinite` 过滤，区间均量只取有限值；
2. `release volume = +inf` → 通过 `>0` 但 `isfinite` 失败 → `ratio=None`（不产出 0）；
3. `release volume = -inf` → `isfinite` 失败 → `ratio=None`；
4. squeeze 区间混有 ±inf 与有限值 → 均量仅用有限值（`[inf,50,-inf]` → mean=50）。

PHASE B negative tests 新增：
1. SQZ_RELEASE + volumes=None → event 生成、seg_start/squeeze_length 正确、mean/ratio=None、无异常；
2. SQZ_RELEASE + squeeze history 含 NaN → window identity 不受影响；
3. SQZ_RELEASE + release volume NaN → event 存在、mean 可存在、ratio=None；
4. active squeeze + volume unavailable → state 合法、mean=None；
5. valid volume → active squeeze mean 正确、release mean/ratio 正确。

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

> **⚠ 该结论在 CORRECTION GATE 3 被推翻**（见下方 PHASE C 独立 transition census）。
> 以下原 LEGIT 论证保留为历史记录，最终裁决以 PHASE C 为准。

- 全市场 5293 个 `fp_release_volume_ratio` 全为 JSON null → 每个成员的
  `MemberObservation.release_volume_ratio` = None。
- 全市场 749 个 8/25 scope facts（concept 371 / industry_l1 31 / industry_l2 90 /
  industry_l3 257）的 `momentum.release_volume_ratio` 全部
  `status=unavailable, valid_count=0`，reason =
  `CURRENT_SOURCE_UNAVAILABLE: no member has a consumable exact-T canonical snapshot value`。

#### PHASE C（独立 release-transition census，不使用 ratio 自身为证据）

使用正式 persisted canonical Core state（`stock_feature_snapshots.summary_payload`
→ `first_pyramid_flat` → `fp_squeeze_state` + `fp_latest_sqz_off_freshness`），
对同时具有 8/24 与 8/25 快照的 instrument 解析真实 momentum squeeze transition：

```text
REAL_RELEASE = squeeze_state(8/24)='挤压中'
            AND squeeze_state(8/25)='已释放'
            AND latest_sqz_off_freshness(8/25)=0
```

结果（真实 SQL 计数，非 ratio 反推）：
```text
ELIGIBLE_PAIR_COUNT          = 5293
REAL_RELEASE_EVENT_COUNT_0825 = 86
RELEASE_RATIO_JSON_NUMBER_COUNT = 0   (全市场 fp_release_volume_ratio 全 JSON null)
RELEASE_RATIO_JSON_NULL_COUNT   = 5293
```

独立证据（未进一步 forensic 重建历史有效 volume 基数）：
```text
REAL_RELEASE_EVENT_COUNT_0825 = 86   (真实 squeeze-release transition 存在)
RELEASE_RATIO_JSON_NUMBER_COUNT = 0  (8/25 全市场 fp_release_volume_ratio 全 JSON null)
EXPECTED_RELEASE_RATIO_COUNT   = 未重建 (因本轮 AST-FORWARD：不再跑 86-stock production forensic)
```

→ **RELEASE_RATIO_0825 = LEGACY_SNAPSHOT_INCOMPLETE_REQUIRES_REPROCESS**

**含义**：8/25 历史快照生产时 `fp_release_volume_ratio` 字段尚未被 materialize
（该字段由本 CHANGE 系列新增，8/25 历史快照早于它），导致 86 个真实 release 成员均无数值
ratio。本轮 gate 规则明确：**NO production deploy / NO reprocess / 不再做旧数据考古**。
因此 8/25 的确切 EXPECTED_RELEASE_RATIO_COUNT（86 ∩ 合法 volume 基数）**有意不再 forensic
重建**，交由未来「正式 deploy 修正后 producer + reprocess」统一验收。

**此冻结结论不阻断已修正的 producer 合同**：PHASE A 的 `np.isfinite` volume 合同与
A1–A5 的 owner 修复保证未来快照正确填充；88/25 页面该字段 unavailable 属「待 reprocess 的
legacy snapshot 不完整」，非页面逻辑错误，也非本轮需继续论证的 PRODUCER_DEFECT 铁证。

**纠正前两轮错误结论**：此前两论称「释放量比 on 8/25 = LEGIT_UNAVAILABLE」是错的（循环论证：
只用 ratio 自身为空反推无 release）。独立 transition census 已证明 86 个真实 release 存在，
但本轮不再进一步 forensic 重建历史有效 volume 基数以升级为 PRODUCER_DEFECT 铁证。

#### PHASE D — 全量 8/25 Review 用户可见字段 census（方法纠正，本轮不重跑）

> **方法纠错（CRITICAL）**：上一版文档写「递归遍历 `ReviewScopeObservationFact.observation_payload`
> 发现 7 个用户可见 leaf unavailable」是**错误的方法**。正式 API 是运行时才把 L1
> `observation_payload` 经 `build_l2_observation_groups()` 投影成 L2 `observationGroups`
> 再送前端；**递归扫描原始 observation_payload ≠ 扫描前端用户可见字段**。
> 本轮按 AST-FORWARD 不再跑 production forensic，故 L2 contract-aware census 推迟到
> History-v3 稳定后的独立验收轮，此处仅记录方法纠错，不保留旧 recursive census 结论。

L2 固定 8 组（`observation_groups.py` `L2_GROUP_SPECS`）及其 L1 source path 已明确，例如：
```text
momentum_squeeze_release:
    squeeze_state      ← momentum.squeeze_state
    bb_position        ← momentum.bb_position
    bb_width           ← momentum.bb_width
    release_volume_ratio ← momentum.release_volume_ratio
trend_volume_confirmation:
    momentum_volume_relation ← momentum.momentum_volume_relation
```
后续 census 应直接调用 `build_l2_observation_groups(fact.observation_payload)`（禁止手写 JSON path），
且**不得用统一的 `status=="available"` 判断所有字段**——不同 fact shape 不同
（`_current_only_distribution` 与 `_open_categorical_distribution` 的 available 输出本就无
`status="available"`，只有 unavailable 才有 `status`）。旧「`momentum_volume_relation` 0/81
available」统计即源于此解释错误：81 行实际是 **81 个 scope 该 categorical 无数据（status=unavailable）**，
其余约 668 个 scope 本就有 available 输出（只是 shape 无 `status` 字段，被 query 漏数）。

按 PHASE E 业务不变量分类（禁止 source null 自动归 LEGIT；本轮不重跑，仅记录待验收项）：

- **PENDING_REPROCESS_VERIFY**（需未来 deploy+reprocess 后由 contract-aware L2 census 验收）
  - `momentum.release_volume_ratio`（独立证明 86 真实 release；0 数值源于 legacy snapshot 未 materialize）。
  - `momentum.momentum_volume_relation`（81 scope 无 categorical 数据；其 canonical
    `vol_divergence` owner 条件已明确，需重算 expected member set 与 persisted parity，疑为
    legacy snapshot 不完整而非 projection defect）。
- **NOT_ASSESSED_IN_THIS_GATE**（chip/price/structure 模块，本轮未跑其独立 source census）
  - `chip`、`price.concentration`、`price.signed_contribution`、`price.amount.concentration`、
    `structure.events` → 各自有独立 canonical source，本轮不判定 LEGIT 也不判定 DEFECT。
- **LEGIT_UNAVAILABLE** = 无本轮确认项（同上，待 contract-aware census 验收）。

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
  projection, vol<=0 ratio None but mean exists）+
  PHASE B（vol=None event 仍生成且身份正确、NaN history 不影响 identity、NaN release vol
  ratio=None、active squeeze vol=None mean=None、valid vol mean/ratio 正确）；
  **A6 finite-input（±inf squeeze 成员过滤、±inf release volume → ratio=None、混合
  区间均量仅用有限值）**.
  B2 consumer（注入 daily_state[T] sentinel → 只转发不重算；缩量挤压分支可达；
  vol_divergence 阈值 ratio=0.50→放量释放, ratio=0.80→not）。
- Regressions: `test_first_pyramid_flatten`, `test_review_observation_prep`,
  `test_review_observation_group_service`, `test_change_20260729_003`,
  `test_review_scope_observation`, `test_review_observation_groups`,
  `test_release_volume_ratio_ssot` → 279 passed.
- `test_review_vectorized_facts` has 4 pre-existing failures confirmed identical on base
  `aedcc766` (unrelated to this change; not a regression).

## 5b. History-v3 contract freeze (PHASE 1–14)

> AST-FORWARD：PHASE 0 之后进入 History-v3。本轮目标 = 把「Daily AfterClose 重新计算
> First Pyramid History」改为「canonical Core 计算一次 → History-v3 纯投影」。

### 不变量
```text
ONE instrument + ONE trade_date + ONE canonical Core input
= ONE DSA/SMC/BB/SQZMOM/VolumeContext compute
History-v3 只能投影、不能运行 kernel。
```
- `review-history-v2` = legacy recomputation semantics = immutable / read-compatible only
- `review-history-v3` = canonical Core projection semantics
- 禁止 v2/v3 混写。

### 实现（已落入代码）
1. **新增常量** `REVIEW_HISTORY_V3_CONTRACT_VERSION = "review-history-v3"`
   （`first_pyramid_service.py`）。
2. **纯投影 owner** `app/services/history_v3_projection.py`
   - `build_history_v3_projection(core_flat, instrument_id, trade_date, core_run_id)`
     纯函数、无 IO、无 kernel 调用；
   - 全字段 RTM：`fp_*` → v3 canonical key，含显式 adapter
     （`momentum_change`/`sqzmom_delta` 数值 → enhancing/weakening/flat；
     Core display enum 透传）；
   - 事件投影：Core `fp_structure_event_*` / `fp_momentum_event_*` / `fp_node_event_*`
     + 由 `fp_squeeze_state=已释放` 推导 `SQZ_RELEASE`；不重判结构/动量事件；
   - `lineage` = sha256(state+events+core_run_id)（deterministic）。
3. **物化器** `materialize_history_v3_from_core(session, instrument_id, trade_date,
   core_flat, core_run_id)`（`first_pyramid_history_service.py`）：
   - 输入仅为 durable Core artifact（`StockFeatureSnapshot.summary_payload["first_pyramid_flat"]`）；
   - 复用既有 `_persist_history_result`（state upsert + events immutable insert），
     `history_contract_version=review-history-v3`；
   - **禁止** `compute_first_pyramid_history` / `advance_history_to_trade_date` /
     `compute_dsa_bundle` / `compute_smc_pine` / `compute_sqzmom_lb` / VolumeContext；
   - 不新增数据库表（复用既有 `FirstPyramidHistoryDailyState`/`Event` 的
     `history_contract_version` + JSONB payload）。
4. **AfterClose 重排**（`after_close_orchestrator.py`）
   - `computing_history` 步骤改用 `_make_history_v3_step`：从当日 `StockFeatureSnapshot`
     读 durable Core flat → 投影物化 v3（不再 `advance_history_to_trade_date`）；
   - 旧 `_make_history_step` + `advance_history_to_trade_date` 保留给 legacy v2
     replay/backfill，daily AfterClose 不可达；
   - Review(T) gate：v3 投影成功即 `ready`（不再等待重算）；正式 Review current owner 为
     `published stock_core(T)`（Core 已算一次），History 提供 `<T` baseline。
5. **状态机**：保留 `computing_history` 枚举（implementation semantics = projection/
   materialization only）；不在本轮扩大 migration scope 改名。

### 测试
- `test_history_v3_projection.py`（PURE_UNIT）：
  - **compute-once spy gate**（`test_v3_is_pure_projection_no_kernel_calls`）：
    投影执行期 `compute_sqzmom_lb` / `compute_dsa_bundle` / `compute_smc_pine` 调用计数 = 0；
  - 全字段 RTM + momentum delta adapter；
  - 事件投影（BOS + SQZ_RELEASE 来自 Core，不重判）；
  - deterministic lineage；`to_history_result_shape` 携带 v3 contract_version。
- `test_history_v3_materialize.py`（@pytest.mark.postgres）：
  - crash/resume 幂等（重复 materialize → state 行数=1、events 不重复）。
- 相关回归 `test_release_volume_ratio_ssot` + `test_first_pyramid_flatten` +
  observation/review 系列 → 285 passed。

### 治理
- Daily History = projection/materialization，not recomputation；
- Review(T) = Core(T) + History(<T)；
- v2 = legacy，v3 = canonical Core projection；
- 当前治理本身已要求 Compute Once，本 CHANGE 仅固化为代码不变量 + spy gate。

## 5c. Slice 1 — REVIEW-CURRENT-OWNER-01（CORRECTION，已提交并推送）

> 单向 Ownership 收敛第一步（修正版）：**Review(T) = Core(T) + History(<T)**。
> 当前第一金字塔事实**全部**来自已发布 Core(T)（StockFeatureSnapshot.first_pyramid_flat，
> 锁定 source_core_run_id）；exact-T History(T) 既不提供 Current 事实，也不挡在 Review 前面。

### 这轮修正了什么（相对首版 Slice 1 的 P0 缺口）
首版只删了 `HISTORY_NOT_READY_T` 硬门，但 Review Current(T) 的 Trend / Structure State /
Momentum / state-driven Volume 仍来自 `FirstPyramidHistoryDailyState(T)`（经
`previous_state_to_flat` / `state_to_continuous`）。这轮把 Current(T) 真正归 Core(T) 所有。

1. **Current(T) 事实归 Core(T)**（KPI-1/2）：
   - `member_fact.snapshot_flat_to_flat_t` / `snapshot_flat_to_continuous`：把 Core(T)
     `first_pyramid_flat`（`fp_*` 键，由 `flatten_first_pyramid` 产出，与 Board producer
     同源）映射到 `previous_state_to_flat` / `state_to_continuous` 的同一输出键空间。
   - `_build_member_observations`：当 Core flat 存在（daily current 路径）时，`flat_t` /
     `continuous` 改由 Core flat 构建；否则回退到 History（历史 replay / union 路径，
     此时 Core(T) 不是观测目标）。**零核重算**——只读取已物化的 Core flat。
   - 因此 Review Current(T) 与 History(T) 是否存在无关（I1/I2）；History(T) 消费次数 = 0（KPI-2）。
2. **编排真正解耦**（KPI-4）：`computing_history`（`_make_history_step` →
   `advance_history_to_trade_date` 的第二次计算）从 Review **之前**移到 **之后**执行。
   从 `stock_core published` 到 `Review compute started` 之间不再有任何 History(T) producer /
   recompute。Legacy v2 函数保留给 backfill，不删除；v3 物化仍隔离（不产生 v3 DB write）。
3. **PG 测试纠正**（KPI-6）：`test_slice1_current_facts_lock.py` 原用 `sqlite+aiosqlite` 自创建，
   违反仓库测试规则。本轮改为复用 conftest `TestAsyncSessionLocal`（由 `PANJI_REMOTE_VERIFY_DB_TEST`
   指向 `bz_stock_verify_<sha>`），并断言 `current_database()` 以 `bz_stock_verify_` 开头且 ≠ `bz_stock`。

### 明确不变量
- I3：Review 只消费 `source_core_run_id` 对应 exact-T Core；禁止 latest / same-day other-run fallback
  （Core flat 缺失 → 对应 Current fact 为 None，不是 History 兜底）。
- I4：stock_core published → Review compute 之间 0 次 History(T) producer / recompute path。
- I5：`stock_core` 原子发布代码零业务修改（KPI-6/范围保护）。

### 测试
- `test_slice1_review_current_owner.py`（PURE，2 passed）：gate 行为
  - KPI-4：History(T) 缺失 + Core 已发布 → Review 进入 compute/publish，绝不 `HISTORY_NOT_READY_T`；
  - KPI-6：Core 未发布 → Review 不计算（skipped），reason ≠ `HISTORY_NOT_READY_T`。
- `test_slice1_core_current_owner.py`（PURE，7 passed）：Current(T) 归 Core(T)
  - KPI-1：Core flat 相同、History(T) 有/无 → Current 结果一致（含 `_build_member_observations`
    行为测试：冲突 History state 不污染 Current 的 trend/momentum/internal）；
  - KPI-2：`snapshot_flat_to_flat_t` / `snapshot_flat_to_continuous` 只读 Core flat，不读
    History `state_payload` 原始键；缺失 Core 键 → None，无 fallback；
  - KPI-3 精神：same-day 错误 run / 缺失 run → fail-closed。
- `test_slice1_current_facts_lock.py`（@pytest.mark.postgres，verify DB）：KPI-3/6
  - 断言 `current_database()` 为 `bz_stock_verify_<sha>` 且 ≠ `bz_stock`；
  - same-day 两 run → 只消费 `source_core_run_id` 快照；错误 run → 空（fail-closed）。
- 相关回归 274 passed（postgres loader 测试需 `PANJI_REMOTE_VERIFY_DB_TEST` 跑）。

## 6. Frozen verdicts (AST-FORWARD)

```text
FINITE_VOLUME_CONTRACT = PASS   (np.isfinite 过滤 squeeze 区间 + release volume)
REAL_RELEASE_EVENT_COUNT_0825 = 86
RELEASE_RATIO_JSON_NUMBER_COUNT_0825 = 0
RELEASE_RATIO_0825 = LEGACY_SNAPSHOT_INCOMPLETE_REQUIRES_REPROCESS
  (EXPECTED_RELEASE_RATIO_COUNT 有意不再 forensic 重建；不阻断已修正 producer 合同)
PRODUCTION_DEPLOY = NONE
AFTER_CLOSE_WORKER = STOPPED
```
