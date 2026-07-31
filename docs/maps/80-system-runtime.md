# 系统运行体系 Map

核验状态：已基于本地原生启动核验（第一阶段），并基于远程只读审计补充腾讯云运行事实（第三阶段）；Phase 4 完成 Git 分支治理与 PRD20/30 代码对齐审计；Phase 5A 完成分支一致性补验与 AC-04 修复；Phase 5B-0 完成 ref/sync 仓库清理、CI 防误推、本地完整原生运行与趋势入口锁定；Phase 5B-2 完成部署脚本修复与静态测试；2026-07-28 完成本地数据架构纠正（永久禁用 bz_stock_test，固定连接 bz_stock 正式库）
最后核验日期：2026-07-28
核验分支：dev
核验提交：c730876（Phase 5B-0 ref/sync 清理）；Phase 5A 修复见 `docs/changes/2026/CHANGE-20260727-002-after-close-daily-readiness.md`；Phase 5B-0 详见 `docs/changes/2026/CHANGE-20260727-003-repo-boundary-local-runtime.md`；Phase 5B-2 详见 `docs/changes/2026/CHANGE-20260727-005-phase-5b-2-capabilities-deploy.md`；远程 origin/main=13a0ef3e2910ee75fe8dd2b583a2ceed0db57fbf
核验范围：本地原生 Backend / Frontend 启动、共享 PostgreSQL / Redis 连接、Scheduler / Worker 默认关闭；远程只读审计（Git/Compose/容器/Redis/健康检查）；本地/origin/服务器分支治理与一致性补验；PRD20/PRD30 代码对齐审计；AC-04 日线 readiness 修复与 P0 Redis 隔离复核
对应 PRD：`../prd/80-system-runtime.md`
事实所有权：本地原生进程、远程 Docker Compose、Git、配置、数据库、Redis、Scheduler、CI 和部署事实

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| SR-01 本地使用原生进程 | `Makefile` 的 `backend` / `frontend` 目标；`backend/app/main.py`；`frontend/vite.config.ts` | 已核验 | Backend PID / Frontend PID；curl /health 返回 200 |
| SR-02 不依赖 Docker 本地启动 | `Makefile` 的 `up` / `down` 已改为废弃警告；`docker-compose.yml` 仅存 Redis 服务且未被本地 dev 流程引用 | 已核验 | 未执行 `docker compose up`；本地无盘迹容器 |
| SR-03 共享 PostgreSQL | `backend/.env` DATABASE_URL → 127.0.0.1:15432 → 腾讯云 Docker 内 `trading-postgres:5432/bz_stock`；**本地固定连接 `bz_stock` 正式库，永久禁止 `bz_stock_test`** | 已核验 | SELECT current_database()=bz_stock；instruments=8272 |
| SR-03a 持久测试库已删除 | `bz_stock_test` 已于 2026-07-28 DROP；本地 Mac / 开发服务器 / 腾讯云禁止创建或复用持久测试库；本地测试只能 `PURE_UNIT_TEST=1`；DB 集成测试只在 CI 临时容器运行（`GITHUB_ACTIONS=true` 或 `PANJI_CI_DB_TEST=1` 识别） | 已核验 | pg_database 中无 bz_stock_test；conftest.py CI 守卫生效 |
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
| 本地 | `/Users/zhenbao/Desktop/coding/market_dev` | `dev` / `069ebcc`；本地另有 `main`、`experiment` | 原生 Python 3.11 venv + Uvicorn；Node.js + Vite | 开发和手动调试 | 关闭 |
| 远程 | `/root/web_dev` | 已切换并清理为 `main` / `13a0ef3`，工作区干净；运行版本对应 `origin/main` `13a0ef3` | Docker Compose | 稳定运行 | 已运行（bars/strategy/calendar scheduler 各 1 实例） |

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
| `dev` | 本地当前分支；已跟踪 `origin/dev`；本地与 origin 一致（Phase 5B-0 提交 `c730876` ref/sync 清理，已 push origin/dev） |
| `main` | 远程稳定分支；`origin/main` SHA = `13a0ef3e2910ee75fe8dd2b583a2ceed0db57fbf`；本地 `main` 同步；服务器 `/root/web_dev` 检出 `main` 工作区干净；当前运行版本一致；**origin/main 仍含 `ref/smc_user_source.pine`**（Phase 5B-0 清理待 PR 合并） |
| `experiment` | **[Phase 5A 一致性补验]** 本地、origin、服务器三处 `experiment` SHA 已对齐；Phase 5B-0 cherry-pick `c730876` 为 `38df3af` 并 push origin/experiment；本地与 origin 一致 |
| 服务器 experiment 归档 | **[Phase 5A]** 服务器原 `experiment`（tip `623ad87`，含 16 个 V2.1 唯一提交）已归档为 annotated tag `archive/server-experiment-wip-20260727`（tag object `40fb4ab2`），tag 已推送 origin 并用 `git ls-remote` 验证；服务器删除分叉的本地 `experiment` 后按 `origin/experiment` 重新创建 tracking 分支 |
| 非保留分支 | 本地/origin 非 main/dev/experiment 分支已删除；已创建 6 个 `archive/*-YYYYMMDD` annotated tag 保存唯一提交（Phase 4 的 5 个 + Phase 5A 的 `archive/server-experiment-wip-20260727`） |
| 服务器分支 | `/root/web_dev` 当前检出 `main`，工作区干净；服务器本地保留 `main`/`dev`/`experiment`，均与 origin 对齐 |
| ref/sync 仓库清理 | **[Phase 5B-0]** `dev`/`experiment` 已通过 `git rm --cached ref/smc_user_source.pine` + 退出跟踪 sync 目录入口（不作为运行时依赖）；`origin/dev`、`origin/experiment` 树中 `git ls-tree -r` 无 `ref/`/`sync/`；本地 `ref/` 实体保留；`sync/` 已从本地删除；`.gitignore` 加入 `/ref/` 与 `/sync/` |
| CI 防误推 | **[Phase 5B-0]** `.github/workflows/ci.yml` governance-rules job 新增显式检查 `git ls-files ref sync` 必须为空；`backend/tests/test_ref_isolation.py` 守护 `git ls-files ref/` 与 `git ls-files sync/` 均为空；双重防护确保未来误推被 CI 拒绝 |
| main PR 状态 | **[Phase 5B-0]** origin/main 仍含 `ref/smc_user_source.pine`，需通过 dev → main PR 合并清理；本轮不创建/合并 PR，等待用户授权 |
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
| after-close orchestrator | `trading-worker-after-close` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=after_close_orchestrator`；**[P0-3 2026-07-31]** 同进程启动 Auction Scheduler co-process（09:25/10:00 触发），无独立 auction 容器 |
| watchdog | `trading-worker-watchdog` | `docker-compose.prod.yml` | redis, postgres | - | `WORKER_TYPE=watchdog` |
| capture | `trading-worker-capture` | `docker-compose.prod.yml` | redis, postgres | `0.0.0.0:8001->8001/tcp` | 独立 Dockerfile.capture；healthcheck 健康 |
| PostgreSQL | `trading-postgres` | `docker-compose.prod.yml` | - | `5432/tcp` | `trading-postgresdata`；healthcheck 健康；**[CHANGE-009]** 复用承载 umami 数据库（独立 user/schema） |
| PostgreSQL (test) | `trading-postgres-test` | `docker-compose.prod.yml` | - | `0.0.0.0:5433->5432/tcp` | 持久化卷待核验 |
| Redis | `trading-redis` | `docker-compose.prod.yml` | - | `6379/tcp` | `trading-redisdata`；`appendonly yes`；healthcheck 健康 |
| Umami 访客分析 | `trading-umami` | `docker-compose.prod.yml` | postgres | - | **[CHANGE-009]** `image: docker.umami.is/umami-software/umami:3.2`；`env_file: /etc/market-dev/umami.env`；`umami_data` volume 持久化 `/app/data`；替代已移除的 GoAccess |

> 远程 Compose 文件默认 `REDIS_URL=redis://redis:6379/0`，所有生产服务共用 DB 0。`docker-compose.live.yml` 叠加后将 `/opt/panji-live` 的运行时代码只读挂载到 Python 服务与 capture worker，实现代码热更新而不重建镜像。

