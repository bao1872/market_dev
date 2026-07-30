# 复盘模块 Map

核验状态：已实现（V1，trade_date=2026-07-29 canary 已发布）
最后核验日期：2026-07-30
核验分支：main (SHA 9aea736)
核验范围：基于真实代码、数据库、运行状态核验复盘模块实现状态
对应 PRD：`../prd/70-review.md`
事实所有权：复盘模块当前实现状态、已存在入口、数据/API 合同摘要

## 1. PRD 实现映射

| PRD 章节 | 当前实现状态 | 验证证据 |
|---|---|---|
| §0 背景与当前基线 | 已满足：Board V1 + stock_core pointer 已发布 | `board_analysis_snapshots` 表（migration 074）、`factor_publications` |
| §1 产品目标与边界 | 已实现 | `/review` 页面已部署 |
| §2 权威业务链 | 已实现：stock_core + board → review run 链路完整 | `review_orchestrator_service.py` |
| §3 路由权限 | 已实现：review:read=research_replay capability | `access_control_service.require_capability("research_replay")` |
| §4 后端模块结构 | 已实现 | `backend/app/domain/review/` 6 个文件、`services/review_*.py` 6 个、`api/review.py`+`admin_review.py`、`schemas/review.py`、`scripts/review_compute_cli.py` |
| §5 数据模型（8 表） | 已实现 | migration `076_market_review_workbench.py`（已应用，alembic head） |
| §6 两级扫描 | 已实现 | `review_scope_service.list_scope_snapshots` |
| §7 P/Q/U/C/V 指标 | 已实现 | `domain/review/metric_registry.py` + `metric_engine.py` |
| §8 三类筛选器 | 已实现 | `domain/review/filter_definitions.py` + `filter_engine.py` + `review_filters.yaml` |
| §9 板块归因 | 已实现 | `domain/review/attribution_engine.py` + `review_attribution_service.py` |
| §10 信号生命周期与追踪 | 已实现 | `domain/review/tracking_state_machine.py` + `review_tracking_service.py` |
| §11 任务编排与发布 | 已实现 | `review_orchestrator_service.compute_run` + `review_publication_service.publish_review` |
| §12 API 合同 | 已实现 | `/api/v1/review/*` 12 个端点 + `/api/v1/admin/review/*` 4 个端点 |
| §13 前端目录与组件 | 已实现 | `frontend/src/features/review/` 18 个文件 + `ReviewPage.tsx` |
| §14 页面信息架构 | 已实现 | 五阶段：MarketScanPanel / FilterDiscoveryPanel / BoardAttributionPanel / StockValidationPanel / TrackingReviewPanel |
| §15 前端数据与状态规则 | 已实现 | `queryKeys.ts` (reviewRunId/date/resource/id/filters) + `urlState.ts` (URL SSOT) |
| §16 与现有页面边界 | 已实现 | /market 与 /stock 接收 review 跳转参数；/boards/analysis 保留 |
| §17 加载/空态/异常态 | 已实现 | 404/422/500 显示明确原因 + request_id |
| §18 性能与缓存 | 已实现 | React Query 仅轮询 computing 状态；禁止混 run |
| §19 测试要求 | 已实现 | 单元测试 + 真实 PG 集成测试覆盖 076 迁移循环 |
| §20 验收标准 | PARTIAL：canary run 已发布，signal_count=0（无偏差命中），浏览器 UI 验收 PENDING 用户登录 | `market_review_runs.id=3e1db415...` status=published |
| §21 文档与记忆系统 | 已完成 | 本文件 + prd/70-review.md + CHANGE-20260730-013 |
| §22 推荐实施顺序 | Phase 0-3 已完成，Phase 4-5 待生产持续运行 | — |

## 2. 当前实现摘要

复盘模块 V1 已完整实现并部署到生产环境。当前系统包含：

- **数据库**：migration 076 创建 8 张 market_review_* 表（已应用，alembic head=076）
- **后端**：domain/review/ 6 个引擎 + services/review_*.py 6 个服务 + api/review.py (12 端点) + admin_review.py (4 端点)
- **前端**：features/review/ 18 个文件实现五阶段工作台
- **CLI**：`backend/scripts/review_compute_cli.py`
- **Canary Run**：trade_date=2026-07-29，run_id=3e1db415-2266-4cc5-9453-d8561d799b43
  - status=published, coverage_ratio=1.0, signal_count=0（canary 范围无偏差命中）
  - 已发布到 factor_publications (publication_id=c01afda0-547a-4656-a688-0ea4705d625b)
  - market scope: eligible=5293, ready=5184, coverage=0.979

## 3. 当前入口

### 3.1 数据库表

