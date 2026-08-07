# CHANGE-20260807-001 — Review 去 Chip 依赖 / coverage 语义 / Chip 提前分叉 / Readiness 三分类

- **日期**：2026-08-07
- **基线**：`dev@30bb34d04b88e788ff78a84cdaedd35ea06af23f`
- **来源**：`ref/代码审计.md` Phase 1–4
- **状态**：`implemented_unconfirmed`
- **类型**：behavior + contract
- **影响域**：复盘（Review）编排与发布门禁、盘后编排步骤顺序、产品就绪度（ProductReadiness）
- **Migration**：无（本轮零 Alembic 迁移、零表结构变更）
- **部署**：未部署
- **远程验证**：未执行（需用户单独授权）

---

## 1. 为什么改

审计在基线 `30bb34d` 上发现四类**合同与实现不一致**，共同特征是：系统对外声明的语义
与代码实际执行的语义不同，且被测试假绿掩盖。

| 编号 | 问题 | 事实后果 |
|---|---|---|
| AUD-04/05 | Review 在创建阶段查询 chip，并把 chip 降级原因写进自身 lineage；`ON CONFLICT DO UPDATE` 允许晚到 chip 改写**已发布**的 Review run | Review 的输入身份随时间漂移：同一 run_id 在不同时刻读出的血统不同 |
| AUD-06 | `run.coverage_ratio` 实为 scope **执行成功率**，却被当作数据质量指标透出，并被发布门禁按 `>= 0.95` 判定 | 10/10 scope 跑完但底层缺两成样本时，系统报告 1.0；"全绿"复盘可能建立在残缺数据上 |
| AUD-08 | chip 入队位于 Review 之后；Review cancelled/interrupted 触发短路直接 raise | chip 只依赖 stock_core，却因与其无因果关系的 Review 被取消而永远不入队 |
| AUD-10 | `dsa_projection`（stock_core 的派生兼容投影）与 chip/state_events/auction 同列为 enhancement | 兼容输出缺失时无法与"可选增强缺失"区分，降级原因不可归因 |
| AUD-07 | `test_review_v21_dependency_contract` 声明"Review 只依赖 stock_core + market_aggregation"，但只断言 `_resolve_source_run_ids()`，而真正的 chip 查询在其调用方 `create_run()` 内 | 合同声明为真、实际被绕过（假绿）；同时 `test_review_dependency_matrix` 反向保护"Review 依赖 chip"，两测试互相矛盾（Test Contract Drift） |

---

## 2. 改了什么

### Phase 1 — Review 与 chip 解耦并冻结 lineage

`backend/app/services/review_orchestrator_service.py`

- 删除 `_resolve_chip_dependency()` 与其私有 helper `_load_core_expected_count()`
  （共约 116 行）。退役而非保留：该函数的 `source_chip_run_id` **恒为 None**
  （chip 无独立 run，经 core_run_id 挂靠 stock_core），唯一实际产出就是把 chip 域的
  `degraded_reasons` / `chip_coverage` 注入 Review。
- `create_run()` 不再写入 `source_chip_run_id` / `degraded_reasons` / `chip_coverage`
  （dry_run 与正式 upsert 两条路径）。
- **`ON CONFLICT DO UPDATE` → `DO NOTHING`**：这是"已有 run 不被改写"唯一可靠的
  结构性保证。仅靠应用层不传 chip 字段并不足够 —— `DO UPDATE` 仍会用新 INSERT
  的其余字段覆盖既有行。已存在时由既有 `get_run_by_keys` 读回，幂等语义不变。
- ORM 列 `source_chip_run_id` / `degraded_reasons` **保留不删**（迁移 083 已建列），
  仅停止写入 → 零 migration、零向后兼容风险。

### Phase 2 — coverage 语义分离

- 新增 `_aggregate_run_data_coverage()`：`SUM(scope.ready_count) / SUM(scope.eligible_count)`，
  分母为 0 返回 `Decimal("0")`（不除零，也不回落成执行率冒充）。
- `run.coverage_ratio` 两处赋值（主路径 + 重算路径）统一改走该 helper。
- 新增 `_scope_execution_rate()` 派生值，在三处返回结构中以 `scope_execution_rate`
  显式表达执行率 —— 两个语义各自有名字，不再互相冒充。
