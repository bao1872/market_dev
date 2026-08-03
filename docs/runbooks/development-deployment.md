# 开发部署 Runbook

本文件是盘迹唯一的当前部署 Runbook。硬约束见
`rules/80-deployment-data-safety.md`，运行事实见 `docs/maps/80-system-runtime.md`。

## 适用边界

- 部署来源只能是已推送到 `origin/dev` 的精确完整 SHA；
- 唯一本地入口是 `scripts/ops/panji-test-deploy`；
- 唯一服务器实现是 `scripts/deploy/panji-deploy.sh`；
- 唯一运行方式是 `docker-compose.prod.yml` + `docker-compose.live.yml`；
- 本 Runbook 不授权生产部署、migration 或业务数据操作，执行这些动作仍需用户在当前任务明确授权。

## 部署前

1. 确认当前分支为 `dev`，工作树内容已经精确提交并推送到 `origin/dev`。
2. 确认目标 SHA 是完整 40 位 commit，且 `git merge-base --is-ancestor <SHA> origin/dev` 成功。
3. 完成修改范围内的本地纯单元测试、静态检查和部署合同测试。
4. 确认没有正在运行的正式盘后任务或其他会被服务重启中断的业务任务。
5. 不在命令中加入数据库 apply、业务 run、publish、withdrawal 或临时恢复脚本。

## Dry Run

```bash
scripts/ops/panji-test-deploy <FULL_SHA> --dry-run
```

dry-run 必须保持远端工作树、运行目录、容器、环境文件、数据库和部署状态文件不变。
检查输出中的目标 SHA、上一成功 SHA、变更分类、镜像构建计划、migration 判定、同步目录、
重启服务和最终验证计划。任一项与实际 diff 不一致时停止。

## 执行

获得本轮明确生产部署授权后执行：

```bash
scripts/ops/panji-test-deploy <FULL_SHA>
```

入口会先运行 `scripts/ops/panji-prod-preflight`，再经 `scripts/ops/panji-prod-ssh` 调用服务器
仓库内的 `scripts/deploy/panji-deploy.sh`。不得用裸 `ssh`、`scp`、`docker cp`、stdin 脚本
或容器内编辑替代该调用链。

## 变更分类

| 变化 | 动作 |
|---|---|
| 普通 Backend 代码 | 同步到 `/opt/panji-live/backend`，不构建镜像，重启 Python 服务 |
| 普通 Frontend 代码 | 构建并同步 `frontend/dist`，不构建镜像，重启 frontend |
| Backend 依赖或 Dockerfile | 构建完整环境镜像 tag 组，仍以 Live Mount 运行 |
| Frontend 依赖、Dockerfile 或 Nginx 运行环境 | 安装锁定依赖、构建 dist，并构建完整环境镜像 tag 组，仍以 Live Mount 运行 |
| Capture Dockerfile | 构建完整环境镜像 tag 组，仍以 Live Mount 运行 |
| Alembic version 文件 | 同步目标代码后执行一次 `alembic upgrade head`；失败时不得重启应用 |
| 纯文档/治理变化 | 不构建镜像、不执行 migration、不重启服务，只更新并核验目标 SHA |

镜像构建口径：普通代码变化**零构建**（Live Mount 直接生效）。
`docker-compose.prod.yml` 中 backend / frontend / worker-capture 共用同一个 `GIT_SHA` image tag，
因此只要发生**任意**环境级变化，就必须把这三个镜像作为**同一 tag 组整体构建**，
不存在"只构建受影响的那一个镜像"。构建完成后仍以 prod + live 叠加启动，
运行代码仍唯一来自 `/opt/panji-live`。

所有分类基于"上一成功部署 SHA到目标 SHA"的完整差异，不使用 `HEAD~1`。

上一部署 SHA 解析**禁止**使用 checkout 后的 repo HEAD（外层已把服务器检出到目标 SHA，
否则 `git diff 目标SHA 目标SHA` 为空、漏判 migration 与依赖变化）：

- **已 Live Mount**：① 部署状态文件 ② `/opt/panji-live/RUNTIME_SHA` ③ 当前运行版本
  `version.runtime_git_sha` ④ `PANJI_BOOTSTRAP_PREVIOUS_SHA`（外层自举前完整 SHA）；
- **首次 Live Mount**（核心容器尚未挂载 `/opt/panji-live`）：① 当前 `trading-backend`
  `/v1/version`（`runtime_git_sha` → `image_git_sha` → `git_sha`）② 当前 `trading-backend`
  镜像 tag 中的 SHA ③ `PANJI_BOOTSTRAP_PREVIOUS_SHA`；仍无法确认则**停止部署**并报告
  `previous_runtime_sha_unknown`，**不得**把 `TARGET_SHA` 当作上一 SHA。

