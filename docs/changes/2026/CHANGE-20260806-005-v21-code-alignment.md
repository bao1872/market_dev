# CHANGE-20260806-005 — V2.1 PRD 完全对齐代码收口

- 日期：2026-08-06
- 类型：behavior + contract + architecture + quality-gate
- 状态：`verified_code_pending_acceptance`
- 审计基线：origin/dev = `c2b436a63e236b6319aa41f472a6e29d93c4cddf`
- 目标 PRD：`docs/prd/10-market-data.md`、`docs/prd/20-quant-model.md`、`docs/prd/30-after-close.md`、`docs/prd/31-after-close-product-closure-v2.1.md`、`docs/prd/70-review.md`、`docs/prd/75-auction-analysis.md`、`docs/prd/80-system-runtime.md`

## 1. 目标

按《ref/开发计划.md》执行盘迹 V2.1 PRD 完全对齐开发计划（Phase 0–7）：使当前代码、数据库契约、运行编排、API/前端和验证证据完整符合已确认的 V2.1 PRD 体系。审计基线为 `c2b436a`（HEAD==origin/dev、工作树 clean、branch=dev）。

## 2. 修改范围

- 后端代码：`backend/app/services/**`、`backend/app/models/**`、`backend/app/domain_status.py`、`backend/alembic/versions/087_*.py`
- 前端代码：ProductReadiness 相关类型与页面
- 测试：`backend/tests/**`（含 `test_pg_*.py`、`test_migration_087_*`）
- 验证脚本：`scripts/verify/**`、`scripts/deploy/**`
- 配置：`docker-compose.verify.yml`、`market.verify.env.example`
- 唯一相关 Change：本文件

本次未修改 PRD、Maps、Runbooks、AGENTS.md、rules/** 或治理检查器。

## 3. 实现状态

Phase 1–5 的主要代码合同收口已在 CP4A Pass1 落地（详见 `docs/changes/2026/CP4A-PG-Acceptance-Matrix.md`），本任务按计划核验合同收口完整性并补齐剩余 P0/P1 缺口。诚实状态（截至 Phase 3 提交 08cf3dc 后继续推进，尚未完成 Phase 7 远程隔离验证）：

- `prd_code_alignment` = `implemented_partial`（Phase 1–5 代码在库，待核验补齐）
- `core_compute_once` = `implemented`（Phase 1：六类 kernel 独立计数在真实调用点、CoreRunContext 冻结 run_mode/source_cutoff/完整 config、artifact schema_version）
- `stock_core_atomic_publication` = `implemented`（Phase 2：唯一入口 publish_stock_core_atomically + 真实 fencing + coverage SSOT 修复 + partial unique 原子事务）
- `chip_run_level_15m` = `implemented_partial`（Phase 3：已新增运行级 refresh coordinator + 八个 canonical reason code + FUTURE_DATA/TIMESTAMP_INVALID；**剩余缺口**：每股 ChipConsensusRunItem 状态机接线、MDAS 15m 批读未实现，逐股仍 get_bars）
- `closure_six_states` = `implemented`（Phase 4：六态 closed、后端 DTO productionClosure/allProductsReady/unreconciledChildren、前端枚举/ViewModel/状态文案）
- `seed_real_producers_only` = `implemented`（Phase 5：PG 测试显式 pytest.mark.postgres、test_pg_100 kernel spy 修正为 canonical 主链、新增四类场景闭包硬断言 test_pg_seed_scenario_closures）
- `code_ready` = `false`
- `remote_pg_verified` = `false`
- `stable_deployed` = `false`
- `data_closed` = `false`
- `full_v2_1_closed` = `false`

> Phase 3 诚实说明：运行级 refresh（有界并发+每股超时+逐股 status）与八个 canonical reason code 已实现并经单测验证；`ChipConsensusRunItem` 模型存在但 executor 尚未接线每股状态机，MDAS 15m 批读仍未实现（保留逐股 get_bars + 冻结 cutoff 占位）。这两项列入后续补齐。

## 4. 已执行验证

- Phase 0 前置：HEAD==origin/dev==`c2b436a`、工作树 clean、branch=dev、merge-base==HEAD 全部通过。

## 5. 未执行验证

- Phase 6 本地全量门禁（PURE_UNIT 全量、Ruff、Mypy changed、compileall、架构、allowlist、治理、docs 一致性、git diff --check；前端 typecheck/lint/contract/build）
- Phase 7 远程隔离 PG 验证（新验证库 `bz_stock_verify_<target_sha>`、Migration 087 闭环、100 股 compute、原子 publication 故障注入、projection 生命周期、Seed 两次幂等、full synthetic E2E）
- 用户验收

## 6. 当前候选 SHA

- 计划执行起点：`c2b436a63e236b6319aa41f472a6e29d93c4cddf`
- **冻结 target_code_sha（Phase 6 本地门禁全过后）：`2299e7a7cbbf8c97682adccdd757ea94fa0d5a14`**（origin/dev，2026-08-06）
  - 候选资格依赖 Phase 7 远程隔离 PG 验证全过；门禁失败则返回本地修复生成新 SHA，原 SHA 自动失去候选资格。
  - 冻结后不得追加改变业务代码的提交。

## 7. 关闭前置

1. Phase 1–5 代码合同核验/补齐完成并按各 Phase 完成条件验收。
2. Phase 6 本地全量门禁全过，提交并推送 dev，冻结 `target_code_sha`。
3. Phase 7 远程隔离验证（新验证库、Migration 闭环、test_pg_*.py、Seed 幂等、full synthetic E2E）全部通过。
4. 用户验收后，本 Change 状态方可转为已确认闭环；在此之前保持 `verified_code_pending_acceptance`，不得自动修改 Maps/Runbooks。

## 8. 被 CHANGE-20260806-008 取代说明

本 Change 的「验证基础设施」部分（§2 中 `scripts/verify/**`、`scripts/deploy/**`、`docker-compose.verify.yml`、部分 `test_pg_*.py`）已由 **CHANGE-20260806-008**（V2.1 远程验证执行器与 synthetic 闭环）实现并取代。008 状态为 `implemented_local_pending_remote_verification`。

- 005 保持 `verified_code_pending_acceptance` 不变；其 Phase 1–5 业务代码合同收口仍有效。
- 008 完成 Phase 10 远程隔离验证且 005 业务验收通过后，再由用户授权统一关闭两者。
- 冻结 target_code_sha 以 008 提交生成的 SHA 为准（005 原 §6 冻结 SHA `2299e7a…` 已被 008 工作流取代，不作为候选）。
