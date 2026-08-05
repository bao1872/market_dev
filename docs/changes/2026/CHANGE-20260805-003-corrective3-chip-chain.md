# CHANGE-20260805-003：Corrective-3 — chip 真实业务链、统一 lineage、后端治理动作

- 日期：2026-08-05
- 类型：behavior + contract + docs
- 领域：ChipConsensusRun 生命周期、chip publication 编排顺序、ProductReadiness lineage、
  治理动作归属、测试分层重定义、验收矩阵纠偏
- 关联前序：`CHANGE-20260805-001-v21-development-chain.md`、
  `CHANGE-20260805-002-v21-dj-completion-pass.md`
- 基线：`94aa38eee1a9e89deeb364ae09677fe55df01740`（origin/dev）
- 收口 SHA：`abbd84500f94e7a165352d94825fe88222e8ab8a`

## 0. 为什么需要 Corrective-3

Completion Pass 1（`94aa38e`）声称 Commit D「已接入真实业务链」。复核代码后发现
该链路在生产上**不可能成功**，属于"看起来接上了、实际必然抛错并被静默吞掉"：

```python
# 94aa38e 中 worker.py 的实际写法（错误）
pub_result = await publish_chip_consensus(
    pub_db,
    trade_date=trade_date,
    core_run_id=core_run_id,   # ← 真实签名没有这个参数
    chip_run_id=None,          # ← 真实签名要求 uuid.UUID
    worker_id=f"...",          # ← 真实签名没有这个参数
)
pub_result.get("status")       # ← 返回的是 FactorPublication ORM，不是 dict
```

而 `publish_chip_consensus` 的真实契约是：

```python
async def publish_chip_consensus(
    session, *, trade_date, chip_run_id: uuid.UUID,
    algorithm_version: str, metadata: dict | None = None,
) -> FactorPublication:
    chip_run = await session.get(ChipConsensusRun, chip_run_id)
    if chip_run is None:
        raise ValueError(...)
```

**最根本的问题**：`after_close_chip_consensus_service` 只写
`StockChipConsensusSnapshot`，**从未向 `chip_consensus_runs` 表写入任何一行**。
即使签名正确，`session.get(ChipConsensusRun, ...)` 也永远为空。

结论：chip pointer 在任何交易日都未曾真正发布过，且因为 `except Exception: warning`
的软失败包装，运维侧没有任何可观测痕迹。

## 1. 行为变化

### 1.1 建立 ChipConsensusRun 生命周期（此前完全缺失）

新增 `app/services/chip_consensus_run_lifecycle.py`：

- `resolve_or_create_chip_run(...)`：在 chip job 领取时创建或解析**唯一**领域 run，
  固定 `id / trade_date / source_core_run_id / algorithm_version / status /
  expected_count / succeeded_count / failed_count / skipped_count /
  coverage_ratio / worker_id / lease_epoch`。
  解析优先级：job metadata 中的 `chip_run_id` → 同
  `(trade_date, source_core_run_id, algorithm_version)` 的未终结 run → 新建。
  **retry/resume 复用同一领域 run，禁止重复创建**；已完成进度不清零。
- `finalize_chip_run(...)`：计算结束后写终态，`coverage_ratio` 由真实计数推导
  （不接受调用方任意传值），并同步 `readiness`（ready/degraded/unavailable）。

`chip_run_id` 通过新增的 `fenced_job_run_service.merge_job_run_metadata`
固定进 `SchedulerJobRun.metadata_json`，恢复任务时复用。

### 1.2 修正执行顺序

```text
修改前：chip snapshots → auction anchor 重建 → （错误的）publish 调用（必然失败）
修改后：chip snapshots → ChipConsensusRun 终态 → publish_chip_consensus
        → commit publication pointer → generate_and_publish_auction_anchors
```

auction 升级**只在 chip pointer 成功发布之后**执行。发布失败时禁止触发
auction composite upgrade，避免产生无法追溯来源的复合锚点。

