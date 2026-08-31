# 系统运行体系 PRD

状态：已确认  
最后确认日期：2026-07-26  
对应 Map：`../maps/80-system-runtime.md`  
需求所有权：本地原生开发、远程容器运行、Git、数据库、Redis、Scheduler、服务和部署边界

> 本文件拥有开发、验证和稳定运行三平面的产品要求。远程临时验证数据库 `bz_stock_verify_<sha>` 与正式验证运行时的安全合同见 [`rules/80-deployment-migration.md`](../../rules/80-deployment-migration.md)；业务闭环的证据状态见 [`31-after-close-product-closure-v2.1.md`](./31-after-close-product-closure-v2.1.md) §11。

## 1. 运行位置与承载方式

### SR-01 三个运行平面

当前区分：本地开发、远程隔离验证、远程稳定运行。远程验证和稳定运行当前可共享物理主机，但数据库、Compose project、端口、凭据、生命周期和授权必须隔离。

IDE 不是运行环境。任何 IDE、编码助手或自动化 Agent 都只是开发和操作工具。

### SR-02 本地使用原生进程

本地开发环境不使用 Docker 或 Docker Compose 启动盘迹应用服务。

本地服务使用原生开发工具运行：

- 后端通过本地 Python 虚拟环境直接启动；
- 前端通过本地 Node.js 和 Vite 直接启动；
- 本地不启动远程常驻 Worker、Scheduler、盘后编排或全市场任务；
- 本地不创建盘迹 PostgreSQL 或 Redis 容器；
- 普通代码修改不得要求反复构建本地 Docker 镜像。

本地测试不连接 PostgreSQL。需要本地页面预览时应优先使用 mock/fixture；任何连接业务数据源的调试都不是测试，也必须获得明确授权并保持只读。

### SR-03 远程使用 Docker Compose

腾讯云远程稳定运行位置使用 Docker Compose 管理已经确认的服务，包括：

- 后端；
- 前端；
- Worker；
- Scheduler；
- PostgreSQL；
- Redis；
- Nginx；
- 其他正式运行服务。

远程容器化方式不得反向成为本地开发的强制依赖。

## 2. Git

### SR-09 长期分支策略

仓库只保留以下三个长期分支：

| 分支 | 职责 | 进入方式与约束 |
|---|---|---|
| `main` | 阶段性稳定锚点 | 未经明确授权不得修改、合并或推送 |
| `dev` | 唯一默认日常开发分支 | 所有变更直接在 `dev` 提交；`dev` 是手工 CI 与手动部署的唯一来源 |
| `experiments` | 明确授权下的隔离实验 | 仅在明确授权时使用；不得作为远程开发部署来源；进入 `dev` 前必须明确审查 |

本地、`origin` 和远程服务器只允许保留 `main`/`dev`/`experiments`。现有分支不符合该命名时，必须在用户明确授权分支删除或改名后单独治理，不得在普通任务中强删。

### SR-10 日常开发分支（dev-only）

本地日常开发直接在 `dev` 进行。所有 IDE、编码助手和自动化 Agent 默认直接在当前 `dev` 分支工作与提交，不按工具区分。

未经用户明确授权：

- 不得创建任何新的本地或远程分支，包括 backup 分支；
- 不得从 `dev` 切换到其他工作分支；
- 不得对任何分支 force push。

需要可恢复点时，使用 **checkpoint commit** 替代新建分支。

### SR-11 稳定分支

`main` 用于远程稳定版本。

### SR-12 开发不自动部署

推送 `dev` 本身不触发 CI 或远程部署。CI 只能通过 `workflow_dispatch` 手工触发，且只是诊断工具。

### SR-13 稳定版本可识别

远程当前运行版本必须能够通过稳定 SHA 或等价方式明确识别。

### SR-14 稳定版本手工部署

开发部署只能由获得本轮明确授权的执行主体，在本地运行
`scripts/ops/panji-test-deploy <FULL_SHA> [--dry-run]`。目标 SHA 必须属于 `origin/dev`。

部署必须满足：串行锁；远端工作树干净；始终叠加 prod + live Compose；普通代码走
`/opt/panji-live`；仅环境变化构建对应镜像；仅 migration 变化执行前向 upgrade；核验完整 SHA、
运行模式、健康端点、挂载来源、关键容器和 Scheduler 单实例；失败恢复上一成功代码和应用容器，
但不自动 downgrade 数据库；永不删除 PostgreSQL/Redis Volume，也不自动执行任何业务数据动作。

GitHub Actions 不包含部署动作。详细合同见 `rules/80-deployment-migration.md`。

### SR-15 本地参考/传输目录不得进入仓库

本地参考资料与废弃中转目录不得进入 Git 仓库的所有活跃分支（`main`/`dev`/`experiments`）：

