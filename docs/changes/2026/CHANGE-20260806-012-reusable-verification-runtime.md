# CHANGE-20260806-012 — 单可复用验证运行时（治理基础设施减法重构）

- **状态**: `implemented_unconfirmed`（候选实现已完成，等待远程隔离重验 + 用户验收后再标记闭环）
- **类型**: 治理/验证基础设施重构（不改变业务 PRD、计算逻辑、稳定部署、业务库结构、ProductReadiness 状态机）
- **日期**: 2026-08-06
- **关联审查**: 用户减法审查（5 处减法）+ 原审查报告 5 P0 + 3 P1 中保留项

## 变更摘要

废止"每 SHA 重建验证镜像 + 每 SHA 独立 compose 栈 + 每 attempt 多容器（backend/frontend/workers/redis）"的旧模式，重构为"1 个长期 `panji-verify-python` + 复用 `trading-postgres` + Redis 条件复用（本轮判定不连 Redis）+ exact SHA + fresh DB/process + attempt cleanup"的治理基础设施。

## 核心改造

### 1. 单可复用验证镜像
- `panji-verify-runtime:current`（不再 `panji-verify-test:<SHA>`）。
- `backend/Dockerfile` verification stage 加 `ARG DEP_HASH` + `LABEL panji.verify.dependency-hash=<hash>`。
- `run_remote_verification.sh` 的 `ensure_verify_runtime`：`expected = SHA256(Dockerfile+pyproject+lockfile)` 与运行容器 image label 两方比较，不一致才 rebuild + recreate；失败 STOP，不自动 rollback。

### 2. 单一长期容器（常驻空闲）
- `docker-compose.verify.yml` 仅 `services.verify-python`：`image: panji-verify-runtime:current`、`container_name: panji-verify-python`、`command: sleep infinity`、固定 project `panji-verify`、不发布 host port、只读 mount 代码 + `/run/panji-verify/:ro`。
- 删除 verify-redis / verify-backend / verify-frontend / verify-worker-* / verify-test 服务。

### 3. 最外层 Single-Flight（唯一锁）
- `run_remote_verification.sh` 持有 `flock /root/.panji-verify/verify.lock` 覆盖整个 remote lifecycle；并发第二 attempt exit 75。
- **删除** `VerifyAttempt` 第二层 fcntl 锁（single-flight 已保证生命周期独占）。

### 4. attempt env 动态注入（小型安全封装）
- 新增 `scripts/verify/verify_exec.py`：读取 `/run/panji-verify/attempt.env`，按第一个 `=` 拆 key/value，`subprocess.run(command, env=env)`。不做进程注册/状态机。修掉 `env $(cat attempt.env) <cmd>` 的 shell word splitting 不可靠问题。
- 容器常驻 env 仅持有稳定变量（APP_ENV/PANJI_SCHEDULER_ENABLED/TZ）；attempt-specific 变量（DATABASE_URL/MIGRATION_DATABASE_URL/JWT_SECRET/TARGET_SHA/ATTEMPT_ID）由 `prepare_verify_environment.py` 生成到 0600 的 `attempt.env`，由 `verify_exec.py` 注入每个 fresh process。

### 5. 各 gate 串行 fresh process
- `verify_attempt.py` 用 `docker exec panji-verify-python verify_exec.py <cmd>` 运行 Migration/PG/Seed/E2E，等退出再下一个。
- **删除** `processes.json` / `setsid` / `PGID` / `attempt_id` 校验 / `kill exact PGID` 进程管理体系。
- 异常/timeout/interrupted 恢复：`docker restart panji-verify-python`（杀容器所有验证进程、不删 container/image/network/PG/Redis/稳定栈、保留 bind mount、重启变干净 env）。

### 6. 容器内身份自检（替代 HTTP 探针）
- `verify_attempt.py` 的 `assert_identity` 做 8 项检查（host HEAD / runtime SHA / Live Mount / mount probe / APP_ENV / DATABASE_URL / `current_database()` 比对 / `!= bz_stock` fail-closed），含 `psycopg` 直连 `current_database()` 比对。不再调用 `/v1/version` HTTP 探针，不启动 verify-backend。

### 7. Redis 决策（一次性审计，固化本 CHANGE）
- **结论**：`full-closure` 验证执行路径（alembic/pytest/seed/e2e）完全不依赖 Redis，仅连 PostgreSQL（`DATABASE_URL` / `MIGRATION_DATABASE_URL`）。`backend/app/core/redis_client.py` 无 FLUSHALL/FLUSHDB/DB0 硬编码/Pub-Sub 串扰，但 verification 路径不调用它。
- **决策**：本轮 verification 不连接 Redis；不引入 `verify-redis` 容器，也不强制复用 `trading-redis`。Cleanup 中无 Redis reset 步骤。
- **未来**：若某 gate 确需 Redis，须单独评估并固化为合同，不得临时动态"智能决策"（删除 `redis_isolation_audit.py` 计划）。

### 8. cleanup 简化（不删常驻栈）
- `cleanup_runner.py`：删除 compose down 分支；`cleanup_attempt` 只 drop `bz_stock_verify_<SHA>` + 标记状态；`PROTECTED_CONTAINERS` 增 `panji-verify-python`，`PROTECTED_PREFIXES` 增 `panji-verify`；`_safe_drop_database` 仍只接受 `bz_stock_verify_<40hex>`。
- `verify_cleanup`（verify_attempt 内）改为状态校验（DB 已删 / attempt.env 已清 / 常驻容器健康），不再要求 compose==0。

### 9. 入口唯一性
- 唯一正式入口 `scripts/ops/panji-verify`。废弃第二入口 `scripts/ops/panji-verify-run` 已删除，从 `rules/PROTECTED_GOVERNANCE_FILES.json` 移除。

## 安全边界（保留的最小必要安全）
- exact 40 位 SHA；clean checkout（HEAD==target + clean）
- `bz_stock_verify_<SHA>`；`current_database() != bz_stock` fail-closed；attempt env 动态注入
- 一个 outer single-flight lock；fresh Python process；evidence
- 失败也 DROP verify DB；禁止 `compose down` / Volume 删除 / `FLUSHALL`；唯一 `scripts/ops/panji-verify` 入口

## 本轮未改变（明确排除）
- 业务 PRD / 计算逻辑 / 稳定部署 / 业务库结构 / ProductReadiness 状态机
- `verification_plan.py` 的 `runtime_profile=after_close` 语义（用户要求本轮不动，以后单独处理死 metadata）

## 验证状态
- **代码门禁**：本地 PURE_UNIT 测试（tools/tests/test_check_governance_rules.py + backend/tests/test_verify_infra_safety.py）通过；bash -n 语法通过；py_compile 通过。
- **未验证**：真实远程隔离重验（需新 SHA → 远程 `panji-verify run --sha <新SHA> --plan full-closure` 全过）。本 CHANGE 状态 `implemented_unconfirmed`，等待远程门禁结果与用户验收后再标记闭环。
- **进行中 attempt**：cfe05e2 后台复验不被本治理改造干扰（Phase0 隔离）。
