# 竞价分析 PRD（Auction PRD）V2.1 — Overnight Repricing Observation

状态：已确认
最后更新：2026-08-15
对应 Map：`../maps/75-auction-analysis.md`
条款前缀：`AU`
需求所有权：Auction（9:25 竞价重新定价观测）的目标行为、事实定义、分析定义与边界约束

> 本文件是 Auction 的唯一需求真源。它回答：隔夜之后，9:25 当前哪里异常、昨日状态如何被重新定价、注意力重心如何变化。
> [`31-after-close-product-closure-v2.1.md`](./31-after-close-product-closure-v2.1.md) 只定义跨域依赖与 lineage 的基本要求，不替代本文件的分析合同。
> 旧 AuctionAnchor 产品（structure/chip anchor 模型）不再是本文件 active 目标合同，见 [§23 Legacy AuctionAnchor Deprecation](#23-legacy-auctionanchor-deprecation--migration-gap)。

### 0.0 正式分析链（Canonical Auction Analysis Pipeline）

Auction 正式分析按以下顺序组织（见 [§10 AU-10](#10-scope-modelau-10) 展开）：

```text
Member Facts
  → L1 Scope Facts
  → L2 Observation Groups
  → Analysis
  → Interpretation / Conclusion
  → Member Attribution
```

- Auction 正式分析输入只包括三类事实：**Gap / Price Repricing**、**Auction Volume**、**Auction Amount**。
- Auction 使用与 Review v2.3 一致的"事实 → 观察 → 分析 → 解释"方法论，**但不继承 Review Dynamics 生命周期语义**（EMA / Velocity / Acceleration / Persistence / 6 阶段生命周期）。
- Industry 与 Concept 为平行 Scope family；Scope family 完整列表见 [§10](#10-scope-modelau-10)。

## 0. 定位与架构前提

### 0.1 产品定位

Auction = **Overnight Repricing Observation（隔夜重新定价观测）**。

它不是一个单一 overnight delta，而是由三部分组成的完整分析：

1. **静态横截面**（Cross-sectional State）—— 今日 9:25 当前谁比谁更异常；
2. **个股 / Scope 状态迁移**（State Transition）—— 昨日 Review 状态如何被今日 Auction 重新定价；
3. **市场注意力重心变化**（Attention Redistribution）—— 异常成交参与与成交贡献的重心在哪里扩张 / 收缩。

底层事实只依赖：

- 竞价价格 / Gap；
- 竞价成交量 / Volume；
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

- 记录每只股票 / 每个 Scope 的 9:25 竞价价格（Gap）、竞价成交量（Volume）与成交额（Amount）及其历史异常度；
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

每只股票在 `T` 日 9:25 至少定义以下事实。Auction canonical member fact 维度为
**Price / Gap + Auction Volume + Auction Amount**，`auction_volume` 与 `auction_price` /
`auction_amount` 并列属于底层原始事实，必须完整进入 canonical Stock Auction Fact。

### AU-04-1 price / gap 事实

- `auction_price`：9:25 集合竞价最终价格；
- `previous_close`：昨收（与 `auction_price` 同复权口径）；
- `gap_pct`：

```text
gap_pct = (auction_price / previous_close) - 1
```

### AU-04-2 participation 事实（Volume / Amount）

- `auction_volume`：9:25 集合竞价成交量（参与 / 关注度强度；不代表资金方向，详见 AU-06-1）；
- `auction_amount`：9:25 集合竞价成交额（参与 / 关注度强度；不代表资金方向，详见 AU-06-1）。

> AU-04-3（本轮冻结边界）：本轮只冻结 `auction_volume` 是 canonical Auction member fact。
> provider-specific volume unit、腾讯 / 新浪 normalization rule、lot/share 换算、具体采集实现
> 属于后续 Data Contract / Architecture 阶段，不在本轮定义。

## 4. Historical Abnormality（AU-05 / AU-06）

### AU-05 Gap Historical Abnormality（member-first）

每个 member 必须先基于**自身历史有效 Auction Gap 序列**计算：

- `gap_percentile`：当日 `gap_pct` 在个股自身历史有效 Gap 分布中的百分位；
- 基于 `gap_percentile` 的 gap historical abnormality（正向 / 负向）；

然后 Scope 层再聚合 member abnormality distribution（见 [§11 AU-11](#11-scope-breadth-metricsau-11) / [§12 AU-12](#12-auction-amount-contribution--concentrationau-12)）。

**禁止用 Scope today total / Scope historical total 替代 member-level historical abnormality。**

允许另外计算 Scope 自身 historical position（Scope 今天相对 Scope 自身历史的分布位置），但必须作为**独立事实**，不得与 member-first abnormality 混为一个指标。

阈值要求：

- **CONFIGURABLE**；
- **VERSIONED**（随 run 记录 `algorithm_version` / `config_hash`）；
- **不得写成不可变业务真理**；
- 本轮不锁死具体百分位与窗口数值，**REQUIRES HISTORICAL CALIBRATION**。

### AU-06 Amount Historical Abnormality（member-first）

每个 member 必须先基于**自身历史有效 Auction Amount / Volume 序列**计算：

- `amount_multiple`：

```text
amount_multiple = auction_amount / historical_median_auction_amount
```

- `amount_percentile`：当日 `auction_amount` 在个股自身历史有效 Auction Amount 分布中的百分位；
- 成交异常：`amount_percentile >= amount_abnormal_threshold`；
- `volume_multiple` / `volume_percentile`：类比 Volume 历史异常度（member-first）。

然后 Scope 层聚合 member abnormality distribution。

**禁止用 Scope today total / Scope historical total 替代 member-level historical abnormality。**

允许另外计算 Scope 自身 historical position，但必须作为**独立事实**，不与 member-first abnormality 混为一个指标。

阈值同样要求 CONFIGURABLE / VERSIONED。本轮不锁死具体百分位与窗口数值，**REQUIRES HISTORICAL CALIBRATION**。

### AU-06-1 Amount / Volume 异常的核心语义（directionless）

`auction amount abnormality` / `auction volume abnormality` = **participation / attention intensity**（参与 / 注意力强度）。

它**没有方向**。它**不是**：

- 净 inflow；
- 买盘强度；
- 主力资金；
- 看多资金流；
- 资金方向。

**成交额 / 成交量本身没有方向。** 任何把成交额 / 成交量直接解释为资金方向或看多程度的叙述，都不属于本轮 Auction 合同。

- **Price 提供方向**（Gap 正负）；
- **Amount / Volume 提供参与强度**（abnormality 高低）。

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

### AU-10-0 Canonical Auction Analysis Pipeline

Auction 正式分析按以下顺序组织（与 [§0.0](#00-正式分析链canonical-auction-analysis-pipeline) 一致）：

```text
Member Facts
  → L1 Scope Facts
  → L2 Observation Groups
  → Analysis
  → Interpretation / Conclusion
  → Member Attribution
```

- **Member Facts**：每只股票 9:25 的 Gap / Auction Volume / Auction Amount 及其历史异常度（见 §3 / §4）。
- **L1 Scope Facts**：全市场 / 风格 / 行业的 Scope 级 breadth / participation / concentration（见 §9–§12）。
- **L2 Observation Groups**：Industry / Concept 平行 Scope family 的观察分组（见 §13 / §15）。
- **Analysis**：Historical Position / Cross-sectional Position / Internal Structure / Overnight Transition 四类业务问题（见 §15）。
- **Interpretation / Conclusion**：由可解释事实组合形成的 09:25 Auction state 描述（见 §19）。
- **Member Attribution**：任何 Scope 级结论必须能回溯到 member evidence（见 §19 / §15）。

### AU-10-1 正式分析输入

Auction 正式分析输入只包括：

1. **Gap / Price Repricing**（方向）；
2. **Auction Volume**（参与强度）；
3. **Auction Amount**（参与强度）。

Auction 使用与 Review v2.3 一致的"事实 → 观察 → 分析 → 解释"方法论，但**不继承 Review Dynamics 生命周期语义**（EMA / Velocity / Acceleration / Persistence / 6 阶段生命周期）。

### AU-10-2 Scope Family（平行）

Industry 与 Concept 为平行 Scope family。完整 Scope family 列表：

- `market`
- `major_index`
- `style`
- `industry_l1`
- `industry_l2`
- `industry_l3`
- `concept`

每个 Scope P0 至少有 §9–§12 定义的事实。Scope membership 复用既有已冻结的平行 Scope Family 与 membership 体系（`board_facts` / Review Scope Observation）。若现有 membership / amount 口径不足以支撑 Auction 所需分母，标记 **IMPLEMENTATION ALIGNMENT GAP**（见 [§14](#14-review--auction-依赖边界)），本轮不改动既有 ownership。

## 9. Scope Breadth Metrics（AU-11 / L1）

每个 Scope P0 至少支持以下 L1 事实。**每个 metric 的分母（denominator）按自身所需事实与历史 readiness 定义独立的 eligible member set，不共用一个 global valid member count**（见 [§16](#16-数据质量--metric-eligibility--denominatorau-18)）。下文分母标注为对应的 metric-specific eligibility：

### PRICE（方向由 Gap 提供）

1. **equal-weight Gap**：Scope 成员 `gap_pct` 等权均值（eligible = current Gap eligible members）；
2. **amount-weighted Gap**：Scope 成员 `gap_pct` 按 `auction_amount` 加权均值（eligible = current Gap eligible members，权重用当前 valid amount）；
3. **positive gap breadth**：正向 Gap 成员数 / **current Gap eligible members**；
4. **negative gap breadth**：负向 Gap 成员数 / **current Gap eligible members**；
5. **positive gap abnormal breadth**：正向 Gap 异常（`gap_percentile >= positive_gap_threshold`）成员数 / **Gap-history eligible members**；
6. **negative gap abnormal breadth**：负向 Gap 异常（`gap_percentile <= negative_gap_threshold`）成员数 / **Gap-history eligible members**。

### PARTICIPATION（强度，无方向）

7. **total auction volume**：Scope 成员 `auction_volume` 合计（eligible = current Volume eligible members）；
8. **total auction amount**：Scope 成员 `auction_amount` 合计（eligible = current Amount eligible members）；
9. **volume abnormal breadth**：Volume 异常（`volume_percentile >= volume_abnormal_threshold`）成员数 / **Volume-history eligible members**；
10. **amount abnormal breadth**：成交异常（`amount_percentile >= amount_abnormal_threshold`）成员数 / **Amount-history eligible members**；
11. **auction amount market contribution**：Scope `total auction amount` / Market `total auction amount`（各自使用 current Amount eligible members）。

### JOINT（方向 × 强度）

12. **positive price + participation abnormal breadth**：同时满足「正向 Gap 异常 AND 成交异常」成员数 / **joint eligible members**（= Gap-history eligible ∩ Amount-history eligible）；
13. **negative price + participation abnormal breadth**：同时满足「负向 Gap 异常 AND 成交异常」成员数 / **joint eligible members**（= Gap-history eligible ∩ Amount-history eligible）。

> AU-11-1：上述 eligible member set 名称是业务 eligibility contract，不是实现 schema 字段名。
> 各 eligibility 的精确条件见 [§16](#16-数据质量--metric-eligibility--denominatorau-18)。
> 不得因为某成员 historical sample 不足，就从 current amount / volume total 中删除一只今天数据有效的股票。

### 硬规则

- **Auction Amount / Volume abnormality = participation / attention intensity（参与 / 注意力强度），它没有方向。**
- **禁止将 Amount / Volume abnormality 解释为**：net inflow、buy pressure、main-force capital、bullish capital flow。
- **Price 提供方向；Amount / Volume 提供参与强度。** 二者为正交维度，不得合并为单一方向性资金指标（见 [§5 AU-07](#5-price--amount-二维解释au-07) / [§4 AU-06-1](#au-06-1-amount--volume-异常的核心语义directionless)）。

## 10. Auction Amount Contribution / Concentration（AU-12）

### AU-12-0 三者是不同的业务问题

- **Breadth**：回答"有多少成员同时参与？"（见 [§9](#9-scope-breadth-metricsau-11--l1)）；
- **Contribution**：回答"哪些成员贡献了 Scope 的竞价金额 / 重新定价？"；
- **Concentration**：回答"参与是否集中在少数成员？"

三者**不能互相替代**。

### AU-12-1 Contribution

```text
AuctionAmountContribution =
  Scope 成员 Auction Amount 总和（current Amount eligible members）/ Market Auction Amount 总和（current Amount eligible members）
```

### AU-12-2 Concentration（正式 facts）

P0 正式 concentration facts 至少包括：

```text
Top1 amount share   = 单一成员 Auction Amount / Scope Auction Amount 总和
Top3 amount share   = Top3 成员 Auction Amount 合计 / Scope Auction Amount 总和
raw HHI             = Σ (member_amount_share)^2           （member_amount_share = 成员 amount / Scope amount 总和）
normalized HHI      = (raw_hhi - 1/N) / (1 - 1/N)         （N = 用于该 Scope amount-share distribution 的 eligible members 数量；N<=1 时 unavailable）
```

> 以上公式仅作事实定义 owner 标注；公式数值实现与架构（如物理 persistence 位置）留到 Architecture Phase，本轮不实现、不锁死额外统计公式。

用于区分"少数大票贡献高成交"与"Scope 成员广泛参与"。

## 11. Concept Overlap Semantics（AU-13）

Concept 是 **overlapping membership**：同一股票可以属于多个 Concept。

因此：

- Concept Auction Amount Contribution 表示"**该 Concept 成员对应的全市场竞价成交贡献**"；
- 不同 Concept contribution **不能直接相加解释为市场资金分配**；
- **不能解释成互斥份额**；
- **总和允许 >100%**（Concept amount contribution 不是互斥 market-share accounting）。

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

## 13. L2 Observation Groups + Analysis（AU-15）

### AU-15-1 L2 Observation Groups

Industry 与 Concept 为平行 Scope family（见 [§10-2](#au-10-2-scope-family平行)）。L2 观察至少覆盖：

- `industry_l1` / `industry_l2` / `industry_l3`
- `concept`

每个 L2 Scope 复用 §9–§12 的 L1 事实定义（breadth / participation / contribution / concentration）。

### AU-15-2 Analysis：四个业务问题

L2 / Analysis 至少组织为以下四个业务问题：

#### 1. Historical Position（历史位置）

今天相对**自身历史**处于什么位置：

- 基于 member-first 历史异常度（见 [§4](#4-historical-abnormalityau-05--au-06)）；
- Scope 自身 historical position 作为独立事实（不与 member-first abnormality 混用）。

#### 2. Cross-sectional Position（横截面位置）

今天相对 **same-family Scope** 处于什么位置：

- **必须 same-family**：`industry ↔ industry`、`concept ↔ concept`；
- **禁止把 Industry 与 Concept 混成同一个排名池**。

#### 3. Internal Structure（内部结构）

使用下列事实判断参与是**扩散还是集中**：

- breadth（多少成员参与）；
- contribution（哪些成员贡献）；
- concentration（Top1/3 / HHI / normalized HHI）；
- EW vs AW Gap（equal-weight vs amount-weighted，方向一致性）。

#### 4. Overnight Transition（隔夜迁移）

允许比较：

```text
Review(t-1) → Auction(t)
```

描述**隔夜重新定价变化**（昨日 Review state 如何被今日 Auction 重新定价）。

**禁止将该比较扩展为**：

- EMA；
- Velocity；
- Acceleration；
- Persistence；
- Review 6 Dynamics Phase。

Auction **不跟踪 6 阶段生命周期**。

### AU-15-3 Attention Redistribution（与 state transition 分离）

- **State Transition**：同一个 Stock / Scope 昨天 → 今天发生什么变化；
- **Attention Redistribution**：当前不同 Scope 之间，异常成交参与和成交贡献重心在哪里扩张 / 收缩。

允许描述："竞价注意力重心向机器人扩张"。
**禁止描述**："资金净流入机器人"——除非未来存在真正净流数据合同（见 [§4 AU-06-1](#au-06-1-amount--volume-异常的核心语义directionless)）。

### AU-15-4 Member Attribution（结论回溯）

任何 Scope-level interpretation / conclusion 必须能回溯到 member evidence。至少支持：

- top positive gap contributors；
- top negative gap contributors；
- top auction amount contributors；
- top positive joint-abnormal members；
- top negative joint-abnormal members；
- top amount-abnormal members。

member evidence 至少能够展示：

- `symbol`；
- `gap`；
- historical position / abnormality（where available）；
- `auction_amount`；
- `auction_volume`；
- contribution / share（where applicable）。

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

## 16. 数据质量 / Metric Eligibility / Denominator（AU-18）

**核心原则：不再使用一个 global valid member count 作为全部 Scope metric 的统一分母。**
每个 metric 根据自身所需事实与历史 readiness 定义**自己的 eligible member set**。
`current fact eligibility`（今天数据是否有效）与 `historical abnormality eligibility`
（是否有足够历史样本计算异常度）必须**分离**：`historical-not-ready ≠ current-invalid`。

### AU-18-1 各 metric-specific eligibility 合同

| Eligibility set | 包含条件 | 供哪些 metric 作 denominator |
|---|---|---|
| **current Gap eligible members** | 当前 `auction_price` valid + `previous_close` valid + 复权口径一致 + `gap_pct` valid | equal-weight Gap、amount-weighted Gap、positive/negative gap breadth（§9 项 1–4） |
| **Gap-history eligible members** | current Gap eligible + 足够 valid Gap history | positive/negative gap abnormal breadth（§9 项 5–6） |
| **current Volume eligible members** | 当前 `auction_volume` valid | total auction volume（§9 项 7） |
| **Volume-history eligible members** | current Volume eligible + 足够 valid Volume history | volume abnormal breadth（§9 项 9） |
| **current Amount eligible members** | 当前 `auction_amount` valid | total auction amount、amount-weighted Gap 权重、Auction Amount Contribution（§9 项 2/8/11） |
| **Amount-history eligible members** | current Amount eligible + 足够 valid Amount history（+ 该 metric 要求的最小 amount gate，如适用） | amount abnormal breadth（§9 项 10） |
| **joint eligible members** | Gap-history eligible ∩ Amount-history eligible | positive/negative price + participation abnormal breadth（§9 项 12–13） |

### AU-18-2 Concentration 的 eligible members

Top1 / Top3 / raw HHI / normalized HHI 只基于**真正进入 amount-share vector 的成员**。
若某成员没有有效 `auction_amount`，不得进入 share vector；
normalized HHI 中的 `N` = 用于该 Scope amount-share distribution 的 eligible members 数量（非笼统 global valid member count）。

### AU-18-3 排除规则（missing ≠ zero，invalid ≠ zero）

- `missing` ≠ `zero`；`invalid` ≠ `zero`；`historical-not-ready` ≠ `current-invalid`；
- 停牌、复权不一致、数据异常等，**只在它们真正影响对应 metric 时**才从该 metric 的 eligible set 中排除；
- 不得因为某成员 historical sample 不足，就从 current amount / volume total 中删除一只今天数据有效的股票；
- 各 metric 的 numerator / denominator / eligibility 必须可追溯到下表语义（具体字段结构与 persistence 留 Architecture Phase 决定）：

```text
每个 breadth / ratio 必须能追溯其：
  - numerator semantic（例如：满足 XX 条件的成员数）
  - denominator semantic（例如：XX-history eligible members）
  - eligibility condition（例如：current valid + sufficient history）
```

### AU-18-4 通用数据质量边界

- 剔除：停牌、缺失、异常、不满足复权口径一致性的记录（仅当影响对应 metric 时）；
- 最小有效样本 / 最小成交门 / Scope 最小有效成员数均属 **calibration / config contract**（本轮 OPEN / CALIBRATION_REQUIRED，不伪造最终值）。

## 17. Publication / lineage 基本要求（AU-19）

- Auction 观测结果必须通过正式 pointer / read model 提供，禁止消费者直接读取"最新 succeeded run"；
- 幂等、版本化、可追溯；修正创建新 run 并 supersede，published run 不原地修改；
- **point-in-time**：禁止 future leakage；不得用今日快照回填历史；
- 本轮只定义业务结果合同；具体 persistence 表 / API / 前端实现由后续 Code Alignment Round 决定。

### AU-19-1 Interpretation / Conclusion Contract

**不冻结固定状态枚举。** `Strong Expansion` / `Selective Concentration` / `Weakness` / `Neutral` 或任何新的固定状态枚举需要 **Historical Validation 后再决定**（见 [§21 阈值合同](#21-阈值合同calibration)）。当前 PRD 只冻结 Conclusion 的**组成合同**：

Conclusion 必须由可解释事实组合形成，至少覆盖：

- **price repricing direction**（Price 方向）；
- **participation breadth**（参与广度）；
- **participation intensity**（参与强度，Amount / Volume abnormality）；
- **concentration**（集中度）；
- **leading scope / member attribution**（主导 Scope / member 证据，见 [§15-4](#au-15-4-member-attribution结论回溯)）。

Conclusion 描述**当前 09:25 Auction state**，而不是未来预测。

**禁止**：

- intraday prediction（盘中预测）；
- return prediction（收益预测）；
- limit-up probability（涨停概率）；
- buy / sell recommendation（买卖建议）；
- single composite Auction opportunity score（单一综合竞价机会评分）。

> 结论语言（如候选的 Strong Expansion 等）的具体语义与阈值，待 Historical Validation 阶段以 CONFIGURABLE / VERSIONED / CALIBRATION_REQUIRED 方式确定，本轮不锁死。

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

- Auction raw fact（含 auction_price / previous_close / gap_pct / auction_volume / auction_amount）
- Gap
- Gap historical abnormality
- Auction volume
- Volume historical abnormality
- Auction amount
- Amount historical abnormality
- Stock static cross-section
- Scope static cross-section
- Stock transition
- Scope transition
- Attention redistribution
- Positive / Negative abnormal breadth
- Amount abnormal breadth
- Volume abnormal breadth
- Positive / Negative joint breadth
- Auction amount contribution
- concentration
- Review → Auction evidence linkage

### P0 EXCLUDE

- AuctionAnchor product semantics（旧 structure/chip anchor 产品语义）
- Structural relocation
- DSA / SMC / Chip Auction interpretation
- 撤单博弈
- 9:15–9:20 超短行为推演
- 主力资金叙事
- 资金净流入推断
- Amount / Volume directional capital interpretation（将成交额 / 成交量解释为净流向 / 买盘 / 主力 / 看多资金）
- Review 6-phase Dynamics（EMA / Velocity / Acceleration / Persistence / 6 阶段生命周期）
- 7-state lifecycle（旧 auction event tracking 生命周期）
- single Auction score（单一综合竞价评分）
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
| Scope Breadth | L1 至少 13 项（PRICE 6 + PARTICIPATION 5 + JOINT 2），各指标按 metric-specific eligibility 定义 denominator（非 global valid member count）；Amount/Volume abnormality 为无方向参与强度 | 合同已冻结 |
| Amount Contribution / Concentration | `AuctionAmountContribution` + Top1 / Top3 share + raw HHI / normalized HHI；Breadth/Contribution/Concentration 三者分离；normalized HHI 的 N = amount-share distribution eligible members | 合同已冻结 |
| Concept Overlap | overlapping membership 语义，贡献不互斥、允许 >100% | 合同已冻结 |
| L2 Observation Groups + Analysis | Industry/Concept 平行 Scope；Historical/Cross-sectional/Internal Structure/Overnight Transition 四问题；禁止 6-phase；Industry↔Industry / Concept↔Concept 同族排名 | 合同已冻结 |
| Member Attribution | Scope 结论可回溯 member evidence（top gap/amount/joint-abnormal contributors + 字段展示） | 合同已冻结 |
| Scope State Transition | 基于 Scope 自身事实，非成员标签计数，P0 无综合分 | 合同已冻结 |
| Attention Redistribution | 与 state transition 分离，禁止资金净流叙述 | 合同已冻结 |
| Conclusion Contract | 由可解释事实组合形成（price direction + participation breadth + intensity + concentration + member attribution）；描述 09:25 state；禁预测/评分；固定状态枚举待 Historical Validation | 合同已冻结（组成合同）；状态枚举 OPEN |
| Review → Auction | 只读正式 snapshot，禁调私有 calculator；缺失字段登记 GAP | 合同已冻结 |
| 版本化 | 阈值 / 配置 versioned 且随 run 记录 | 合同已冻结 |
| 数据质量 | 各 metric 按 metric-specific eligibility 定义 denominator（current eligibility 与 historical-history eligibility 分离）；最小门限为 calibration contract | 合同已冻结 |
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
- 现有 `/auction` 三级页面与 Review "竞价回流"（`AuctionBackflowPanel`）的旧产品形态；
- `auction_aggregation_service._classify_status_label` / `_classify_confidence`：若其仍基于 legacy anchor / status 语义（OB zone / dual breakout / supply-demand OB 等），只能作为 **DEPRECATED implementation gap**，**不能作为正式 Auction Conclusion authority**。

**代码迁移 / 删除由后续 Code Alignment Round 处理**，本轮仅完成需求事实源对齐，不修改 production legacy code。

全项目旧 Auction 引用已同步登记：

| 位置 | 处置 |
|---|---|
| `31-after-close-product-closure-v2.1.md`（§2 九节点 / §5 PC-31 / §6 PC-41） | 标记 DEPRECATED / 从新 P0 业务链移除 |
| `30-after-close.md`（AC-16 顶层步骤 `auction_anchor`） | 标记 DEPRECATED（legacy 盘后节点） |
| `70-review.md`（§27 依赖矩阵 auction 行） | 标记 DEPRECATED（旧竞价回流依赖） |

> 以上 GAP 为**显式登记**的 PRD↔代码暂时不一致，不隐藏；后续 Code Alignment Round 再处置实现。
