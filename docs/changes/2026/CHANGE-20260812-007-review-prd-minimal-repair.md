# CHANGE-20260812-007 — Review PRD Minimal Repair（Specification Recovery）

## 1. 概要

本轮为 `docs/prd/70-review.md` 的 **minimal repair / specification recovery**，目标是恢复 70-review.md
作为当前 Review 唯一、清晰、可直接开发的 PRD truth source。

- **非** PRD 重写、**非** PRD consolidation project、**非** 产品重新设计、**非** Filter/Discovery 新实验、
  **非** 代码实现。
- 参考语义版本：`9f74a9607d800bc1788df678966096b48c074f16`（已完成 Scope Observation Model / P/Q/U/C/V
  first-layer 废弃 / Signal / Discovery / Cross-Scope / Frontend Discovery Workspace 收口，但未被后续
  Round 1C / 2A / 2B 实现过程描述污染）。
- 当前已验收实现事实（Round 1A/1B/1C = PASS、Round 2A = PASS）已并入本轮 PRD 状态。

## 2. 本轮只修 5 类问题

- **FIX 1 — 删除错误的 §8.0 Experimental Filter**：删除 Round 2B 新增的 `§8.0 Experimental Filter
  Redesign Contract`（含 `CandidateResult` / `BREADTH_EXPANSION` / `PARTICIPATION_CONFIRMATION` /
  Experiment Configuration / Phase-1 archetype / D1-D3 mandatory D5 optional / Concentration Phase-1 proposal）。
  这些**不是**原 Scope Observation Experiment 已确认的 accepted experiment result，而是后续 Design Audit
  提议，不应作为当前正式 PRD 的既定业务事实。§8 恢复为 `Filter Engine（内部 Evidence Family）` 当前合同，
  保留正确原则（Filter 只消费 Structured Observation Evidence；legacy A/B/C = IMPLEMENTATION_REDESIGN_REQUIRED；
  A/B/C/D 可作内部 Evidence Family；不做 black-box composite score；具体 Observation-based 条件若未冻结则
  标 `IMPLEMENTATION_DESIGN_REQUIRED` / `NOT YET FROZEN`，IDE 不自行设计）。
- **FIX 2 — 修正 L1 / L2 边界**：§6.4 中 `delta1d` / `delta5d` / `historical percentile` / `peer percentile`
  从 L1 Canonical Observation facts 列表移除，明确归属 **L2 Objective Evidence**（§7.5），并加 L1/L2
  ownership 边界注记（Observation ≠ Evidence，Evidence 不回流 L1）。
- **FIX 3 — 修正 Persistence 已实现状态**：§7.8.6 与 §7.9.9 由「persistence shape DEFER / Observation Model
  可能推翻」改为已落地 `review_scope_observation_facts`（grain = trade_date + scope_type + scope_key），
  Persistence 只 serialize/validate/upsert/read，不重算 ratio/HHI/transition/percentile/score，不保存
  score/ranking/opportunity/risk/bullish-bearish/filter/Discovery judgment；`market_review_scope_snapshots`
  p/q/u/c/v 仅 legacy compatibility。
- **FIX 4 — 修正会约束新代码的 legacy P/Q/U/C/V 强合同**：§23.5 发布门禁 #1 由「market P/Q/U/C/V value 非空」
  hard gate 改为「market Canonical Observation facts 就绪」新门禁；旧 P/Q/U/C/V 门禁仅标 legacy compatibility，
  不得继续作为新链路 hard gate。
- **FIX 5 — 修正 Acceptance Examples**：§20.1 Case 1 / 4 / 7 中 `P/Q/U/C/V` 改善语义改为 Observation /
  Evidence language（PRICE Breadth / TREND State+Breadth / Transition / PARTICIPATION / Concentration 等），
  业务验收目标（Concept 独立发现 / 多轴共振 / Theme Led / Conflict）全部保留。

## 3. §22 Implementation Order

新增 DONE / NEXT 块：Canonical Observation、Canonical Persistence、Objective Evidence 标 DONE；
Filter redesign / Signal-Discovery integration / Cross-Scope / API-Frontend cutover / legacy cleanup 标 NEXT。

## 4. 不受影响 / 合法遗留

- 不修改 CHANGE-20260812-005 / 006（历史真实记录保留）。
- 不清理全量 legacy P/Q/U/C/V 章节；§23/§24/§25/§26/§27 中 P/Q/U/C/V 引用保持为 legacy implementation
  baseline（已标 legacy）。
- 不重建 §8.1–8.4（legacy A/B/C 当前合同，IMPLEMENTATION_REDESIGN_REQUIRED）。

## 5. 文件变更

| 文件 | 变更 |
|---|---|
| `docs/prd/70-review.md` | 5 类 minimal repair（删除 §8.0、L1/L2 边界、Persistence 状态、§23.5 gate、§20.1 验收语义、§22 顺序） |
| `docs/changes/2026/CHANGE-20260812-007-review-prd-minimal-repair.md` | 本 CHANGE |
| `docs/changes/INDEX.md` | 插入 007 行 |

## 6. Validation

- repo docs consistency / governance 检查通过（docs-only，无 CI / backend / frontend / DB / Docker）。
- `experiments/` 保持 untracked 未触碰。

## 7. Status

PRD_70_MINIMAL_REPAIR_READY_FOR_EXTERNAL_AUDIT（见最终报告）。
