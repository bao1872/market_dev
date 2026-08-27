# PHASE B1.1 — Schema + Reader Architecture Design

> MASTER GOAL: Review 前后端生产闭环
> CURRENT_PHASE: B1 — Core Rerun Lifecycle Decoupling (B1.1 = Schema + Reader Architecture Design)
> BASE_SHA: `939600be6174b78a0d75935e0698323b69670cb7`
> PRIOR_CHECKPOINT_SHA: `d350236c5b935fade8c7301170413a2020991101` (B1 schema-blocker checkpoint)
> THIS_SHA (design only):见 §11

---

## 0. 状态与结论

B1 上一轮（checkpoint `d350236c`）因 `stock_feature_snapshots` 唯一键不含 `source_run_id` 而判定
**SCHEMA_BLOCKER / STOP**。本轮 B1.1 重新审计后结论：

**该 blocker 是「可解」的，且解是 ADDITIVE / SAFE（非破坏性），可以按 §15 governance + PG 验证推进到 B1.2 实现。**

根因不变：唯一键 `(instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)`
不含 `source_run_id`，且 `upsert_snapshot` 的行级覆盖保护以 `published_at IS NOT NULL` 为判据——
PHASE A 之后 Core 可在 `published_at=None` 下 `succeeded`，故 succeeded-but-unpublished run A 不受保护，
同日 rerun B 会物理覆盖 A 的 snapshot 行 → A/B mixed world（§8 CASE C，P0）。

解法的两个支柱（本设计详述）：
1. **Schema**：DROP 旧全键唯一约束，ADD **partial unique index** 把 `source_run_id` 纳入归属
   （`WHERE source_run_id IS NOT NULL`）。A/B 因 `source_run_id` 不同而共存，互不覆盖。
2. **Reader**：所有非 Review 的 snapshot 读取路径必须「先解析 canonical run → 再按 `source_run_id`
   精确读取」，杜绝 trade_date-only 读取在 rerun 后返回 A+B 混合行。

**重要更正（相对 B1 checkpoint 报告）**：REVIEW 读取路径（`review_scope_service.py:495/885`、
`review_observation_prep_service.py:434`）**已经**按 `source_core_run_id` 精确过滤，Phase A 的 Review
隔离在 B1 下仍然成立，Review 不是 mixed-world 风险点。真正的爆破半径见 §3.3。

---

## 1. Schema 设计（B1.2 目标 DDL）

### 1.1 当前
- `stock_feature_snapshots` 唯一键：`uq_feature_snapshot_instrument_date_tf_adj_schema`
  = `(instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)`，
  **不含 `source_run_id`**（model `stock_feature_snapshot.py:117-125`）。
- `source_run_id` 为 nullable lineage FK（`stock_feature_snapshot.py:72-77`），仅 `ix_feature_snapshot_run_instrument` 索引。
- `upsert_snapshot`（`feature_snapshot_service.py:1834-1846`）`ON CONFLICT` 命中上述全键约束，
  `WHERE` 子句以 `published_at IS NOT NULL` 为覆盖保护判据。

### 1.2 目标（partial unique index）
```sql
-- 1) 删除旧全键唯一约束（不再约束 source_run_id）
ALTER TABLE stock_feature_snapshots
  DROP CONSTRAINT uq_feature_snapshot_instrument_date_tf_adj_schema;

-- 2) 新增归属隔离唯一索引（仅对带 source_run_id 的行生效）
CREATE UNIQUE INDEX uq_feature_snapshot_run_isolated
  ON stock_feature_snapshots (
    instrument_id,
    trade_date,
    primary_timeframe,
    secondary_timeframe,
    adj,
    schema_version,
    source_run_id
  )
  WHERE source_run_id IS NOT NULL;
```
- **A/B 共存证明**：run A 写 `source_run_id=A`、run B 写 `source_run_id=B`，二者 `(base, source_run_id)`
  不同 → partial index 不冲突 → 同时 INSERT，互不覆盖。✓
