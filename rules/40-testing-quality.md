# 40 测试与质量

> 来源：AGENTS.md §五、§七.20、§八、§六.6、§六.8、§七.8（测试部分）
> 状态：并行验证

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
> TRAE 完整过程只在对话输出，不写入仓库；普通 Bug 由 Git 历史记录，只有重要行为变化才写一个 CHANGE。
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

### TQ-82 禁止推后不监控CI

- dev push **不是结束**；以下完整闭环必须在对话终止前真实执行：
  1. `git push origin <branch>` →
  2. 找到最新 CI Actions run（对应 head_sha = 推送 SHA）→
  3. 等待 **CI Gate = success（全绿）** →
  4. 查看所有失败 job 的 annotations / logs 并修复后重推 →
  5. 全部门禁通过后才允许部署。
- 禁止以下三种"未全绿即声称成功"：
  - "我本地通过了，CI 失败应该是 flaky" → 不允许；
  - "Playwright 视觉回归是环境问题，我 skip 3 个" → 不允许；
  - "PG 集成测试 0 skipped 但有 1 个失败，我先部署再修" → 不允许。
- PG 集成测试必须 **0 skipped**；单个 failed 必须定位根因修复后重推，不得在 CHANGE 中写成 "1 个 flaky" 掩盖。

### TQ-83 禁止局部Canary冒充整体成功

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
- `AGENTS.md` / `docs/current/*.md` / `docs/maps/*.md` 不得把 `ref/` 文件称为"真源"、"合同"、"fixture 生成器"或"运行依赖"；应称为"参考源（人工阅读）"；
- 算法真源必须是生产代码（如 `smc_pine_core`、`node_cluster_engine`、`indicator_contract`、`indicator_semantics`）。

## Migration 测试纪律

修改 migration 必须有 upgrade / downgrade / upgrade 验证。详见 `80-deployment-data-safety.md`。

## 持久测试数据库禁用（CHANGE-20260728-007）

> 来源：AGENTS.md §8 基础安全边界
> 状态：硬约束

### 禁止范围

- 本地 Mac、开发服务器、腾讯云**创建或复用**持久测试数据库（如 `bz_stock_test`）。
- 本地测试连接正式库 `bz_stock` 或任何持久测试库。
- 把 CI 临时 Postgres 容器改为长期库。
- 保留 `.env.test`、`TEST_DATABASE_URL` 持久配置、SSH 测试库隧道说明、conftest 持久测试引擎或 Alembic 自动迁移到本地 Mac。
- 保留任何会自动创建或复用 `bz_stock_test` 的脚本。

### 唯一例外

CI（GitHub Actions）job 级临时 Postgres 容器，job 结束自动销毁。
CI 工作流中 `POSTGRES_DB: bz_stock_test` 仅作为容器内临时数据库名，不持久化。

### 本地测试规则

- 本地测试只能纯单元/mock。
- 必须设置 `PURE_UNIT_TEST=1` 跳过 DB 初始化。
- `backend/tests/conftest.py` 通过 `GITHUB_ACTIONS=true` 或显式 `PANJI_CI_DB_TEST=1` 识别 CI 环境。
- 非 CI 环境且未设置 `PURE_UNIT_TEST=1` 时，conftest 加载即失败。

### CI 临时库规则

- CI 工作流使用 job 级 `postgres:16` 容器，job 结束自动销毁。
- `TEST_DATABASE_URL` 由 CI 工作流注入，指向 `localhost:5432/bz_stock_test`（容器内）。
- 不得在 CI 之外保留 `TEST_DATABASE_URL` 环境变量。
- 数据库集成测试（使用 `db_session` fixture）只在 CI 运行；本地不运行。

### 新增测试规则

- 新增测试优先写成纯单元测试（不连接数据库）。
- 必须连接数据库的集成测试，必须使用 `db_session` fixture，并在 CI 临时库运行。
- 不得在本地 Mac 创建持久测试库以运行集成测试。
