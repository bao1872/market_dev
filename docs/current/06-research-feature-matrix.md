# 06 研究特征矩阵与因果口径

## 1. 目的与边界

研究特征矩阵（research feature matrix）用于探索因子组合规律，与生产 `stock_feature_snapshots` 严格分离。**DB 是研究矩阵的主存储**，parquet 只作为可选 debug 导出。

| 矩阵 | 数据载体 | 服务对象 | 因果口径 |
|---|---|---|---|
| production snapshot | `stock_feature_snapshots` 表 | 最近交易日、自选股、前端展示、`watchlist_ready` | 必须 point-in-time，禁止 hindsight/label |
| research feature matrix | `research_feature_matrix_runs` + `research_feature_matrix_rows` 两张表 | 因子探索、回测实验、研究脚本 | 可同时包含 causal/confirmed_delay/hindsight/label，但严格分命名空间 |

约束：
- 研究矩阵不接入 `watchlist_ready`；
- 研究矩阵不修改 production snapshot；
- 研究矩阵只写入专用 research 表，不写 `stock_feature_snapshots`；
- parquet 只作为可选 debug 导出（`--export-parquet`，仅 sample scope），不作为主存储；
- 不生成中间 parquet/CSV/coverage/截图/大日志/DB 备份；
- 历史 full snapshot 回补仍 BLOCKED（见 `03-jobs-integrations-operations.md` 2.4 节），研究回补改走 research matrix，按月分批 + 三道硬阈值 + 分阶段验证。

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
- `FeatureSpec` 必填 `namespace` / `source` / `compute_policy`，缺一抛 `ValueError`；
- registry 仍保留 dotted key（`causal.atr`），写 DB 时由 `FeatureSpec.db_column` 映射成下划线列名（`causal_atr`）。

## 3. 因果口径登记表

`build_default_registry()` 当前登记 **33 个字段**（causal 16 + confirmed_delay 4 + hindsight 6 + label 7）：

### 3.1 causal（16 个，允许回测）

| key | DB 列名 | source | 说明 |
|---|---|---|---|
| `causal.atr` | `causal_atr` | structural_factor_service | ATR 波动率 |
| `causal.bb_percent_b` | `causal_bb_percent_b` | structural_factor_service | BB %B（close 在 band 中的位置） |
| `causal.bb_bandwidth_pct` | `causal_bb_bandwidth_pct` | structural_factor_service | BB 带宽百分比 |
| `causal.sqzmom_val` | `causal_sqzmom_val` | structural_factor_service | SQZMOM 动量值 |
| `causal.sqzmom_delta_1` | `causal_sqzmom_delta_1` | structural_factor_service | SQZMOM 一阶差分 |
| `causal.volume_ratio_20` | `causal_volume_ratio_20` | structural_factor_service | 20 日成交量比率 |
| `causal.volume_percentile_120` | `causal_volume_percentile_120` | structural_factor_service | 120 日成交量百分位 |
| `causal.active_swing_dir` | `causal_active_swing_dir` | structural_factor_service | active swing 方向（当时可知） |
| `causal.active_swing_high` | `causal_active_swing_high` | structural_factor_service | active swing 高点 |
| `causal.active_swing_low` | `causal_active_swing_low` | structural_factor_service | active swing 低点 |
| `causal.developing_swing_dir` | `causal_developing_swing_dir` | structural_factor_service | developing swing 方向 |
| `causal.developing_swing_high` | `causal_developing_swing_high` | structural_factor_service | developing swing 高点 |
| `causal.developing_swing_low` | `causal_developing_swing_low` | structural_factor_service | developing swing 低点 |
| `causal.dsa_confirmed_segment` | `causal_dsa_confirmed_segment` | structural_factor_service | DSA 段（当时已确认状态） |
| `causal.dsa_confirmed_direction` | `causal_dsa_confirmed_direction` | structural_factor_service | DSA 方向（当时已确认） |
| `causal.dsa_confirmed_age_bars` | `causal_dsa_confirmed_age_bars` | structural_factor_service | DSA 段已持续 bar 数（当时已确认） |

### 3.2 confirmed_delay（4 个，允许回测，不回填 anchor）

| key | DB 列名 | source | 说明 |
|---|---|---|---|
| `confirmed_delay.confirmed_swing_high` | `confirmed_delay_confirmed_swing_high` | structural_factor_service.swing | 已确认 swing 高点 anchor |
| `confirmed_delay.confirmed_swing_low` | `confirmed_delay_confirmed_swing_low` | structural_factor_service.swing | 已确认 swing 低点 anchor |
| `confirmed_delay.bars_since_confirmed_swing_high` | `confirmed_delay_bars_since_confirmed_swing_high` | structural_factor_service.swing | 距上次确认 swing 高点的 bar 数 |
| `confirmed_delay.bars_since_confirmed_swing_low` | `confirmed_delay_bars_since_confirmed_swing_low` | structural_factor_service.swing | 距上次确认 swing 低点的 bar 数 |

