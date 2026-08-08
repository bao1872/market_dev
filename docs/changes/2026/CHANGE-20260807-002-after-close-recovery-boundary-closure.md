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

### 2.3 重入安全性

`board_analysis_service.compute_all_boards` 内置 lineage 校验：按
`source_core_run_id`（取自 stock_core pointer）+ taxonomy / membership / algorithm_version
匹配既有 `BoardAnalysisRun`；命中且 `published_at is not None` 时直接幂等返回，
不重复聚合、不重复写库。因此「始终调用」是安全的，且与 lineage 唯一事实源一致。

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

## 5. 限制与未完成

- 未部署，无 migration，未连接任何数据库；
- 远程隔离验证（`scripts/ops/panji-verify`）未获授权、未执行；
- 未运行 full PURE_UNIT 套件（按审计报告测试纪律，只跑 targeted）；
- 未修改 PRD / Maps / Runbooks（§4.2 授权门，待用户验收后单独授权）；
- 报告中 Phase 7 未启动。
