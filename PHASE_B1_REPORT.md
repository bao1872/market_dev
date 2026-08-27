# PHASE B1 — Core Rerun Lifecycle Decoupling

> 状态：**SCHEMA_BLOCKER → STOP**（未实现 B1 解耦；保留全部现有 published 保护；交 ChatGPT 决策迁移设计）

## MASTER_GOAL
Review 前后端生产闭环（Core→Review Backend → API → Frontend → Runtime → Deploy → Real Data → Browser Acceptance）。

## CURRENT_PHASE
**B1 — Core Rerun Lifecycle Decoupling**（`published_at` 不得拥有 Core 生命周期决策）

## BASE_SHA
`939600be6174b78a0d75935e0698323b69670cb7`（= PHASE_A_FINAL_SHA，已 ff）

## CHECKPOINT_SHA（B1 审计 checkpoint）
见文末 git 段。`PHASE_B1_CORE_LIFECYCLE_CLOSED = NO`。

---

## CORE_LIFECYCLE_OWNER_MAP

范围：`StockFeatureSnapshotRun`（Core run）+ `StockFeatureSnapshot`（snapshot 行）。
Owner 列：`create / compute / persist / finalize / finish / rerun / resume / consume`。

| operation | current owner | current published_at dependency | target owner (B1 意图) | 能否安全解耦 |
|---|---|---|---|---|
| create | `feature_snapshot_service.create_snapshot_run` → `get_published_full_run` → 抛 `PublishedSnapshotRunExistsError` | **YES**（`published_at IS NOT NULL` 阻止新建 full run） | run-identity / status，非 published_at | 否（见 §6/§8） |
| compute | `compute_for_trade_date` / orchestrator `_compute_features_op` | NO | 不变 | — |
| persist | `upsert_snapshot` ON CONFLICT WHERE（source_run_id IS NULL OR 链接 run 非 succeeded+published） | **YES**（行级覆盖保护以 `published_at` 为判据） | run-identity 隔离 | 否（见 §6/§8） |
| finalize | `finalize_snapshot_run_compute_complete`（item truth） | NO（PHASE A 已解耦；仅 `published_at is not None` 时早退，不阻塞） | 不变 | — |
| finish | `finish_snapshot_run` succeeded 时写 `published_at` | **YES**（写入 published_at） | status only；published_at 改 legacy/可选 | 否（与 persist 保护耦合） |
| rerun | `create_snapshot_run` 保护；orchestrator `computing_features` catch `PublishedSnapshotRunExistsError` → 复用已发布 run（`skipped_already_published=True`） | **YES**（仅当已 published 才阻止 rerun） | 安全同日 rerun 需 schema 隔离 | **否（SCHEMA_BLOCKER）** |
| resume | orchestrator `computing_features` 复用 running run（status=="running"） | NO | 不变 | — |
| consume (Review) | `_validate_core_ready`（`status==succeeded` + trade_date + item truth） | **NO**（PHASE A 已解耦） | 不变 | 已解耦 ✓ |
| consume (watchlist) | `has_succeeded_snapshot_run`（`status==succeeded AND published_at IS NOT NULL AND scope='full'`） | **YES** | `status==succeeded AND scope='full'`（非 published_at） | 否（无 rerun 隔离会读 mixed world） |
| consume (after_close_pipeline) | `after_close_pipeline_service` L228 `published_at.is_not(None)` | **YES** | `status==succeeded` | 否（同上） |

> Review 侧的 Core Ready（`_validate_core_ready`）在 PHASE A 已解耦，本包无需改动。
> 真正卡住 B1 的是 **create / persist / rerun / watchlist-consume** 四条 owner，其解耦都依赖 snapshot 行级隔离。

---

## §6 Schema / Unique Ownership（前置门禁，决定性）

### StockFeatureSnapshot 唯一键
```sql
-- migration 056_stock_feature_snapshots + 模型 stock_feature_snapshot.py:116
uq_feature_snapshot_instrument_date_tf_adj_schema = UNIQUE (
    instrument_id, trade_date,
    primary_timeframe, secondary_timeframe,
    adj, schema_version
)
```

