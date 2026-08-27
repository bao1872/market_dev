# Map: Review Backend/API Contract Closure (PHASE C1)

> **本文件状态：ACTIVE CONTRACT MAP — PHASE C1 审计产出（2026-08-27）**
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
| MULTI_RUN_DETERMINISM | PASS |
| SOURCE_CORE_LINEAGE | PASS |
| CORE_PUBLICATION_DEPENDENCY | 0 |
| DISPLAY_STALE_POINTER_BLOCKS_REVIEW | NO |
| API_EMPTY_STATUS_CONTRACT | DEFINED |
| FRONTEND_DATA_CONTRACT | DEFINED |
| PHASE_C1_REVIEW_API_CONTRACT_CLOSED | CANDIDATE（待 ChatGPT 按 exact SHA 审计；见 §12） |

---

## 1. REVIEW_DOMAIN_OWNER_MAP

> 说明：Review 没有独立的 `ReviewPublication` 表。正式发布 owner 通过 **复用 `factor_publications` 表** 实现（`publication_kind=market_review`）。

| entity | 模型 / 表 | producer | source owner | read owner | API consumer | status |
|---|---|---|---|---|---|---|
| ReviewRun | `MarketReviewRun` / `market_review_runs` | review_orchestrator_service.create_run/compute_run/resume_run | 显式 `StockFeatureSnapshotRun`（CoreRun）经 `source_core_run_id`（缺失即 fail-closed，**不**回退 stock_core pointer） | `FactorPublication(kind=market_review)` 指针（每 trade_date 唯一），由 `get_published_review_run_id` 解析 | api/review.py（用户）+ api/admin_review.py（admin） | DONE |
| ReviewRunItem | `MarketReviewRunItem` / `market_review_run_items` | review_orchestrator_service | 所属 ReviewRun | 随 run 读取 | 内部 | DONE |
| ScopeSnapshot | `MarketReviewScopeSnapshot` / `market_review_scope_snapshots` | review_orchestrator_service._persist_canonical_scope_observation | 所属 ReviewRun | scope 端点 | scopes | DONE |
| ObservationFact (L1) | `ReviewScopeObservationFact` / `review_scope_observation_facts` | scope_observation.compute_scope_observation | 所属 ReviewRun（grain: review_run_id+trade_date+scope_type+scope_key） | `get_scope_observation_fact_by_run` | scopes/{type}/{key} | DONE |
| Composition | `ReviewScopeCompositionSnapshot` / `review_scope_composition_snapshots` | canonical_composition.compose_canonical_review_scope | 所属 ReviewRun | scope 端点 | scopes/{type}/{key} | DONE |
| MetricObservation | `MarketReviewMetricObservation` / `market_review_metric_observations` | review_metric_observation_service | 所属 ReviewRun | scope 端点 | scopes | DONE |
| Signal | `MarketReviewSignal` / `market_review_signals` | signal 服务 | 所属 ReviewRun | overview（signalCount）+ scopes | overview | DONE |
| SignalAttribution | `MarketReviewSignalAttribution` / `market_review_signal_attributions` | attribution 服务 | 所属 ReviewRun | scope 端点 | scopes | DONE |
| SignalInstrument | `MarketReviewSignalInstrument` / `market_review_signal_instruments` | signal 服务 | 所属 ReviewRun | 内部 | — | DONE |
| Tracking | `MarketReviewTracking` / `market_review_trackings` + `MarketReviewTrackingEvaluation` | review tracking 服务 | 所属 ReviewRun | **未挂用户 API / 前端**（按任务§9"不全部暴露"，非阻断） | — | AVAILABLE_BY_DESIGN |

**关键事实**：所有 Review 数据 reader 均以 `review_run_id` 为 grain 键（非 trade_date 直接猜 Core）；正式用户读路径只返回 **FactorPublication(kind=market_review) 指针指向的 run**。

---

## 2. REVIEW_RUN_LIFECYCLE（已闭合）

真实状态机（来自 `MarketReviewRun` 模型 docstring + schema）：

```
created → computing → partial → signals_ready → published
                                          ↘ (失败) → failed / completed_with_errors / cancelled
```

- **Review compute complete** = 所有 scope 计算完成 → `status = signals_ready`（DB-enum 兼容 token；发布门禁仍识别）。
- **Review 可读** = run 行存在且非 `created/computing` 中间态（用户路径下仅当已正式发布才可读，见 §4）。
- **Review formally published** = `FactorPublication(kind=market_review, trade_date=T)` 指针存在 **且** `run.status = published`（二者由 `publish_review` 原子写入）。
- **Core publication 不混入** Review lifecycle：`source_core_run_id` 仅作为显式 lineage 字段，不参与 Review 状态机；Review 状态机不引用任何 stock_core 发布状态。

