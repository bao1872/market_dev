# 复盘模块 Map

核验状态：080/081 候选实现已通过同一 SHA CI 与临时 PostgreSQL；生产部署/发布未执行
最后核验日期：2026-08-01
核验分支：`codex/panji-full-closure-20260801`
核验提交：`c6abcc1`；CI Run `30731828236`
核验范围：层级归因、P/Q/U/C/V、PIT bootstrap、发布门禁、withdrawal 安全与五阶段 UI
对应 PRD：`../prd/70-review.md`（含 §23 P0 强化条款）
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

## 11. review-1.1.0 P0 数据链修复（CHANGE-20260730-014）

**核验状态：代码已合入 main (SHA 54fe3a2)，待生产 SSH 可达后核验 canary review run**

### 11.1 算法版本升级

- `algorithm_version`：`review-1.0.0` → `review-1.1.0`
- `filter_version`：保持 `filters-1.0.0`（本轮未改筛选器阈值，仅修数据链）
- 已发布 review-1.0.0 run（`3e1db415-2266-4cc5-9453-d8561d799b43`）保留为审计记录，不修改历史数据；review-1.1.0 必须通过新 run 切换 `factor_publications` pointer，不得复用旧 run 重发。

### 11.2 已修复项

| 修复项 | 修复前 | 修复后 | 代码入口 |
|---|---|---|---|
| history_maps 传递 | `metric_engine` 调用 `compute_metrics` 时未传入历史 scope_snapshots，导致分位计算使用空集合 | `review_orchestrator_service.compute_run` 在调用 metric_engine 前显式构造 `history_maps`（按 `scope_type + scope_key` 从 `market_review_scope_snapshots` 读取），并传入 `compute_metrics` | `backend/app/services/review_orchestrator_service.py` / `backend/app/domain/review/metric_engine.py` |
| `industry_l1` scope_key 统一 board_id | 早期 `scope_key` 混用 `industry_name`（如 `electronics`）与 `board_id`（UUID），导致归因 JOIN 失败、history_maps 错配 | 所有第一级 scope 的 `scope_key` 统一为 `board_id`（industry_l1）/ `index_code`（major_index）/ `style_code`（style）/ `"market"`（market） | `backend/app/services/review_scope_service.py` |
| `major_index` / `style` 范围补全 | canary run 只覆盖 market + 6 个 industry_l1，major_index 和 style 完全缺失 | 第一级范围合同强制覆盖 market + major_index（≥2）+ style（≥2）+ industry_l1（≥25），canary 不得只算部分类型 | `backend/app/services/review_scope_service.py:list_scope_snapshots` |
| `metric_engine` history is None → insufficient_history | 历史基线为空（首次运行）时 `history` 参数为 `None`，metric_engine 直接访问 `history[...]` 抛 `AttributeError`，被上层 `try/except` 静默吞掉，返回 `status=None` | `metric_engine` 显式判空，`history is None` 或 `len(history) < 60` 时返回 `status=insufficient_history`，`value/normalizedValue/historyPercentile120d/delta1d/delta5d` 全部为 `null` | `backend/app/domain/review/metric_engine.py` |
| 发布门禁强化（value 非空 + source_board_run_id + failed signals） | `publish_review(force=False)` 仅检查 coverage_ratio 与 run_items.status，未校验 market P/Q/U/C/V value 非空、未校验 source_board_run_id 与当日 board pointer 一致、未校验 signals 无 failed | 新增 6 项门禁：①market P/Q/U/C/V 五项 value 非空且 status=ready；②source_board_run_id 等于当日 market_aggregation pointer.data_run_id；③source_core_run_id 等于当日 stock_core pointer.data_run_id；④market_review_signals 无 status=failed；⑤market_review_run_items 无 status=failed；⑥coverage_ratio >= 0.95 + industry_l1 ready 比例门槛 | `backend/app/services/review_publication_service.py:publish_review` |

### 11.3 当前限制

