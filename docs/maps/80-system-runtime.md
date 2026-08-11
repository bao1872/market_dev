# 系统运行体系 Map

本 Map 只记录已由仓库或只读运行证据核验的当前事实。目标行为见
`docs/prd/80-system-runtime.md`，操作步骤见 `docs/runbooks/development-deployment.md`。

## 本地边界

| 项目 | 当前实现 |
|---|---|
| 代码目录 | `/Users/zhenbao/Desktop/coding/market_dev` |
| 默认分支 | `dev`，跟踪 `origin/dev` |
| Backend | `backend/.venv` + Uvicorn，入口 `backend/app/main.py:app` |
| Frontend | Node.js + Vite，配置入口 `frontend/vite.config.ts` |
| PostgreSQL | 不在本地启动；本地测试禁止连接任何数据库；旧 SSH Tunnel 业务库调试路径仍存在，但不再是正式测试路径 |
| Redis | 不在本地启动；开发连接使用隔离逻辑 DB，测试使用 mock |
| Worker/Scheduler | 本地不得启动远程常驻 Worker、Scheduler、盘后编排或全市场任务 |
| 测试 | `conftest.py` 只允许 `PURE_UNIT_TEST=1` 与远程 `PANJI_REMOTE_VERIFY_DB_TEST=1`；共享业务库 pytest 分支已删除 |

本地启动和隧道命令以 `docs/runbooks/local-development.md` 为准。本地进程隔离不能被
解释为测试数据隔离；任何连接共享开发业务数据库的写入都是真实业务写入。

## 远程开发运行身份

| 项目 | 当前事实 |
|---|---|
| SSH 配置别名 | `panji-prod` |
| 唯一 SSH 执行入口 | `scripts/ops/panji-prod-ssh` |
| 部署前检查 | `scripts/ops/panji-prod-preflight` |
| 服务器仓库 | `/root/web_dev` |
| 运行代码目录 | `/opt/panji-live` |
| 环境配置 | `/etc/market-dev/market.env` |
| Compose | `docker-compose.prod.yml` 与 `docker-compose.live.yml` 始终叠加 |
| 部署状态文件 | `/etc/market-dev/.panji-deploy-state`，保存上一成功部署完整 SHA |

原始 IP 和旧 SSH 别名不是允许的操作入口。远程开发运行身份的具体网络值由 SSH 配置和
`panji-prod-preflight` 校验，不在普通命令或报告中重复传播。

## Git 与 CI

- `dev` 是默认开发分支，也是手工 CI 与开发部署的唯一代码来源；
- `.github/workflows/ci.yml` 只接受 `workflow_dispatch`；可选 `base_sha` 用于范围分类，
  为空时使用目标 SHA 的第一父提交；
- CI 是诊断工具，不执行部署，也不是部署前置门禁；
- `main` 未经明确授权不得修改、合并或推送；隔离实验分支不得作为部署来源；
- 当前仓库中只有 `ci.yml` 一个 workflow。

## 部署调用图

```text
scripts/ops/panji-test-deploy <FULL_SHA> [--dry-run]
  -> scripts/ops/panji-prod-preflight
  -> scripts/ops/panji-prod-ssh
  -> /root/web_dev/scripts/deploy/panji-deploy.sh <FULL_SHA> [--dry-run]
  -> docker-compose.prod.yml + docker-compose.live.yml
  -> /opt/panji-live
  -> health / ready / version / mount / scheduler-singleton verification
```

## 远程验证框架

当前仓库的正式本地控制入口为 `scripts/ops/panji-verify`；旧
`scripts/ops/panji-verify-run` 仅委托给该入口。`full-closure` 计划由
`scripts/verify/plans/full-closure.json` 选择注册 profile，
`scripts/verify/verification_plan.py` 拒绝未知字段与未注册值。

远端编排位于 `scripts/verify/verify_attempt.py`：完整 SHA 派生精确数据库和 Compose project，
全局非阻塞锁阻止并发 attempt；数据库创建和删除都通过维护库连接；Migration 执行
upgrade/downgrade/upgrade/重复 upgrade；PG 由 Compose 的一次性 `verify-test` 运行；finally 导出
证据、精确清理并复检。`cleanup_runner.py` 不执行 volume/image/global prune，证据目录不随清理删除；
`evidence_exporter.py` 对日志和总证据设上限。以上为已通过本地合同测试和静态检查的代码事实，
尚未在远程 PostgreSQL 完整实跑，因此 `remote_verification_verified=false`。

