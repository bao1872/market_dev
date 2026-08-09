# 盘后任务 PRD

状态：已确认
最后确认日期：2026-08-06
对应 Map：`../maps/30-after-close.md`
需求所有权：盘后触发、readiness、编排、计算、校验、发布和补跑

> 本文件拥有盘后触发、编排、恢复和发布行为；[`31-after-close-product-closure-v2.1.md`](./31-after-close-product-closure-v2.1.md) 只拥有跨域节点、运行身份、readiness、lineage 和产品闭环。两者冲突时按具体职责归属处理，不以总纲覆盖本文件编排细则。

## 1. 目标

每个 A 股交易日完成所需数据准备、全市场日线因子和事件计算、结果校验和正式发布，并支持本地纯单元调试及远程隔离验证、补跑。

## 2. 已确认需求

### AC-01 远程自动运行

远程稳定运行位置应在交易日数据 ready 后自动启动盘后任务。

### AC-02 本地不自动调度

本地自动 Scheduler 默认关闭。

### AC-03 分平面调试与执行

本地只用 pure unit、mock/fixture 调试单步骤和编排行为，不启动 Scheduler、正式 Worker、盘后编排或全市场任务。需要 PostgreSQL、跨 Worker、migration、发布指针或完整链路的验证，只能在按 SHA 隔离的远程验证环境执行；稳定运行补跑和业务数据写入必须另有明确授权。

### AC-04 日线盘后计算

盘后 core 主链不以 15m 数据作为发布门禁，趋势、结构、动量和 review core 主要基于日线计算。盘后仍必须刷新并保留 15m 行情，因为独立 `after_close_chip_consensus` 使用当日收盘后的 15m 数据计算筹码共识。15m 不得阻塞 stock_core 发布，但必须成为 chip 阶段自己的 readiness 输入。

### AC-05 固定参数一次计算

正式盘后统一 Core 每日以冻结参数计算一次。DSA 作为第一金字塔 Core 的 Trend 维度，与 SMC、Momentum 等在同一个 CoreRunContext 中 Compute Once；`dsa_projection` 只是对同一 canonical DSA artifact 的兼容投影，scheduled after-close 不存在第二次 DSA 计算。页面只筛选已计算结果，不触发策略组合和重新计算。

### AC-06 Readiness 门槛

任务启动前必须检查与本次计算相关的数据 readiness，并给出不满足原因。

### AC-07 Run 隔离

每次完整运行具有明确 run 标识。局部调试、失败运行和未校验结果不得自动成为正式结果。

### AC-08 计算与发布分离

计算完成不等于发布。只有满足校验和发布条件的 run 才能成为正式结果。

### AC-09 正式发布指针

正式读取通过明确发布标识或 `published_run_id` 指向当前正式 run。

### AC-10 两阶段发布

发布应采用可重复、可恢复的两阶段语义：

1. 结果和状态准备完成；
2. 校验通过后切换正式发布指针。

不得把“同一数据库事务内一次完成所有长链路操作”作为唯一安全保证。

### AC-11 幂等与补跑

任务、子任务和发布过程应支持安全重试与补跑，避免重复记录、重复发布和状态倒退。

### AC-12 跨 Worker 领取

子任务领取、超时和重新领取必须具有明确规则，避免多 Worker 重复处理或永久丢失。

### AC-13 完成状态

至少区分：

- pending；
- running；
- partial；
- completed；
- failed；
- published。

具体字段可以不同，但语义必须明确。

### AC-14 部分失败

全市场任务部分失败时，不得直接标记整体成功。必须保留成功、失败、跳过和待重试范围。

### AC-15 旧触发路径清理

不再使用的盘后自动触发入口应删除，不长期保留重复编排路径。

### AC-16 Feature Snapshot 批处理性能合同

- 全市场 `feature_snapshot` 必须经 MDAS 批量入口预读 `symbols × bars × adj_factor`，不得由快照服务直连行情 Repository 或自行实现复权；同一股票、周期、交易日的 canonical bars frame 与诊断 hash 必须在该批计算内复用。
- 批处理并发必须有显式上限；失败按单股隔离并计数，批次完成后发送 heartbeat/progress；禁止无界任务创建。
- 成功快照按批量 upsert/flush 写入，不得逐股票 commit；调用方仍持有整日期事务，失败率超阈值时可整体 rollback，已发布快照保护不变。
- 批结果必须暴露批次数、MDAS 批读次数、成功/失败数、耗时与有效并发度等低基数 metrics，支持性能回归核验。

