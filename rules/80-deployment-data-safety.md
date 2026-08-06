# 80 部署与数据安全

> 来源：AGENTS.md §七.9-11、§七.22

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

普通清理只允许（且仅在本轮实际构建了镜像时由部署脚本自动执行）：

- `docker builder prune -f`；
- `docker image prune -f`。

未构建镜像的普通 Live Mount 代码部署不做任何自动清理。

## 服务器资源预算门禁（2026-08-02 收口）

远程开发运行服务器根分区 118G、内存 7.4G，是**共享且不可弹性扩容**的资源。
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

### 部署后回收（按本轮是否实际构建镜像分档）

清理是**有条件的**，不是每次部署都执行：

- **本轮未构建任何镜像**（普通 Live Mount 代码部署，`images_built=false`）：
  **不执行任何清理**。此时既没有新增 BuildKit 缓存，也没有产生悬空镜像，
  执行 `builder prune` 只会清掉与本轮无关的历史缓存，且可能误伤其他容器/镜像。
- **本轮确实构建了环境镜像**（`images_built=true`）：执行受控范围清理，
  保证镜像与缓存净增长趋近于零：

```
docker builder prune -f
docker image prune -f
```

清理后若可用空间仍低于门禁下限，脚本发出显式警告，提示下次部署会被拦截。

### 允许的清理范围

> 下列范围仅在**本轮确实构建了镜像**时才允许由部署脚本自动执行；
> 未构建镜像的普通 Live Mount 部署不做任何自动清理。

- BuildKit 构建缓存：`docker builder prune -f`（可全量清，重建只是变慢）；
- 悬挂（dangling）镜像：`docker image prune -f`；
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

当前运行后端版本端点（以 `docs/runbooks/development-deployment.md` 实测探针为准）应返回：

- `runtime_git_sha`（= `/opt/panji-live/RUNTIME_SHA` 文件内容）；
- `deployment_mode`（`live`）。

> 早期镜像曾返回 `image_git_sha` / `GIT_SHA` 环境变量，属镜像构建时代的残留；Live Mount 模式下运行时来源是 `RUNTIME_SHA` 文件，不再依赖镜像内置 `GIT_SHA`。如运行后端未暴露该端点，以 `RUNTIME_SHA` 文件内容 + 服务器 repo HEAD（部署后已 checkout 到目标 SHA）作为部署后 SHA 一致性证据（见 §部署版本合同）。注意：**上一真实运行 SHA 的解析不得依赖 checkout 后的 repo HEAD**，否则会漏判 migration 与环境变化（详见 DS-91）。

验证部署时 `runtime_git_sha` 必须等于目标 **dev SHA**。

## 部署顺序与回滚

- 部署按 `backend → frontend → worker` 顺序，禁止并行；
- 普通变更走 Live Mount（同步运行代码 + 重启受影响服务），**不构建镜像**，因此回滚即重新同步上一已知良好 dev SHA 的运行代码并重启；
- 仅当依赖 / Dockerfile / 基础镜像变化触发镜像构建时，才以镜像 SHA 标签区分版本，回滚为切回上一镜像并重启；
- 不可逆 migration 必须在变更说明中明确标注并提供 downgrade 步骤；
- migration 不自动回滚。

## 部署来源与三平面边界

当前运行模型只有三个平面：本地开发、远程隔离验证、远程稳定运行。三者不是发布状态机，不得互相冒充证据或复用授权。

- `dev` 是 CI 与开发部署的唯一来源；
- push `dev` 本身不触发 CI、远程验证或稳定运行部署；CI 仅可手工触发；
- 远程验证使用隔离验证栈，稳定运行部署使用 **Live Mount**，两者入口、数据库、状态和授权分离；
- 触及数据库、Worker/编排、发布指针、权限写入或跨服务链路的变更，必须先以同一 SHA 通过相应远程验证，再申请稳定运行部署；
- Release Gate / GHCR / Registry / immutable image release 当前未实现，禁止表述为可用路径。

## 远程开发运行服务器 SSH SSOT（CHANGE-20260730-015）

> 来源：CHANGE-20260730-015（SSH 目标漂移防复发）

### 唯一允许的入口

