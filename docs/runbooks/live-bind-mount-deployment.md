# Live Bind Mount 部署 Runbook

> [CHANGE-20260724-004] 个股详情与 Live Mount 部署

## 概述

Live Mount 部署模式将运行时代码通过只读 bind mount 挂载到容器内，
实现代码热更新而无需重建镜像。适用于纯 Python/前端代码变更，
不适用于依赖变更或 Dockerfile 变更。

## 文件结构

```
/opt/panji-live/
├── backend/
│   ├── app/              # Python 应用源码（rsync --delete 同步）
│   ├── alembic/          # 数据库迁移脚本
│   └── alembic.ini       # Alembic 配置
├── frontend/
│   └── dist/             # 前端构建产物
└── RUNTIME_SHA           # 当前运行时 git SHA
```

## 挂载矩阵

| 服务 | 挂载源 | 容器路径 | 权限 |
|------|--------|----------|------|
| backend + 所有 Python worker | /opt/panji-live/backend/app | /app/app | ro |
| backend + 所有 Python worker | /opt/panji-live/backend/alembic | /app/alembic | ro |
| backend + 所有 Python worker | /opt/panji-live/backend/alembic.ini | /app/alembic.ini | ro |
| backend + 所有 Python worker | /opt/panji-live/RUNTIME_SHA | /app/RUNTIME_SHA | ro |
| frontend | /opt/panji-live/frontend/dist | /usr/share/nginx/html | ro |
| frontend | capture_static (volume) | /usr/share/nginx/html/static/captures | ro |

## 部署流程

### 首次部署

```bash
# 1. 构建前端
cd /root/web_dev/frontend
NODE_OPTIONS=--max-old-space-size=1024 npm run build

# 2. 执行完整部署
cd /root/web_dev
bash scripts/deploy_live_runtime.sh
```

### 仅更新 Python 代码

```bash
cd /root/web_dev
bash scripts/sync_live_runtime.sh
# 脚本会自动停止→同步→重启应用容器
```

### 仅更新前端代码

```bash
cd /root/web_dev/frontend
NODE_OPTIONS=--max-old-space-size=1024 npm run build

cd /root/web_dev
bash scripts/sync_live_runtime.sh --skip-stop
# 手动重启 frontend
docker compose -f docker-compose.prod.yml -f docker-compose.live.yml restart frontend
```

### 需要重建镜像的场景

- pyproject.toml 依赖变更
- Dockerfile 变更
- 基础镜像升级

```bash
cd /root/web_dev
docker compose -f docker-compose.prod.yml build backend
docker compose -f docker-compose.prod.yml -f docker-compose.live.yml up -d --force-recreate
```

## 验证

```bash
# /version 返回 runtime_git_sha
curl http://localhost:8000/version | python -m json.tool

# deployment_mode 应为 "live"
# runtime_git_sha 应等于 main HEAD
```

## 回滚

```bash
# 回滚到镜像内置代码（移除 live mount）
docker compose -f docker-compose.prod.yml up -d --force-recreate

# 回滚到指定 SHA
git checkout <sha>
bash scripts/sync_live_runtime.sh
```

## 约束

- 所有挂载为只读 (`:ro`)
- 同步期间必须停止应用容器，避免读取半写入文件
- rsync 使用 `--delete` 保持目标与源完全一致
- 不复制 .git、docs、tests、node_modules、缓存
