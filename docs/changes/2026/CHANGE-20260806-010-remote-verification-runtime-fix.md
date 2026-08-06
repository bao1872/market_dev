# CHANGE-20260806-010 — 正式远程验证运行时修复

- 日期：2026-08-06
- 类型：governance + verification-infrastructure + bugfix
- 状态：`implemented_pending_remote_verification`
- 需求出处：用户明确授权修改受保护治理体系、远程验证框架并执行完整测试与部署流程

## 根因与修复

`panji-verify` 要求远程非交互 Shell 预置 `VERIFY_DB_URL`，实际环境永远不会提供；后续编排还错误
依赖宿主机 `psql`/Alembic，并指向不存在的 `e2e_readiness_check.py`。正式入口因此无法运行。

修复后入口只传完整 SHA 与登记计划。目标 SHA 的远端 runner 在仓库外生成 `0600` 单次环境文件，
秘密不进入 SSH 参数、进程参数、manifest 或 Git，并由 trap 删除。建删库通过 PostgreSQL 容器；
Migration、PG、Seed 和 closure E2E 都由一次性 `verify-test` 运行。治理检查器与合同测试禁止旧模式回潮。

本地全流程检查同时发现前端 `test:contract` 依赖 Node 22 的实验参数，而项目实际 Node 20 无法启动。
现已锁定 `tsx` 开发依赖，并将五个测试文件的 `import.meta.dirname` 改为标准
`fileURLToPath(import.meta.url)`，使合同测试在 Node 20+ 可重复运行。