- **无需 migration**：`MarketReviewScopeSnapshot` 已有 `eligible_count` / `ready_count`
  真实有效样本字段，run 级数据覆盖可直接聚合得出。

### Phase 3 — chip 提前分叉

`backend/app/services/after_close_orchestrator.py`

- chip 入队从步骤 4.9（Review 之后）前移到**步骤 4.6（stock_core 发布成功后立即分叉）**，
  守卫复用既有 stock_core 发布成功判据
  （`snapshot_error is None and snapshot_run_id is not None and not publish_failed`），
  与 state_events 同层。
- 修正 `_is_terminal_review_short_circuit()` 的 docstring 与日志：短路仍不覆盖总任务
  终态，但**不再影响 chip**（chip 已在更早阶段入队）。
- 可行性依据（已核验）：`create_after_close_chip_consensus_job` 以确定性
  `run_key = chip_consensus:<trade_date>` 走 `acquire_job_run_lock`，同日重复调用返回
  既有 job（`is_new=False`），故断点恢复重跑本段安全。

### Phase 4 — ProductReadiness 三分类

`backend/app/services/product_readiness_service.py`

- 新增 `REQUIRED_COMPATIBILITY_PRODUCTS = {"dsa_projection"}`，从 `ENHANCEMENT_PRODUCTS`
  移出；`NINE_NODES` 改三集合并集（仍为 9 节点）。新增 `classify_product()`。
- `ClosureEvaluation` 增加 `required_compatibility_ready` 布尔维度 +
  `REQUIRED_COMPATIBILITY_NOT_READY` 归因 issue（在所有分支之前生成，确保无论闭包落在
  哪个取值，归因都不丢失）。
- **closure 取值集合刻意保持不变**（仍是既有 6 个）：新增枚举值会破坏既有 API 消费方
  与前端映射，而"兼容输出未就绪"通过独立布尔字段 + issue 即可无损表达。
- **闭包时序严格向后兼容**：`_enhancement_terminal()` 的判定范围改为全部非 mandatory
  产品（enhancement + required_compatibility）。若只看 enhancement，`dsa_projection`
  仍在计算时会被误判成 `degraded_ready`（"已降级定型"），而事实是它还没跑完。
- API/schema 增加 `requiredCompatibilityReady`（additive，带默认值）。

---

## 3. 行为差异（改前 → 改后）

| 场景 | 改前 | 改后 |
|---|---|---|
| Review 创建 | 查询 chip 快照表 + stock_core expected_count | 零次 chip 查询 |
| 晚到 chip + 重复 create_run | 改写已发布 run 的 lineage 与降级原因 | DB 层零写入，返回原行 |
| `run.coverage_ratio` | scope 执行成功率 | 真实有效样本覆盖率 |
| Review 前端 chip 横幅 | 显示 chip 覆盖率 | 恒显示"不可用"（见下方遗留项） |
| Review cancelled/interrupted | chip **永不入队** | chip 已在 stock_core 发布后入队，不受影响 |
| `dsa_projection` 未就绪 | 混在泛化增强降级中 | 独立维度 + 专属归因 code |

---

## 4. 验证

本地 `PURE_UNIT_TEST=1`（`backend/.venv/bin/python`）：

| 测试 | 结果 |
|---|---|
| `test_review_v21_dependency_contract.py`（含 4 个新增 create_run 层用例） | 13 passed |
| `test_review_immutability_contract.py`（新增） | 3 passed |
| `test_review_coverage_contract.py`（新增） | 6 passed |
| `test_review_dependency_matrix.py`（退役 chip 反向合同，保留 11 项有效用例） | 11 passed |
| `test_after_close_phase0_contracts.py`（新增 4 个 chip 分叉顺序用例） | 28 passed |
| `test_product_readiness_service_layer.py`（新增 5 个三分类用例） | 29 passed |
| `product_readiness_service` 内置自检块 | OK |
| 受影响域整体（`-k "review or readiness or after_close or chip or closure"`） | 458 passed |
| 全量 PURE_UNIT | 723 passed / 341 skipped |

