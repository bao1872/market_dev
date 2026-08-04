# CHANGE-20260804-003 Review 依赖矩阵 + 发布质量硬门 + 原子发布幂等（QM-63）

- 日期：2026-08-04
- 范围：backend Review 计算编排、发布门禁、数据模型、Migration、契约测试
- 关联：PRD `docs/prd/70-review.md` §27，Map `docs/maps/70-review.md` §24
- 前序：CHANGE-20260804-002（FS 列合同 + cancel 短路）、QM-62/QM-63 第一金字塔 canonical 与 SMC formatter 收口

## 改了什么

### 1. Review run 显式承载上游 chip 依赖状态

`market_review_runs` 新增两列（Migration `083_review_run_chip_dependency`）：

| 列 | 类型 | 语义 |
|---|---|---|
| `source_chip_run_id` | `UUID NULL` | 输入 chip 共识 run id；`NULL` 明确表示 chip 不可用（core-only 降级） |
| `degraded_reasons` | `JSONB NOT NULL DEFAULT '[]'` | 降级原因列表；空数组表示无降级 |

新增 `review_orchestrator_service._resolve_chip_dependency()`，在 `create_run` 中查询
`stock_chip_consensus_snapshots` 并按 status 分组判定：

- 无行 / 全失败 → `(None, ["CHIP_UNAVAILABLE"])`
- 部分成功 → `(core_run_id, ["CHIP_PARTIAL"])`
- 全部成功 → `(core_run_id, [])`

`on_conflict_do_update` 的 `set_` 同步包含这两列，保证重建 run 时降级状态被**刷新**而非沿用陈旧值。

### 2. 发布门禁新增三条质量硬门

`review_publication_service.evaluate_publish_gate` 在既有检查之后新增：

1. **无未来数据（point-in-time）**：本 run 的 `market_review_metric_observations`
   不得存在 `trade_date >= run.trade_date` 的记录，检出即 block 并报告条数；
2. **reason 完整性**：market 范围 P/Q/U/C/V 处于非 ready 状态
   （`raw_ready=false`，或 `raw_ready=true` 且 `normalized_ready=false`）
   必须给出非空 `readiness.reason`，否则 block；
3. **all-null 禁止发布空壳**：market 范围 P/Q/U/C/V 的 `value` 全为 `None` 时禁止正式发布
   （仍可以 provisional/failed 留档审计）。

三条硬门只产出 blocker，不做任何自动修正或静默兜底。

### 3. 测试

- 新增 `backend/tests/test_review_dependency_matrix.py`（13 项纯单元契约测试）：
  chip 四态、auction 不阻断、59/60 边界语义、未来数据 block/放行、industry 隔离、
  all-null block、reason 缺失/给出、重复发布零写入幂等。
- 适配 `backend/tests/test_review_publication_safety.py`（22 项）：
  `_FakeResult` 补 `.scalar()`，`_gate_pass_results` 参数化 `live_review_pointer` / `future_obs_count`，
  以匹配新的门禁查询顺序。

## 为什么改

原实现存在三类可发布"看起来正常但实际不可信"的数据的口子：

1. **chip 降级不可追溯**：Review 生成时若 chip 共识缺失或全失败，run 记录中没有任何字段表明
   "本次是 core-only 降级"，事后无法区分"chip 本来就没有信号"与"chip 计算失败被静默跳过"。
2. **历史基线可能混入当日/未来数据**：point-in-time 隔离此前只靠计算侧代码约定，
   发布门禁没有对落库结果做事后校验，一旦回填逻辑出错会污染分位数且无人发现。
3. **无原因的不可用与空壳发布**：非 ready 状态允许 `reason` 为空，
   全 null 的 market 快照也能通过门禁成为正式 pointer，前端只能展示一片空白且无法解释原因。

## 修改前后关键差异

