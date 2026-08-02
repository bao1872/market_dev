# 80 部署与数据安全

> 来源：AGENTS.md §七.9-11、§七.22
> 状态：生效（Phase 2 激活）

## Migration 纪律

- 不得修改已发布历史 migration；
- 只允许新增前向 migration；
- 修改 migration 必须有 upgrade / downgrade / upgrade 验证。

## 测试期部署不备份数据库

测试期部署默认不备份数据库。

除非用户明确说"先备份数据库"，否则禁止：

- `pg_dump` / 大体积备份；
- 写入 `/root/backups` 或 `/root/web_dev/backups`。

当前物理机磁盘紧张，优先节省硬盘。

### 备份授权判定（2026-08-02 澄清，CHANGE-20260802-001）

只有**用户本人在当前任务中直接、明确**提出"先备份数据库"或等价明确指令，才算备份授权。
以下**均不构成**备份授权：

- AI 助手生成的实施计划 / 任务书 / 执行指令（含 AI 编写后由用户粘贴的指令）；
- 历史聊天中的建议或"检查备份""确认备份是否就绪""提供回滚方案"等风险描述；
- IDE 或代理自行认为"安全起见应备份""部署前应准备备份"。

无法确定是否得到用户本人直接授权时：**默认不备份、不运行 `pg_dump`、不写 `/root/backups` 或 `/root/web_dev/backups`**，
并在执行前向用户提出一个明确确认问题，等用户本人直接授权后再继续。

- 数据库备份授权**只对当次明确范围有效，不得继承到后续任务**；
- 磁盘空间紧张是长期事实，**禁止把备份作为部署 / Migration / 回滚的默认前置条件**；
- 误创建的备份：用户明确授权后可只删除本轮误建文件，禁止删除历史文件、非本轮备份、
  PostgreSQL 数据目录、Docker volume 或其他业务数据。

## Docker 镜像保护

`node:20-alpine` 是受保护基础镜像，拉取很慢。

禁止：

- 主动删除 `node:20-alpine`；
- `docker image prune -a`。

