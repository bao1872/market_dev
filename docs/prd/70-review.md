# Review Scope Observation v2.3 — Final Product Contract

> **2026-08-13 SUPERSESSION NOTICE**
>
> 2026-08-13 Scope Observation v2.3 **正式取代** v2.2 中仍存在的未明确 L1 聚合口径，包括：
> - Turnover Rate；
> - Active OB Count；
> - Amount-weighted Return joint-valid universe；
> - Return Dispersion；
> - Segment Volume / Amount Mean Ratio Scope aggregation；
> - Structure Event Level semantics；
> - categorical-state preservation；
> - VolumeContext ownership；
> - Current vs Historical availability semantics；
> - Release Volume Ratio member-first semantics。
>
> 旧代码实现状态继续作为 **CURRENT IMPLEMENTATION BASELINE**（见 §7.17），
> **不再作为 TARGET PRODUCT CONTRACT**。
>
> 最后确认日期：2026-08-13。
> 本文档是 Review 唯一产品真相源（Single Product Source of Truth）。
>
> **PRODUCT CONTRACT = FINAL**。
> Dynamics Phase 六类的 exact numerical contract 已冻结（见 §7.11「Dynamics Phase Numerical Contract（FROZEN）」）。
> Leadership Migration exact algorithm 已冻结（见 §7.10「Leadership Migration Numerical Contract（FROZEN）」）。
> Internal Structure Type / Trading Context 的 exact threshold、
> conflict priority、tie-break 仍属于 **ALGORITHM MAPPING REQUIRED**。
> 这不代表 Product Contract 未冻结。

---

## 0. 领域输入

Review Scope Observation 的输入：

1. **第一金字塔（First Pyramid）**：个股层面的趋势 / 结构 / 动量 / 量能 / 事件 canonical 状态。
2. **行情（Market Data）**：日线 OHLC、成交量、成交额、复权因子、VWAP。

> **v2.3 输入清理**：Turnover Rate 不再列为 v2.3 必需的 Scope Observation 输入。
> 若底层数据源未来存在 `turnover_rate`，可继续作为 upstream raw data，
> 但 v2.3 Scope Observation **不消费 Turnover Rate**。
> 不得因此删除第一金字塔或其他模块可能存在的 turnover raw field。
3. **Scope PIT membership**：每个交易日 point-in-time 有效的 Scope 成员关系
   （industry_l1 / industry_l2 / industry_l3 / concept / major_index / style / market）。

这三类输入共同构成 Scope Observation 的事实基础。

---

## 1. 产品目标与边界

Review 应明确回答以下十个问题：

1. 今天市场最明显变化在哪里？
2. 相比其他 Scope，它今天有多突出？
3. 相比自身历史，现在处于什么位置？
4. 当前变化是在向上 / 向下迁移？
5. 这种变化是在加速还是减速？
6. 当前状态是否持续？
7. Scope 内部是扩散、核心集中、轮动还是碎片化？
8. 是哪些成员推动这种结构？
9. 当前更适合哪一种交易研究模式？
10. 每个解释的底层证据是什么？

产品边界（保持）：

- **不预测收益**；
- **不直接产生买卖建议**；
- **不产生黑盒综合分**（不发明强弱分、机会分、风险分）；
- 不恢复 P/Q/U/C/V 作为第一层 Observation；
- 不恢复 Filter/Signal 作为必经目标架构；
- 不把 Industry 重新作为 Concept discovery gate。

---

## 2. 权威业务链

Review Scope Observation 的目标产品链：

```
第一金字塔 + 行情 + Scope PIT membership
        ↓
L1 Scope Facts
        ↓
L2 Observation Groups（8 个市场逻辑组）
        ↓
Analysis
  ├─ Cross-sectional Analysis（横截面分位）
  ├─ Historical Dynamics
  │    ├─ Position
  │    ├─ Velocity
  │    ├─ Acceleration
  │    └─ Persistence
  └─ Internal Structure Dynamics
       ├─ Breadth
       ├─ Capital Tilt
       ├─ Concentration
       └─ Leadership Migration
        ↓
Interpretation
  ├─ Dynamics Phase（6 类）
  └─ Internal Structure Type（5 类）
        ↓
Trading Context（5 类）
        ↓
Member Attribution（成员下钻）
        ↓
用户判断 / Tracking
```

Scope Family 继续保持平行：

```
market
major_index
style
industry_l1
industry_l2
industry_l3
concept
```

不得改变现有「市场 / 风格 / 行业 / 概念平行」的产品原则。

---

## 3. 页面路由、权限与 URL 状态

（本节产品语义与 v2.2 不冲突，保留。）

Review 前端页面采用「市场结构工作台」语义，支持 Scope Family 平行切换、Discovery 详情下钻、个股证据、追踪。

权限：

- 阅读 Review 需要复盘权限；
- 管理端发布 / 撤销 / resume 需要管理权限；
- 用户无权限时返回明确 403，不得伪装为「无数据」。

URL 状态：

- `tradeDate` / `scopeType` / `scopeKey` / `discoveryId` / `instrument` 必须可序列化到 URL；
- 前进 / 后退必须可恢复页面状态；
- 不得混合不同 Review Run。

---

## 4. 后端模块结构

（本节产品语义与 v2.2 不冲突，保留其作为 Implementation Baseline 记录。）

当前实现包含：

- `review_orchestrator_service`：盘后编排；
- `scope_observation` / `scope_evidence_service`：Scope fact 计算与历史上下文；
- `first_pyramid_*`：第一金字塔 canonical 状态与事件；
- `board_analysis`：行业 / 概念板块聚合；
- `review_publication_service`：发布与撤销；
- `tracking`：用户追踪。

> **v2.3 实现重对齐要求（IMPLEMENTATION_REALIGNMENT_REQUIRED，见 §7.17）**：
> 上述模块当前承载的是旧 Objective Evidence / D1/D3/D5 / 24 Transition 实现，
> 需要在下一轮 PRD→CODE Impact Audit 中按 v2.3 目标合同重对齐，不得反向约束产品定义。

---

## 5. 数据模型

（本节作为 CURRENT IMPLEMENTATION BASELINE 保留；v2.2 不要求新建 Scope history table 或持久化框架，见 §7.15 / §7.17。）

当前关键表：

- `market_review_runs`：Review 运行记录；
- `market_review_run_items`：每个 Scope 的计算 item；
- `review_scope_observation_facts`：Scope 级 fact 持久化（business grain = `trade_date + scope_type + scope_key`）；
- `market_review_signals` / `market_review_discoveries`：legacy evidence / finding 记录；
- `market_review_metric_observations`：legacy P/Q/U/C/V component 持久化（legacy baseline）；
- `review_publications`：发布 pointer。

> PRD 不要求新建 schema。v2.2 只要求：新版 Scope Facts 必须能够形成逐交易日日序列（实现如何复用已有 daily Scope fact persistence，留给后续 Implementation Design）。详见 §7.15。

---

## 6. Scope Discovery 模型

### 6.1 Scope 定义

Scope = 一个有 point-in-time 成员关系的市场子集，类型包括：

```
market
major_index
style
industry_l1 / industry_l2 / industry_l3
concept
```

每个 Scope 在每个交易日独立计算，互不阻塞。

### 6.2 每个 Scope 的产品链

```
members
  ↓
L1 Scope Facts（当前 Scope 客观事实）
  ↓
L2 Observation Groups（8 个市场逻辑组织）
  ↓
Analysis（Cross-sectional / Historical Dynamics / Internal Structure）
  ↓
Interpretation（Dynamics Phase × Internal Structure Type）
  ↓
Trading Context（5 类）
  ↓
Member Attribution（成员下钻）
```

### 6.3 PIT membership（保持）

> v2.3 明确区分 **Product Family 冻结** 与 **Membership Source Readiness**。
> market / major_index / style / industry / concept 均为已冻结的平行 Scope Family；
> 当前未冻结的是各自正式 PIT membership taxonomy / canonical source。

- `industry_l1` = `PRODUCT_FAMILY_FROZEN / MEMBERSHIP_SOURCE_NOT_AVAILABLE`：Industry L1 产品语义已冻结；缺的是 PIT membership source；source 不可用时真实 unavailable；不得伪造 historical membership。
- `csi300` / `csi500` = `MEMBERSHIP_SOURCE_DEFERRED`：Major Index 作为 Scope Family 已冻结；当前缺口是 PIT membership canonical source；source 未 ready → unavailable；不得 current membership 回填历史；不阻塞其他 family。
- `style` = `MEMBERSHIP_SOURCE_DEFERRED`：Style 作为 Scope Family 已是 v2.3 FINAL CONTRACT；当前未冻结的是 Style 的正式 membership taxonomy / source；在 source 未 ready 前 Style 可真实返回 unavailable；Implementation 不得自行定义 Style 分类；不得用当前主观分类、临时标签或后验结果伪造历史 Style membership；该问题不阻塞其他 Scope Family 开发。
- 缺 PIT 成员写 `bootstrap_unavailable`，禁止 current×historical / latest backfill / forward-fill。

### 6.4 Scope 合同（重写 ownership 描述）

旧描述「`delta1d` / `delta5d` / `historical percentile` / `peer percentile` = L2 Objective Evidence」不再成立。

新版合同：

- **L1** = 当前 Scope 客观事实（PRICE / TREND / STRUCTURE / MOMENTUM / VOLUME 等 member 聚合量）；
- **L2** = 对 L1 的市场逻辑组织（8 个 Observation Groups，仅组织 / 导航 / 解释上下文，不产生 score）；
- **Analysis** = 对事实做横截面、历史动力、内部结构分析。

#### 6.4.1 Comparable Peer Cohort

Peer Cohort 逻辑保留，但明确它归属于 **Cross-sectional Analysis**，而不是旧 L2 Objective Evidence。

规则（same-family）：

- `industry_l1` 只和 `industry_l1`；
- `industry_l2` 只和 `industry_l2`；
- `industry_l3` 只和 `industry_l3`；
- `concept` 只和 `concept`；
- `style` 只和 `style`；
- `major_index` 只和 `major_index`；
- `market` 无 peer。

规模敏感 raw fact（Total Amount / Total Volume / Event Count / Member Count / Raw HHI）不得无条件跨不可比 Scope 排名。如果没有合理 peer universe：Cross-sectional = unavailable。

### 6.5 Review 发布就绪门禁（Phase 4C 校正）

（本节与 §11.1 构成同一份发布门禁合同，保留。）

- underlying coverage >= 0.95；
- 必要 Scope Observation facts 状态可用；
- market 缺失 / not ready / coverage 低于强制门槛 → whole Review publication CLOSED；
- `industry_l1` / `major_index` / `style` 属 PROGRESSIVE OPTIONAL，数据不可用不阻塞 whole Review publication；
- optional/parallel 语义只豁免「数据源不可用」，不豁免「执行异常」。

---

## 7. Scope Observation Product Model（v2.3 Final Product Contract）

### 7.1 产品分层与术语

| 层 | 名称 | 职责 |
|---|---|---|
| L1 | Scope Facts | 当前 Scope 客观事实 |
| L2 | Observation Groups | 对 L1 的市场逻辑组织（8 组） |
| A | Analysis | 横截面 / 历史动力 / 内部结构 |
| I | Interpretation | Dynamics Phase × Internal Structure Type |
| TC | Trading Context | 5 类交易研究模式 |
| MA | Member Attribution | 成员下钻 |

术语冻结原则：分类名称和语义 = FROZEN PRODUCT CONTRACT。
若 v2.3 未定义某个精确数值 threshold / 冲突优先级 / 多条件 tie-break，不自行发明，标 `ALGORITHM MAPPING REQUIRED`（代表实现规则未冻结，不代表产品层重新变成 NOT YET FROZEN）。

### 7.2 L1 — Price / Capital

| Fact | 定义 | Scope 主表达 |
|---|---|---|
| Equal-weight Return | 普通成员整体表现 | Scope 等权收益 |
| Amount-weighted Return | 当天主要成交资金所交易成员整体表现 | Scope 金额加权收益 |
| Total Volume | 成员成交量合计 | Scope total |
| Total Amount | 成员成交额合计 | Scope total |
| Price Raw HHI | 价格维度原始赫芬达尔指数 | Scope scalar |
| Price Normalized HHI | 价格维度归一化 HHI | Scope scalar |
| Amount Raw HHI | 成交额维度原始 HHI | Scope scalar |
| Amount Normalized HHI | 成交额维度归一化 HHI | Scope scalar |

- Equal-weight Return 与 Amount-weighted Return **必须同时保留**；
- **Turnover Rate 已正式删除（v2.3）**：Volume Ratio / Percentile / Z-score 已描述成员自身历史量能异常，Total Amount / Amount HHI 已描述 Scope 资金规模与集中度；Scope Turnover Rate 在当前产品中的增量信息不足，且会引入额外 denominator / aggregation 语义，因此 v2.3 不作为 Scope Fact。
- Capital Tilt **不进入 L1**：`Capital Tilt = Amount-weighted Return − Equal-weight Return`，属于 Internal Structure Analysis（§7.10）；
- 保留 normalized HHI 既有已确认数学合同；
- 不得把 Price HHI / Amount HHI / Capital Tilt 压成一个综合 Concentration Score。

#### 7.2.1 Equal-weight Return

使用 **price-valid member universe** 的 1D Return 等权平均。缺失 / 非 finite Return 不进入 valid universe。不得因为 Amount 缺失把成员从 Equal-weight Return 中删除。

#### 7.2.2 Amount-weighted Return

冻结数学合同：

```
AW_VALID =
  成员属于 price-valid universe
  AND amount finite
  AND amount >= 0

Amount-weighted Return =
  Σ(Return_i × Amount_i)
  /
  Σ(Amount_i)
```

权重只在 `AW_VALID` universe 内重新归一。明确：

- Return valid + Amount missing → 留在 EW，退出 AW；
- Amount valid + Return missing → 不进入 AW；
- Amount = 0 → 合法，但权重为 0；
- joint-valid total amount <= 0 → unavailable；
- 不得使用 Amount HHI universe 代替 AW joint-valid universe。

#### 7.2.3 HHI

Price / Amount Raw + Normalized HHI 继续复用既有已冻结数学合同。本轮不重新设计 HHI。

### 7.3 L1 — Trend

| Fact | 定义 | Scope 主表达 |
|---|---|---|
| Trend Direction Member Ratio | Up / Neutral / Down 成员占比 | Categorical → Member Ratio |
| Trend Strength | 可跨成员比较的连续事实 | Member Median |
| Current Segment Bars | 当前趋势段持续 bar 数 | Member Median |
| DSA-VWAP Deviation % | 当前趋势段相对 VWAP 偏离 | Member Median |
| Segment Change % | 趋势段变化百分比 | Member Median |
| Segment Slope | 趋势段斜率 | Member Median |
| Segment Volume Mean Ratio | 当前段均量 / 上一完整段均量 | Member Median |
| Segment Amount Mean Ratio | 当前段均额 / 上一完整段均额 | Member Median |
| VWAP Return Total | 相对 VWAP 的总收益 | Member Median |

- Trend Direction 是 First Pyramid canonical categorical state，Scope 用 Member Ratio 表达；
- 其余连续量（含 VWAP Return Total）均为 comparable continuous member fact，Scope 主表达**全部为 Member Median**；
- 不得把所有 trend 量压成一个综合 Trend Score。

#### 7.3.1 Segment Slope

canonical definition：`Segment Slope = Segment Change % / Segment Bars`。单位为 `% / bar`，是无量纲价格尺度下的趋势速度，允许跨股票比较并取 Member Median。**禁止重新归一化**。

#### 7.3.2 Segment Volume Mean Ratio

member fact：`Current Segment Average Volume / Previous Completed Segment Average Volume`。Scope fact = `Median(member Segment Volume Mean Ratio)`。明确禁止 `Σ Current Segment Volume / Σ Previous Segment Volume`。

#### 7.3.3 Segment Amount Mean Ratio

member fact：`Current Segment Average Amount / Previous Completed Segment Average Amount`。Scope fact = `Median(member Segment Amount Mean Ratio)`。明确：Amount ratio 的 denominator **必须是 Previous Segment Average Amount**，不能错误引用 Previous Segment Average Volume。

#### 7.3.4 VWAP Return Total

必须消费 First Pyramid canonical member fact：**VWAP Return Total**；Scope = Member Median。禁止：用 DSA-VWAP Deviation % 代理 VWAP Return Total；禁止 Review 自行创造替代公式。若 Current canonical source 有该 fact，Current L1 可以显示；若 historical daily fact 尚未覆盖，只有 Historical Dynamics unavailable，不得把 Current L1 一并判 unavailable。

### 7.4 L1 — Structure

**A. 当天 Structure Events**

| Event | 表达 |
|---|---|
| BOS | Event Type × Direction × Structure Level → Member Ratio |
| CHoCH | Event Type × Direction × Structure Level → Member Ratio |
| OB_CREATED | Event Type × Direction × Structure Level → Member Ratio |
| OB_ENTERED | Event Type × Direction × Structure Level → Member Ratio |
| OB_MITIGATED | Event Type × Direction × Structure Level → Member Ratio |
| EQH | EQH Member Ratio |
| EQL | EQL Member Ratio |

#### 7.4.1 Level 正式定义

Structure Event 中的 **Level = STRUCTURE LEVEL**，只允许 `Swing` / `Internal`。Level 绝不是 event price、break price、OB high/low、numeric level。numeric event price 可以作为底层 evidence，但不得参与 `Event Type × Direction × Level` 的 Scope aggregation key。

#### 7.4.2 BOS / CHoCH

Scope cell：`Event Type × Direction × Structure Level`。Structure Level 来自 canonical：`internal=true → Internal`，`internal=false → Swing`。**不得使用事件价格作为 Level**。

#### 7.4.3 OB lifecycle

OB_CREATED / OB_ENTERED / OB_MITIGATED 同样使用 `Event Type × Direction × Structure Level`，Structure Level 必须来自 First Pyramid canonical（`structure_level` / equivalent canonical semantics），不得从 numeric level 推断。

#### 7.4.4 EQH / EQL

EQH / EQL：只生成 Member Ratio，不定义 Swing / Internal，不人为补 Structure Level。

#### 7.4.5 Member Ratio denominator

