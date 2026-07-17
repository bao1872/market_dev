# 市场数据 SSOT 与前复权统一出口分析（CHANGE-20260717-002）

本文件记录"统一行情读取出口并修复前复权"（Market Data SSOT + QoFQ Adjustment v2）的完整设计：旧问题、最终数据流、复权唯一出口、`adjustment_as_of` 公式、周/月"复权日线后聚合"、日内因子映射、factor rebuild/fingerprint/cache 失效、迁移/回滚、603538 真实回归证据。

> 真源：`ref/PROMPT.md`（任务规范）、`backend/app/services/market_data_aggregation_service.py`（MDAS）、`backend/app/services/adjustment_factor_service.py`（AdjustmentFactorService）、`backend/app/services/adj_factor.py`（复权计算）、`backend/app/services/kline_aggregator.py`（周/月聚合）。

---

## 一、旧问题（本次修复前）

### 1.1 行情读取出口分散
- 多个业务模块（indicator_service、strategy_batch、feature_snapshot、chart_bars、structural_factor、capture、monitor）各自调用 `bar_repository` 的私有查询（`_query_daily_bars`/`_query_15min_bars`/`_query_60min_bars`/`_query_minute_bars`）或旧 `bar_repository.get_bars`，绕过统一出口。
- 复权在多处分散执行：`apply_adj_factor_to_bars`（repository 封装）、`apply_adj_factor`/`apply_adj_factor_intraday`（service 计算模块）被业务层直接调用，存在"二次复权"风险。

### 1.2 adj_factor 列信任 Bug（核心缺陷）
- pytdx hybrid bar（15:00:00 合成日线）和 15m/60m/1m 行内自带 `adj_factor=1.0`（错误值）。
- 旧 `_apply_adj_factor_core` 优先使用 bar 自带的 `adj_factor` 列，而非权威因子序列 `merge_asof` 结果 `_adj`：
  ```python
  # 旧（错误）：若 bar 自带 adj_factor 列则直接用，忽略权威序列
  if "adj_factor" in merged.columns:
      ratio = merged["adj_factor"] / denominator_factor
  ```
- 后果：pytdx hybrid bar 的 `adj_factor=1.0` 被采用，导致 qfq 未调整，除权日前后价格断层。

### 1.3 复权无 point-in-time 语义
- 历史回算（盘后 snapshot、DSA）与当前页面使用同一因子序列（含未来除权事件），存在未来泄漏：`adjustment_as_of` 未定义，as_of 之后的除权事件会影响历史 qfq 价。

### 1.4 日内/周/月复权口径不统一
- 15m/60m/1m 信任行内旧 `adj_factor` 作为复权真源（违反"权威日线因子覆盖"原则）。
- 周/月线未明确"日线完成复权后再聚合"的顺序，可能在 raw 上聚合后再复权，导致价格不一致。

### 1.5 factor rebuild 只更新最近 5 根
- 公司行为因子更新时只刷新最新 5 根 bar 的 `adj_factor`，不重建完整历史因子序列，导致历史因子缺失/错误。

### 1.6 因子失败用 1.0 伪装成功
- factor rebuild 失败时返回 1.0 伪装成功，不返回 degraded 状态和原因，下游无法感知数据降级。

---

## 二、目标架构

### 2.1 MarketDataAggregationService (MDAS) — 行情读取唯一出口

```
┌─────────────────────────────────────────────────────────────┐
│  业务层（indicator / strategy_batch / feature_snapshot /    │
│         chart_bars / structural / capture / monitor / API） │
└───────────────────────────┬─────────────────────────────────┘
                            │ 仅调用 MDAS.get_bars(...)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  MarketDataAggregationService (MDAS) — 唯一行情出口          │
│  - 读取 raw bars（经 repository 私有 _query_*）              │
│  - 调用 AdjustmentFactorService 获取权威因子序列             │
│  - 应用复权（adj_factor._apply_adj_factor_core，仅一次）     │
│  - 周/月：日线完成复权后经 kline_aggregator 聚合             │
│  - 返回 bars + 诊断字段（hash/contract_version/as_of/...）   │
└──────────┬──────────────────────┬───────────────────────────┘
           │                      │
           ▼                      ▼
┌──────────────────┐   ┌─────────────────────────────────────┐
│ bar_repository   │   │ AdjustmentFactorService              │
│ - 私有 _query_*  │   │ - get_factor_series(as_of=) 截断     │
│ - raw OHLCV 读写 │   │ - detect_company_action_change       │
│ - 上游 fetch     │   │ - rebuild_factor_series（完整重建）  │
│   （pytdx 等）   │   │ - fingerprint + 缓存精确失效          │
└──────────────────┘   └─────────────────────────────────────┘
```

