# 量化模型 Map

核验状态：已基于代码审计更新（Phase 4）
最后核验日期：2026-07-27
核验分支：dev
核验提交：069ebcca207f75c68abbf3c320e29fdb0d9c3bf2
事实所有权：趋势、结构、动量、筹码和板块模型的权威代码、输入、输出和调用方

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. 当前实现摘要

- 趋势由 DSA VWAP 体系承担，权威入口为 `dsa_selector.py` 的 `compute_dsa_history` / `compute_dsa_bundle`，底层为 `dynamic_swing_anchored_vwap.py`。
- 结构由 SMC Pine 语义核心承担，权威入口为 `smc_pine_core.py` 的 `compute_smc_pine`，薄包装为 `smc_indicator.py`；已排除 FVG，有 Pine 对齐测试。
- 动量以 Bollinger + SQZMOM_LB 为主，权威入口为 `bollinger_features_plotly.py` 的 `bollinger` 和 `sqzmom_lb.py`；盘中监控通过 `bollinger_monitor.py` 输出穿越事件。
- 筹码共识（Node Cluster）由 `node_cluster_engine.py` 的 `compute_node_cluster_profile` 统一入口，底层 `unified_volume_profile.py`；架构守护测试禁止业务模块绕过 engine。
- 板块/指数层仅有板块数据同步（`board_sync_service.py`）和市场列表查询（`market_stocks_service.py`），尚未发现基于个股因子聚合生成板块状态的正式服务。
- 个股状态文字化由 `atomic_fact_contract_service.py` 基于 structural/temporal payload 输出中文事实；连续因子与离散事件在选股、监控、详情链中分离。

## 2. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| QM-01 维度顺序 | `atomic_fact_contract_service.py` `_DIMENSION_ORDER = ["trend", "momentum", "structure", "volume"]`；`atomic_fact_presentation_v1.json` `ui_sections.default_order` | 已实现并核验 | 合同文件与代码一致 |
| QM-02 必选/可选 | `structural_factor_service.py` 5 组因子；`dsa_selector.yaml` 筹码相关参数不存在于 DSA 必选参数 | 已实现并核验 | DSA/SMC/BB 为实际生产路径；Node Cluster 为监控/详情可选 |
| QM-03 文字化输出 | `atomic_fact_contract_service.py:compute_atomic_facts()` → `atomic_fact_presentation_v1.json` publicLabel/valueText | 已实现并核验 | 测试 `test_stock_context_atomic_facts.py` / `test_user_facing_labels.py` |
| QM-10~QM-13 趋势 | `dsa_selector.py:compute_dsa_history` / `compute_dsa_bundle`；底层 `dynamic_swing_anchored_vwap.py`；趋势事件 `strategy/events/detectors/trend_events.py` | 已实现并核验 | `test_dsa_selector.py` / `test_dsa_publish_validation.py` / `test_dsa_visual_segments.py` |
| QM-13 趋势与 SMC 边界 | DSA 负责长周期方向（dir/segment）；SMC 只输出 BOS/CHoCH/EQH/EQL/OB，不维护等价趋势段 | 已实现并核验 | `smc_pine_core.py` 无趋势段逻辑；`dsa_selector.py` 无 BOS/CHoCH |
| QM-20~QM-23 结构 | `smc_pine_core.py:compute_smc_pine` → 事件/OB/pivots/trailing；`smc_indicator.py` 薄包装；`smc_monitor.py` 盘中监控 | 已实现并核验 | `test_smc_pine_deterministic.py` / `test_smc_indicator.py` / `test_smc_monitor_five_event_types.py` |
| QM-23 Pine 对齐 | `smc_pine_core.py` 默认参数逐项匹配 Pine；`ref/smc_user_source.pine` SHA256 0bd3d2ad 为真源 | 已实现并核验 | `test_smc_pine_deterministic.py` 注释 |
| QM-30~QM-33 动量 | `bollinger_features_plotly.py:bollinger`；`sqzmom_lb.py:compute_sqzmom_lb`；`structural_factor_service.py` 第 4 组；`bollinger_monitor.py` 事件 | 已实现并核验 | `test_stock_detail_feishu.py` / `test_monitor_rhythm_regression.py` / `test_indicator_view.py` |
| QM-40~QM-43 筹码共识 | `node_cluster_engine.py:compute_node_cluster_profile`；`volume_node_monitor.py` 事件；`build_node_regions` 为四链统一 DTO | 已实现并核验 | `test_node_cluster_engine.py` / `test_node_cluster_architecture.py` / `test_node_cluster_contract.py` |
| QM-42 禁止 VAH/VAL 替代 | `node_cluster_engine.py` `value_area_filters_peaks = False`；架构守护测试禁止业务模块直接调用底层 VP | 已实现并核验 | `test_node_cluster_architecture.py` |
| QM-50~QM-51 板块聚合 | `board_sync_service.py` 仅同步板块目录/成分；`market_stocks_service.py` 仅列表查询 | 未实现 | 未找到基于个股因子聚合板块趋势的正式服务 |
| QM-60 连续因子与事件分离 | `dsa_selector.py` 输出 `factor_per_bar`（连续）+ `visual_segments`；`trend_events.py`/`smc_monitor.py`/`bollinger_monitor.py`/`volume_node_monitor.py` 输出离散事件 | 已实现并核验 | 代码结构可见 |
| QM-61 参数固定 | `dsa_selector.yaml` 参数 `allowed_scopes: [system]`；`structural_factor_service.py` 硬编码固定参数；`smc_pine_core.py` `DEFAULT_PARAMS` | 已实现并核验 | manifest/代码常量 |
| QM-62 可追踪 | `StrategyRun.effective_config` / `effective_config_hash`；`StockFeatureSnapshotRun` 含 `source_bar_hash` / `adj_factor_hash` / `market_data_contract_version` | 已实现并核验 | `strategy_run.py` / `stock_feature_snapshot_run.py` |

