# 08 - 指标计算合同

> 文档状态：CURRENT DESIGN BASELINE
> 本文档不重复 baseline 字段（以 `docs/current/MANIFEST.md` 全局基线为准）。

## 1. 概述

本文档逐指标记录业务含义、输入、参数、语义、输出、调用方、版本、允许差异、禁止变化、验收样本。
任何修改产品/业务行为的人必须先读本文档。

三层真源分离：

| 角色 | 文件 | 职责 |
|---|---|---|
| 数值参数真源 | `backend/app/constants/indicator_contract.py` | 根数/行数/阈值/TTL（"用多少"） |
| 语义真源 | `backend/app/contracts/indicator_semantics.py` | 输入口径/过滤规则/输出口径（"是什么/怎么做"） |
| 计算内核 | `backend/app/services/node_cluster_engine.py` | 唯一业务入口，调用底层 VP（"在哪里算"） |

**三链同核**：盘后链（feature_snapshot / after_close）、详情链（indicator / API / frontend）、
监控链（monitor）共用 `node_cluster_engine.compute_node_cluster_profile` 唯一入口，禁止任何链
绕过 engine 自行调用 `compute_unified_volume_profile`。

## 2. Node Cluster（筹码分布 / Volume Profile）

### 2.1 业务含义

Node Cluster（筹码分布 / 成交量分布图）将价格区间划分为 100 个等宽档位，按各周期成交量
分配到对应档位，识别主要支撑/阻力节点（Peak）、价值区域（Value Area, VA）、POC（Point of Control）。
用于判断当前价格所处位置（VA 内/VAH 上方/VAL 下方）及最近节点距离。

### 2.2 输入

| 周期 | 根数 | 用途 | 复权口径 |
|---|---|---|---|
| 1d | 250 根 | 决定价格范围（最高/最低价 → 100 档） | completed qfq |
| 15m | 4000 根 | 分配成交量到价格档位 | completed qfq |
| 1m | 2 根 | 盘中穿越检测（监控链实时） | include_realtime=True |

- daily/15m: `completed_only=True`，`adjustment_as_of` 锚定业务日，禁止未来除权事件泄漏
- 监控链 1m: `include_realtime=True`（实时穿越），但 Profile 仍用 completed daily/15m

### 2.3 参数（引用 `indicator_contract.py` 常量）

| 常量 | 值 | 说明 |
|---|---|---|
| `DAILY_HISTORY_BARS` | 250 | 日线回看根数（唯一字面量） |
| `NODE_CLUSTER_PRIMARY_PERIOD` | `"1d"` | 主周期 |
| `NODE_CLUSTER_PRIMARY_BARS` | 250 (= `DAILY_HISTORY_BARS`) | 主周期取数根数 |
| `NODE_CLUSTER_LOW_PERIOD` | `"15m"` | 低周期 |
| `NODE_CLUSTER_LOW_BARS` | 4000 (= 250×16) | 低周期取数根数 |
| `NODE_CLUSTER_MINUTE_BARS` | 2 | 1m 取数根数 |
| `NODE_CLUSTER_15M_BARS_PER_DAY` | 16 | 每交易日 15m 根数 |
| `NODE_CLUSTER_EVENT_TTL_SECONDS` | 600 | 事件去重 TTL |

### 2.4 语义（引用 `indicator_semantics.py` 冻结项）

冻结的语义不变量（任何变更必须 bump `NODE_CLUSTER_CONTRACT_FINGERPRINT`）：

1. 1d 最近 250 根已完成 qfq 日线决定价格范围
2. 15m 最近 4000 根已完成 qfq bar 分配成交量
3. 1m 最近 2 根已完成 bar 只用于盘中穿越检测
4. Peak 搜索域为完整 100 行 Profile
5. **`value_area_filters_peaks = False`（VA 外 Peak 有效，禁止过滤）**
6. VAL/VAH 仅用于价值区显示/位置分类，不得排除 VA 外 Peak
7. **nearest node 来自全部 Peak（含 VA 外）**
8. 三链（盘后 / 详情 / 监控）同 stock/as_of/输入 → `profile_hash` 必须完全一致

### 2.5 输出字段（`NodeClusterProfileResult`）

不可变 frozen dataclass，所有字段构造时确定：

| 字段 | 类型 | 说明 |
|---|---|---|
| `instrument_id` | `int` | 股票 ID |
| `as_of_date` | `date \| None` | 锚定业务日 |
| `profile_rows` | `list[dict]` | 完整 100 行 VP 价格档位快照（含 `is_peak`/`is_poc`/`is_value_area`） |
| `peak_rows` | `list[dict]` | 全部 Peak 节点快照（含 VA 外 Peak，禁止过滤） |
| `all_peak_prices` | `list[float]` | 全部 Peak 价格列表（含 VA 外） |
| `poc_price` | `float \| None` | POC 价格 |
| `vah_price` | `float \| None` | Value Area High |
| `val_price` | `float \| None` | Value Area Low |
| `value_area_volume_ratio` | `float` | VA 成交量占比 |
| `total_volume` | `float` | 总成交量 |
| `price_step` | `float` | 价格档位宽度 |
| `price_min` | `float` | 价格范围下界 |
| `price_max` | `float` | 价格范围上界 |
| `algorithm_version` | `str` | 算法版本（`nc-v1`） |
| `output_schema_version` | `int` | 输出 schema 版本（`1`） |
| `contract_fingerprint` | `str` | 合同指纹（`nc-cf-v1`） |
| `profile_hash` | `str` | 100 行 profile 内容 hash（三链一致性断言用） |
| `computed_at` | `datetime` | 计算时间戳 |
| `degraded` | `bool` | 是否降级（数据不足等） |
| `degraded_reason` | `str \| None` | 降级原因 |