### 1.3 发布软失败可治理

`publish_chip_and_upgrade_auction` 返回 `ChipPublicationOutcome`，worker 将其
`to_metadata()` 写入 SchedulerJobRun：

```text
chip_publication_status    = succeeded | failed | skipped
chip_publication_error_code
chip_publication_error_message
chip_publication_retryable
chip_publication_id
```

`classify_publication_error` 区分可重试错误与 lineage 冲突类不可重试错误
（后者重试不会成功，必须人工介入）。

ProductReadiness 侧：`_chip_state` 在 chip run `succeeded` 但**无 publication
pointer** 时，不再返回 `ready`，而是返回
`degraded + stale + reason_code=CHIP_PUBLICATION_MISSING`，
后端解析出 `recommended_action=retry_chip_publication`。

### 1.4 lease fencing

发布前与 auction 前各做一次 `ownership_check`。失去租约则跳过全部写入，
返回 `skipped + CHIP_LEASE_LOST`。

## 2. ProductReadiness 统一 lineage

新增 `LINEAGE_KEYS`（18 键）。每个节点返回**全部键**，缺失值显式为 `None`
（键不得缺席，避免前端与审计侧出现"字段有时在有时不在"）：

```text
source_type / publication_id / pointer_data_run_id / domain_run_id /
parent_product / parent_run_id / source_core_run_id / source_board_run_id /
algorithm_version / parameter_hash / coverage / status / reason_code /
published_at / calculated_at / freshness / retryable / recommended_action
```

具体修正：

| 节点 | 修改前 | 修改后 |
|---|---|---|
| publication 类节点 | 仅读 `FactorPublication`，`source_core_run_id` 常为 None | 新增 `_load_domain_run` 按 `publication_kind` 联查领域 run，`source_core_run_id` 取领域 run 真实字段 |
| review | 仅看 `MarketReviewRun.status == 'published'` | 以正式发布指针（`published_at`）为准；run 自称 published 但 `published_at` 为空 → `degraded + REVIEW_NOT_PUBLISHED` |
| dsa_projection | 随 stock_core 自动 ready | `_count_dsa_projections` 核验 `stock_feature_snapshots` 真实行数；无产物 → `NO_PROJECTION` |
| state_events | 随 stock_core 自动 ready | `_count_state_events` 按 `event_type` 核验真实事件；无事件 → `NO_STATE_EVENTS` |
| chip | succeeded → ready | succeeded 但无 pointer → `degraded + CHIP_PUBLICATION_MISSING` |
| auction structure_only | 与 succeeded 同样 ready/fresh | `degraded + stale + AUCTION_STRUCTURE_ONLY`（体现等待 chip 升级） |
| auction 终态失败 | 无 lineage | 含 `run_id` / `reason_code` / `error_message` |
| pending 节点 | 无 reason_code | 一律给出 `NO_RUN` / `NO_PUBLICATION` / `NO_CHIP_RUN` 等 |

## 3. 治理动作归属后端（契约变化）

新增 `resolve_governance_action(reason_code, readiness) -> (retryable, action, operation)`，
作为治理动作的**唯一事实源**。

`ProductReadinessDTO` 新增一级字段：

```text
reasonCode / retryable / recommendedAction / operation / targetRunId
```

前端 `AdminReadinessWorkbench.tsx` **删除** `recommendedAction()`
（此前在前端用 if-else 猜测业务动作），改为 `ACTION_LABELS` 纯文案映射 +
`actionText()`。前端不再解释 reason code。

## 4. 测试分层重定义

