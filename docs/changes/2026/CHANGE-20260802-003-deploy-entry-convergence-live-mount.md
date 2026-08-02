# CHANGE-20260802-003 部署执行入口收敛为两脚本 + CI 降级为手工诊断工具

| 项 | 值 |
|---|---|
| 日期 | 2026-08-02 |
| 类型 | architecture + ops + governance |
| 影响范围 | 部署执行脚本 / GitHub Actions workflow / 部署纪律规则 / Change ID 治理 |
| 业务代码 | **零改动**（backend/app、frontend/src、alembic 均未触碰） |
| 数据操作 | **零操作**（未连接生产、未执行部署、未改动数据库） |
| 前置提交 | `a2ce9b96d5e0f5328413a0f1f544c4ee92011f1d`（治理文档层收口） |

## 1. 为什么改

`a2ce9b9` 已在文档层确立「Live Mount 是唯一开发部署方式、CI 不是部署前置门禁、
dev-only 分支模型」，但**实际执行脚本与 workflow 仍停留在旧模型**，形成治理与实现的直接冲突：

1. **四个部署脚本并存**，职责重叠且互相矛盾：
   - `scripts/ops/panji-test-deploy`：脚本头明文写「禁止 Live Mount / 禁止叠加 docker-compose.live.yml」，与已确认治理相反；
   - `scripts/ops/panji-deploy-remote.sh`：经 stdin 拷贝到远端 `/tmp` 执行，非受版本控制的执行体；
   - `scripts/deploy_live_runtime.sh` / `scripts/sync_live_runtime.sh`：另一套独立的 Live Mount 实现，无 SHA 门禁、无回滚；
   - `scripts/deploy/panji-deploy.sh`：已正确实现 Live Mount 合同，但校验 `origin/main` 且保留镜像/Live 双模式。
2. **本地入口默认开启 `--allow-local-build`**（`PANJI_ALLOW_LOCAL_BUILD:-1`），
   使"普通代码变更不构建镜像"的规则在实际执行中失效。
3. **成功判据用短 SHA**：本地入口只比较 `runtime_git_sha` 前 7 位，且公网端点不可达时降级为 WARN 继续报成功。
4. **CI 仍由 push dev / PR main 自动触发**，与「CI 不是部署门禁、不进入默认开发闭环」冲突；
   `release.yml`（Release Gate + GHCR）、`nightly.yml`、`deploy-production.yml` 属已废止流程但文件仍在。
5. **Change ID 撞号**：两个文件同为 `CHANGE-20260802-002`；同时存在 6 处指向尚不存在的 `CHANGE-20260802-003` 的悬空引用。

## 2. 改了什么

### 2.1 部署执行入口收敛为两个文件

| 文件 | 角色 | 处置 |
|---|---|---|
| `scripts/ops/panji-test-deploy` | 本地唯一用户入口（瘦客户端） | **重写** |
| `scripts/deploy/panji-deploy.sh` | 服务器端唯一实现 | **重写** |
| `scripts/ops/panji-deploy-remote.sh` | 旧 stdin/tmp 执行体 | **删除** |
| `scripts/deploy_live_runtime.sh` | 旧并行 Live Mount 编排 | **删除** |
| `scripts/sync_live_runtime.sh` | 旧并行同步脚本 | **删除** |

**`panji-test-deploy`（本地）职责收窄为三件事**：校验目标 SHA 是 `origin/dev` 祖先 →
运行 `panji-prod-preflight` → SSH 调用**服务器仓库内**的 `scripts/deploy/panji-deploy.sh`。
不再把脚本经 stdin 拷到 `/tmp`；不再提供任何模式/构建开关；
SSH 前对服务器端脚本执行 `bash -n` 静态预检。

**`panji-deploy.sh`（服务器）承担全部部署实现**：
`fetch → validate SHA → checkout → 变更分类 → 环境镜像 build（仅必要）→ rsync 到 /opt/panji-live →
migration（仅 migration 变更）→ 重启 → 健康与 SHA 核验 → 状态记录 → 失败回滚`。

### 2.2 删除双模式，只保留 Live Mount

| 已删除 | 说明 |
|---|---|
| `COMPOSE_CMD_NO_LIVE` | 不叠加 live.yml 的 Compose 变体 |
| `PANJI_FORCE_IMAGE_BUILD` / `FORCE_IMAGE_BUILD` | 强制镜像构建开关 |
| `DEPLOYMENT_MODE=image` | 镜像部署模式 |
| `--allow-local-build` | 本地入口的构建开关 |
| Registry / pull-only / Release Gate 相关逻辑 | 已废止发布流程残留 |
| `repo = image = runtime` 三/四重一致合同 | 镜像 tag 不再是运行代码来源 |
| 短 SHA 成功判据 | 改为完整 SHA 全等 |

