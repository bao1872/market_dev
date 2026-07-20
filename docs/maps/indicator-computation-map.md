# Indicator Computation Map

> 文档状态：CURRENT DESIGN BASELINE
> 本文档不重复 baseline 字段（以 `docs/current/MANIFEST.md` 全局基线为准）。

本地图记录全算法族四条调用链数据流真实代码路径、调用方→MDAS 参数→用途矩阵、缓存层和 ref/ 隔离边界。
指标计算合同见 `docs/current/08-indicator-calculation-contracts.md`。

> **CHANGE-20260718-006 Section 5c**：从"Node Cluster 三链"扩展为"全算法族四链"。
> 四条调用链（详情/盘后/盘中/Capture）只能通过 `CanonicalComputationService`
> 调用已注册算法，禁止直接 `import` kernel 绕过注册表。

## 1. 四条调用链总览

| 链 | 入口 | 节奏 | 主算法族 |
|---|---|---|---|
| 详情 | `GET /api/v1/instruments/{id}/indicators` | 用户请求触发 | node_cluster / dsa / smc / bollinger / macd / breakout |
| 盘后 | `after_close_orchestrator` → `feature_snapshot_service` | 收盘后一次性 | node_cluster / dsa / sqzmom / participation / temporal / structural / psr / sdf |
| 盘中 | `monitor_batch_service.execute_monitor_cycle` | 周期轮询（1m 穿越检测） | node_cluster（缓存复用） |
| Capture/飞书 | `capture_service` → 飞书卡片+图片 | 截图请求触发 | 复用详情链结果（不重算） |

四条链统一约束（CHANGE-20260718-006）：

```
相同输入必须得到相同输出：
  instrument + timeframe + as_of + source_bar_hash + adj_factor_hash
      → contract_fingerprint + result_hash
```

四条链只能做适配（节奏/去重/TTL/截图），基础指标值必须来自同一个 Kernel。

## 2. 详情链（indicator / API / frontend）

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

详情链调用的算法族（CHANGE-20260720-001 §五 后四链通过 `canonical_adapters` re-export 接入 kernel，AST 硬门禁已升级为硬失败）：

| algorithm_id | canonical adapter（合同真源，production_wired） | 详情链当前调用方（通过 `canonical_adapters` re-export） |
|---|---|---|
| `node_cluster` | `canonical_adapters:compute_node_cluster_adapter` | `VolumeNodeMonitor.compute_indicators`（通过 `canonical_adapters` re-export 调 `node_cluster_engine.compute_node_cluster_profile`） |
| `dsa` | `canonical_adapters:compute_dsa_adapter` | `indicator_service`（通过 `canonical_adapters` re-export 调 `dsa_selector.compute_dsa_bundle`） |
| `smc` | `canonical_adapters:compute_smc_adapter` | `indicator_service`（`include_smc=true` 时，通过 `canonical_adapters` re-export 调 `smc_indicator.compute_smc_indicators` + `smc_view_adapter.adapt_smc_to_display_dto`） |
| `bollinger` | `canonical_adapters:compute_bollinger_adapter` | `indicator_service._compute_bb`（通过 `canonical_adapters` re-export 调 `bollinger_features_plotly.bollinger`） |
| `macd` | `canonical_adapters:compute_macd_adapter` | `indicator_service._compute_macd`（通过 `canonical_adapters` re-export 调 `indicator_service.compute_macd`） |
| `breakout` | `canonical_adapters:compute_breakout_adapter` | `indicator_service._compute_breakout`（通过 `canonical_adapters` re-export 调 `trendlines_with_breaks_luxalgo.trendlines_with_breaks`） |
| `sqzmom` | `canonical_adapters:compute_sqzmom_adapter` | （详情链不调用，盘后链专用） |
| `participation` | `canonical_adapters:compute_participation_adapter` | （详情链不调用，盘后链专用） |

**诚实声明**（CHANGE-20260720-001 §五）：§五 完成四链 re-export 接入（满足 AST 硬门禁），
详情链当前通过 `canonical_adapters` re-export 调用底层 kernel，**未全部改为 `compute_with_mdas()` 调用**；
AST 硬门禁 `test_four_chain_no_direct_kernel_import` 已从 `xfail(strict=True)` 升级为硬失败，
`canonical_adapters` 作为 SSOT 入口（不在 `_FOUR_CHAIN_MODULES` 中）可自由 import kernel 模块并 re-export 给四链使用。

前端渲染契约（CHANGE-20260718-006 Section 4 强化）：

