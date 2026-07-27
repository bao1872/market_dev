# 系统运行体系 Map

核验状态：已基于本地原生启动核验（第一阶段）
最后核验日期：2026-07-26
核验分支：dev
核验提交：06bf5109b07a966207e7203e2b2ba12c7e12388d
核验范围：本地原生 Backend / Frontend 启动、共享 PostgreSQL / Redis 连接、Scheduler / Worker 默认关闭
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
| SR-60 至 SR-62 部署、Volume 和 Nginx | 远程 `docker-compose.prod.yml` 保留；本地不启动 Nginx 容器 | 未核验 | 本阶段只读不修改远程 |

## 2. 运行位置

| 位置 | 代码目录 | 分支/SHA | 承载方式 | 主要用途 | 自动 Scheduler |
|---|---|---|---|---|---|
| 本地 | `/Users/zhenbao/Desktop/coding/market_dev` | `dev` / `405d3ee` | 原生 Python 3.11 venv + Uvicorn；Node.js + Vite | 开发和手动调试 | 关闭 |
| 远程 | `/root/web_dev` | `main`/稳定 SHA 待核验 | Docker Compose | 稳定运行 | 应开启，未核验 |

### 远程服务器身份

| 项目 | 值 |
|---|---|
| 角色 | 腾讯云稳定运行服务器 |
| 权威公网 IP | `43.136.118.82` |
| 项目 SSH 别名 | `panji-prod`（定义于 `~/.ssh/config`，HostName 必须为 `43.136.118.82`） |
| 远程代码目录 | `/root/web_dev` |
| 生产 Compose | `docker-compose.prod.yml` |
| 自动部署状态 | 未实施；当前 `main` 合并不触发自动部署，需手动 SSH 部署 |

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
| `dev` | 本地当前分支；已跟踪 `origin/dev`；本地领先 3 个提交（`a817595`、`eaffb11`、`405d3ee`） |
| `main` | 远程稳定分支，最新 SHA 未核验 |
| dev push | 本阶段未 push；按 PRD 应只触发 CI，不自动部署 |
| main 自动部署 | 未实施；PRD 目标为 `main` 经 PR 合并且 CI 通过后自动部署腾讯云，本轮只写目标不实现 workflow |
| CI gate | 未核验 |

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
| 逻辑 DB | DB 15（临时使用，尚未正式保留） | DB 0 |
| 队列 | 未启动 Worker，无队列写入 | 未核验 |
| 锁 | 未使用 | 未核验 |
| 缓存 | 未启用 | 未核验 |

> DB 15 为第二阶段本地开发临时选定的隔离逻辑库，尚未获得项目层面的正式保留依据。在未确认前，不得启动任何 Worker。

## 7. 远程 Docker Compose 服务

本节只记录腾讯云远程稳定运行，不作为本地启动依据。

| 服务 | Compose 定义 | 依赖 | 端口 | Volume |
|---|---|---|---|---|
| frontend | `docker-compose.prod.yml` | 待核验 | 待核验 | 待核验 |
| backend | `docker-compose.prod.yml` | 待核验 | 待核验 | 待核验 |
| scheduler | `docker-compose.prod.yml` | 待核验 | - | 待核验 |
| workers | `docker-compose.prod.yml` | 待核验 | - | 待核验 |
| nginx | `docker-compose.prod.yml` | frontend/backend | 80 | 待核验 |
| PostgreSQL | `docker-compose.prod.yml` | - | 5432 | 核心数据 |
| Redis | `docker-compose.prod.yml` | - | 6379 | 持久化待核验 |

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

- 容器对应 SHA：未核验
- 前端和后端是否为同一稳定版本：未核验
- 端口 80 页面：未核验
- Scheduler 是否运行：未核验
- Worker 是否消费远程 Redis DB：未核验
- 数据服务未被误重建：未操作远程

## 10. 已知偏差与风险

- 本地 `docker-compose.yml` 仍保留 redis 服务，虽然 `Makefile` 已标记 `up/down` 废弃，但文件本身仍可能误导新开发者。本轮按任务要求不删除，仅标记“非本地开发入口”。
- 本地 Redis DB 15 为临时使用，尚未获得项目层面正式保留依据；在确认前不得启动 Worker。
- 本地与远程共享 PostgreSQL，开发中的破坏性操作需要额外注意；当前未做权限只读限制。
- 远程运行细节（Compose 服务、Scheduler、Worker、Nginx、Volume）本阶段未核验。

## 11. 更新触发条件

以下变化必须更新本 Map：

- 本地原生启动入口或配置变化；
- 本地重新引入 Docker 依赖；
- 远程 Compose 服务变化；
- PostgreSQL 或 Redis 连接关系变化；
- Scheduler、Worker 或队列隔离变化；
- CI、部署或稳定版本识别方式变化。