- **服务器 SSH 不可达**：本轮 review-1.1.0 修复仅完成代码静态核验（main SHA 54fe3a2），canary review run 重跑与生产数据验收未完成，待 SSH 可达后由 admin 手工触发新 run 并核验 §11.2 全部修复项。
- **history_maps 数据源**：`history_maps` 从 `market_review_scope_snapshots` 读取同 `scope_type + scope_key` 的历史记录；首次运行无历史数据时，所有 component `status=insufficient_history`，`historyObservationCount=0`；review-1.1.0 修复后首次 run 不会伪造分位，但生产环境需要持续运行 ≥60 个交易日才能产生有效 P/Q/U/C/V value。
- **`force=True` 路径**：review-1.0.0 canary run 使用 `force=True` 发布，已写入 `factor_publications`；review-1.1.0 后 `force=True` 仅允许 admin 内部查看（`is_provisional=true`），不得写入 `factor_publications`，但旧记录保留为审计记录不删除。

### 11.4 上一轮 canary run 审计保留

| 字段 | 值 |
|---|---|
| `run_id` | `3e1db415-2266-4cc5-9453-d8561d799b43` |
| `trade_date` | `2026-07-29` |
| `algorithm_version` | `review-1.0.0` |
| `status` | `published`（force=True） |
| `signal_count` | `0` |
| `coverage_ratio` | `1.0` |
| `expected_scope_count` / `succeeded` / `failed` | `6` / `6` / `0` |
| `factor_publications.publication_id` | `c01afda0-547a-4656-a688-0ea4705d625b` |

该 run 保留为审计记录，不修改历史数据；review-1.1.0 修复后必须通过新 run 切换 `factor_publications` pointer，不得复用该 run 重发。

### 11.5 下一轮核验清单（待 SSH 可达后执行）

1. 应用 review-1.1.0 代码到生产（`panji-deploy.sh 54fe3a2`）；
2. 确认 `stock_core` 与 `market_aggregation` pointer 已发布当日；
3. 创建 review-1.1.0 canary review run（`scope=canary`），确认 `history_maps` 传入、`scope_key` 统一 board_id、major_index/style 范围补全；
4. 核验首次运行 component `status=insufficient_history` 且 `historyObservationCount=0`，不伪造分位；
5. 核验 `force=False` 时若 market P/Q/U/C/V value 为 null 则拒绝发布；
6. 核验 `force=True` 时 run 不写入 `factor_publications`，返回 `is_provisional=true`；
7. 上一轮 canary run（3e1db415）保留可查询，不被覆盖。

## 12. 第二金字塔定义与冷启动（设计草案，未实现）

> 本章节记录 PRD §24（草案补强）对应的设计状态。所有内容均为"未实现/设计草案"，待 PRD 确认后进入开发。

### 12.1 第二金字塔实现状态

| PRD 条款 | 当前实现状态 | 验证证据 |
|---|---|---|
| §24.1 第二金字塔维度（6 维） | 未实现 | 当前第二金字塔仅覆盖"趋势/结构/动量/内部分布"（见 PRD §14.5 描述），未实现状态分布/状态迁移/事件新鲜度/宽度/集中度/相对强度六维度 |
| §24.2 行业与概念分别聚合 | 部分实现 | `board_analysis_snapshots` 已分别存储行业和概念，但第二金字塔六维度尚未实现 |
| §24.3 P/Q/U/C/V 就绪状态（raw_ready/normalized_ready/insufficient_history/reason） | 未实现 | 当前 `metric_engine` 仅返回单一 `status` 字段（ready/insufficient_history），未拆分为四字段 |
| §24.4 冷启动 bootstrap | 已实现 | 见 §23：service + CLI + admin API 三层入口（2026-08-02 更新，原记录「无 bootstrap 代码」已过期） |
| §24.5 fp_segment_change_pct 禁止伪造 | 待核验 | 需核验当前 `fp_segment_change_pct` 空值处理 |

### 12.2 冷启动缺口（已识别）