### 7.1 访客分析服务（CHANGE-20260729-009）

**[CHANGE-009] Umami 替代 GoAccess**：GoAccess 从未成功部署（容器和卷均不存在，nginx access.log 是符号链接到 `/dev/stdout`），改用 Umami 作为访客分析服务。

| 项 | 值 |
|---|---|
| 容器名 | `trading-umami` |
| 镜像 | `docker.umami.is/umami-software/umami:3.2`（官方固定版本） |
| 数据库 | 复用 `trading-postgres`，独立 `umami` 数据库和用户 |
| 配置文件 | `/etc/market-dev/umami.env`（`DATABASE_URL` + `APP_SECRET` + `TZ=Asia/Shanghai`） |
| Website ID | `109c6241-d39e-47b0-a6f2-29a6bc15bd09`（写入 `/etc/market-dev/market.env` 的 `UMAMI_WEBSITE_ID`） |
| 数据持久化 | `trading-umami-data` volume（`/app/data`） |
| Nginx 代理 | `location /umami/` 反向代理到 `umami:3000`，剥离 `/umami/` 前缀 |
| Tracking script 注入 | nginx `sub_filter` 在 `</head>` 前动态注入 `<script async src="/umami/script.js" data-website-id="..."></script>` |
| Live Mount 适配 | 通过 `docker-entrypoint.sh` 用 `sed` 替换 nginx.conf 中的 `${UMAMI_WEBSITE_ID}` 占位符（dist 只读挂载，无法直接修改 index.html） |
| 开发/capture 模式 | 不注入 tracking script（docker-entrypoint.sh 通过 UMAMI_WEBSITE_ID 是否设置控制） |
| nginx access.log | 保留 + logrotate 每 15 分钟检查轮转（不依赖 GoAccess） |
| GoAccess runbook | `docs/runbooks/goaccess-deployment.md` 保留为历史记录，标注 superseded |

