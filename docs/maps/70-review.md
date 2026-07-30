# 复盘模块 Map

核验状态：待实现（复盘模块尚未开始开发）
最后核验日期：2026-07-30
核验分支：dev
核验范围：基于现有代码核验复盘模块实现状态；当前仅 Board V1 作为输入基线存在
对应 PRD：`../prd/70-review.md`
事实所有权：复盘模块当前实现状态、已存在入口、计划实现入口与数据/API 合同摘要

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。
> 复盘模块尚未实现：当前不存在 review_runs/signals/attributions/trackings 表，不存在 /review 路由，不存在 ReviewPage，不存在 review API。

## 1. PRD 实现映射

| PRD 章节 | 当前实现状态 | 验证证据 |
|---|---|---|
| §0 背景与当前基线 | 部分满足：Board V1 与 stock_core pointer 已存在 | `board_analysis_snapshots` 表（migration 074）、`BoardAnalysisPage.tsx`、`factor_publications` |
| §1 产品目标与边界 | 未实现 | 无 /review 页面 |
| §2 权威业务链 | 未实现：stock_core + board 已发布，但 review run 链路不存在 | `after_close_orchestrator` 编排止于 board_analysis 发布 |
| §3 路由权限 | 未实现：无 /review 路由；review:read/review:track/review:admin 权限待核验 | 无 `ReviewPage.tsx` |
| §4 后端模块结构 | 未实现：无 `backend/app/domain/review/` 目录 | 目录不存在 |
| §5 数据模型（8 表） | 未实现：无任何 market_review_* 表 | migration 075 未创建 |
| §6 两级扫描 | 未实现 | 无 scope_service |
| §7 P/Q/U/C/V 指标 | 未实现：无 metric_engine / metric_registry | 无相关代码 |
| §8 三类筛选器 | 未实现：无 filter_engine / review_filters.yaml | 无相关代码 |
| §9 板块归因 | 未实现：无 attribution_engine | 无相关代码 |
| §10 信号生命周期与追踪 | 未实现：无 tracking_state_machine | 无相关代码 |
| §11 任务编排与发布 | 未实现：review_orchestrator 不存在 | `after_close_orchestrator` 不包含 review 步骤 |
| §12 API 合同 | 未实现：无 review.py / admin_review.py | 无 /api/v1/review 路由 |
| §13 前端目录与组件 | 未实现：无 `frontend/src/features/review/` | 无 ReviewPage.tsx |
| §14 页面信息架构 | 未实现 | 无 review 前端 |
| §15 前端数据与状态规则 | 未实现 | 无 review 前端 |
| §16 与现有页面边界 | 未实现：/market 与 /stock 尚未接收 review 跳转参数 | 无跳转合同代码 |
| §17 加载/空态/异常态 | 未实现 | 无 review 前端 |
| §18 性能与缓存 | 未实现 | 无 review 缓存逻辑 |
| §19 测试要求 | 未实现 | 无 review 测试 |
| §20 验收标准 | 未实现 | 全部待开发 |
| §21 文档与记忆系统 | 已完成（本轮）：7 个文档已更新 | 本文件 + prd/70-review.md + 其他 5 个文档 |
| §22 推荐实施顺序 | 计划中 | Phase 0 输入门禁部分就绪（Board V1），Phase 1-5 待开发 |

## 2. 当前实现摘要

复盘模块尚未开始开发。当前系统仅存在复盘的**输入基线**：

- **Board V1（板块分析）**：作为复盘阶段三"板块归因"的输入来源之一，已实现完整链路：
  - `board_analysis_snapshots` 表（migration `074_board_analysis_v1`）
  - `backend/app/services/board_analysis_service.py`（compute_board_analysis / compute_all_boards / publish_board_analysis）
  - `backend/app/api/board_analysis.py`（用户路由 + 管理路由）
  - `frontend/src/pages/BoardAnalysisPage.tsx`（列表 + 详情页）
  - 复用 `factor_publications` 表发布指针（`publication_kind=market_aggregation`、`scope_type=board`）
  - 算法版本 `board-v1-20260730`，coverage 门禁 0.95

- **stock_core pointer**：复盘的另一个输入，已通过 `factor_publications`（`publication_kind=stock_core`）发布。

