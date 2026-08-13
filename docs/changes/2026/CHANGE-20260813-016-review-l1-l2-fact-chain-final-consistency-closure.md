# CHANGE-20260813-016 — Review L1-L2 Fact Chain Final Consistency Closure

- 日期：2026-08-13
- 类型：behavior+contract+architecture（Implementation Slice / Consistency Closure）
- 领域：复盘模块 / Canonical Observation（L1）+ Objective Evidence（L2-A）/ PRD 70-review.md
- 状态：`verified_code`（PURE_UNIT 87/87；ruff/compileall PASS；无 migration、未改 L1 业务逻辑、未进 Discovery/Filter/Signal、未改 API/前端）
- 关联 PRD：`docs/prd/70-review.md`（§7.2/§7.3/§7.9.3；CHANGE-013/014/015）
- 关联 Maps：`docs/maps/70-review.md`（未修改；Maps 同步需用户验收后授权）

## 1. 背景

L1 Canonical Observation（CHANGE-013）与 L2 Objective Evidence（CHANGE-014 Core 29 + CHANGE-015 Transition 24 = 53）核心功能已完成。本轮不新增任何 Evidence 算法，只处理两个已确认的残留并做最终 Requirement Traceability：

1. `scope_evidence.py` 中重复定义 `PRIMITIVE_NAMES`（PRIMITIVE_PATHS 后有一处旧 `tuple(PRIMITIVE_PATHS)` 定义，Transition spec 后才是唯一正式定义）；
2. PRD 中仍把已完成的第三阶段B工作写成"待实现"。

## 2. 代码修改（唯一一处）

文件：`backend/app/domain/review/scope_evidence.py`

- 删除 PRIMITIVE_PATHS 后的旧定义及其"Phase-1 primitive order"注释：
  ```python
  # Phase-1 primitive order for deterministic iteration / output.
  PRIMITIVE_NAMES: tuple[str, ...] = tuple(PRIMITIVE_PATHS)
  ```
- 只保留唯一正式定义（位于 TRANSITION_PRIMITIVE_SPECS 之后）：
  ```python
  # Deterministic merge: 29 CORE scalar facts + 24 transition ratio facts = 53.
  PRIMITIVE_NAMES: tuple[str, ...] = (
      *PRIMITIVE_PATHS.keys(),
      *TRANSITION_PRIMITIVE_SPECS.keys(),
  )
  ```
- 未改变：29 CORE mapping、24 Transition mapping、顺序、Evidence 数学、peer 逻辑、`extract_primitive` 行为。

## 3. PRD 修改（仅状态收口，不重新设计业务）

- §7.2 amount_share 当前状态：由"Core 仅在内部 HHI 计算时产生 shares，尚无正式 amount_share 输出字段 → 待第三阶段B 代码对齐"改为：
  Canonical Scope Core 已实现 `compute_member_amount_contributions()`；（member_id, amount, amount_share）已有唯一 Scope-relative compute owner；Amount HHI 复用同一份 amount_share；完整 member vector 仍不进 observation_payload；member-level physical persistence 仍标 IMPLEMENTATION DESIGN REQUIRED。**计算 owner 已完成与持久化 owner 尚未定义必须分开**。
- §7.2 顶部 `price.amount` shape 块末尾：原 Architecture Drift 警告（"当前实现中存在 top-level amount 属于已确认的 Architecture Drift...后续代码 slice 需要迁移"）更新为"该迁移已实现：canonical top-level amount 已切除，Amount Contribution/Concentration 归属 price.amount 嵌套结构；persistence validator 拒绝 legacy top-level amount；formal PG 已验证"。
- §7.2 normalized HHI ACCEPTED CONTRACT：状态由"ACCEPTED（已进入实现范围，第三阶段B 按此落地）"改为"ACCEPTED 且已实现"。
- §7.9.3 合同缺口块（原"待第三阶段B 代码对齐"）：
  - Amount payload topology：已实现（price.amount 嵌套、top-level amount 已切除、validator 拒绝 legacy、formal PG 已验证）；
  - normalized HHI：ACCEPTED CONTRACT 已实现（Price/Amount 均保存 raw + normalized，N<=1/zero-total 边界已落地并测试）；
  - amount_share：compute owner 已完成，member-level physical persistence 仍 IMPLEMENTATION DESIGN REQUIRED。

## 4. L1 → L2 最终一致性 RTM（Requirement Traceability Matrix）

状态语义：PASS=需求→L1计算→L1持久化→L2证据全链一致；PASS-DEFERRED=合同已定、部分持久化/物理 owner 待定；MISSING=缺失；CONFLICT=冲突；UNVERIFIED=未验证；PARTIAL=部分。

