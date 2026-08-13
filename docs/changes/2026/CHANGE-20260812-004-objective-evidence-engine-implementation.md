# CHANGE-20260812-004 — Round 2A Objective Evidence Engine Implementation

- **类型**：behavior+architecture（Objective Evidence Engine；query-time derived，无新表、无 migration）
- **领域**：复盘模块 / Canonical Scope Observation Facts → Objective Evidence（L2-A）
- **状态**：`verified_code_pending_acceptance`（本地 pure/unit 24、L1 74 无回归、ruff、mypy-changed、compileall、governance 均通过；remote clean isolated Concept peer verification 通过：real_concept_generated=389 == DB row=389，peer_count=389，无污染；待用户产品/理论验收后再收口）
- **修正**：CHANGE-20260812-004 于 2026-08-12 经外部审计判定 ROUND 2A = PARTIAL / MINOR CORRECTION REQUIRED，执行最小 correction（§8），不进入 Round 2B
- **修正 SHA**：`REVIEW_OBJECTIVE_EVIDENCE_ROUND2A_CORRECTED_SHA` = `0a8c754835ec9ccb3823bd22c4c4694e49d408d5`（已 push origin/dev）
- **关联 PRD**：`docs/prd/70-review.md`（§7.6 min-sample=60 / §7.9 Canonical Facts；本轮不新增主观产品语义）
- **关联 Maps**：`docs/maps/70-review.md`（未修改；待实现验收后单独授权同步）
- **关联 Rules**：无（本轮不修改治理；AGENTS.md / rules/* / governance checker 均未改动，governance check PASS）
- **No new table / No migration / No cache table / No API / No frontend / No Filter / Discovery / Publication / Round 2B**

## 1. 背景

Round 1C（CHANGE-20260812-003）已 CLOSED：`review_scope_observation_facts` 持久化每日
canonical objective facts。本轮 **Round 2A** 在 L1 facts 之上派生 Objective Evidence：

> “今天的这些客观事实，和自己的过去相比、和今天同类 Scope 相比，
> 处在什么位置、发生了什么变化？”

仍然只允许 objective evidence；禁止 Opportunity / Risk / Strong / Weak / Candidate / Filter /
Discovery / Ranking / Score / Grade / Recommendation。

## 2. 变更内容

### 2.1 新增 pure domain module `backend/app/domain/review/scope_evidence.py`

- `percentile_rank(value, samples) -> float | None`：neutral 0..100 percentile rank；过滤
  None/NaN/inf；empty → None；deterministic ties；无 direction / weight / negative inversion /
  score normalization（prompt §7）。只提取 repo 既有 cross-sectional rank 的纯数学语义
  （`below_or_equal / n * 100`），**不 import legacy `_normalize_component` / P/Q/U/C/V**。
- `PRIMITIVE_PATHS`：6 个 Phase-1 primitive 的显式 path mapping（prompt §10 / §14）：
  `price_return_mean → price.return.mean`、`price_advance_ratio → price.breadth.advance_ratio`、
  `trend_up_ratio → trend.state.up_ratio`、`momentum_expanding_ratio → momentum.state.expanding_ratio`、
  `participation_volume_p50 → participation.volume.p50`、`price_raw_hhi → price.concentration.raw_hhi`。
- `extract_primitive(payload, primitive) -> float | None`：只接受 finite int/float；**bool 拒绝**
  （prompt §11）；None/NaN/inf → None（不 NULL→0）。
- `compute_delta(current, reference)`：`current - reference` 原生单位，无 improving/deteriorating 标签。
- context builders：`build_current_context` / `build_delta_context` / `build_historical_context`
  / `build_peer_context`，每个 context 独立 status（ready / unavailable / insufficient_history），
  无 overall Evidence Score / overall readiness。
- `HISTORICAL_MIN_SAMPLE = 60`（PRD §7.6）：历史有效样本 < 60 → `insufficient_history`，percentile=None，
  不得用 peer percentile 替代。
- `RAW_HHI_PEER_DISABLED_REASON = "raw_hhi_not_cross_scope_comparable"`：raw HHI 禁 peer position
  （prompt §15）。

### 2.2 新增薄 service `backend/app/services/scope_evidence_service.py`

- `compute_scope_evidence(session, trade_date, scope_type, scope_key) -> dict`。
- 职责仅：读 L1 fact（`get_scope_observation_fact`）；`_nth_previous_trading_day` 复用
  `calendar_service.get_previous_trading_day_async` 迭代解析 exact D1/D3/D5（**禁止**
  nearest/latest/calendar-day subtraction/fallback）；读历史 facts（same scope, trade_date < T）；
  读 same-family peer facts（same trade_date, same scope_type）；调 pure calc；返回 dict。
- peer cohort：`concept/industry_l1/industry_l2/industry_l3` same-family only；market → no peer
  （prompt §14）。不做 peer registry。
- **不写回 L1 facts，不读 legacy p/q/u/c/v payload**（prompt §2 / §4）。
- 输出 shape 按 prompt §19（scope / trade_date / primitives{ primitive: { current, d1, d3, d5,
  historical, peer } }），每 context 独立 status。

## 3. 不改动 Round 1C / L1 Core

- 未修改 `scope_observation.py`（不改 `_percentile` quantile-value helper，prompt §6）。
- 未修改 `review_observation_persistence_service.py` / `review_observation_prep_service.py` /
  `observation_prep.py` / `market_review.py` / migration。
- 未新增 Evidence 表 / migration / cache（prompt §3，NO NEW TABLE）。
- 未实现 Filter / threshold / Candidate / Opportunity / Risk / Discovery / Ranking / Score /
  Tracking / Publication / API / Frontend（prompt §29）。

## 4. 本地验证

- 新增 `tests/test_review_scope_evidence.py`（22 tests，prompt §20 A–R）：percentile_rank basic/
  ties/NaN-inf、primitive extraction、bool rejection、delta、D1/D3/D5 exact、missing→unavailable
  no fallback、historical <60 / >=60、current excluded、same-family peer、cross-family isolation、
  raw HHI peer disabled、no subjective keys、input not mutated。
- 新增 `tests/test_review_scope_evidence_pg.py`（targeted PG，prompt §21）：evidence 不写行、
  exact D1/D3/D5 query、missing exact date、peer same-family query、raw HHI peer disabled。
- L1 regression：`test_review_scope_observation.py` / `test_review_observation_prep.py` /
  `test_review_observation_persistence.py` 74 passed（无回归）。
- ruff：All checks passed；compileall OK；mypy modified scope 无新增错误（既有 baseline 在
  未修改文件）；governance check PASS。

## 5. 遗留 / Deferred

- Diffusion 保持 PROVISIONAL：仅 D1/D3/D5 breadth delta 作为客观 Evidence，不新增 Diffusion
  Score/State/Label/persistence（prompt §18）。
- Round 2B（threshold / Filter / Candidate / Opportunity / Discovery / ranking / score）不进入本轮；
  只在路线图标记其输入接口。
- Historical window 不硬编码 120-day product window（prompt §8）：读取该 Scope 当前可获得的所有
  历史 canonical facts，按 min60 门槛输出 `insufficient_history` / `ready`。

## 6. 外部验证（远程 isolated verification + real-data）

在 frozen SHA `3aa82840badf1e1eaecfb24098b8a54ebc29fe4e` 的 isolated verification DB
（`bz_stock_verify_3aa82840...`，跑在既有 `trading-postgres` 容器内，不写 production `bz_stock`）
完成。验证结果全部通过：

- **PG query contract**：`tests/test_review_scope_evidence_pg.py` 4 passed（evidence 不写行、
  exact D1/D3/D5、missing exact date→unavailable no fallback、peer same-family isolation、
  raw HHI peer disabled）。
- **Short window（§25）**：2 concept + 2 industry_l1，T0=2026-08-10。全部 `current_status=ready`，
  D1/D3/D5 `ready`，D1 reference_date=2026-08-07，delta 按原生单位计算（含 0 delta 与正值）。
- **Historical <60（§26）**：真实数据仅 6 个交易日，sample_count=5 → `insufficient_history`，
  percentile=null（真实数据不足以触发 ≥60 分支，如实保留）。
- **Historical ≥60（§26）**：真实数据仅 6 天，无法天然到达 min60；在**隔离验证库**写入 65 个
  synthetic canonical facts（`concept/synth_min60_*`）演示 ≥60 分支：`historical_status=ready`，
  sample_count=65，percentile=56.92。真实 ≥60 为 `DATA_BLOCKED`（数据不足，非实现缺陷）。
- **Peer cohort（§27）**：concept 389/389 facts generated（full_cohort_verified=true，
  peer_count=390，percentile=82.56）；industry_l1 257/257（peer_count=257，percentile=68.87）。
  仅 same-family 入 cohort，market 无 peer cohort。

**数据可用性记录**：`review_scope_observation_facts` 在真实 `bz_stock` 中不存在（L1 facts 仅写入
验证库）；短期真实数据受 PIT membership 可用性限制（08-03..08-10 共 6 个交易日）。CHANGE 状态保持
`verified_code_pending_acceptance`，待用户做产品/理论验收。

## 7. 清理

验证结束已删除本轮创建的 verify 库与临时文件（见 §8 清理执行）。未触碰 production `bz_stock`、
共享 PostgreSQL/Redis 卷、稳定运行容器或受保护镜像。

## 8. ROUND 2A CORRECTION（2026-08-12，外部审计后最小修正）

外部审计结论：ROUND 2A = PARTIAL / MINOR CORRECTION REQUIRED。本修正为最小 correction，
**不进入 Round 2B**，不改 PRD / 不新增 table / 不新增 migration / 不改 L1 / 不增加 primitive /
不改 percentile math / 不改 D1/D3/D5 contract / 不改 peer family contract / 不做
Filter / Candidate / Discovery / 不做新架构设计。

### 8.1 Historical status precedence 修正

原 `build_historical_context()` 在「current value unavailable + history sample < 60」时返回
`insufficient_history`，语义不准确。修正为明确优先级（不新增 status）：

- **A**：current value is None → `status=unavailable`，`percentile=None`；保留 `sample_count` /
  `history_start_date` / `history_end_date`。
- **B**：current available 但 `sample_count < 60` → `status=insufficient_history`。
- **C**：current available 且 `sample_count >= 60` → `status=ready`。

只修改 `backend/app/domain/review/scope_evidence.py`（`build_historical_context`）与
`backend/tests/test_review_scope_evidence.py`。`scope_evidence_service.py` 仍直接调用
`build_historical_context`，无需改动（由测试证明）。

### 8.2 新增 pure tests

- current=None + history=5 → `unavailable`
- current=None + history=60 → `unavailable`
- current valid + history=5 → `insufficient_history`（既有 K 覆盖）
- current valid + history=60 → `ready`（既有 L 覆盖）

### 8.3 本地验证（修正后）

- Round 2A pure/unit：`test_review_scope_evidence.py` 24 tests passed（22 + 2 新增）
- Round 1 L1 regression：74 passed 无回归
- ruff：All checks passed；mypy modified scope：Success（2 files）；compileall OK；
  governance check PASS
- 未跑 CI（按本次修正指令 §7，local checks only）

### 8.4 Clean Concept peer verification

上一轮 peer_count=390 而 real concept generated=389，存在额外 row 污染风险，不接受该结果。
本轮在**新建的干净 isolated verification DB** 内重新验证 Concept peer：

- 先输出 `real_concept_generated_count` 与 `DB concept row_count on target date`，两者必须相等；
- 再计算一个真实 Concept Evidence，`peer_count` = 该 primitive 的 finite real peer count
  （不要求等于总 Concept 数，因 primitive 可能 unavailable），并解释 total real rows /
  finite peer rows / target included；
- 排除 synth_min60 / PG test seed / dummy scope / manual fake scope。

**实际 clean Concept peer verification 结果（isolated DB `bz_stock_verify_0a8c754…`，production bz_stock READ ONLY）：**

- `read_current_database` = bz_stock（只读确认）；`write_current_database` =
  `bz_stock_verify_0a8c754835…`（isolated verify DB）
- `discovered_concept_specs` = 389；`real_concept_generated_count` = 389；
  `real_concept_skipped` = 0；`errors` = []（无 invariant / 契约错误）
- `db_concept_row_count_on_target` = 389；`db_concept_keys_count` = 389；
  `generated_matches_db` = **true**（389 == 389）
- 独立 DB 复核：`SELECT scope_type, count(*), count(DISTINCT scope_key) … GROUP BY scope_type`
  → `concept | 389 | 389`
- 污染扫描：0 行匹配 `synth|seed|dummy|fake` 或 `test%`；`contamination_keys` = []；
  `no_synthetic_or_test_rows` = **true**
- 真实 Concept Evidence（target `bc73d2c4-e728-4761-bfdd-7ba1556c988a`，`price_return_mean`）：
  - `current_status` = ready；`peer_status` = ready
  - `peer_count` = **389**（total real rows = 389，finite real peer rows = 389，
    target included；该 primitive 对所有真实 concept 均 finite，故 peer_count == 总 Concept 数）
  - `peer_percentile` = 51.41
  - `historical_status` = `insufficient_history`（`sample_count` = 0：真实库仅 1 个交易日
    的 L1 facts，无 prior 历史样本；真实 >=60 仍 DATA_BLOCKED，见 §8.6）

**结论**：clean isolated verify DB 内 Concept peer 无污染，`real_concept_generated_count ==
DB row_count == 389`，peer_count == 389 == finite real peer count，无 synthetic / test /
dummy / fake row。

### 8.5 Industry L1

上一轮 257/257、peer_count=257 无污染，不重跑整个 Industry cohort（除 clean DB verification
顺手低成本检查外，不为形式重复大实验）。

### 8.6 Historical >=60

真实 >=60 仍 DATA_BLOCKED，保持；不重新制造真实历史。synthetic >=60 unit/PG evidence 已足够
验证代码分支，不再做 65-day replay。

### 8.7 Final

修正后 Commit + push origin/dev，记录 `REVIEW_OBJECTIVE_EVIDENCE_ROUND2A_CORRECTED_SHA`。
最终状态：`ROUND_2A_CORRECTION_READY_FOR_EXTERNAL_AUDIT`，然后 STOP。
