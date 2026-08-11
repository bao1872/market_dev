# CHANGE-20260811-002 — Compose 运行配置变更必须驱动应用容器重建（部署对齐缺陷修复）

- **类型**：deployment-contract fix（运行期对齐）
- **领域**：部署 / panji-deploy.sh 变更分类与重启决策
- **状态**：`implemented_unconfirmed`（契约测试通过；未真实部署；未 recompute Review）
- **关联 PRD**：`prd/80-system-runtime.md`
- **关联 Maps**：`maps/80-system-runtime.md`（§容器资源预算现状，已同步 should vs actual）
- **No business logic change / No Review algorithm change / No Migration / No deploy this round**

## 1. 问题（FIRST_BLOCKER = COMPOSE_ONLY_RUNTIME_CONFIG_NOT_APPLIED_BY_DEPLOY）

CHANGE-20260811-001 将 `docker-compose.prod.yml` / `docker-compose.live.yml` 的 backend 与
after-close `mem_limit` 从 `1024m` 校准为 `4096m`。但远程审计发现官方部署分类器 **不分类**
这两个 Compose 文件：

- `classify_changes` 仅检查 `backend/(app|alembic)`、`frontend/`、`backend/alembic/versions/`、
  environment 文件（`Dockerfile` / `pyproject.toml` 等）、`backend/Dockerfile.capture`；
  完全遗漏 `docker-compose.prod.yml` / `docker-compose.live.yml`。
- 因此纯 Compose 变更会导致：`need_backend=false`、`need_frontend=false`、`restart_list=empty`，
  但 `RUNTIME_SHA` 仍会推进 —— **容器保持旧 cgroup 配置，新 Compose 配置未生效**。
- 该缺陷使 CHANGE-20260811-001 的 4096m 校准在部署后实际不生效，OOM blocker 未被真正解除。

## 2. 修正（最小必要，不扩大）

仅引入一个显式运行期分类 `COMPOSE_RUNTIME_CHANGED`，并重用既有重启路径。

### 2.1 `scripts/deploy/panji-deploy.sh`
1. `classify_changes`：新增分类
   ```bash
   if echo "${changed_files}" | grep -qE '^docker-compose\.(prod|live)\.yml$'; then
       COMPOSE_RUNTIME_CHANGED=true
   fi
   ```
2. `main()` 标志声明新增 `COMPOSE_RUNTIME_CHANGED=false`。
3. `_backend_runtime_will_mutate()`：OR 条件新增 `|| "${COMPOSE_RUNTIME_CHANGED}" == "true"`。
   → 已有 `guard_active_after_close_jobs` 调用此函数，故活跃盘后任务门禁会在 runtime mutation 前执行。
4. 重启装配（main 内联）：`COMPOSE_RUNTIME_CHANGED=true` 时
   `restart_list+=("${PYTHON_SERVICES[@]}")` + `restart_list+=(frontend)`，
   复用以有的 `restart_services`（`--force-recreate --no-build`）。
   - **不**置 `need_backend`/`need_frontend`（无代码同步）；
   - **不**置 `*_ENVIRONMENT_CHANGED`（STEP 5：不自动镜像构建）；
   - **不**置 `MIGRATION_CHANGED`（STEP 6：不自动 migration）；
   - 作用域仅 `PYTHON_SERVICES` + `frontend`；`postgres`/`redis`/`umami` 永不重启。

### 2.2 未改动（明确边界）
- **未改** `x-resource-app-heavy` 锚点（仍 1024m）→ strategy-batch / capture / 其他 heavy 不变。
- **未引入** 新部署框架 / 服务依赖图 / compose diff 解析器 / 新部署命令 / 新 worker / 新 config 服务。
- **未**创建通用资源框架；REVIEW-V2 运行时验收仅显式核验 `trading-backend` / `trading-worker-after-close`
  的 `HostConfig.Memory >= 4294967296`（STEP 8，仍为验收指令，不扩展部署架构）。
- `market.verify.env.example` 的 `PANJI_BACKEND_MEM_LIMIT=4096m` 维持（STEP 11：NONBLOCKING / NO-OP）。

### 2.3 `scripts/deploy/panji-deploy.test.sh`
新增 `== 9/9 COMPOSE_RUNTIME_CHANGED 部署契约（控制流） ==`，用 PATH 上的 `git` stub
（读取 `MOCK_DIFF_FILES` 环境变量）驱动 `classify_changes` 真实控制流，覆盖：

- CASE1 仅 prod compose 变化 → `COMPOSE_RUNTIME_CHANGED=true`
- CASE2 仅 live overlay 变化 → `COMPOSE_RUNTIME_CHANGED=true`
- CASE3 `COMPOSE_RUNTIME_CHANGED=true` → `_backend_runtime_will_mutate=true`
- CASE4 compose 变化重启分支挂载 `PYTHON_SERVICES` + `frontend`
- CASE5 compose 变化不置 `*_ENVIRONMENT_CHANGED`、不置 `MIGRATION_CHANGED`
- CASE6 仅 docs 变化 → `COMPOSE_RUNTIME_CHANGED=false`，无新重启/迁移行为

## 3. 验收状态

- `bash -n scripts/deploy/panji-deploy.sh` / `panji-deploy.test.sh`：通过。
- `bash scripts/deploy/panji-deploy.test.sh`：87 通过 / 2 失败。
  - 2 个失败为 **HEAD `80b1fb5` 既有 baseline 失败**（`构建使用完整 tag 组`、`禁止删除 node:20-alpine`，
    属过时测试断言，与本轮无关），**NEW_FAILURES = 0**。
- `git diff --check`：clean。
- **未真实部署、未 recompute Review、未发布、未 mutate 生产 DB**（STOP 规则）。

## 4. 后续（Deferred，需用户授权）

- 真实部署（`scripts/ops/panji-test-deploy`）后，按 STEP 8 验收核验
  `docker inspect trading-backend/worker-after-close` 的 `HostConfig.Memory >= 4294967296`。
- 部署后把 `maps/80-system-runtime.md` 的 backend / after-close 行从「repo 默认目标」更新为「已验证运行时 4096m」。
- CHANGE-20260811-001 与本文档状态可由 `verified_code_pending_acceptance` 推进至闭环。