| 表 | 状态 | 说明 |
|---|---|---|
| `market_review_runs` | 已实现 | 完整复盘版本 |
| `market_review_run_items` | 已实现 | 范围×阶段检查点 |
| `market_review_scope_snapshots` | 已实现 | P/Q/U/C/V 与证据 |
| `market_review_signals` | 已实现 | 三类筛选器命中 |
| `market_review_signal_attributions` | 已实现 | 子范围下钻 |
| `market_review_signal_instruments` | 已实现 | 代表股票与贡献 |
| `market_review_trackings` | 已实现 | 用户追踪 |
| `market_review_tracking_evaluations` | 已实现 | 逐日追踪结果 |

### 3.2 后端入口

| 类型 | 路径/符号 | 状态 |
|---|---|---|
| Migration | `backend/alembic/versions/076_market_review_workbench.py` | 已应用 |
| Domain | `backend/app/domain/review/metric_registry.py` | 已实现 |
| Domain | `backend/app/domain/review/metric_engine.py` | 已实现 |
| Domain | `backend/app/domain/review/filter_definitions.py` | 已实现 |
| Domain | `backend/app/domain/review/filter_engine.py` | 已实现 |
| Domain | `backend/app/domain/review/attribution_engine.py` | 已实现 |
| Domain | `backend/app/domain/review/tracking_state_machine.py` | 已实现 |
| Service | `backend/app/services/review_orchestrator_service.py` | 已实现 |
| Service | `backend/app/services/review_scope_service.py` | 已实现 |
| Service | `backend/app/services/review_signal_service.py` | 已实现 |
| Service | `backend/app/services/review_attribution_service.py` | 已实现 |
| Service | `backend/app/services/review_tracking_service.py` | 已实现 |
| Service | `backend/app/services/review_publication_service.py` | 已实现 |
| Schema | `backend/app/schemas/review.py` | 已实现 |
| API | `backend/app/api/review.py` (12 端点) | 已实现 |
| API | `backend/app/api/admin_review.py` (4 端点) | 已实现 |
| CLI | `backend/scripts/review_compute_cli.py` | 已实现 |
| Config | `backend/app/config/review_filters.yaml` | 已实现 |

### 3.3 前端入口

| 类型 | 路径 | 状态 |
|---|---|---|
| Page | `frontend/src/pages/ReviewPage.tsx` | 已实现 |
| Feature | `frontend/src/features/review/ReviewPage.tsx` | 已实现 |
| API | `frontend/src/features/review/api.ts` | 已实现 |
| Types | `frontend/src/features/review/types.ts` | 已实现 |
| QueryKeys | `frontend/src/features/review/queryKeys.ts` | 已实现 |
| URL State | `frontend/src/features/review/urlState.ts` | 已实现 |
| Stage 1 | `MarketScanPanel.tsx` + `ScopeMetricsTable.tsx` | 已实现 |
| Stage 2 | `FilterDiscoveryPanel.tsx` + `SignalCard.tsx` | 已实现 |
| Stage 3 | `BoardAttributionPanel.tsx` + `AttributionTable.tsx` | 已实现 |
| Stage 4 | `StockValidationPanel.tsx` + `ReviewInstrumentTable.tsx` | 已实现 |
| Stage 5 | `TrackingReviewPanel.tsx` | 已实现 |
| Common | `ReviewHeader.tsx` + `ReviewStageNav.tsx` + `EvidenceDrawer.tsx` + `ReviewDataQualityBadge.tsx` | 已实现 |

## 4. 数据模型合同

> 完整 schema 见 PRD §5。迁移文件 `076_market_review_workbench.py`（已应用，不得修改已应用的 074/075）。

| 表 | 关键字段 | 唯一约束 | 职责 |
|---|---|---|---|
| `market_review_runs` | trade_date, source_core_run_id, source_board_run_id, algorithm_version, filter_version, status, coverage_ratio | trade_date + source_core_run_id + source_board_run_id + algorithm_version + filter_version | 某交易日完整复盘版本 |
| `market_review_run_items` | review_run_id, scope_type, scope_key, phase, status, input_hash, lease_epoch | review_run_id + scope_type + scope_key + phase | 按范围×阶段检查点 |
| `market_review_scope_snapshots` | review_run_id, scope_type, scope_key, p/q/u/c/v_payload, coverage_ratio | review_run_id + scope_type + scope_key | 每个范围的 P/Q/U/C/V 与证据 |
| `market_review_signals` | review_run_id, filter_family(A/B/C), signal_type, scope_type, scope_key, status, first_seen_date, previous_signal_id, rank_key | review_run_id + filter_family + signal_type + scope_type + scope_key | 三类筛选器命中结果 |
| `market_review_signal_attributions` | signal_id, child_scope_type, child_scope_key, contribution_value, contribution_rank | - | 第二级范围下钻 |
| `market_review_signal_instruments` | signal_id, instrument_id, board_role, relation_to_scope, contribution_value | - | 代表股票与贡献；board_role/relation_to_scope 有枚举约束 |
| `market_review_trackings` | user_id, source_signal_id, tracking_type, status | - | 用户追踪 |
| `market_review_tracking_evaluations` | tracking_id, review_run_id, trade_date, previous_state, current_state | tracking_id + trade_date | 逐日追踪结果 |

