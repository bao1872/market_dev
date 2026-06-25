# 指标口径统一清单（Volume Profile 相关）

> 生成时间：2026-06-25
> 扫描范围：`/root/web_dev/backend/app`（含 services / strategy / api / strategy_assets / scripts / tests）
> 参考真源：`/root/web_dev/ref/交易/app/monitoring.py`

## 1. 目标统一参数

所有监控/图表/通知链路统一使用以下 Volume Profile 参数：

| 参数 | 值 | 说明 |
|------|-----|------|
| VP_LOOKBACK | 360 | 日线回看根数 |
| VP_ROWS | 100 | 价格档位行数 |
| VP_VALUE_AREA_PCT | 0.70 | 价值区域占比 |
| VP_PEAK_DETECTION_PCT | 0.05 | Peak 检测百分比 |
| VP_NODE_THRESHOLD_PCT | 0.01 | Node 成交量阈值 |

> 参考真源：`ref/交易/app/monitoring.py` 第 107-112 行。

## 2. Volume Profile 计算入口清单

### 2.1 权威算法实现

| # | 文件 | 入口 | 当前角色 | 备注 |
|---|------|------|----------|------|
| 1 | `backend/app/strategy_assets/algorithms/features/luxalgo_volume_profile_pytdx_15m_aligned.py` | `VolumeProfileConfig` / `compute_volume_profile` / `VolumeProfileResult` / `extract_nearest_nodes` | **当前唯一核心算法库** | 输出 `profile_df`（含 `total_volume` / `bullish_volume` / `bearish_volume` / `is_peak` / `is_poc` / `is_value_area` / `price_mid` / `price_low` / `price_high`）、`all_peak_prices`、`peak_df`、`poc_price`、`vah_price`、`val_price`、`lowest_price`、`highest_price`、`price_step` |
| 2 | `backend/app/strategy_assets/algorithms/features/pavp_tv_fixed_params_factors.py` | 内部函数 `_historical_fixed_segment` / `_developing_segment` | 另一套固定区间 Volume Profile 算法 | 使用 `VALUE_AREA_PCT=0.68`、`PROFILE_LEVELS=32`、自定义 Pivot 检测，**未参与监控/图表链路**，但属于概念重复的 volume profile 实现 |
| 3 | `backend/app/strategy_assets/algorithms/features/dynamic_volume_profile_oscillator.py` | `calculate_volume_profile_metrics` | DVPO 指标内部计算 | 基于滚动 price/volume 计算 `vwap_level` / `price_deviation`，与 LuxAlgo 算法不同 |

### 2.2 业务调用入口（监控/指标/图表/通知）

| # | 文件 | 使用字段/函数 | 当前行为 | 与真源关系 |
|---|------|---------------|----------|------------|
| 4 | `backend/app/strategy/monitors/volume_node_monitor.py` | `_compute_volume_profile`（调用 LuxAlgo）<br>`calculate_state` 产出 `current_price/upper_node/lower_node/position_0_1/poc_price/last_touched_node`<br>`compute_indicators` 产出时间序列 `upper_node/lower_node/poc_price/position_0_1/current_price`<br>`_detect_node_crossover_signals` 使用 `all_peak_prices` | 调用 LuxAlgo，参数与真源一致，但参数在该文件重复定义 | 功能正确但存在**参数硬编码重复**和**辅助计算重复** |
| 5 | `backend/app/strategy/monitors/watchlist_monitor.py` | 委托 `VolumeNodeMonitor` 计算，合并 `upper_node/lower_node/position_0_1/poc_price/last_touched_node` 到 MonitorState | 薄包装 | 无独立计算，依赖 VN monitor |
| 6 | `backend/app/services/monitor_batch_service.py` | `_compute_volume_profile`（调用 LuxAlgo）<br>用于 `render_monitoring_chart` | 与 `volume_node_monitor.py` 中参数完全相同的重复实现 | **重复计算入口** |
| 7 | `backend/app/services/monitor_chart_renderer.py` | `render_monitoring_chart` 读取 `profile.profile_df` 的 `bullish_volume` / `bearish_volume` / `total_volume` / `is_peak` / `price_step`，读取 `profile.peak_df` 的 `price_mid` / `bullish_volume` / `bearish_volume` | 仅可视化 | 应复用统一结果对象 |
| 8 | `backend/app/services/indicator_service.py` | `compute_all_indicators` 调用 `runtime.compute_indicators(context)` -> `volume_node_monitor.compute_indicators` | 通过 VN monitor 间接调用 | 改造后间接通过统一模块 |
| 9 | `backend/app/api/indicators.py` | 返回 `upper_node` / `lower_node` / `poc_price` / `position_0_1` / `current_price` | API 透传 | 依赖 indicator_service |
| 10 | `backend/app/api/watchlist.py` | 解析 `upper_node` / `lower_node` / `last_touched_node` 的 JSON 结构 | 仅反序列化 | 无计算 |
| 11 | `backend/scripts/verify_monitor_alignment.py` | 直接调用 LuxAlgo `compute_volume_profile` | 对齐验证脚本 | 可继续直接调用核心算法库 |

