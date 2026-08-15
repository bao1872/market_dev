# CHANGE-20260815-005 — Review v2.3 Dynamics Phase Numerical Contract Freeze

## 元数据

- 日期：2026-08-15
- 类型：`docs-only`（PRD contract freeze / Dynamics Phase 六类 exact numerical contract 正式冻结）
- 领域：复盘模块 / `docs/prd/70-review.md`（§7.11 / §7.11.1）
- 授权：用户在 `ref/复盘模块修改指令专用.md`（Review v2.3 — Dynamics Phase Numerical Contract Freeze）中明确授权进行 docs-only PRD freeze round
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）
- 备注：指令指定 ID 为 CHANGE-20260815-004，但该 ID 已被漂移提交 8b6d1d2（顶部搜索 / 行情与自选职责分离 PRD 对齐 Round 1）占用；经用户确认改用 CHANGE-20260815-005，语义与流程不变。

## 背景

Historical Dynamics 数学合同（CHANGE-20260815-001 EMA / -002 Persistence）与 Phase 输入架构（CHANGE-20260815-003）均已冻结。
六类 Dynamics Phase 的分类名称与语义 = FROZEN PRODUCT CONTRACT，但 **exact threshold / conflict priority / tie-break** 此前仍标 ALGORITHM MAPPING REQUIRED。

上一轮「Dynamics Phase Numerical Contract Closure」= MAPPING_RECOMMENDED（基于 13 scopes × 240D 真实历史数据，all-four-ready = 793 obs）。
本轮唯一目标：把已经验证通过的 Dynamics Phase exact numerical contract **正式冻结进 PRD**，并新增 CHANGE + INDEX，commit + push dev。

## 变化内容（docs-only）

### A. `docs/prd/70-review.md` §7.11 新增「Dynamics Phase Numerical Contract（FROZEN）」

- **五个 frozen constants（exact，不得写成约数）**：
  - `DYNAMICS_PHASE_VELOCITY_GATE = 2.0`
  - `DYNAMICS_PHASE_ACCELERATION_GATE = 1.0`
  - `DYNAMICS_PHASE_POSITION_HIGH = 70.0`
  - `DYNAMICS_PHASE_UPPER_OCCUPANCY_GATE = 0.20`
  - `DYNAMICS_PHASE_LOWER_OCCUPANCY_GATE = 0.30`
- **Exact state definitions（numerical states，不是新 Phase）**：V_NEG（v≤-2）/ V_MID（-2<v≤2）/ V_POS（v>2）；A_NEG（a≤-1）/ A_ZERO（-1<a≤1）/ A_POS（a>1）。
- **HIGH_REGIME = position ≥ 70.0 AND upper_occupancy ≥ 0.20**（`position==70.0` 属 HIGH_REGIME，前提 upper occupancy 同时满足）。
- **BOTTOM_RECOVERY_CONTEXT = position < 70.0 AND lower_occupancy ≥ 0.30**（joint eligibility context，禁止写成 `Position < 70 = bottom`）。
- **六类 exact boolean conditions（by construction mutually exclusive）**：
  - Weakening：`velocity <= -2.0`（Position / Acceleration / Persistence 不 gate）；
  - Decelerating：`HIGH_REGIME AND velocity > -2.0 AND acceleration <= -1.0`；
  - Sustained：`HIGH_REGIME AND velocity > -2.0 AND -1.0 < acceleration <= 1.0`；
  - Early Lift：`BOTTOM_RECOVERY_CONTEXT AND velocity > 2.0 AND acceleration > 1.0`；
  - Repairing：`BOTTOM_RECOVERY_CONTEXT AND velocity > -2.0 AND acceleration <= 1.0`（不需要 `velocity <= 2.0`）；
  - Strengthening：`velocity > 2.0 AND acceleration > 1.0 AND NOT BOTTOM_RECOVERY_CONTEXT`（必须写出完整 boolean，不得简写 non-bottom）。
- **Availability / status propagation**：Required inputs = EW Position / Velocity / Acceleration / Persistence；任一 `unavailable_current` → Phase `unavailable_current` / phase null；否则任一 `insufficient_history` → Phase `insufficient_history` / phase null；否则 ready 后执行六类 classifier；ready + no match → phase null。不得新增 status vocabulary。
- **Mutual exclusion / priority = NONE**：六类规则按数学条件 mutually exclusive；一个 ready observation 最多匹配一个 Phase；无 priority chain；tie-break 不需要。
- **Ready but unclassified**：四个 required inputs 全部 ready 但六类条件均不满足 → status = ready / phase = null；不是第七类 Phase，也不是 unavailable；不得强制全覆盖。
- **Boundary ownership（exact）**：velocity==-2→Weakening；acceleration==-1+HIGH_REGIME+vel>-2→Decelerating；acceleration==1+HIGH_REGIME+vel>-2→Sustained；acceleration==1+BOTTOM_RECOVERY_CONTEXT+vel>-2→Repairing；position==70+up≥.20→HIGH_REGIME；position just below 70+lo≥.30+vel>2+acc>1→Early Lift；lower_occupancy==.30+pos<70→BOTTOM_RECOVERY_CONTEXT。删除 / 不引入旧实验边界（如 pos==30 mid）。
- **Deterministic cases（Case A–G）**：A=v=-2→Weakening；B=HIGH_REGIME+v>-2+a=-1→Decelerating；C=HIGH_REGIME+v>-2+a=1→Sustained；D=BOTTOM_RECOVERY_CONTEXT+v>2+a>1→Early Lift；E=BOTTOM_RECOVERY_CONTEXT+v>2+a=1→Repairing；F=v>2+a>1+NOT BOTTOM_RECOVERY_CONTEXT→Strengthening；G=all ready no match→status ready / phase null。
- **Output contract（domain output semantics）**：至少 trade_date / phase / status；建议透明 evidence：position / velocity / acceleration / upper_occupancy / lower_occupancy；可选 derived states：velocity_state / acceleration_state / high_regime / bottom_recovery_context；**禁止** phase_score / confidence_score / strength_score / composite_score。本轮不设计 API schema。