唯一 Compose 组合固定为
`docker compose --env-file <env> -f docker-compose.prod.yml -f docker-compose.live.yml`，
`DEPLOYMENT_MODE` 恒为 `live`。即使因依赖/Dockerfile 变化重建镜像，
重建后仍以 prod+live 叠加启动，运行代码唯一来自 `/opt/panji-live`。

### 2.3 部署来源改为 dev

`validate_sha` 由 `git fetch origin main` + `merge-base --is-ancestor origin/main`
改为 `git fetch origin dev` + `merge-base --is-ancestor origin/dev`；
部署结束不再 `git checkout main`。

### 2.4 变更范围判定改为「上一部署 SHA → 目标 SHA」

`classify_changes` 使用 `git diff --name-only "${PREVIOUS_SHA}" "${TARGET_SHA}"`，
`PREVIOUS_SHA` 读自 `/etc/market-dev/.panji-deploy-state`。
**不使用 `HEAD~1`**——一次部署可能跨多个 commit，`HEAD~1` 会漏判。
无上一部署记录或该 SHA 不可解析时，按首次部署处理（全量同步 + migration）。

输出 6 个标志：

| 标志 | 触发路径 | 后果 |
|---|---|---|
| `backend_runtime_changed` | `backend/app/`、`backend/alembic/`、`backend/alembic.ini` | rsync backend + 重启全部 Python 服务 |
| `frontend_runtime_changed` | `frontend/src/`、`public/`、`index.html`、`vite.config`、`tsconfig` | vite build + rsync dist + 重启 frontend |
| `migration_changed` | `backend/alembic/versions/` | 执行 `alembic upgrade head` |
| `backend_environment_changed` | `backend/Dockerfile`、`pyproject.toml`、依赖锁 | build backend 镜像 |
| `frontend_environment_changed` | `frontend/Dockerfile`、`package.json`、`nginx.conf`、entrypoint | build frontend 镜像 |
| `capture_environment_changed` | `backend/Dockerfile.capture` | build worker-capture 镜像 |

backend 运行代码变化时重启**全部 11 个 Python 服务**（backend + 10 个 worker），
不得只重启 backend。`postgres` / `redis` / `umami` **永不参与重启**。

### 2.5 部署成功判据（全部基于完整 SHA）

服务器端 `verify_deployment` 必须**全部**满足，任一不符即回滚：

1. `git -C /root/web_dev rev-parse HEAD` = 目标完整 SHA；
2. `/opt/panji-live/RUNTIME_SHA` = 目标完整 SHA；
3. `GET :8000/v1/health` 与 `:8000/v1/health/ready` 返回 200；
4. `GET :8000/v1/version` 的 `runtime_git_sha` = 目标完整 SHA；
5. 同端点 `deployment_mode` = `live`；
6. `trading-backend` 容器 Mounts 包含 `/opt/panji-live`（前端变更时另验 `trading-frontend`）；
7. 关键容器运行中，三个 Scheduler 各恰好 1 实例。

服务器实现通过内部版本端点完成完整 SHA 与运行模式核验；本地入口不绕过
`panji-prod-ssh` 直接访问原始 IP。SSH 返回码透传服务器端全部验证结果。

### 2.6 端点合同（后端零改动）

经代码核对，后端已满足合同，本轮**未修改任何后端代码**：

| 层 | 路径 | 说明 |
|---|---|---|
| 容器内直连 | `/v1/health`、`/v1/health/ready`、`/v1/version` | router `prefix="/v1"` |
| 公网经 Nginx | `/api/v1/health`、`/api/v1/health/ready`、`/api/v1/version` | `/api/` 前缀被剥离 |

`/v1/version` 已返回 `runtime_git_sha`（读 `/opt/panji-live/RUNTIME_SHA`）与 `deployment_mode`。
此前报告中的「`/version` Not Found」为探测路径错误（探了 `:8000/version`），非后端缺陷。

### 2.7 Workflow 收敛

| 文件 | 处置 |
|---|---|
| `.github/workflows/ci.yml` | 触发方式改为**仅** `workflow_dispatch`；移除 `push: dev` 与 `pull_request: main` |
| `.github/workflows/release.yml` | **删除**（Release Gate + GHCR 已废止） |
| `.github/workflows/nightly.yml` | **删除**（定时全量回归已废止） |
| `.github/workflows/deploy-production.yml` | **删除**（自动部署已废止） |

