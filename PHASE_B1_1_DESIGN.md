# PHASE B1.1-R1 — Snapshot Ownership Architecture Finalization

> MASTER GOAL: Review 前后端生产闭环
> CURRENT_PHASE: B1.1 — Snapshot Ownership Design（FINAL REVISION）
> BASE_SHA: `e8a693fcb883b4a6095affe346afcd7df22df49b`
> PRIOR: `d350236c`（B1 schema-blocker checkpoint）→ `e8a693fc`（B1.1 v1 design）
> THIS_SHA (design only): 见 §14
>
> **B1.1-R1 审计裁决前置：GIT_GOVERNANCE_PASS = YES；B1.1_DESIGN_APPROVED = NO；
> 本修订按审计 §1-§13 逐项收口。禁止 production code / migration implementation / DB write。**

---

## 0. 修订摘要（相对 B1.1 v1 的五大修正）

| 审计缺口 | B1.1 v1（被否） | B1.1-R1（修正，附证据） |
|---|---|---|
| 1. 又造 latest 新 owner | `get_canonical_snapshot_run` = latest succeeded + finished_at DESC | **取消**。explicit lineage 优先；仅真正 date-only 产品 reader 进入 DISPLAY_CORE_OWNER 合同（§6） |
| 2. 11 个 reader 统一 canonical | R1/R3..R14 全部 canonical 化 | **证据修正为 15 个中 10 个已 EXPLICIT**，1 个 PARENT_JOB_RUN，仅 4 个显示类 reader 依赖 DISPLAY_CORE_OWNER（§5） |
| 3. NULL legacy 失去 DB 唯一 | 仅一个 non-null partial index | **双 universe 双 partial unique index**（CURRENT run-scoped + LEGACY base-scoped），有同仓先例（§3） |
| 4. ON CONFLICT 契约错误 | `constraint="uq_..._run_isolated"`（partial index 名） | **修正为 index_elements + index_where 推断**；代码库注释明文禁止 ON CONFLICT ON CONSTRAINT <partial-index>（§4） |
| 5. ADDITIVE/SAFE 分类错误 | ADDITIVE / SAFE | **重分类：NON-DATA-DESTRUCTIVE BUT CONTRACT-BREAKING（schema relaxation）**（§2） |

**补充重大发现（v1 未识别）**：market_stocks 的「当前展示 Core」现 owner = `FactorPublication(kind=stock_core).data_run_id`
（`market_stocks_service.py:807-817`）。PHASE A 已退役运行时自动 publication 写入
（`publish_stock_core_atomically` 在 runtime 零调用；仅 admin 手动可写）→ **该 pointer 已冻结/陈旧**。
即：**display owner 现状已损坏（stale-or-latest-guessed），与是否做 B1 无关**。这使
DISPLAY_CORE_OWNER 合同成为**必须**，而不是可选增强（§6）。

---

## 1. 保留的已确认事实（§1，无需重做）

- `stock_feature_snapshots` 唯一键 `uq_feature_snapshot_instrument_date_tf_adj_schema`
  = `(instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj, schema_version)`，
  **不含 `source_run_id`**（model `stock_feature_snapshot.py:117-125`）。
- `source_run_id` 为 nullable lineage FK（`stock_feature_snapshot.py:72-77`）；migration 061 晚于 056 新增
  （nullable 兼容历史），pre-061 行 `source_run_id=NULL`。
- `upsert_snapshot`（`feature_snapshot_service.py:1834-1846`）`ON CONFLICT` 命中上述全键约束，
  `WHERE` 以 `published_at IS NOT NULL` 为覆盖保护判据 → succeeded-but-unpublished run 不受保护 → rerun 覆盖（P0）。
- Review R8/R9 已 explicit `source_core_run_id`（Phase A 隔离成立）。
- `compute_review_core_with_run_items` 要求 `snapshot_run_id`（positional 必填）→ 主链 ALWAYS-bound。

---

## 2. Migration 分类修正（§2 / §15）

