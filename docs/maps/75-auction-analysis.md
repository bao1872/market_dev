# 竞价分析 Map（Auction V3.2 — Scope Observation）

核验状态：**代码层已跨过 milestone boundary（Backend Source Candidate CLOSED）**；生产双源 / PG / 部署仍阻断或未运行
最后更新：2026-09-02
核验分支：`f78b5aa9`（= `067b5016` 源码 + 一个空 evidence checkpoint，无任何文件变化）
对应 PRD：`../prd/75-auction-analysis.md`（**V3.2 — Overnight Repricing Observation**，2026-09-01 冻结）
事实所有权：竞价分析层实现状态

> **本轮更新说明（2026-09-02）**：此前本 Map 明确自述为「legacy implementation baseline」，
> 未随 V3.2 production wiring 完成而更新。现在 V3.2 production 主链（input loader → writer/orchestrator
> → scheduler lane → publication read-model → API）已落地并跨过 milestone，故本 Map 重写为 **V3.2 ACTUAL**。
> 此更新只记录「已经实现什么」，不重新设计架构、不修改 PRD、不引入新 contract。
>
> **Runtime 仍写 NOT_RUN**：本 Map 不声称任何生产/PG/浏览器验收。生产双源、PG Integration、
> 部署、真实数据 E2E 全部 `DEFERRED_FOR_EXPLORATION` / `NOT_RUN`。

---

## 0. 核验状态与边界（诚实声明）

| 项目 | 状态 | 事实 |
|---|---|---|
| 源码里程碑 | **CLOSED** | KPI-2 Lifecycle/Fencing = CLOSED_FOR_EXPLORATION；KPI-3 Loader / KPI-4 Writer-Orchestrator / KPI-5 Scheduler / KPI-6 Contracts = CLOSED_FOR_SOURCE |
| 静态核验（IDE evidence） | verified | `ruff` / `py_compile` clean；Targeted V3.2 `304 passed` |
| Full PURE_UNIT regression | genuine = 0 | raw new failures = 79，但 100% 落在 BASE 当时 collection/import 报错的模块（测试从未运行到 outcome），可比集合内无新增失败/collection error |
| Source Gate | PASS | 对 exact SHA `067b5016` 三轮远端源码审计 |
| PG Integration | `DEFERRED_FOR_EXPLORATION` | 不跑真实 PostgreSQL；纯单测 + PURE_UNIT_TEST=1 |
| 生产双源真值 | `NOT_RUN` | 不写 `bz_stock`、不部署、不跑本地 Worker |
| 历史 120 日 bootstrap | `NOT_RUN` / `REQUIRES_AUTHORIZATION` | `historical_auction_backfill_writer` 已隔离落库但当前无消费方；运行时数据问题，报告 `RUNTIME_HISTORY_BOOTSTRAP = NOT_RUN` |
| Migration | `NOT_RUN`（零 migration） | 全部 canonical payload 承载于 `AuctionScopeResult.payload` JSONB；`schema_version` / `algorithm_version` 写入 payload |
| 前端 V3.2 Workspace | **已实现**（本轮） | tsc clean / build OK / 6 契约测试 pass；见 §6.2 |

---

## 1. V3.2 产品对齐（来自 PRD，非本 Map 新增）

- 事实链：`AuctionFinalQuote → Auction Member Facts → Canonical Scope Membership → Scope L1 → Historical/Cross-sectional/Internal Structure → Member Contribution → Publication → Scope API → List Workspace`。
- 只正式分析 `industry` 与 `concept`，**平行且禁止合并 peer universe**；核心事实仅 **PRICE = Auction Gap** 与 **PARTICIPATION = Auction Amount**；Volume 仅留 raw/source 与 legacy persistence，不进正式分析主轴。
- 不比较 Review：`Review(T-1) → Auction(T)` 的 NEW/PERSIST/DECAY/REVERSE/CONFLICT/QUIET 与 Attention Redistribution 不实现。与 Review 共享 spec-driven 数学原语，但**不共享业务 fact**。
- 不变量 INV-01…06：一事实一 owner（frontend/API/test 均不得重算或复制公式）；industry/concept 同算法（membership adapter + 同一 calculator，禁止按 scope_type 分支）；Missing ≠ Zero；baseline 严格 `< T` 且无未来数据；5 分钟快读（一次取完整 family snapshot，前端本地 filter/sort/paginate，禁 backend Top10 与逐行 N+1）。

---

## 2. 数据模型（既有表，零新增 migration）

V3.2 复用既有 `auction_*` 表，关键承载点：

