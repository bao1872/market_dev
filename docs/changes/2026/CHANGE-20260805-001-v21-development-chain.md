# CHANGE-20260805-001：V2.1 开发链 Commit D–J 收口

- 日期：2026-08-05
- 类型：behavior + contract + architecture + docs
- 领域：chip/board aggregation/review 发布指针与血统、ProductReadiness 九节点与治理、
  Admin 盘后工作台、竞价锚点编排生命周期、文档与运行手册收口
- 关联 PRD：`ref/instruction.md`、`ref/next.md`（EPIC-08，Commit D–J）
- 关联 Maps：`docs/maps/30-after-close.md`（§13）、`docs/maps/70-review.md`（§26）、
  `docs/maps/75-auction-analysis.md`（§10）、`docs/maps/80-system-runtime.md`（V2.1 状态）
- 基线：`2267d43`（dev HEAD）；最终 SHA：`6f008ca`（Commit I 收口）

> 本轮为**代码开发阶段**，不是集成、部署或真实数据验收阶段。用户指令覆盖旧计划
> "PG 通过后才继续 D–J" 条款：`pg_tested = false`、`pg_gate = deferred`、
> `continue_development = true`。Migration 与 PG 测试文件正常编写，但标记
> `authored_not_executed`，不阻塞开发。

## 1. 背景

`ref/next.md` 规划 Commit D–J 的 V2.1 模型合同收口。前序 Commit A/B/C 已完成（provider
硬化、Compute Once、DSA projection 对账）。本 CHANGE 记录 Commit D–I 的实现与 Commit J
的文档收口。

## 2. 修改内容

### 2.1 Commit D：chip 正式发布指针与血统

- `factor_publication_service.publish_chip_consensus`：`ChipConsensusRun` 达可发布终态
  （`succeeded`/`partial`）后原子写入 `PUBLICATION_KIND_CHIP_CONSENSUS` 发布指针。
- 严格 lineage：chip_run 存在 → trade_date 匹配 → status 可发布 →
  当日已发布 `stock_core` pointer 存在 → `source_core_run_id` 与已发布 pointer 一致。
- coverage 由 DB 统计（`chip_run.coverage_ratio`），不接受调用方任意传值。
- 重复发布 `on_conflict_do_update` 幂等；失败只重试指针，不重算 DSA/SMC/momentum。
- 测试：`test_chip_publication_unit.py`（8 项，PURE_UNIT_TEST）。

### 2.2 Commit E：Board Aggregation 发布指针合同

- `test_board_aggregation_publication_unit.py`（12 项）：基于精确 stock_core publication、
  同一 Board Facts taxonomy/member version、industry L1/L2/L3 与 concept 分开、exact lineage、
  缺板块/stale/partial/reuse 路径、aggregation publication/pointer。

### 2.3 Commit F：Review V2.1 依赖与血统

- `test_review_v21_dependency_contract.py`：Review 只依赖 `stock_core` + `market_aggregation`
  两个正式 publication pointer；不等待 chip、不等待 auction；创建阶段禁止查询其他 kind；
  exact lineage（board run 与 stock_core pointer 同源/同日/succeeded）；consumer 只读发布结果。
- 测试改服务级 `get_published_review_run_id` 消费，避免 API 层 Redis 依赖。

### 2.4 Commit G：ProductReadiness 九节点与治理

- `ProductReadinessService.collect_states`：九节点
  （daily_facts/board_facts/stock_core/board_aggregation/review mandatory +
  dsa_projection/chip/state_events/auction_anchor enhancement）。
- `evaluate_closure`：pending/blocked/core_ready/degraded_ready/fully_ready；
  terminal 与 consumable 分离；以 stock_core 为轴心分阶段判定。
- `evaluate_governance`：pointer lineage / stale children / unmatched active children /
  ready/pending/blocked/unavailable 分组 / degraded reasons。
