# 开发部署 Runbook

本文件是盘迹唯一的当前部署 Runbook。硬约束见
`rules/80-deployment-migration.md`，运行事实见 `docs/maps/80-system-runtime.md`。

## 适用边界

- 部署来源只能是已推送到 `origin/dev` 的精确完整 SHA；
- 唯一本地入口是 `scripts/ops/panji-test-deploy`；
- 唯一服务器实现是 `scripts/deploy/panji-deploy.sh`；
- 唯一运行方式是 `docker-compose.prod.yml` + `docker-compose.live.yml`；
- 本 Runbook 不授权 stable deployment、migration 或业务数据操作；source-only Live Refresh
  仅在用户要求查看或验证远程效果时执行，并继承代码改动的治理等级。

## 三条独立远程流程

### live refresh（开发运行刷新）

- 目标：把 source-only 改动同步到已建立的 Live Mount，并只刷新受影响进程；
- 触发：用户要求查看或验证远程效果；
- 边界：不 build environment image、不 recreate container、不 Migration、不写业务数据；
- 入口与身份：仍使用正式入口和 `origin/dev` exact SHA，不允许单文件热修。

### stable runtime deployment（稳定运行部署）

- 目标：把已确认 SHA 部署到 `panji-prod` 正式运行栈（`docker-compose.prod.yml` + `docker-compose.live.yml`），操作 `bz_stock`。
- 触发：用户明确授权后，且 V2.1 已在验证栈通过验收。
- 前置：`panji-prod-preflight` + `panji-prod-ssh` 入口（见 `rules/80`）。

### remote verification（远程验证，不等同于稳定运行部署）

- 目标：用 exact SHA 的代码在固定验证 runtime 中操作一次性 `bz_stock_verify_<40SHA>`。
- 约束（详见 `rules/80` DS-110/111/112）：
  - 固定 Compose project `panji-verify` 与长期空闲容器 `panji-verify-python`；
  - 不发布 host port，不启动 Scheduler/Worker/Uvicorn，不连接 Redis；
  - 复用 `trading-postgres` 网络，但绝不连接或读取 `bz_stock`；
  - `current_database()` 必须全等于 SHA 派生验证库；
  - 每个 gate 由 `verify_exec.py` 运行 fresh process。
- 唯一正式入口：`scripts/ops/panji-verify`；不得恢复第二入口或拼装低层脚本。
- 验证栈不得替代正式运行栈；验收通过后才允许同 SHA 申请部署正式栈。验证授权不自动包含正式栈部署授权。

按目标选择一个注册计划：

```bash
scripts/ops/panji-verify run --sha <FULL_40_SHA> --plan targeted-pg
scripts/ops/panji-verify run --sha <FULL_40_SHA> --plan migration-roundtrip
scripts/ops/panji-verify run --sha <FULL_40_SHA> --plan full-closure
```

Exploration 的 PostgreSQL 合同使用 `targeted-pg`；Migration 专项使用
`migration-roundtrip`；`full-closure` 只用于明确 Hardening/Release 或完整 closure。

入口先执行 preflight，再经受控 SSH 调用目标 SHA 的 runner。不得手工提供数据库 URL、
环境文件、pytest 参数或插件。PG 测试由 `evidence_manifest.json` 的显式 selector 注册。
结束后检查 target SHA/数据库身份、required contracts 全部 `passed`，不存在 required
`skipped/deselected/not_registered/not_run/blocked`，并确认 `cleanup.json` 中验证库
`dropped=true`、`blocked_cleanup=false`。清理阻塞时停止新 attempt，只按 manifest
中的精确资源身份处理。

## 部署前

