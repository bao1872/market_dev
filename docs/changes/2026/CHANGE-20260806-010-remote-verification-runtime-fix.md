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

## 数据、验证与风险

无 Migration，不接触 `bz_stock`。用户已单独授权精确删除 8 个历史 `bz_stock_verify_*` 数据库。
本地验证：治理/验证合同 56 passed；全量 PURE_UNIT、架构与 allowlist 通过；前端合同 552 passed；
TypeScript、Lint（0 error，既有 warning 保留）和 production build 通过。提交 SHA、远程清理和正式
full-closure 结果在本任务完成后如实报告；远程通过前状态保持 `implemented_pending_remote_verification`。

- 分支：`dev`
- Commit：提交后以 Git 记录为准
- 回滚：回退本 Change 提交；不得恢复 Shell 凭据传输或宿主机依赖
