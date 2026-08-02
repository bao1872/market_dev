# CHANGE-20260802-001 — dev-only 分支治理 + 区间筛选双输入 + Review bootstrap 正式入口

- **日期**：2026-08-02
- **类型**：governance + behavior + architecture + contract
- **影响范围**：分支模型与工作协议 / 行情页筛选交互 / Review 历史回填 / 盘后 Worker 运行结构
- **状态**：进行中（代码 + 本地纯单元测试通过；exact-SHA CI 待确认；**本轮未部署、未修改任何生产数据**）
- **基线**：`codex/panji-full-closure-20260801` @ `d6360ec5902124cf5394bfc6e883fc2a3852ac32`（CI Run 30732130951 = success）

---

## 1. 为什么改

三个彼此独立、但都会阻塞收口的问题：

1. **分支模型与实际工作方式不符**：规则文档仍描述多分支 + `trae/agent-*` 内部分支 + PR 流程，
   但实际所有 AI 助手都直接在 `dev` 提交。文档与现实不一致会持续产生无授权分支。
2. **行情页「区间」筛选不可用**：数字列选择「区间」操作符后只渲染一个输入框，
   用户无法输入上界，区间筛选实际不可用。
3. **Review 历史回填缺正式入口**：生产 pointer 停在 `review-1.1.0` 旧 run，代码目标为
   `review-2.0.0`。回填只有 CLI 入口，缺少 admin API，无法在生产侧受控执行、追溯与恢复。

---

## 2. 改了什么

### 2.1 分支治理（dev-only）

长期分支收敛为 `main` / `dev` / `experiment` 三个：

| 分支 | 定位 | 约束 |
|---|---|---|
| `dev` | 唯一日常开发分支 | 所有 AI 助手直接在此提交；`git push origin dev` |
| `main` | 稳定锚点 | 修改 / 合并 / 推送均需明确授权 |
| `experiment` | 隔离实验 | **不得作为部署来源** |

硬约束：禁止创建任何新分支（含 `backup-*`、`trae/agent-*`）、禁止从 `dev` 切换到其他工作分支、
禁止 force push。需要可恢复点时使用 **checkpoint commit** 而非分支。

同步修改：`AGENTS.md` §8、`rules/50-git-development-flow.md`、`rules/60-trae-work.md`、
`rules/70-trae-cn.md`、`rules/90-deprecated-forbidden.md`、`rules/README.md`、
`docs/prd/80-system-runtime.md`（SR-09 / SR-10）、`docs/runbooks/branch-governance.md`。

> **连带修复**：`tools/check_governance_rules.py` 是 CI 门禁之一，原先硬编码要求
> `60-trae-work.md` 必须包含 `"trae/agent-"` 与 `"git push origin HEAD:dev"`。
> 若只改规则不改脚本，CI 必然失败。已同步将校验短语改为 dev-only 关键短语。

### 2.2 行情页区间筛选（two-input）

**根因**：`StrategyDataTable.tsx` 中 `isNumberInput` 的判定条件未排除 `between` 操作符，
导致数字列选择「区间」时仍走单输入框分支，双输入框分支永远不可达。

修复（未复制第二套组件、未硬编码任何字段名）：

- `isNumberInput` 增加 `!isBetween` 约束，新增 `isNumericBetween` 走双输入框分支；
- 数值区间两个输入框 `type="number"`，补 `aria-label="下界" / "上界"`；
  日期区间补 `aria-label="起始日期" / "结束日期"`；
- 新增行内校验：空值 / 非数值 / 下界 > 上界 / 起始日期 > 结束日期一律 `setError` 拦截且**不提交**
  （替代原先静默 `onClear()` 清空的行为）；切换操作符时清除错误。

`value` + `value2` 的 URL / preset 往返经核验本已正确（`marketWorkspaceUrlState.ts`），本次只补测试固化。

### 2.3 Review bootstrap 正式入口