---

## 3. REVIEW_READ_OWNER（EXPLICIT — 核心）

审计对象：`review_publication_service` + 所有 `get_published_review_run_id` / `is_formally_published_review_run` / `publish_run`。

判定（trade_date T → ReviewRun Y）：

- **存在正式 durable owner**：`factor_publications` 行，`publication_kind = market_review`、`scope_type = market`、`scope_key = market`、`trade_date = T`、`data_run_id = Y.id`。
- 唯一性：`UNIQUE(scope_type, scope_key, trade_date, publication_kind) WHERE superseded_by IS NULL` —— 每 trade_date 至多一条 market_review 指针。
- 解析函数：`get_published_review_run_id(session, T)` → `_get_publication(...).data_run_id`。
- 这是 **Review 产品自己的正式 publication owner**，与已退役的 `stock_core` 同步 gate 完全分离（见 §5/§6）。

---

## 4. REVIEW_API_OWNER（EXPLICIT）+ MULTI_RUN_DETERMINISM（PASS）

### 4.1 端点清单

**用户 API**（`app/api/review.py`，均需 `REVIEW_CAPABILITY`）：

| METHOD | PATH | service | 响应 schema | run owner 解析 | trade_date 处理 | 空行为 |
|---|---|---|---|---|---|---|
| GET | `/api/v1/review/dates` | list_published_review_dates | ReviewDatesResponse | — | — | 空列表 200 |
| GET | `/api/v1/review/latest` | get_published_review_run_id | ReviewLatestResponse | 指针解析 | — | 无发布→404 |
| GET | `/api/v1/review/{trade_date}/overview` | get_published_review_run_id | ReviewOverviewResponse | 指针解析 | 路径参数(422 若格式错) | 无发布→404 |
| GET | `/api/v1/review/{trade_date}/scopes` | get_published_review_run_id | ReviewScopeListResponse | 指针解析 | 路径参数 | 无发布→404 |
| GET | `/api/v1/review/{trade_date}/scopes/{scope_type}/{scope_key}/composition` | get_published_review_run_id | ReviewScopeCompositionDetailResponse | 指针解析 | 路径参数 | 无发布→404 |

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

- 用户端点 **不返回 latest ReviewRun**，只返回 `FactorPublication(kind=market_review, T).data_run_id`。
- `publish_review` 用 `pg_insert(...).on_conflict_do_update(index_elements=[scope_type, scope_key, trade_date, publication_kind])` —— 同一 T 重新发布会 **覆盖** `data_run_id`，不会新增行。
- 因此 API 返回哪个 run 由 **"发布动作"** 决定，而非 Z 创建更晚。即使 Z 后建，只要指针指向 Y，API 返回 Y。
- 不发明新 pointer；复用既有 `factor_publications` 唯一约束。

---

## 5. SOURCE_CORE_LINEAGE（PASS）

- `MarketReviewRun.source_core_run_id`、`source_board_run_id`（nullable legacy）、`source_chip_run_id`（nullable legacy）均落库。
- 用户 `overview` 响应显式携带 `sourceCoreRunId` / `sourceBoardRunId` / `sourceChipRunId`（见 `ReviewOverviewResponse`）。
- `ReviewLatestResponse` 不携带 sourceCoreRunId（仅指针信息；`/latest` 重定向到 `/overview` 取全量 lineage，可接受）。
- 创建路径：`_resolve_source_core_run_id` 在 `source_core_run_id is None` 时 **直接 fail-closed**（明确信息："未显式提供 source_core_run_id，Review 必须显式绑定本次 AfterCloseRun 产生的 CoreRun，不得经 stock_core publication pointer 解析"），**绝不**从 `FactorPublication(kind=stock_core)` 回退。
- 内部 DTO 保留 lineage（即使 UI 不全展示），满足任务 §10。

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
| PUBLICATION_KIND_STOCK_CORE | 仅 `review_orchestrator_service.py` 顶部的 **import + 注释/文档字符串**；实际解析为显式 fail-closed（`KPI-2/3/4: reads=0`） | 不引入运行时依赖 |

**发布门禁实际代码**（`evaluate_publish_gate` L182-214）仅校验：
1. `StockFeatureSnapshotRun`（CoreRun）存在且 `trade_date` 匹配；
2. CoreRun `status == succeeded`；
3. scope readiness（market mandatory，其余 progressive optional）；
4. 无 run item 处于 failed/pending/running。