| 文件 | 说明 |
|---|---|
| `test_v21_synthetic_e2e_pure.py` → `test_v21_readiness_auction_decision_integration.py` | 更名。原名声称 E2E，实际只组合 3 个决策纯函数，不经过任何编排路径 |
| `test_chip_worker_orchestration.py`（新增） | 调用**真实** `publish_chip_and_upgrade_auction` / `resolve_or_create_chip_run` / `finalize_chip_run`，注入 fake session 与 fake adapter。覆盖：真实 chip_run_id、algorithm_version、ORM 属性读取、publish→auction 顺序、失败不触发 auction、治理 metadata、retry 复用同一 run、lease 丢失阻断、coverage 推导 |
| `test_readiness_lineage_governance.py`（新增） | 覆盖 18 键完整性、`source_core_run_id` 非 None、chip publication 缺失可治理、auction structure_only/终态失败、pending reason_code、后端治理动作解析 |

## 5. 修改文件

```text
backend/app/services/chip_consensus_run_lifecycle.py      新增
backend/app/services/fenced_job_run_service.py            +merge_job_run_metadata
backend/app/worker.py                                     chip 链路重写
backend/app/services/product_readiness_service.py         统一 lineage + 治理动作
backend/app/schemas/product_readiness.py                  DTO 新增 5 字段
backend/app/api/admin_readiness.py                        透传治理字段
backend/tests/test_chip_worker_orchestration.py           新增
backend/tests/test_readiness_lineage_governance.py        新增
backend/tests/test_v21_readiness_auction_decision_integration.py  更名
frontend/src/api/endpoints.ts                             类型同步
frontend/src/features/product-readiness/AdminReadinessWorkbench.tsx  移除前端猜测
docs/changes/2026/PRD-Acceptance-Matrix-V2.1-D-J-2026-08-05.md      重写
docs/changes/2026/CHANGE-20260805-002-v21-dj-completion-pass.md     加更正声明
```

## 6. 验证状态（如实）

本轮受 Corrective-3 §一执行边界约束，**本地未执行任何** py_compile / Ruff /
Mypy / pytest / TSC / ESLint / build / migration / 数据库连接。
全部验证在远程隔离 worktree 精确检出 `f1612f6` 后执行。

```text
remote_static_verified          = true    # Ruff All checks passed；Mypy 改动文件零错误
remote_unit_verified            = true    # PURE_UNIT_TEST 52 passed，postgres=0
remote_frontend_build_verified  = true    # TSC 0 / ESLint 0 errors / vite build ✓
pg_tested                       = false
deployed                        = false
runtime_verified                = false
data_closed                     = false
browser_verified                = false
```

验证方式：`git worktree add --detach /root/corrective3_verify f1612f6`，
**未触碰运行中的部署**（部署树保持 `6f008ca`、工作树干净、15 个容器全程运行），
未连接 PG，未执行 migration，未中断 worker。前端复用
`/root/web_dev/frontend/node_modules`（package.json 校验一致，只读软链，验证后移除）。

### Mypy 对缺陷的独立佐证

在基线 `94aa38e` 上 `mypy app/worker.py` 输出 50 个错误，其中 4 个精确对应本次修复：

```text
worker.py:1832 Unexpected keyword argument "core_run_id" for "publish_chip_consensus"
worker.py:1832 Unexpected keyword argument "worker_id" for "publish_chip_consensus"
worker.py:1836 Argument "chip_run_id" has incompatible type "None"; expected "UUID"
worker.py:1844 "FactorPublication" has no attribute "get"
```

Corrective-3 后降至 46 个，上述 4 项全部消失，`app/worker.py` 自身零错误；
剩余 46 个分布于未改动文件（既有问题，未扩大范围处理）。

## 7. 已知限制

1. `chip_consensus_runs` 表历史无数据。本次只保证**新执行**的 chip 任务建立领域 run，
   不回填历史交易日。历史日期的 chip 节点将显示 `NO_CHIP_RUN`。
2. `_count_dsa_projections` 以 `stock_feature_snapshots` 当日行数作为 DSA 投影
   产物证据（投影随特征快照落库，无独立投影表）。若后续拆出独立投影表，需同步调整。
3. `_load_domain_run` 在领域 run 查询异常时静默返回 None，以避免阻断 readiness 评估；
   此时 lineage 的领域字段为 None，不会伪造数值。
