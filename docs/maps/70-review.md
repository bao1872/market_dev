# Map: Review Scope Observation 当前实现（70-review）

> **本文件状态：ACTIVE DOMAIN MAP — 重建于 2026-08-24（G1 Coverage Audit 后）**
>
> 这是 Review 当前实现的**事实映射**，不是目标合同。目标合同唯一源是 `docs/prd/70-review.md`（v2.3）。
> 本文件在 **milestone boundary** 同步（见 `rules/60` §10.4），不在每次小 slice 提交时更新。
>
> 上次遗留的 legacy V1 描述（P/Q/U/C/V、五阶段 UI、“V1 已完整实现”）已于本次重建中删除，
> 因其与 v2.3 PRD 语义与 R2A/R2B/R2C 当前代码严重冲突，是本次认知漂移的制度性原因。

---

## 1. 架构主线（已核验当前实现）

```
第一金字塔 + 行情 + Scope PIT membership
        ↓
L1 Scope Facts  (compute_scope_observation @ scope_observation.py:1298)
        ↓ 落库 ReviewScopeObservationFact (migration 090, grain = trade_date+scope_type+scope_key)
        ↓
唯一编排入口：review_orchestrator_service.py::compute_run / resume_run
        ↓ 逐 scope 调 _persist_canonical_scope_observation (review_orchestrator_service.py:1462)
        ↓
唯一 Composition owner：canonical_composition.py::compose_canonical_review_scope
   固定 5 层：scope_observation / historical_dynamics / internal_structure_facts / leadership / member_attribution
   （明确 no score / threshold / Internal Structure Type / Trading Context）
        ↓
Snapshot 持久化：ReviewScopeCompositionSnapshot.composition_payload
        ↓
API（只读 5 GET，app/api/review.py）：
   /dates  /latest  /{trade_date}/overview  /{trade_date}/scopes  /{trade_date}/scopes/{scope_type}/{scope_key}
        ↓
前端 Scope-first 研究终端（6 Detail Tab）
```

**关键约束（已核验）**：
- Review 计算**只在** `compute_run`/`resume_run` 编排发生；API 是 read-only，不触发计算。
- `L1 Scope Facts` 已双写落库（090）+ Composition 投影。
- `L2 Observation Groups` / `Cross-sectional` / `Evidence` 后端代码已就绪，但**未接线到编排主链与用户 API**（见 §3）。

---

## 2. Active Domain Coverage Matrix（核心表）

状态取值：
- **DONE** — backend owner + persistence + API + frontend formal surface + tests + representative runtime 全链闭合
- **PARTIAL** — 链存在但某一环节缺失或不完整（标注缺口）
- **MISSING_UI** — backend 已就绪（含可能 runtime-wired），但无 frontend formal surface
- **MISSING** — 完全未实现
- **UNAVAILABLE_BY_DESIGN** — PRD 明确 ALGORITHM MAPPING REQUIRED / NEXT，当前不实现是设计意图
- **AUDIT** — 需进一步运行时核验

| Product Contract | Backend Owner | Persistence | API | Frontend | Tests | Runtime | 状态 |
|---|---|---|---|---|---|---|---|
| L1 Scope Facts | scope_observation.py:1298 | Fact (090) | scopes / scopes/{type}/{key} | Facts Tab (raw) | yes | yes | DONE |
| L2 Observation Groups (8组) | observation_groups.py:136 + review_observation_group_service.py | derived (不落库) | **无端点** | **MISSING**（无 8 组卡片） | backend only | unit only | **MISSING_UI** |
| Analysis: Cross-sectional | cross_sectional.py + review_cross_sectional_service.py | derived | **无端点** | PARTIAL（仅 Explorer 排序+Trajectory 散点） | backend only | unit only | PARTIAL |
| Analysis: Historical Dynamics (Pos/Vel/Acc/Pers) | review_scope_dynamics_service.py:128 → scope_dynamics.py:65 | composition_payload | scopes/{type}/{key} | Dynamics Tab（P/V/A 图） | yes | yes | DONE |
| Analysis: Internal Structure Dynamics (Breadth/Tilt/Concentration/Leadership Migration) | internal_structure.py:93 / leadership_migration.py | composition_payload | scopes/{type}/{key} | Internal Tab + Leadership Tab | yes | yes | DONE |
| Interpretation: Dynamics Phase (6类, FROZEN) | dynamics_phase.py:252（经 Historical Dynamics 链） | composition_payload | scopes/{type}/{key} | 标签展示（无独立分类 UI） | yes | yes | PARTIAL |
| Interpretation: Internal Structure Type (5类, ALGORITHM MAPPING REQUIRED) | 无实现模块（仅 candidate JSON + test） | — | 无 | MISSING | candidate only | no | UNAVAILABLE_BY_DESIGN |
| Trading Context (5类) | 零后端文件 | — | 无 | MISSING | no | no | UNAVAILABLE_BY_DESIGN |
| Member Attribution (canonical) | analysis/member_attribution.py:198 | composition_payload | scopes/{type}/{key} | Attribution Tab（5 子 Tab） | yes | yes | DONE |
| Member Attribution (legacy Signal) | review_attribution_service.py | MarketReviewSignal* 表 | 无存活端点 | 退休 | legacy | no | MISSING（已退休） |
| Evidence (Objective Evidence) | scope_evidence.py + scope_evidence_service.py:94 | derived | **无端点** | PARTIAL（仅 RawFactsPanel + AuctionBackflowPanel 原始形态） | backend only | unit only | PARTIAL |
| Scope Explorer（家族 tab / 11 排序 / 薄列表 / Trajectory） | review_observation_persistence_service.py (family filter R2C closed) | Fact + Composition LEFT JOIN | /scopes | PRESENT | yes | yes | DONE |
| Scope Family Snapshot（R2A/R2C） | 同上 | 同上 | /scopes?scope_type= | PRESENT | yes | yes | DONE |