首次远程 attempt 在 Migration gate 暴露 asyncpg URL 被同步 Alembic engine 使用，触发
`MissingGreenlet`。cleanup 已确认数据库、容器、网络和敏感文件均归零。修复为验证环境同时生成
异步 `DATABASE_URL` 与同步 `MIGRATION_DATABASE_URL`，Alembic 明确优先后者；该失败证据不计入新 SHA。
第二次 attempt 发现该变量误配到 backend 而未注入 `verify-test`；cleanup 同样完整归零。现已修正
Compose 服务作用域，并将治理检查升级为 `verify-test.environment` 定点断言。
第三次 attempt 已通过 Migration round-trip、运行时与 SHA 身份 gate，但稳定运行镜像不含 pytest，
PG runner 无法启动；cleanup 再次完整归零。现新增 Dockerfile `verification` target，在构建期安装
锁定 `.[dev]` 依赖，使用目标 SHA 专属 tag，并由 runner trap 精确删除。
第四次 attempt 的专用镜像和 Migration round-trip 均通过，但固定等待 5 秒后执行的身份探针在
Backend 尚未就绪时失败；cleanup 已精确移除验证库、容器、网络、环境文件和专用镜像。身份探针
现改为计划超时内的 bounded retry，并在超时前将脱敏后的 Compose 状态和 Backend 尾部日志纳入
attempt 证据，避免服务启动失败只留下空 `curl` 错误。
第五次 attempt 已通过身份门禁并进入 PG tests，但 Compose/pytest 的失败正文位于 stdout，旧执行器
只把 stderr 写入 gate，导致证据仅显示依赖容器状态。现统一合并、限长并脱敏 stdout/stderr，覆盖
PG tests、Seed 和 E2E 失败路径，同时移除从未生成却反复尝试复制的伪 JUnit 报告引用。
第六次 attempt 的有效证据显示：projection 生命周期测试漏建 `instruments` 和 snapshot run 外键前置；
基础 PG runner 又提前执行了明确依赖 Seed 的 closure 测试。现补齐 projection 测试自有的完整前置
数据，并将 closure 测试仅保留在两次 synthetic Seed 之后的 E2E gate，恢复“基础 PG 测试不依赖
Seed、每个测试自包含”的执行合同。
第七次 attempt 将基础 PG failures 收敛为 projection 单项：测试快照仍手写已废弃的 `coreArtifact`
包装，生产 codec 因缺少 canonical 顶层 `dsaProjection` 而跳过全部记录。测试现直接复用
`encode_dsa_projection_to_summary` 生成合同数据，消除测试 payload 与生产 codec 的双重定义。
第八次 attempt 的基础 PG 门禁已全部通过。Seed 使用绝对脚本路径启动时，Python 却从镜像 wheel
而非目标 SHA live mount 导入 `app`，因 wheel 缺合同 JSON 而失败。Seed 现通过
`python -m scripts.verify.seed_v21_verify_data` 从 `/app` 启动，确保脚本和应用代码都解析为目标 SHA；
PG gate 文案同步移除尚未执行的 closure 声明。
第九次 attempt 确认目标 SHA 导入路径生效，Seed 进入 synthetic instruments 写入后暴露 typed bind
错误：脚本向 PostgreSQL `date/timestamp` 参数传入 ISO 字符串。现对 instrument listing date、交易日历、
日线日期及 60m/15m 时间统一传递 Python `date/datetime`，与 asyncpg 的类型合同一致。
第十次 attempt 继续暴露 Seed 使用旧行情 schema：`bars_daily.adj` 已不存在，主键也不包含复权类型。
现按当前 Bar ORM 将日线、60m、15m 统一写入 `amount` 与 `adj_factor`，冲突键使用各表真实复合主键；
量额由同一 synthetic close/volume 同量纲生成，保留筹码共识所需的 15m 量额事实。
第十一次 attempt 已写入 instruments、calendar、daily 和 60m，随后暴露旧 15m slot 算法会生成
`09:60` 等非法时间。现按两个交易时段生成 09:45..11:30 与 13:15..15:00 共 16 个收盘时间，
明确保证午间断档和末根 15:00。
第十二次 attempt 已生成全部 synthetic 多周期行情和板块成员，随后在 DSA manifest 写入处暴露
SQLAlchemy bind 与 PostgreSQL 紧邻 cast 不兼容。` :manifest::jsonb` 已改为可移植解析的
`CAST(:manifest AS jsonb)`，并确认脚本内无其他同类 bind cast。
第十三次 attempt 到达 DSA version 写入，暴露 definition `ON CONFLICT(strategy_key)` 后仍沿用候选
UUID 的错误；基础 PG 已存在同 key 时该 UUID 并非真实父记录。Seed 现按 strategy key 回读数据库中的
canonical definition ID 后再写 version，使其在空库和已有前置事实两种状态下均幂等。
第十四次 attempt 已完成首个 scenario 的 100 股 core run，DSA readiness 随后按 canonical A 股
universe 过滤掉 `market='cn'` 的 synthetic instruments。600xxx 标的现标记为 `market='SH'`，与
`stock_symbol_sql_filter` 及真实 instrument identity 合同一致，不绕过 readiness。
第十五次 attempt 已完成 core、DSA projection 和 board-facts，chip 写入被 fenced lease 正确拒绝，
因为 Seed 直接传递了未 claim 的 epoch 0。Seed 现通过 `claim_next_job_run` 领取 queued chip job，提交
真实 worker/epoch 后才调用 executor；claim 身份与目标 job 不一致时 fail closed。
第十六次 attempt 的第一遍四场景 Seed 已完整成功，第二遍因正确的 published-run 不可覆盖门禁失败。
Seed 现仅在四个固定场景日期的 core runs 全部存在时进入幂等只读路径，并重新验证 closures；部分
场景存在时不标记完成，避免把半成品误判为幂等成功。

## 数据、验证与风险

无 Migration，不接触 `bz_stock`。用户已单独授权精确删除 8 个历史 `bz_stock_verify_*` 数据库。
本地验证：治理/验证合同 56 passed；全量 PURE_UNIT、架构与 allowlist 通过；前端合同 552 passed；
TypeScript、Lint（0 error，既有 warning 保留）和 production build 通过。提交 SHA、远程清理和正式
full-closure 结果在本任务完成后如实报告；远程通过前状态保持 `implemented_pending_remote_verification`。

- 分支：`dev`
- Commit：提交后以 Git 记录为准
- 回滚：回退本 Change 提交；不得恢复 Shell 凭据传输或宿主机依赖
