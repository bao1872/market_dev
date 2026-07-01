# ADR-001: PostgreSQL Only for Integration Tests

| 项 | 内容 |
|---|---|
| 编号 | ADR-001 |
| 标题 | PostgreSQL Only for Integration Tests |
| 状态 | Accepted |
| 提出日期 | 2026-07-01 |
| 决策人 | 项目治理任务 owner |

---

## 背景

项目生产环境使用 PostgreSQL，且 `backend/app/config.py` 已对所有环境启动 `DATABASE_URL` 硬校验：任何包含 `sqlite` 字样的 URL 都会被拒绝启动（`InvalidDatabaseURLError`）。然而，当前测试体系存在与生产架构冲突的以下问题：

1. **测试数据库与生产数据库不一致**：`backend/tests/test_auth.py`、`test_auth_login.py`、`test_membership.py`、`test_instruments.py`、`test_watchlist.py` 等文件仍使用 `sqlite+aiosqlite:///:memory:` 运行 Integration 测试。
2. **绕过 `config.py` 校验**：测试模块直接调用 `sqlalchemy.ext.asyncio.create_async_engine("sqlite+aiosqlite:///:memory:")`，完全不经过 `config.py` 和 `app.db.py` 的 engine 创建路径。
3. **手写 schema 与 Alembic 漂移**：上述测试通过 `_SQLITE_DDL_STATEMENTS` 手写 `CREATE TABLE`，并注册 `@compiles(JSONB, "sqlite")` 编译回退，导致测试 schema 与 Alembic 迁移的真实结构不一致。
4. **模块级 fixture 覆盖公共 `db_session`**：`backend/tests/conftest.py` 已提供基于 PostgreSQL 的 `db_session` fixture，但多个模块仍自定义同名 fixture，破坏事务隔离。
5. **`aiosqlite` 依赖传递错误信号**：`backend/pyproject.toml` 仍在 `dev` 依赖中声明 `aiosqlite>=0.20`，使开发者误以为 SQLite 是合法测试方案。
6. **测试结果含义不统一**：同一 pytest 运行结果中，部分测试在 SQLite 通过，部分在 PostgreSQL 通过，无法说明系统在真实目标数据库上的可用性。

这些问题与 `docs/安全规范.md` 已规定的 PostgreSQL 方案、`advice.md` 明确禁止 SQLite Integration 测试的要求直接冲突。

---

## 决策

我们决定：**所有 Integration 测试必须使用 PostgreSQL + Alembic；禁止 SQLite / aiosqlite / 内存数据库；禁止手写 schema。**

具体决策内容：

1. Integration 测试唯一目标数据库为 PostgreSQL 测试库 `bz_stock_test`。
2. 测试结构必须来自 Alembic 迁移，运行测试前必须执行 `alembic upgrade head`。
3. `backend/tests/conftest.py` 是唯一数据库测试基础设施，提供 `db_session`、`client`、`user_factory`、`role_factory`、`subscription_factory`、`invite_code_factory`、`instrument_factory` 等标准 fixtures。
4. 任何测试模块不得覆盖 `db_session`，不得手写 `CREATE TABLE`，不得 `import aiosqlite`，不得创建 SQLite engine。
5. `backend/pyproject.toml` 必须移除 `aiosqlite` 依赖。
6. `APP_ENV=test` 且 `TEST_DATABASE_URL` 必须指向 PostgreSQL 且库名以 `_test` 结尾。

---

## 影响

### 对测试代码的影响

- 必须迁移以下（至少）SQLite 测试文件到 PostgreSQL：
  - `backend/tests/test_auth.py`
  - `backend/tests/test_auth_login.py`
  - `backend/tests/test_membership.py`
  - `backend/tests/test_instruments.py`
  - `backend/tests/test_instruments_batch.py`
  - `backend/tests/test_universe.py`
  - `backend/tests/test_watchlist.py`
  - `backend/tests/test_pinyin_search.py`
  - `backend/tests/test_job_runs_and_monitor_status.py`
  - `backend/tests/test_strategy_batch.py`
  - `backend/tests/test_selector_query_integration.py`
- 上述文件需要删除：模块级 `db_session` fixture、`_SQLITE_DDL_STATEMENTS`、SQLite JSONB compiler、手写 `CREATE TABLE`、`aiosqlite skip` 逻辑、SQLite 文件头说明。
- `backend/tests/conftest.py` 需要补齐 `role_factory`、`subscription_factory`、`invite_code_factory`、`instrument_factory` 等工厂 fixture，并确保事务隔离使用 `join_transaction_mode=create_savepoint`（或等效 nested transaction 方案）。