口径约束：只能在确认 bar 生效，不得回填 anchor date（否则会引入未来信息）。

### 3.3 hindsight（6 个，禁止回测）

| key | DB 列名 | source | 说明 |
|---|---|---|---|
| `hindsight.dsa_finalized_segment` | `hindsight_dsa_finalized_segment` | dsa_selector | DSA 段（未来确认后回标注） |
| `hindsight.dsa_finalized_direction` | `hindsight_dsa_finalized_direction` | dsa_selector | DSA 方向（未来确认后） |
| `hindsight.dsa_finalized_age_bars` | `hindsight_dsa_finalized_age_bars` | dsa_selector | DSA 段最终持续 bar 数 |
| `hindsight.node_cluster_label` | `hindsight_node_cluster_label` | volume_node_monitor | Node Cluster 结构标注（允许未来信息） |
| `hindsight.node_cluster_support` | `hindsight_node_cluster_support` | volume_node_monitor | Node Cluster 支撑结构（后验标注） |
| `hindsight.node_cluster_resistance` | `hindsight_node_cluster_resistance` | volume_node_monitor | Node Cluster 阻力结构（后验标注） |

口径约束：允许未来信息，只做结构标注，不可用于真实回测。

### 3.4 label（7 个，禁止作为 feature）

| key | DB 列名 | source | 说明 |
|---|---|---|---|
| `label.future_return_5d` | `label_future_return_5d` | research_label_service | 未来 5 日收益率 |
| `label.future_return_10d` | `label_future_return_10d` | research_label_service | 未来 10 日收益率 |
| `label.future_return_20d` | `label_future_return_20d` | research_label_service | 未来 20 日收益率 |
| `label.future_max_drawdown_10d` | `label_future_max_drawdown_10d` | research_label_service | 未来 10 日最大回撤 |
| `label.future_max_drawdown_20d` | `label_future_max_drawdown_20d` | research_label_service | 未来 20 日最大回撤 |
| `label.breakout_success_10d` | `label_breakout_success_10d` | research_label_service | 未来 10 日是否突破成功 |
| `label.failure_breakdown_10d` | `label_failure_breakdown_10d` | research_label_service | 未来 10 日是否破位失败 |

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

### 4.1 [Phase 1] hindsight DSA 未实现，全 NULL

**当前 Phase 1 状态**：`hindsight_dsa_finalized_*` 3 列在 DB 中全部 NULL。

- 真正的 hindsight 需要绕过 `_remove_dsa_lookahead` 取 raw DSA full series（翻转点会修正）；
- **禁止用 causal 近似冒充 hindsight 写入 DB**（Blocker 1）；
- `compute_dsa_dual_track_features` 保留 hindsight 3 列占位但写入 NaN；
- run metadata_json 必须记录 `dsa_hindsight_status=not_implemented`；
- 后续 PR 实现 raw DSA 后，再回填历史月份的 hindsight 列。
- **Phase 1 生产验证结果**：`hindsight_dsa_finalized_segment` / `direction` / `age_bars` 非 NULL 数为 0（2026-01 到 2026-07 共 621,769 行）。

## 5. Node Cluster 后验口径

Node Cluster 字段只能是 hindsight，不得是 causal：

- `hindsight.node_cluster_label` / `hindsight.node_cluster_support` / `hindsight.node_cluster_resistance`；
- 允许未来信息，用于事后标注支撑/阻力结构；
- 不得进入 causal 命名空间；
- 不得接入 watchlist 或生产筛选；
- 不得作为回测 feature。

### 5.1 [Phase 1] Node Cluster 未实现，全 NULL

**当前 Phase 1 状态**：`hindsight_node_cluster_*` 3 列在 DB 中全部 NULL。

- `compute_all_features` 保留 3 列占位但写入 NaN；
- run metadata_json 必须记录 `node_cluster_status=not_implemented` + `feature_version=phase1_no_node_cluster`；
- 后续 PR 集成 `VolumeNodeMonitor` 后再回填。
- **Phase 1 生产验证结果**：`hindsight_node_cluster_label` / `support` / `resistance` 非 NULL 数为 0（2026-01 到 2026-07 共 621,769 行）。

## 5.2 Phase 1 字段范围

当前 Phase 1 实际有值的字段：