### source_run_id 是否属于 unique ownership
**否。** `source_run_id`（stock_feature_snapshot.py:72）是 **nullable FK（lineage only）**，NOT 唯一键成员。
- 同一 (instrument_id, trade_date, tf, adj, schema_version) 组合 **至多一行**。
- 同日存在 Core A 与 Core B 时：B 写入 snapshot 会命中同一唯一键 → **覆盖 A 的该行**（ON CONFLICT DO UPDATE）。
- 数据库**不能同时保存 Snapshot(A) 与 Snapshot(B)** 而不互相覆盖。

### 唯一覆盖保护现状
`upsert_snapshot`（feature_snapshot_service.py:1837–1845）的 WHERE：
```sql
WHERE stock_feature_snapshots.source_run_id IS NULL
   OR NOT EXISTS (
     SELECT 1 FROM stock_feature_snapshot_runs r
     WHERE r.id = stock_feature_snapshots.source_run_id
       AND r.status = 'succeeded'
       AND r.published_at IS NOT NULL
   )
```
该保护**仅**在现有行的归属 run 为 `succeeded AND published_at IS NOT NULL` 时阻止覆盖。
**PHASE A 之后 Core run 可在 `published_at = NULL` 状态下 succeeded**，因此一个 succeeded-but-unpublished 的 run A **不受此保护**：rerun B 对其已写行计算时，WHERE 求值为 TRUE → B 覆盖 A 的行（更新 source_run_id=B + payload）。

### 历史 NULL 行（迁移证据）
- `source_run_id` 由 **migration 061_snapshot_source_run_id**（2026-07-11）新增，晚于建表 migration 056（2026-07-07）。
- 061 显式 `nullable=True`「兼容历史数据」；其设计说明（L13–17）**自认**：原唯一键在「同日多次 run（full/scoped/retry/force）时会产生归属歧义」，并选择「source_run_id 作为补充关联，不删除原有唯一约束」。
- 故 pre-061 历史行 `source_run_id = NULL`，且任何未填 source_run_id 的行均为 NULL。
- 若要把 `source_run_id` 纳入唯一键，必须：① 把所有 NULL 行回填为真实 run_id，或 ② 改用 partial unique index `(..., source_run_id) WHERE source_run_id IS NOT NULL` 并把 NULL 行删除/回填 —— 均为**历史回写/删除/不可逆重构**。

---

## §8 业务合同裁决（CASE A–E）

| CASE | 合同 | 当前 schema 下结果 | 裁决 |
|---|---|---|---|
| A | A succeeded（published_at=None）→ 允许创建 B | 若解耦 create 保护：可创建 B | 受 §6 阻断 |
| B | B succeeded → Review 只消费 source_core_run_id=B | B 行覆盖 A 行后，按 source_run_id=B 查询仅得 B 计算过的子集；A 未覆盖行为 A 所有 → **B lineage 不完整** | 违反（mixed 读取） |
| C | B 中途失败 → 不得形成 A/B mixed canonical world | B 已覆盖的行 source_run_id=B 且 payload 为 B 半成品；A 未覆盖行仍属 A → **同一 trade_date 快照宇宙既含 A 行又含 B 行** = A/B mixed world | **P0 数据完整性违规** |
| D | running B 不得作 Review source | `_validate_core_ready` 仅认 succeeded → fail-closed | 满足（PHASE A） |
| E | failed B 不得作 Review source | 同上 fail-closed | 满足（PHASE A） |

> 关键：CASE B/C 要求「A 与 B 的 snapshot 宇宙可同时、互不污染地共存」，而当前 schema 唯一键不含 source_run_id，物理上无法保证 → 解耦 produce/persist/rerun 会在 PHASE A 的「succeeded 无 published_at」世界里制造 mixed world。

---

## §7 / §11 / §15 / §19 Schema Gate 裁决

