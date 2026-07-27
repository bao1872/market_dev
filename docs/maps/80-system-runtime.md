# 系统运行体系 Map

核验状态：已基于本地原生启动核验（第一阶段），并基于远程只读审计补充腾讯云运行事实（第三阶段）
最后核验日期：2026-07-27
核验分支：dev
核验提交：79f5965633e2e636075626572a198fbbd707c43f（本地）；远程 origin/main=13a0ef3e2910ee75fe8dd2b583a2ceed0db57fbf
核验范围：本地原生 Backend / Frontend 启动、共享 PostgreSQL / Redis 连接、Scheduler / Worker 默认关闭；远程只读审计（Git/Compose/容器/Redis/健康检查）
对应 PRD：`../prd/80-system-runtime.md`
事实所有权：本地原生进程、远程 Docker Compose、Git、配置、数据库、Redis、Scheduler、CI 和部署事实

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| SR-01 本地使用原生进程 | `Makefile` 的 `backend` / `frontend` 目标；`backend/app/main.py`；`frontend/vite.config.ts` | 已核验 | Backend PID / Frontend PID；curl /health 返回 200 |
| SR-02 不依赖 Docker 本地启动 | `Makefile` 的 `up` / `down` 已改为废弃警告；`docker-compose.yml` 仅存 Redis 服务且未被本地 dev 流程引用 | 已核验 | 未执行 `docker compose up`；本地无盘迹容器 |
| SR-03 共享 PostgreSQL | `backend/.env` DATABASE_URL → 127.0.0.1:15432 → 腾讯云 Docker 内 `trading-postgres:5432/bz_stock` | 已核验 | SELECT 1 / current_database() / version() / 表存在性查询成功 |
| SR-10 至 SR-13 Git、CI 和版本 | `dev` 已创建；本地基于 `origin/dev` rebase 后领先 2 个提交；未做 push | 已核验 | `git status --branch` |
| SR-20 至 SR-22 PostgreSQL 连接和 Schema | `backend/app/db.py`；`backend/app/config.py` DATABASE_URL 解析；PostgreSQL 16.14 | 已核验 | 健康接口 /version 返回 alembic_revision |
| SR-30 至 SR-33 Redis DB 和队列 | `backend/app/config.py` REDIS_URL；本地 DB 15（临时）；远程 DB 0 | 已核验 | PING / DBSIZE=0；DB 0 启动被 `config.py` 拒绝 |
| SR-32 本地 Redis 安全启动 | `backend/app/config.py` `_resolve_redis_url` / `_validate_redis_url` | 已核验 | 单元测试 `test_config_validation.py` 通过 |
| SR-40 至 SR-43 Scheduler、Worker 和手动入口 | `backend/app/worker.py` 手动入口；Backend 不启动 Scheduler；本地 lifespan 跳过维护写入 | 已核验 | 进程列表无 scheduler/worker；后端日志无 seed/calendar/recovery；lifespan 测试通过 |
| SR-50 至 SR-52 代码、配置和承载边界 | 同一套代码；配置通过 `.env` / `CONFIG_FILE` / `config.local.py` 差异化 | 已核验 | 本地与远程共用 `app.main:app` 和 `frontend/src` |
| SR-14 稳定版本自动部署 | `.github/workflows/deploy-production.yml` + `scripts/deploy/panji-deploy.sh` 已准备；服务器侧 `/usr/local/bin/panji-deploy.sh`、锁文件、state 文件、GitHub Secrets 尚未启用 | 代码已准备 / 链路未启用 | workflow 与脚本存在；远程无安装；未触发真实部署 |
| SR-31.1 本地 Redis DB15 正式保留 | 远程 Redis `databases=16`，DB15 存在且 `DBSIZE=0`；生产 Compose、生产脚本、业务代码均使用 DB 0，未引用 DB15 | 已确认 | 远程 `CONFIG GET databases` / `INFO keyspace` / `SELECT 15 DBSIZE`；仓库 grep 未在 prod 引用 /15 |
| SR-60 至 SR-62 部署、Volume 和 Nginx | 远程 `docker-compose.prod.yml` 保留；`trading-postgresdata`、`trading-redisdata`、`trading-capture-static` Volume 存在；Nginx 监听 80 | 已核验（只读） | 远程 docker ps / docker volume ls / curl :80 |

## 2. 运行位置

| 位置 | 代码目录 | 分支/SHA | 承载方式 | 主要用途 | 自动 Scheduler |
|---|---|---|---|---|---|
| 本地 | `/Users/zhenbao/Desktop/coding/market_dev` | `dev` / `79f5965` | 原生 Python 3.11 venv + Uvicorn；Node.js + Vite | 开发和手动调试 | 关闭 |
| 远程 | `/root/web_dev` | 当前检出 `refactor/invite-capability-access-v2` / `0f17e7d`；运行版本对应 `origin/main` `13a0ef3`；工作区不干净 | Docker Compose | 稳定运行 | 已运行（bars/strategy/calendar scheduler 各 1 实例） |

