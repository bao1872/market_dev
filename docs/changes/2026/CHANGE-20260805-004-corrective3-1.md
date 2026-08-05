# CHANGE-20260805-004：Corrective-3.1 — 发布 fencing / 领域终态治理 / 精确 lineage / 领域 run 唯一性

- 日期：2026-08-05
- 类型：behavior + contract + docs
- 领域：ChipConsensusRun 发布 fencing、领域 run 终态失败治理、ProductReadiness 精确 lineage、
  `ChipConsensusRun` 数据库唯一约束、Migration 086 去重逻辑修正、验收矩阵纠偏
- 关联前序：`CHANGE-20260805-003-corrective3-chip-chain.md`
- 基线（Corrective-3.1 起点）：`5a96e34`
- 最终代码 SHA：`16b056f`

## 0. 为什么需要 Corrective-3.1

Corrective-3（`abbd845` → `f1612f6`）代码主体通过，但审查中发现 4 项结构性缺陷未修复，
开发阶段验收不能标 `development_complete`：

```text
P0-1 生产 worker 调用 publish_chip_and_upgrade_auction 未传 ownership_check；
     且 publication 在 finally: heartbeat.stop() 之后执行（租约已释放），fencing 对生产不成立。
P0-2 finalize_chip_run 异常被吞，仍无条件写 main_status=succeeded，
     导致 SchedulerJobRun=succeeded 而 ChipConsensusRun=running 不一致。
P1-唯一性 resolve_or_create_chip_run 是 SELECT-then-INSERT，无 DB 唯一约束，并发不幂等。
P1-lineage DSA 仅按 trade_date 计数、state_events 仅日期级存在性、
            review 只查 MarketReviewRun 未查 FactorPublication pointer。
```

## 1. 行为变化

### 1.1 生产发布 fencing 上移并传 ownership_check（P0-1）

`app/worker.py` 修改：

- 将 publication / auction upgrade **从** `finally: heartbeat.stop()` 之后
  上移到 `finalize_job_run` 之前，**在 job lease 仍有效时执行**。
- 调用 `publish_chip_and_upgrade_auction(..., ownership_check=heartbeat.ensure_owned)`，
  publication 与 auction 均在租约保护区内进行；lease 丢失则 `ownership_check` 抛错，
  `ownership_check_failed=True`，跳过 publication/auction 写入，worker 重新排队领取。
- 生产源码块（`await publish_chip_and_upgrade_auction(`）已通过
  `test_chip_worker_orchestration.py::test_production_worker_passes_ownership_check`
  静态断言确认传入 `ownership_check=heartbeat.ensure_owned`。

### 1.2 领域 run 终态失败治理（P0-2）

`finalize_chip_run` 失败不再被静默吞掉：

- 失败写入 `metadata_updates`：`chip_domain_finalize_status="failed"` /
  `error_code="CHIP_DOMAIN_FINALIZE_FAILED"` / `error_message` / `retryable`。
- `domain_finalized=False` → 阻断后续 publication 与 auction upgrade。
- 主任务 `main_status="failed"`（保留既有 `interrupted` / `skipped` 分支）。
- 区分 `CHIP_SYSTEMIC_FAILURE`（worker 级致命，业务侧视为 unavailable）与
  `CHIP_DOMAIN_FINALIZE_FAILED`（领域 run 终态写库失败）。
- 状态机使用合法值（`succeeded/running/failed/skipped/interrupted/queued/
  resume_queued`），**不使用 `degraded`**（SchedulerJobRun 无该值）。

### 1.3 数据库级领域 run 唯一性（P1-唯一性）

- `app/models/chip_consensus_run.py`：`__table_args__` 新增
  `UniqueConstraint("trade_date","source_core_run_id","algorithm_version",
  name="uq_chip_consensus_runs_date_core_algo")`。
