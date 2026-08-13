# CHANGE-20260813-012 — Review L1 Contract Finalization: amount_share member ownership + normalized HHI formula

## 元数据

- 日期：2026-08-13
- 类型：`docs-only`（L1 Canonical Observation 最终合同收口 / PRD→Code Alignment）
- 领域：复盘模块 / `docs/prd/70-review.md`
- 授权：用户在「第三阶段A 最终收口」任务中明确授权进行 docs-only 极小 correction
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

CHANGE-20260813-011 大方向正确，但留下两处实现歧义，使 L1 合同尚不可直接进入第三阶段B 代码对齐：
1. `amount_share` 同时被表述为「Scope aggregate payload 的 PRICE 内部事实（位于 price.amount.contribution）」与「不持久化完整 member share vector」，语义不闭合。
2. `normalized HHI` 停留在 `FORMULA_CONFIRMATION_REQUIRED`（PROPOSED），未达实现可用状态。

本轮消除这两处歧义，使 L1 合同达到可实现状态。

## 修复内容（docs-only）

### A. §7.2 amount_share 重新定义为 MEMBER-LEVEL CANONICAL CONTRIBUTION EVIDENCE
- 业务语义：`amount_share` = member amount / Scope valid amount total（Scope 内有效成员 amount 之和）；**不是** amount concentration HHI，二者语义分开、禁止混淆。
- 逻辑归属：PRICE → Amount Contribution；但它是 **member-level 事实**，不是单一 scope-level scalar。
- **ownership 分为两层**：
  - **A. Scope-level Canonical Observation**（`price.amount`）：保存 scope 聚合量（`valid_count` / `total_amount` / `concentration.{raw_hhi,normalized_hhi,member_count,status}`）。`price.amount` **不** 包含单个 `amount_share` scalar（未来 Top-N contribution summary 属单独设计，不在本轮）。
  - **B. Member-level canonical contribution evidence**：`(member_id, amount, amount_share)` 属 L1 客观事实体系，完整 member vector **不** 存进 `review_scope_observation_facts.observation_payload`；复用既有 member evidence owner（如 `ReviewMemberFact` 已有 `amount` 字段），**不新建表、不新建重复 owner**；physical persistence 标 **IMPLEMENTATION DESIGN REQUIRED**。

### B. §7.2 normalized HHI 升级为 ACCEPTED CONTRACT
- 公式：`normalized_hhi = (raw_hhi - 1/N) / (1 - 1/N)`，N > 1，N = 对应 concentration universe 的 valid member count（Price/Amount 各自 universe）。
- 语义：equal-share → 0；single-member-dominant → 1；削弱 member-count 对 raw HHI 下限机械影响。
- **边界正式冻结**：N=0 → None/unavailable；N=1 → None/unavailable（reason=`insufficient_member_count`，**不得** 定义为 1，因 denominator=0 且无内部 concentration 可比较空间）；raw_hhi unavailable → None；zero_abs_return → raw+normalized None；zero_amount_total → raw+normalized None；允许极小浮点误差 clamp 到 [0,1] 但不得掩盖公式错误。
- 状态：**ACCEPTED**（进入实现范围，第三阶段B 按此落地）。

### C. §7.9.2.1 新增 scope snapshot vs member-level contribution evidence 边界
- Scope snapshot（`observation_payload`）只保存 scope-level facts，不保存完整 `amount_share` member vector。
- Member-level `amount_share` 属 L1 canonical member contribution evidence，由既有 member evidence 承载（不新建表；复用方式标 IMPLEMENTATION DESIGN REQUIRED）。
- **明确**：不得写成"amount_share 不持久化 therefore 不属于 canonical fact"——它不进 scope JSONB，但仍属 L1 客观事实。

### D. §7.9.3 L1 合同缺口标注同步
- normalized HHI：ACCEPTED CONTRACT（不再 FORMULA_CONFIRMATION_REQUIRED）。
- amount_share：MEMBER-LEVEL canonical contribution evidence，复用 member evidence owner。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- 一致性：
  1. `amount_share` 不再同时被表述为 scope scalar + 不保存 vector 的矛盾合同（现为 scope 聚合量 + member-level evidence 双层 ownership）。
  2. `normalized HHI` 不再 FORMULA_CONFIRMATION_REQUIRED（升级 ACCEPTED）。
  3. N=1 唯一正式行为：unavailable（不得为 1）。
  4. §6.4 / §7.1 / §7.2 / §7.9 ownership 一致。
- 未修改任何 Python / Tests / DB / Migration / API / Frontend / Maps / Runbooks / 治理。

## 下一步

- **第三阶段B：标准观测事实代码实现对齐** — 按本轮最终合同：迁移 `top-level amount` → `price.amount`；实现 `normalized_hhi`（ACCEPTED 公式）；实现 `amount_share` 正式 member-level 输出（复用 `ReviewMemberFact` owner）；补齐 unit/pg tests。
- L1 事实链完整前继续阻止 Discovery Product Design。
