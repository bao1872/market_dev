# PRD 验收矩阵 — V2.1 开发链 Commit D–J

## SHA 谱系（[Corrective-3 §六] 必须完整记录）

| 阶段 | SHA |
|---|---|
| D–J 原始开发基线 | `2267d43` |
| D–J 初次收口 | `5df542d` |
| Completion Pass 1 | `94aa38e` |
| Corrective-3 | `<pending_commit>`（提交后回填） |

**生成日期**: 2026-08-05
**当前判断（Corrective-3 代码提交后、远程验证前）**：

```text
development_chain_D_to_J = partial
remote_static_verified   = false
remote_unit_verified     = false
remote_frontend_build_verified = false
pg_tested        = false
deployed         = false
runtime_verified = false
data_closed      = false
browser_verified = false
```

> **诚实声明**：本文件在 Completion Pass 1 中曾出现两类不实标注，均已删除：
> 1. 多行标注 `remote_static_verified` / `remote_unit_verified`，但从未在远程精确检出
>    SHA 后执行过任何检查；
> 2. Commit I 被称为 `real_e2e`，实际只组合了三个决策纯函数，不经过 worker、
>    publication adapter 或任何真实编排路径。
>
> Corrective-3 已按证据重置：所有 `remote_*` 标记在远程验证输出产生前一律为 `false`。
> 本轮**未在本地执行任何 Ruff / Mypy / pytest / TSC / ESLint / build**（受
> Corrective-3 §一执行边界约束），因此也不存在 `local_*_verified` 声明。

---

## 证据等级

每条需求记录其最高已达成的离散等级（禁止用单个 ✅ 混合代表不同层级）：

- `authored`：代码/测试/文档已编写，未验证
- `implemented`：有实现，未经本轮验证
- `remote_static_verified`：远程精确检出 SHA 后 Ruff + Mypy 通过（**本轮 false**）
- `remote_unit_verified`：远程 PURE_UNIT_TEST 通过（**本轮 false**）
- `remote_frontend_build_verified`：远程 TSC + ESLint + build 通过（**本轮 false**）
- `pg_tested`：PG 集成测试通过（**本轮 deferred，禁止执行**）
- `deployment_pending` / `data_validation_pending` / `browser_pending`：未执行

---

## Corrective-3 修复的 Commit D 真实缺陷

Completion Pass 1 声称"D 已接入生产链"，但代码证据显示该链路在生产上**必然失败**：

| # | 缺陷 | 证据 | Corrective-3 修复 |
|---|---|---|---|
| D-1 | **没有任何生产路径创建 `ChipConsensusRun`** | `after_close_chip_consensus_service` 只写 `StockChipConsensusSnapshot`，`chip_consensus_runs` 表从未被写入 | 新增 `chip_consensus_run_lifecycle.resolve_or_create_chip_run` / `finalize_chip_run`，在 worker 领取任务时建立、计算结束后写终态 |
| D-2 | `chip_run_id=None` 调用发布 | `publish_chip_consensus` 内部 `session.get(ChipConsensusRun, chip_run_id)` 必然为空 → `ValueError` | worker 传入真实 `chip_run_id` |
| D-3 | **调用签名完全错误** | worker 传了不存在的 `core_run_id=` / `worker_id=`，真实签名为 `(session, trade_date, chip_run_id, algorithm_version, metadata)` | 按真实签名调用 |
| D-4 | **把 ORM 当 dict 读** | `pub_result.get("status")`，而返回值是 `FactorPublication` → `AttributeError` | 改为 `pub.id` / `pub.data_run_id` / `pub.publication_kind` |
| D-5 | **执行顺序颠倒** | auction anchor 重建在 chip pointer 发布**之前**，auction 永远看不到当次 pointer | 改为 `chip 终态 → publish → commit → auction upgrade` |
| D-6 | **软失败不可治理** | 发布失败只 `logger.warning`，无任何持久化痕迹 | 写入 SchedulerJobRun metadata：`chip_publication_status/error_code/error_message/retryable` |
| D-7 | retry 可能重复建领域 run | 无 run id 固定机制 | `chip_run_id` 固定进 job metadata，resume/retry 复用 |
| D-8 | lease 丢失后仍可能写入 | 无 fencing 检查 | 发布前/auction 前双重 `ownership_check` |