- 远程开发运行服务器只能通过仓库脚本 `scripts/ops/panji-prod-ssh` 访问，该脚本固定使用别名 `panji-prod`；
- 权威身份定义在 `docs/maps/80-system-runtime.md` 的“远程开发运行身份”；具体网络值由 `panji-prod-preflight` 校验；
- 部署/恢复/审计前必须先运行 `scripts/ops/panji-prod-preflight` 校验 ssh -G 解析值、远程目录、`/etc/market-dev/market.env`、Compose 项目和 `trading-backend` 容器；
- preflight 通过后本轮不得重复检查 SSH，除非连接实际中断。

### 禁止行为

- 禁止使用 `root@panji-server`、`55-server`、原始 IP 或任何 `~/.ssh/config` 中其他 Host 作为盘迹远程开发运行入口；
- 禁止上下文压缩或子代理恢复后自行重新"发现"服务器入口：必须以 `docs/maps/80-system-runtime.md` §2 为权威参数，
  经 `scripts/ops/panji-prod-preflight` 校验后继续；禁止猜测 SSH 别名或重读 `~/.ssh/config` 重新选择 Host；
- 禁止使用可能掩盖 SSH 退出码的管道（如 `ssh ... | head`、`| tail`、`| grep`），必须先 `SSH_OUTPUT=$(ssh ...); SSH_RC=$?` 再单独裁剪输出；
- 禁止把私钥、密码或完整 IdentityFile 路径写入脚本/日志/CHANGE；
- `~/.ssh/config` 中 `55-server` 已加 `DEPRECATED-PANJI-DO-NOT-USE` 注释，不得删除该别名（保留历史运维），但盘迹操作禁止使用。

## 远程开发修改与部署版本合同（2026-07-30 收口）

> 来源：闭环缺口防复发
> 成功判定三要素见 `40-testing-quality.md` TQ-98。

### 禁止 docker cp 和未审计 stdin 脚本

- 禁止使用 `docker cp` 向生产容器写入文件、配置或代码补丁；
- 禁止通过 `docker exec ... python -c "..."`、`docker exec ... psql -c "..."`、heredoc stdin 注入等未审计方式修改生产容器或共享开发业务数据；
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

### 部署版本合同（Live Mount 开发部署）

- 必须部署 **exact dev SHA**：服务器 `git checkout` / `git fetch` 到目标 dev SHA，运行代码同步到 `/opt/panji-live`（`RUNTIME_SHA` 文件写入该 SHA）。
- 部署成功门禁必须**同时**验证以下两项一致，**`/health=200` 不能单独判成功**：
  1. 服务器 repo HEAD（`git -C /root/web_dev rev-parse HEAD`）= 目标 dev SHA；
  2. `runtime_git_sha`（运行代码 `RUNTIME_SHA` 文件 / 版本端点返回的 `runtime_git_sha`）= 目标 dev SHA。
- 任一项不匹配即视为部署失败，必须回到上一已知良好 SHA，不得通过"重启容器"或"重新部署"掩盖不一致。
- `RUNTIME_SHA` 文件必须**原地写入**（truncate + write 同一 inode），并在写入前后校验 inode 不变、写入后回读校验内容。**禁止** `temp file + mv`、`rsync`、`rename` 或 `sed -i` 更换 inode 的做法。以 `scripts/deploy/panji-deploy.sh` 的 `write_runtime_sha` 与 `docs/maps/80-system-runtime.md` 为权威实现，`tools/check_governance_rules.py` 自动断言禁止 `mv`/`rsync`。
- 代码部署**不自动执行**任何数据 apply / run / publish 操作；migration 仅在确有新 migration 时由部署脚本显式、幂等地执行，且不属于"自动数据发布"。
- 具体探针命令与逐服务校验以 `docs/runbooks/development-deployment.md` 为准（当前运行后端版本端点路径以该 runbook 实测为准，不在此硬编码）。

## 部署脚本结构与执行纪律（2026-08-02 收口，CHANGE-20260802-003 配套）

> 来源：2026-08-02 部署事故——旧实现整段 §8（`up -d`）静默未执行，
> 镜像已构建但容器仍跑旧 SHA，`/health=200` 且无任何告警。