**职责边界**：
- **MDAS**：行情读取 + 复权应用 + 周/月聚合的**唯一出口**。生产模块除 MDAS 和 repository 内部外，禁止导入 repository 私有 `_query_*`/`_get_adj_factor_df`/`apply_adj_factor_to_bars`/旧 `get_bars`。
- **AdjustmentFactorService**：权威因子序列管理。`get_factor_series(session, instrument_id, as_of=date)` 返回**只含 `trade_date <= as_of` 的截断因子序列**（point-in-time 语义）。
- **bar_repository**：仅负责 raw OHLCV / 公司行为因子的 DB 读写和上游拉取，不负责复权应用。
- **adj_factor（计算模块）**：纯计算，由 AdjustmentFactorService / MDAS 包装调用，业务层禁止直接导入。
- **kline_aggregator**：周/月聚合出口，仅 MDAS 导入。

### 2.2 raw 不复权，qfq 只应用一次
- 原始 bar 在 repository / DB 层**保持不复权**。
- qfq 只在 MDAS 出口应用**一次**（`_apply_adj_factor_core`），使用权威因子序列 `merge_asof` 结果 `_adj`，**不信任 bar 自带 `adj_factor` 列**（pytdx hybrid / 15m/60m/1m 行内旧值不可信）。
- 周/月线在**日线完成复权后**再聚合，不会再次进入复权函数。

### 2.3 adjustment_as_of — point-in-time 复权

**公式**：`qfq_price = raw_price × factor(bar_date) / factor(as_of)`

- `factor(as_of)`：截至 `as_of` 的最近交易日因子（ffill 语义），取**截断因子序列**（`trade_date <= as_of`）的最后一个因子。
- `factor(bar_date)`：截断序列中 `<= bar_date` 的最后一个因子（ffill）。
- **无未来泄漏**：as_of 之后的除权事件不参与计算。bar_date > as_of 的 bar 用截断序列 ffill 的因子（即 as_of 时点已知的最后一个因子）。
- **当前页面**：`adjustment_as_of` 锚定请求业务日（默认 None=最新因子，向后兼容）。
- **盘后/历史回算**：`adjustment_as_of=trade_date`，只使用截至该日可知的公司行为和因子。
- **adj=none 时**：`adj_factor_hash` 为空，不应用复权。

### 2.4 周/月"复权日线后聚合"
- 周/月线流程：MDAS 先取 raw 日线 → 应用 qfq 复权（仅一次）→ 经 `kline_aggregator.convert_kline_frequency` 聚合为周/月。
- 禁止在 raw 日线上聚合后再复权（会导致聚合价格与日线 qfq 不一致）。

### 2.5 日内因子映射
- 15m/60m/1m 的复权：同一交易日内所有 bar 映射到**同一权威日线因子**（`merge_asof` 按 trade_date 对齐）。
- 禁止信任 15m/60m/1m 行内旧 `adj_factor` 作为复权真源；列可保留兼容，但读取时必须由权威日线因子覆盖。

### 2.6 factor rebuild / fingerprint / cache 失效
- **完整重建**：公司行为集合或 fingerprint 变化时，从**最早受影响日期**重新计算该股票完整日线 factor 序列并原子 upsert；禁止只更新最近 5 根。
- **fingerprint**：detect → earliest_affected → rebuild → 成功后更新 fingerprint；失败回滚 fingerprint，可重试。
- **缓存精确失效**：rebuild 成功后精确失效该股票的 MDAS / indicator 缓存（按 instrument_id + 算法版本隔离）。
- **失败不伪装**：rebuild 失败不得用 1.0 伪装成功，返回 degraded 状态和原因。

### 2.7 盘后顺序
```
原始日线刷新 → 公司行为/factor 重建（成功）→ 覆盖率门禁 / DSA → snapshot 发布
              ↓（因子未完成）
        不得创建 DSA 或发布 snapshot
```
因子失败时不得创建 DSA 或发布受影响结果。

---

## 三、MDAS v2 请求契约

### 3.1 请求参数

