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
| ALIGN-034 | admin monitor 资格修复待生产验证 | 代码已实现 `filter_monitor_eligible_recipients`/`is_user_eligible_for_monitor`，monitor_batch/event_recipient/outbox_relay 三处已切到监控资格过滤；测试覆盖 active admin、active member+subscription、disabled admin、无订阅普通用户。生产环境尚未重新 build/restart 验证真实监控 universe 与投递链路。 | 部署后检查 monitor_batch universe 包含 admin 自选股，monitor 日志无 admin 被过滤，outbox/delivery 为 admin 生成 MessageDelivery | P1 |
| ALIGN-035 | quote 可信化与 pytdx 连接保护待生产验证 | 代码已实现 QuoteResponse 可信字段、午休统一口径、Redis 短缓存、pytdx 单例+线程锁；测试与本地 ASGI 验证通过。生产环境尚未验证真实 pytdx 连接在交易时段的成功/fallback 行为、断线重连、以及容器日志的可观测性。 | 部署后在交易时段 curl /quote，确认 pytdx 成功/降级字段正确，日志可见区分日志，页面状态徽章非固定“实时行情” | P1 |
| ALIGN-036 | delivery_worker monitor 资格修复待生产验证 | 代码已实现 `MONITOR_SOURCE_TYPES`（`app/constants/monitor_source_types.py` 单点真源）与 `is_user_eligible_for_monitor` 在 `delivery_worker.py` 投递前复核；`outbox_relay.py` 与 `delivery_worker.py` 共享同一 source_type 集合；测试覆盖 active admin/active member/disabled admin/plain user。生产环境尚未验证真实 monitor_event 能生成 MessageDelivery 并实际投递。 | 部署后检查 monitor_event 来源的 MessageDelivery 为 active admin 与 active member 生成，disabled admin/plain user 被标记 dead/USER_INELIGIBLE | P1 |
| ALIGN-037 | 1d partial daily bar 与 live 1m monitor 待生产验证 | 代码已实现 `MarketDataAggregationService` 交易时段合成 partial daily bar、`monitor_batch_service` 使用 live 1m 输入；测试覆盖交易/非交易场景。生产诊断已确认 2026-07-07 盘中 `MarketDataAggregationService` 构造的 `live_start` 为 naive datetime、`live_end` 为 aware Asia/Shanghai datetime，传入 `pytdx_adapter.get_minute_bars` 后触发 `can't subtract offset-naive and offset-aware datetimes`，导致 worker-monitor 全天无法获得 1m 数据、无 monitor_evaluations/strategy_events/通知。PR #35 修复后部署验证，发现 `pytdx_adapter.get_minute_bars` 内部将拉取到的 1m 数据 `datetime` 列显式 `tz_localize(None)`，但 aware 输入未同步转为 naive，导致比较时抛出 `Invalid comparison between dtype=datetime64[us] 和 Timestamp`。新增 `fix/pytdx-adapter-aware-minute-comparison` 修复并重新部署验证。 | 部署后交易时段 curl `/instruments/{id}/bars?timeframe=1d`，确认最后一根为当日 partial bar；查看 monitor worker 日志包含 `minute_last_bar_time` 与 `minute_is_partial`，且无 offset-naive/offset-aware 或 datetime64/Timestamp 比较异常 | P1 |
| ALIGN-040 | 统一行情工作区代码未部署/生产未验收 | release/unified-market-workspace-v1 已完成：`/market` 三栏工作区（MarketInstrumentPane + StockResearchWorkspace + ResearchContextPanel）、`/admin/stock-debug` 独立管理员调试路由、`buildStructureSummary`/`buildUserEventExplanation` 纯函数、`normalizeInternalReturnTo` returnTo 安全校验、debug 从 `/market` URL 契约移除。前端 node tests/tsc/eslint/vite build 全通过，CDP E2E 验证 URL 状态/请求门控/文案。但代码尚未合并到 main、未部署生产。`/stock/:symbol` 已复用共享 `StockResearchWorkspace`，但保留独立 header（tv-symbol-bar + 详情页 actions：自选/上下切换/memo/飞书/全屏），与 `/market` 三栏布局视觉不统一——此为有意保留的兼容适配器，详情页专属 actions 不适合迁移到 `/market`。 | release PR 合并并部署后：1) 生产 `/market` 验证三栏布局 + ResearchContextPanel 只渲染 4 张用户卡；2) `/admin/stock-debug/:symbol` 验证管理员可见原始因子/JSON、普通用户 403；3) `/market?debug=1` 管理员重定向、普通用户清除；4) returnTo 外部 URL 被拒绝；5) `/stock/:symbol` 仍可用且复用共享图表区 | P1 |

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
| ALIGN-038 | monitor 图片链路 capture token claims 修复后生产验证通过。PR #43 (commit 79d96e0) 补全 `scope/user_id/instrument_id`，部署后构造 `[SMOKE_IMAGE]` 单标的事件：capture_jobs.status=succeeded、image_url 非空；Outbox delivery_type=image 已 processed；message_deliveries delivery_type=image status=success、image_upload_status=success、image_key 非空；delivery_worker 日志确认“飞书图片消息投递成功”。 |
| ALIGN-039 | capture worker 偶发 502 因 `page.goto` networkidle 超时修复后生产验证通过。PR #45 (commit 8c1f9c4) 将 `wait_until` 从 `"networkidle"` 改为 `"load"`，保留 `data-render-ready="true"` 等待 bars+indicators。部署后通过 `notification_service.test_channel_latest_event` 触发单条图片链路：capture_jobs.status=succeeded 且 image_url 非空；Outbox delivery_type=image 已 processed；message_deliveries delivery_type=image status=success、image_upload_status=success、image_key 非空；delivery_worker 日志确认“飞书图片消息投递成功”。 |

## 关闭要求

每项关闭必须有：代码 commit、测试、CI 或生产验证证据、CHANGE 记录。关闭后只保留摘要，详细历史归入 CHANGE。
