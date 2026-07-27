# CHANGE-20260726-002：本地与远程运行模型

状态：进行中
日期：2026-07-26
类型：runtime
领域：运行体系
负责人：AI 代理（第一阶段本地核验）

相关 PRD：

- `../../prd/80-system-runtime.md`：SR-01～SR-62
- `../../prd/30-after-close.md`：AC-01～AC-03

相关 Maps：

- `../../maps/80-system-runtime.md`
- `../../maps/30-after-close.md`
- `../../maps/technical/data-storage.md`

相关提交或 PR：

- 本地基线提交 `a817595 chore: align local runtime setup with native Python/Node.js`
- 本地基线提交 `eaffb11 docs: restructure docs and rules layout`
- 第二阶段工作区修改包含 `backend/app/config.py`、`backend/app/main.py`、`backend/app/api/health.py`、`backend/tests/test_config_validation.py`、`backend/tests/test_main_lifespan.py`、`Makefile`、`backend/README.md`、`scripts/local/ssh-tunnel.sh` 及多项文档更新

替代：

- 将 IDE、开发位置、运行环境和容器承载方式混为一谈的旧认知

被替代：

- 无

## 1. 摘要

明确盘迹当前只有本地开发和远程稳定运行两个位置：

- 本地直接在 `dev` 开发，使用原生 Python、Node.js 和 Vite 进程；
- 腾讯云稳定版本使用 `main` 或明确稳定 SHA，通过 Docker Compose 运行；
- PostgreSQL 共享；
- Redis 同实例但按逻辑数据库隔离任务状态（本地 DB 15，远程 DB 0）；
- 本地关闭自动 Scheduler，但保留完整手动盘后调试能力；
- 本地和远程共享同一业务代码，不因承载方式不同维护两套实现。

## 2. 背景与问题

此前对 TRAE CN、腾讯云、本地、远程和 Docker 的角色边界存在混淆，可能导致：

- 把 IDE 误认为独立环境；
- 把腾讯云 Docker 运行方式强加给本地开发；
- 本地每次修改都需要构建镜像和重启 Compose；
- 本地 Docker 占用额外磁盘和内存；
- 为本地原生进程和远程容器维护不同业务代码；
- 本地任务进入远程 Redis 队列；
- 本地无法方便地单独调试后端、前端或指定 Worker；
- 开发分支推送与远程自动部署绑定；
- 对共享数据库采取过度只读或错误隔离方案。

## 3. 变化前

旧认知和实际状态需要通过代码与配置审计确认。

- `Makefile` 原 `up` / `down` 目标引用 `docker-compose.yml`，引导本地启动 Redis 容器；
- `backend/README.md` 引导执行 `make up`；
- `.env.example` 的 Redis URL 未明确区分本地与远程 DB；
- 本地 Redis 逻辑 DB 编号未确认；
- 本地原生启动入口未核验。

## 4. 变化内容

### 运行位置

- 运行位置只区分本地和远程；
- IDE 只是工具。

### 本地原生开发

- 本地日常开发直接使用 `dev`；
- 后端使用 Python 虚拟环境直接启动；
- 前端使用 Node.js 和 Vite 直接启动；
- 指定 Worker 和 Orchestrator 按需以 Python 进程手动启动；
- 本地不使用 Docker 或 Docker Compose 启动盘迹应用服务；
- 本地不创建盘迹 PostgreSQL 或 Redis 容器；
- 本地自动 Scheduler 关闭。

### 远程容器运行

- `main` 用于远程稳定版本；
- 腾讯云继续通过 Docker Compose 运行后端、前端、Worker、Scheduler、PostgreSQL、Redis、Nginx 等正式服务；
- 远程保持每日盘后自动运行和手动补跑能力。

### 代码与配置

- `dev` 推送只触发 CI，不自动部署；
- 本地和远程共享 PostgreSQL；
- 本地可正常读写、修复、回填和重算；
- Redis 使用同一实例、不同逻辑 DB；
- 本地和远程复用同一业务代码、数据模型、Worker、Migration、指标和配置字段；
- 差异仅通过配置值和进程承载方式表达。

## 5. 变化后

目标运行关系：

```text
本地开发
├── Backend：Python 虚拟环境原生启动
├── Frontend：Node.js / Vite 原生启动
├── Worker：按需以 Python 进程启动
├── Scheduler：默认关闭
├── PostgreSQL：直接连接共享实例
└── Redis：直接连接共享实例的本地专用逻辑 DB

腾讯云稳定运行
└── Docker Compose
    ├── Backend
    ├── Frontend
    ├── Workers
    ├── Scheduler
    ├── PostgreSQL
    ├── Redis
    └── Nginx
```

实际配置、启动入口、服务和代码关系以 `maps/80-system-runtime.md` 为准。

## 6. 影响范围

### 本地开发体验

不再依赖 Docker Desktop、镜像构建和本地 Compose。后端、前端和指定 Worker 可独立启动、停止和调试。

### Git 与 CI

开发、稳定和部署边界发生变化。

### PostgreSQL

不按本地和远程拆分两套核心行情数据。

### Redis

保持代码一致性，同时隔离队列、锁、缓存和临时状态。

### Scheduler

自动触发与手动任务执行能力分离。

### 腾讯云部署

继续保留容器化稳定运行，不因本地原生开发而取消 Docker Compose。

### 数据安全

共享数据库提高了调试真实性，也提高了破坏性操作风险。

## 7. 迁移与兼容

已处理：