## 3. 趋势

| 项目 | 当前事实 |
|---|---|
| 权威入口 | `backend/app/strategy/selectors/dsa_selector.py:compute_dsa_history` / `compute_dsa_bundle`；`DSASelector.execute` / `compute_indicators` |
| 输入 | 日线 OHLCV DataFrame（open/high/low/close/volume/amount）；`DSAConfig`（prd/baseAPT/useAdapt/volBias） |
| 参数来源 | `dsa_selector.yaml` 参数 `allowed_scopes: [system]`；代码常量 `MIN_DIR_BARS=50` 为 regime 命中阈值 |
| 趋势段输出 | 方向 `dsa_dir`、持续 bar 数 `dsa_dir_bars`、VWAP 序列、regime、visual_segments、pivot_labels |
| 平均成交量 | `compute_dsa_history` 计算 `current_segment_volume_sum` / `prev_segment_volume_sum` / `current_vs_prev_volume_ratio` |
| 写入位置 | `strategy_results.payload`（盘后 run）、`stock_feature_snapshot`（盘后）、实时 API（indicator_service） |
| 调用方 | `DSASelector.execute`（盘后/选股）、`compute_indicators`（详情页实时）、`structural_factor_service`（结构面板） |
| 验证入口 | `test_dsa_selector.py`、`test_dsa_publish_validation.py`、`test_dsa_visual_segments.py` |

## 4. 结构

| 项目 | 当前事实 |
|---|---|
| 权威入口 | `backend/app/strategy_assets/algorithms/features/smc_pine_core.py:compute_smc_pine` |
| BOS/CHoCH | `display_structure` 方法输出 events（type=BOS/CHoCH），anchor/confirmed 因果契约 |
| OB 和进入事件 | `internal_order_blocks` / `swing_order_blocks`，含 mitigation；`smc_monitor.py` 五类触碰事件 |
| 连续高点/低点 | `equal_highs_lows`（EQH/EQL）事件 |
| 成交量信息 | SMC 本身不包含独立成交量过滤；结构面板通过 `structural_factor_service` 第 5 组成交参与补充 |
| Pine 对齐位置 | `smc_pine_core.py` 实现 Pine 语义原语；`ref/smc_user_source.pine` 为真源；FVG 完全排除 |
| 写入位置 | SMC 事件进入 `strategy_events` / `monitor` 状态；结构因子进入 `stock_feature_snapshot` |

## 5. 动量

