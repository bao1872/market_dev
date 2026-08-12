# CHANGE-20260812-003 — Round 1C Canonical Observation Fact Persistence Implementation

- **类型**：behavior+contract+architecture（Canonical Observation Fact 持久化；新表 + ORM + persistence service + shadow wiring）
- **领域**：复盘模块 / Scope Observation Model / Canonical Scope Observation Facts persistence
- **状态**：`implemented_unconfirmed`（本地 pure/unit、ruff、mypy-changed、compileall 通过；remote isolated verification + real-data→isolated persistence smoke **已执行通过**，见 §4；外部审计提出 2 个 blocker，已按 §6 修正，仍待外部复审/验收）
- **关联 PRD**：`docs/prd/70-review.md`（§7.9 Canonical Scope Observation Facts — Exploration Persistence Contract；由 CHANGE-20260812-002 收口）
- **关联 Maps**：`docs/maps/70-review.md`（未修改；Map 继续描述 legacy implementation，待实现验收后单独授权同步）
- **关联 Rules**：无（本轮不修改治理；AGENTS.md / rules/10* / rules/20* / governance checker 均未改动，governance check PASS）
- **No consumer switch / No API / No frontend / No Filter / Discovery / Publication redesign**

## 1. 背景

Round 1A（Canonical Scope Observation Core）与 Round 1B（Real-Data Shadow Verification，external
verdict = PASS）已验证真实数据能按 PRD 计算客观观察事实；Round 1C-0（CHANGE-20260812-002）已收口
Exploration persistence contract（PRD §7.9）。本轮为 **Round 1C implementation**：把 Round 1A/1B
已验证的 Canonical Scope Observation 持久化为

`trade_date + scope_type + scope_key → one daily objective fact snapshot`。

## 2. 变更内容

### 2.1 新表 `review_scope_observation_facts`（Migration `090_scope_observation_facts`）

- 业务 grain 锁死为 `(trade_date, scope_type, scope_key)`，唯一约束
  `uq_review_scope_observation_facts_day_scope`（prompt §3）。
- 最小字段集（id / trade_date / scope_type / scope_key / scope_name / canonical_t1 /
  pit_member_count / pit_member_count_t1 / provided_member_count / t1_membership_available /
  pit_status_t / pit_status_t1 / readiness / observation_payload(JSONB) / diagnostics(JSONB) /
  algorithm_version / created_at / updated_at）。
- 不增加 review_run FK / publication FK / revision_id / version_id / active pointer /
  revision chain / generation table / history table（prompt §4）。
- 纯新增表，不修改历史 migration；downgrade 仅 drop 该表（prompt §21）。

### 2.2 ORM `ReviewScopeObservationFact`（`app/models/market_review.py`）

- 最小 ORM 与迁移对齐；不修改 `MarketReviewScopeSnapshot` / P/Q/U/C/V ORM 语义，legacy model
  不依赖新表（prompt §6）。

### 2.3 Persistence service（`app/services/review_observation_persistence_service.py`）

- `save_scope_observation_fact` / `get_scope_observation_fact` / `list_scope_observation_facts`。
- `save` 使用 PostgreSQL `INSERT ... ON CONFLICT (trade_date, scope_type, scope_key) DO UPDATE`，
  幂等 upsert：第一次 row_count=1，第二次同 grain 不同 payload 仍 row_count=1 且 payload 更新
  （prompt §12）。
- save 输入直接包含 `PreparedScope` metadata + Core observation result；**不重新计算任何事实**
  （不重算 ratio / HHI / transition / percentile / 不重新解释 NULL / 不把 unavailable 转 0 /
  不生成 score）（prompt §11）。
- snapshot-level readiness 仅由现有明确状态导出（unavailable / no_members / ready），**无主观
  coverage threshold**（prompt §20）。
- 失败语义：PIT(T) unavailable 或 no members → 不写 row、抛 ValueError（不写假 `{}`）；
  Core 正常返回但某些 axis unavailable → 原样保存（prompt §19）。

### 2.4 Scope activation（prompt §15/§16/§17）

- `ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES = {industry_l1, industry_l2, industry_l3, concept}`。
- Market / major_index / style 在 persistence 层被 `ScopePersistenceNotActivatedError` 阻断
  （即使 generic loop 传入也阻止）——与 prep 层 guard 形成双保险。

### 2.5 Shadow write point（`app/services/review_observation_shadow.py`）

- 已沿 `prepare_scope → compute_scope_observation → save_scope_observation_fact` 接线（prompt §14）。
- `run_shadow_scope(..., write_session=None)`：默认不持久化（shadow evidence only，绝不写 production）；
  仅当显式传入 isolated verification DB 的 `write_session` 时才 persist（prompt §30）。
- 未接 legacy Review publication / Filter / Discovery / API / Frontend（prompt §14/§32）。

## 3. 测试

### 3.1 新增 pure/unit（`tests/test_review_observation_persistence.py`）

