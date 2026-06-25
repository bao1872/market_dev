# 主页布局/时区统一/虚假功能清理与测试账号 Spec

## Why

advice.md 指出当前存在 7 类问题：主页两列宽度不协调且通知渠道 KPI 重复、用户通知规则卡片是前端假状态未接入后端、时间戳混用 UTC 与本地时区导致海外用户看到错误时间、首页 N+1 查询与交易时段判断依赖浏览器时区、任务页不够"live"、文档体系不完整、以及需要提供测试账号。本 spec 覆盖全部 7 项。

## What Changes

### 1. 主页布局修正
- 主页主体从 `split-2` 改为 `split-even`，选股结果与自选监控等宽
- 子卡片加 `min-width: 0` 防止监控表撑宽
- 删除第 4 张"通知渠道"KPI，主页 KPI 改为三项：最新选股结果 / 监控自选股 / 今日策略事件
- 删除 `useNotificationChannels` / `channelsQuery` / `feishuChannel` / `kpi4Status` / `kpi4Time` 等相关代码

### 2. 删除虚假"用户通知规则"卡片
- 删除设置页整张"用户通知规则"卡（`cooldown` / `quietStart` / `quietEnd` / `pauseOnDelay`）
- 删除相关本地 state 与 `pointerEvents: none` 逻辑
- 保留"我的通知渠道"和飞书实测
- 添加占位说明"用户级通知规则将在后续版本支持"

### 3. 时区统一
- **后端**：新增 `app/core/time.py`，提供 `now_utc()` / `now_shanghai()` / `shanghai_business_date()` / `to_shanghai_iso()`
- **前端**：新增 `formatShanghaiTime()` / `formatShanghaiDate()` 工具函数，固定 `timeZone: 'Asia/Shanghai'`
- 全仓库禁止 `new Date().toISOString().slice(0, 10)` 用于 A 股业务日期
- 全仓库禁止 `toLocaleTimeString()` 不指定 `timeZone`
- Docker `TZ: Asia/Shanghai` 已在现有 compose 中设置（确认）

### 4. 性能优化
- **P0 N+1 查询**：首页选股结果直接使用 `r.instrument_name` / `r.instrument_symbol` / `r.instrument_market`，删除 `useQueries` + `getInstrumentById` 循环
- **P0 交易时段**：`isInTradingHours()` 改为优先调用后端 `/market/status`，或前端用 `Intl.DateTimeFormat` 固定上海时区
- **P1 任务页 live**：`useSchedulerJobRuns` 增加 `refetchInterval: 10_000`；管理员系统概览增加 15s 轮询
- **P1 截图缓存**：按 `event_id + instrument_id + chart_version` 缓存 5-15 分钟，文本重试不重新截图

### 5. 管理员任务页增强
- 任务页增加：最后心跳、租约到期时间、当前子任务/StrategyRun ID、最新处理 Bar 时间
- 盘中监控"绿色"判定需同时满足：worker-monitor 心跳 < 90s、monitor_scheduler session=running、最新 MonitorEvaluation source_bar_time 距当前 ≤ 120s
- 盘后"完成"判定需同时满足：bars_scheduler 当日 succeeded、DSA StrategyRun.trade_date=最近交易日、status=published、failed_count=0
- 每 10s 自动刷新

### 6. 文档体系
- 新增 `docs/产品与业务规则.md`、`docs/系统架构.md`、`docs/策略与指标口径.md`、`docs/API与事件契约.md`、`docs/定时任务与运行手册.md`、`docs/部署与回滚.md`、`docs/开发与测试.md`、`docs/运维排障.md`、`docs/安全规范.md`
- 每份文档顶部记录：最后验证 Commit、负责人、事实来源、自动/人工维护
- `tools/update_docs.py` 增加 `--check` 模式

### 7. 测试账号
- 创建专用普通用户账号（仅连接生产数据库，只读权限足够）
- 创建专用管理员账号
- 提供测试环境 URL、临时密码、预期测试数据说明
- 测试账号不启用 MFA
- **BREAKING**：测试阶段不得修改数据库用户密码

