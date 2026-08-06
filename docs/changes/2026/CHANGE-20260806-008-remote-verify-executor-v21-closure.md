# CHANGE-20260806-008 — V2.1 远程验证执行器与 synthetic 闭环

- 日期：2026-08-06
- 类型：implementation + verification-infra + contract + quality-gate
- 状态：`implemented_local_pending_remote_verification`
- 审计基线：origin/dev = `26544de9958f43b07971fdf832489e5242db6578`
- 关联合同：CHANGE-20260806-007（同 SHA、自包含远程验证合同，治理层）
- 关联收口：CHANGE-20260806-005（V2.1 PRD 完全对齐代码收口，本 008 取代其验证基础设施部分）

## 1. 目标

将 CHANGE-20260806-007 规定的治理合同落地为可执行的验证基础设施：

- 一次性 `verify-test` 服务（复用 backend 镜像底座、只读挂载、--rm 自动删除），禁止运行时 pip install / /tmp 复制；
- `verify_attempt.py` attempt 身份模型 + 状态机 + finally 合同（证据导出 → 精确清理 → 清理校验）；
- `evidence_exporter.py` 统一证据模板（manifest/gates/pytest-report/logs/resources/cleanup/summary）；
- `cleanup_runner.py` 按 attempt manifest 精确清理 + 永久保护清单 + blocked_cleanup；
- 三个自包含 PG 测试（atomic / projection / 100-stock closures），不依赖 Seed；
- `seed_v21_verify_data.py` 重写为 100% synthetic，删除 bz_stock 读取路径（DS-112 合规）；
- 本地全量门禁（PURE_UNIT / Ruff / Mypy / compileall / shell+yaml 语法）全过。

本轮执行到**冻结 target_code_sha** 为止；单次远程验证尝试由后续独立授权执行。

## 2. 修改范围

新增：

- `scripts/ops/panji-verify-run` — 外层入口（SSH 与权限边界，调用远程 verify_attempt.py）；
- `scripts/verify/verify_attempt.py` — attempt 身份模型、状态机、finally 合同；
- `scripts/verify/evidence_exporter.py` — 证据导出；
- `scripts/verify/cleanup_runner.py` — 精确清理 + 保护清单 + blocked_cleanup；
- `backend/tests/test_verify_infra_safety.py` — 验证工具静态测试（7 passed，PURE_UNIT）。

修改：

- `docker-compose.verify.yml` — 新增一次性 `verify-test` 服务；
- `scripts/deploy/panji-verify-deploy.sh` — 导出 VERIFY_TEST 镜像变量与挂载路径；
- `scripts/verify/seed_v21_verify_data.py` — 默认 synthetic 100 只、删 bz_stock 路径、chip_partial 去硬编码、chip_full 走真实链（绕过运行级 refresh）；
- `backend/tests/test_pg_atomic_publication.py` — 自包含建 5 instruments / snapshot run / 5 StockFeatureSnapshot / lease；
- `backend/tests/test_pg_projection_lifecycle.py` — 自建 StrategyDefinition/Version/Run/RunItems/CoreArtifacts；修复 `CoreArtifactRepository` / `project_dsa_batch` 真实签名；
- `backend/tests/test_pg_seed_scenario_closures.py` — full_success 断言放宽至 (fully_ready / mandatory_ready_enhancing / degraded_ready)，记录 board_facts 门禁需全市场原始事实的诚实边界。