---

## Commit D — Chip、State Event、Auction

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| ChipConsensusRun 生命周期（创建/复用/终态） | `chip_consensus_run_lifecycle.resolve_or_create_chip_run` / `finalize_chip_run`；`worker.py` 接入 | `test_chip_worker_orchestration.py` | authored | implemented（Corrective-3 新增，此前完全缺失） |
| chip run 完成路径接入发布 pointer | `worker.py` `_chip_consensus_poll_once` → `publish_chip_and_upgrade_auction` | 同上 | authored | implemented（Corrective-3 修复签名与顺序） |
| publisher 按真实签名调用并读 ORM | `chip_consensus_run_lifecycle.publish_chip_and_upgrade_auction` | 同上 | authored | implemented |
| publish → auction 顺序 | 同上 | 同上（顺序断言） | authored | implemented |
| publication 软失败可治理 | `ChipPublicationOutcome.to_metadata` + `merge_job_run_metadata` | 同上 | authored | implemented |
| chip publication/pointer/lineage 校验链 | `factor_publication_service.publish_chip_consensus`（既有） | `test_chip_publication_unit.py` | implemented | implemented |
| state event candidate → confirmed | 真实产物核验（`_state_events_state`） | `test_readiness_lineage_governance.py` | authored | implemented（不再随 stock_core 自动 ready） |
| structure-only auction | `auction_anchor_service.generate_and_publish_auction_anchors` | — | implemented | implemented |
| chip 到达后 hybrid/composite 升级 | 模式决策 + 发布后升级 | `test_v21_readiness_auction_decision_integration.py` | implemented | implemented |
| 晚到/重试/幂等/恢复 | `on_conflict_do_update` 幂等 + run 复用 | `test_chip_worker_orchestration.py` | authored | implemented |
| lease 丢失禁止写入 | `ownership_check` fencing | 同上 | authored | implemented |

## Commit E — Board Aggregation

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| 基于精确 stock_core publication | `publish_market_aggregation`（既有） | `test_board_aggregation_publication_unit.py` | implemented | implemented |
| 同一 Board Facts taxonomy/member version | 批处理标识（既有） | 同上 | implemented | implemented |
| industry L1/L2/L3 与 concept 分开 | taxonomy 合同（Commit A） | 同上 | implemented | implemented |
| exact lineage | board run 与 stock_core pointer 同源 | 同上 | implemented | implemented |
| 缺板块/stale/partial/reuse 路径 | 同上 | 同上 | implemented | implemented |

> Corrective-3 未修改 E 的生产实现；此前标注的 `remote_*_verified` 已删除。

## Commit F — Review V2.1

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| Review 只依赖 stock_core + board aggregation | `review_orchestrator_service._resolve_source_run_ids` | `test_review_v21_dependency_contract.py` | implemented | implemented |
| 不等待 chip / 不等待 auction | 创建阶段禁止其他 kind | 同上 | implemented | implemented |
| Review 就绪以正式发布指针为准 | `_review_state` 检查 `published_at`（pointer 写入时间） | `test_readiness_lineage_governance.py` | authored | implemented（Corrective-3 修复：此前仅看 run.status） |
| publication 和 pointer | `review_publication_service` | 同上 | implemented | implemented |
| consumer 只读发布结果 | `get_published_review_run_id` | 同上 | implemented | implemented |

