> 文档状态：CURRENT DESIGN BASELINE  
> 设计基线日期：2026-07-03  
> 设计确认截止日期：2026-07-03  
> 实现核对基线：ddca659b8c9d64b6a414da0b4bbd6f80f704aef1  
> 实现核对分支：main  
> 最近一致性检查日期：2026-07-03  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 11 后台任务与第三方集成

## 1. Worker

统一入口：`backend/app/worker.py`。服务名以 `docker-compose.prod.yml` 为准。

| WORKER_TYPE | 职责 |
|---|---|
| `bars_scheduler` | 更新行情、聚合和触发盘后链路 |
| `strategy_scheduler` | DSA 兜底调度，不重复已有运行 |
| `calendar_scheduler` | 更新交易日历 |
| `monitor_scheduler` | 对有效会员自选股进行盘中监控 |
| `strategy_batch` | 领取并执行 StrategyRun |
| `outbox` | 扩张消息和投递 |
| `delivery` | 实际渠道投递、重试和最终状态 |
| `after_close_orchestrator` | 领取并执行盘后编排任务 |
| capture service | 生成个股详情图片 |

## 2. 调度语义

- 日历刷新：约 02:00 Asia/Shanghai；
- 盘后行情：交易日约 16:00；
- DSA 兜底：交易日约 18:30；
- 盘中监控：09:30–11:30、13:00–15:00，按配置轮询；
- Outbox/Delivery：短轮询；
- 心跳：按配置持续更新。

精确 Cron 和间隔由代码/环境变量作为机器事实源。

## 3. 任务状态与恢复

重要任务保存 run_key、business_date、scheduled/started/finished、status、heartbeat、lease、instance、计数、错误和 Git SHA。重复触发复用或返回 duplicate；stale 任务按服务规则恢复，禁止直接手改数据库状态。

### 3.1 Worker 心跳与僵尸清理

`worker_heartbeats` 表记录每个 Worker 实例的运行状态。`_heartbeat_loop` 启动时 INSERT（status=running），运行中每 60s UPDATE heartbeat_at，正常 SIGTERM 退出时 UPDATE status=stopped。

容器被 SIGKILL（无 SIGTERM）或进程崩溃时，`_heartbeat_loop` 无法执行退出清理，会残留 status=running 的僵尸记录，导致管理员面板的 Worker 健康状态不可信。

僵尸心跳由 `_recovery_watchdog_loop` 统一清理：

- 函数：`app.worker.mark_stale_worker_heartbeats(db, now=None, threshold_seconds=600)`
- 阈值：`STALE_HEARTBEAT_THRESHOLD_SECONDS=600`（10 个心跳周期，远大于正常抖动），通过环境变量可调整
- 行为：原子 UPDATE `worker_heartbeats SET status='stopped' WHERE status='running' AND heartbeat_at < now - threshold`；不删除记录，保留 started_at/heartbeat_at/build_sha 供审计；不 commit（由调用方控制事务，与 `recover_stale_scheduler_job_runs` 模式一致）
- 幂等：status 已是 stopped 的记录不会被重复处理
- 触发：`_recovery_watchdog_loop` 每 60s 调用一次，与 `recover_stale_scheduler_job_runs` 同事务执行后 commit
- 异常处理：数据库异常向上传播，由 watchdog 外层捕获并记录日志，下个周期继续重试

`stopped` 是 `WorkerHeartbeat.status` 的合法值（`String(32)`，注释 `running/idle/stopped`），不涉及数据库 migration。

## 4. 行情集成

Pytdx 提供行情，Mootdx 提供交易日历。统一行情聚合服务 `market_data_aggregation_service.py` 是唯一事实源，负责历史完成 Bar、尾部补齐和盘中 partial Bar；外部源失败时记录降级。交易日不能通过“是否有 K 线”推断。

当前消费者：
- `/instruments/{id}/bars` 行情 API；
- `indicator_service` 日线与指标计算；
- `monitor_snapshot_service` 盘中监控快照；
- `stock_detail_feishu_service` 截图与飞书个股详情；
- DSA `factor_per_bar` 与 `last_row_metrics` 计算。

所有消费者使用同一聚合语义，不得各自形成第二套行情路径。

## 5. DSA 运行链

`bars_scheduler` 只负责准备行情和创建/复用 queued run；`strategy_batch` 执行计算；`strategy_scheduler` 兜底；完整性门禁通过后发布。缺少任一 Worker 都不能认为盘后链路正常。

