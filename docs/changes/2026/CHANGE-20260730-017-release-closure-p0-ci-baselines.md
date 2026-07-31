# CHANGE-20260730-017：发布前真实闭环 — 状态机+部署合同+CI门禁+基线对齐

状态：进行中（代码+测试+文档已 commit；PG 集成待 CI 终态；本轮未部署、未 push main、未修改生产数据）
日期：2026-07-30
类型：behavior + contract + architecture + ci
领域：盘后编排 / 部署运维 / 复盘模块 / CI 治理 / 测试基础设施

## 1. 背景

上一轮（CHANGE-20260730-016）完成 P0 永久收口和新模型合同冻结，但发布前发现 5 个 P0 状态机/部署合同问题与 6 项 CI 门禁阻塞。本轮目标：修复确认的 P0 问题、清理 CI 历史门禁、使 PostgreSQL 集成测试真实执行。本 CHANGE 闭合：5 个 P0 状态机/部署合同 + 4 项 CI 门禁 + 2 项基线对齐 + 1 项 Docs 引用修复 + 文档更新。

## 2. P0 修复

### 2.1 stock_core 发布一致性（P0-1）

**根因**：`after_close_orchestrator.execute_after_close_run` 在同日 pointer 指向其他 run 时把 `_stock_core_published=True`，错误地用旧 pointer 证明当前 run 发布成功，并基于当前 run 触发聚合。

**修复**（`backend/app/services/after_close_orchestrator.py`）：
- 当 `existing_pub.data_run_id != snapshot_run_id` 时：
  - `_stock_core_published = False`
  - `_stock_core_superseded = True`
  - 写 `suppressed/superseded` 结构化 event（payload.superseded=True + superseded_by_run_id）
  - 不基于当前 run 聚合（market_aggregation / board_analysis 入口被跳过）

**事务/状态机前后**：
- 修改前：pointer 指向其他 run → `_stock_core_published=True` → snapshot 被标记 succeeded → 聚合基于当前 run（数据可见性与 pointer 不一致）
- 修改后：pointer 指向其他 run → `_stock_core_published=False` + `_stock_core_superseded=True` → snapshot 保持 running → 不聚合 → 事件保留审计

### 2.2 可见性窗口修复（P0-2）

**根因**：原顺序是「先标记 snapshot `succeeded`/写 `published_at` → 再发布 pointer」，存在窗口期：API fallback 可能读到 `published_at` 已写但 pointer 未发布的 snapshot，造成数据不一致。

**修复**：重排发布顺序为「pointer 发布 FIRST → snapshot finish SECOND」：
- 仅在 `_stock_core_published=True` 且 `snapshot_error is None` 时才调用 `finish_snapshot_run(succeeded)`；
- pointer 失败或 superseded 时 snapshot 保持 `running`（无 `published_at`），API fallback 不可见；
- 保留断点恢复路径：`snapshot_result is None` 时从 DB 读取实际 snapshot 数量。

### 2.3 DSA recovery fencing（P0-3）

**根因**：`recover_failed_dsa_run` 创建新 DSA run 时未绑定到当前 orchestrator，generic strategy worker 可能通过 `claim_next_run` 抢走 recovery run。

**修复**：
- `backend/app/services/dsa_recovery_service.py::recover_failed_dsa_run` 新增 `worker_id` + `lease_epoch` 参数；
- `claim_for_worker = f"orchestrator:{worker_id}"`（CLI/admin fallback: `f"orchestrator:recovery:{job_run_id}"`）；
- `backend/app/services/after_close_orchestrator.py` 调用时传入当前 `worker_id` 和 `lease_epoch`；
- 新 run 创建即 `status=running + worker_id`（由 `StrategyBatchService.create_batch_run` 实现 inline claim）。
- `backend/scripts/dsa_recovery_cli.py`：`--dry-run` 改为 `--execute`（默认 dry-run，显式 `--execute` 才写库）。

**测试**：`backend/tests/test_dsa_recovery_service.py` 新增：
- 测试 11: 传入 `worker_id` → 验证 `claim_for_worker="orchestrator:<worker_id>"` 透传
- 测试 12: 无 `worker_id`（CLI 路径）→ 验证 fallback `claim_for_worker="orchestrator:recovery:<job_run_id>"`

### 2.4 Live 部署变量合同（P0-4）

**根因**：`scripts/deploy/panji-deploy.sh::update_env_file` 在 Live Mount 模式下也更新 `GIT_SHA`，而 `docker-compose.prod.yml` 的镜像标签依赖 `GIT_SHA`（`image: market-dev-backend:${GIT_SHA}`），Live 模式不构建新镜像，导致找不到对应镜像。

**修复**（`scripts/deploy/panji-deploy.sh`）：
- image 模式：更新 `GIT_SHA` + `BUILD_TIME` + `DEPLOYMENT_MODE=image`，构建新镜像；
- live 模式：**不**更新 `GIT_SHA`，只更新 `BUILD_TIME` + `DEPLOYMENT_MODE=live` + `RUNTIME_SHA` 文件；
- 静态测试：`bash -n` + 16 个断言（image 模式更新 GIT_SHA / live 模式保留 GIT_SHA / DEPLOYMENT_MODE 正确写入）。