### 远程服务器身份

| 项目 | 值 |
|---|---|
| 角色 | 腾讯云稳定运行服务器 |
| 权威公网 IP | `43.136.118.82` |
| 项目 SSH 别名 | `panji-prod`（定义于 `~/.ssh/config`，HostName 必须为 `43.136.118.82`） |
| 远程代码目录 | `/root/web_dev` |
| 生产 Compose | `docker-compose.prod.yml` + `docker-compose.live.yml`（Live Mount） |
| CI Workflow | `.github/workflows/ci.yml`，名称为 `CI`；`dev` push / PR 到 `main` 触发；当前任务包含架构规则、文档一致性、测试白名单、治理规则、Reports、Ruff、Mypy、Alembic、PostgreSQL 集成测试、前端 TSC/Lint/Build/Contract/E2E |
| 自动部署代码 | 已准备：`.github/workflows/deploy-production.yml`、`scripts/deploy/panji-deploy.sh`；支持精确 SHA、`--dry-run`、`flock` 串行、健康检查、失败回滚 |
| 自动部署状态 | 未启用；服务器侧 `/usr/local/bin/panji-deploy.sh`、锁文件、state 文件、GitHub Secrets 尚未配置；本轮未触发任何真实部署 |

> 涉及远程服务器、数据库、Redis、路径和端口时，必须先读取本节。聊天记忆和本机任意 SSH 别名（如 `55-server`）不能作为权威来源。`55-server` 解析到 `120.234.137.109`，不是盘迹生产服务器，禁止用于盘迹操作。

## 3. 本地原生开发进程

本地不应通过 Docker 或 Docker Compose 启动盘迹应用服务。

| 服务或依赖 | 预期启动方式 | 实际入口 | 配置入口 | 端口 | 自动启动 | 核验状态 |
|---|---|---|---|---|---|---|
| Backend | Python 虚拟环境 + Uvicorn | `backend/app/main.py:app` via `make backend` | `backend/.env`（`APP_ENV=development`，环境变量优先） | 8000 | 否 | 已核验 |
| Frontend | Node.js + Vite | `frontend/vite.config.ts` via `npm run dev` | `frontend/vite.config.ts` 内置代理 | 8008 | 否 | 已核验 |
| 指定 Worker | Python 进程手动启动 | `python -m app.worker`（需显式 `WORKER_TYPE`） | `backend/.env` REDIS_URL | - | 否 | 未实际启动，入口已核验 |
| Orchestrator | Python 进程手动启动 | `python -m app.services.after_close_orchestrator`（待确认） | `backend/.env` | - | 否 | 未核验 |
| Scheduler | 不应自动启动 | 无自动启动入口；Scheduler 逻辑在 `app.worker` 各类型中按需运行 | `WORKER_TYPE` 显式指定 | - | 否 | 已核验 |
| PostgreSQL | 直接连接共享实例 | 不在本地启动 | `backend/.env` DATABASE_URL → 127.0.0.1:15432 | 15432（SSH 隧道） | - | 已核验 |
| Redis | 直接连接共享实例的本地专用逻辑 DB | 不在本地启动 | `backend/.env` REDIS_URL → 127.0.0.1:16379/15 | 16379（SSH 隧道） | - | 已核验 |
| SSH 隧道 | 本地脚本手动启动/停止 | `scripts/local/ssh-tunnel.sh` via `make tunnel` | `~/.ssh/config` Host 别名 `panji-prod`（HostName 43.136.118.82） | 15432/16379 | 否 | 已核验 |

需要重点核验：

