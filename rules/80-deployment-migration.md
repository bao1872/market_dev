# 80 远程部署、Migration 与运行安全

## 1. 部署位置

盘迹实际运行部署只发生在远程运行服务器 `panji-prod`。

本地机器只承担：

- 代码编辑；
- 本地纯单元/静态/前端验证；
- Git 提交与 push；
- 作为远程控制端执行正式脚本；
- SSH Tunnel / 只读调试。

本地 preview 不等于部署。

## 2. 唯一远程入口

远程访问使用仓库正式入口：

- `scripts/ops/panji-prod-preflight`
- `scripts/ops/panji-prod-ssh`

禁止使用历史别名、原始 IP 或自行发现替代入口绕开当前 runtime map。

## 3. 部署来源

部署只接受 `origin/dev` 可解析的 exact SHA。

部署后必须确认：

- runtime SHA == target SHA；
- DB revision 符合 target；
- backend 可以启动；
- 当前 slice 所需 API 可用。

Exploration 不自动要求全域 Scheduler/Worker/Readiness release certification。

### 3.1 Live Refresh 与 Operational Deployment

在已核验的 Live Mount 开发运行栈中：

- **Code Sync**：同步 backend source 或 frontend dist；
- **Live Refresh**：Code Sync 后只 restart 受影响的应用进程；frontend source-only 不 restart；
- **Operational Deployment**：container recreate、environment image build、Compose/runtime env
  变化、Migration 或 stable Release。

source-only Code Sync / Live Refresh 继承代码改动的 Level，不因远程同步或进程 restart 单独
升级为 Level 3。它们仍必须经唯一正式入口、绑定 origin/dev exact SHA、保留 rollback owner、
通过当前 claim 所需 health/smoke。Operational Deployment 继续执行 Level 3 门禁。

## 4. 正式部署方式

运行方式、Live Mount/镜像模式、Compose、构建流程以当前 `docs/maps/80-system-runtime.md` 与正式 Runbook 为准。

禁止：

- `scp` 单文件热修；
- `docker cp` 注入业务源码；
- SSH 进容器 vi/sed 改源码；
- 临时 `PYTHONPATH` 拼接另一 SHA；
- 把未提交服务器代码当正式版本。

## 5. Migration 基本纪律

- 不修改已发布历史 migration；
- 只新增 forward migration；
- migration 必须有明确 downgrade（若技术上可逆）；
- 修改 migration 时在验证库执行 upgrade/downgrade/upgrade；
- 执行前确认当前 DB revision；
- 执行后确认 target revision；
- Migration 只能在远程受控流程执行，不从本地直接连业务 DB 跑 Alembic。

## 6. Migration 风险等级

### M0 — No Migration

无 Schema 变化。

无需 migration rehearsal。

### M1 — Additive / Low Risk

典型：

- 新 nullable column；
- 新表；
- 小范围 index；
- 小规模、可验证 metadata backfill；
- 不改变既有列语义；
- 不删除数据。

Exploration 要求：

- migration 静态审查；
- upgrade/downgrade/upgrade on verify DB；
- 对真实业务 DB 做只读 conflict/precondition check；
- 如要写 `bz_stock`，需要明确部署/migration 授权。

**默认不要求 production clone / pg_dump。**

### M2 — Constraint / Rewrite / Medium Risk

典型：

- NOT NULL；
- UNIQUE；
- FK；
- 大量 backfill；
- column type conversion；
- 可能锁大表；
- publication/pointer identity 变化。

要求：

- M1 全部；
- 真实业务数据只读 precheck；
- 评估锁和耗时；
- 明确 rollback/downgrade；
- 必要时在 representative copy / clone rehearsal。

是否 clone 由真实风险决定，不是机械必做。

### M3 — Destructive / Irreversible / High Risk

典型：

- DROP；
- destructive rewrite；
- 无可靠 downgrade；
- 可能丢失唯一数据；
- 大规模历史重构。

自动进入 Hardening。

必须：

- 用户明确授权；
- 明确 backup / clone / recovery 策略；
- production-like rehearsal；
- release decision。

## 7. 备份

开发期不默认整库备份。

只有：

- 用户明确要求；
- M3 / Hardening 风险需要并经用户授权；
- 或确有不可恢复风险；

才进行大体积 `pg_dump` / clone。

不得把“安全起见”作为每次 Migration 创建 17GB clone 的默认理由。

## 8. 真实业务 DB 写入

对 `bz_stock` 执行 Migration 或业务写入前：