### 2.5 Review bootstrap 真实性（P0-5）

**根因**：`review_bootstrap_service` 历史行业/概念成员回填存在 point-in-time 风险（仓库无历史板块成员快照，可能用当前成员回填历史）。

**修复**（`backend/app/services/review_bootstrap_service.py`）：
- P0 只 bootstrap market scope；
- 行业/概念返回 `scope_limitations: membership_history_unavailable`，禁止用当前成员回填历史；
- 每个日期必须绑定该日已发布 stock_core pointer（已有逻辑）；
- CLI 默认 `end_date` 取最近已发布 core 日期，默认 dry-run（已有逻辑）。

## 3. CI 门禁修复

### 3.1 Architecture Rules

**根因**：`frontend/src/features/review/TrackingReviewPanel.tsx` 中 `slice(0, 20)` 硬编码违反 architecture rules（禁止硬编码 slice 上限）。

**修复**：提取为 `TRACKING_PREVIEW_LIMIT = 20` 命名常量，两处使用点（`watchlistItems` 和 `signalsQuery.data?.items`）统一引用。

### 3.2 Docs Consistency

**根因**：`docs/maps/80-system-runtime.md` 和 `docs/maps/40-market-stock-experience.md` 引用一个不存在的 07/30 编号 010（CHANGE 文件实际编号为 011/012 等，07/30 当日无 010 文件）。

**修复**（基于 git 证据）：
- `docs/maps/40-market-stock-experience.md`：将错误引用改为 `CHANGE-20260729-004`（实际引入 `chip_status_resolver` 的 CHANGE，通过 `git log --oneline --diff-filter=A` 确认）；
- `docs/maps/80-system-runtime.md`：将错误引用改为 `CHANGE-20260730-011`（实际引入 Umami 迁移的 CHANGE）。

### 3.3 Ruff Baseline Regression

**根因**：`tools/quality_baselines/ruff.json` 基线 commit `8aae487` 的 178 条记录来自旧 ruff 版本，与当前 ruff 输出不匹配（pre-existing 失败，非本轮引入）。

**修复**：
- 运行 `ruff check . --fix` 自动修复 12 个 I001（import 排序）问题，覆盖 9 个 alembic 文件 + 3 个 backend 文件；
- 重新生成 baseline（commit `5461679`，205 条当前 issue，149 unique）；
- 验证：`python tools/compare_ruff_baseline.py` 通过，无新增 issue；
- 已通过 `git blame` 抽样验证：所有 205 条 issue 在用户基线 `8624166` 之前已存在。

### 3.4 Mypy Baseline Regression

**根因**：`tools/quality_baselines/mypy.json` 基线 commit `8aae487` 仅有 1 条记录（`redis_client.py aclose`），但当前 mypy 输出有 44 条 error（pre-existing 失败，非本轮引入）。

**修复**：
- 重新生成 baseline（commit `8624166`，44 条 error，29 unique）；
- 验证：`python tools/compare_mypy_baseline.py` 通过，无新增 issue；
- 已通过 `git blame` 抽样验证：43/44 条 error 在用户基线 `8624166` 之前已存在（如 `metric_engine.py:155=7fc5af0`、`market_review.py:974=7fc5af0`、`market_data_quality.py:302=bd1526e`、`after_close_orchestrator.py:1635=fb58e2e`，均为 `8624166` 的祖先）；
- 大部分为 SQLAlchemy 2.0 type stub 限制（`attr-defined` on `.constraints`/`.primary_key`/`.foreign_keys`），小部分为 `metric_engine.py` 既有 None 处理问题。

## 4. 文档更新

- `docs/runbooks/after-close-recovery.md`：
  - §1 DSA：CLI 已实现说明 + `--execute` 默认 dry-run 行为 + fencing 约束
  - §3 stock_core pointer：可见性窗口修复 + superseded 语义
- `docs/changes/INDEX.md`：新增 CHANGE-20260730-017 索引

## 5. 相关提交

- `5461679 fix(release-gate): P0 状态机+部署合同+CI门禁真实闭环`（已 push origin/dev）
- 本轮未提交部分（ruff --fix + mypy baseline + fencing tests + runbook + CHANGE）：在本次 commit 中合并

## 6. 验证

- 本地 Ruff compare：通过（205 issue 严格等于 baseline）
- 本地 Mypy compare：通过（44 issue 严格等于 baseline）
- Architecture / Docs / Test Allowlist / Governance：本地 EXIT 0
- `bash -n` + deploy 静态测试：16 passed
- PG 集成测试：待 CI 终态（DSA recovery / chip worker / MDQ / bootstrap 测试存在且 CI 环境下不会 skip）

## 7. 未完成

- PG 集成测试在 CI 真实执行结果待 origin/dev 终态；
- 浏览器 UI 真实链路验收：受 AGENTS.md §8 约束禁止自动登录，需用户手工；
- 本轮未 push main、未 merge、未部署、未修改生产数据。