除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`。

普通清理只允许：

- `docker builder prune -f`；
- `docker image prune -f`；
- `docker container prune -f`。

## 服务器资源预算门禁（2026-08-02 收口）

生产服务器根分区 118G、内存 7.4G，是**共享且不可弹性扩容**的资源。
历史上每次部署都会新增三个业务镜像（backend 1.2G + capture 3.0G + frontend 65M）
与数 GB BuildKit 缓存，且从不回收，磁盘长期单向增长直至逼近写满。
本节把"单次部署不产生持久资源净增长"固化为硬约束。

### 硬门禁阈值

`scripts/ops/panji-test-deploy` 在**修改任何状态之前**校验以下阈值，任一不满足即失败退出：

| 指标 | 阈值 | 环境变量覆盖 |
|---|---|---|
| 根分区可用空间 | ≥ 20 GB | `PANJI_MIN_DISK_GB` |
| 根分区使用率 | ≤ 82% | `PANJI_MAX_DISK_PCT` |
| MemAvailable | ≥ 4096 MB | `PANJI_MIN_MEM_MB` |

阈值依据：一次全量构建的峰值临时占用约 8–12 GB（capture 镜像层 + BuildKit 缓存），
20 GB 下限保证构建期间不会触及 fs 写满；82% 使用率给 PostgreSQL 与日志留出增长余量。

门禁失败时禁止用"扩阈值"或"跳过门禁"绕过，必须先按下方允许范围清理。

### 部署后强制回收

部署成功后（步骤 11）自动执行受控清理，保证净增长趋近于零：

```
docker builder prune -f
docker image prune -f
docker container prune -f
```

清理后若可用空间仍低于门禁下限，脚本发出显式警告，提示下次部署会被拦截。

### 允许的清理范围

- BuildKit 构建缓存：`docker builder prune -f`（可全量清，重建只是变慢）；
- 悬挂（dangling）镜像：`docker image prune -f`；
- 已停止容器：`docker container prune -f`；
- **旧 SHA 业务镜像**：`market-dev-{backend,capture,frontend}:<旧SHA>`，
  但必须保留：当前运行 SHA、上一个可回滚 SHA、任何 `*-rollback` 标签；
- 生产上遗留的临时诊断脚本与部署日志（`/tmp/*.py`、`/tmp/deploy_*.log` 等）；
- systemd journal：`journalctl --vacuum-size=200M`。

### 禁止的清理操作

- `docker system prune -a`（会删除全部未使用镜像，包括受保护基础镜像）；
- `docker image prune -a`（同上）；
- `docker volume prune` / 任何删除卷的操作（业务数据）；
- 删除当前运行镜像或唯一可回滚镜像；
- 删除 `node:20-alpine`、`postgres:16`、`redis:7-alpine`、`nginx:alpine` 等基础镜像；
- 删除 `/var/lib/docker/volumes` 下任何内容；
- 为了通过门禁而删除业务数据、日志表或历史 run。

### 长任务内存预算

批量历史回填类长任务（如 Review bootstrap）必须自带内存上限，不得依赖"服务器内存够大"：

- 按自然分片（交易日 / scope）处理，分片结束释放 ORM identity map；
- 只保留聚合计数，不在进程内线性累积逐条明细；
- 记录 RSS 高水位；超过预算时**安全停止并如实上报 partial 状态**，
  绝不静默截断、不假装成功、不通过扩容内存掩盖实现缺陷；
- 并发固定为 1，禁止用并行放大峰值内存。

违反上述任一条即视为实现缺陷，必须修实现而不是调大预算。

## Live Mount 部署规则

Live Mount 部署通过只读 bind mount 将运行时代码挂载到容器，实现代码热更新而无需重建镜像。

### 固定运行目录

`/opt/panji-live/{backend/app, backend/alembic, backend/alembic.ini, frontend/dist, RUNTIME_SHA}`

### 叠加配置

- `docker-compose.prod.yml` + `docker-compose.live.yml`；
- 不修改 prod 配置。

### 挂载权限

- 所有挂载为只读 (`:ro`)；
- backend + 所有 Python worker + capture worker 挂载 app / alembic / alembic.ini / RUNTIME_SHA；
- frontend 挂载 dist（保留 capture_static 嵌嵌挂载）。

### 同步脚本

- Live Mount 同步脚本使用 `rsync --delete`；
- 只复制运行必需文件（排除 .git / docs / tests / node_modules / 缓存）；
- 同步期间先停止应用容器。

### 部署脚本

- Live Mount 部署脚本编排完整流程：前端构建 → 同步 → config 校验 → alembic → force-recreate。

### 适用范围

- 纯 Python / 前端代码变更用 Live Mount；
- 依赖 / Dockerfile / 基础镜像变化必须重建镜像。

### 版本端点

`/version` 返回：

- `runtime_git_sha`（RUNTIME_SHA 文件）；
- `image_git_sha`（GIT_SHA 环境变量）；
- `deployment_mode`（live / image）。

验证部署时 `runtime_git_sha` 必须等于 main HEAD。

## 部署顺序与回滚

- 部署按 `backend → frontend → worker` 顺序，禁止并行；
- 镜像必须打 SHA 标签，便于回滚；
- 保留当前 + 1 rollback 镜像；
- 不可逆 migration 必须在 PR 描述中明确标注并提供 downgrade 步骤；
- migration 不自动回滚（自动部署 PLANNED 阶段同样不自动回滚 migration）。

## 自动部署（PLANNED）

> 提议中，当前未实现。详见 `70-trae-cn.md`。

- dev push 自动部署为 PLANNED；
- 当前 dev push 只触发 CI 质量门禁；
- 自动部署需要：`panji-deploy` 服务器用户 + SSH forced command + GitHub Environment + 部署锁 + 变更分类；
- 自动部署不自动回滚 migration；
- 自动部署不读取数据库秘密；
- 自动部署只部署 GitHub commit。

## 生产服务器 SSH SSOT（CHANGE-20260730-015）

> 来源：CHANGE-20260730-015（SSH 目标漂移防复发）
> 状态：生效（2026-07-30）

### 唯一允许的入口

- 生产服务器只能通过仓库脚本 `scripts/ops/panji-prod-ssh` 访问，该脚本固定使用别名 `panji-prod`；
- 权威参数定义在 `docs/maps/80-system-runtime.md` §2：HostName=`43.136.118.82`、User=`root`、Port=`22`、远程目录=`/root/web_dev`；
- 部署/恢复/审计前必须先运行 `scripts/ops/panji-prod-preflight` 校验 ssh -G 解析值、远程目录、`/etc/market-dev/market.env`、Compose 项目和 `trading-backend` 容器；
- preflight 通过后本轮不得重复检查 SSH，除非连接实际中断。

### 禁止行为

- 禁止使用 `root@panji-server`、`55-server`、原始 IP 或任何 `~/.ssh/config` 中其他 Host 作为盘迹生产入口；
- 禁止 Compact/子代理恢复后重新发现服务器入口，必须读取 `/tmp/trae_review_*_ledger.md` 继续工作；
- 禁止使用可能掩盖 SSH 退出码的管道（如 `ssh ... | head`、`| tail`、`| grep`），必须先 `SSH_OUTPUT=$(ssh ...); SSH_RC=$?` 再单独裁剪输出；
- 禁止把私钥、密码或完整 IdentityFile 路径写入脚本/日志/CHANGE；
- `~/.ssh/config` 中 `55-server` 已加 `DEPRECATED-PANJI-DO-NOT-USE` 注释，不得删除该别名（保留历史运维），但盘迹操作禁止使用。

## 生产修改与部署版本合同（2026-07-30 收口）

> 来源：闭环缺口防复发
> 状态：生效（2026-07-30）
> 与 `70-trae-cn.md` "闭环恢复与成功判定硬约束" 叠加生效。

### 禁止 docker cp 和未审计 stdin 脚本

- 禁止使用 `docker cp` 向生产容器写入文件、配置或代码补丁；
- 禁止通过 `docker exec ... python -c "..."`、`docker exec ... psql -c "..."`、heredoc stdin 注入等未审计方式修改生产容器或生产数据；
- 临时诊断只能用只读 `docker exec ... python -c "..."` 查询，禁止写入；
- 任何对生产容器或数据的修改必须通过正式 service / CLI / migration / 部署脚本完成，并留 Git 历史 + 审计日志。

### 手工恢复走正式 service/CLI

- 手工恢复（DSA 失败、chip_consensus 卡住、stock_core pointer 缺失、聚合失败、Review 冷启动）必须走正式 service 或 CLI，并留审计记录：
  - DSA 失败恢复：`dsa_recovery_service.recover_failed_dsa_run`（创建新 run，不修改原 run）
  - Review 冷启动：`review_bootstrap_service.bootstrap_history(dry_run=False)`
  - stock_core pointer 恢复：`factor_publication_service.publish_stock_core`（幂等重发）
  - chip_consensus 恢复：worker 自动领取 `resume_queued`，使用 `FOR UPDATE SKIP LOCKED`
- 禁止裸 SQL 直接改 `scheduler_job_runs` / `strategy_runs` / `factor_publications` / `market_review_runs` 等状态表；
- 禁止 `/tmp` Python 脚本绕过 service 直接操作 ORM；
- 禁止 DELETE 历史 `dsa_only` 记录或失败 run，必须通过正式 cancel/interrupted/retry 服务处理。

### 部署版本合同

- 构建成功后必须先原子更新 `/etc/market-dev/market.env` 中的 `GIT_SHA`，再执行 `docker compose` 重建：
  - 更新方式：临时文件 + `mv` 原子替换，**禁止 `sed -i`**（`sed -i` 在某些环境下不是原子操作，且会破坏文件权限/SELinux 上下文）；
  - 模板：`cp market.env market.env.tmp && { grep -v '^GIT_SHA=' market.env.tmp; echo "GIT_SHA=<SHA>"; } > market.env.tmp && mv market.env.tmp market.env`；
  - `market.env` 更新成功后才能执行 `docker compose -f docker-compose.prod.yml up -d --force-recreate`。
- 部署成功门禁必须**同时**验证以下四项一致，**`/health=200` 不能单独判成功**：
  1. repo HEAD（`git -C /root/web_dev rev-parse HEAD`）= 目标 SHA；
  2. image tag（`docker images` 中 `trading-backend:<SHA>` 存在）= 目标 SHA；
  3. container env `GIT_SHA`（`docker inspect trading-backend` 的 env）= 目标 SHA；
  4. `/version` runtime SHA（`curl /api/v1/version` 的 `runtime_git_sha` + `image_git_sha`）= 目标 SHA。
- 四项任一不匹配即视为部署失败，必须回滚至上一已知良好 SHA，不得通过"重启容器"或"重新部署"掩盖不一致。
- Live Mount 部署同样适用：`RUNTIME_SHA` 文件必须原子替换（temp file + `mv`），禁止 `sed -i`。

## 部署脚本结构与执行纪律（2026-08-02 收口，CHANGE-20260802-002 配套）

> 来源：2026-08-02 部署事故——旧实现整段 §8（`up -d`）静默未执行，
> 镜像已构建但容器仍跑旧 SHA，`/health=200` 且无任何告警。
> 状态：生效（2026-08-02）

### DS-90 远程部署逻辑必须是受版本控制的真实脚本

- 远程部署逻辑必须存放为仓库中的真实文件（`scripts/ops/panji-deploy-remote.sh`），**禁止**写在本地脚本的 heredoc 里再经 `bash -s` 从 stdin 执行。
  - 理由：heredoc 无法本地 `bash -n` / shellcheck 静态检查；执行失败时无法定位行号；未加引号的 heredoc 还会在本地被提前变量展开，产生与预期不符的远端脚本。
- 本地入口（`scripts/ops/panji-test-deploy`）在传输前必须执行 `bash -n` 语法预检，把远端运行期错误提前到部署之前。
- 远程脚本必须设置 `ERR` trap，失败时输出**阶段名、行号、失败命令、退出码**四项。
- **禁止** `scp` 单个业务文件、`docker cp`、容器内改码、`/tmp` 临时脚本改生产等任何绕过正式部署入口的做法（与本文件"禁止 docker cp 和未审计 stdin 脚本"叠加生效）。

### DS-91 禁止按变更文件推断部署范围

- **禁止**在部署脚本中依据 `git diff` 结果决定重启哪些服务（如 `RESTART_BACKEND` / `RESTART_FRONTEND` / `BUILD_IMAGES` 之类的标志位）。
  - 理由：推断错误时部分服务会静默停留在旧 SHA 且不告警——这正是 2026-08-02 事故的直接成因。
- 每次部署必须**一次性重建全部无状态服务**，不做任何范围裁剪。
- **有状态服务（`postgres` / `redis`）必须明确排除**，不参与重建，避免触碰持久化数据。
- 重建后必须**逐服务**校验其镜像 SHA 等于目标 SHA，任一不符即判部署失败。
- Release Gate 的 `deploy-drill` job 通过 grep 断言部署脚本中不存在上述推断逻辑，防止回潮。

### DS-92 镜像来源：Registry 优先，服务器构建为显式过渡开关

- 正式路径：镜像由 Release Gate 在 CI 中构建为不可变镜像并推送 Registry，服务器**只 pull 不 build**。
- 服务器本地构建仅在显式传入 `--allow-local-build` 时允许，且必须在部署日志中打印告警。这是 Registry 凭据打通前的**过渡开关**，凭据就绪后应移除。
- 构建参数必须与 `docker-compose.prod.yml` 同源（用 `docker compose build` 而非手写 `docker build`）。
  - 理由：前端镜像需要 `VITE_GIT_SHA` / `VITE_BUILD_TIME` 构建参数，手写 `docker build` 极易漏传，漏传会导致页面显示错误版本号；两套构建定义也必然随时间漂移。
- 盘迹共 **3 个业务镜像**：`backend` / `frontend` / `capture`。全部 `worker-*` 服务复用 `backend` 镜像，不单独构建。
- Registry 凭据不可用时，Release Gate 允许完成到"构建 + 本地 manifest 校验"，并把推送步骤显式标记为 **`blocked_registry_auth`**。此时**禁止**：伪造 digest、改用 image tar 旁路、回退为服务器构建来"绕过"阻塞。

### DS-93 部署互斥与资源门禁

- 远程部署脚本必须用 `flock`（`/var/lock/panji-test-deploy.lock`）保证同一时刻只有一次部署在执行。
- 资源门禁（磁盘/内存阈值）必须在**改动任何状态之前**校验（见本文件"服务器资源预算门禁"）。
- 涉及 stdin 的远端命令必须重定向 `</dev/null`，防止后续脚本内容被子进程吞掉。

## 分层发布与增量检查点纪律

> 来源：CHANGE-20260729-006
> 状态：生效（2026-07-29）

### batch 不是发布边界

- batch_size 只控制吞吐和内存，不是完成或发布边界；
- 计算/事务/检查点粒度为"单股×阶段"；
- 单股结果 commit 成功后才标记 item succeeded；
- 单股失败只回滚该股票，不得回滚其他已成功股票；
- 禁止 N 股共用一个大事务。

### checkpoint 必须在 commit 后写

- `stock_feature_snapshot_run_items.status=succeeded` 必须在该股结果 commit 成功后写入；
- 禁止"先标 succeeded 再 commit"的顺序，避免 commit 失败导致 item 与数据不一致；
- lease_epoch fencing 用于防止旧 Worker 覆盖新 Worker 的状态。

### optional 任务不得反改 core

- chip / aggregation / events / 通知等 optional 任务失败，只重试自身，不回滚核心；
- 主编排在 core pointer 发布后即可标记 `core_published` 并允许复盘；
- 最终状态可为 `completed_with_errors`，但不得因 optional 失败反改 core。

### publication 只指向覆盖门禁通过的不可变 run

- `factor_publications` 的 `data_run_id` 必须指向覆盖率门禁通过的不可变 run；
- `CORE_PUBLICATION_MIN_COVERAGE = 0.98`，低于门禁拒绝发布；
- 发布只做小事务原子切换指针，不复制结果数据；
- 不得修改已发布 run；重算生成新 run，新 run 通过门禁后切换 pointer；
- 不同 run 的数据禁止混合；
- 无 publication pointer 时，读请求可兼容回退到 `published_at IS NOT NULL`。

### ID 合同：禁止一列双义

- `orchestrator_job_run_id`（SchedulerJobRun.id）：任务追踪，纯 metadata；
- `snapshot_run_id`（StockFeatureSnapshotRun.id）：当日核心数据版本；
- `history_run_id`（FirstPyramidHistoryRun.id）：历史回补版本；
- `chip.core_run_id` 必须等于 `snapshot_run_id`，不得指向 `SchedulerJobRun.id`；
- `FactorPublication.data_run_id` 指向 `snapshot_run_id` 或 `history_run_id`。

### publication pointer 一致性（CHANGE-20260729-007 补充）

- `factor_publications.trade_date` 必须为 NOT NULL；禁止用可空列配普通唯一约束制造多个 NULL "latest pointer"；
- `publish_market_aggregation` 必须验证 `source_core_run_id` 等于该日期已发布 `stock_core` pointer 的 `data_run_id`，不匹配抛错；
- `publish_history_cross_section` 的 coverage 必须由 DB 统计（`compute_history_coverage`），不接受调用方任意传值；
- `is_stale` 真源为 `bars_daily.max(trade_date)`，不是 `StockFeatureSnapshot.max(trade_date)`；
- 读取端（stock_context / market_stocks / watchlist）优先读 publication pointer，无 pointer 时兼容回退 `published_at IS NOT NULL`；有 pointer 后禁止混读不同 run。