- `ref/` 是本地参考资料（第三方项目源码、用户原创 Pine、实验归档脚本），保留本地实体但不得被 Git 跟踪；
- `sync/` 是已废弃的临时中转目录，不得在 Git 或本地存在；
- `.gitignore` 必须包含 `/ref/` 与 `/sync/` 根锚定规则；
- CI 必须显式检查 `git ls-files ref sync` 输出为空；
- 架构守护测试 `test_ref_isolation.py` 必须守护两条目录均无跟踪文件；
- 例外：`archive/*` 注解标签中的历史提交不重写，保留旧版跟踪记录作为只读历史。

## 3. PostgreSQL

### SR-20 数据库平面隔离

本地测试不连接 PostgreSQL；远程验证只连接按 SHA 创建的 `bz_stock_verify_<sha>`；远程稳定运行只连接业务库 `bz_stock`。三个平面不得通过默认配置互相回退。

### SR-21 本地业务调试

本地页面或 API 调试优先使用 fixture/mock。确需观察真实业务数据时，必须获得当前任务明确授权并使用只读凭据；本地不得写入、修复、回填或重算 `bz_stock`。任何业务写入只允许通过远程正式 service/CLI，在独立授权下执行。

### SR-22 Schema 兼容

远程旧代码仍运行时，本地 Schema 变化不得立即破坏旧代码。破坏性变化需要兼容过渡。

## 4. Redis

### SR-30 共享实例

本地和远程使用同一 Redis 实例。

### SR-31 逻辑库隔离

本地和远程使用不同逻辑数据库隔离：

- 队列；
- 锁；
- 缓存；
- 临时运行状态。

### SR-31.1 本地 Redis DB15 正式保留

远程 Redis 配置 `databases=16`，DB15 存在且 `DBSIZE=0`；生产 `docker-compose.prod.yml`、生产脚本和生产业务代码均使用 DB 0，未引用 DB15；无其他项目用途记录。因此 DB15 正式保留为本地开发临时状态隔离库。

### SR-32 本地 Redis 安全启动

本地 development 环境启动时：

- 缺少 `DATABASE_URL` 或 `REDIS_URL` 时必须失败；
- `REDIS_URL` 指向 Redis DB 0 时必须失败；
- 禁止回退到 `redis://localhost:6379/0` 等默认远程队列。

production 现有行为不得被意外改变。

### SR-33 代码一致

逻辑库隔离不得演变成两套业务实现。

## 5. Scheduler 和任务

### SR-40 本地 Scheduler

本地自动 Scheduler 默认关闭。

### SR-41 本地调试能力

本地只启动 Backend、Frontend、Capture 和 SSH Tunnel。远程常驻 Worker、Scheduler、盘后编排和
全市场任务只能在远程正式运行位置执行，且必须获得相应授权。

### SR-42 远程 Scheduler

远程稳定位置通过容器运行自动 Scheduler，并支持手动补跑和调试。

### SR-43 本地禁止业务库写入

本地 development 环境不得执行业务数据库写入，包括但不限于：

- 僵尸任务恢复；
- 策略种子；
- 日历刷新。

需要执行上述动作时，必须使用远程正式 service/CLI 并取得独立授权。

## 6. 服务一致性

### SR-50 同一套代码

本地和远程尽量使用相同业务代码、ORM、Repository、Worker、Orchestrator、指标实现、配置字段和依赖版本。

### SR-51 差异配置化

运行位置差异通过明确配置表达，不通过复制业务代码、主机名或 IDE 类型判断。

### SR-52 代码一致，承载方式允许不同

本地与远程必须复用同一套：

- 业务代码；
- 数据模型；
- Migration；
- API；
- Worker 实现；
- 指标算法；
- 配置字段。

允许存在以下承载差异：

- 本地使用原生进程；
- 远程使用 Docker 容器。

不得为了适配两种承载方式复制业务代码或维护两套业务逻辑。

## 7. 部署与数据安全

### SR-60 部署不默认重建数据服务

普通前后端部署不默认重建 PostgreSQL、Redis 或删除持久化 Volume。

### SR-61 远程任务稳定优先

开发修改不得无意中断远程每日盘后运行。

### SR-62 端口 80

远程端口 80 不得显示默认 Nginx 页面。

## 8. 验收标准

- 本地和远程的运行位置、承载方式、版本、数据库和 Redis 关系可明确识别。
- 本地后端和前端能够不依赖 Docker 原生启动。
- 本地不创建或启动盘迹 PostgreSQL、Redis 和应用容器。
- 本地和 CI 不连接 `bz_stock` 运行测试；真实 PG 测试只在 `bz_stock_verify_<sha>` 运行。
- 远程验证通过不会自动获得稳定运行部署或业务数据操作授权。
- 本地任务不会进入远程队列。
- 本地自动 Scheduler 关闭；完整手动链路只在远程隔离验证环境可用。
- 远程继续通过 Docker Compose 稳定运行正式服务。
- 本地原生进程和远程容器复用同一业务代码和配置语义。
- `dev` 推送不自动部署。
- Map 能指向真实本地启动入口、远程 Compose、配置、CI 和版本核验入口。

