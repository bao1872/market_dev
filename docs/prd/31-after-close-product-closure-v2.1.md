# PRD 31 — 盘后数据生产、第一金字塔与复盘业务链（V2.1 产品闭环）

> **文档性质**：V2.1 盘后生产链、第一金字塔、DSA 兼容投影、chip 异步增强、板块市场聚合、市场复盘 Review、次日竞价锚点、产品 readiness 与管理后台的**正式业务架构、数据合同、状态语义与端到端验收事实源**。
> **适用分支**：`dev`。**环境定位**：本地开发与质量验证；部署、Migration、运行时任务、真实数据闭环和最终浏览器验收在远程开发运行服务器（`panji-prod`）执行。
> **产品边界**：不预测涨跌、不承诺收益、不替用户决策。
>
> **吸收来源说明**：本 PRD 吸收 `ref/instruction.md`（参考源，仅人工阅读，非正式真源）中已确认的需求与决策，并按产品图 / 九节点 readiness / closure 定义 / Granular restart 正式枚举 / P1-3 readiness 完整性重新组织。凡引用 `ref/instruction.md` 处仅为历史溯源，不视为运行依赖。

## 0. 文档优先级与不变量

发生冲突时优先级：`本 PRD 31` > 对应领域 Map > Acceptance Matrix > Change > Runbook > 历史注释。
Change 不得替代当前 Map；测试文件存在不得替代行为证据。

以下状态必须独立表达，不得互相推断：`code_ready`、`remote_deployed`、`runtime_verified`、`data_closed`、`browser_verified`、`mandatory_products_ready`、`enhancement_jobs_terminal`、`production_closure`。

不变量：`remote_deployed != runtime_verified`、`runtime_verified != data_closed`、`data_closed != browser_verified`、`main_run_succeeded != fully_ready`、`HTTP_200 != browser_verified`、`degraded_ready != fully_ready`。

## 1. 产品图

```text
daily_facts
    ↓
board_facts
    ↓
stock_core
    ├── dsa_projection
    ├── state_events
    ├── chip
    │     └── auction upgrade
    └── board_aggregation
            └── review
```

说明：

- `daily_facts`：刷新目标交易日日线、日线覆盖率与市场数据门禁。
- `board_facts`：板块/概念源事实（行业 L1/L2/L3 与 concept 分离），只消费正式 stock_core 之前的外部源，不重算 daily。
- `stock_core`：个股核心状态唯一 canonical 正式事实。`FirstPyramidCoreSnapshot`（trend+structure+momentum）+ DSA artifact，原子发布。
- `dsa_projection`：第一金字塔趋势 artifact 的兼容投影，仅从持久化 core artifact 重建，禁止第二次 DSA 计算。
- `state_events`：stock_core 派生增强，从当前 core artifact 重建 events。
- `chip`：独立、非破坏、可恢复的异步增强，在 stock_core 原子发布后创建。
- `auction`：structure-only / hybrid / composite 三种 publication，使用当前 core/chip pointer 重建 anchor。
- `board_aggregation`：使用正式 core + board facts 重建 aggregation。
- `review`：使用正式 core + aggregation 重建 Review，不等待 chip、不消费 auction 作为输入。

## 2. 九节点 readiness 定义

管理后台数据生产中心展示九个节点。每个产品节点至少定义：**mandatory / enhancement**、**terminal**、**consumable**、**freshness**、**coverage**、**lineage**、**失败与降级语义**。

| 节点 | mandatory/enhancement | terminal | consumable | freshness | coverage | lineage | 失败/降级 |
|---|---|---|---|---|---|---|---|
| daily_facts | mandatory | 日线覆盖率达标或门禁失败 | board_facts/stock_core 输入 | 目标交易日当日 | 覆盖率≥阈值 | 目标交易日 | 不达标→blocked |
| board_facts | mandatory | 行业/概念源拉取完成或失败 | stock_core 不消费；board_aggregation 消费 | 目标交易日 | 行业覆盖≥99%、depth≤3、概念≤100 | 源版本 | 失败→reuse 旧值并标记 reused，不静默截断 |
| stock_core | mandatory | 原子发布成功/失败 | 所有下游唯一 canon | run_calculated_at | 成功 coverage | CoreRunContext | 失败→blocked |
| dsa_projection | enhancement（V2.1 默认 required_compatibility） | 投影成功/失败 | DSA 兼容 API | 同 core run | matched==eligible | source_core_run_id + 参数hash | 失败→父任务 partial_success，readiness=failed，可从 core artifact 重建 |
| state_events | enhancement | events 完整/失败 | 详情/导出 | 同 core run | 事件生命周期完整 | source_core_run_id | 失败→readiness partial/failed，可重建 |
| chip | enhancement | ready/partial/skipped/unavailable/failed/stale | 第一金字塔增强、auction 升级 | 发布时间 | eligible/ready/partial 统计 | ChipConsensusRun.id | 失败不影响 core；partial→degraded |
| auction | enhancement | structure_only/hybrid/composite/unavailable/failed | 竞价页面 | 发布时间 | chip_ready_coverage | source_core_run_id + source_chip_run_id? | 失败不阻断 Review |
| board_aggregation | mandatory | 聚合发布成功/失败 | Review 唯一输入 | 同 core run | 行业/概念分布完整 | source_core_run_id | 失败→阻断当次 Review，不撤销 core |
| review | mandatory | Review 质量门通过/失败 | Review 页面、导出 | 同 core run | P/Q/U/C/V 非全空、历史门槛 | source_core_run_id + source_board_run_id | 失败保留旧 pointer |