### DS-90 部署执行入口只有两个文件

- 本地唯一用户入口：`scripts/ops/panji-test-deploy`（瘦客户端，只做 SHA 校验 + preflight + SSH 调用）。
- 服务器端唯一实现：`scripts/deploy/panji-deploy.sh`（受版本控制的真实文件）。
- 除以上两个文件外，仓库中**不得**存在其他部署执行脚本。已删除并禁止恢复：
  `scripts/ops/panji-deploy-remote.sh`、`scripts/deploy_live_runtime.sh`、`scripts/sync_live_runtime.sh`。
- **禁止**把部署逻辑写在本地脚本的 heredoc 里再经 `bash -s` 从 stdin 执行；
  **禁止**把部署脚本 `scp` / 管道拷贝到 `/tmp` 后执行。
  服务器执行的必须是服务器仓库内受版本控制的 `scripts/deploy/panji-deploy.sh`。
  - 理由：heredoc / `/tmp` 副本无法本地 `bash -n` / shellcheck 静态检查；执行失败时无法定位行号；未加引号的 heredoc 还会在本地被提前变量展开，产生与预期不符的远端脚本。
- 本地入口在 SSH 调用前必须对 `scripts/deploy/panji-deploy.sh` 执行 `bash -n` 语法预检。
- **禁止** `scp` 单个业务文件、`docker cp`、容器内改码、`/tmp` 临时脚本改生产等任何绕过正式部署入口的做法（与本文件"禁止 docker cp 和未审计 stdin 脚本"叠加生效）。

### DS-91 变更范围判定必须基于「上一真实运行 SHA → 目标 SHA」

- 变更分类**必须**使用 `git diff --name-only <上一真实运行 SHA> <目标 SHA>`。
  - **禁止**使用 `HEAD~1`：一次部署可能跨多个 commit，`HEAD~1` 会漏判。
  - **禁止**在本地入口已将服务器 `checkout` 到目标 SHA 之后，把"当前 repo HEAD"当作上一真实运行 SHA——
    P0（2026-08-02）：这会导致 `git diff 目标SHA 目标SHA` 为空、漏判 migration 与依赖/Dockerfile 变化。
    正确来源顺序见 `docs/runbooks/development-deployment.md`：
    - 已 Live Mount：部署状态文件 → `/opt/panji-live/RUNTIME_SHA` → 当前运行版本 `version.runtime_git_sha` → 外层自举前 `PANJI_BOOTSTRAP_PREVIOUS_SHA`；
    - 首次 Live Mount：当前运行 `trading-backend` `/v1/version` → 镜像 tag SHA → `PANJI_BOOTSTRAP_PREVIOUS_SHA` → 仍无法确认则**停止部署**并报告 `previous_runtime_sha_unknown`。
  - 无上一部署记录或该 SHA 本地不可解析时，必须按首次部署处理（全量同步 + migration）。
- 分类结果只用于决定「是否构建运行环境镜像」「是否执行 migration」「重启哪一组服务」，
  且必须满足：backend 运行代码变化时重启**全部** Python 服务（backend + 所有 worker），
  不得只重启 backend。
- **有状态服务（`postgres` / `redis` / `umami`）必须明确排除**，不参与重启，避免触碰持久化数据。
- 部署结束必须校验运行代码 SHA（`RUNTIME_SHA` 文件与版本端点 `runtime_git_sha`）
  等于目标**完整** dev SHA，任一不符即判部署失败并回滚。短 SHA 不作为成功判据。

### DS-92 镜像构建触发条件（仅在依赖或 Dockerfile 变化时才 build）

普通开发变更**不构建镜像**，使用 Live Mount 同步运行代码（见 §Live Mount 部署规则）。只有以下变化才构建对应镜像：

- `pyproject.toml` 或 Python 依赖锁；
- `package.json` / `package-lock.json`；
- `Dockerfile` / `Dockerfile.capture`；
- 系统依赖（如 apt 层）；
- 基础镜像；
- Capture 浏览器运行环境；
- 必须烘焙进镜像的 Nginx 配置。

约束：

