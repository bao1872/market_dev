# ADR-0002: Node Cluster 输入契约隔离

- **状态**: accepted
- **日期**: 2026-07-20
- **关联**: CHANGE-20260720-001 / PRD V2.0 §6.2 H-1

## Context（背景）

旧实现存在两个关键问题：

1. **`MarketDataContext.bars_daily = macd_bars`**（CHANGE-20260720-001 修复前）：将展示周期的 bars 直接赋值给 `bars_daily`，导致 Node Cluster 主输入随展示周期变化。例如用户切换到 15m 时，Node Cluster 收到的是 15m bars 而非 250 根日线，违反固定契约。
2. **`_REQUIRED_INPUTS[WATCHLIST_MONITOR]` 错误声明**：声明为 `daily` 但实际包含 `VolumeNodeMonitor`，导致 15m/1m 数据未按需加载，触发运行时错误。

固定契约（AGENTS §七.5）：
```
1d  = 250 根日线
15m = 250 * 16 = 4000 根
1m  = 2 根已完成 Bar
```

约束：
- 图表显示数量、指标输出数量、Node 内部输入数量必须分离
- 五周期（1d/15m/1h/1w/1mo）切换时 Node Cluster `profile_hash` 必须完全一致
- 禁止飞书舞台 90 bar 展示参数进入任何指标计算逻辑

## Decision（决策）

### 1. `MarketDataContext` 字段拆分

```python
@dataclass
class MarketDataContext:
    # 展示周期（图表与 DSA/MACD/SQZMOM 图层使用）
    bars_display: pd.DataFrame
    display_timeframe: str

    # 计算输入（与展示周期完全分离）
    bars_daily: pd.DataFrame      # 250 根已完成前复权日线
    bars_15min: pd.DataFrame      # 4000 根 15m bars（Node VP 辅助）
    bars_minute: pd.DataFrame     # 2 根已完成 1m bar（触发检测）
```

### 2. `_REQUIRED_INPUTS` 映射（`indicator_service.py`）

```python
_REQUIRED_INPUTS: dict[str, frozenset[str]] = {
    "dsa_selector":          frozenset({"daily"}),
    "volume_node_monitor":   frozenset({"daily", "15min", "minute"}),
    "bb_monitor":            frozenset({"daily"}),
    "watchlist_monitor":     frozenset({"daily"}),
}
```

`_determine_required_bars()` 合并所有注册策略需求返回 `frozenset[str]`；不需要的数据类型为空 `pd.DataFrame()`。

### 3. 详情 API Node 输入独立（`/stock/:symbol`）

- 详情 API 必须独立输出 `data.node_cluster`，使用 fixed completed qfq 250 1d + 4000 15m bars
- 不加载 1m（除非 `watchlist_monitor` 触发检测需要）
- Node Cluster 主输入独立于展示周期
- `TestNodeClusterFivePeriodConsistency` 覆盖五周期 `profile_hash` 一致性

### 4. SMC 日线主周期

- `SmcMonitor` 主输入为已完成前复权日线（`bars_daily`），1m 仅触发检测
- 调用 `canonical_adapters.compute_smc_adapter`
- FVG 完全排除（AGENTS §七.14）

## Consequences（后果）

- **正面影响**：
  - 五周期切换 Node Cluster `profile_hash` 完全一致（之前会变化）
  - 数据加载按需进行，避免无条件读取 750 天 15m/1m
  - 15m 数据加载从 ~750 天降至 400 天，节省数据库查询时间
  - 1m 数据加载从全量降至 5 天，显著降低负载

- **负面影响**：
  - `MarketDataContext` 字段增加，构造函数变复杂
  - 测试需覆盖五周期一致性（新增 `TestNodeClusterFivePeriodConsistency`）

- **风险与缓解**：
  - 风险：新增策略未同步更新 `_REQUIRED_INPUTS` 会得到空 DataFrame
  - 缓解：默认 fallback `frozenset({"daily"})`，但策略注册时必须同步更新映射
  - 风险：`bars_daily` 与 `bars_display` 在 1d 周期时数据重复
  - 缓解：MDAS 缓存层去重，相同请求参数复用同一 DataFrame

- **后续约束**：
  - 写入 AGENTS §七.5「Node Cluster 固定契约」硬规则（禁止修改 250/4000/2）
  - 任何修改 `indicator_service.py` / `MarketDataContext` 必须运行 `TestNodeClusterFivePeriodConsistency`
  - 任何修改 `watchlist_monitor` / `volume_node_monitor` 策略必须同步更新 `_REQUIRED_INPUTS`