- §7：禁止为启用 rerun 直接删除 `PublishedSnapshotRunExistsError` / `published_at` 覆盖保护 —— **遵守，未删除**。
- §11：若 schema 无法安全支持 A+B coexistence → **STOP B1 implementation expansion，形成 SCHEMA_BLOCKER** —— **触发**。
- §15：需要 drop 唯一约束 / rewrite 历史行 / delete 数据 / 不可逆重构 → **形成远端 checkpoint SHA，STOP，报告 ChatGPT；不得擅自执行破坏性 Migration** —— **触发**。
- §19 STOP 条件 #4「当前 schema 无法满足 rerun isolation」—— **命中**。

### schema change 分类
- **NONE**：不可能（唯一键缺 source_run_id，无法隔离）。
- **ADDITIVE / SAFE**：不足以解决（加列/加索引不改变唯一键语义）。
- **DESTRUCTIVE / RISKY（需要）**：将 source_run_id 纳入唯一键（含 partial unique index + 历史 NULL 行回填/清理）。属不可逆历史重构，**必须经 migration governance 设计 + PG 验证，且不得部署到正式 DB**。

---

## §8 字段填表（终态）

```
snapshot unique key =
  (instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)

source_run_id 是否属于 unique ownership = NO（lineage FK only）

A succeeded → B create = 当前被 published_at 保护阻止；解耦后物理上会覆盖 A 行 → 不安全
A published  → B create = 当前被 PublishedSnapshotRunExistsError 阻止（正确，保留）

B succeeded snapshot ownership = source_run_id=B，但仅覆盖非碰撞行（与 A 互斥唯一键）
B failed   snapshot ownership = B 部分行覆盖 A（mixed world）

mixed A/B = YES（若解耦 rerun 且 schema 不变）→ 禁止
A artifact preserved after B failure = NO（mixed world）→ P0

published_at lifecycle dependencies removed =
  PARTIAL（Review readiness 已解耦；create/persist/rerun/watchlist-consume 仍依赖，且不能安全移除）

running Review consume  = fail-closed（PHASE A _validate_core_ready）
failed  Review consume  = fail-closed（PHASE A _validate_core_ready）

schema change = RISKY（需 source_run_id 纳入唯一键 + 历史回填）
Migration = STOP_REQUIRED（设计/PG 验证，不部署 prod）
```

---

## §16/§18/§21 收口判定

```
PG-B1-A = NOT EXECUTED（blocked by schema）
PG-B1-B = NOT EXECUTED（blocked by schema）
PG-B1-C = NOT EXECUTED（blocked by schema）
PG-B1-D = N/A（PHASE A 已 fail-closed，无需重测）
PG-B1-E = N/A（PHASE A 已 fail-closed，无需重测）
PG-B1-F = NOT EXECUTED（blocked by schema）

Phase A regression = N/A（本 checkpoint 未改动任何 production 代码；PHASE_A_FINAL_SHA 不变）

formal PG = N/A（无代码变更，regression 由 PHASE A targeted-pg 62/0 保证）

PHASE_B1_CORE_LIFECYCLE_CLOSED = NO（SCHEMA_BLOCKER）

NEXT = ChatGPT 决策：是否立项「B1-MIGRATION」将 source_run_id 纳入唯一键
       （partial unique index + 历史 NULL 行回填/清理，经 migration governance 设计 + PG 验证，
       不部署 prod）。批准后再重入 B1 实现解耦 + PG-B1-A..F。
```

---

## 本轮未做任何 production 代码改动（防 false-green / 防 mixed world）

- 未删除 `PublishedSnapshotRunExistsError`。
- 未改动 `upsert_snapshot` 的 `published_at` 覆盖保护。
- 未改动 `finish_snapshot_run` 的 `published_at` 写入。
- 未解耦 `has_succeeded_snapshot_run` / `after_close_pipeline_service` L228（避免读 mixed world）。
- 仅新增本审计/checkpoint 文档。

## Git
- 精确 `git add -- PHASE_B1_REPORT.md`（additive artifact；无 add ./-A/-u）。
- commit + `git fetch origin dev` + ff 检查 + `git push origin dev` → CHECKPOINT_SHA。
- HEAD == origin/dev == CHECKPOINT_SHA（ff-only）。
