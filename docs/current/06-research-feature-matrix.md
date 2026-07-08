# 06 研究特征矩阵与因果口径

## 1. 目的与边界

研究特征矩阵（research feature matrix）用于探索因子组合规律，与生产 `stock_feature_snapshots` 严格分离。

| 矩阵 | 数据载体 | 服务对象 | 因果口径 |
|---|---|---|---|
| production snapshot | `stock_feature_snapshots` 表 | 最近交易日、自选股、前端展示、`watchlist_ready` | 必须 point-in-time，禁止 hindsight/label |
| research feature matrix | 研究脚本输出（默认不落盘） | 因子探索、回测实验、研究脚本 | 可同时包含 causal/confirmed_delay/hindsight/label，但严格分命名空间 |

约束：
- 研究矩阵不接入 `watchlist_ready`；
- 研究矩阵不修改 production snapshot；
- 研究矩阵不新增数据库表；
- 研究矩阵脚本默认 `--dry-run`，无 `--output` 不落盘；
- `--output` 必须配合 sample scope（`--symbols` 或 `--limit-instruments`），禁止无过滤全市场输出文件；
- 历史 full snapshot 回补仍 BLOCKED（见 `03-jobs-integrations-operations.md` 2.4 节），研究回补改走 research matrix，小样本 + 默认 dry-run。

## 2. 因果口径命名空间

所有研究矩阵字段必须带命名空间前缀，由 `backend/app/research/feature_causality_registry.py` 统一登记。

| namespace | 含义 | compute_policy | allowed_for_backtest | 用途 |
|---|---|---|---|---|
| `causal` | 当时可知的滚动特征 | `series_once` | True | 回测 X |
| `confirmed_delay` | 仅在确认 bar 生效的字段 | `confirmed_only` | True | 回测 X（不回填 anchor） |
| `hindsight` | 允许未来信息的结构标注 | `hindsight_once` | False | 只做结构标注，禁止进入回测 |
| `label` | 未来收益/胜负标签 | `future_label` | False | 只能作为 y，不得作为 X |

核心规则：
- `hindsight.*` 禁止作为 feature 进入回测；
- `label.*` 禁止作为 feature 进入回测；
- `key` 必须以 `{namespace}.` 开头（如 `causal.atr`、`hindsight.dsa_finalized_segment`）；
- `FeatureSpec` 必填 `namespace` / `source` / `compute_policy`，缺一抛 `ValueError`。

## 3. 因果口径登记表

`build_default_registry()` 当前登记 27 个字段：

### 3.1 causal（10 个，允许回测）

| key | source | 说明 |
|---|---|---|
| `causal.atr` | structural_factor_service | ATR 波动率 |
| `causal.bb` | structural_factor_service | Bollinger Bands 上下轨 |
| `causal.sqzmom` | structural_factor_service | SQZMOM 动量 |
| `causal.volume_ratio_20` | structural_factor_service | 20 日成交量比率 |
| `causal.volume_percentile_120` | structural_factor_service | 120 日成交量百分位 |
| `causal.active_swing` | structural_factor_service | 已确认 swing（当时可知的 active 段） |
| `causal.developing_swing` | structural_factor_service | 发展中 swing（未确认但当前可见） |
| `causal.dsa_confirmed_segment` | structural_factor_service | DSA 段（当时已确认状态） |
| `causal.dsa_confirmed_direction` | structural_factor_service | DSA 方向（当时已确认） |
| `causal.dsa_confirmed_age_bars` | structural_factor_service | DSA 段已持续 bar 数（当时已确认） |

### 3.2 confirmed_delay（4 个，允许回测，不回填 anchor）

| key | source | 说明 |
|---|---|---|
| `confirmed_delay.confirmed_swing_high` | structural_factor_service.swing | 已确认 swing 高点 anchor |
| `confirmed_delay.confirmed_swing_low` | structural_factor_service.swing | 已确认 swing 低点 anchor |
| `confirmed_delay.bars_since_confirmed_swing_high` | structural_factor_service.swing | 距上次确认 swing 高点的 bar 数 |
| `confirmed_delay.bars_since_confirmed_swing_low` | structural_factor_service.swing | 距上次确认 swing 低点的 bar 数 |

