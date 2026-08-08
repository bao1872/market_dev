# 10 产品域不变量

本文件只保存长期稳定、直接影响产品含义的业务不变量。它们在 Exploration 与 Hardening 中都生效。

## 1. 产品边界

盘迹是 A 股研究、全市场特征计算、自选股复盘/监控和消息投递工具。

产品输出遵循：

`事实 → 状态 → 变化 → 市场分布 → 用户判断`

不做：

- 自动交易；
- 券商账户连接；
- 资金管理；
- 收益承诺；
- 用单一指标直接替代用户投资判断；
- 普通用户修改正式算法参数；
- 将产品描述为预测系统或“喊单”系统。

## 2. 第一金字塔

第一金字塔的核心顺序：

**Trend > Structure > Momentum > Chip Consensus**

含义：

- Trend：长周期方向与趋势状态，当前主要由 DSA / VWAP 长周期逻辑提供；
- Structure：中周期市场结构，当前主要由 SMC 提供；
- Momentum：动量、挤压/释放、成交量关系等；
- Chip Consensus：短/超短周期筹码共识，作为异步增强。

### 2.1 Core 与 Chip 解耦

- daily core 只依赖 daily 业务输入；
- daily core 不得等待 15m；
- Chip 使用目标交易日收盘后的 15m；
- Chip 失败、partial、skipped 不得反向修改已发布 Core；
- Chip 可以晚到并单独重试。

### 2.2 DSA 不是独立业务主链

- DSA 是第一金字塔 Trend 的组成部分；
- Core 中只计算一次 canonical DSA；
- `dsa_projection` 只允许把同一 Core artifact 投影给兼容消费方；
- 禁止为了兼容接口重复计算第二份 DSA。

## 3. Review

正式 Review 仍是五阶段：

1. 市场扫描；
2. 筛选发现；
3. 板块归因；
4. 个股验证；
5. 追踪复核。

Auction 回流是辅助输入/增强，不是第六阶段。

Review 的 identity、核心过滤和阶段状态不得依赖 Chip 是否完成。

## 4. ProductReadiness 分类

当前产品分类：

### mandatory

- `daily_facts`
- `board_facts`
- `stock_core`
- `board_aggregation`
- `review`

### required compatibility

- `dsa_projection`

### enhancement

- `chip`
- `state_events`
- `auction_anchor`

Chip / Auction / State Events 缺失可以降低 fully-ready，但不得被错误解释为 Core 不可消费。

在 Exploration 中，ProductReadiness 只在当前 hypothesis slice 确实依赖它时成为 blocker。

## 5. 策略与监控

当前正式保留：

- `dsa_selector`
- `watchlist_monitor`

已废弃的多策略组合不得恢复。

有效会员添加自选后自动进入盘中监控；不创建 MonitoringPlan。

到期用户保留历史数据，但不能继续读取受限数据、修改、监控或产生新投递，具体权限以当前 PRD/安全合同为准。

## 6. 盘中监控触发

- 盘中监控只依赖最新已完成 1m bar；
- `source_bar_time` 来自已完成 bar；
- 不得把未完成 1m bar 当作正式触发事实；
- `monitor_batch_service` 的业务计算输入不得因截图需求改口径。

watchlist_monitor 当前只保留两类触发：

- Structure：SMC BOS / CHoCH / EQH / EQL / OB first touch；
- Chip Consensus：node_cluster_touch。

Bollinger 不作为当前盘中监控触发类别。

## 7. 飞书

唯一接入方式：

`feishu_platform_app`

禁止恢复：

- `feishu_webhook` / `FEISHU_WEBHOOK`；
- 独立管理员飞书 App；
- 独立管理员接收人配置。

飞书截图当前固定使用 Structure + Chip Consensus 组合视图；事件文字只描述实际触发事件，图片图层与触发类别解耦。

## 8. Experimental / Validated / Stable / Released

产品或算法假设可以处于：

- **EXPERIMENTAL**：实现与测试可以正确，但产品价值尚未由用户确认；
- **VALIDATED**：用户通过真实结果确认该假设值得继续；
- **STABLE**：多轮结果稳定，接口/语义开始冻结；
- **RELEASED**：进入正式长期兼容和发布治理。

实现成功不得自动把 EXPERIMENTAL 升级为 STABLE/RELEASED。
