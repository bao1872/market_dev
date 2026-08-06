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

Phase 1–5 的主要代码合同收口已在 CP4A Pass1 落地（详见 `docs/changes/2026/CP4A-PG-Acceptance-Matrix.md`），本任务按计划核验合同收口完整性并补齐剩余 P0/P1 缺口。诚实状态（当前为计划执行起点，尚未完成 Phase 7 远程隔离验证）：

- `prd_code_alignment` = `implemented_partial`（Phase 1–5 代码在库，待核验补齐）
- `core_compute_once` = `implemented_partial`（需 Phase 1 核验）
- `stock_core_atomic_publication` = `implemented_partial`（需 Phase 2 核验）
- `chip_run_level_15m` = `implemented_partial`（需 Phase 3 补齐）
- `closure_six_states` = `implemented_partial`（需 Phase 4 补齐）
- `seed_real_producers_only` = `implemented_partial`（需 Phase 5 核验）
- `code_ready` = `false`
- `remote_pg_verified` = `false`
- `stable_deployed` = `false`
- `data_closed` = `false`
- `full_v2_1_closed` = `false`

## 4. 已执行验证

- Phase 0 前置：HEAD==origin/dev==`c2b436a`、工作树 clean、branch=dev、merge-base==HEAD 全部通过。

## 5. 未执行验证

- Phase 6 本地全量门禁（PURE_UNIT 全量、Ruff、Mypy changed、compileall、架构、allowlist、治理、docs 一致性、git diff --check；前端 typecheck/lint/contract/build）
- Phase 7 远程隔离 PG 验证（新验证库 `bz_stock_verify_<target_sha>`、Migration 087 闭环、100 股 compute、原子 publication 故障注入、projection 生命周期、Seed 两次幂等、full synthetic E2E）
- 用户验收

## 6. 当前候选 SHA

- 计划执行起点：`c2b436a63e236b6319aa41f472a6e29d93c4cddf`
- 目标候选 SHA：待 Phase 6 本地门禁通过、提交推送后冻结 `<40-char origin/dev SHA>`

## 7. 关闭前置

1. Phase 1–5 代码合同核验/补齐完成并按各 Phase 完成条件验收。
2. Phase 6 本地全量门禁全过，提交并推送 dev，冻结 `target_code_sha`。
3. Phase 7 远程隔离验证（新验证库、Migration 闭环、test_pg_*.py、Seed 幂等、full synthetic E2E）全部通过。
4. 用户验收后，本 Change 状态方可转为已确认闭环；在此之前保持 `verified_code_pending_acceptance`，不得自动修改 Maps/Runbooks。