- activation set 精确性；market / major_index / style excluded；market 即使被 generic loop 传入仍 blocked；
- PIT unavailable / no members 不进入 save path；
- `_build_fact_values` 不修改 Core output（同一对象引用原样保存）；
- partial facts 可保存（readiness 保持 ready，无阈值降级）；readiness 映射。

### 3.2 新增 PostgreSQL targeted（`tests/test_review_observation_persistence_pg.py`，`@pytest.mark.postgres`）

- insert（1 row）；idempotent update（row_count=1，payload 更新）；
- date / scope / family isolation；diagnostics+readiness round-trip；legacy
  `market_review_scope_snapshots` 零写入；`list` 过滤。

## 4. 验证状态

- 本地（Mac）：compileall OK；ruff OK；mypy-changed 无新增错误（既有 baseline 错误在未修改文件）；
  governance check PASS；Round 1A/1B/1C pure unit `60 passed`（PG tests 在纯单元模式自动跳过）。
- **Remote isolated verification（frozen SHA `55ef285c57a996795401e7c0141911d75639d2f1`，isolated DB `bz_stock_verify_<SHA>`）**：
  - migration `upgrade head` → 达到 `090_scope_observation_facts (head)`，新表 schema + 唯一约束
    `uq_review_scope_observation_facts_day_scope` 全部就位；
  - migration `downgrade -1` → 表 drop（`to_regclass` 为空），再 `upgrade head` → 表重建，round-trip PASS；
  - targeted PG persistence tests `tests/test_review_observation_persistence_pg.py`：**8 passed**（insert /
    idempotent update row_count=1 / date / scope / family isolation / diagnostics+readiness round-trip /
    legacy `market_review_scope_snapshots` 零写入 / list filters）；
  - **real-data→isolated persistence smoke**（真实 bz_stock READ-ONLY → prep/Core → 写入 isolated verify DB）：
    `industry_l1/6d7bff29...`（pit=5, provided=5）与 `concept/bc73d2c4...`（pit=99, provided=99），
    sanity all_pass、persisted、read-back 成功，observation_payload 顶层键 = scope/price/amount/trend/
    structure/momentum/participation/chip；重复运行 row_count 保持 2（幂等）；
    `industry_l2` / `industry_l3` 真实 PIT scope 当前不存在 → **NOT_OBSERVED**（未伪造）；
  - legacy isolation：`market_review_scope_snapshots` count=0，未触发 Filter/Discovery/Publication；
  - **未写 production `bz_stock`**（仅 READ-ONLY 作为输入事实；写路径全部进入 isolated verify DB）；
  - 验证库与 /tmp 临时脚本/证据已按 §8 清理。
- **已知 pre-existing baseline failure（与本轮无关）**：标准 `targeted-pg` plan 的
  `test_pg_review_runtime_blocker_closure.py::test_query2_projected_result_supports_build_stock_state`
  调用 `build_stock_state(snapshot, symbol)`，而当前签名已是 `build_stock_state(snapshot, run, symbol)`，
  导致该 closure 测试失败。该测试文件与 `stock_state.py` 的最近改动 commit 均早于本提交（`538bc95` /
  `9c651a6`），本轮 commit `55ef285` 未触碰这两个文件 —— 属于既有 baseline 失败，非本轮回归。
  此为本轮唯一未达绿的 remote gate，且与 Round 1C persistence 无调用链关系。

## 5. 后续

- 已记录 `REVIEW_OBSERVATION_ROUND1C_SHA = 55ef285c57a996795401e7c0141911d75639d2f1`（origin/dev）。
- 保持 Map 描述 legacy implementation；待外部审计/验收后单独授权同步 Maps。

## 6. Round 1C Correction（外部审计 2 个 blocker）

外部审计对 Round 1C 给出 `PARTIAL / CORRECTION REQUIRED`，提出 2 个 blocker。本修正只补这两点，
**不改变已确认的业务语义 / schema / 激活范围 / 幂等 grain**，仍不触发 Filter / Discovery /
Publication / API / Frontend：

### 6.1 Blocker #1 — Canonical payload contract 校验缺失（§6-A..I）

- `app/services/review_observation_persistence_service.py` 新增
  `CANONICAL_TOP_LEVEL_SECTIONS`（唯一合法顶层集合 = scope/price/amount/trend/structure/
  momentum/participation/chip）与 `validate_scope_observation_payload()`：
  - 顶层键集合必须**精确等于** canonical 集合（缺 canonical 段或含任何额外主观键如
    opportunity_score / marker 都拒绝）；
  - 每个 canonical 段必须是 dict；
  - `scope` 段身份（scope_type / scope_key / trade_date）必须与 PreparedScope 一致。
- `save_scope_observation_fact` 在写库前调用该校验器，非法 payload 抛
  `ScopeObservationPayloadValidationError`，**不写任何行**（Blocker #1/#3）。
- 合法 partial axis（如空 price universe、某 axis unavailable）保留完整 canonical 结构即通过——
  只校验合同形状，不重算任何事实（save-only ownership，prompt §4/§11）。

### 6.2 Blocker #2 — invariant 失败不得持久化（§8）

