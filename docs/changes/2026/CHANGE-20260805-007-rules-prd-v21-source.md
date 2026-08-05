# CHANGE-20260805-007 — 规则纠偏 + 建立 V2.1 正式 PRD 真源（Phase 0 收口）

日期：2026-08-05
类型：governance+docs+contract
领域：治理规则 / 测试合同 / V2.1 产品闭环真源 / 验证基础设施合同
关联 PRD：`docs/prd/31-after-close-product-closure-v2.1.md`（新建，V2.1 正式真源）、`prd/30-after-close.md`、`prd/70-review.md`、`prd/75-auction-analysis.md`、`prd/80-system-runtime.md`
关联 Rules：`40-testing-quality.md`（TQ-100 三模式）、`50-git-development-flow.md`（Plan-scoped authorization）、`80-deployment-data-safety.md`（DS-110/111/112）、`81-remote-deployment-only.md`（4.1 验证栈）、`90-deprecated-forbidden.md`（测试库例外）、`AGENTS.md` §8（远程验证库 + 任务范围授权）
关联 Tools：`tools/check_governance_rules.py`、`backend/tests/conftest.py`

## 背景（问题）

此前把 `ce0a2ec`（PRD Alignment Pass）直接交付验收会失败，根因有三：

1. **规则互相矛盾**：要求真实 PG 集成 / Migration / E2E，又永久禁止任何临时验证数据库（`AGENTS.md` §8 与 `rules/40` TQ-100 二模式），导致任何 PG 验证必然被规则本身阻断。
2. **授权粒度错误**：已批准的开发闭环被拆成每条命令重新确认（`rules/50` 无 plan-scoped 授权）。
3. **PRD 真源不稳定**：总需求临时引用 `ref/instruction.md`，但 `ref/` 已被规则定为仅人工参考、非正式合同。

当前代码虽修复主要 P0，仍明确存在：granular restart 大部分返回 501、PG E2E 未执行、完整前端合同未验证、Migration 085/086 未应用。

## 修改内容

### 1. AGENTS.md §8

- 删除"创建或复用任何独立/临时测试数据库"绝对禁令；改为"唯一例外是 DS-110 远程验证库 `bz_stock_verify_<sha>`，本地/CI 仍永久禁止"。
- 新增「允许的远程临时验证数据库」子节（命名、创建入口、连接校验、用途、验收后删除）。
- 新增「任务范围授权」子节：明确计划级一次授权覆盖修改代码/文档、提交推送、创建验证库、应用 Migration、启动验证栈、写入验证数据、执行验证、验收后清理；同闭环内不逐条重问。

### 2. rules/40-testing-quality.md

- TQ-100 由二模式改为三模式：`PURE_UNIT_TEST=1` / `PANJI_SHARED_DEV_DB_TEST=1` / `PANJI_REMOTE_VERIFY_DB_TEST=1`。
- 本地/CI 仍永久禁止临时库；远程验证模式 fail-closed（`APP_ENV=verification`、`current_database()` 不得为 `bz_stock`、允许 DDL/Alembic 仅对验证库）。

### 3. rules/50-git-development-flow.md

- 新增「Plan-scoped authorization」：批准明确计划即一次闭环授权，不得因下一阶段/重开上下文/测试失败重新逐条询问；仅 5 类真实阻塞重问。

### 4. rules/80-deployment-data-safety.md

- 新增 DS-110 远程临时验证数据库（命名 `bz_stock_verify_<7-40位SHA>`、创建入口、连接校验、禁止连 `bz_stock`、Migration 规则、验收后删除、无备份要求）。
- 新增 DS-111 远程验证栈（独立 Compose project、端口仅 127.0.0.1、独立 env、Scheduler 关闭、必要 Worker、运行 SHA 可查）。
- 新增 DS-112 验证数据合同（不完整复制 `bz_stock`、seed CLI 只读业务库、四类场景、可重跑）。

### 5. rules/81-remote-deployment-only.md