- **历史 NULL 行**：pre-061 行 `source_run_id=NULL`，被 `WHERE source_run_id IS NOT NULL` 豁免 →
  不受唯一约束 → 保持可读（经 legacy `source_run_id IS NULL` 回退，见 §3.2）。✓
- **新 run B vs 旧 NULL run A（同 base）**：旧约束已 DROP，partial index 豁免 NULL A → B INSERT 成功，
  A(NULL) 保留 → 共存。✓

### 1.3 Migration 分类（§15）
- **分类：ADDITIVE / SAFE（但触碰约束，需 governance + PG 验证）**。
- **非破坏性**：不删行、不重写历史行、不不可逆重构。仅「约束替换 + 索引新增」。
- **强制应用不变量**（B1.2 落地护栏）：**所有 `StockFeatureSnapshot` 写入必须设置 `source_run_id`**
  （run 必然已知自身 id）。任何遗漏 `source_run_id` 的写入会在同一 base 下产生两条 NULL 行，
  破坏 legacy 回退的「每 base 唯一」假设 → 必须在 B1.2 审计所有 writer（仅 `upsert_snapshot` 一条路径，
  见 §4.1，已确认它写入 `source_run_id=snapshot.source_run_id`）。
- **部署纪律**：本 migration 不得直接部署到正式 DB；须经 migration governance 评审 + `targeted-pg`
  / `migration-roundtrip` 验证（B1.3）后方可排期。符合 §15。

### 1.4 upsert ON CONFLICT 目标
`upsert_snapshot`（`feature_snapshot_service.py:1834`）改为：
- `on_conflict_do_update(constraint="uq_feature_snapshot_run_isolated", set_=update_cols)`。
- **`WHERE` 子句简化**：因隔离已结构性由 partial index 保证（同 run 重 upsert 才冲突，跨 run 不冲突），
  原 `published_at` 覆盖保护可移除；仅保留「同 run 内始终可更新」语义。跨 run 不会被覆盖。
- 行为：B 重跑 B 自身行 → 更新；B 写 A 的 base（不同 source_run_id）→ 新行，不碰 A。✓

---

## 2. Reader 架构（B1.2 核心）

### 2.1 Canonical run 解析（新增 owner）
新增 `get_canonical_snapshot_run(db, trade_date, *, scope="full", schema_version=_SCHEMA_VERSION)`
→ 返回「当前可读」的单一 run：
- 筛选：`trade_date`, `schema_version`, `status == succeeded`, `metadata_['scope'] == scope`。
- **排序**：`finished_at DESC`（最新 succeeded 优先；rerun B 成功后取代 A 成为 canonical）。
- **不再要求 `published_at IS NOT NULL`**（§9/§13 解耦）。
- legacy（pre-061，所有 run `source_run_id=NULL`）回退：取 `published_at IS NOT NULL` 的最新 run（兼容现有 watchlist 语义直到 B3 退役 publication）。

### 2.2 标准读取模式（modeled on `api/stock_context.py:223-253`）
每个 snapshot 读取点统一为两段式：
```python
# 1) 精确：source_run_id == canonical_run.id（或调用方已有的显式 run id）
stmt = select(StockFeatureSnapshot).where(
    StockFeatureSnapshot.instrument_id == iid,
    StockFeatureSnapshot.source_run_id == canonical_run.id,
)
# 2) Legacy 回退（仅当精确查无结果且 canonical_run.source_run_id IS NULL）：
#    WHERE base-key AND source_run_id IS NULL
```
- 调用方若已持有显式 `run_id`（如 Review 持有 `source_core_run_id`、market_stocks 持有 `snapshot_run_id`），
  **直接用该 id**，不经 canonical 解析（更精确、零歧义）。
- 调用方仅持有 `trade_date`（如 watchlist monitor-status），**先 `get_canonical_snapshot_run` 解析**，再精确读。

### 2.3 读取点清单（爆破半径 + 改造要求）