Member Ratio denominator = `PIT(T)` ∩ 具有该日有效 canonical First Pyramid event coverage 的成员（缺事件 ≠ 没发生事件）。如果成员该日 canonical event capability 本身不可用，不得把它作为"未发生"进入分母。

同一成员在同一个 Event Cell 内当天发生多次：`event_count` 可以 > 1，`member_count` 只能计 1 次。Scope 主分析消费 `member_ratio`；Event Count / Member Count 只作为 supporting evidence。

#### 7.4.6 Current Structure State

| Fact | 定义 | Scope 主表达 |
|---|---|---|
| Structure Alignment | 结构对齐状态 | Member Ratio |
| Distance to Trailing Top % | 距 trailing 顶部百分比 | Member Median |
| Distance to Trailing Bottom % | 距 trailing 底部百分比 | Member Median |

- **Active OB Count 已正式删除（v2.3）**：Scope Active OB Count 无论 Sum 还是 Median，当前产品解释价值都不足；Sum 还会明显受 Scope member count 污染。个股 First Pyramid 仍然可以保留 Active OB Count，不得删除个股 First Pyramid fact。
- Structure Alignment 是 canonical categorical state，Scope 主表达 Member Ratio，不得转换为 numeric continuous fact、不得计算 Median、不得自行重新编码。
- Distance to Trailing Top % / Bottom % 属于 Comparable Continuous Member Fact，Scope = Member Median；Current source available 时 Current 可以显示；historical fact 缺失时只 Historical Dynamics unavailable，不能把 Current 一并 suppress。

### 7.5 L1 — Momentum / Volume

**Momentum**

| Fact | 定义 | Scope 主表达 |
|---|---|---|
| Squeeze State | 压缩状态 | Categorical → Member Ratio |
| BB Position | 布林带位置 | Member Median |
| BB Width | 布林带宽度 | Member Median |
| Release Volume Ratio | 释放量能比 | Member Median |
| Momentum / Volume Relation | 动量量能关系 | Member Ratio |

- SQZMOM Raw Value **不进入** Scope L1（原始值存在绝对价格尺度问题，不能直接跨成员 Median 后解释为统一 Scope 动量）。

#### 7.5.1 Squeeze State

Squeeze State 是 First Pyramid canonical categorical state，Scope = Member Ratio。Review 不得把字符串状态强转数字、不得自行重新编码状态、不得根据其他字段发明另一套 Squeeze State。

#### 7.5.2 BB Position / BB Width

必须消费 canonical First Pyramid fact；Scope = Member Median。Current source available → Current L1 显示；Historical source unavailable → 只阻塞其 Historical Dynamics。不得因为 history daily_state 未保存就把 Current 标成 upstream unavailable。

#### 7.5.3 Release Volume Ratio

member-level continuous fact。Scope = `Median(member daily Release Volume Ratio)`。产品语义：每个成员每天最多对 Scope aggregation 贡献一个 Release Volume Ratio fact。禁止直接把所有 SQZ_RELEASE event 放进 Scope median 导致一天事件更多的股票权重更高。

如果一个成员同日存在多个 raw release event：Review 不自行发明 event weighting；必须先按 First Pyramid canonical daily semantics 解析成一个 member-day fact，再进入 Scope Median。若当前 First Pyramid 尚未冻结 member-day 多事件解析规则，标 **ALGORITHM MAPPING REQUIRED**，但 member-first 原则已冻结。

#### 7.5.4 Momentum / Volume Relation

First Pyramid canonical categorical fact，Scope = Member Ratio。Review 不得根据 volatility phase × momentum direction × 任意 volume indicator 自行发明 Momentum / Volume Relation。若 canonical member fact Current 可用则 Current L1 可显示；若 history 不可用则 Historical Dynamics unavailable。

**Volume**

| Fact | 定义 | Scope 主表达 |
|---|---|---|
| Volume Ratio 20D | 量比 20 日 | Member Median |
| Volume Ratio 200D | 量比 200 日 | Member Median |
| Volume Percentile 20D | 量分位 20 日 | Member Median |
| Volume Percentile 200D | 量分位 200 日 | Member Median |
| Volume Z-score 20D | 量 Z 分 20 日 | Member Median |
| Volume Z-score 200D | 量 Z 分 200 日 | Member Median |

#### 7.5.5 Volume Single Source of Truth

Volume 20D / 200D 必须严格消费 First Pyramid canonical VolumeContext（或与其完全相同的 canonical owner）。Review 不得：复制一套 rolling formula；放宽窗口；改变 ddof；改变 percentile semantics；自行把短样本当成 200D。

#### 7.5.6 Volume Readiness

每个 Volume fact 的 availability 完全继承 canonical VolumeContext 的 readiness contract。特别明确：200D Fact 只有在 canonical 200D Fact ready 时可用。禁止 25D / 60D 等短历史被 Review 重新解释成 200D。`unavailable ≠ 0`。

### 7.6 Scope Aggregation Grammar

产品聚合原则（FINAL GRAMMAR）：

| 输入类型 | Scope 主表达 |
|---|---|
| Categorical State | Member Ratio |
| Structure Event | Member Ratio |
| Return | Equal-weight + Amount-weighted |
| Total Amount / Volume | Scope total |
| Concentration | HHI |
| 可比较 Continuous Member Fact | Member Median |
| 不可跨成员比较的 Raw Fact | 不生成 Scope scalar |

删除任何 Turnover Rate aggregation；删除 Active OB Count aggregation。同时增加两条：

1. Categorical canonical state 必须保持 categorical semantics，不得因工程方便数值化。
2. Member Ratio 的 denominator 必须是对应 fact 的 valid member universe，不能把"数据 unavailable"解释为"状态未发生"。

不得因为第一金字塔存在某字段，就强行创建 Scope Fact。

### 7.7 L2 — 8 Observation Groups

L2 不是算法层，不是评分层。固定为：

1. **价格与资金表现**（Price / Capital：Equal-weight Return、Amount-weighted Return、Total Volume、Total Amount、Price/Amount HHI）
2. **趋势状态**（Trend Direction Member Ratio、Trend Strength、DSA-VWAP Deviation %）
3. **趋势进程**（Current Segment Bars、Segment Change %、Segment Slope、Segment Volume/Amount Mean Ratio、VWAP Return Total）
4. **趋势量能确认**（Segment Volume/Amount Mean Ratio + Momentum/Volume Relation）
5. **结构突破与转折**（BOS / CHoCH Member Ratio + Direction/Level 分布）
6. **结构演化与位置**（OB_CREATED / OB_ENTERED / OB_MITIGATED Member Ratio、Structure Alignment、Distance to Trailing Top %、Distance to Trailing Bottom %、EQH/EQL Member Ratio）
7. **动量与压缩释放**（Squeeze State、BB Position、BB Width、Release Volume Ratio）
8. **量能异常**（Volume Ratio/Percentile/Z-score 20D/200D）

> v2.3 修改：Group 1 删除 Turnover Rate；Group 6 删除 Active OB Count，最终消费 OB_CREATED/OB_ENTERED/OB_MITIGATED、Structure Alignment、Distance to Trailing Top %/Bottom %、EQH/EQL。

每组必须列出其包含的 L1 Facts（见上）。L2 只做：组织 / 导航 / 解释上下文。
不得：产生综合 score；隐藏原始 L1；创建 opportunity / risk。

### 7.7.5 Analysis Foundation — Observation Series Contract

本节定义下游分析模块共同消费的共享输入契约。Cross-sectional（§7.8）、Historical Dynamics（§7.9）、Internal Structure Dynamics（§7.10）均消费同一份 `ObservationSeries`，由 Analysis Foundation Layer 统一产出。

#### Ownership boundary

**History Service**（已实现，commit 471dfa4 的 `review_observation_history_service.py`）：

- 提供按 `trade_date` 排序的历史快照序列；
- 提供可用性元数据（availability）；
- 不计算任何分析。

**Observation Primitive Registry**（已实现，commit de30606 的 `observation_primitives.py`）：

- 拥有 canonical primitive 定义（`key`）；
- 拥有 L1 payload 路径映射（`l1_path`）；
- 拥有标量提取规则（`extract`）。

**Observation Series Builder**（Analysis Foundation Layer，实现待定）：

- 将快照序列转换为 primitive series；
- 仅执行提取（调用 registry 的 `extract`）；
- 不计算：
  - percentile；
  - velocity；
  - acceleration；
  - persistence；
  - regime；
  - structure change；
  - signals。

#### 数据契约（冻结）

**ObservationSeries**

字段：

- `scope_type`
- `scope_key`
- `query_window`
  - `from_date`
  - `to_date`
- `availability`
- `primitives`

**PrimitiveSeries**

字段：

- `key`
- `l1_path`
- `points`

**PrimitivePoint**

字段：

- `trade_date`
- `readiness`
- `value`
- `available`

#### 可用性语义（冻结）

`available` 含义：

> 「primitive extractor 返回了一个有限标量（finite scalar）。」

它**独立于**快照 `readiness`。

规则：

- `readiness` 仅为元数据；
- 不得按 `readiness` 过滤 points；
- `partial` 快照仍可有 `available` 的 primitive 值；
- `ready` 快照仍可有 `unavailable` 的 primitive 值（`value = None`）；
- `None` 不等价于 `0`。

#### 窗口语义（冻结）

两个独立概念：

**Series Query Window**

归属历史层：

- `from_date`
- `to_date`

定义检索哪些快照。

**Analysis Window**

归属下游分析算法，本节不定义。

例如：

- percentile lookback（Position percentile lookback 已冻结于 §7.9）；
- EMA window（EMA numerical contract 已冻结于 §7.9）；
- persistence window。

**Position percentile lookback**（默认历史窗口 120 observations、最低有效历史 60 observations、baseline strictly pre-T、no future leakage，§7.9 Position contract，Position Foundation 已 CLOSED）与 **EMA numerical contract** 均已在 §7.9 **FROZEN**。**Persistence**（20D Historical Position Occupancy numerical / availability contract）已冻结于 §7.9 **Persistence Numerical Contract（FROZEN）**。**Dynamics Phase / Leadership / Interpretation thresholds** 仍保持 **IMPLEMENTATION DESIGN REQUIRED**。不得把 Dynamics Phase / Leadership / Interpretation thresholds 一并标成 ready。

#### 历史 Source 归属与边界（FROZEN）

`ObservationSeries` / `PrimitiveSeries` / `PrimitivePoint` 是**共享数据 shape**，对上游历史 source 的物理实现保持解耦。「History Service 是所有下游分析唯一历史 source」这类表述存在潜在歧义，正式拆分如下：

**1. Observation Series Shape Owner**

Observation Series Builder 负责：

- trading-date axis alignment；
- snapshot gap preservation（missing trading-observation slot → `available=False / value=None` point，slot 保留不压缩）；
- primitive extraction（经 registry）。

它**不决定 membership universe**。同一份 ObservationSeries shape 可以承载不同 Analysis 各自冻结的 source 语义。

**2. Source Adapter Ownership**

上游 source 必须先依据**具体 Analysis 冻结的 universe contract**，提供对应的 historical snapshots。Source 语义属于 Analysis 层决策，不属于 Builder。

**3. Analysis B source**

Historical Dynamics（§7.9）的正式历史 source = **CURRENT STATIC reconstruction**（`review_historical_scope_reconstruction_service.py`；详见 §7.9 Historical Membership Universe Contract（FROZEN））。

**4. Persisted PIT History**

`review_scope_observation_facts` 仍是 **daily historical-PIT Canonical Scope Observation history**。它可以用于需要 historical-PIT 事实语义的 consumer，但**不得直接冒充 Analysis B current-static source**（直接 wire 进 Dynamics Phase 会静默改变产品语义，见 §7.9 Implementation Boundary）。

不删除 History Service，只是收紧其适用边界。

### 7.8 Analysis A — Cross-sectional

核心：Cross-sectional Percentile。

回答：「今天相比可比较 Scope 在哪里？」

使用 Comparable Peer Cohort（same-family 规则见 §6.4.1）。

规模敏感 raw fact（Total Amount / Total Volume / Event Count / Member Count / Raw HHI）不得无条件跨不可比 Scope 排名。
如果没有合理 peer universe：Cross-sectional = unavailable。

#### 7.8.1 C1 Cross-sectional Analysis — Implementation Contract（v2.3 实现闭环）

本小节是 §7.8 的实现契约，用于关闭 REVIEW-V23-C1。本轮只定义 contract，不落地代码、不新增 migration / table / API / frontend。

C1 属于 **Analysis 派生视图（derived view）**：输入 L1 Canonical Facts + L2 Observation Groups，输出横截面位置证据；**不** recompute metric、**不**修改 fact、**不**创建新 technical indicator。

##### A. Peer Input Contract

| 层 | 责任 | 禁止 |
|---|---|---|
| **Domain**（`app/domain/review/analysis/cross_sectional.py`） | 纯确定性 projection。输入 = ①当前 scope 的 L1 payload（`observation_payload` dict）+ L2 groups（由 `build_l2_observation_groups` 生成）；②**外部预取好的 peer-fact 集合**：`dict[scope_key, peer_l1_payload]`（同族、同 trade_date 的其他 Scope 的 L1 payload）。输出 = cross-sectional facts。 | **不访问 DB**。不读 bars / tick / first-pyramid raw / indicators。 |
| **Service**（`app/services/review_cross_sectional_service.py`） | 1) 用 `get_scope_observation_fact(db, trade_date, scope_type, scope_key)` 读当前 scope L1；2) 用已有 `list_scope_observation_facts(db, scope_type=scope_type, from_date=trade_date, to_date=trade_date)` 取**同族同 trade_date 全量**（含当前 scope），按 `scope_key` 建 peer dict；3) 调 domain projection；4) 不重新计算任何 L1/L2 数值。 | 不新建 query / 不新增表读取路径；`list_scope_observation_facts` 已存在，仅复用。 |
| **Persistence** | 无新增责任。C1 不落库（derived view）。 | 不新增 schema / migration。 |

Peer universe 边界（来自 §6.4.1 same-family）：`industry_l1`↔`industry_l1`、`industry_l2`↔`industry_l2`、`industry_l3`↔`industry_l3`、`concept`↔`concept`、`style`↔`style`、`major_index`↔`major_index`；`market` 无 peer → C1 直接 `unavailable`，不进 projection。

##### B. Cross-sectional Comparison Definition

- **数学定义（empirical percentile rank）**：对某一可比字段 `f`，令 `v = current_scope[f]`（标量），`P = {peer[f] : peer ∈ peer universe, peer[f] is finite AND peer[f] status == ready}`（即仅 valid peer facts）。
  `percentile = (count(p < v) + 0.5 * count(p == v)) / valid_peer_count * 100`，取值 `[0, 100]`；`valid_peer_count == 0` → unavailable。
  等价表述：`percentile` = 当前 scope 的 `f` 值在 **valid peer 同字段经验分布**中的相对位置（含自身参与，与 L1 自身分布 percentile 同一约定）。
  **percentile 的分母必须是 `valid_peer_count`，never `peer_count`** —— peer universe 中可能含 unavailable / missing facts 的 peer，这些 peer 不进入 percentile 分母，percentile 只基于 valid peer facts 计算。`peer_count` 与 `valid_peer_count` 必须分别记录（见 §D）。
- **不使用 ranking / score**：`percentile` 是 **position evidence**（位置证据），不是 score、不是 rank、不暗示优劣方向。它只回答「当前 scope 的 f 值在可比群体中处于什么位置」。
- 输出结构（每可比字段）：
  `{"field": f, "value": v, "percentile": float|None, "peer_count": N, "valid_peer_count": M, "status": "ready"|"unavailable", "reason": ...}`
- **仅做 position，不做差异显著性、不做 hypothesis test、不生成任何 direction 语义（higher/lower 优劣由消费层解释，C1 不判定）。**

##### C. Comparable Field Whitelist

**C1 v1 字段范围（收紧）**：第一版 C1 **不使用**「所有 scale-invariant 字段」，仅允许以下显式 `C1_CORE_FIELDS` 类别。其余字段一律不进入 v1（见下「暂不进入 C1 v1」）。

**C1_CORE_FIELDS（v1 允许）**：

| 类别 | 字段（L1/L2 path） | 来源段 | 原因 |
|---|---|---|---|
| Price | `equal_weight_return` | price | 等权收益率，无量纲，跨同族 Scope 可比 |
| Price | `amount_weighted_return` | price | 金额加权收益率，无量纲，跨同族 Scope 可比 |
| Trend | `trend.continuous.regime_strength` | trend | 单位化连续强度量，跨同族可比 |
| Participation | `participation.volume.ratio20` / `ratio200` | participation | 量能相对量（自身历史比率），无量纲 |
| Momentum | `momentum.bb_position` | momentum | 无量纲布林位置量 |
| Momentum | `momentum.bb_width` | momentum | 无量纲布林宽度量 |

**暂不进入 C1 v1（需单独定义，不在 v1 范围）**：

| 字段 | 原因 |
|---|---|
| `structure.distance_to_trailing_top_pct` / `distance_to_trailing_bottom_pct` | 结构字段存在上下文依赖（trailing window / 相对参照），需单独定义比较语义 |
| `structure.alignment.Aligned_ratio` | 结构字段上下文依赖，需单独定义 |
| 任何 `_events`（BOS / CHoCH / OB_* / EQH / EQL cell）及 event statistics | 事件计数规模敏感，且属 L2 event evidence，非横截面量；raw event statistics 暂不进入 |
| `return_1d` / `return_5d` / `return_20d`（price 单期收益） | v1 仅取 equal/amount 加权聚合收益，单期成员收益分位后续单独评估 |
| `breadth.*_ratio` / `concentration.normalized_hhi` / `squeeze_state` ratio / `trend.state _ratio` 等 | 第一版聚焦核心四类，其余 scale-invariant 字段留待后续版本扩展（fail-closed：未列即禁止） |

> **注意**：`C1_CORE_FIELDS` 之外、且不在「暂不进入」清单的字段，默认**禁止**比较（fail-closed），需单独 PRD 确认后才加入白名单。

**Distribution-valued `C1_CORE_FIELDS` 标量提取（v2.3 实现闭环澄清）**：