- 新增 §4.1：验证栈仍属远程部署；验证前端必须在服务器构建、本地 Vite 不作为验收证据；验证通过后才允许同 SHA 部署正式栈。

### 6. rules/90-deprecated-forbidden.md

- 测试库清理边界改为"本地/CI 永久禁止"，新增"远程验证库例外"（DS-110 唯一允许的临时库）。

### 7. tools/check_governance_rules.py

- 测试库 token 扫描豁免 DS-110 远程验证库 token（`bz_stock_verify_`、`PANJI_REMOTE_VERIFY_DB_TEST`、DS-110 等）。
- 新增断言：活跃文档不得再出现"所有临时数据库永久禁止"绝对表述（除非引用 DS-110 例外）。
- 新增断言：活跃 `docs/prd`、`docs/runbooks`、`docs/maps` 不得把 `ref/instruction.md` 当正式真源引用（除非标记为参考/非正式）。

### 8. backend/tests/conftest.py

- 引入第三种模式 `_REMOTE_VERIFY_DB`，顶层 fail-closed：非 PURE/SHARED/VERIFY 三者之一即报错；本地/CI 启用 VERIFY 模式且 `APP_ENV != verification` 即报错。
- 新增 verify 分支校验：`DATABASE_URL` 库名必须匹配 `bz_stock_verify_<7-40位SHA>`，`TestAsyncSessionLocal` 正常建立（允许 DDL）。

### 9. docs/prd/31-after-close-product-closure-v2.1.md（新建，V2.1 正式真源）

吸收 `ref/instruction.md` 已确认需求，按用户要求结构组织：产品图、九节点 readiness 定义、closure 定义（fully_ready 必须 composite auction + 真实 ready 增强）、总体目标与正式决策、P1-3 readiness 完整性（eligible/matched/coverage/algorithm_version/parameter_hash/source_run/event lifecycle）、Granular restart 正式枚举（10 个 boundary 全定义）、运行对象与状态机、publication/lineage 不变量、API/前端逐页合同、测试与验收矩阵（四类场景硬断言）、完成定义（诚实标记 `code_ready=false`）。

### 10. 现有 PRD / Runbook 交叉引用与流程

- `30-after-close.md` / `70-review.md` / `75-auction-analysis.md` / `80-system-runtime.md`：加 V2.1 交叉引用（指向 31 PRD / DS-110），不复制整套定义。
- `docs/runbooks/development-deployment.md`：增加 primary / remote verification 两种部署子流程；V2.1 段改写为 PG 验证只在 `bz_stock_verify_<sha>` 执行（不再"先在真实业务库跑 PG 测试"）。
- `docs/runbooks/v21-manual-acceptance.md`（新建）：SSH Tunnel、登录、测试交易日/股票、四类场景、每页预期、问题记录格式。

## 门禁结果

- `tools/check_governance_rules.py`：PASS（新增 DS-110 例外 + ref 真源断言通过）。
- `tools/check_docs_consistency.py`：PASS（14 PRD / 12 maps / 114 链接；补 Acceptance Matrix `**基线**` 字段后为全通过）。
- `backend/tests/conftest.py`：`py_compile` 通过（本地环境无依赖，未跑 pytest；模式逻辑与现有 PURE/SHARED 路径等价）。
- 未触碰业务代码；本 Change 属规则+文档+测试基础设施，不引入运行时行为变化。

## 状态（诚实标记）

- `code_ready=false`（不变；Phase 0 只收口规则与 PRD，未实现剩余 backend/frontend/Migration/PG）。
- `pg_tested=false`、`deployed=false`、`data_closed=false`、`browser_verified=false`。
- 下一步：Phase 1 Backend 功能闭合（granular restart 全落地、P1-3 readiness、错误 DTO）→ Phase 2 Frontend → Phase 3 验证基础设施脚本 → Phase 4 Migration/PG → Phase 5 验证栈 → Phase 6 自动场景 → Phase 7 手动验收。
- 本 Change 不改动任何既有未完成状态；`ce0a2ec` 的 Alignment Pass 结论（`partial`/`false`）继续有效。