> GoAccess 容器和 `goaccess_reports` / `nginx_logs` 共享卷已从 `docker-compose.prod.yml` 中移除；`deploy_live_runtime.sh` 的容器启动列表也已移除 `goaccess` 改为 `umami`。

**[CHANGE-20260730-011] `/admin/visitors` API 与前端页面真正迁移到 Umami（CHANGE-009 遗漏修复）**

| 维度 | 修改前 | 修改后 |
|---|---|---|
| 后端 `admin_visitors.py` | 硬编码 GoAccess：`GOACCESS_REPORT_PATH=/srv/goaccess/report.json`，解析 GoAccess JSON | 调用 `UmamiAnalyticsAdapter.fetch_umami_report()`，独立只读连接查询 `umami` 数据库 |
| 后端 Schema `visitors.py` | `data_source` 值为 `goaccess_json` / `empty` / `error` | `data_source` 值为 `umami` / `empty` / `error`，`generated_at` 为真实查询时间 |
| 前端 `AdminVisitorsPage.tsx` | 标题"访问统计"，描述"GoAccess 报告"，错误指向 GoAccess | 标题"Umami 访客分析"，错误指向 Umami 服务，新增"打开详细分析"按钮跳转 `/umami/` |
| Umami 凭据 | 仅部署容器 | backend 通过 `UMAMI_DATABASE_URL` 独立只读连接查询（不接触 Umami admin 密码） |
| Nginx access.log | 保留 + logrotate（运维用） | 保留 + logrotate（运维用） |

凭据位置：`/etc/market-dev/market.env` 的 `UMAMI_DATABASE_URL`（生产注入）和 `UMAMI_WEBSITE_ID`。前端不接触数据库密码。

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
- 当前检出分支：`main`，HEAD `13a0ef3`，工作区干净；运行版本与 `origin/main` 的 `13a0ef3` 一致
- 端口 80 页面：HTTP 200
- `/health`：`{"status":"ok","service":"trading-platform","version":"1.1.0"}`
- `/health/ready`：`{"status":"ready"}`
- `/version`：`deployment_mode=live`，`alembic_revision=067_scheduler_job_runs_lease_epoch_attempt_no`
- Scheduler 是否运行：`trading-worker-bars-scheduler`、`trading-worker-strategy-scheduler`、`trading-worker-calendar` 各 1 实例
- Worker 是否消费远程 Redis DB：生产服务均配置 `REDIS_URL=redis://redis:6379/0`，DB0 当前 keys=5300
- 数据服务未被误重建：未执行 down / volume 删除；`trading-postgresdata`、`trading-redisdata` 存在

## 10. 已知偏差与风险