L1 Canonical payload 中，`participation.volume.ratio20` / `ratio200` 与 `momentum.bb_position` / `bb_width` 并非标量，而是**分布对象**（`{p25, p50, p75, valid_count, ...}`）。C1 比较这些字段时，只取该分布对象的中心趋势 **`p50`** 作为 comparable scalar：

- `comparable scalar = p50`；
- `p50` 缺失 / 非 finite → 该 peer 视为 invalid，不进入 `valid_peer_count`，也不进入 percentile 分母；
- 当前 scope 自身 `p50` 缺失 / 非 finite → 该字段 `status = "unavailable"`，`reason = CURRENT_FIELD_UNAVAILABLE`；
- 不取 `p25` / `p75` / `median` / `valid_count` 等其他键；提取规则唯一、fail-closed，不自行语义映射。

（注：`trend.continuous.regime_strength` 在 L1 中为标量 median，直接比较，不取分布；`price.*` 同为标量。）

**永久禁止比较字段（规模敏感 / 不可跨 Scope 排名，所有版本适用）**：

| 字段 | 原因 |
|---|---|
| `price.total_volume` | 规模敏感 raw，跨不可比 Scope 无意义（§6.4.1 / §7.8） |
| `price.amount.total_amount` | 规模敏感 raw（绝对金额） |
| 各类 `event_count` / `member_count` | 规模敏感计数，取决于成员基数 |
| `price.concentration.raw_hhi` | 未归一化，受 member_count 机械下界影响 |
| `amount.total_amount` 等绝对量 | 规模敏感 raw |

##### D. Availability Contract（复用 L1 availability 风格）

字段（与 L1 每个 fact 的 `status` / `valid_count` / `denominator` 同风格）：

- `peer_count`：peer universe 中**同族同 trade_date** Scope 的总数（含当前 scope 自身）。
- `valid_peer_count`：peer universe 中该字段值 finite（且非 unavailable）的 peer 数（**不含**当前 scope 自身；当前 scope 的 `value` 单独记录）。
- **minimum_valid_peer_count = 5**（minimum valid peer sample）：C1 v1 要求至少 5 个 valid peer 才能构成有意义的横截面分布。
- **unavailable 条件**（任一触发 → `status="unavailable"`）：
  1. `scope_type == "market"`（无 peer）；
  2. 当前 scope 该字段 `value is None` / `unavailable`；
  3. `valid_peer_count < minimum_valid_peer_count`（即 `< 5`）→ `reason = INSUFFICIENT_PEER_SAMPLE`。
     （注：不再使用 `peer_count >= 2` 作为阈值；`peer_count` 仍记录，但有效性判定只基于 `valid_peer_count`。`valid_peer_count == 0` 是 `< 5` 的特例，同样归入 `INSUFFICIENT_PEER_SAMPLE`。）
- `status` 取值：`"ready"` / `"unavailable"`；`unavailable` 必须带 `reason`（枚举式字符串，沿用 L1 风格如 `NO_PEER_UNIVERSE` / `INSUFFICIENT_PEER_SAMPLE` / `CURRENT_VALUE_UNAVAILABLE`）。
- 不引入新 status 词汇表；沿用 L1 `unavailable` + `reason` 约定。

##### E. 禁止事项（C1 输出硬约束）

C1 **不产生**：

- `score`（任何 0–100 / 字母 / 综合分）；
- `rank`（排序名次 / 排名列表）；
- `signal`（买卖 / 方向性触发）；
- `opportunity`（机会判定）；
- `risk`（风险判定 / 风险等级）。

输出只含：每可比字段的 `value` + `percentile`（position evidence）+ availability 元信息。C1 是事实投影，不是结论生成器。

##### C1 Ownership Decision（实现闭环用）

- **输入 owner**：L1 `ReviewScopeObservationFact.observation_payload`（canonical SSOT）+ L2 `build_l2_observation_groups(...)` 输出（8 组）。仅限这两层。
- **输出 owner**：`app/domain/review/analysis/cross_sectional.py`（纯 projection）+ `app/services/review_cross_sectional_service.py`（复用 `get_scope_observation_fact` / `list_scope_observation_facts`）。
- **Persistence**：不需要（derived view，禁止 schema/migration）。
- **Derived view**：是。
- **无新 metric / 新 signal / opportunity / risk / ranking**。

### 7.9 Analysis B — Historical Dynamics

Historical Dynamics 消费 §7.7.5 定义的 Observation Series 契约（共享输入边界）。

删除 D1 / D3 / D5 作为目标核心时序表达。统一采用：

```
Position → Velocity → Acceleration → Persistence
```

**Position**：Scope Fact 当前 Historical Percentile。

- 默认历史窗口：120 trading days；
- 最低有效历史：60 observations；
- 当前 T 的历史基线只使用 T 之前数据，禁止未来泄漏。

**Velocity**：

- Fast EMA = 5，Slow EMA = 20（**产品简称 EMA5 / EMA20**；精确算法为 span=5 / span=20 valid input observations，见下方 EMA Numerical Contract）；
- `Velocity = EMA5(Position) − EMA20(Position)`；
- Velocity > 0 → 短期历史位置高于中期，向上迁移；
- Velocity < 0 → 短期历史位置低于中期，向下迁移。

**Acceleration**：

- `Signal = EMA5(Velocity)`；
- `Acceleration Proxy = Velocity − Signal`；
- 产品语言统一叫 **Acceleration**；
- 这是稳定的加速度代理，不是严格数学二阶导数，也不是传统价格 MACD。

**Persistence**：

- 20D Historical Position Occupancy；
- Upper Occupancy = 最近 20 日 Position ≥ 80 的占比；
- Lower Occupancy = 最近 20 日 Position ≤ 20 的占比。

#### EMA Numerical Contract（FROZEN）

本节冻结 EMA 的全部数值语义，消除下一轮 implementation 的 numerical semantic ambiguity。**PRD 公式本身是 owner，不得依赖 pandas 等库的 hidden defaults**（可与之数值对应，但不得以库默认行为替代本合同）。

**A. Standard span definition**

对 span = N：

```
alpha_N = 2 / (N + 1)
EMA_N(t) = alpha_N * x(t) + (1 - alpha_N) * EMA_N(previous_valid)
```

这等价于 standard span recursive EMA（可与 `pandas.ewm(span=N, adjust=False)` 数值对应），但本公式为唯一 owner。

**B. Seed**

第一条 valid input：

```
EMA_N(first_valid) = x(first_valid)
```

内部 EMA state 从第一条 valid input 开始建立。

**C. Warmup**

内部 state 允许从第一条 valid observation 开始更新，但输出 readiness：

- EMA5：至少累计 5 个 valid inputs 后 ready；
- EMA20：至少累计 20 个 valid inputs 后 ready。

warmup 未满足时：`value = null`、`status = insufficient_history`（若当前 input 同时 unavailable，按下方 Availability Status precedence 归 `unavailable_current`）。注意：内部 EMA state 已存在，只是输出尚未 ready；不得理解为「第 20 个 observation 才开始 seed」。

**D. Valid-observation clock**

EMA span 的推进单位是 **valid input observation**，不是绝对 calendar day。EMA5 / EMA20 是产品简称，精确语义为 span=5 / span=20 valid observations。input 完整连续时等价于 5 / 20 个 trading observations；某 trading day input unavailable 时，该日**不推进 EMA clock**。

**E. Missing input**

当 `Position(T) = unavailable / None`：

- `EMA5(T) = null`、`EMA20(T) = null`、`status = unavailable_current`；
- 保留 previous internal EMA state；
- 下一条 valid Position 到来后，直接从 previous_valid EMA state 继续递归。

禁止：forward-fill、zero-fill、synthetic value、daily decay、dropna 后改变日期输出对齐、gap reset。

**F. Gap**

普通单日或连续多日 unavailable：**不 reset EMA**；不定义 3-day / 5-day / 20-day reset 之类任意阈值。缺失期间：不 update、不 decay、不推进 valid_count。

**G. Velocity**

```
Fast(T) = EMA5(Position)(T)
Slow(T) = EMA20(Position)(T)
Velocity(T) = Fast(T) − Slow(T)
```

Velocity ready iff：1) `Position(T)` ready；2) `Fast(T)` ready；3) `Slow(T)` ready。任意条件不满足 → `Velocity(T) = null`。Velocity 不 forward-fill。

**H. Signal**

`Signal = EMA5(Velocity)`，使用完全相同 EMA contract：alpha=2/(5+1)、recursive、first-valid seed、min valid inputs=5、valid-observation clock、missing state-preserve。Signal ready iff：`Velocity(T)` ready AND 累计至少 5 个 valid Velocity observations。

**I. Acceleration**

```
Acceleration(T) = Velocity(T) − Signal(T)
```

ready iff：`Velocity(T)` ready AND `Signal(T)` ready；否则 `null`。

**J. No Future Leakage**

所有 EMA 只使用当前和过去已存在的 input。未来 observation 永远不能影响历史 EMA / Velocity / Signal / Acceleration。

#### Availability Status（FROZEN）

复用 Position 已有 availability 语义：`ready` / `insufficient_history` / `unavailable_current`。**不得创建** `warming` / `gap` / `paused` / `stale` 等新 status。

##### Historical Dynamics derived-fact availability precedence（FROZEN）

以下 precedence 统一适用于 **EMA5 / EMA20 / Velocity / Signal / Acceleration** 全部 derived fact（不在各 fact 重复书写）。

**Status propagation 基础规则（FROZEN）**：

Derived fact 的 availability propagation **必须基于 upstream `status`，不得只基于 upstream `value == null`**。`null` 是结果值（result value），不是 availability 原因；availability 原因必须由 `status` 传播。当 upstream `value = null` 时，可能来自 `unavailable_current` 或 `insufficient_history`，Implementation 必须读取 upstream `status` 才能确定 downstream status，**禁止仅凭 `value is None` 推断原因**。

**统一 precedence**：

1. 如果任一 required upstream fact `status = unavailable_current`：
   - current fact：`status = unavailable_current`
   - `value = null`
2. 否则，如果任一 required upstream fact `status = insufficient_history`，或 current fact 自身 warmup / valid-count 不足：
   - current fact：`status = insufficient_history`
   - `value = null`
3. 否则，所有 required upstream `status = ready` 且本 fact 自身 readiness 满足：
   - `status = ready`

即：**`unavailable_current` > `insufficient_history` > `ready`**。任一 required upstream `unavailable_current` 即终止并覆盖 downstream；`insufficient_history` 仅在无 `unavailable_current` 时生效。

**各 fact 的 upstream status propagation（FROZEN；不复制第二套 status enum）**：

- **EMA5 / EMA20**：upstream owner = `Position(T)` status；
- **Velocity**：upstream statuses = `Position(T)` + `Fast(T)` + `Slow(T)`（Fast/Slow 为直接 required facts；若 Fast/Slow 已严格继承 Position status，propagation 仍必须保留 cause，可追溯到 Position）；
- **Signal**：upstream status = `Velocity(T)`；
- **Acceleration**：upstream statuses = `Velocity(T)` + `Signal(T)`。

**Deterministic examples（FROZEN）**：

- **Example A**：`Position(T)=ready`、`EMA5(T)=ready`、`EMA20(T)=insufficient_history` → `Velocity(T)=insufficient_history`（**不是** `unavailable_current`）；
- **Example B**：`Position(T)=unavailable_current` → `EMA5(T)/EMA20(T)=unavailable_current` → `Velocity(T)=unavailable_current`；
- **Example C**：`Velocity(T)=ready`、`Signal(T)=insufficient_history` → `Acceleration(T)=insufficient_history`；
- **Example D**：`Velocity(T)=unavailable_current` → `Signal(T)=unavailable_current` → `Acceleration(T)=unavailable_current`；
- **Example E**：`Velocity(T)=insufficient_history` → `Signal(T)=insufficient_history`（**不是** 仅因 `Velocity.value=null` 判 `unavailable_current`）；
- **EMA 自身（与 Position Foundation 现有 contract 一致）**：`EMA5` 只有 3 个 valid Position 且 `Position(T)=None` → `unavailable_current`（不是 `insufficient_history`）；`Position(T)` valid → `insufficient_history`。

#### Persistence Numerical Contract（FROZEN）

本节冻结 Persistence（20D Historical Position Occupancy）的全部数值与 availability 语义，消除下一轮 implementation 的 numerical semantic ambiguity。**Persistence 直接消费 Position series（trade_date ascending），不是 Velocity / Signal / Acceleration**；它是与 Velocity / Signal / Acceleration 同层的 Historical Dynamics 派生结果。

**A. Window contract**

`PERSISTENCE_WINDOW_SIZE = 20`。

`Persistence(T)` 使用以 T 为右端、**包含 T** 的最近最多 20 个 trading observations：`[T-19, T]`。若 series 尚不足 20 observations，只使用实际已有 observations，不得向未来补齐。

禁止：向更早历史寻找 20 个 valid Position；dropna 后压缩成 20 valid observations；使用 T+1。

- `window_size = 20`；
- `candidate_count = min(20, available observations through T)`（**不得硬编码为 20**）。

**B. Current-day inclusion**

`Persistence(T)` 包含 `Position(T)`。产品含义：截至 T（含 T），当前状态在最近 20 trading observations 中是否持续出现。这与 Position percentile 的 pre-T baseline 不同；**不得把 Position 的 pre-T rule 复制到 Persistence**。

**C. Valid Position**

valid Position observation 定义：upstream `Position.status == ready` **且** `Position.position` 为 finite numeric value。Position owner 合法值域为 `[0, 100]`。

如果 `status == ready` 但 `position` 为 None / NaN / inf / 越界 → 这是 **upstream contract violation**，Implementation 必须 **fail fast**，不得把它静默转成 historical missing。禁止：zero fill、forward fill、clamp、silent drop。

**D. Historical missing**

窗口内部、但不是当前 T 的 observation，若 `Position.status` 为 `unavailable_current` 或 `insufficient_history`：该 observation **仍占一个 trading-window slot**，但**不进入 Upper numerator、不进入 Lower numerator、不进入 valid denominator**。不得向窗口之前补找 valid Position。

**E. Denominator**

- `valid_count` = 当前 20-slot trading window 内 valid Position observation 数；
- `upper_count` = valid Position ≥ 80 的数量；
- `lower_count` = valid Position ≤ 20 的数量；
- `upper_occupancy = upper_count / valid_count`；
- `lower_occupancy = lower_count / valid_count`。

禁止固定 `denominator = 20`（fixed20 会把 missing 隐式当成 middle Position）。

**F. Minimum valid contract**

`PERSISTENCE_MINIMUM_VALID_COUNT = 15`。目标 20D 窗口要求 `valid_count >= 15` 才允许 Persistence ready（等价目标 coverage `valid_count / 20 >= 0.75`）。这是 frozen product algorithm threshold，必须是 exact `15`（不得写成「大约 75%」「通常 15」「建议 15」）。

若 `valid_count < 15` 且当前 Position 本身不是 `unavailable_current`：`status = insufficient_history`、`upper_occupancy = null`、`lower_occupancy = null`。

**G. Current status precedence**

Persistence 自己的 availability precedence（**current upstream availability 优先于 historical-window coverage**）：

1. 若 `Position(T).status == unavailable_current` → `Persistence(T).status = unavailable_current`，Upper / Lower = null。即使历史窗口 valid_count ≥ 15 **也不得输出旧 Persistence**；
2. 否则若 `Position(T).status == insufficient_history` → `Persistence(T).status = insufficient_history`，Upper / Lower = null；
3. 否则若 `Position(T).status == ready` 但 `valid_count < 15` → `insufficient_history`；
4. 否则（`Position(T).status == ready` 且 `valid_count >= 15`）→ `ready`。

**H. Upper / Lower contract**

- `upper_count = count(valid Position >= 80)`；
- `lower_count = count(valid Position <= 20)`；
- 边界 inclusive：**80 属于 Upper，20 属于 Lower**；
- 由于 80 > 20，同一 Position 不可能同时属于 Upper 和 Lower；
- `Upper + Lower` **不要求 = 1**。例如所有 Position 均为 50：Upper = 0、Lower = 0，Persistence 仍可 `ready`（只要 valid_count ≥ 15）；
- **不新增** Middle Occupancy 作为 v2.3 product fact。

**I. Metadata**

Persistence fact 至少透明输出：`window_size = 20`、`minimum_valid_count = 15`、`candidate_count`（实际窗口 observation 数，≤ 20）、`valid_count`、`coverage = valid_count / window_size`（即 `valid_count / 20`）、`upper_count`、`lower_count`、`upper_occupancy`、`lower_occupancy`、`status`。

`coverage` 的 denominator 是 target `window_size = 20`，**不是 candidate_count**。原因：Persistence 是 20D target horizon；series 开头不足 20 observations 时不能显示成 100% coverage。

**J. No Future Leakage**

`Persistence(T)` 只允许读取 `<= T` 的 Position observations。T+1 / T+2 不得改变 `Persistence(T)` 的 `upper_count` / `lower_count` / `valid_count` / `coverage` / Upper Occupancy / Lower Occupancy。

**K. Status vocabulary**

只复用 `ready` / `insufficient_history` / `unavailable_current`。不得新增 `partial` / `low_coverage` / `warming` / `stale` / `gap` / `paused`。

**Deterministic examples（FROZEN）**：

- **Case A**：20 slots、20 valid、5 upper、5 lower → `ready`，Upper = 0.25，Lower = 0.25；
- **Case B**：20 slots、16 valid、16 upper、4 historical missing、current T ready → `ready`，Upper = 1.0，Lower = 0，coverage = 0.80；
- **Case C**：20 slots、14 valid、current T ready → `insufficient_history`，Upper / Lower = null；
- **Case D**：20 slots、19 valid、但 `Position(T)=unavailable_current` → `unavailable_current`，Upper / Lower = null；
- **Case E**：20 slots、19 valid、但 `Position(T)=insufficient_history` → `insufficient_history`；
- **Case F**：20 valid Position 全部在 20~80 之间 → `ready`，Upper = 0，Lower = 0；
- **Case G**：series 开头只有 10 observations → `candidate_count = 10`、`window_size = 20`、coverage ≤ 0.5（不得伪报 `candidate_count = 20`）。

#### Implementation Ownership

