# 竞价分析 PRD V1.0

状态：已确认（CONFIRMED，已实现）
最后更新：2026-07-30
对应 Map：`../maps/75-auction-analysis.md`
条款前缀：`AU`
需求所有权：竞价分析层的目标行为、锚点合同、分析定义与边界约束

> 本文件是竞价分析层的 PRD。竞价分析是一个独立的分析层，不属于第一金字塔或第二金字塔。
> [CHANGE-20260730-018] 已实现完整链路：Migration 077+078、7张表、3个service、6个API端点、前端三级页面、248个单元测试+15个PG集成测试。

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

### 4.3 锚点生命周期

```
formed → confirmed → weakened → failed → expired
```

- `formed`：锚点首次形成；
- `confirmed`：价格测试后锚点被确认有效；
- `weakened`：强度衰减或新鲜度变差；
- `failed`：价格突破失效线，锚点失效；
- `expired`：超过有效期，不再参与分析。

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
- 本轮仅产出 PRD/Map 设计草案，迁移与业务代码延后。

## 6. 本轮范围（DRAFT）

本 PRD 为草案（DRAFT），尚未确认。本轮只产出：

- `docs/prd/75-auction-analysis.md`（本文件）；
- `docs/maps/75-auction-analysis.md`（设计状态 Map 草案）。

本轮不包含：

- 数据库迁移；
- 后端 service/domain/API 代码；
- 前端代码；
- 测试代码。

## 7. 待确认问题

以下问题在草案阶段未明确，需在确认前补充：

- 锚点有效期阈值（`freshness` 由 fresh 转为 stale/expired 的具体交易日数）；
- 板块扩散中"同板块"的口径（行业 vs 概念，是否两者各自计算扩散度）；
- 锚点与第二金字塔字段的复用边界；
- 历史参与的"测试"判定标准（触及锚点边界的容差）。

## 8. 竞价分析真实闭环状态（CHANGE-20260801-001 核验）

### AU-CL-01 当前已闭环部分

**仅 quote capture（竞价 quote 实时抓取落库）已确认通过：**

- 竞价时段（09:15–09:25）的逐笔 quote 写入链路已验证；
- capture service 容器健康，capture 日志与落库一一对应；
- 竞价 capture 与收盘行情不存在互斥冲突。

### AU-CL-02 当前未闭环部分（**不得在对外宣传/状态页声称全部通过**）

以下环节仍未形成真实生产可用闭环，在完成对应验收前：

1. **09:25 真值**：09:25 集中竞价最终成交价格的权威真值（集合竞价撮合价）仍未通过
   三源对比（L1 snapshot / broker quote / 第三方数据源交叉验证）；
2. **scan 阶段**：全市场锚点扫描（`auction_scan_service`）未实现/未验证 09:25 后 T+0
   时间窗内端到端 scan 的完整性与幂等；
3. **aggregate 阶段**：同一锚点生命周期的多日竞价迁移聚合未在生产跑通至少 20 个
   交易日样本验证（覆盖率、扩散度、参与率三类指标）；
4. **publish 阶段**：auction analysis 正式 pointer/发布门禁、失败回滚、
   与 after_close publishing 的衔接未完成；
5. **前端三级页面**：竞价板块看板 → 竞价板块详情 → 竞价个股详情 的三级页面
   未通过真实交易日 E2E 验证（含 09:25 后 5 分钟内自动刷新、点击跳转、
   与第一金字塔个股联动）。

### AU-CL-03 Canary 与整体成功语义

- 不能把单个 capture service 启动成功 / 单次 migration 通过 当作竞价分析"整体成功"。
- 整体状态只能写：`partial_closed: quote_capture_only`。任何 status=200 health
  响应不得声明 auction 已整体 ready。详见 `rules/40-testing-quality.md` §质量门禁"
  禁止局部 Canary 冒充整体成功"。
