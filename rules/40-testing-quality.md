# 40 测试与质量

> 来源：AGENTS.md §五、§七.20、§八、§六.6、§六.8、§七.8（测试部分）

## CHANGE 规则

普通 Bug 与局部代码修改默认由 Git 历史记录。只有重要业务规则、契约、主要实现结构、运行方式或重大数据修复发生变化时，才在 `docs/changes/YYYY/` 新增一个 Change 并更新 `docs/changes/INDEX.md`。

CHANGE 必填字段：

- 变更编号；
- 任务名称；
- 需求出处；
- 修改前/后行为；
- 影响模块；
- 修改文件；
- 文档更新；
- 测试证据；
- Git 分支；
- Git Commit；
- 数据库迁移；
- 配置变化；
- 风险；
- 遗留问题。

禁止为同一闭环拆出多个重复 Change，也禁止新建报告或治理目录。

## 文档目录与 CI 门禁

`tools/check_docs_consistency.py` 必须通过。

规则包括：

- PRD、Maps、Changes 和 Runbooks 的本地链接有效；
- `docs/current/` 保持 legacy 只读，不作为变更同步门禁；
- 本地 Markdown 链接有效；
- 无"待填写"占位符；
- `feishu_webhook` 不得回退为当前方案；
- open-decisions 不得把 Webhook vs Platform App 写回 OPEN；
- CHANGE 引用必须可达；
- ref/ 隔离文本扫描。

CI 应在文档职责、链接、禁止路径或已确认事实出现矛盾时失败；不得仅因普通代码 SHA 变化强制制造无意义文档变更。

## 质量门禁

```
Ruff    新增/修改 Python 文件零错误；历史债务由 tools/quality_baselines/ruff.json 管控
Mypy    新增 backend/app Python 生产文件零错误；历史债务由 tools/quality_baselines/mypy.json 管控
Docs    python tools/check_docs_consistency.py
Arch    python tools/check_architecture.py
Allow   python tools/check_test_allowlist.py
Gov     python tools/check_governance_rules.py
```

禁止通过全局 ignore、批量 noqa、扩大 exclude、批量 `type: ignore` 或关闭检查掩盖新增问题。

前端：

- `tsc --noEmit`；
- `npm run lint`；
- `npm run build`；
- `npm run test:contract`；
- `npm run test:e2e`。

## 报告与对话输出（2026-07-29 收口）

> 硬规则：禁止新建未经用户确认的报告/治理目录（如 `reports/`）。
> 完整执行过程只在对话输出，不写入仓库；普通 Bug 由 Git 历史记录，只有重要行为变化才写一个 CHANGE。
> `docs/current/` 标记为 legacy 只读，本轮起不得新增或修改其中文件，后续另行迁移；
> CI 与规则中不再要求"代码变更必须同步 docs/current"。

历史 `reports/` 目录已删除（见 CHANGE-20260729-004 配套提交），`tools/check_reports.py` 与 CI `Reports System` job 一并移除。

## 测试纪律

- 删除测试以适配错误实现：禁止；
- 修改 API 不检查前端调用：禁止；
- 修改数据模型不检查 migration：禁止；
- 修改 Worker 不检查幂等、心跳、重试：禁止；
- 把 Mock E2E 说成真实生产 E2E：禁止；
- 把 OPEN 问题写成最终结论：禁止；
- 把临时实验写成永久规则：禁止。

## ref/ 隔离测试

`ref/` 目录下所有文件仅供人工阅读参考，**禁止作为运行依赖**。

## 2026-08-01 收口：全局安装、baseline膨胀、局部Canary、部署黑名单（CHANGE-20260801-001 配套）

### TQ-80 禁止用户级/全局依赖安装

- **禁止** `pip install <package>`、`npm install <package>`、`brew install <package>`、`conda install <package>` 四种用户级依赖安装，除非：
  1. PRD 明确新增了依赖并在 `pyproject.toml` / `package.json` 声明；
  2. 且本轮任务必须在本地真实运行该依赖（非 CI 替代）。