| 命名空间 | 字段数 | Phase 1 状态 |
|---|---|---|
| causal (rolling + swing + dsa_confirmed) | 16 | ✅ 全部实现（causal_dsa_confirmed_* 有值） |
| confirmed_delay | 4 | ✅ 全部实现 |
| hindsight (dsa_finalized + node_cluster) | 6 | ❌ 全 NULL（Phase 1 未实现） |
| label | 7 | ✅ 全部实现 |
| **合计** | **33** | 27 有值 + 6 NULL |

## 6. confirmed_swing 口径

`confirmed_delay.confirmed_swing_*` 与 `confirmed_delay.bars_since_confirmed_swing_*` 必须是 `confirmed_delay` 命名空间：

- 只能在确认 bar 生效，不回填 anchor date；
- 不得作为 hindsight 默认回填；
- 允许回测，但必须遵守 confirmed_only 计算策略；
- 与 `causal.active_swing_*` / `causal.developing_swing_*` 区分清楚：
  - `causal.active_swing_*` 是当时可知的 active 段（已确认 major leg）；
  - `causal.developing_swing_*` 是未确认但当前可见的回落/反弹；
  - `confirmed_delay.confirmed_swing_*` 是已确认的 swing 高/低点 anchor。

## 7. 数据模型（DB 主存储）

研究矩阵写入两张专用表，由 `backend/alembic/versions/058_research_feature_matrix.py` 创建：

### 7.1 `research_feature_matrix_runs`（run 级元数据）

按月分批的 run 级元数据，记录状态机与统计摘要。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 运行 ID |
| `run_key` | Text UNIQUE | 运行唯一键（如 `2026-01_full`） |
| `month` | Text | 月份 YYYY-MM |
| `start_date` | Date | 起始日期 |
| `end_date` | Date | 结束日期 |
| `status` | Text | 运行状态：`running` / `succeeded` / `failed` |
| `instruments_count` | Integer | 实际处理股票数 |
| `trade_dates_count` | Integer | 实际处理交易日数 |
| `rows_count` | Integer | 写入行数 |
| `failed_count` | Integer | 失败行数 |
| `duration_seconds` | Float | 总耗时秒 |
| `started_at` | DateTime(tz) | 开始时间 |
| `finished_at` | DateTime(tz) | 完成时间 |
| `metadata_json` | JSONB | 小摘要 JSONB（scope/notes/thresholds），**不存完整 payload，不建 GIN 索引** |
| `created_at` | DateTime(tz) | 创建时间 |
| `updated_at` | DateTime(tz) | 更新时间 |

唯一约束：`run_key` 全局唯一，支持 `--resume` 通过 `run_key` 查找已有 run。

### 7.2 `research_feature_matrix_rows`（扁平宽表）

一只股票一个交易日的 33 个 feature 值，扁平宽表设计（非 EAV）。

| 列分类 | 列数 | 说明 |
|---|---|---|
| metadata | 5 | `id` / `run_id` / `instrument_id` / `symbol` / `trade_date` |
| causal feature | 16 | `causal_atr` / `causal_bb_percent_b` / `causal_bb_bandwidth_pct` / `causal_sqzmom_val` / `causal_sqzmom_delta_1` / `causal_volume_ratio_20` / `causal_volume_percentile_120` / `causal_active_swing_*` (3) / `causal_developing_swing_*` (3) / `causal_dsa_confirmed_*` (3) |
| confirmed_delay feature | 4 | `confirmed_delay_confirmed_swing_high/low` / `confirmed_delay_bars_since_confirmed_swing_high/low` |
| hindsight feature | 6 | `hindsight_dsa_finalized_*` (3) / `hindsight_node_cluster_*` (3) |
| label feature | 7 | `label_future_return_*` (3) / `label_future_max_drawdown_*` (2) / `label_breakout_success_10d` / `label_failure_breakdown_10d` |
| created_at | 1 | 创建时间 |
| **合计** | **39** | 5 metadata + 33 feature + 1 created_at |

设计原则：
- 33 个 feature 列与 `feature_causality_registry.db_column()` 1:1 对应；
- **不存完整 JSON payload，不写大 JSONB，不建 GIN 索引**；
- 所有 feature 列允许 NULL（warmup 期、未来 label 未到计算期等）；
- 唯一约束 `(instrument_id, trade_date)` 全局唯一，跨 run 幂等 upsert。

索引（精简，不每个 feature 建索引）：

| 索引名 | 字段 | 类型 |
|---|---|---|
| `uq_research_matrix_rows_inst_date` | `(instrument_id, trade_date)` | unique |
| `ix_research_matrix_rows_trade_date` | `trade_date` | btree |
| `ix_research_matrix_rows_instrument_id` | `instrument_id` | btree |
| `ix_research_matrix_rows_run_id` | `run_id` | btree |