| 项 | 值 |
|---|---|
| DATA_REWRITE_REQUIRED | **NO**（设计成立前提下；无行重写） |
| HISTORICAL_BACKFILL_REQUIRED | **NO**（历史 NULL 行保留为 legacy universe，由 legacy partial index 保护，无需回填） |
| SCHEMA_RELAXATION | **YES**（每 base 1 行 → 每 base N 行，按 run 隔离） |
| OLD CONSTRAINT DROP | **YES**（DROP 旧 6 列唯一约束） |
| COMPATIBILITY_SENSITIVE | **YES**（DROP 后 legacy NULL 唯一性依赖新 partial index，必须与 writer/reader 同版本原子验证） |
| MIGRATION_RISK | **MEDIUM**（无数据破坏/无回填；但约束替换 + 双 partial index 必须单 migration 原子应用 + migration-roundtrip/targeted-pg 验证） |
| **MIGRATION_CLASS** | **NON-DATA-DESTRUCTIVE BUT CONTRACT-BREAKING（schema relaxation）** |

**禁止再写 ADDITIVE / SAFE。** 部署纪律：不直部署 prod；governance + `migration-roundtrip`/`targeted-pg` 验证后方可排期（§15）。

---

## 3. DB Invariant 双 universe 设计（§3）

### 3.1 目标 DDL（RECOMMENDED_DB_INVARIANT = OPTION A2：两个 partial unique index）

```sql
-- 1) DROP 旧全键唯一约束（不再约束 source_run_id）
ALTER TABLE stock_feature_snapshots
  DROP CONSTRAINT uq_feature_snapshot_instrument_date_tf_adj_schema;

-- 2) CURRENT universe：run-scoped 唯一性（source_run_id NOT NULL）
CREATE UNIQUE INDEX uq_feature_snapshot_run_current
  ON stock_feature_snapshots (
    instrument_id, trade_date, primary_timeframe, secondary_timeframe,
    adj, schema_version, source_run_id
  )
  WHERE source_run_id IS NOT NULL;

-- 3) LEGACY universe：base-key 唯一性（source_run_id NULL 保留 DB 级保护）
CREATE UNIQUE INDEX uq_feature_snapshot_legacy_base
  ON stock_feature_snapshots (
    instrument_id, trade_date, primary_timeframe, secondary_timeframe,
    adj, schema_version
  )
  WHERE source_run_id IS NULL;
```

### 3.2 正确性证明

- **A/B 共存（CURRENT）**：A(source=A)、B(source=B) 的 `(base, source_run_id)` 不同 → CURRENT index 不冲突 → 同时 INSERT，互不覆盖。
- **同 run 幂等（CURRENT）**：run B 重 upsert 自身行 → `(base, B)` 冲突 → DO UPDATE，更新 B 行。
- **LEGACY 唯一（DB fail-closed）**：`source_run_id IS NULL` 的行受 `uq_feature_snapshot_legacy_base` 约束 →
  同 base 最多 1 行。**即使未来 writer 遗漏 source_run_id，DB 仍拒绝同 base 重复 NULL 行**（不再依赖应用层自觉）。
- **LEGACY/CURRENT 共存**：NULL 行在 CURRENT index 中被豁免（`WHERE IS NOT NULL`），CURRENT 行在 LEGACY index 中被豁免 →
  历史 NULL 行与新 run 行同 base 共存，legacy 回退读 NULL 行，精确读 CURRENT 行。✓
- **OPTION A1（real UniqueConstraint(base..., source_run_id) + partial legacy）被否**：
  real constraint 在 Postgres 中允许 NULL 不冲突（NULL ≠ NULL），CURRENT universe 语义与 A2 相同；
  但 A1 的 CURRENT 需 `ON CONFLICT ON CONSTRAINT`，而代码库 CURRENT 写入路径与 partial index 先例
  （`FirstPyramidHistoryEvent`）完全一致 —— **A2 是 house pattern，A1 无额外收益**。

### 3.3 同仓先例（强证据）
`FirstPyramidHistoryEvent`（`first_pyramid_history_service.py:1629-1652`）**已实施完全相同的双 universe**：
普通 UNIQUE 约束拆成两个 partial unique index（`WHERE history_contract_version IS NULL` /
`WHERE history_contract_version IS NOT NULL`），注释明文：
> "on_conflict 必须用 index_elements + index_where 做 index inference，禁止 ON CONFLICT ON CONSTRAINT <partial-index-name>，
> 保证旧 NULL X + v2 X 可共存、v2 X 重跑仍幂等。"

这正是审计要求的形态，且已在 production 验证。