### 2.3 参考真源（只读，不修改）

| # | 文件 | 入口 | 说明 |
|---|------|------|------|
| 12 | `ref/交易/app/monitoring.py` | `compute_volume_profile`（从 `features.luxalgo_volume_profile_pytdx_15m_aligned` 导入）<br>`detect_node_cluster_signals`<br>`render_monitoring_chart`<br>常量 `VP_LOOKBACK/VP_ROWS/VP_VALUE_AREA_PCT/VP_PEAK_DETECTION_PCT/VP_NODE_THRESHOLD_PCT` | 业务参考实现，含参数默认值和渲染逻辑 |

## 3. 重复/冲突点

### 3.1 重复计算入口

- `backend/app/strategy/monitors/volume_node_monitor.py` 与 `backend/app/services/monitor_batch_service.py` 各自定义了参数完全相同的 `_compute_volume_profile` / `VolumeProfileConfig` 调用逻辑。
- 两处都硬编码了 `VP_LOOKBACK=360`、`VP_ROWS=100`、`VP_VALUE_AREA_PCT=0.70`、`VP_PEAK_DETECTION_PCT=0.05`、`VP_NODE_THRESHOLD_PCT=0.01`。

### 3.2 业务逻辑冲突

- **无直接语义冲突**：当前 monitor_batch_service 与 volume_node_monitor 使用同一 LuxAlgo 算法和同一组参数，输出一致。
- **潜在视觉冲突**：`monitor_chart_renderer.py` 当前 K 线与迷你多空柱使用 "绿涨红跌"（`#26a69a` / `#ef5350`），而 spec 任务 2 要求改为 A 股 "红涨绿跌"。本次任务 1 若仅替换数据源而不改配色，则不会引入新冲突，但后续任务 2 必须调整。

### 3.3 注释/代码陈旧

- `backend/app/services/monitor_chart_renderer.py` 顶部 docstring 描述为 "K线+布林带+筹码峰色带+POC/VAH/VAL 标注"，但当前实现未绘制 POC/VAH/VAL 水平线，仅绘制筹码峰色带和迷你多空柱。
- `backend/app/strategy/monitors/volume_node_monitor.py` 多处注释声称 "SSOT"，但实际上辅助计算（如 `position_0_1`、POC 行查找、上下 node 查找）在 monitor 内重复实现，未集中到共享模块。

## 4. 建议清理/复用方案

1. **新增共享模块**：`backend/app/strategy_assets/algorithms/features/unified_volume_profile.py`
   - 封装 `compute_unified_volume_profile(df, profile_df=None, main_period="day")`，内部固定使用真源参数。
   - 返回结果对象包含：
     - `poc_price` / `vah_price` / `val_price`
     - `peak_rows`（`profile_df` 中 `is_peak=True` 的行，含 `price_mid` / `bullish_volume` / `bearish_volume` / `total_volume` / `is_peak`）
     - `bullish_volume` / `bearish_volume`（整列或聚合后的序列）
     - `upper_node` / `lower_node`（当前价上下最近 peak 节点 JSON）
     - `position_0_1`
   - 提供 `extract_nearest_nodes` 兼容函数。

