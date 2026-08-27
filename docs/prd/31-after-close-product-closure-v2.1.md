# PRD 31 — 盘后数据生产与产品闭环总纲

状态：已确认
最后确认日期：2026-08-06
对应 Maps：`../maps/20-quant-model.md`、`../maps/30-after-close.md`、`../maps/70-review.md`、`../maps/75-auction-analysis.md`
需求所有权：盘后跨域依赖、canonical、publication lineage、产品 readiness 与闭环状态

## 0. 文档定位

本 PRD 是盘后跨域总纲，只定义多个领域之间必须一致的边界。领域细节继续由唯一所属 PRD 负责：

| 领域 | 权威 PRD |
|---|---|
| 行情周期、复权、数据来源与 readiness | [`10-market-data.md`](./10-market-data.md) |
| 第一金字塔、DSA、SMC、动量、筹码语义 | [`20-quant-model.md`](./20-quant-model.md) |
| 盘后触发、编排、恢复与父任务状态 | [`30-after-close.md`](./30-after-close.md) |
| 行情列表、个股详情和第一金字塔展示 | [`40-market-stock-experience.md`](./40-market-stock-experience.md) |
| Review P/Q/U/C/V、历史、归因、追踪与页面 | [`70-review.md`](./70-review.md) |
| 竞价真值、锚点、分析与发布 | [`75-auction-analysis.md`](./75-auction-analysis.md) |
| 本地开发、远程验证、稳定运行与部署 | [`80-system-runtime.md`](./80-system-runtime.md) |

本 PRD 不复制治理规则，不保存当前 SHA、运行进度、服务器身份、容器或数据库实例状态。实现状态和缺口写入 Maps，证据写入 Acceptance Matrix/Change，操作步骤写入 Runbooks。

本 PRD 也不覆盖盘中监控算法对齐、DSA 选股规则全量对齐或通用 15m/1h/daily 行情管理的全部细节；这些分别归属 PRD 50、PRD 20 和 PRD 10。

## 1. 最终业务链

> **[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] 冻结架构**：正常主链为
> `Core Compute Once → Core Ready（canonical 事实源）→ Review(X) → History(T) →
> post-core optional/compatibility`。`stock_core` FactorPublication 已不是
> Core→Review 主链的 mandatory step 或 readiness owner（publication 不是内部
> readiness owner）；它仅作为 legacy 兼容机制暂留。post-core 的 DSA compatibility、
> state_events、chip 均不阻断 Core / Review。

```text
市场事实
→ CoreRunContext
→ 第一金字塔 Core Compute Once
   ├─ Trend = DSA
   ├─ Structure = SMC
   └─ Momentum
→ CoreComputationArtifact
→ Core Ready Gate（canonical 唯一事实源：run 存在 AND id 匹配 AND trade_date==T
   AND status==succeeded；snapshot_run_id 非空 / publication / snapshot_error 均不构成就绪）
→ Review（source_core_run_id = X 显式绑定）
→ History(T)
→ post-core optional / compatibility
   ├─ DSA compatibility projection（同一 canonical DSA artifact，不重新计算；
   │  经统一执行器 optional=True；失败 → degraded/partial_success，不撤销 Core/Review）
   ├─ state_events（X）
   ├─ chip_consensus（daily + 15m 异步增强）
   └─ auction_anchor（legacy AuctionAnchor，DEPRECATED，见 [PRD75 §23](../prd/75-auction-analysis.md#23-legacy-auctionanchor-deprecation--migration-gap)；新 Auction 是次日 9:25 产品，不属于盘后编排节点）
   （legacy 兼容路径：stock_core publication 仅在此之后按需保留，非 mandatory 主链）
→ ProductReadinessService 动态聚合
→ 行情、详情、Review、竞价和管理后台消费正式结果
```

### PC-01 Canonical