---

## 4. ON CONFLICT 契约（可实施级，§4）

### 4.1 CURRENT writer（run-scoped）
```python
stmt = pg_insert(StockFeatureSnapshot).values(...)
stmt = stmt.on_conflict_do_update(
    index_elements=[
        StockFeatureSnapshot.instrument_id,
        StockFeatureSnapshot.trade_date,
        StockFeatureSnapshot.primary_timeframe,
        StockFeatureSnapshot.secondary_timeframe,
        StockFeatureSnapshot.adj,
        StockFeatureSnapshot.schema_version,
        StockFeatureSnapshot.source_run_id,
    ],
    index_where=text("source_run_id IS NOT NULL"),
    set_={...payload cols..., "updated_at": func.now()},
)
```
- **冲突目标 = `uq_feature_snapshot_run_current`（partial unique index，index inference）**。
- **禁止 `constraint="uq_..."`**（ON CONFLICT ON CONSTRAINT 仅对 real constraint 合法；对 partial unique index 无效）。

### 4.2 LEGACY writer（source_run_id IS NULL）
```python
stmt = stmt.on_conflict_do_update(
    index_elements=[base 6 列...],          # 不含 source_run_id
    index_where=text("source_run_id IS NULL"),
    set_={...},
)
```

### 4.3 `source_run_id=None` 是否允许进入 CURRENT writer？—— **fail-closed：禁止**
- CURRENT writer（`compute_review_core_with_run_items` 及 batch 入口 run-scoped 模式）在 upsert 前
  **assert `snapshot.source_run_id is not None`**（否则 `ValueError`）。
- LEGACY 写入（backfill，若保持 NULL）必须**显式走 legacy 冲突目标**，与 CURRENT 完全分离，
  **禁止共享一个 silent optional contract**。
- 若 B1.2 采用「修复 backfill 传 source_run_id」（推荐，见 §7），则 LEGACY writer 仅剩「历史数据维护脚本」，
  legacy 冲突目标仅用于兜底。

---

## 5. Writer Caller Map（函数级 → caller 级，§5）

| writer（函数） | 生产 caller | source_run_id | 分类 |
|---|---|---|---|
| `compute_review_core_with_run_items` | `after_close_orchestrator.py:3597`（AfterClose scheduled computing_features） | **ALWAYS**（`snapshot_run_id` positional 必填；upsert 于 1474/1490） | **CURRENT** |
| `compute_review_core_for_trade_date` | 经 with_run_items（1460 `source_run_id=snapshot_run_id`）；经 batch（1194 透传 param） | ALWAYS（主链）；MAYBE_NULL（batch 入口） | CURRENT / legacy-entry |
| `compute_review_core_batch_for_trade_date` | **无生产 caller**（tests 仅） | MAYBE_NULL（param 默认 None） | legacy-entry（保留） |
| `compute_for_trade_date` | **无生产 caller**（tests 仅） | MAYBE_NULL（param 默认 None） | legacy-entry（保留） |
| `compute_feature_snapshot_for_date` | `feature_snapshot_backfill.py:593,867`；`canonical_adapters.compute_snapshot_derived_adapter:581`（无活跃生产 caller，registry 合同仅） | **NULL**（backfill 不传） | **LEGACY/backfill** |
| `create_snapshot_run` | `after_close_orchestrator.py:3536`（CURRENT）；`feature_snapshot_backfill.py:492,1000`（backfill 建 run） | — | CURRENT + backfill |

**关键事实**：
- **CURRENT production writer 仅一条**（orchestrator → with_run_items），source_run_id **ALWAYS**（非可选）。
- **backfill 存在 lineage gap**：`feature_snapshot_backfill.py:492/1000` 创建 run，但 `593/867` 计算快照时
  **不传 source_run_id** → backfill 快照落入 NULL universe（run 存在但行未绑定）。B1.2 推荐修复为传
  `source_run_id=run.id`（一行改动，把 backfill 并入 CURRENT universe，杜绝 future NULL 写入）。
- legacy-entry（batch / compute_for_trade_date）无生产 caller → B1.2 只补 fail-closed 守卫，不扩展。

**目标策略（§5）**：CURRENT production writer `source_run_id MUST NOT BE NULL`；backfill 修复为 run-scoped；
历史 NULL 行保持 legacy universe（DB 级保护，不回填）。