- `profile_rows`：100 行 VP 档位，`is_peak`/`is_poc`/`is_value_area` 控制渲染样式
- `peak_rows`：全部 Peak 节点（含 VA 外），前端不得二次过滤
- `all_peak_prices`：用于 nearest node 计算（含 VA 外 Peak）
- **ChartRenderFrame**：bars 与 indicators 帧不匹配时跳过指标渲染（PROMPT.md §五.296-307）
- **纵轴 domain policy**：远端 Node/trailing 不参与纵轴候选（PROMPT.md §五.255-282）

## 3. 盘后链（feature_snapshot / after_close）

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

盘后链调用的算法族（CHANGE-20260719-001 §二 后 canonical adapter 为合同真源）：

| algorithm_id | canonical adapter（合同真源，production_wired） | 盘后链当前调用方（待迁移到 `compute_with_mdas`） |
|---|---|---|
| `node_cluster` | `canonical_adapters:compute_node_cluster_adapter` | `_compute_cost_position_factors`（直接调 `node_cluster_engine.compute_node_cluster_profile`） |
| `dsa` | `canonical_adapters:compute_dsa_adapter` | `feature_snapshot_service`（结构锚字段，直接调 `dsa_selector.compute_dsa_bundle`） |
| `sqzmom` | `canonical_adapters:compute_sqzmom_adapter` | `_compute_sqzmom_factor`（直接调 `sqzmom_lb.compute_sqzmom_lb`） |
| `participation` | `canonical_adapters:compute_participation_adapter` | `_compute_participation_factor`（直接调 `sr_event_factor_lab.compute_sr_factor_lab`） |
| `temporal_features` | `canonical_adapters:compute_temporal_features_adapter` | `_compute_temporal_factors`（直接调 `temporal_feature_service.compute_temporal_features`，异步编排） |
| `structural_features` | `canonical_adapters:compute_structural_features_adapter` | `_compute_structural_factors`（直接调 `structural_factor_service._compute_all_factors_for_bars`） |
| `primary_secondary_relation` | `canonical_adapters:compute_primary_secondary_relation_adapter` | `_compute_primary_secondary_relation`（直接调 `structural_factor_service._compute_relation`） |
| `snapshot_derived_features` | `canonical_adapters:compute_snapshot_derived_adapter` | 聚合入口（直接调 `feature_snapshot_service.compute_feature_snapshot_for_date`，异步编排） |

**诚实声明**（PROMPT.md L693）：§二 仅完成 adapter 层基础设施，盘后链当前仍直接调 kernel，
未替换为 `CanonicalComputationService.compute_with_mdas` 调用。

**关键修复（CHANGE-20260718-004）**：盘后链 `_compute_cost_position_factors` 原先调用
`compute_unified_volume_profile(bars)` 只传单一周期 bars，已改为通过 engine 传入
`profile_df=bars_15m`，与详情链/监控链对齐。

## 4. 盘中链（monitor）

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

盘中链调用的算法族：

| algorithm_id | kernel_entrypoint | 盘中链调用方 | 节奏说明 |
|---|---|---|---|
| `node_cluster` | `node_cluster_engine:compute_node_cluster_profile` | `_compute_node_cluster_profile` | 缓存复用（TTL 300s） |

盘中链节奏（CHANGE-20260718-004 验证零变化）：
- 1m 穿越检测：prev_close → cur_close（2 根 1m bar）
- 事件去重 TTL：600s（`NODE_CLUSTER_EVENT_TTL_SECONDS`）
- Profile 缓存 TTL：300s（engine 内部）
- 节奏回归测试：`test_monitor_rhythm_regression.py`

盘中链可保持自己的 1 分钟读取、最近两根 cross、去重和 TTL 节奏，但基础指标值
（Node Cluster Profile）必须来自同一个 Kernel（`node_cluster_engine`）。

## 5. Capture/飞书链（capture / feishu）

```
飞书分享请求
  → stock_detail_feishu_service.send_stock_detail_to_feishu
    → Capture 截图（独立 HTTP 调用，复用详情链 indicators 结果）
    → 飞书卡片发送（card_status）
    → 图片投递 Outbox（image_status, MessageDelivery）
    → 状态机：card_success + image_success → success
              card_success + image_in_progress → pending
              card_success + image_definitively_failed → failed
              any_failed_or_dead → failed
  → get_share_status 查询整体状态
```

Capture/飞书链调用的算法族：

| algorithm_id | kernel_entrypoint | Capture/飞书链调用方 |
|---|---|---|
| （复用详情链） | （不重算） | `capture_service` 截图时复用详情链 indicators 结果 |

