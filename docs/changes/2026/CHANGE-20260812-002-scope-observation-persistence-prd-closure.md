# CHANGE-20260812-002 — Round 1C-0 Minimal Observation Persistence PRD Closure

- **类型**：docs-only（需求收口；无代码 / 无 migration / 无 DB design / 无 Filter / Discovery redesign）
- **领域**：复盘模块 / Scope Observation Model / Canonical Scope Observation Facts persistence
- **状态**：`prd_confirmed`（docs-only；未进入 Implementation Design，未写代码，未决定 physical schema / migration / API / frontend 形状）
- **关联 PRD**：`docs/prd/70-review.md`（§7.9 新增）
- **关联 Maps**：`docs/maps/70-review.md`（未修改；Map 继续描述当前 legacy implementation）
- **关联 Rules**：无（本轮不修改治理，AGENTS.md / rules/10* / rules/20* 均不动）
- **No production implementation / No migration / No DB design / No Filter / Discovery redesign / No API / No frontend / No tests / No code change**

## 1. 背景与输入

Round 1A（Canonical Scope Observation Core）与 Round 1B（Real-Data Shadow Verification，
external verdict = PASS：5 trading days × 2 Industry × 2 Concept + Market historical guard，
25 shadow evidence files，sanity_all_pass = 25/25）已完成并验证。

本轮为 **Round 1C-0**：仅对 **Canonical Scope Observation Facts 的 Exploration-stage
persistence contract** 做需求收口。不重跑数据实验，不开始 Round 1C implementation。

## 2. 变更内容

### 2.1 docs/prd/70-review.md — 新增 §7.9 Canonical Scope Observation Facts — Exploration Persistence Contract

- **Persistence purpose**：只保存已由 Canonical Scope Observation Core 计算完成的客观事实快照；
  不保存 / 不承担 opportunity / risk / strong-weak / recommendation / ranking / score / grade /
  filter / Discovery 判断；persistence 不解释、不判断。
- **Snapshot grain**：`trade_date + scope_type + scope_key → one Canonical Observation Fact Snapshot`；
  不增加 revision graph / publication version / immutable generation / 复杂 lineage model。
- **Persisted facts（第一阶段）**：PRICE（return level / distribution / breadth / price+amount
  concentration）、TREND、STRUCTURE（Swing + Internal 的 State/Breadth/Transition）、MOMENTUM、
  PARTICIPATION（volume / amount distribution）、CHIP（仅 unresolved/unavailable status），
  以及 denominator / eligible / valid count / readiness / diagnostics。Diffusion 仍 PROVISIONAL
  不要求 persistence；Signed Return Contribution 继续 `PRD_CLARIFICATION_REQUIRED`。
- **No subjective persistence**：禁止保存 opportunity / risk / strong-weak / recommendation /
  ranking / score / grade / filter result / Discovery label / Anomaly conclusion。
- **Scope activation**：第一阶段只 activation `industry_l1 / industry_l2 / industry_l3 / concept`；
  Market 为 `NOT ACTIVATED FOR HISTORICAL OBSERVATION PERSISTENCE`（禁止 current active universe
  × historical trade_date）；Major Index / Style 为 `NOT ACTIVATED`，本轮不定义其 universe。
- **Idempotency**：同一 `trade_date + scope_type + scope_key` 重复计算时安全更新同一份快照；
  不建设 V1/V2 coexistence / immutable revision history / publication pointer / revision chain。
- **Persistence ownership**：Core 是事实唯一计算 owner；persistence 只 serialize / validate
  contract shape / upsert；禁止重新计算 ratio / HHI / transition / percentile / 重新解释 NULL /
  把 unavailable 转 0 / 生成 score。
- **与现有 layers 关系**：不改变 legacy P/Q/U/C/V / Filter / Discovery / Publication / API /
  Frontend；Round 1C 初期仍为 shadow path，Canonical Observation Fact Snapshot 暂不成为其正式输入。
- **Exploration boundary**：保存尽量原始、可重新解释的事实；避免保存主观判断；避免复杂版本治理。

## 3. 未决定（DEFER）

- physical persistence shape：单个 `observation_payload` JSONB / 多个 payload column / 新表 /
  migration 形状 → 留给 Round 1C Implementation Design 结合真实 ORM / consumer dependency 判断
  （本轮不在 PRD 中提前锁 schema）。
- API / frontend / Filter / Discovery 与 Observation facts 的绑定方式。
- Signed Return Contribution 语义（保持 `PRD_CLARIFICATION_REQUIRED`）。
- Diffusion persistence（保持 PROVISIONAL）。

## 4. 验证状态

- 文档自检：未新增 score / weight / threshold / opportunity model / risk model / ranking model /
  version platform / revision graph / 新 Scope taxonomy / DB schema 决策 / migration 设计 /
  API 形状 / frontend 设计。
- 未修改 governance（AGENTS.md、rules/10-product-domain-invariants.md、rules/20-market-data-computation.md）。
- 未修改 code / tests / migration / DB / API / frontend；未运行 CI、未部署、未写 DB。
- 未创建其它报告目录。

## 5. 后续

- Round 1C Implementation Design 阶段再决定 physical schema / migration 形状。
- 保持 Map（docs/maps/70-review.md）继续描述当前 legacy implementation，待实现验收后单独授权同步。
