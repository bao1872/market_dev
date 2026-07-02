> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 12 策略与指标契约

## 1. 生产策略

当前生产只保留：

- `dsa_selector`：全市场特征计算；
- `watchlist_monitor`：有效会员自选股盘中事件判断。

两者不可由用户组合，生产参数不可由用户修改。

## 2. DSA Selector

> 实现核对：本节为已确认契约；`6f5ae2c` 尚未满足完整发布要求，当前差异见 `ALIGN-004`、`ALIGN-005`。

### 2.1 职责分离

DSA Selector 由四个独立阶段组成：

1. **Universe Builder**：确定 total universe 与 computable universe；
2. **Feature Computation**：为 computable universe 中每只股票计算特征并写入 `StrategyResult`；
3. **Run Quality Gate**：检查运行完整性，决定是否允许自动发布；
4. **Selector Query**：在已发布结果上执行前端筛选、排序、分页，不触发重算。

### 2.2 Universe

- 总 Universe：全部 active A 股标的；
- computable universe：满足市场支持、最小 250 根前复权日线和数据质量要求的股票；
- 停牌、行情不足或明确不支持可 skipped，必须保存 reason code；
- BSE 或其他市场不能因为数据源实现缺陷被静默排除，应明确支持或明确 skipped 原因。

### 2.3 计算原则

- 只计算特征，不做预筛选；
- 多头、空头、震荡、强弱和 `matched=false`、特征值为负都应写入结果；
- 用户筛选不影响运行 Universe；
- 每个 computable 股票必须产生一条 StrategyResult；
- 单股超时、异常、数据库失败和预算失败属于 failed，不得当作 skipped 或未命中；
- DSA 公式、窗口、复权和输出由 released StrategyVersion 固定。

### 2.4 输出

| 输出 | 用途 |
|---|---|
| `factor_per_bar` | 因果时间序列、查询和回测 |
| `last_row_metrics` | StrategyResult 快照 |
| `visual_segments` | 图表分段渲染，不用于筛选 |
| `pivot_labels` | HH/HL/LH/LL 标签 |
| `anchor` | 锚点元信息 |
| `diagnostics` | 数据质量、耗时和错误诊断 |

### 2.5 发布完整性

自动发布要求同时满足：

- run 状态为 `completed`；
- `failed_count = 0`；
- `result_count = succeeded_count`；
- `succeeded_count + skipped_count = total_instruments`；
- 所有 skipped 有允许原因并保存 reason code；
- computable universe 结果覆盖率为 100%。

`partial_failed` 不得自动发布。用户查询始终绑定不可变 `published_run_id`。

前端显示字段：

| 字段 | 含义 |
|---|---|
| `total` | 总 Universe 股票数 |
| `computable` | 可计算 Universe 股票数 |
| `succeeded` | 成功计算数 |
| `failed` | 失败数 |
| `skipped` | 允许跳过数 |
| `result_count` | 写入 StrategyResult 数 |
| `coverage` | computable universe 覆盖率 |
| `source_total` | 原始数据源股票数 |
| `filtered_total` | 前端筛选后股票数 |

每页 50 仅是分页，不代表只计算 50。

### 2.6 资源预算

单股 100ms 硬中断不是确认的永久产品规则。预算必须通过代表性基准测试得到，至少统计冷/热启动 p50、p90、p95、p99、max，并使用 run 级心跳和总超时。预算超限不得被当作未命中或允许 skipped。

基于 2026-07-02 对 350 只代表性活跃 A 股的基准测试（`backend/tools/dsa_benchmark.py`）：

| 启动类型 | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) | max (ms) | avg (ms) |
|---|---|---|---|---|---|---|
| 冷启动 | 129.62 | 181.87 | 203.89 | 231.60 | 321.32 | 138.17 |
| 热启动 | 130.46 | 182.29 | 195.79 | 224.66 | 270.01 | 136.53 |

测试规模：有效冷启动 350、有效热启动 350、失败 0、跳过 0、总耗时约 105.78s。完整报告见 `backend/reports/dsa_benchmark_20260702.md`。

## 3. Watchlist Monitor

输入包括：最新两根已完成 1m Bar、日线与 15m 历史、Bollinger、Volume Node Cluster、上一交易日收盘和当前用户资格。

同一策略版本、股票、源 Bar 只评估一次；Node 按 touch episode 去重；BB 按边界和冷却去重。事件只为有效会员生成 Recipient 和投递。

## 4. 行情和图表契约

DSA、Node、页面和截图必须使用同一行情快照和时间键。partial Bar 明确标识；缺失数据不绘制虚假连线；`visual_segments` 与 `factor_per_bar` 不混用。

## 5. 参数所有权

- 基础指标参数：`indicator_contract.py`；
- 策略版本、输入和输出：Manifest + released StrategyVersion；
- 套餐：`plans` 表；
- 页面：只展示和提交筛选条件，不产生算法默认值。

代码中发现重复预算、窗口或事件常量时，必须登记 Alignment 并收口，不能复制新的第三处。

## 6. Node Cluster 输入契约（目标）

- 日线：最近 250 根已完成 qfq（DAILY_HISTORY_BARS=250）
- 15m：最近 4000 根已完成 qfq（250*16=4000）
- 1m：最近 2 根已完成 Bar
- 当前状态：KNOWN_GAP（运行时仍为 3600，待 Phase B 修复，见 ALIGN-016）
