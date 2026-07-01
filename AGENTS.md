# AGENTS.md — 项目硬规则

> 适用范围：所有 AI Agent、人类开发者、CI 流水线。
> 生效日期：2026-07-01
> 地位：仅次于 ADR 架构决策，高于 docs 项目规范、公共基础设施、模块文档与文件头注释。

## 规则优先级

当不同层级的规则、注释或文档发生冲突时，按以下优先级从高到低适用：

1. ADR 架构决策（`docs/architecture/ADR-*.md`）
2. AGENTS.md 项目硬规则（本文档）
3. docs 项目规范（如 `docs/安全规范.md`、`docs/测试规范.md`、`docs/事实源清单.md`）
4. 公共基础设施（`backend/tests/conftest.py`、`backend/app/db.py`、`backend/app/config.py` 等）
5. 模块文档字符串
6. 文件头注释

**底线：文件头注释、模块文档字符串、局部 fixture 均不得推翻 ADR 和 AGENTS.md。若发现文件头注释与本文档冲突，以本文档为准，并应删除或修正该注释。**

---

## 硬规则清单

### 规则 1：所有数据库集成测试只使用 PostgreSQL

- **规则正文**：凡被归类为 Integration 的数据库测试，必须连接 PostgreSQL 测试库 `bz_stock_test`，并使用真实 Alembic 迁移产生的结构。禁止在 Integration 测试中使用 SQLite、内存数据库或其他关系型数据库。
- **理由**：生产环境使用 PostgreSQL，测试目标架构必须与生产一致；否则测试通过不能代表生产可用。
- **违反示例**：`backend/tests/test_auth.py:108`、`test_auth_login.py:134`、`test_membership.py:169` 直接创建 `sqlite+aiosqlite:///:memory:` engine。
- **如何验证**：运行 `grep -RInE 'sqlite|aiosqlite' backend/tests`，除 ADR/规则引用或 `test_config_validation.py` 中用于校验拒绝 sqlite 的场景外，不得出现 SQLite 相关字符串；运行 `python tools/check_architecture.py` 进行静态门禁；Integration 测试必须通过 `APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest` 执行。

### 规则 2：禁止 SQLite、aiosqlite、内存数据库

- **规则正文**：项目全生命周期（开发、测试、CI、预发布）禁止使用 SQLite 作为应用或测试数据库；`backend/pyproject.toml` 禁止声明 `aiosqlite`；代码与测试中禁止 `import aiosqlite` 或创建 SQLite engine。
- **理由**：SQLite 方言、事务语义、JSONB 支持、锁行为与 PostgreSQL 差异显著；保留该依赖会误导开发者认为 SQLite 是合法回退方案。
- **违反示例**：`backend/pyproject.toml:52` 声明 `"aiosqlite>=0.20"`；`backend/tests/test_watchlist.py:98` 创建 `sqlite+aiosqlite:///:memory:`。
- **如何验证**：`grep -RInE 'aiosqlite' backend` 无任何命中；`grep -RInE 'sqlite' backend` 仅命中 `config.py` 拒绝校验、`test_config_validation.py` 校验场景及 ADR/文档说明；运行 `python tools/check_architecture.py` 进行静态门禁。

### 规则 3：禁止测试手写 schema

- **规则正文**：测试中禁止通过字符串 `CREATE TABLE`、手写 DDL、`@compiles(JSONB, "sqlite")` 等方式定义生产表结构。所有数据库结构必须通过 Alembic 迁移生成，测试仅使用 ORM 模型操作数据。
- **理由**：手写 schema 会与 Alembic 真实结构漂移，导致测试无法发现迁移缺失、字段类型差异或约束错误。
- **违反示例**：`backend/tests/test_auth.py:47-107` 定义 `_SQLITE_DDL_STATEMENTS` 手写 users / roles / subscriptions 等表；`test_auth_login.py:45-46` 注册 SQLite JSONB 编译回退。
- **如何验证**：`grep -RInE 'CREATE TABLE|@compiles' backend/tests` 无任何命中（除规则文档引用）；运行 `python tools/check_architecture.py` 进行静态门禁。

### 规则 4：测试结构来自 Alembic

- **规则正文**：每次运行 Integration 测试前，必须对测试库执行 `alembic upgrade head`；测试用例只能依赖 Alembic 迁移后的实际表结构，不能依赖静态 SQL seed 或手写建表脚本。
- **理由**：Alembic 是 schema 唯一事实源；测试结构来自 Alembic 才能保证迁移与 ORM 映射一致。
- **违反示例**：测试模块本地执行 `_SQLITE_DDL_STATEMENTS` 创建表，再插入数据，完全绕过 Alembic。
- **如何验证**：`backend/tests/conftest.py` 的 `init_test_db` fixture 必须在 session 范围自动调用 `alembic upgrade head`；运行 `python tools/check_architecture.py` 检查测试目录无手写 schema。

### 规则 5：禁止模块 fixture 覆盖公共 db_session