### AC-16 统一盘后编排（CHANGE-20260728-008）

系统只允许 `job_name=after_close_orchestrator`、`run_type=full` 一种盘后任务类型。不得存在 `dsa_only` 独立端点、独立 `mode` 分支或独立 `run_type`。

重算通过现有 `force` 端点 + `restart_from="daily_ready"` 参数实现，仍是同一 `after_close` 任务，不创建 `dsa_only` 类型，不跳过后续特征/快照/发布步骤。仅 admin 可用；必须先验证日线覆盖率 ≥ 90%。显式 `restart_from` 必须属于允许的步骤并验证其前置步骤已完成；重启 run 在 `metadata_json` 保存 `parent_job_run_id`、`restart_from` 和重启次数，不新增数据库列。`restart_from="daily_ready"` 的语义是重新进入 Core computation：DSA/SMC/Momentum 按统一 Core 合同重新计算一次，后续 projection / board / Review 等正常重建；不存在"从 DSA 阶段单独重算"这一步骤。

状态链：`queued→running→refreshing_daily→syncing_boards→checking_coverage→computing_features→publishing→succeeded`；`StrategyRun` 状态链：`running→completed→published`，异常 → `failed`。不得在发布前伪造 `completed`。

顶层步骤使用统一执行合同：开始时写步骤状态、进度和 heartbeat，执行受明确 timeout 约束；成功、失败、超时、取消和合法不可用均写结构化 `step_summary`；`finally` 必须停止 heartbeat 并保存结束时间。可选步骤失败使主 run 成为 `partial_success`，不得伪装全成功；`auction_anchor` 超时或无数据统一记为 `skipped_unavailable`，默认非阻断，不得仅因此阻断 Review。

管理 API 的 cancel 与 reconcile 必须幂等：重复 cancel 不改变终态；reconcile 只依据现有任务事实修复派生状态，不启动真实数据任务。stale watchdog 同时检查 heartbeat 与步骤级 timeout，避免长 lease 掩盖已失联或已超时的步骤；健康 heartbeat 的长任务不得被接管。

对已有旧 `dsa_only` queued/running 记录只读识别；生产执行前通过正式 cancel/interrupted/retry 服务处理，禁止 DELETE 或直接改 metadata。

## 3. 验收标准

- 远程交易日任务可自动运行。
- 本地不会因 Scheduler 自动触发，也不运行真实完整盘后链；完整手动验证在远程隔离验证环境执行。
- 单股、股票池和全市场使用同一核心链路。
- 未校验 run 不会成为正式发布结果。
- 重复执行不会产生无法解释的重复发布。
- 系统不存在 `dsa_only` 独立端点、独立 mode 分支或独立 run_type。
- `restart_from="daily_ready"` 重新进入 Core computation（DSA/SMC/Momentum 统一 Core 合同重新计算一次），仍执行完整后续链路，不存在独立 DSA 阶段重算。
- Map 对 AC-01 至 AC-16 给出实现状态、入口和验证证据。

## 4. 增量发布架构需求（CHANGE-20260729-006/007）

### AC-08：单股事务与检查点
- 计算/事务/检查点粒度为"单股×阶段"；batch 只控制吞吐和内存，不是完成或发布边界。
- 结果 commit 成功后才标记 item succeeded；单股失败只回滚该股票，不回滚其他已成功股票。
- 恢复只处理 pending、可重试 failed、lease 过期 running；成功且 hash/version 相同不重算。

### AC-09：分层发布指针
- `factor_publications` 按交易日+kind 维护发布指针，`data_run_id` 指向覆盖率门禁通过的不可变 run。
- `CORE_PUBLICATION_MIN_COVERAGE = 0.98`，低于门禁拒绝发布。
- 发布只做小事务原子切换指针，不复制结果数据；不得修改已发布 run。
- `trade_date` 必须为 NOT NULL（禁止普通唯一约束允许多 NULL 产生重复 pointer）。
- `publish_market_aggregation` 必须验证 `source_core_run_id` 等于该日期已发布 stock_core pointer。
- `publish_history_cross_section` 的 coverage 必须由 DB 统计，不接受调用方任意传值。
- pointer 不得倒退到旧 run。

