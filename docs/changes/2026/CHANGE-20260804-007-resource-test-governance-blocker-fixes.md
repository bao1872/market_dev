# CHANGE-20260804-007: 治理垂直切片 — 规则定义与文档对齐（实施待办）

- 日期：2026-08-04
- 类型：governance+docs+contract
- 领域：治理规则 / 文档映射 / 验收要求
- 关联 PRD：`docs/prd/80-system-runtime.md`
- 关联 Maps：`docs/maps/80-system-runtime.md`
- 关联 Changes：CHANGE-20260804-004（治理垂直切片首版，经审查判定不通过）
- 数据操作：**零写入、零部署**（未连接远程服务器、未 dry-run、未执行真实部署、未修改共享开发业务数据库）

## 1. 本 Change 的边界：仅完成规则与文档阶段

上一版（CHANGE-20260804-004）把「字段出现、局部测试通过」高估为「业务语义已落实」，经用户静态审查判定**不通过**。审查揭示的问题归为两类：

- **规则/文档层缺口**：DS-107 三条长任务主链的落地边界不清晰（stock core 是单股纯计算还是独立长任务）、成功判定与「partial 伪装 success」的语义冲突、预算必须显著低于容器上限的数值关系未落实到文档。
- **实现层缺口**：生产盘后链（`compute_review_core_with_run_items`）未接入应用级内存预算；stale repair 以 95% 覆盖写 succeeded；resume_token 非真断点；部署总超时 wrapper 有 `main: command not found` 缺陷；checker 只扫字符串不验行为。

**本 Change 只解决第一类（规则与文档）**，并列出第二类（实现）为后续独立实施阶段的待办。任何「已修复 / 已落地 / 已闭环」的实现性结论，本 Change **一律不宣称**。

## 2. 本轮实际完成：规则定义 + 文档对齐

- `rules/80-deployment-data-safety.md` DS-107：
  - 明确 **stock core 边界**：`first_pyramid` core 是单股同步纯计算、非独立批量长任务，内存由外层 Feature Snapshot 批量预算门禁统一治理；若未来引入独立入口须另行满足全部字段。消除「规则说必须有、代码没有、文档宣称完成」的三方矛盾。
  - 明确 **成功判定**：run 标 `succeeded` 必须同时满足 `stop_reason is None` **且** `processed_count == expected_count` **且** 失败率不超阈值；内存超限或未覆盖全部预期单位时必须写 `failed`（或模型支持的 `partial`）并如实记录 `stop_reason`，禁止发布 publication pointer。
  - 明确 **预算数值关系**：`memory_budget_mb` 必须显著低于所在容器 `mem_limit`（禁止等于或高于容器上限），由后续 checker 断言。
  - 明确 **并发**：默认 1，`--workers` 必须等于 1，非 1 拒绝。
- `docs/maps/80-system-runtime.md`：容器资源预算现状补充「预算必须显著低于 mem_limit」的硬约束与 checker 断言描述。
- 删除/避免任何「已实现、已闭环、已安全恢复」的越界声明。

## 3. 状态

```text
rules_defined       = true   （DS-100~107 规则定义完成）
docs_aligned        = true   （rules / PRD / Map / Runbook 已对齐；Map 明确现状与目标态）
implementation_pending = true （业务代码 / 部署脚本 / 资源预算 / 长任务恢复机制 / checker 行为门禁 / 回归测试均未开始）
runtime_unverified  = true   （无运行证据）
needs_deploy        = false
```

**不得**写作「P0/P1 已全部修复」「long_task_safe_resume = true」「部署脚本 DS-103/104 已补全」。这些属于后续实施阶段，一旦落地需各自独立 Change。

## 4. 撤回上一轮越界的实现改动

上一轮提交（`d5cabac`）同时混入业务代码、部署脚本、checker 与测试，超出「规则与文档」范围。本 Change 已通过追加收口提交将下列实现改动**撤回**（保留规则/文档内容）：

- `backend/app/services/review_bootstrap_service.py`
- `backend/app/utils/long_task_budget.py`
- `backend/scripts/feature_snapshot_backfill.py`
- `backend/app/services/after_close_orchestrator.py`
- `backend/tests/test_after_close_orchestrator.py`
- `scripts/deploy/panji-deploy.sh`
- `tools/check_governance_rules.py`
- `tools/tests/test_check_governance_rules.py`
- `tools/tests/test_long_task_budget.py`

> 说明：`rules/`、`docs/maps/`、`docs/changes/INDEX.md` 及本 CHANGE 属于文档层，予以保留。checker 中用于检查文档结构与规则引用的部分在后续实施阶段保留，但不允许用字符串标记假装业务实现已完成。

## 5. 后续实施阶段（单独形成各自 Change，不并入本规则 Change）

| 实施项 | 验收证据要求 |
|---|---|
| 容器与应用预算 | `memory_budget_mb < mem_limit` 且余量充足；部署后 `docker stats --no-stream` 实测高水位 |
| 长任务资源治理（Feature Snapshot / after-close / Review） | 生产入口与应用级 RSS 预算、安全停止、partial 如实上报 |
| checkpoint / resume | 业务断点按已提交 batch/item 持久化，续跑真正从 cursor 恢复，禁 partial 伪装 success |
| 部署脚本超时与回滚 | 总超时 wrapper 走既有失败路径；真实 shell 执行测试；回滚后 health/version/mount 验证 |
| checker 与回归测试 | 行为级验证（非字符串标记）；控制流真实执行 |
| dry-run | 真实服务器授权后的 dry-run 证据 |
| 经授权的真实部署 | 部署后资源复检与运行证据回填 Map |

## 6. 文档一致性检查

- 仅运行文档一致性、Markdown/link/引用、governance 纯文档类检查。
- 不运行部署测试、不连接数据库、不连接服务器。

## 7. 不在范围

- 未部署、未 dry-run、未连接远程服务器。
- 业务代码 / 部署脚本 / 长任务实现 / 行为级 checker 均未落地（属实施阶段）。
- 未创建分支、未 force push、未碰 `main`。