**未验证（如实标注 UNVERIFIED）**：
- 远程 PostgreSQL 集成、真实数据端到端、部署后行为 —— 本轮未授权，未执行。
- 发布门禁在真实数据下的实际通过率变化（见下方风险项）。

**两个 pre-existing 失败（与本次改动无关，已核验）**：
- `test_readiness_lineage_governance.py::test_dsa_counter_signature_requires_core_run`
  —— 断言 `_count_dsa_projections` 绑定 `StockFeatureSnapshot.source_run_id`，但该函数在
  `HEAD` 上（本次改动之前）已迁移到 `StrategyRun/StrategyRunItem`。陈旧测试。
- `test_review_member_fact_metric_contract.py::test_orchestrator_finishes_cross_section_before_any_signal`
  —— mock harness 问题（`'Session' object has no attribute 'execute'`）。
- 核验方式：排除本次全部改动域后重跑，同样 2 failed，且 `HEAD` 版本源码已确认不含
  `source_run_id`。

---

## 5. 风险与遗留

### 5.1 发布门禁判据发生实质变化（需关注）

`review_publication_service.py` 的 `coverage_ratio >= 0.95` 门槛**数值未改**，但其比较的
**量已改变**（执行率 → 数据覆盖）。真实数据下，"scope 全部执行成功但有效样本不足"的
交易日将从"可发布"变为"被门禁拦截"。

这正是审计意图（阻止残缺数据被当作完整复盘发布），但属于**行为变化**，需在真实数据上
观察实际影响面后再确认阈值是否仍取 0.95。**本轮未在真实数据上验证。**

### 5.2 Review 前端 chip 横幅降级

`frontend/src/features/review/ReviewHeader.tsx` 读取 `chipCoverage` / `degradedReasons`，
两者现恒为空 → 横幅恒显示"Chip: 不可用"。已核验前端有 `null` 与 `length > 0` 守卫，
**不会崩溃**，但信息丢失。

按审计 Phase 1 Task 5，chip 就绪度应改由 ProductReadiness / chip 域提供。
**前端改动不在本轮范围，未执行。**

### 5.3 规格缺口：`daily_facts` 与 `history_cross_section` 不等价（仅报告，未修改）

追溯结论：**不等价，且判定源可能长期为假。**

- `_daily_facts_state()` 用 `PUBLICATION_KIND_HISTORY_CROSS_SECTION` publication pointer
  判定 `daily_facts` 就绪。
- 但 `publish_history_cross_section()` 在 `backend/app/` **无任何生产调用方**，
  仅被 seed 脚本调用 —— 即正式盘后链路从不发布该 kind。
- 全仓不存在 `daily_facts` publication kind；PRD31 定义 `daily_facts` 为"目标交易日日线
  readiness"，与"历史截面发布指针"并非同一事实。

按 §4.2 PRD 授权门与审计原文（"不要让 IDE 直接修"），**只报告不擅改判定源**。
需用户单独发起 PRD 任务确认 `daily_facts` 的正确判据。

### 5.4 断点恢复的既有边界（非本次引入）

resume 且 `skip_publish=True` 时，`publish_failed` 重置为 `False` 而 `snapshot_run_id`
从 metadata 恢复。若上次 publish 确实失败，守卫仍可能为真。该行为**与既有
state_events 完全一致**，非本次引入；chip 入队本身幂等，重复调用安全。未扩大范围处理。

### 5.5 Phase 0 三项规格冲突（已决策，本轮未实施）

用户已拍板但属 Phase 5/6（需 PRD/前端授权），本轮仅记录：

1. stock_core 已发布但 Review 失败时父 run 终态 → 取 `partial_success`（改 PRD 对齐代码）
2. Auction lifecycle → 正式采纳 7 态（改 PRD）
3. Review 页面 → 五阶段 + Auction 辅助面板（改前端）

---

## 6. 未做

- Phase 5（规格依赖实现）、Phase 6（PRD/Maps 收口）、Phase 7（远程验证）。
- 未修改 PRD / Maps / Runbooks（本轮无授权）。
- 未 commit、未 push、未建分支。
- 工作区既有的 `product_readiness_service.py` lineage 校验改动按要求**原样保留**，
  未 commit / stash / restore / 覆盖；本轮三分类逻辑与其正交叠加。
