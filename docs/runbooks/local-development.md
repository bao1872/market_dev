# 本地开发环境启动与停止

本 Runbook 描述如何在本地以原生进程启动盘迹 Backend 和 Frontend，并通过 SSH 隧道安全连接远程共享 PostgreSQL / Redis。

## 前置条件

- Python 3.11+ 虚拟环境 `backend/.venv` 已创建，依赖已安装。
- Node.js 20+ 和前端依赖 `frontend/node_modules` 已安装。
- `~/.ssh/config` 中已配置可连接腾讯云的 Host 别名（默认使用 `55-server`）。
- `backend/.env` 已按 `backend/.env.example` 配置：
  - `APP_ENV=development`
  - `DATABASE_URL=postgresql+psycopg://***@127.0.0.1:15432/bz_stock`
  - `REDIS_URL=redis://127.0.0.1:16379/15`

> 注意：不要把真实密码写入仓库跟踪文件。`backend/.env` 已被 `.gitignore` 排除。

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
拒绝启动：开发环境 REDIS_URL 指向 Redis DB 0（远程生产队列）
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

确认 `~/.ssh/config` 中存在对应 Host 别名，例如：

```text
Host 55-server
    HostName <服务器 IP>
    User <用户名>
    IdentityFile ~/.ssh/<私钥>
```

不要设置 `StrictHostKeyChecking=no`，保持默认 host key 校验。

## 安全边界

- 本地开发不启动 Docker 或 Docker Compose 盘迹服务。
- 不执行 Alembic migration、CREATE TABLE、TRUNCATE 或其他破坏性 SQL。
- 不启动 Scheduler 或 Worker，除非明确需要调试盘后链路。
- DB 15 为临时隔离库，尚未正式保留；在确认前不要启动 Worker。
- 不要把 `backend/.env` 或任何含密码/完整连接串的文件提交到 Git。
