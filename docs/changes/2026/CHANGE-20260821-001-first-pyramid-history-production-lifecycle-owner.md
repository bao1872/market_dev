# CHANGE-20260821-001：First Pyramid History 生产生命周期 Owner（架构决策 + PRD 契约）

状态：prd_confirmed（docs-only；未进入 Implementation，未写业务/测试代码、未建表、未 migration、未改 API/前端/Maps/Runbooks/治理、未连接生产、未补历史数据）
日期：2026-08-21
类型：docs-only（governance + contract + architecture）
领域：量化模型 / 盘后编排 / 第一金字塔 canonical history / Review 上游
负责人：待填写

相关 PRD：

- `../../prd/80-first-pyramid-history-production-lifecycle.md`（新建，本文档对应的 PRD 契约）

相关 Maps：

- `../../maps/20-quant-model.md`（未修改；Maps 同步需用户验收后授权）
- `../../maps/30-after-close.md`（未修改；Maps 同步需用户验收后授权）
- `../../maps/70-review.md`（未修改；本文档不影响 Review consumer contract）

相关 Rules：

- `../../../rules/00-core-governance.md`
- `../../../rules/50-git-development-flow.md`

相关提交或 PR：

- 审计基线 `ecc2388ef736a42f89d9d2a4b1b74907cc806253`（dev / runtime）

替代：

- 无

被替代：

- 无

## 1. 摘要

通过 Task 1–4 只读审计（生产 PG + 代码 + 既有 PRD/CHANGE），将"First Pyramid history 在 2026-08-10 后无 successful advancement"这一事实收敛为明确的架构根因：**缺少 canonical history 的 production lifecycle owner**（current-run resolution、membership reconciliation、daily advancement、failure semantics）。本 CHANGE 记录该架构决策，并以独立 PRD `80-first-pyramid-history-production-lifecycle.md` 收口最小生产契约。未进入实现。

## 2. 背景与问题

- 现象：生产 `first_pyramid_history_daily_state` / `first_pyramid_history_events` 在 2026-08-10 后无新增；Review 已上线并真实消费 FP history，但历史表不跟随每日推进。
- 已排除的误推（用户指正）：
  - `events MAX(event_time)=08-07` 不能推"events 只回补到 08-07"（events 是事件表，稀疏属正常）。
  - `run_items 8/11–8/20 = 0` 不能证"没人尝试生产 history"（advance 语义不复用/不 claim/不修改 run items，故 run/run_items 无活动无法观察 advance 是否运行）。
  - `SPEC_OWNERSHIP_GAP` 在未做 PRD audit 前不得写 YES；本轮前为 UNRESOLVED，本 CHANGE 收口为 `P4_UNDEFINED`（无 daily/separate producer 明文）。
- 根因（审计实证）：`FirstPyramidHistoryRun` 同时承担"历史回补 execution record"与"长期 canonical dataset lineage anchor"两种语义（Model C）；生产侧缺少 daily advancement owner，membership 冻结在 run 创建时。

## 3. 变化前

- AfterClose 计算当日 canonical FP 并写 `StockFeatureSnapshot.summary_payload.first_pyramid`，但不 materialize 到 history 两表。
- history 两表只能由手动 CLI（`advance_history_to_trade_date.py` / `first_pyramid_history_backfill_cli.py`）补充。
- `advance_history_to_trade_date` 依赖已有 canonical `history_run_id`（如 `be56dcd2...`），participating set = 该 run 现有 succeeded run items（冻结）；单股失败 soft-fail（`except: logger.warning`，不 raise，最终 commit 可能部分成功）。
- Review 侧 `_resolve_canonical_history_source()` 取最新 ready `all_a_share` run，bind 后 fail closed；consumer 严格按 `algorithm_version` + `history_contract_version` + `source_history_run_id` 读取（不混读跨 lineage/version）。

## 4. 变化内容

- 新增独立 PRD `docs/prd/80-first-pyramid-history-production-lifecycle.md`，定义 6 条 producer 契约：Owner / Canonical Identity / Run Rollover / Membership Reconciliation / Failure / Consumer。
- 架构决策（本 CHANGE）：在 Model C 事实上按 Model A 方向收敛（沿用 `FirstPyramidHistoryRun` 作 dataset lineage，不新建第二套表，不采用 Model B 每日/epoch run）。
- 明确分界：Review resolver（consumer resolution）与 production `ensure_current_first_pyramid_history_run()`（producer lifecycle）为两个独立函数，不可混用。
- 明确 `PARTICIPATING_SET = FROZEN` 对每日 Review 不成立，须增量 reconcile（existing active / new-missing bootstrap / skipped-failed reevaluate / no-longer-current inactive）。
- 明确新股 onboarding 必须 bootstrap 所需 lookback（T-N…T），不能只算今天。
- 明确 failure 映射：exact-T incomplete → Review 不发布、AfterClose `partial_success`、resume 可重试；禁止 soft-fail 后报整轮 SUCCESS（Gate #9）。
- 明确禁止硬编码任何 production run id（代码已禁止 `be56dcd2`）。