### AC-10：读取端统一接入 pointer
- 用户市场列表、筛选排序、个股详情默认只读取 stock_core pointer 指向的 run。
- 无 pointer 时兼容回退到 `published_at IS NOT NULL`（旧数据）；有 pointer 后严禁混读不同 run。
- 个股暂存结果可在管理端查看；详情若展示未正式发布结果，必须返回 `is_provisional=true`、`data_run_id` 和 `calculation_status`。
- `is_stale` 真源为 `bars_daily.max(trade_date)`，不是 `StockFeatureSnapshot.max(trade_date)`。

### AC-14：独立任务与核心保护
- 市场聚合、事件、chip、通知为独立任务，失败只重试自身，不反改核心。
- 主编排在 core pointer 发布后即可标记 `core_published` 并允许复盘。
- 最终状态可为 `completed_with_errors`，但不得因 optional 失败反改 core。
- chip.core_run_id = snapshot_run_id（不指向 SchedulerJobRun.id）。
- chip 严格按 instrument_id + trade_date + snapshot_run_id + algorithm_version + status=succeeded 匹配。

## 5. 板块分析 V1（CHANGE-20260730-011）

### BA-01：定位与门禁

板块分析 V1 是基于已发布 `stock_core` 数据的衍生分析，独立于 chip 共识。chip 是可选维度，不作为板块核心门禁。

输入门禁：

1. 必须存在已发布 `stock_core` pointer（否则拒绝计算）
2. 只纳入 `published stock_core pointer` 同 run、`core_factor_ready=true`、`valid_for_market_aggregation=true` 的股票
3. 退市股（`Instrument.status != 'active'`）不参与聚合，不进入 `eligible_count` / `missing_count`
4. 数据不足股票进入 coverage 分母说明，但不参与有效统计
5. 行业与概念分开计算；成员和股票因子必须同一 `trade_date`，禁止使用未来数据

### BA-01B：Board Analysis V1 产品范围（CHANGE-20260809，Phase 4D.3 决策 A）

Board Analysis V1 的**正式产品范围**只有两类板块实体：

| scope_family | 来源 | 是否属于 Board Analysis V1 |
|---|---|---|
| `industry`（L1/L2/L3） | `market_boards.type == 'industry'` | **IN_SCOPE** |
| `concept` | `market_boards.type == 'concept'` | **IN_SCOPE** |
| `major_index`（csi300/csi500） | `universe_definitions` | **OUT_OF_SCOPE** |
| `style`（large_cap/small_cap） | `universe_definitions` | **OUT_OF_SCOPE** |
| `market` | Review scope，非 board 实体 | **OUT_OF_SCOPE** |

`major_index` / `style` 仍是 **Review optional scopes**（PRD 70 §6.3），由 universe membership
自己的正式链路负责，**不属于 Board Analysis V1 batch universe**。因此它们不得进入：

- `BoardAnalysisRun.expected_count`
- `BoardAnalysisRun.succeeded_count`
- `BoardAnalysisRun.failed_count`
- board batch 层的 population blockers
- board `coverage_ratio` 分母

`universe_definitions` 中的这些定义**保留**（不删除 seed，不改 migration 079 历史）；
仅 board_analysis 不再消费这些 out-of-scope definitions。

### BA-02B：BoardAnalysisRun batch status 语义（CHANGE-20260809，Phase 4D.3 决策 B）

`BoardAnalysisRun.status` 的**完整合法取值**如下，不得再出现其他 batch-level 状态：

| status | 终态 | 语义 |
|---|---|---|
| `pending` | 否 | 尚未开始 |
| `running` | 否 | 计算中 |
| `succeeded` | 是 | 所有 in-scope board 均完成计算，且全部达到 board ready contract（`coverage >= 0.95`） |
| `partial` | 是 | 所有 in-scope board 均完成**正式计算**，不存在 execution / DB / contract failure，但至少一个 board `coverage < 0.95` 或存在其他明确的数据完整性 degradation |
| `failed` | 是 | 存在 board compute exception、DB persistence failure、contract violation、无法完成 mandatory board computation 或其他 unexpected execution failure |

`partial` 是 **terminal + truthful + degraded**，**不是** execution failure。

**禁止**：`blocked_external_population` 不得作为 `BoardAnalysisRun.status` 使用。该语义只属于
`UniverseDefinition.population_status` 与 scope-level population readiness，其 scope/universe
层用法保持不变。

**status 推导顺序（显式，execution failure 优先）**：

