# CHANGE-20260813-013 — Review L1 Canonical Observation Code Alignment

- 日期：2026-08-13
- 类型：behavior+contract+architecture（Implementation Slice，非 docs-only）
- 领域：复盘模块 / Scope Observation Model / Canonical Observation Core（L1）
- Baseline SHA：`19eab75f103fb28c081d961cfccc12953037698b`（dev HEAD == origin/dev）
- PRD Basis：CHANGE-011（L1 合同补全）、CHANGE-012（L1 合同最终收口）
- Status：`verified_code_pending_acceptance`（PURE_UNIT PASS 64/64；targeted-pg BLOCKED_BY_VERIFY_INFRA_AND_PRE_EXISTING_FAILURE，非代码回归）

## 1. Hypothesis / Vertical Slice

- **Hypothesis**：PRD §7（CHANGE-011/012 后）定义的 L1 Canonical Observation 合同可在 Core 层真实落地：`price.amount` 嵌套拓扑、`normalized_hhi` ACCEPTED 公式、`amount_share` 单 owner 计算。
- **PRD Basis**：CHANGE-011 §7.1（price.amount 嵌套，禁 top-level amount）、§7.2（normalized HHI ACCEPTED、amount_share member-level 单 owner）；CHANGE-012 §7.9.2.1（scope snapshot vs member-level 边界）。
- **Visible Outcome**：`compute_scope_observation()` 返回的 payload 不再含 top-level `amount`；`price.amount` 携带 `{valid_count, total_amount, concentration.{raw_hhi,normalized_hhi,member_count,status}}`；`normalized_hhi` 与 `amount_share` 来自同一 canonical owner。
- **Vertical Slice**：`scope_observation.py`（compute）→ `review_observation_persistence_service.py`（validate/serialize/upsert）→ tests（PURE_UNIT + targeted-pg）。
- **Deferred**：L2 Evidence 扩展、Discovery、Filter/Signal、Cross-Scope、API/Frontend、production deployment、full-market run。

## 2. 核心代码改动

### 2.1 `backend/app/domain/review/scope_observation.py`

1. **`_normalized_hhi(raw_hhi, member_count)`（新增唯一纯函数）**
   - 公式：`(raw_hhi - 1/N) / (1 - 1/N)`，N = member_count > 1。
   - 边界（frozen，与 PRD §7.2 一致）：`raw_hhi is None → None`；`member_count <= 1 → None`；`1 - 1/N <= _EPSILON → None`；仅浮点误差 near [0,1] clamp，越界即 `ValueError`（不静默、不用 abs 修正、不 min/max clamp、不 N=1=1、不 raw None=0）。
2. **`_price_concentration(returns)`（重写）**
   - N = `len(returns)`（含 zero-return member，zero return 仍属 price concentration universe）；
   - `total_abs_return <= _EPSILON → {raw_hhi:None, normalized_hhi:None, status:"zero_abs_return"}`；
   - `N <= 1 → normalized_hhi:None, status:"insufficient_member_count"`；否则 `normalized_hhi=_normalized_hhi(...)`。
3. **`MemberAmountContribution` / `AmountContributionFacts`（新增 frozen dataclass）**
   - 单 member 事实 `(member_id, amount, amount_share)`；scope 聚合 `(valid_count, total_amount, members)`。
4. **`compute_member_amount_contributions(members)`（新增唯一计算 owner）**
   - amount None/NaN/inf/negative → 排除；amount==0 合法成员；
   - total > 0 → 每个 valid member 的 `amount_share = amount/total`，总和 ~= 1；
   - total <= _EPSILON → 所有 `amount_share=None`；
   - 不生成 ranking/TopN/strong-weak、不写 DB、不依赖 legacy attribution。
5. **`_amount_concentration(contribution_facts)`（重写，复用单 owner）**
   - 直接消费 `compute_member_amount_contributions` 产出的 `amount_share` 算 HHI；**不第二套 share 公式**；
   - `total_amount <= _EPSILON → status:"zero_amount"`；`member_count <= 1 → insufficient_member_count`。
6. **`compute_scope_observation()` payload 迁移**
   - 删除顶层 `"amount": {...}`；
   - `price` 下新增 `"amount": {valid_count, total_amount, concentration:_amount_concentration(amount_contribution)}`；
   - `signed_contribution.status = "prd_clarification_required"` 保留；
   - `participation.amount` 保持既有 `amt_ratio20` 分布（不参与 amount_share owner，属独立 PRIMITIVE）。

### 2.2 `backend/app/services/review_observation_persistence_service.py`

