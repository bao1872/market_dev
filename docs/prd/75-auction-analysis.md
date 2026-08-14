# 竞价分析 PRD（Auction PRD）V2.0 — Overnight Repricing Observation

状态：已确认
最后更新：2026-08-14
对应 Map：`../maps/75-auction-analysis.md`
条款前缀：`AU`
需求所有权：Auction（9:25 竞价重新定价观测）的目标行为、事实定义、分析定义与边界约束

> 本文件是 Auction 的唯一需求真源。它回答：隔夜之后，9:25 当前哪里异常、昨日状态如何被重新定价、注意力重心如何变化。
> [`31-after-close-product-closure-v2.1.md`](./31-after-close-product-closure-v2.1.md) 只定义跨域依赖与 lineage 的基本要求，不替代本文件的分析合同。
> 旧 AuctionAnchor 产品（structure/chip anchor 模型）不再是本文件 active 目标合同，见 [§23 Legacy AuctionAnchor Deprecation](#23-legacy-auctionanchor-deprecation--migration-gap)。

## 0. 定位与架构前提

### 0.1 产品定位

Auction = **Overnight Repricing Observation（隔夜重新定价观测）**。

它不是一个单一 overnight delta，而是由三部分组成的完整分析：

1. **静态横截面**（Cross-sectional State）—— 今日 9:25 当前谁比谁更异常；
2. **个股 / Scope 状态迁移**（State Transition）—— 昨日 Review 状态如何被今日 Auction 重新定价；
3. **市场注意力重心变化**（Attention Redistribution）—— 异常成交参与与成交贡献的重心在哪里扩张 / 收缩。

底层事实只依赖：

- 竞价价格 / Gap；
- 竞价成交额 / Amount；

及其历史异常度。

### 0.2 在业务链中的位置

```text
First Pyramid（市场发生了什么）
→ Review(t-1)（昨日形成了什么市场状态）
→ Auction(t)（9:25 当前哪里异常、昨日状态如何被重新定价、注意力重心如何变化）
→ Open Verification（未来阶段，非本轮 P0）
```

- Auction 消费昨日 Review 的正式 snapshot / canonical evidence，见 [§14](#14-review--auction-依赖边界)。
- Open Verification 不是本轮 P0 实现内容。

### 0.3 回答的核心问题

- **A. 今天 9:25 当前哪里最异常？**
- **B. 相比昨日 Review，哪些个股和 Scope 新增、延续、衰减、反向或出现高参与但方向不明确？**
- **C. 市场异常定价和异常成交的重心移动到了哪里？**

## 1. 产品目标与边界

### 1.1 目标

- 记录每只股票 / 每个 Scope 的 9:25 竞价价格（Gap）与成交额（Amount）及其历史异常度；
- 在 Stock / Market / Style / Industry / Concept 之间做静态横截面对比；
- 识别个股与 Scope 从昨日 Review 到今日 Auction 的状态迁移（NEW / PERSIST / DECAY / REVERSE / CONFLICT / QUIET）；
- 描述市场注意力重心在 Scope 之间的再分布。

### 1.2 非目标

Auction 是 observation，不是：

- 机会 / 风险 / 买卖建议；
- 预测（涨停概率、胜率、收益）；
- 综合机会评分；
- 自动交易 / HFT。

完整 P0 / Non-goals 见 [§19](#19-p0--non-goals)。

## 2. 数据时点与交易日身份

### AU-03 数据时点

- Auction 是**次日 9:25 集合竞价**产品。
- 分析对象是 `trade_date = T` 日的 9:25 竞价快照（`auction_price`、`auction_amount`）。
- 昨日 Review 是 `trade_date = T-1` 的正式发布状态。

### AU-03-1 交易日身份与口径

- 交易日身份、复权口径与价格调整版本必须与既有行情合同一致（`price_adjustment_version`）。
- `auction_price` 与 `previous_close` 必须处于同一复权口径，否则 `gap_pct` 不可用。
- 禁止把连续交易时段的日内数据混入 9:25 竞价事实。

## 3. Stock Auction Fact（AU-04）

每只股票在 `T` 日 9:25 至少定义以下事实。

### AU-04-1 price 事实

- `auction_price`：9:25 集合竞价最终价格；
- `previous_close`：昨收（与 `auction_price` 同复权口径）；
- `gap_pct`：

```text
gap_pct = (auction_price / previous_close) - 1
```

### AU-04-2 amount 事实

- `auction_amount`：9:25 集合竞价成交额。

## 4. Historical Abnormality（AU-05 / AU-06）

### AU-05 Gap Historical Abnormality

基于**个股自身历史有效 Auction Gap 分布**计算。

- `gap_percentile`：当日 `gap_pct` 在个股自身历史有效 Gap 分布中的百分位；
- 正向异常：`gap_percentile >= positive_gap_threshold`；
- 负向异常：`gap_percentile <= negative_gap_threshold`。

阈值要求：

- **CONFIGURABLE**；
- **VERSIONED**（随 run 记录 `algorithm_version` / `config_hash`）；
- **不得写成不可变业务真理**；
- P0 默认候选（仅候选，须用真实历史数据校准）：`positive_gap_threshold = 90`，`negative_gap_threshold = 10`。
  **REQUIRES HISTORICAL CALIBRATION**。

### AU-06 Amount Historical Abnormality

- `amount_multiple`：

```text
amount_multiple = auction_amount / historical_median_auction_amount
```

- `amount_percentile`：当日 `auction_amount` 在个股自身历史有效 Auction Amount 分布中的百分位；
- 成交异常：`amount_percentile >= amount_abnormal_threshold`。

阈值同样要求 CONFIGURABLE / VERSIONED。P0 候选：`amount_abnormal_threshold = 90`，**REQUIRES HISTORICAL CALIBRATION**。

### AU-06-1 Amount 异常的核心语义

`auction amount abnormality = participation / attention intensity`（参与 / 注意力强度）。

它**不是**：

- 主力资金；
- 买盘强度；
- 净流入；
- 看多程度。

**成交额本身没有方向。** 任何把成交额直接解释为资金方向或看多程度的叙述，都不属于本轮 Auction 合同。

### AU-06-2 最小有效成交门

必须设置最小有效成交门（`minimum_auction_amount`），避免极小基数导致虚假高倍数。具体金额阈值本轮不拍脑袋固定，标记为 **calibration / config contract**（OPEN / CALIBRATION_REQUIRED）。

## 5. Price × Amount 二维解释（AU-07）

必须保留**方向（repricing）** 与**参与强度（amount abnormality）** 两个正交维度，禁止在 P0 将其压成单一 Auction Score。

| Price repricing | Amount abnormality | 语义 |
|---|---|---|
| 大 | 高 | **高参与的显著重新定价** |
| 大 | 低 | 价格变化明显，但异常参与有限 |
| 小 | 高 | 高参与，但方向一致性不足 |
| 小 | 低 | 无明显新增 Auction 信息 |

## 6. 静态横截面（AU-08）

Auction 必须独立支持今日 9:25 横截面分析，回答"今天现在谁比谁更异常？"。

必须同时保留**两个参照系**：

- **A. historical abnormality**：今天相对对象自身历史有多异常；
- **B. cross-sectional position**：今天相对其他 Stock / Scope 有多突出。

二者**不得混成同一概念**。

静态横截面至少覆盖：

- Stock
- Market
- Style
- Industry
- Concept

**Market / Style / Industry / Concept 必须保持平行 Scope**（参考 `70-review.md` §7.8 Scope Family：`market / major_index / style / industry_l1 / industry_l2 / industry_l3 / concept`）。

**不得建立 `Industry → Concept → Stock` 这种强包含业务树作为 Auction 的分析前提。**

## 7. Stock State Transition（AU-09）

Auction 必须能比较 `Review(t-1) → Auction(t)`。

- 比较的是"各自窗口标准化后的业务状态"，**不是**把昨日全天成交额与今日竞价成交额做绝对金额比较。
- 例：`review amount abnormality` 与 `auction amount abnormality` 分别基于自己的历史分布标准化后比较。

P0 状态迁移语言与业务语义：

| 状态 | 语义 |
|---|---|
| `NEW` | 昨日无显著状态，今日 Auction 出现显著异常 |
| `PERSIST` | 昨日高关注 / 显著状态，今日 Auction 仍维持同方向或相容的显著状态 |
| `DECAY` | 昨日显著状态，今日 Auction 的异常参与或方向显著减弱 |
| `REVERSE` | 昨日存在明确方向，今日出现高参与的显著反向重新定价 |
| `CONFLICT` | 参与异常仍高，但价格方向弱、混合或与昨日状态不形成简单延续 / 反转 |
| `QUIET` | 当前 Level 和 State Change 均无显著新增信息 |

这些是 **observation state**，不是机会 / 风险 / 买卖建议。本轮不凭空锁死全部数值阈值（见 [§15](#15-算法--配置版本化au-17)）。

## 8. Scope Model（AU-10）

Scope 必须支持：

- Market
- Style
- Industry
- Concept

每个 Scope P0 至少有 §9–§12 定义的事实。Scope membership 复用既有已冻结的平行 Scope Family 与 membership 体系（`board_facts` / Review Scope Observation）。若现有 membership / amount 口径不足以支撑 Auction 所需分母，标记 **IMPLEMENTATION ALIGNMENT GAP**（见 [§14](#14-review--auction-依赖边界)），本轮不改动既有 ownership。

## 9. Scope Breadth Metrics（AU-11）

每个 Scope P0 至少有（分母均为 `valid member count`，见 [§16](#16-数据质量--valid-member-denominatorau-18)）：

1. **正向异常 Breadth**

```text
PositiveAbnormalBreadth = 正向 Gap 异常成员数 / valid member count
```

2. **负向异常 Breadth**

```text
NegativeAbnormalBreadth = 负向 Gap 异常成员数 / valid member count
```

3. **成交异常 Breadth**

```text
AmountAbnormalBreadth = 成交异常成员数 / valid member count
```

4. **正向联合异常 Breadth**

```text
PositiveJointAbnormalBreadth =
  同时满足「正向 Gap 异常 AND 成交异常」的成员数 / valid member count
```

5. **负向联合异常 Breadth**

```text
NegativeJointAbnormalBreadth =
  同时满足「负向 Gap 异常 AND 成交异常」的成员数 / valid member count
```

## 10. Auction Amount Contribution / Concentration（AU-12）

6. **竞价成交贡献**

```text
AuctionAmountContribution =
  Scope 成员 Auction Amount 总和 / Market valid Auction Amount 总和
```

7. **Concentration**（P0 至少保留）

```text
Top1 amount contribution
Top3 amount contribution
```

用于区分"少数大票贡献高成交"与"Scope 成员广泛参与"。

### AU-12-1 Breadth / Contribution / Concentration 必须分离

- **Breadth**：回答"有多少成员同时发生？"
- **Amount Contribution**：回答"全市场有多少竞价成交发生在这个 Scope 成员中？"
- **Concentration**：回答"Scope 内成交是否由少数成员主导？"

三者**不能互相替代**。

## 11. Concept Overlap Semantics（AU-13）

Concept 是 **overlapping membership**：同一股票可以属于多个 Concept。

因此：

- Concept Auction Amount Contribution 表示"**该 Concept 成员对应的全市场竞价成交贡献**"；
- 不同 Concept contribution **不能直接相加解释为市场资金分配**；
- **不能解释成互斥份额**；
- **总和允许 >100%**。

## 12. Scope State Transition（AU-14）

Scope 同样需要 `Review(t-1) → Auction(t)` 迁移识别（`NEW / PERSIST / DECAY / REVERSE / CONFLICT / QUIET`，语义同 [§7](#7-stock-state-transitionau-09)）。

但**不得简单"统计成员标签数量"代替 Scope 事实**。Scope transition 应基于 Scope 自身：

- directional breadth；
- amount abnormal breadth；
- joint breadth；
- amount contribution；
- concentration；
- 昨日 Review state；

综合形成可解释状态。**P0 不做综合分数。**

## 13. Attention Redistribution（AU-15）

必须与 state transition 分开。

- **State Transition**：同一个 Stock / Scope 昨天 → 今天发生什么变化；
- **Attention Redistribution**：当前不同 Scope 之间，异常成交参与和成交贡献重心在哪里扩张 / 收缩。

允许描述："竞价注意力重心向机器人扩张"。
**禁止描述**："资金净流入机器人"——除非未来存在真正净流数据合同。

## 14. Review → Auction 依赖边界（AU-16）

新的 Auction 会读取昨日 Review 的**正式 snapshot / canonical evidence**。

但是：

- Auction **不允许调用 Review 内部私有计算逻辑**形成强耦合；
- 目标关系：

```text
Review publication / canonical snapshot
        ↓
Auction transition layer
```

而不是：

```text
Auction → 调用 Review 私有 calculator → 重跑 Review
```

- 本轮 PRD 只定义业务依赖，**不提前规定不存在的类名**；
- 如果当前 Review contract 尚未提供 Auction 所需的正式字段，标记 **IMPLEMENTATION ALIGNMENT GAP**；
- **本轮不得为了 Auction 去改 Review 代码。**

## 15. 算法 / 配置版本化（AU-17）

- 所有阈值（gap 阈值、amount 阈值、窗口、样本门限、Scope 最小成员数）**CONFIGURABLE + VERSIONED**；
- 版本与配置摘要必须随 run 记录（`algorithm_version` / `config_hash` / 阈值快照）；
- 禁止把任何阈值写成不可变业务真理。

## 16. 数据质量 / valid member denominator（AU-18）

- `valid member` 定义：该成员具备有效 `auction_price` + `auction_amount`，且满足最低历史样本门限；
- 剔除：停牌、缺失、异常、不满足复权口径一致性的记录；
- 最小有效样本 / 最小成交门 / Scope 最小有效成员数均属 **calibration / config contract**（本轮 OPEN / CALIBRATION_REQUIRED，不伪造最终值）。

## 17. Publication / lineage 基本要求（AU-19）

- Auction 观测结果必须通过正式 pointer / read model 提供，禁止消费者直接读取"最新 succeeded run"；
- 幂等、版本化、可追溯；修正创建新 run 并 supersede，published run 不原地修改；
- **point-in-time**：禁止 future leakage；不得用今日快照回填历史；
- 本轮只定义业务结果合同；具体 persistence 表 / API / 前端实现由后续 Code Alignment Round 决定。

## 18. API / frontend 目标合同（AU-20）

本轮只定义业务结果，**不实现**：

- 市场 / 风格 / 行业 / 概念横截面；
- 个股 Auction fact 与历史异常；
- 个股 / Scope 状态迁移；
- Scope breadth / joint breadth / amount contribution / concentration；
- Attention Redistribution；
- Review → Auction 证据联动。

现有 `/auction` 三级页面与 `AuctionBackflowPanel` 描述的是旧 AuctionAnchor 产品实现，属 [§23](#23-legacy-auctionanchor-deprecation--migration-gap) 的 deprecated implementation gap，不在本 P0 目标合同内。

## 19. P0 / Non-goals（AU-21）

### P0 INCLUDE

- Auction raw fact
- Gap
- Gap historical abnormality
- Auction amount
- Amount historical abnormality
- Stock static cross-section
- Scope static cross-section
- Stock transition
- Scope transition
- Attention redistribution
- Positive / Negative abnormal breadth
- Amount abnormal breadth
- Positive / Negative joint breadth
- Auction amount contribution
- concentration
- Review → Auction evidence linkage

### P0 EXCLUDE

- Structural relocation
- DSA / SMC / Chip Auction interpretation
- 撤单博弈
- 9:15–9:20 超短行为推演
- 主力资金叙事
- 资金净流入推断
- 买点
- 收益预测
- 涨停概率
- 胜率模型
- 综合机会评分
- 自动交易
- HFT

## 20. Acceptance Matrix（AU-22）

| 验收项 | 业务结果要求 | 状态 |
|---|---|---|
| Stock Auction Fact | `auction_price` / `previous_close` / `gap_pct` / `auction_amount` 定义完整且口径一致 | 合同已冻结 |
| Gap Historical Abnormality | `gap_percentile` + 正 / 负向异常判定，阈值 configurable/versioned | 合同已冻结，阈值待校准 |
| Amount Historical Abnormality | `amount_multiple` / `amount_percentile` / 成交异常判定，含最小成交门 | 合同已冻结，阈值待校准 |
| Price × Amount 二维 | 保留方向与参与强度两个正交维度，禁止单分 | 合同已冻结 |
| Static Cross-Section | Stock / Market / Style / Industry / Concept 平行覆盖，双参照系分离 | 合同已冻结 |
| Stock State Transition | Review(t-1) → Auction(t) 六态迁移，标准化后比较 | 合同已冻结 |
| Scope Breadth | 5 项 breadth（含 joint）分母 = valid member count | 合同已冻结 |
| Amount Contribution / Concentration | `AuctionAmountContribution` + Top1 / Top3 | 合同已冻结 |
| Concept Overlap | overlapping membership 语义，贡献不互斥、允许 >100% | 合同已冻结 |
| Scope State Transition | 基于 Scope 自身事实，非成员标签计数，P0 无综合分 | 合同已冻结 |
| Attention Redistribution | 与 state transition 分离，禁止资金净流叙述 | 合同已冻结 |
| Review → Auction | 只读正式 snapshot，禁调私有 calculator；缺失字段登记 GAP | 合同已冻结 |
| 版本化 | 阈值 / 配置 versioned 且随 run 记录 | 合同已冻结 |
| 数据质量 | valid member 分母与最小门限为 calibration contract | 合同已冻结 |
| Publication / lineage | 正式 pointer / PIT / 幂等 / supersede | 合同已冻结 |

## 21. 阈值合同（Calibration）

本轮**不伪造最终阈值**。P0 calibration candidate（统一标注 **CONFIGURABLE / VERSIONED / REQUIRES HISTORICAL CALIBRATION**）：

| 项 | P0 候选 | 状态 |
|---|---|---|
| history window | 120 valid trading days | CALIBRATION_REQUIRED |
| positive gap percentile | 90 | CALIBRATION_REQUIRED |
| negative gap percentile | 10 | CALIBRATION_REQUIRED |
| amount abnormal percentile | 90 | CALIBRATION_REQUIRED |
| minimum history count | — | OPEN / CALIBRATION_REQUIRED |
| minimum auction amount | — | OPEN / CALIBRATION_REQUIRED |
| scope minimum valid member count | — | OPEN / CALIBRATION_REQUIRED |

当前没有数据实验证据时，一律保持 **OPEN / CALIBRATION_REQUIRED**，不得凭感觉锁死。

## 22. 明确排除（Structural Relocation 移除）

### AU-02-1 Auction P0 不使用

- DSA VWAP
- SMC
- Chip position / chip consensus
- Bollinger
- 中周期趋势 / 结构
- "突破结构位"
- "结构锚点"
- chip composite anchor

**理由**：9:25 是集合竞价价格点，不应承担连续交易中的中周期结构判断。

### AU-02-2 旧语义废止

旧 Auction 的 `structure_only` / `hybrid` / `composite` 语义若来自 structure / chip anchor，应在新正式业务合同中废止，见 [§23](#23-legacy-auctionanchor-deprecation--migration-gap)。

**注意**：本 PRD 废止该产品语义**不等于本轮删除代码**；代码处置留给后续 Code Alignment Round。

## 23. Legacy AuctionAnchor Deprecation / Migration Gap（AU-23）

> **DEPRECATED PRODUCT CONTRACT**

以下内容是旧 Auction Anchor 产品语义，**不再属于当前 Auction P0 目标合同**：

- 盘后生成的 structure / chip 竞价锚点（`AuctionAnchorRun` / `auction_anchor_snapshots` / `auction_anchor_items` / `auction_anchor_publications`）；
- `structure_only` / `hybrid` / `composite` 批次发布模式与 `auction_mode_service` 决策；
- 基于锚点的位置迁移（`structure_position` / `chip_position`）、7-state 事件生命周期（`formed → confirmed → continued/weakened → failed/transformed/expired`）与 `auction_event_trackings`；
- 盘后 after-close orchestrator 中 `auction_anchor` 节点与 chip 晚到升级回调；
- 现有 `/auction` 三级页面与 Review "竞价回流"（`AuctionBackflowPanel`）的旧产品形态。

**代码迁移 / 删除由后续 Code Alignment Round 处理**，本轮仅完成需求事实源对齐。

全项目旧 Auction 引用已同步登记：

| 位置 | 处置 |
|---|---|
| `31-after-close-product-closure-v2.1.md`（§2 九节点 / §5 PC-31 / §6 PC-41） | 标记 DEPRECATED / 从新 P0 业务链移除 |
| `30-after-close.md`（AC-16 顶层步骤 `auction_anchor`） | 标记 DEPRECATED（legacy 盘后节点） |
| `70-review.md`（§27 依赖矩阵 auction 行） | 标记 DEPRECATED（旧竞价回流依赖） |

> 以上 GAP 为**显式登记**的 PRD↔代码暂时不一致，不隐藏；后续 Code Alignment Round 再处置实现。