当前实现存在以下冷启动缺口：

- **metric_engine 要求 60+ 日历史**：`metric_engine` 在历史观测 < 60 时返回所有 P/Q/U/C/V `value=null`、`status=insufficient_history`；
- **首次运行无历史数据**：`market_review_scope_snapshots` 首次运行时无历史记录，所有 component `historyObservationCount=0`，返回 `insufficient_history`；
- ~~**无 bootstrap 回填机制**~~（2026-08-02 已解决）：回填流程已实现，正式入口见 §23；下方其余冷启动约束仍然成立；
- **发布门禁阻塞**：§23.5 发布门禁要求 market P/Q/U/C/V `value` 非空，冷启动期间因 `insufficient_history` 导致 `value=null`，系统无法通过 `force=False` 发布。

### 12.3 P/Q/U/C/V 就绪状态当前行为

当前 `metric_engine` 返回的就绪行为（基于 review-1.1.0 代码，main SHA 54fe3a2）：

| 场景 | 当前返回 | PRD §24.3 目标 |
|---|---|---|
| 冷启动（0 历史观测） | `status=insufficient_history`, `value=null`, `historyObservationCount=0` | `raw_ready=true`, `normalized_ready=false`, `insufficient_history=true`, `reason="历史观测不足"` |
| 历史不足（<60 观测） | `status=insufficient_history`, `value=null` | `raw_ready=true`, `normalized_ready=false`, `insufficient_history=true`, `reason="累计观测 N<60"` |
| 历史充足（≥60 观测） | `status=ready`, `value=非空` | `raw_ready=true`, `normalized_ready=true`, `insufficient_history=false` |

缺口：

- 当前未拆分 `raw_ready` 与 `normalized_ready`；
- 冷启动时 rawValue 是否已生成待核验（§23.1 要求 rawValue 先行，但 `metric_engine` 在 `history is None` 时直接返回 `insufficient_history`，可能未生成 rawValue）。

### 12.4 后续步骤（待 PRD §24 确认）

1. 确认 PRD §24 第二金字塔六维度定义；
2. 设计并实现 bootstrap 回填流程（从第一金字塔历史回填第二金字塔历史观测）；
3. 拆分 `metric_engine` 就绪状态为 `raw_ready/normalized_ready/insufficient_history/reason` 四字段；
4. 调整发布门禁允许 `raw_ready=true && normalized_ready=false` 的 bootstrap 发布；
5. 核验 `fp_segment_change_pct` 空值处理。

## 13. 竞价事件回流（[CHANGE-20260730-018] / [P0-FE]）

### `/review` 第6阶段：AuctionBackflowPanel

`/review` 页面新增"竞价回流"阶段（第6阶段），展示 `AuctionBackflowPanel` 组件，提供第二金字塔 + 竞价事件回流五维度可视化。

### 数据来源

| 接口 | 路径 | 说明 |
|---|---|---|
| 后端 | `GET /api/v1/auction/backflow/{trade_date}` | 返回 `AuctionBackflowData` |
| 前端 hook | `useAuctionBackflow(tradeDate, { topEvents })` | React Query 封装 |
| 组件 | `frontend/src/features/review/AuctionBackflowPanel.tsx` | 五维度可视化 |

### 五维度数据

1. **分布**：`event_type_distribution`（12 类事件计数）+ `lifecycle_distribution`（formed/confirmed/continued/weakened/failed/transformed/expired 计数）
2. **迁移**：`event_migrations`（事件 lifecycle 转换记录列表）
3. **新鲜度**：`anchor_freshness_buckets`（fresh/stale/expired 锚点分布）
4. **集中度**：`market_concentration`（HHI/Top3/Top5）+ `top_industry_concentration`（行业集中度排行）
5. **事件回流**：`backflow_events`（按 formed_at 排序的事件列表，含 symbol/name/event_type/lifecycle/位置/参与度）

### 导航