- `CANONICAL_TOP_LEVEL_SECTIONS` 移除 `"amount"`（仅 `{scope,price,trend,structure,momentum,participation,chip}`）。
- validator 行为：接受 `price.amount`；拒绝任何 top-level `amount`（extra 校验直接 fail）；**不做 silent compatibility fallback、不做 topology migration、不重新计算 normalized HHI**。
- 仍只 serialize / validate / upsert / read；observation_payload 为 JSONB，无需 migration。

### 2.3 `backend/app/domain/review/scope_evidence.py`

- docstring 修正：删除 `Diffusion remains PROVISIONAL`；改为 "State/Breadth D1/D3/D5 changes are continuous Objective Evidence; Diffusion is not an independent canonical state/primitive"（对齐 CHANGE-011/012）。
- 本轮不扩 `PRIMITIVE_PATHS`（仍 6 primitive；`price_raw_hhi` 路径 `("price","concentration","raw_hhi")` 仍有效）；不修改 `PEER_SCOPE_TYPES`（major_index/style peer 属 L2，留待第五阶段）。

### 2.4 明确不修改

- `backend/app/domain/review/member_fact.py`：未新增 `amount_share`（scope-relative 不回写 scope-free instrument object）。
- `attribution_engine.py` / `MarketReviewSignalInstrument` / `MarketReviewSignalAttribution`：未修改；`amount_share` 不写入 `contribution_payload`（legacy Signal-linked attribution，V2 未冻结）。
- `docs/prd/70-review.md` / `docs/maps/*` / API / Frontend / Filter / Signal / Discovery / migration / schema：均未改。

## 3. 测试改动

- `tests/test_review_scope_observation.py`：
  - family divergence 断言：去顶层 `"amount"`，加 `assert "amount" not in out` / `assert "amount" in out["price"]`；
  - amount universe 断言改 `out["price"]["amount"]["valid_count"]`；
  - §18.18/19 重写为单 owner 验证（valid_count=4、total=600、shares 1/6,1/2,1/3,0、sum=1、HHI from same owner）；
  - `test_core_does_not_generate_pqucv` 去顶层 amount 断言；
  - numeric safety：nan/negative/inf amount excluded（新增 inf）、zero amount valid，路径改 `price.amount`；
  - 新增 normalized HHI 边界：equal→0、concentrated→1、N=1→None/insufficient_member_count、zero price→None/zero_abs_return、zero amount→None/zero_amount + amount_share all None。
- `tests/test_review_scope_evidence.py`：fixture top-level `amount` 移入 `price.amount`（不扩 PRIMITIVE_PATHS；Current/D1/D3/D5/Historical/Peer 测试保持 PASS）。
- `tests/test_review_observation_persistence_pg.py`：新增 `test_persisted_payload_uses_price_amount_topology`（assert 无顶层 amount、price.amount 嵌套 + raw/normalized HHI round-trip）、`test_save_rejects_legacy_top_level_amount`（人为 `obs["amount"]=obs["price"].pop("amount")` 必须抛 `ScopeObservationPayloadValidationError`）。

## 4. 验证证据

### 4.1 PURE_UNIT（本地，PURE_UNIT_TEST=1，禁 PG/network）

- `tests/test_review_scope_observation.py` + `tests/test_review_scope_evidence.py`：
  - **64 passed**（postgres=0, pure_unit=64, external_data=0）。
- ruff（venv）：modified files 无 error（已清理未使用 import）。
- compileall：4 个 modified source 模块 COMPILE_OK。

### 4.2 Targeted PG Verification（panji-verify targeted-pg，bz_stock_verify_<SHA>）

- 已执行：`panji-verify run --sha 10dac74... --plan targeted-pg`（EXIT=50，fail-closed）。
- **关键事实 1（测试覆盖缺口）**：`verify_attempt.py::run_self_contained_pg_tests` 的 registered suite **硬编码**只跑 4 个文件
  （`test_pg_atomic_publication.py` / `test_pg_projection_lifecycle.py` / `test_pg_100_stock_call_counts.py` /
  `test_pg_review_runtime_blocker_closure.py`）。本轮新增的 `tests/test_review_observation_persistence_pg.py`
  **不在 registered suite 内**，因此 targeted-pg 实际**未运行**我的 L1 PG 测试（insert / read-back / idempotent /
  legacy top-level amount reject / price.amount normalized HHI round-trip / legacy P/Q/U/C/V isolation）。