| # | 文件:行 | 当前过滤 | mixed-world 风险 | B1.2 改造 |
|---|---|---|---|---|
| R1 | `api/watchlist.py:371-379` | instrument_id IN + trade_date + schema_version（**无 source_run_id**） | **高**（用户面） | 先 `get_canonical_snapshot_run`，再 `source_run_id == canonical.id`；legacy NULL 回退 |
| R2 | `api/stock_context.py:223-253` | `source_run_id == run.id` 精确 + legacy 回退 | 低（已正确） | 验收一致即可；legacy 回退保留 |
| R3 | `api/stock_context.py:515-521` | trade_date DESC limit(1)（无 source_run_id） | 中 | 解析 canonical 后精确读 |
| R4 | `market_stocks_service.py:478-501`（lateral） | `source_run_id == snapshot_run_id` 当给定；否则 `trade_date DESC limit(1)` | 中（legacy 分支） | 无 `snapshot_run_id` 时改走 canonical 解析 |
| R5 | `market_stocks_service.py:280-281,383-384` | instrument join + trade_date DESC | 中 | canonical 精确读 |
| R6 | `market_stocks_service.py:1009-1022`（window） | partition instrument, trade_date DESC | 中 | canonical 精确读 |
| R7 | `state_event_service.py:282-377` | max(trade_date) subquery + instrument | 中（跨日） | 每 base date 解析 canonical；legacy NULL 回退 |
| R8 | `review_scope_service.py:495,885` | **`source_run_id == source_core_run_id`** | **无**（已隔离） | 不变（Phase A 已正确） |
| R9 | `review_observation_prep_service.py:434` | **`source_run_id == source_core_run_id`** | **无**（已隔离） | 不变 |
| R10 | `core_artifact_repository.py:47` | trade_date + order_by instrument | 中 | canonical 精确读 |
| R11 | `granular_restart_service.py:556,690` | trade_date / instrument_id | 低 | canonical 精确读 |
| R12 | `product_readiness_service.py:1596-1615` | trade_date count | 低 | 绑定 canonical run id 计数 |
| R13 | `auction_anchor_service.py:935-937` | trade_date | 中 | canonical 精确读 |
| R14 | `after_close_orchestrator.py:2269-2275`（history 物化） | trade_date（SELECT source_run_id 列，遍历所有行） | 中 | 仅物化 canonical run 的行（`WHERE source_run_id == canonical.id`） |
| R15 | `after_close_orchestrator.py:5269-5271` | **`source_run_id == snap_id`** | **无**（已正确） | 不变 |

**结论**：R1/R3/R4/R5/R6/R7/R10/R11/R12/R13/R14 共 11 处需在 B1.2 改造；R2/R8/R9/R15 已正确（验收即可）。
Review 隔离（R8/R9）已成立，Phase A 不变量延续到 B1。

---

## 3. Writer / Lifecycle 解耦（B1.2）

### 3.1 `upsert_snapshot`（§4.1，已述）
`ON CONFLICT` 命中 `uq_feature_snapshot_run_isolated`；`WHERE` 移除 `published_at` 判据。

### 3.2 `create_snapshot_run`（§8 CASE A / §11）
- 当前：`scope='full'` 且 `get_published_full_run`（succeeded+**published**+full）存在 →
  抛 `PublishedSnapshotRunExistsError`（`feature_snapshot_service.py:2182-2198`）。
- 目标：A succeeded（`published_at=None`）**必须允许**创建 B（§8 CASE A）。解耦为：
  - 保留「已有 `running` run 则幂等复用」（`L2164-2180`，不变）。
  - **移除 `published_at` 门控**：不再因 A 已 published / 曾 published 拒绝 B。
  - 仍阻止「同 base 已有 `running` 但未被复用」的并发创建（由 run 表 partial unique index 保证；
    当前 run 表已是 `status='running'` 部分唯一，见 `create_snapshot_run` docstring L2129-2131）。
  - 不再抛 `PublishedSnapshotRunExistsError`（或重命名为 `RunningSnapshotRunExistsError` 仅拦 running）。