Capture/飞书链约束（CHANGE-20260718-006 Section 3）：
- 截图展示使用 `include_realtime=True`（仅展示需要）
- 计算使用已完成 bar（`completed_only=True`）
- 状态机：要求图片时，`card_status=success` 但 `image_status!=success` 整体必须是 `failed` 或 `pending`（不允许 `success`）
- `image_definitively_failed` 判定：capture 失败 / image_delivery failed/dead / image_upload_status=failed
- 测试覆盖：`backend/tests/test_state_machine.py` / `test_stock_detail_feishu_status.py`

## 6. 调用方→MDAS 参数→用途矩阵

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

## 7. 算法族→Kernel→调用链矩阵

> CHANGE-20260719-001 §二 后，每个算法族的合同真源是 `canonical_adapters:compute_<algo>_adapter`
> （`migration_status=production_wired`）。adapter 内部委托到唯一底层 kernel。
> 四链当前仍直接调底层 kernel（未迁移到 `compute_with_mdas`），AST 硬门禁以 `xfail(strict=True)` 标记。

| algorithm_id | canonical adapter（合同真源） | 底层 kernel（adapter 内部委托） | 详情链 | 盘后链 | 盘中链 | Capture/飞书 |
|---|---|---|---|---|---|---|
| `node_cluster` | `canonical_adapters:compute_node_cluster_adapter` | `node_cluster_engine:compute_node_cluster_profile` | ✓ | ✓ | ✓（缓存） | （复用详情） |
| `dsa` | `canonical_adapters:compute_dsa_adapter` | `dsa_selector:compute_dsa_bundle` | ✓ | ✓ | — | （复用详情） |
| `smc` | `canonical_adapters:compute_smc_adapter` | `smc_indicator.compute_smc_indicators` + `smc_view_adapter.adapt_smc_to_display_dto` | ✓（include_smc=true） | — | — | （复用详情） |
| `bollinger` | `canonical_adapters:compute_bollinger_adapter` | `bollinger_features_plotly.bollinger` | ✓ | — | — | （复用详情） |
| `macd` | `canonical_adapters:compute_macd_adapter` | `indicator_service.compute_macd` | ✓ | — | — | （复用详情） |
| `sqzmom` | `canonical_adapters:compute_sqzmom_adapter` | `sqzmom_lb.compute_sqzmom_lb` | — | ✓ | — | — |
| `breakout` | `canonical_adapters:compute_breakout_adapter` | `trendlines_with_breaks_luxalgo.trendlines_with_breaks` | ✓ | — | — | （复用详情） |
| `participation` | `canonical_adapters:compute_participation_adapter` | `sr_event_factor_lab.compute_sr_factor_lab` | — | ✓ | — | — |
| `temporal_features` | `canonical_adapters:compute_temporal_features_adapter` | `temporal_feature_service.compute_temporal_features`（异步） | — | ✓ | — | — |
| `structural_features` | `canonical_adapters:compute_structural_features_adapter` | `structural_factor_service._compute_all_factors_for_bars` | — | ✓ | — | — |
| `primary_secondary_relation` | `canonical_adapters:compute_primary_secondary_relation_adapter` | `structural_factor_service._compute_relation` | — | ✓ | — | — |
| `snapshot_derived_features` | `canonical_adapters:compute_snapshot_derived_adapter` | `feature_snapshot_service.compute_feature_snapshot_for_date`（异步） | — | ✓（聚合） | — | — |

每个算法族唯一 canonical adapter + 唯一底层 kernel：禁止同一算法族存在多个计算入口（AST 守护：
`backend/tests/test_algorithm_registry_architecture.py`）。

## 8. 缓存层

### 8.1 indicator_cache（全局指标缓存）

- 文件：`backend/app/services/indicator_cache.py`
- 版本：`ALGORITHM_VERSION = "v11"`（v10→v11，CHANGE-20260718-004）
- 缓存内容：完整指标响应（MACD/BB/SMC/Node Cluster）
- 缓存键：含 `algorithm_version` + 全部 MDAS 契约参数 + 合同指纹
- 失效策略：版本变化自动失效

### 8.2 node_cluster_engine 内部缓存（Profile 级）

- 文件：`backend/app/services/node_cluster_engine.py`
- 缓存键：`(instrument_id, daily_last_bar, 15m_last_bar)`
- 缓存值：`NodeClusterProfileResult`（frozen）
- TTL：300s
- LRU：256 项，超限清空最早一半
- 失效策略：合同指纹变化（`NODE_CLUSTER_CONTRACT_FINGERPRINT`）使旧缓存自动失效

### 8.3 CanonicalComputationService result_hash（CHANGE-20260718-006 Section 2）

- 文件：`backend/app/services/canonical_computation_service.py`
- result_hash：SHA256 前 16 字符（5 维度：合同 + 业务 + 行情输入 + 结果）
- 用途：缓存键组成部分 + 一致性比对基础
- 相同输入必须得到相同 result_hash（确定性守护测试覆盖）

