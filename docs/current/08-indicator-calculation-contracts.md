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

## 5. 全算法族统一注册表（CHANGE-20260718-006 Section 2）

> 自 CHANGE-20260718-006 起，全部 12 个算法族通过 `AlgorithmRegistry`
> （`backend/app/contracts/algorithm_registry.py`）统一注册，四条调用链
> （详情/盘后/盘中/Capture）通过 `CanonicalComputationService`
> （`backend/app/services/canonical_computation_service.py`）统一调度。
> 禁止生产模块直接 `import` 算法 kernel 绕过注册表（AST 守护：
> `backend/tests/test_algorithm_registry_architecture.py`）。

### 5.1 算法族总表

| algorithm_id | algorithm_version | contract_fingerprint | kernel_module | kernel_entrypoint | input_timeframes | adjustment_mode | completed_only | warmup_bars | output_schema_version | migration_status |
|---|---|---|---|---|---|---|---|---|---|---|
| `node_cluster` | `nc-v1` | `nc-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_node_cluster_adapter` | `("1d","15m")` | `qfq` | True | 250 | 1 | `production_wired` |
| `dsa` | `dsa-v1` | `dsa-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_dsa_adapter` | `("1d",)` | `qfq` | True | 250 | 1 | `production_wired` |
| `smc` | `smc-v1` | `smc-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_smc_adapter` | `("1d","15m","1h","1w","1mo")` | `qfq` | True | 500 | 1 | `production_wired` |
| `bollinger` | `bb-v1` | `bb-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_bollinger_adapter` | `("1d","15m","1h","1w","1mo")` | `qfq` | True | 250 | 1 | `production_wired` |
| `macd` | `macd-v1` | `macd-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_macd_adapter` | `("1d","15m","1h","1w","1mo")` | `qfq` | True | 250 | 1 | `production_wired` |
| `sqzmom` | `sqzmom-v1` | `sqzmom-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_sqzmom_adapter` | `("1d",)` | `qfq` | True | 250 | 1 | `production_wired` |
| `breakout` | `brk-v1` | `brk-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_breakout_adapter` | `("1d",)` | `qfq` | True | 250 | 1 | `production_wired` |
| `participation` | `part-v1` | `part-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_participation_adapter` | `("1d",)` | `qfq` | True | 250 | 1 | `production_wired` |
| `temporal_features` | `tmp-v1` | `tmp-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_temporal_features_adapter` | `("1d","15m")` | `qfq` | True | 250 | 1 | `production_wired` |
| `structural_features` | `str-v1` | `str-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_structural_features_adapter` | `("1d","15m")` | `qfq` | True | 250 | 1 | `production_wired` |
| `primary_secondary_relation` | `psr-v1` | `psr-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_primary_secondary_relation_adapter` | `("1d","15m")` | `qfq` | True | 250 | 1 | `production_wired` |
| `snapshot_derived_features` | `sdf-v1` | `sdf-cf-v1` | `app.services.canonical_adapters` | `app.services.canonical_adapters:compute_snapshot_derived_adapter` | `("1d","15m")` | `qfq` | True | 250 | 1 | `production_wired` |

注册表版本常量：`ALGORITHM_REGISTRY_VERSION = "reg-v1"`（注册表结构变更时 bump，
不影响各算法自身版本）。

**`migration_status` 含义**（CHANGE-20260718-007 S3.2 + CHANGE-20260719-001 §二）：

| 状态 | 含义 | 是否可经 `compute_with_mdas` 调用 |
|---|---|---|
| `registered_only` | 合同已登记，但 `kernel_entrypoint` callable 不存在或未适配统一 `(bars: pd.DataFrame, **kwargs)` 签名 | 否（直接调用 kernel 或先 wiring） |
| `input_provider_wired` | callable 存在且接受统一签名 | 是（经 `compute_with_mdas` 自动取行情+校验+哈希） |
| `production_wired` | adapter 已在 `canonical_adapters.py` 中实现并通过自测；与 `input_provider_wired` 语义等价（`compute_with_mdas` 接受），但更强地断言"该算法已是生产 canonical 入口"，禁止再退回 `registered_only` | 是 |