1. 确认当前分支为 `dev`，工作树内容已经精确提交并推送到 `origin/dev`。
2. 确认目标 SHA 是完整 40 位 commit，且 `git merge-base --is-ancestor <SHA> origin/dev` 成功。
3. 完成修改范围内的本地纯单元测试、静态检查和部署合同测试。
4. Backend Live Refresh 或 Operational Deployment 前，确认没有会被受影响进程刷新中断的正式任务。
5. 不在命令中加入数据库 apply、业务 run、publish、withdrawal 或临时恢复脚本。

## Dry Run

```bash
scripts/ops/panji-test-deploy <FULL_SHA> --dry-run
```

dry-run 必须保持远端工作树、运行目录、容器、环境文件、数据库和部署状态文件不变。
其中"容器不变"包含部署临界区：dry-run **只模拟** supervisor-drain fence（只读探测 worker
容器状态与活跃盘后任务计数），**不得** `stop` / `up` / `--force-recreate`
`worker-after-close`；日志中只允许出现模拟态字段
`AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true`，不允许出现真实 fence 字段
`AFTER_CLOSE_PICKUP_FENCED=true` 或 `AFTER_CLOSE_PICKUP_RESTORED=true`。
dry-run 也不读取真实 `MemAvailable`（内存 headroom 延后到真实部署 fence 之后执行，
日志显示 `deferred`）；rollback owner 解析等只读步骤仍会真实执行。

检查输出中的目标 SHA、上一成功 SHA、变更分类、镜像构建计划、migration 判定、同步目录、
重启服务和最终验证计划。任一项与实际 diff 不一致时停止。

若 dry-run 过程中实际发生了容器状态变化，即为合同违反：必须如实记录受影响容器与恢复动作，
不得在报告中写成"零生产修改"。

## 执行

用户要求查看/验证远程效果，或明确授权 stable deployment 后执行：

```bash
scripts/ops/panji-test-deploy <FULL_SHA>
```

入口会先运行 `scripts/ops/panji-prod-preflight`，再经 `scripts/ops/panji-prod-ssh` 调用服务器
仓库内的 `scripts/deploy/panji-deploy.sh`。不得用裸 `ssh`、`scp`、`docker cp`、stdin 脚本
或容器内编辑替代该调用链。

## 变更分类

| 变化 | 动作 |
|---|---|
| Backend API-only | 同步到 `/opt/panji-live/backend`，不构建/不 recreate，只 `restart backend` |
| 其他 Backend runtime source | 同步，不构建/不 recreate，当前保守 classifier 刷新全部 Python services；`worker-after-close` 继续走既有 owned fence/restore |
| 普通 Frontend 代码 | 构建并同步 `frontend/dist`，不构建镜像，不重启 frontend |
| Backend 依赖或 Dockerfile | 构建完整环境镜像 tag 组，仍以 Live Mount 运行 |
| Frontend 依赖、Dockerfile 或 Nginx 运行环境 | 安装锁定依赖、构建 dist，并构建完整环境镜像 tag 组，仍以 Live Mount 运行 |
| Capture Dockerfile | 构建完整环境镜像 tag 组，仍以 Live Mount 运行 |
| Alembic version 文件 | 同步目标代码后执行一次 `alembic upgrade head`；失败时不得重启应用 |
| 纯文档/治理变化 | 不构建镜像、不执行 migration、不重启服务，只更新并核验目标 SHA |

镜像构建口径：普通代码变化**零构建**（Live Mount 直接生效）。
source-only backend 使用 `docker compose restart` 刷新现有进程，不使用
`docker compose up --force-recreate`；frontend source-only 依靠 dist bind mount 直接生效。
当前 backend classifier 只有两档：API-only 刷新 `backend`，其他 backend runtime source
保守刷新全部 Python services；不承诺细粒度“相关 worker”分析。
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
- 当 backend / migration / 首次 Live Mount 触发 Python refresh/recreate 时，
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

## 部署资源门禁顺序

资源门禁分两阶段，顺序是合同（详见 `rules/80-deployment-migration.md` §11.1）：