口径约束：只能在确认 bar 生效，不得回填 anchor date（否则会引入未来信息）。

### 3.3 hindsight（6 个，禁止回测）

| key | source | 说明 |
|---|---|---|
| `hindsight.dsa_finalized_segment` | dsa_selector | DSA 段（未来确认后回标注） |
| `hindsight.dsa_finalized_direction` | dsa_selector | DSA 方向（未来确认后） |
| `hindsight.dsa_finalized_age_bars` | dsa_selector | DSA 段最终持续 bar 数 |
| `hindsight.node_cluster_label` | volume_node_monitor | Node Cluster 结构标注（允许未来信息） |
| `hindsight.node_cluster_support` | volume_node_monitor | Node Cluster 支撑结构（后验标注） |
| `hindsight.node_cluster_resistance` | volume_node_monitor | Node Cluster 阻力结构（后验标注） |

口径约束：允许未来信息，只做结构标注，不可用于真实回测。

### 3.4 label（7 个，禁止作为 feature）

| key | source | 说明 |
|---|---|---|
| `label.future_return_5d` | research_label_service | 未来 5 日收益率 |
| `label.future_return_10d` | research_label_service | 未来 10 日收益率 |
| `label.future_return_20d` | research_label_service | 未来 20 日收益率 |
| `label.future_max_drawdown_10d` | research_label_service | 未来 10 日最大回撤 |
| `label.future_max_drawdown_20d` | research_label_service | 未来 20 日最大回撤 |
| `label.breakout_success_10d` | research_label_service | 未来 10 日是否突破成功 |
| `label.failure_breakdown_10d` | research_label_service | 未来 10 日是否破位失败 |

口径约束：只能作为 y，不得作为 X。

## 4. DSA 双轨口径

DSA 字段必须同时存在 causal 与 hindsight 两类，分别对应"当时可知"与"未来确认后回标注"：

| 维度 | causal.* | hindsight.* |
|---|---|---|
| `segment` | `causal.dsa_confirmed_segment`（当时已确认状态） | `hindsight.dsa_finalized_segment`（未来确认后回标注） |
| `direction` | `causal.dsa_confirmed_direction` | `hindsight.dsa_finalized_direction` |
| `age_bars` | `causal.dsa_confirmed_age_bars` | `hindsight.dsa_finalized_age_bars` |

- `causal.dsa_confirmed_*` 只使用当时已确认状态，可用于回测；
- `hindsight.dsa_finalized_*` 允许未来信息，用于事后分析段是否真的延续/转向，禁止用于回测；
- 不允许把 `hindsight.dsa_finalized_*` 当成 `causal.dsa_confirmed_*` 使用；
- registry 必须同时登记两类，缺一视为口径不完整。

## 5. Node Cluster 后验口径

Node Cluster 字段只能是 hindsight，不得是 causal：

- `hindsight.node_cluster_label` / `hindsight.node_cluster_support` / `hindsight.node_cluster_resistance`；
- 允许未来信息，用于事后标注支撑/阻力结构；
- 不得进入 causal 命名空间；
- 不得接入 watchlist 或生产筛选；
- 不得作为回测 feature。

## 6. confirmed_swing 口径

`confirmed_delay.confirmed_swing_*` 与 `confirmed_delay.bars_since_confirmed_swing_*` 必须是 `confirmed_delay` 命名空间：

- 只能在确认 bar 生效，不回填 anchor date；
- 不得作为 hindsight 默认回填；
- 允许回测，但必须遵守 confirmed_only 计算策略；
- 与 `causal.active_swing` / `causal.developing_swing` 区分清楚：
  - `causal.active_swing` 是当时可知的 active 段（已确认 major leg）；
  - `causal.developing_swing` 是未确认但当前可见的回落/反弹；
  - `confirmed_delay.confirmed_swing_*` 是已确认的 swing 高/低点 anchor。