正式入口只把 SHA 与登记计划交给 `run_remote_verification.sh`。后者调用
`prepare_verify_environment.py`，从既有容器身份生成仓库外、权限 `0600` 的单次环境文件并设置
trap 删除；数据库 URL 不进入 SSH 命令、进程参数、manifest 或 Git。建删库通过 PostgreSQL 容器，
Migration、PG、Seed 与 Synthetic E2E 通过一次性 `verify-test` 执行，不依赖宿主机 Python/PG 工具。
验证环境分别生成 asyncpg `DATABASE_URL` 与 psycopg `MIGRATION_DATABASE_URL`；Alembic 优先读取
后者，应用与 PG 测试继续使用前者。
`verify-test` 使用 `backend/Dockerfile` 的 `verification` target：继承运行镜像内容，但在构建阶段加入
锁定的 `.[dev]` 测试依赖，tag 为 `panji-verify-test:<完整SHA>`，attempt 结束后精确删除。

实现边界：

- 本地入口只校验来源、运行 preflight，并让服务器先自举到目标 SHA 工作树，
  再执行**目标工作树中**的 `scripts/deploy/panji-deploy.sh`。
  远端序列：`fetch origin dev → 工作树干净校验 → 祖先校验 → 记录原始 REF(SHA) →
  checkout -f --detach 目标 SHA → 执行部署实现`；dry-run 与失败经 `trap` 恢复原始 REF，
  正式部署成功后保持在目标 SHA。这解决了"服务器停在旧 SHA 时跑的是旧部署脚本"的问题；
- 服务器实现根据"上一真实运行 SHA 到目标 SHA"的完整差异分类；
  上一真实运行 SHA 解析**禁止**使用 checkout 后的 repo HEAD（否则 diff 为空、漏判 migration/环境变化）。
  已 Live Mount：状态文件 → `/opt/panji-live/RUNTIME_SHA` → `version.runtime_git_sha`
  → `PANJI_BOOTSTRAP_PREVIOUS_SHA`（外层自举前完整 SHA）；
  首次 Live Mount：当前运行 `trading-backend` `/v1/version` → 镜像 tag SHA
  → `PANJI_BOOTSTRAP_PREVIOUS_SHA` → 仍无法确认则停止并报告 `previous_runtime_sha_unknown`。
  仅全部失败（非首次未知基线）才强制全量同步 + migration，状态文件缺失本身不构成 migration 理由；
- 首次 Live Mount 部署由 `docker inspect` 判定（`trading-backend` / `trading-frontend`
  是否挂载 `/opt/panji-live`）。判定为首次时强制全量同步 Python 与前端运行代码以建立挂载，
  但**不会**据此设置 `migration_changed`；
- 普通 Backend 代码只同步 Live Mount，不构建镜像；
- 普通 Frontend 代码在服务器生成 `dist` 后同步，不构建镜像；
- backend / frontend / worker-capture 三个镜像共用同一 `GIT_SHA` tag。依赖、Dockerfile、
  系统依赖或必须烘焙的 Nginx 配置变化时，必须把三者作为**同一 tag 组整体构建**，
  不存在只构建其中一个的做法；
- 即使构建环境镜像，服务仍通过 prod + live 叠加运行，代码来源仍是 `/opt/panji-live`；
- migration 只在 migration 文件发生变化时执行，且**始终早于任何服务重启**；
  部署不自动执行 bootstrap、业务 run、publish、withdrawal 或其他业务数据动作；
- `/opt/panji-live/RUNTIME_SHA` 是单文件 bind mount 源，采用原地写入（保持 inode），
  写后校验 inode 未变并回读完整 SHA；
- 失败处理分两类：migration 失败（服务未重启）只恢复文件层并输出
  `migration_failed_requires_inspection`，不做任何容器重建、不声称数据库已回滚；
  服务已重启后的核验失败才执行容器级回滚；
