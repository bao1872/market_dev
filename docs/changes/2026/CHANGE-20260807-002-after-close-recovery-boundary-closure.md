# CHANGE-20260807-002 — 盘后断点恢复边界收口：Board Aggregation 解绑 skip_publish

- **日期**: 2026-08-07
- **类型**: behavior（correctness bugfix）
- **影响范围**: 盘后编排（after_close_orchestrator）断点恢复路径 / Board Aggregation / Review 前置条件
- **状态**: `implemented_unconfirmed`（targeted PURE_UNIT 全过；未部署、无 migration、远程验证未授权未执行）
- **来源**: `ref/代码审计.md` Phase 4.4 Recovery Boundary Closure，严重级 (P0) RB-01

## 1. 问题（RB-01 Recovery Boundary Collapse）

`backend/app/services/after_close_orchestrator.py` 中 Board Aggregation 的执行守卫为：

```python
if not skip_publish and _stock_core_published and snapshot_run_id is not None:
```

`skip_publish` 的真实语义是 `"publishing" in completed`，即**上一次尝试已完成 stock_core
发布、检查点停在 publishing**。把它作为 aggregation 的守卫，会导致：

1. 任何从 `publishing` 检查点恢复的盘后 run，**mandatory Board Aggregation 被永久跳过**；
2. `_aggregation_status` 保持 `"skipped"`，而 `_execute_review_step` 的前置条件是
   `stock_core_published and aggregation_status == "succeeded"`，因此 **Review 也被连带永久跳过**；
3. 该 trade_date 的盘后链路在断点恢复后**永远无法收敛到完整态**——重试次数再多也不会补齐。

这是正确性缺陷，不是性能或体验问题：aggregation 的合法性只应取决于「当前 stock_core
pointer 是否已正式发布」，与「本轮是否需要重新发布」无关。

## 2. 修改

### 2.1 守卫条件（唯一行为变更）

```python
# before
if not skip_publish and _stock_core_published and snapshot_run_id is not None:
# after
if _stock_core_published and snapshot_run_id is not None:
```

判据改为「当前 stock_core pointer 已发布 且 snapshot_run_id 存在」。

### 2.2 各路径行为

| 路径 | `_stock_core_published` | aggregation | 说明 |
|---|---|---|---|
| normal publish | True（刚发布） | 执行 | 行为不变 |
| `publishing` 断点恢复 | True（`resolve_stock_core_published` 重新核验） | **执行（修复点）** | 补齐 mandatory aggregation + review |
| `computing_review` 断点恢复 | True | 执行但幂等复用 | `compute_all_boards` lineage 命中 |
| stock_core 未发布 / superseded | False | 跳过 | 边界未放宽 |
| `succeeded` 整链路完成 | — | 不进入本段 | 更早的 `recovery_point == "succeeded"` 已提前 return |

### 2.3 重入安全性（历史表述已校正，详见 §6 Phase 4.4.1）

`board_analysis_service.compute_all_boards` 内置 lineage 校验：按
`source_core_run_id`（取自 stock_core pointer）+ taxonomy / membership / algorithm_version
匹配既有 `BoardAnalysisRun`。**关键校正（原 §2.3 表述不准确）**：`published_at is not None`
只表示「该 batch 历史上曾被发布」，**并不等于**当前正式 `market_aggregation` pointer 仍指向
它（lineage 唯一事实源）。命中既有 batch 时须进一步做 live pointer reconciliation（见 §6），
只有 live pointer 正确指向该 batch 且 lineage 一致才幂等复用；否则只恢复 pointer
（不重算）或走正式重算路径。因此「始终调用」是安全的。

### 2.4 注释同步

修正 4 处已与实现漂移的注释（函数顶部状态变量说明、normal publish 段依赖图、
skip_publish 分支说明、top-level post-core 依赖图），明确 aggregation 为 mandatory、
不受 `skip_publish` 控制；`auction anchor` 与 `publishing` 检查点仍是 normal publish 专属。

### 2.5 未改变的语义

- `skip_publish` 本身的定义与 stock_core 不重复发布语义不变；
- auction anchor / publishing checkpoint 仍仅在 normal publish 执行；
- aggregation 失败仍为 optional 软失败（`_aggregation_status="failed"`，不回退 core）；
- chip / state events 早于 aggregation 的 PC-8 顺序不变；
- superseded run 不消费任何 post-core 副作用的 P0-1 边界不变。

## 3. 变更文件

- `backend/app/services/after_close_orchestrator.py`（守卫条件 + 注释）
- `backend/tests/test_after_close_phase0_control_flow.py`（断言更新 + 3 项新增回归）

## 4. 测试

原 `test_skip_publish_pointer_current_recovers_but_no_normal_publish_steps` 中
「skip_publish 不得执行 board aggregation」的断言直接编码了本缺陷，已移除；
该测试其余断言（chip 重入、auction 不执行、publishing 检查点不推进）保留。

新增 3 项 targeted 回归：

| 测试 | 断言 |
|---|---|
| `test_rb01_skip_publish_recovery_still_runs_mandatory_board_aggregation` | `publishing` 断点恢复下 aggregation 必须执行 |
| `test_rb01_skip_publish_recovery_runs_review_after_aggregation` | aggregation 后 review 前置满足并执行；chip 仍早于 aggregation |
| `test_rb01_stock_core_not_published_still_skips_aggregation` | stock_core 未发布/superseded 时 aggregation 仍跳过（边界未放宽） |

执行结果（`PURE_UNIT_TEST=1`，targeted，未跑 full PURE_UNIT）：