```text
if execution_failure:        -> failed
elif not all in-scope boards completed -> partial（未达 ready contract 的终态）
elif any board snapshot partial        -> partial
else                                   -> succeeded
```

population 相关分支**不得**置于 execution failure 之前，以免掩盖真实 failure。

**counter 基数统一**：`expected_count` / `succeeded_count` / `failed_count` 必须是**同一实体基数
= in-scope MarketBoard（industry + concept）**，不得混入 universe blockers。

- `expected_count` = in-scope board 数量
- `succeeded_count` = 达到 ready contract 的 board snapshot 数量
- `failed_count` = **execution failure 的 board 数量**（coverage 不足的 partial board **不计入**）
- `partial_count`（coverage 不足的 board 数）当前 schema 无持久化列，从 snapshot statuses 派生，
  在 metadata / diagnostics / readiness DTO 中表达

### BA-02：数据模型

新增 `board_analysis_snapshots` 表（migration `074_board_analysis_v1`）：

- 单表设计：每条记录既是 run 又是 snapshot（含 `status` / `started_at` / `finished_at`）
- 唯一键 `(trade_date, board_id, algorithm_version)` 保证幂等
- 复用 `factor_publications` 表发布指针：`publication_kind=market_aggregation`、`scope_type=board`、`scope_key=board_id::text`、`data_run_id=board_analysis_snapshot.id`
- `algorithm_version="board-v1-20260730"`，`parameter_hash` 由算法版本+固定参数 SHA-256 截断
- 失败的单板块不阻塞其他板块

### BA-03：指标 payload（V1）

V1 输入仅趋势、结构、动量、量能、结构事件和权威行业/概念成员关系。指标 payload 至少包括：

- **趋势**：上/下/中性比例、平均 VWAP 偏离、强度分布（avg/p25/p50/p75）
- **结构**：主要结构方向（swing up/down/neutral）、对齐状态（aligned/misaligned/neutral）、平均活跃 OB 数
- **结构事件**：BOS/CHoCH/OB 方向计数、EQH/EQL presence、事件率（rate = 有事件数 / ready_members）
- **动量**：正/负/中性比例、挤压/释放/正常、增强/减弱/flat 比例、平均 SQZMOM
- **量能**：放量/缩量/正常/未知比例、20/200 日平均 volume_ratio、20/200 日分位分布（5 桶）
- **汇总**：`total_members` / `ready_members` / `missing_members` / `missing_reasons`

禁止使用不可解释的综合评分；排序只按单一公开指标或明确可配置权重。

### BA-04：发布门禁

- `coverage_ratio = ready_count / eligible_count`
- `coverage >= 0.95` 才可正式发布（写入 `factor_publications` 指针）
- 不足时保存 `partial` 结果但不切 pointer（可重复计算，幂等）
- 发布只做小事务原子切换指针，不复制数据
- pointer 不得倒退到旧 run

**batch 级 pointer 发布（CHANGE-20260809，Phase 4D.3 决策 C）**：`market_aggregation` pointer
允许指向 `status == succeeded` 或 `status == partial` 的 `BoardAnalysisRun`，但 `partial`
必须满足 **DEGRADED PUBLISHABLE CONTRACT**（见 PRD 31 §PC-42）。

本轮**不引入新的 batch coverage 阈值**（如 90%/92%/94%/95%）。原因：
`coverage_ratio = succeeded_count / expected_count` 只是 **board readiness ratio**，
不是 member-weighted 数据覆盖率，不能机械决定整个 batch 是否 publishable。
degraded eligibility 由「全部执行完成 + 无 execution failure + partial 原因可解释」决定。
未来若产品需要 batch quality threshold，单独定义。

pointer 本身**不得把 partial run 伪装成 succeeded**：`pointer.data_run_id` 可以指向 partial run，
消费者通过 `run.status` + readiness DTO / diagnostics 得知 DEGRADED。

### BA-05：API 与 UI

- 用户路由 `GET /api/v1/boards/analysis` 列表分页（按 type / trade_date / sort 过滤）
- 用户路由 `GET /api/v1/boards/{board_id}/analysis` 单板块详情（含完整 payload）
- 管理路由 `POST /api/v1/admin/boards/{board_id}/analysis/compute` 单板块触发
- 管理路由 `POST /api/v1/admin/boards/analysis/compute-all` 批量触发（canary/全量）
- 前端 `/boards` 列表页 + `/boards/:boardId` 详情页；行业/概念切换、覆盖率、四维分布、事件清单；不做复杂动画

