# 40 测试与质量

> 来源：AGENTS.md §五、§七.20、§八、§六.6、§六.8、§七.8（测试部分）
> 状态：并行验证

## CHANGE 规则

每次修改必须新增 `docs/changes/records/CHANGE-YYYYMMDD-NNN.md` 并更新 `docs/changes/CHANGELOG.md`。

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

不存在"小改不用 CHANGE"。`tools/check_docs_consistency.py` 规则 12 强制校验 CHANGE 引用可达性。

## 文档目录与 CI 门禁

`tools/check_docs_consistency.py` 必须通过。

规则包括：

- MANIFEST 存在且含实现核对基线（40 位 SHA 且为 HEAD 祖先）；
- baseline 必须在 HEAD 的最近 50 个 commit 内；
- `docs/current/*.md` 与 `docs/maps/*.md` 存在；
- 本地 Markdown 链接有效；
- 无"待填写"占位符；
- `feishu_webhook` 不得回退为当前方案；
- open-decisions 不得把 Webhook vs Platform App 写回 OPEN；
- CHANGE 引用必须可达；
- ref/ 隔离文本扫描。

CI 必须失败若代码 SHA 变化后未同步 current/contracts/CHANGE/MANIFEST baseline。

## 质量门禁

```
Ruff    新增/修改 Python 文件零错误；历史债务由 tools/quality_baselines/ruff.json 管控
Mypy    新增 backend/app Python 生产文件零错误；历史债务由 tools/quality_baselines/mypy.json 管控
Docs    python tools/check_docs_consistency.py
Arch    python tools/check_architecture.py
Allow   python tools/check_test_allowlist.py
Gov     python tools/check_governance_rules.py
Reports python tools/check_reports.py
Sync    python tools/update_docs.py --check
```

禁止通过全局 ignore、批量 noqa、扩大 exclude、批量 `type: ignore` 或关闭检查掩盖新增问题。

前端：

- `tsc --noEmit`；
- `npm run lint`；
- `npm run build`；
- `npm run test:contract`；
- `npm run test:e2e`。

## Reports 报告体系（主归属）

`reports/` 是长期可读取的执行报告和验证证据目录。**主归属规则在本文件**，其他文件只建立入口引用。

详细规则见 `reports/README.md`（10 节）。要点：

1. 所有需要长期保留的 TRAE 完整报告写入 `reports/current/REPORT-YYYYMMDD-NNN-任务短名称.md`；
2. TRAE 对话只输出简短摘要 + 报告路径 + commit SHA + push 结果 + blocker；
3. `reports/LATEST.md` 是 AI 读取最新任务状态的固定入口；
4. `reports/INDEX.md` 是历史报告索引（按日期倒序）；
5. 不再向 `sync/outbox/` 写入报告（`sync/` 仅为临时中转站，不作为运行时真源）；
6. `sync/` 仅用于临时中转；
7. `reports/` 不是产品和架构事实真源；
8. 每次报告必须使用 `reports/templates/TASK-REPORT-TEMPLATE.md` 模板（固定 15 章节）；
9. 每次报告必须包含 Base SHA、Implementation SHA、Report Published Through SHA、检查结果、Git、部署、数据库和 Known Gaps；
10. 未提交、未 push 的报告不能描述为远程可读取；
11. 用户要求"查看最新 TRAE 报告"时，优先读取 `reports/LATEST.md`；
12. `tools/check_reports.py` 强制校验 15 个检查组（覆盖 SHA 完整性、秘密检测、模板章节、状态、命名等约束）。

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
