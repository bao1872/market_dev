# 竞价分析 PRD V1.0

状态：已确认
最后更新：2026-08-06
对应 Map：`../maps/75-auction-analysis.md`
条款前缀：`AU`
需求所有权：竞价分析层的目标行为、锚点合同、分析定义与边界约束

> 本文件是竞价真值、锚点、扫描、聚合、publication、追踪和 Review 回流的唯一需求真源。[`31-after-close-product-closure-v2.1.md`](./31-after-close-product-closure-v2.1.md) 只定义竞价节点与盘后闭环的依赖和 lineage，不替代本文件的双源真值及发布门禁。

> 本文件是竞价分析层的 PRD。竞价分析是一个独立的分析层，不属于第一金字塔或第二金字塔。

## 0. 背景与定位

现有分析栈：

- 第一金字塔（个股）：趋势、结构、动量 + 波动率/可选筹码；
- 第二金字塔（板块）：状态分布、状态迁移、事件新鲜度、宽度、集中度、相对强度；
- P/Q/U/C/V 聚合变量与复盘筛选器（见 `70-review.md`）。

竞价分析是 NEW 分析层，回答："最终竞价价格相对于已确认锚点的位置迁移、历史参与和板块扩散"。
它不替代第一/第二金字塔，也不参与 P/Q/U/C/V 综合分。

## 1. 产品目标与边界

### 1.1 目标

- 记录每个已确认锚点的生命周期（形成→确认→减弱→失效→过期）；
- 追踪最终竞价价格相对锚点的位置迁移；
- 统计历史参与次数（价格测试锚点的次数与结果）；
- 衡量同板块内同类型锚点的扩散度。

### 1.2 不做的内容

- 不生成第三金字塔；
- 不生成综合分或黑箱"机会分"；
- 不预测次日涨跌；
- 不在前端计算锚点或迁移结论。

## 2. 在分析栈中的位置

| 层级 | 职责 | 综合分 |
|---|---|---|
| 第一金字塔（个股） | 趋势/结构/动量 + 波动率/可选筹码 | 无总分 |
| 第二金字塔（板块） | 状态分布/迁移/事件新鲜度/宽度/集中度/相对强度 | 无总分 |
| 竞价分析（NEW） | 锚点位置迁移/历史参与/板块扩散 | 无总分、无第三金字塔 |

约束要点：

- 第一金字塔保留：趋势/结构/动量 + 波动率/可选筹码，无总分；
- 第二金字塔：行业和概念分别聚合（SEPARATELY），不混合；
- 竞价分析是独立层，不并入第一/第二金字塔。

## 3. 竞价锚点合同（Auction Anchor Contract）

### 3.1 锚点通用字段

每个锚点必须保存：

| 字段 | 类型 | 说明 |
|---|---|---|
| `anchor_type` | enum: `structure` \| `chip` | 锚点来源类型 |
| `source` | UUID | `source_core_run_id`（structure）或 `source_chip_run_id`（chip） |
| `direction` | enum: `up` \| `down` | 锚点方向 |
| `lower_price` | decimal | 锚点下边界 |
| `upper_price` | decimal | 锚点上边界 |
| `center_price` | decimal | 锚点中心 |
| `strength` | float (0-1) | 锚点强度 |
| `freshness` | enum: `fresh` \| `stale` \| `expired` | 新鲜度 |
| `validity` | enum: `valid` \| `invalid` \| `invalidated` | 有效性 |
| `price_adjustment_version` | string | 关联复权因子版本（adj_factor version） |

### 3.2 结构锚点（anchor_type=structure）

来源：第一金字塔结构维度（`source_core_run_id`）。必须保存：

- 高点/低点（high/low points）；
- BOS/CHoCH 触发线（trigger lines）；
- Order Block（OB）；
- 失效线（invalidation lines）。

### 3.3 筹码锚点（anchor_type=chip）

来源：筹码共识维度（`source_chip_run_id`）。必须保存：

- 上/下共识区（upper/lower consensus zones）；
- 主峰（main peak）。

### 3.3.1 锚点模式与晚到筹码（V2.1 对齐 PRD31 §5 PC-31 / §6 PC-40 / §6 PC-41）

> 本条款为 PRD31 竞价节点合同（模式：§5 PC-31；不可变：§6 PC-40；lineage：§6 PC-41）在同域 PRD 的显式传播，不引入新业务决策，不新增 PC 编号。

必须明确区分两个不同层级的"模式"，禁止把 `hybrid` 写成单股 anchor mode：

**A. 单股锚点模式（per-instrument anchor mode）** —— 对应 PRD31 §5 PC-31：

