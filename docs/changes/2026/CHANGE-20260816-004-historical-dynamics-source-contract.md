# CHANGE-20260816-004 — Review v2.3 Historical Dynamics Source Contract Freeze

## 元数据

- 日期：2026-08-16
- 类型：`docs-only`（PRD contract freeze / Analysis B Historical Dynamics 历史 source contract 正式冻结）
- 领域：复盘模块 / `docs/prd/70-review.md`（§7.7.5 / §7.9 / §7.15 / §7.16）
- 授权：用户在 `ref/prompt.md`（Review v2.3 — Historical Dynamics Source Contract Freeze）中明确授权进行 docs-only PRD freeze round
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）
- 备注：CHANGE ID 读取 INDEX 现存最新 20260816 ID（003）后选择下一个有效编号 `004`，未猜测编号。

## 背景

repo 中存在两种合法但语义不同的 historical Scope series：

- **A. Daily persisted Scope Observation**：membership universe = PIT(T)，回答「交易日 T 当时的 Scope 表现如何？」；
- **B. Historical Dynamics reconstruction**：membership universe = CURRENT STATIC，回答「当前 Scope 的这些成员，过去一路怎样演化到今天？」。

Dynamics Phase 产品 contract 使用 **B**。若下一轮直接 `persisted PIT history → ObservationSeries → Dynamics Phase`，shape 完全兼容，但会静默改变产品语义。因此必须先在本轮 PRD SSOT 冻结 Source Contract。

## 变化内容（docs-only）

### A. `docs/prd/70-review.md` §7.7.5 新增「历史 Source 归属与边界（FROZEN）」

- 明确 `ObservationSeries` / `PrimitiveSeries` / `PrimitivePoint` 为**共享数据 shape**，对上游历史 source 物理实现保持解耦；
- 修正「History Service 是所有下游分析唯一历史 source」的潜在歧义，正式拆分四部分：
  1. **Observation Series Shape Owner**：Builder 只负责 align / gap preservation / primitive extraction，**不决定 membership universe**；
  2. **Source Adapter Ownership**：上游 source 必须先依据具体 Analysis 冻结的 universe contract 提供 historical snapshots；
  3. **Analysis B source** = CURRENT STATIC reconstruction（`review_historical_scope_reconstruction_service.py`）；
  4. **Persisted PIT History**：`review_scope_observation_facts` 仍为 daily historical-PIT Canonical Scope Observation history，可用于需要 historical-PIT 事实语义的 consumer，但**不得直接冒充 Analysis B current-static source**；
- 不删除 History Service，仅收紧适用边界。

### B. `docs/prd/70-review.md` §7.9 新增「Historical Membership Universe Contract（FROZEN）」

- `membership_mode = "current_static"`；
- 对 analysis as-of A：member universe **只 resolve 一次**，整段历史 T 固定不变；
- historical fact time = **exact T + canonical T-1** 的真实 canonical facts，再调用 `compute_scope_observation()`；
- **禁止**：PIT(T) membership replacement / historical-asof membership mixing / current member FACT backfill / future facts；
- **Provenance（至少）**：`membership_mode` / `membership_asof_date` / `member_count`；
- **Product meaning**：Historical Dynamics = 「当前成员的历史演化」，不是「每日当时定义下的 Scope 表现」（后者仍合法但不是 Dynamics Phase lifecycle owner）；
- **Accepted recomputation semantics**：同一历史 T 在不同 analysis as-of date 可重算出不同结果，**非数据错误**。

### C. `docs/prd/70-review.md` §7.9 新增「Implementation Boundary」

- 现有 `review_historical_scope_reconstruction_service.py` = current-static semantic owner / foundation，但仍为 **shadow execution path**；
- 下一 implementation = **Current-Static Reconstruction → ObservationSeries → Scope Dynamics** 的 application integration；
- **DO NOT wire** `review_observation_history_service` 的 persisted PIT series 直接进入 Dynamics Phase；
- **Scale note**：reconstruction 物理计算包含 Scope × member × historical trade_date，runtime integration 前 **SCALE GATE REQUIRED**；不发明秒级 SLA（recomputation cadence / current-static result persistence 属后续 Scale / Execution Model Design）。

### D. `docs/prd/70-review.md` §7.15 新增「7.15.2 Current-Static Membership 与 Historical Fact Availability」

- current-static membership **不意味着** member historical fact 必须存在；
- 固定 current member 在历史 T 无 canonical fact → 该 primitive point 仍应 `unavailable`，并保留 trading observation slot；
- **禁止** forward-fill / current-backfill 历史 member facts（该日缺失 = 合法 unavailable，不是 contract violation）。

### E. `docs/prd/70-review.md` §7.16 补充「PIT membership 长期原则 vs Analysis B current-static exception」

- PIT membership 仍是 **Daily Canonical Scope Observation** 的长期架构原则，**不删除**；
- Analysis B Historical Dynamics 是明确冻结的 **derived-analysis exception**（产品问题本身是「当前成员的历史演化」）；
- `current_static` **只冻结在 Analysis B 相关 source contract**，不得无意扩展到 Cross-sectional（§7.8）/ Internal Structure（§7.10）等其他 Review 模块。

## 明确未修改

- 未设计 API schema / frontend / new DB table / new cache / materialization / scheduler / after-close orchestration / runtime SLA / recomputation cadence / current-static result persistence；
- 未重新讨论 Position 120/60、EMA5/20、Persistence 20/15、Dynamics Phase thresholds、EW primary owner、ObservationSeries gap semantics（全部 CLOSED）；
- 未修改 backend/*、frontend/*、migration/*、tests/*、experiments/*、AGENTS.md、rules/*；
- `docs/maps/70-review.md` 未修改（Maps 同步需用户验收后授权）。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- `git diff --stat` / `git status --short`：仅 3 个 docs target（`docs/prd/70-review.md`、`docs/changes/INDEX.md`、`docs/changes/2026/CHANGE-20260816-004-historical-dynamics-source-contract.md`）。
- 重新读取修改后的 §7.7.5 / §7.9 / §7.15 / §7.16，逐项确认（见 Final Report VERIFY 5 项）。
- exact-stage：`git add` 仅列明 3 个文件，禁止 `git add .` / `-A` / `-u`。

## 下一步

- NEXT BLOCKER：`CURRENT-STATIC-DYNAMICS-APPLICATION-INTEGRATION`（Current-Static Reconstruction → ObservationSeries → Scope Dynamics 的 application integration）；**不自行设计或执行**。
- Scale Gate：reconstruction 正式 runtime integration 前必须通过 SCALE GATE（Scope × member × historical trade_date）。