- 构建参数必须与 `docker-compose.prod.yml` 同源（用 `docker compose build` 而非手写 `docker build`），避免构建定义漂移。
- 盘迹共 **3 个业务镜像**：`backend` / `frontend` / `capture`。全部 `worker-*` 服务复用 `backend` 镜像，不单独构建。
- 运行代码始终只来自 Live Mount。依赖/Dockerfile 变化时构建的新镜像只提供运行环境，
  不得切换为镜像内置业务代码。

### DS-93 部署互斥与资源门禁

- 远程部署脚本必须用 `flock`（`/var/lock/panji-test-deploy.lock`）保证同一时刻只有一次部署在执行。
- 资源门禁（磁盘/内存阈值）必须在**改动任何状态之前**校验（见本文件"服务器资源预算门禁"）。
- 涉及 stdin 的远端命令必须重定向 `</dev/null`，防止后续脚本内容被子进程吞掉。

## 2026-08-04 收口：资源与测试治理垂直切片（CHANGE 待记）

> 本节把「写了未落实」的容器运行期资源控制、部署串行/超时/复检、定向清理与长任务预算固化为可执行合同，
> 每条条款必须同时具备：规则文本 + 代码/配置落实点 + 治理检查断言（`tools/check_governance_rules.py`）。
> 无法被 checker 断言的条款一律降级为 `docs/maps/80-system-runtime.md` 记录而非本 Rules 条款。

### DS-100 主机资源准入时机

- 主机资源门禁（磁盘 / 使用率 / MemAvailable）必须在**任何状态修改之前**执行，作为第一道关卡；
- `dry-run` / 预检模式只读不改，只输出资源读取与校验结果，不创建锁、不修改任何状态；
- 资源读取必须以结构化行输出（`key=value`），便于后续 grep 与 Map 记录；
- 门禁失败禁止用「扩阈值」或「跳过门禁」绕过，必须先按允许范围清理（见 DS-105）后重试。

### DS-101 容器运行期硬预算

所有运行时服务（postgres、redis、backend、全部 worker、frontend、capture、umami）必须配置**容器级**资源限制：

- **内存上限** `mem_limit`；
- **内存预留** `mem_reservation`；
- **CPU 上限** `cpus`；
- **PID 上限** `pids_limit`；
- **优雅停止时长** `stop_grace_period`；
- **日志轮转** `logging`（复用现有 `x-logging` 锚点）。

规则：

- 全部数值用 `${PANJI_<SERVICE>_<FIELD>:-<默认值>}` 环境变量形式，可在 `market.env` 覆盖收紧；
- 初始值为保守宽松值，后续按部署后实测高水位（DS-104）收紧，**禁止只采集不限制**；
- 宿主机保留 ≥1G 余量（内核 / 文件缓存），不得把 7.4G 内存全部分光；
- 重任务服务的应用级 `memory_budget_mb`（见 DS-107）必须**显著低于**其所在容器 `mem_limit`，为 ORM / 解释器开销与安全边界预留空间，该数值关系由 checker 断言。

### DS-102 构建与重启串行纪律

- 全局 `COMPOSE_PARALLEL_LIMIT=1`，禁止 Compose 并行拉起多容器；
- 镜像构建逐服务串行，禁止前端构建与 `docker build` 并行、禁止 migration 与构建 / 重启并行；
- 重启按固定波次，禁止一次性 `up -d` 交出全部 Python 服务：
  1. backend → 健康 / 就绪检查；
  2. frontend；
  3. Scheduler 单独成波并立即校验单实例；
  4. 普通 Worker 小批次；
  5. after-close / watchdog；
  6. capture（最后）；
- **数据服务（postgres / redis / umami）永不进入普通重启列表**，避免触碰持久化数据；
- 波与波之间插入健康 / 存在性检查，任一失败即停并走失败路径。

### DS-103 长命令超时

所有长命令（`npm ci` / Vite build / `docker build` / alembic / compose up / health-ready 等待 / 远程总时长）必须有**外层超时**：

- 统一封装为单一辅助函数（如 `run_with_timeout <stage> <seconds> -- <cmd...>`），避免散落的重复 timeout 逻辑，也便于 checker 断言；
- 超时时必须：记录 `failure_stage`、释放部署锁、走既有失败路径、**禁止自动重试**、不得继续后续步骤；
- 未超时阈值由 Runbook 给出，禁止"超时值被绕过（如 `timeout 0` / 无 timeout）"。

