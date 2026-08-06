# CHANGE-20260806-011 — ProductReadiness 六状态事实对齐

- 日期：2026-08-06
- 类型：verification-infrastructure + testing + governance
- 状态：`implemented_unconfirmed`（本地门禁全过；远程 full-closure 复验未执行）
- 需求出处：用户提交审查报告，要求将「四场景调试」升级为「六状态事实证明」，
  并明确授权修改受保护治理域（`scripts/verify/**`）与相关合同测试；
  fully_ready 策略经用户选定为「选项 B — 扩大 synthetic 规模合法达成」（8 条约束）

## 问题

原验证 Seed 用 `full_success / async_enhance / degraded / governance` 四个场景，
与正式 `ProductReadinessService` 已实现的**六态 closure** 语义不对应，并存在四处事实错位：

1. **Review 只是 observer**：Seed 从不调用 review producer，`market_review` publication
   永远缺失，mandatory 链条永远不完整。
2. **board_aggregation 缺 pointer**：Seed 只做单板块 compute，从不产生
   `market_aggregation` publication，`_board_aggregation_state` 永远 unavailable。
3. **async 场景未发布 stock_core**：导致本应 `mandatory_ready_enhancing` 的场景退化为 pending。
4. **degraded 场景未完成 mandatory 主链**：degraded 语义（mandatory 就绪、enhancement
   全终态但非全 truly ready）无法成立。

结果是四个场景实际全部落到 `pending` / `blocked`，而断言写成 `assert closure in (...)`
的宽松形式，把语义错位掩盖成「测试通过」。

## 变更

### 1. 六态 canonical 场景（替换四场景）

| 场景 | 交易日 | 唯一预期 closure |
|---|---|---|
| `pending_no_core` | 2026-07-28 | `pending` |
| `blocked_mandatory_failure` | 2026-07-29 | `blocked` |
| `core_ready_waiting_mandatory` | 2026-07-30 | `core_ready` |
| `mandatory_ready_enhancing` | 2026-07-31 | `mandatory_ready_enhancing` |
| `degraded_terminal_partial` | 2026-08-01 | `degraded_ready` |
| `fully_ready_all_fresh` | 2026-08-02 | `fully_ready` |

**不新增第七种 closure**。外部门禁不满足以「节点终态失败 + 无 publication」表达，
最终 closure 落 `blocked`；`synthetic_external_ceiling` 仅作 Pure Unit 诊断场景，
不参与绿色验收。

### 2. Seed 补齐真实 producer（`scripts/verify/seed_v21_verify_data.py`）

- `_run_and_publish_review`：`create_run` → `compute_run` → `publish_run(force=False)`。
  **不使用 force**，必须真实通过发布门禁；门禁失败如实打印并由严格断言暴露，不静默兜底。
  显式传 `source_core_run_id` / `source_board_run_id` 记录同 universe lineage（约束 5）。
- `_publish_board_aggregation`：调用 `compute_all_boards(publish=True)`，
  由生产路径产生 `board_analysis` 与 `market_aggregation` publication。不直接写
  `factor_publications`（约束 4）。
- `_seed_blocked_board_failure`：显式写入终态 failed `BoardFactsRun` 并删除该日
  `board_facts` publication，使 blocked 语义与 universe 规模**解耦**（约束 7），
  不依赖「100 只标的规模不足」这一偶发原因。
- `_add_board_prereq` 现返回 `BoardFactsRun.id` 供 Review lineage 消费。

### 3. full_market universe（用户选项 B）

`FM_N_INST=5200` synthetic 标的、220 行业 + 320 概念、约 67,600 条成员关系，
经**正式** `validate_snapshot` / `sync_boards` 真实计算并通过 Board Facts 绝对门禁
（raw_rows ≥ 5000 / industry ≥ 200 / concept ≥ 300 / relation ≥ 60000 / coverage ≥ 0.99）。

**未修改任何门禁阈值，未 mock 质量门，未直接写 readiness**（约束 3、4）。
原 `N_INST=100` 保留作历史兼容，不承担 fully_ready 证明（约束 1）。

### 4. Seed twice 严格事实向量幂等

新增 `--seed-twice`：两次运行各采集完整事实向量（run / item / snapshot / publication /
pointer / natural-key / closure 向量）并做结构化 diff。仅审计时间字段
（`created_at` / `updated_at` / `started_at` / `finished_at` / `published_at` 等）允许变化，
其余任何差异判定为幂等违规并抛错。

### 5. 严格 closure 断言（废弃宽松断言）