| 表 | V3.2 用途 |
|---|---|
| `auction_final_quotes` | raw fact 源（final_price / prev_close / amount；09:25:05 / 10:00 Scheduler 窗口；双源真值门禁） |
| `auction_analysis_publications` | **正式可见性 pointer**；唯一键 `(trade_date, algorithm_version)`，`algorithm_version = "auction-v3.2"` |
| `auction_scan_runs` | scan run（final），`algorithm_version = "auction-v3.2"` |
| `auction_scope_results` | Scope 聚合承载表；`scope_type`(family) / `scope_id` / `scope_name` / `payload`(JSONB) / `reason_codes` |
| `auction_instrument_results` / `auction_event_trackings` | 个股事实与事件（legacy 字段保留，V3.2 canonical 不依赖） |
| `auction_anchor_*` / `auction_quote_capture_*` | V3.2 canonical 不依赖，仅作 raw fact 采集上游 |

V3.2 canonical payload 写入 `AuctionScopeResult.payload` JSONB（见 §5），`schema_version = "auction-scope-v3.2"`。

---

## 3. 计算层（backend/app/domain/auction/，纯计算、无 DB 依赖）

| 模块 | 职责（owner） |
|---|---|
| `member_fact.py` | `AuctionMemberFact` + `compute_gap_ratio`（canonical：`+2.3% → 0.023`；任一 None/非 finite 或 `previous_close <= 0` → None） |
| `member_fact_adapter.py` | `to_member_facts`：把 raw quote → `AuctionMemberFact` |
| `membership_pit.py` | `resolve_scope_members`：全量多 board（按 `MarketBoard.type` 分 industry/concept 两个平行 family），`Membership interval ∩ BoardDefinitionVersion interval` 逐日 PIT |
| `scope_fact.py` | `compute_auction_l1_scope_facts`：family-agnostic L1（PRICE / AMOUNT）；三 denominator 独立（price/amount/joint），Breadth/Dispersion/Price HHI/Amount HHI 与 share 单一 owner |
| `scope_dynamics.py` | `compute_dynamics`：EW Position/Velocity/Acceleration/Persistence；`compute_amount_participation`：Amount Position/Multiple/Abnormality |
| `member_history.py` | `compute_member_history_evidence` + `filter_strictly_pre_t`（baseline 严格 `< T`）；member-first amount history 跨 Scope 共享（每 instrument 只算一次） |
| `scope_history.py` | `build_scope_history_series`：bounded bulk 历史 loader（window 120 / min 60） |
| `amount_history.py` | scope 级 + member-first amount position/multiple/abnormal breadth（跨 Scope 共享） |
| `cross_sectional.py` | `compute_cross_sectional` / `axis_primary_positions`：同 family 一次批量 cross-section position（禁 family 混排、MIN_VALID_PEERS 守卫） |
| `contribution.py` | EW / AW / Amount 三 contribution owner + `reconcile`（Σ 机器对账） |
| `leadership.py` | direction / aligned / 50% 最小前缀 / retained / entrants / exits / Jaccard / migration，显式 empty 语义 |
| `analysis_preparation.py` | **V3.2 事实链唯一装配 owner**（pure）：production writer 与 T3 业务链测试都调用它，不得手搓 domain helper 组合；内部带 machine counters（如 member history 每 instrument 只算一次） |
| `scope_payload.py` | `build_scope_payload` / `parse_scope_payload` / `canonical_scope_key` / `canonical_scope_name`；`SCHEMA_VERSION = "auction-scope-v3.2"`；fail-closed 校验 schema_version + algorithm_version |
| `version.py` | `V32_ALGORITHM_VERSION = "auction-v3.2"`（唯一机器定义；writer / read-model / API / payload 都 import 此处） |
| `publication_read.py` | **读模型 owner**：`select_published_run` / `read_published_scope_results`（完整 family snapshot，无 Top-N）/ `to_scope_list_items` / `to_scope_detail` / `find_scope_result_by_key` / `published_dates`；只 READ，绝不重算 |

---

## 4. 服务层（V3.2 写入路径）

| 服务 | 职责 |
|---|---|
| `auction_v32_input_loader.py` | V3.2 输入加载（raw quote + membership + history bulk read） |
| `auction_v32_analysis_service.py` | V3.2 分析编排：调 `analysis_preparation` 装配每个 scope 的 canonical payload |
| `auction_scope_persistence_service.py` | 把 canonical payload 持久化进 `AuctionScopeResult.payload`（零 schema 变更） |
| `historical_auction_backfill_writer.py` | 历史行回补 writer（source 隔离落库；当前无消费方，运行时 bootstrap NOT_RUN） |
| `auction_scan_service.py` | scan run 生命周期（幂等 + lease fencing + 恢复）；membership 改全量多 board |
| `auction_scheduler_service.py` | `run_verified_auction_pipeline`：来源留证 → 真值验证 → 共识 capture → scan → aggregate → publish；09:25:05 / 10:00:00 触发窗口；与 after_close Worker 同进程 co-process，异常隔离 |
| `auction_publication_service.py` | `publish_auction_analysis`：校验 truth/namespace/capture/scan/coverage/aggregate，幂等写 `auction_analysis_publications` |