---

## 6. Reader Domain Owner Map（15 个读取点终审，§6-§8）

| R | 站点 | DOMAIN_OWNER | 裁决 |
|---|---|---|---|
| R1 | `api/watchlist.py:371-379`（monitor-status） | **PRODUCT_DISPLAY_OWNER**（仅 trade_date 上下文） | **改**：解析 DISPLAY_CORE_OWNER → `source_run_id` 精确读 |
| R2 | `api/stock_context.py:223-253` | **EXPLICIT_RUN**（`source_run_id == run.id` + legacy 回退） | OK（验收） |
| R3 | `api/stock_context.py:495-535`（recent changes） | **PRODUCT_DISPLAY_OWNER**（published-gated 多日读） | **改**：去 published 门控；per-date display owner 绑定 |
| R4 | `market_stocks_service.py:493-501`（lateral） | **EXPLICIT_RUN** 当 `snapshot_run_id` 给定（819 传 `published_core_run_id`） | **改**：display owner 重新指定来源（现为陈旧 pub pointer） |
| R5 | `market_stocks_service.py:807-819`（display owner 解析） | **PRODUCT_DISPLAY_OWNER**（现 = `FactorPublication(stock_core).data_run_id`，**已陈旧**） | **改**：按 §6.3 合同替换 |
| R6 | `market_stocks_service.py:1007-1028`（window rn=1） | **PRODUCT_DISPLAY_OWNER**（date-only latest，无 pin） | **改**：display owner 绑定 |
| R7 | `state_event_service.generate_events_for_run:293` | **EXPLICIT_RUN**（`source_run_id == run.id`；事件幂等键 = symbol:source_run_id:algo） | OK |
| R8 | `review_scope_service.py:495,885` | **REVIEW_LINEAGE**（`source_core_run_id`） | OK |
| R9 | `review_observation_prep_service.py:434` | **REVIEW_LINEAGE**（`source_core_run_id`） | OK |
| R10 | `core_artifact_repository.py:48` | **EXPLICIT_RUN**（`source_run_id == source_core_run_id`） | OK |
| R11 | `granular_restart_service.py:557,691` | **EXPLICIT_RUN**（`source_run_id == source_core_run_id`） | OK |
| R12 | `product_readiness_service.py:1599,1614` | **EXPLICIT_RUN**（`source_run_id == source_core_run_id`；跨日 existence 子查询无害） | OK |
| R13 | `auction_anchor_service.py:938` | **EXPLICIT_RUN**（`_load_core_snapshots(trade_date, core_run_id)` 双键） | OK |
| R14 | `after_close_orchestrator.py:2269-2275`（History 物化） | **PARENT_JOB_RUN**（execution 持 `snapshot_run_id=X`/`core_run_id`） | **改**：`WHERE source_run_id == X`（禁止全量遍历） |
| R15 | `after_close_orchestrator.py:5269-5271` | **EXPLICIT_RUN**（`source_run_id == snap_id`） | OK |

**结论（§8 要求的 DISPLAY_OWNER_REQUIRED_READERS）**：
- EXPLICIT / REVIEW_LINEAGE / PARENT 直接绑定 = **10 + 1 = 11 个已/可直接 explicit**，**零 canonical resolver**。
- 真正 date-only 产品展示 reader（需 DISPLAY_CORE_OWNER）= **R1 / R3 / R4+R5 / R6，共 4 个站点（1 个合同）**。
- **R14 是唯一「parent 绑定」改动**（history 物化只读当前 execution 的 X）。
- 审计 §7 特别裁决：R7 state_events → 绑定 X（**已绑定**，`generate_events_for_run(db, snapshot_run_id)`）；
  R11 granular_restart → 绑定 restart lineage（**已绑定**，`source_core_run_id`）；R14 History → `History(X)`（需改）；
  R12 readiness → 绑定 X（**已绑定**，`source_core_run_id`）。

---

## 7. DISPLAY_CORE_OWNER_CONTRACT（§8/§9）