**当前全部 12 个算法族已完成 `production_wired` 迁移**（CHANGE-20260719-001 §二）：
adapter 在 `backend/app/services/canonical_adapters.py` 中实现并通过自测，
`compute_with_mdas` 接受 `production_wired` 算法。

**诚实声明**（PROMPT.md L693）：
- §二 **仅完成 adapter 层基础设施**——`migration_status` 升级到 `production_wired`，7 个 broken
  entrypoints 已修复（指向真实存在的 adapter callable）
- **四链实际未迁移到 `compute_with_mdas`**：详情/盘后/盘中/Capture 当前仍直接调 MDAS + 各自 kernel
- AST 硬门禁 `test_four_chain_no_direct_kernel_import` 以 `@pytest.mark.xfail(strict=True)` 标记此目标
- **result_hash 矩阵仍是基线**：四链 result_hash 字段为 None，迁移后才能填充对比

### 5.2 各算法族合同（按 12 项规范记录）

> 12 项：业务含义 / 输入 / 参数 / 复权 / completed 或 partial / warmup /
> Kernel 路径 / 输出 schema / 算法版本 / 调用链 / 允许差异 / 禁止变化 / 验收样本

#### 5.2.1 node_cluster（筹码分布 / Volume Profile）

详见第 2 节（完整合同）。

- **业务含义**：价格区间分 100 档，按成交量分配，识别 Peak/VA/POC
- **输入**：1d 250 根 + 15m 4000 根 + 1m 2 根（监控链）
- **复权**：qfq，`adjustment_as_of` 锚定业务日
- **completed/partial**：daily/15m completed_only=True；1m include_realtime=True
- **warmup**：250 根（决定价格范围）
- **Kernel 路径**：`app.services.canonical_adapters:compute_node_cluster_adapter`（CHANGE-20260719-001 §二 迁移；多 timeframe，adapter 接受 `daily_bars` + `bars_15m`，内部委托 `node_cluster_engine.compute_node_cluster_profile`）
- **输出 schema**：`NodeClusterProfileResult`（frozen dataclass，version=1）
- **算法版本**：`nc-v1` / 指纹 `nc-cf-v1`
- **调用链**：详情 / 盘后 / 盘中（三链同核，profile_hash 必须一致）
- **允许差异**：不同 stock/as_of 自然不同；监控链 1m 不影响 Profile hash
- **禁止变化**：VA 外 Peak 不得过滤；不得绕过 engine；不得单周期调用
- **验收样本**：000725 / 603538 / 三链一致性测试

#### 5.2.2 dsa（DSA 选股策略）