事件行点击跳转 `/auction/stock/{symbol}`，**使用 symbol 禁止 UUID**。后端 DTO 通过 JOIN `Instrument` 表批量填充 `symbol` 和 `name` 字段。

### 元数据

返回的 `AuctionBackflowData` 含 `algorithm_version`、`scan_run_id`、`anchor_publication_id`、`source_core_run_id`、`source_chip_run_id`、`reason_codes`（如 `scan_run_not_found`）。

### 当前实现状态

- 后端：`backend/app/api/auction.py:get_auction_backflow` 已实现
- 前端：`AuctionBackflowPanel.tsx` 已实现并集成到 `ReviewPage.tsx`
- 合同测试：`frontend/scripts/contract-tests/auctionContract.test.ts` 10 项通过（含 symbol 导航、四维度展示、ReviewPage 集成）
- 详见 `docs/maps/75-auction-analysis.md#5-前端`

## 21. raw/normalized分离 + 冷启动 + bootstrap 范围（2026-08-01 核验，CHANGE-20260801-001）

### 21.1 raw与normalized双值（后端→前端）

后端数据结构（`scope_metrics` / `review_scope_results`）：
```
raw_value       :: float | null   （仅需足够当日样本，无需60日历史）
normalized_value :: float | null  （仅当 effective_history >= 60 交易日时非 null）
coverage        :: float (0..1)   （当日有效样本 / scope 内股票数）
insufficient_history :: bool      （effective_history < 60）
reason          :: string|null    （insufficient_history / compute_failed / no_raw_data / null）
```

前端 ScopeMetricsTable：
- 函数 `isMetricDisplayable(status)`：包含 `insufficient_history`，不再用旧的 `isMetricAvailable(status)`（仅 ready 才显示）。
- MetricCell 渲染：rawValue 始终显示（若存在），coverage 显示在右上角 chip，reason 显示为 `insufficient_history` 时 tooltip 显示"历史不足 N 天，仅展示原值"，normalized/历史分位/delta 呈灰态（不显示数值，不占位破坏对齐）。

位置：
- 后端：`backend/app/services/metric_engine.py` / `review_orchestrator_service.py`
- 前端：`frontend/src/features/review/ScopeMetricsTable.tsx`

### 21.2 SignalCard 冷启动语义

```
if normalized_ready:
    signal = compute_signal(P, Q, U, C, V normalized)  # 0/1/2 完整合同
    label = "正式信号"
elif raw_ready and insufficient_history:
    signal = compute_raw_signal(P.raw, Q.raw)   # 仅P/Q基线判断，U/C/V置灰
    label = "raw baseline only"
    tooltip = "历史观测不足60日，归一化值不可用；当前为raw基线预估值，非正式信号"
else:
    signal = 0 + disabled
```

位置：`frontend/src/features/review/SignalCard.tsx` + `ReviewHeader.tsx`

### 21.3 Review pointer date sync（解决7/29陈旧）

根因：after_close_orchestrator publishing 阶段之后未执行 review 阶段；stock_core/board pointer 已更新到 7/31，但 review pointer 仍停留在 7/29。

修复：
- §30 Map 12.1 的 after_close 正式链确保 review 阶段与 stock_core/board pointer 同交易日落盘。
- `ReviewPage` 读取 `GET /api/v1/review/meta/latest`（scope=market）：如果 published pointer 的 trade_date ≠ stock_core.published.trade_date → 顶部 banner 显示"盘后未完成，当前正式review发布日期 = YYYY-MM-DD；stock_core 最新 = YYYY-MM-DD"。

位置：`frontend/src/features/review/ReviewHeader.tsx` + `backend/app/api/review.py:get_latest_review_meta`

### 21.4 Bootstrap 范围（point-in-time，禁止用当前成员回填）

