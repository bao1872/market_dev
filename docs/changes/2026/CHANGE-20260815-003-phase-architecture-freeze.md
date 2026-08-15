# CHANGE-20260815-003 — Review v2.3 Scope Dynamics Phase Input Architecture Freeze

## 元数据

- 日期：2026-08-15
- 类型：`docs-only`（PRD contract freeze / Scope Dynamics Phase 输入架构与 Algorithm Mapping 边界收口）
- 领域：复盘模块 / `docs/prd/70-review.md`（§7.9 / §7.11 / §7.11.1）
- 授权：用户在 `ref/复盘模块修改指令专用.md`（Review v2.3 — Scope Dynamics Phase Architecture Contract Freeze）中明确授权进行 docs-only PRD freeze round
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

Historical Dynamics 的 EMA / Persistence 数值合同已冻结（CHANGE-20260815-001 / -002）。
六类 Dynamics Phase 分类名称与语义为 FROZEN PRODUCT CONTRACT，但 **exact threshold / conflict priority / tie-break 尚未冻结**（依赖真实历史数据分布）。

本轮冻结 **Scope Dynamics Phase 的输入架构**，消除 Interpretation 层当前的实现歧义：

1. Phase 由哪个 primitive 的 Historical Dynamics 主导（lifecycle primary owner）；
2. AW / Breadth / Volume 等是否允许改写 Phase label；
3. Internal Structure 事实是否参与 Phase 合成；
4. Phase / Internal Structure Type / Trading Context 三层 Algorithm Mapping 的依赖顺序与阻塞关系。

## 变化内容（docs-only）

### A. `docs/prd/70-review.md` §7.9 新增「Interpretation Input Ownership（FROZEN）」

- **lifecycle primary owner = Equal-weight Return（EW）Historical Dynamics**。
- Phase 核心输入固定为：EW Position / EW Velocity / EW Acceleration / EW Persistence。
- **明确禁止**：11 primitive voting（每个 primitive 一票表决）、multi-factor score / weighted composite / 综合分、EW/AW dual-primary（两条平行主轴）。
- **AW 不是第二条 Phase lifecycle primary axis**：属于 Capital Confirmation，回答「主要成交资金所交易成员是否确认 EW 所表达的整体生命周期」；EW/AW 背离不得删除或覆盖 EW Phase。
- **Persistence owner**：Phase 使用 EW Persistence；AW Persistence 只属 Capital Confirmation supporting evidence；不得定义 EW/AW joint Persistence。
- 其余 primitives 的 Historical Dynamics 保留为 objective dynamics evidence，其 Interpretation 层角色见 §7.11。

### B. `docs/prd/70-review.md` §7.11 新增「Scope Dynamics Phase 输入架构（FROZEN）」

- **Phase label 语义边界**：Dynamics Phase 六类只描述 Scope 的动力生命周期阶段（现在在哪里 / 往哪里走 / 是否加速 / 是否持续）；Phase owner 不得扩大为「综合所有确认后的整体市场状态」。
- **Parallel confirmation evidence（不得改写 Phase label）**：
  - **Capital Confirmation**：Amount-weighted Return Historical Dynamics；EW/AW 背离 → `not confirmed / divergence evidence`，不删除或覆盖 EW Phase；
  - **Breadth Confirmation**：`advance_ratio` 为 canonical confirmation axis；`decline_ratio` / `unchanged_ratio` 为 supporting evidence，不得作为额外独立 vote；Breadth 仍属 §7.10 Internal Structure 正式组成部分；
  - **Volume Participation Confirmation**：`participation.volume.ratio20` / `ratio200`，无价格方向 owner 权限（Volume positive 不得自动等于 Phase Strengthening）。
  - Confirmation 不得通过多数投票 / 加权 score / 综合分重新产生 Phase。