### 7.1 现状（证据）
- `market_stocks_service.py:807-817`：`get_publication(scope=market, kind=stock_core) → pub.data_run_id`。
- `stock_context.py:112-118,167-174`：同款 publication 读（stock context 展示 owner）。
- PHASE A 后 `publish_stock_core_atomically` **runtime 零调用**（仅注释残留）；pointer 仅 admin 手动可写
  （`admin_incremental_publish.py:80`）→ **pointer 冻结在最后一次手动发布，或 NULL**。
- 因此：R5 的 display owner **要么陈旧（stale），要么回退 latest-guess** —— 现状已损坏，与 B1 无关。

### 7.2 合同（推荐默认，需产品签核）
> **DISPLAY_CORE_OWNER(T) = 当日 AfterClose execution lineage 记录的 `feature_snapshot_run_id`**
> （`job_run.metadata["feature_snapshot_run_id"]`，写入点 `after_close_orchestrator.py:3576`，
> 恢复读取点 2003/3056/5188；**durable 执行血缘，非 snapshot 时间戳**）。
> - 资格：full-scope + `status == succeeded` 的 Core run（CORE_READY 语义，§13/Phase A 已批准，不含 published_at）。
> - **显式 supersede**：对 T 发起新 AfterClose execution（rerun）→ 新 execution 成为当日 owner（执行血缘即权威）。
> - **backfill/manual 日**（无 AfterClose execution）：owner = 该 backfill/manual 全量 run（其自身 run 血缘记录）；
>   运营显式 supersede。
> - **明确禁止**：(a) 对 `stock_feature_snapshot_runs` 按 `finished_at DESC` 取 latest（timestamp 猜测）；
>   (b) 新增 display pointer 表/列（= 再造 publication，Design B 即此）。

### 7.3 LATEST_SUCCESSFUL_RERUN_WINS
- **YES，但仅经执行血缘（job_run / backfill run lineage），非 snapshot finished_at**。
- 资格：`run_type ∈ {after_close, manual, backfill}` 且 full scope；scheduled/manual/backfill 谁有资格抢占
  display owner = **产品合同**（本设计提供默认：AfterClose execution 为权威；manual/backfill 仅在其缺席时，
  且须显式 ops supersede）。
- `scope=full` **≠** canonical（审计 §9 正确）；full 只是资格必要条件。

---

## 8. DESIGN A（修正版）评估（§10/§11）

| 维度 | 结论 |
|---|---|
| rerun isolation | writer run-scoped（已具备主链）+ DB run-scoped（A2 双 partial index） |
| failed B safety | B 行 `source_run_id=B` 但 run=failed；display/Review 均不选 failed；A 行完好 |
| reader blast radius | **5 个站点**：R14（parent 绑定）+ R1/R3/R5/R6（display owner 合同） |
| schema complexity | 1 migration：DROP 1 constraint + ADD 2 partial unique index（house 先例） |
| migration risk | MEDIUM（contract-breaking，无数据破坏） |
| historical rewrite | **NO**（NULL 留 legacy universe，DB 级保护） |
| publication dependency | display owner 从 publication pointer 迁移到 execution lineage（顺带修复现状 stale pointer） |
| watchlist owner complexity | 中（1 个合同 + 4 站点） |
| implementation effort | 中：migration + writer fail-closed 守卫 + backfill 传 run id + R14 + display 合同 + PG-B1-A..F |
| deployment risk | 中（约束替换需与 writer/reader 原子发布） |
| Exploration speed | 快（B1.2 实现 + B1.3 PG 可闭环） |
| future cleanliness | 高（消灭 timestamp guessing；display owner 显式 durable） |

**DESIGN_A = VIABLE**（修正 scope 后：不是「十几个 reader + 新 display owner 才能 rerun」，而是 5 站点 + 1 合同；
且 display owner 合同**本来就必须做**——现状已损坏）。

---

## 9. DESIGN B（staging → atomic promote）评估（§10）

| 维度 | 结论 |
|---|---|
| schema changes | staging universe + canonical pointer（新表/新列，或复用 snapshot 行 + supersede 列） |
| reader changes | 全部 reader 改读 pointer（display owner 显式化，同 Design A 的 R1/R3/R5/R6 + 更多） |
| failed B isolation | B 不 promote → A 保持 display ✓（隔离性好） |
| storage | 双写 staging + canonical（或 supersede 版本列） |
| promotion transaction | 新增原子 promote 事务（pointer 更新 = 单行 update，但要处理并发/回滚） |
| **publication-like pointer 是否重现** | **YES —— 明确写出**：promote = 写 canonical pointer = 再造 publication 机制（与 B2/B3 目标矛盾） |
| implementation cost | 高（新机制 + 事务 + 版本化 + 兼容） |
| Review lineage | 不受影响（source_core_run_id 独立） |
| watchlist compatibility | 依赖 pointer 写入（backfill/AfterClose 都要 promote） |

