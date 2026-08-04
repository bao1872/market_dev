# 跨系统统一实现 Map

核验状态：部分核验（2026-08-04 阶段1 收口：统一错误合同 + 状态语义 + 正式结果指针）
最后核验日期：2026-08-04
最后核验提交：142115b（阶段1 收口基线）
核验范围：以下 SW 条目基于阶段1 盘后编排 + 管理后台 + 权限链的真实代码与测试核验；其余条目仍待跨模块审计
对应 PRD：`../prd/90-system-wide-requirements.md`
事实所有权：统一时间、标识、状态、来源、版本和正式结果语义的实际实现

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. PRD 实现映射

| PRD 条款 | 权威实现 | 状态 | 验证证据 |
|---|---|---|---|
| SW-20 | 状态语义由各领域枚举统一：`SchedulerJobRun.status`（queued/running/succeeded/failed/skipped/interrupted/resume_queued/cancelled）、`StrategyRun.status`（queued/running/completed/partial_failed/published/failed）、`AfterCloseRunStatus`（7 步编排枚举） | 已核验 | `docs/maps/30-after-close.md` §5 状态机；`after_close_orchestrator.py:683 AfterCloseRunStatus` |
| SW-21 | 失败不伪装成功：执行器 result 与 step_summary 分离，业务软失败必须显式翻译为 step failed；终态短路保留 cancelled/interrupted | 已核验 | `test_after_close_phase0_contracts.py::test_board_business_failure_must_not_report_step_succeeded` / `::test_cancelled_error_not_overwritten_as_failed` |
| SW-30 | 来源可追踪：`factor_publications`（scope_type/scope_key/trade_date/publication_kind/data_run_id）记录业务产物来源 pointer | 已核验 | `factor_publication_service`；`docs/maps/30-after-close.md` §6 |
| SW-31 | 运行版本可追踪：`SchedulerJobRun.run_key`/`attempt_no`/`lease_epoch` + `GIT_SHA`/`image_git_sha`/`runtime_git_sha` 部署标识 | 部分核验 | `after_close_orchestrator.py` 领取/重试逻辑；部署脚本 version 标识 |
| SW-40 | 单一正式结果：读取端统一按 `published_at IS NOT NULL` / 已发布 pointer 过滤，`get_published_snapshot_run_id` 统一消费 | 已核验 | `stock_context.py`；`factor_publication_service.get_published_snapshot_run_id` |
| SW-50 | 关键失败可见：status API 暴露 `step_summary`/`running_steps`/`step_timed_out`/`stale`/`partial_success`/`skip_reason` watchdog 字段 | 已核验 | `test_after_close_phase0_contracts.py::test_status_response_*` |
| SW-51 | 不虚构完成：pipeline overall_status 如实暴露 partial_success/cancelled/interrupted（不落 else→not_started） | 已核验 | `test_after_close_phase0_contracts.py::test_pipeline_overall_status_exposes_terminal_states` |
| SW-01 | 时区配置：交易/盘点逻辑统一 `Asia/Shanghai`；`bars_scheduler` 16:00 上海时区 | 部分核验 | `app/core/time.py shanghai_business_date`；运行时区待跨模块审计 |
| SW-02 | 时间字段语义 | 未核验 | — |
| SW-10 | symbol/trade_date/run_id 稳定标识 | 部分核验 | `normalizeAShareSymbol` 统一；run_id 合同见 `docs/maps/30-after-close.md` §11.2 |
| SW-11 | 主键和关联 | 未核验 | — |
| SW-41 | 调试与正式结果分离 | 未核验 | — |

**管理 API 统一错误合同（2026-08-04 新增）**：`backend/app/api/admin_errors.py` 的 `admin_error` 是管理后台错误响应的唯一事实源，稳定字段 `detail/message/error_code/severity/retryable/resumable/recommended_action/request_id`，业务上下文经 `**extra` 透传。`admin_after_close.py` 全部错误分支已改用该构造器（PA-31 模块边界落地）。证据：`test_admin_errors.py`（8 项纯单元，含端点不再手工构造的源码守卫）。

## 2. 统一时间

| 语义 | 字段/类型 | 生成位置 | 消费位置 |
|---|---|---|---|
| 交易日 | 待核验 | 待核验 | 待核验 |
| 行情时间 | 待核验 | 待核验 | 待核验 |
| 计算时间 | 待核验 | 待核验 | 待核验 |
| 发布时间 | 待核验 | 待核验 | 待核验 |
| UI 显示时间 | 待核验 | 待核验 | 待核验 |

## 3. 稳定标识

| 标识 | 权威定义 | 存储 | API | 前端 |
|---|---|---|---|---|
| symbol | 待核验 | 待核验 | 待核验 | 待核验 |
| trade_date | 待核验 | 待核验 | 待核验 | 待核验 |
| run_id | 待核验 | 待核验 | 待核验 | 待核验 |
| published_run_id | 待核验 | 待核验 | 待核验 | 待核验 |
| user_id | 待核验 | 待核验 | 待核验 | 待核验 |
| event_id | 待核验 | 待核验 | 待核验 | 待核验 |

## 4. 状态语义

| 业务语义 | 后端 | 数据库 | API | 前端 |
|---|---|---|---|---|
| 未加载 | - | - | - | 待核验 |
| 无数据 | 待核验 | 待核验 | 待核验 | 待核验 |
| 不可用 | 待核验 | 待核验 | 待核验 | 待核验 |
| 失败 | 待核验 | 待核验 | 待核验 | 待核验 |
| 部分完成 | 待核验 | 待核验 | 待核验 | 待核验 |
| 已完成 | 待核验 | 待核验 | 待核验 | 待核验 |
| 已发布 | 待核验 | 待核验 | 待核验 | 待核验 |
| stale | 待核验 | 待核验 | 待核验 | 待核验 |

## 5. 正式结果

待核验唯一正式读取链：

```text
published_run_id
→ 正式结果查询
→ API
→ 前端
```

检查调试页面、后台接口和普通 API 是否使用同一正式语义。

## 6. 已知偏差

待跨模块审计后填写。任何同一状态多套定义都应在此索引，并链接到具体领域 Map。