- 本地 `docker-compose.yml` 仍保留 redis 服务，虽然 `Makefile` 已标记 `up/down` 废弃，但文件本身仍可能误导新开发者。本轮按任务要求不删除，仅标记"非本地开发入口"。
- 本地 Redis DB 15 已正式保留为本地开发临时状态隔离库，但 Worker 启动前仍需确认 `REDIS_URL` 以 `/15` 结尾。
- 本地与远程共享 PostgreSQL，开发中的破坏性操作需要额外注意；当前未做权限只读限制。
- 远程 `/root/web_dev` 已切换为 `main`，工作区干净，满足自动部署脚本的 workspace 检查；但自动部署代码尚未启用。
- 自动部署代码已准备但尚未启用：服务器侧缺少 `/usr/local/bin/panji-deploy.sh`、锁文件、state 文件和 GitHub Secrets；`.github/workflows/deploy-production.yml` 尚未合并到 `main`，因此不会触发真实部署。
- 远程 PostgreSQL test 容器 (`trading-postgres-test`) 映射 5433 端口，其用途和持久化策略尚未核验。
- **[Phase 5B-0]** `origin/main` 仍含 `ref/smc_user_source.pine`，需通过 dev → main PR 合并清理；PR 合并前 main 分支不满足 SR-15。
- **[Phase 5B-0]** 本地 Vite 开发服务器无 Nginx 前置，访问 `/` 时 `LandingPage` 组件 `window.location.replace('/')` 会触发无限刷新；已知本地开发限制，可通过直接访问 `/login` 或 `/market` 绕过；生产环境由 Nginx 精确分流，不受影响。
- **[Phase 5B-0]** 本地完整路由验证已完成（admin token），覆盖公共门户、登录后首页、行情列表、个股详情、自选/盘中监控、管理员权限相关页面及实际路由重定向；详细结果见 `docs/maps/40-market-stock-experience.md` / `50-watchlist-intraday.md` / `60-permissions-admin.md` 的前端验证章节。

## 11. 更新触发条件

以下变化必须更新本 Map：

- 本地原生启动入口或配置变化；
- 本地重新引入 Docker 依赖；
- 远程 Compose 服务变化；
- PostgreSQL 或 Redis 连接关系变化；
- Scheduler、Worker 或队列隔离变化；
- CI、部署或稳定版本识别方式变化。

## 12. Phase 5B-2 部署脚本修复（已核验）

Phase 5B-2 修复 `scripts/deploy/panji-deploy.sh` 的若干偏差并新增静态测试。部署工作流（`.github/workflows/deploy-production.yml`）本身不变：仍监听 `workflow_run`（CI success on main）+ `workflow_dispatch`，SSH 到 `panji-prod` 调用 `/usr/local/bin/panji-deploy.sh`。链路启用状态不变（未启用）。

### 12.1 脚本修复点（已核验）

| 修复项 | 位置 | 变化 |
|---|---|---|
| `git fetch origin main` | `panji-deploy.sh:115` | SHA 校验前先 fetch，避免本地 origin 引用过期 |
| calendar 容器名 | `panji-deploy.sh:429` | `trading-worker-calendar`（非 `trading-worker-calendar-scheduler`） |
| 部署后切回 main | `panji-deploy.sh:488,507` | `git checkout main`，避免 detached HEAD |
| dry-run 措辞 | `panji-deploy.sh:369-371` | 使用"计划验证"而非"健康检查"（dry-run 不执行真实健康检查） |
| state 目录初始化 | `panji-deploy.sh:93-97` | `STATE_FILE` 父目录不存在时 `mkdir -p` 创建 |

### 12.2 静态测试（已核验）

- 文件：`scripts/deploy/panji-deploy.test.sh`
- 测试数：16 项静态断言（bash 语法、关键函数存在、calendar 容器名、dry-run 措辞、git fetch、checkout main、state 目录、不碰 postgres/redis、flock 锁、SHA 验证等）
- 运行：`bash scripts/deploy/panji-deploy.test.sh`

### 12.3 不变项

- 部署工作流不变：`workflow_run` on CI success + `workflow_dispatch`，SSH 到 `panji-prod`
- 自动部署链路启用状态不变：服务器侧 `/usr/local/bin/panji-deploy.sh`、锁文件、state 文件、GitHub Secrets 尚未配置
- 不 down -v、不删除 PostgreSQL/Redis Volume、不自动 migration 的约束不变