## 7. 研究矩阵脚本骨架

`backend/scripts/research_feature_matrix_backfill.py` 是研究矩阵 CLI 骨架，本 PR 只完成骨架 + dry-run：

### 7.1 CLI 参数

| 参数 | 默认 | 语义 |
|---|---|---|
| `--start` | 必填 | 起始日期 YYYY-MM-DD |
| `--end` | `latest` | 结束日期 YYYY-MM-DD 或 `latest` |
| `--symbols` | None | 只处理指定股票代码（逗号分隔，触发 sample scope） |
| `--limit-instruments` | None | 限制 instrument 数量（触发 sample scope） |
| `--dry-run` | False | 只打印计划与字段分类统计，不写 DB，不写文件 |
| `--output` | None | 输出文件路径（必须配合 sample scope） |
| `--include-hindsight` | `true` | 是否包含 hindsight 命名空间字段 |
| `--include-labels` | `true` | 是否包含 label 命名空间字段 |

### 7.2 scope 规则

- `_resolve_scope(symbols, limit_instruments)`：任一过滤启用 → `sample`，都未启用 → `full`；
- `--output` 必须配合 sample scope，禁止无过滤全市场输出文件（`_validate_output_scope` 抛 `ValueError`）；
- dry-run 也要校验 scope（避免误用）。

### 7.3 默认行为

- dry-run：打印计划 + 字段分类统计，不写 DB，不写文件；
- 非 dry-run 无 `--output`：只打印计划（骨架阶段不实际计算）；
- 非 dry-run 有 `--output`：校验 sample scope 通过后，骨架阶段不实际写文件（仅打印计划写入提示）。

### 7.4 字段分类统计

`build_plan()` 基于 `build_default_registry()` 统计各命名空间字段数，根据 `--include-hindsight` / `--include-labels` 开关调整 hindsight/label 计数（关闭时为 0）。

### 7.5 后续实现顺序（不在本 PR 范围）

1. causal rolling features：ATR / BB / SQZMOM / volume；
2. confirmed_delay swing：按确认 bar 生效，不回填 anchor；
3. DSA 双轨：
   - `causal.dsa_confirmed_*`：当时可知
   - `hindsight.dsa_finalized_*`：未来确认后回标注
4. Node Cluster：只输出 `hindsight.node_cluster_*`，允许未来信息，不得进入 causal；
5. labels：用未来 close/high/low 生成 `label.future_*`。

计算设计原则：单只股票只 load bars 一次，内存中按 `trade_date` slice。

## 8. 与 production snapshot 的区别

| 维度 | production snapshot | research matrix |
|---|---|---|
| 数据载体 | `stock_feature_snapshots` 表 | 研究脚本输出（默认不落盘） |
| 时效 | 最近交易日 + 自选股 | 任意历史日期范围 |
| 因果口径 | 必须 point-in-time | 可同时包含 causal/hindsight/label |
| watchlist_ready | 是 | 否（不接入） |
| 字段登记 | 不强制 namespace 前缀 | 必须带 namespace 前缀 |
| 写库 | 是（upsert 幂等） | 否（骨架阶段不写 DB） |
| 全市场回补 | BLOCKED（PR #41 126min > 120min 阈值） | 禁止无过滤输出文件 |

## 9. 禁止项

- 不要把 hindsight 或 label 字段当成 causal feature；
- 不要把 hindsight/label 接入 watchlist 或生产筛选；
- 不要新增数据库表（research matrix 不写 DB）；
- 不要写大 CSV/parquet（骨架阶段不写任何文件）；
- 不要跑历史回补或 production full backfill；
- 不要生成 coverage/截图/大日志/DB 备份；
- 不要删除数据库卷或运行中镜像。