## 5. 变化后

- FP history 的生产责任被正式定义为 AfterClose orchestration owner 的上游契约。
- canonical run 的 current-run resolution 与 membership reconciliation 由 producer owner 负责，与 Review consumer resolver 解耦。
- 实现 slices（Phase 1–7：production current-run resolver → membership reconciliation → daily advance owner → AfterClose wiring + failure/checkpoint → 真实 PG tests → backfill 08/11→current → runtime canary）待单独实现任务授权，不在本 CHANGE 范围内。

## 6. 影响范围

### API 或契约

- 新增 PRD `80-first-pyramid-history-production-lifecycle.md`（producer 契约）。
- `70-review.md` **未修改**（Review Freeze 继续生效，consumer contract 不变）。

### 数据

- 未改动任何表 / migration / 生产数据。

### 后端

- 未改动任何代码。

### 部署与运行

- 未部署、未连接生产、未补历史数据。

（其余小节不适用，删除。）

## 7. 迁移与兼容

无（docs-only；实现阶段如需 migration 由对应实现 Change 记录）。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| FP history 08-10 后无 advancement | 生产 PG 只读 | PASS（事实） | `daily_state MAX=2026-08-10`；runs 08-10 后 0 新 run；run_items 8/11–8/20 窗口 0 activity |
| advance 无自动 caller | 代码 grep | PASS | `advance_history_to_trade_date` caller 仅 CLI + 测试 |
| history_runs 唯一生产者 | 代码 grep | PASS | `create_history_run` 仅 `first_pyramid_history_backfill_cli.py` 调用 |
| run identity 幂等契约 | 代码 + 测试 | PASS | `create_history_run` lookup = algo+hash+scope，不含 trade_date/status |
| consumer lineage 严格 | 代码 | PASS | `review_observation_prep_service` 强制 algo+contract+source_run_id |
| 本 CHANGE 实现验证 | — | 未验证（docs-only） | 未进入 Implementation |

不得用"代码看起来正确"代替运行证据。

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD | 新增 `docs/prd/80-first-pyramid-history-production-lifecycle.md` |
| Maps | 无变化（待实现验收后单独授权同步） |
| Runbooks | 无变化 |
| Rules | 无变化 |

## 10. 回滚方案

docs-only；无代码/数据/部署可回滚。若 PRD 方向被后续决策推翻，删除 `80-first-pyramid-history-production-lifecycle.md` 并标记本 CHANGE 为 `superseded`。

## 11. 遗留问题与风险

- 实现阶段须决策：production current-run resolver 如何发现并切换到新 run（resolver 已解决 Review 侧，但 advance 写入侧需对应机制）。
- 实现阶段须决策：advance 的 soft-fail 在 AfterClose 下应映射 `FAILED` 还是 `PARTIAL_SUCCESS`（本 PRD 已定 `PARTIAL_SUCCESS`，但仍需实现确认与现有 AfterClose failure contract 对齐）。
- `FirstPyramidHistoryRun` 的 execution-record 痕迹（status/completed_at/run_item status）为技术债，本 PRD 不重构 schema，后续如需显式 dataset-epoch 语义再评估。

## 12. 后续变化

- 实现阶段 Change（Phase 1–7）将引用本 CHANGE 与 PRD 80，并各自记录实现、测试、PG、部署证据。

## 13. Implementation / Rollout 顺序（实施决策，非长期产品契约）

1. 修 production producer（current-run resolver / membership reconciliation / daily advance owner）。
2. PG 验证（真实运行证据，非仅 unit/mock）。
3. 再补 2026-08-11 起的缺失历史：用修好的 canonical path 补齐，不依赖独立人工 repair 掩盖生产缺口。

## 14. Review Freeze 边界声明（治理/实现边界标签，非 runtime 配置常量）

以下为本 CHANGE 的治理/实现边界标签，不是 runtime configuration constants；不得据此在代码中新增配置项。

- `REVIEW_CODE_FREEZE = TRUE`：Review 代码零修改；Review 测试仅作为回归保护使用。
- `REVIEW_CONSUMER_CONTRACT = PRESERVE`：Review 的 canonical binding、lineage resolution、`_resolve_canonical_history_source()`、composition、publication 全部保持不变。
- `FIX_DIRECTION = UPSTREAM_ONLY`：修复方向只在上游 producer（本 PRD / 本 CHANGE 范围）；不反向修改已验收的 Review consumer 以适配新 producer。
- `docs/prd/70-review.md` 未修改；其 lineage / version 读取语义与 canonical-history readiness contract 保持不变。
- 现有 Review 已允许 `partial` / `succeeded` 的合法 canonical HistoryRun；PRD §5 适配该 readiness，不重新定义 Review 成功标准。