| 参数 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `timeframe` | str | — | `1d`/`15m`/`1h`/`1m`/`1w`/`1mo` |
| `adj` | str | `qfq` | `none`/`qfq` |
| `include_realtime` | bool | False | 是否包含 partial/实时 bar |
| `completed_only` | bool | False | 仅返回已完成 bar |
| `start_date`/`end_date` | date | None | 时间窗口 |
| `limit` | int | None | bar 数上限 |
| `warmup_bars` | int | None | 额外 warmup bar 数（SMC 等） |
| `adjustment_as_of` | date | None | point-in-time 复权锚点（None=最新） |

### 3.2 返回字段（BarListResponse / BarsResult）

| 字段 | 语义 |
|---|---|
| `bars` | DataFrame（qfq 或 raw OHLCV） |
| `market_data_contract_version` | 契约版本（隔离旧客户端） |
| `source_bar_hash` | raw OHLCV + 时间 SHA256[:16]（跨调用方一致性） |
| `adj_factor_hash` | 因子序列 SHA256[:16]（adj=none 时为空） |
| `adjustment_as_of` | 回显实际使用的 as_of |
| `completed_through` | 已完成 bar 截止时间（`pd.Timestamp \| None`） |
| `data_source` / `degraded` / `degraded_reason` | 来源与降级诊断 |

### 3.3 缓存键
缓存键包含全部参数和算法版本（timeframe/adj/include_realtime/completed_only/start/end/limit/warmup/as_of + contract_version + algorithm_version），true/false 状态隔离。

---

## 四、调用方 → MDAS 参数 → 用途矩阵

> 全库扫描确认：前端主 K 线、小 K 线、指标/SMC、DSA、盘后 snapshot、monitor、capture、structural/temporal 全部经 MDAS。`/quote` 允许独立实时报价出口，但不得作为历史 K 线或指标输入。

| 调用方 | 文件:函数 | timeframe | adj | include_realtime | completed_only | adjustment_as_of | 用途 |
|---|---|---|---|---|---|---|---|
| bars API | `api/bars.py:get_bars` | 用户传入 | 用户传入 | 用户传入 | 用户传入 | 用户传入 | 前端主/小 K 线行情 |
| indicators API | `indicator_service.compute_all_indicators` | 1d/15m/1m/1h/1w/1mo | adj | True | — | — | 指标计算（MACD/Node/BB 等） |
| SMC | `indicator_service`（SMC 分支） | 15m | adj | True | — | — | SMC 计算（warmup_bars=1000） |
| strategy_batch (DSA) | `strategy_batch_service._execute_single_instrument` | 1d | qfq | False | True | run.trade_date | DSA 全市场特征计算 |
| feature_snapshot | `feature_snapshot_service._fetch_bars_from_db` | 1d | adj | False | True | trade_date | 盘后特征快照 |
| chart_bars | `chart_bars_service.load_chart_bars` | 1d | adj | — | True | — | 图表日线 |
| structural_factor | `structural_factor_service.compute_structural_factors` | 1d/15m | adj | False | True | — | 结构因子（主/副周期） |
| capture | `api/capture.py:get_capture_snapshot` | 用户传入 | qfq | True | — | — | 截图展示（实时 partial） |
| monitor | `watchlist_monitor` | 1d/15m | qfq | False | — | — | 盘中监控（仅已完成 bar） |

**一致性契约**：同一股票/周期/截止日下，`/bars`、indicator/SMC、strategy_batch、feature_snapshot 的时间序列、OHLC、`source_bar_hash`、`adj_factor_hash` 必须一致。

---

## 五、核心修复：adj_factor 列信任 Bug

`backend/app/services/adj_factor.py::_apply_adj_factor_core`

**修复前**（错误）：
```python
if "adj_factor" in merged.columns:
    ratio = merged["adj_factor"] / denominator_factor  # 用 bar 自带列（pytdx=1.0，错误）
```

**修复后**（始终用权威序列 `_adj`）：
```python
# 始终使用权威因子序列（merge_asof 的 _adj），不信任 bar 自带 adj_factor 列。
# pytdx hybrid bar / 15m/60m/1m 行内旧 adj_factor 不可信（可能为 1.0），
# 必须由权威日线因子覆盖（CHANGE-20260717-002 硬规则）。
missing_count = int(merged["_adj"].isna().sum())
merged["_adj"] = merged["_adj"].fillna(denominator_factor)
ratio = merged["_adj"] / denominator_factor
```

---

## 六、架构守护（AST 测试）