- 本地 Python 虚拟环境和依赖安装方式：已核验，`backend/.venv` Python 3.11.7，`pip install -e .` + `pip install -e '.[dev]'`
- 后端原生启动命令：`cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
- 前端原生启动命令：`cd frontend && npm run dev`
- 本地配置文件和环境变量入口：`backend/.env`（gitignored）
- 本地 Redis 逻辑 DB：DB 15（远程使用 DB 0）
- Scheduler 明确关闭的位置：Backend `main.py` lifespan 不启动 Scheduler；Scheduler 仅在 `app.worker` 中按 `WORKER_TYPE` 手动运行
- Worker 是否只能进入本地隔离队列：只要 `REDIS_URL` 指向 DB 15，Worker 不会进入远程 DB 0 队列
- 仓库中的本地 `docker-compose.yml` 是否已经废弃、仍被脚本引用或会误导开发：`Makefile` dev/backend/frontend 不再引用它；`up/down` 已标记废弃；文件本身仅保留 redis 服务，本阶段不删除

## 4. Git 与 CI

| 项目 | 当前事实 |
|---|---|
| `dev` | 本地当前分支；已跟踪 `origin/dev`；本地领先 4 个提交（`a817595`、`eaffb11`、`405d3ee`、`79f5965`） |
| `main` | 远程稳定分支；`origin/main` SHA = `13a0ef3e2910ee75fe8dd2b583a2ceed0db57fbf`；当前运行版本一致 |
| dev push | 本阶段未 push；按 PRD 应只触发 CI，不自动部署 |
| main 自动部署 | 代码已准备，链路未启用；`.github/workflows/deploy-production.yml` 监听 `workflow_run`（CI success on main）和 `workflow_dispatch`；SSH 调用 `/usr/local/bin/panji-deploy.sh` |
| CI gate | 已核验配置：`.github/workflows/ci.yml` 存在，名称为 `CI`，与 workflow_run 引用一致 |

## 5. PostgreSQL

| 项目 | 当前事实 |
|---|---|
| 是否共享 | 是，本地通过 SSH 隧道连接腾讯云 Docker 内 `trading-postgres` |
| 本地连接配置 | `postgresql+psycopg://***@127.0.0.1:15432/bz_stock`（`backend/.env`） |
| 远程容器配置 | 通过 `/etc/market-dev/market.env` 和 `docker-compose.prod.yml` 配置，未修改 |
| 本地权限 | 用户 `bz` 可读写；本阶段只执行 SELECT |
| Schema 管理 | Alembic 已存在；本阶段未执行 Migration |
| 核心数据保护 | 未执行 DELETE/UPDATE/TRUNCATE/DROP；development 环境 lifespan 已跳过策略种子、日历刷新和僵尸任务恢复 |

## 6. Redis

| 项目 | 本地 | 远程 |
|---|---|---|
| 实例 | 同一共享实例 | 同一共享实例 |
| 承载 | 不在本地启动容器 | Docker 容器 `trading-redis` |
| 逻辑 DB | DB 15（已正式保留给本地开发临时状态） | DB 0 |
| 队列 | 未启动 Worker，无队列写入 | DB0 keys=5300（含队列/锁/缓存/临时状态） |
| 锁 | 未使用 | DB0 内 |
| 缓存 | 未启用 | DB0 内 |

> DB 15 正式保留依据：远程 Redis 配置 `databases=16`；DB15 存在且 `DBSIZE=0`；`docker-compose.prod.yml`、生产脚本、生产业务代码均使用 DB 0，未引用 DB15；无其他项目用途记录。本地 `.env.example`、测试和 `scripts/deploy/` 等仍不指向 DB15。Worker 启动前仍需确认 `REDIS_URL` 以 `/15` 结尾。

## 7. 远程 Docker Compose 服务

本节只记录腾讯云远程稳定运行，不作为本地启动依据。

| 服务 | 容器名 | Compose 定义 | 依赖 | 端口 | Volume / 备注 |
|---|---|---|---|---|---|
| nginx + 前端 | `trading-frontend` | `docker-compose.prod.yml` | backend | `0.0.0.0:80->80/tcp` | `capture_static` 挂载到 `/usr/share/nginx/html/static/captures` |
| backend | `trading-backend` | `docker-compose.prod.yml` | redis, postgres | `0.0.0.0:8000->8000/tcp` | `./doc:/doc:ro`；`/etc/market-dev/config.production.py:/app/app/config.production.py:ro` |
| bars scheduler | `trading-worker-bars-scheduler` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=bars_scheduler` |
| strategy scheduler | `trading-worker-strategy-scheduler` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=strategy_scheduler` |
| calendar scheduler | `trading-worker-calendar` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=calendar_scheduler` |
| monitor | `trading-worker-monitor` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=monitor_scheduler` |
| strategy batch | `trading-worker-strategy-batch` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=strategy_batch` |
| outbox | `trading-worker-outbox` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=outbox` |
| delivery | `trading-worker-delivery` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=delivery` |
| after-close orchestrator | `trading-worker-after-close` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=after_close_orchestrator` |
| watchdog | `trading-worker-watchdog` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=watchdog` |
| capture | `trading-worker-capture` | `docker-compose.prod.yml` | redis, postgres | `0.0.0.0:8001->8001/tcp` | 独立 Dockerfile.capture；healthcheck 健康 |
| PostgreSQL | `trading-postgres` | `docker-compose.prod.yml` | - | `5432/tcp` | `trading-postgresdata`；healthcheck 健康 |
| PostgreSQL (test) | `trading-postgres-test` | `docker-compose.prod.yml` | - | `0.0.0.0:5433->5432/tcp` | 持久化卷待核验 |
| Redis | `trading-redis` | `docker-compose.prod.yml` | - | `6379/tcp` | `trading-redisdata`；`appendonly yes`；healthcheck 健康 |

> 远程 Compose 文件默认 `REDIS_URL=redis://redis:6379/0`，所有生产服务共用 DB 0。`docker-compose.live.yml` 叠加后将 `/opt/panji-live` 的运行时代码只读挂载到 Python 服务与 capture worker，实现代码热更新而不重建镜像。