Historical Dynamics EMA math 与 **Persistence（20D Historical Position Occupancy）** 均属于 **Analysis B pure domain owner**。下一轮应扩展 `app/domain/review/analysis/historical_dynamics.py`（或遵循最终 implementation naming）。Persistence **直接消费 Position series**（不是 Velocity / Signal / Acceleration），与 Velocity / Signal / Acceleration 同层。该模块：不访问 DB、不负责 reconstruction、不负责 membership、不持久化、不访问 API、不做 Interpretation。

Historical Dynamics 用户语言固定为：

```
现在在哪里？（Position）
正在往哪里走？（Velocity）
是否加速 / 减速？（Acceleration）
是否持续？（Persistence）
```

#### Historical Membership Universe Contract（FROZEN）

Analysis B Historical Dynamics 使用 **CURRENT STATIC MEMBERSHIP × historical member facts** 作为正式历史 Scope universe contract。

**membership_mode = `"current_static"`**

对 Scope S 和 analysis as-of date A：

- `members(S, A)` = A 时点当前 canonical membership（**只 resolve 一次**）；
- 整个 historical observation window 内 member universe **固定不变**；
- 对每个历史交易日 T：读取 `members(S, A)` 中这些 member 在 **exact historical T 及 canonical T-1** 的真实 canonical facts，再调用 `compute_scope_observation()` 形成该 T 的 analysis-source Scope Observation。

**禁止**：

- 使用历史 T 的 Scope membership 替换固定成员集合（PIT(T) membership replacement）；
- historical / ASOF membership mixing；
- current member FACT 回填历史 T（current fact backfill）；
- future facts（no future leakage）。

**Provenance（至少）**：

- `membership_mode`
- `membership_asof_date`
- `member_count`

**Product meaning**：Historical Dynamics 回答「今天这个 Scope 的成员，过去是怎样演化到当前状态的？」它不是回答「历史上每一天当时定义下的 Scope 表现如何？」——后者由 PIT daily Scope Observation history 表达，仍是合法事实，但**不是 Dynamics Phase lifecycle owner**。

**Accepted recomputation semantics**：由于 current membership 未来可能变化，同一个历史 T 的 Historical Dynamics 在不同 analysis as-of date 可以重新计算出不同结果。这是 **accepted recomputation semantics，不是数据错误**。

#### Implementation Boundary

当前已实现的 `review_historical_scope_reconstruction_service.py` 是 current-static semantic owner / foundation，但仍为 **shadow execution path**。

下一 implementation 要解决的是：

```
Current-Static Reconstruction
→ ObservationSeries
→ Scope Dynamics
```

的 **application integration**。

**DO NOT wire** `review_observation_history_service` 的 persisted PIT series 直接进入 Dynamics Phase。

**Scale note**：current-static reconstruction 物理计算包含 Scope × member × historical trade_date，正式 runtime integration 前 **SCALE GATE REQUIRED**（具体 SLA 由 runtime / execution model 设计决定，PRD 不发明秒级 SLA；recomputation cadence / current-static result persistence 亦属后续 Scale / Execution Model Design）。

#### Interpretation Input Ownership（FROZEN）

Scope Dynamics Phase 的 **lifecycle primary owner = Equal-weight Return（EW）Historical Dynamics**。

Phase 的核心输入固定为：

- EW Position；
- EW Velocity；
- EW Acceleration；
- EW Persistence。

产品语义：EW = 普通成员整体表现，用于描述 Scope **整体动力生命周期**（「现在在哪里 / 往哪里走 / 是否加速 / 是否持续」）。

**明确禁止**的表达方式：

- 11 primitive voting（每个 primitive 一票表决）→ 见 §7.11「11 primitive ≠ 11 phase」；
- multi-factor score / weighted composite / 综合分；
- EW/AW dual-primary（两条平行主轴）。

**Amount-weighted Return（AW）Historical Dynamics 不是第二条 Phase lifecycle primary axis**：它属于 **Capital Confirmation**（见 §7.11），回答「主要成交资金所交易成员是否确认 EW 所表达的整体生命周期」。EW/AW 背离不得删除或覆盖 EW Phase。

**Persistence owner**：Phase 使用 **EW Persistence** 作为 persistence owner；AW Persistence 只属于 Capital Confirmation supporting evidence；不得定义 EW/AW joint Persistence。

其余 primitives 的 Historical Dynamics 作为 objective dynamics evidence 保留，其 Interpretation 层角色（parallel confirmation / structure-only / excluded）见 §7.11。

### 7.10 Analysis C — Internal Structure Dynamics

Internal Structure Dynamics 消费 §7.7.5 定义的 Observation Series 契约（共享输入边界）。

固定四条：

1. **Breadth**：Equal-weight Return、Advance Ratio、Decline Ratio、Unchanged Ratio、Return Dispersion；
2. **Capital Tilt**：`Amount-weighted Return − Equal-weight Return`；
3. **Concentration**：Price Normalized HHI、Amount Normalized HHI；
4. **Leadership Migration**：判断主导 Scope 的成员是否正在换人（成员贡献同时考虑 `Amount Share × Return`，通过连续交易日成员贡献排序稳定性表达；不发明黑盒 Leadership Score）。

**Return Dispersion 冻结（v2.3）**：price-valid member 1D Return 的 **population standard deviation**；公式语义 `sqrt( Σ(Return_i − mean(Return))² / N )`。`N < 2` 时 unavailable。不是 sample std、不是 MAD、不是 IQR、不是 variance。

**Capital Tilt / Concentration / Leadership Migration 语义冻结（v2.3）**：Capital Tilt 保持 `Amount-weighted Return − Equal-weight Return`；Concentration 保持 Price/Amount Normalized HHI；Leadership Migration 产品语义冻结为 `member contribution = Amount Share × Return` 比较连续交易日成员贡献排名是否稳定/迁移。

#### Leadership Migration Numerical Contract（FROZEN）

Leadership Migration exact algorithm 已在真实本地数据（4 scopes × 60 trading days）经 mapping 验证，现正式冻结如下：

1. **Raw Contribution**（沿用 §7.10 既有 owner）：`Contribution_{i,T} = AmountShare_{i,T} × Return_{1d,i,T}`。`amount_share` 唯一来源为 `compute_member_amount_contributions`；`return_1d` 来自 MemberObservation；任一 unavailable → `contribution = None`；真实 0 → `contribution = 0`。禁止 Leadership Migration 再算一套 amount denominator。
2. **Scope Direction**：唯一来源为 canonical `equal_weight_return`（`compute_scope_observation()["price"]["equal_weight_return"]`）。`D_T = +1` 当 `EW_T > 0`，`-1` 当 `EW_T < 0`。`EW = None`（unavailable）或 `EW = 0`（no prevailing direction）→ Leadership Snapshot unavailable（不得 None→0、不得 member_id 伪排名）。
3. **Direction-Aligned Contribution**：`Aligned_{i,T} = Contribution_{i,T} × D_T`。回答「是否推动 Scope 当天主要方向」；原始 contribution 保留，不被覆盖。
4. **Leader Candidate Universe**：仅 `Aligned_{i,T} > 0` 成员进入；`= 0` 非 leader；`< 0` 逆势/对立成员（排除）。
5. **Ranking（FROZEN）**：`aligned_score DESC, member_id ASC`。无其他 tie-break。
6. **Coverage（FROZEN）**：`LEADERSHIP_COVERAGE = 0.50`。令 `P_T = Σ_{i:Aligned_{i,T}>0} Aligned_{i,T}`；Leader Set `L_T` = 按 ranking 排序、达到 `Σ_{i∈L_T} Aligned_{i,T} / P_T ≥ 0.50` 的**最小前缀**。不是 Top-N，不动态调 40/60%。
7. **无 minimum-member threshold**：1 个有效 leader 即合法 Leader Set；小 Scope 自然允许更小 Leader Set。
8. **Snapshot Availability**：`EW` unavailable（Case A）→ `status=unavailable, leader_set=None`；`EW=0`（Case B）→ `status=unavailable, reason=no_prevailing_direction`；`EW` 有方向但无 `Aligned>0`（Case C）→ `status=ready, leader_set=[], leader_count=0`（合法空，`None ≠ []`）。
9. **Transition Availability**：任一 snapshot unavailable → Migration `status=unavailable`（禁止 unavailable→leader_count 0、unavailable→migration 0）。若参与比较的任一 Leader Set 合法为空（`[]`）→ Migration `status=unavailable, reason=empty_leader_set`（fail-closed，不定义 empty→empty = stable 或 empty→nonempty = 100% migration）。
10. **Primary Stability（FROZEN）**：`Jaccard Stability J_T = |L_{T-1} ∩ L_T| / |L_{T-1} ∪ L_T|`，字段 `jaccard_stability`，范围 `[0,1]`。同时感知 exits 与 entrants。
11. **Migration Scalar（FROZEN）**：`LeadershipMigration_T = 1 − J_T`，字段 `migration`，范围 `[0,1]`。这是结构变化事实，非风险/机会/买卖信号；禁止命名 leadership_score / rotation_score / risk_score / confidence。
12. **Supporting Fact**：`Previous Retention R_T = |L_{T-1} ∩ L_T| / |L_{T-1}|`，字段 `previous_retention`。不参与 Migration 公式；禁止综合评分（如 `0.7J + 0.3R`）。
13. **Transparent Set-change Facts**：输出 `previous_leader_count / current_leader_count / retained_count / entrant_count / exit_count`，其中 `Retained = L_{T-1} ∩ L_T`、`Entrants = L_T − L_{T-1}`、`Exits = L_{T-1} − L_T`；并保留 `previous_leader_ids / current_leader_ids / entrant_ids / exit_ids` 供 Member Attribution / drill-down。

**Internal Structure Type 五类分类（Broadening / Core-led / Rotating 等）仍标 `ALGORITHM MAPPING REQUIRED`**，不得与 Leadership Migration 一并冻结（属 Interpretation 层）。

这四类 Internal Structure Fact 同样可以消费 Position / Velocity / Acceleration / Persistence，形成内部结构时序。

### 7.11 Interpretation

最终 Analysis 不得停在一堆数字。解释层固定由：

```
Dynamics Phase（6 类） × Internal Structure Type（5 类）
```

**Dynamics Phase（6 类）**：

- Early Lift
- Strengthening
- Sustained
- Decelerating
- Weakening
- Repairing

**Internal Structure Type（5 类）**：

- Broadening
- Core-led
- Rotating
- Fragmenting
- Balanced

产品语义按 v2.3 固定。分类名称和语义 = FROZEN PRODUCT CONTRACT。
若 v2.3 未定义精确数值 threshold / 冲突优先级 / tie-break，标 `ALGORITHM MAPPING REQUIRED`。

#### Scope Dynamics Phase 输入架构（FROZEN）

**Phase label 语义边界**：Dynamics Phase（六类）**只描述 Scope 的动力生命周期阶段**（Position「现在在哪里」/ Velocity「往哪里走」/ Acceleration「是否加速」/ Persistence「是否持续」）。Phase owner 不得扩大为「综合所有确认后的整体市场状态」。

**Lifecycle primary owner**：EW Historical Dynamics（见 §7.9「Interpretation Input Ownership」）。Phase 核心输入 = EW Position / EW Velocity / EW Acceleration / EW Persistence。AW / Breadth / Volume / 结构事实**均不是第二条主轴**。

**Parallel confirmation evidence（不得改写 Phase label）**：

- **Capital Confirmation**：Amount-weighted Return Historical Dynamics。回答「主要成交资金所交易成员是否确认 EW 所表达的整体生命周期」。EW/AW 背离 → `not confirmed / divergence evidence`，不得删除或覆盖 EW Phase。
- **Breadth Confirmation**：`advance_ratio` 为 canonical confirmation axis；`decline_ratio` / `unchanged_ratio` 为 supporting evidence，**不得作为额外独立 vote**。Breadth 本身仍属 §7.10 Internal Structure 正式组成部分；此处只定义其同时可作为 parallel Phase confirmation evidence。
- **Volume Participation Confirmation**：`participation.volume.ratio20` / `participation.volume.ratio200`。回答「当前 EW lifecycle 是否得到量能参与支持」。它们**没有价格方向 owner 权限**——Volume positive 不得自动等于 Phase Strengthening。

Confirmation 不得通过多数投票 / 加权 score / 综合分重新产生 Phase。Parallel confirmations 后续由 **Internal Structure、Trading Context、explanation** 消费。

**Structure-only inputs（不参与 Dynamics Phase v1 label）**：`return_dispersion`、`price_normalized_hhi`、`amount_normalized_hhi`、Capital Tilt、Leadership Migration。它们属于 Internal Structure context。

**Excluded from Phase v1**：`trend.continuous.regime_strength`。原因：其 **Phase directional ownership 尚未冻结**；Historical Dynamics 可以继续存在，但 Phase v1 **fail-closed 不消费**。不删除该 primitive。

**11 primitive ≠ 11 phase**：Historical Dynamics 对多个 primitive 形成 objective dynamics evidence，**不意味着每个 primitive 各自产生一个产品 Dynamics Phase**。产品层：每个 Scope / trade_date **最多一个 Dynamics Phase**；Phase synthesis owner 遵循本节冻结架构。

**Deterministic architecture cases（FROZEN，只冻结架构，不冻结 confirmation label enum / threshold）**：

- **Case A**：EW lifecycle 明显向上、AW lifecycle 向下 → Phase 由 EW 决定；AW = capital divergence evidence；**不因为 AW 反向而把 Phase null**。
- **Case B**：EW lifecycle strengthening、Breadth deteriorating → Phase 仍由 EW lifecycle 决定；Breadth = negative / weakening confirmation evidence；**Breadth 不重写 Phase**。
- **Case C**：EW lifecycle repairing、Volume participation weakening → Phase lifecycle 不被 volume 改写；Volume = absent / weak confirmation evidence。
- **Case D**：HHI rapidly rising、EW lifecycle flat → **HHI 不得投票把 Phase 变成 Strengthening**。

#### Dynamics Phase Numerical Contract（FROZEN）

Dynamics Phase 六类分类的 exact numerical contract 已基于真实历史数据（distribution inspection + representative / event replay + sensitivity + mutual-exclusion verification）冻结。Implementation 必须使用下列精确常量与 boolean 条件，不得引入任何额外阈值、优先级链或综合分。

**冻结常量（exact，不得写成约数）**：

```
DYNAMICS_PHASE_VELOCITY_GATE         = 2.0
DYNAMICS_PHASE_ACCELERATION_GATE     = 1.0
DYNAMICS_PHASE_POSITION_HIGH         = 70.0
DYNAMICS_PHASE_UPPER_OCCUPANCY_GATE  = 0.20
DYNAMICS_PHASE_LOWER_OCCUPANCY_GATE  = 0.30
```

**Exact state definitions（numerical states，不是新 Phase）**：

```
V_NEG : velocity <= -2.0
V_MID : -2.0 < velocity <= 2.0
V_POS : velocity > 2.0

A_NEG : acceleration <= -1.0
A_ZERO: -1.0 < acceleration <= 1.0
A_POS : acceleration > 1.0
```

**HIGH_REGIME**：

```
HIGH_REGIME = position >= 70.0 AND upper_occupancy >= 0.20
```

产品语义：historical upper regime 已形成，且 current Position 仍与 high regime 相容。`position == 70.0` 属于 HIGH_REGIME（前提 upper_occupancy >= 0.20 同时满足）。

**BOTTOM_RECOVERY_CONTEXT**：

```
BOTTOM_RECOVERY_CONTEXT = position < 70.0 AND lower_occupancy >= 0.30
```

产品语义：historical lower-regime evidence 存在，且 current observation 尚未进入 high position region。它是 **joint eligibility context**，不是 raw Position low-zone 定义（禁止写成 `Position < 70 = bottom`）。

**Exact phase conditions（by construction mutually exclusive）**：

| Phase | Boolean condition |
|---|---|
| Weakening | `velocity <= -2.0`（Position / Acceleration / Persistence 不 gate） |
| Decelerating | `HIGH_REGIME AND velocity > -2.0 AND acceleration <= -1.0` |
| Sustained | `HIGH_REGIME AND velocity > -2.0 AND -1.0 < acceleration <= 1.0` |
| Early Lift | `BOTTOM_RECOVERY_CONTEXT AND velocity > 2.0 AND acceleration > 1.0` |
| Repairing | `BOTTOM_RECOVERY_CONTEXT AND velocity > -2.0 AND acceleration <= 1.0` |
| Strengthening | `velocity > 2.0 AND acceleration > 1.0 AND NOT BOTTOM_RECOVERY_CONTEXT` |

明确：
- **Repairing 不需要 `velocity <= 2.0`**：允许 `velocity > 2.0` 但 `acceleration <= 1.0`，即 bottom recovery 但尚未达到 Early Lift 的明确加速确认；
- **Strengthening 必须写出完整 boolean `NOT BOTTOM_RECOVERY_CONTEXT`**，不得简写为「non-bottom」。

**Mutual exclusion（priority = NONE）**：六类规则按数学条件 **mutually exclusive**；一个 ready observation 最多匹配一个 Phase。**不存在 priority chain**（不得写 `Weakening > Decelerating > ...`）；当前 final contract 无需 priority 即可唯一分类，tie-break 不需要。

**Ready but unclassified**：四个 required inputs 全部 ready，但六个 Phase 条件均不满足 → `analysis status = ready`、`phase = null`。这不是第七类 Phase，也不是 unavailable；不得强制全覆盖。

**Availability / status propagation**：Required inputs = EW Position / EW Velocity / EW Acceleration / EW Persistence。
- 任一 required input status = `unavailable_current` → Phase status = `unavailable_current`、`phase = null`；
- 否则任一 required input status = `insufficient_history` → Phase status = `insufficient_history`、`phase = null`；
- 否则 Phase status = `ready`，再执行六类 classifier；ready + no match → `phase = null`。
- 不得新增 status vocabulary。

**Boundary ownership（exact，只写当前实际 threshold）**：

