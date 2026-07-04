# Code / Docs / Production Alignment

> 本文件只记录“当前确认设计已经明确，但实现、测试、部署或生产表现尚未一致”的问题。历史经过进入 `changes/`。

## 当前 KNOWN_GAP

| ID | 领域 | 当前证据 | 目标 | 优先级 |
|---|---|---|---|---|
| ALIGN-010 | 飞书图文 E2E | 生产已有图文成功记录，但 partial_failed、仅重试图片、失败状态生产 E2E 尚未系统验证 | 独立 card/image 状态、partial_failed、仅重试图片、真实 E2E | P1 |
| ALIGN-012 | 管理页面 E2E | AdminJobsPage 与部分管理 API 已存在，Worker 心跳可观察性已补齐（`GET /admin/worker-heartbeats` + 前端 Tab + 测试）；用户启停、订阅变更、任务与审计生产操作未完整验收 | 所有管理按钮真实 API、审计完整、生产 E2E 通过 | P1 |
| ALIGN-015 | 服务健康与业务能力 | CORE_ONLY 不包含 capture/outbox/delivery；服务不全会造成业务部分可用 | 部署能力与业务功能匹配；服务不可用时不假成功 | P1 |
| ALIGN-021 | Ruff/Mypy 历史债务 | 全仓 Ruff/Mypy Full Report 仍有历史债务（baseline 930/242 → 当前 861/227），非阻断展示。2026-07-04 已完成 P0/P1/P2 分级审计：见 `docs/architecture-audits/AUDIT-20260704-ruff-mypy-debt-triage.md`，P0=0，P1=6 项（after_close_orchestrator/worker None 处理、bars_metrics 私有属性、chart_bars 死代码、strategy_run import、metrics 版本兼容），P2=849/189 项保留本条 | 独立债务分支清零，再改为完全阻断 | P2 |
| ALIGN-025 | `_notify_monitor_status` 绕过 Outbox | `worker.py:1087-1191` 直接调用 `adapter.send()` 绕过 Outbox/Delivery Worker，缺少重试/幂等/静默时段规避/可查询状态；代码 TODO 已标记，待产品决策（降级路径 vs 一致性） | 待产品决策后确定目标状态 | P2 |
| ALIGN-030 | 部分标的历史 bars 覆盖不足 | 生产 DB 中 `000001` 仅 5 根日线、66 根 15m（约 6 个交易日），导致个股详情 K 线显示最近几天；`600519`/`300750` 日线 846 根、15m 约 8000 根数据充足。2026-07-04 已对 `000100`（TCL科技）执行单标回补：日线 846 根、15m 8000 根、60m 2000 根，API `page_size=4000` 可正常返回 4000 根。全市场仍有约 81 只 active 标的日线 < 50 根，需后续统一回补决策。2026-07-04 全市场约 187 只 active 标的日线 < 60 根（BSE_920 97 只、主板 85 只、其他 5 只），已通过 _DSA_MIN_HISTORY_BARS=60 前置分类为 skipped/insufficient_history | 完成全市场历史行情回补，或明确排除/标记该类标的；所有页面显示标的需满足 Node Cluster 最小输入 | P1 |
| ALIGN-031 | DSA-only 大量 failed | 1881 只 failed 全部 reason_code=timeout，根因为 run 级总超时 600s 与编排层 7200s 冲突；historical bars 不足标的未前置分类；execute_run 覆盖 skipped_count | 修复后需生产验证 failed_count 大幅下降 | P1 |
| ALIGN-033 | `strategy_run_items.result_id` 未回填 | PR #14 batch service 写入 `strategy_results` 后未更新 `strategy_run_items.result_id`（始终为 None）。PR #15 的 `query_run_items_with_results` 已改用 `(run_id, instrument_id)` 关联 `strategy_results` 绕过此问题。但 `result_id` 字段仍为 None，需后续修复 batch service 在写入 results 后回填 `result_id` | 修复 `strategy_batch_service._write_results_to_db` 写入 results 后回填 `result_id` | P2 |

## CLOSED 摘要

| ID | 摘要 |
|---|---|
| ALIGN-004/005/019/020 | DSA 发布门禁、预算、partial_failed 发布、数量语义已收口 |
| ALIGN-006/007/008 | Watchlist、趋势 API、Worker 资格已接入统一权限/资格路径 |
| ALIGN-009 | 行情聚合与尾部补齐相关路径已收口 |
| ALIGN-011/018 | Capture Token 与 Capture Snapshot 链路已实现并测试 |
| ALIGN-016 | Node Cluster 15m 输入已修正为 4000 |
| ALIGN-017 | 飞书 Platform App only，Webhook 永久删除 |
| ALIGN-022 | `target_channel_id` 手动通知跳过资格过滤，自动通知仍过滤，已补隔离测试 |
| ALIGN-023 | worker-watchdog 生产服务已部署(67105c2)，38 条 stale running 已自动清理为 stopped，stale running=0 |
| ALIGN-024 | docs v2 结构已通过 PR #5 合并落库（cafbdc4），旧 00-18 归档，check 已适配 |
| ALIGN-032 | 趋势选股页全量 universe 展示已生产验证通过。PR #15 部署后 run_id=f0c15e1c: source_total=5293, filtered_total=5293, succeeded 行正确显示 35 个 DSA 指标，skipped 行显示股票但指标为空。修复 commit: cc025e0 (JOIN by run_id+instrument_id) |

## 关闭要求

每项关闭必须有：代码 commit、测试、CI 或生产验证证据、CHANGE 记录。关闭后只保留摘要，详细历史归入 CHANGE。