### 对依赖的影响

- 从 `backend/pyproject.toml` 删除 `"aiosqlite>=0.20"`。
- 重新锁定依赖并更新虚拟环境。

### 对 CI 的影响

- CI 必须提供 PostgreSQL 服务（或指向已存在的 `bz_stock_test`）。
- CI 中 Integration 测试必须以 `APP_ENV=test TEST_DATABASE_URL=postgresql://...` 运行。
- 需要新增 `backend/tests/test_architecture_rules.py` 作为门禁，防止 SQLite 相关模式回潮。

### 对开发流程的影响

- 本地运行 Integration 测试前必须确保 PostgreSQL 可访问并存在 `bz_stock_test` 库。
- 新写 Integration 测试必须使用 `conftest.py` 提供的 fixtures，禁止本地造 engine。

---

## 迁移策略

详细迁移步骤参考 `/root/web_dev/.trae/specs/project-governance-and-test-ssot/migration-plan.md`。核心步骤如下：

1. **规则文档先行**：创建 `AGENTS.md`、本文档、`docs/测试规范.md`、`docs/事实源清单.md`，明确优先级与硬规则。
2. **重构 `backend/tests/conftest.py`**：补齐所需 factory fixtures，确保 `APP_ENV=test`、`_test` 库名校验、`alembic upgrade head`、事务隔离。
3. **逐个迁移 SQLite 测试文件**：删除模块级 `db_session`、手写 schema、SQLite engine，改用公共 fixtures。
4. **移除 `aiosqlite`**：从 `pyproject.toml` 删除依赖，验证 SQLite 引用清零。
5. **统一角色与套餐事实源**：`user` → `member`，删除历史管理员套餐代码常量，套餐数值只来自 `plans` 表。
6. **新增架构门禁 `test_architecture_rules.py`**：AST + 字符串扫描禁止 SQLite / aiosqlite / 手写 schema / 局部 `db_session` / `Role(name="user")` / 历史管理员套餐代码常量。
7. **CI 门禁**：增加 architecture rules、ruff、type check、Alembic upgrade/downgrade/upgrade、PostgreSQL integration tests、frontend tsc/lint/build。
8. **全量验证**：运行 PostgreSQL 专属关键测试 + 全量 pytest，输出最终治理报告。

---

## 替代方案

### 替代方案 A：保留 SQLite 作为 Integration 测试快速回退

- **方案描述**：继续允许部分 Integration 测试使用 SQLite，但要求通过 `pytest.mark` 明确标注。
- **拒绝原因**：
  - 与生产 PostgreSQL 架构不一致，测试结果无法代表生产可用性。
  - 直接绕过 `backend/app/config.py` 的 URL 校验，破坏配置一致性。
  - 手写 SQLite schema 与 Alembic 迁移漂移，无法发现真实 schema 问题。
  - 继续维护两套测试基础设施，违背“一个业务概念一个事实源”原则。

### 替代方案 B：保留 aiosqlite 但仅用于 Unit 测试

- **方案描述**：不删除 `aiosqlite`，但限制其仅可用于不连接真实 schema 的 Unit 测试。
- **拒绝原因**：
  - 保留依赖会传递“SQLite 仍被项目接受”的错误信号。
  - 任何 SQLite engine 都可能被误用于 Integration 测试，难以持续审查。
  - 项目当前没有任何必须使用 SQLite 的 Unit 测试场景（Unit 测试应使用 Mock / 普通对象）。

---

## 相关文档

- `AGENTS.md` — 项目硬规则
- `docs/测试规范.md` — Unit / Integration / E2E 分类与 fixture 使用规范
- `docs/事实源清单.md` — roles、plans、schema、algorithm、worker 等领域事实源
- `docs/安全规范.md` — 数据库账号权限与测试库部署规范
- `/root/web_dev/.trae/specs/project-governance-and-test-ssot/migration-plan.md` — 详细迁移计划
- `backend/tests/conftest.py` — 唯一数据库测试基础设施
- `backend/tests/test_architecture_rules.py` — 架构规则门禁（待创建）

---

## 备注

- 本文档为 ADR 架构决策，优先级高于 `AGENTS.md`、docs 规范、模块文档和文件头注释。
- 后续若需重新评估本决策，必须发起新的 ADR 流程，并同步更新 `AGENTS.md` 与 `docs/测试规范.md`。