**DESIGN_B = VIABLE but BLOCKED for B1 recommendation**：其本质是「再造一个 publication-like pointer」，
与 PHASE B「退役 publication」的目标自相矛盾；成本高于 Design A，收益相同。

---

## 10. DEFER B1 分析（§11）

| 维度 | 结论 |
|---|---|
| 保持 current schema | 是（不 DROP 约束，保留 `PublishedSnapshotRunExistsError`，保留 publication 保护） |
| 同日 full rerun | 保持 blocked（已发布 full run 存在时拒绝新 run） |
| **是否阻塞 Review 产品上线** | **NO** —— Review 已 explicit `source_core_run_id` + `status==succeeded`（Phase A），
  与同日 rerun 无关；backfill 新日期（无 published run）不受阻 |
| 遗留问题 | (1) 同日 rerun 不可用（ops 能力缺失）；(2) display owner stale pointer 未修
  （**注意：这是独立于 B1 的现存 bug**，即便 DEFER 也应单独立项修复） |

**DEFER_B1 = VIABLE**（不阻塞 Phase C），但必须单独立项修 display stale pointer（否则 watchlist/market 展示持续陈旧）。

---

## 11. 三方案正式比较（§12）

| 维度 | A（修正） | B | DEFER |
|---|---|---|---|
| rerun isolation | 强（DB run-scoped） | 强 | 无（rerun blocked） |
| failed rerun safety | 强（行级隔离） | 强（不 promote） | N/A（不允许 rerun） |
| reader blast radius | 5 站点 + 1 合同 | 全部经 pointer | 0 |
| schema complexity | 中（1 migration + 2 index） | 高（pointer 机制） | 0 |
| migration risk | MEDIUM | HIGH | 0 |
| historical rewrite | NO | NO（但需 pointer 回填） | N/A |
| publication dependency | display owner 去 publication | **重现 publication-like** | 保留 |
| watchlist owner complexity | 中 | 高 | 0（但 stale bug 未解） |
| implementation effort | 中 | 高 | 低（仅修 stale bug） |
| deployment risk | 中 | 高 | 低 |
| Exploration speed | 快 | 慢 | 立即进 Phase C |
| future cleanliness | 高 | 低（违背 B2/B3） | 中（rerun 悬置） |

**RECOMMENDED_DESIGN = A**（证据支撑：15 个 reader 中 10 个已 explicit、主链 writer 已 run-scoped，
系统已在向 immutable run artifact 演进——与审计 §0「Design A 最有潜力」判断一致；
且 display owner 合同是现存 bug 的必修项，与 B1 深度协同）。
**DEFER = 有效 fallback**（若产品/治理希望 Phase C 优先、零 migration 风险；rerun 冻结不阻塞 Review 上线）。
**B = 否**（再造 publication-like pointer，违背 PHASE B 目标）。

---

## 12. B1.1 Done Condition 清单（§13）

| 项 | 值 |
|---|---|
| HISTORICAL_BACKFILL_REQUIRED | **NO**（NULL 留 legacy universe，DB 级保护，不回填） |
| DB_CURRENT_UNIQUENESS | `uq_feature_snapshot_run_current`（base..., source_run_id）WHERE source_run_id IS NOT NULL |
| DB_LEGACY_NULL_UNIQUENESS | `uq_feature_snapshot_legacy_base`（base 6 列）WHERE source_run_id IS NULL |
| ON_CONFLICT_CONTRACT | **VALIDATED_DESIGN**：CURRENT `index_elements=[base7...source_run_id], index_where="source_run_id IS NOT NULL"`；
  LEGACY `index_elements=[base6], index_where="source_run_id IS NULL"`；禁止 `constraint=` 引用 partial index
  （同仓先例 first_pyramid_history_service.py:1632 已验证） |