新增三个 admin 端点，**提交与执行分离**：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/admin/review/bootstrap` | **202 + job_run_id**，只入队不执行 |
| GET | `/api/v1/admin/review/bootstrap/{job_run_id}` | summary + 分页明细 |
| POST | `/api/v1/admin/review/bootstrap/{job_run_id}/resume` | 失败 / 中断任务重新入队 |

- 120 交易日 × 全 scope 的回填耗时远超 HTTP 超时，**禁止单请求同步跑完**。
  提交端点只创建 `status=queued` 的 `SchedulerJobRun`，计算由 Worker 领取。
- 状态查询返回全局 summary（`succeeded` / `skipped` / `unavailable` / `failed` 四类计数
  + `reason_codes`）+ 按 `(trade_date, scope_type, scope_key)` 的**分页**明细。
- 新增 `review_bootstrap_job_service`：run_key 构造、提交 / 领取元数据、执行编排、状态摊平。
  dry-run 与 apply 使用不同 run_key，互不幂等抵消。
- CLI 补齐 `--operator` / `--reason` / `--algorithm-version` 与四类计数摘要。

**安全默认**：

- `dry_run` 默认 True，且 dry-run 路径**零业务写入**——不建 run、不写 `metadata_json`、
  不写 observations、不切 pointer；`operator` / `reason` / `input_hash` 仅在响应与日志返回，
  **apply 才落库**。
- `operator` / `reason` 必填；`algorithm_version` 必须等于 `BOOTSTRAP_ALGORITHM_VERSION`。
- `end_date` 留空时解析为**最近一个完整 A 股交易日**（查 `trading_calendar`），
  不使用自然日 today。

### 2.4 Worker 挂载点（实现过程中发现的真实缺陷）

初版把 bootstrap worker 注册为 `WORKER_TYPE in ("review_bootstrap", "all")`。
核验 `docker-compose.prod.yml` 后发现：**生产不存在 `WORKER_TYPE=all` 容器**，
after-close 容器跑的是 `WORKER_TYPE=after_close_orchestrator`。
按初版实现，admin 提交的任务会**永远停在 queued 且无任何报错**。

已改为与 chip consensus 同构：`run_after_close_orchestrator_worker` 主循环按
**core → chip consensus → review bootstrap** 顺序轮询，bootstrap 排最后，
保证历史回填不抢占当日盘后主链；独立 worker 仅在 `WORKER_TYPE=review_bootstrap`
时启动（调试 / 独立部署），避免 `all` 模式重复领取。

该缺陷类别（静默不执行）已由三条回归测试固化。

### 2.5 历史序列兼容性契约（仅固化，未改判定实现）

`load_metric_history()` 的兼容性维度 = scope identity（`scope_type` + `scope_key`）
+ compatible taxonomy + `algorithm_version` + metric definition version。

**`membership_version` 随每条观测持久化（可追溯当日成员），但不参与历史序列过滤**——
成分股增减是常态，若按其过滤，任何一次调仓都会截断 60 日历史并重新冷启动。
本次仅新增断言固化该契约（WHERE 子句不得含 `membership_version`；
跨三个 `membership_version` 的历史必须连续），**未修改判定实现**。

---

## 3. 修改前后关键差异

| 项 | 修改前 | 修改后 |
|---|---|---|
| 长期分支 | 多分支 + `trae/agent-*` + PR | `main` / `dev` / `experiment`，dev-only 直提交 |
| 恢复点机制 | backup 分支 | checkpoint commit |
| 数字列「区间」 | 只渲染一个输入框，不可用 | 双输入框 + 行内校验，非法输入不提交 |
| 区间非法输入 | 静默 `onClear()` 清空 | 显示错误原因并拦截 |
| bootstrap 入口 | 仅 CLI | CLI + admin API（202 异步 + status 分页 + resume） |
| bootstrap 审计 | 无 | `operator` / `reason` / `input_hash` 必填并落库（仅 apply） |
| bootstrap dry-run | 会创建 run 记录 | 零业务写入 |
| `end_date` 缺省 | 自然日 today | 最近一个完整 A 股交易日 |
| bootstrap 执行者 | 无 | after-close 主循环最低优先级轮询 |

---

## 4. 受影响的行为 / 契约 / 结构 / 运行方式

- **契约**：新增 3 个 admin 端点与对应 schema；`bootstrap_history()` 签名扩展
  （新增 `dry_run` / `algorithm_version` / `operator` / `reason`，返回值新增
  `scope_counts` / `reason_codes` / `input_hash`）。
- **运行方式**：after-close 容器新增第三类轮询任务；新增可选 `WORKER_TYPE=review_bootstrap`。
- **数据**：本轮**未修改任何生产数据**。bootstrap apply 会写
  `market_review_metric_observations` 与 review run metadata，需在部署后单独授权执行。
- **前端**：区间筛选交互变化（双输入框 + 校验），无 API 契约变化。

---

## 5. 验证

本地全部在 `PURE_UNIT_TEST=1` 下运行，**未连接任何数据库**：

| 项 | 结果 |
|---|---|
| `test_review_bootstrap_admin_entry.py` | 42 passed |
| `test_review_metric_observation_bootstrap.py` | 与上合计 47 passed |
| review / worker / bootstrap 相关回归 | 177 passed（基线 135，新增 +42）；3 failed + 43 errors **与基线逐项一致**，均为需 DB 的 CI-only 集成模块 |
| 前端 `test:contract` | 526 passed / 0 failed（含新增 16 条区间契约 + 2 条 URL 往返） |
| `tsc -b` / `eslint` | exit 0 |
| `ruff check`（本次改动文件） | All checks passed |
| `tools/check_governance_rules.py` | PASS |
| 模块自测 | `review_bootstrap_service` / `review_bootstrap_job_service` / `schemas.review` / `api.admin_review` 全部通过 |

反向验证：区间筛选测试在注入原始 bug 后 fail 1、还原后 16 passed，确认能真实捕获回归。

**未验证 / 待确认**：

- exact-SHA CI（含 PostgreSQL 集成）终态未确认；
- 生产部署、bootstrap apply、`review-2.0.0` run 创建与 pointer 发布**均未执行**；
- Migration 079 的生产数据量级与 PostgreSQL 版本未确认（见 runbook §6.2）。

---

## 6. 关联文档

- `docs/maps/70-review.md` §23（bootstrap 正式入口与兼容性契约）
- `docs/maps/80-system-runtime.md`（after-close 容器轮询顺序）
- `docs/runbooks/review-restore-and-publish.md` §2（入口与安全默认）、§6（Migration 079–082 清单）
- `docs/prd/80-system-runtime.md` SR-09 / SR-10
- `rules/50-git-development-flow.md`、`rules/60-trae-work.md`、`rules/90-deprecated-forbidden.md`

---

## 7. 遗留与风险

- **Migration 079 是唯一需要停机窗口的迁移**：含 `SET NOT NULL`（无 server_default，
  依赖前置 UPDATE 100% 覆盖）、唯一约束语义收窄、`factor_publications.data_run_id`
  指针重写无逆向脚本。**回滚必须依赖物理备份，不得依赖 `alembic downgrade`**。
  详见 runbook §6.1。
- `docs/maps/70-review.md` §12.1 / §12.2 中「无 bootstrap 代码 / 无回填机制」的旧记录已过期，
  本次已就地更正并指向 §23。