## 9. 稳定运行部署 SSOT（dev SHA + Live Mount）

### SR-70 部署环境定位

**当前 `panji-prod` 腾讯云物理机是盘迹唯一的远程运行环境，同时承担日常开发部署与业务运行。**

稳定运行部署只指：把 `dev` 上已验证的精确 SHA 同步到稳定运行栈并重启受影响服务。远程验证、业务数据操作和稳定运行部署是三类独立动作，授权与证据不得互相替代。

`dev` 是部署的唯一来源。禁止从 `main` / `experiments` / 任意本地未推送状态部署。

### SR-71 禁止的部署方式

以下方式一律禁止（即使用于"先测试一下"）：

1. 禁止 `scp` 单个 `.py/.tsx/.js` 文件到服务器；
2. 禁止 `docker cp` 从本地拷贝任意容器内文件；
3. 禁止 SSH 进入容器内手动 `sed/vi` 修改源代码；
4. 禁止临时业务脚本（`create_run.py`/`publish_review_oneoff.py` 等）在远程开发运行服务器上任意执行（如果必须执行，必须通过正式 orchestrator API 或 `panji-test-deploy` 的受控 `worker oneoff` 步骤）；
5. 禁止只重建 `backend` 单服务不做健康检查；
6. 禁止在一次部署中混合 Live Mount 代码同步与镜像重建。

### SR-72 `panji-test-deploy` 正式入口

`scripts/ops/panji-test-deploy` 是开发部署的唯一入口（SSOT）。入口必须：

| 步骤 | 约束 |
|---|---|
| `preflight` | 运行 `scripts/ops/panji-prod-preflight`；通过后方可继续；不通过立即退出且不做任何修改 |
| SHA 校验 | 精确校验待部署 SHA = 本地 `dev` HEAD = `origin/dev` 已推送 SHA；禁止"latest tag"/"HEAD of dev"模糊匹配 |
| 代码同步 | 普通 Python / 前端变更走 Live Mount：服务器 checkout 精确 SHA，同步运行代码到 `/opt/panji-live`，写入 `RUNTIME_SHA`；**不构建镜像** |
| 镜像构建 | 仅当依赖清单（`pyproject.toml`/lock、`package.json`/lock）、`Dockerfile`、系统依赖、基础镜像、Capture 运行环境或必须固化的 Nginx 配置变化时才构建；一次部署不得混合两种模式 |
| 服务范围 | 只重启受影响服务：backend/frontend/capture；**禁止**重建 PostgreSQL、Redis、删除 volume、修改持久数据 |
| Migration | 仅在变更包含 migration 时执行 `alembic upgrade head`（幂等）；禁止 `alembic downgrade` |
| 健康检查 | 后端 `/health`、前端 `/`、版本端点必须返回 200；业务 smoke 通过方可标记部署成功 |
| SHA 一致性证明 | 部署结束后必须核验 2 项：① 服务器仓库 HEAD = 目标 dev SHA；② 运行时 `runtime_git_sha` = 目标 dev SHA。任一不一致视为部署失败 |
| 数据边界 | 代码部署不得自动执行数据 apply / 建 run / 发布 pointer；数据操作是独立授权动作 |

### SR-73 部署与 CI 的关系

1. CI（`.github/workflows/ci.yml`）是**人工诊断工具**，不是部署前置条件；
2. CI 未运行本身不阻止部署，但已取得的 CI 失败必须处理或明确证明与目标 SHA/修改范围无关；
3. 部署前必须通过本地修改范围测试；触及数据库、Worker/编排、发布指针、权限写入或跨服务链路时，还必须由同一 SHA 通过对应远程验证；
4. 部署完成后，运行时版本端点返回的 `runtime_git_sha` 必须与目标 dev SHA 一致；
5. 不得只在聊天输出声称部署成功，必须给出上述 2 项 SHA 核验证据。

### SR-74 可重复远程验证框架

正式远程验证必须由长期保留的单一框架执行，而不是为每次需求复制脚本。调用方只提交完整
40 位 `target_code_sha` 和仓库登记的封闭计划；计划只能组合预注册 runtime/test/seed/e2e/
timeout profile，不接受任意命令。每次 attempt 使用唯一数据库与 Compose project，冻结
target/repo/runtime SHA、数据库、Alembic revision 和输入计划，按 Migration round-trip、运行身份、
自包含 PG、synthetic seed 幂等及 Synthetic E2E 顺序执行。无论结果如何都精确清理本次临时资源，
清理失败状态为 `blocked_cleanup`；有界且脱敏的证据必须保留供归因。