```
velocity == -2.0
→ Weakening

velocity just above -2.0
→ 不属于 Weakening，由其他 context / acceleration 条件判断

acceleration == -1.0 + HIGH_REGIME + velocity > -2
→ Decelerating

acceleration just above -1.0 + HIGH_REGIME + velocity > -2
→ Sustained

acceleration == 1.0 + HIGH_REGIME + velocity > -2
→ Sustained

acceleration == 1.0 + BOTTOM_RECOVERY_CONTEXT + velocity > -2
→ Repairing

position == 70.0 + upper_occupancy >= .20
→ HIGH_REGIME

position just below 70 + lower_occupancy >= .30 + velocity > 2 + acceleration > 1
→ Early Lift

lower_occupancy == .30 + position < 70
→ BOTTOM_RECOVERY_CONTEXT
```

删除 / 不引入旧实验边界（如 `pos==30 mid`）。

**Deterministic cases（PRD examples）**：

- **Case A**：`velocity == -2.0` → **Weakening**。
- **Case B**：`HIGH_REGIME AND velocity > -2 AND acceleration == -1` → **Decelerating**。
- **Case C**：`HIGH_REGIME AND velocity > -2 AND acceleration == 1` → **Sustained**。
- **Case D**：`BOTTOM_RECOVERY_CONTEXT AND velocity > 2 AND acceleration > 1` → **Early Lift**。
- **Case E**：`BOTTOM_RECOVERY_CONTEXT AND velocity > 2 AND acceleration == 1` → **Repairing**。
- **Case F**：`velocity > 2 AND acceleration > 1 AND NOT BOTTOM_RECOVERY_CONTEXT` → **Strengthening**。
- **Case G**：all required inputs ready 但无规则匹配 → `status = ready` / `phase = null`。

**Output contract（domain output semantics）**：Phase analysis output 至少包含 `trade_date` / `phase` / `status`；建议透明 evidence：`position` / `velocity` / `acceleration` / `upper_occupancy` / `lower_occupancy`；可选 derived states：`velocity_state` / `acceleration_state` / `high_regime` / `bottom_recovery_context`。**禁止** `phase_score` / `confidence_score` / `strength_score` / `composite_score`。本轮只冻结 domain output semantics，不设计 API schema。

#### 7.11.1 Algorithm Mapping 边界（v2.3）

Dynamics Phase 六类的 exact numerical contract 已冻结（见 §7.11「Dynamics Phase Numerical Contract（FROZEN）」）；Internal Structure Type 五类 / Trading Context 五类继续冻结。
尚未冻结部分的 exact threshold、conflict priority、tie-break 必须基于真实历史数据，通过 distribution inspection + representative case replay 冻结 Algorithm Mapping。Implementation 不得自行发明 `Position > 70`、`Acceleration > X` 等 arbitrary threshold。

Mapping 依赖按层级拆解，**互不阻塞、各自独立 ready**：

- **A. Dynamics Phase Algorithm Mapping = FROZEN / CLOSED**：依赖 L1 canonical facts + EW Historical Dynamics（Position / Velocity / Acceleration / Persistence）真实历史数据。依赖链：L1 → EW Historical Dynamics → 六类 Phase 分类。本层已冻结：
  - exact thresholds 已冻结（「Dynamics Phase Numerical Contract（FROZEN）」五个常量）；
  - boolean conditions 已冻结（六类 exact boolean conditions）；
  - mutual exclusion 已冻结（数学条件 mutually exclusive）；
  - priority = NONE（无 priority chain）；
  - tie-break 不需要（一个 ready observation 最多匹配一个 Phase）；
  - ready-but-unclassified 已冻结（status = ready / phase = null）。
- **B. Internal Structure Type Mapping 依赖 = ALGORITHM MAPPING REQUIRED**：Internal Structure 四类事实（Breadth / Capital Tilt / Concentration / Leadership Migration）真实数据。依赖链：L1 → Internal Structure facts → 五类 Internal Structure Type 分类 Algorithm Mapping。与 Dynamics Phase mapping 并行独立，不等待 A。其中 **Leadership Migration exact algorithm 已冻结**（见 §7.10「Leadership Migration Numerical Contract（FROZEN）」：coverage=0.50 / aligned_score DESC + member_id ASC / Jaccard primary / Migration=1−Jaccard / Retention supporting），**但五类 Internal Structure Type 分类本身的 threshold / conflict priority / tie-break 仍标 ALGORITHM MAPPING REQUIRED**，不要在 PRD 里现在选一个未经数据验证的分类公式。
- **C. Trading Context Mapping 依赖 = ALGORITHM MAPPING REQUIRED**：Dynamics Phase（A）与 Internal Structure Type（B）均 ready 后，再冻结五类 Trading Context 的 mapping。Trading Context 消费 Phase × Type 组合，因此是**最晚**解锁的一层；但 A / B 未 ready 不阻塞 L1 / L2 / Cross-sectional / Historical Dynamics 开发。

分层原则：三者 mapping 各自独立 ready，任一层的 threshold 数据尚未产生时，其余层不得被该层阻塞；已 ready 层可以先行冻结，无需等待全链。

### 7.12 Trading Context

固定五类：

1. **Early Discovery** → 找最早趋势 / 结构 / 量能确认成员；
2. **Leader Focus** → 聚焦稳定核心，不把 Scope 强势理解为普涨；
3. **Broadening Participation** → 可从核心向有证据的扩散成员扩大候选池；
4. **Rotation Search** → 重点寻找贡献排名和结构确认正在上升的新核心；
5. **Confirmation / Patience** → 提高新交易确认要求，等待 Breadth / Acceleration / Structure 再次确认。

Trading Context 回答：「当前市场结构下，应该采用什么交易研究模式？」

明确：不是 Buy / Sell；不是 Opportunity / Risk；不是仓位建议；不是收益预测。Trading Context 五类的 exact threshold / conflict priority / tie-break 同样属于 ALGORITHM MAPPING REQUIRED。

### 7.13 Member Attribution

Scope 分析不是终点。完整链：

```
发现 Scope
  → 判断 Dynamics Phase
  → 判断 Internal Structure
  → 确定 Trading Context
  → 下钻到成员
  → 用户判断
```

针对不同 Trading Context，定义对应 Member Attribution 关注方向。
不得把 Member Attribution 写成自动选股推荐。

### 7.14 Member State Migration Evidence

保留旧 Transition 的真实信息价值：member exact `T-1 → T` state migration。

重新定位为 **Member State Migration Evidence**，支持：

- Trend / Structure / Momentum 的 member 状态迁移；
- drill-down / explanation / member identity migration。

不再定义「24 个 Transition ratio = 一级 L2 Objective Evidence 产品体系」。
旧代码可以暂时存在，后续 Implementation Audit 决定复用、删除或适配。

### 7.15 Current vs Historical Availability Contract

继续保留：

- PIT(T)
- valid_count
- denominator
- coverage
- unavailable
- insufficient_history

**不得用 0 代替 unavailable。**

#### 7.15.1 两个独立维度（v2.3 强化）

Current Availability 和 Historical Availability 是两个独立维度。正式规则：如果一个 member / Scope Fact **Current canonical source = ready**，则 **Current L1 可以显示**。即使 historical daily series = insufficient / unavailable，也只导致 Historical Position / Velocity / Acceleration / Persistence unavailable。**禁止 `history missing → Current L1 upstream unavailable`**。

典型适用：VWAP Return Total、Distance to Trailing Top %、Distance to Trailing Bottom %、BB Position、BB Width、Momentum / Volume Relation 等。

同时明确：Current-only 不允许使用 current snapshot 倒填历史。禁止 future leakage。

Historical Dynamics 要求：对应 Scope Fact 必须存在连续、点时正确的历史。如果只有 Current：

- Current 可以显示；
- 但 Position / Velocity / Acceleration / Persistence 必须 unavailable。

PRD 不要求新建 Scope history table。产品要求只是：新版 Scope Facts 必须能够形成逐交易日日序列。实现如何复用已有 daily Scope fact persistence，留给后续 Implementation Design。

#### 7.15.2 Current-Static Membership 与 Historical Fact Availability（v2.3 补充）

Current-static membership **只固定 MEMBER UNIVERSE**。它**不改变** L1 Canonical Scope Observation 已有的 **field-specific valid-universe** denominator / availability semantics。

对于固定 current member 在历史 T 缺某个 canonical member fact：

1. 该 member 的该字段保持 `unavailable` / missing。
2. **禁止**：forward-fill / current-backfill / future fact / 其他日期替代。
3. Scope aggregate 继续由 `compute_scope_observation()` 按该字段既有 canonical valid-universe semantics 计算。
4. Member-level missing **不得自动升级**为整个 Scope snapshot unavailable 或整个 PrimitivePoint unavailable。
5. 只有当 canonical Scope aggregate 对目标 primitive **最终得到** `None` / non-consumable value 时，ObservationSeries 对该 T 输出 `value = None, available = False`。
6. 无论 primitive 最终 available / unavailable，canonical trading-date slot 都必须保留（slot 保留规则见 §7.7.5）。

**Member Availability → Canonical Scope Aggregation → Primitive Availability 三层分离，不得合并**：

- Current-static 只决定 **WHO** is in the universe（成员集合）；
- Canonical L1 aggregation 决定 **WHICH** members are valid for each field（field-specific valid universe / denominator）；
- ObservationSeries 从 **resulting Scope primitive value** 决定 availability（能否经 registry extraction 得到 finite scalar）。

**极短示例（仅说明，非 coverage threshold）**：

假设 current-static Scope 有 100 个成员。历史 T：95 个成员有合法 1D Return，5 个成员缺 exact-T1 Return。则：

- 这 5 个 member 不进入 EW Return valid universe；
- EW Return 仍由 95 个 valid returns 计算，Scope `equal_weight_return` 仍是 finite canonical value；
- 因此 PrimitivePoint 仍可 `available = true`；
- 只有 valid return universe 最终为空（导致 Scope `equal_weight_return = None`）时，该 PrimitivePoint 才 `available = false`。

（该示例中的 95/100 只是说明数字，**不是冻结的 coverage threshold**。）

### 7.16 Scope Architecture / PIT / Peer Contract

保留当前已经正确的长期架构原则：

- family-agnostic calculation（家族无关计算）；
- parallel Scope Family（平行 Scope 家族）；
- PIT membership（点时成员）；
- comparable peer cohort（可比同侪群）；
- taxonomy ≠ discovery gate（分类法不是发现门禁）；
- membership ≠ trading logic（成员关系不是交易逻辑）；
- readiness / data quality（就绪 / 数据质量）。

不得因重写 §7 而丢失这些长期架构原则。

**PIT membership 长期原则 vs Analysis B current-static exception（v2.3 明确）**：

PIT membership 仍是 **Daily Canonical Scope Observation** 的长期架构原则，本节上述原则**不被删除**。

但 **Analysis B Historical Dynamics** 是明确冻结的 **derived-analysis exception**：它使用 current-static universe，因为产品问题本身是「当前成员的历史演化」（见 §7.9 Historical Membership Universe Contract（FROZEN））。该 exception **只冻结在 Analysis B 相关 source contract**，不得无意扩展到 Cross-sectional（§7.8）/ Internal Structure（§7.10）等其他 Review 模块。

### 7.17 Legacy Supersession / Current Implementation Gap

**TARGET PRODUCT CONTRACT** = v2.3 Final Product Contract（本文档 §0–§7.16）。

**CURRENT IMPLEMENTATION BASELINE** = 现有旧 Scope Observation / Objective Evidence 实现，包括但不限于：

- 29 CORE scalar；
- 24 Transition；
- Current / D1 / D3 / D5 / Historical / Peer。

这些是 CURRENT ACTUAL，不是新目标产品合同。

标记：

- `SUPERSEDED_AS_TARGET`
- `IMPLEMENTATION_REALIGNMENT_REQUIRED`

不要删除代码状态历史，也不要让它继续污染新产品定义。

**Implementation Realignment（当前实现必须对齐 v2.3 final contract，只记录，不写修复代码）**：

1. Remove Scope Turnover Rate target consumption.
2. Remove Scope Active OB Count target aggregation.
3. Amount-weighted Return must use joint-valid universe.
4. Return Dispersion must use population std.
5. Segment Volume / Amount Mean Ratio = member ratio → Scope Median.
6. Structure Event Level = Swing / Internal, never numeric price.
7. Canonical categorical states must remain categorical.
8. Volume 20D/200D must reuse canonical VolumeContext readiness and math.
9. Release Volume Ratio must be member-first before Scope Median.
10. Current source availability must not be suppressed by missing historical coverage.
11. Structure Events continue consuming canonical immutable event evidence.

明确：当前已经存在的代码实现不是 PRD authority。下一轮必须从 v2.3 Final 重新做 implementation correction / verification。

不要在这里：新建历史表设计；新建 member vector persistence；定 schema；写 migration。

---

## 8. Legacy Filter / Signal Compatibility（非 V2 目标架构）

> **2026-08-13 架构降级更新**：本节所述 A/B/C/D Filter、`filter_engine`、`MarketReviewSignal` 等属于
> **legacy implementation compatibility**，不定义 Scope Observation v2.3 的目标发现架构。
>
> v2.3 目标产品链已冻结至：
> **L1 Scope Facts → L2 8 Observation Groups → Analysis → Interpretation → Trading Context → Member Attribution**。
>
> 以下全部 **NOT YET FROZEN（PRODUCT DESIGN REQUIRED）**，不在本轮冻结、也不在实现轮次中自行推导：
> - 是否需要独立 Filter Engine；
> - 是否需要 threshold condition；
> - 是否需要 matched / unmatched；
> - 是否必须存在 Atomic Signal；
> - Discovery 是否必须聚合 Signal；
> - 是否继续使用 A/B/C/D family；
> - Discovery 的排序 / 聚类 / 异常组织机制。
>
> **禁止 Implementation 阶段从 legacy 架构推导「Filter 是下一必做模块」。**

A/B/C/D 当前作为**内部算法 family** 继续存在以维持现有实现兼容，但不再作为用户前端一级信息架构，也不作为 V2 目标发现路径。

**历史定位（2026-08-11，仅作 legacy 说明）：**

- A/B/C/D 是 legacy Filter Engine 内部的算法分类，不是前端一级产品结构；
- 前端不再按 A/B/C/D 分组展示，转用用户语义（状态 / 改善 / 恶化 / 扩散 / 收缩 / 异常 / 共振）；
- D Family（state migration / freshness / diffusion / concentration / relative strength）定位为 legacy **Discovery Evidence Family**，不是独立 Signal Family；
- `MarketReviewSignal` 保留为 legacy atomic evidence record；Signal/Discovery 是否保留该聚合关系仍 NOT YET FROZEN（见 §10A）。

**Observation Model 收口（2026-08-12，2026-08-13 重定向）：**

- Legacy Filter / Discovery 应只消费 structured Observation Evidence（§7 Scope Observation Model v2.3），不得依赖 P/Q/U/C/V score 作为 first-layer observation；
- 以下 A/B/C 初始阈值（§8.1–8.3）当前以 `P/Q/U/C/V` 分位 / `value` 表达，属于对 P/Q/U/C/V first-layer 的硬依赖，标记 `LEGACY IMPLEMENTATION REFERENCE / NOT V2 TARGET SPEC`：它们是既有实现的历史说明，不得作为新实现要求，不得现场发明新 P/Q/U/C/V 阈值；
- D 族（state migration / freshness / diffusion / concentration / relative strength）消费第二金字塔 raw evidence（非 P/Q/U/C/V score），保持不变（legacy）；
- 具体 Observation-based Filter 条件（含任何 threshold / archetype）若 PRD 当前尚未正式冻结定义，明确标记为 **IMPLEMENTATION_DESIGN_REQUIRED / NOT YET FROZEN**，由后续 Discovery Product Design 在真实数据回放基础上确定。

### 8.1 A类：表面表现与内部质量偏差 — IMPLEMENTATION_REDESIGN_REQUIRED

> **2026-08-12（LEGACY IMPLEMENTATION REFERENCE / NOT V2 TARGET SPEC）**：A 类条件原以 `P.value / P.historyPercentile120d / Q.delta1d / U.delta1d` 表达，依赖已废弃的 P/Q/U/C/V first-layer。这是 legacy 实现历史说明，不得作为新实现要求。新 V2 是否仍有 A 类条件、条件形态如何，属于 NOT YET FROZEN 的 Discovery Product Design，本 PRD 不定义新阈值。

**A1 surface_strong_internal_weak**

初始条件（legacy P/Q/U/C/V 表达，REDESIGN REQUIRED）：

```
P.historyPercentile120d >= 70
(P.value - Q.value) 的自身历史分位 >= 90
Q.delta1d <= 0 或 U.delta1d <= 0
coverage >= 0.95
```

**A2 surface_weak_internal_improving**

初始条件（legacy P/Q/U/C/V 表达，REDESIGN REQUIRED）：

```
P.historyPercentile120d <= 40
Q.delta1d 的历史分位 >= 70
U.delta1d 的历史分位 >= 60
coverage >= 0.95
```

### 8.2 B类：当前状态与变化速度偏差 — IMPLEMENTATION_REDESIGN_REQUIRED

> **2026-08-12（LEGACY IMPLEMENTATION REFERENCE / NOT V2 TARGET SPEC）**：B 类依赖 P/Q/U/C/V 历史分位与 1 日变化分位，是 legacy 实现历史说明，不得作为新实现要求。新 V2 是否仍有 B 类条件属于 NOT YET FROZEN 的 Discovery Product Design，本 PRD 不定义新阈值。

**B1 high_level_slowing**

```
P/Q/U/V中至少2项历史分位>=70
Q/U/V中至少2项1日变化分位<=30
```

**B2 low_level_repair**

```
P/Q/U中至少2项历史分位<=40
Q与U的1日变化分位>=70
结构破坏扩散率不再继续上升
```

### 8.3 C类：成交、参与与集中度偏差 — IMPLEMENTATION_REDESIGN_REQUIRED

> **2026-08-12（LEGACY IMPLEMENTATION REFERENCE / NOT V2 TARGET SPEC）**：C 类依赖 V/U/C 分位是 legacy 实现历史说明，不得作为新实现要求。新 V2 是否仍有 C 类条件属于 NOT YET FROZEN 的 Discovery Product Design，本 PRD 不定义新阈值。

**C1 volume_without_breadth**

```
V历史分位>=70或V变化分位>=70
U变化分位<=40
C历史分位>=70或C继续上升
```

**C2 breadth_without_volume**

```
U变化分位>=70
V历史分位<=50或V变化分位<=50
```

**C3 synchronized_expansion**