- `_verify_closures(strict=True)`：六场景一对一断言，不匹配即抛 `AssertionError`
  并输出阻塞节点清单。`--no-strict` 仅供诊断，不得用于绿色验收。
- `backend/tests/test_pg_seed_scenario_closures.py` 重写：删除
  `assert closure in (...)` 宽松形式，改为 `assert ev.closure == expected`；
  新增 7 个负例（pending 的 stock_core 不可消费、blocked 有 terminal mandatory、
  core_ready 的缺失 mandatory 非 terminal failure、enhancing 真有未终态 enhancement、
  degraded 的 enhancement 全终态但非全 ready、fully_ready 全 fresh + composite auction、
  board_aggregation ↔ stock_core lineage 关系一致）。

### 6. 双层 golden 测试

- **Pure Unit**（`test_product_readiness_service.py`）：手工 `ProductReadinessState[]`
  → `evaluate_closure`，证明状态机本身正确。
- **PG E2E**（`test_pg_seed_scenario_closures.py`）：真实 DB 事实 → `collect_states`
  → `evaluate_closure`，证明 Seed 事实与状态机语义一致。
- 新增 `backend/tests/readiness_fixtures.py`：两层共享**期望值**，不共享算法。
- lineage 断言比较**关系**（`board_aggregation.source_core_run_id == stock_core pointer`）
  而非随机 UUID 字面值。

### 7. 结构化诊断输出

固定文件名写入 `READINESS_DIAG_DIR`（默认 `/tmp/readiness-diagnostics`）：
`readiness-scenario-matrix.json`、`readiness-lineage.json`、`closure-decision.json`、
`seed-idempotency-diff.json`。日志仅保留汇总数、失败节点、reason code 与有界样本。

## 如实标注（不得当作已验证事实）

1. **`BoardFactsRun` 无 `reason_code` 列**：run 级原因载体是 `error_code` /
   `error_message` / `gate_results_json`。Seed 写入
   `error_code=EXTERNAL_GATE_UNSATISFIED` 作为 DB 侧事实。
2. **`ProductReadinessState.lineage["reason_code"]` 由 service 自行生成为 `RUN_FAILED`**
   （`product_readiness_service.py:937`），**不读取** `BoardFactsRun.error_code`。
   Seed 未修改 service 判定算法，因此 PG 断言侧期望值是 `RUN_FAILED`。
3. **`_board_facts_state` 优先看 publication**：只要该日存在 `board_facts` publication
   就判 ready，因此 blocked 场景必须先删除该 publication，failed run 才会生效。
4. **`compute_all_boards` 的发布门禁极严**：要求全部板块 `failed == 0` 才发布
   `market_aggregation`。full_market universe 下能否真实全绿**仅在远程验证库可证**，
   本地无法验证。
5. **fully_ready 是否真实可达尚未证明**：约束 6 要求严格断言，但该断言只有在远程
   full-closure 复验中才会被执行。本地仅证明「状态机正确」与「Seed 代码可导入、
   类型与语法正确」。

## 本地验证结果

| 门禁 | 结果 |
|---|---|
| Pure Unit（3 个相关测试文件） | 37 passed / 14 skipped |
| Ruff（changed files） | All checks passed |
| Mypy（changed files） | Success: no issues found in 4 source files |
| Compileall | OK |
| `tools/check_governance_rules.py` | Governance check passed |
| `git diff --check` | OK |

**未执行**：远程 `panji-verify` full-closure 复验（Migration / PG / Seed twice / E2E /
cleanup）。在该复验通过前，本变更不得视为闭环。

## 受影响文件

| 文件 | 类型 |
|---|---|
| `scripts/verify/seed_v21_verify_data.py` | MODIFY（受保护治理域，本轮已授权） |
| `backend/tests/test_pg_seed_scenario_closures.py` | MODIFY（治理域合同测试） |
| `backend/tests/test_product_readiness_service.py` | MODIFY（Pure Unit golden） |
| `backend/tests/readiness_fixtures.py` | NEW（共享期望值 fixture） |

**未修改**：`backend/app/services/product_readiness_service.py`（判定算法保持不变）、
`docker-compose.verify.yml`、`panji-verify` / `run_remote_verification.sh` /
`verify_attempt.py` 契约（`--scenario all` 仍有效）、AGENTS.md、rules/、PRD、Maps、Runbooks。

## 后续

1. 提交并 push `dev`，冻结 40 位 SHA。
2. 执行远程 full-closure 复验（高风险，需二次确认）。
3. 复验结果决定本 CHANGE 状态从 `implemented_unconfirmed` 转为闭环或转入新根因修复。
