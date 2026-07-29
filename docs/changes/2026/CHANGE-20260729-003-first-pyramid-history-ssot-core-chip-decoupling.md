# CHANGE-20260729-003：第一金字塔历史SSOT、筛选器原子特征与盘后核心/筹码解耦

> **编号说明**：代码中部分注释引用早期工作名 002，该编号为本变更的早期名。
> 最终规范编号为 CHANGE-20260729-003，二者指向同一变更体。
> 新代码与新文档统一使用 -003；历史 -002 注释保留不强制改写。

状态：代码+目标纯单元测试+Ruff+TSC+ESLint 通过；P0-1~P0-12 全部修复；浏览器真实链路验收待用户手工；CI 待触发
日期：2026-07-29
类型：architecture + behavior + bugfix
领域：量化模型 / 盘后编排 / 筛选器

相关 PRD：
- `../../prd/20-quant-model.md`：QM-01~QM-43（第一金字塔）、QM-60~QM-62（事件与连续因子分离）
- `../../prd/30-after-close.md`：AC-04（盘后 review core 发布门禁）

相关 Maps：
- `../../maps/20-quant-model.md`：SSOT 调用链、history 数据流、主 run 与 chip job 关系
- `../../maps/30-after-close.md`：盘后核心发布门禁、chip 后置非阻塞

相关 Rules：
- `../../../rules/20-market-data-indicators.md`：历史点时安全、anchor/confirmed/event 分离、review core 不得依赖 Node/15m、chip 不得阻塞发布

相关提交：
- 基线：e5eb40e（dev = origin/dev）
- 本轮 commit：20fff8800d7c8cd3ad8ffe837acdb2b6e15c8b08

## 1. 变更摘要

### 1.1 派生逻辑修复

1. **DSA 量能**：废弃"当前累计量/上一段总量"（sum/sum），改为"当前段截至当日均量/上一完整段均量"（mean/mean）。volume 和 amount 同口径。`structural_factor_service` 不再用 `visual_segments` 复制计算，改为消费 DSA `factor_per_bar` 权威字段。
2. **Rope 方向占比**：同一 DSA 段内按截至当日 expanding 计数/段长计算，禁止完整 group 统计回填过去（消除未来数据泄漏）。
3. **SMC OB 生命周期**：增加可选逐 bar timeline（`emit_timeline=True`）；OB 生命周期输出为不可变的 `OB_CREATED` / `OB_ENTERED` / `OB_MITIGATED` 三状态。ENTERED 仅在 OB 确认后、mitigation 前，由前一 bar 与区域无重叠到当前 bar high-low 首次重叠触发。删除"活跃 OB = OB_ENTRY"派生。
4. **动量 SQZ_RELEASE**：direction 按当日 SQZMOM 值正/负/0 映射 up/down/null；释放量比仅在 `sqzOn[t-1]` 且 `sqzOff[t]` 时，从 t-1 向前取连续 sqzOn 区间均量，再与 t 日量比较；生成逐日事件，不只查最后一根。
5. **regime_strength**：修复 `first_pyramid_service` 错误读取 `trend_strength`（不存在）导致静默 None 的 bug，改为读取 `regime_strength`（DSA SSOT 输出）。

### 1.2 筛选器所需个股原子输出

新增但不新增技术算法：
- `trend_transition`：UP_CONFIRMED / DOWN_CONFIRMED / UP_BROKEN / DOWN_BROKEN / UP_TO_DOWN / DOWN_TO_UP / NONE
- SMC 每日 `swing_bias` / `internal_bias` / `active_internal_ob_count` / `active_swing_ob_count`
- `volatility_phase` / `momentum_direction` / `momentum_change` / `sqzmom_delta`
- `core_factor_ready` / `history_sufficient` / `valid_for_market_aggregation` / `invalid_reason`

有效性只依赖趋势/结构/动量和日线完整性，严禁依赖筹码。市场覆盖率、事件率、集中度、异常分位、综合分数不得写入个股层。

### 1.3 历史 SSOT

新增唯一 `compute_first_pyramid_history(...)`：单股完整可用日线一次输入，一次计算 DSA/SMC/Bollinger/SQZMOM/VolumeContext，输出最近 N 个有效日的 daily state 及不可变 events，默认 N=250、`include_chip=False`。禁止循环 250 次调用 snapshot；历史 DSA 用 `lookback=None`，当前 snapshot 的 250-bar 合同保持不变。

### 1.4 核心与筹码彻底解耦

