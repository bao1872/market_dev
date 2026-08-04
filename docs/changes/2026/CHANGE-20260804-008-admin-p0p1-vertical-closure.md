# CHANGE-20260804-008：管理后台 P0+P1 垂直闭环收口

- 日期：2026-08-04
- 类型：behavior + contract + architecture
- 领域：管理后台信息架构 / 系统概览统一摘要 / 数据生产中心 / 统一错误模型
- 关联 PRD：`ref/后台管理.md`（管理后台优化 PRD，§6 信息架构、§8.1/8.2 统一生产与发布、§8.4.9 统一错误模型）
- 关联 Maps：`docs/maps/80-system-runtime.md`
- 关联前序提交：`33dc56e`(P0 信息架构)、`ddff3c1`(P1 摘要层)、`f8cf56d`(R14 错误模型)

## 1. 背景

上一轮完成管理后台 P0 信息架构收口 + P1 统一摘要层 + R14 错误模型雏形并推送 dev。
独立垂直切片审查指出多处未闭环：概览字段回归测试缺失、quality_gate 三态语义错误、
数据生产中心仍为"P1 后续提供"占位、R14 helper 未真正统一消费、错误码无双字段兼容、
任务中心标题未改、用户 Tab 由 URL 与 state 双控制。本轮逐项收口。

`data_closed=false`，不连库、不部署、不 apply Migration，不做真实数据验证。

## 2. 修改内容

### 2.1 系统概览合同收口（PRD §8.1/8.2）

- `test_response_has_18_fields`：回归测试从"17 字段"修正为"18 字段"（含 `summary`），
  并断言 `summary` 核心键完整。
- `quality_gate_passed` 三态语义：passed→True；failed→False；
  `pending`/`not_applicable`→`None`（未触发不代表未通过）。
- Pydantic 嵌套默认值改为 `Field(default_factory=...)`（避免可变默认实例）。
- `overall_status` 判定规则文档化为后端唯一权威：error→blocked；warning→attention；无→ok。
- `TodayIssue` 新增 `error_code`（稳定机器码）与 `target_route`（前端跳转目标），
  issues 均带 `overview_*` 稳定错误码。

### 2.2 数据生产中心真正接入（PRD §8.2）

- `ProductionChainNode` 扩展字段：`run_id / quality_gate / publication_status /
  blocking_reason / recommended_action`。
- 新增 `_compute_product_nodes(db, business_date)`：从 6 张数据产品表
  （`bars_daily` / `first_pyramid_history_runs` / `board_analysis_snapshots` /
  `market_review_runs` / `auction_anchor_snapshots` / `StrategyRun(published)`）
  查询完整 6 节点状态，覆盖行情 / 第一金字塔 / 板块分析 / 复盘 / 竞价准备 / 正式发布，
  替代原纯派生的 3 节点（行情/选股/发布）。
- 前端数据生产中心：总览 tab 渲染 6 节点（detail/blocking_reason/recommended_action），
  业务产品 tab 展示聚合读模型的筛选视图，移除过期的"P1 后续提供"占位。

### 2.3 R14 统一错误模型闭环（PRD §8.4.9）

- `admin_error` 新增 `legacy_error_code` 参数，响应双字段兼容：
  `stable_error_code`（权威统一码，如 `after_close_conflict`）+
  `error_code`/`reason`（保留历史码，如 `DUPLICATE_RUN`），保证旧前端不回归。
- **`admin_after_close.py` 全部 13 处手工 `HTTPException` 改用统一构造器**，
  覆盖所有盘后编排端点与错误场景：
  - 创建：`after_close_invalid_trade_date`(422) / `after_close_non_trading_day`(409) /
    `after_close_conflict`(409，legacy `DUPLICATE_RUN`，透传 `after_close_run_id`)；
  - force：`after_close_bad_request` / `after_close_run_not_found` /
    `after_close_coverage_insufficient`(409，legacy `DATA_COVERAGE_INSUFFICIENT`)；
  - retry：`after_close_run_not_found` / `after_close_not_retryable`；
  - resume：`after_close_run_not_found` / `after_close_wrong_job_type` /
    `after_close_missing_trade_date` / `after_close_not_resumable` / 冲突；
  - cancel / reconcile：`after_close_run_not_found` + **透传 `request_id`**；
  - status / list_events：`after_close_run_not_found`。
- 前端新增 `utils/adminErrors.ts`：`parseAdminApiError` / `formatAdminApiError`
  消费 `stable_error_code`/`recommended_action`/`retryable`/`resumable`，
  并透出 legacy `error_code`/`reason` 兼容。
- `AfterClosePipelineCard.formatAfterCloseCreate409Message` 优先消费统一错误字段。
- `test_admin_errors.py` 新增源码守卫：断言 `admin_after_close.py` 使用全部 4 个 helper、
  不再手工 `raise HTTPException`（防止回归）。

### 2.4 P0 遗留收口

- 任务中心：`AdminJobsPage` 页面标题"任务与事件"→"任务中心"。
- 用户与权限：Tab 以 URL query 为唯一真源（`useEffect` 同步 URL→state，
  前进/后退/外部修改均可恢复；每个 tab 有明确 URL 表示）。

## 3. 完成状态（如实区分）

| 范围 | 状态 |
|---|---|
| P0 信息架构收口 | 完成 |
| P1 统一生产与发布总览（6 节点状态直出） | 完成到"总览 + 各业务 Tab 筛选视图"层级 |
| 各业务详情 Tab（第一金字塔/板块/复盘/竞价 的深度详情） | 仍待后续（本轮仅聚合筛选视图） |
| R14 统一错误模型（helper + 全部盘后端点接入 + 前端消费） | 完成 |
| 发布预检 preflight + 撤回确认（R13） | 未开始 |
| 全局搜索 / 字段链路追踪 / 数据质量中心 / 权限诊断 / 版本一致性（P2） | 未开始 |
| 部署 | 未执行 |
| 真实数据闭环（`data_closed`） | 未执行（保持 false） |

## 4. 验证

- 前端：`tsc --noEmit` 通过；eslint 0 error；node 测试套件 84 passed
  （导航/路由 29 + adminErrors 4 + 数据生产中心 5 + 用户 Tab 4 + 任务中心标题 2 + 既有 40）。
- 后端纯单元（`PURE_UNIT_TEST=1`，无需 DB）：
  - `test_admin_errors.py`：8 passed（含源码守卫：端点统一用 helper、不手工 raise HTTPException）；
  - `test_system_overview_service.py`：15 passed；
  - 本轮改动 6 文件 Ruff 全部通过；`git diff --check` 通过。
- 需 DB 连接测试（`postgres` 依赖 60+43 项，如 `test_response_has_18_fields`、
  `test_after_close_endpoints` 等）在共享开发库 `bz_stock` 下运行。因 AGENTS.md 安全边界
  未授权 `PANJI_SHARED_DEV_DB_TEST` 连接，本轮跳过；不影响代码正确性判断，但真实端到端
  行为验证留待后续授权后进行。
- 文档一致性：`tools/check_docs_consistency.py` 全部通过（105 文档链接、占位符、CHANGE 引用）。