短 SHA 仅在仓库中能唯一解析为完整 commit 时才允许使用。
全部失败（非首次未知基线）才按首次未知基线（全量同步 + migration）处理。
**仅状态文件缺失不构成强制 migration 的理由。**

## 首次 Live Mount 部署

服务器仓库可能仍停在旧 SHA，因此本地入口会先让服务器自举：
`cd 仓库 → fetch origin dev → 工作树干净校验 → 目标 SHA 属于 origin/dev →
记录原始 HEAD → checkout --detach 目标 SHA → 执行目标工作树中的 panji-deploy.sh`。
dry-run 或任何失败都会恢复原始 REF（分支名或 detached 完整 SHA）；正式部署成功后服务器保持在目标 SHA。

服务器实现通过 `docker inspect` 检查 `trading-backend` 与 `trading-frontend` 是否挂载
`/opt/panji-live`。任一未挂载即判定为首次 Live Mount 部署，强制全量同步 Python 与前端
运行代码以建立挂载。**首次挂载只提升同步范围，不会因此执行 migration。**

## 成功判据

部署脚本必须确认：

- 服务器 repo HEAD = 目标完整 SHA；
- `/opt/panji-live/RUNTIME_SHA` = 目标完整 SHA；
- `/v1/version.runtime_git_sha` = 目标完整 SHA；
- `/v1/version.deployment_mode` = `live`；
- `/v1/health` 与 `/v1/health/ready` 通过；
- `trading-backend` 的 Mounts 包含 `/opt/panji-live`（无条件核验）；
- `trading-frontend` 的 Mounts 包含 `/opt/panji-live/frontend/dist`（无条件核验）；
- 当 backend / migration / 首次 Live Mount 触发 Python 重启时，
  全部 11 个共用 Live Mount 的 Python 服务（backend、worker-bars-scheduler、
  worker-strategy-scheduler、worker-calendar、worker-monitor、worker-strategy-batch、
  worker-outbox、worker-delivery、worker-after-close、worker-watchdog、worker-capture）
  的 Mounts 均包含 `/opt/panji-live`；
- 关键容器运行，三个 Scheduler 各为单实例。

只有全部成立后才能写入上一成功部署状态。`/health=200` 不能单独判成功。

`/opt/panji-live/RUNTIME_SHA` 是**单文件 bind mount** 源。更新时必须**原地写入**
（truncate + write 保持同一 inode），禁止"写临时文件再 rename"或 `rsync` 覆盖——
换 inode 会让容器内继续读到旧内容。写入后校验 inode 未变并回读完整 SHA。

## 失败与回滚

失败路径按**服务是否已重启**分两类，不共用同一条回滚：

**A. migration 失败（服务尚未重启）**

migration 始终早于任何服务重启。migration 失败时：

- 恢复仓库、Live Mount 文件、`RUNTIME_SHA` 与 `market.env` 到上一 SHA；
- **不执行任何 `docker compose up` 或 `--force-recreate`**，容器仍运行上一 SHA 的代码；
- 不写入成功状态；
- **不得声称数据库已回滚**——脚本不会自动 downgrade，数据库实际状态需人工确认；
- 输出结论 `migration_failed_requires_inspection` 并停止。

**B. 服务已重启后失败（health / SHA / Mount 核验不通过）**

恢复上一成功 SHA 的仓库、Live Mount 内容、前端 dist 和环境镜像引用，
然后重建应用容器。回滚同样不得执行数据库 downgrade，不得删除 Volume。

若没有上一成功 SHA、migration 已产生无法自动恢复的外部影响，或回滚验证失败，必须停止并
报告真实状态，不能继续重试或用手工覆盖掩盖。

## 部署后清理

清理按本轮是否实际构建镜像分档：

- 本轮未构建任何镜像（普通 Live Mount 代码部署）→ **不做任何清理**；
- 本轮构建了环境镜像 → 执行 `docker builder prune -f` 与 `docker image prune -f`。

任何情况下都禁止 `docker image prune -a`、`docker system prune`、`docker volume prune`、
删除 `node:20-alpine`、删除 PostgreSQL 或 Redis Volume。

## 禁止操作

- `docker compose down -v` 或删除 PostgreSQL/Redis Volume；
- 在服务器或容器内手工改源码；
- 自动执行 bootstrap、Review/Auction run、publish 或 withdrawal；
- 将 PostgreSQL、Redis、Umami 加入普通应用重启列表；
- 同一次部署让运行代码同时来自镜像内置代码和非 Live Mount 路径；
- 未验证完整 SHA、运行模式和挂载来源就报告成功。