- 部署后清理是有条件的：本轮未构建镜像则完全不清理；构建了镜像才执行
  `builder prune -f` 与 `image prune -f`。禁止 `-a` 级 prune、`system prune`、`volume prune`；
- PostgreSQL、Redis 和 Umami 不进入普通重启列表；禁止 `down -v`。

成功证据必须同时满足：服务器 repo HEAD、`/opt/panji-live/RUNTIME_SHA` 和版本接口的
`runtime_git_sha` 等于目标完整 SHA，`deployment_mode=live`，健康/就绪探针通过，
`trading-backend` 与 `trading-frontend` 挂载来源分别包含 `/opt/panji-live` 与
`/opt/panji-live/frontend/dist`；当本轮重启了 Python 服务或属首次 Live Mount 部署时，
全部 11 个共用 Live Mount 的 Python 服务挂载均需核验通过。
状态文件只在这些检查全部通过后更新。

## Compose 服务边界

`docker-compose.prod.yml` 定义 frontend、backend、PostgreSQL、Redis、Umami，以及 bars、
strategy、calendar、monitor、strategy-batch、outbox、delivery、after-close、watchdog、capture
等 Worker。`docker-compose.live.yml` 为应用服务叠加 `/opt/panji-live` 只读运行代码挂载。

有状态服务的数据卷由 Compose 管理。部署脚本不得删除或重建 PostgreSQL/Redis Volume，
不得把测试数据库或测试数据写入生产持久化资源。

## 容器资源预算现状

> 2026-08-04 治理垂直切片落地 `docker-compose.prod.yml` 容器级资源限制（DS-101）。
> 以下为**初始保守宽松值**，全部经环境变量 `${PANJI_<SERVICE>_<FIELD>:-default}` 可配置，可在
> `/etc/market-dev/market.env` 覆盖收紧。初始值依据服务器 7.4G 内存为宿主机保留 ≥1G 余量规划。
>
> **重要修正（2026-08-11，REVIEW_RESOURCE_BUDGET_CALIBRATION_DEFECT）**：`backend` 与 `trading-worker-after-close`
> 的 `mem_limit` 初值 `1024m` 是**未经生产验证的初始值**，已被 2026-08-11 生产 cgroup OOM 证据**证伪**
> （REVIEW FULL compute_run 在 ~970MB RSS 处被 `CONSTRAINT_MEMCG` 杀死，`oom_kill=4`）。两者 Review 能力
> **仓库默认目标值**已校准为 `4096m`（仍低于 7.4G 宿主余量规划，未触碰宿主机保留）。
>
> **Should vs Actual（部署前）**：`4096m` 是 **repo/default 目标值**（compose 默认与 `market.verify.env.example`
> 对齐），**不是已生效的运行时事实**。运行时是否生效（容器 `HostConfig.Memory`）须等待真实部署并用
> `docker inspect trading-backend/worker-after-close` 核验后方可记入 Map。当前（部署前）：runtime effective = pending。
> 该修正**不改变** strategy-batch、capture 及其他 heavy/light 服务的 `mem_limit`，也未触碰 PRD 语义、算法或 Migration。

| 服务 | mem_limit 初值 | mem_limit 当前值 | mem_reservation | cpus | pids_limit | 类别 |
|---|---|---|---|---|---|---|
| postgres | 1536m | 1536m | 1024m | 2 | 512 | 数据服务 |
| redis | 256m | 256m | 128m | 1 | 256 | 数据服务 |
| backend | 1024m（证伪） | **4096m**（repo 默认目标；runtime effective 待部署核验） | 512m | 2 | 1024 | 应用服务（Review-capable） |
| capture | 768m | 768m | 384m | 2 | 512 | 应用服务 |
| strategy-batch | 1024m | 1024m（不变） | 512m | 1 | 1024 | 应用服务 |
| after-close（review/feature/stock core） | 1024m（证伪） | **4096m**（repo 默认目标；runtime effective 待部署核验） | 512m | 1 | 1024 | 应用服务（Review-capable） |
| 轻 Worker / scheduler / watchdog | 512m | 512m | 256m | 1 | 512 | 应用服务 |
| frontend（Nginx 静态） | 128m | 128m | 64m | 1 | 256 | 应用服务 |
| umami | 384m | 384m | 128m | 1 | 512 | 应用服务 |

