# CHANGE-20260813-011 — Review PRD 标准观测事实 L1 合同补全：Amount payload ownership / normalized HHI / amount_share

## 元数据

- 日期：2026-08-13
- 类型：`docs-only`（L1 Canonical Observation 合同补全 / PRD→Code Alignment Contract Freeze）
- 领域：复盘模块 / `docs/prd/70-review.md`
- 授权：用户在「第三阶段A」任务中明确授权进行 docs-only L1 合同补全
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

上轮（2026-08-12 事实链完整性审计）已确认 L1 Canonical Observation 主体实现完整，但暴露三处 L1 合同缺口：
1. **Amount payload topology**：PRD §7.1 声明 Amount Contribution/Concentration 属 PRICE 内部事实，但代码以独立顶层 `amount` section 表达，且 persistence 白名单 `CANONICAL_TOP_LEVEL_SECTIONS` 冻结了 top-level `amount`。审计结论原将其判为合规（误），本轮纠正为 **PRD→Code CONTRACT CONFLICT / Architecture Drift**。
2. **normalized HHI**：PRD §7.2 已命名 `price_contribution_hhi_normalized` / `amount_contribution_hhi_normalized`，但缺公式。全仓库检索未发现已被正式采用的 normalized HHI 定义。
3. **amount_share**：PRD §7.2 要求，但 Core 仅在内部 HHI 计算时产生 shares，无正式输出字段。

本轮只补齐**为 PRD→Code 对齐所必需的 L1 Canonical Observation 合同**，不扩 L2、不进入 Discovery、不实现 Filter/Signal。

## 上轮审计纠错（在本 CHANGE 体现）

1. Objective Evidence 不是「Phase-1 仅 Current」：scope_evidence_service 对现有 6 个 PRIMITIVE_NAMES 均计算 current/d1/d3/d5/historical/peer（其中 raw_hhi peer disabled、market no peer、未激活 scope family 暂无实际数据为 intentional unavailable）。正确表述：「L2 fact coverage 只有 6 个基础事实（PARTIAL）；这 6 个事实的 context pipeline 已实现 Current/D1/D3/D5/Historical/Peer」。已删除错误结论。
2. Signed Return Contribution：`price.signed_contribution.status = prd_clarification_required` 与 PRD §7.9.3 一致，状态应为 **PASS-DEFERRED / PRD_CLARIFICATION_REQUIRED**，不得写 CONFLICT。已在 §7.9.3 明确。

## 修复内容（均为 docs-only contract freeze）

### A. §7.1 Amount payload topology 合同冻结
- 新增「Canonical payload topology」说明块：Amount Contribution / Concentration **必须** 嵌套在 `price` 之下（`price.amount.{contribution,concentration}`），**禁止** 独立顶层 `amount` canonical section。
- 明确当前 top-level `amount` 为已确认 Architecture Drift / PRD→Code CONTRACT CONFLICT（persistence 白名单冻结 top-level `amount` 只是实现历史，不能证明合规）。
- 后续代码 slice 需迁移：`top-level amount` → `price.amount`，同步 core / persistence validator / tests / evidence paths；**本轮不实施**。

### B. §7.2 normalized HHI 公式状态
- 全仓库检索结论：**未发现已被正式采用的 normalized HHI 数学定义**（`scope_evidence.py:50` 仅注释 raw HHI 不可跨 scope 比较，无公式）。
- 提供最小候选供审查（标准 member-count normalized HHI）：`normalized_hhi = (raw_hhi - 1/N) / (1 - 1/N)`，N > 1；目标语义 equal contribution → 0、single-member → 1、去除 member-count 对 raw HHI 下限机械影响。
- 候选边界：N=0 → UNAVAILABLE；N=1 → 待定（1 或 UNAVAILABLE）；zero_abs_return / zero_amount / raw_hhi unavailable → normalized = None。
- 状态标记 **FORMULA_CONFIRMATION_REQUIRED**：用户确认前仅为 PROPOSED，**不得** 标为 accepted contract、不得进入实现。

### C. §7.2 amount_share 合同冻结
- 业务语义：`amount_share` = member amount / Scope valid amount total（Scope 内有效成员 amount 之和）；**不是** amount concentration HHI，二者语义分开、禁止混淆。
- owner：Scope aggregate payload 的 PRICE 内部事实（位于 `price.amount.contribution`）；**不** 在 Canonical Observation payload 持久化完整 member share vector；完整 member-level share 优先复用已有 member evidence ownership，避免重复保存；physical representation 标 **IMPLEMENTATION DESIGN**，本轮不发明复杂 schema。
- 当前状态：Core 无正式 `amount_share` 输出字段 → L1 实现缺口，待第三阶段B 代码对齐。

### D. §7.9.3 L1 合同缺口诚实标注
- Signed Return Contribution 明确为 **PASS-DEFERRED / PRD_CLARIFICATION_REQUIRED**（非 CONFLICT）。
- 追加 L1 合同缺口清单（Amount topology / normalized HHI / amount_share），状态与上一致。

## 额外记录但不在本轮修复（P3 / PARTIAL）

1. `scope_evidence.py:16` docstring "Diffusion remains PROVISIONAL" 与 PRD §7.3 冲突 → 记录为 P3 semantic drift，下一代码 slice 顺手修（不改 PRD）。
2. `PEER_SCOPE_TYPES` 当前缺 `major_index` / `style`（PRD peer cohort 明确支持二者）→ 标 PARTIAL / Architecture Drift；当前未 activation 非运行 blocker；下一 slice 补。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- §7.1 / §7.2 / §7.9.3 不自相矛盾：Amount 归 `price.amount` 与 §7.1 顶层维度声明一致；normalized HHI / amount_share 与 §7.2 一致；§7.9.3 缺口标注与 §7.2 一致。
- 不引入 Filter / Signal / Discovery 新规则；未修改任何 Python / Tests / DB / Migration / API / Frontend / Maps / Runbooks / 治理。

## 下一步

- **第三阶段B：标准观测事实代码实现对齐** — 按本轮冻结合同迁移 `top-level amount` → `price.amount`、实现 `normalized_hhi`（待公式确认后）、实现 `amount_share` 正式输出，并补齐对应 unit/pg tests。
- L1 事实链完整前继续阻止 Discovery Product Design。
