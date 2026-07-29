# CHANGE-20260729-003：第一金字塔历史SSOT、筛选器原子特征与盘后核心/筹码解耦

状态：代码+目标纯单元测试+Ruff 通过；浏览器真实链路验收待用户手工；chip 持久化 migration 为下一阶段唯一 blocker
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
- 本轮 commit：待填写

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
5. **本轮仅完成计算边界、独立 job 接口/状态合同和文档**；chip 持久化 migration 列为下一阶段唯一 blocker。禁止修改已发布 core snapshot、禁止用 Redis 冒充持久化、禁止未经验证新增 migration。

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
- 状态合同：queued / running / succeeded / partial / failed / interrupted / resume_queued
- `create_after_close_chip_consensus_job(db, trade_date, core_run_id)`：幂等创建
- `execute_after_close_chip_consensus(...)`：**接口合同已定义，执行实现为下一阶段 blocker**

## 3. 未解决问题

1. **chip 持久化 migration**（下一阶段唯一 blocker）：chip 结果持久化表/migration 未实现，本轮仅完成计算边界和接口合同
2. `after_close_orchestrator` 关键路径未切换到 `compute_review_core_for_trade_date`（等待 chip 持久化 migration 完成后统一切换）
3. 浏览器真实链路验收待用户手工
4. CI 未处理（本轮不要求）

## 4. 验证

### 4.1 目标纯单元测试

`PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 pytest tests/test_change_20260729_003.py -v -p no:cacheprovider`

26 项全过，覆盖：
1. 量能均量比（mean/mean）
2. Rope 前缀不变性
3. OB 三事件时间线
4. SQZ_RELEASE 三方向与前置挤压量
5. regime_strength 读取正确
6. history 一次计算多日
7. 最后日与 core snapshot 一致
8. core 不调用 Node Cluster
9. 主 run 不等待 chip（接口合同）
10. chip 失败不影响 core

### 4.2 既有测试修正

- `test_first_pyramid_contract.py`：字段重命名 `current_vs_prev_volume_ratio` → `current_vs_prev_volume_mean_ratio`
- `test_dsa_bundle_consistency.py`：新增 `string_keys` 集合处理 `trend_transition` 字符串字段

### 4.3 Ruff

修改文件全部通过 `ruff check --no-cache`。