### DS-104 部署后资源验收

部署成功前必须复检（任一失败即判部署失败，不得写成功状态文件）：

- **主机**：磁盘可用 / 使用率 / MemAvailable / swap；
- **容器**：任一关键容器 `State.OOMKilled=true`、异常 `RestartCount` → 失败；
- **配置生效**：`docker inspect` 读取 `Memory` / `PidsLimit` / `NanoCpus` 为 0（未生效）→ 失败；
- **高水位采集**：`docker stats --no-stream` 输出各容器内存，作为后续收紧预算的证据；
- **服务**：health / ready / 单实例校验。

清理（DS-105）之后再执行一次同样的资源复检，确认清理后资源不反弹。

### DS-105 旧 SHA 业务镜像精确回收

仅当本轮实际构建镜像（`IMAGES_BUILT=true`）时触发：

- 先构造**保留集合**：当前运行 SHA、上一成功部署 SHA、任何 `*-rollback` 标签、基础镜像、非 `market-dev` 项目镜像；
- 按**完整 SHA 分组**枚举 `market-dev-{backend,capture,frontend}:<sha>`，只有该 SHA 组的三个标签**全部不在保留集合中**才整组删除；
- **禁止**按模糊名（`<*>`）、创建时间或数量上限删除镜像；
- 删除前后各输出一次磁盘证据，并记录回收的 SHA 列表；
- 禁止 `docker system prune` / `docker image prune -a` / `docker volume prune`。

### DS-106 遗留容器定向治理

- 遗留无用容器禁止用通用 `container prune` 清理；
- 只能先做**只读盘点**，记录：容器名、镜像、状态、退出码、创建时间、挂载卷、是否有日志；
- 满足以下**全部**条件才建议删除，且删除仍需**当轮明确授权**：
  1. 不属于当前 `docker compose` 项目管理的服务；
  2. 状态为 exited / dead（非 running）；
  3. 挂载卷不包含业务数据或持久数据；
  4. 镜像仍受保护或可重建（不依赖唯一本地镜像）；
  5. 退出码为已知正常结束；
  6. 仓库无活跃引用。
- **Volume 永不随容器删除**（`docker rm -v` 禁止），业务数据卷只能经正式备份 / 迁移流程处理。

### DS-107 长任务统一资源合同

以下长任务主链必须统一支持资源治理：**Feature Snapshot（第一金字塔）**、**stock core**、**Review**（含 bootstrap / 回填）。

**stock core 边界（本条款的权威定义，避免「规则说必须有、代码没有、文档却宣称完成」）：**
`stock core`（`first_pyramid` 核心快照）是**单股、同步、纯计算**单元
（`first_pyramid_service.compute_first_pyramid_core_snapshot`），其内存有界、不随任务线性累积；
它**不是**独立的批量长任务。stock core 总是在 Feature Snapshot 批量管线内部、按股调用
（`feature_snapshot_service.compute_review_core_for_trade_date` → `compute_feature_snapshot_for_date`），
其内存治理由**外层 Feature Snapshot 批量循环的 DS-107 预算门禁**统一承担。
因此：stock core **不得**各自复制一份独立预算实现；只要 Feature Snapshot 批量入口（回填 / after_close）
落实了本合同的全部必备字段，即视为 stock core 已纳入治理。若未来引入独立于 Feature Snapshot 的
stock core 批量入口，该入口必须另行满足本合同的全部字段。

必备字段（checker 可断言的落地形态）：