- `chip_consensus_run_lifecycle.resolve_or_create_chip_run`：改为
  `pg_insert(ChipConsensusRun).values(...).on_conflict_do_nothing(
  index_elements=["trade_date","source_core_run_id","algorithm_version"]
  ).returning(ChipConsensusRun.id)`；冲突时回读复用同一 run（原子 upsert，
  并发下只生成一条 run）。
- Migration `086_chip_consensus_run_uniqueness.py`：改动见 §3。

### 1.4 ProductReadiness 精确 lineage（P1-lineage）

`app/services/product_readiness_service.py`：

- `_count_dsa_projections(db, trade_date, source_core_run_id=None)`：返回
  `{total, matched}`，`matched` 为归属当前 core run 的投影数。
- `_count_state_events`：返回 `{total, matched, by_type, algorithm_versions}`，
  `matched` 为归属当前 core run 的事件数。
- 调用方按 `matched` 判 ready：`matched>0` → ready；`total>0 and matched==0`
  → `PROJECTION_LINEAGE_MISMATCH` / `STATE_EVENTS_LINEAGE_MISMATCH`（degraded，不误报 ready）。
- `_review_state`：先查 `FactorPublication(publication_kind=market_review)` 正式
  pointer；无 pointer（即使 `MarketReviewRun` 自称 published）不判 ready，
  `REVIEW_POINTER_MISSING` / `REVIEW_NOT_PUBLISHED`。
- 治理映射补 3 项（lineage mismatch / review pointer missing / domain finalize failed）。

## 2. 测试修复（与 Corrective-3.1 代码契约对齐）

- `test_chip_worker_orchestration.py`：`FakeSession` 新增 `unique_index` 与
  `_insert_values`/`_apply_upsert` 模拟 `ON CONFLICT DO NOTHING` 原子 upsert；
  production ownership_check 断言改为定位 `await publish_chip_and_upgrade_auction(`
  调用处（不依赖 import 行触发配置校验）。
- `test_product_readiness_service_layer.py`：**重写为按查询实体模型 + publication_kind
  路由的 `_FakeDB`**。原线性 `pop(0)` mock 与 Corrective-3.1 的真实查询结构
  （review 先查 `FactorPublication(market_review)` + `MarketReviewRun`；dsa 用
  `db.scalar` 计数；state_events 用 `db.execute`）不一致，导致错位失败。新增
  `test_review_requires_market_review_pointer` / `test_dsa_lineage_mismatch_not_ready`
  / `test_dsa_exact_match_ready` / `test_state_events_lineage_mismatch_not_ready` /
  `test_state_events_exact_match_ready` 等精确 lineage 用例。
- `test_migration_086_chip_run_uniqueness_contract.py`（**新增**，纯文件级，不连库）：
  覆盖 migration chain `085 → 086`、upgrade 重复 preflight 报错不修改历史行、
  downgrade 只删约束、ORM 与 migration 约束名称一致。

## 3. Migration 086 去重逻辑修正（关键）

原 086 逻辑：找出重复 `ChipConsensusRun` → 把重复行 status 改 `cancelled`
→ 创建唯一约束。**但 status=cancelled 不改变唯一键三列**，重复组依然存在，
唯一约束仍无法创建；原迁移实际只能在"历史上无重复行"时成功。

修正后 086：

```text
upgrade:
1. 只做重复数据 preflight 检查（只读 SELECT，GROUP BY 三键 HAVING COUNT>1）
2. 若存在重复 → 明确 RAISE 并输出重复组（前 50），事务整体回滚，约束不创建
3. 无重复 → 创建硬唯一约束
```

- **不修改任何历史业务记录**（无 UPDATE/DELETE on chip_consensus_runs）。
- 若真实库存在重复，需单独数据对账方案（选 canonical run、核查 publication
  pointer / run items / SchedulerJobRun metadata、明确引用关系、经人工确认后
  再合并或归档），不在迁移内自动处理。
- downgrade 只 `drop_constraint`，不修改业务数据。

## 4. 修改文件