2. **删除/替换重复入口**：
   - `volume_node_monitor.py`：删除 `_compute_volume_profile` 中的 `VolumeProfileConfig` 构造逻辑，改为调用统一模块；保留 `calculate_state`/`compute_indicators` 的编排职责。
   - `monitor_batch_service.py`：删除 `_compute_volume_profile`，改为调用统一模块。
   - `monitor_chart_renderer.py`：接收统一结果对象，保持渲染逻辑不变（配色由任务 2 处理）。

3. **保留的独立算法**：
   - `pavp_tv_fixed_params_factors.py` 与 `dynamic_volume_profile_oscillator.py` 不用于监控链路，暂时保留，但清单中标记为 "非监控口径"。

4. **验证**：
   - `pytest tests/ -q` 全量通过。
   - `python -m app.strategy_assets.algorithms.features.unified_volume_profile` 模块自测通过。

## 5. 清理结果（任务 1 实施记录，2026-06-25）

### 5.1 已完成清理

| # | 文件 | 清理动作 | 状态 |
|---|------|----------|------|
| 1 | `backend/app/strategy_assets/algorithms/features/unified_volume_profile.py` | 共享模块已建立：`compute_unified_volume_profile` + `UnifiedVolumeProfileResult`，固定真源参数；新增 `peak_df` 别名属性以保持与 `VolumeProfileResult` 接口兼容 | ✅ 完成 |
| 2 | `backend/app/strategy/monitors/volume_node_monitor.py` | 删除内部 `_compute_volume_profile` / `_node_row_to_json` / `_lookup_node_by_price` / `_lookup_poc_node` / `_find_touched_node` / `_prepare_bars_for_vp` 等重复辅助函数；`calculate_state` 与 `compute_indicators` 改为调用 `compute_unified_volume_profile` + `state_for_price`；补全此前被截断的 `calculate_state` 返回语句、`detect_events` 方法与模块自测入口 | ✅ 完成 |
| 3 | `backend/app/services/monitor_batch_service.py` | 删除 `_compute_volume_profile` 内部的 `VolumeProfileConfig` 构造与 `vp_compute` 直调；改为调用 `compute_unified_volume_profile`，返回 `UnifiedVolumeProfileResult`；移除原 `try/except: return None` 静默吞异常，改为补上下文后 re-raise（由上层 `_render_instrument_chart` 统一降级） | ✅ 完成 |
| 4 | `backend/app/services/monitor_chart_renderer.py` | 无需改动代码（按鸭子类型访问 `profile_df` / `peak_df` / `price_step`）；仅更新 docstring 注释说明可接收 `UnifiedVolumeProfileResult \| VolumeProfileResult` | ✅ 完成 |
| 5 | `backend/app/services/indicator_service.py` | 经扫描无独立 volume profile 计算，通过 `runtime.compute_indicators` 间接走 `VolumeNodeMonitor`，自动继承统一模块 | ✅ 无需改动 |

### 5.2 验证结果

- 共享模块自测：`python -m app.strategy_assets.algorithms.features.unified_volume_profile` → `OK`（POC/VAH/VAL/profile_df 字段/peak_rows/state_for_price 全部通过）
- VolumeNodeMonitor 自测：`python -m app.strategy.monitors.volume_node_monitor` → `OK`
- 后端全量测试：`pytest tests/ -q` → **315 passed, 0 failed, 0 error**（3 个 warning 为预先存在的 sqlalchemy/httpx 弃用警告，与本次改动无关）

### 5.3 当前唯一真源调用链

```
volume_node_monitor.calculate_state / compute_indicators
        └──> compute_unified_volume_profile (共享模块)
                └──> luxalgo_volume_profile_pytdx_15m_aligned.compute_volume_profile (底层算法 SSOT)

monitor_batch_service._compute_volume_profile
        └──> compute_unified_volume_profile (共享模块)
                └──> (同上)

monitor_chart_renderer.render_monitoring_chart
        └──> 接收 UnifiedVolumeProfileResult（鸭子类型访问 profile_df/peak_df/price_step）
```

业务层已无独立的 `VolumeProfileConfig` 构造或 `compute_volume_profile` 直调，所有参数固定在 `unified_volume_profile.py` 顶部常量（VP_LOOKBACK=360 等）。