## Impact

- Affected specs: `unify-monitoring-ssot-and-feishu-image-delivery`（截图缓存复用 delivery 链路）、`fix-prod-deployment-and-critical-chains`（TZ 已设置）
- Affected code:
  - `frontend/src/pages/IndexPage.tsx`（主页布局、KPI、N+1 修复）
  - `frontend/src/pages/SettingsPage.tsx`（删除虚假通知规则卡）
  - `frontend/src/pages/AdminJobsPage.tsx`（任务页 live 增强）
  - `frontend/src/pages/AdminSystemOverviewPage.tsx`（15s 轮询）
  - `frontend/src/utils/datetime.ts`（新增上海时区工具）
  - `frontend/src/hooks/useApi.ts`（refetchInterval、market status）
  - `frontend/src/styles/global.scss`（split-even、min-width: 0）
  - `backend/app/core/time.py`（新增）
  - `backend/app/services/stock_capture_service.py`（截图缓存）
  - `backend/app/api/health.py`（market status 端点确认）
  - `docs/` 目录（新增多份文档）

## ADDED Requirements

### Requirement: 统一时区工具模块

The system SHALL provide a single timezone utility module for both backend and frontend.

#### Scenario: Backend time
- **WHEN** any backend code needs current time
- **THEN** it uses `now_utc()` for storage and `now_shanghai()` for business logic

#### Scenario: Frontend display
- **WHEN** any frontend code formats a timestamp
- **THEN** it uses `formatShanghaiTime()` with `timeZone: 'Asia/Shanghai'`

#### Scenario: Business date
- **WHEN** A-share business date is needed
- **THEN** it uses `shanghai_business_date()` not `new Date().toISOString().slice(0, 10)`

### Requirement: 截图缓存

The system SHALL cache screenshot results by event_id + instrument_id + chart_version for 5-15 minutes.

#### Scenario: Text retry
- **WHEN** a text delivery retries
- **THEN** no new screenshot is taken

#### Scenario: Image retry
- **WHEN** an image delivery retries
- **THEN** it reuses the cached screenshot if available

### Requirement: 测试账号

The system SHALL provide dedicated test accounts.

#### Scenario: Normal user
- **WHEN** a tester logs in with the normal user account
- **THEN** they can access all user-facing features without admin privileges

#### Scenario: Admin user
- **WHEN** a tester logs in with the admin account
- **THEN** they can access admin pages for verification

## MODIFIED Requirements

### Requirement: 主页布局

The homepage SHALL use equal-width two-column layout with three KPI cards.

#### Scenario: Layout
- **WHEN** viewing the homepage
- **THEN** selection results and watchlist monitor are equal width

#### Scenario: KPI count
- **WHEN** viewing KPI cards
- **THEN** exactly three are shown: latest selection count, monitored stocks, today's events

### Requirement: 交易时段判断

The trading hours check SHALL use Asia/Shanghai timezone, not browser local time.

#### Scenario: Backend market status
- **WHEN** frontend needs trading status
- **THEN** it calls `/market/status` API which uses Shanghai timezone

#### Scenario: Fallback
- **WHEN** API is unavailable
- **THEN** frontend uses `Intl.DateTimeFormat` with `timeZone: 'Asia/Shanghai'`

### Requirement: 任务页实时刷新

The admin jobs page SHALL auto-refresh every 10 seconds.

#### Scenario: Live status
- **WHEN** admin views jobs page
- **THEN** scheduler job runs refresh every 10s with `refetchIntervalInBackground: false`

## REMOVED Requirements

### Requirement: 用户通知规则卡片

**Reason**: 前端假装支持但后端无对应 API，误导用户。
**Migration**: 删除整张卡片及相关 state，保留"我的通知渠道"。将来实现 `UserNotificationPreference` 表后再恢复。