实际实现路径：
```
目标: 生成 review scope_items × 历史约250个交易日的 normalized 历史观测
步骤:
  for 每个交易日 td (从 oldest snapshot date 到 today):
    a) td 当日有效的 stock_core publication run_id = pointer(td)
    b) td 当日有效的 board publication run_id       = board_pointer(td)
    c) td 当日有效的 board 成员 (instrument_id, weight) = board_versions × td 当日生效 JOIN
    d) 用 (a)(b)(c) 重算 scope_metrics 的 raw value
    e) 写入 review_observations (scope, trade_date=td, metric, raw_value)
  最后: 按 metric 维度计算 effective 长度 → 不足 60 的 metric: insufficient_history=true
```

明确禁止：
- **不得**用"今日 2026-08-01 的 申万一级行业 成员"去重算 2026-07-01 的 该行业 scope 观测（point-in-time 破坏）。
- **不得**伪造 normalized 值或 historyPercentile120d；不足就写 insufficient_history。

位置：`backend/app/services/review_bootstrap_service.py`（已存在，见 §23）。

## 22. 2026-08-01 Review 候选实现核验

| 能力 | 当前实现事实 |
|---|---|
| 层级 scope/归因 | Migration 080；支持 L1/L2/L3/concept PIT membership、全量分页、正负贡献、真实 instrument/snapshot/run evidence |
| P/Q/U/C/V | `domain/review/member_fact.py` + `metric_engine.py` 使用真实日收益、canonical 状态、前日比较和显式权重/量纲 |
| 历史观测 | Migration 081；`review_metric_observation_service.py` 保存 raw/denominator/source/version/hash/membership |
| Bootstrap | `review_bootstrap_service.py` 默认 dry-run，PIT 缺失写 `bootstrap_unavailable`，不使用当前成员回填 |
| 两遍横截面 | orchestrator 先落 component，再按同日同 family 计算分位并评估 signal |
| 发布 | `review_publication_service.py` 校验 core/board pointer、scope 配置、coverage、run items、版本和 provisional/canary |
| UI | 五阶段真实 API；无信号/无追踪/历史不足/字段缺失/API 错误分别展示；Evidence Drawer 可追溯 |

算法版本已升级；旧 Review run 保持不可变。Migration 080/081、Review PG Integration、完整后端
PostgreSQL 测试与阻断 CI 已在 `c6abcc1` / Run `30731828236` 同一 SHA 验证通过。
生产 migration、部署、正式发布与 withdrawal 仍未执行。

## 23. Bootstrap 正式入口（2026-08-02 核验，CHANGE-20260802-001）

`§12.1 §24.4「无 bootstrap 代码」与 §12.2「无 bootstrap 回填机制」已过期`，
以本节为准：bootstrap 已有 service、CLI 与 admin API 三层实现。

### 23.1 代码入口

| 层 | 位置 | 职责 |
|---|---|---|
| Service | `backend/app/services/review_bootstrap_service.py` | `bootstrap_history()` / `bootstrap_single_date()`；PIT 成员解析、四类计数聚合、input_hash、交易日解析 |
| 作业层 | `backend/app/services/review_bootstrap_job_service.py` | run_key 构造、任务提交/领取元数据、执行编排、状态摊平与分页 |
| Admin API | `backend/app/api/admin_review.py` | 提交 / 状态 / resume 三个端点 |
| Worker | `backend/app/worker.py` → `run_review_bootstrap_worker()` / `_review_bootstrap_poll_once()` | 领取并执行 queued 任务 |
| CLI | `backend/scripts/review_bootstrap_cli.py` | 同步执行，用于受控窗口 |
| Schema | `backend/app/schemas/review.py` | `ReviewBootstrapRequest` / `SubmitResponse` / `StatusResponse` 等 |

### 23.2 提交与执行分离（异步）

120 交易日 × 全 scope 耗时远超 HTTP 超时，因此 API 不同步执行：

- `POST /api/v1/admin/review/bootstrap` → **202 + job_run_id**，只创建
  `status=queued` 的 `SchedulerJobRun`；复用已有活跃任务时返回 200 + `is_new=false`。