> 后续确认哪些 feature 字段常查，再加单独索引。

## 8. 写入模块（research_matrix_writer）

`backend/app/research/research_matrix_writer.py` 提供研究矩阵 DB 写入与 run 生命周期管理：

| 函数 | 作用 |
|---|---|
| `check_disk_threshold(path="/")` | 磁盘剩余空间 >= 15GB → True |
| `check_month_size_threshold(estimated_gb)` | 单月预估 <= 3GB → True |
| `check_failure_rate(failed, total)` | 失败率 <= 5% → True（failed = failed_rows，total = expected_rows） |
| `resolve_month_range(month)` | `YYYY-MM` → `(start_date, end_date)`，用 `calendar.monthrange` 处理闰年 |
| `estimate_month_size(instruments_count, trade_dates_count)` | 估算单月 DB 占用 GB（rows × 2KB / 1024³） |
| `create_or_resume_run(db, *, month, start_date, end_date, scope, metadata)` | 创建或恢复 monthly run（`run_key = f"{month}_{scope}"`，相同 run_key 返回已存在 run） |
| `finalize_run(db, run, *, status, instruments_count, ..., failed_instruments=0)` | 终结 run：更新 status/统计/duration/finished_at；[Blocker Fix] 同时记录 `failed_count`（= failed_rows）与 metadata_json 中的 `failed_instruments` / `failed_rows` |
| `upsert_rows_batch(db, rows)` | 批量 upsert 到 `research_feature_matrix_rows`，使用 `INSERT ... ON CONFLICT (instrument_id, trade_date) DO UPDATE` 幂等覆盖；自动分批 1000 行 |
| `acquire_run_lock(db, *, month, scope)` → `bool` | [Blocker Fix] 尝试获取 `pg_try_advisory_lock(namespace, key)`，session-level；成功返回 True（调用方负责 finally 关闭 session 释放），已被占用返回 False |
| `acquire_lock_file(month, scope)` → `str \| None` | [Blocker Fix] 用 `os.open(O_CREAT \| O_EXCL \| O_WRONLY)` 原子创建 `/tmp/research_matrix_backfill_{month}_{scope}.lock`；成功返回路径，已存在返回 None |
| `release_lock_file(lock_path)` → `None` | [Blocker Fix] 删除 lock file，不存在静默忽略 |

常量：
- `DISK_MIN_GB = 15`
- `MONTH_SIZE_MAX_GB = 3.0`
- `FAILURE_RATE_MAX = 0.05`
- `UPSERT_BATCH_SIZE = 1000`（asyncpg 参数上限保守值）
- `_BYTES_PER_ROW = 2048`（39 列平均 ~50 字节）
- `_ADVISORY_LOCK_NAMESPACE = 0x5245534D`（"RESM"，避免与其他业务 advisory lock 冲突）

**失败率统计口径（Blocker Fix）**：
- `failed_count` 列存 `failed_rows`（行级失败数，用于失败率 = `failed_rows / expected_rows`）；
- `metadata_json.failed_instruments` 存股票级失败数（一个股票失败对应多 trade_date 行）；
- `metadata_json.failed_rows` 与 `failed_count` 列保持一致，便于查询；
- resume 不重置已 `succeeded` 的 full run 统计（`create_or_resume_run` 命中已有 run 时直接返回，不覆盖 status/started_at）。

**进程锁（Blocker Fix）**：
- 双保险：`pg_advisory_lock` + lock file；
- 同 `month/scope` 已有 running run 或 lock 存在时，CLI 退出并打印 `[BLOCKED]` 信息，不重复启动后台任务；
- `_advisory_lock_key(month, scope)` 用 sha1 稳定 hash（同一输入跨进程一致）；
- session-level advisory lock：CLI 退出（session close）自动释放，即使 lock file 未清理，下次也能通过 advisory lock 判断；
- CLI 主流程必须 try/finally 释放锁（先 close session，再 release_lock_file）。

## 9. 计算模块（feature_computer）

`backend/app/research/feature_computer.py` 提供 per-bar 因果口径特征计算：

- **与生产 `structural_factor_service` 的区别**：
  - 生产 snapshot: 只计算最后一根 bar 的 single-snapshot 值；
  - 研究矩阵: 计算每根 bar 的 per-bar 值（full series），用于按月回补。
- **入口**：`compute_all_features(bars)` 返回 DataFrame，index 为 trade_date，包含 33 个 feature 列。
- **底层复用现有算法 SSOT**（不重写公式）：
  - `compute_atr` (ATR 14, RMA)
  - `bollinger` (BB 20, 2σ)
  - `compute_sqzmom_lb` (SQZMOM)
  - `_tv_pivots_confirmed` (swing pivot)
  - `compute_dsa_history` (DSA 段历史)