- 语义：rerun 合法；正确性由 §1.2 写隔离 + §2 读隔离保证，而非「拒绝创建」。

### 3.3 `finish_snapshot_run`（§9/§13）
- 当前：`status==succeeded` 时写 `published_at`（`feature_snapshot_service.py:2345-2347`）。
- 目标：`published_at` **不再作为任何 Core/Readiness 门控**（§9/§13）。
- B1 决定：**保留写 `published_at`**（§14 禁止在 B1 删除 publication subsystem；B3 才退役）。
  仅确保「无 reader 再以其为可读判据」——由 §2.3 读改造落实。最小爆破半径。

### 3.4 `has_succeeded_snapshot_run` → `has_canonical_snapshot_run`（watchlist gate）
- 当前：`succeeded AND published_at IS NOT NULL AND scope='full'`（`feature_snapshot_service.py:2456-2468`）。
- 目标：`succeeded AND scope='full'`，**去掉 `published_at`**；语义等价于「存在 canonical run」。
- `after_close_pipeline_service.py:703` 调用点随之改名；`_get_snapshot_run_summary`（`L225-259`）
  `WHERE published_at.is_not(None)` 一并去掉（与 `has_canonical_snapshot_run` 一致）。

### 3.5 `finalize_snapshot_run_compute_complete`（compute 终态）
- 不变：基于 run-items item-truth 判 `succeeded/failed/running`（`feature_snapshot_service.py:2363-2429`）。
- 其 `published_at is not None` early-return（`L2393`）保留为「已发布 run 不回退」无害守卫。

---

## 4. 更新后的 CORE_LIFECYCLE_OWNER_MAP

| operation | current owner | current published_at dependency | target owner (B1.2) |
|---|---|---|---|
| create | `create_snapshot_run` → `PublishedSnapshotRunExistsError` | YES（拦 published） | `create_snapshot_run` → 仅拦 running（rerun 合法） |
| compute/rerun | orchestrator `computing_features` | 间接（经 create 拦 published） | 直接（create 解耦后 rerun 自由） |
| persist (upsert) | `upsert_snapshot` WHERE `published_at` | YES（覆盖保护判据） | `upsert_snapshot` ON CONFLICT `uq_feature_snapshot_run_isolated`（结构性隔离） |
| finalize (compute terminal) | `finalize_snapshot_run_compute_complete` | NO（item-truth） | 不变 |
| finish (publish sem) | `finish_snapshot_run` 写 `published_at` | YES（写） | 保留写（B3 退役），不再作门控 |
| rerun | 受 create 拦 published 限制 | YES | 解耦（见 create） |
| resume | orchestrator running 复用 | NO | 不变 |
| consume-watchlist | `has_succeeded_snapshot_run` + `api/watchlist.py` trade_date 读 | YES（published_at + 无 source_run_id） | `has_canonical_snapshot_run` + canonical 精确读 |
| consume-Review | `review_scope/review_observation_prep` `source_core_run_id` | **NO**（已隔离） | 不变 ✓ |
| consume-market_stocks | `market_stocks_service` trade_date/latest | 无 published_at，但无 source_run_id | canonical 精确读 |
| consume-history | `after_close_orchestrator:2269` trade_date 全量 | 无 published_at，但全量遍历 | canonical 精确物化 |

---

## 5. §8 合同裁决（CASE A–F 如何在设计中满足）

- **CASE A**（A succeeded, published_at=None → 允许创建 B）：§3.2 `create_snapshot_run` 移除 published 门控 → B 可建。✓
- **CASE B**（B succeeded → Review 仅消费 B）：R8/R9 已 `source_core_run_id=B` 精确读；A 行 `source_run_id=A` 不被选。✓
- **CASE C**（B 中途 failed → 不形成 A/B mixed）：B 失败行 `source_run_id=B` 但 run=`failed`；canonical =
  最新 `succeeded`（=A 或 none）；R1/R4..R14 走 canonical，绝不读 failed B 的行；A 行 `source_run_id=A` 完好。
  → 无 mixed world。✓
