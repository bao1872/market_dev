# CHANGE-20260814-001 — Auction PRD Alignment Round 1: Overnight Repricing Observation + Legacy AuctionAnchor Deprecation

## 元数据

- 日期：2026-08-14
- 类型：`docs-only`（需求事实源对齐 / Auction 产品合同改写 / PRD→Contract Alignment）
- 领域：竞价模块 / `docs/prd/75-auction-analysis.md`（+ PRD31 / PRD30 / PRD70 / PRD README / Map75 / CHANGE INDEX）
- 授权：用户在 `ref/竞价模块修改指令专用命令.md`（Auction PRD Alignment Round 1）中明确授权进行 docs/contract round
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端/Maps 正文/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

旧 Auction PRD（`docs/prd/75-auction-analysis.md`）将竞价定义为 **structure/chip anchor 模型**（盘后生成的竞价锚点、位置迁移、7-state 事件生命周期、`structure_only/hybrid/composite` 批次模式）。

新已确认 Auction 产品合同（`ref/竞价模块修改指令专用命令.md` SHOULD）将 Auction 定义为 **Overnight Repricing Observation（隔夜重新定价观测）**：

- 底层事实只依赖 9:25 竞价价格/Gap 与竞价成交额/Amount 及其历史异常度；
- 分析由三部分构成：静态横截面、个股/Scope 状态迁移、注意力重心再分布；
- **明确移除 Structural Relocation**：P0 不使用 DSA / SMC / Chip position / Bollinger / 中周期结构 / "结构锚点" / chip composite anchor；
- 业务链位置：First Pyramid → Review(t-1) → Auction(t) → Open Verification（未来）。

本轮只完成"需求事实源对齐"，不实现 Auction 业务代码。旧代码处置留给后续 Code Alignment Round。

## 变化前

- `docs/prd/75-auction-analysis.md`：structure/chip 竞价锚点合同、位置迁移、7-state lifecycle、`structure_only/hybrid/composite`、双源真值、盘后编排接入。
- `docs/prd/31-after-close-product-closure-v2.1.md`：`auction_anchor` 为盘后 enhancement 节点（PC-31 / PC-41 lineage）。
- `docs/prd/30-after-close.md`：AC-16 顶层步骤含 `auction_anchor`（legacy 盘后节点）。
- `docs/prd/70-review.md`：§27 依赖矩阵含 `auction 竞价` 行（旧竞价回流）。
- `docs/prd/README.md`：75 职责为"竞价真值、锚点、扫描、聚合、发布和 Review 回流"。
- `docs/maps/75-auction-analysis.md`：旧 AuctionAnchor 实现 baseline（无 PRD 语义收口注记）。

## 变化内容（docs-only）

### A. 重写 `docs/prd/75-auction-analysis.md`（唯一 authority PRD）

新 Auction 合同覆盖以下章节（条款前缀 `AU`）：

1. 产品定位与非目标（Overnight Repricing Observation；非机会/风险/买卖建议）
2. 数据时点与交易日身份（次日 9:25；复权口径一致；AU-03）
3. Stock Auction Fact（`auction_price` / `previous_close` / `gap_pct` / `auction_amount`；AU-04）
4. Historical Abnormality（`gap_percentile` 正/负异常；`amount_multiple` / `amount_percentile` 成交异常；AU-05/AU-06）
5. Price × Amount 二维解释（四个象限；禁止压成单一 Auction Score；AU-07）
6. 静态横截面（historical abnormality 与 cross-sectional position 双参照系分离；Stock/Market/Style/Industry/Concept 平行；AU-08）
7. Stock State Transition（Review(t-1) → Auction(t)；NEW/PERSIST/DECAY/REVERSE/CONFLICT/QUIET；标准化后比较；AU-09）
8. Scope Model（Market/Style/Industry/Concept 平行；复用既有 Scope Family；AU-10）
9. Scope Breadth Metrics（Positive/Negative/Amount/Joint 5 项 breadth；AU-11）
10. Auction Amount Contribution / Concentration（`AuctionAmountContribution` + Top1/Top3；三者分离；AU-12）
11. Concept Overlap Semantics（overlapping membership、贡献不互斥、允许 >100%；AU-13）
12. Scope State Transition（基于 Scope 自身事实，非成员标签计数，P0 无综合分；AU-14）
13. Attention Redistribution（与 state transition 分离；禁止资金净流叙述；AU-15）
14. Review → Auction 依赖边界（只读正式 snapshot，禁调私有 calculator；缺失字段登记 IMPLEMENTATION ALIGNMENT GAP；AU-16）
15. 算法 / 配置版本化（CONFIGURABLE + VERSIONED；AU-17）
16. 数据质量 / valid member denominator（AU-18）
17. Publication / lineage 基本要求（正式 pointer / PIT / 幂等 / supersede；AU-19）
18. API / frontend 目标合同（只定义业务结果，不实现；AU-20）
19. P0 / Non-goals（INCLUDE / EXCLUDE 清单；AU-21）
20. Acceptance Matrix（AU-22）
21. 阈值合同（history window=120 候选、gap 90/10 候选、amount 90 候选，全部 CALIBRATION_REQUIRED；最小样本/成交门/Scope 最小成员 OPEN）
22. 明确排除 Structural Relocation（AU-02-1/02-2）
23. Legacy AuctionAnchor Deprecation / Migration Gap（AU-23，DEPRECATED PRODUCT CONTRACT）

### B. 同步旧 Auction 引用（逐项判断 A/B/C）