**ready 判定语义**：

- `is_terminal`：节点达到可解释终态（含 failed/partial/unavailable/skipped），非 active/running/stale。
- `is_truly_ready`：mandatory 节点要求 fresh 且通过质量门；enhancement 节点要求 terminal 且达到产品定义的 ready 条件（见 §5 P1-3）。
- `auction_composite`：仅当 auction 节点 `is_terminal` 且其 `auction_mode == "composite"` 时为 True；非 terminal 时默认为 True（不阻塞），terminal 但非 composite 时阻塞 fully_ready。

## 3. closure 定义

`production_closure` 取值：

| 状态 | 定义 |
|---|---|
| `pending` | 必选链仍在运行或尚未开始 |
| `blocked` | stock_core 未形成或正式 publication 失败 |
| `core_ready` | stock_core 已发布，但 board/Review 未全部完成 |
| `degraded_ready` | 必选链 ready，增强链均已终结，但至少一个增强为 partial/skipped/unavailable/failed |
| `fully_ready` | 必选链 ready，所有当前 required 增强产品完整 ready，不存在降级和未对账任务 |

`fully_ready` 必须同时满足：

- mandatory 全部 fresh（stock_core / board_aggregation / review 已发布且有效）；
- chip 真正 ready（not partial/skipped/unavailable/failed/stale）；
- state_events 真正 ready（完整生命周期与 coverage，非仅一条记录）；
- dsa_projection 完整（matched_count == eligible_count 或达正式 coverage 门槛，参数 hash / 算法版本 / source core run 一致）；
- auction 为 `composite`；
- 没有 active/stale/unreconciled child。

兼容字段：`allProductsReady = (productionClosure == "fully_ready")`；不得把 `degraded_ready` 显示为"全部产品已就绪"。

## 4. 总体目标与正式决策（吸收 instruction 已确认项）

单向事实链：`市场事实 → CoreRunContext → 个股核心 Compute Once → stock_core canonical → 并行数据产品（DSA 投影 / chip / state events / structure auction / board→Review）→ chip 晚到增强 → ProductReadinessService 聚合 → 用户查询与管理验收`。

正式业务决策（已确认，不回退）：

- 个股核心 canonical = `stock_core`；第一金字塔 core = trend+structure+momentum；chip = 第一金字塔异步增强，不属于 core。
- DSA 业务身份 = 第一金字塔趋势算法；scheduled DSA StrategyResult = 统一 artifact 的兼容投影；**scheduled after-close 不再二次计算 DSA**。
- chip 不阻断 stock_core / board / Review；chip 不进入 Review 唯一键；chip 晚到不自动重算 Review；auction 不阻断 Review。
- chip 启动时机：stock_core 原子发布成功后立即创建 child run；父任务 succeeded 不代表 chip 完成。
- auction 批次模式：`structure_only | hybrid | composite`；每只股票 own anchor mode。
- `allProductsReady` 仅作为 `productionClosure == fully_ready` 的兼容派生布尔。

禁止模式（scheduled after-close）：第一金字塔先算 DSA 后 StrategyRuntime 再算 DSA；core 与 projection 使用不同配置；从"最新成功行"隐式选择正式数据；chip 写回/覆盖/原地修改 stock_core；chip 晚到原地修改已发布 Review；board/Review 直接选 max(created_at)；前端通过时间/颜色/文案/空值推断业务状态；用父任务 metadata 作为异步 child job 最终事实源；用单一 succeeded 表示全部产品完整就绪。

## 5. Readiness P1-3 完整性（补齐项）

DSA projection 与 state_events 的 readiness **不得只做存在性检查**，必须验证完整条件，不满足时返回 `pending / degraded / unavailable`，不得 ready。

**DSA projection 必须验证**：

- `eligible_count`：本次 core run 应投影股票数（来自 CoreRunContext eligible universe）；
- `matched_count`：实际投影成功数；
- `coverage = matched_count / eligible_count`，达到正式 coverage 门槛（默认 1.0，可配置化）；
- `algorithm_version`、`parameter_hash`、`source_core_run_id` 与 core run 一致；
- 参数 hash、算法版本、source core run 任一不一致 → `degraded` / `unavailable`，不 ready。