### 2.1 Slice PASS 记录（本 map 重建前已完成）

| Slice | SLICE STATUS | PRODUCT COVERAGE IMPACT | Remote SHA |
|---|---|---|---|
| R2A Scope Explorer Multi-Sort | PASS | Scope Explorer: 前端 7→11 排序，URL SSOT | 08715d4c |
| R2B Observation Thin Projection | PASS | Scope 薄列表增加 observationSummary（8 标量），OWNER BOUNDARY 分离 | 141e3b6f |
| R2C Family SQL Filter Closure (P0) | PASS | 修复 count/page family 谓词不一致（Slice-B 遗留） | b601e762 |
| R2D Thin Contract Test Closure | PASS | 测试基线全绿（消除过期 thin-SQL 断言 + 补 R2B response size） | d485a599 |

**Review overall: still PARTIAL**（见 §3 缺口）。

---

## 3. 当前真实缺口（G1 Audit 结论）

### 3.1 Backend 已代码就绪但**未接线到 API / 前端**
- **L2 Observation Groups (8 组)**：`build_l2_observation_groups` 仅在薄服务 `review_observation_group_service.py` 调用，该服务未挂 API/orchestrator。前端无 8 组观察卡片 UI。
  → 这是 **R3（Canonical Observation Frontend Completion）** 的核心缺口。
- **Cross-sectional Analysis**：`compute_cross_sectional` 仅 unit/service 测试；无独立横截面对比面板（PRD §7.8.1 明确不落代码/API，但前端仅 Explorer 排序覆盖）。
- **Evidence**：`compute_scope_evidence` 仅被 experimental_filter 实验路径调用，未挂主链/API。

### 3.2 Backend 实现但前端无 formal surface / 无独立分类 UI
- **Dynamics Phase (6类)**：作为 Historical Dynamics 计算链一环 runtime-wired，但前端仅作标签展示，无独立“Phase 分类/解释”面板。
- **Trading Context / Internal Structure Type**：PRD 标 ALGORITHM MAPPING REQUIRED / NEXT，后端零实现，前端零 UI —— 属 `UNAVAILABLE_BY_DESIGN`，非回归。

### 3.3 文档/代码不一致（已发现，待修）
- `review_scope_dynamics_service.py` 模块注释自称 "NOT_RUNTIME"，但 orchestrator:1185 实际已调用它。以代码为准（功能已接线），注释需更新（Deferred，非当前 blocker）。

---

## 4. 后续路线（用户 2026-08-24 指令）

```text
G0  Governance correction          DONE (rules/60 §10)
G1  Review Product Traceability   DONE (本 map)
M0  Rebuild 70-review.md          DONE (本文件)
 ↓
R3  Canonical Observation Frontend Completion
     ├ R3A  Observation API contract (优先复用 build_l2_observation_groups，不前端重建 L2 mapping)
     ├ R3B  Current workspace shell
     ├ R3C  Price / Trend
     ├ R3D  Structure
     ├ R3E  Momentum / Volume
     └ R3F  Current completeness closure
     R3-Closure 必须证明：8/8 PRD Observation Groups → backend canonical owner → API
       → frontend formal surface → null/unavailable truthful → tests → representative runtime
 ↓
R4  Analysis Completion (Cross-sectional / Historical Dynamics closure / Internal Structure closure)
 ↓
R5  Interpretation / Trading Context (only frozen contracts)
 ↓
R6  Evidence Workflow + Fact-only Detail
 ↓
R7  Market-level Review Overview
 ↓
R8  Browser / Real-data Product Acceptance
 ↓
R9  Visual / Interaction Polish
```

### 4.1 治理保护（不得违反）
- **Product semantic frozen ≠ exact algorithm frozen**：Internal Structure Type / Trading Context 的 threshold / conflict priority / tie-break 未冻结前，前端不得自行拼“强势趋势”“主升”“机会”等判断。
- R3 不得因 8 个组件“都有数据”就 PASS；必须证明 8/8 全链闭合（见 §2 + §10）。

---

## 5. 核验来源（本次 map 重建事实基础）

- 后端：`app/api/review.py`、`review_orchestrator_service.py`、`canonical_composition.py`、`observation_groups.py`、`analysis/*`、`scope_evidence.py`、`review_observation_persistence_service.py`
- 前端：`frontend/src/features/review`（ScopeExplorer*、ScopeDetailTabs、Scope*Panel、types.ts）
- PRD：`docs/prd/70-review.md` v2.3 §2 / §7 / §22
- 本 map 最后完整核验：2026-08-24（G1 Coverage Audit）