```
U变化分位>=70
V变化分位>=70
C未处于异常高位或未继续上升
```

### 8.4 D类：第二金字塔 Evidence Family

> D 族只在 industry/concept scope 评估（需 pyramid_v2 数据）；market/major_index/style scope 无 board_analysis，D 族不命中。D 族输出的是 Discovery Evidence，不是独立用户 Finding。

**D1 state_migration_positive**

```
positive_migration_count >= 5
positive_ratio >= 0.6
negative_migration_count <= positive_migration_count
```

**D2 event_freshness_high**

```
decay_weighted_density >= 0.3
today_count >= 1 或 last_5d_count >= 3
```

**D3 breadth_expansion**

```
participation_coverage >= 0.3
total_migration_count >= 5
```

**D4 concentration_high**

```
hhi >= 0.1 或 top5_contribution >= 0.4
leader_median_gap > 0
```

> **Concentration 语义校正（2026-08-11）**：`concentration_high` 是 **State**，不是 **Anomaly**。必须区分：`concentration_state_high`（背景状态）/ `concentration_rising`（Change）/ `concentration_abnormal`（Anomaly）/ `concentration_broadening`（Change，反向）/ `concentration_narrowing`（Change，反向）。用户 Discovery 优先消费 Change/Anomaly 变体。

**D5 relative_strength_strong**

```
vs_market.ratio >= 1.1
equal_weight_diff > 0
```

### 8.5 Discovery 排序

Discovery 必须进行**全量排序后再分页**。

禁止：`DB LIMIT 50 → 再在这 50 条中排序`。

正确逻辑：`全部 eligible discovery → 统一 rank → Top N → pagination`。

排名必须可解释，不生成不可追溯黑箱总分。排序至少考虑：异常程度 / 变化强度 / 参与宽度 / 证据一致性 / 持续时间 / coverage / cross-scope confirmation。具体算法权重另由算法版本控制。`rank_key` 必须把上述分项保存下来。

**已废弃**：`scope_type` 固定优先级作为排序键（平行发现后 scope family 平等）。

---

## 9. 归因（Attribution）与 Cross-Scope Relation

筛选器只负责发现 evidence，归因负责解释。

Attribution 正式区分三层业务语义：

### 9.1 ATTR-1：Taxonomy Hierarchical Attribution

用于 Industry taxonomy 内部的层级贡献（L1 ↔ L2、L2 ↔ L3）。回答：一个行业范围内部，哪些下级 taxonomy scope 对上级状态 / 变化产生主要贡献。

对每个 Discovery：识别相关下级 taxonomy scope；计算下级 scope 对上级 Scope Observation facts 变化的贡献（Return Level/Distribution、State+Breadth、Member State Migration、Concentration 等；不再以 P/Q/U/C/V score 为贡献对象）；保留正贡献和负贡献；按绝对贡献排序；保存前 N 项，API 支持分页读取全部。

归因不得仅按涨幅排序。注意：这是 attribution，不是 discovery gate。不得恢复「L1 命中 → 才允许扫描 L2/L3」。

### 9.2 ATTR-2：Member Attribution

回答某个 Scope / Discovery 内：哪些 instrument 是主要贡献成员。至少可表达：

- **Observation contribution**（对 Return Level / State+Breadth / Concentration / Participation 等 Observation facts 的贡献；不再以 P/Q/U/C/V score 为贡献对象）；
- board/scope role（core / second_line / elasticity / follower / laggard）；
- relation to scope；
- fresh event evidence；
- contributionPayload / roleEvidence。

PRD 定义业务合同，具体 ranking weight 由算法版本控制。每只成员计算：对 Return Level/Distribution 的表面变化贡献；对 Trend/Structure/Momentum State+Breadth 与 Member State Migration 的贡献；对 Participation 的参与确认；对 Price/Amount Concentration 的集中度贡献；对 Volume/Amount Participation 的成交贡献；新鲜结构 / 动量事件；与板块状态的关系。角色分类与因子状态分开保存，必须保留 role_evidence。

### 9.3 ATTR-3：Cross-Scope Relation

平行扫描完成后，增加独立的 **Cross-Scope Relation** 阶段。该阶段不是重新计算第一金字塔，而是比较各 Scope Discovery 的：成员交集 / Scope Observation facts（PRICE / State+Breadth / Member State Migration / Participation）/ 变化方向 / 异常强度 / 结构事件 / 扩散程度 / 代表股票。目标是识别：今天不同分类体系是否在描述同一股市场资金行为。

#### 第一阶段支持的 Relation Type

至少支持：`concept ↔ industry`、`concept ↔ concept`、`concept ↔ style`、`industry ↔ style`、`industry ↔ industry`。

#### Relation 输出语义

禁止输出模糊「相关」。至少区分：THEME_LED / INDUSTRY_LED / BROAD_CONFIRMATION / ISOLATED_THEME / STYLE_LED / CONFLICTING。

#### Relation 数据来源

第一阶段只使用已有结构化事实：membership overlap、price、first pyramid、Scope Observation facts、pyramid_v2、history。

> **2026-08-12**：`P/Q/U/C/V` 已从 Relation 数据来源中移除，替换为 Scope Observation facts。

不得依赖新闻、研报、公告语义或 LLM 推理作为 Relation 成立的必要条件。Relation 是 Discovery 后的关系解释，不是 Scope 之间新的计算 gate。

---

## 10. State / Change / Anomaly 分离

这是 Review Discovery 的 P0 原则。

> **2026-08-13 Observation Model 收口（v2.2 重定向）**：State / Change / Anomaly 不再定义为 P/Q/U/C/V score 的变化，改以结构化 Observation facts 表达（§7）：
> - **State** = 当前 L1 Scope Fact 客观事实（如 Price Breadth、Trend/Structure/Momentum State Member Ratio、Concentration、Participation distribution）；
> - **Change** = 两类客观变化：
>   1. **exact canonical T-1 → T member State Migration**（Member State Migration Evidence，§7.14）；
>   2. **同一 Observation fact 在 historical window 上的连续数值变化**（Historical Dynamics Position/Velocity/Acceleration，§7.9；旧 D1/D3/D5 连续差值不再作为目标核心时序表达，标 `SUPERSEDED_AS_TARGET`）。
>   不得存在独立的 diffusion state；所谓「扩散 / 收缩」是 State/Breadth 跨期连续变化的解释性语言，不是 underlying observation primitive。
> - **Anomaly** = 当前事实或变化相对于 **自身历史** 或 **same-family comparable peer cohort** 的相对位置。
> **不设计新的 anomaly score**；具体统计公式如尚未验证，DEFER 到 algorithm implementation。

### 10.1 State（状态）

State 描述：现在是什么样。例如：集中度高、趋势向上、量能活跃、价格处于高位、内部结构较强。State 可以作为证据，但静态 State 不得默认生成用户可见 Discovery。

### 10.2 Change（变化）

Change 描述：今天相对昨天发生了什么。例如：集中度快速上升（Concentration 的 Historical Dynamics 连续变化）、参与度扩张（Participation distribution 跨期连续变化）、动量增强成员增加（Momentum State Member Ratio 跨期连续变化）、龙头与跟随开始同步（Member State Migration 跨期变化）。Change 可以形成 Discovery Candidate。上述「扩张 / 上升」均指 L2 / Analysis 的连续数值变化，不得离散化为独立 diffusion state。

### 10.3 Anomaly（异常）

Anomaly 描述：这个变化相对自身历史或同类范围是否异常。最低应允许以下比较维度：1D change、5D change、self historical percentile、same-day cross-sectional percentile。

### 10.4 Discovery 成立条件

一个用户可见 Discovery 原则上应至少包含：State + Change，或 State + Historical/Cross-sectional Anomaly。而不是只有 State。

---

## 10A. Signal 与 Discovery 分层

### 10A.1 历史定义（Legacy Compatibility，非 V2 强制架构）

> **2026-08-12（Round 2C-1 降级）**：以下 Signal / Discovery 分层定义属于 legacy implementation compatibility。V2 当前只正式定义 Discovery = user-level market finding，且必须能够追溯到 L1 Scope Facts / L2 Observation Groups / Analysis。以下全部 NOT YET FROZEN：Discovery 是否必须由 Signal 聚合、Signal 是否必须存在、Signal 是否由 Filter 产生、Filter 是否存在、Evidence aggregation topology。现有 `MarketReviewSignal` 可继续兼容，但不得反向约束新 V2 architecture。

- **Signal（legacy）** = atomic evidence：legacy Filter Engine 命中的单条技术信号；
- **Discovery = user-level market finding**（用户级市场发现）：可追溯到 L1/L2/Analysis 的用户可理解市场发现。

### 10A.2 关系（Legacy 说明）

在 legacy 实现中，一个 Scope 可同时命中多个内部 Signal，legacy 用户侧聚合成一个 Discovery。下钻后才能查看哪些 filter 命中、哪些 metric 贡献、哪些 component 支持、哪些股票贡献。

### 10A.3 Discovery Domain Object（历史提案 / PRODUCT DESIGN INPUT / NOT CURRENT V2 TARGET SPEC）

> **2026-08-12（Round 2C-1 follow-up）**：以下为历史 Discovery Domain Proposal，非当前冻结的 schema / domain contract。后续 Discovery 产品设计可以参考、修改或完全放弃本结构；本结构不构成 V2 target requirement。

历史提案中曾将 Review domain 表达为：

```
Market Review
├─ Scope Observations
├─ Signals（atomic evidence）
├─ Discoveries（user-level finding）
├─ Cross-Scope Relations
├─ Attribution
└─ Tracking
```

这是历史逻辑 / domain ownership 提案，不是当前强制物理 storage topology，也不得作为 V2 target contract。

### 10A.4 历史兼容（Legacy Implementation Compatibility / NOT V2 TARGET REQUIREMENT）

> **2026-08-12（Round 2C-1 follow-up）**：本节为 legacy implementation compatibility，不构成 V2 target requirement。

- 原有 `MarketReviewSignal` 和 A/B/C/D filter family可继续存在于 legacy runtime；
- 是否成为未来 Discovery 的输入尚未决定（NOT YET FROZEN），不要求 V2 consume 它们；
- 迁移优先采用 additive 而非 destructive，但不要求立即执行任何 cleanup；
- 后续 Discovery Product Design 冻结后，再决定 legacy signal schema 的去留。

---

## 10B. 信号生命周期与追踪状态机（LEGACY IMPLEMENTATION COMPATIBILITY / NOT V2 TARGET REQUIREMENT）

> **2026-08-12（Round 2C-1 follow-up）**：本节描述的 Signal lifecycle 状态机属于 legacy implementation compatibility，不得作为未来 Discovery 生命周期的默认模板。Discovery identity / schema / lifecycle 的具体实现继续 NOT YET FROZEN，待 Discovery Product Design 冻结后决定。

### 10B.1 系统信号（legacy）

```
new → continuing → confirmed → weakened → invalidated → transformed
```

规则（legacy）：同一 scope 同一 signal_type 连续命中 continuing；达到 filter 配置中的确认条件 confirmed；偏差减弱但尚未失效 weakened；达到失效条件 invalidated；转为另一信号类型旧信号 transformed 并关联新信号。禁止前端根据颜色自行判断状态。

### 10B.2 用户追踪

Tracking target 必须支持：Discovery（使用 Discovery logical identity）、Scope、Instrument。Legacy Signal tracking 可以兼容保留。每天 Review Run 完成后自动生成 evaluation。用户关闭追踪不删除历史。实现阶段必须采用 additive-compatible 方式支持 Discovery tracking。具体 schema/migration DEFER 到实现阶段。

---

## 11. 任务编排与发布

盘后顺序（平行扫描模型）：

```
stock_core published
→ board_analysis published
→ create market_review_run
→ compute ALL scope L1 Scope Facts 并行（market / major_index/* / style/* / industry_l1/* / industry_l2/* / industry_l3/* / concept/*）
→ persist L1 Scope Facts
→ organize L2 Observation Groups（8 组）
→ [Discovery consumer path — NOT YET FROZEN]
→ compute Cross-Scope Relations
→ compute attributions + representative instruments
→ evaluate active trackings
→ quality gate
→ publish review pointer
```

> **2026-08-13（v2.3 重定向）**：上述 target behavior 中 `persist L1 Scope Facts` 与 `organize L2 Observation Groups` 已正式冻结为 v2.3 目标合同；`[Discovery consumer path]` 未冻结，实现阶段不得假装中间 Discovery algorithm（Filter / Signal / 聚合）已经冻结，不得强制 evaluate filters → generate Signal → aggregate Discovery 为必经步骤。

要求：

- 每个 scope 独立 item、短事务、可恢复；
- 一个 scope 失败不回滚其他 scope；
- 重启只处理 pending / 可重试 failed / 过期 running；
- 相同输入 hash 和版本的 succeeded item 不得重算；
- Attribution 幂等（若 Attribution 属已确认目标，可保留其幂等要求）；
- Signal 幂等仅属于 legacy path compatibility，不作为 V2 future architecture requirement；
- pointer 切换失败只重试发布，不重算；
- 依赖按矩阵解析：`stock_core` 是必需依赖，板块依赖缺失时允许明确的 `core_only` 降级；run 元数据必须记录每项来源 pointer/run、解析方式与降级原因；
- 指标同时保留 raw 与 normalized；历史读取必须满足 `observation.trade_date < run.trade_date`，并按算法版本、scope 类型、scope key 及兼容版本隔离，禁止未来数据和跨范围污染；
- run coverage 表示底层有效样本覆盖率，不能用成功 scope 数比例冒充；
- quality gate、Review pointer upsert 与 run published 状态在同一调用方事务内完成；任一步失败必须回滚并保留旧 pointer。

### 11.1 发布门禁

本节与 §6.5「Review MVP 发布就绪门禁（Phase 4C 校正）」构成同一份合同。

> **2026-08-13 Observation Model 收口（v2.2 重定向）**：发布门禁就绪性改以 **Scope Observation facts（§7 v2.3）** 表达，不再以 P/Q/U/C/V 五项 `normalized_ready` 作为 first-layer 门禁对象。旧 P/Q/U/C/V gate 引用标记为 legacy baseline / IMPLEMENTATION_REDESIGN_REQUIRED。

单 scope：

- underlying coverage >= 0.95；
- 必要 Observation facts 状态可用（market 至少含 PRICE Return Level/Distribution/Breadth、Trend/Structure/Momentum State Member Ratio 等 CORE facts；具体字段集在 Implementation Design 确定）。

整套 Review（渐进式 scope readiness）：

**1. MANDATORY — market（HARD GATE）**

- `market` scope 必须存在且状态 `ready`，并满足 market coverage/quality 要求（含 §7 CORE Observation facts 就绪；旧 P/Q/U/C/V 五项 `normalized_ready` 为 legacy baseline，映射 DEFER 到 implementation）；
- market missing / not ready / coverage 低于强制门槛 → whole Review publication CLOSED。

**2. PROGRESSIVE OPTIONAL — industry_l1 / major_index / style**

- 真实就绪 → 正常参与产品输出；
- PIT unavailable / `insufficient_history` / `blocked_external_population` / `bootstrap_unavailable` / skipped → 记录为 scope-level diagnostic / unavailable，保留真实状态；
- 上述 optional unavailable 不得阻塞 whole Review MVP publication；
- 禁止把 optional scope 状态伪装成 `ready`。

**3. PARALLEL SCOPES — industry_l2 / industry_l3 / concept**

- 各自独立 readiness，不阻塞其他 scope；
- 真实就绪 → 正常参与 Discovery；
- 不可用 → 记录诊断，不影响其他 scope 的 Discovery 发布。

**4. UNEXPECTED EXECUTION FAILURE 仍然阻塞**

- 任何 scope（含 optional / parallel）出现非预期执行失败或非终态 → whole Review publication CLOSED；
- optional/parallel 语义只豁免「数据源不可用」，不豁免「执行异常」。

**5. 数据来源硬约束**

- 禁止 current membership × historical date 回填；
- 禁止 latest snapshot backfill / forward-fill 冒充 PIT 成员。

**6. 其他整套条件（不变）**

- **[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]** `source_core_run_id` 由 AfterClose 编排以显式 Core `snapshot_run_id`（StockFeatureSnapshotRun.id）直接绑定，**不再经由 `stock_core` FactorPublication pointer 解析或回退**；`source_board_run_id` 恒为 null（board/market_aggregation 已退役，见 [Slice 4A9]）。Review 创建/发布门禁不查询 stock_core publication pointer。

> **2026-08-12（Round 2C-1 follow-up）**：旧发布门禁中的「signal evaluation 无系统性异常」从 V2 target publication gate 中移除。若现有 legacy runtime 仍依赖 signal evaluation，该条件仅作为 Legacy compatibility runtime condition，不构成 V2 future gate。本轮不发明新的 Discovery gate。

> **[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]** Review 发布门禁（`evaluate_publish_gate`）直接通过 `session.get(StockFeatureSnapshotRun, source_core_run_id)` 校验 CoreRun 完整性（run 存在 + trade_date 一致 + status == succeeded），**不比较 `stock_core` publication `data_run_id`**，也不要求 stock_core pointer 已存在/已发布。历史 observations 的 First Pyramid History 生产不在 Review 创建/启动前作为 prerequisite。

---

## 12. API合同

统一前缀：`/api/v1/review`

### 12.1 日期与总览

```
GET /api/v1/review/dates
GET /api/v1/review/latest
GET /api/v1/review/{trade_date}/overview
```

overview 返回 reviewRunId / tradeDate / status / sourceCoreRunId / sourceBoardRunId / algorithmVersion / baselineWindow / coverage / discoverySummary / signalSummary（legacy evidence diagnostics）。

### 12.2 市场扫描

```
GET /api/v1/review/{trade_date}/scopes
```

参数：scope_type / scope_family / sort / page / page_size / include_partial=false。

返回每个范围的 **Scope Observation facts（§7 v2.3：PRICE / TREND / STRUCTURE / MOMENTUM / VOLUME）**、L2 Observation Groups（8 组）、Analysis（Cross-sectional / Historical Dynamics / Internal Structure）与命中数量。（旧 P/Q/U/C/V 聚合变量不作为 first-layer observation 返回。）

### 12.3 信号（Signal = atomic evidence）