- Admin readiness API：`GET /v1/admin/readiness/{trade_date}`（`require_roles("admin")`）。
- 测试：`test_governance_report_unit.py`（161 行，PURE_UNIT_TEST）。

### 2.5 Commit H：前端 Admin 盘后工作台

- `AdminReadinessWorkbench.tsx` 挂载于 `AdminDataProductionPage`「数据生产中心」总览。
- 展示九节点状态、run/publication/pointer/coverage/reason、loading/empty/degraded/failed/stale。
- 用户侧只消费正式 publication。
- 前端合同测试 `adminReadinessWorkbench.test.mjs` + tsc + eslint + build。

### 2.6 Commit I：Synthetic E2E 与质量门

- 纯逻辑 E2E `test_v21_synthetic_e2e_pure.py`：`SyntheticAuctionRepository` 内存模拟
  anchor batch transitions（structure_only→hybrid→composite、晚到 chip、failure matrix、
  retry 幂等、performance instrumentation）。
- PG 依赖 `test_v21_synthetic_e2e_pg.py`：`status = authored_not_executed`、
  `reason = pg_gate_deferred_during_development`。

### 2.7 Commit J：文档与运行手册

- 更新 Maps：`30-after-close.md` §13、`70-review.md` §26、`75-auction-analysis.md` §10、
  `80-system-runtime.md` V2.1 状态。
- 新增本 CHANGE 并更新 `docs/changes/INDEX.md`。
- Acceptance Matrix：`PRD-Acceptance-Matrix-V2.1-D-J-2026-08-05.md`。
- Runbooks 更新：`after-close-recovery.md`、`development-deployment.md`。
- 记录 Migration 085 与 PG deferred 状态。

## 3. 完成状态（如实区分）

| 范围 | 状态 |
|---|---|
| Commit D chip 发布指针 | implemented + remote_static_verified + remote_unit_verified |
| Commit E board aggregation 发布合同 | implemented + remote_static_verified + remote_unit_verified |
| Commit F review 依赖与血统 | implemented + remote_static_verified + remote_unit_verified |
| Commit G nine-node readiness + governance + admin API | implemented + remote_static_verified + remote_unit_verified |
| Commit H Admin 工作台前端 | implemented + frontend_build_verified |
| Commit I Synthetic E2E（纯逻辑） | implemented + remote_static_verified + remote_unit_verified |
| Commit I PG 依赖部分 | authored_not_executed（pg_gate_deferred_during_development） |
| Commit J 文档/Runbook/Acceptance Matrix | implemented |
| Migration 085 | authored（未 apply） |
| 部署 | 未执行（deployment_pending） |
| 真实数据闭环 | 未执行（data_validation_pending） |
| 浏览器验收 | 未执行（browser_pending） |

## 4. 验证

- 远程安全门禁（授权范围内）：Ruff、改动文件 Mypy、PURE_UNIT_TEST、静态合同/架构检查、
  前端 tsc / ESLint / build。
- 本地：仅代码/git 操作；本地未运行 Ruff/Mypy/pytest/build/Migration/数据库/部署/浏览器。
- PG 集成测试未执行（`pg_tested = false`，PG gate deferred），不视为开发失败。
- 文档一致性：按 `tools/check_docs_consistency.py` 要求保证链接/占位符/CHANGE 引用一致。

## 5. 剩余风险与未验证项

- PG 集成、Migration 085 apply、真实全市场任务、生产部署、浏览器验收均未执行。
- Chip/board/review 的落库全链路（items 批量 upsert → publication 指针切换）需授权后
  在 PostgreSQL 验证。
- 生产不能称为 fully_ready；`production_fully_ready = false`。

## 6. 回滚

- 本轮为纯代码/文档开发，未部署、未 apply Migration、未连接生产数据库。
- 如需回滚提交，目标 SHA 全部在 `dev` 分支，可精确 reset 到 `2267d43` 基线；
  无数据/运行时副作用。