# Map: Review Backend/API Contract Closure (PHASE C1 + C1 CONTINUATION + C1 FINAL)

> **本文件状态：ACTIVE CONTRACT MAP — PHASE C1 审计 + C1 CONTINUATION 修复 + C1 FINAL 统一正式读边界（2026-08-28）**
>
> 目标：回答"Review 产品从数据库到 API 是否已拥有稳定、显式、可供前端消费的合同"。
> 范围：仅 Review 域；不触及 Phase B snapshot migration / Core publication cleanup / same-day rerun / frontend redesign / 生产部署。
> 源：代码实测（app/models/market_review.py、app/services/review_*.py、app/api/review.py、app/api/admin_review.py、app/schemas/review.py、frontend/src/features/review/*）。

---

## 0. 结论速览（Done-Condition）

| 判定项 | 结果 |
|---|---|
| REVIEW_RUN_LIFECYCLE | CLOSED |
| REVIEW_READ_OWNER | EXPLICIT |
| REVIEW_API_OWNER | EXPLICIT |
| OVERVIEW_RUN_ISOLATION | PASS |
| REVIEW_LIVE_POINTER_FILTER | PASS |
| SUPERSEDED_POINTER_RESURRECTION | BLOCKED |
| FORMAL_PUBLICATION_READ_GUARD | PASS |
| PRODUCTION_PUBLISH_PATH_TEST | PASS |
| MULTI_RUN_POINTER_DETERMINISM | PASS |
| MULTI_RUN_API_DETERMINISM | PASS |
| SOURCE_CORE_LINEAGE | PASS |
| CORE_PUBLICATION_DEPENDENCY | 0 |
| LIVE_POINTER_OWNER | PASS |
| FORMAL_REVIEW_READ_OWNER | PASS（5/5 用户端点统一） |
| DATES_FORMAL_ONLY | PASS |
| LATEST_FORMAL_ONLY | PASS |
| SCOPES_FORMAL_ONLY | PASS |
| DETAIL_FORMAL_ONLY | PASS |
| BROKEN_POINTER_OVERVIEW | FAIL_CLOSED |
| BROKEN_POINTER_LATEST | FAIL_CLOSED |
| BROKEN_POINTER_DATES | EXCLUDED |
| C1_EXACT_PG | PASS |
| FULL_TARGETED_PG | NOT_RUN_BY_SCOPE（known pre-deploy debt = 5 legacy failures，未触及） |
| DISPLAY_STALE_POINTER_BLOCKS_REVIEW | NO |
| API_EMPTY_STATUS_CONTRACT | DEFINED |
| FRONTEND_DATA_CONTRACT | DEFINED |
| C1_CONTRACT_MAP | CURRENT |
| C1_TEST_FALSE_GREEN | CLOSED |
| PHASE_C1_REVIEW_API_CONTRACT_CLOSED | CANDIDATE（待 ChatGPT 按 exact SHA 审计） |

---

## 1. REVIEW_DOMAIN_OWNER_MAP

> 说明：Review 没有独立的 `ReviewPublication` 表。正式发布 owner 通过 **复用 `factor_publications` 表** 实现（`publication_kind=market_review`）。

| entity | 模型 / 表 | producer | source owner | read owner | API consumer | status |
|---|---|---|---|---|---|---|
| ReviewRun | `MarketReviewRun` / `market_review_runs` | review_orchestrator_service.create_run/compute_run/resume_run | 显式 `StockFeatureSnapshotRun`（CoreRun）经 `source_core_run_id`（缺失即 fail-closed，**不**回退 stock_core pointer） | `FactorPublication(kind=market_review, superseded_by IS NULL)` 指针（每 trade_date 唯一），由 `get_published_review_run_id` 解析 | api/review.py（用户）+ api/admin_review.py（admin） | EXPLICIT |
| ReviewRunItem | `MarketReviewRunItem` / `market_review_run_items` | review_orchestrator_service | 所属 ReviewRun | 随 run 读取 | 内部 | DONE |
| ScopeSnapshot | `MarketReviewScopeSnapshot` / `market_review_scope_snapshots` | review_orchestrator_service._persist_canonical_scope_observation | 所属 ReviewRun | scope 端点 | scopes | DONE |
| ObservationFact (L1) | `ReviewScopeObservationFact` / `review_scope_observation_facts` | scope_observation.compute_scope_observation | 所属 ReviewRun（grain: review_run_id+trade_date+scope_type+scope_key） | `get_scope_observation_fact_by_run`（按 run）+ overview 双 lineage 过滤 | scopes/{type}/{key} | DONE |
| Composition | `ReviewScopeCompositionSnapshot` / `review_scope_composition_snapshots` | canonical_composition.compose_canonical_review_scope | 所属 ReviewRun | scope 端点 | scopes/{type}/{key} | DONE |
| MetricObservation | `MarketReviewMetricObservation` / `market_review_metric_observations` | review_metric_observation_service | 所属 ReviewRun | scope 端点 | scopes | DONE |
| Signal | `MarketReviewSignal` / `market_review_signals` | **RETIRED** — `review_signal_service` 已物理删除；无 active producer；历史 ORM 保留不 DROP；用户 API 路径返回 404 | — | — | — | RETIRED ORM |
| SignalAttribution | `MarketReviewSignalAttribution` / `market_review_signal_attributions` | writer 存在（`review_attribution_service` 写入），但 legacy per-scope Signal/Attribution 用户 API 已删除 → 当前无用户消费端 | 所属 ReviewRun（grain: review_run_id） | 无用户端点（RETIRED surface） | — | WRITER_ONLY |
| SignalInstrument | `MarketReviewSignalInstrument` / `market_review_signal_instruments` | 无 active producer（`review_signal_service` 已删除） | — | — | — | RETIRED ORM |
| Tracking | `MarketReviewTracking` / `market_review_trackings` + `MarketReviewTrackingEvaluation` | 无 active producer（`review_tracking_service` 已删除）；历史 ORM 保留 | — | 未挂用户 API/前端（任务§9 允许不全暴露，非阻断） | — | RETIRED ORM |
| Discovery | （无独立表；`review_discovery_service` 已物理删除） | 无 producer / 无 API | — | — | — | RETIRED SERVICE |

**关键事实**：正式用户读路径只返回 **FactorPublication(kind=market_review, superseded_by IS NULL) 指针指向的 run**（live universe 唯一，partial unique index 保证）。overview 的 coverage/observation 读取以 `(review_run_id, trade_date)` 双 lineage SQL 过滤（C1 CONTINUATION BLOCKER A fix），杜绝同日多 run 事实聚合污染。跨 run 分析类 reader（cross-sectional / history / evidence）按设计使用 trade_date 区间扫描，不声称单 run ownership。

---

## 2. REVIEW_RUN_LIFECYCLE（已闭合）

真实状态机（来自 `MarketReviewRun` 模型 docstring + schema）：

```
created → computing → partial → signals_ready → published
                                          ↘ (失败) → failed / completed_with_errors / cancelled
```

- **Review compute complete** = 所有 scope 计算完成 → `status = signals_ready`（DB-enum 兼容 token；发布门禁仍识别）。
- **Review 可读** = run 行存在且非 `created/computing` 中间态（用户路径下仅当已正式发布才可读，见 §4.3）。
- **Review formally published** = `FactorPublication(kind=market_review, trade_date=T)` 指针存在 **且** `run.status = published`（二者由 `publish_review` 原子写入）。
- **Core publication 不混入** Review lifecycle：`source_core_run_id` 仅作为显式 lineage 字段，不参与 Review 状态机；Review 状态机不引用任何 stock_core 发布状态。

---

## 3. REVIEW_READ_OWNER（EXPLICIT — 核心）

审计对象：`review_publication_service` + 所有 `get_published_review_run_id` / `is_formally_published_review_run` / `publish_run`。

判定（trade_date T → ReviewRun Y）：

- **存在正式 durable owner**：`factor_publications` 行，`publication_kind = market_review`、`scope_type = market`、`scope_key = market`、`trade_date = T`、`data_run_id = Y.id`、`superseded_by IS NULL`。
- 唯一性：`UNIQUE(scope_type, scope_key, trade_date, publication_kind) WHERE superseded_by IS NULL` —— 每 trade_date 至多一条 market_review 指针。
- **Live universe 过滤（C1 CONTINUATION §3）**：`_get_publication` 与 `list_published_review_dates` 均显式 `WHERE superseded_by IS NULL`，**不**靠 `ORDER BY published_at DESC` 猜 owner。partial unique index 已保证 live universe 唯一，current pointer 查询不依赖 latest timestamp。
- 解析函数：`get_published_review_run_id(session, T)` → `_get_publication(...).data_run_id`。
- 这是 **Review 产品自己的正式 publication owner**，与已退役的 `stock_core` 同步 gate 完全分离（见 §6）。

### 3.1 LIVE POINTER OWNER ≠ FORMAL REVIEW READ OWNER（PHASE C1 FINAL §5）

两者**不是同一个概念**，误用会产生"pointer 存在 ⇒ 已正式发布"的假绿：

| | LIVE POINTER OWNER | FORMAL REVIEW READ OWNER |
|---|---|---|
| 回答的问题 | T 的 live pointer 指向哪个 `run_id`？ | T 是否可以作为**正式用户 Review** 返回？ |
| 判定条件 | `(kind=market_review, trade_date=T, superseded_by IS NULL)` | live pointer 存在 **AND** `MarketReviewRun` 存在 **AND** `status == published` **AND** `published_at IS NOT NULL` |
| 实现 | `get_published_review_run_id`、`list_published_review_dates` | `app.api.review._get_published_run`（统一 guard，内含 `is_formally_published_review_run`）、`list_formally_published_review_dates`（DB 层 JOIN） |
| 允许的用户用途 | 仅用于**定位**候选 run / 候选交易日，其后**必须**再走 formal guard | 用户正式 endpoint 的唯一判定依据 |
| 禁止 | **不得**把非 None 返回值单独当作 `FORMALLY_PUBLISHED = TRUE` | — |

**关键**：pointer 是发布动作写入的，但 run 行本身可以被后台流程/异常状态改写。因此 pointer 存在只是"曾经发布过"，run formal state 才是"当前仍是正式发布"。`/v1/review/dates` 与 `/v1/review/latest` 在 C1 FINAL 之前只用了 LIVE POINTER OWNER，属于同一类假绿，本轮统一收口。

---

## 4. REVIEW_API_OWNER（EXPLICIT）+ MULTI_RUN_DETERMINISM（PASS）

### 4.1 端点清单

**用户 API**（`app/api/review.py`，均需 `REVIEW_CAPABILITY`）：

| METHOD | PATH | service | 响应 schema | run owner 解析 | trade_date 处理 | 空行为 |
|---|---|---|---|---|---|---|
| GET | `/api/v1/review/dates` | **list_formally_published_review_dates**（C1 FINAL：DB 层 pointer JOIN run） | ReviewDatesResponse | — | — | 空列表 200 |
| GET | `/api/v1/review/latest` | list_published_review_dates 定位候选日 **+ `_get_published_run` formal guard**（C1 FINAL §3） | ReviewLatestResponse | 指针解析 + run formal state | — | 无 live pointer→404；broken pointer→500 |
| GET | `/api/v1/review/{trade_date}/overview` | get_published_review_run_id | ReviewOverviewResponse | 指针解析 | 路径参数(422 若格式错) | 无发布→404 |
| GET | `/api/v1/review/{trade_date}/scopes` | get_published_review_run_id | ReviewScopeListResponse | 指针解析 | 路径参数 | 无发布→404 |
| GET | `/api/v1/review/{trade_date}/scopes/{scope_type}/{scope_key}` | get_published_review_run_id | ReviewScopeCompositionDetailResponse | 指针解析 | 路径参数 | 无发布→404 |

> 注：scope detail 端点真实路径为 `/scopes/{scope_type}/{scope_key}`，处理函数 `get_review_scope_composition`；**不**带 `/composition` 后缀。

**Admin/Debug API**（`app/api/admin_review.py`）：

| METHOD | PATH | 说明 |
|---|---|---|
| POST | `/api/v1/admin/review/runs` | 创建 run（需显式 source_core_run_id，否则 400 fail-closed） |
| POST | `/api/v1/admin/review/runs/{id}/resume` | 续算 |
| POST | `/api/v1/admin/review/runs/{id}/publish` | 正式发布（写 FactorPublication 指针） |
| GET | `/api/v1/admin/review/runs/{id}/status` | 状态（`include_partial=True`：可见非发布 run，调试用） |
| GET | `/api/v1/admin/review/runs/{id}/timeline` | 时间线 |

### 4.2 同日多 run 假绿防护（MULTI_RUN_DETERMINISM = PASS）

场景：同日 T，Core A / Core B；Review Y(source=A) / Review Z(source=B)。

- 用户端点 **不返回 latest ReviewRun**，只返回 `FactorPublication(kind=market_review, T, superseded_by IS NULL).data_run_id`。
- `publish_review` 用 `pg_insert(...).on_conflict_do_update(index_elements=[scope_type, scope_key, trade_date, publication_kind])` —— 同一 T 重新发布会 **覆盖** `data_run_id`，不会新增行。
- 因此 API 返回哪个 run 由 **"发布动作"** 决定，而非 Z 创建更晚。即使 Z 后建，只要指针指向 Y，API 返回 Y。
- 不发明新 pointer；复用既有 `factor_publications` 唯一约束。

### 4.3 用户正式读路径 publication guard（§4 / C1 CONTINUATION）

`_get_published_run(run, include_partial=False)` 经 `is_formally_published_review_run` 强制正式发布判定，区分三种情形（复用单一 guard，不创造第二套 publication 语义）：

- **A. 无 live pointer** → `404 no published review`。
- **B. live pointer 指向不存在的 run** → `500 data-integrity`，**绝不**回退 latest run。
- **C. live pointer 指向 run，但 `status != published` 或 `published_at IS NULL`** → `500 data-integrity`，不得作为正式 Review 返回。
- `include_partial=True`（admin/调试）跳过 guard，回退到任意 run。

### 4.4 用户 endpoint FORMAL OWNER GUARD 审计（PHASE C1 FINAL §6）

审计范围：**仅 5 个用户正式 endpoint**。admin/debug endpoint（`admin_review.py` 的 `include_partial` 可见语义）按 §4 明确不在本审计内，其 partial 可见语义保持不变。

| 用户 endpoint | formal owner 实现 | FORMAL OWNER GUARD |
|---|---|---|
| `/v1/review/dates` | `list_formally_published_review_dates`（DB 层 `factor_publications JOIN market_review_runs`，一次性过滤 `status=published AND published_at IS NOT NULL`） | **YES** |
| `/v1/review/latest` | `list_published_review_dates`（定位候选最新交易日）→ `_get_published_run`（统一 formal guard） | **YES** |
| `/v1/review/{T}/overview` | `_get_published_run` | **YES** |
| `/v1/review/{T}/scopes` | `_get_published_run` | **YES** |
| `/v1/review/{T}/scopes/{type}/{key}` | `_get_published_run` | **YES** |

**审计结论：5/5 全部 YES**（C1 FINAL 之前为 3/5，`/dates` 与 `/latest` 只验证 pointer，不符合 FORMAL_REVIEW_READ_OWNER）。

`/dates` 合同（§4）：名义语义 = "已正式发布 Review 的交易日"，因此 live pointer 指向 invalid / missing / non-published `MarketReviewRun` 的 T **不得**列入。全部条件在 DB query 层完成：

```
publication_kind = market_review
AND scope_type = market AND scope_key = market
AND superseded_by IS NULL
AND JOIN market_review_runs run ON run.id = data_run_id
AND run.status = published
AND run.published_at IS NOT NULL
ORDER BY trade_date DESC
```

→ 单次 JOIN 查询，**无 N+1**（不为每个 T 单独 `db.get` run）。

`/latest` 合同（§3）：**禁止**自行 `pointer → db.get(MarketReviewRun) → return`，必须与 overview/scopes/detail 复用同一 formal guard：

- `list_published_review_dates(limit=1)` 只用于取**候选**最新交易日（LIVE POINTER OWNER）；
- 随后 `_get_published_run(T)` 执行 formal guard：
  - broken pointer（pointer → 缺失 run）→ **500 fail-closed**
  - `status != published` → **500 fail-closed**
  - `published_at IS NULL` → **500 fail-closed**
  - 无 live pointer → 404
- **绝不**回退到 latest `MarketReviewRun`，**也绝不**跳过到更早交易日。

---

## 5. SOURCE_CORE_LINEAGE（PASS）

- `MarketReviewRun.source_core_run_id`、`source_board_run_id`（nullable legacy）、`source_chip_run_id`（nullable legacy）均落库。
- 用户 `overview` 响应显式携带 `sourceCoreRunId` / `sourceBoardRunId` / `sourceChipRunId`（见 `ReviewOverviewResponse`）。
- `ReviewLatestResponse` 不携带 sourceCoreRunId（仅指针信息；`/latest` 重定向到 `/overview` 取全量 lineage，可接受）。
- 创建路径：`_resolve_source_core_run_id` 在 `source_core_run_id is None` 时 **直接 fail-closed**（明确信息："未显式提供 source_core_run_id，Review 必须显式绑定本次 AfterCloseRun 产生的 CoreRun，不得经 stock_core publication pointer 解析"），**绝不**从 `FactorPublication(kind=stock_core)` 回退。
- 内部 DTO 保留 lineage（即使 UI 不全展示）。
- `MarketReviewRun.source_core_run_id` 注释（C1 CONTINUATION 修正）现为"显式 CoreRun lineage：`StockFeatureSnapshotRun.id`"，不再误指 `factor_publications(stock_core).data_run_id`。

---

## 6. REVIEW_LEGACY_DEPENDENCY_MATRIX（CORE_PUBLICATION_DEPENDENCY = 0）

检索范围：`app/**/review*.py` + `app/api/*review*.py`。
检索模式：`stock_core_publication_service` / `resolve_stock_core_published` / `get_published_full_run` / `has_succeeded_snapshot_run` / `market_stocks_service` / `watchlist` / `PUBLICATION_KIND_STOCK_CORE` / `FactorPublication(kind=stock_core)` / `get_published_stock_core`。

| 依赖项 | Review 用户/读取路径 | 结论 |
|---|---|---|
| stock_core_publication_service | 0 命中 | 无依赖 |
| resolve_stock_core_published / get_published_full_run | 0 命中 | 无依赖 |
| has_succeeded_snapshot_run | 0 命中 | 无依赖 |
| market_stocks_service | 0 命中 | 无依赖 |
| watchlist | 0 命中 | 无依赖 |
| PUBLICATION_KIND_STOCK_CORE | 仅 `review_orchestrator_service.py` 顶部 import + 文档字符串；`PUBLICATION_KIND_STOCK_CORE_REF` 定义后从未使用 | 不引入运行时依赖 |

**发布门禁实际代码**（`evaluate_publish_gate`）仅校验：
1. `StockFeatureSnapshotRun`（CoreRun）存在且 `trade_date` 匹配；
2. CoreRun `status == succeeded`；
3. scope readiness（market mandatory，其余 progressive optional）；
4. 无 run item 处于 failed/pending/running。

**不**查 stock_core pointer。

---

## 7. P1 DISPLAY_STOCK_CORE_POINTER_STALE（NOT BLOCKING REVIEW）

- Review 产品路径完全不依赖 `FactorPublication(kind=stock_core)` / `market_stocks` / `watchlist` display owner（见 §6：0 命中）。
- 该 stale pointer 仅影响 legacy/display reader（P1 debt，由 Phase B 决策冻结，本轮不修）。
- **结论**：`DISPLAY_STOCK_CORE_POINTER_BLOCKS_REVIEW = NO`。

---

## 8. REVIEW_FRONTEND_DATA_CONTRACT（DEFINED）

源：`frontend/src/features/review/types.ts` + `api.ts`（与后端 schema 高度对齐）。

| 产品需求 | 前端字段 | 后端来源 |
|---|---|---|
| 市场状态 | coverage / eligibleCount / providedCount / coverageRatio | ReviewOverviewCoverageDTO.industryL1 等 float 覆盖率 |
| 板块/主题 | industry_l1/l2/l3/concept scope 列表 | scopes 端点 |
| signals | ~~signalCount + 各 scope signal~~（legacy Signal pipeline 已退休，用户 API 不再产生 signal，RETIRED） | — |
| discoveries | observationGroups / dynamics（**canonical Scope Observation**，非 legacy Discovery service） | scopes/{type}/{key} |
| observations/facts | ReviewScopeCompositionDetailResponse.observation + observationSummary | scopes 端点 |
| attribution | member_attribution 各 layer（writer 存在，无用户 API） | scopes/{type}/{key} |
| readiness/status | status（created/computing/signals_ready/published/failed…） | 各端点 status 字段 |
| run / trade date | reviewRunId / tradeDate | 各端点 |
| degraded reasons | degradedReasons[] | overview |
| source core lineage | sourceCoreRunId（overview） | MarketReviewRun.source_core_run_id |

- Tracking 尚未进入前端（任务§9 允许不全暴露，非阻断）。
- 前端 contract 与后端 schema 一致，无需新增字段暴露。

---

## 9. API_EMPTY_STATUS_CONTRACT（DEFINED）

| 情形 | HTTP | business status | payload |
|---|---|---|---|
| 无 ReviewRun | 404（/overview,/scopes,/latest）/ 200 空列表（/dates） | `no_published_review` | error detail 或空 |
| Review running（created/computing/signals_ready） | 404（用户端点，因未发布） | 非用户可见 | —（admin `/status include_partial` 可见） |
| Review failed / completed_with_errors / cancelled | 404（用户端点） | 非用户可见 | —（admin 可见） |
| live pointer 指向未正式发布 run（status!=published / published_at IS NULL） | 500 data-integrity（§4.3 case C） | 非用户可见 | error detail |
| Review partial/degraded | 发布后 200 | `published` + `degradedReasons[]` + 各 scope readiness=not_ready | 完整 payload |
| Review published | 200 | `published` | 完整 payload |
| 交易日无数据 | 404（无发布指针） | `no_published_review` | error detail |

设计意图：**用户仅可见已正式发布的 Review**；running/failed/未正式发布为 admin-only 或 fail-closed（§4.3），避免前端自行猜测状态。此为非歧义合同（非"猜测"），故判 DEFINED。

---

## 10. 缺口分类（GAP）

| 项 | 状态 | 说明 |
|---|---|---|
| API_EMPTY_STATUS_CONTRACT | DEFINED | running/failed/未正式发布 用户不可见是设计意图，非歧义 |
| ReviewLatestResponse 缺 sourceCoreRunId | 小 gap | `/latest` 重定向到 `/overview` 取全量 lineage，可接受 |
| Tracking 未进前端 | AVAILABLE_BY_DESIGN | 任务§9 允许不全暴露 |
| stale docstring（stock_core pointer） | **C1 已修** | review_publication_service.py / review_orchestrator_service.py 注释与错误信息已对齐真实 fail-closed 行为 |
| overview 同日多 run 事实聚合污染 | **C1 CONTINUATION 已修** | `get_review_overview` 现以 `(review_run_id, trade_date)` 双 lineage SQL 过滤 |
| live pointer 误用 latest timestamp | **C1 CONTINUATION 已修** | `_get_publication` / `list_published_review_dates` 显式 `superseded_by IS NULL` |
| 非发布 run 可被当作正式 Review 返回 | **C1 CONTINUATION 已修** | `_get_published_run` 增加 `is_formally_published_review_run` guard（case A/B/C） |
| source_core_run_id 注释误指 stock_core | **C1 CONTINUATION 已修** | 改为显式 `StockFeatureSnapshotRun.id` lineage |
| `/v1/review/dates` 只验证 pointer、不验证 run formal state | **C1 FINAL 已修（§4）** | 改用 `list_formally_published_review_dates`（DB 层 pointer JOIN run，单次查询无 N+1） |
| `/v1/review/latest` 自行 `pointer → db.get → return`，未走 formal guard | **C1 FINAL 已修（§3）** | 改为 `list_published_review_dates` 定位候选日 + `_get_published_run` formal guard；broken pointer fail-closed |
| `_get_published_run_or_404` 的 `except HTTPException: return None` 会吞掉 500 data-integrity | **P2 DEFERRED（§7，本轮不修）** | 当前**无任何 production caller**（legacy Discovery 服务与用户端点已随 REVIEW-BACKEND-FINAL-CLOSURE Phase 5 退休；全仓检索仅剩定义处）。为不为清洁代码扩大范围，本轮只记录不删除；**复用前必须**改为只把真正的 404 no publication 转 None，500 必须继续上抛 |

无重大 product schema / 跨域 owner 缺口 → 无需 checkpoint+STOP。

---

## 11. C1 + C1 CONTINUATION + C1 FINAL 代码变更

C1（注释一致性，锁定 `CORE_PUBLICATION_DEPENDENCY=0`）：

- `review_publication_service.py`：注释 —— "source_core_run_id 必须匹配 stock_core pointer" → "必须引用已存在且 succeeded 的 CoreRun"。
- `review_orchestrator_service.py`：注释与错误信息 —— `source_core_run_id` 缺失即 fail-closed，绝不回退 stock_core pointer。

C1 CONTINUATION（运行时行为修复，均为 Review 域内最小改动）：

- `review_observation_persistence_service.py`：`list_scope_observation_facts` 新增 `review_run_id` 参数，SQL 层 `WHERE review_run_id = :run_id` —— **BLOCKER A fix**，杜绝 overview 同日多 run 事实聚合污染（原调用仅按 trade_date 区间扫描）。
- `app/api/review.py`：`get_review_overview` 调用改为 `list_scope_observation_facts(db, review_run_id=run.id, from_date=run.trade_date, to_date=run.trade_date)`；`_get_published_run` 增加 `is_formally_published_review_run` guard，区分 case A(404)/B(500)/C(500) fail-closed，绝不回退 latest run。
- `review_publication_service.py`：`_get_publication` 与 `list_published_review_dates` 显式 `WHERE superseded_by IS NULL`，移除 `ORDER BY published_at DESC LIMIT 1` —— live universe 唯一性由 partial unique index 保证，不靠 latest timestamp 猜 owner。
- `app/models/market_review.py`：`MarketReviewRun.source_core_run_id` 注释由 "输入 stock_core snapshot_run_id（factor_publications.data_run_id）" 改为显式 `StockFeatureSnapshotRun.id` lineage。

C1 FINAL（统一 FORMAL_REVIEW_READ_OWNER，运行时行为修复，Review 域内最小改动）：

- `app/services/review_publication_service.py`：新增 `list_formally_published_review_dates` —— DB 层 `factor_publications INNER JOIN market_review_runs ON run.id = data_run_id`，一次性过滤 `publication_kind=market_review` + `scope=market/market` + `superseded_by IS NULL` + `run.status = published` + `run.published_at IS NOT NULL`，`ORDER BY trade_date DESC`（**单次查询，无 N+1**）。
- `app/services/review_publication_service.py`：`get_published_review_run_id` / `list_published_review_dates` docstring 显式标注为 **LIVE POINTER OWNER**（只回答 pointer identity，不单独证明 run formal state；用户 endpoint 不得据此判定 `FORMALLY_PUBLISHED`）。
- `app/api/review.py` `/dates`：改用 `list_formally_published_review_dates`，不再只验证 pointer。
- `app/api/review.py` `/latest`：删除自行 `pointer → db.get → return` 的实现，改为 `list_published_review_dates`（定位候选最新交易日）+ `_get_published_run`（复用与 overview/scopes/detail 完全相同的 formal guard）。broken pointer / 未正式发布 run → 500 fail-closed，绝不回退 latest ReviewRun、绝不跳过到更早交易日。
- `app/api/review.py` `_get_published_run_or_404`：仅更新 docstring 记录 §7 的 P2 deferred 风险（**不改行为、不删除**：当前无 production caller）。

---

## 12. 测试要求（T1 / T2 / 多 run 假绿 / superseded / broken / C1 FINAL §8 CASE A/B/C）

测试文件：`backend/tests/test_pg_review_read_owner_c1.py`（self-contained synthetic，验证库 `bz_stock_verify_<SHA>`，不读不写生产 `bz_stock`）。经 `verify_exec.py` / targeted-pg 验证数据库正式通道运行。

- **T1（PG suite，非 pure unit）**：`_resolve_source_core_run_id` 在 `None` 时 fail-closed。
- **T2（production publish + schema）**：`publish_review` 生产发布 → `/overview` 响应暴露 `sourceCoreRunId` 且等于显式绑定的 CoreRun；`coverage.industryL1 == 0.8`。
- **T5/T7（生产发布路径多 run 假绿）**：同日 T，Core A/B + Review Y(A)/Z(B)；Y: eligible=100/provided=80，Z: 100/20；`publish_review(Y)` → overview=Y(0.8)，`publish_review(Z)` → overview=Z(0.2)；owner 由 publication action 决定，非 created_at/latest。直接断言 `list_scope_observation_facts(review_run_id=...)` 的 SQL 级隔离。
- **§7（superseded pointer 假绿）**：同 T 制造 historical H(superseded) + live L；H.published_at 更晚，但 `get_published_review_run_id` 返回 L；仅 superseded history、无 live pointer → T 不视为当前 owner。
- **§8（broken pointer fail-closed）**：live pointer → run `status=signals_ready` / `published_at=NULL` → 用户正式 read path `500` fail-closed，不得返回 200 正式 Review。

C1 FINAL 新增（§8 CASE A/B/C，统一正式读边界）：

- **CASE A（broken live pointer = 唯一 live pointer）**：`/overview` → 500 fail-closed；`/latest` → 500 fail-closed（不回退同日其它 run、不跳过到更早交易日）；`/dates` 不把 T 列为正式已发布日期。同时断言 `T ∈ list_published_review_dates`（live pointer 仍存在）且 `T ∉ list_formally_published_review_dates` —— **直接证明排除来自 run formal state，而非 pointer 缺失**。
- **CASE B（valid published run，生产 `publish_review` 路径）**：`/dates` 包含 T 且 `latest_trade_date == T`；`/latest` 返回该正式 run（`status == "published"`）。
- **CASE C（T 仅剩 superseded historical pointer）**：historical run 自身是正式发布态（`status=published` + `published_at` 非空），被排除的唯一原因是其 pointer 已被 supersede；`/dates` 不包含 T、包含次新正式发布日 `T_PREV`；`/latest` 返回 `T_PREV` 的 run，**不 resurrect** historical run。

`/latest` 语义 = live pointer 中最大 `trade_date`。CASE A/B/C 使用 sentinel 日期 `2099-12-31` / `2099-12-30`（远大于本文件其它用例的 `2026-08-xx`），确保"该 T 必为 `/latest` 命中目标"确定成立，**不依赖 pytest 执行顺序或其它测试文件残留**。

**禁止**自制 `_publish_pointer()` 复制生产 SQL；正常 owner case 全部调用生产 `publish_review(...)` 构造最小合法 publishable ReviewRun（source CoreRun exists + succeeded + trade_date 匹配 + algorithm_version 正式 + status=signals_ready + expected_scope_count>0 + coverage≥gate + canonical_composition_readiness 非空 ready + 非 canary/非 provisional/无 run item）。仅在**人工制造 corrupt / superseded 异常 DB 状态**时允许 fixture 直接插表（`superseded_by` 列无 FK 约束）—— 因为测试目标本身就是异常状态，不存在正常发布路径可以产生它。

---

## 13. 核验来源

- 后端：`app/models/market_review.py`、`app/services/review_publication_service.py`、`app/services/review_orchestrator_service.py`、`app/services/review_observation_persistence_service.py`、`app/api/review.py`、`app/api/admin_review.py`、`app/schemas/review.py`
- 前端：`frontend/src/features/review/types.ts`、`frontend/src/features/review/api.ts`
- 依赖扫描：regex 检索 `review*.py` + `app/api/*review*.py`
- 测试：`backend/tests/test_pg_review_read_owner_c1.py`（targeted-PG，验证库 `bz_stock_verify_<SHA>`，gate cleanup 丢弃，不写生产 `bz_stock`）
- 本 map 完整核验：2026-08-28（PHASE C1 + C1 CONTINUATION + C1 FINAL）
