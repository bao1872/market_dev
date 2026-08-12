# CHANGE-20260812-003 — Round 1C Canonical Observation Fact Persistence Implementation

- **类型**：behavior+contract+architecture（Canonical Observation Fact 持久化；新表 + ORM + persistence service + shadow wiring）
- **领域**：复盘模块 / Scope Observation Model / Canonical Scope Observation Facts persistence
- **状态**：`implemented_unconfirmed`（本地 pure/unit、ruff、mypy-changed、compileall 通过；**remote PG verification / real-data→isolated persistence smoke 未执行**，见 §4）
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
- **未执行**：remote isolated verification（migration upgrade / targeted PG tests / downgrade roundtrip）、
  real-data→isolated persistence smoke、industry_l2/l3 smoke → 待 frozen SHA push 后 remote verification plane 执行。

## 5. 后续

- push origin/dev 后记录 `REVIEW_OBSERVATION_ROUND1C_SHA`，针对该 SHA 在 isolated verification DB
  执行 migration + targeted PG tests + real-data→isolated persistence smoke + L2/L3 smoke。
- 保持 Map 描述 legacy implementation；待外部审计/验收后单独授权同步 Maps。