本次当时未修改 PRD、Maps、Runbooks、AGENTS.md、rules/** 或治理检查器。其“验证基础设施代码不属于治理层”的判断已由 CHANGE-20260806-009 纠正：远程验证框架现属于受保护治理变更域。

## 3. 关键实现决策

1. **verify-test 服务复用 backend 镜像底座 + 只读挂载**：compose `verify-test` 服务 `image: ${VERIFY_BACKEND_IMAGE}`，volumes 只读挂载 `backend/app`、`backend/tests`、`backend/pyproject.toml`、`backend/pytest.ini`、`backend/alembic`、`backend/alembic.ini`、`scripts/verify`、`RUNTIME_SHA`；`command: pytest -m postgres <目标>`；`--rm` 自动删除。复用 `panji-verify-deploy.sh` 的镜像底座探测逻辑。

2. **自包含 PG 测试**：atomic 测试内自建 5 instruments + snapshot run + 5 StockFeatureSnapshot（source_run_id 一致）+ Scheduler/domain lease（消除对 Seed 依赖，匹配 `validate_quality_gate` 真实查 StockFeatureSnapshot）；projection 测试内自建 StrategyDefinition + StrategyVersion + StrategyRun + StrategyRunItems + CoreArtifacts（消除对 dsa_selector 预置依赖）；100-stock 测试依赖 synthetic Seed 提供 100 只，五 kernel 断言已就位。

3. **100% Synthetic Seed**：确定性生成 100 instruments / 120 交易日 1d bars / 1h bars / ≥500 根 15m（每完整交易日 16 根、末根 15:00）/ released dsa_selector config / calendar / board+auction raw facts / PIT membership；完全删除 `--bz-db-url` 与 bz_stock 复制路径；符合 DS-112。

4. **chip 真实链（synthetic 边界，如实标注）**：Seed chip 场景调用真实 `create_after_close_chip_consensus_job` + `execute_after_close_chip_consensus`（真实算法 + RunItem 生命周期），但**绕过 `execute_after_close_chip_consensus` 顶层的 `refresh_15m_batch`（联网 pytdx）**——synthetic 15m bars 已由 Seed 直接注入验证库，`_fetch_chip_bars(skip_refresh=True)` 已支持只读已有 bars。不修改 `execute_after_close_chip_consensus` 顶层签名（最小必要）。`chip_partial` 通过注入部分标的 15m 数据不足/缺失使真实统计自然 partial，删除硬编码 5/3/2/10 计数。

5. **evidence/cleanup 执行器**：`verify_attempt.py` 的 try/except/finally 等价于用户计划 §16 的 finally 合同；cleanup 基于 attempt manifest 精确删除，永久保护清单硬编码拒绝 `bz_stock` / `postgres` / `template*` / 共享 Volume / `trading-*` 容器 / 基础镜像 / 来源不明资源；禁止全局 prune 与模糊 DB drop；清理失败标记 blocked_cleanup 并停止新建资源。

## 4. 业务代码收口复核

由 backend-architect 子代理对 Phase 5–8 业务代码（Core/Pub/DSA/Chip/Closure/Board/Review/Auction/API/前端）做只读复核，结论：**无必须修复的业务 bug**。本轮发现并已修复的签名问题（`CoreArtifactRepository` 构造器、`project_dsa_batch` 缺 `trade_date` / `strategy_version_id`）属于测试 fixtures 对齐真实服务签名，已在 `test_pg_projection_lifecycle.py` 修正。

## 5. 已执行验证（本地）

- `PURE_UNIT_TEST=1` 全量纯单元测试：107 passed（含新增 test_verify_infra_safety 7 passed），无回归；
- ruff（改动 .py 文件）：All checks passed!
- mypy（改动脚本）：Success: no issues found in 5 source files；
- compileall：无语法错误；
- `docker-compose.verify.yml` YAML 解析 OK；
- `bash -n` 校验 `panji-verify-run` 与 `panji-verify-deploy.sh` 语法 OK。

## 6. 未执行验证（需后续独立授权）

- Phase 10 单次远程验证尝试：新验证库 `bz_stock_verify_<target_sha>`（DS-110）、Migration 087 round-trip、`verify-test` 服务跑 test_pg_*.py、Seed 两次幂等、full synthetic E2E、evidence 导出、强制清理 + 清理校验。
- 用户验收。

## 7. 当前候选 SHA

- 计划执行起点：origin/dev = `26544de9958f43b07971fdf832489e5242db6578`
- **冻结 target_code_sha（本地全量门禁全过后，由本任务提交并推送 dev 生成新 SHA）**：`99c1d0aaf43f014c3b6f9b667e79e16da2a333b8`（已推送 origin/dev）。
  - 候选资格依赖 Phase 10 远程隔离 PG 验证全过；门禁失败则返回本地修复生成新 SHA，原 SHA 自动失去候选资格。
  - 冻结后不得追加改变业务代码的提交。

## 8. 关闭前置

1. 本地全量门禁全过，提交并推送 dev，冻结 `target_code_sha`。
2. Phase 10 远程隔离验证（新验证库、Migration 闭环、test_pg_*.py、Seed 幂等、full synthetic E2E）全部通过。
3. 用户验收后，本 Change 状态方可转为已确认闭环；在此之前保持 `implemented_local_pending_remote_verification`，不得自动修改 Maps/Runbooks。

## 9. 005 取代说明

CHANGE-20260806-005 的「验证基础设施」部分（§2 中 `scripts/verify/**`、`scripts/deploy/**`、`docker-compose.verify.yml`、部分 `test_pg_*.py`）由本 Change（008）实现并取代。005 保持 `verified_code_pending_acceptance` 状态不变，其业务代码收口（Phase 1–5 合同）仍有效；后续若 008 远程验证通过、005 业务验收完成，再由用户授权统一关闭。