**硬约束（DS-107 规则，尚未实现）**：重任务服务（review / feature / stock core）的应用级
`memory_budget_mb` 必须**显著低于**其所在容器 `mem_limit`（禁止等于或高于上限）。该数值关系为**规则要求**，
**当前应用级预算尚未在代码中落地**（见 CHANGE-20260804-007：本阶段仅规则与文档，实施待办）。部署后按
`docker stats --no-stream` 实测高水位再收紧。

**状态**：容器级 `mem_limit` 已在 `docker-compose.prod.yml` 落地（DS-101，源自 CHANGE-20260804-004）；
**应用级预算 / 长任务资源治理为实施待办**（CHANGE-20260804-007 `implementation_pending`）。**部署后真实
高水位待采集**（DS-104 的 `docker stats --no-stream` 结果需在用户授权真实部署后回填本 Map），据此再收紧
预算；禁止只采集不限制。

## 部署后资源证据字段

部署成功后应在状态文件 / Map 记录以下结构化字段（`key=value`），便于 grep 与预算收紧对账：

- `post_deploy_oom_killed=<true|false>`：任一关键容器 OOMKilled 为 true 即部署失败；
- `post_deploy_restart_count=<int>`：异常重启计数；
- `stats_mem_usage_mb_<service>=<value>`：各服务 `docker stats --no-stream` 高水位；
- `mem_limit_effective=<true|false>`：`docker inspect` 校验 `Memory`/`PidsLimit`/`NanoCpus` 已生效；
- `cleanup_disk_before_mb` / `cleanup_disk_after_mb`：清理前后磁盘证据（DS-105）。

## 最近只读运行证据

2026-08-02 的只读核验记录显示，当时服务器 repo 和运行容器仍处于旧镜像构建模式，
容器未挂载 `/opt/panji-live`；同时观察到 `trading-postgres-test` 持久测试容器。两者均不符合
当前合同。此次治理修改没有连接生产，也没有部署、迁移或删除资源，因此不能把它们写成已修复。

后续只有在用户明确授权生产操作后，才能通过 `panji-prod-preflight` 和只读命令重新确认；
持久测试容器的删除还需要单独确认影响范围和数据保护条件。

## 更新触发条件

以下事实变化后必须更新本 Map：本地/远程入口、SSH 身份、Compose 服务、运行目录、
部署调用图、CI 触发方式、数据库/Redis 边界、版本证据或已知生产偏差。

## V2.1 开发链运行状态（2026-08-05 基线 2267d43）

> 当前为代码开发阶段，非集成/部署阶段。以下 status 如实区分，禁止把代码完成写成
> 生产 fully_ready，也禁止把 PG deferred 写成开发失败。

SHA 谱系：`2267d43`（D–J 原始开发基线）→ `5df542d`（D–J 初次收口）
→ `94aa38e`（Completion Pass 1）→ Corrective-3（见提交记录）。

- `git_branch = dev`
- `remote_verification_sha = f1612f6`（隔离 worktree 精确检出，未触碰部署树）
- `remote_static_verified = true`（Ruff 全通过；Mypy 改动文件零错误）
- `remote_unit_verified = true`（PURE_UNIT_TEST 52 passed，postgres=0）
- `remote_frontend_build_verified = true`（TSC 0 错误 / ESLint 0 错误 / vite build 成功）
- `deployed_head = 6f008ca`（部署树未随验证变动）
- `pg_tested = false`
- `pg_gate = deferred`
- `migration_085_authored = true`（Corrective-2 已存在，未 apply）
- `migration_085_applied = false`
- `deployed = false`
- `runtime_verified = false`
- `data_closed = false`
- `browser_verified = false`
- `production_fully_ready = false`

上述状态仅是对应 V2.1 任务在该 SHA 的历史验证证据，不定义当前环境权限。当前权限与运行边界以 `AGENTS.md`、`rules/40`、`rules/80` 和 `rules/81` 为准。

当前实现缺口：本地 SSH Tunnel 与 development 配置仍可指向 `bz_stock`，尚未提供经核验的专用只读数据库凭据，因此该路径不得作为默认本地预览或测试入口；需要真实业务数据调试时必须单独授权并先证明连接只读。