- **关键事实 2（pre-existing 无关 failure）**：gate 报告的唯一失败是
  `test_pg_review_runtime_blocker_closure.py::test_query2_projected_result_supports_build_stock_state`，
  错误 `build_stock_state() missing 1 required positional argument: 'symbol'`。
  该 test 最近修改于 `538bc95`（远早于本轮 baseline 19eab75），`build_stock_state` 当前签名为
  `(snapshot, run, symbol)`（见 `app/schemas/stock_state.py:445`），test 仍用旧调用 `(snapshot, symbol)`。
  **此为与本轮 L1 Canonical Observation 无关的 pre-existing 测试漂移，非本轮 regression**。
- **结论**：targeted-pg gate 因 registered suite 中的 pre-existing 无关 failure 而 fail-closed；我的 L1 代码
  未被该 gate 实际验证（测试未被收集）。PG 证据状态：**blocked_by_verify_infra_and_pre_existing_failure**，
  非代码缺陷。
- **待用户授权**：将 `test_review_observation_persistence_pg.py` 注册进 `verify_attempt.py` 的 registered
  PG suite（属受保护治理域 `scripts/verify/`，修改需用户显式授权），或授权在 `bz_stock_verify_<SHA>` 上
  通过 `verify_exec.py` 手动运行该文件以取得真实 PG 证据。

## 5. 历史数据处理

- 不 migration 已有 observation_payload；
- 不 SQL UPDATE 全表；
- 不做 `payload["price"]["amount"] = payload["amount"]` fallback；
- 不双写 old/new topology、不兼容别名；
- 旧实验数据保留原样作历史审计证据；后续若需历史 amount Evidence 另开 backfill slice。

## 6. 范围与禁止项确认

- 未扩 L2 Evidence / PRIMITIVE_PATHS；
- 未开始 Discovery / Filter / Signal；
- 未修改 API / Frontend；
- 未建表 / 未 migration / 未改 schema；
- 未部署 / 未跑 full-market / Scheduler / Worker。

## 7. 关联

- 上游合同：CHANGE-011、CHANGE-012（PRD §7.1/§7.2/§7.9.2.1/§7.9.3）。
- 下游（未本轮）：第三阶段B 代码收口后，member-level `amount_share` physical persistence 仍 `IMPLEMENTATION DESIGN REQUIRED`；major_index/style peer 对齐留第五阶段。

## 8. External Audit Correction（第 3C 轮，同 implementation slice，非新 CHANGE）

- Status 保持：`verified_code_pending_acceptance`。
- **审计发现 PG test expectation defect**：
  - `tests/test_review_observation_persistence_pg.py::test_persisted_payload_uses_price_amount_topology`
    fixture 两个成员 amount = 100 / 200 → total=300 → shares 1/3, 2/3。
  - 正确 raw HHI = (1/3)² + (2/3)² = 5/9 ≈ 0.5556；正确 normalized HHI = (5/9 − 1/2)/(1 − 1/2) = 1/9 ≈ 0.1111。
  - 原测试错误写成 `raw_hhi == 0.5`、`normalized_hhi == 0.0`。
  - 该测试此前**未被** registered PG suite 收集（见 §4.2 关键事实 1），因此错误未被运行暴露。
  - 已修复测试：改为 `amount_concentration["raw_hhi"] == pytest.approx(5.0/9.0)`、`["normalized_hhi"] == pytest.approx(1.0/9.0)`。
- **补充 PURE_UNIT 回归**：`tests/test_review_scope_observation.py::test_amount_hhi_two_member_unequal_distribution`
  直接固化 amount 100/200 的数学期望（valid_count=2, total=300, raw=5/9, normalized=1/9, member_count=2, status=ready），
  作为单一真相，防止 PG fixture 再次写错。不创建第二套 HHI helper。
- **核心业务实现未因该问题修改**：`scope_observation.py` / `review_observation_persistence_service.py` 本轮零改动（审计确认 `_normalized_hhi` / `_amount_concentration` / `compute_member_amount_contributions` 实现正确，错误仅在测试期望值）。
- **PG execution evidence 仍为 UNVERIFIED**：原因同上轮 §4.2，即 verification registration gap（registered suite 仍不含该 PG 文件），非代码缺陷。
- 本轮**未**执行 targeted-pg（避免违反 Two-Strike 的重复无效执行；registered suite 的 pre-existing 无关 failure 仍存在）。
- 本轮**未**修改受保护治理域 `scripts/verify/*` 与 `scripts/ops/panji-verify`。
- PURE_UNIT 复跑：`65 passed`（较上轮 64 +1，新增 unequal-distribution 回归）。