- **业务含义**：基于 VWAP/结构锚的 DSA 选股与可视化分段
- **输入**：1d 250 根 qfq bars
- **参数**：引用 `indicator_contract.py`（DSASelector 内部常量）
- **复权**：qfq，`adjustment_as_of` 锚定业务日
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_dsa_adapter`（CHANGE-20260719-001 §二 迁移；包装 `dsa_selector.compute_dsa_bundle`）
- **输出 schema**：`DsaSelectorData`（含 `visual_segments` / `anchor_time` / `vwap` 等）
- **算法版本**：`dsa-v1` / 指纹 `dsa-cf-v1`
- **调用链**：详情（indicator API） / 盘后（feature_snapshot）
- **允许差异**：不同 stock 自然不同；非 1d 周期为验证图层
- **禁止变化**：禁止 1d-only 硬编码（全周期支持）；禁止 source mismatch 时渲染
- **验收样本**：DSA source alignment 测试（`dsaSourceAlignment.test.ts`）

#### 5.2.3 smc（Smart Money Concepts）

- **业务含义**：BOS/CHoCH 结构事件 + Order Block + EQH/EQL + Swing Bias
- **输入**：1d/15m/1h/1w/1mo 全周期支持
- **参数**：详见 `docs/maps/smc-pine-parity-map.md` 和 `AGENTS.md` clause 53
- **复权**：qfq
- **completed/partial**：completed_only=True（Pine 语义，最新已完成 K 线）
- **warmup**：500 根（覆盖足够 swing 结构）
- **Kernel 路径**：`app.services.canonical_adapters:compute_smc_adapter`（CHANGE-20260719-001 §二 迁移；二合一 adapter：`smc_indicator.compute_smc_indicators` + `smc_view_adapter.adapt_smc_to_display_dto`）
- **输出 schema**：SMC DTO（events / order_blocks / equal_highs_lows / trailing / swing_bias）
- **算法版本**：`smc-v1` / 指纹 `smc-cf-v1`
- **调用链**：详情（include_smc=true 时计算）
- **允许差异**：PINE_PARITY_PENDING（TV CSV 不可用时 skip，不标完成）
- **禁止变化**：算法硬约束不得重写（详见 `AGENTS.md` clause 53）；`include_smc=false` 时 0 核心函数调用
- **验收样本**：000725 回归基线（17 events / 21 OB / 2 EQL / swing_bias=1，项目回归基线非 TV golden）

#### 5.2.4 bollinger（布林带）

- **业务含义**：BB(20, 2.0) 上下轨 + 带宽
- **输入**：全周期 qfq bars
- **参数**：`BB_WIN=20`, `BB_K=2.0`（`indicator_contract.py`）
- **复权**：qfq
- **completed/partial**：completed_only=True（与 `algorithm_registry.py` 合同一致；partial bar 由前端 quote overlay 单独呈现，不进入指标计算）
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_bollinger_adapter`（CHANGE-20260719-001 §二 迁移；包装 `bollinger_features_plotly.bollinger`）
- **输出 schema**：BB upper/lower/mid/bandwidth 数组
- **算法版本**：`bb-v1` / 指纹 `bb-cf-v1`
- **调用链**：详情（indicator API）
- **允许差异**：1w/1mo 也支持（不再 skip）
- **禁止变化**：参数 20/2.0 不得偏离常量；全周期支持不得回退到 1d-only
- **验收样本**：BB overlay 全周期测试（`dsaOverlayPolicy.test.ts` 第 5 节）

#### 5.2.5 macd（MACD）

- **业务含义**：MACD(12, 26, 9) 标准实现（A 股 2× 版本：DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF,signal), HIST=2*(DIF-DEA)）
- **输入**：全周期 qfq bars
- **参数**：`MACD_FAST=12`, `MACD_SLOW=26`, `MACD_SIGNAL=9`
- **复权**：qfq
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_macd_adapter`（CHANGE-20260718-007 S3.2 接线 + CHANGE-20260719-001 §二 升级到 production_wired）
  - adapter 内部从 bars 提取 close 后调用 `app.services.indicator_service:compute_macd`
- **输出 schema**：MACD/SIGNAL/HIST 数组
- **算法版本**：`macd-v1` / 指纹 `macd-cf-v1`
- **调用链**：详情（indicator API）；可经 `CanonicalComputationService.compute_with_mdas` 调用
- **migration_status**：`production_wired`（CHANGE-20260719-001 §二 从 `input_provider_wired` 升级；adapter 已通过自测）
- **允许差异**：时间对齐命中率低于阈值时输出诊断（不阻塞渲染）
- **禁止变化**：12/26/9 参数不得偏离
- **验收样本**：标准 MACD 计算回归；result_hash 矩阵基线测试（`test_canonical_result_hash_matrix.py`）

#### 5.2.6 sqzmom（Squeeze Momentum）

- **业务含义**：Squeeze Momentum Indicator（动量挤压）
- **输入**：1d qfq bars
- **参数**：`indicator_contract.py` 中 SQZMOM 常量
- **复权**：qfq
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_sqzmom_adapter`（CHANGE-20260719-001 §二 迁移；包装 `sqzmom_lb.compute_sqzmom_lb`）
- **输出 schema**：SQZMOM 状态数组
- **算法版本**：`sqzmom-v1` / 指纹 `sqzmom-cf-v1`
- **调用链**：盘后（feature_snapshot）
- **允许差异**：仅 1d 周期计算
- **禁止变化**：算法公式不得修改
- **验收样本**：SQZMOM layer 契约测试（`sqzmom-layer.test.ts`）

