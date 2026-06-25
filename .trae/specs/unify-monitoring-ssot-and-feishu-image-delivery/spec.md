# 统一监控指标真源与飞书图片投递 Spec

## Why

advice.md 指出当前项目存在以下关键风险：

1. **监控指标口径未统一**：BB、POC、上下节点、position、peak_rows、bullish_volume、bearish_volume 等可能在 monitor、detail、watchlist、通知多处分复计算，导致同一股票同一时刻各页面数值不一致。
2. **个股详情图表达不完整**：缺少节点多头/空头量、peak 强度、A股风格配色，无法直接作为飞书推送图源。
3. **飞书正式投递链未闭环**：当前以复杂卡片为主，未做到"文本+图片"两段式投递，且存在重复时间字段。
4. **首页与自选页监控组件虽已复用，但字段/adapter 仍需与统一口径对齐**。

本轮目标是以 `ref/交易/app/monitoring.py` 的 volume profile 参数与算法为唯一真源（SSOT），统一监控相关指标计算，补强 detail 图，并把飞书用户通知改为"文本+图片"。

## What Changes

- **新增/重构**：共享 volume profile 计算模块，统一参数 `VP_LOOKBACK=360`、`VP_ROWS=100`、`VP_VALUE_AREA_PCT=0.70`、`VP_PEAK_DETECTION_PCT=0.05`、`VP_NODE_THRESHOLD_PCT=0.01`。
- **修改**：`VolumeNodeMonitor` / `WatchlistMonitor` / `indicator_service` / `monitor_chart_renderer` 统一调用共享模块，不再各自计算 profile。
- **修改**：个股详情页图表增加 peak 节点多空量标签与迷你多空柱，配色改为 A 股红涨绿跌；图表区域可作为飞书截图唯一图源。
- **新增**：飞书"文本+图片"两段式投递：一次事件生成 `delivery_type=text` 和 `delivery_type=image` 两条 `MessageDelivery`。
- **修改**：飞书文本模板精简字段，只保留一个"触发时间"，删除重复的第二个数据时间。
- **修改**：`watchlist-monitor` 共享组件字段与统一口径对齐；首页仅做只读摘要版，不自持独立计算。
- **修改**：`stock_capture_service` 与 detail 页约定 `data-testid="stock-detail-capture"` 渲染区域，确保截图与页面展示完全一致。
- **新增/更新**：验收文档（指标口径统一清单、截图证据、worker 日志、delivery 记录）。
- **资源约束适配**：物理机为 4 核 / 7.4GB 内存 / swap 几乎耗尽，截图 Worker 单浏览器上下文、volume profile 计算按股票串行/分块，避免并发 OOM。

## Impact

- Affected specs: `fix-prod-deployment-and-critical-chains`（已建立的 MessageDelivery 状态机需复用）、`watchlist-monitor-closure`。
- Affected code:
  - `backend/app/strategy/monitors/volume_node_monitor.py`
  - `backend/app/strategy/monitors/watchlist_monitor.py`
  - `backend/app/services/monitor_batch_service.py`
  - `backend/app/services/indicator_service.py`
  - `backend/app/services/monitor_chart_renderer.py`
  - `backend/app/services/stock_capture_service.py`
  - `backend/app/services/feishu_card_builder.py`
  - `backend/app/services/feishu_platform_app_adapter.py`
  - `backend/app/services/message_builder.py`
  - `backend/app/services/delivery_worker.py`
  - `backend/app/schemas/notification.py`
  - `backend/app/api/indicators.py`
  - `frontend/src/pages/StockDetailPage.tsx`
  - `frontend/src/features/watchlist-monitor/*`

## ADDED Requirements

### Requirement: 共享 Volume Profile 计算模块

The system SHALL provide a single volume profile computation module that all monitor/detail/chart/notification code paths call.

#### Scenario: Parameter alignment
- **WHEN** any code path needs volume profile for a stock
- **THEN** it uses `VP_LOOKBACK=360`, `VP_ROWS=100`, `VP_VALUE_AREA_PCT=0.70`, `VP_PEAK_DETECTION_PCT=0.05`, `VP_NODE_THRESHOLD_PCT=0.01`

#### Scenario: No duplicate implementation
- **WHEN** a developer searches for volume profile logic
- **THEN** only one implementation exists outside of `ref/` reference code

### Requirement: 飞书两段式投递

The system SHALL create two `MessageDelivery` records for each monitor event destined for Feishu: one `text` and one `image`.

#### Scenario: Text delivery
- **WHEN** an event is expanded to a Feishu channel
- **THEN** a `MessageDelivery` with `delivery_type=text` is created carrying a plain-text message

#### Scenario: Image delivery
- **WHEN** the same event is expanded
- **THEN** a second `MessageDelivery` with `delivery_type=image` is created; the image is produced by `worker-capture` from the stock detail page

#### Scenario: State tracking
- **WHEN** delivery worker processes either record
- **THEN** `status` transitions through `pending -> sending -> success|retrying|dead` with `attempt_count` and `next_attempt_at`

### Requirement: 个股详情图增强

The stock detail chart SHALL display bullish/bearish volume per peak node using A-share color convention.

#### Scenario: Peak node label
- **WHEN** a peak node is rendered
- **THEN** its label shows price, bullish volume, and bearish volume

#### Scenario: A-share colors
- **WHEN** K-line and volume bars are rendered
- **THEN** up is red, down is green; node/POC/peak highlights are clearly distinguishable

## MODIFIED Requirements

### Requirement: 飞书通知模板

The Feishu notification SHALL be plain text plus an image, not a complex card with duplicate timestamps.

#### Scenario: Text content
- **WHEN** text delivery is sent
- **THEN** it contains symbol/name, trigger type, trigger time, current price, BB triple bands, upper/lower nodes, POC, position

#### Scenario: Single timestamp
- **WHEN** text is generated
- **THEN** only "触发时间" is shown; no second "数据时间"/"更新时间" appears

### Requirement: 首页/自选监控组件字段

The `watchlist-monitor` shared component SHALL use the unified monitor field set.

#### Scenario: Field consistency
- **WHEN** viewing 首页 or 我的自选
- **THEN** columns and adapters consume the same backend fields in the same order

## REMOVED Requirements

### Requirement: 飞书复杂卡片通知

**Reason**: advice.md 明确建议改为"文本+图片"，避免字段堆砌和两个时间造成的困惑。
**Migration**: 保留 `feishu_card_builder.py` 中核心 helper 以兼容管理后台/测试预览，但用户事件通知不再默认走 interactive card。