- 当前任务必须已明确授权；
- 必须确认 target SHA；
- 必须确认当前 revision；
- 必须通过适用的 T0/T1/必要测试；
- migration 必须通过风险等级要求；
- current_database 必须显式确认。

## 9. 远程验证库

允许的真实 PG 测试库：

`bz_stock_verify_<sha>`

要求：

- 位于已有 PostgreSQL；
- 由正式验证入口创建/清理；
- 不能连接/读取 `bz_stock` 作为测试 fixture；
- 每次 attempt 结束精确清理；
- 不创建新 PG Volume；
- 不用模糊 prune。

存在验证栈不意味着 Exploration 每轮必须 full-closure。

### 9.1 验证执行安全合同（Always-On）

远程验证执行遵守以下硬约束（单可复用运行时 CHANGE-20260806-012）：

- 唯一正式入口是 `scripts/ops/panji-verify`，禁止增加第二入口；
- 单可复用验证容器 `panji-verify-python`，常驻空闲（`sleep infinity`），固定 Compose project `panji-verify`，不发布 host port；
- attempt env 由 `prepare_verify_environment.py` 生成并注入 `attempt.env`（0600）；容器常驻 env 只持有稳定变量；
- Migration / PG / Seed / E2E 各 gate 串行以 `docker exec panji-verify-python verify_exec.py <cmd>` 运行 fresh process；
- 异常/timeout/interrupted 以 `docker restart panji-verify-python` 恢复干净环境，不删 container/image/network/PG/Redis/稳定栈；
- 验证库 `bz_stock_verify_<sha>` 跑在已有 `trading-postgres` 容器内；cleanup 只 drop 该库 + 删 attempt 临时状态，不 `compose down`、不删 Volume、不 `FLUSHALL`；
- **清理必须 fail-closed（`blocked_cleanup`）**：任一残留或清理错误都标记 `blocked_cleanup` 并阻止进入后续阶段；
- 禁止：`down -v`、`--rmi`、`docker cp`、`--remove-orphans`、host 直接跑 psql/alembic、在清理/runner/entry 中引用已删除的 `panji-verify-run`。

## 10. Runtime Alignment

当运行 SHA 明显落后当前需要验证的 target SHA：

- 先判断当前 hypothesis 是否需要 target runtime；
- 如果需要，优先把远程开发运行栈对齐到 target；
- 不应花大量时间按新 PRD 审计一个不实现新合同的旧 Runtime；
- 但部署前仍必须完成当前改动要求的 Correctness/Test/Migration 安全门。

## 11. 资源安全

禁止：

- `docker system prune -a`
- `docker image prune -a`
- `docker volume prune`
- 删除 PostgreSQL / Redis 持久 Volume；
- 删除当前运行或唯一回滚镜像；
- 为腾空间删除业务数据。

资源清理必须精确指向本轮可确认的临时/旧 SHA 资产。

### 11.1 部署资源门禁顺序

部署资源门禁分两个语义独立的阶段，**顺序本身是合同**：

1. **静态资源预算**（任何状态修改之前，fail-closed）：阈值配置健全性 + 主机磁盘余量。
   该阶段**不含** `MemAvailable` 判定。
2. **部署内存 headroom**（fail-closed）：必须在 supervisor-drain fence 之后、
   **首笔 runtime mutation 之前**读取 `MemAvailable`，要求 `>= PANJI_MIN_MEM_MB`。

禁止：

- 在任何 fence 之前用 `MemAvailable` 门槛否决部署——fence 释放长任务 worker 的常驻内存后
  才是本次部署真实可用的 working set，提前判定会阻止本可安全回收内存的部署；
- 把 `PANJI_MIN_MEM_MB` 解释为"宿主机稳态必须空闲"的指标，或据此新增第二个数值阈值；
- 由容器 `mem_limit`（单容器允许使用的上限）反推宿主机必须空闲的内存。

`PANJI_MIN_MEM_MB` 是**部署期**保守 headroom，本阶段不下调。

**部署后主机内存为 observation-only**：只作为收紧预算的证据记录，不作为失败门槛。
部署后仍然 fail-closed 的是：磁盘门槛、容器健康（`OOMKilled` / 异常 `RestartCount` /
`Memory`·`PidsLimit`·`NanoCpus` 是否实际生效）、health 与 SHA/Mount 核验。

内存 headroom 的测试 seam 只允许在 dry-run 生效；真实部署遇到该变量必须 fail-closed 拒绝。

### 11.2 dry-run 零 mutation

`--dry-run` 必须是**纯计划**：不得修改远端工作树、运行目录、容器、环境文件、数据库或部署状态文件。

