# 本地开发环境启动与停止

本 Runbook 描述如何在本地以原生进程启动盘迹 Backend 和 Frontend，并通过 SSH 隧道安全连接远程共享 PostgreSQL / Redis。

## 核心数据架构规则（2026-07-28 起）

- **本地固定连接正式数据源**：PostgreSQL=`bz_stock`，Redis 使用 `backend/.env` 中的正式运行配置（本地隔离 DB）。
- **永久禁止本地 / 远程开发运行环境创建或复用任何独立/临时测试库**；独立测试库已于 2026-07-28 DROP，不得重建。
- **本地与服务器隔离边界是进程，不是数据复制**：本地只启动 Backend、Frontend、Capture 和 SSH Tunnel；Scheduler、远程常驻 Worker、盘后编排和全市场任务必须为 0。
- **本地写入均为真实业务写入**：禁止创建测试用户、测试邀请码、测试权限、测试任务、测试快照或测试通知渠道；禁止清库、批量更新、Migration、删除正式数据。
- **8752028@qq.com 为受保护 Owner 账户**：禁止修改其密码、邮箱、状态、角色、权限、订阅和业务数据。
- **本地测试只能纯单元/mock**：必须设置 `PURE_UNIT_TEST=1`；禁止连接共享开发业务数据库 `bz_stock` 或任何持久测试库。
- **已永久删除独立/临时测试数据库路线**；本地测试只允许 `PURE_UNIT_TEST=1`（纯单元）或 `PANJI_SHARED_DEV_DB_TEST=1`（经 SSH 隧道连共享开发业务数据库 `bz_stock` 的授权目标测试）。详见 `rules/40-testing-quality.md`。

## 前置条件

- Python 3.11+ 虚拟环境 `backend/.venv` 已创建，依赖已安装。
- Node.js 20+ 和前端依赖 `frontend/node_modules` 已安装。
- `~/.ssh/config` 中已配置 Host `panji-prod`（HostName 必须为 `43.136.118.82`）。
- 建隧道前可运行 `ssh -G panji-prod` 校验解析出的 `hostname` 精确等于 `43.136.118.82`；不符合时禁止启动隧道。
- 不得使用 `55-server`（解析到 `120.234.137.109`，不是盘迹远程开发运行服务器）。
- `backend/.env` 已按 `backend/.env.example` 配置：
  - `APP_ENV=development`
  - `DATABASE_URL=postgresql+psycopg://***@127.0.0.1:15432/bz_stock`（共享开发业务数据库）
  - `REDIS_URL=redis://127.0.0.1:16379/15`（本地隔离 DB，避免进入远程远程开发业务队列）

> 注意：不要把真实密码写入仓库跟踪文件。`backend/.env` 已被 `.gitignore` 排除。
> 禁止创建 `backend/.env.test` 或任何指向独立测试库的本地配置。

## 启动流程

### 1. 启动 SSH 隧道

```bash
make tunnel
```

等价于：

```bash
scripts/local/ssh-tunnel.sh start
```

隧道映射：

- `127.0.0.1:15432` -> 远程 `trading-postgres:5432`
- `127.0.0.1:16379` -> 远程 `trading-redis:6379`

检查状态：

```bash
make tunnel-status
```

### 2. 启动 Backend

```bash
make backend
```

等价于：

```bash
cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

如需不使用 `--reload`：

```bash
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 3. 启动 Frontend

在另一个终端：

```bash
make frontend
```

等价于：

```bash
cd frontend && npm run dev
```

## 验证

### 后端健康

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/version
```

期望：

- `/health` 返回 `{"status":"ok"}`
- `/health/ready` 返回 `{"status":"ready"}`
- `/version` 返回 `deployment_mode=native-development`

### 前端访问

浏览器打开 `http://127.0.0.1:8008/`，确认首页和 `/market` 可加载。

### PostgreSQL 只读核验

```bash
psql postgresql://<user>@127.0.0.1:15432/bz_stock -c "SELECT current_database(), version();"
```

本阶段只执行 SELECT，不执行 INSERT/UPDATE/DELETE/TRUNCATE/DROP。

### Redis 只读核验

```bash
redis-cli -p 16379 -n 15 PING
redis-cli -p 16379 -n 15 DBSIZE
```

本阶段只执行 PING / DBSIZE，不写 Key。

### 确认无 Scheduler / Worker

```bash
ps aux | grep -E 'scheduler|worker|celery' | grep -v grep
```

应无盘迹相关 Scheduler 或 Worker 进程。

### 全路由验证（Phase 5B-0 起）

本地完整原生运行验证应覆盖前端所有实际路由。路由清单从 `frontend/src/App.tsx` 的 `routeConfig` 导出读取，不猜测路径。

**前置条件**：

- Backend / Frontend / SSH 隧道已启动并通过健康检查；
- 持有现有合法全权限管理员账号的 JWT token（通过 `/api/v1/auth/login` 获取）；
- 禁止绕过认证、修改权限模型或创建硬编码管理员。

**验证方式**：

- HTTP 状态码（curl）；
- 主要 API 响应（curl + jq）；
- 前端页面运行错误（浏览器 console，但不安装浏览器自动化依赖）；
- 不以截图作为唯一证据。