### BA-06：Canary → 全量流程

1. 先 5 个行业 + 5 个概念 canary，核对成员数和 SQL
2. canary 通过后全量计算并发布 pointer
3. 单板块失败不阻塞其他板块
4. CLI：`scripts/first_pyramid_history_backfill_cli.py` 之外的 `backend/scripts/board_analysis_cli.py`
   - `--canary`（每类型 5 个）/ `--all` / `--type` / `--limit` / `--trade-date` / `--publish` / `--no-publish` / `--dry-run`

## 6. 盘后阶段依赖与发布闭环（2026-07-30 补充）

> 本节明确盘后链路各阶段的发布闭环和依赖合同。与 §4 增量发布架构需求、§5 板块分析 V1 叠加生效。

### AC-17：stock_core 发布闭环

- DSA StrategyRun 发布成功（`status=published`）且 snapshot run coverage 达标（`CORE_PUBLICATION_MIN_COVERAGE = 0.98`）后，after_close 编排必须显式调用 `factor_publication_service.publish_stock_core` 切换 `stock_core` publication pointer；
- `publish_stock_core` 是 stock_core 正式发布的唯一入口，禁止跳过该步骤直接依赖 `published_at IS NOT NULL`；
- pointer 切换失败只重试 `publish_stock_core`，不重新计算数据；
- `stock_core` pointer 未发布前，下游 board aggregation / market aggregation / review 不得启动；
- 失败恢复必须走 `dsa_recovery_service` 或 `publish_stock_core` 幂等重发，禁止裸 SQL 改状态（详见 `rules/80-deployment-data-safety.md` "手工恢复走正式 service/CLI"）。

### AC-18：chip_consensus Worker

- chip job 进入逐股计算前，必须先完成一次运行级、有界、可观测的 `bars_15min` 刷新阶段，再通过 MDAS 批量读取 canonical 数据；不得在逐股计算循环内执行无界全历史刷新；
- 刷新阶段必须校验目标交易日、最后完成 bar、预期时段覆盖、最低输入根数及数据来源，并保存 coverage 和失败明细；
- 15m 不新鲜或不足时必须记录结构化 reason、actual/required bars 和 source cutoff，不得回退到旧交易日 15m 或伪造成功；该结果只影响 chip readiness，不反改已经发布的 stock_core/review core；
- chip_consensus 任务在现有 after-close worker 容器内领取执行，**不新增常驻容器**；
- worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取 `queued` / `resume_queued` 的 `after_close_chip_consensus` 任务，`lease_epoch` fencing 防止旧 worker 覆盖新 worker 状态；
- worker 每约 30 秒以 job id、`status=running`、worker instance 和 `lease_epoch` 的完整 fencing 条件刷新 heartbeat 与 lease；刷新失败即失去所有权，旧 worker 不得再写 snapshot 或终态；
- watchdog 只有在 lease 已过期且 heartbeat 不健康时才回收任务，健康长任务不得因执行超过 90 秒被接管；
- 断点续算：`resume_queued` 任务只重试未成功 instrument，已 `succeeded` 或合法 `skipped` 的 instrument 不重算；
- chip_consensus 失败只重试自身，不反改 core（`execute_after_close_chip_consensus` 内部已隔离）；
- 终态必须写 `finished_at`、释放 lease 并保存 succeeded/failed/skipped 计数与结构化原因；全成功、部分成功、全部合法 skipped、系统性失败分别映射为 `succeeded/succeeded`、`succeeded/partial`、`succeeded/skipped`、`failed/failed`（主 status / `metadata.chip_status`）；
- `auto_resume_interrupted_after_close_runs` 同时处理 `after_close_orchestrator` 和 `after_close_chip_consensus` 两类 `interrupted` 任务，最多恢复 3 次。

### AC-19：聚合依赖合同

- board aggregation / market aggregation 必须在 `stock_core` pointer 发布成功后才能触发；
- `run_market_factor_aggregation` 必须读取已发布 `stock_core` pointer，校验 `source_core_run_id` 等于该日期 `stock_core` pointer 的 `data_run_id`，不匹配抛错；
- board_analysis 输入门禁要求存在已发布 `stock_core` pointer，否则拒绝计算；
- 聚合失败只重跑聚合，**不影响已发布 stock_core**；
- 聚合 pointer 不得倒退到旧 run；不足门禁时保存 `partial` 结果但不切 pointer。