- **分片**：按自然分片（交易日 / scope / chunk）处理，不在内存线性累积全部结果；
- **并发**：默认 1，禁止用并行放大峰值内存（`--workers` 必须等于 1，非 1 直接拒绝）；
- **内存预算**：`memory_budget_mb` 可配置，且必须**显著低于**所在容器 `mem_limit`（DS-101，初值取重 worker 上限的 ~75%），禁止等于或高于容器上限；
- **峰值 RSS**：记录 `peak_rss_mb`；
- **心跳与进度**：记录 heartbeat / progress / processed / remaining；
- **安全停止**：超预算安全停止，返回 `stop_reason`（completed / memory_budget_exceeded / cancelled / error），**绝不静默截断、不假装成功、OOM 被杀不得写 success**；
- **成功判定（禁止 partial 伪装 success）**：run 标 `succeeded` 必须同时满足 `stop_reason is None` **且** `processed_count == expected_count` **且** 失败率不超阈值；内存超限或未处理完全部预期单位时必须写 `failed`（或数据模型支持的 `partial`），并如实记录 `stop_reason`，禁止发布 publication pointer；
- **检查点恢复**：`resume_token` / checkpoint 必须承载**业务断点**（最后完成的 instrument / trade_date / run_id / 输入参数 hash / schema 版本），并**被续跑入口真正读取**以决定从何处继续，而非仅作状态快照字符串；已完成分片恢复须幂等；
- **partial 状态**：未完成时如实上报 partial，不得写成功。

分片结束必须释放 ORM 对象（`session.expunge_all()` 等），批次内部按步长采样内存（O(1) 系统调用）。

共享工具：`backend/app/utils/long_task_budget.py`，统一提供 RSS 采样 / 预算判定 / 峰值累计 / 停止原因 / checkpoint 序列化；禁止各长任务各自复制实现导致漂移。

违反上述任一条即视为实现缺陷，必须修实现而不是调大预算。

## 远程临时验证数据库与验证栈（2026-08-05，V2.1 验收闭环）

> 背景：V2.1 开发链需要真实 PG 验证（Migration、PG 集成、Synthetic E2E、远程手动验收），
> 但本地/CI 永久禁止任何临时数据库。因此在 `panji-prod` 已有 PostgreSQL 容器内引入
> **唯一允许的临时数据库** `bz_stock_verify_<sha>`，由正式验证脚本创建、检查与删除。
> 本节的合同优先级高于 `40-testing-quality.md` 中"本地/CI 永久禁止"的条款，但**不授权**任何本地/CI 临时库。

### DS-110 远程临时验证数据库

- **命名**：`bz_stock_verify_<7到40位SHA>`，SHA 必须为待验收的 `origin/dev` 精确 commit。
- **创建入口**：仅允许正式验证脚本（`scripts/verify/create_verify_database.sh`）在 `panji-prod` 已有 PostgreSQL 容器内创建；禁止新建 PostgreSQL 容器或 Volume。
- **连接校验**：应用/测试连接建立后必须执行 `SELECT current_database(), current_user;` 并确认数据库名匹配验证库命名；若 `current_database()` 返回 `bz_stock`，立即中止并告警。
- **禁止连接 `bz_stock`**：验证栈的所有连接字符串、Worker、测试、seed 脚本不得指向 `bz_stock`；任何写入 `bz_stock` 的动作视为越权。
- **Migration 规则**：允许 DDL 与 Alembic，但只针对验证数据库，从确认 head 升级到目标 migration（含 085/086），可执行 upgrade→downgrade→upgrade 验证；不得触碰 `bz_stock` schema。
- **连接终止**：每次验证尝试结束后，清理脚本必须先停止并断开验证栈连接，再删除该次精确命名验证数据库。
- **尝试后删除**：无论成功、失败、取消或超时，都由 `scripts/verify/drop_verify_database.sh` 在证据导出后删除 `bz_stock_verify_<sha>`；不得以等待验收或保留现场为由长期占用磁盘。
- **无备份要求**：验证数据库不是业务数据库，不要求备份，删除不可逆但无业务影响。

### DS-111 远程验证栈

- **Compose project 独立命名**：使用独立的 `docker-compose.verify.yml` 与 project 名（如 `panji-verify`），不得复用正式 `market-dev` project，避免容器名/网络冲突。
- **端口仅绑定 `127.0.0.1`**：验证栈所有对外端口只绑定服务器回环地址，只通过 SSH Tunnel 给用户访问，不得暴露公网。
- **独立环境文件**：使用独立的 `market.verify.env`，明确 `APP_ENV=verification`、`DATABASE_URL=<bz_stock_verify_<sha>>`、独立的 Redis DB 或 key 前缀，不读取正式 `market.env`。
- **自动 Scheduler 关闭**：验证栈 `PANJI_SCHEDULER_ENABLED=false`（或等价），避免自动盘后编排干扰验收。
- **只启动必要 Worker**：仅启动 after-close / chip / watchdog 等验收必需的 Worker，不得复用正式 Worker 容器，不得加入正式 Nginx 公网入口。
- **运行 SHA 可检查**：验证栈必须暴露 `runtime_git_sha`、repo HEAD、镜像 SHA 三类证据，且均等于目标验收 SHA。