- **Structure-only inputs（不参与 Dynamics Phase v1 label）**：`return_dispersion`、`price_normalized_hhi`、`amount_normalized_hhi`、Capital Tilt、Leadership Migration，属 Internal Structure context。
- **Excluded from Phase v1**：`trend.continuous.regime_strength`（Phase directional ownership 未冻结，fail-closed 不消费；不删除该 primitive）。
- **11 primitive ≠ 11 phase**：每个 Scope / trade_date **最多一个 Dynamics Phase**；Phase synthesis owner 遵循冻结架构。
- **Deterministic architecture cases（FROZEN，只冻结架构，不冻结 confirmation label enum / threshold）** Case A–D：EW↑+AW↓ → Phase 由 EW 决定、AW=divergence evidence、不因 AW 反向而 null Phase；EW strengthening + Breadth deteriorating → Phase 仍由 EW 决定、Breadth=negative confirmation、不重写 Phase；EW repairing + Volume weakening → Phase 不被 volume 改写、Volume=absent confirmation；HHI rapidly rising + EW flat → HHI 不得投票把 Phase 变成 Strengthening。

### C. `docs/prd/70-review.md` §7.11.1 拆解 Algorithm Mapping 依赖（A/B/C 分层）

Mapping 依赖按层级拆解，**互不阻塞、各自独立 ready**：

- **A. Dynamics Phase Algorithm Mapping 依赖**：L1 canonical facts + EW Historical Dynamics 真实历史数据；EW Historical Dynamics ready 即解锁本层，不等待 Internal Structure / Trading Context。
- **B. Internal Structure Type Mapping 依赖**：Internal Structure 四类事实（Breadth / Capital Tilt / Concentration / Leadership Migration）真实数据；与 Dynamics Phase mapping 并行独立，不等待 A。
- **C. Trading Context Mapping 依赖**：Dynamics Phase（A）与 Internal Structure Type（B）均 ready 后冻结；消费 Phase × Type 组合，为最晚解锁层；但 A/B 未 ready 不阻塞 L1 / L2 / Cross-sectional / Historical Dynamics 开发。
- 分层原则：任一层的 threshold 数据尚未产生时，其余层不得被该层阻塞；已 ready 层可先行冻结，无需等待全链。

## 明确未修改（产品语义保留）

- 六类 Dynamics Phase / 五类 Internal Structure Type / 五类 Trading Context 的**分类名称与语义** = FROZEN PRODUCT CONTRACT，未改动。
- Historical Dynamics EMA / Persistence 数值合同（CHANGE-20260815-001 / -002）未改变。
- 未发明任何 exact threshold / conflict priority / tie-break（仍标 `ALGORITHM MAPPING REQUIRED`）。
- 未删除 `trend.continuous.regime_strength` primitive（仅 Phase v1 不消费）。
- 未新增七类 Phase / 未把 confirmation 改为 score 合成。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- 重新读取修改后的 §7.9 / §7.11 / §7.11.1，逐项确认以下歧义均已消除：
  1. **Phase owner**：EW Historical Dynamics 是唯一 lifecycle primary owner，AW 非第二条主轴；
  2. **AW 影响**：AW 只做 Capital Confirmation，EW/AW 背离不删除不覆盖 EW Phase，不把 Phase null；
  3. **Breadth / Volume 影响**：只做 parallel confirmation evidence，不得改写 Phase label，不参与 vote；
  4. **HHI / Dispersion 参与**：属 structure-only inputs，不参与 Dynamics Phase v1 label；
  5. **Leadership Migration 依赖**：不参与 Phase v1，属 Internal Structure Type Mapping 依赖（§7.11.1-B）；
  6. **每 Scope Phase 数**：每个 Scope / trade_date 最多一个 Dynamics Phase（11 primitive ≠ 11 phase）。
- 仅修改 `docs/prd/70-review.md`、新增本 CHANGE、更新 CHANGE INDEX；未修改任何 Python / Tests / DB / Migration / API / Frontend / Maps / Runbooks / 治理。

## 下一步

- **A 层（Dynamics Phase Algorithm Mapping）**：待 L1 + EW Historical Dynamics 真实历史数据 ready 后，通过 distribution inspection + representative case replay 冻结六类 Phase 的 exact threshold / conflict priority / tie-break。
- **B 层（Internal Structure Type Mapping）**：待 Internal Structure 四类事实真实数据 ready 后冻结；其中 Leadership Migration 的 rank-stability algorithm（Spearman / Top-N overlap / 其他）仍标 ALGORITHM MAPPING REQUIRED。
- **C 层（Trading Context Mapping）**：A、B 均 ready 后冻结五类 Trading Context mapping。