- **warmup**：调用方需加载 `start_date - 400 天` 的 bars 作为 warmup（约 16 个月，足够 250 日 BB/ATR + 120 日 percentile）。

## 10. CLI（research_feature_matrix_backfill）

`backend/scripts/research_feature_matrix_backfill.py` 是研究矩阵 CLI 入口，整合 universe/date resolver + feature_computer + writer + 三道硬阈值。

### 10.1 CLI 参数

| 参数 | 必填 | 语义 |
|---|---|---|
| `--month YYYY-MM` | 与 `--start` 互斥必填其一 | 单月回补（推荐用法） |
| `--start YYYY-MM-DD` | 与 `--month` 互斥必填其一 | 起始日期（与 `--end` 配合用于跨月 sample 验证） |
| `--end YYYY-MM-DD` / `latest` | 可选，默认 `latest` | 结束日期 |
| `--symbols` | 可选 | 只处理指定股票代码（逗号分隔，触发 sample scope） |
| `--limit-instruments N` | 可选 | 限制处理 instrument 数（触发 sample scope） |
| `--dry-run` | 可选 | 只打印计划与估算，不写 DB，不写文件 |
| `--resume` | 可选 | 续跑模式：已存在 run 复用，已存在 instrument/date 幂等 upsert |
| `--export-parquet PATH` | 可选 | 可选 debug 导出 parquet 路径（仅 sample scope） |

> 已移除 `--output` / `--include-hindsight` / `--include-labels`：始终计算全部 33 字段，DB 主存储。

### 10.2 scope 规则

- `_resolve_scope(symbols, limit_instruments)`：
  - 有 `--symbols` → `sample_symbols`
  - 有 `--limit-instruments` → `sample_N`
  - 都无 → `full`
- `run_key = f"{month}_{scope}"`（如 `2026-01_full` / `2026-01_sample_100`）。

### 10.3 主流程

1. 解析日期范围（`--month` → `resolve_month_range`；`--start/--end` → 直接 `date.fromisoformat`）；
2. 检查磁盘阈值（`df -h /` 剩余 < 15GB 停止）；
3. 获取 universe（symbols/limit/full，默认只取 A 股 6 位数字 symbol）+ trade_dates（从 `bars_daily` 查询）；
4. 估算单月大小（rows × 2KB，> 3GB 停止）；
5. `--dry-run` 退出（只打印计划，不获取锁）；
6. [Blocker Fix] 获取进程锁（双保险）：
   - `acquire_lock_file(month, scope)` 创建 `/tmp/research_matrix_backfill_{month}_{scope}.lock`，失败退出（打印 `[BLOCKED] lock file 已存在`）；
   - `acquire_run_lock(lock_session, month=..., scope=...)` 获取 `pg_advisory_lock`，失败释放 lock file 并退出（打印 `[BLOCKED] pg_advisory_lock 已被占用`）；
   - 整个主循环 + finalize 包在 `try` 块中，`finally` 必须释放锁（先 `await lock_session.close()`，再 `release_lock_file(lock_path)`）；
7. 创建/resume run（`status=running`，`started_at=now`，`metadata_json` 必含 `feature_version=phase1_no_node_cluster` + `dsa_hindsight_status=not_implemented` + `node_cluster_status=not_implemented`）；
8. instrument-first 循环：每只股票 `load bars 1 次（含 warmup） → compute_all_features → 按月份 trade_date 切片 → upsert`；tqdm 进度条；每 100 只 instrument commit 一次；
   - [Blocker Fix] `_process_instrument` 各失败点返回 `(0, expected_rows)`（而非 `0, 1`），便于统计 `failed_rows`；
   - [Blocker Fix] upsert 异常时 `await db.rollback()` 后继续下一只股票（避免污染后续事务），并记 `failed_rows += expected_rows` + `failed_instruments += 1`；
9. 检查失败率（[Blocker Fix] `failed_rows / expected_rows > 5%` 标 `failed`，不继续后续月份）；
10. finalize run（更新 status/统计/duration/finished_at，`failed_instruments` 写入 `metadata_json.failed_instruments`，`failed_rows` 同时写入 `metadata_json.failed_rows` 与 `failed_count` 列）；
11. 可选 `--export-parquet`（仅 sample scope）。

### 10.4 用法示例

```bash
# dry-run 查看计划
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --dry-run

# 2 symbols 验证
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --symbols 000001,600000

# 100 stocks × 1 month
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --limit-instruments 100

# 全市场 2026-01
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01

# --resume 续跑（幂等 upsert）
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --resume

# 可选 debug 导出 parquet（不作为主存储）
cd /root/web_dev/backend && python -m scripts.research_feature_matrix_backfill \
    --month 2026-01 --symbols 000001 --export-parquet /tmp/debug.parquet
```