| CURRENT_WRITER_NULL_ALLOWED | **NO**（fail-closed：upsert 前 assert source_run_id is not None） |
| READER_EXPLICIT_OWNER_COUNT | **11 / 15**（R2,R7,R8,R9,R10,R11,R12,R13,R15 + R14 parent 绑定 + R4 传入即 explicit） |
| DISPLAY_OWNER_REQUIRED_READERS | **R1 / R3 / R5 / R6（4 站点，1 合同）** |
| DISPLAY_CORE_OWNER_CONTRACT | **AfterClose execution lineage（job_run.metadata.feature_snapshot_run_id）**；资格 full+succeeded；显式 supersede；禁止 timestamp/pointer |
| DESIGN_A | **VIABLE** |
| DESIGN_B | **VIABLE but publication-like → BLOCKED for B1** |
| DEFER_B1 | **VIABLE（不阻塞 Phase C）** |
| RECOMMENDED_DESIGN | **A**（DEFER 为 fallback） |
| MIGRATION_IMPLEMENTATION_AUTHORIZED | **NO**（本包仅设计；批准后 B1.2 实现） |

---

## 13. B1.2 / B1.3 工作分解（交接，待批准）

### B1.2 — Schema + Ownership Implementation
1. migration：DROP 旧约束 + ADD 双 partial unique index（§3.1）；model `__table_args__` 同步。
2. `upsert_snapshot`：CURRENT 冲突目标 + fail-closed assert；LEGACY 冲突目标显式分离（§4）。
3. `create_snapshot_run`：去 `published_at` 门控（A succeeded 可建 B，§8 CASE A）；`PublishedSnapshotRunExistsError`
   收敛为仅拦 running。
4. `has_succeeded_snapshot_run` → 弃 bool，改 `resolve_display_snapshot_run(T) → run`（§7 合同；一个 API 返回 owner，
   不再 has_xxx()+再猜两步）。
5. R14 History 物化绑定 X；R1/R3/R5/R6 走 DISPLAY_CORE_OWNER（含 market_stocks/stock_context 去 publication 读）。
6. backfill 传 `source_run_id=run.id`（修 lineage gap）；legacy-entry batch 补 fail-closed 守卫。
7. writer 静态审计：所有 `StockFeatureSnapshot(...)` 构造必须显式 source_run_id（CURRENT）或显式 legacy 标记。

### B1.3 — Same-day rerun PG closure
- PG-B1-A..F（同 v1 §9 清单，逐条断言 run id / source_run_id / status / trade_date 全列归属）。
- `panji-verify targeted-pg`：Phase A 62 passed 不回归 + B1 用例全 PASS；`migration-roundtrip` 验证双 partial index。

---

## 14. 字段填表（最终报告用）

```
BASE_SHA                 = e8a693fcb883b4a6095affe346afcd7df22df49b
FINAL_SHA (B1.1-R1)      = <B1_1_FINAL_DESIGN_SHA>

DB invariant             = dual-universe partial unique indexes (A2)
legacy NULL invariant    = uq_feature_snapshot_legacy_base (base6) WHERE source_run_id IS NULL
ON CONFLICT design       = index_elements + index_where (CURRENT run-scoped / LEGACY base-scoped); 禁 constraint=partial-index

historical backfill      = NO
writer current-null policy = NO (fail-closed)

readers:
  explicit lineage       = 11/15 (R2,R7,R8,R9,R10,R11,R12,R13,R15 + R14 parent + R4 传入即 explicit)
  display owner          = R1, R3, R5, R6 (4 sites, 1 contract)
  legacy                 = R2 legacy-fallback (kept); backfill NULL rows (legacy universe)
  unknown                = 0

DISPLAY_CORE_OWNER       = AfterClose execution lineage (job_run.metadata.feature_snapshot_run_id); full+succeeded; explicit supersede

Design A                 = VIABLE
Design B                 = VIABLE but publication-like → BLOCKED for B1
Defer                    = VIABLE (不阻塞 Phase C)

recommended              = A

migration classification = NON-DATA-DESTRUCTIVE BUT CONTRACT-BREAKING (schema relaxation); RISK=MEDIUM
production changed       = NO
migration created        = NO

B1_1_DESIGN_CLOSED       = CANDIDATE
NEXT_REQUEST             = B1.2 / (或 DEFER → PHASE C)
```