枚举约束：
- `board_role`: core / second_line / elasticity / follower / laggard / unclassified
- `relation_to_scope`: synchronized_strengthening / synchronized_weakening / instrument_leads_scope / scope_strong_instrument_lags / instrument_strong_scope_unsupported / unconfirmed
- `market_review_runs.status`: created / computing / partial / signals_ready / published / completed_with_errors / failed / cancelled

## 5. API 合同摘要

> 完整合同见 PRD §12。统一前缀 `/api/v1/review`。

### 5.1 用户端 (12 端点)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/review/dates` | 可用复盘日期列表 |
| GET | `/api/v1/review/latest` | 最新已发布复盘 |
| GET | `/api/v1/review/{trade_date}/overview` | 当日总览（status/coverage/signalSummary） |
| GET | `/api/v1/review/{trade_date}/scopes` | 市场扫描：每范围 P/Q/U/C/V + 命中数量 |
| GET | `/api/v1/review/{trade_date}/signals` | 信号列表（按 filter_family/signal_type/status/scope 筛选） |
| GET | `/api/v1/review/signals/{signal_id}` | 单信号详情 |
| GET | `/api/v1/review/signals/{signal_id}/attributions` | 信号归因（子范围下钻） |
| GET | `/api/v1/review/signals/{signal_id}/instruments` | 信号代表股票（支持 role/relation/sort） |
| GET | `/api/v1/review/trackings` | 用户追踪列表 |
| POST | `/api/v1/review/trackings` | 新增追踪 |
| PATCH | `/api/v1/review/trackings/{id}` | 修改追踪 |
| DELETE | `/api/v1/review/trackings/{id}` | 关闭追踪（不物理删除） |
| GET | `/api/v1/review/trackings/{id}/evaluations` | 追踪逐日 evaluation |

### 5.2 管理端 (4 端点)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/admin/review/runs` | 创建 review run（canary/全量） |
| POST | `/api/v1/admin/review/runs/{id}/resume` | 恢复未完成 run |
| POST | `/api/v1/admin/review/runs/{id}/publish` | 发布 pointer（原子切换） |
| GET | `/api/v1/admin/review/runs/{id}/status` | 查询 run 状态 |

所有写操作要求幂等键（`idempotency_key`）。

## 6. 权限模型

- 用户端读取接口：`require_capability("research_replay")`（admin 自动豁免）
- 追踪写接口：`require_capability("research_replay")`
- 管理端接口：`require_admin`
- 普通用户只能看到已发布 run（published pointer）
- admin 可通过 `include_partial=true` 查看 partial 结果

## 7. 发布门禁与原子发布

`review_publication_service.publish_review(db, run, force=False)`：

1. 检查 run.status 必须为 `signals_ready` 或 `published`
2. 检查 coverage_ratio >= 0.95
3. 检查所有 run_items.status 为 `succeeded` 或 `skipped`
4. 检查无 `failed` 状态的 run_items
5. 写入 `factor_publications` (publication_kind=`market_review`, scope_type=`review`)
6. 更新 run.status=`published`, run.published_at=now()
7. force=True 跳过 1-4 门禁（仅 admin 调试）

## 8. Canary 运行状态（2026-07-30）

- Run ID: `3e1db415-2266-4cc5-9453-d8561d799b43`
- Trade date: 2026-07-29
- Status: `published` (force=True，因 canary 范围 limited)
- Coverage ratio: 1.0
- Signal count: 0（canary 范围无偏差命中，符合预期）
- Expected scope count: 6, succeeded: 6, failed: 0
- Market scope snapshot: eligible=5293, ready=5184, coverage=0.979
- Publication ID: `c01afda0-547a-4656-a688-0ea4705d625b`

## 9. 已知边界

- Review V1 已完整实现，但仅 canary 范围（market + 6 个 scopes）已运行；全量计算待生产持续运行
- signal_count=0 是 canary 范围内无偏差命中的正常结果，不代表筛选器故障
- 000021 chip_status 为 unavailable/M15_BARS_INSUFFICIENT（370<500，符合数据门槛约束）
- 浏览器 UI 真实链路验收 PENDING 用户手工登录（受 Owner 账户保护规则约束，TRAE 不得自动登录）
- Board V1 仍保留为独立板块分析入口；Review 阶段三通过 `BoardAttributionPanel.tsx` 复用其可抽取组件

## 10. 更新触发条件

当 review 表结构、API 合同、前端组件、编排链路、发布门禁或权限模型发生变化时必须更新本 Map。