```
GET /api/v1/review/{trade_date}/signals
GET /api/v1/review/signals/{signal_id}
```

Signal endpoint 定位为 evidence / debug / drilldown。保留用于兼容和算法调试，不作为用户一级发现入口。

### 12.3A Discovery（用户一级发现入口，新增）

```
GET /api/v1/review/{trade_date}/discoveries
GET /api/v1/review/discoveries/{discovery_id}
```

Discovery 是 primary user-level finding endpoint。筛选参数：scope_type / scope_family / status / sort（按 rank_key 排序）/ page / page_size。

Discovery detail 必须返回或可导航到：scope / state（当前状态摘要）/ change（1D / 5D 变化）/ anomaly（historical / cross-sectional percentile）/ keyEvidence / relatedScopes / representativeInstruments / lifecycle / dataQuality。

Attribution 和 instrument evidence 以 Discovery 为用户入口。前端不得从 Signal endpoint 自行聚合 Discovery。

### 12.4 归因与个股

```
GET /api/v1/review/discoveries/{discovery_id}/attributions
GET /api/v1/review/discoveries/{discovery_id}/instruments
GET /api/v1/review/signals/{signal_id}/attributions       # 兼容
GET /api/v1/review/signals/{signal_id}/instruments         # 兼容
```

### 12.5 追踪

```
GET    /api/v1/review/trackings
POST   /api/v1/review/trackings
PATCH  /api/v1/review/trackings/{id}
DELETE /api/v1/review/trackings/{id}   # 实际为关闭，不物理删除
GET    /api/v1/review/trackings/{id}/evaluations
```

### 12.6 管理端

```
POST /api/v1/admin/review/runs
POST /api/v1/admin/review/runs/{id}/resume
POST /api/v1/admin/review/runs/{id}/publish
GET  /api/v1/admin/review/runs/{id}/status
```

所有写操作要求幂等键。

---

## 13. 前端目录与组件

建议目录：

```
frontend/src/features/review/
  api.ts
  types.ts
  queryKeys.ts
  urlState.ts
  ReviewHeader.tsx
  ScopeBrowser.tsx           # Scope Family 平行切换浏览器
  DiscoveryList.tsx           # Discovery 列表
  DiscoveryCard.tsx           # 单条 Discovery 卡片
  DiscoveryDetail.tsx         # Discovery 详情页
  ScopeDetailPanel.tsx        # Scope 详情面板（L1 / L2 / Analysis）
  InternalStructurePanel.tsx  # 内部结构展示（Breadth/Capital Tilt/Concentration/Leadership Migration）
  DynamicsPhasePanel.tsx      # Interpretation 展示（Dynamics Phase × Internal Structure Type）
  TradingContextPanel.tsx     # Trading Context 展示
  PyramidV2Panel.tsx          # Pyramid V2 面板
  CrossScopeRelationPanel.tsx # Cross-Scope Relation 面板
  InstrumentEvidencePanel.tsx # 个股证据面板
  TrackingPanel.tsx           # 追踪面板
  EvidenceDrawer.tsx          # 结构化证据抽屉
  ScopeMetricsTable.tsx
  AttributionTable.tsx
  ReviewInstrumentTable.tsx
  ReviewDataQualityBadge.tsx
  review.module.scss

frontend/src/pages/ReviewPage.tsx
```

现有 `BoardAnalysisPage.tsx` 不删除。应抽取可复用的 BoardMetricsSummary / BoardDistributionPanel / BoardEventDistribution 供板块分析页和复盘归因阶段共同使用，禁止复制两套计算和展示逻辑。

---

## 14. 页面信息架构（Discovery Workspace）

**已废弃**：旧五阶段 UI（市场扫描 / 筛选发现 / 板块归因 / 个股验证 / 追踪复核）不再作为用户信息架构的强制要求。

新用户主路径调整为**市场结构工作台**语义：

```
市场发现 / 今日结构
    ↓
异常发现 / Discovery Workspace
    ↓
Discovery 详情（Scope + Evidence + Relation + Instruments）
    ↓
我的追踪
```

后台仍然可以保留 `scope / filter / signal / attribution / tracking` 作为内部 domain object，但用户不需要理解系统执行了几个 pipeline phase。

### 14.1 固定顶部

展示：交易日与前后交易日；Review 发布状态；Core/Board Run；覆盖率；算法版本、筛选器版本、历史基线；数据质量入口。顶部不得显示 AI 自由生成的市场结论。

### 14.2 Scope 浏览器

Scope Family 必须允许平行切换：全市场 / 主要指数 / 风格 / 行业 / 概念。Industry 内再选择 L1 / L2 / L3（仅浏览维度，不是 discovery gate）。不得重新引入「先选择 Industry → 才能看 Concept」的隐式 gate。

### 14.3 市场发现首页

首页首要回答：**今天市场发生了什么？** 建议最小结构：今日市场状态、主要发现（含 Dynamics Phase / Internal Structure Type / Trading Context 提示）。不得首先展示 A/B/C/D 分类。

### 14.4 Discovery 详情必需信息

每一个 Discovery 至少必须能下钻看到：

#### Scope
- family / type / name / members / coverage

#### Current State
- **Scope Observation facts（§7 v2.3）**：PRICE（Return Level / Distribution / Breadth / Concentration）、Trend/Structure/Momentum State Member Ratio、Participation distribution（旧 P/Q/U/C/V 不作为 first-layer observation 展示；如需 summary 属 presentation layer）

#### Change
- Member State Migration（member exact T-1 → T 状态迁移）ratio、1D / 5D observation change

#### Position
- Historical Dynamics Position（historical percentile）
- Cross-sectional percentile

#### Internal Structure
- 应尽可能消费现有后端 component：trend breadth / structure breadth / momentum breadth / synchronized improvement / structure breakdown / non-leader participation / HHI / Top5 contribution / volume expansion / Capital Tilt / Leadership Migration

#### Pyramid V2
若 scope 可用：migration / freshness / diffusion（legacy evidence family）/ concentration / relative strength

#### Related Scopes
Cross-Scope Relation（THEME_LED / INDUSTRY_LED / BROAD_CONFIRMATION 等）

#### Representative Instruments
- first pyramid / fresh events / contribution payload / role（core / second_line / elasticity / follower / laggard）/ role evidence / relation to scope

### 14.5 Evidence Drawer（结构化证据解释器）

Evidence Drawer 是结构化证据解释器，不是 JSON Debugger。正式用户页面禁止直接将 `JSON.stringify(payload)` 作为主要展示。Raw JSON 仅允许 admin/debug mode。普通用户必须转换为结构化展示：metric / value / change / percentile / denominator / coverage / source / component / trigger reason / member contribution。

### 14.6 个股证据

个股下钻必须展示：First Pyramid / Fresh Events / Observation contribution（contributionPayload：对 Return Level / State+Breadth / Concentration / Participation 等 Observation facts 的贡献；旧 P/Q/U/C/V contribution 为 legacy baseline）/ Board Role / Role Evidence / Relation To Scope。不得只展示单一 `contributionValue`。个股「为什么重要」必须可解释。

### 14.7 追踪

内部子 Tab：过去发现 / 自选映射 / 事件演化。「过去发现」字段：首次日期 / Discovery / 范围 / 当前状态 / 连续天数 / 状态变化 / 后续证据。

---

## 15. 前端数据与状态规则

- 使用 React Query；
- query key 必须包含 reviewRunId / tradeDate / resource / id / filters；
- 已发布历史复盘使用较长 staleTime，不每 30 秒刷新；
- 最新交易日处于 computing 时仅轮询 run status，发布后停止；
- 页面组件不得拼接不同 Review Run；
- 切换 Discovery 时取消无效请求；
- 后端返回 partial / stale / unavailable 时必须显示具体状态；
- 禁止无限「加载中」；请求超时、404、422、500 分别显示明确错误和 request_id。

---

## 16. 与现有页面的边界

### /market

负责：全字段筛选、排序、列设置、导出、自选管理。Review 跳转参数：reviewDiscoveryId / tradeDate / sourceCoreRunId / boardId / firstPyramidFilters / sort。

### /stock/:symbol

负责：K 线、第一金字塔完整详情、事件和筹码状态。Review 只传：from=review / discoveryId / boardId / tradeDate。

### /boards/analysis

保留为板块原始分析和管理 / 研究入口；Review 阶段三复用其组件，不复制业务。

---

## 17. 加载、空态和异常态

必须覆盖：当日 Review 尚未计算；计算中；partial 未发布；已发布但无 Discovery（可下钻查看 signal/evidence diagnostics）；已发布有 Discovery 但无 Signal；scope coverage 不足；历史不足无法计算分位；Discovery 无可归因子范围 / 个股；个股无第一金字塔；用户无复盘权限；API 超时或版本不一致。

用户主空态：「今日无满足当前 Discovery 条件的市场发现」（可下钻查看 signal/evidence diagnostics）。Signal 空态（evidence 层）：「今日未命中已配置偏差筛选器」。

---

## 18. 性能与缓存

- 页面首屏只加载 overview、Discovery 摘要和 scope 摘要；
- 归因和股票列表按需加载；
- 所有长列表服务端分页；
- 不一次返回几千只成员；
- Review 计算读取已发布快照，禁止逐只重新计算第一金字塔；
- 120 日分位应批量计算或预聚合，禁止 N+1；
- Redis 只缓存已发布、不可变的 Review 响应，cache key 包含 review_run_id；
- pointer 切换后旧缓存自然隔离，不做全局 flush。

---

## 19. 测试要求

### 19.1 后端单元测试

- component registry 映射；
- **Scope Observation facts 计算（§7 v2.3：PRICE / TREND / STRUCTURE / MOMENTUM / VOLUME / Internal Structure）**；旧 P/Q/U/C/V 计算为 legacy baseline，映射 DEFER 到 implementation；
- Historical Dynamics Position/Velocity/Acceleration/Persistence 计算；
- Dynamics Phase / Internal Structure Type 分类可解释性；
- State / Change / Anomaly 分离；
- Signal → Discovery 聚合（legacy）；
- A/B/C/D 各 Evidence 正反例（含 D concentration state/change/anomaly 语义）；
- Signal 生命周期；
- Discovery 生命周期；
- Cross-Scope Relation 计算与 relation type 分类；
- ATTR-1 / ATTR-2 / ATTR-3；
- global rank before pagination；
- tracking 状态机；
- 模板化解释。

### 19.2 PostgreSQL集成测试

不得 skip：migration upgrade/downgrade/upgrade；run/item 并发 claim；相同输入幂等；Signal 唯一约束；Discovery identity / idempotency（若实现选择 persistence）；Signal ↔ Discovery evidence lineage；pointer 不混 run；published 与 partial 隔离；Discovery pagination / ranking；attribution 和 instrument 分页；tracking evaluation 逐日唯一；用户权限隔离。

### 19.3 前端目标测试

- URL hydration 与前进 / 后退；
- Scope Browser 平行导航（无 Industry→Concept gate）；
- Discovery 列表与详情；
- Discovery evidence drilldown；
- Cross-Scope Relation 展示；
- representative instrument evidence；
- 追踪面板；
- 结构化 Evidence Drawer；
- 加载 / 空态 / degraded / 错误状态；
- 个股跳转参数；
- 加入追踪。

### 19.4 生产 canary

先固定：全市场 / 2 个主要指数 / 2 个风格范围 / 5 个一级行业 / 5 个概念 / 3 个二级行业 / 3 个三级行业。

验证：Scope Observation facts 值可复算（§7 v2.3）；至少一条正向和一条风险 Discovery；Concept 独立产生 Discovery；Cross-Scope Relation 可生成；下钻路径和成员归因一致；/market 与 /stock 跳转正确；次日 tracking 状态可重复计算。

---

## 20. 验收标准与场景

### 20.1 验收场景

#### Case 1 — Concept 独立发现（京东方 / 玻璃基板）

假设：京东方属于显示面板行业，同时属于玻璃基板 Concept；显示面板行业无明显异常；玻璃基板 Concept 出现可解释的结构改善 Evidence（PRICE Breadth 改善；TREND / STRUCTURE / MOMENTUM 的 State Member Ratio / Member State Migration 改善；PARTICIPATION 改善）。PRD 必须保证：玻璃基板可以独立被发现，不得因为显示面板没命中而漏掉。

#### Case 2 — 小行业局部强

假设：Industry L1 整体普通，某 Industry L2/L3 显著改善。PRD 必须保证：L2/L3 可以独立产生 Discovery。

#### Case 3 — 高集中但长期如此

假设：HHI 高、Top5 contribution 高，但与历史相比无明显变化。不得仅因为 `concentration_high` 生成高价值 Discovery。

#### Case 4 — 集中度快速恶化

假设：PRICE / Trend breadth 收缩，或内部参与减弱；PARTICIPATION 弱化（成交 / 参与向少数 leader 集中）；Price / Amount Concentration（HHI、Top5 contribution）上升；leader-median gap 扩大。应能形成「行情向少数龙头收缩」类 Discovery。方向语义仅定义方向，不定义具体 numeric threshold，不发明新的 HHI normalization。

#### Case 5 — 多轴共振

Industry + Concept + Style 同时改善。不得生成三个互不相关的重复 Finding，应允许形成 BROAD_CONFIRMATION。

#### Case 6 — Theme Led

Concept 强、Industry 普通。应允许输出 THEME_LED。

#### Case 7 — Conflict

Concept 表面 Observation 很强（PRICE Breadth / TREND State Member Ratio 高），但 Industry L1 的 TREND/STRUCTURE/MOMENTUM State Member Ratio 与 PARTICIPATION 恶化。不得强行合并成 bullish conclusion，应保留 CONFLICTING relation。

### 20.2 完整验收标准

完整验收必须满足：所有 Scope Family 独立平行参与 Discovery；Concept / L2 / L3 不受 Industry L1 的 discovery gate；前端没有 Scope Observation facts 或筛选器计算代码（旧 P/Q/U/C/V 亦不计算）；同一页面不混合不同 run；Discovery 可下钻到子范围、Cross-Scope Relation 和股票；个股第一金字塔与板块关系可解释（含 contributionPayload 和 roleEvidence）；Discovery 可保存追踪并在下一交易日产生 evaluation；过去发现可显示确认 / 持续 / 减弱 / 失效 / 转化；coverage、历史不足和 partial 不被伪装成完成；Evidence Drawer 展示结构化证据，非 Raw JSON；全量 rank → paginate，非 paginate → rank；真实登录浏览器完成 URL、页面、Console 和 Network 验收。

---

## 21. 文档与记忆系统

必须更新：

- `docs/prd/70-review.md`（本文档）
- `docs/maps/70-review.md`（真实调用链、表、API、组件）
- `docs/prd/30-after-close.md`（Core→Board→Review 编排）
- `docs/maps/30-after-close.md`（pointer 和 run 关系）
- `docs/maps/40-market-stock-experience.md`（Review→Market→Stock 跳转合同）
- `docs/runbooks/after-close-remote-development-run.md`（review canary/resume/publish）
- `rules/40-testing-quality.md`（TQ-97 页面验收三类证据、TQ-98 成功判定三要素）

保持：docs/current 只读；不创建 reports；不新增重复治理目录；AGENTS.md 只保留入口，不扩写业务细节。

---

## 22. 推荐实施顺序（Discovery Model Refactor）

**DONE（2026-08-12 已验收，作为 CURRENT IMPLEMENTATION BASELINE）：**

- Canonical Observation Core（Scope Observation Model，Round 1A/1B/1C = PASS，旧 L1/L2）
- Canonical Observation Fact Persistence（`review_scope_observation_facts`，trade_date + scope_type + scope_key）
- L2 Objective Evidence Engine（CURRENT / D1 / D3 / D5 / HISTORICAL POSITION / PEER POSITION，Round 2A = PASS，**SUPERSEDED_AS_TARGET**）

**NEXT（Product Design Question，非 Implementation Task）：**

> **2026-08-13（v2.3 重定向）**：当前下一产品阶段应表述为 **「Scope Observation v2.3 Final Product Contract 落地：L1/L2/Analysis/Interpretation/Trading Context/Member Attribution 实现重对齐」**。v2.3 产品链（§2 / §7）已冻结为 FINAL PRODUCT CONTRACT，不再处于 NOT YET FROZEN 状态；未冻结的仅是 Discovery consumer path（Filter/Signal 聚合拓扑）与 exact algorithm mapping（Dynamics Phase threshold 等，标 ALGORITHM MAPPING REQUIRED）。

- v2.3 L1/L2/Analysis 实现重对齐（按 §7.17 已知实现影响）
- Interpretation / Trading Context / Member Attribution 前端与算法映射
- Cross-Scope Relation / Attribution（可独立于 Discovery 设计推进）
- API / Frontend cutover（Discovery Workspace / Evidence Drawer / Representative Instruments）
- Legacy P/Q/U/C/V Filter / Discovery cleanup（待新路径验收后）

> **>>> HISTORICAL ROADMAP / SUPERSEDED AS CURRENT EXECUTION PLAN（2026-08-12 Round 2C-1 follow-up）**
> 以下 P0-A / P0-B / P0-C / P1 / Phase 5 为历史 roadmap，仅保存历史上下文，不再是当前 NEXT。其中凡涉及 Filter / Signal / Signal→Discovery 聚合的部分，必须等待 Discovery Product Design 决策后重新确认，不得作为当前 V2 执行计划。IDE 不得从该历史 roadmap 自动生成开发任务。

**P0-A：Scope 平行化 + A/B/C Corrective + 排序修复（HISTORICAL）**

- 扩展 scope scanning 到所有 scope family 平行计算
- A/B/C history context 闭环（CR-01）
- Global ranking before pagination（CR-02）
- Frontend/API contract alignment（CR-03/CR-04）

**P0-B：Discovery Domain + State/Change/Anomaly（HISTORICAL）**

- State/Change/Anomaly 分离（已部分冻结，v2.2 §10 重定向）
- Concentration 语义校正（state vs change vs anomaly）

**P0-C：Cross-Scope Relation（HISTORICAL）**

- THEME_LED / INDUSTRY_LED / BROAD_CONFIRMATION / ISOLATED_THEME / STYLE_LED / CONFLICTING

**P1：前端 Discovery Workspace 重构**