- **CASE D**（B running → Review fail-closed）：canonical 解析排除 `running`；Review `_validate_core_ready`
  已要求 `status==succeeded`；running B 不被消费。✓
- **CASE E**（B failed → Review fail-closed）：同上，failed 排除；Review 不消费。✓
- **CASE F**（published_at None/非None 不改变生命周期判断）：§3.3/`has_canonical_snapshot_run` 去掉
  `published_at` 门控；Core ready/rerun/snapshot 可读性仅取决于 `status==succeeded` + canonical。✓

---

## 6. KPI 映射（§18）

| KPI | 满足方式 |
|---|---|
| B1-1 lifecycle 不依赖 published_at | §3.3/§3.4 去门控 |
| B1-2 A succeeded 后可安全建 B | §3.2 |
| B1-3 B succeeded 时 snapshot 100% source_run_id=B | §1.2 partial index（写隔离）+ R8/R9 精确读 |
| B1-4 B failed 不形成 mixed | §5 CASE C |
| B1-5 A 历史 artifact 不被失败 B 破坏 | §1.2（B 写 source_run_id=B，不碰 A 行） |
| B1-6 running/failed B 不被 Review 消费 | §5 CASE D/E |
| B1-7 Phase A publication read/write=0 不回归 | Review 路径(R8/R9/R15) 不变；orchestrator CURRENT 分支不变 |
| B1-8 Phase A targeted PG 继续 PASS | B1.3 跑 `targeted-pg`，含新增 B1 用例 |
| B1-9 B1 targeted PG rerun cases 全 PASS | §7 PG-B1-A..F |

---

## 7. Rerun-isolation 真值表（A/B 共存矩阵）

| 场景 | A.source_run_id | B.source_run_id | upsert 冲突？ | watchlist 读 | Review 读 |
|---|---|---|---|---|---|
| 仅 A（legacy NULL） | NULL | — | — | legacy NULL 回退=A | A（若持有 A id） |
| A succeeded + B rerun 中 | A / NULL | B(running) | 否 | canonical=A（running 排除 B） | A |
| A succeeded + B succeeded | A / NULL | B(succeeded) | 否 | canonical=B（finished_at 新） | B（source_core_run_id=B） |
| A succeeded + B failed | A / NULL | B(failed) | 否 | canonical=A（failed 排除 B） | A |
| 两 legacy NULL（不合理，历史最多 1/base） | NULL | NULL | 旧约束已 DROP，但历史仅 1 行/base | legacy 回退 | — |

---

## 8. 风险登记 / 护栏

1. **应用不变量**：所有 writer 必须写 `source_run_id`（仅 `upsert_snapshot` 一条路径，已确认写入）。
   若新增 writer 遗漏 → legacy 回退歧义。B1.2 静态审计所有 `StockFeatureSnapshot(...)` 构造。
2. **partial index 与 ON CONFLICT**：`on_conflict_do_update(constraint=...)` 引用新索引名；PG 要求
   冲突列集合精确匹配索引列。B1.2 单测验证。
3. **canonical 解析竞态**：同日多 succeeded run 时 `finished_at DESC` 取最新；rerun B 成功后自然取代 A。
   无需额外锁（同一 after_close 串行）。
4. **legacy NULL 回退范围**：仅 pre-061 行；B1.2 后所有新写均带 source_run_id，legacy 分支仅兜底历史。
5. **`finish_snapshot_run` 仍写 `published_at`**：B1 保留（B3 退役），但无 reader 以其为门控（§2.3）。
   若后续发现遗漏 reader，B1.3 PG 必暴露。
6. **Migration 部署**：§1.3 纪律——不直部署 prod；governance + `migration-roundtrip`/`targeted-pg` 验证。

---

## 9. B1.2 / B1.3 工作分解（交接）

### B1.2 — Migration / Ownership Implementation
1. 新增 migration：DROP 旧约束 + ADD `uq_feature_snapshot_run_isolated`（partial）。更新 model
   `__table_args__`（移除旧 `UniqueConstraint`，新增 `Index(..., postgresql_where=...)` 表达 partial；
   保留 `ix_feature_snapshot_run_instrument`）。