- `structure_only`：仅 structure 锚点，`source_chip_run_id = NULL`；
- `composite`：组合视图（结构 + 筹码在同一分析视图内聚合呈现）；
- `unavailable`：筹码维度不可用，该维度整体缺失；
- `failed`：筹码计算失败，该维度标记为失败。

单股锚点模式描述**一枚锚点 / 一只股票**的维度可用性，**没有 `hybrid` 这一单股模式**。

**B. 批次 `AuctionAnchorRun` 发布模式（batch publication mode）** —— 锚点集合的发布形态：

- `structure_only`：批次内锚点均为 structure-only；
- `hybrid`：`AuctionAnchorRun` 同时承载 structure 锚点与 chip 锚点（跨多股聚合的发布形态）；
- `composite`：组合发布视图。

`hybrid` 只用于批次 / run 发布层级，不用于描述单股锚点。

**晚到筹码升级（late chip upgrade）** —— 对应 PRD31 §6 PC-40（不可变）/ §6 PC-41（lineage）：

- 若已发布 `structure_only` 批次因 chip 共识晚到需要升级为含 chip 的形态，**必须创建新的 `AuctionAnchorRun`** 并补填 `source_chip_run_id`；旧的 run 与已发布 `auction_analysis_publications` historical publication **不可变**；
- `current` pointer **原子切换到新正式 publication**（pointer 本来就需要切换，禁止写"pointer 内容不可修改"——pointer 切换是合法且必要的）；
- 任何修正（含 late chip 升级）必须创建新 run 并 supersede 旧发布，与 PRD31 §6 PC-40「published run 不原地修改」一致；lineage 由新 run 的 `source_chip_run_id` 与 supersede 链记录，符合 PRD31 §6 PC-41。

### 3.4 新鲜度与有效性

- `freshness`：基于锚点形成后经过的交易日数衰减；`expired` 锚点不再参与当日分析；
- `validity`：`valid` 表示当前有效；`invalidated` 表示被价格突破失效；`invalid` 表示数据或计算异常；
- `price_adjustment_version`：锚点价格必须可追溯到复权因子版本；复权版本变化时必须重新校验锚点，不得跨版本直接复用。

## 4. 竞价分析定义（Auction Analysis Definition）

### 4.1 分析对象

竞价分析定义"最终竞价价格"（final auction price）相对于锚点的行为。

### 4.2 三个分析维度

1. **位置迁移（position migration）**：最终竞价价格相对于锚点（upper/lower/center）的位置变化轨迹；
2. **历史参与（historical participation）**：历史上价格测试该锚点的次数与结果；
3. **板块扩散（sector diffusion）**：同一板块内具有相同 `anchor_type` 的股票数量与比例。

### 4.3 竞价事件生命周期（7-state 合同，NON-LINEAR）

> 本合同由 PRD75 owning（PRD31 不新增 lifecycle 条款）。状态集合与转换来自 `auction_scan_service` 当前实现，非线性、非单边推进。

**状态集合（7）**：

- `formed`：事件首次形成（创建时即 `lifecycle="formed"`）；
- `confirmed`：开盘后价格测试维持触发条件，锚点被确认有效；
- `continued`：已 `confirmed` 的突破 / 支撑 / 阻力类事件，在窗口末价仍维持触发条件（维持触发）；
- `weakened`：价格自触发线回落至 2% 容差带内，强度衰减；
- `failed`：价格突破失效线（回落 / 越过 >2%），锚点失效；
- `transformed`：结构性变化（板块扩散失败 / 龙头孤立 / 指数背离），事件形态发生本质转变；
- `expired`：超过有效期，不再参与分析（定义态；当前转换逻辑不产生该态，见下）。

**活跃集与终态**：

- 活跃集（参与 `update_event_lifecycle` 重算）：`{formed, confirmed, continued, weakened}`；
- 终态（一旦进入不再被重算覆盖）：`{failed, transformed, expired}`。

**NON-LINEAR 转换图**（源态 → 目标态，依据事件类型与开盘后窗口价）：

```text
                          ┌─────────────────────────────────────────────┐
                          │  结构性变化（任意活跃态）                     │
   formed ──价格维持触发──▶ confirmed ──窗口末价仍维持触发──▶ continued   │
     │                      │  │                                          │
     │                      │  └──窗口末价回落 2% 带 / 跌破失效线──▶ weakened / failed
     │                      │                                             │
     ├──回落 ≤2%──▶ weakened                                              │
     ├──回落 >2% / 越线 >2%──▶ failed                                      │
     └──无触发线事件（inside_open 等）──▶ formed（维持）                   │
                          │                                             │
                          └──────────────▶ transformed ◀───────────────┘
                                       (sector_dispersion_failed /
                                        leader_isolation /
                                        index_divergence)
```

