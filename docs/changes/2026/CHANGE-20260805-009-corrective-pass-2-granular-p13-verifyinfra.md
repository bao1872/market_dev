# CHANGE-20260805-009 — Corrective Pass 2：granular restart 确定性缺陷 + P1-3 补强 + 验证栈可部署化

日期：2026-08-05
类型：behavior+contract+infra
领域：granular restart / P1-3 readiness / 验证部署基础设施
关联前序：CHANGE-20260805-008（granular restart 初版，本 Change 修正其过度声明）、CHANGE-20260805-007（Phase 0）
关联代码：`backend/app/services/granular_restart_service.py`、`backend/app/api/admin_after_close.py`、`backend/app/services/product_readiness_service.py`、`docker-compose.verify.yml`、`scripts/deploy/panji-verify-deploy.sh`、`scripts/ops/panji-verify-deploy`、`market.verify.env.example`

## 背景（用户审查指出 CHANGE-008 的确定性缺陷）

用户审查结论：Phase 1 提交存在但实现未达 PRD，存在确定性代码缺陷；Phase 3 验证栈不可部署。
明确停止原因应为"当前 Phase 1/3 代码存在确定性缺陷，必须先修复"，而非重新授权。

### Phase 1 缺陷（P0-1~P0-5）
- **P0-1**：`state_events` 无真实 publisher，却靠 `is_implemented_boundary = boundary in ALL_BOUNDARIES` 误判为"已实现"（枚举即实现，违反禁止伪造成功）。
- **P0-2**：`_find_source_run_id` 用 `table.c.id`（ORM Model 无 `.c` 属性），首次真实调用必 `AttributeError`。
- **P0-3**：子产品 publisher 成功后只写事件，child 仍 `queued`，污染 Scheduler 聚合/Readiness/watchdog。
- **P0-4**：`run_key` 固定但无幂等复用/查询已有 child，重复点击可能唯一键冲突或产生重复任务。
- **P0-5**：各 publisher 签名未经验证（本地无依赖未跑测试），不能接受"10 个全部真实落地"结论。

### P1-3 缺陷
仅实现 `matched/total >= 1.0`，未验证 `matched_count == eligible_count`（精确 eligible universe）、`algorithm_version`/`parameter_hash`/`source_core_run_id` 一致性、state events 完整生命周期。

### Phase 3 缺陷（P0-6~P0-10）
- **P0-6**：Redis 矛盾（建 verify-redis 却默认连 trading-redis/1）。→ 方案 B 独立 verify-redis。
- **P0-7**：验证 compose 未接正式 Postgres network，`trading-postgres` 不可解析。→ 加 external network `market-dev-default`。
- **P0-8**：远程脚本内部再调 `panji-prod-ssh`（自环）+ 用 `/root/web_dev` 而非 `/root/web_dev_verify`。→ 本地控制/远程实现分离。
- **P0-9**：探针用 `app.__version__` + `/health`，不符既有合同 `/v1/version.runtime_git_sha` + `/v1/health` + `/v1/health/ready`。→ 修正。
- **P0-10**：验证库 SHA 失配（库名 773f827 但 HEAD 8b1e4a3）。→ 已删除失配库，待最终 SHA 重建。

## 修改内容

### 1. granular_restart_service.py（P0-1~P0-5 修复）
- **P0-1**：删 `is_implemented_boundary = boundary in ALL_BOUNDARIES`；改为 `_REAL_HANDLERS` registry（仅含主链4 + 子产品5真实 handler）。`state_events` 明确不计入 → `is_implemented_boundary("state_events")=False`（诚实未实现）。`dispatch_restart` 对无 handler boundary 抛 `NotImplementedError`。
- **P0-2**：`_find_source_run_id` 改用真实 Model 字段（`model.id`/`model.trade_date`/`model.status`），移除 `.c`。
- **P0-3**：子产品 publisher 成功后 `child.status="succeeded"` + `finished_at=now`；失败 `failed` + `finished_at` + 错误事件。状态机 `queued→running→succeeded/failed`。
- **P0-4**：`_create_or_reuse_child` 按 `run_key` 查询已有 child，存在则复用（failed 重置 queued，其余复用不新建），避免唯一键冲突/重复任务。
- **P0-5**：各 `_publish_*` 用真实签名调用已确认 publish 函数；API 层捕获 `NotImplementedError`→`admin_error`、`ValueError`→`admin_bad_request`（不 501、不伪造成功）。