1. 拆分 `compute_first_pyramid_core_snapshot` / `compute_chip_consensus_snapshot` / `assemble_first_pyramid_view`；保留 `compute_first_pyramid_snapshot` 兼容包装。core 的 version/parameterHash/inputHash 排除 Node 参数，chip 使用独立 version/hash/run 关联。
2. `feature_snapshot_service` 新增 `compute_review_core_for_trade_date`：为盘后 review core 提供明确的 daily-core 路径，禁止 Node Cluster 和 15m Node 输入；不得用单周期 VP 伪装筹码。现有非盘后调用保持兼容。
3. `after_close_orchestrator` 关键路径目标设计：日线 → core 个股状态/事件 → 质量门禁 → 发布。核心发布成功即将主 run 标记 succeeded 并可复盘。
4. 发布后只创建独立 `after_close_chip_consensus` job，不 await、不加入主 run 成功门禁。chip 任务可失败/部分成功/单独重试，绝不反改主 run 或重算 core。
5. **本轮仅完成计算边界、独立 job 接口/状态合同和文档**；~~chip 持久化 migration 列为下一阶段唯一 blocker~~ **[2026-07-29 收口更新]** P0-10 chip 持久化已实现（migration 071 + `StockChipConsensusSnapshot`）；P0-11 非筹码历史回补已实现（migration 072 + `FirstPyramidHistoryDailyState`/`FirstPyramidHistoryEvent` + `first_pyramid_history_service.backfill_first_pyramid_history_batch`）。禁止修改已发布 core snapshot、禁止用 Redis 冒充持久化。

## 2. 行为变化

### 2.1 DSA 量能字段重命名

| 旧字段 | 新字段 | 说明 |
|---|---|---|
| `current_vs_prev_volume_ratio` | `current_vs_prev_volume_mean_ratio` | mean/mean 权威口径 |
| `current_vs_prev_amount_ratio` | `current_vs_prev_amount_mean_ratio` | mean/mean 权威口径 |
| `current_segment_volume_sum`（deprecated） | `current_segment_volume_mean` | 段内均量 |
| `prev_segment_volume_sum`（保留兼容） | `prev_segment_volume_mean` | 上一段均量 |

### 2.2 新增 DSA 字段

- `trend_transition`：字符串枚举（UP_CONFIRMED / DOWN_CONFIRMED / UP_BROKEN / DOWN_BROKEN / UP_TO_DOWN / DOWN_TO_UP / NONE）
- `rope_dir1_pct`：段内 expanding 方向占比（禁止未来泄漏）

### 2.3 SMC 新增输出

- `ob_lifecycle_events`：OB_CREATED / OB_ENTERED / OB_MITIGATED 不可变事件列表
- `state_timeline`（emit_timeline=True）：逐 bar 的 swing_bias / internal_bias / active_internal_ob_count / active_swing_ob_count
- `swing_bias` / `internal_bias`：最后 bar 状态（顶层字段）

### 2.4 SQZMOM 新增输出

- `build_momentum_history(sqzmom_result, volume_series, times)`：
  - `daily_state`：逐 bar 的 volatility_phase / momentum_direction / momentum_change / sqzmom_delta
  - `sqz_release_events`：SQZ_RELEASE 事件（含 direction + release_volume_ratio）
  - `momentum_zero_cross_events`：零轴穿越事件

### 2.5 第一金字塔拆分

| 函数 | 职责 | Node Cluster | 15m | chip_consensus |
|---|---|---|---|---|
| `compute_first_pyramid_core_snapshot` | 核心快照（trend/structure/momentum） | 禁止 | 禁止 | None |
| `compute_chip_consensus_snapshot` | 筹码共识（独立计算） | 调用 | 可选 | DimensionResult |
| `assemble_first_pyramid_view` | 组装完整视图 | — | — | 可选 |
| `compute_first_pyramid_snapshot` | 兼容包装（core + chip 一次算） | 调用 | 可选 | 可选 |
| `compute_first_pyramid_history` | 历史 SSOT 一次计算 | 禁止 | 禁止 | 默认 False |

### 2.6 盘后 review core 路径

- `compute_review_core_for_trade_date`：daily-core only，禁止 Node Cluster / 15m
- `summary_payload._review_core = True` 标记
- `node_cluster.availability = "review_core_no_chip"` 显式标记
- chip consensus 由独立 `after_close_chip_consensus` job 异步计算

### 2.7 chip consensus 独立 job 接口

- `job_name = "after_close_chip_consensus"`
- 状态合同（复用 SchedulerJobRun，不新增 status）：queued / running / succeeded / failed / interrupted / resume_queued
- 部分成功写 `metadata.chip_status="partial"`，主 status 保持 succeeded/failed
- `create_after_close_chip_consensus_job(db, trade_date, core_run_id, scope, expected_count)`：幂等创建
- `execute_after_close_chip_consensus(...)`：**已实现**（[P0-10] 修复）
  - 分批获取 daily+15m bars，调用 `compute_chip_consensus_snapshot`
  - 幂等 upsert 到 `stock_chip_consensus_snapshots` 表
  - 单股失败不阻塞其他股票，写入失败记录便于断点续算
- `metadata_json` 只存 scope/expected_count/core_run_id/checkpoint（[P0-9] 禁止 UUID 数组）

### 2.8 chip 持久化与历史回补表（[P0-10/11] 收口新增）