1. **静态资源预算**（任何状态修改之前，fail-closed）：阈值配置健全性 + 主机磁盘余量。
   此阶段**不判定** `MemAvailable`。
2. **部署内存 headroom**（fail-closed）：在 supervisor-drain fence 之后、**首笔 runtime
   mutation 之前**读取 `MemAvailable`，要求 ≥ `PANJI_MIN_MEM_MB`。fence 释放长任务 worker
   常驻内存后才是本次部署真实可用的 working set，因此不得把该门槛提前到 fence 之前。

`PANJI_MIN_MEM_MB` 是部署期 headroom，不是"宿主机稳态必须空闲"的指标。

## 部署后资源复检（DS-104）

部署成功（SHA / health / Mount 核验通过）后，在写成功状态**之前**必须复检资源，任一失败即判部署失败：

- **主机（门禁）**：磁盘可用 ≥ `PANJI_MIN_DISK_GB`、使用率 ≤ `PANJI_MAX_DISK_PCT`；
- **主机内存（observation-only）**：记录 `MemAvailable` 作为收紧预算的证据，**不作为失败门槛**
  ——部署完成后 worker 已恢复常驻，此时的空闲内存不代表异常；真正的内存异常由下面的容器级
  `OOMKilled` / `RestartCount` / limits 生效性门禁捕获；
- **容器**：任一关键容器 `State.OOMKilled=true` 或异常 `RestartCount` → 失败；
- **配置生效**：`docker inspect` 读取关键容器 `Memory` / `PidsLimit` / `NanoCpus` 非 0（未生效）→ 失败；
- **高水位**：`docker stats --no-stream` 采集各服务内存，按 `key=value` 记录，作为后续收紧预算的证据；
- **服务**：health / ready / 单实例校验。

## 部署后清理

清理按本轮是否实际构建镜像分档：

- 本轮未构建任何镜像（普通 Live Mount 代码部署）→ **不做任何清理**；
- 本轮构建了环境镜像（`IMAGES_BUILT=true`）→ 执行受控清理，保证镜像与缓存净增长趋近于零：
  1. `docker builder prune -f`；
  2. `docker image prune -f`；
  3. **旧 SHA 业务镜像精确回收**（DS-105）：构造保留集合（当前运行 SHA、上一成功部署 SHA、任何 `*-rollback` 标签、基础镜像、非 `market-dev` 项目镜像），按完整 SHA 组枚举 `market-dev-{backend,capture,frontend}:<sha>`，仅当该组三标签全部不在保留集合中才整组删除；**禁止**按模糊名或创建时间删除；
  4. 清理**前后**各输出一次磁盘证据（`cleanup_disk_before/after_mb`），记录回收的 SHA 列表。

清理后再执行一次资源复检，确认清理后资源不反弹。

任何情况下都禁止 `docker image prune -a`、`docker system prune`、`docker volume prune`、
`container prune`、删除 `node:20-alpine`、删除 PostgreSQL 或 Redis Volume。

## 任务产物收尾（GF-100）

部署 / 任务结束收尾时，按 `rules/50-git-development-flow.md` GF-100 盘点本轮产生的临时产物
（临时脚本、一次性日志、临时 JSON/CSV、调试截图、构建残留、缓存目录等），明确可删 / 不可删边界，
报告输出创建 / 删除 / 保留清单与保留原因。不得错删 `.venv` / `node_modules` / 正式 fixtures /
用户上传文件 / 未知来源 / 任何数据库或 Volume。

## 禁止操作

- `docker compose down -v` 或删除 PostgreSQL/Redis Volume；
- 在服务器或容器内手工改源码；
- 自动执行 bootstrap、Review/Auction run、publish 或 withdrawal；
- 将 PostgreSQL、Redis、Umami 加入普通应用重启列表；
- 同一次部署让运行代码同时来自镜像内置代码和非 Live Mount 路径；
- 未验证完整 SHA、运行模式和挂载来源就报告成功。
