# 开发部署 Runbook（Live Mount 开发部署）

本 Runbook 是盘迹**唯一**的部署操作权威来源。它只描述开发阶段的部署闭环。
禁止在本文件定义其他阶段，禁止描述未来正式发布方式。

权威来源（三者一致，冲突时以本文件 + `rules/80-deployment-data-safety.md` +
`docs/maps/80-system-runtime.md` 为准）：

- `rules/80-deployment-data-safety.md`（部署与数据安全硬约束）
- `docs/maps/80-system-runtime.md`（已核验的当前运行事实）
- 本文件（可重复执行的操作步骤）

> 其他部署相关历史文档（如旧 `production-deployment.md` 描述的 `main` 自动部署 /
> GHCR pull-only / Release Gate）已废止，不作为当前操作指令。

## 0. 当前唯一开发闭环

```
本地开发
→ 修改范围测试（单元 + 静态检查；CI 非前置条件）
→ 精确 commit（git add <file>，禁止 git add ./-A/-u）
→ push origin/dev
→ 服务器 checkout 精确 dev SHA
→ Live Mount 同步运行代码到 /opt/panji-live
→ 重启受影响服务
→ health / version / 业务 smoke
→ 停止
```

CI 可保留为手工诊断工具（分类测试、全量回归、集成测试），**不进入默认开发闭环**，
不作为部署门禁。本地测试失败禁止部署；本地无法运行测试时如实报告，不得用 CI 或
服务器测试掩盖。

## 1. 部署合同（硬约束）

### 1.1 普通 Python 代码变化

- **不 build 镜像**；
- 同步 `backend/app` 等运行代码到 `/opt/panji-live`；
- 通过 `docker-compose.prod.yml` + `docker-compose.live.yml` 叠加，重启受影响的
  Python 服务（backend 及所有 worker / capture）。

### 1.2 普通前端代码变化

- 执行 `frontend build` 生成 `dist`；
- 同步 `dist` 到 `/opt/panji-live/frontend/dist`；
- 重启 frontend；
- **不 build 前端 Docker 镜像**。

### 1.3 只有以下变化才 build 对应镜像

- `pyproject.toml` 或 Python 依赖锁；
- `package.json` / `package-lock.json`；
- `Dockerfile` / `Dockerfile.capture`；
- 系统依赖；
- 基础镜像；
- Capture 浏览器运行环境；
- 必须烘焙进镜像的 Nginx 配置。

### 1.4 互斥约束

- **单次部署禁止同时使用 Live Mount 代码和新镜像内置代码**，避免运行时来源不明确；
- 普通变更走 Live Mount，依赖 / Dockerfile 变化才走镜像构建，二者不混用。

### 1.5 精确 SHA 与验证

- 必须部署 **exact dev SHA**；
- 必须验证 `runtime_git_sha`（= `/opt/panji-live/RUNTIME_SHA`）等于目标 dev SHA；
- 必须验证服务器 repo HEAD 等于目标 dev SHA；
- `/health` 正常不能单独判成功，两项 SHA 一致才是部署成功判据。

### 1.6 数据边界

- 代码部署**不自动执行**任何数据 apply / run / publish 操作；
- migration 仅在确有新 migration 时由部署脚本显式、幂等地执行，且不属于"自动数据发布"。

## 2. 前置条件

1. 目标 SHA 已 `git push origin dev`；
2. 修改范围内的单元测试与静态检查已在本地通过（失败禁止部署）；
3. 当前无活跃盘后 / 正式任务，避免部署中断正在运行的正式流程；
4. 通过 `scripts/ops/panji-prod-preflight` 校验（SSH 别名、仓库根、repo status、
   docker compose、DB/Redis 端口）。

## 3. 执行入口

当前正式部署入口仍为 `scripts/ops/panji-test-deploy <SHA>`（经 `scripts/ops/panji-prod-ssh`
唯一 SSH 入口）。执行结构：

```
preflight → 校验目标 dev SHA → 同步运行代码到 /opt/panji-live（rsync --delete） →
逐服务重启受影响服务 → 逐服务校验 RUNTIME_SHA == 目标 SHA → health/version 业务 smoke
```

### 3.1 执行

```bash
# 1) dry-run 检查计划（不改任何状态）
scripts/ops/panji-test-deploy <FULL_SHA> --dry-run

# 2) 核对 dry-run：目标 SHA、待重启服务清单、postgres/redis 已排除、资源门禁结果

# 3) 正式部署（Live Mount 同步 + 重启受影响服务，普通变更不 build 镜像）
scripts/ops/panji-test-deploy <FULL_SHA>
```

`postgres` / `redis` 为有状态服务，明确排除，不参与重启，避免触碰持久化数据。

## 4. 版本与 SHA 核验

当前运行后端版本端点路径以实测为准（早期镜像暴露 `/version` / `/api/v1/version`，
Live Mount 模式以后端实际路由为准）。SHA 一致性证据：

```bash
# 服务器 repo HEAD 必须等于目标 dev SHA
ssh panji-prod 'cd /root/web_dev && git rev-parse HEAD'

# RUNTIME_SHA 文件必须等于目标 dev SHA
ssh panji-prod 'cat /opt/panji-live/RUNTIME_SHA'

# 受影响服务容器的运行代码来源（live 挂载点）应指向 /opt/panji-live
ssh panji-prod 'docker inspect trading-backend --format "{{json .Mounts}}"'
```

任一不一致即判部署失败，回到上一已知良好 dev SHA 重新同步，不得通过"重启容器"
或"重新部署"掩盖不一致。

## 5. 健康检查与业务 smoke

```bash
# 端口 80
curl -sf http://127.0.0.1:80 >/dev/null && echo "port 80 OK"

# 后端健康（以实际暴露端点为准）
curl -s http://127.0.0.1:8000/health

# 版本 SHA
curl -s http://127.0.0.1:8000/version

# 关键容器状态
docker ps --format "table {{.Names}}\t{{.Status}}" | grep trading-
```

## 6. 回滚

普通变更（Live Mount）：重新同步上一已知良好 dev SHA 的运行代码到 `/opt/panji-live`
并重启受影响服务。

镜像构建变更：切回上一镜像并重启。

均不回滚数据库，不执行 `docker compose down -v`，不删除 PostgreSQL / Redis Volume。

## 7. 禁止操作（黑名单）

1. `scp file.py panji-prod:/app/...` 手工单文件同步；
2. `docker cp local.py trading-backend:/app/...` 容器内手工覆盖；
3. SSH 进容器 `vi / sed` 修改源代码 / migration；
4. 一次性业务脚本（`python -c '...'` 直接改生产）；
5. `docker compose down -v` 或 `rm -rf /var/lib/docker/volumes/trading_*`；
6. 单次部署混用 Live Mount 代码与新建镜像；
7. 部署时自动执行数据 apply / run / publish。
