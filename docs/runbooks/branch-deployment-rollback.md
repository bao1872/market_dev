# Runbook: 分支部署与镜像级回滚

- **触发条件**: 用户明确批准部署（消息：`批准分支部署，完成真实生产验收；merge前仍需停止。`）/ 已部署后发现严重问题需要回滚
- **前置条件**: 已读 AGENTS §九（分支与 PR）+ §七.10（测试期部署不备份数据库）+ §七.11（Docker 镜像保护）/ 已完成《待部署报告 V3》所有验收
- **影响范围**: backend / frontend / shared backend worker 三个服务
- **预计恢复时间**: 部署 10-20 分钟 / 回滚 5-10 分钟

## 症状识别

部署后需要回滚的典型场景：
- 后端 API 500 错误率 > 1%
- 前端白屏 / 关键页面无法加载
- 飞书消息投递完全失败
- after_close 编排卡住或失败
- 数据库 migration 不可逆错误

## 排查步骤

1. **确认部署目标 SHA**：

```bash
# 当前分支 HEAD SHA（必须与《待部署报告 V3》一致）
cd /root/web_dev
git rev-parse HEAD
git log --oneline -5
```

2. **确认镜像构建成功**：

```bash
docker images | grep -E "market-dev-(backend|frontend|worker)" | head -10
# 必须看到对应 SHA 标签的镜像
```

3. **确认 docker-compose 配置**：

```bash
cat docker-compose.prod.yml | grep -E "image:|build:"
```

## 修复操作

### 操作 1: 部署分支（按顺序：backend → frontend → shared backend worker）

⚠️ **破坏性**: 部署会重启生产容器，影响所有用户。必须用户明确批准后执行。

#### 1.1 构建镜像（带 SHA 标签）

```bash
cd /root/web_dev
export DEPLOY_SHA=$(git rev-parse HEAD)
export DEPLOY_TAG="${DEPLOY_SHA:0:12}"

docker compose -f docker-compose.prod.yml build \
  --build-arg GIT_SHA=$DEPLOY_SHA \
  --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  backend frontend worker
```

**预期输出**: 三个镜像构建成功，无 CACHED 依赖层失败。
**异常处理**: 若 `npm ci` 层失败，检查 `frontend/package-lock.json` 是否同步；若 `pip install` 层失败，检查 `backend/pyproject.toml`。

#### 1.2 推送镜像标签（便于回滚）

```bash
docker tag market-dev-backend:latest market-dev-backend:$DEPLOY_TAG
docker tag market-dev-frontend:latest market-dev-frontend:$DEPLOY_TAG
docker tag market-dev-worker:latest market-dev-worker:$DEPLOY_TAG
```

#### 1.3 部署 backend

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps backend
sleep 10
docker compose -f docker-compose.prod.yml logs backend --tail 50
```

**验证**: 日志显示 `Application startup complete`，无 traceback。

#### 1.4 部署 frontend

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps frontend
sleep 5
curl -sI https://<production-domain>/ | head -5
```

**验证**: HTTP 200，`Content-Type: text/html`。

#### 1.5 部署 shared backend worker

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps worker
sleep 5
docker compose -f docker-compose.prod.yml logs worker --tail 30
```

**验证**: worker 日志显示 `Worker started`，无异常。

#### 1.6 跑迁移（如本次有新 migration）

```bash
docker compose -f docker-compose.prod.yml exec backend alembic upgrade head
docker compose -f docker-compose.prod.yml exec backend alembic current
```

### 操作 2: 镜像级回滚（部署失败后）

⚠️ **破坏性**: 回滚会还原服务到上一个稳定 SHA。如本次部署包含不可逆 migration，回滚前需先手动 downgrade migration。

#### 2.1 确认回滚目标 SHA

```bash
# 回滚到 CP-15 (c3dfcb2) 或更早的稳定 SHA
export ROLLBACK_TAG=c3dfcb2
docker images | grep market-dev-backend | grep $ROLLBACK_TAG
```

#### 2.2 修改 docker-compose.prod.yml 镜像标签（如使用 latest）

```bash
# 方式 1: 直接使用 SHA 标签启动
docker compose -f docker-compose.prod.yml up -d --no-deps \
  backend frontend worker
# 但要先停止当前服务
docker compose -f docker-compose.prod.yml stop backend frontend worker

# 方式 2: 重新打 latest 标签指向回滚 SHA
docker tag market-dev-backend:$ROLLBACK_TAG market-dev-backend:latest
docker tag market-dev-frontend:$ROLLBACK_TAG market-dev-frontend:latest
docker tag market-dev-worker:$ROLLBACK_TAG market-dev-worker:latest
docker compose -f docker-compose.prod.yml up -d --no-deps backend frontend worker
```

#### 2.3 Migration downgrade（如本次部署有新 migration）

⚠️ **破坏性**: 必须先确认 migration 可安全 downgrade，否则会丢数据。

```bash
# 查看当前 migration
docker compose -f docker-compose.prod.yml exec backend alembic current

# downgrade 一级
docker compose -f docker-compose.prod.yml exec backend alembic downgrade -1

# 验证
docker compose -f docker-compose.prod.yml exec backend alembic current
```

## 验证

1. **服务健康检查**：

```bash
docker compose -f docker-compose.prod.yml ps
# 所有服务状态应为 Up
curl -s https://<production-domain>/api/v1/health | jq
# {"status": "healthy"}
```

2. **关键功能验证**：
   - 普通用户登录 `/market` 列表加载成功
   - 点击股票进入 `/stock/:symbol` 详情页 K 线渲染
   - 飞书手动分享功能正常（`POST /api/v1/stock-detail-feishu`）
   - 自选添加/移除功能正常

3. **生产验收证据**: 在 `docs/evidence/` 创建本次部署的验收记录，绑定最终 merge SHA 与镜像 SHA。

## 防止复发

- 部署前必须完成《待部署报告 V3》所有验收项
- 部署必须按 `backend → frontend → shared backend worker` 顺序，禁止并行
- 镜像必须打 SHA 标签（`market-dev-backend:<12位SHA>`），便于回滚
- 保留当前 + 1 rollback 镜像（`KEEP_VERSIONS=2`，`scripts/cleanup-docker.sh`）
- 禁止 `docker image prune -a` / 删除 `node:20-alpine`（AGENTS §七.11）
- 任何不可逆 migration 必须在 PR 描述中明确标注并提供 downgrade 步骤

## 关联

- AGENTS §九（分支与 PR）+ §七.10（测试期部署不备份数据库）+ §七.11（Docker 镜像保护）+ §七.21（提交安全）
- CHANGE-20260718-003（Docker 构建性能与磁盘治理）
- 《待部署报告 V3》