## 11. 三道硬阈值

研究矩阵回补必须遵守三道硬阈值，任一不通过即停止：

| 阈值 | 检查函数 | 触发条件 | 行为 |
|---|---|---|---|
| 磁盘剩余 | `check_disk_threshold` | `df -h /` 剩余 < 15GB | 停止（不创建 run） |
| 单月预估 | `check_month_size_threshold` | `estimate_month_size > 3GB` | 停止（不创建 run） |
| 失败率 | `check_failure_rate(failed_rows, expected_rows)` | [Blocker Fix] `failed_rows / expected_rows > 5%` | run 标 `failed`，不继续后续月份 |

设计原因：磁盘约 61GB 可用，数据库在 `/` 分区上，写数据库也会占用磁盘。

**失败率口径（Blocker Fix）**：
- `failed_rows` = 失败行数（一个股票失败对应多个 trade_date 行失败）；
- `expected_rows` = `instruments_count × trade_dates_count`（理论预期总行数）；
- `failed_instruments` = 失败股票数（仅写入 metadata_json，不参与失败率计算）；
- 单只股票 `_process_instrument` 失败时返回 `(0, expected_rows)`，主流程累加 `total_failed_rows += expected_rows` + `total_failed_instruments += 1`。

## 12. 分阶段验证

禁止直接全量跑到当前。必须按以下顺序逐阶段验证：

| 阶段 | 命令 | 验收点 |
|---|---|---|
| A. dry-run | `--month 2026-01 --dry-run` | 打印计划，`expected_rows` / `estimated_db_size` 合理 |
| B. 2 symbols | `--month 2026-01 --symbols 000001,600000` | run succeeded，rows 写入正确 |
| C. 100 stocks × 1 month | `--month 2026-01 --limit-instruments 100` | run succeeded，failed_rate < 5% |
| D. 全市场 2026-01 | `--month 2026-01` | run succeeded，磁盘占用合理 |
| E. 后台逐月回补到当前 | nohup 串行跑 `2026-02` 到当前 | 每月 run succeeded，磁盘监控 |

**Phase 1 实际验收结果**：
- A/B/C/D 前台验证全部通过（D 阶段 rows=102603，failed_rate=2.90%，表大小 38MB）；
- E 阶段 2026-02 到 2026-07 后台串行逐月完成，共写入 621,769 行；
- 全部 9 个 run status=succeeded，failed_rate 均 <= 5%；
- 覆盖日期：2026-01-05 到 2026-07-08（122 个交易日）；
- `research_feature_matrix_rows` 总大小：223 MB。

**关键约束**：
- 阶段 B/C/D 必须在 PR merge + migration 058 应用后才能执行；
- 阶段 A/B/C/D 必须前台执行（前台跑完才能跑下一阶段），不允许 nohup；
- **只有 D 阶段通过后，才允许启动后台逐月回补（阶段 E）**；
- 后台逐月回补必须串行，不并行多月；
- 后台逐月 runbook 与停止条件详见 `03-jobs-integrations-operations.md` §2.5 节；
- 每阶段必须检查：`df -h /` / `rows_count` / `failed_rows` / `failed_rate` / `run.status` / 表大小 / 日志是否有 traceback。

## 13. 与 production snapshot 的区别

| 维度 | production snapshot | research matrix |
|---|---|---|
| 数据载体 | `stock_feature_snapshots` 表 | `research_feature_matrix_runs` + `research_feature_matrix_rows` 两张表 |
| 时效 | 最近交易日 + 自选股 | 任意历史日期范围 |
| 因果口径 | 必须 point-in-time | 可同时包含 causal/hindsight/label |
| watchlist_ready | 是 | 否（不接入） |
| 字段登记 | 不强制 namespace 前缀 | 必须带 namespace 前缀 |
| 写库 | upsert 幂等 | upsert 幂等（`ON CONFLICT (instrument_id, trade_date) DO UPDATE`） |
| 全市场回补 | BLOCKED（PR #41 126min > 120min 阈值） | 按月分批 + 三道硬阈值 |
| 计算 | single-snapshot（最后一根 bar） | per-bar full series |
| 数据载体设计 | JSONB summary + 结构化字段 | 扁平宽表 33 列 |

## 14. 禁止项