| 维度 | 修改前 | 修改后 |
|---|---|---|
| chip 缺失 | 静默继续，run 无标记 | `source_chip_run_id=NULL` + `degraded_reasons=["CHIP_UNAVAILABLE"]` |
| chip 部分失败 | 与全成功无法区分 | `degraded_reasons=["CHIP_PARTIAL"]` |
| 重建 run | 降级状态可能沿用旧值 | `on_conflict_do_update` 刷新 |
| 未来数据 | 门禁不检查 | 检出 `trade_date >= run.trade_date` 即 block |
| 非 ready 缺 reason | 允许发布 | block |
| P/Q/U/C/V 全 null | 可成为正式 pointer | block（仅可 provisional 留档） |
| auction 失败 | 语义未固化 | 明确不参与门禁，默认降级不阻断 |

## 受影响面

- **行为**：发布门禁更严格，此前能通过的三类劣质 run 现在会被阻断（这是预期收紧）。
- **契约**：`MarketReviewRun` 新增两列；admin/API 若需暴露降级信息可读取 `degraded_reasons`。
- **数据**：需要 apply Migration 083 才能写入新列。
- **测试隐式依赖**：`evaluate_publish_gate` 的查询顺序被 mock-session 契约测试固化，
  调整实现顺序时必须同步更新两个测试文件的结果序列（已在 Map §24.3 记录）。

## 验证

- `PURE_UNIT_TEST=1 pytest tests/test_review_dependency_matrix.py` → 13 passed
- `PURE_UNIT_TEST=1 pytest tests/test_review_publication_safety.py` → 22 passed
- `PURE_UNIT_TEST=1 pytest tests/ -k "review or after_close or chip or publication"` → 335 passed, 119 skipped
- Ruff：改动文件 All checks passed
- Mypy：改动文件 20 errors，与基线（stash 后重跑）**完全一致**，`new_errors=0`
- 全量 `PURE_UNIT_TEST=1 pytest tests/`：19 failed / 2776 passed；
  19 项失败（auction entitlement / calendar v9 / bars freshness / stock state entitlement）
  经 stash 基线对比确认为**既有失败**，与本次改动无关。

## 4. 前端展示收口（Commit 5，同日完成）

在依赖矩阵与质量硬门落地后，前端同步收口（详见 Map §25）：

- **四层贯通**：`degraded_reasons` / `source_chip_run_id` 自 ORM → schema → API → 前端类型 → UI 全链路透传。
  `ReviewHeader` 显示 Chip Run 溯源（null 显式「不可用」）与降级横幅（未知 code 原样展示不猜测）。
- **requiredObservationCount**：`ScopeMetricsTable` 冷启动 title 复用 `buildColdStartTitle`（含 `min_required`），
  删除本地重复实现；`EvidenceDrawer` 新增「所需观测数」，冷启动原因读 `readiness.min_required`
  （后端未给出则显式「未知」，禁止硬编码 60）。
- **chip 七态**：`ChipStatusState` 补齐 `interrupted/partial`；`FirstPyramidPanel` 用 `CHIP_STATE_LABEL` 七态映射
  （unavailable/interrupted/partial 各有专属标签），新增 `ChipProvenance`（source run / job / freshness / coverage）。
- **run 级溯源**：`FirstPyramidProvenanceVM`（sourceRunId/calculatedAt/fromBatchRun），
  Panel 头部批量 run 显示 `run <id> · <calculatedAt>`，即时计算显式标注。

**前端验证**：contract 测试 552 项全绿；TSC 0 errors；ESLint 0 errors；`npm run build` 通过。
**后端新增**：schema/API 两处透传测试（`test_review_dependency_matrix.py` 增 2 项，共 15 项）。

## 5. 未验证 / 缺口

- Migration 083 **未 apply**（本地不连共享库；远程 apply 需另行授权并先跑 `scripts/ops/panji-prod-preflight`）。
- `source_chip_run_id` / `degraded_reasons` 的真实数据表现尚未验证，属 `data_closed=false`。
- `sourceChipRunId` / `degradedReasons` 真实数据下的 UI 渲染效果未在浏览器端到端验证（无真实 run 数据，
  仅纯单元/合同测试覆盖）。
