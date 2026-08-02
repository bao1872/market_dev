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
| Backend 依赖或 Dockerfile | 构建 backend 环境镜像，仍以 Live Mount 运行 |
| Frontend 依赖、Dockerfile 或 Nginx 运行环境 | 安装锁定依赖、构建对应环境镜像和 dist，仍以 Live Mount 运行 |
| Capture Dockerfile | 构建 capture 环境镜像，仍以 Live Mount 运行 |
| Alembic version 文件 | 同步目标代码后执行一次 `alembic upgrade head`；失败时不得重启应用 |
| 纯文档/治理变化 | 不构建镜像、不执行 migration、不重启服务，只更新并核验目标 SHA |

所有分类基于“上一成功部署 SHA到目标 SHA”的完整差异，不使用 `HEAD~1`。

## 成功判据

部署脚本必须确认：

- 服务器 repo HEAD = 目标完整 SHA；
- `/opt/panji-live/RUNTIME_SHA` = 目标完整 SHA；
- `/v1/version.runtime_git_sha` = 目标完整 SHA；
- `/v1/version.deployment_mode` = `live`；
- `/v1/health` 与 `/v1/health/ready` 通过；
- 受影响容器的 Mounts 包含 `/opt/panji-live`；
- 关键容器运行，三个 Scheduler 各为单实例。

只有全部成立后才能写入上一成功部署状态。`/health=200` 不能单独判成功。

## 失败与回滚

失败时服务器实现恢复上一成功 SHA 的仓库、Live Mount 内容、前端 dist 和环境镜像引用，
然后重建受影响应用容器。回滚不得执行数据库 downgrade，不得删除 Volume。

若没有上一成功 SHA、migration 已产生无法自动恢复的外部影响，或回滚验证失败，必须停止并
报告真实状态，不能继续重试或用手工覆盖掩盖。

## 禁止操作

- `docker compose down -v` 或删除 PostgreSQL/Redis Volume；
- 在服务器或容器内手工改源码；
- 自动执行 bootstrap、Review/Auction run、publish 或 withdrawal；
- 将 PostgreSQL、Redis、Umami 加入普通应用重启列表；
- 同一次部署让运行代码同时来自镜像内置代码和非 Live Mount 路径；
- 未验证完整 SHA、运行模式和挂载来源就报告成功。