- `Makefile`：`dev` / `backend` / `frontend` 改为原生进程；`up` / `down` 标记废弃并给出警告；新增 `tunnel` / `tunnel-status` / `tunnel-stop`；
- `backend/.env.example`：明确本地 Redis 必须使用独立逻辑 DB（如 `/15`）；
- `backend/README.md`：移除 `make up` 引导，改为原生启动说明，增加隧道和 DB 0 校验说明；
- `backend/.env`：本地 gitignored 配置，DATABASE_URL 指向隧道端口 15432，REDIS_URL 指向隧道端口 16379/15；
- `backend/app/config.py`：development 环境缺失 DATABASE_URL / REDIS_URL 时启动失败；Redis DB 0 时启动失败；禁止回退 localhost 默认数据库或 `redis://localhost:6379/0`；production 行为不变；
- `backend/app/main.py`：development 环境 lifespan 默认跳过 `recover_stale_scheduler_job_runs`、`seed_strategies`、`seed_calendar_from_mootdx`；
- `backend/app/api/health.py`：本地原生启动时 `/version` 返回 `deployment_mode=native-development`；
- `scripts/local/ssh-tunnel.sh`：新建可重复 SSH 隧道脚本，使用 `~/.ssh/config` Host 别名，动态获取容器 IP，端口占用即失败；
- 测试：`backend/tests/test_config_validation.py` 增加 Redis URL fail-closed 测试；`backend/tests/test_main_lifespan.py` 增加 development 跳过维护写入测试。

仍待处理：

- `docker-compose.yml` 本地文件仍保留 redis 服务，建议在确认无调用方后删除或重命名；
- 远程稳定 SHA、Compose 服务、Scheduler / Worker 运行状态待下一阶段核验；
- DB 15 为临时使用，需项目层面确认是否正式保留。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| dev 已创建并 rebase origin/dev | Git | 已确认 | `git branch --show-current=dev`，基于 `origin/dev` 领先 2 个提交 |
| 本地 Backend 原生启动 | 本地 | 已验证 | 监听 0.0.0.0:8000；curl /health 返回 200 |
| 本地 Frontend 原生启动 | 本地 | 已验证 | 监听 0.0.0.0:8008；首页和 /market 返回 HTML |
| 本地不依赖 Docker | 本地 | 已验证 | 未执行 `docker compose up`；本地无盘迹容器 |
| 本地不启动 PostgreSQL/Redis 容器 | 本地 | 已验证 | 通过 SSH 隧道连接远程容器 |
| PostgreSQL 共享 | 配置与运行 | 已验证 | SELECT 1 / current_database=bz_stock / version=PostgreSQL 16.14 |
| Redis 逻辑 DB 隔离 | 本地/远程 | 已验证 | 本地 DB 15（临时）DBSIZE=0；DB 0 启动被 `config.py` 拒绝 |
| 本地 Scheduler 关闭 | 本地 | 已验证 | 无 scheduler 进程；后端日志无 scheduler 启动 |
| 本地 Worker 未启动 | 本地 | 已验证 | 无 worker 进程 |
| 前端访问后端 | 本地 | 已验证 | `/api/health` 代理返回 `{"status":"ok"}` |
| 配置 fail-closed | 单元测试 | 通过 | `test_config_validation.py` 通过 |
| lifespan 本地无写入 | 单元测试 | 通过 | `test_main_lifespan.py` 通过 |
| 本地完整盘后手动运行 | 本地 | 未验证 | 未启动 Worker / Orchestrator |
| 远程 Docker Compose 运行 | 腾讯云 | 未验证 | 未操作远程 |
| 远程每日盘后运行 | 腾讯云 | 未验证 | 未操作远程 |
| 两端复用同一业务代码 | 代码审计 | 已验证 | 本地与远程共用 `app.main:app`、`frontend/src`、`app.worker.py` |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | `prd/80-system-runtime.md` 增加 SR-32 本地 Redis 安全启动、SR-43 本地启动默认不写入共享库 |
| Maps | `maps/80-system-runtime.md`、`maps/technical/codebase-modules.md`、`maps/technical/data-storage.md` 已按实际运行结果更新 |
| Runbooks | 新建 `runbooks/README.md`、`runbooks/local-development.md` |
| Rules | 未修改 |

## 10. 回滚方案

腾讯云 Docker Compose 继续保留，因此本地原生开发方案不影响远程稳定运行。

如果本地原生启动暂时存在阻塞：

- 可以暂停本地运行修复；
- 不得默认回退为长期本地 Docker 开发方案；
- 不得为本地和远程复制业务代码；
- 应修复原生启动入口、依赖或配置。

数据库和 Redis 方案变化涉及核心数据和任务状态，不能通过清空、重建或删除 Volume 回滚。

## 11. 遗留问题与风险

- 本地 `docker-compose.yml` 仍存在，可能误导开发者；
- 远程运行细节（Compose 服务、Scheduler、Worker、Nginx、Volume）未核验；
- 本地与远程共享 PostgreSQL，开发中需避免破坏性操作；
- DB 15 为临时使用，需项目层面确认是否正式保留；
- 完整本地盘后运行尚未验证。

## 12. 后续变化

完成代码、配置和运行核验后，补充提交、测试和运行证据，再决定是否标记为“已完成”。

下一阶段建议：

1. 删除或重命名本地 `docker-compose.yml`（确认无调用方后）；
2. 修改 `backend/app/config.py` 本地 Redis 默认值为 DB 15 或强制要求显式配置；
3. 远程运行核验：容器 SHA、Compose 服务、Scheduler / Worker 状态；
4. 验证指定 Worker 在本地 DB 15 下运行不进入远程队列。
