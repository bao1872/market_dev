# Indicator Computation Map

> 文档状态：CURRENT DESIGN BASELINE
> 本文档不重复 baseline 字段（以 `docs/current/MANIFEST.md` 全局基线为准）。

本地图记录 Node Cluster 三链数据流真实代码路径、调用方→MDAS 参数→用途矩阵、缓存层和 ref/ 隔离边界。
指标计算合同见 `docs/current/08-indicator-calculation-contracts.md`。

## 1. Node Cluster 三链数据流

三链共用 `node_cluster_engine.compute_node_cluster_profile` 唯一入口，禁止任何链绕过 engine。

### 1.1 盘后链（feature_snapshot / after_close）

```
after_close_orchestrator
  → BarsScheduler 顺序: 原始日线刷新 → 公司行为/factor 重建 → 覆盖率门禁 → DSA → snapshot
  → FeatureSnapshotService.compute_feature_snapshot_for_date
    → MDAS.get_bars(1d, completed_only=True, end_date=trade_date, adjustment_as_of=trade_date)  # 250 根 qfq
    → MDAS.get_bars(15m, completed_only=True, end_date=trade_date, adjustment_as_of=trade_date) # 4000 根 qfq
    → node_cluster_engine.compute_node_cluster_profile(daily, bars_15m)  # 唯一 engine 调用
    → _compute_all_factors_for_bars(df_1d, "1d", precomputed_node_cluster=profile)
    → _compute_all_factors_for_bars(df_15m, "15m", precomputed_node_cluster=None)  # 单周期 15m
    → structural_payload {
        primary.1d.node_cluster,         # NodeClusterProfileResult 序列化
        primary.1d.cost_position,        # 兼容字段
        secondary.15m.timeframe_volume_profile
      }
    → StockFeatureSnapshot (schema_version=3)
    → finish_snapshot_run (读取实际 snapshot 数量)
```

**关键修复（CHANGE-20260718-004）**：盘后链 `_compute_cost_position_factors` 原先调用
`compute_unified_volume_profile(bars)` 只传单一周期 bars，已改为通过 engine 传入
`profile_df=bars_15m`，与详情链/监控链对齐。

### 1.2 详情链（indicator / API / frontend）

```
GET /api/v1/instruments/{id}/indicators
  → indicator_service.compute_all_indicators
    → MDAS.get_bars(1d, include_realtime=True)                    # daily_bars
    → MDAS.get_bars(15m, include_realtime=True, limit=4000)       # bars_15min
    → MDAS.get_bars(1m, include_realtime=True, limit=2)           # bars_minute
    → MarketDataContext
    → StrategyLoader.load("volume_node_monitor")
    → VolumeNodeMonitor.compute_indicators(context)
      → node_cluster_engine.compute_node_cluster_profile(bars_daily, bars_15min)  # 唯一 engine 调用
      → profile.profile_rows / peak_rows / poc_price / vah_price / val_price
    → 前端 StrategyChart 直接渲染 profile_rows/peak_rows
      （禁止重算，禁止 VA 过滤，VA 外 Peak 必须可见）
```

前端渲染契约：
- `profile_rows`：100 行 VP 档位，`is_peak`/`is_poc`/`is_value_area` 控制渲染样式
- `peak_rows`：全部 Peak 节点（含 VA 外），前端不得二次过滤
- `all_peak_prices`：用于 nearest node 计算（含 VA 外 Peak）

### 1.3 监控链（monitor）

```
MonitorBatchService.execute_monitor_cycle
  → _process_instrument_evaluation
    → _fetch_md_bars(1m, include_realtime=True)   # 最新已完成 1m
    → _compute_node_cluster_profile(bars_daily, bars_15min)  # 唯一 engine 调用
      → node_cluster_engine.compute_node_cluster_profile
        # 按 (instrument_id, daily_last_bar, 15m_last_bar) 缓存，TTL 300s
    → VolumeNodeMonitor.calculate_state  # 复用缓存 profile，derive_state_for_price
    → VolumeNodeMonitor.detect_events    # detect_crossover_signals（1m prev_close/cur_close）
    → _check_event_cooldown              # dedupe（TTL 600s，零变化）
    → StrategyEvent 写入
    → _send_merged_notification           # 飞书卡片（零变化）
```

监控链节奏（CHANGE-20260718-004 验证零变化）：
- 1m 穿越检测：prev_close → cur_close（2 根 1m bar）
- 事件去重 TTL：600s（`NODE_CLUSTER_EVENT_TTL_SECONDS`）
- Profile 缓存 TTL：300s（engine 内部）
- 节奏回归测试：`test_monitor_rhythm_regression.py`