## 8. 代码和承载边界

本地原生进程与远程容器共同使用：

- 同一后端应用入口：`backend/app/main.py:app`
- 同一前端源码：`frontend/`
- 同一 Worker 分发入口：`backend/app/worker.py`
- 同一 ORM 和 Migration：`backend/app/models/`、`backend/alembic/`
- 同一指标算法：`backend/app/services/`、`backend/app/strategy_assets/`
- 同一配置字段：`backend/app/config.py`
- 不同配置值和启动命令：通过 `.env` / `CONFIG_FILE` / `docker-compose.prod.yml.environment` 表达

不得存在仅为本地或仅为 Docker 复制的业务实现。当前未发现本地专用业务代码复制。

## 9. 版本和运行验证

### 本地

- 后端原生进程及端口：0.0.0.0:8000
- 前端 Vite 进程及端口：0.0.0.0:8008
- 前端访问后端：`/api/health` 代理返回 `{"status":"ok"}`
- PostgreSQL 连接：SELECT 1 / current_database=bz_stock / version=PostgreSQL 16.14 / 10 张表存在
- Redis 本地逻辑 DB：DB 15（临时），DBSIZE=0
- Scheduler 未自动启动：无 scheduler 进程，后端日志无 scheduler 启动
- 本地 lifespan 未执行 seed/calendar/recovery（development 环境跳过）
- 未启动任何本地 Docker 应用服务：未执行 `docker compose up`
- `/version` deployment_mode 返回 `native-development`

### 远程

- 容器对应 SHA：`/version` 返回 `runtime_git_sha=13a0ef3e2910ee75fe8dd2b583a2ceed0db57fbf`，与 `origin/main` 一致
- 当前检出分支：`refactor/invite-capability-access-v2`，HEAD `0f17e7d`，工作区存在未提交修改；运行版本仍为 `origin/main` 的 `13a0ef3`
- 端口 80 页面：HTTP 200
- `/health`：`{"status":"ok","service":"trading-platform","version":"1.1.0"}`
- `/health/ready`：`{"status":"ready"}`
- `/version`：`deployment_mode=live`，`alembic_revision=067_scheduler_job_runs_lease_epoch_attempt_no`
- Scheduler 是否运行：`trading-worker-bars-scheduler`、`trading-worker-strategy-scheduler`、`trading-worker-calendar` 各 1 实例
- Worker 是否消费远程 Redis DB：生产服务均配置 `REDIS_URL=redis://redis:6379/0`，DB0 当前 keys=5300
- 数据服务未被误重建：未执行 down / volume 删除；`trading-postgresdata`、`trading-redisdata` 存在

## 10. 已知偏差与风险

- 本地 `docker-compose.yml` 仍保留 redis 服务，虽然 `Makefile` 已标记 `up/down` 废弃，但文件本身仍可能误导新开发者。本轮按任务要求不删除，仅标记“非本地开发入口”。
- 本地 Redis DB 15 已正式保留为本地开发临时状态隔离库，但 Worker 启动前仍需确认 `REDIS_URL` 以 `/15` 结尾。
- 本地与远程共享 PostgreSQL，开发中的破坏性操作需要额外注意；当前未做权限只读限制。
- 远程 `/root/web_dev` 当前检出的不是 `main`（而是 `refactor/invite-capability-access-v2`），且工作区不干净；运行版本与 `origin/main` 一致，但 deploy 脚本在启用后会因“工作区不干净 / 非 main 分支”而拒绝部署，需要先人工清理。
- 自动部署代码已准备但尚未启用：服务器侧缺少 `/usr/local/bin/panji-deploy.sh`、锁文件、state 文件和 GitHub Secrets；`.github/workflows/deploy-production.yml` 尚未合并到 `main`，因此不会触发真实部署。
- 远程 PostgreSQL test 容器 (`trading-postgres-test`) 映射 5433 端口，其用途和持久化策略尚未核验。

## 11. 更新触发条件

以下变化必须更新本 Map：

- 本地原生启动入口或配置变化；
- 本地重新引入 Docker 依赖；
- 远程 Compose 服务变化；
- PostgreSQL 或 Redis 连接关系变化；
- Scheduler、Worker 或队列隔离变化；
- CI、部署或稳定版本识别方式变化。