**migration 071 `stock_chip_consensus_snapshots`**：
- 字段：id / instrument_id / trade_date / core_run_id / algorithm_version / chip_hash / chip_payload(JSONB) / status / error_message / created_at / updated_at
- 唯一键：`(instrument_id, trade_date, core_run_id, algorithm_version)`
- 索引：trade_date / core_run_id / (instrument_id, trade_date desc)

**migration 072 `first_pyramid_history_daily_state` + `first_pyramid_history_events`**：
- daily_state：每只标的每个交易日的 point-in-time 状态（最近 250 日）
  - 唯一键：`(instrument_id, trade_date, algorithm_version)`
  - upsert（on_conflict_do_update）幂等重跑
- events：不可变事件流（BOS/CHoCH/OB_CREATED/OB_ENTERED/OB_MITIGATED/EQH/EQL/SQZ_RELEASE/ZERO_CROSS_*）
  - 唯一键：`(instrument_id, algorithm_version, event_id)`
  - insert on_conflict_do_nothing（不可变，重跑不覆盖）

### 2.9 主 run 与 chip job 时序（[P0-6/7] 收口确认）

```
after_close_orchestrator.execute_after_close_run:
  1. 日线刷新 → 板块同步 → 覆盖率检查
  2. compute_review_core_batch_for_trade_date (daily-core only, 禁止 Node/15m)
  3. 质量门禁 → publish_run → 主 run status=succeeded
  4. [主 run 成功后] 软失败创建 after_close_chip_consensus job（不 await）
     - chip job 创建失败只 warn，不反改主 run
     - chip job 由独立 Worker 领取执行
```

### 2.10 非筹码历史回补服务（[P0-11] 收口新增）

`first_pyramid_history_service.backfill_first_pyramid_history_batch`：
- 按"个股为外层，一次调用 history SSOT"模式回补
- 每只股票：MDAS 读完整可用日线 → `compute_first_pyramid_history(include_chip=False)` → upsert daily_state + insert events
- 分批 25—50 股，每批 commit + checkpoint（progress_callback）
- 幂等重跑：相同 (instrument_id, trade_date, algorithm_version) 重复执行只更新 daily_state，events 不重复插入
- 禁止逐日调用 snapshot，禁止回补 chip

## 3. 未解决问题

1. ~~chip 持久化 migration（下一阶段唯一 blocker）~~ **[已解决]** P0-10 已实现 migration 071
2. ~~after_close_orchestrator 关键路径未切换~~ **[已解决]** P0-6/7 已切换到 `compute_review_core_batch_for_trade_date` + 软失败创建 chip job
3. 浏览器真实链路验收待用户手工
4. CI 待触发（push dev 后）
5. 服务器部署与 250 日回补待执行（canary 5—10 只先行）

## 4. 验证

### 4.1 目标纯单元测试

`PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 pytest tests/test_change_20260729_003.py -v -p no:cacheprovider`

**27 项全过**（含 chip execute 实现 + orchestrator 不导入 execute 的合同验证），覆盖：
1. 量能均量比（mean/mean）
2. Rope 前缀不变性
3. OB 三事件时间线
4. SQZ_RELEASE 三方向与前置挤压量
5. regime_strength 读取正确
6. history 一次计算多日
7. 最后日与 core snapshot 一致
8. core 不调用 Node Cluster
9. 主 run 不等待 chip（已实现 execute + 不导入到 orchestrator）
10. chip 失败不影响 core
11. chip hash 独立于 core

### 4.2 P0-11 历史回补服务测试

`PURE_UNIT_TEST=1 pytest tests/test_first_pyramid_history_service.py -v -p no:cacheprovider`

**8 项全过**，覆盖：
1. 每只股票一次调用 history SSOT
2. 单股失败不阻塞其他股票
3. 进度回调每批后被调用
4. 空 bars 标记 skipped
5. 不导入 compute_first_pyramid_snapshot
6. event_id 构造稳定性（bar_index/anchor_time/hash 三级 fallback）

### 4.3 既有测试回归

- `test_first_pyramid_contract.py`：**44 项全过**（含 OB_ENTRY 事件 structure_level 验证，向后兼容）
- `test_after_close_orchestrator.py`：3 个纯单元通过；30 个 DB 集成测试在 PURE_UNIT_TEST 模式下预期跳过（CI 临时 Postgres 运行）
- `test_watchlist_monitor_status_snapshot.py` / `test_stock_detail_feishu.py`：4 个纯单元通过；DB 集成测试 CI 运行
- `test_after_close_orchestrator.py` mock 路径已从 `compute_for_trade_date` 更新为 `compute_review_core_batch_for_trade_date`

### 4.4 Ruff / TSC / ESLint

- Python 修改文件全部通过 `ruff check`（含新 migration 072、新 model、新 service、新 test）
- 前端 `npx tsc --noEmit` 通过
- 前端 `npx eslint` 修改文件全部通过（含 firstPyramidViewModel 字段重命名、firstPyramidColumns helpText 更新、CaptureStockPage computeCombinedReady）
- 前端纯函数测试因本地 Node 20.10 不支持 `--experimental-strip-types`（需 Node 21+），待 CI 运行