## 2. 调用方→MDAS 参数→用途矩阵

| 调用方 | 周期 | completed_only | include_realtime | adjustment_as_of | limit | 用途 |
|---|---|---|---|---|---|---|
| feature_snapshot_service | 1d | True | False | trade_date | 250 | Node Cluster 价格范围 |
| feature_snapshot_service | 15m | True | False | trade_date | 4000 | Node Cluster 成交量分配 |
| indicator_service（详情） | 1d | False | True | N/A | 250 | daily_bars + 指标 |
| indicator_service（详情） | 15m | False | True | N/A | 4000 | bars_15min + Node Cluster |
| indicator_service（详情） | 1m | False | True | N/A | 2 | bars_minute 穿越检测 |
| monitor_batch_service | 1d | True | True | N/A | 250 | Node Cluster（缓存） |
| monitor_batch_service | 15m | True | True | N/A | 4000 | Node Cluster（缓存） |
| monitor_batch_service | 1m | False | True | N/A | 2 | 实时穿越检测 |
| capture/monitor 截图 | 按需 | 按需 | True（仅展示） | N/A | 按需 | 截图展示 |
| quote overlay | N/A | N/A | True | N/A | N/A | 实时报价（独立出口） |

**MarketDataAggregationService（MDAS）是唯一行情读取出口**：业务/API/indicators/tasks
禁止直接调用 repository 私有查询或自行复权。

## 3. 缓存层

### 3.1 indicator_cache（全局指标缓存）

- 文件：`backend/app/services/indicator_cache.py`
- 版本：`ALGORITHM_VERSION = "v11"`（v10→v11，CHANGE-20260718-004）
- 缓存内容：完整指标响应（MACD/BB/SMC/Node Cluster）
- 缓存键：含 `algorithm_version` + 全部 MDAS 契约参数 + 合同指纹
- 失效策略：版本变化自动失效

### 3.2 node_cluster_engine 内部缓存（Profile 级）

- 文件：`backend/app/services/node_cluster_engine.py`
- 缓存键：`(instrument_id, daily_last_bar, 15m_last_bar)`
- 缓存值：`NodeClusterProfileResult`（frozen）
- TTL：300s
- LRU：256 项，超限清空最早一半
- 失效策略：合同指纹变化（`NODE_CLUSTER_CONTRACT_FINGERPRINT`）使旧缓存自动失效

### 3.3 缓存隔离

- `include_smc=true/false` 状态缓存键隔离（SMC 默认禁用）
- `include_smc=false` 时 0 核心函数调用
- Node Cluster 缓存独立于 SMC 缓存

## 4. ref/ 隔离边界

- `ref/` 目录仅人工阅读，非运行依赖
- 生产代码（`backend/app/**/*.py`）、工具脚本（`tools/**/*.py`）禁止运行时
  `import`/`open`/`read`/`glob` `ref/` 目录
- 测试代码（排除 fixtures）禁止运行时 `open`/`read`/`glob` `ref/` 目录
- SMC 算法计算入口是生产代码 `smc_pine_core.py`；`ref/` 下文件为参考源（人工阅读），非运行依赖
- SMC 测试只读 `backend/tests/fixtures/smc_pine/*.csv`（TradingView CSV 不可用时 skip）
- `ref/smc_user_export.pine` 已 `git rm --cached`，不再纳入 git 跟踪
- 文档中 `ref/` 文件应称为"参考源（人工阅读）"或"历史路径"，禁止称为"真源"/"运行依赖"
- ref/ 隔离架构守护测试：`backend/tests/test_ref_isolation.py`
- ref/ 隔离文本扫描：`tools/check_docs_consistency.py` 规则 14

## 5. AST 架构守护

- 模板：`backend/tests/test_market_data_ssot_architecture.py`
- 新增：`backend/tests/test_node_cluster_architecture.py`
- 规则：除 `MarketDataAggregationService` 和 repository 内部外，生产模块禁止导入或调用
  repository 私有 `_query_*`、`_get_adj_factor_df`、`apply_adj_factor*`、旧 `bar_repository.get_bars`
- 禁止业务层自行周/月聚合或二次复权
- Node Cluster 三链必须通过 `node_cluster_engine.compute_node_cluster_profile` 调用

## 6. 变更历史

- CHANGE-20260718-004：初始版本（Node Cluster 三链数据流 + MDAS 矩阵 + 缓存层 + ref/ 隔离边界）