**不**查 stock_core pointer。`review_publication_service.py` 头部与 `evaluate_publish_gate` docstring 旧写"source_core_run_id 必须匹配当前正式 stock_core pointer"——已在 C1 中修正为"必须引用已存在且 succeeded 的 CoreRun（显式 lineage，不取自 stock_core pointer）"（见 §11 修改变更）。

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
| 市场状态 | coverage / eligibleCount / providedCount / coverageRatio | ReviewScopeSummaryDTO + overview.coverage |
| 板块/主题 | industry_l1/l2/l3/concept scope 列表 | scopes 端点 |
| signals | signalCount + 各 scope signal | overview / scopes |
| discoveries | observationGroups / dynamics | scopes/{type}/{key} |
| observations/facts | ReviewScopeCompositionDetailResponse.observation + observationSummary | scopes 端点 |
| attribution | member_attribution 各 layer | scopes/{type}/{key} |
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
| Review partial/degraded | 发布后 200 | `published` + `degradedReasons[]` + 各 scope readiness=not_ready | 完整 payload |
| Review published | 200 | `published` | 完整 payload |
| 交易日无数据 | 404（无发布指针） | `no_published_review` | error detail |

设计意图：**用户仅可见已正式发布的 Review**；running/failed 为 admin-only（调试），避免前端自行猜测状态。此为非歧义合同（非"猜测"），故判 DEFINED。未来可增强用户态状态端点，超出 C1 范围。

---

## 10. 缺口分类（GAP）

| 项 | 状态 | 说明 |
|---|---|---|
| API_EMPTY_STATUS_CONTRACT | DEFINED | running/failed 用户不可见是设计意图，非歧义 |
| ReviewLatestResponse 缺 sourceCoreRunId | 小 gap | `/latest` 重定向到 `/overview` 取全量 lineage，可接受 |
| Tracking 未进前端 | AVAILABLE_BY_DESIGN | 任务§9 允许不全暴露 |
| stale docstring（stock_core pointer） | **C1 已修** | review_publication_service.py / review_orchestrator_service.py 注释与错误信息已对齐真实 fail-closed 行为 |

无重大 product schema / 跨域 owner 缺口 → 无需 checkpoint+STOP。

---

## 11. C1 代码变更（最小、授权内）

仅修正过时注释/错误信息，锁定 `CORE_PUBLICATION_DEPENDENCY=0` 不变量并澄清合同：

- `app/services/review_publication_service.py`：L16-17、L100-101 注释 —— "source_core_run_id 必须匹配 stock_core pointer" → "必须引用已存在且 succeeded 的 CoreRun（显式 lineage，不取自 stock_core pointer）"。
- `app/services/review_orchestrator_service.py`：L230-231/236/253、L395-396/401/416 注释与错误信息 —— 同上对齐：`source_core_run_id` 缺失即 fail-closed，绝不回退 stock_core pointer。

未改任何运行时行为；纯文档一致性修复。

---

## 12. 测试要求（T1 / T2 / 多 run 假绿）

- **T1（modified-scope unit）**：`_resolve_source_core_run_id` 在 `None` 时 fail-closed；`get_published_review_run_id` 解析确定性。
- **T2（API/schema contract）**：`/overview` 响应携带 `sourceCoreRunId` 且等于显式绑定的 CoreRun。
- **targeted PostgreSQL 多 run 假绿**：同日 T，Core A/B + Review Y(A)/Z(B)；发布 Y 后指针=Y（非最新 Z）；再发布 Z 后指针=Z；且 `Y.source_core_run_id == A.id`。使用验证库真实 `FactorPublication` 唯一约束 + SQL lineage。

测试文件：`backend/tests/test_pg_review_read_owner_c1.py`（自包含合成数据，经 `verify_exec.py` 正式通道运行，验证库 `bz_stock_verify_<SHA>` 由 gate cleanup 丢弃，不写生产 `bz_stock`）。

---

## 13. 核验来源

- 后端：`app/models/market_review.py`、`app/services/review_publication_service.py`、`app/services/review_orchestrator_service.py`、`app/services/review_observation_persistence_service.py`、`app/api/review.py`、`app/api/admin_review.py`、`app/schemas/review.py`
- 前端：`frontend/src/features/review/types.ts`、`frontend/src/features/review/api.ts`
- 依赖扫描：regex 检索 `review*.py` + `app/api/*review*.py`
- 本 map 完整核验：2026-08-27（PHASE C1）