### B. `docs/prd/70-review.md` 顶部 SUPERSESSION NOTICE 同步

- header 中「Dynamics Phase / Internal Structure Type / Trading Context 的 exact threshold 仍属 ALGORITHM MAPPING REQUIRED」修正为：Dynamics Phase 六类 exact numerical contract 已冻结（引用 §7.11），仅 Internal Structure Type / Trading Context 仍属 ALGORITHM MAPPING REQUIRED。

### C. `docs/prd/70-review.md` §7.11.1 Algorithm Mapping 边界 A/B/C 层状态同步

- **A. Dynamics Phase Algorithm Mapping = FROZEN / CLOSED**（由 mapping required → FROZEN），并引用本节 numerical contract，明确：
  - exact thresholds 已冻结；boolean conditions 已冻结；mutual exclusion 已冻结；priority = NONE；tie-break 不需要；ready-but-unclassified 已冻结。
- **B. Internal Structure Type Mapping 依赖 = ALGORITHM MAPPING REQUIRED**（保持不变；其中 Leadership Migration 的 rank-stability algorithm 仍标 ALGORITHM MAPPING REQUIRED）。
- **C. Trading Context Mapping 依赖 = ALGORITHM MAPPING REQUIRED**（保持不变）。
- 明确未把 Interpretation 全部写成 CLOSED。

## Evidence Note（仅在 CHANGE 记录，不塞进 PRD 主合同）

- mapping 基于真实历史数据：13 scopes × 240D（149 trading days），all-four-ready = 793 obs；方法 = distribution inspection + representative / event replay + sensitivity + mutual-exclusion verification。
- final candidate 验证结果：`match_count >= 2 = 0`（无重叠）；`semantic invariant violations = 0`；sensitivity 邻域无灾难性翻转（尤其 LOWER_GATE 邻域不再导致 Strengthening / Early Lift 分布灾难性迁移）。
- 数据集为单一「强涨后回落」regime，**Sustained / Decelerating 样本较少**：记录为后续复核事项（扩展时间窗口或补充低位反弹 / 高位持续 / 冲高减速代表性 scopes 后复核数值稳定性）。**不得把已冻结合同重新标 provisional / proposed**。

## 明确未修改（产品语义保留）

- Historical Dynamics EMA / Persistence 数学合同（CHANGE-20260815-001 / -002）未改变。
- 六类 Dynamics Phase / 五类 Internal Structure Type / 五类 Trading Context 的**分类名称与语义** = FROZEN PRODUCT CONTRACT，未改动。
- 未引入 priority chain、tie-break、phase_score / confidence_score / strength_score / composite_score、七类 Phase。
- 未实现任何 production code / tests / DB / schema / migration / API / frontend / orchestrator；未重新实验、未重新调 threshold。
- Leadership Migration / Internal Structure Type mapping / Trading Context mapping 保持 ALGORITHM MAPPING REQUIRED，未改动。
- `docs/maps/70-review.md` 未修改（Maps 同步需用户验收后授权）。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- `git diff --stat` / `git status --short`：仅 3 个 docs target（`docs/prd/70-review.md`、`docs/changes/INDEX.md`、`docs/changes/2026/CHANGE-20260815-005-phase-numerical-freeze.md`）。
- 重新读取修改后的 §7.9 / §7.11 / §7.11.1 / §7.12，逐项确认（见 Final Report VERIFY 10 项）。
- exact-stage：`git add` 仅列明 3 个文件，禁止 `git add .` / `-A` / `-u`。
- 未修改任何 Python / Tests / DB / Migration / API / Frontend / Maps / Runbooks / 治理。

## 下一步

- **A 层（Dynamics Phase Algorithm Mapping）= FROZEN / CLOSED**：后续进入 Implementation 时直接消费本 numerical contract。
- **B 层（Internal Structure Type Mapping）**：仍 ALGORITHM MAPPING REQUIRED；待四类结构事实真实数据 ready 后冻结。
- **C 层（Trading Context Mapping）**：仍 ALGORITHM MAPPING REQUIRED；A、B 均 ready 后冻结。
- **数据复核事项（Deferred）**：Sustained / Decelerating 样本较少，扩展数据集后复核数值稳定性；不影响已冻结合同状态。