| 项目 | 当前事实 |
|---|---|
| 权威入口 | `backend/app/strategy_assets/algorithms/features/bollinger_features_plotly.py:bollinger`；`sqzmom_lb.py:compute_sqzmom_lb` |
| squeeze | `compute_sqzmom_lb` 输出 squeeze_on/squeeze_off；`atomic_fact_contract_v1.json` M5_squeeze_state |
| 扩张/扩散 | `bollinger` 输出 upper/mid/lower/width/percent_b；`structural_factor_service.py` 计算 bb_width_percentile、bb_position |
| 成交量 | `structural_factor_service.py` 第 5 组成交参与（volume_ratio_20、volume_percentile_120） |
| 绝对水平 | `bollinger` mid/upper/lower；`sqzmom_lb` momentum/lb 值 |
| 相对变化 | `atomic_fact_contract_service.py` M3_aligned_momentum_delta；`temporal_feature_service.py` daily_sqzmom_change_since_segment_start |
| 事件新鲜度 | 监控事件含 `state_ttl_seconds`；`stock_state_event` 含 detected_at/occurred_at |

## 6. 筹码共识

| 项目 | 当前事实 |
|---|---|
| Node Cluster | `backend/app/services/node_cluster_engine.py:compute_node_cluster_profile`（三链唯一业务入口） |
| 价值共识 | 输出 POC/VAH/VAL、peak_rows、profile_rows；`build_node_regions` 生成 Canonical Node DTO |
| 60m 低周期路径 | Node Cluster 使用日线 250 根定范围 + 15m 4000 根分配成交量；盘中监控使用 1m 检测穿越 |
| 腾讯/pytdx fallback | 底层 `unified_volume_profile.py` 数据源待核验；当前通过 `MarketDataAggregationService` 获取 bars |
| 上穿/下穿 | `volume_node_monitor.py` `detect_events` 输出 `node_cluster_touch`；`node_cluster_engine.py:detect_crossover_signals` |
| VAH/VAL 范围过滤 | 应不存在：`value_area_filters_peaks=False`，架构守护测试禁止业务模块直接调用底层 VP 做 VA 过滤 |

## 7. 板块和指数聚合

待核验/未实现：

- 个股输入：已存在（`strategy_results` / `stock_feature_snapshot`）。
- 行业/概念归属：`market_boards` / `market_board_memberships` 已同步。
- 趋势受损、结构事件分布、动量绝对和相对变化、板块内部个股分布：未找到正式聚合服务。
- 聚合输出和存储：未实现。

偏差：QM-50/QM-51 当前只有板块数据目录和个股列表查询，没有基于个股因子生成板块/指数状态的计算链路。

## 8. 重复实现审计

| 业务定义 | 权威实现 | 其他实现 | 是否违反 SSOT |
|---|---|---|---|
| DSA VWAP 趋势 | `dsa_selector.py:compute_dsa_history` | `dynamic_swing_anchored_vwap.py` 被直接调用（同一函数） | 否，SSOT 已声明 |
| SMC 结构 | `smc_pine_core.py:compute_smc_pine` | `smc_indicator.py` 为薄包装 | 否 |
| Bollinger 动量 | `bollinger_features_plotly.py:bollinger` | `bollinger_monitor.py` / `structural_factor_service.py` / `temporal_feature_service.py` 调用同一函数 | 否 |
| Node Cluster 筹码 | `node_cluster_engine.py:compute_node_cluster_profile` | 业务模块均通过 engine，底层 `unified_volume_profile.py` 不被业务直接调用 | 否（架构守护测试保障） |

## 9. 已知偏差与风险

- P2：QM-50/QM-51 板块/指数层聚合尚未实现，市场列表仅展示个股字段。
- P1：`after_close_orchestrator.py` 的 `checking_coverage` 仍检查 15m 覆盖率，与 AC-04“不再以 15m 为主计算要求”存在局部冲突（详见 `maps/30-after-close.md`）。
- P1：SMC 结构成交量信息未在 SMC 核心内显式保留，依赖结构面板的成交参与组补充。
- P3：`ref/交易/` 下存在大量实验/参考脚本，需与 `backend/app/` 生产路径保持隔离；架构守护测试 `test_ref_isolation.py` 已部分覆盖。

## 10. 更新触发条件

- 指标入口、参数源、输出 Schema 或写入位置变化；
- 新增或删除因子与事件；
- 单股与批量调用关系变化；
- 发现重复权威实现；
- 与 PRD 的算法边界变化；
- 板块/指数聚合实现时。