- **规则正文**：`backend/tests/conftest.py` 提供的 `db_session` fixture 是项目唯一标准数据库 session。任何测试模块不得定义同名的 `db_session` fixture 进行覆盖，也不得在模块内创建独立 engine/session。
- **理由**：局部覆盖会破坏事务隔离、回滚机制与数据清理策略，导致测试间状态泄漏。
- **违反示例**：`backend/tests/test_auth.py:98`、`test_auth_login.py:131`、`test_membership.py:162` 均定义模块级 `@pytest_asyncio.fixture db_session`。
- **如何验证**：AST 扫描或 `grep -RInE '@pytest_asyncio\.fixture\s*\n?.*\bdb_session\b|def db_session' backend/tests` 仅命中 `conftest.py`；运行 `python tools/check_architecture.py` 增加同名 fixture 覆盖检查。

### 规则 6：发现旧 SQLite 测试必须迁移，不能复制

- **规则正文**：任何基于 SQLite 或手写 schema 的旧测试，必须迁移到 PostgreSQL + 公共 fixture + ORM factory 方案；禁止保留旧 SQLite 测试副本，禁止新增仅 SQLite 可运行的测试分支。
- **理由**：复制旧测试会维护两套口径，继续让 SQLite 逻辑事实存在，违背 PostgreSQL Only 决策。
- **违反示例**：迁移时把 `test_auth.py` 复制一份为 `test_auth_sqlite_legacy.py`。
- **如何验证**：`backend/tests` 下不存在同时包含 `sqlite` 字符串与 `create_async_engine` 的文件；所有 Integration 测试均能在 `APP_ENV=test` + PostgreSQL 环境下运行。

### 规则 7：普通用户角色为 member，不是 user

- **规则正文**：系统中代表普通用户的角色名必须是 `member`。迁移、seed、测试、API、前端不得使用 `user` 作为角色名。
- **理由**：`advice.md` 明确要求统一角色名；`user` 与 `member` 并存会导致 RBAC 事实源不统一，权限校验出现漏洞。
- **违反示例**：`backend/tests/test_me_access.py:125`、`test_watchlist_limit.py:331`、`test_invite_code_concurrency.py:64` 调用 `_ensure_role(db, "user")`；`backend/app/models/user.py:72` 注释仍写 "admin/user/strategy_author"。
- **如何验证**：`grep -RInE 'Role\(.*name\s*=\s*["\x27]user["\x27]|_ensure_role\(.*["\x27]user["\x27]' backend` 无任何命中；运行 `python tools/check_architecture.py` 增加角色字符串门禁。

### 规则 8：管理员无套餐、无 subscription

- **规则正文**：`admin` 角色不绑定任何套餐，不创建 subscription 记录，`plan_code=None`，限额无限制。禁止为管理员硬编码历史管理员套餐代码常量、`research_50` 或任何套餐信息。
- **理由**：admin 的权限应来自角色本身，而非套餐契约；给 admin 绑定 subscription 会引入事实源冲突，导致限额校验与套餐到期逻辑混乱。
- **违反示例**（历史）：`backend/app/constants/plan_codes.py` 曾定义历史管理员套餐代码常量并将其设为 `"research_50"`；`backend/app/api/me.py` 曾对 admin 返回该常量；相关测试曾断言管理员返回 `research_50`。
- **如何验证**：删除历史管理员套餐代码常量；使用 grep 检查 backend 目录不再包含该常量字符串（零命中）；管理员相关测试断言 `plan_code is None`；运行 `python tools/check_architecture.py` 增加该常量门禁。

### 规则 9：一个业务概念只能有一个事实源

- **规则正文**：同一个业务规则、计算公式、状态定义、枚举值、套餐数值、服务名、角色名，在项目中只能存在一个权威实现。禁止在多个文件重复定义同义常量，禁止前端复制后端业务计算，禁止文档手写与代码不一致的值。
- **理由**：多处定义会导致口径漂移，修改时遗漏，最终出现文档/代码/测试互相矛盾。
- **违反示例**：套餐 `monitor_limit=20/50` 在 `backend/alembic/versions/048_plans_table.py`、`backend/app/constants/plan_codes.py`、`backend/app/api/me.py`、`frontend/src/api/endpoints.ts` 等多处重复；worker 服务名在文档中手写而与 `docker-compose.prod.yml` 不一致。
- **如何验证**：各业务领域事实源见 `docs/事实源清单.md`；plans 数值重复检测本轮通过 `python tools/check_architecture.py` 实现；算法常量重复检测为计划项，当前不强制；`tools/update_docs.py` 尚未实现，当前为可选，不强制在每次文档修改后运行。

---

## 附则

- **解释权限**：本文档由项目治理任务 owner 解释。对规则适用有争议时，按上述优先级裁决；若仍无法裁决，由主管拍板。
- **修订流程**：新增、删除或修改硬规则，必须同步更新 ADR 或对应 docs 规范。`tools/update_docs.py` 尚未实现，当前不强制运行；受影响的自动生成文档后续由独立任务统一处理。
- **冲突解决**：任何文件头注释、局部 docstring、模块级说明若与 ADR 或 AGENTS.md 冲突，视为无效，应修正或删除。