### AC-20：Daily Facts / Board Facts 独立输入分支（V2.1 对齐 PRD31 §1 / §2 / §8）

> 本条款为 PRD31 跨域节点、运行身份与编排阶段在同域 PRD 的显式传播，不引入新业务决策，不新增 PC 编号。权威来源：
> - 最终业务链与节点归属：PRD31 §1；
> - 九节点注册表（`daily_facts` / `board_facts` 节点定义）：PRD31 §2；
> - 编排与恢复（阶段顺序、readiness、lineage）：PRD31 §8。

- **Daily Facts 与 Board Facts 是两条独立输入分支**：Daily Facts（`daily_facts` 产品节点）只承载目标交易日**日线** readiness；Board Facts（`board_facts` 产品节点）只承载板块 **taxonomy / PIT membership / board-facts publication readiness**；二者不得合并或互相替代，也不得用模糊的"板块行情"概念混入其它领域事实。
- **`stock_core` 只依赖 Daily Facts**：core 计算链的输入门禁只看 Daily Facts 是否达标，Board Facts 不参与 core 发布门禁，Board Facts 缺失或降级不得阻断 `stock_core` 发布。
- **Board Aggregation 依赖正式 `stock_core` + usable Board Facts**：`board_analysis` 在 `stock_core` pointer 发布成功后触发，并以 usable board facts（taxonomy / PIT membership / publication readiness）为输入；Board Facts 不可用时应记录结构化 reason 并降级 / 暂缓，不得反改已发布 `stock_core`。
- **Review 依赖正式 `market_aggregation`**：`market_review_run` 创建需要 `stock_core` pointer 与 `board_analysis` pointer 均已发布（即 `market_aggregation` 已就绪）；Board Facts 通过 board aggregation 间接进入 Review，不绕过该 lineage。
- 上述分支与 PRD31 §8 编排阶段 `daily → core → post_core → board_review → finalize` 一致：core 阶段只读 Daily Facts，board_review 阶段才消费 Board Facts。

## 复盘编排 (Review Orchestration)

> 对应 PRD：`70-review.md` §11；对应 Map：`../maps/30-after-close.md` §复盘 pointer 与 run 关系。
> 复盘编排是盘后链路 stock_core → board_analysis 之后的独立阶段。实现状态属于 Map，不在本 PRD 内陈述。

### RV-AC-01 触发条件

当 `stock_core` pointer 和 `board_analysis` pointer 均已发布后，创建 `market_review_run`。两个输入 pointer 缺一不可。

### RV-AC-02 编排顺序

```text
stock_core published
→ board_analysis published
→ create market_review_run
→ compute level-1 scope metrics（market / major_index / style / industry_l1）
→ evaluate filters（A/B/C 三类偏差筛选器）
→ compute level-2 attribution（仅对命中信号的父范围下钻）
→ map representative instruments
→ evaluate active trackings
→ quality gate
→ publish review pointer
```

### RV-AC-03 隔离与恢复

- 每个 scope 独立 item、短事务、可恢复；
- 一个 scope 失败不回滚其他 scope；
- 重启只处理 pending / 可重试 failed / 过期 running；
- 相同输入 hash 和版本的 succeeded item 不得重算；
- 信号和归因幂等；
- pointer 切换失败只重试发布，不重算。

### RV-AC-04 发布门禁

单 scope：

- underlying coverage >= 0.95；
- P/Q/U/C/V 必要组件状态可用。

整套 Review：

- market 范围必须 ready；
- 配置的主要指数和风格范围必须 ready；
- 一级行业 ready 比例达到配置门槛；
- signal evaluation 无系统性异常；
- `source_core_run_id` 和 `source_board_run_id` 均指向当前正式 pointer。

## 17. 正式盘后含review阶段 + 时间线负耗时修复（CHANGE-20260801-001）

### AC-70 盘后7步正式状态机（含复盘）

新的 after_close_orchestrator 状态机为 **7 个展示步骤**（旧 8 步收敛为新 7 步）：