**State events 必须验证**：

- 事件生命周期完整：每个 instrument 的 required event_type 均有对应 `event_identity`；
- `coverage`：已生成事件数 / eligible 数达到门槛；
- `source_core_run_id` 绑定一致；
- 仅有孤立一条记录但缺 lifecycle → `degraded`，不 ready。

**auction 必须验证**：`total_core_count`、`chip_eligible_count`、`chip_ready_count`、`chip_ready_coverage`；composite 要求 `chip_ready_coverage >= composite_threshold`（默认 0.98，配置化+版本化+写入 run）。

## 6. Granular restart 正式枚举

`restart_from` 枚举（用户计划推荐使用，替换原 `board` 歧义边界）：

| Boundary | 输入 pointer | 允许重算内容 | 禁止重算内容 | 新建 run | 幂等 key | 发布 | 下游影响 |
|---|---|---|---|---|---|---|---|
| `daily_ready` | 已有日线 | 从 core 链开始（daily 跳过） | 不重跑 daily_facts | 新 StockFeatureSnapshotRun | trade_date+context hash | 原子发布 stock_core | 触发下游全部 |
| `board_facts` | 当前 daily + 外部源 | 只重跑 Board Facts | 不重算 daily | 新 BoardAnalysisRun（facts 段） | trade_date+source hash | 更新 board_facts pointer | board_aggregation 可重建 |
| `core` | 当前 daily_facts | 新建 core run，计算趋势/结构/动量 | 不重算 daily | 新 StockFeatureSnapshotRun | trade_date+context hash | 原子发布 stock_core | 下游全部重建 |
| `stock_core_published` | 已通过门禁的 core run | 重试 publication | 不重算 core | 复用原 run | run_id | 切换 stock_core pointer | 下游重建 |
| `dsa_projection` | 持久化 core artifact | 从 artifact 重建投影 | 禁止再次运行 DSA | 新 StrategyRun | source_core_run_id+hash | 发布 dsa_projection | dsa 兼容 API |
| `state_events` | 当前 core artifact | 重建 events | 不重算 core | 新 events run | source_core_run_id | 发布 state_events | 详情/导出 |
| `chip` | 当前 core pointer | 创建/恢复 chip domain run | 不重算 core | ChipConsensusRun | trade_date+source_core_run_id+algo+hash | 原子发布 chip_consensus | 第一金字塔增强、auction 升级 |
| `auction` | 当前 core/chip pointer | 重建 anchor | 不重算 core/chip | AuctionAnchorRun | source_core_run_id+source_chip_run_id?+mode | 原子切换 auction pointer | 竞价页面 |
| `board_aggregation` | 正式 core + board facts | 重建 aggregation | 不重算 core/daily | BoardAnalysisRun | source_core_run_id | 发布 market_aggregation | Review 重建 |
| `review` | 正式 core + aggregation | 重建 Review | 不重算 core/board | MarketReviewRun | source_core_run_id+source_board_run_id+review_algo | 原子发布 market_review | Review 页面 |

每个 restart 都必须具备：Admin API、`SchedulerJobRun`、`parent_job_run_id`、`operation`、`target_run_id`、幂等 key、权限检查、事件时间线、单元测试、PG 集成测试、前端按钮及反馈。**不允许任何 boundary 只接受枚举然后返回 `not_implemented`（501）**。

## 7. 运行对象与状态机（吸收 instruction §3.4 / §13）

运行对象：AfterCloseRun（编排必选链并可靠启动增强 child runs）、StockFeatureSnapshotRun、StrategyRun(dsa_selector)、ChipConsensusRun、SchedulerJobRun、BoardAnalysisRun、MarketReviewRun、AuctionAnchorRun。

五个编排阶段：`daily / core / post_core / board_review / finalize`；产品节点单独展示（daily_facts、stock_core、dsa_projection、state_events、chip_consensus、auction_anchor、board_aggregation、market_review）。

父任务终态：`succeeded`（必选链成功、required child 已可靠创建）/ `partial_success`（必选链成功但某增强失败/不可用）/ `failed` / `cancelled` / `interrupted`。child chip running 时父可 succeeded；child 后续失败不回写父历史终态；ProductReadinessService 反映后续实际闭环。

通用治理：cancel（已发布不回滚，child 按策略处理）、reconcile（依据 worker/heartbeat/lease/run items/pointer/child jobs 对账，不唯父 status）、restart（上述枚举）、force（full rerun，新 run+supersede，不绕过质量门，需审计二次确认）。

## 8. Publication 与 lineage 不变量（吸收 instruction §15）