2. `upsert_snapshot`：`ON CONFLICT` 改新索引名 + 简化 WHERE。
3. `create_snapshot_run`：移除 `published_at` 门控（§3.2）；`PublishedSnapshotRunExistsError` 语义收敛为仅拦 running。
4. `has_succeeded_snapshot_run` → `has_canonical_snapshot_run`（去 published_at）；`_get_snapshot_run_summary` 同步。
5. 新增 `get_canonical_snapshot_run`。
6. R1/R3/R4/R5/R6/R7/R10/R11/R12/R13/R14 共 11 处读取点改造为 canonical/精确读（§2.3）。
7. 静态审计：所有 `StockFeatureSnapshot(...)` 构造必须传 `source_run_id`。

### B1.3 — Same-day rerun PG closure
- PG-B1-A：A succeeded(pub=None) → B create allowed。
- PG-B1-B：B succeeded → 全部 snapshot `source_run_id==B`；Review 仅消费 B。
- PG-B1-C：B failed 中途 → 无 A/B mixed canonical；A 行完好。
- PG-B1-D：running B → Review/consume fail-closed。
- PG-B1-E：failed B → Review/consume fail-closed。
- PG-B1-F：`published_at` None/非None 不改变 lifecycle/可读性判断。
- 跑 `panji-verify targeted-pg`：Phase A 62 passed 不回归 + B1 新增用例全 PASS。

---

## 10. git 纪律（§20）

- 本设计文档为**非破坏性 additive artifact**，不含 production 代码变更。
- `git add -- PHASE_B1_1_DESIGN.md`；commit；`git fetch origin dev`；确认 `merge-base --is-ancestor origin/dev HEAD`（ff-only）；
  `git push origin dev` → 形成 `B1.1_VERIFY_SHA`。
- 禁止 `add . / -A / -u` / rebase / amend / force / main / new branch。
- **STOP**：B1.1 设计完成，交 ChatGPT exact-SHA 审计批准后再进入 B1.2 实现。

---

## 11. 字段填表（最终报告用）

```
MASTER_GOAL                = Review 前后端生产闭环
CURRENT_PHASE              = B1.1 — Schema + Reader Architecture Design
BASE_SHA                   = 939600be6174b78a0d75935e0698323b69670cb7
FINAL_SHA (B1.1 design)    = <B1.1_VERIFY_SHA>

snapshot unique key (current) = (instrument_id, trade_date, tf, adj, schema_version)  [无 source_run_id]
schema change              = ADD partial unique index (...) WHERE source_run_id IS NOT NULL; DROP old full uq
source_run_id 属唯一归属    = YES (after B1.2 migration)

A succeeded → B create     = ALLOWED (create_snapshot_run 去 published 门控)
A published → B create     = ALLOWED (同上; published_at 不再是创建判据)

B succeeded snapshot ownership = 100% source_run_id=B (partial index 写隔离)
B failed snapshot ownership   = source_run_id=B 但 run=failed, 不被 canonical 选; A 行完好
mixed A/B                    = NO (写隔离 + canonical 读隔离)
A artifact preserved after B failure = YES

published_at lifecycle deps removed = YES (create/finish/has_*/readers 去门控; finish 仍写但无 reader 用)
running Review consume  = NO (fail-closed)
failed  Review consume  = NO (fail-closed)

schema change = ADDITIVE / SAFE (constraint swap, no row rewrite)
Migration     = DESIGNED_NOT_DEPLOYED (governance + PG 验证后方可排期)

PG-B1-A..F   = NOT EXECUTED (B1.3)
Phase A regression = PASS (未触碰 orchestrator/Review 路径)
formal PG    = pending (B1.3)

PHASE_B1_CORE_LIFECYCLE_CLOSED = NO (B1.1 design only; B1.2+B1.3 后裁定)
NEXT = B1.2 Runtime Publication Dependency Zero (Migration / Ownership Implementation)
```