- 不得 `stop` / `up` / `--force-recreate` 任何生产容器，**包括**为建立部署临界区而停止
  长任务 worker；dry-run 只能**模拟** supervisor-drain fence（只读探测容器状态与活跃任务计数）。
- 模拟态不得冒充真实 fence 的状态字段：真实 fence 专属字段只允许在真实部署中出现。
- 无测试 seam 时 dry-run 不读取真实 `MemAvailable`，该门槛延后到真实部署 fence 之后执行。
- rollback owner 解析、变更分类、计划核验等**只读**步骤仍必须真实执行，不得跳过。
- dry-run 若发生了任何容器状态变化，即属违反本条，必须按事故如实记录，不得写成"零生产修改"。

## 12. 基础镜像

受保护基础镜像不得因普通清理删除。

构建缓存和 dangling image 只在本轮确实产生相应构建资产时受控清理。

## 13. 长任务

大规模 backfill / bootstrap：

- 单并发或有明确内存预算；
- 分片；
- 记录 progress；
- 失败时 partial；
- 不因内存不足静默截断；
- 不通过随意扩大机器资源掩盖实现缺陷。

### 13.1 超时与 liveness 政策（四类隔离）

长任务的失败判定必须区分以下四类，不得混用：

- **A. 基础设施命令超时**（deploy / rsync / compose / verification gate / migration command / 短基础设施操作）：允许 absolute wall-clock timeout 作为 Always-On Safety。此类超时继续有效，不受本政策影响。
- **B. 长任务业务绝对超时**：对于工作量随 instrument count / backfill window / provider throughput 变化的 long-running business batch（全市场 refresh、backfill、bootstrap、盘后重计算），**禁止仅因 generic fixed absolute elapsed time 判 failed**。此类任务只要持续产生 valid progress，即允许其按实际所需时间自然完成。
- **C. 卡死 / 无进展（stall / no-progress）**：long-running batch 真正防卡死的依据是「持续无真实业务 progress」，而非「总耗时过长」。stall watchdog 基于 `now - last_real_progress`，不计 `total_elapsed`。
- **D. 业务截止时间（business deadline）**：只有当权威 PRD 明确存在 business cutoff / deadline 时，才允许 total deadline 成为业务失败条件。

### 13.2 progress / heartbeat 正确性

- 长任务必须存在可观察的真实 progress signal（chunk / batch / item 完成数 / checkpoint / last real progress），复用现有 `long_task_budget` 的 checkpoint / resume 能力，不新建 observability framework。
- heartbeat / lease 刷新**不得**被当作业务 progress 的唯一依据：当 step 处于 CPU-bound 或 blocking provider call 无法刷新真实 progress 时，heartbeat 单独存在不构成「有进展」，不得据此排除 stall。
- stall 阈值、heartbeat interval、watchdog 实现属于 Code / Config / Runbook，**不写进规则文件作为实现数字**。

### 13.3 resume / data safety 不变

本政策下的 timeout / liveness 调整不得削弱现有 `long_task_budget`（DS-107）的 idempotent / resumable / checkpoint-safe 约束，亦不得削弱 resume、data safety 与 lineage 正确性。

### 13.4 边界

本规则**不意味着全系统取消 timeout**：A 类基础设施命令超时、verification plan timeout、deploy safety timeout 继续允许。仅当发现某基础设施超时错误复用了 B 类 long-running business policy 时才需纠正。

## 14. Exploration 部署完成

当前 hypothesis slice 的远程部署/运行只需证明：

- target SHA 正确；
- DB revision 正确；
- backend 启动；
- 当前 slice API 正确；
- 当前 slice runtime/前端技术闭环成立。

不自动要求：

- 全域 fully_ready；
- full release smoke；
- 所有 Worker；
- full closure。

source-only Live Refresh 的动作范围：

- frontend source：远端 build dist → sync，Nginx 容器不 restart；
- backend API-only：sync → restart backend → endpoint/health smoke；
- 其他 backend runtime source：sync → 当前保守 classifier restart 全部 Python services →
  targeted smoke；`worker-after-close` 继续走既有 owned fence/restore；
- 当前 classifier 只提供上述两档，不要求 dependency graph、自动 import analysis 或
  细粒度“相关 worker”影响矩阵；
- dependency/Dockerfile/runtime env/Compose/Migration：不得降级为 Live Refresh，走现有严格路径。

## 15. Hardening 部署

正式 release 的部署与验收追加 `70-hardening-release.md`。