**覆盖范围**（基于 `App.tsx` routeConfig）：

| 类别 | 路由 | 主要 API |
|---|---|---|
| 公开 | `/`、`/login`、`/subscription-expired`、`/membership-expired`、`/capture/stock/:symbol` | - |
| 用户级 | `/market`、`/replay`、`/stock/:symbol`、`/settings`、`/messages` | `/market/stocks`、`/market/boards`、`/market/status`、`/strategies`、`/api/v1/stocks/{symbol}/context`、`/api/v1/instruments/{id}/bars`、`/indicators`、`/structural-factors`、`/temporal-features`、`/quote`、`/chart-snapshot`、`/me`、`/me/access`、`/messages` |
| 管理员 | `/admin`、`/admin/users`、`/admin/beta-applications`、`/admin/after-close/pipeline`、`/admin/jobs`、`/admin/strategies`、`/admin/stocks/:symbol/debug`、`/admin/audit-logs`、`/admin/members`、`/admin/message-deliveries` | `/admin/system-overview`、`/admin/users`、`/admin/beta-applications`、`/admin/after-close/pipeline/latest`、`/admin/scheduler-job-runs`、`/admin/worker-heartbeats`、`/api/v1/admin/stocks/{symbol}/debug`、`/admin/audit-logs`、`/admin/members`、`/admin/message-deliveries` |
| 重定向 | `/overview`、`/watchlist`、`/screener`、`/admin/strategies`、`/admin/stock-debug/:symbol`、`*` | SPA 客户端重定向 |

**[Phase 5B-1 已修复]**：本地 Vite 下 `/` 已修复为一次性跳转 `/portal/index.html`（`LandingPage.tsx` DEV 模式 `window.location.replace('/portal/index.html')`），不再无限刷新；生产环境由 Nginx 精确分流，行为不变；若生产 Nginx 误配置进入 SPA，显示稳定入口链接（不跳转当前 URL）。测试：`node --test frontend/src/pages/__tests__/landingPageRoot.test.mjs`（7 测试）。

**记录内容**：每个路由的页面加载状态、主要 API 成功/失败、数据展示、权限正确性、阻塞原因。详细结果记录在 `docs/maps/40-market-stock-experience.md` / `50-watchlist-intraday.md` / `60-permissions-admin.md` 的前端验证章节。

## 停止流程

### 停止 Frontend

在 Frontend 终端按 `Ctrl+C`。

### 停止 Backend

在 Backend 终端按 `Ctrl+C`。

### 停止 SSH 隧道

```bash
make tunnel-stop
```

等价于：

```bash
scripts/local/ssh-tunnel.sh stop
```

## 故障处理

### 隧道启动失败：端口被占用

检查并停止占用 15432 或 16379 的进程：

```bash
lsof -iTCP:15432 -sTCP:LISTEN
lsof -iTCP:16379 -sTCP:LISTEN
```

### Backend 启动失败：Redis DB 0

错误示例：

```text
拒绝启动：开发环境 REDIS_URL 指向 Redis DB 0（远程远程开发业务队列）
```

解决：将 `backend/.env` 中的 `REDIS_URL` 改为独立逻辑 DB，例如：

```text
REDIS_URL=redis://127.0.0.1:16379/15
```

### Backend 启动失败：缺少 DATABASE_URL / REDIS_URL

错误示例：

```text
DATABASE_URL 未设置
REDIS_URL 未设置
```

解决：确认 `backend/.env` 存在并包含上述配置。

### 无法连接 SSH Host

确认 `~/.ssh/config` 中存在 `Host panji-prod` 且 `HostName` 为 `43.136.118.82`：

```text
Host panji-prod
    HostName 43.136.118.82
    User root
    Port 22
    IdentityFile ~/.ssh/id_rsa
```

校验方式：

```bash
ssh -G panji-prod | grep hostname
# 必须输出: hostname 43.136.118.82
```

不要设置 `StrictHostKeyChecking=no`，保持默认 host key 校验。禁止使用 `55-server`。

## 安全边界

- 本地开发不启动 Docker 或 Docker Compose 盘迹服务。
- **本地固定连接共享开发业务数据库 `bz_stock`**；禁止连接任何独立测试库或创建新的独立/临时测试库。
- 不执行 Alembic migration、CREATE TABLE、TRUNCATE 或其他破坏性 SQL。
- **禁止本地启动 Scheduler、远程常驻 Worker、盘后编排或全市场任务**；本地只启动 Backend、Frontend、Capture 和 SSH Tunnel。
- 禁止创建测试用户、测试邀请码、测试权限、测试任务、测试快照或测试通知渠道。
- **8752028@qq.com 为受保护 Owner 账户**：禁止修改其密码、邮箱、状态、角色、权限、订阅和业务数据。
- 不得在命令、日志、浏览器自动化或报告中写入 Owner 真实密码；TRAE 不得自动登录 Owner 账户，登录由用户手工完成。
- Redis DB 15 为本地隔离逻辑 DB，避免进入远程远程开发业务队列；不要把 `backend/.env` 或任何含密码/完整连接串的文件提交到 Git。