#### 5.2.7 breakout（突破压力区）

- **业务含义**：基于 trendlines_with_breaks 的突破识别
- **输入**：1d qfq bars
- **复权**：qfq
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_breakout_adapter`（CHANGE-20260719-001 §二 迁移；包装 `trendlines_with_breaks_luxalgo.trendlines_with_breaks`）
- **输出 schema**：Breakout 事件数组
- **算法版本**：`brk-v1` / 指纹 `brk-cf-v1`
- **调用链**：详情（indicator API）
- **允许差异**：仅 1d 周期计算
- **禁止变化**：trendlines_with_breaks 算法源不得替换
- **验收样本**：详情页 Breakout 渲染

#### 5.2.8 participation（成交参与）

- **业务含义**：基于 SR Event Factor 的成交参与度因子
- **输入**：1d qfq bars
- **复权**：qfq
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_participation_adapter`（CHANGE-20260719-001 §二 迁移；包装 `sr_event_factor_lab.compute_sr_factor_lab`）
- **输出 schema**：Participation 因子数组
- **算法版本**：`part-v1` / 指纹 `part-cf-v1`
- **调用链**：盘后（feature_snapshot）
- **允许差异**：仅 1d 周期计算
- **禁止变化**：SR Event Factor 算法公式不得修改
- **验收样本**：盘后 snapshot 含 participation 字段

#### 5.2.9 temporal_features（时序特征）

- **业务含义**：基于 K 线时序的特征（如连阳/连阴、缺口等）
- **输入**：1d + 15m qfq bars
- **复权**：qfq
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_temporal_features_adapter`（CHANGE-20260719-001 §二 迁移；异步编排，包装 `temporal_feature_service.compute_temporal_features`，调用方用 `compute()` 直接调度）
- **输出 schema**：时序特征字典
- **算法版本**：`tmp-v1` / 指纹 `tmp-cf-v1`
- **调用链**：盘后（feature_snapshot）
- **允许差异**：1d/15m 双周期计算
- **禁止变化**：特征定义不得修改
- **验收样本**：盘后 snapshot 含 temporal 字段

#### 5.2.10 structural_features（结构特征）

- **业务含义**：基于 Node Cluster Profile 的结构因子（含 VA/POC/nearest node）
- **输入**：1d + 15m qfq bars + 预计算 Node Cluster Profile
- **复权**：qfq
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_structural_features_adapter`（CHANGE-20260719-001 §二 迁移；包装 `structural_factor_service._compute_all_factors_for_bars`，接受 `precomputed_node_cluster`）
- **输出 schema**：结构特征字典
- **算法版本**：`str-v1` / 指纹 `str-cf-v1`
- **调用链**：盘后（feature_snapshot）
- **允许差异**：消费预计算 Node Cluster 结果（不得重算 VP）
- **禁止变化**：不得绕过 node_cluster_engine 自行计算 VP
- **验收样本**：盘后 snapshot 含 structural 字段

#### 5.2.11 primary_secondary_relation（主次关系）

