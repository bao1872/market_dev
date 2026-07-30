# 竞价分析 Map（设计草案）

核验状态：待核验 — DESIGN DRAFT（尚未实现，本文档仅记录设计状态，非已核验实现）
最后更新：2026-07-30
核验分支：dev
核验范围：无实现可核验；本文档记录 PRD 草案对应的设计落地状态
对应 PRD：`../prd/75-auction-analysis.md`
事实所有权：竞价分析层当前设计状态（DTO/Model 草案、未实现的入口清单）

> 注意：本 Map 记录的是设计草案状态，不是已核验的实现。
> 按 Maps 规则，Map 通常只记录已核验事实；本文件作为例外，所有条目明确标注为"未实现/设计草案"，
> 用于承接 PRD 草案的设计落地状态。任何条目被实现后必须重新核验并将状态改为"已核验"或"部分核验"。

## 1. PRD 实现映射

| PRD 章节 | 当前实现状态 | 验证证据 |
|---|---|---|
| §0 背景与定位 | 未实现 | 无 |
| §1 产品目标与边界 | 未实现 | 无 |
| §2 分析栈位置 | 未实现 | 无 |
| §3 竞价锚点合同 | 未实现（仅有 DTO/Model 设计草案，见 §3） | 无代码 |
| §4 竞价分析定义 | 未实现 | 无代码 |
| §5 约束 | 未实现 | 无代码 |
| §6 本轮范围 | 本轮仅 PRD/Map 草案 | 本文件 + `prd/75-auction-analysis.md` |
| §7 待确认问题 | 待 PRD 确认 | 无 |

## 2. 当前设计状态摘要

竞价分析层尚未实现。当前状态：

- **数据库**：无迁移存在；
- **后端**：无 service/domain/api 代码；
- **前端**：无组件；
- **CLI**：无脚本；
- **配置**：无配置文件；
- **测试**：无测试。

唯一存在的产出物为本 PRD 草案与本 Map 草案。

## 3. 数据模型设计（DTO/Model 草案，非实现）

> 以下为 PRD §3 锚点合同对应的 DTO/Model 设计草案。仅为设计意图，不表示数据库表或代码已存在。
> 完整合同定义见 PRD `../prd/75-auction-analysis.md#3-竞价锚点合同auction-anchor-contract`。

### 3.1 锚点通用模型（草案）

```python
# 设计草案，非实现
class AuctionAnchor:
    anchor_type: Literal["structure", "chip"]
    source: UUID                  # source_core_run_id / source_chip_run_id
    direction: Literal["up", "down"]
    lower_price: Decimal
    upper_price: Decimal
    center_price: Decimal
    strength: float               # 0.0 - 1.0
    freshness: Literal["fresh", "stale", "expired"]
    validity: Literal["valid", "invalid", "invalidated"]
    price_adjustment_version: str
```

### 3.2 结构锚点扩展（草案）

```python
# 设计草案，非实现
class StructureAnchorPayload:
    high_point: Decimal
    low_point: Decimal
    bos_trigger_line: Decimal
    choch_trigger_line: Decimal
    order_block: dict              # OB upper/lower
    invalidation_line: Decimal
```

### 3.3 筹码锚点扩展（草案）

```python
# 设计草案，非实现
class ChipAnchorPayload:
    upper_consensus_zone: Decimal
    lower_consensus_zone: Decimal
    main_peak: Decimal
```

### 3.4 竞价分析结果（草案）

```python
# 设计草案，非实现
class AuctionAnalysis:
    anchor_id: UUID
    final_auction_price: Decimal
    position_migration: dict           # 相对 upper/lower/center 的迁移轨迹
    historical_participation: dict     # 历史测试次数与结果
    sector_diffusion: dict             # 同板块同类型锚点扩散度
    lifecycle: Literal["formed", "confirmed", "weakened", "failed", "expired"]
```

## 4. 未实现的入口

### 4.1 数据库（不存在）

| 预期表 | 状态 | 说明 |
|---|---|---|
| `auction_anchors` | 不存在 | 锚点存储 |
| `auction_anchor_payloads` | 不存在 | 结构/筹码锚点扩展 payload |
| `auction_analyses` | 不存在 | 竞价分析结果 |
| `auction_sector_diffusion` | 不存在 | 板块扩散统计 |

无迁移文件。当前 alembic head=076（`076_market_review_workbench.py`）；下一可用迁移编号待 PRD 确认后分配。

### 4.2 后端（不存在）

| 类型 | 预期路径 | 状态 |
|---|---|---|
| Domain | `backend/app/domain/auction/` | 不存在 |
| Service | `backend/app/services/auction_*.py` | 不存在 |
| Schema | `backend/app/schemas/auction.py` | 不存在 |
| API | `backend/app/api/auction.py` | 不存在 |
| CLI | `backend/scripts/auction_*.py` | 不存在 |

### 4.3 前端（不存在）

| 类型 | 预期路径 | 状态 |
|---|---|---|
| Page | 无竞价分析页面 | 不存在 |
| Feature | `frontend/src/features/auction/` | 不存在 |

## 5. 与 PRD 的合同指针

- 锚点合同定义：PRD §3 `../prd/75-auction-analysis.md`
- 竞价分析定义：PRD §4
- 约束：PRD §5
- 待确认问题：PRD §7

本 Map 不复制 PRD 全文，仅记录设计落地状态。

## 6. 已知缺口与后续步骤

- 锚点复权版本校验机制未设计（待 PRD 确认后补充）；
- 板块扩散的"同板块"定义（行业 vs 概念）未在 PRD 中明确，见 PRD §7；
- 锚点有效期阈值未定义，见 PRD §7；
- 历史参与"测试"判定标准（容差）未定义，见 PRD §7；
- 与第二金字塔的字段复用关系未明确。

## 7. 更新触发条件

当以下任一发生时更新本 Map：

- PRD 草案被确认并进入开发；
- 锚点合同字段变化；
- 实现任何迁移、服务或 API；
- 设计状态从"草案"变为"已实现"。

按 Maps 规则，实现后必须重新核验并将状态从"待核验（设计草案）"改为"已核验"或"部分核验"。