```
refreshing_daily        // 刷新 & 校验日线 readiness
→ syncing_boards        // 同步板块成员
→ checking_coverage     // 检查 coverage 质量门槛
→ computing_features    // 统一特征计算（含旧4步：creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot）
→ publishing            // stock_core & board_analysis 正式 pointer 切换
→ computing_review      // 【新增】复盘计算与发布（create_run→compute_run→publish_review）
→ watchlist_ready      // 全部就绪：选股监控与自选可用
```

- `watchlist_ready` 只在 **stock_core 成功 + board_analysis 成功 + review 成功 三项全部满足** 时标记为 completed；任何一环失败都不得显示为整体成功。
- review 四步（create/compute/publish 三步 + 结果校验）必须在盘后编排代码中显式调用，不得仅存在于注释中。
- 失败不得静默写主任务 SUCCEEDED：review 任一步骤失败或返回 failed 时，主任务整体转 FAILED，在 `after_close_runs.metadata` 和 `job_run_event` 时间线中记录 `review_run_id / review_status / review_reason`。

### AC-71 幂等review重跑合同

- review 创建/计算/发布必须遵循输入幂等原则：

| 输入变化情况 | 行为 |
|---|---|
| `stock_core_pointer run_id + board_analysis run_id 与上次相同 | 直接返回已存在的同一 review_run；已发布则不重复切换 pointer |
| 任一上游 run_id 变化（pointer 切换为新 run） | 创建新 review_run；新 run 通过后再切换正式 pointer；旧 review_run 保留供审计不删 |
| 计算中/发布中（running） | 不重入；返回当前 run 完成后再判定 |

### AC-72 时间线合同（防负数耗时）

盘后管理页的步骤时间线必须满足：

1. **同 run 同 attempt 配对原则：每一步骤的 `started_at` / `finished_at` 必须来自同一 `job_run_id` 且同一 attempt（attempt 由 queued/manual_resume/START 等边界事件切开）。跨 attempt 或跨 job_run 的事件不得配对为同一步骤耗时。
2. **Asia/Shanghai 时区统一**：所有时间戳统一转换为 Asia/Shanghai 时区 aware datetime。DB 内 naive 时间按 UTC 解释再转上海；后端返回给前端的 started_at/finished_at 字符串带上海时区或显式说明。
3. **缺一端不填负数**：若只有 started_at 无 finished_at → 状态为 running，duration 为 null，前端显示"进行中"；若两端缺失或顺序异常（started_at > finished_at），不得用 `max(duration, 0)` 掩盖，而应在 `PipelineStep.warnings` 中记录 `invalid_order_or_zero_duration` 并返回 `duration_seconds: null`，前端显示为"未知"并使用黄底警告样式（`timeline-meta-warn`）提示管理员。
4. **多 attempt 语义**：一次 after_close run 被 queued/manual_resume 重启后，后续步骤的前一次 attempt 记为 failed/interrupted；UI 不展示警告。

### AC-72A 管理诊断与恢复操作合同

- 盘后管理页与任务详情必须直接展示服务端返回的每步真实状态、`processed/total`、最近进度时间、心跳年龄、租约剩余、已用时、错误与重试信息，以及发布状态；存在非关键项失败但核心结果已发布时，必须明确显示“部分成功”，不得仅靠颜色表达状态。
- 管理操作必须语义分离：**终止任务**只请求协作式取消；**对账状态**只核验并修正持久化状态；**从此处续跑**保留成功检查点；**完整强制重跑**清空检查点并从头排队。四项操作不得复用含义模糊的“重试/强制执行”文案。
- 危险操作必须二次确认，按钮在请求期间禁用，并向管理员说明影响范围；操作完成后必须刷新流水线聚合、单次运行、最近运行及任务列表缓存。
- 前端不得根据时间戳自行判定心跳陈旧、租约过期、重试资格、发布成功或部分成功；这些诊断与能力字段必须由服务端合同提供，缺失时显示未知或禁用操作。

### AC-73 review 冷启动合同（历史不足）

- Review pipeline 不因为历史 < 60 交易日不显示为整页"不可用"。正确行为：
1. rawValue（从 metric_engine 已有 raw）必须展示，coverage 必须展示，reason 显示 `insufficient_history`；
2. 历史分位、normalized 值、delta 可为 null；
3. 筛选信号若基于 normalized 值的筛选器在 insufficient_history 下禁用；
4. `fp_segment_change_pct` 等第一金字塔上游字段全空时，按 §MX-63 的 null 语义合同返回 `null + "无可用分段数据"`，不得在 review 层伪造 0 或均值回填。