`backend/tests/test_market_data_ssot_architecture.py`（5 个测试，全部通过）：

1. `test_no_business_module_imports_forbidden_from_bar_repository` — 业务模块禁止导入 repository 私有 `_query_*`/`_get_adj_factor_df`/`apply_adj_factor_to_bars`/旧 `get_bars`。
2. `test_no_business_module_imports_adj_factor_directly` — 业务模块禁止直接导入 `adj_factor.apply_adj_factor*`（应经 AdjustmentFactorService/MDAS）。
3. `test_only_mdas_imports_kline_aggregator` — kline_aggregator 仅 MDAS 导入（周/月聚合出口唯一）。
4. `test_no_business_module_resamples_weekly_monthly` — 业务层禁止自行 resample 周/月聚合（例外：`strategy_assets/algorithms/` 算法内部可在已获取 bars 上计算特征，如 SMC 的 PDH/PDL 水平线，不属于行情出口聚合）。
5. `test_mdas_is_sole_importer_of_private_queries` — 正向守护：MDAS 导入 bar_repository 私有 `_query_*`（确认出口唯一）。

---

## 七、603538 真实回归证据（Step6）

> 验证脚本：`backend/scripts/verify_603538_step6.py`。除权日 2026-07-09，验证窗口 2026-06-15 ~ 2026-07-15。

### 7.1 Step1: factor rebuild
- 重建后因子序列 856 根记录，窗口内 06-15 ~ 07-08 全部 `factor=0.711471`，除权日 07-09 起 `factor=1.0`。
- 除权日前因子一致 ✓，除权日后 factor=1.0 ✓。

### 7.2 Step2: 1d/15m/1h × none/qfq
- none 与 qfq bar 数量、时间索引一致 ✓。
- adj=none 时 `adj_factor_hash` 为空 ✓。
- 除权日前 raw_close≈39，qfq_close≈28（×0.7115），价格连续 ✓。

### 7.3 Step3: adjustment_as_of 三锚点（无未来泄漏）

| as_of | factor(as_of) | 表现 | 无泄漏 |
|---|---|---|---|
| 2026-07-01（除权前） | 0.711471 | 所有 bar qfq=raw（ratio=1） | ✓ 截断序列无未来事件 |
| 2026-07-03（除权前） | 0.711471 | 所有 bar qfq=raw | ✓ |
| 2026-07-15（除权后） | 1.0 | 除权前 bar 下调（39.39→28.02） | ✓ 截断序列因子数=完整序列 ≤as_of 因子数 |

公式 `qfq = raw × factor(bar_date) / factor(as_of)` 全部通过 ✓。

### 7.4 Step4: 对照股 600276 恒瑞医药（无公司行为）
- factor 全 1.0，none 与 qfq 一致 ✓。

### 7.5 Step5: 跨调用方 hash 一致性
- `/bars`、indicator、strategy_batch、feature_snapshot 四调用方：
  - `source_bar_hash=48d5bd812528ca42` ✓ 一致
  - `adj_factor_hash=262a210aea141032` ✓ 一致

### 7.6 Step6: factor rebuild 幂等 + fingerprint 回滚
- detect 返回 None（无新增公司行为变化）✓。
- 两次 rebuild 因子序列完全一致（幂等）✓。

### 7.7 SMC parity
- SMC 仅验证输入一致和无回归，不宣称 Pine 输出级完全对齐，保留 `PINE_PARITY_PENDING`。

---

## 八、迁移与回滚

### 8.1 迁移
- 纯代码层重构 + bug fix，无 schema migration（复用现有 `bars_daily.adj_factor` 列）。
- 新增 `alembic` 迁移 063↔064（feature_snapshot schema v2，含 contract_version/source_bar_hash/adj_factor_hash/completed_through/adjustment_as_of 落库），需测试库可逆验证。

### 8.2 回滚
- 回滚分支即可还原代码；MDAS v2 契约版本号隔离旧客户端缓存。
- factor rebuild 已写入 `bars_daily.adj_factor`，回滚后旧因子仍在（可重新 rebuild）。
- snapshot schema v2 与 v1 不混用（schema version 校验，旧格式 fallback 兼容）。

---

## 九、未完成事项

- **PINE_PARITY_PENDING**：SMC 输出级 parity 待 TradingView CSV fixture（不伪造，不宣称完全对齐）。
- **部署验收**：分支部署 → 页面真实验收 → PR/merge main → 从 main 完整部署（Step9）。
