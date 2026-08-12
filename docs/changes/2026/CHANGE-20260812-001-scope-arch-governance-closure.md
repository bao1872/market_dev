# CHANGE-20260812-001 — Review Scope Architecture PRD + Governance Closure

- **类型**：governance + architecture + docs（产品/工程架构契约，无代码改动）
- **领域**：复盘模块 / Scope Observation Model / 治理规则
- **状态**：`prd_confirmed`（docs-only；未进入 Implementation Design，未写代码，未决定 persistence/API/filter 形状）
- **关联 PRD**：`docs/prd/70-review.md`（§7.8 新增）
- **关联 Maps**：`docs/maps/70-review.md`（未修改；Map 继续描述当前 legacy implementation）
- **关联 Rules**：`rules/10-product-domain-invariants.md`（§3.1）、`rules/20-market-data-computation.md`（§6.1）
- **No PRD semantic 指标重设计 / No implementation design / No Migration / No tests / No code change**

## 1. 背景与输入

本决策的输入为「Review Observation Model — PRD → Code Impact Audit」（2026-08-12），
该 audit 已确认 KEEP / 可复用（PIT membership、member atomic facts、部分 `_derive_*` primitives、
parallel Scope scanning skeleton、D-family raw evidence、tracking/lineage/publication 生命周期框架）
与 Legacy/conflict（P/Q/U/C/V scoring aggregation、A/B/C filters、Discovery 投影、Cross-Scope Q/U
percentile、attribution score contribution、publication `normalized_ready` gate、API/frontend P/Q/U/C/V 投影）。

本轮不重新做该 audit，只在此基础上正式冻结两个长期架构决策，避免未来
Industry / Concept / Index / Style / Market 各写一套 Observation calculator。

## 2. 变更内容

### 2.1 docs/prd/70-review.md — 新增 §7.8 Scope Architecture Contract

- **Scope logical contract**：scope identity / PIT membership at T / peer cohort / metadata-readiness。
- **Scope Family 平行可扩展**：market / major_index / style / industry / concept 均为平行 Family；
  Family 差异主要属于 membership resolver / metadata-taxonomy / peer cohort / source-readiness，
  **不得默认**属于 Price / Trend / Structure / Momentum / Participation / Concentration calculation。
- **Canonical Observation ownership**：同一 Observation fact 只有一个 canonical production owner；
  不得按 Family 复制核心 Observation 计算；Family-specific adapter 只处理 membership / metadata /
  peer cohort / readiness；确需 Family-specific computation 必须先修改正式 PRD。
- **Scope maturity**：区分 architecture support / product validation / release maturity。
- **Peer cohort** 属于 Scope contract；Observation Engine 消费 resolved peer cohort，不得自行猜。
- **Persistence boundary**：`observation_payload` JSONB / 多 payload column / 新表 / migration 形状继续 DEFER（§5.3）。

### 2.2 rules/10-product-domain-invariants.md — §3.1 Scope Family 可扩展性

- Scope Family 是平行、可扩展观察对象；
- taxonomy hierarchy ≠ Discovery gate hierarchy；
- architecture support ≠ product validation；
- 不同 Family 可处于不同 EXPERIMENTAL / VALIDATED / STABLE / RELEASED 状态；
- 新 Family 不得因其他 Family 未命中/未 ready 而失去独立观察资格。

### 2.3 rules/20-market-data-computation.md — §6.1 Canonical Scope Observation

正式逻辑链 `PIT member set + target trade date + canonical member facts → canonical Scope Observation`，
含 5 条硬规则（唯一 production owner / 不按 Family 复制 calculator / adapter 边界 /
核心时间因果口径一致 / Family-specific 需 PRD 授权）。

## 3. 未决定（DEFER）

- Scope Observation persistence 形状（单个 `observation_payload` JSONB / 多个 payload column / 新表 / migration）；
- API / frontend / filter 的具体实现形状（与 Observation facts 的绑定方式）；
- Diffusion D1/D3/D5 horizon 选择（PRD §7.3 保持 PROVISIONAL）；
- Filter 新阈值、Anomaly 算法、新 Style taxonomy、新 Index universe；
- 本决策未引入任何 plugin / factory / production class 命名要求。

## 4. 验证状态

- 文档自检：未引入新 score / weight / strong-weak threshold / Style taxonomy / Index universe /
  DB schema / plugin / factory / production class 命名要求；
- 未修改 AGENTS.md、rules/40*、docs/maps/70-review.md、production code、tests、migration、schema、API、frontend；
- 未创建其它报告目录；
- 未运行 CI、未部署、未写 DB。

## 5. 后续

- 进入 Implementation Design 时再决定 Observation persistence / API / filter 形状；
- 保持 Map（docs/maps/70-review.md）继续描述当前 legacy implementation，待实现验收后单独授权同步。
