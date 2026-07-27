# 生产环境自动部署 Runbook

本 Runbook 描述盘迹生产环境（腾讯云 `panji-prod`）的自动部署机制、启用前准备、手动操作和回滚步骤。

> 当前状态：部署代码已准备（`scripts/deploy/panji-deploy.sh`、`.github/workflows/deploy-production.yml`），但 GitHub Secrets 和服务器侧入口尚未启用。启用前必须完成本清单并经过 dry-run 验证。

## 1. 设计约束

- 只部署 `main` 分支上的 commit，且必须属于 `origin/main`；
- `dev` 推送只触发 CI，不自动部署；
- 部署脚本接收精确 SHA，工作区不干净即失败；
- 默认使用 Live Mount（`docker-compose.prod.yml` + `docker-compose.live.yml`），不重建镜像；
- 依赖 / Dockerfile / 基础镜像变化时按 `image` 范围重建镜像；
- 纯文档 / 治理 / 部署脚本变更跳过应用部署；
- 永不执行 `docker compose down -v`；
- 不删除 / 重建 `postgresdata`、`redisdata` 等持久化卷；
- 不自动执行 Alembic migration；
- 部署失败自动回滚代码和应用容器，但不回滚数据库；
- 部署串行（`flock`），不取消进行中的部署。

## 2. GitHub Secrets

在仓库 Settings -> Secrets and variables -> Actions 中配置以下 Secrets（仅名称，不含值）：

| Secret 名称 | 用途 | 示例值说明 |
|---|---|---|
| `PANJI_PROD_HOST` | 生产服务器公网 IP | `43.136.118.82` |
| `PANJI_PROD_USER` | SSH 登录用户 | `root` |
| `PANJI_PROD_SSH_KEY` | SSH 私钥（推荐专用 deploy key） | PEM 格式私钥 |

> 不要将数据库密码、JWT secret 或其他应用 secret 作为 GitHub Actions secret 传给部署脚本。

## 3. 服务器侧准备清单

在 `panji-prod`（`43.136.118.82`）上完成以下步骤：

### 3.1 部署脚本安装

```bash
# 将脚本从仓库复制到固定位置
cp /root/web_dev/scripts/deploy/panji-deploy.sh /usr/local/bin/panji-deploy.sh
chmod +x /usr/local/bin/panji-deploy.sh

# 校验语法
bash -n /usr/local/bin/panji-deploy.sh
```

### 3.2 状态与锁目录

```bash
# 锁文件（flock 使用）
touch /var/lock/panji-deploy.lock

# 状态文件（记录 previous/last-good SHA）
touch /etc/market-dev/.panji-deploy-state
chmod 600 /etc/market-dev/.panji-deploy-state
```

### 3.3 环境文件确认

确认 `/etc/market-dev/market.env` 已存在且权限不宽于 `600`：

```bash
ls -l /etc/market-dev/market.env
stat -c '%a' /etc/market-dev/market.env
```

只确认存在和变量名，不回显值：

```bash
grep -E "^[A-Z_]+=" /etc/market-dev/market.env | awk -F= '{print $1}'
```

### 3.4 必需命令确认

```bash
command -v git docker flock rsync curl
```

### 3.5 仓库状态确认

```bash
cd /root/web_dev
git branch --show-current  # 应为 main
git status --short         # 应为空
git remote -v              # 应能访问 origin
```

### 3.6 GitHub 部署公钥

如果使用专用 deploy key，将公钥添加到 `/root/.ssh/authorized_keys`，建议配置 `command="/usr/local/bin/panji-deploy.sh"` 或 `restrict` 等 forced-command 限制（可选，待后续安全加固阶段实施）。

## 4. GitHub Environment

在仓库 Settings -> Environments 中创建 `production` 环境，建议配置：

- Required reviewers：至少一名维护者；
- Deployment branches：限制为 `main`；
- Wait timer：按需设置（如 0 秒）。

## 5. 启用自动部署

完成以上清单后：

1. 将 `.github/workflows/deploy-production.yml` 合并到 `main`；
2. 确保 `main` 分支的 CI workflow（`.github/workflows/ci.yml`）名称与 `workflow_run.workflows` 中 `"CI"` 完全一致；
3. 向 `main` 发起 PR 并合并；
4. CI 通过后，自动触发 Deploy Production workflow；
5. 首次触发前建议先用 `workflow_dispatch` 对当前 `main` HEAD 执行 dry-run。

## 6. 手动部署

在 GitHub Actions 页面选择 `Deploy Production` workflow，点击 `Run workflow`，输入要部署的 main 分支 commit SHA，即可手动触发。

也可以在服务器本地执行（用于紧急调试）：

```bash
ssh panji-prod
/usr/local/bin/panji-deploy.sh <SHA>
```

## 7. 安全 dry-run

在正式部署前，使用 dry-run 模式检查计划命令和影响范围：

```bash
/usr/local/bin/panji-deploy.sh <SHA> --dry-run
```

dry-run 只输出计划，不执行：

- git checkout；
- 镜像构建；
- rsync 同步；
- 容器重建 / 启动；
- 状态文件写入。

## 8. 回滚

### 8.1 自动回滚

部署脚本在 health check 失败时会自动回滚到 `STATE_FILE` 中记录的 previous SHA。

### 8.2 手动回滚

```bash
ssh panji-prod
cd /root/web_dev
# 查看上一次成功 SHA
cat /etc/market-dev/.panji-deploy-state

# 手动检出上一个 SHA 并重新同步（如需）
git checkout -f <PREVIOUS_SHA>
bash scripts/sync_live_runtime.sh --skip-stop

# 重新创建应用容器（不回滚数据库）
docker compose --env-file /etc/market-dev/market.env \
  -f docker-compose.prod.yml -f docker-compose.live.yml \
  up -d --force-recreate --no-build \
  backend frontend \
  worker-bars-scheduler worker-strategy-scheduler worker-calendar \
  worker-monitor worker-strategy-batch worker-outbox worker-delivery \
  worker-after-close worker-watchdog worker-capture
```

## 9. 部署后验证

```bash
# 端口 80
curl -sf http://127.0.0.1:80 >/dev/null && echo "port 80 OK"

# 后端健康
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/ready

# 版本 SHA
curl http://127.0.0.1:8000/version

# 关键容器
docker ps --format "table {{.Names}}\t{{.Status}}" | grep trading-

# Scheduler 单实例（每类一个）
for s in trading-worker-bars-scheduler trading-worker-strategy-scheduler trading-worker-calendar-scheduler; do
  docker ps --format '{{.Names}}' | grep -cx "${s}"
done
```

## 10. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 部署锁失败 | 另一个部署正在进行 | 等待或检查 `/var/lock/panji-deploy.lock` |
| SHA 验证失败 | SHA 不在 `origin/main` | 确认 PR 已合并且 SHA 正确 |
| 工作区不干净 | 远程有未提交修改 | 手动处理，禁止自动 stash |
| Compose config 失败 | 环境变量缺失 | 检查 `/etc/market-dev/market.env` |
| health check 失败 | 新代码启动异常 | 脚本自动回滚，查看 `docker logs trading-backend` |
| Scheduler 数量异常 | 容器重复或缺失 | 检查 `docker ps`，手动重建 |

## 11. 禁用自动部署

如需临时禁用：

1. 在 GitHub Environments 中禁用 `production` 环境或添加 reviewer；
2. 或在服务器上移除 `/usr/local/bin/panji-deploy.sh` 的执行权限；
3. 不要删除 `STATE_FILE`，以保留 last-good SHA。
