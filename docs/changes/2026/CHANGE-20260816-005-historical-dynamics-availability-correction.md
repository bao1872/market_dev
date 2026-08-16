# CHANGE-20260816-005 — Review v2.3 Historical Dynamics Availability Correction

## 元数据

- 日期：2026-08-16
- 类型：`docs-only`（PRD contract correction / member-level vs scope-level availability 错误传播纠正）
- 领域：复盘模块 / `docs/prd/70-review.md`（§7.15.2）
- 授权：用户在 `ref/prompt.md`（Review v2.3 — Historical Dynamics Source Contract Availability Correction）中明确授权进行 docs-only contract correction round
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）
- 备注：CHANGE ID 读取 INDEX 现存最新 20260816 ID（004）后选择下一个有效编号 `005`，未猜测编号。

## 关系

**89b320e（CHANGE-20260816-004 Historical Dynamics Source Contract Freeze）的主体保留不变**：

- `membership_mode = "current_static"` 只 resolve 一次、整段历史 T 固定；
- historical fact time = exact T + canonical T-1；
- 禁止 PIT(T) replacement / historical-asof mixing / current fact backfill / future facts；
- Provenance（membership_mode / membership_asof_date / member_count）；
- 产品语义 = 「当前成员的历史演化」，PIT daily principle 保留，current_static 仅冻结在 Analysis B；
- Implementation Boundary：DO NOT wire persisted PIT series 直接进 Dynamics Phase；reconstruction 仍 shadow，下一实现 = Current-Static Reconstruction → ObservationSeries → Scope Dynamics application integration。

本轮**只纠正**一处 Decision Correctness gap：member-level historical fact availability 与 Scope-level primitive availability 之间的**错误传播**。

## Root Cause（错误点）

原 §7.15.2 错误地把「某个 current-static member 在历史 T 缺 canonical fact」直接等价为「Scope PrimitivePoint unavailable」。这是错误的层级传播：

```
（错误）ANY_MEMBER_MISSING → SCOPE_PRIMITIVE_UNAVAILABLE
```

实际 canonical owner（`backend/app/domain/review/scope_observation.py`）使用 **field-specific valid universe**：

- Equal-weight Return 只消费 `price_candidate ∩ finite exact-T1 return`；
- 一个 member 缺 return 只从该 primitive 的 valid denominator 排除；
- 只要仍存在 valid returns，Scope `equal_weight_return` 仍是 finite canonical value。

ObservationSeries owner（`backend/app/domain/review/analysis/observation_series.py`）：PrimitivePoint `available` 由 canonical Scope payload 经 registry extraction 是否得到 finite scalar 决定，**不是**由某个 member 是否缺 fact 决定。

## Corrected Contract（§7.15.2 已改写）

Current-static membership **只固定 MEMBER UNIVERSE**，不改变 L1 Canonical Scope Observation 已有的 field-specific valid-universe denominator / availability semantics：

1. 固定 current member 在历史 T 缺某个 canonical member fact → 该 member 的该字段保持 `unavailable` / missing；
2. 禁止 forward-fill / current-backfill / future fact / 其他日期替代；
3. Scope aggregate 继续由 `compute_scope_observation()` 按该字段既有 canonical valid-universe semantics 计算；
4. Member-level missing **不得自动升级**为整个 Scope snapshot unavailable 或整个 PrimitivePoint unavailable；
5. 只有当 canonical Scope aggregate 对目标 primitive **最终得到** `None` / non-consumable value 时，ObservationSeries 对该 T 输出 `value = None, available = False`；
6. 无论 primitive 最终 available / unavailable，canonical trading-date slot 都必须保留。

三层分离不得合并：

- Current-static 决定 **WHO** is in the universe；
- Canonical L1 aggregation 决定 **WHICH** members are valid for each field；
- ObservationSeries 从 resulting Scope primitive value 决定 availability。

并新增极短示例（100 成员 / 95 valid returns / 5 缺 exact-T1）：5 个 member 不进入 EW valid universe，EW Return 仍由 95 个 valid returns 计算 → PrimitivePoint 仍可 `available = true`；仅当 valid universe 最终为空导致 Scope `equal_weight_return = None` 时才 `available = false`。示例中的 95/100 **不是冻结的 coverage threshold**。

## 明确未修改

- **No algorithm change**；
- **No current-static decision change**；
- **No ObservationSeries algorithm change**；
- **No L1 aggregation change**；
- 未重新讨论 CURRENT STATIC membership / PIT vs current-static source / Position 120/60 / EMA5/20 / Persistence 20/15 / Phase thresholds / EW Phase owner / ObservationSeries gap preservation / Scope valid denominator algorithms（全部保留）；
- 未修改 backend/*、frontend/*、tests/*、migration/*、experiments/*、AGENTS.md、rules/*；
- `docs/maps/70-review.md` 未修改（Maps 同步需用户验收后授权）。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- `git diff --stat` / `git status --short`：仅 3 个 docs target（`docs/prd/70-review.md`、`docs/changes/INDEX.md`、`docs/changes/2026/CHANGE-20260816-005-historical-dynamics-availability-correction.md`）。
- 重新读取修改后的 §7.15.2，逐项确认（见 Final Report Validation A–H）。
- exact-stage：`git add` 仅列明 3 个文件，禁止 `git add .` / `-A` / `-u`。

## 下一步

- NEXT BLOCKER：`CURRENT-STATIC-DYNAMICS-APPLICATION-INTEGRATION`（Current-Static Reconstruction → ObservationSeries → Scope Dynamics 的 application integration）；**不自行设计或执行**。
- Scale Gate：reconstruction 正式 runtime integration 前必须通过 SCALE GATE（Scope × member × historical trade_date）。