鸭子类型适配器（委托 `_vp_result`，兼容旧消费者）：
- `profile_df` → `pd.DataFrame`（100 行 VP）
- `peak_df` → `pd.DataFrame | None`（Peak 节点）

### 2.6 调用方（三链）

| 链 | 调用方 | engine 入口 |
|---|---|---|
| 盘后 | `feature_snapshot_service` → `_compute_cost_position_factors` | `compute_node_cluster_profile(daily, bars_15m)` |
| 详情 | `VolumeNodeMonitor.compute_indicators` | `compute_node_cluster_profile(bars_daily, bars_15min)` |
| 监控 | `monitor_batch_service._compute_node_cluster_profile` | `compute_node_cluster_profile(bars_daily, bars_15min)` |

三链均通过 `node_cluster_engine.compute_node_cluster_profile` 唯一入口调用，engine 内部
按 `(instrument_id, daily_last_bar, 15m_last_bar)` 缓存 Profile（TTL 300s，LRU 256 项）。

### 2.7 版本

| 版本标识 | 当前值 | 说明 |
|---|---|---|
| `NODE_CLUSTER_ALGORITHM_VERSION` | `nc-v1` | engine 算法版本 |
| `NODE_CLUSTER_OUTPUT_SCHEMA_VERSION` | `1` | `NodeClusterProfileResult` 字段版本 |
| `NODE_CLUSTER_CONTRACT_FINGERPRINT` | `nc-cf-v1` | 语义合同指纹（变更时自动失效缓存） |
| `indicator_cache.ALGORITHM_VERSION` | `v11` | 全局指标缓存版本（v10→v11，CHANGE-20260718-004） |

### 2.8 允许差异

- 三链同 stock/as_of/输入 → `profile_hash` **必须完全一致**（无差异允许）
- 不同 stock 或不同 as_of → `profile_hash` 自然不同（预期行为）
- 监控链 1m 穿越检测使用实时 1m bar，但 Profile 本身仍用 completed daily/15m（Profile hash 不受 1m 影响）

### 2.9 禁止变化

1. VA 外 Peak 不得过滤（`value_area_filters_peaks = False` 不可改为 True）
2. 1d/15m 根数不得偏离 250/4000（引用 `DAILY_HISTORY_BARS` / `NODE_CLUSTER_LOW_BARS` 常量）
3. `completed_only` / `adjustment_as_of` 语义不得弱化（禁止用 partial bar 或未来因子）
4. 三链不得绕过 engine 自行调用 `compute_unified_volume_profile`
5. 盘后链不得只传单一周期 bars（必须同时传 daily + 15m）
6. `profile_hash` 计算不得引入非确定性因素（如时间戳、随机数）

### 2.10 验收样本

- **000725（京东方A）**：VAH 上方 Peak 可见（VA 外 Peak 有效）
- **603538（美诺华）**：VAL 下方 Peak 可见（VA 外 Peak 有效）
- 三链 `profile_hash` 一致性测试通过（`test_node_cluster_three_chain_consistency.py`）

## 3. SMC（智能资金）

- 算法真源：`backend/app/services/smc_pine_core.py`（生产代码）
- 参考源：`ref/smc_user_source.pine`（人工阅读，非运行依赖，历史路径）
- `ref/smc_user_export.pine` 已 `git rm --cached`，不再纳入 git 跟踪
- PINE_PARITY_PENDING：TV CSV parity 测试在 TradingView CSV 不可用时自动 skip
- 000725 回归基线：17 events / 21 OB / 2 EQL / swing_bias=1（项目回归基线，非 TV golden）

SMC 参数和执行顺序见 `docs/maps/smc-pine-parity-map.md` 和 `AGENTS.md` clause 53。

## 4. MACD / SQZMOM / Bollinger / DSA / Swing

这些指标的计算合同引用现有文档：

| 指标 | 参数真源 | 计算入口 | 说明 |
|---|---|---|---|
| MACD | `indicator_contract.py` | `indicator_service` | 标准 MACD(12,26,9) |
| SQZMOM | `indicator_contract.py` | `indicator_service` | Squeeze Momentum |
| Bollinger | `indicator_contract.py` | `indicator_service` | 布林带(20,2) |
| DSA | `indicator_contract.py` | `dsa_selector` / `watchlist_monitor` | DSA 选股策略 |
| Swing | `indicator_contract.py` | `smc_pine_core.py` | 摆动高低点 |

这些指标不涉及三链一致性约束（单链计算，无跨链 hash 断言）。

## 5. 三链数据流图

见 `docs/maps/indicator-computation-map.md`。

## 6. 变更历史

- CHANGE-20260718-004：初始版本（Node Cluster 唯一语义合同 + engine 计算内核 + ref/ 隔离 + 三链统一）