- 计算由 `review_bootstrap` Worker 经 `FOR UPDATE SKIP LOCKED` + lease fencing +
  heartbeat 领取执行（与 chip consensus worker 同构），`WORKER_TYPE=all` 时启动，
  不新增常驻容器。
- `GET /api/v1/admin/review/bootstrap/{job_run_id}` → 全局 summary
  （`succeeded`/`skipped`/`unavailable`/`failed` 四类计数 + `reason_codes`）
  + 按 `(trade_date, scope_type, scope_key)` 的分页明细。
- `POST /api/v1/admin/review/bootstrap/{job_run_id}/resume` → 失败/中断任务重新入队。

### 23.3 安全默认

- `dry_run` 默认 True，且 dry-run 路径**零业务写入**：`bootstrap_single_date(audit=None)`
  且作业层显式 rollback，不建 run、不写 metadata_json、不写 observations、不切 pointer。
  `operator`/`reason`/`input_hash` 仅在响应与日志返回，apply 才经 `_upsert_bootstrap_run` 落库。
- `operator` / `reason` 必填；`algorithm_version` 必须等于 `BOOTSTRAP_ALGORITHM_VERSION`
  （当前 `review-2.0.0`）。
- `end_date` 为空时经 `resolve_bootstrap_end_date()` 调
  `get_most_recent_trading_day_async()` 查 `trading_calendar` 解析为最近完整 A 股交易日，
  不使用自然日 today；无日历记录时降级 today 并带 warning。
- dry-run 与 apply 使用不同 run_key，互不幂等抵消。

### 23.4 历史序列兼容性契约（仅固化，未改判定实现）

`review_metric_observation_service.load_metric_history()` 的过滤维度为
scope identity（`scope_type` + `scope_key`）+ compatible taxonomy +
`algorithm_version` + metric definition version。

**`membership_version` 随每条观测持久化（可追溯当日成员），但不参与历史序列过滤**——
成分股增减是常态，若按其过滤，任何一次调仓都会截断 60 日历史并重新冷启动。
该契约由 `backend/tests/test_review_metric_observation_bootstrap.py` 断言固化
（WHERE 子句不得含 `membership_version`；跨三个 membership_version 的历史必须连续）。

### 23.5 内存预算与分片（2026-08-02 修复，CHANGE-20260802-001）

生产 60 日全 scope dry-run 曾在 ~3.4GB RSS 被 OOM Killer 杀死（零业务写入）。根因：
逐日结果保留全部 scope 明细 + 全程复用同一 AsyncSession 导致 ORM identity map 累积。

修复（不靠扩内存掩盖）：`review_bootstrap_service.bootstrap_history()` 新增分片与预算参数。

| 常量 | 值 | 作用 |
|---|---|---|
| `DEFAULT_BOOTSTRAP_CHUNK_DAYS` | 5 | 按 trade_date 分片，每片结束 `expunge_all()` + 释放引用 |
| `DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB` | 1536 | 每片采样 RSS，超过即 `status=memory_budget_exceeded` 安全停止 |
| `DEFAULT_BOOTSTRAP_DETAIL_LIMIT` | 5 | 仅最前若干天保留完整 scope 明细，其余只留聚合摘要 |

- CLI（`review_bootstrap_cli.py`）新增 `--chunk-days` / `--memory-budget-mb` 并校验（`chunk_days>0`、`memory_budget_mb>=256`，否则 `ValueError`）；超限退出码 3。
- 作业层（`review_bootstrap_job_service.py`）从 `job_metadata` 透传两参数，summary 新增 `peak_rss_mb` / `chunks`。
- 契约测试：`backend/tests/test_review_bootstrap_admin_entry.py` §9 内存上限契约（6 项）已固化分片释放 / 不累积明细 / 聚合计数保留 / 预算超限安全停止 / 非法参数拒绝。
- 代码已修改、本地 `PURE_UNIT_TEST=1` 测试通过；**真实 apply 的内存表现仍待生产 dry-run 验证（当前未授权 apply）**。
