# CHANGE-20260805-006 — PRD Alignment Pass（D–J 合同级缺口收口）

- 日期: 2026-08-05
- 类型: 行为/契约修正（代码层，PG 行为待验证）
- 关联: `PRD-Acceptance-Matrix-V2.1-D-J-2026-08-05.md`、`ref/instruction.md`
- 分支: dev

## 背景

用户审查 `Corrective-3.2 + Gate 1 Finalization` 后指出：开发主体已完成，但存在若干
**直接违反 PRD 的合同级缺口**，原 `development_complete` / `code_ready=true` 标记过早。
本轮做一次聚焦的 PRD Alignment Pass，只收口已明确的合同缺口，不回退代码、不部署。

## 改动

### P0-1 `fully_ready` 逻辑错误（product_readiness_service.py）
- `ProductReadinessState` 新增 `auction_mode` 与 `is_product_ready` 语义字段，及 `is_truly_ready`
  属性（区分 terminal 与真正就绪：failed/partial/cancelled 虽 terminal 但不可消费）。
- `evaluate_closure`：fully_ready 现要求 enhancement **全部真正就绪** 且 **auction.mode==composite**；
  structure_only / hybrid / failed / partial 不得误判 fully_ready（改为 degraded_ready）。
- `_auction_state`：无论经由 publication 还是 domain run，均读取 `run.mode` 合并判定。

### P0-2 Board Facts fetch/reuse 顺序（board_facts_service.py）
- `run_board_facts` 重写为：先尝试 fetch 当前快照；仅当 fetch 或门禁失败、且前一个
  published run 在允许陈旧度内才复用（新增 `_try_reuse_previous_on_failure`）。
- `historical_replay` 仍禁 pywencai，只解析目标 trade_date 的 PIT publication。
- 原"先查 previous 即复用返回"逻辑删除。

### P0-3 / P0-4 概念与行业深度静默截断（wencai_board_provider.py）
- 概念 > 100：由静默截断改为抛 `WencaiConceptLimitError`（新增异常类）。
- 行业深度 > 3：由 `parts[:3]` 静默截断改为抛 `WencaiIndustryDepthError`（新增异常类）。
- `PROVIDER_TIMEOUT_SECONDS` 注释说明 PRD 未强制具体秒数（P1-1，据实：instruction.md 无 1800）。

### P0-5 Board Facts 门禁补全（board_sync_service.py）
- `_compute_snapshot_stats` 增加 `industry_coverage` / `invalid_industry_depth_count` /
  `max_concepts_per_stock` 统计。
- `validate_snapshot` 新增绝对门禁：行业覆盖率 ≥ 99%、行业深度违规 = 0、单股概念 ≤ 100、
  effective_date 不得晚于今天（禁止回填未来）。

### P1-2 ProductReadiness 接入真实 parent/child/heartbeat/lease
- schema：新增 `SchedulerReadinessDTO`（schedulerJobRunId/status/latestHeartbeat/leaseEpoch/
  isStale/totalChildren/processedChildren/unreconciledChildren），`GovernanceReportDTO.scheduler`。
- service：新增 `collect_scheduler` 查询 AfterCloseRun 的 SchedulerJobRun + enhancement 子任务真实聚合。
- `evaluate_governance` 接收真实 scheduler 聚合，优先判定未对账子任务。
- admin_readiness API 调用并注入 `governance.scheduler`。

### P1-4 Granular restart 合同（admin_errors.py / admin_after_close.py）
- `force?restart_from` 枚举扩展为 PRD §13.7 全边界（core/stock_core_published/dsa_projection/
  state_events/chip/auction/board/review）。
- 仅 `daily_ready` 已实现隔离重算；其余边界后端隔离重算函数未实现，返回
  `admin_not_implemented`（HTTP 501）显式化缺口，禁止伪造成功。

### P1-5 Synthetic E2E（tests/test_v21_synthetic_e2e_decision.py）
- 新增决策层 Synthetic E2E（无 PG，可 PURE_UNIT_TEST 执行），硬断言：
  DSA compute-once 门禁、board 禁回填未来、概念/行业深度门禁、fully_ready 需 composite auction。
- 完整 PG E2E（`test_v21_synthetic_e2e_pg.py`）仍为 `authored_not_executed`。

### 测试修正（据 PRD 纠正旧错误假设）
- `test_product_readiness_service.py` / `test_product_readiness_service_layer.py` /
  `test_v21_readiness_auction_decision_integration.py` / `test_wencai_board_provider.py`：
  修正此前编码了错误逻辑（terminal→fully_ready、概念截断）的测试，改为符合 PRD。

## 验证

- Ruff（改动 8 文件）：All checks passed
- Mypy changed-file（`scripts/quality/mypy-changed.sh`）：Success，exit 0
- PURE_UNIT_TEST pytest（9 目标文件）：140 passed，postgres=0

## 未闭合项（诚实标记）

- 所有代码改动尚未在真实 PG 验证（dev 环境禁止连 PG）。
- granular restart 仅 `daily_ready` 后端实现；其余边界后端隔离重算函数未实现。
- 前端完整页面接入（行情/详情/Review/auction/父任务与产品 readiness 分离/晚到更新/
  structure-only/hybrid/composite 展示）仅 Admin 工作台已验证，需逐页手动验证（P1-6）。
- PG synthetic E2E 未执行。

## 结论

```text
development_chain_D_to_J = partial
code_ready              = false   # 合同缺口已修，PG/前端整链未闭环
prd_code_alignment      = partial
```
