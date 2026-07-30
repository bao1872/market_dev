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