Legacy（非 V3.2 canonical 产品面，schema 仍保留）：`auction_anchor_service` / `auction_aggregation_service` / `auction_mode_service` / `auction_truth_service`（V3.2 复用其 9:25 真值门禁采集 raw quote）。

---

## 5. API（backend/app/api/auction.py）

### 5.1 V3.2 scope-first workspace 端点（本轮新加，PRD 目标合同）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/v1/auction/scopes` | `research_replay`（与 review 同机器值，无独立 auction capability） | **完整同 family snapshot（无 Top-N）**；`trade_date`（默认当日）、`family=industry\|concept`；返回 `AuctionScopeListOut`（所有 numeric 字段可空，技术 ID 不在此层） |
| GET | `/v1/auction/scopes/{scope_key}` | `research_replay` | 单个已发布 scope 的五 canonical 组 + diagnostics；`scope_key` 为 `MarketBoard.externalCode`（业务身份），UUID 仅留 diagnostics |
| GET | `/v1/auction/meta/dates` | `research_replay` | 拥有正式 V3.2 publication 的交易日（新→旧）；历史数据日期排除（PRD AU-04-6） |

读链：`(trade_date, algorithm_version="auction-v3.2") → AuctionAnalysisPublication（可见性边界）→ scan_run_id → AuctionScopeResult → payload（schema_version 校验）`。**禁止**取「最新 scan run」或「最新 succeeded run」或「最新 AuctionScopeResult」。

### 5.2 Legacy 端点（仍保留，产品面降级；非 V3.2 canonical）

`GET /v1/auction`（市场级 top N）、`/auction/board/{board_id}`、`/auction/stock/{symbol}`、`/auction/anchors/{trade_date}`、`/auction/backflow/{trade_date}`；`POST /v1/admin/auction/scan`、`/admin/auction/anchors`。这些对应旧三级页面与 Review 回流，V3.2 canonical 不依赖。

### 5.3 V3.2 DTO（backend/app/schemas/auction.py）

- `AuctionScopeListItemOut`：list row，只读 payload 取值。字段（全部 `float | None`，Missing≠Zero）：
  `scope_key`、`scope_name`、`equal_weight_gap`、`amount_weighted_gap`、`capital_tilt`、`positive_gap_breadth`、`negative_gap_breadth`、`unchanged_gap_breadth`、`gap_dispersion`、`price_normalized_hhi`、`ew_position`、`ew_velocity`、`ew_acceleration`、`amount_historical_position`、`amount_multiple`、`amount_abnormal_breadth`、`total_auction_amount`、`normalized_hhi`、`cross_sectional`（`dict[str, float|None]`）、`leadership_migration`（`float|None`）、`price_valid_count`（`int|None`）。**不含任何技术 UUID**。
- `AuctionScopeListOut`：`trade_date`、`family`、`algorithm_version`、`schema_version`、`total_scopes`、`scopes[]`。
- `AuctionScopeDetailOut`：`trade_date`、`family`、`scope_key`、`scope_name` + 五组 dict（`repricing` / `historical_dynamics` / `participation` / `cross_sectional` / `member_attribution`）+ `diagnostics`（含 `scope_id` / `scan_run_id` 等技术 ID）。
- `AuctionMetaDatesOut`：`trade_dates[]`、`latest`。

---

## 6. 前端

### 6.1 现状（2026-09-02，ACTUAL）

`frontend/src/features/auction/` 现状（2026-09-02）：**V3.2 List-first Workspace 已实现**（本轮），`/auction` 路由指向 `AuctionScopeWorkspace`；legacy 三级页面 `AuctionMarketPage.tsx` / `AuctionBoardPage.tsx` / `AuctionInstrumentPage.tsx` 仍保留，其中 `AuctionBoardPage` / `AuctionInstrumentPage` 继续服务 `/auction/board`、`/auction/stock` 降级路由；`api.ts` / `types.ts` 已增补 V3.2 端点与类型（legacy 端点/类型保留以兼容降级路由）。详见 §6.2 核验状态。

### 6.2 V3.2 目标（本轮已实现，2026-09-02）