## Commit G — ProductReadiness 与治理 API

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| 九节点状态 | `ProductReadinessService.collect_states` | `test_governance_report_unit.py` | implemented | implemented |
| terminal 与 consumable 分离 | `ProductReadinessState` | 同上 | implemented | implemented |
| 闭包状态机 | `evaluate_closure` | 同上 | implemented | implemented |
| **统一 lineage 结构（18 键，缺失显式 None）** | `LINEAGE_KEYS` + `_product_lineage` + `_publication_lineage` | `test_readiness_lineage_governance.py` | authored | implemented（Corrective-3） |
| publication 节点与领域 run 联查 | `_load_domain_run` + `_publication_lineage` | 同上 | authored | implemented |
| `source_core_run_id` 不得默认 None | 同上 | 同上 | authored | implemented |
| DSA projection 检查真实产物 | `_dsa_projection_state` + `_count_dsa_projections` | 同上 | authored | implemented（此前随 stock_core 自动 ready） |
| state_events 检查真实事件 | `_state_events_state` + `_count_state_events` | 同上 | authored | implemented（同上） |
| chip run 成功但 publication 缺失 → degraded | `_chip_state` → `CHIP_PUBLICATION_MISSING` | 同上 | authored | implemented |
| auction structure_only 体现等待升级 | `_auction_state` → `AUCTION_STRUCTURE_ONLY` + stale | 同上 | authored | implemented |
| auction terminal failure 含 run_id/reason | 同上 | 同上 | authored | implemented |
| pending 节点必给 reason_code | 各 `_*_state` 分支 | 同上 | authored | implemented |
| **治理动作由后端输出** | `resolve_governance_action` + DTO `retryable/recommendedAction/operation/targetRunId` | 同上 | authored | implemented（Corrective-3 §四） |

## Commit H — 前端

| Requirement | 实现文件/函数 | 证据等级 | 状态 |
|---|---|---|---|
| Admin 盘后工作台 | `AdminReadinessWorkbench.tsx` | authored | implemented |
| 展示统一 lineage（跳过 null） | 同上 | authored | implemented |
| **删除前端自行猜测业务动作** | 移除 `recommendedAction()`，改为 `actionText()` 纯文案映射 | authored | implemented（Corrective-3 §四） |
| 展示后端 reasonCode / recommendedAction / targetRunId | 同上 | authored | implemented |
| TSC / ESLint / build | — | `remote_frontend_build_verified = false` | pending（远程验证） |

## Commit I — 测试分层（[Corrective-3 §五] 重新定义）

| Requirement | 实现文件/函数 | 测试 | 证据等级 | 状态 |
|---|---|---|---|---|
| **决策函数集成测试**（非 E2E） | `evaluate_closure` + `evaluate_governance` + `decide_auction_mode` | `test_v21_readiness_auction_decision_integration.py`（由 `test_v21_synthetic_e2e_pure.py` 更名） | authored | implemented（更名以如实反映范围） |
| **worker 编排服务级测试** | 真实 `publish_chip_and_upgrade_auction` + fake session/adapter | `test_chip_worker_orchestration.py` | authored | implemented（Corrective-3 新增） |
| 调用顺序 / publication ID / retry 不重复发布 | 同上 | 同上 | authored | implemented |
| soft-failure metadata / late chip 升级 | 同上 | 同上 | authored | implemented |
| lineage 与治理动作测试 | `_product_lineage` / `resolve_governance_action` | `test_readiness_lineage_governance.py` | authored | implemented |
| PG 依赖项目 | `test_v21_synthetic_e2e_pg.py` | — | `authored_not_executed`（pg_gate deferred） | 不阻塞 |

## Commit J — 文档与运行手册

| Delivery | 文件 | 证据等级 | 状态 |
|---|---|---|---|
| Maps | `maps/30-after-close.md` §13、`maps/70-review.md` §26、`maps/75-auction-analysis.md` §10、`maps/80-system-runtime.md` | implemented | done |
| Change（Corrective-3） | `docs/changes/2026/CHANGE-20260805-003-corrective3-chip-chain.md` | authored | done |
| Acceptance Matrix | 本文件 | authored | done |
| Migration 085 状态 | `085_board_definition_identity_contract.py`（authored，未 apply） | authored | done |

---

## 剩余阻塞 / 未验证项

1. **远程验证未执行**：Ruff / Mypy / PURE_UNIT_TEST / TSC / ESLint / build
   必须在远程 `/root/web_dev` 精确检出 Corrective-3 SHA 后运行。在此之前
   所有 `remote_*` 标记保持 `false`。
2. PG 集成（chip/board/review/auction 落库全链路）——本轮明令禁止，deferred。
3. Migration 085 apply——未授权。
4. 真实全市场任务、生产部署、浏览器验收——未授权。
5. `ChipConsensusRun` 表此前从无生产写入，历史交易日不存在领域 run 记录；
   Corrective-3 只保证**新执行**的 chip 任务会建立领域 run，不回填历史。