- **`formed` 源**：突破类（dual_breakout / structure_breakout / chip_repricing）开盘价 ≥ 触发价 → `confirmed`；回落 ≤2% → `weakened`；回落 >2% → `failed`。支撑确认类开盘价 ≥ 触发价 → `confirmed`，回落 >2% → `failed`。阻力阻挡类开盘价 ≤ 触发价 → `confirmed`，越过 >2% → `failed`。测试类（test_upper/test_lower）达标 → `confirmed`，不达标 → `weakened`。无明确触发线的事件（inside_open / anchor_insufficient / anchor_expired / insufficient_participation）维持 `formed`。
- **`confirmed` 源（窗口末价二次判定）**：突破 / 支撑 / 阻力类窗口末价仍维持触发 → `continued`；回落至 2% 容差带 → 保持 `confirmed`（不降级但未达 `continued`）；回落 >2% → `weakened` / `failed`。测试类窗口末价维持 → `continued`，否则保持 `confirmed`。
- **`transformed` 最高优先级**：任意活跃态在结构性变化检测命中时一律转为 `transformed`，覆盖价格判定结果（证据：`sector_dispersion_failed` / `leader_isolation` / `index_divergence`）。
- **`expired`**：当前 `update_event_lifecycle` 的转换逻辑（`_determine_lifecycle_transition` / `_classify_continued_lifecycle` / `_detect_structural_transformation`）均不产出 `expired`；该态为已定义的终态，但**当前转换路径未生成**（属于锚点 freshness 过期等外部逻辑，UNVERIFIED 本轮未做 code audit）。不得假设 `expired` 由开盘后窗口价转换产生。

### 4.4 明确排除

- 不生成第三金字塔；
- 不生成综合分或复合评分（composite score）；
- 不与 P/Q/U/C/V 合并。

## 5. 约束

### 5.1 第一金字塔约束

- 保留：趋势、结构、动量 + 波动率、可选筹码；
- 不生成总分（NO total score）。

### 5.2 第二金字塔约束

- 维度：状态分布、状态迁移、事件新鲜度、宽度、集中度、相对强度；
- 行业（industry）和概念（concept）必须分别聚合（SEPARATELY），不得混合；
- 不生成总分。

### 5.3 竞价分析约束

- 是 NEW 分析层，不属于第一/第二金字塔；
- 不生成第三金字塔、不生成综合分；
- 锚点价格必须关联复权因子版本；
- 最终报价必须经过至少两个独立 `provider_family` 的一致性验证；同一供应链的不同服务器不构成两个来源。

## 6. 最终报价真值合同

最终报价 DTO 固定包含：`symbol`、`market`、`final_price`、`prev_close`、`volume`、
`amount`、`source_timestamp`、`source_server`、`raw_payload`、`capture_time`、
`is_final_auction`。

- 价格差不得超过最小价格跳动单位；量和额分别使用配置的相对容差。
- 任一维度冲突时标记 `conflict`，不得进入 scan。
- 配置了两个独立来源但个股报价缺失时标记 `partial`，不得正式发布。
- 实际独立来源少于两个时标记 `blocked_external_auction_truth_source`。
- 同一供应链的不同服务器不构成两个独立来源，不得用于满足双源门禁。

## 7. 编排与发布合同

正式链路为：来源采集留证 → 独立性与一致性验证 → 共识报价落库 → scan →
market/industry/concept aggregate → `auction_analysis_publications` pointer。

- 用户 API 只读取专属 pointer 指向的 `scan_run_id`，禁止直接读取最新 succeeded run。
- 正式发布要求 truth=verified、production namespace、共识 capture succeeded、scan succeeded、
  数据覆盖率不低于 0.95 且聚合结果存在。
- Canary、partial、conflict、blocked_external 均不得写正式 pointer。
- 重试复用唯一键对应 run，清理未发布半成品；已成功 run 幂等返回。
- 开盘确认与 Review 回流独立于 P/Q/U/C/V，不并入第一或第二金字塔。

## 8. 验收与状态语义

内部代码合同以 `verified` 为最高状态；生产整体只有在 migration、PG Integration、真实双源、
正式交易日 scan/aggregate/publish 和三级页面 E2E 全部通过后才能称为闭环。

缺少真实第二独立供应商时，生产状态必须为
`blocked_external_auction_truth_source`，不得声称生产竞价闭环。具体供应商和实现验证状态只记录在 Map 与 Change。