### DS-112 验证数据合同

- **标准验证 100% synthetic**：验证栈、PG tests、Seed 和 Synthetic E2E 不得连接、读取或复制 `bz_stock`；所有输入由目标 SHA 内受版本控制的 deterministic synthetic producer 生成。
- **原始事实范围**：Seed 只创建有限 instruments、1d/1h/15m bars、released config、交易日历、board/auction raw facts、PIT membership、历史 prerequisite 和受控失败输入。
- **禁止伪造终态**：Seed 不得直接写 `succeeded/published`、固定 coverage、固定成功/失败/跳过计数、最终 Review/chip/auction payload、publication pointer 或 ProductReadiness；终态必须由真实 producer、Worker 和质量门自然形成。
- **业务数据抽样另立合同**：未来如需真实业务样本，必须由用户另行发起需求和授权，设计受控、脱敏、离线导出；验证栈仍不得直接连接 `bz_stock`，且该模式不得成为基础 PG 验证前置条件。
- **四类场景支持**：seed 必须能生成至少四类代表状态数据：
  1. 完整成功（fully_ready）；
  2. 异步增强（core_ready，chip 异步）；
  3. 降级（degraded_ready，board reused / chip partial / hybrid）；
  4. 治理与恢复（publication missing / lease lost / retryable child）。
- **不得写成一次性远程脚本**：seed 必须是仓库内可复跑的正式 CLI，受版本控制，支持重建验证库时幂等重跑。

### DS-113 每次验证尝试强制清理

每次远程验证、PG 测试、Migration round-trip、Synthetic E2E 或调试尝试都是一个有界资源单元。正式入口必须用 trap/finally 或等价机制保证 pass、fail、cancelled、interrupted、timeout 全路径进入 cleanup；不得依赖操作者记忆手工收尾。

清理前必须导出到本地控制端或轻量持久证据目录：target/repo/runtime SHA、验证数据库名、Alembic revision、失败 gate、pytest/JUnit 摘要、关键日志尾部、`docker ps`、`docker stats --no-stream`、磁盘和内存快照。证据不得包含秘密、Owner 密码或完整业务数据。

自动清理范围仅限本次尝试创建且可精确归属的资源：

- `panji-verify-<sha>`（或合同规定的唯一 project）所属验证容器与 network，执行 `down` 时禁止 `-v`；
- `bz_stock_verify_<sha>`，先终止该库连接，再由正式 drop 脚本按全名删除；
- 本次创建的临时 env、挂载目录、测试报告中间文件、无消费者的测试容器和 BuildKit 临时缓存；
- 仅在本次确实构建时，按精确 tag 删除该次验证专用镜像；复用的基础镜像、稳定运行镜像、当前/上一成功/rollback 镜像不得删除。

永久禁止清理：`bz_stock`、共享 PostgreSQL/Redis/Umami Volume、稳定运行 `market-dev` 容器/network、基础镜像、受保护镜像、非本次创建或来源不明资源。禁止 `docker system prune`、`docker volume prune`、模糊数据库匹配和批量 drop。

cleanup 必须输出 created/deleted/retained/failed 四份精确清单，并在清理后复检磁盘可用量、MemAvailable、验证容器/网络残留和验证数据库不存在。任一残留或清理错误都标记 `blocked_cleanup`，停止创建新验证库或验证栈，先修复清理问题。

### DS-114 验证身份与证据

正式远程验证开始前必须冻结完整 40 位 `target_code_sha`，并满足 target SHA 已提交、可从 `origin/dev` 解析、本地 `HEAD == origin/dev == target_code_sha`、工作树干净。该等式只约束候选验证时点，不约束日常编辑中的工作树。