- **业务含义**：1d 主结构 + 15m 次结构的关系因子
- **输入**：1d + 15m qfq bars
- **复权**：qfq
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_primary_secondary_relation_adapter`（CHANGE-20260719-001 §二 迁移；包装 `structural_factor_service._compute_relation`）
- **输出 schema**：主次关系因子字典（含 `secondary.15m.cost_position` 等）
- **算法版本**：`psr-v1` / 指纹 `psr-cf-v1`
- **调用链**：盘后（feature_snapshot）
- **允许差异**：`secondary.15m.cost_position` 语义已调整（CHANGE-20260718-006）
- **禁止变化**：主次关系因子定义不得修改
- **验收样本**：盘后 snapshot schema_version=3

#### 5.2.12 snapshot_derived_features（快照派生特征）

- **业务含义**：从 StockFeatureSnapshot 派生的综合特征（聚合上述各族）
- **输入**：1d + 15m qfq bars
- **复权**：qfq，`adjustment_as_of=trade_date`
- **completed/partial**：completed_only=True
- **warmup**：250 根
- **Kernel 路径**：`app.services.canonical_adapters:compute_snapshot_derived_adapter`（CHANGE-20260719-001 §二 迁移；异步编排，包装 `feature_snapshot_service.compute_feature_snapshot_for_date`，调用方用 `compute()` 直接调度）
- **输出 schema**：完整 StockFeatureSnapshot（schema_version=3）
- **算法版本**：`sdf-v1` / 指纹 `sdf-cf-v1`
- **调用链**：盘后（feature_snapshot）
- **允许差异**：聚合各族结果，本身不做新计算
- **禁止变化**：schema_version=3 不得降级；`finish_snapshot_run` 必须读实际 snapshot 数量
- **验收样本**：盘后 snapshot 发布成功

### 5.3 CanonicalComputationService 调度

四条调用链通过 `CanonicalComputationService.compute(algorithm_id, ...)` 统一调度：

```
CanonicalComputationService.compute
  → AlgorithmRegistry.get(algorithm_id)            # 查询合同
  → _validate_contract(contract, kernel_kwargs)    # 校验输入（bars 参数 + 可选 timeframe/adj/completed_only）
  → _load_kernel(contract)                         # importlib 加载 kernel_module:callable
  → kernel(**kernel_kwargs)                        # 调用算法 kernel / adapter
  → _compute_result_hash(...)                      # SHA256 前 16 字符（5 维度）
  → CanonicalResult(algorithm_id, version, hash, payload, ...)
```

**S3.2 InputProvider**（CHANGE-20260718-007）：`compute_with_mdas(algorithm_id, session, instrument_id, as_of, ...)` 是更高层入口，
调用方只传 MDAS 参数（不传 bars），Canonical 内部自动：

```
CanonicalComputationService.compute_with_mdas
  → AlgorithmRegistry.get(algorithm_id)            # 查合同
  → 校验 migration_status ∈ {"input_provider_wired", "production_wired"} # 否则抛 ContractViolationError
                                                     # CHANGE-20260719-001 §二 增加 production_wired
  → 从合同推导 MDAS 参数（adj/completed_only/warmup_bars）
  → 校验 timeframe 在 contract.input_timeframes 中
  → MarketDataAggregationService.get_bars(...)     # 取 bars + source_bar_hash + adj_factor_hash
  → 调用 compute(bars=bar_result.bars, ...)        # 转入统一 compute 流程