**核验**：`npx tsc --noEmit` 干净；`npm run build`（tsc -b + vite build）成功；`src/features/auction/__tests__/auctionScopeViewModel.test.ts` 6 个契约测试 pass（null-last 双向、preset remap、分页、搜索、6 preset 字段合法性）。

- `/auction` 改为 **List-first Workspace**：工具条（日期 + 行业/概念 family 切换 + Search + 数据状态摘要）+ 左侧完整 family 列表 + 右侧 Selected Scope Detail。
- 实现文件：`AuctionScopeWorkspace.tsx` / `AuctionScopeTable.tsx` / `AuctionScopeDetail.tsx` / `auctionScopeViewModel.ts`（已对齐真实后端 DTO，非旧草案）/ `auctionUrlState.ts`（URL SSOT）/ `api.ts`（3 个 V3.2 hook）/ `types.ts`（V3.2 DTO 类型）。
- 6 个 transparent preset（chip 展示其背后 filter+sort 字段，非黑盒评分）。
- 全 numeric 列 ASC/DESC 且 **null 永远最后**；sticky 表头 + sticky Scope 首列。
- URL SSOT：`trade_date` / `family` / `scope` / `sort` / `direction` / `search` / `preset` / `page`。
- 技术 ID（publication / scan_run / source run / scope UUID）不出现在主界面，收进 Diagnostics 折叠区。
- 复用 `review.module.scss` 既有 token 与暗色 analytic 风格；图表沿用 `lightweight-charts`，共享 X 轴 + crosshair 同步与清理。
- 前端**只承载结构化展示，绝不重算**业务指标（canonical owner 在后端 payload）。

---

## 7. Active Domain Coverage Matrix（PRD V3.2 → 实现）

| PRD 范围 | 后端实现 | 前端 | 核验 |
|---|---|---|---|
| §0.0-B V3.2 Core Contract（范围/主轴/INV-01..06） | 已实现（domain + API） | 待建 | 代码 verified |
| Scope 与双主轴（industry/concept 平行） | 已实现（membership_pit） | 待建 | 代码 verified |
| Gap canonical（0.023）与三 denominator 独立 | 已实现（member_fact / scope_fact） | 待建 | 代码 verified |
| Historical EW/AW Dynamics（window 120 / min 60，baseline<T） | 已实现（scope_dynamics / member_history / scope_history） | 待建 | 代码 verified |
| Amount Historical Abnormality（member-first 跨 Scope 共享） | 已实现（amount_history） | 待建 | 代码 verified |
| Cross-sectional（同 family 批量，MIN_VALID_PEERS 守卫） | 已实现（cross_sectional） | 待建 | 代码 verified |
| Contribution（EW/AW/Amount 三 owner + Σ 对账） | 已实现（contribution） | 待建 | 代码 verified |
| Leadership（方向/对齐/最小前缀/Jaccard/migration） | 已实现（leadership） | 待建 | 代码 verified |
| Publication 读模型 + /v1/auction/scopes 完整 snapshot | 已实现（publication_read + API） | 待建 | 代码 verified |
| /scopes/{scope_key} 五组 + diagnostics | 已实现（API + DTO） | 待建 | 代码 verified |
| /meta/dates | 已实现 | 待建 | 代码 verified |
| List-first Workspace + 6 preset + URL SSOT + 隐藏技术 ID | 已实现（本轮） | 已实现 | tsc clean / build OK / 6 契约测试 pass |

---

## 8. 已知缺口与阻断（诚实清单）

| 项目 | 状态 | 事实 |
|---|---|---|
| 生产双独立真值源 | `NOT_RUN` | 仓库仅有通达信供应链（pytdx/tongdaxin） |
| PG Integration | `DEFERRED_FOR_EXPLORATION` | 不跑真实 PostgreSQL |
| 历史 120 日真实 bootstrap | `NOT_RUN` / `REQUIRES_AUTHORIZATION` | writer 已隔离落库，无消费方 |
| Migration apply / 部署 / 真实数据 E2E / 浏览器验收 | 未执行 | 本轮明确不部署生产、不写生产 |
| 前端 V3.2 Workspace | 已实现（本轮） | 见 §6.2，tsc clean / build OK / 6 契约测试 pass |

---

## 9. 更新触发条件

当以下任一发生时更新本 Map：
- V3.2 contract（payload schema / algorithm_version / API）变化；
- 新增/修改 domain/auction 模块或 V3.2 service；
- 前端 V3.2 Workspace 落地后回填 §6.2；
- 运行时验收（PG / 浏览器）执行后升级核验状态。