`publication_kind`：stock_core / dsa_projection / chip_consensus / market_aggregation / market_review / auction_anchor。

lineage 不变量：

- `board.source_core_run_id == stock_core.pointer.data_run_id`
- `review.source_core_run_id == stock_core.pointer.data_run_id`
- `review.source_board_run_id == market_aggregation.pointer.data_run_id`
- `chip.source_core_run_id == stock_core.pointer.data_run_id`
- `auction.source_core_run_id == stock_core.pointer.data_run_id`
- `auction.source_chip_run_id == chip_consensus.pointer.data_run_id or null`

pointer 规则：所有消费者只读正式 pointer；不直接 max(created_at)；pointer 更新失败保留旧结果；published 数据不可原地覆盖；修正必须新 run + 新 publication。

point-in-time 不变量：bars/adj factor 不读目标交易日以后；Review history 只读目标日期之前；replay/manual run 不污染 scheduled current pointer。

## 9. API 与前端合同（吸收 instruction §16，逐页）

页面与必须验证的合同：

| 页面 | 合同 |
|---|---|
| Admin Data Production | 九节点、closure、lineage、heartbeat、lease、child jobs |
| Admin Tasks | cancel / reconcile / resume / full restart / granular restart（全部 boundary） |
| Market | 只读正式 stock_core pointer |
| Stock Detail | source run、freshness、chip 状态、null 原因 |
| Review | 不等待 chip，显示 core+aggregation lineage；chip 为 external enhancement |
| Auction | structure-only / hybrid / composite |
| Board | 行业 L1/L2/L3 与 concept 分离 |
| 错误状态 | loading / null / degraded / failed / retryable |

前端不得自己推断业务动作，只展示后端返回的 `operation` 和 `recommendedAction`。业务错误返回 `stable_error_code / legacy_error_code / message / requestId / details / retryable / resumable / recommendedAction`；前端不得解析中文 message 决定业务分支。

## 10. 测试与验收矩阵（吸收 instruction §19，关键硬断言）

跨模块 synthetic E2E 覆盖：`Migration → Board Facts → CoreRunContext → DSA compute once → stock_core publication → DSA projection → state events → board aggregation → Review publication → structure-only auction → chip late arrival → hybrid/composite auction upgrade → ProductReadiness → Admin API DTO`。

硬断言：DSA 每股每 core run 一次，不 backdate；Review 不等待 chip；chip 晚到不改变 Review source；旧 Worker 失去 lease 后不能发布；所有正式读取通过 pointer；structure-only/hybrid 不得 fully_ready；composite 才 fully_ready；degraded_ready != fully_ready；chip partial 产生 hybrid 不伪装 composite。

四类验收场景（远程手动验收）：

- A 完整成功：stock_core ready / dsa_projection ready / state_events ready / chip ready / auction composite / board_aggregation ready / review ready / closure fully_ready。
- B 异步增强：stock_core ready / review ready / chip running / auction structure_only / closure core_ready。
- C 降级：board_facts ready_reused / chip partial / auction hybrid / closure degraded_ready。
- D 治理与恢复：publication missing / lease lost / retryable child / granular restart / reconcile。

## 11. 完成定义（吸收 instruction §21，当前状态诚实标记）

`code_ready=true` 必须同时满足：scheduled DSA 只计算一次；projection 不调用 StrategyRuntime；CoreRunContext config/version 统一；stock_core publication 原子；chip 在 stock_core 后立即创建；ChipConsensusRun 与 chip publication 完成；Review 与 chip 核心依赖分离；auction hybrid 合同完成；ProductReadinessService 完成；degraded_ready/fully_ready 语义完成；synthetic E2E 通过；后端/PG/前端/build/architecture/docs/governance 门通过；最终 SHA == origin/dev；工作树干净；Acceptance Matrix 基线满足治理窗口。

> **当前诚实状态（2026-08-05 后）**：`code_ready=false`。原因：granular restart 大部分 boundary 后端未实现（PG E2E 未执行）；完整前端合同未验证；Migration 085/086 未在真实 PG 应用；本地禁止连 PG 仅 PURE_UNIT_TEST。详见 `docs/changes/2026/PRD-Acceptance-Matrix-V2.1-D-J-2026-08-05.md`。本 PRD 作为正式真源已建立，后续 Gate 2/3/4 在远程验证环境（`bz_stock_verify_<sha>`，见 `rules/80` DS-110）执行。

## 12. 非目标（吸收 instruction §2.2）

不要求：改造 Review P/Q/U/C/V 为 chip 驱动；用 chip 覆盖已发布 stock_core；为旧 DSA API 保留第二套盘后算法；前端自拼 canonical；本地 Vite/Uvicorn/Compose 作为最终部署；自动改 main；未经授权共享库写入；全量重算历史；manual/replay DSA 与 scheduled after-close 强行合并。