```

调用方契约：调用 `compute_with_mdas` 时只需提供 `algorithm_id`、`session`、`instrument_id`、
`as_of`、可选 `timeframe/limit/warmup_bars/adjustment_as_of`，以及 kernel 自身参数（如 `fast/slow/signal`）。
canonical 层完成取数、校验、哈希、result 包装，调用方不再持有 bars。

result_hash 5 维度：
- algorithm_id + contract_fingerprint（算法合同维度）
- instrument_id + as_of（业务维度）
- source_bar_hash + adj_factor_hash（行情输入维度）
- result 内容（结果维度）

相同输入必须得到相同 result_hash（缓存键 + 一致性比对基础）。

### 5.4 架构守护测试

- `backend/tests/test_algorithm_registry_architecture.py`
  - `TestAlgorithmRegistryIntegrity`：注册完整性 + 唯一性 + 模块可导入 + node_cluster 合同与 semantics 一致
  - `TestAlgorithmRegistryCallConstraint`：只有 registry module 调用 `AlgorithmRegistry.register`
  - `TestCanonicalComputationServiceInterface`：list/get_contract/未注册异常/hash 确定性/序列化稳定性
  - `TestMigrationStatusGuard`（S3.2 + CHANGE-20260719-001 §二）：
    - `test_migration_status_documented_for_all`：valid_statuses 含 `registered_only`/`input_provider_wired`/`production_wired`；断言 12 个 `production_wired` + 0 个 `registered_only`
    - `test_wired_algorithms_have_existing_callables`：`wired` 列表含 `input_provider_wired` 和 `production_wired`，callable 必须真实存在
    - `test_all_adapters_in_canonical_adapters_module`（§二 新增）：所有 `production_wired` 算法的 `kernel_module` 必须是 `app.services.canonical_adapters`
    - `test_registered_only_algorithms_need_not_have_callables`：保留作为未来新算法守护
  - `TestFourChainDirectImportGate`（CHANGE-20260719-001 §二 新增，AST 硬门禁）：
    - `_FOUR_CHAIN_MODULES`：indicator_service / feature_snapshot_service / stock_capture_service / monitor_batch_service
    - `_KERNEL_MODULE_PREFIXES`：strategy_assets.algorithms.features / node_cluster_engine / smc_view_adapter / structural_factor_service / temporal_feature_service
    - `test_four_chain_no_direct_kernel_import`：`@pytest.mark.xfail(strict=True)` 标记，待四链迁移完成后移除 xfail
    - `test_canonical_adapters_exports_all_12`：验证 `canonical_adapters.py` 导出全部 12 个 adapter callable
- `backend/tests/test_canonical_input_provider.py`（S3.2，8 用例 + CHANGE-20260719-001 §二 新增 2 用例）：
  - mock MDAS 验证 compute_with_mdas 端到端
  - §二 新增 `test_compute_with_mdas_accepts_production_wired_smc` 和 `test_compute_with_mdas_accepts_production_wired_bollinger`
  - §二 重写 `test_compute_with_mdas_rejects_registered_only`：临时注册 `_test_registered_only_algo` 算法验证拒绝逻辑
- `backend/tests/test_canonical_result_hash_matrix.py`（S3.2，6 用例）：result_hash 矩阵基线，作为四链迁移后验收标准；§二 更新 macd `migration_status` 断言为 `production_wired`

## 6. 三链数据流图

见 `docs/maps/indicator-computation-map.md`（CHANGE-20260718-006 Section 5c 扩展为四链）。

## 7. 变更历史

- CHANGE-20260718-004：初始版本（Node Cluster 唯一语义合同 + engine 计算内核 + ref/ 隔离 + 三链统一）
- CHANGE-20260718-006 Section 2：全算法族统一注册表 + CanonicalComputationService（12 算法族 SSOT）
- CHANGE-20260718-006 Section 5c：扩展 08 文档为全算法族合同（12 项规范）
- CHANGE-20260718-007 S3.2：AlgorithmContract 增加 `migration_status` 字段 + `compute_with_mdas` InputProvider + macd 统一 adapter（参考实现）+ result_hash 矩阵基线测试 + `TestMigrationStatusGuard` 守护
- CHANGE-20260719-001 §二：12 算法族全部迁移到 `production_wired` adapter（`canonical_adapters.py` 实现 12 个 adapter + 自测）+ `compute_with_mdas` 接受 `production_wired` + 7 broken entrypoints 修复 + `TestFourChainDirectImportGate` AST 硬门禁（`xfail(strict=True)`，待四链迁移完成后移除）+ `test_all_adapters_in_canonical_adapters_module` 守护 + `test_canonical_adapters_exports_all_12` 守护