- **禁止**绕过：`pip install --user` / `npm install -g` / 临时安装后不写入 package.json/pyproject.toml。
- 依赖缺失 → 优先使用 py_compile / ast / 语法检查（后端）或交 CI 跑全量测试，不为了"本地跑测试"而装全局依赖。

### TQ-81 禁止 baseline 膨胀

- **Ruff / Mypy baseline**（`tools/quality_baselines/ruff.json` 与 `mypy.json`）：
  1. 每轮任务 baseline 文件 **净增大** 不得超过 3 行；
  2. 不得批量 `# noqa`、批量 `type: ignore`、扩大 `exclude` 目录；
  3. CI 中 **Ruff 新增错误为 0** 是门禁；任何 "修改规则基线以适配错误代码" 行为必须在 CHANGE 中单独解释根因与修复计划。
- **Playwright baseline 截图**：
  1. 单轮变更 baseline 图片张数新增 ≤ 3；
  2. 不得删除旧 baseline 图以"适配"视觉回归失败；必须解释视觉差异确实来自 UI 合法变化。

### TQ-82 禁止用 CI 或服务器测试掩盖本地失败

- **本地测试失败时禁止部署**：修改范围内的单元测试或静态检查未通过，不得进入部署步骤。
- **本地无法运行测试时如实报告**：若本地环境确实无法运行某类测试（如缺依赖、缺 DB），必须明确说明，不得用 "CI 会跑" 或 "服务器部署后验证" 掩盖未验证状态。
- 禁止把未运行的测试说成已通过；禁止把服务器 smoke 当成完整回归。

### TQ-83 禁止局部 Canary 冒充整体成功

- 整体功能（如竞价分析、review 五阶段、after_close 七步）不得把单组件/局部 Canary 通过写成整体完成：
  - 例子 1：capture 服务启动 ≠ 竞价分析整体闭环（09:25真值/scan/aggregate/publish 未过）；
  - 例子 2：stock_core publishing ≠ after_close watchlist_ready（review 阶段未跑）；
  - 例子 3：1 个行业 review scope 成功 ≠ 全市场 ready。
- 规则：整体 status = `min(各组件 status)`，任何一个未通过 → 整体不是"成功"；
  - 正确写法：`partial_closed: quote_capture_only`、`review_in_progress: stock_core_ok_but_review_pointer_not_published`；
  - 健康接口不得返回 `overall: "success"` 给以上部分成功情形。

### TQ-84 部署黑名单方式永久禁止

见 `rules/80-deployment-data-safety.md` §部署永久黑名单。

- 禁止 `scp` 单文件；禁止 `docker cp`；禁止 SSH 进容器 vi/sed 修改源码；禁止临时 `python -c` 执行业务脚本。
- 所有部署 / review 恢复 / after_close 重跑 **必须** 走正式 CLI / orchestrator API / admin 后端 API。

- 生产代码、测试、工具、构建脚本在运行时不得 `import` / `open` / `read` / `glob` `ref/` 目录下任何文件；
- SMC Pine parity 测试只读取 `backend/tests/fixtures/smc_pine/*.csv`；
- 禁止从 DB 重新取 bar 或依赖 `ref/` 导出脚本；
- `AGENTS.md` / `docs/maps/*.md` 不得把 `ref/` 文件称为"真源"、"合同"、"fixture 生成器"或"运行依赖"；应称为"参考源（人工阅读）"；
- 算法真源必须是生产代码（如 `smc_pine_core`、`node_cluster_engine`、`indicator_contract`、`indicator_semantics`）。

## Migration 测试纪律

修改 migration 必须有 upgrade / downgrade / upgrade 验证。详见 `80-deployment-data-safety.md`。