- Ruff：`All checks passed!`
- `test_after_close_phase0_control_flow.py`：9 passed
- 扩展回归（phase0_control_flow / orchestrator / phase0_contracts / status_detail / worker / board_sync）：**55 passed, 50 skipped, 0 failed**

## 5. Phase 4.4.1 收口：Board Aggregation publication / pointer 闭环（RB-01 续）

基于已推送 `6c8a845` 继续，不 reset/rewrite，不启动 Phase 5/7。

RB-01 已解决 `skip_publish` 屏蔽 aggregation 的问题，但 `compute_all_boards()` 与
orchestrator 之间仍存在两个合同缺口：

1. `compute_all_boards()` 命中 `batch_run.published_at is not None` 就直接 `idempotent_reuse`，
   **没有验证当前正式 `market_aggregation` pointer 是否仍指向该 batch**；
2. orchestrator 对 `compute_all_boards()` 任意非异常返回都直接写 `_aggregation_status="succeeded"`，
   **没有验证 `agg_result.status` 与正式 pointer**。

### 5.1 `compute_all_boards()` live pointer reconciliation

将「historically published」与「currently published pointer」分开判断：

- **情况 A（confirmed reuse）**：既有 batch `published_at is not None` + `status=="succeeded"`
  + 当前 `market_aggregation` pointer（`get_publication(scope=market, kind=market_aggregation)`
  的 `data_run_id`）**正确指向本 batch** + `source_core_run_id` 与当前 stock_core 一致
  → 幂等复用，不重算、不重新 publish；返回 `pointer_confirmed=True, pointer_status="confirmed"`。
- **情况 B（pointer recovery only）**：batch 已 `succeeded/published`，lineage 仍匹配当前
  stock_core，但 **live pointer 缺失**（如发布记录丢失）→ **只恢复 publication pointer**
  （`publish_market_aggregation` 指向既有 batch_run），**不重算 Board 数据**；
  遵守「pointer retry 不重算数据」合同；返回 `pointer_recovered=True, pointer_confirmed=True`。
- **情况 C（formal recompute）**：live pointer 指向旧 core / 错误 run（`data_run_id` 非本
  batch）或 lineage 与当前 stock_core 不一致 → **不得视为 current ready**，落入下方正式
  precompute + publication 路径按当前 lineage 重算并发布；不假绿。
- **withdrawal 语义**：Board / BoardAnalysisRun 无 intentional withdrawal 标记，按用户指示
  **不自行发明**；仅尊重 live `market_aggregation` pointer 与 `published_at` 标志。

### 5.2 orchestrator 如实映射

orchestrator 不再因 `compute_all_boards()` 未抛异常就写 `"succeeded"`：

```python
if _agg_batch_status == "succeeded" and _agg_pointer_confirmed:
    _aggregation_status = "succeeded"
elif _agg_batch_status in ("partial", "failed", "blocked_external_population"):
    _aggregation_status = _agg_batch_status        # 如实映射
elif _agg_batch_status == "succeeded" and not _agg_pointer_confirmed:
    _aggregation_status = "pointer_mismatch"        # 杜绝假绿
else:
    _aggregation_status = _agg_batch_status or "pointer_mismatch"
```

只有 `status=="succeeded"` **且** 正式 `market_aggregation` pointer 已确认属于当前
`snapshot_run_id`（lineage 一致）时，才置 `"succeeded"`；`partial / failed / blocked_external_population
/ pointer_mismatch` 必须如实映射，Review 前置条件 `aggregation_status == "succeeded"` 不满足，
Review 不执行。

### 5.3 Phase 4.4.1 变更文件

- `backend/app/services/board_analysis_service.py`（`compute_all_boards` reconciliation +
  导入 `SCOPE_TYPE_MARKET` + 返回体新增 `pointer_confirmed` / `pointer_status`）
- `backend/app/services/after_close_orchestrator.py`（`_aggregation_status` 如实映射 +
  注释）
- `backend/tests/test_board_aggregation_publication_unit.py`（3 项 reconciliation 单测：
  A 正确 pointer 复用、B pointer 缺失恢复、C pointer 指向旧 run 不假绿）
- `backend/tests/test_after_close_phase0_control_flow.py`（4 项 RB-01.1 测试：
  pointer mismatch / partial / failed → review 不执行；succeeded+confirmed → review 执行；
  同步将 aggregation spy 默认返回改为 truthful success 合同）

### 5.4 Phase 4.4.1 测试执行（`PURE_UNIT_TEST=1`，targeted）

- Ruff：`All checks passed!`
- `test_board_aggregation_publication_unit.py`：**15 passed**（含 3 新增）
- `test_after_close_phase0_control_flow.py`：**13 passed**（含 4 新增 RB-01.1）
- after-close + board publication 扩展回归（phase0_control_flow / board_publication /
  orchestrator / phase0_contracts / status_detail / worker / board_sync / idempotent_dsa_pipeline）：
  **74 passed, 51 skipped, 0 failed**
- 未跑 full PURE_UNIT（按审计测试纪律只跑 targeted）。

## 6. 限制与未完成

- 未部署，无 migration，未连接任何数据库；
- 远程隔离验证（`scripts/ops/panji-verify`）未获授权、未执行；
- 未运行 full PURE_UNIT 套件（按审计报告测试纪律，只跑 targeted）；
- 未修改 PRD / Maps / Runbooks（§4.2 授权门，待用户验收后单独授权）；
- 报告中 Phase 5 / Phase 7 未启动。