- 个股核心唯一 canonical 是 Core compute 完成的第一金字塔事实（`StockFeatureSnapshotRun`
  status==succeeded / compute-complete，且由 `ReviewRun.source_core_run_id` 显式绑定）；
  **[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]** 正常 Core→Review 主链不再经
  `stock_core` FactorPublication pointer 解析。`stock_core` pointer 作为 legacy
  兼容机制可继续存在，但其发布/读取不再是 Core→Review 主链的 readiness 条件或同步机制。
- 第一金字塔 core 固定为 trend + structure + momentum。
- chip 是非破坏异步增强，不得写回或覆盖已发布的 Core 产物与 Review lineage。
- 正式消费者不得用 `max(created_at)`、最新成功行或父任务 metadata 代替 canonical run
  状态 / Core Ready 判定。Core Ready 唯一事实源为对真实 CoreRun 行的显式校验，
  不接受以 `snapshot_run_id 非空`、`publication 存在` 或 `snapshot_error is None` 代位。

### PC-02 Compute Once

- scheduled core 中 DSA、SMC、Bollinger、SQZMOM 和 VolumeContext 每股每 core run 各计算一次。
- scheduled DSA StrategyResult 只允许从持久化 core artifact 投影，禁止再次运行 StrategyRuntime。
- 第一金字塔趋势与 DSA projection 必须消费同一个 released DSA config、输入 hash 和算法版本。
- manual/replay DSA 可以使用明确指定的 StrategyVersion，但不得成为 scheduled stock_core 的 canonical 来源。

### PC-03 1d 与 15m 边界

- daily-core 和 Review 只消费日线及其派生 canonical facts，15m 读取次数必须为 0。
- chip 独立消费 daily + 15m；15m 不阻断 stock_core、board 或 Review。
- current 模式先执行 run 级有界并发 15m refresh，再冻结逐股 source cutoff/readiness，最后批量读取 canonical 15m 并计算；不得在计算循环中无界逐股重复刷新。
- historical replay 只允许使用覆盖目标日期的 point-in-time 已存 15m，禁止用当前刷新结果伪装历史事实。

## 2. 九节点注册表

节点名称、mandatory/enhancement 分类和 canonical 依据如下，后端 DTO、管理后台和验收矩阵必须引用同一注册表：