- 不要把 hindsight 或 label 字段当成 causal feature；
- 不要把 hindsight/label 接入 watchlist 或生产筛选；
- 不要把 research matrix 写入 `stock_feature_snapshots`（只写专用 research 表）；
- 不要写大 JSONB payload 或建 GIN 索引（扁平宽表设计）；
- 不要写大 CSV/coverage/截图/大日志/DB 备份；
- 不要把 parquet 作为主存储（仅可选 debug 导出）；
- 不要在无 sample scope 下导出 parquet（`--export-parquet` 只允许 sample scope）；
- 不要直接全量跑到当前（必须分阶段验证）；
- 不要删除数据库卷或运行中镜像；
- **[Blocker Fix] 不要把 hindsight 近似（causal segment_ids/directions/age_bars）冒充 hindsight 写入 DB**，Phase 1 未实现的 hindsight 字段必须保持 NULL；
- **[Blocker Fix] 不要在 PR body 或 docs 中宣称 Node Cluster 已完成**，Phase 1 `hindsight_node_cluster_*` 全 NULL，必须写明 `feature_version=phase1_no_node_cluster`；
- **[Blocker Fix] 不要在 upsert 异常后不 rollback 直接继续下一只股票**，会污染后续事务；
- **[Blocker Fix] 不要在无进程锁的情况下启动后台回补**，必须同时持有 `pg_advisory_lock` 与 lock file；
- **[Blocker Fix] 不要并行多月回补**，每月串行，前一个月完成才跑下一个月；
- **[Blocker Fix] 不要跑 production `stock_feature_snapshots` 历史回补**（仍 BLOCKED，见 `03-jobs-integrations-operations.md` §2.4）。

## 15. Regime Discovery（无监督候选状态发现 V1）

### 15.1 目的与边界

- 只读 `research_feature_matrix_rows` + `bars_daily`，派生 17 个聚类特征
- 与生产完全隔离：不改 API/前端/Worker/scheduler/migration/snapshot/watchlist/通知
- 事务 `READ ONLY` + `statement_timeout=120s`
- 不强行得出聚类结论

### 15.2 特征清单（17 个）

11 个基础归一化特征（从 causal_* 派生，不重算公式）：
- `atr_pct` = causal_atr / close
- `bb_percent_b` = causal_bb_percent_b
- `bb_bandwidth_log` = log1p(causal_bb_bandwidth_pct)
- `sqzmom_atr` = causal_sqzmom_val / causal_atr
- `sqzmom_delta_atr` = causal_sqzmom_delta_1 / causal_atr
- `volume_ratio_log` = log1p(causal_volume_ratio_20)
- `volume_percentile_120` = causal_volume_percentile_120
- `swing_position` = active/developing swing 位置 [0, 1]
- `dsa_dir` = causal_dsa_confirmed_direction sign（-1/0/1）
- `dsa_age_log` = log1p(causal_dsa_confirmed_age_bars)

6 个时序差分特征（按 instrument 时间排序派生）：
- `bb_percent_b_delta_5` / `bandwidth_delta_5` / `sqzmom_atr_delta_5` / `volume_percentile_delta_5`
- `return_5d` / `realized_vol_10d`

### 15.3 双表示

- **absolute**: RobustScaler（median + IQR）
- **cross_sectional**: 按 trade_date 横截面 rank（pct=True，输出 [0, 1]）
- `--representation both` 时分别跑两种，manifest 记录两者结果

### 15.4 模型

- 主模型：MiniBatchKMeans（k=3..8，n_init=10，max_iter=100）
- 辅助模型：diagonal-covariance GMM（max 60,000 行，仅对比不作为主模型）
- PCA 降维：相关性剪枝后保留 90% 方差且最多 8 维

### 15.5 拒绝门槛

| 指标 | 门槛 |
|---|---|
| silhouette | >= 0.08 |
| bootstrap ARI | >= 0.60 |
| centroid cosine | >= 0.85 |
| 最小簇占比 | >= 3% |
| 最大簇占比 | <= 60% |

任一不达标则不选 k，报告 "当前样本未发现稳定固定组合"。

### 15.6 簇命名与描述

- 簇只命名 R1…Rk
- 仅当某特征在 >=80% bootstrap 中方向一致且 |median z|>=0.5 才进入状态描述
- 禁止直接命名为吸筹/主升/派发

### 15.7 CLI 参数

```
--dry-run, --start, --end, --sample-rows (150000), --seed (42),
--k-min (3), --k-max (8), --chunk-size (25000), --max-rss-mb (1500),
--representation {absolute,cross_sectional,both}, --output-dir
```

### 15.8 输出文件

默认输出到 `/home/ubuntu/panji_research_outputs/regime_discovery/<run_id>/`：
- manifest.json（git SHA / data_as_of / SQL row count / 特征清单 / 排除原因 / 种子 / 阈值 / 模型参数 / 资源峰值）
- distribution_summary.csv / drift_summary.csv / model_selection.csv
- cluster_profiles.csv / cluster_stability.csv / transition_matrix.csv
- report.md