- `docs/prd/31-after-close-product-closure-v2.1.md`：
  - §1 盘后链路 `auction_anchor` 行 → 标记 legacy DEPRECATED，注明新 Auction 非盘后节点；
  - §2 九节点注册表 → 原 `auction_anchor` 行保留（代码仍在），新增 `auction`（新，未来节点）说明行；
  - §5 PC-31 → 加 DEPRECATED PRODUCT CONTRACT 注记；
  - §6 PC-41 lineage `auction.*` → 加 DEPRECATED 注记。
- `docs/prd/30-after-close.md`：AC-16 顶层步骤 `auction_anchor` → 加 DEPRECATED 注记（legacy 盘后节点）。
- `docs/prd/70-review.md`：§27 依赖矩阵 `auction 竞价` 行 → 加 DEPRECATED 注记（旧竞价回流依赖；新方向为 Review→Auction）。
- `docs/prd/README.md`：75 文件职责行改写为新合同描述。
- `docs/maps/75-auction-analysis.md`：顶部加 2026-08-14 PRD 语义收口注记（遵循 `maps/70-review.md` 同款偏差注记惯例），正文仍为 legacy baseline。

## SHOULD ↔ ACTUAL 最小 Gap Matrix

| CONTRACT | CURRENT PRD | CURRENT CODE | GAP | CLASS | SEVERITY | ACTION THIS ROUND |
|---|---|---|---|---|---|---|
| Auction = Overnight Repricing Observation（Gap/Amount 历史异常） | 旧锚点位置迁移/参与/扩散 | `auction_anchor`/`auction_scan`（锚点驱动） | 新合同 vs 旧模型 | contract | HIGH | PRD75 重写为新区块 |
| 移除 Structural Relocation（无 DSA/SMC/Chip/Bollinger/结构锚点） | 结构/chip 锚点合同 | `auction_mode_service` structure_only/hybrid/composite、`auction_anchor_run` | 语义废止 | contract | HIGH | PRD75 §22/§23 标记 DEPRECATED |
| AuctionAnchorRun / structure_only/hybrid/composite | §3.3.1 + V2.1 模式合同 | `auction_anchor_run` 模型 + `auction_mode_service` | 旧产品语义 | contract | HIGH | PRD75 §23 DEPRECATED；PRD31 PC-31 注记 |
| chip coverage 不进入新 Auction | chip 门禁决定锚点模式 | `publish_chip_and_upgrade_auction` 回调 | 新合同无 chip 依赖 | contract | HIGH | PRD75 §22 排除；PRD31 PC-31 注记 |
| Auction 是否盘后 orchestrator 生成 | 是（auction_anchor 节点） | after_close_orchestrator 接入 | 新合同=次日 9:25，非盘后 | contract | HIGH | PRD75 §2/§23；PRD31 §1/§2；PRD30 AC-16 注记 |
| frontend 已有 Auction anchor 页面 | §5 三级页面 | `/auction` 三级页面 + `AuctionBackflowPanel` | 属旧产品 | contract | MED | PRD75 §18/§23 标记 deprecated gap |
| Auction persistence/API 名称 | §3/§4/§7 | `auction_*` 10 表 + `api/auction.py` 6 端点 | 新 fact 合同未定物理形状 | contract | MED | PRD75 §17/§18 只定义业务结果，defer |
| Review 可复用 snapshot/evidence | Review §7.9 | `ReviewScopeObservationFact` + `MarketReviewScopeSnapshot` + L2 evidence | 可复用，但 per-stock 迁移字段待核 | alignment | MED | PRD75 §14 定义依赖边界 + 登记 GAP |
| Scope membership / amount_share ownership | Review §7.2 amount_share；Scope Family 平行 | `board_facts`/`market_board_memberships`；`ReviewMemberFact` | 复用既有，口径待核 | alignment | MED | PRD75 §8/§16 引用既有并登记 GAP |
| 9:25 raw fact 采集基建 | — | `auction_final_quotes`（price/prev_close/amount）+ 双源真值 + 09:25 Scheduler | 可复用为新 raw fact 采集 | infra reuse | LOW | Map75 注记注明可复用 |
| 阈值 | 部分固定 | — | 新阈值候选 90/10/120 待校准 | contract | MED | PRD75 §21 全部 CALIBRATION_REQUIRED/OPEN |

## 验证

- `git diff --check`：见提交前验证（EXIT=0）。
- 一致性：
  1. 不再存在"新 Auction P0 依赖 structure/chip anchor"的 active PRD 语义；
  2. 旧 AuctionAnchor 合同（PRD75 §23）已显式标记 DEPRECATED，代码仍存在 → 显式登记 implementation gap；
  3. Review → Auction 依赖边界已写清（只读 snapshot，不调私有 calculator）；
  4. Stock / Scope / static cross-section / transition / attention redistribution / breadth / joint / contribution / concentration / Concept overlap 均已进入正式 PRD；
  5. 阈值未伪装成最终业务真理（全部 CONFIGURABLE/VERSIONED/CALIBRATION_REQUIRED/OPEN）；
  6. PRD31 / PRD30 / PRD70 / README 旧 Auction 引用逐项判断并同步。
- 未修改任何 Python / Tests / DB / Migration / API / Frontend / Runbooks / 治理。

## 下一步

- **Code Alignment Round**：按 PRD75 新合同实施 Auction 业务代码（raw fact → historical abnormality → cross-section → transition → scope aggregation → attention redistribution），并处置旧 AuctionAnchor 代码迁移/删除。
- **校准实验**：用真实历史数据校准 gap/amount 阈值与最小门限（当前 OPEN / CALIBRATION_REQUIRED）。
- Review per-stock 迁移所需正式字段缺失 → IMPLEMENTATION ALIGNMENT GAP，需 Review 侧按既有合同扩展（非本轮）。