## 6. 飞书

用户渠道唯一接入方式为 `feishu_platform_app`（Platform App），每个用户最多一个 active 渠道。`feishu_webhook` 已永久删除，禁止恢复（`feishu_webhook_adapter.py` 已删除，migration 055 添加 CHECK 约束禁止 `adapter_type='feishu_webhook'`）。管理员内测申请通知复用管理员用户自己在 /settings 配置的 `feishu_platform_app` NotificationChannel，系统不存在独立管理员飞书凭证或 Webhook 配置；管理员身份由 users/roles/user_roles 决定，不要求管理员拥有 subscription，不使用 eligible_user_service。

文字和图片分开记录 Delivery；Capture、图片上传和图片发送分别可失败。状态必须可查询，支持仅重试图片。

## 7. 截图与投递健康

Capture Worker 使用短期 Capture Token 访问专用 `/capture/stock/:symbol` 路由（不经过 ProtectedLayout/AppShell），等待 `data-render-ready=true`，截取 `data-testid="stock-detail-capture"` 区域。截图使用与页面相同的行情聚合快照，保存 as_of 和 source hash。

Capture、Outbox、Delivery 服务必须健康可用；服务未运行或健康检查失败时，后端不得返回整体成功，也不得伪造成功状态。消息组应记录真实失败状态并允许后续重试。

### 7.1 Capture Worker 链路

- `stock_detail_feishu_service.send_stock_detail_to_feishu` 调用 `create_capture_token`（scope=stock_detail_capture + instrument_id + user_id）生成短期 token；
- 通过 `httpx.AsyncClient` POST `{capture_worker_url}/capture` 触发截图 worker；
- 截图 worker 内部 `stock_capture_service.capture_stock_chart` 访问 `{frontend_base_url}/capture/stock/{symbol}?source=watchlist&strategy=watchlist_monitor&event_id=...&token=...`；
- 前端 `CaptureStockPage` 使用独立 `captureClient`，从 query 参数读取 token 写入 `CAPTURE_TOKEN_KEY`，不污染普通 Access Token；
- 截图成功：worker 返回 `image_url`，后端创建图片消息 + 写入 image Outbox（与 text 共享 message_group_id）；
- 截图失败：写入 `CaptureJob(status=failed)` + 返回 `status="partial_failed"` + `failed_step` + `error_code` + `error_message`，文本 Outbox 已写入不阻塞；
- `capture_resp.raise_for_status()` 失败时解析 worker 返回的响应体（不丢弃 502 错误详情）。

### 7.2 状态机

| 状态 | 含义 | 触发条件 |
|---|---|---|
| `pending` | 截图成功，Outbox 异步投递中（终态由 delivery_worker 决定） | 截图 + 图片 Outbox 均成功 |
| `partial_failed` | 截图失败，文本已写入 Outbox，支持仅重试图片 | 截图或图片 Outbox 失败 |
| `failed` | 卡片段失败，整条消息组失败 | 文本 Outbox 失败 |
| `success` | 卡片 + 图片均投递成功 | delivery_worker 投递成功 |

失败上下文字段：

- `failed_step`：`capture` / `image_outbox` / `image_upload` / `image_delivery` / `card` / `text_outbox` / `snapshot`；
- `error_code`：`NO_IMAGE_URL` / `CAPTURE_REQUEST_FAILED` / `IMAGE_OUTBOX_FAILED` / `SNAPSHOT_FAILED` / `TEXT_OUTBOX_FAILED` 等；
- `error_message`：错误详情（最多 500 字符，含 worker 返回的响应体）。

## 8. 可观察性

管理员和运维必须能回答：运行中的 Worker、Git SHA、心跳、next run、当前任务、股票计数、失败阶段、重试状态、发布完整性、文字状态、图片状态和数据新鲜度。

## 9. 飞书渠道（已实现 Platform App only）

- 唯一接入方式：`feishu_platform_app`；
- `feishu_webhook` 已永久删除（`feishu_webhook_adapter.py` 已删除，migration 055 添加 CHECK 约束禁止 `adapter_type='feishu_webhook'`）；
- 管理员通知复用管理员用户自己的 `feishu_platform_app` NotificationChannel（通过 /settings 配置），不维护独立管理员飞书凭证；
- 实现证据：`backend/tests/test_feishu_platform_app_only.py` 11 passed，ALIGN-017 已 CLOSED。