### 15.9 资源约束

- float32、单进程
- OMP/OPENBLAS/MKL threads=1
- RSS ≤ 1.5GB
- 输出 ≤ 50MB，保留最近 3 次
- 不导出全量 CSV/Parquet，不复制数据库，原始矩阵不落盘

### 15.10 横截面 rank 正确性（Phase E 修复）

**核心规则**：最终选中的 cross-sectional rank 表示，必须基于同一 `trade_date` 下完整市场横截面计算。

**严禁**：
- 按股票分块后，在每个股票块内部独立计算横截面 rank；
- 按任意行 chunk 分割同一交易日后分别 rank；
- 使用 15 万样本的 transition matrix 冒充全量结果。

**实现**：
- Phase E 使用 `data_access.get_all_matrix_rows(session, start, end)` 一次性读取全量 621k 行；
- `pd.read_sql` 配合 `chunksize=50000` 流式读取 + 逐 chunk `astype(np.float32)`，降低峰值 RSS；
- `SET LOCAL statement_timeout = '600s'` 应对全量读取（默认 120s 不足）；
- `build_features` 内部 `sort_values(["instrument_id", "trade_date"])` 排序，`transform_feature_matrix` 在完整 DataFrame 上按 `trade_date` 分组 rank；
- `chunksize` 只控制读取内存峰值，不影响 rank 正确性 — 所有 chunk 合并为完整 DataFrame 后再 rank。

**Fit/Transform 分离**：
- `fit_winsorize_bounds(features_df, features)` → 返回 bounds dict
- `transform_winsorize(features_df, features, bounds)` → 截断
- `transform_feature_matrix(df, features, representation, prep_params)` → 应用 scaler/rank + dropna
- `transform_pca(X, pca_params)` → PCA 降维
- Phase E 复用 sample 拟合的 `winsorize_bounds` / `scaler` / `pca_params`，不重新拟合，保证全量 assignment 与 sample 模型一致。

**测试**：
- `test_preprocessing.py::TestCrossSectionalRankFullSection`（4 测试）：完整横截面 rank 正确性、chunk 分割产生不同 rank（反证）、按 trade_date 分割结果一致、`transform_feature_matrix` 完整横截面；
- `test_cli.py::TestSampleVsFullAssignmentMode`（3 测试）：dry-run 提及两阶段、`--sample-rows` 控制样本量、full assignment 使用 `get_all_matrix_rows`。

### 15.11 全量 Phase E 验证结果（2026-07-13）

**运行配置**：`--sample-rows 150000 --seed 42 --k-min 3 --k-max 8 --representation both`

**结果**：
- 数据范围：2026-01-05 ~ 2026-07-08（122 个交易日）
- SQL 行数：621,769
- 样本行数：126,292（分层抽样）
- 全量 assignment 有效行：570,011（dropna 后）
- 特征数：17 → 16（相关性剪枝丢弃 `developing_swing_position`，与 `active_swing_position` |ρ|=1.0）
- PCA 维度：8（cross_sectional 累计解释方差 ~92.6%）
- 候选 k：3（cross_sectional representation 通过）
- silhouette：0.3675（cross_sectional）/ 0.1619（absolute）
- bootstrap ARI：0.7843（cross_sectional，>= 0.6 通过）/ 0.4075（absolute，< 0.6 拒绝）
- cluster 占比：R1=24.0% / R2=55.0% / R3=21.0%
- peak RSS：1242.55 MB（< 1.5GB 目标）
- 输出大小：0.04 MB（< 50MB 限制）
- 无 traceback

**输出文件**（10 个）：
- manifest.json / distribution_summary.csv / drift_summary.csv / model_selection.csv
- cluster_profiles.csv / cluster_stability.csv / transition_matrix.csv
- monthly_prevalence.csv / dwell_time.csv / report.md

### 15.12 候选状态性质说明

**当前数据期约 6 个月（2026-01 至 2026-07），不足以证明长期稳定。**

- 目前寻找的是**候选状态**，不是固定规律；
- 6 个月数据仅能作为候选，需后续扩展数据期复验；
- 若没有特征达到 80% bootstrap 描述门槛，**不得强行生成状态解释**；
- 允许最终结论为"没有发现达到标准的稳定组合"；
- 当前样本无特征满足 80% bootstrap 一致 + |median z|>=0.5 门槛 — 报告"无特征满足描述门槛"，不强行命名状态；
- 簇只命名 R1/R2/R3，禁止直接命名为吸筹/主升/派发；
- `absolute` representation 未通过稳定性门槛（ARI=0.4075 < 0.6），属设计预期，不作为主模型产出。