**不存在的内容**（不得描述为已实现）：
- 无 `market_review_runs` / `market_review_run_items` / `market_review_scope_snapshots` / `market_review_signals` / `market_review_signal_attributions` / `market_review_signal_instruments` / `market_review_trackings` / `market_review_tracking_evaluations` 表
- 无 `/review` 前端路由与 `ReviewPage.tsx`
- 无 `backend/app/domain/review/` 目录
- 无 `review_orchestrator` / `review_scope_service` 等服务
- 无 `/api/v1/review` API 路由
- 无 P/Q/U/C/V 指标引擎
- 无 A/B/C 三类筛选器
- 无信号生命周期与追踪状态机

## 3. 当前入口

| 类型 | 路径/符号 | 状态 | 说明 |
|---|---|---|---|
| 数据表 | `board_analysis_snapshots` | 已实现 | Board V1，复盘阶段三输入基线 |
| 数据表 | `factor_publications`（kind=stock_core / market_aggregation） | 已实现 | 复盘输入指针来源 |
| 前端路由 | `/boards` + `/boards/:boardId` | 已实现 | `BoardAnalysisPage.tsx`，非复盘页 |
| API | `GET /api/v1/boards/analysis` | 已实现 | Board V1 列表 |
| API | `GET /api/v1/boards/{board_id}/analysis` | 已实现 | Board V1 详情 |
| API | `POST /api/v1/admin/boards/{board_id}/analysis/compute` | 已实现 | Board V1 单板块触发 |
| API | `POST /api/v1/admin/boards/analysis/compute-all` | 已实现 | Board V1 批量触发 |
| CLI | `backend/scripts/board_analysis_cli.py` | 已实现 | Board V1 计算 CLI |
| 服务 | `board_analysis_service.py` | 已实现 | Board V1 计算与发布 |

## 4. 计划实现入口

> 以下为 PRD §4 / §5 / §12 / §13 定义的计划入口，**尚未实现**。完整合同见 `../prd/70-review.md`。

### 4.1 后端模块结构（PRD §4）

```
backend/app/domain/review/
  metric_registry.py          # ReviewMetricComponentRegistry，字段映射
  metric_engine.py            # P/Q/U/C/V 计算
  filter_definitions.py       # A/B/C 筛选器定义（Pydantic schema）
  filter_engine.py            # 筛选器执行
  attribution_engine.py       # 子范围与个股归因
  tracking_state_machine.py   # 信号生命周期与追踪状态机

backend/app/services/
  review_orchestrator.py      # 盘后 review 编排
  review_scope_service.py     # 范围扫描与 scope snapshot
  review_signal_service.py    # 信号生成与生命周期
  review_attribution_service.py
  review_tracking_service.py
  review_publication_service.py

backend/app/api/
  review.py                   # 用户端 API
  admin_review.py             # 管理端 API

backend/app/schemas/
  review.py                   # Pydantic schemas

backend/scripts/
  review_compute_cli.py       # review 计算 CLI
```

### 4.2 前端目录（PRD §13）

```
frontend/src/features/review/
  api.ts / types.ts / queryKeys.ts / urlState.ts
  ReviewHeader.tsx / ReviewStageNav.tsx
  MarketScanPanel.tsx / FilterDiscoveryPanel.tsx
  BoardAttributionPanel.tsx / StockValidationPanel.tsx
  TrackingReviewPanel.tsx / EvidenceDrawer.tsx
  ScopeMetricsTable.tsx / SignalCard.tsx
  AttributionTable.tsx / ReviewInstrumentTable.tsx
  ReviewDataQualityBadge.tsx / review.module.scss

frontend/src/pages/ReviewPage.tsx
```

## 5. 数据模型合同

> 完整 schema 见 PRD §5。建议迁移文件名 `075_market_review_workbench.py`（不得修改已应用的 074）。

