# CHANGE-20260812-009 — Review PRD Specification Repair: Diffusion 移除 + Filter/Signal 降级为 Legacy Compatibility

## 元数据

- 日期：2026-08-12
- 类型：`docs-only`（Specification Repair / PRD 目标行为与架构边界修复）
- 领域：复盘模块 / `docs/prd/70-review.md`（Scope Observation Model / Observation-Evidence 边界 / Filter / Signal / Discovery / State-Change-Anomaly / Orchestration / Roadmap）
- 授权：用户在 Round 2C-0 后续中明确授权修改 `docs/prd/70-review.md` 修复 Specification Defect
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

Round 2C-0（Filter Engine Implementation Design 审计）确认：

- L1 Canonical Observation 与 L2 Objective Evidence 已实现且测试充分（PG + pure PASS）；
- 正式 Filter boundary 缺失，且 `experimental_filter.py`（Round 2B）的 PRD 合同已被 CHANGE-20260812-007/008 撤销。

本轮在 Round 2C-0 基础上，执行最小 PRD–Specification Repair，**只修文档，不实现 Filter**。

## 修复内容

### A. 移除 Diffusion 独立事实模型

PRD 残留两类旧假设，本轮最小修复：

1. **Diffusion 作为独立 Observation / Change 概念** → 正式移除。
   - §7.1 结构图删除 TREND/STRUCTURE/MOMENTUM 下三处 `Diffusion [PROVISIONAL]`；
   - §7.3 删除 "Diffusion（PROVISIONAL）" 定义，重写为：State/Breadth 是 L1 当前日原始分布，Transition 是 exact T-1→T 迁移；
     所谓「扩散 / 收缩」客观上是 State/Breadth 的 **D1/D3/D5 连续数值变化**，属于 **L2 Objective Evidence**（如 Trend Up Breadth `T=0.62, T-1=0.54 → D1=+0.08`），
     **不得离散化为** `EXPANDING/CONTRACTING/STABLE` 的 diffusion state，不得定义 diffusion threshold / score / persistence object；
   - §6.4 TREND/STRUCTURE/MOMENTUM facts 列表移除 `Diffusion（PROVISIONAL）`；
   - §6 小图、§2 权威业务链、顶部业务链移除 `State / Transition / Diffusion`；
   - §10 Change 定义从 `Transition / Diffusion / observation change` 改为两类客观变化（exact T-1→T Transition + 同 fact 跨期连续数值变化）；
   - §10.2 Change 示例「结构破坏开始扩散」等改为连续变化解释语言；
   - §7.9.3 删除 "Diffusion 当前不要求 persistence（仍 PROVISIONAL 且未实现）" 暗示性表述，改为「跨期 State/Breadth change 属于 L2 Objective Evidence，不属于 L1 Canonical Observation persistence」；
   - §12.2 / §14.4 残留 `... / Transition / Diffusion / ...` 一并清除。

   **保留**：用户解释语言中的「参与正在扩散」「集中度扩散」作为 Discovery / Presentation layer 的自然语言，不构成独立 canonical primitive（§1.1 问题 4 保留）。

### B. Filter / Signal 从 V2 mandatory target architecture 降级为 Legacy Compatibility