### 2. product_readiness_service.py（P1-3 补强）
- `_dsa_projection_state`/`_state_events_state`：保留 coverage 门槛（消除存在性检查），lineage 暴露真实可查的 `algorithm_versions`、`eligible_count`（代理=归属当前 core run 的 snapshot/event 总数）、`matched_count`、`coverage_ratio`、`p1_3_exact_completeness=partial/not_complete`。
- 明确注释：精确 eligible universe 比对、parameter_hash 一致性、state events 完整生命周期验证待 Phase 4 在验证库补全（不伪称 complete）。

### 3. 验证基础设施（P0-6~P0-10 修复）
- `docker-compose.verify.yml`：方案 B（独立 verify-redis，`REDIS_URL=redis://verify-redis:6379/0`，不暴露宿主机端口）+ external network `market-dev-default`（接入 trading-postgres）。
- `scripts/deploy/panji-verify-deploy.sh`：改为远程实现脚本（在 `/root/web_dev_verify` 内执行，不调 panji-prod-ssh）；compose 静态验证；探针改 `/v1/version.runtime_git_sha` + `/v1/health` + `/v1/health/ready`；运行时 SHA 校验。
- `scripts/ops/panji-verify-deploy`：本地控制（preflight + panji-prod-ssh 远程执行验证目录脚本），不再自环。
- `market.verify.env.example`：REDIS_URL 改方案 B。
- 失配验证库 `bz_stock_verify_773f827...` 已远程删除（P0-10 已执行）。

### 4. 测试（test_granular_restart_service.py 重写）
- `is_implemented_boundary` 以真实 handler 为权威（state_events=False）。
- 子产品成功后 child.status==succeeded + finished_at 有值。
- 幂等：同 run_key 复用。
- state_events dispatch 抛 NotImplementedError。
- 主链设置正确 last_completed_step。

## 门禁结果（本地，无 PG）
- `py_compile`：PASS（granular_restart_service / product_readiness_service / admin_after_close / test 全过）。
- lint：0 诊断。
- ruff：远程未安装，CI 门禁留待执行。
- pytest：本地无依赖（fastapi 未装），新增测试待远程/CI 跑（PURE_UNIT_TEST）。
- compose 静态验证（`docker compose config`）待 Phase 3 在远程执行。

## 诚实状态（修正 CHANGE-008 过度声明）
- `granular_restart_contract = implemented_skeleton`（主链4 + 子产品5 有真实 handler；state_events 待补领域级重建入口）。
- `granular_restart_runtime_verified = false`（未跑 PG，publish 签名未经验证）。
- `granular_restart_complete = false`。
- `dsa_state_events_coverage_gate = partially_improved`（coverage 门槛已加，非存在性检查）。
- `p1_3_exact_completeness = not_complete`（eligible universe/parameter_hash/完整生命周期未全验证）。
- `verify_infra_ready = false`（compose 未静态验证、验证库未以最终 SHA 重建、未部署）。
- `code_ready = false`、`pg_tested = false`、`verify_deployed = false`、`manual_acceptance_ready = false`。

## 下一步（按用户 Step 1-5）
- Step 1：granular restart 已修（state_events 仍需补真实重建 handler 才能 complete）。
- Step 2：P1-3 核心字段已暴露，精确比对待 Phase 4 验证库。
- Step 3：验证栈可部署化已修（待最终 SHA 重建库 + 静态验证）。
- Step 4：Phase 2 前端待 Backend API 稳定后接入。
- Step 5：等 Backend/Frontend/验证栈代码全提交，以最终 SHA 创建 `bz_stock_verify_<final_sha>` 再走 Migration→Seed→PG E2E→部署→验收。