## 测试数据库与测试模式（2026-08-06 收敛为本地纯单元 + 远程验证库）

> 引用修订（2026-08-02，CHANGE-20260802-003）：原文的悬空 Change 引用已移除；
> 实际来源是 CHANGE-20260728-004（禁用临时测试库）与 CHANGE-20260728-008（删除持久测试库）。
> 2026-08-05 引入 `PANJI_REMOTE_VERIFY_DB_TEST=1`；2026-08-06 进一步废止共享业务库 pytest，
> 收敛为“本地/CI 纯单元，真实 PG 只在 DS-110 远程验证库运行”。

> 来源：AGENTS.md §8 基础安全边界

### 禁止范围（本地 / CI / Docker 容器）

- **本地、CI、Docker 容器**：禁止创建或复用任何独立/临时测试数据库；禁止启动测试专用数据库容器（含 CI service container 与 `docker compose` 临时 Postgres）；禁止创建测试专用 Volume 或持久测试引擎；禁止引入 `TEST_DATABASE_URL` 一类独立测试库连接变量；禁止使用 `bz_stock_test` 一类独立测试库名；禁止在 CI 中为数据库测试挂载 `services: postgres`。
- **唯一允许的临时数据库**是 `rules/80-deployment-data-safety.md` DS-110 定义的远程验证数据库 `bz_stock_verify_<sha>`，且仅在 `panji-prod` 已有 PostgreSQL 容器内、由正式验证脚本创建。

### 测试模式规则

- 两种模式见 TQ-100。非两种模式之一时，`conftest` 加载即失败。
- 本地和 CI 只运行纯单元、静态、合同及前端测试，不连接 PostgreSQL。
- 远程验证模式必须在 `panji-prod` 运行，连接 `bz_stock_verify_<sha>`，`APP_ENV=verification`，且 `current_database()` 不得为 `bz_stock`。
- 已删除共享业务库 pytest 兼容入口；不得重新引入连接 `bz_stock` 的测试模式。

### 新增测试规则

- 新增测试优先写成纯单元测试（不连接数据库）。
- 必须连接数据库的集成测试必须使用 `db_session` fixture，并且只能在 `PANJI_REMOTE_VERIFY_DB_TEST=1` 远程验证库模式下运行。
- 不得在本地 Mac 创建持久测试库以运行集成测试。

### TQ-100 唯一测试模式合同

> 本条是测试运行环境的**唯一权威**。任何文档、脚本、CI 配置、注释与本条冲突的，以本条为准。

**两种允许的测试模式**：

| 模式 | 触发变量 | 数据库 | 适用范围 |
|---|---|---|---|
| 纯单元 | `PURE_UNIT_TEST=1` | 不连接任何数据库、不联网 | 默认模式，绝大多数测试 |
| 远程临时验证库 | `PANJI_REMOTE_VERIFY_DB_TEST=1` | `panji-prod` 上 `bz_stock_verify_<sha>` | Migration、PG 集成、完整 Synthetic E2E |

两个变量均未设置时，`backend/tests/conftest.py` 必须在加载阶段 fail-closed 直接失败，不得回退到任何默认数据库。

**远程验证模式的强制前置条件**（fail-closed，任一不满足立即中止）：

- 只能在远程 `panji-prod` 运行，禁止在本地 / CI 启用；
- `APP_ENV=verification`；
- `DATABASE_URL` 指向 `bz_stock_verify_<7到40位SHA>`（DS-110 命名规则）；
- 连接建立后必须执行 `SELECT current_database()`，若返回 `bz_stock` 立即中止（禁止触碰业务库）；
- 允许 DDL 与 Alembic，但只针对验证数据库；
- 允许完整 PG 测试（Migration、PG 集成、Synthetic E2E）；
- 禁止创建测试 PostgreSQL 容器与测试 Volume。

**本地 / CI 永久禁止**（与第一种模式无关，仅约束本地/CI）：