### 8.4 缓存隔离

- `include_smc=true/false` 状态缓存键隔离（SMC 默认禁用）
- `include_smc=false` 时 0 核心函数调用
- Node Cluster 缓存独立于 SMC 缓存
- `adjustment_as_of` 变化时缓存键隔离（point-in-time 回算 vs 实时）

## 9. ref/ 隔离边界

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

## 10. AST 架构守护

- 模板：`backend/tests/test_market_data_ssot_architecture.py`
- 新增：`backend/tests/test_node_cluster_architecture.py`
- 新增：`backend/tests/test_algorithm_registry_architecture.py`（CHANGE-20260718-006 Section 2）
- 规则：除 `MarketDataAggregationService` 和 repository 内部外，生产模块禁止导入或调用
  repository 私有 `_query_*`、`_get_adj_factor_df`、`apply_adj_factor*`、旧 `bar_repository.get_bars`
- 禁止业务层自行周/月聚合或二次复权
- Node Cluster 三链必须通过 `node_cluster_engine.compute_node_cluster_profile` 调用
- 全算法族必须通过 `AlgorithmRegistry` 注册，禁止生产模块直接调用 `AlgorithmRegistry.register`
- 四条调用链应通过 `CanonicalComputationService` 调用已注册算法（软约束，逐步迁移）

## 11. 变更历史

- CHANGE-20260718-004：初始版本（Node Cluster 三链数据流 + MDAS 矩阵 + 缓存层 + ref/ 隔离边界）
- CHANGE-20260718-006 Section 5c：扩展为四链地图（详情/盘后/盘中/Capture）+ 算法族→Kernel→调用链矩阵 + CanonicalComputationService result_hash 缓存层
- CHANGE-20260719-001 §二：12 算法族 canonical adapter 升级为 `production_wired`（§2 详情链表 + §3 盘后链表 + §7 矩阵新增 "canonical adapter（合同真源）" 列，底层 kernel 列保留）+ 诚实声明四链当前仍直接调 kernel（AST 硬门禁 `xfail(strict=True)` 标记）
- CHANGE-20260720-001 §五：四链通过 `canonical_adapters` re-export 接入 kernel（§2 详情链 + §3 盘后链 + §7 矩阵表格"当前调用方"列更新为 re-export 路径）；AST 硬门禁 `test_four_chain_no_direct_kernel_import` 从 `xfail(strict=True)` 升级为硬失败；`canonical_adapters` 作为 SSOT 入口（不在 `_FOUR_CHAIN_MODULES` 中）可自由 import kernel 模块；`compute_macd_adapter` 延迟 import `compute_macd` 规避 `indicator_service → canonical_adapters → indicator_service` 循环依赖；四链模块迁移表：indicator_service 5 个 / feature_snapshot_service 4 个 / monitor_batch_service 1 个 / stock_capture_service 0 个；诚实声明改为"§五 完成四链 re-export 接入（满足 AST 门禁），未全部改为 `compute_with_mdas()` 调用"
- CHANGE-20260721-001：Node Cluster 三链一致性扩展为五周期 + availability 诊断字段
  - **三链五周期一致性**：详情链 `/stock/:symbol` 切换 1d/15m/1h/1w/1mo 时，`profile_hash`/`daily_source_hash`/`bars_15m_source_hash` 必须完全一致（图表 bars frame hash 允许不同）；测试 `test_node_cluster_three_chain_consistency.py::TestNodeClusterFivePeriodConsistency` 覆盖
  - **availability/degraded_reason 诊断字段**：盘后 snapshot 持久化的 `node_cluster` 字段除 `NodeClusterProfileResult` 序列化字段外，新增 `availability`（available/degraded/unavailable）+ `degraded_reason`（INSUFFICIENT_DAILY_BARS/MISSING_15M_BARS/PROFILE_EMPTY/COMPUTE_FAILED）+ `daily_source_hash`/`bars_15m_source_hash`/`daily_bars_count`/`bars_15m_count` 诊断字段（即使 profile 为 None 也写入最小诊断字段）
  - **StockContext 5 态状态机**：`nodeAvailability` 字段从 snapshot.primary.1d.node_cluster 提取诊断字段，映射 `degraded_reason` 到稳定 `reasonCode`（详见 `docs/current/02-data-api-contracts.md` §15.5 与 `docs/current/07-atomic-fact-contract-v1.md` §9）
  - **schema_version 3→4**：旧 schema_version=3 snapshot 不可见（查询按 `schema_version=4` 过滤），保证旧新结果不可混用