| 节点 | 分类 | Canonical/readiness 依据 | 失败影响 |
|---|---|---|---|
| `daily_facts` | mandatory input | 目标交易日日线 readiness | 不达标阻断 core |
| `board_facts` | mandatory input | 正式 board facts run/pointer 与 PIT membership | 不可用阻断对应聚合；复用必须显式 degraded |
| `stock_core` | mandatory product（legacy 兼容路径） | 正式 stock_core pointer（legacy，[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 不再阻塞 Review） | 失败则 legacy stock_core publication blocked；**不再阻断 Review / watchlist_ready** |
| `dsa_projection` | required compatibility output | 投影 run、source core、版本与 matched coverage | 不撤销 core，但形成 degraded/issue |
| `state_events` | enhancement | source core、事件生命周期与 coverage | 不阻断 board/Review |
| `chip_consensus` | enhancement | ChipConsensusRun、逐股 readiness、coverage 与正式 pointer | 不阻断 core/board/Review |
| `auction_anchor` | enhancement | AuctionAnchorRun、mode、coverage 与正式 pointer | 不阻断 Review |
| `auction`（新，未来节点） | 次日 9:25 竞价重新定价观测 | 非盘后编排节点；见 [PRD75](./75-auction-analysis.md) | 非本轮 P0 实现 |
| `board_aggregation` | mandatory product（[Slice 4A9] 已退役） | 正式 market aggregation pointer（READY 或 DEGRADED，见 PC-42） | **退役**，不再阻断 Review；新合同下 Review 经 `source_core_run_id` 直接绑定 Core |
| `market_review` | mandatory product | 正式 Review pointer 与发布质量门 | 失败保留旧 pointer |

每个节点至少返回：`status/readiness/mandatory/runId/publicationId/sourceRunIds/coverage/processed/total/heartbeat/lease/isStale/reasonCode/reasonText/recommendedAction`。

`market_review` 的正式业务结果包含 Review domain 内部的 scope observations、signals、discoveries、cross-scope relation evidence、attribution 和 tracking。这些属于 Review domain internal artifacts / read models，不要求独立 product node、独立 run、独立 publication 或独立 pointer。Discovery 的详细语义以 [`70-review.md`](./70-review.md) 为权威。

`dsa_projection` 节点独立表达的是 projection 的 persistence / lineage / retry / compatibility readiness，**不代表独立 DSA 业务计算**。它属于"required compatibility output"，与 chip / state_events / auction 这类真正的业务增强（enhancement）不是同一类：业务分类不同，但兼容投影失败仍可能使系统不是 `fully_ready`（老接口兼容链未闭合），此时 reason 应表达为 `compatibility output incomplete`，而不是 "DSA enhancement failed"。

不是所有节点都必须使用 `factor_publications`；领域 PRD 必须明确该节点的 canonical run/pointer/read model，禁止为统一表面形式制造无意义 pointer。

## 3. 运行对象与身份

### PC-10 CoreRunContext

run 开始时冻结：trade date、run mode、eligible universe/version、日线 cutoff、复权版本、DSA/SMC/momentum/volume config、算法版本和 parameter hash。单股不得自行重新解析配置。

### PC-11 CoreComputationArtifact

每股 artifact 至少保存 core snapshot、DSA projection payload/visual contract、state event candidates、field availability、input/bars/adjustment hashes、算法版本和 diagnostics。projection 和可重建增强只能消费该 artifact，不得重新计算 core。

### PC-12 ChipConsensusRun

ChipConsensusRun 是产品 lineage；SchedulerJobRun 只是调度、heartbeat、lease 和 retry 外壳。唯一身份必须覆盖：

```text
trade_date + run_mode + source_core_run_id + algorithm_version
+ config_hash + universe_version + input_contract_version
```

retry/resume 复用同一领域 run；新的输入、模式、算法或 config 必须产生新 run。逐股 run item 保存 refresh/readiness、input hash、source cutoff 和终态。

## 4. Chip 15m 合同

### PC-20 Refresh 阶段

scheduled/manual current 在 chip compute 前对 frozen universe 执行独立 refresh phase：有界并发、可恢复、逐股记录结果；refresh 结束后再由 MDAS 批量读取 canonical、completed、point-in-time 15m。refresh 和 compute 必须可分别诊断，不能以旧缓存静默掩盖刷新失败。

### PC-21 Readiness

逐股至少校验：时间戳可解析且无冲突、最新 session 日期等于 trade date、当日至少 16 根、最后一根不早于 15:00、历史至少 500 根、无 future data。16/15:00 是当日完整性，500 是算法历史门槛，不得混淆。

Canonical reason code：

| 条件 | reason code | 状态 |
|---|---|---|
| refresh/provider 失败 | `M15_REFRESH_FAILED` | unavailable |
| 无数据 | `M15_BARS_MISSING` | unavailable |
| 时间戳非法/冲突 | `M15_TIMESTAMP_INVALID` | failed |
| 最新交易日陈旧 | `M15_TRADE_DATE_STALE` | unavailable |
| 当日少于 16 根 | `M15_SESSION_INCOMPLETE` | unavailable |
| 缺 15:00 收盘 bar | `M15_CLOSE_BAR_MISSING` | unavailable |
| 历史少于 500 根 | `M15_BARS_INSUFFICIENT` | skipped |
| 存在 future data | `M15_FUTURE_DATA` | failed |

旧 reason code 只允许在输入 adapter 做兼容映射；API、数据库新记录和前端输出统一使用上述 canonical 值。算法、数据库、lease、fencing 和持久化异常一律 failed，不得伪装成数据不足。

## 5. Review 与竞价边界

### PC-30 Review

Review 的硬依赖只有**当前 Core（`snapshot_run_id` 绑定的 StockFeatureSnapshotRun）与历史 observations**。**[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]** Review 不再依赖 `stock_core` FactorPublication pointer 或 `market_aggregation` pointer 已发布；其 `source_core_run_id` 由 AfterClose 编排显式绑定 Core `snapshot_run_id`，而非经 pointer 解析。chip/auction 不进入 Review 唯一键、P/Q/U/C/V 或发布门；chip 晚到不改写 Review run/pointer。P/Q/U/C/V 公式、60 日边界、PIT membership、bootstrap、Discovery、Cross-Scope Relation、归因和 Discovery Workspace UI 只以 PRD 70 为权威。

### PC-31 Auction

> **DEPRECATED PRODUCT CONTRACT（2026-08-14，PRD75 §23）**：本条款描述旧 AuctionAnchor 产品（每股 anchor `structure_only | composite | unavailable | failed`，批次 mode `structure_only | hybrid | composite`）。该产品语义已被 [PRD75](./75-auction-analysis.md) 新 Auction（次日 9:25 Overnight Repricing Observation）取代，不再属于新 Auction P0 目标合同；代码迁移/删除由后续 Code Alignment Round 处理。

每股 anchor 为 `structure_only | composite | unavailable | failed`；批次 publication mode 为 `structure_only | hybrid | composite`。chip 部分可用必须形成 hybrid，不能冒充 composite。阈值配置化、版本化并写入 run；竞价真值双源和发布门以 PRD 75 为权威。

## 6. Publication 与 lineage

### PC-40 Pointer

- 所有正式读取只通过该领域规定的正式 pointer/read model。
- pointer 更新失败保留旧结果；published run 不原地修改；修正创建新 run 并 supersede。
- publication、run published 状态、pointer 和审计必须处于同一事务或具有等价原子性。

### PC-41 Lineage

```text
[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]
board.source_core_run_id == snapshot_run_id (retired, [Slice 4A9] board 退役)
review.source_core_run_id == snapshot_run_id   // Core 计算的 StockFeatureSnapshotRun.id，直接绑定，不再经 stock_core.pointer 解析
review.source_board_run_id == null             // board/market_aggregation 已退役，不再作为 Review lineage
chip.source_core_run_id == snapshot_run_id
auction.source_core_run_id == snapshot_run_id
auction.source_chip_run_id == chip_consensus.pointer.data_run_id or null
```

> **合同变更（[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]）**：Review（`review.source_core_run_id`）由 AfterClose 编排以显式 `snapshot_run_id`（Core `StockFeatureSnapshotRun.id`）直接绑定，**不再等于 `stock_core.pointer.data_run_id`**，也不经 `market_aggregation` pointer 解析。`stock_core` FactorPublication pointer 的发布/切换已不是 Core→Review 的业务依赖、readiness 条件或同步机制。`stock_core` publication service/schema 作为 legacy 兼容保留（KPI-10），不属于正常 Core→Review DAG。

> 上述 `auction.*` lineage 为**旧 AuctionAnchor 产品**的 lineage（DEPRECATED，见 PRD75 §23）。新 Auction（次日 9:25）的 lineage 以 [PRD75](./75-auction-analysis.md) §17 为准，不在此列。

### PC-42 Board aggregation degraded publication（CHANGE-20260809，Phase 4D.3）

`board_aggregation` 是 mandatory product，但 **MANDATORY 不等于 PERFECT**。其产品状态区分三档：

| 产品状态 | 条件 | 是否阻断 Review |
|---|---|---|
| `READY` | 正式 pointer 指向 `BoardAnalysisRun.status == succeeded` | 否 |
| `DEGRADED` | 正式 pointer 指向满足下述 DEGRADED PUBLISHABLE CONTRACT 的 `status == partial` run | **否** |
| `FAILED` / `BLOCKED` | 无合法正式 pointer，或 pointer 指向 failed / 非终态 run | **是** |

**DEGRADED PUBLISHABLE CONTRACT**：`BoardAnalysisRun.status == partial` 允许正式发布
`market_aggregation` pointer，当且仅当同时满足：

- **A** run 已 terminal
- **B** 所有 board_analysis 正式 in-scope board（industry + concept）都完成过计算并持久化 snapshot
- **C** 没有 execution failure
- **D** 没有 DB failure
- **E** 没有 contract violation
- **F** partial 原因仅属于已知 data completeness / member coverage degradation
- **G** 每个 partial board 都有真实的 `coverage_ratio` / `eligible_count` / `ready_count` /
  `missing_count` / `status`
- **H** 不存在 UNKNOWN failure

`degraded != failed`。**degraded pointer 仍然是正式的 `market_aggregation` pointer**（legacy 路径，[Slice 4A9] board 退役）。

> **[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]** PC-40/PC-41 重锚：Review 不再消费 `market_aggregation` pointer，也不设 `source_board_run_id`（恒为 null）；Review 通过 `source_core_run_id` 直接绑定 Core `snapshot_run_id`。

**禁止 Review 侧自行解析 stock_core / board source 作为前置依赖**：不得增加 Review-side stock_core pointer resolver、fallback、"latest partial run" 或任何绕过 `snapshot_run_id` 的路径。Review 接受的是 AfterClose 编排显式传入的 `source_core_run_id` 指向的 Core run，而不是「自己挑一个 pointer 或 partial run」。

### PC-43 Long-running step liveness contract（Phase 4D.4）

工作量会随 **instrument count / backfill window / provider throughput** 变化的 long-running step，
其业务成败**不得由单一 fixed generic absolute wall-clock 上限决定**。

- **long-running business step 的失败条件只限**：
  - 显式 execution failure（异常、DB 失败、contract violation）；
  - 正式运行治理认定的 **no-progress / stalled**（基于真实业务 progress signal，而非总耗时）；
  - 权威 PRD 明确写明的 **business deadline / cutoff**（仅当该 deadline 确实存在时）。
- **禁止**仅因 `total_elapsed > 固定 generic duration` 将仍在正常产生 valid progress 的 long-running business step 判为失败。
- 长任务必须存在**可观察的真实 progress signal**（chunk / batch / item 完成数 / checkpoint / last real progress），
  stall watchdog 的依据是 `now - last_real_progress`，不是 `total_elapsed`。
- heartbeat / lease 刷新**不得**被当作业务 progress 的唯一依据：当 step 处于 CPU-bound 或 blocking provider call
  而无法刷新真实 progress 时，heartbeat 单独存在不构成「有进展」。
- 本契约**不取消**基础设施命令超时（deploy / rsync / compose / verification gate / migration / 短基础设施操作），
  那些属于 Always-On Safety，与 long-running business batch 的 liveness 政策相互独立。
- 具体 stall 阈值、heartbeat interval、watchdog 实现属于 Rules / Config / Runbook / Code，**不在此 PRD 写实现数字**。

bars、adjustment factor 和 membership 不得读取目标交易日以后；Review history 只读目标日期以前；manual/replay 不污染 scheduled current pointer。

## 7. 父任务与产品闭环

### PC-50 状态分离

父 AfterCloseRun 终态、九节点 readiness、mandatory readiness、enhancement terminal 和整体 closure 必须独立表达。父任务 succeeded 不代表 chip 已完成，也不代表 fully ready。

### PC-51 Closure

节点按业务角色分三层，避免把兼容输出误当业务增强：

```text
mandatory products:
  stock_core / board_aggregation / market_review

required compatibility outputs:
  dsa_projection（仅对 canonical DSA artifact 做兼容投影，不代表独立 DSA 业务计算）

optional / asynchronous enhancements:
  state_events / chip_consensus / auction_anchor
```

业务分类改变不等于 readiness 结果改变：`dsa_projection` 兼容投影未闭合仍可使系统不是 `fully_ready`（老接口兼容链未闭合），但 reason 应表达为 `compatibility output incomplete`，而不是 "DSA enhancement failed"。

| closure | 定义 |
|---|---|
| `pending` | mandatory 链尚未形成 stock_core 或仍在运行 |
| `blocked` | stock_core 或 mandatory publication 失败 |
| `core_ready` | Core 计算完成（snapshot_run_id 可用），但 Review 尚未全部 ready |
| `mandatory_ready_enhancing` | mandatory 全部 ready，但仍有 active/stale 未对账增强任务 |
| `degraded_ready` | mandatory 全部 ready、增强均终结，但至少一个 required compatibility output（如 dsa_projection）或 enhancement 降级 |
| `fully_ready` | mandatory 全部 ready、required enhancement 全部达到正式 ready 条件且无 active/stale/unreconciled child |

`mandatoryProductsReady`、`enhancementJobsTerminal` 独立返回；兼容字段 `allProductsReady = (productionClosure == "fully_ready")`。不得把 `mandatory_ready_enhancing` 或 `degraded_ready` 显示为“全部产品已就绪”。

### PC-52 动态 Read Model

ProductReadinessService 动态读取九节点 canonical、领域 run、child jobs、heartbeat/lease、coverage、reason 和 lineage。父 metadata 只能保存 enqueue 当时快照，不能成为异步产品最终真源。缓存或派生快照必须可重建。

## 8. 编排与恢复

编排阶段固定为 `daily → core → post_core → board_review → finalize`，产品节点不等于编排阶段。**[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]** Core 计算完成后 AfterClose 编排直接以 `snapshot_run_id` 绑定 `ReviewRun.source_core_run_id` 进入 Review（computing_review），不再等待 `stock_core` / `market_aggregation` publication；`stock_core` publication 成功后应尽早并行创建 DSA projection、state events、chip 和 structure-only auction。

granular restart 边界：`daily_ready/core/dsa_projection/state_events/chip/auction/review`。`stock_core_published` / `board_aggregation` 已不再属于正常 Core→Review DAG 的 restart 边界（legacy 兼容保留）。恢复下游不得重算上游；chip 独立 retry；projection 只从 artifact 重建；force 创建新 run、保留历史、不绕过质量门并记录审计。

## 9. API 与用户状态

- 行情与详情只读正式 stock_core，并独立组装当前 chip 增强。
- Review 明确展示 core + aggregation lineage，并将 chip 标为 external enhancement。
- 竞价展示 publication mode、coverage、source core/chip runs 和原因。
- 管理后台展示父任务、九节点、closure、child job、heartbeat/lease 和可执行治理动作。
- 前端不得通过时间、颜色、空值或文案猜测 readiness。

## 10. 验收合同

### PC-60 Pure unit

覆盖 compute once、artifact encode/decode、DSA projection、chip reason mapping、closure 全状态、pointer/lineage 纯逻辑和前端 ViewModel。daily-core/Review 的 15m forbidden spy 命中即失败，不能只靠源码文本搜索。

### PC-61 Remote PG

同一 target SHA 在远程验证库完成 Migration cycle、PG Integration 和 Synthetic E2E：raw facts → core → publication → projections/enhancements → board → Review → chip late arrival → auction upgrade → readiness DTO。断言 fencing、幂等、失败回滚、PIT、无第二次 DSA 和正式读取只走 pointer。

### PC-62 Seed

Seed 只准备 instruments、raw bars、board/auction raw facts、历史 prerequisite 和受控故障输入。禁止直接写 succeeded/published、coverage=1、最终第一金字塔/Review payload、readiness 终态或固定成功失败计数。重复 seed 必须幂等，业务终态由真实 producer/Worker/质量门自然形成。

### PC-63 四类场景

| 场景 | 预期 closure |
|---|---|
| mandatory 尚未完成 | pending/core_ready |
| mandatory ready、chip 等增强仍运行 | mandatory_ready_enhancing |
| mandatory ready、required compatibility output 或增强终结但部分不可用 | degraded_ready |
| mandatory、required compatibility output 与增强全部完整 | fully_ready |

## 11. 完成定义

`code_ready`、`verification_deployed`、`pg_verified`、`verification_runtime_verified`、`stable_deployed`、`data_closed`、`browser_verified` 必须分别记录，互不推断。目标代码、验证运行时和稳定运行代码必须是同一已推送 SHA；纯证据文档提交使用独立 evidence SHA。

本 PRD 完成只表示目标合同已确认，不表示代码、Migration、远程验证、稳定部署、真实数据或浏览器验收已经完成。具体状态以 Maps 和 Acceptance Matrix 为准。