| 表 | 关键字段 | 唯一约束 | 职责 |
|---|---|---|---|
| `market_review_runs` | trade_date, source_core_run_id, source_board_run_id, algorithm_version, filter_version, status, coverage_ratio | trade_date + source_core_run_id + source_board_run_id + algorithm_version + filter_version | 某交易日完整复盘版本 |
| `market_review_run_items` | review_run_id, scope_type, scope_key, phase(metrics/signals/attribution/tracking), status, input_hash, lease_epoch | review_run_id + scope_type + scope_key + phase | 按范围×阶段检查点 |
| `market_review_scope_snapshots` | review_run_id, scope_type, scope_key, p/q/u/c/v_payload, coverage_ratio | review_run_id + scope_type + scope_key | 每个范围的 P/Q/U/C/V 与证据 |
| `market_review_signals` | review_run_id, filter_family(A/B/C), signal_type, scope_type, scope_key, status, first_seen_date, previous_signal_id, rank_key | review_run_id + filter_family + signal_type + scope_type + scope_key | 三类筛选器命中结果 |
| `market_review_signal_attributions` | signal_id, child_scope_type, child_scope_key, contribution_value, contribution_rank | - | 第二级范围下钻 |
| `market_review_signal_instruments` | signal_id, instrument_id, board_role, relation_to_scope, contribution_value | - | 代表股票与贡献；board_role/relation_to_scope 有枚举约束 |
| `market_review_trackings` | user_id, source_signal_id, tracking_type(signal/scope/instrument), status(active/confirmed/invalidated/closed) | - | 用户追踪 |
| `market_review_tracking_evaluations` | tracking_id, review_run_id, trade_date, previous_state, current_state | tracking_id + trade_date | 逐日追踪结果 |

枚举约束：
- `board_role`: core / second_line / elasticity / follower / laggard / unclassified
- `relation_to_scope`: synchronized_strengthening / synchronized_weakening / instrument_leads_scope / scope_strong_instrument_lags / instrument_strong_scope_unsupported / unconfirmed
- `market_review_runs.status`: created / computing / partial / signals_ready / published / completed_with_errors / failed / cancelled

## 6. API 合同摘要

> 完整合同见 PRD §12。统一前缀 `/api/v1/review`。

### 6.1 用户端

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

### 6.2 管理端

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/admin/review/runs` | 创建 review run（canary/全量） |
| POST | `/api/v1/admin/review/runs/{id}/resume` | 恢复未完成 run（仅 pending/可重试 failed/过期 running） |
| POST | `/api/v1/admin/review/runs/{id}/publish` | 发布 pointer（原子切换） |
| GET | `/api/v1/admin/review/runs/{id}/status` | 查询 run 状态 |

所有写操作要求幂等键。

## 7. 实施顺序

> 完整说明见 PRD §22。

| Phase | 内容 | 依赖 |
|---|---|---|
| Phase 0 | 输入门禁：第一金字塔、板块分析、行情完整性、发布 pointer | **部分就绪**（Board V1 + stock_core pointer 已实现） |
| Phase 1 | Review 后端骨架：迁移、模型、scope snapshot、P/Q/U/C/V、run/item、API overview/scopes | Phase 0 |
| Phase 2 | 筛选器与归因：A/B/C 筛选器、signals、attributions、instrument mapping、发布门禁 | Phase 1 |
| Phase 3 | 五阶段前端：ReviewPage、URL 状态、市场扫描、筛选发现、板块归因、个股验证 | Phase 2 |
| Phase 4 | 追踪闭环：tracking、daily evaluation、过去发现、自选映射、事件演化 | Phase 3 |
| Phase 5 | 历史回放与阈值校准：验证筛选器稳定性；阈值变化升级 filter_version | Phase 4 |

## 8. 已知边界

Board V1 **不是**完整复盘，明确边界：

- Board V1 **不是** P/Q/U/C/V 聚合变量（那是 review metric_engine 的职责）；
- Board V1 **不是** A/B/C 三类偏差筛选器（那是 review filter_engine 的职责）；
- Board V1 **不是**信号生命周期状态机（那是 review tracking_state_machine 的职责）；
- Board V1 **不是**两级范围扫描（review 第一级扫描 market/major_index/style/industry_l1，第二级下钻 industry_l2/l3/concept/instrument）；
- Board V1 是复盘阶段三"板块归因"的**输入基线**之一，提供趋势/结构/动量/量能/事件分布；
- `BoardAnalysisPage.tsx` 保留为板块原始分析入口，Review 阶段三复用其可抽取组件（BoardMetricsSummary / BoardDistributionPanel / BoardEventDistribution），不复制业务逻辑；
- 复盘页不得重新实现 99 字段列设置和导出。

## 9. 更新触发条件

复盘模块任何开发开始前先确认 PRD；开发完成且通过验证后再更新本 Map 的实现状态。
当 review 表、API、前端组件、编排链路发生变化时必须更新本 Map。
