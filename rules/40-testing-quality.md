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

## 2026-08-02 收口：CI 三层结构与测试分类（CHANGE-20260802-002 配套）

### TQ-90 CI 三层结构

单体 CI（14 个 job 无条件全量运行）已拆为三层，各层职责不得混淆：

| 层 | 工作流 | 触发 | 职责 | 阻断门禁 |
|---|---|---|---|---|
| Fast CI | `.github/workflows/ci.yml` | push dev / PR main | 按变更范围裁剪的快速反馈 | `CI Gate` |
| Release Gate | `.github/workflows/release.yml` | 手动指定 exact SHA | 全量测试 + 构建不可变镜像 + 生成 manifest | `Release Gate` |
| Nightly | `.github/workflows/nightly.yml` | 每日 03:00 | 全量回归兜底 | `Nightly Summary` |

- Fast CI 的裁剪只允许依据 `changes` job 的 git diff 输出，**不得**依据人工判断或 IDE 推测跳过任何 job。
- 被 `changes` 判定为范围内的 job 若 skipped，`CI Gate` 必须失败；被判定为范围外的 job 若实际运行且失败，同样必须失败（说明 `if` 条件与 `changes` 输出不一致，属 CI 配置错误）。
- Fast CI 的裁剪是速度优化，不是覆盖率削减；被裁剪掉的部分由 Nightly 每日兜底执行。
- `CI Gate = success` 仍是部署前置条件（见 TQ-82），三层拆分不放宽该要求。

### TQ-91 测试分类 marker

后端测试按执行环境依赖分为三类，由 `backend/tests/conftest.py` 的 `pytest_collection_modifyitems` 统一判定并在每次收集时输出 `[test-classification]` 摘要行：

| 类别 | marker | 含义 | 运行位置 |
|---|---|---|---|
| PG 集成 | `postgres` | 需要真实 PostgreSQL（锁、事务、JSONB、唯一约束等） | CI 临时容器（Fast CI 的 `db_changed` 分支 / Release / Nightly） |
| 外部数据 | `external_data` | 依赖 mootdx / pytdx / 交易所网络接口 | 仅 Nightly 独立分组 |
| 纯单元 | 无 | 不连库、不联网 | 所有层 + 本地 `PURE_UNIT_TEST=1` |

约束：

- **三类计数必须可对账**：`postgres + 纯单元 = 总数`，`external_data` 与前两类正交。任何一次收集的摘要行都应能与本文件记录的基线核对，出现漂移必须查明原因。
- `external_data` 失败**不阻断** Fast CI 与 Release Gate——这类失败通常源于外部服务不可达或数据延迟，而非本仓库代码回归。但 Nightly 必须单独报告其结果；连续多日失败需人工核查数据源可用性，不得长期无人过问。
- 禁止把 `external_data` 当作"测试跑不过就贴上去"的免死金牌。仅当失败原因确实是外部依赖时才可标注；断言逻辑本身有缺陷的测试必须修复，不得改标 marker 掩盖。

### TQ-92 新增测试必须显式标注 marker

- 新增测试若需要真实数据库，**必须**由作者显式写 `@pytest.mark.postgres`；若依赖外部数据源，**必须**显式写 `@pytest.mark.external_data`。
- `conftest.py` 中基于 fixture 闭包与源码文本的自动判定是**过渡机制**，只为存量测试做一次性归类，**不是长期唯一分类来源**。其固有缺陷：文本匹配无法理解语义（误判），新的连库方式不在列表中会静默漏判。
- 配套的漏标检查（`_DB_SUSPECT_PATTERN`）在收集期报告"源码含连库调用但未被判定为 postgres"的嫌疑用例。该检查**只报告、不自动补 marker**——自动补标会掩盖分类规则的盲区，而暴露盲区正是它的目的。出现嫌疑项必须人工确认并补显式 marker。
- 存量归类稳定后应逐步移除源码文本扫描，改为纯显式 marker。

### TQ-93 分类基线

2026-08-02 实测基线（`PURE_UNIT_TEST=1 pytest --collect-only`）：

```
postgres=1178  pure_unit=2496  external_data=6  total=3674
漏标嫌疑=0
```

修改测试分类规则后必须重新对账；总数或分类数出现非预期变化，须在 CHANGE 中解释原因，不得默默接受。