每个 gate 必须同时记录并核对：`target_code_sha`、`remote_repo_sha`、`runtime_sha`、`verify_database` 和 `alembic_revision`。target/repo/runtime 三个 SHA 必须完全相等，验证库必须精确为 `bz_stock_verify_<40位target SHA>`；任一不一致立即失败，旧 SHA 证据不得累计到新 SHA。

验证栈必须明确表达 verification runtime 身份。`deployment_mode=live` 只能描述代码挂载方式，不能冒充运行平面；正式证据至少包含 `APP_ENV=verification` 或独立 `runtime_mode=verification`，并确认 Scheduler 关闭、端口只绑定回环地址、数据库不是 `bz_stock`。

### DS-115 正式验证执行器与顺序

- Migration 只能由正式 runner 使用 target SHA checkout 中的 `backend/alembic` 和 `backend/alembic.ini` 执行；禁止使用旧运行镜像内置 migration。标准 round-trip 为 upgrade head → schema/revision 断言 → downgrade previous → downgrade 断言 → upgrade head → 重复 upgrade 幂等检查，每一步前后断言 `current_database()`。
- PG tests 只能由一次性 `verify-test` 服务运行。该服务必须包含 target SHA 的 app/tests/pytest 配置/migration/scripts 和固定依赖，设置 `PANJI_REMOTE_VERIFY_DB_TEST=1`、`APP_ENV=verification`，只连接本轮验证库，运行结束退出。
- 标准顺序固定为：本地修改范围门禁 → commit/push → 冻结 SHA → 远程 clean checkout → 创建验证库 → Migration round-trip → 启动验证栈 → SHA/DB/runtime 断言 → 自包含 PG tests → synthetic Seed 两次及幂等断言 → Synthetic E2E → 导出证据 → DS-113 cleanup。
- 基础 PG tests 必须先于 Seed；依赖 synthetic 场景的测试只能在 Seed 后以 Synthetic E2E 身份运行。

### DS-116 失败、新 SHA 与禁止远程修补

业务代码、Migration、测试、Seed 或验证工具任一失败时，必须停止后续 gate，导出证据并执行 DS-113 cleanup。修复只能回到本地完成，经过本地门禁、commit 和 push 形成新 target SHA；新 SHA 使用新的精确验证库并从 Migration round-trip 重新开始。

禁止远程修改仓库文件、容器内改码、手工修改验证库 Schema、复制 patched script 后继续沿用旧 SHA、把旧 SHA 结果累计到新 SHA，或失败后继续执行后续 gate 再统一宣称通过。cleanup 失败时优先处理资源残留，不得创建新验证环境。

## 分层发布与增量检查点纪律

> 来源：CHANGE-20260729-006

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

## 2026-08-02 收口：通用部署与生产操作纪律（CHANGE-20260802-005 配套）

> 来源：从已删除的工具专属角色文件中提炼的通用规则。

### DS-95 生产只读核验的默认姿势

- 未获本轮明确部署授权时，对生产只允许只读核验：版本端点、健康端点、日志查询、DB 只读查询；
- 只读核验不得修改任何配置、容器、数据或运行状态；
- 只读结论必须给出实际命令与输出，不得以推断代替核验。

### DS-96 部署后必须留存证据

- 部署完成后必须记录：目标完整 SHA、运行代码 SHA（`RUNTIME_SHA` 与版本端点 `runtime_git_sha`）、
  `deployment_mode`、健康与就绪端点结果、本次是否执行 migration；
- 证据不足时判定为部署未验证，不得写成部署成功。

### DS-97 Migration 门禁

- migration 保持人工门禁，不随部署自动放行到不可逆操作；
- 任何不可逆 migration 必须在提交说明与 CHANGE 中明确标注，并提供 downgrade 步骤；
- 不得修改已发布的历史 migration。

### DS-98 生产恢复禁止临时脚本代替代码修复

- 禁止用 `/tmp` Python 脚本、裸 SQL、`docker cp`、stdin 注入等临时手段替代正式代码修复；
- 发现缺口必须走"代码修复 + 正式测试"，再经正式部署入口生效；
- 已用临时手段补过的生产状态，必须回滚或转正后才能视为闭环。