- `app/services/review_observation_shadow.py` 的 `run_shadow_scope` 在
  `check_observation_invariants(obs)` 返回任一失败时，若提供了 `write_session` 则
  **fail-fast 抛 `ScopeObservationInvariantError`**，绝不调用 `save_scope_observation_fact`
  （`sanity_all_pass == False` → 拒绝持久化，无论 axis 是否 partial）。

### 6.3 测试补强

- `tests/test_review_observation_persistence.py` 新增 validator A..I（全 canonical PASS /
  缺 price / 缺 trend / arbitrary payload / 额外主观键 / scope_type / scope_key / trade_date
  不匹配 / 合法 partial PASS / 非 dict 段），以及
  `test_shadow_invariant_fail_does_not_persist`（invariant 失败时 shadow 写路径不触达 DB）。
- `tests/test_review_observation_persistence_pg.py`：
  - 所有 payload fixture 改用真实 `compute_scope_observation` 输出（合法 canonical shape）；
  - 新增 `test_save_rejects_non_canonical_payload`、`test_save_rejects_scope_identity_mismatch`、
    `test_legal_partial_payload_persists`；
  - 新增 `test_seeded_legacy_pqucv_unchanged`（§9）：预置 legacy `market_review_scope_snapshots`
    的 P/Q/U/C/V，保存 canonical fact 后重读 legacy 各行 P/Q/U/C/V 完全不变。

### 6.4 验证状态（本次修正）

- 本地（Mac）：compileall OK；ruff OK；mypy-changed 无新增（既有 baseline 错误在未修改文件）；
  governance check PASS；Round 1A/1B/1C pure unit 全绿（74 passed）。
- **Registered targeted-pg（代码 SHA `4105a2b`）**：preflight / create_database / migration（
  upgrade head → `090_scope_observation_facts (head)`）/ identity 全 PASS；`pg_tests` gate 仅因
  **既有 baseline failure**（`test_pg_review_runtime_blocker_closure.py::test_query2_...
  build_stock_state` 签名不匹配，§4 已记录，非本轮回归）失败；registered plan 的固定
  pg_contract 文件列表不含本修正新增的 `test_review_observation_persistence_pg.py`。
- **Manual isolated verification（最终 SHA `9c319fd4fafd2eb9dd24bfdf977f2dad35e9ca90`，isolated DB
  `bz_stock_verify_9c319fd...`）**：
  - migration `upgrade head` → `090_scope_observation_facts (head)`；
  - identity（`current_database` == 验证库，非 `bz_stock`）通过；
  - `tests/test_review_observation_persistence_pg.py` **12 passed**：insert / idempotent
    update（row_count=1）/ date / scope / family isolation / diagnostics+readiness round-trip /
    legacy `market_review_scope_snapshots` 零写入 / save 拒绝非 canonical payload / save 拒绝
    scope identity 不匹配 / legal partial payload persists / **seeded legacy P/Q/U/C/V 保存后不变** /
    list filters；
  - 两次 manual attempt 创建的验证库（`bz_stock_verify_4105a2b...`、`bz_stock_verify_9c319fd...`）
    与 attempt.env / RUNTIME_SHA 已按 §8 清理。
- `REVIEW_OBSERVATION_ROUND1C_CORRECTED_SHA`（origin/dev）= `9c319fd4fafd2eb9dd24bfdf977f2dad35e9ca90`。

### 6.5 Parent targeted-pg baseline confirmation（prompt §10，BASELINE_CONFIRMED）

- 通过唯一正式入口 `scripts/ops/panji-verify run --sha 1490d60... --plan targeted-pg`
  在 **parent SHA `1490d60332d89e9ae885b3bf209aec31d066c085`** 上实际运行相同 standard
  targeted-pg plan，而非仅依据"文件未修改"推断 pre-existing。
- 结果：preflight / create_database / migration（upgrade head → `089_review_discovery_tracking (head)`）/
  identity 全 PASS；`pg_tests` gate **fail**，唯一失败为
  `test_pg_review_runtime_blocker_closure.py::test_query2_projected_result_supports_build_stock_state`，
  root cause 与当前 dev 完全一致：
  `TypeError: build_stock_state() missing 1 required positional argument: 'symbol'`
  （测试仍按旧签名 `build_stock_state(snapshot, symbol)` 调用，而当前签名已是
  `build_stock_state(snapshot, run, symbol)`）。**1 failed, 17 passed, 5 deselected**。
- 结论：**BASELINE_CONFIRMED** —— 该失败在 parent `1490d60` 即以同一 root cause 存在，
  与 Round 1C 修正无调用链关系，非本轮回归；Round 1C 未触碰该 closure 测试或
  `stock_state.py`。
- 证据保留于远程 `/root/.panji-verify/evidence/verify-1490d60332d8-1786578044-0320331d/`
  （gates.json / logs.txt / summary.md）；验证库 `bz_stock_verify_1490d60...` 已按 §8 清理
  （cleanup.json dropped=true，无 blocked）。