```text
backend/alembic/versions/086_chip_consensus_run_uniqueness.py   重复 preflight（不伪造去重）
backend/app/models/chip_consensus_run.py                        +UniqueConstraint
backend/app/services/chip_consensus_run_lifecycle.py            pg_insert ON CONFLICT DO NOTHING 原子 upsert
backend/app/worker.py                                          publication/auction 上移 + ownership_check
backend/app/services/product_readiness_service.py               精确 lineage（matched 判定）
backend/tests/test_chip_worker_orchestration.py                 FakeSession upsert 模拟 + 路径读取断言
backend/tests/test_readiness_lineage_governance.py             精确 lineage 用例
backend/tests/test_product_readiness_service_layer.py          重写为路由式 _FakeDB
backend/tests/test_migration_086_chip_run_uniqueness_contract.py  新增（纯文件级）
docs/changes/2026/PRD-Acceptance-Matrix-V2.1-D-J-2026-08-05.md  加 Corrective-3.1 章节
```

## 5. 验证状态（如实）

阶段 2（开发阶段非数据库验证）在远程隔离 worktree 精确检出 `16b056f` 后执行，
阶段 4（PG 集成）尚未授权。

```text
remote_static_verified          = true    # Ruff 9 个改动文件 All checks passed
remote_unit_verified            = true    # PURE_UNIT_TEST 90 passed（含 086 contract），postgres=0
remote_frontend_build_verified  = true    # vite build ✓（前端代码未变，同一 SHA 复验）
migration_086_authored          = true
migration_086_static_verified   = true
migration_086_applied           = false   # 阶段 4 PG 集成后才执行
migration_086_pg_verified       = false
production_publication_fenced  = true    # 源码块静态断言 + ownership_check
chip_domain_finalize_failure_governed = true
database_run_uniqueness_authored = true  # ORM 约束 + pg_insert，待 PG 验证
exact_lineage_by_core_run       = true    # 服务层测试覆盖 matched 判定
review_pointer_exact            = true
development_chain_D_to_J       = development_complete   # 开发阶段收口
pg_tested        = false
deployed         = false
runtime_verified = false
data_closed      = false
browser_verified = false
```

验证方式：`git worktree add -f /root/c31_verify 16b056f`（清理并重建自旧 worktree），
**未触碰运行部署**，未连接 PG，未执行 migration。前端复用本地 `frontend/node_modules`
（`16b056f` 未改前端，package.json 一致），`npm run build` 成功。

### Mypy 改动模块零新增错误

`mypy` 对 5 个改动模块（`chip_consensus_run_lifecycle.py` /
`product_readiness_service.py` / `worker.py` / `chip_consensus_run.py` /
`086_chip_consensus_run_uniqueness.py`）在本轮与基线 `5a96e34` 均报告 45 errors，
且全部位于未改动文件 `after_close_orchestrator.py`（16）/ `auction_aggregation_service.py`（5）
（级联 import 分析产物），**Corrective-3.1 改动模块零新增错误**。

### PURE_UNIT_TEST 目标集（90 passed）

```text
test_chip_worker_orchestration.py
test_readiness_lineage_governance.py
test_chip_publication_unit.py
test_product_readiness_service_layer.py
test_governance_report_unit.py
test_v21_readiness_auction_decision_integration.py
test_migration_086_chip_run_uniqueness_contract.py
postgres connections = 0
```

## 6. 已知限制

1. Migration 086 仅完成**静态**验证（preflight 逻辑、约束名称、chain）。
   PG 集成（含历史重复数据对账）在阶段 4 执行，未授权前不得 apply。
2. `database_run_uniqueness` 的并发幂等（两事务只生成一条 run）经 pg_insert
   构造覆盖，但真实数据库并发行为待阶段 4 验证。
3. `exact_lineage_by_core_run` 由服务层测试覆盖 matched 判定；真实 DB 计数
   行为待阶段 4 验证。
4. `production_publication_fenced` 经 worker 源码块静态断言（ownership_check
   传入 + 执行位置在 lease 保护区内），真实 lease 丢失运行行为待阶段 5 验证。