| 维度 | 事实 | PRD | L1 compute | L1 persistence | L2 Evidence | status |
|---|---|---|---|---|---|---|
| PRICE-Return Level | mean/median/p25/p75 | §7.2 | PASS | PASS | PASS (Current/D1/D3/D5/Hist/Peer) | PASS |
| PRICE-Return Distribution | (above/below thresholds) | §7.2 | PASS | PASS | 非 primitive（分布派生，未单列） | PASS |
| PRICE-Breadth | advance/decline/unchanged ratio | §7.2 | PASS | PASS | PASS | PASS |
| PRICE-Concentration | raw/normalized HHI | §7.2/§7.9.3 | PASS | PASS | PASS（peer raw disabled） | PASS |
| PRICE-Amount Concentration | amount raw/normalized HHI | §7.2/§7.9.3 | PASS | PASS | PASS（peer raw disabled） | PASS |
| PRICE-Signed Contribution | signed_return_contribution | §7.2 | PASS-DEFERRED（code status=prd_clarification_required） | PASS-DEFERRED | 未进 Evidence | PASS-DEFERRED |
| TREND-State/Breadth | up/neutral/down ratio | §7.3 | PASS | PASS | PASS | PASS |
| TREND-Transition | 6 ratio | §7.3（4B合同） | PASS | PASS（sparse） | PASS | PASS |
| STRUCTURE-Swing State/Breadth | up/neutral/down ratio | §7.3 | PASS | PASS | PASS | PASS |
| STRUCTURE-Swing Transition | 6 ratio | §7.3 | PASS | PASS | PASS | PASS |
| STRUCTURE-Internal State/Breadth | up/neutral/down ratio | §7.3 | PASS | PASS | PASS | PASS |
| STRUCTURE-Internal Transition | 6 ratio | §7.3 | PASS | PASS | PASS | PASS |
| MOMENTUM-State/Breadth | expanding/flat/contracting ratio | §7.3 | PASS | PASS | PASS | PASS |
| MOMENTUM-Transition | 6 ratio | §7.3 | PASS | PASS | PASS | PASS |
| PARTICIPATION-Volume Distribution | p25/p50/p75 | §7.2 | PASS | PASS | PASS | PASS |
| PARTICIPATION-Amount Distribution | p25/p50/p75 | §7.2 | PASS | PASS | PASS | PASS |
| CHIP | unresolved/unavailable | §7.9.3 | PASS（status only） | PASS（status only） | PASS（status only） | PASS（unresolved by design） |
| amount_share member vector | (member_id,amount,amount_share) | §7.2/§7.9.2.1 | PASS（compute owner） | DEFERRED（physical persistence owner） | 不进 scope payload | PASS-DEFERRED |

**RTM 结论**：无新增 MISSING / CONFLICT。唯一 deferred 项为 Signed Contribution（PRD clarification）与 amount_share member-level physical persistence（implementation design），二者在之前 CHANGE 已明确标 deferred，本轮不是新引入。

## 5. 53 内部 Evidence facts 最终状态

- 29 CORE scalar（PRIMITIVE_PATHS）+ 24 Transition ratio（TRANSITION_PRIMITIVE_SPECS）= **53 内部 numeric extraction facts**。
- **53 是内部 numeric evidence facts，不是产品指标数量**（无 score/ranking/Filter/Signal/Discovery）。

每类 Context 覆盖：

| Context | 覆盖 | 例外 |
|---|---|---|
| Current | 53 全 | — |
| D1 | 53 全（delta） | missing exact D1 → unavailable |
| D3 | 53 全（delta） | missing exact D3 → unavailable |
| D5 | 53 全（delta） | missing exact D5 → unavailable |
| Historical | 53 全 | <60 样本 → insufficient_history |
| Peer | 53 全 | price_raw_hhi unavailable、amount_raw_hhi unavailable、market 无 peer（no_cross_sectional_peer） |

## 6. 验证

- ruff：All checks passed
- compileall：OK
- PURE_UNIT_TEST=1 pytest tests/test_review_scope_observation.py tests/test_review_scope_evidence.py：**87 passed / 0 failed**（与 CHANGE-015 基线一致，无测试数量变化；测试文件未修改）
- 未跑 PG：本轮无 DB/persistence/query 变化

## 7. 边界（未做）

- 未新增 Evidence 算法
- 未修改 scope_observation.py 业务逻辑
- 未修改 scope_evidence_service.py 业务逻辑
- 未新增/删除 primitive、未改 percentile、D1/D3/D5、Transition、peer cohort、persistence、DB、API/前端
- 未进入 Discovery / Filter / Signal

## 8. 结论

L1/L2 客观事实链 = READY FOR DISCOVERY PRODUCT DESIGN（底层事实准备就绪；不代表已设计 Discovery）。