- 禁止在本地 / CI 创建或复用独立测试数据库、临时测试数据库；
- 禁止启动测试专用数据库容器（含 CI service container 与 `docker compose` 临时 Postgres）；
- 禁止创建测试专用 Volume 或持久测试引擎；
- 禁止引入 `TEST_DATABASE_URL` 一类独立测试库连接变量；
- 禁止使用 `bz_stock_test` 一类独立测试库名；
- 禁止在 CI 中为数据库测试挂载 `services: postgres`。

**文档一致性要求**：`rules/`、`docs/maps/`、`docs/prd/`、`docs/runbooks/`、`.github/workflows/` 的活跃内容中，不得出现描述上述本地/CI 禁止路径为**当前可用方案**的表述；但**不得再出现**"所有临时数据库永久禁止"的绝对表述（远程验证库为允许例外，见 DS-110）。历史 `docs/changes/` 记录与本条中明确标注为"禁止"的语句不受此限。该约束由 `tools/check_governance_rules.py` 自动断言。

## 2026-08-02 收口：测试合同（开发与部署治理）

> 来源：用户本轮治理指令（开发阶段收口）

### TQ-90 测试合同（开发闭环内）

盘迹当前只关心**开发阶段**。测试与部署的边界如下，禁止定义或保留其他阶段的工作流程：

1. **默认只运行修改范围单元测试和静态检查**：本地验证聚焦本次改动相关的测试 + Ruff/Mypy/TSC/Lint/Arch/Allow/Gov。
2. **不默认运行全仓测试**：不把全仓测试作为普通开发的前置要求。
3. **CI 不是普通开发部署的前置条件**：`ci.yml` 不得因 push dev 自动阻止服务器开发部署；CI 失败不阻断开发者按 Live Mount 合同部署 dev SHA。
4. **CI 可保留为手工诊断工具**：CI 用于按需诊断（如分类测试、全量回归、集成测试），但不进入默认开发闭环，不作为部署门禁。
5. **本地测试失败时禁止部署**：见 TQ-82。
6. **本地无法运行测试时如实报告**：见 TQ-82，不得用 CI 或服务器测试掩盖。

### TQ-91 禁止的无关流程（当前不定义）

以下流程当前与盘迹开发阶段无关，**有效治理文档中不得描述、保留或改名为 deferred 后继续保留**：

- Release Gate（`.github/workflows/release.yml` 的 `Release Gate` job）；
- GHCR / Registry / 镜像仓库推送；
- Release Manifest / immutable image release / formal release candidate；
- 服务器只 pull 不 build；
- Fast CI 作为部署强制门禁；
- 多阶段 delivery phase / 未来正式发布流程。

> 上述工作流文件已删除，禁止恢复。任何当前部署行为以
> `rules/80-deployment-data-safety.md`、`docs/maps/80-system-runtime.md` 和
> `docs/runbooks/development-deployment.md` 为唯一权威。

### TQ-92 测试分类（仅用于 CI 诊断，不影响部署）

后端测试按执行环境依赖分为三类，由 `backend/tests/conftest.py` 的 `pytest_collection_modifyitems` 统一判定并输出 `[test-classification]` 摘要行，仅供 CI 诊断与对账：

| 类别 | marker | 含义 | 运行位置 |
|---|---|---|---|
| PG 集成 | `postgres` | 需要真实 PostgreSQL | 仅远程验证库（`PANJI_REMOTE_VERIFY_DB_TEST=1`） |
| 外部数据 | `external_data` | 依赖外部数据源 | CI（失败不阻断开发部署） |
| 纯单元 | 无 | 不连库、不联网 | 本地 `PURE_UNIT_TEST=1` + CI |

约束：

- 三类计数可对账：`postgres + 纯单元 = 总数`，`external_data` 与前两类正交。
- `external_data` 失败属外部依赖问题，不阻断开发部署；连续多日失败需人工核查数据源。
- 禁止把 `external_data` 当作"测试跑不过就贴上去"的免死金牌；断言逻辑缺陷必须修复。