- Scope Browser 平行切换 / Discovery 列表 / Discovery 详情页 / Evidence Drawer / 追踪面板

**Phase 5：历史回放与阈值校准（HISTORICAL / LEGACY FILTER ROADMAP）**

- 使用历史 Review Run 验证筛选器稳定性（Filter 是否存在 NOT YET FROZEN）

---

## 23. P0 强化条款（review-1.1.0）

> 本章节为 review-1.1.0 算法版本（CHANGE-20260730-014）追加的强制条款，是对旧 §7（P/Q/U/C/V 指标合同）、§11（任务编排与发布）、§6（Scope Discovery 模型）的补强。本章节条款优先级高于历史 §7/§11 的 **P/Q/U/C/V legacy baseline** 冲突描述。
>
> **2026-08-13 当前 authoritative publication contract**：当本 §23 legacy gate 与 §6.5 / §11.1 的 2026-08-13 当前 contract（含 industry_l1 / major_index / style 属 PROGRESSIVE OPTIONAL、数据不可用不阻塞 whole Review publication）冲突时，以 §6.5 / §11.1 为当前 authoritative publication contract。本 §23 不得重新覆盖 §6.5 / §11.1 的 progressive readiness 合同。

> **2026-08-13 Observation Model 收口（v2.2 重定向）**：本章节及其后的 §24/§25/§26/§27 中所有 `P/Q/U/C/V` 引用都属于 **legacy implementation baseline**：它们是既有实现 / 历史的 persistence 与 gate 契约，不复活 P/Q/U/C/V 作为 first-layer observation model（§7 v2.3）。这些 legacy 契约与 §7 Scope Observation Model 的映射（gate 就绪对象、就绪字段、schema shape）全部 DEFER 到 Implementation Design。本节不现场重写这些 legacy 契约。

### 23.1 历史原始组件 bootstrap 合同

每个历史日的聚合组件必须按 point-in-time 语义重建，禁止使用未来成员或未来因子：

- **成员 point-in-time**：每个历史日必须使用当日有效成员关系；
- **因子 point-in-time**：每个历史日必须使用当日已发布的 `stock_core` 快照对应的扁平化 99 字段；
- **rawValue 先行**：每个历史日必须先写入组件 `rawValue`；`normalizedValue`、`delta1d`、`delta5d`、`historyPercentile120d`、`crossSectionPercentile` 在未达到 60 个有效观测前必须为 `null`；
- **观测计数**：`historyObservationCount` 必须真实反映已保存的 rawValue 数量；
- **聚合顺序**：达到 60 个观测后才允许计算 `normalizedValue`、P/Q/U/C/V 的 `value` 及 1d/5d 变化与历史分位。

### 23.2 至少 60 日才允许生成 P/Q/U/C/V

任意 P/Q/U/C/V 聚合变量的 `value` 与 `historyPercentile120d` 必须在累计达到 60 个有效历史观测后才能生成；不足 60 日时 `status=insufficient_history`，且相关值必须为 `null`。该合同同时适用于所有 scope family。

### 23.3 canary 不得切正式 market_review pointer

canary review run 必须以 `scope=canary` 显式声明，且只能通过 admin 端 provisional 入口查看，不得写入 `factor_publications`（`publication_kind=market_review`）。canary run 的 `status` 可以为 `published`（仅表示 run 内部计算完成），但 `factor_publications` 表中不得存在对应 `data_run_id` 指针。

### 23.4 完整 Scope 合同（平行扫描）

Discovery 阶段必须独立覆盖以下全部 Scope Family，缺一不可：market / major_index/* / style/* / industry_l1/* / industry_l2/* / industry_l3/* / concept/*。合同要求见原条款（market eligible_count 不小于 4500；major_index 不少于 2 个；style 不少于 2 个；industry_l1 不少于 25 个；industry_l2/l3/concept 独立参与不受 gate）。`scope_key` 命名规范：market 固定；major_index 用 index_code；style 用 style_code；industry_*/concept 用 board_id。

### 23.5 禁止 force 发布不可用数据

`review_publication_service.publish_review(db, run, force=False)` 必须严格执行以下门禁；`force=True` 仅允许 admin 在内部调试时使用，且不得写入 `factor_publications`：

整套 Review 发布门禁（force=False 时强制校验）：

1. **market Canonical Observation facts 就绪（新发布门禁）**：market 范围的 Canonical Scope Observation Facts（`review_scope_observation_facts`，business grain = `trade_date + scope_type + scope_key`）必须已成功计算且 `readiness` 通过；任一 required Canonical Observation fact 为 `null` / `status=insufficient` / `status=not_ready` 拒绝发布。
   > **LEGACY 历史兼容**：旧 `market_review_scope_snapshots.p/q/u/c/v` payload `value` 非空门禁仅作为 legacy implementation compatibility 保留，不得继续作为新发布链路的 hard gate。新门禁以 required Canonical Observation facts readiness + coverage / execution / lineage 现行合同为准。
2. **source_board_run_id 恒为 null**（[Slice 4A9] board 退役；不再作为门禁校验对象）；
3. **source_core_run_id 绑定 CoreRun 完整性（[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01]）**：`session.get(StockFeatureSnapshotRun, source_core_run_id)` 必须存在且 `trade_date == run.trade_date` 且 `status == succeeded`；**不再与 stock_core FactorPublication pointer 的 `data_run_id` 比较，也不要求 stock_core pointer 已发布**。
4. **无 failed signals**；
5. **无 failed run_items**；
6. **coverage_ratio >= 0.95**（market coverage hard gate）。

> **2026-08-13 progressive readiness 对齐（§11.1）**：`industry_l1` / `major_index` / `style` 属于 PROGRESSIVE OPTIONAL；其数据不可用不得阻塞 whole Review publication。

> **[P0 2026-08-04] 无未来数据门（历史基线 point-in-time）**：本 run 正常落库的当日观测（`trade_date == run.trade_date`）是合法行为，不得被当作「未来数据」拦截。门禁只拦截严格未来观测（`trade_date > run.trade_date`）。

force=True 时跳过 1-6 门禁，但必须：不得写入 `factor_publications`；run.status 不得进入 `published`；必须返回 `is_provisional=true` 标记；该 run 永远不得作为普通用户读取入口的正式 pointer。

### 23.5A Review publication withdrawal（撤销正式 pointer）

- 撤销唯一正式入口：`review_publication_service.withdraw_review_publication`（CLI：`python -m app.scripts.withdraw_review_publication`，默认 dry-run，`--apply` 才执行写入）；
- 只删除 `(scope_type=market, scope_key=market, publication_kind=market_review, trade_date=指定日)` 的唯一 pointer，不得触碰其他交易日或其他 publication_kind；
- 保留 review run / scope snapshot / signal / attribution / instrument 全部数据，禁止删除 Review run，禁止裸 SQL；
- withdrawal 只撤销 pointer；被撤销 pointer 指向的 run 的 `status` / `published_at` 和全部子数据是历史审计事实，禁止回退、清空或原地重算；
- 撤销审计必须写入 run.metadata_json["publication_withdrawal"]；
- 幂等：pointer 不存在时不做任何写入，返回 `already_withdrawn`。

### 23.6 history_maps 读取合同

- `metric_engine` 读取历史基线时必须从 `market_review_scope_snapshots` 读取同 `scope_type + scope_key` 的历史记录，禁止从 `board_analysis_snapshots` 或 `factor_publications` 直接拼装；
- 首次运行（无历史数据）时，所有 component `status=insufficient_history`，`historyObservationCount=0`；
- `metric_engine` 中 `history is None` 必须显式映射为 `status=insufficient_history`，禁止抛 `AttributeError` 或被 `try/except` 静默吞掉。

---

## 24. 第二金字塔定义与冷启动（草案补强）

> 本章节为第二金字塔定义的草案补强，明确第二金字塔的维度、聚合口径、P/Q/U/C/V 就绪状态与冷启动 bootstrap 合同。本章节在确认为「已确认」后，优先级高于历史描述中与之冲突的部分（特别是冷启动发布行为）。

### 24.1 第二金字塔维度

第二金字塔（板块级分析层）定义以下六个维度：

| 维度 | 说明 |
|---|---|
| 状态分布（state distribution） | 板块成员第一金字塔状态分布 |
| 状态迁移（state migration） | 板块状态在时间轴上的迁移轨迹（v2.2 重定位为 Member State Migration Evidence，§7.14） |
| 事件新鲜度（event freshness） | 板块新鲜结构 / 动量事件的覆盖与衰减 |
| 宽度（breadth） | 参与成员比例 |
| 集中度（concentration） | 贡献集中度 |
| 相对强度（relative strength） | 板块相对市场 / 指数的强度 |

第二金字塔不生成综合总分。

### 24.2 行业与概念分别聚合

- 行业（industry）和概念（concept）必须分别聚合（SEPARATELY），不得混合计算；
- 行业聚合结果与概念聚合结果各自独立存储与发布；
- 禁止把概念成员混入行业分母，反之亦然。

### 24.3 P/Q/U/C/V 就绪状态合同

每个 P/Q/U/C/V 聚合变量必须返回以下就绪状态字段（legacy baseline）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `raw_ready` | bool | 原始组件值是否已就绪 |
| `normalized_ready` | bool | 归一化值是否已就绪（需累计 ≥60 个有效观测） |
| `insufficient_history` | bool | 是否因历史不足无法归一化 |
| `reason` | string | 具体原因 |

### 24.4 冷启动 bootstrap

- 系统不得强制要求「上线后累计 60 个交易日」才允许发布第二金字塔；
- 必须提供可重复执行的 bootstrap 流程，从已有第一金字塔历史回填第二金字塔历史观测；
- bootstrap 生成的历史观测必须遵循 point-in-time 语义；
- bootstrap 不得伪造 normalized 值或历史分位；
- bootstrap 流程必须幂等且可重放；
- bootstrap 完成后必须在发布元数据中记录 `bootstrap=true` 与回填的观测数量。

### 24.5 fp_segment_change_pct 禁止伪造

- `fp_segment_change_pct` 在数据为空时必须返回 `null`，不得伪造为 0、均值或前值；
- 该字段为空时必须在 `reason` 中明确记录「无可用分段数据」。

---

## 最终原则

- Filter Engine（A/B/C/D）是 **Evidence Engine**（证据引擎，legacy）；
- 第二金字塔是 **Explanation Engine**（解释引擎）；
- 第一金字塔是 **Verification Engine**（验证引擎）；
- Discovery 聚合是 **User Finding Engine**（用户发现引擎）；
- Cross-Scope Relation 是 **Market Understanding Engine**（市场理解引擎）；
- 自选与盘中监控是 **Tracking Engine**（追踪引擎）；
- 历史复核是 **Feedback Engine**（反馈引擎）。

复盘页必须把这七个引擎连接成一个可解释、可下钻、可追踪、可复现的市场结构工作台。

---

## 25. raw与normalized分离 + 冷启动展示 + bootstrap 生效范围（CHANGE-20260801-001）

### RV-25-01 raw 与 normalized 双值分离合同

Review 的每个 P/Q/U/C/V 维度和每个聚合指标必须显式维护两套值（legacy baseline）：

- `raw_value` / `rawValue`：原始聚合值，只要有足够的当日样本即可计算；
- `normalized_value` / `normalizedValue`：相对历史分位或归一化值，只有当有效历史观测 ≥ 60 交易日时才允许非 null。

前端展示规则（ScopeMetricsTable / SignalCard / 五阶段）：rawReady && normalizedReady 展示完整；rawReady && insufficient_history 必须展示 raw + reason，normalized 不展示；上游失败 null/disabled。

### RV-25-02 pointer 日期同步

Review 正式发布 pointer 的 `trade_date` 必须满足：`review.pointer.trade_date == stock_core.pointer.trade_date == board_analysis.pointer.trade_date`。

### RV-25-03 bootstrap 生效范围

- 优先使用历史正式 `stock_core` / `board_analysis` 当日快照做 point-in-time bootstrap；禁止用「今日板块成员」去回补昨天或更早 review 的 `scope_items`；
- 没有历史成员版本的板块明确标为 `bootstrap_unavailable`，不得伪造；
- `review_runs.metadata.bootstrap = true` 必须记录来源 run_id、覆盖交易日范围、真实观测数量、未回填板块清单及原因。

### RV-25-04 五个阶段的冷启动表现

Review 五阶段在 insufficient_history 场景的展示规则（legacy UI 阶段描述，保留）。

---

## 26. Review 计算事实、历史观测与发布终态合同（2026-08-01）

- 当日与历史计算统一使用 `ReviewMemberFact`：instrument identity、日线 OHLC / 真实日收益、rolling position、volume/amount、第一金字塔 canonical 当前 / 前日状态、新鲜事件和权重；
- P 使用真实日收益与价格位置；Q 使用 canonical 趋势 / 结构 / 新鲜事件；U 至少两个维度较前日改善；C 明确 `equal_weight/official_weight/amount_weight`；V 分离 volume 与 amount 并使用同量纲均量比（legacy baseline 描述）；
- 编排采用两遍：先保存 raw/normalized，再按同日同 scope family 计算横截面分位，最后评估 signal；
- `market_review_metric_observations` 保存 component raw、denominator、field source、weight mode、algorithm/input/membership version；
- Bootstrap 默认 dry-run，按 market/index/style/industry/concept 分开处理；缺 PIT 成员时写 `bootstrap_unavailable`；
- force 永远只产生 provisional，不写正式 pointer；旧 run 不原地修改；
- 五阶段 UI 必须区分无信号、无追踪、历史不足、字段缺失和 API 错误；Evidence Drawer 展示来源、分母、权重、版本与 readiness。

---

## 27. Review 依赖矩阵与发布质量硬门（QM-63，2026-08-04）

### RV-27-01 上游依赖矩阵

Review run 创建时必须显式解析上游依赖状态，并把结果固化到 run 记录，禁止用「没查到就当成功」掩盖降级：

| 上游 | 状态 | Review 行为 | 记录 |
|---|---|---|---|
| stock_core | 失败 / 未发布正式 pointer | **阻断**，不得发布 | publish gate blocker |
| 第一金字塔核心字段 | 不完整 | **阻断** | publish gate blocker |
| chip 共识 | 全缺失 / 全失败 / expected 无法确定 | 降级为 **core-only**，仍可生成 | `degraded_reasons=["CHIP_UNAVAILABLE"]` + `chip_coverage` |
| chip 共识 | 部分覆盖 | 生成，标记部分降级 | `degraded_reasons=["CHIP_PARTIAL"]` + `chip_coverage` |
| chip 共识 | 全部覆盖 | 正常生成 | `degraded_reasons=[]` + `chip_coverage` |
| auction 竞价 | 失败 / 不可用 | **默认降级，不阻断** | 由 auction 自身 readiness 表达 |
| 历史基线 | <60 日 | 保留 raw，normalized 不就绪 | `status=insufficient_history` |

> **[DEPRECATED 2026-08-14，PRD75 §23]** 依赖矩阵中 `auction 竞价` 行描述旧 AuctionAnchor 产品的 Review 竞价回流依赖。新 Auction 是次日 9:25 产品、消费 Review 正式 snapshot（Review(t-1) → Auction(t)），Review 不依赖新 Auction，方向见 [PRD75 §14](./75-auction-analysis.md)。该行保留为 legacy 依赖记录。

> **[P0 2026-08-04] chip 覆盖率合同**：chip 无独立 run 记录，只通过 `core_run_id` 挂靠 stock_core。`source_chip_run_id` 恒为 `NULL`，不得把 stock_core 的 run id 写成 source_chip_run_id 冒充 chip run。chip 质量改由真实覆盖率判定。

### RV-27-02 发布质量硬门（在既有门禁基础上新增）

`evaluate_publish_gate` 新增三条硬门：

1. **无未来数据（point-in-time 硬门）**：本 run 落库的 `market_review_metric_observations` 不得存在 `trade_date > run.trade_date` 的严格未来记录；
2. **reason 完整性**：market 范围的 P/Q/U/C/V，凡处于非 ready 状态必须给出非空 `readiness.reason`；
3. **all-null 禁止发布空壳**：market 范围 P/Q/U/C/V 的 `value` 全部为 `None` 时禁止正式发布。

三条硬门均只产出 blocker，不做任何「自动修正」或静默兜底。

### RV-27-03 原子发布与幂等

- publication 记录写入与 current pointer 更新必须在同一事务内完成；
- 失败时保留旧 pointer，新 publication 对普通用户不可见；
- 对已是当前正式 pointer 的 published run 重复发布：返回既有 publication，零写入；
- 已 published 但已非当前正式 pointer 的旧 run：禁止原地重发。

---

## 28. Corrective Requirements（实现修正，非新产品功能）

> **2026-08-13**：本节的 Corrective Requirements 属于 **legacy A/B/C filter 实现** 的修正（依赖 P/Q/U/C/V history context）。因 A/B/C filters 已标记 `IMPLEMENTATION_REDESIGN_REQUIRED`（§8），这些 CR 在 Observation Model 改写后需一并纳入 redesign；不构成对 P/Q/U/C/V first-layer 的复活。

以下项目不定义为新产品功能，而是现有设计的实现修正。

### CR-01 A/B/C History Context 闭环

必须正式生成并注入：P-Q historical percentile / Q delta1d historical percentile / U delta1d historical percentile / V delta1d historical percentile / structure breakdown change / C rising / C anomaly state。否则任何依赖这些字段的 filter 不得被声明为 production-ready。

### CR-02 Global Ranking Before Pagination

所有 Signal / Discovery：`全部 eligible → 统一 rank → Top N → pagination`。禁止 `DB LIMIT 50 → 再排序`。

### CR-03 Frontend/API Contract Alignment

前端必须完整接收后端 DTO（contributionPayload / roleEvidence）。

### CR-04 Signal Payload Rendering

前端读取的数据结构必须与后端真实合同一致。

---

## 29. 不在本次范围

明确不做：

- LLM 自动判断股票炒作逻辑
- 新闻 NLP 自动分类
- 概念可信度模型
- 主营业务真实性评分
- 自动预测涨跌
- 买卖建议 / 组合推荐
- 实时盘中 Review
- 自动交易
- AI 主营业务分类
- 人工真实概念标签
- 新闻 NLP 分类
- 唯一炒作逻辑
- Economic Exposure 数据库