2. **不再预设 Objective Evidence 之后必经 Filter Engine → matched/unmatched → Atomic Signal → A/B/C/D family**。
   - §2 权威业务链：Observation → Evidence → `[Discovery Product Design — NOT YET FROZEN]` → Cross-Scope/Attribution/Tracking；删除 `运行 Filter Engine` / `Signal = atomic evidence 生成` / `Discovery 聚合（多个 Signal → 一个 Discovery）`；
   - §7.7 移除 "Filter 与 Discovery 只消费 structured Observation Evidence" 的必经表述，新增「Filter / Signal 不是 V2 必经目标架构」说明；
   - **§8 标题改为 `Legacy Filter / Signal Compatibility（非 V2 目标架构）`**，明确：
     - A/B/C/D Filter、`filter_engine`、`MarketReviewSignal` 属 legacy implementation compatibility，继续存在以维持实现兼容；
     - 当前 V2 正式冻结边界停在 **Canonical Observation → Objective Evidence**；
     - 以下全部 NOT YET FROZEN（PRODUCT DESIGN REQUIRED）：是否需要独立 Filter Engine / threshold condition / matched-unmatched / Atomic Signal / Discovery 是否必须聚合 Signal / 是否继续 A/B/C/D family / Discovery 排序聚类异常机制；
     - **禁止 Implementation 阶段从 legacy 架构推导「Filter 是下一必做模块」**；
   - §8.1/§8.2/§8.3 原 `IMPLEMENTATION_REDESIGN_REQUIRED` 标记改为 `LEGACY IMPLEMENTATION REFERENCE / NOT V2 TARGET SPEC`，旧条件保留为历史说明，明确「不得作为新实现要求」；
   - §10A.1/§10A.2 Signal/Discovery 分层降级为 Legacy Compatibility，V2 仅正式定义 Discovery = user-level finding 且必须可追溯到 Observation/Evidence；Signal 是否必须存在 / 由 Filter 产生 / 聚合拓扑全部 NOT YET FROZEN；
   - §11 任务编排移除强制 `evaluate filters → generate Signal records → aggregate Discovery candidates`，改为 `compute Canonical Observations → persist → compute Objective Evidence → [Discovery consumer path — NOT YET FROZEN]`；
   - §22 NEXT 区域修正：「下一阶段 = Filter Engine redesign」改为 **Discovery Product Design = NEXT PRODUCT DESIGN QUESTION**（不是 Filter Implementation = NEXT IMPLEMENTATION TASK），本轮不开始 Discovery 设计。

### C. L1 / L2 边界正式明确

- L1 Canonical Observation：只保存目标交易日 T 原始客观事实（PRICE/Trend·Structure·Momentum State+Breadth+Transition/Participation/Concentration/Contribution/readiness/denominator/diagnostics）；不保存 D1/D3/D5 变化、历史/横截面分位、diffusion state、strong/weak、opportunity/risk、ranking/score、Filter/Discovery 判定。
- L2 Objective Evidence：由不同交易日 L1 派生（Current/D1/D3/D5/Historical Position/Same-family Peer Position）；D1/D3/D5 是连续数值变化，不离散化为 diffusion classification。

### D. State / Change / Anomaly 重锚（§10）

- State = 当前 Canonical Observation 客观事实；
- Change = ① exact canonical T-1→T member Transition ② 同 fact 跨 exact historical horizons 连续数值变化（D1/D3/D5）；不得存在独立 diffusion state；
- Anomaly = 当前事实/变化相对于自身历史或 same-family peer cohort 的相对位置。

## 上一轮 Round 2C-0 输出的处理

Round 2C-0 的 Filter Design Audit（FilterCondition / primitive·context·operator·threshold / MissingBehavior / AtomicSignal / one condition→one signal / generic Filter evaluator / Round 2C-1 Filter implementation）**不作为 accepted design 进入正式 PRD**；本轮明确 V2 不冻结 Filter/Signal 架构，Discovery organization 仍是 NOT YET FROZEN 的产品设计问题。

## 未修改（遵守边界）

- 未修改任何业务代码 / 测试代码 / 数据库 / API / Frontend / Maps / Runbooks / 治理文件；
- 未开始 Filter 实现；未开始 Discovery 产品设计；
- `experimental_filter.py` 等 legacy 文件保留，待新路径验收后由单独 cleanup round 处理；
- `docs/maps/70-review.md` 未修改（实现尚未 cutover，不得伪装已落地新 target architecture）。

## 验证

- `git diff --check`：无 trailing whitespace / 无冲突标记；
- 文档级 grep 验证：无残留 `Diffusion [PROVISIONAL]` / `State / Transition / Diffusion` / 强制 Filter 必经架构 / `Signal 必须由 Filter 生成` / `Discovery 必须由 Signal 聚合` / `下一阶段 = Filter implementation`；
- 自然语言「参与扩散」「集中度扩散」保留为解释性语言，不构成 canonical primitive；
- 未跑全系统测试 / PG 测试 / migration / deployment。

## 状态诚实声明

本 CHANGE 为 **docs/specification change**，描述 PRD 目标行为修复；**不得写成 implementation completed**。V2 正式冻结边界停在 Canonical Observation → Objective Evidence；Discovery organization 仍 NOT YET FROZEN。