### TQ-93 新增测试必须显式标注 marker

- 新增测试若需要真实数据库，**必须**由作者显式写 `@pytest.mark.postgres`；若依赖外部数据源，**必须**显式写 `@pytest.mark.external_data`。
- `conftest.py` 中基于 fixture 闭包与源码文本的自动判定为过渡机制，配套漏标检查 `_DB_SUSPECT_PATTERN` **只报告、不自动补 marker**；出现嫌疑项必须人工确认并补显式 marker。
- 存量归类稳定后应逐步移除源码文本扫描，改为纯显式 marker。

## 2026-08-02 收口：验收证据与结论纪律（CHANGE-20260802-005 配套）

> 来源：从已删除的工具专属角色文件中提炼的通用规则。

### TQ-94 测试必须进入正式测试文件

- 验收断言必须写入 `backend/tests/` 或 `frontend/src/**/__tests__/`；
- 禁止仅以临时 `python -c` / `node -e` / 一次性脚本的输出作为验收证据；
- 临时命令只能用于探查，不能替代可复跑的测试。

### TQ-95 失败重跑上限

- 同一测试集最多运行 2 次：第一次失败只修复相关问题，再复跑一次；
- 第二次仍失败必须停止并报告真实失败原因，禁止无限重跑或反复微调直到偶然通过。

### TQ-96 禁止用未验证结论冒充事实

- 未取得证据前，禁止在对话输出、CHANGE 或文档中写 `DONE` / `SUCCESS` / `COMPLETED` / `PASSED` 等成功结论；
- 未知、未验证、部分完成、阻塞和失败必须如实标记；
- 局部通过不得写成整体通过（与 TQ-83 叠加）。

### TQ-97 页面验收必须有三类证据

涉及前端页面的变更，验收时必须真实在浏览器完成并记录：

- **URL**：目标路由实际访问 URL（含 query 参数），确认 hydration 后不被默认值覆盖，前进/后退能正确恢复状态；
- **Console**：是否存在 error / warning，异常必须定位根因或明确标注为已知无关警告；
- **Network**：关键 API 请求的状态码与响应摘要，不得仅凭页面渲染成功推断 API 正常。

禁止以 IDE 截图或静态代码审查代替行为核验。

### TQ-98 成功判定三要素（涉及发布 pointer 的任务）

判定发布类任务成功必须**同时**具备：

- **pointer**：`factor_publications` 中对应 kind 的 pointer 已切换至目标 run，`data_run_id` 指向本轮 run；
- **版本**：repo HEAD、`algorithm_version`、运行代码 SHA 一致；
- **真实数据证据**：DB 查询或日志证明发布已生效。

`/health=200` 或"页面能打开"只能作为辅助证据，不能单独判成功。

### TQ-99 CI 结论读取纪律

CI 是**手工诊断工具**（`workflow_dispatch`），不是部署门禁（见 TQ-90.3/TQ-90.4）。手动触发 CI 后：

- 必须监控该**精确 commit SHA** 直到 Workflow 终态，不得用前一次 push 的 SHA 代替；
- 查询降级顺序：GitHub 连接器 → 已认证 `gh` CLI → 公开 REST API（`/repos/{owner}/{repo}/actions/runs?head_sha={sha}`，无需认证）；
- `gh` 未认证不能作为停止监控的理由；
- 数据库测试不通过 CI 执行；Migration、PG Integration 和 Synthetic E2E 只在远程验证库运行；
- 必须按真实日志修复，不得凭猜测改代码；无法下载日志时仍须报告失败 Job 名称并运行其本地等价命令；
- 报告须列出每个 Job 的 name 与 result，以及 `CI Gate` 的最终 conclusion；单个 Job 通过不能代替 `CI Gate` 结论。