CI 保留为**手工诊断工具**，不是部署前置门禁，也不进入默认开发闭环。
job 内容与 `CI` / `CI Gate` 名称未变，仅改触发方式。

### 2.8 Change ID 治理修复

| 问题 | 处置 |
|---|---|
| 两个文件同为 `CHANGE-20260802-002` | CI/部署那个重编号为 `CHANGE-20260802-004`；竞价/邀请码保留 002（先落库者保留原号） |
| 6 处指向不存在的 `CHANGE-20260802-003` | 本文件创建后引用全部可解析 |
| `CHANGE-20260801-002` 曾有两个文件 | 状态表重编号为 `CHANGE-20260801-003`，候选版本保留 002 |

### 2.9 规则与测试

- `rules/80-deployment-data-safety.md`：
  - **DS-90** 由「远程逻辑必须是真实脚本」扩写为「部署执行入口只有两个文件」，
    明确列出已删除且禁止恢复的三个脚本，并禁止 heredoc/stdin//tmp 执行；
  - **DS-91** 由「禁止按变更文件推断部署范围」改写为
    「变更范围判定必须基于上一部署 SHA → 目标 SHA，禁止 `HEAD~1`」，
    并保留有状态服务排除与完整 SHA 判据。
    *修订理由*：原 DS-91 要求"一次性重建全部无状态服务"是针对镜像部署事故的补偿措施；
    在 Live Mount 下运行代码由 rsync 统一同步，事故根因（推断错误导致部分服务停留旧 SHA）
    已由"backend 变化即重启全部 Python 服务 + 逐项完整 SHA 核验 + 失败回滚"覆盖。
- `scripts/deploy/panji-deploy.test.sh`：两脚本结构静态契约测试（34 项）；
- `scripts/ops/test-panji-test-deploy-contracts.sh`：直接执行真实服务器实现的 dry-run 合同测试（9 项）；
- `tools/check_governance_rules.py`：单一治理入口同时扫描规则、文档、实际脚本、workflow、
  Runbook 与 Change ID，并由正式测试覆盖合法样例和违规注入。

## 3. 受影响契约

| 契约 | 变化 |
|---|---|
| 部署命令 | `scripts/ops/panji-test-deploy <FULL_SHA> [--dry-run]`，只接受完整 SHA，删除所有模式/构建开关 |
| 部署来源分支 | `origin/main` → `origin/dev` |
| 部署成功判据 | 短 SHA 前缀匹配 + 端点不可达降级 WARN → 完整 SHA 全等 + 不可达即失败 |
| 运行模式 | image / live 双模式 → 仅 live |
| CI 触发 | push dev / PR main / 手动 → 仅手动 |
| Change ID | `CHANGE-20260802-002`（CI/部署）→ `CHANGE-20260802-004` |

## 4. 验证

本轮**未执行真实部署、未连接生产服务器、未改动数据库**。

| 项 | 方式 | 结果 |
|---|---|---|
| 保留 shell 语法 | `bash -n` | 通过 |
| 部署结构契约 | `bash scripts/deploy/panji-deploy.test.sh` | 34 通过 / 0 失败 |
| 真实实现 dry-run 合同 | `bash scripts/ops/test-panji-test-deploy-contracts.sh` | 第二轮 9 通过 / 0 失败 |
| Compose 叠加可解析 | `docker compose ... config --quiet` | 本机无 Docker CLI，留待手工 CI/目标环境验证 |
| Workflow YAML | Python `yaml.safe_load` | 通过，12 jobs |
| 治理检查器 | `tools/check_governance_rules.py` | 通过 |
| 检查器负向有效性 | 5 类违规注入 | 5 项均被拒绝；正例在移除悬空自指后由检查器通过 |

## 5. 已知遗留

| 项 | 状态 | 说明 |
|---|---|---|
| 真实部署验证 | `deferred_with_reason` | 本轮未获生产部署授权；新脚本尚未在 panji-prod 实跑，首次实跑会先 dry-run |
| Compose 叠加解析 | `blocked_external` | 本机无 Docker CLI；需由手工 CI 或目标环境执行 |
| 历史本地额外分支 | `deferred_with_reason` | 删除或改名属于破坏性仓库操作，本轮未获单独授权 |
| 历史 CHANGE 中的旧脚本引用 | 保留 | `CHANGE-20260729-005/009` 等历史文件提到 `deploy_live_runtime.sh`，属历史事实，不修改 |
