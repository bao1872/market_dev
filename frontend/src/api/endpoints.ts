// API 端点定义层 - 类型安全的 API 访问函数
//
// 职责：
// 1. 定义所有 API 实体的 TypeScript 接口（与后端 Pydantic schema 对齐，字段使用 snake_case）
// 2. 导出按领域分组的 API 调用函数，每个函数使用 apiClient 或 publicApiClient 发起请求并返回 response.data
//
// 约定：
// - 后端直接返回数据（FastAPI response_model 序列化），不包裹在 ApiResponse 中
// - 字段命名使用 snake_case 以匹配后端 JSON 格式（apiClient 无 camelCase 转换）
// - UUID / datetime / date 字段在 TS 中统一为 string（JSON 序列化后为字符串）
// - user_id 由认证上下文注入，不出现在请求体中（V1.1 安全约束）
// - 通知 API 当前使用 X-User-Id header（占位，后续接入 JWT）
// - 公开端点（login/register/refresh）使用 publicApiClient，避免携带旧 token 或触发 401 refresh

import { apiClient, publicApiClient } from './client'
import { useAuthStore } from '../store/auth'

// ============================================================
// 通用辅助
// ============================================================

/**
 * 获取当前用户 ID（从 auth store 读取），用于通知 API 的 X-User-Id header。
 * 通知 API 当前使用 X-User-Id 占位，后续接入 JWT 后可移除。
 */
function getUserIdHeader(): Record<string, string> {
  const user = useAuthStore.getState().user
  return user ? { 'X-User-Id': user.id } : {}
}

// ============================================================
// Auth 领域类型
// ============================================================

// [Auth] - 描述: AccessProfile 当前用户完整权限上下文（11 字段，对齐后端 AccessProfileResponse）
// 与 backend/app/schemas/access.py AccessProfileResponse 字段语义完全一致
// 唯一真源为 backend/app/services/access_control_service.get_access_context
export interface AccessProfile {
  user_id: string
  account_status: string
  roles: string[]
  is_admin: boolean
  is_member: boolean
  subscription_active: boolean
  plan_code: string | null
  plan_display_name: string | null
  expires_at: string | null
  features: string[]
  limits: Record<string, number>
}

// [Auth] - 描述: 登录响应 - 含 4 个 token 字段 + 10 个 AccessProfile 字段（对齐后端 LoginResponse）
// 替代旧字段 membership_expired（语义等价：subscription_active = not membership_expired；字段名保留为 V1.6 API 兼容）
// next_route 由后端权威计算：admin→/admin/overview；member active→/overview；member expired→/subscription-expired
export interface LoginResponse {
  // token 字段（4 个）
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  // AccessProfile 字段（10 个）
  is_admin: boolean
  roles: string[]
  subscription_required: boolean
  subscription_active: boolean
  plan_code: string | null
  plan_display_name: string | null
  expires_at: string | null
  features: string[]
  limits: Record<string, number>
  next_route: string
}

/** Token 刷新响应 */
export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

/** 用户信息响应（含角色列表） */
export interface UserResponse {
  id: string
  email: string
  status: string
  timezone: string
  roles: string[]
  created_at: string
  updated_at: string
}

/** 用户列表分页响应 */
export interface UserListResponse {
  items: UserResponse[]
  total: number
  limit: number
  offset: number
}

/** 审计日志列表项 */
export interface AuditLogListItem {
  id: string
  actor_user_id: string
  action: string
  target_type: string
  target_id: string | null
  before_data: Record<string, unknown> | null
  after_data: Record<string, unknown> | null
  request_id: string | null
  ip_hash: string | null
  created_at: string
}

/** 审计日志列表响应 */
export interface AuditLogListResponse {
  items: AuditLogListItem[]
  total: number
  limit: number
  offset: number
}

/** 会员状态响应 */
export interface MembershipResponse {
  status: string
  started_at: string
  expires_at: string
  remaining_days: number
  renewal_count: number
}

/** 注册成功响应（membership_* 字段为 V1.6 API 遗留命名，语义等价于 subscription_*） */
export interface RegisterSuccessResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  membership_started_at: string
  membership_expires_at: string
}

/** 续期成功响应（membership_status 为 V1.6 API 遗留命名，语义等价于 subscription_status） */
export interface RenewSuccessResponse {
  membership_status: string
  started_at: string
  old_expires_at: string | null
  new_expires_at: string
  remaining_days: number
}

/** 订阅记录响应 */
export interface SubscriptionResponse {
  id: string
  user_id: string
  plan_code: string
  status: string
  starts_at: string
  expires_at: string
  entitlement_snapshot: Record<string, unknown>
  source: string
  created_by: string | null
  created_at: string
  updated_at: string
}

/** 管理员续期订阅响应 */
export interface SubscriptionRenewResponse extends SubscriptionResponse {
  old_expires_at: string
  new_expires_at: string
}

// ============================================================
// Instrument 领域类型
// ============================================================

/** 股票主数据 */
export interface Instrument {
  id: string
  symbol: string
  name: string
  market: string
  status: string
  listing_date: string | null
  created_at: string
  updated_at: string
}

/** 股票列表分页响应 */
export interface InstrumentListResponse {
  items: Instrument[]
  total: number
  page: number
  page_size: number
  pages: number
}

// ============================================================
// Strategy 领域类型
// ============================================================

/** 策略定义 */
export interface Strategy {
  id: string
  strategy_key: string
  kind: string
  display_name: string
  created_at: string
}

/** 策略列表响应 */
export interface StrategyListResponse {
  items: Strategy[]
  total: number
}

/** 策略版本 */
export interface StrategyVersion {
  id: string
  strategy_definition_id: string
  version: string
  status: string
  build_hash: string
  released_at: string | null
  manifest: Record<string, unknown>
}

/** 策略版本列表响应 */
export interface StrategyVersionListResponse {
  items: StrategyVersion[]
  total: number
}

/** 策略版本 schema 响应 */
export interface StrategySchema {
  strategy_id: string
  version: string
  kind: string
  parameters: Record<string, unknown>[]
  outputs: Record<string, unknown>[]
  input: Record<string, unknown>
  capabilities: Record<string, unknown>
}

// ============================================================
// Strategy Run 领域类型
// ============================================================

/** 策略运行记录 */
export interface StrategyRun {
  id: string
  strategy_version_id: string
  run_type: string
  trade_date: string | null
  data_cutoff: string | null
  status: string
  input_overrides: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
  idempotency_key: string
  published_at: string | null
  total_instruments: number | null
  succeeded_count: number | null
  failed_count: number | null
  skipped_count: number | null
}

/** 策略运行列表响应 */
export interface StrategyRunListResponse {
  items: StrategyRun[]
  total: number
}

/** 策略运行结果 */
// [全量 universe] - 描述: id/payload 等字段可空（skipped/failed 行无 strategy_results 记录）
export interface StrategyResult {
  id: string | null
  run_id: string | null
  strategy_version_id: string | null
  instrument_id: string
  instrument_symbol?: string
  instrument_name?: string
  instrument_market?: string
  trade_date: string | null
  payload: Record<string, unknown> | null
  created_at: string | null
  // 全量 universe 改造新增字段
  item_status: string
  reason_code?: string
  error_message?: string
}

/** 策略运行结果列表响应（分页） */
export interface StrategyResultListResponse {
  items: StrategyResult[]
  total: number
  page: number
  page_size: number
  source_total?: number
  filtered_total?: number
}

// ============================================================
// Monitor State 领域类型
// ============================================================

/** 监控状态 */
export interface MonitorState {
  strategy_version_id: string
  instrument_id: string
  bar_time: string
  calculation_id: string
  state_schema_version: number
  payload: Record<string, unknown>
  updated_at: string
}

/** 监控状态列表响应 */
export interface MonitorStateListResponse {
  items: MonitorState[]
  total: number
}

// ============================================================
// Strategy Event 领域类型
// ============================================================

/** 策略事件（列表项，不含 snapshot） */
export interface StrategyEvent {
  id: string
  event_key: string
  strategy_version_id: string
  instrument_id: string
  event_type: string
  event_time: string
  logical_entity_id: string | null
  schema_version: number
  payload: Record<string, unknown>
  created_at: string
}

/** 策略事件详情（含 snapshot 快照） */
export interface StrategyEventDetail extends StrategyEvent {
  snapshot: Record<string, unknown>
}

/** 策略事件列表响应 */
export interface StrategyEventListResponse {
  items: StrategyEvent[]
  total: number
}

// ============================================================
// Notification 领域类型
// ============================================================

/** 消息投递状态机：与后端 MessageDelivery.status 对齐（models/notification.py） */
export type DeliveryStatus = 'pending' | 'sending' | 'success' | 'failed' | 'retrying' | 'dead'

/** 消息投递记录 */
export interface MessageDelivery {
  id: string
  channel_id: string
  notification_message_id: string
  adapter_type: string
  display_name: string
  status: DeliveryStatus
  attempt_count: number
  next_retry_at: string | null
  last_error_code: string | null
  created_at: string
  // [消息投递管理] - 从关联消息提取的摘要与主要标的
  message_summary: string | null
  primary_instrument: {
    instrument_id?: string
    symbol?: string
    name?: string
  } | null
}

/** 主要标的（结构化字段） */
export interface PrimaryInstrument {
  instrument_id?: string
  symbol?: string
  name?: string
}

/** 通知消息 */
export interface NotificationMessage {
  id: string
  user_id: string
  message_type: string
  template_key: string
  template_version: string
  source_type: string
  source_id: string | null
  body: Record<string, unknown>
  deliveries: MessageDelivery[]
  read_at: string | null
  created_at: string
  // [消息中心] - 结构化字段：前端表格直接展示
  strategy_key: string | null
  strategy_name: string | null
  instrument_count: number | null
  primary_instrument: PrimaryInstrument | null
  event_summary: string | null
}

/** 通知消息列表响应 */
export interface NotificationMessageListResponse {
  items: NotificationMessage[]
  total: number
}

/** 未读消息计数响应（角标专用） */
export interface UnreadCountResponse {
  unread_count: number
}

/** 批量标记已读响应 */
export interface ReadAllMessagesResponse {
  marked_count: number
}

/** 通知渠道 */
export interface NotificationChannel {
  id: string
  user_id: string
  adapter_type: string
  display_name: string
  status: string
  last_verified_at: string | null
  last_error_code: string | null
  created_at: string
  target_config?: Record<string, unknown>
}

/** 通知渠道列表响应 */
export interface NotificationChannelListResponse {
  items: NotificationChannel[]
  total: number
}

/** 投递结果 */
export interface DeliveryResult {
  success: boolean
  error_code: string | null
  error_message: string | null
  provider_response: Record<string, unknown> | null
}

/** 渠道测试响应 */
export interface ChannelTestResponse {
  channel: NotificationChannel
  delivery: DeliveryResult
}

/** 最近事件实测响应 */
export interface ChannelLatestEventTestResponse {
  channel: NotificationChannel
  delivery: DeliveryResult
  diagnostics: Record<string, unknown>
}

// [Capture] - 描述: Capture Snapshot API 响应类型
// 与后端 backend/app/api/capture.py 的 get_capture_snapshot 返回结构对齐

/** 截图数据快照响应（一次返回 instrument / bars / indicators / events） */
export interface CaptureSnapshotResponse {
  instrument: Instrument
  bars: BarListResponse
  indicators: IndicatorResponse
  events: StrategyEventListResponse
  snapshot_time: string
  // 注意：last_live_bar_time / is_partial / data_source 只在 bars (BarListResponse) 内，
  // 前端实时状态必须从 snapshot.bars.xxx 读取，禁止从 snapshot 顶层读取
  capture: {
    user_id: string
    event_id: string
    scope: string
  }
}

// [StockDetailFeishu] - 描述: 异步 Outbox 投递模式类型契约（POST 创建 + GET 状态轮询）
// 与后端 backend/app/api/stock_detail_feishu.py 的 SendFeishuResponse / ShareStatusResponse 对齐

/** 单条投递状态（card / image / capture 共用） */
export type ShareDeliveryStatus =
  | 'pending'
  | 'sending'
  | 'success'
  | 'failed'
  | 'retrying'
  | 'dead'
  | 'not_created'

/** POST /instruments/{instrument_id}/send-feishu 响应 - 创建异步投递任务 */
export interface StockDetailFeishuCreateResponse {
  test_run_id: string
  message_group_id: string
  message_id: string
  image_message_id: string | null
  status: 'pending'
}

/** GET /stock-detail-feishu/{test_run_id}/status 响应 - 查询投递状态 */
export interface StockDetailFeishuStatusResponse {
  test_run_id: string
  message_group_id: string | null
  card_status: ShareDeliveryStatus
  capture_status: ShareDeliveryStatus
  image_upload_status: ShareDeliveryStatus
  image_status: ShareDeliveryStatus
  overall_status: 'pending' | 'success' | 'partial_failed' | 'failed'
  failed_step: 'capture' | 'image_upload' | 'image_delivery' | 'card' | 'image' | null
  error_code: string | null
  error_message: string | null
  image_message_id: string | null
}

/** 消息预览响应 */
export interface NotificationPreviewResponse {
  dto: Record<string, unknown>
  in_app: Record<string, unknown>
  feishu_card: Record<string, unknown>
}

// ============================================================
// Watchlist 领域类型
// ============================================================

/** 自选股项 */
export interface WatchlistItem {
  id: string
  user_id: string
  instrument_id: string
  source: string
  active: boolean
  created_at: string
  removed_at: string | null
}

/** 自选股列表响应 */
export interface WatchlistListResponse {
  items: WatchlistItem[]
  total: number
}

/** 自选股+监控状态聚合项（与 backend/app/schemas/watchlist.py WatchlistMonitorStatusItem 对齐） */
export interface WatchlistMonitorStatusItem {
  watchlist_item_id: string
  instrument_id: string
  symbol: string
  name: string
  market: string
  watchlist_created_at: string
  monitor_status: MarketSession | 'WAITING_FIRST_RUN' | 'SUCCEEDED' | 'FAILED' | 'STALE'
  market_session: MarketSession
  calculation_status: 'SUCCEEDED' | 'FAILED' | 'STALE' | 'WAITING_FIRST_RUN'
  freshness_seconds: number | null
  last_bar_time: string | null
  evaluation_status: string | null
  retry_count: number | null
  error_code: string | null
  source_bar_time: string | null
  metrics: Record<string, unknown> | null
  latest_event?: {
    event_type: string
    event_time: string
    boundary: number | null
  } | null
  updated_at: string | null
}

/** 自选股+监控状态聚合响应 */
export interface WatchlistMonitorStatusResponse {
  items: WatchlistMonitorStatusItem[]
}

// ============================================================
// Bar 领域类型
// ============================================================

/** 单条行情数据 */
export interface Bar {
  instrument_id: string
  trade_date: string | null
  trade_time: string | null
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
  adj_factor: number
}

/** 行情列表响应（服务端分页 + 数据源诊断） */
export interface BarListResponse {
  items: Bar[]
  total: number
  page: number
  page_size: number
  timeframe: string
  adj: string
  // [bars] - 数据源诊断字段
  data_source: string
  as_of: string | null
  is_partial: boolean
  // [bars] - 实时 bar 时间（对齐后端 BarListResponse schema）
  last_persisted_bar_time?: string | null
  last_live_bar_time?: string | null
  freshness_seconds: number
  degraded: boolean
  degraded_reason: string | null
}

// ============================================================
// Calendar 领域类型
// ============================================================

/** 交易日历条目 */
export interface CalendarDay {
  id: string
  trade_date: string
  is_trading_day: boolean
  market: string
  created_at: string
}

/** 交易日历列表响应 */
export interface CalendarListResponse {
  items: CalendarDay[]
  total: number
}

/** 是否交易日查询响应 */
export interface TradingDayResponse {
  trade_date: string
  is_trading_day: boolean
  source: string
}

// ============================================================
// Admin Membership 领域类型
// ============================================================

// [plan_contract] - 描述: 套餐定义由后端 plans 表唯一真源驱动，前端不硬编码套餐字段
// 权威值为 backend/app/models/plan.py / app/services/plan_service.py，通过 GET /plans 公开端点获取
/** 套餐代码（与后端 plans.plan_code 一致） */
export type PlanCode = string

/** 套餐定义响应（与 backend/app/schemas/plan.py PlanResponse 对齐） */
export interface PlanResponse {
  plan_code: string
  display_name: string
  monitor_limit: number
  notification_channel_limit: number
  message_retention_days: number
  features: string[]
}

/** 邀请码响应（含明文，仅生成时返回）+ 套餐快照 */
export interface InviteCode {
  id: string
  code: string
  grant_days: number
  plan_code: PlanCode | null
  monitor_limit: number | null
  grant_months: number | null
  note: string | null
  created_at: string
}

/** 邀请码列表项（不含明文）+ 套餐快照 */
export interface InviteCodeListItem {
  id: string
  status: string
  grant_days: number
  plan_code: PlanCode | null
  monitor_limit: number | null
  grant_months: number | null
  note: string | null
  created_by: string
  created_at: string
  used_by: string | null
  used_at: string | null
  usage_type: string | null
}

/** 邀请码列表响应 */
export interface InviteCodeListResponse {
  items: InviteCodeListItem[]
  total: number
  limit: number
  offset: number
}

/** 兑换记录 */
export interface InviteRedemption {
  id: string
  invite_code_id: string
  user_id: string
  usage_type: string
  old_expires_at: string | null
  new_expires_at: string
  redeemed_at: string
}

/** 订阅账户列表项（MemberListItem / membership_status 为 V1.6 API 遗留命名） */
export interface MemberListItem {
  user_id: string
  email: string
  account_status: string
  membership_status: string | null
  started_at: string | null
  expires_at: string | null
  remaining_days: number | null
  renewal_count: number
  created_at: string
}

/** 会员账户列表响应 */
export interface MemberListResponse {
  items: MemberListItem[]
  total: number
  limit: number
  offset: number
}

// ============================================================
// Version 领域类型
// ============================================================

/** 版本信息响应 */
export interface VersionInfo {
  git_sha: string
  build_time: string
  app_version: string
  alembic_revision: string
}

/** 获取后端版本信息（无需认证） */
export async function getVersion(): Promise<VersionInfo> {
  const res = await apiClient.get<VersionInfo>('/version')
  return res.data
}

// ============================================================
// Health 领域类型
// ============================================================

/** 健康检查响应 */
export interface HealthResponse {
  status: 'ok' | string
  service: string
  version: string
}

/** 获取后端健康状态（无需认证） */
export async function getHealth(): Promise<HealthResponse> {
  const res = await apiClient.get<HealthResponse>('/health')
  return res.data
}

// ============================================================
// Market Status 领域类型
// ============================================================

/** 市场阶段枚举（6 值，与 backend app.services.market_status_service 对齐） */
export type MarketSession =
  | 'NON_TRADING_DAY'
  | 'PRE_OPEN'
  | 'MORNING_SESSION'
  | 'LUNCH_BREAK'
  | 'AFTERNOON_SESSION'
  | 'MARKET_CLOSED'

/** 市场状态 */
export interface MarketStatus {
  is_trading_day: boolean
  is_trading_hours: boolean
  status_text: string  // "交易中" / "已收盘" / "休市" / "盘前"（向后兼容）
  market_session: MarketSession  // 6 值枚举
}

// ============================================================
// 请求体类型
// ============================================================

/** 登录请求 */
export interface LoginRequest {
  email: string
  password: string
}

/** 注册请求 */
export interface RegisterRequest {
  email: string
  password: string
  invite_code: string
  timezone?: string
}

/** 续期请求 */
export interface RenewRequest {
  invite_code: string
}

/** 触发策略运行请求 */
export interface TriggerRunRequest {
  trade_date?: string
  instrument_ids?: string[]
  run_type?: string
}

/** 加入自选请求 */
export interface WatchlistAddRequest {
  instrument_id: string
  source?: string
}

/** 创建通知渠道请求 */
export interface CreateChannelRequest {
  adapter_type: string
  display_name: string
  target_config: Record<string, unknown>
  secret_ref?: string
}

/** 消息预览请求 */
export interface NotificationPreviewRequest {
  message_type: string
  context: Record<string, unknown>
  locale?: string
}

/** 邀请码生成请求 - plan_code/grant_months 由前端提交，monitor_limit 由后端按 plan_code 计算 */
export interface InviteCodeCreateRequest {
  count?: number
  note?: string
  plan_code?: PlanCode
  grant_months?: number
}

/** 管理员授予用户套餐请求 */
export interface GrantSubscriptionRequest {
  plan_code: PlanCode
  grant_months: number
}

/** 管理员续期用户套餐请求 */
export interface RenewSubscriptionRequest {
  grant_months: number
}

/** 管理员变更用户套餐请求 */
export interface ChangePlanRequest {
  plan_code: PlanCode
  grant_months: number
}

/** 管理员变更用户账户状态请求 */
export interface ChangeAccountStatusRequest {
  status: 'active' | 'disabled'
}


// ============================================================
// 查询参数类型
// ============================================================

/** 股票列表查询参数 */
export interface InstrumentQueryParams {
  keyword?: string
  market?: string
  status?: string
  page?: number
  page_size?: number
}

/** 策略事件查询参数 */
export interface StrategyEventQueryParams {
  event_type?: string
  start_time?: string
  end_time?: string
  limit?: number
}

/** 策略运行结果查询参数 */
export interface StrategyResultQueryParams {
  matched_only?: boolean
  metric_filters?: string
  keyword?: string
  sort_by?: string
  sort_desc?: boolean
  page?: number
  page_size?: number
  limit?: number
  offset?: number
  universe?: 'all' | 'watchlist'
}

/** 行情查询参数 */
export interface BarQueryParams {
  timeframe?: string
  adj?: string
  start_date?: string
  end_date?: string
  page?: number
  page_size?: number
}

/** 日历查询参数 */
export interface CalendarQueryParams {
  start_date?: string
  end_date?: string
  market?: string
}


/** 分页查询参数 */
export interface PaginationParams {
  limit?: number
  offset?: number
}

// ============================================================
// ===== Auth 端点 =====
// ============================================================

// [Auth] - 描述: 用户登录 - 返回 token + AccessProfile 权限上下文 + next_route（公开接口）
// 前端不再判断 membership_expired，直接使用 next_route 跳转
export async function login(email: string, password: string): Promise<LoginResponse> {
  const { data } = await publicApiClient.post<LoginResponse>('/auth/login', { email, password })
  return data
}

/** 邀请码注册 - 原子操作创建账户 + 开通 30 天会员（公开接口） */
export async function register(payload: RegisterRequest): Promise<RegisterSuccessResponse> {
  const { data } = await publicApiClient.post<RegisterSuccessResponse>('/auth/register', payload)
  return data
}

/** 邀请码续期 - 未到期顺延 / 已到期从当天计算（需认证，保持 apiClient） */
export async function renew(inviteCode: string): Promise<RenewSuccessResponse> {
  const { data } = await apiClient.post<RenewSuccessResponse>('/auth/renew', { invite_code: inviteCode })
  return data
}

/** 使用 refresh token 刷新，返回新的 access + refresh token（公开接口）
 * refresh_token 通过 JSON body 提交（非 query string），避免被 access log / referer 泄露
 */
export async function refreshToken(refreshToken: string): Promise<TokenResponse> {
  const { data } = await publicApiClient.post<TokenResponse>('/auth/refresh', {
    refresh_token: refreshToken,
  })
  return data
}

/** 获取当前用户信息（含角色列表） */
export async function getMe(): Promise<UserResponse> {
  const { data } = await apiClient.get<UserResponse>('/me')
  return data
}

// [Auth] - 描述: 获取当前用户完整权限上下文 AccessProfile（11 字段，对齐后端 AccessProfileResponse）
// 续期成功后调用此接口刷新前端 accessProfile，避免重新登录
export async function getMyAccess(): Promise<AccessProfile> {
  const { data } = await apiClient.get<AccessProfile>('/me/access')
  return data
}

/** 获取当前用户订阅状态（/me/membership 为 V1.6 遗留路径名） */
export async function getMyMembership(): Promise<MembershipResponse> {
  const { data } = await apiClient.get<MembershipResponse>('/me/membership')
  return data
}

// ============================================================
// ===== Events Summary 领域类型
// ============================================================

/** 策略事件汇总响应 */
export interface EventsSummaryResponse {
  date: string
  total_events: number
  instruments_with_events: number
  last_event_at: string | null
}

/** 查询当前用户指定日期的策略事件汇总 */
export async function getEventsSummary(date: string): Promise<EventsSummaryResponse> {
  const { data } = await apiClient.get<EventsSummaryResponse>('/me/events/summary', {
    params: { date },
  })
  return data
}

// ============================================================
// ===== Instruments 端点 =====
// ============================================================

/** 查询股票列表，支持关键词搜索、市场/状态筛选与分页 */
export async function getInstruments(params?: InstrumentQueryParams): Promise<InstrumentListResponse> {
  const { data } = await apiClient.get<InstrumentListResponse>('/instruments', { params })
  return data
}

/** 批量查询股票响应 */
export interface InstrumentBatchResponse {
  items: Instrument[]
  total: number
}

/** 按 ID 列表批量查询股票（最多 1000 个） */
export async function batchGetInstruments(ids: string[]): Promise<InstrumentBatchResponse> {
  const { data } = await apiClient.post<InstrumentBatchResponse>('/instruments/batch', { ids })
  return data
}

/** 按 ID 查询单个股票 */
export async function getInstrumentById(instrumentId: string): Promise<Instrument> {
  const { data } = await apiClient.get<Instrument>(`/instruments/${instrumentId}`)
  return data
}

/** 按 symbol 查询股票（symbol 唯一，最多返回 1 条） */
export async function getInstrumentBySymbol(symbol: string): Promise<Instrument> {
  const { data } = await apiClient.get<Instrument>(`/instruments/by-symbol/${symbol}`)
  return data
}

// ============================================================
// ===== Strategies 端点 =====
// ============================================================

/** 获取策略列表（支持 kind 过滤） */
export async function getStrategies(kind?: string): Promise<StrategyListResponse> {
  const { data } = await apiClient.get<StrategyListResponse>('/strategies', { params: { kind } })
  return data
}

/** 获取策略详情 */
export async function getStrategy(strategyKey: string): Promise<Strategy> {
  const { data } = await apiClient.get<Strategy>(`/strategies/${strategyKey}`)
  return data
}

/** 获取策略的所有版本 */
export async function getStrategyVersions(strategyKey: string): Promise<StrategyVersionListResponse> {
  const { data } = await apiClient.get<StrategyVersionListResponse>(`/strategies/${strategyKey}/versions`)
  return data
}

/** 获取策略版本的 schema（参数/输出/输入/能力） */
export async function getStrategyVersionSchema(strategyKey: string, version: string): Promise<StrategySchema> {
  const { data } = await apiClient.get<StrategySchema>(
    `/strategies/${strategyKey}/versions/${version}/schema`,
  )
  return data
}

/** 创建策略（admin）- 提交 Manifest 创建策略定义 + 草稿版本 */
export async function createStrategy(
  manifest: Record<string, unknown>,
  strategySchema?: Record<string, unknown>,
): Promise<StrategyVersion> {
  const { data } = await apiClient.post<StrategyVersion>('/admin/strategies', {
    manifest,
    schema: strategySchema,
  })
  return data
}

/** 发布策略版本（admin）- draft -> released */
export async function releaseStrategyVersion(strategyKey: string, version: string): Promise<StrategyVersion> {
  const { data } = await apiClient.post<StrategyVersion>(
    `/admin/strategies/${strategyKey}/versions/${version}/release`,
  )
  return data
}

/** 归档策略版本（admin）- released -> archived */
export async function archiveStrategyVersion(strategyKey: string, version: string): Promise<StrategyVersion> {
  const { data } = await apiClient.post<StrategyVersion>(
    `/admin/strategies/${strategyKey}/versions/${version}/archive`,
  )
  return data
}

// ============================================================
// ===== Strategy Runs 端点 =====
// ============================================================

/** 触发策略运行（admin） */
export async function triggerStrategyRun(strategyKey: string, payload: TriggerRunRequest): Promise<StrategyRun> {
  const { data } = await apiClient.post<StrategyRun>(
    `/admin/strategies/${strategyKey}/run`,
    payload,
  )
  return data
}

/** 查询策略运行历史（admin） */
export async function getStrategyRuns(
  strategyKey: string,
  params?: { status?: string; limit?: number; offset?: number },
): Promise<StrategyRunListResponse> {
  const { data } = await apiClient.get<StrategyRunListResponse>(
    `/strategies/${strategyKey}/runs`,
    { params },
  )
  return data
}

/** 查询策略运行历史（admin，/admin 前缀路径） */
export async function getAdminStrategyRuns(
  strategyKey: string,
  params?: { status?: string; limit?: number; offset?: number },
): Promise<StrategyRunListResponse> {
  const { data } = await apiClient.get<StrategyRunListResponse>(
    `/admin/strategies/${strategyKey}/runs`,
    { params },
  )
  return data
}

/** 查询已发布的运行批次（普通用户可访问，无需 admin 权限） */
export async function getPublishedRuns(
  strategyKey: string,
  params?: { limit?: number; offset?: number },
): Promise<StrategyRunListResponse> {
  const { data } = await apiClient.get<StrategyRunListResponse>(
    `/strategies/${strategyKey}/published-runs`,
    { params },
  )
  return data
}

/** 查询运行结果（分页+筛选+排序） */
export async function getStrategyRunResults(
  runId: string,
  params?: StrategyResultQueryParams,
): Promise<StrategyResultListResponse> {
  const { data } = await apiClient.get<StrategyResultListResponse>(
    `/strategy-runs/${runId}/results`,
    { params },
  )
  return data
}

/** 获取单个运行结果详情 */
export async function getStrategyRunResultDetail(
  runId: string,
  resultId: string,
): Promise<StrategyResult> {
  const { data } = await apiClient.get<StrategyResult>(
    `/strategy-runs/${runId}/results/${resultId}`,
  )
  return data
}

// ============================================================
// ===== Monitor States 端点 =====
// ============================================================

/** 查询某股票的所有监控策略状态 */
export async function getInstrumentMonitorStates(instrumentId: string): Promise<MonitorStateListResponse> {
  const { data } = await apiClient.get<MonitorStateListResponse>(
    `/instruments/${instrumentId}/monitor-states`,
  )
  return data
}

/** 查询某策略的所有股票状态（支持 version 过滤） */
export async function getStrategyMonitorStates(
  strategyKey: string,
  version?: string,
): Promise<MonitorStateListResponse> {
  const { data } = await apiClient.get<MonitorStateListResponse>(
    `/strategies/${strategyKey}/monitor-states`,
    { params: { version } },
  )
  return data
}

// ============================================================
// ===== Strategy Events 端点 =====
// ============================================================

/** 查询某股票的策略事件 */
export async function getInstrumentEvents(
  instrumentId: string,
  params?: StrategyEventQueryParams,
  options?: { signal?: AbortSignal },
): Promise<StrategyEventListResponse> {
  const { data } = await apiClient.get<StrategyEventListResponse>(
    `/instruments/${instrumentId}/events`,
    { params, signal: options?.signal },
  )
  return data
}

/** 查询某策略的事件（支持 version/event_type/时间范围过滤） */
export async function getStrategyEvents(
  strategyKey: string,
  params?: { version?: string } & StrategyEventQueryParams,
): Promise<StrategyEventListResponse> {
  const { data } = await apiClient.get<StrategyEventListResponse>(
    `/strategies/${strategyKey}/events`,
    { params },
  )
  return data
}

/** 查询事件详情（含 snapshot 快照） */
export async function getStrategyEventDetail(eventId: string): Promise<StrategyEventDetail> {
  const { data } = await apiClient.get<StrategyEventDetail>(`/strategy-events/${eventId}`)
  return data
}

// ============================================================
// ===== Notifications 端点 =====
// ============================================================

/** 获取用户消息列表（支持 unread_only 过滤） */
export async function getMessages(params?: {
  unread_only?: boolean
  limit?: number
  offset?: number
}): Promise<NotificationMessageListResponse> {
  const { data } = await apiClient.get<NotificationMessageListResponse>('/messages', {
    params,
    headers: getUserIdHeader(),
  })
  return data
}

/** 标记消息已读 */
export async function markMessageRead(messageId: string): Promise<NotificationMessage> {
  const { data } = await apiClient.post<NotificationMessage>(
    `/messages/${messageId}/read`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

// [Messages] - 描述: 未读消息计数，角标专用（避免 list 接口 total 字段语义混淆）
/** 获取当前用户未读消息总数（角标专用） */
export async function getUnreadCount(): Promise<UnreadCountResponse> {
  const { data } = await apiClient.get<UnreadCountResponse>('/messages/unread-count', {
    headers: getUserIdHeader(),
  })
  return data
}

// [Messages] - 描述: 批量标记当前用户所有未读消息为已读
/** 批量标记当前用户所有未读消息为已读 */
export async function readAllMessages(): Promise<ReadAllMessagesResponse> {
  const { data } = await apiClient.post<ReadAllMessagesResponse>(
    '/messages/read-all',
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

/** 获取用户通知渠道列表 */
export async function getNotificationChannels(): Promise<NotificationChannelListResponse> {
  const { data } = await apiClient.get<NotificationChannelListResponse>('/notification-channels', {
    headers: getUserIdHeader(),
  })
  return data
}

/** 创建通知渠道 */
export async function createNotificationChannel(payload: CreateChannelRequest): Promise<NotificationChannel> {
  const { data } = await apiClient.post<NotificationChannel>('/notification-channels', payload, {
    headers: getUserIdHeader(),
  })
  return data
}

/** 更新通知渠道 */
export async function updateNotificationChannel(
  channelId: string,
  data: { display_name?: string; target_config?: Record<string, unknown> },
): Promise<NotificationChannel> {
  const res = await apiClient.put<NotificationChannel>(
    `/notification-channels/${channelId}`,
    data,
  )
  return res.data
}

/** 删除通知渠道 */
export async function deleteNotificationChannel(
  channelId: string,
): Promise<NotificationChannel> {
  const res = await apiClient.delete<NotificationChannel>(
    `/notification-channels/${channelId}`,
  )
  return res.data
}

/** 验证通知渠道配置 */
export async function verifyNotificationChannel(channelId: string): Promise<NotificationChannel> {
  const { data } = await apiClient.post<NotificationChannel>(
    `/notification-channels/${channelId}/verify`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

/** 测试渠道投递（发送测试消息到渠道） */
export async function testNotificationChannel(channelId: string): Promise<ChannelTestResponse> {
  const { data } = await apiClient.post<ChannelTestResponse>(
    `/notification-channels/${channelId}/test`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

/** 最近事件实测（发送最近事件到渠道并返回诊断结果） */
export async function testNotificationChannelLatestEvent(channelId: string): Promise<ChannelLatestEventTestResponse> {
  const { data } = await apiClient.post<ChannelLatestEventTestResponse>(
    `/notification-channels/${channelId}/test-latest-event`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

// [StockDetailFeishu] - 描述: 创建异步投递任务（Outbox 链路），返回 test_run_id 供轮询
export async function sendStockDetailFeishu(
  instrumentId: string,
): Promise<StockDetailFeishuCreateResponse> {
  const { data } = await apiClient.post<StockDetailFeishuCreateResponse>(
    `/instruments/${instrumentId}/send-feishu`,
    {},
  )
  return data
}

// [StockDetailFeishu] - 描述: 轮询投递状态（card_status / image_status / overall_status）
export async function getStockDetailFeishuStatus(
  testRunId: string,
): Promise<StockDetailFeishuStatusResponse> {
  const { data } = await apiClient.get<StockDetailFeishuStatusResponse>(
    `/stock-detail-feishu/${testRunId}/status`,
  )
  return data
}

/** 消息预览 - 返回渠道无关 DTO + 站内渲染 + 飞书 card JSON */
export async function previewNotification(payload: NotificationPreviewRequest): Promise<NotificationPreviewResponse> {
  const { data } = await apiClient.post<NotificationPreviewResponse>('/notification-previews', payload)
  return data
}

// ============================================================
// ===== Admin Message Deliveries 端点 =====
// ============================================================

/** 查询消息投递记录（admin） */
export async function getMessageDeliveries(params?: {
  status?: DeliveryStatus
  limit?: number
  offset?: number
}): Promise<MessageDelivery[]> {
  const { data } = await apiClient.get<MessageDelivery[]>('/admin/message-deliveries', { params })
  return data
}

/** 立即重试指定消息投递记录（admin） */
export async function retryMessageDelivery(deliveryId: string): Promise<MessageDelivery> {
  const { data } = await apiClient.post<MessageDelivery>(`/admin/message-deliveries/${deliveryId}/retry`)
  return data
}

// ============================================================
// ===== Watchlist 端点 =====
// ============================================================

/** 查询当前用户的自选列表（仅 active=true） */
export async function getWatchlist(): Promise<WatchlistListResponse> {
  const { data } = await apiClient.get<WatchlistListResponse>('/watchlist')
  return data
}

/** 加入自选（instrument_id，user_id 由认证上下文注入） */
export async function addToWatchlist(payload: WatchlistAddRequest): Promise<WatchlistItem> {
  const { data } = await apiClient.post<WatchlistItem>('/watchlist', payload)
  return data
}

/** 移除自选（软删除：active=false + removed_at） */
export async function removeFromWatchlist(instrumentId: string): Promise<void> {
  await apiClient.delete(`/watchlist/${instrumentId}`)
}

/** 查询自选股+监控状态聚合数据 */
export async function getWatchlistMonitorStatus(): Promise<WatchlistMonitorStatusResponse> {
  const { data } = await apiClient.get<WatchlistMonitorStatusResponse>('/watchlist/monitor-status')
  return data
}

// ============================================================
// ===== Stock Memo 端点 =====
// ============================================================

/** 个股备忘录 */
export interface StockMemo {
  id: string
  user_id: string
  instrument_id: string
  content: string
  notify_feishu: boolean
  created_at: string
  updated_at: string
}

/** 备忘录 upsert 请求 */
export interface StockMemoUpsertRequest {
  content: string
  notify_feishu?: boolean
}

/** 切换飞书推送开关请求 */
export interface StockMemoNotifyToggleRequest {
  notify_feishu: boolean
}

/** 获取当前用户对指定股票的备忘录 */
export async function getStockMemo(instrumentId: string): Promise<StockMemo | null> {
  try {
    const { data } = await apiClient.get<StockMemo>(`/instruments/${instrumentId}/memo`)
    return data
  } catch (err: unknown) {
    if (err && typeof err === 'object' && 'response' in err) {
      const axiosErr = err as { response?: { status?: number } }
      if (axiosErr.response?.status === 404) return null
    }
    throw err
  }
}

/** 创建/更新备忘录（upsert） */
export async function upsertStockMemo(
  instrumentId: string,
  payload: StockMemoUpsertRequest,
): Promise<StockMemo> {
  const { data } = await apiClient.put<StockMemo>(`/instruments/${instrumentId}/memo`, payload)
  return data
}

/** 删除备忘录 */
export async function deleteStockMemo(instrumentId: string): Promise<void> {
  await apiClient.delete(`/instruments/${instrumentId}/memo`)
}

/** 切换飞书推送开关 */
export async function toggleMemoNotify(
  instrumentId: string,
  payload: StockMemoNotifyToggleRequest,
): Promise<StockMemo> {
  const { data } = await apiClient.patch<StockMemo>(
    `/instruments/${instrumentId}/memo/notify`,
    payload,
  )
  return data
}

// ============================================================
// ===== Bars 端点 =====
// ============================================================

/**
 * 查询指定标的的行情数据
 * 后端 bars router 自带 prefix="/api/v1"，完整路径为 /api/v1/instruments/{id}/bars
 * apiClient baseURL="/api" 会添加网关前缀，代理层处理后到达后端 /api/v1/instruments/{id}/bars
 */
export async function getBars(instrumentId: string, params?: BarQueryParams, options?: { signal?: AbortSignal }): Promise<BarListResponse> {
  const { data } = await apiClient.get<BarListResponse>(
    `/api/v1/instruments/${instrumentId}/bars`,
    { params, signal: options?.signal },
  )
  return data
}

// ============================================================
// ===== Quote 端点 =====
// ============================================================

/** 实时报价响应（可信来源与新鲜度） */
export interface QuoteResponse {
  instrument_id: string
  symbol: string
  name: string
  current_price: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  prev_close: number
  change_pct: number
  update_time: string
  source: 'pytdx' | 'daily_fallback'
  is_realtime: boolean
  freshness_seconds: number
  degraded: boolean
  degraded_reason: string | null
  amount?: number
}

/** 查询指定标的的实时报价（交易时段 pytdx 实时，非交易时段降级到数据库最新日线） */
export async function getQuote(instrumentId: string, options?: { signal?: AbortSignal }): Promise<QuoteResponse> {
  const { data } = await apiClient.get<QuoteResponse>(
    `/api/v1/instruments/${instrumentId}/quote`,
    { signal: options?.signal },
  )
  return data
}

// ============================================================
// ===== Indicators 端点 =====
// ============================================================

/** 策略图表图层定义（来自 manifest 的 chart_layers） */
export interface ChartLayer {
  strategy_id: string
  strategy_name: string
  layer_id: string
  layer_name: string
  renderer: string  // line | dsa_polyline | price_zone | marker | band
  pane: string      // price | volume | separate
  color?: string
  direction_colored?: boolean
  direction_up_color?: string
  direction_down_color?: string
  // [DSA 分段] - regime_field 指定 regime_id 字段名，前端按 regime 分段渲染（切换点不连接）
  regime_field?: string
  // [DSA 分段] - anchor_field 指定 anchor_time 字段名，前端在锚点 bar 绘制小圆点
  anchor_field?: string
  fields: string[]
  hover_fields: string[]
}

/** [DSA 分段] - 视觉段：方向 + 点序列，dsa_polyline 渲染器按段独立 beginPath/stroke */
export interface VisualSegment {
  direction: 1 | -1
  points: { time: string; value: number }[]
}

// [DSA 数据契约] - dsa_selector 策略的 data 结构（visual_segments 属于 data，不属于 ChartLayer）
//   与后端 manifest v1.4.1 对齐：visual_segments 由后端预计算，前端从 data.dsa_selector.visual_segments 读取
export interface DsaSelectorData {
  time: string[]
  visual_segments: VisualSegment[]
  dsa_vwap: (number | null)[]
  dsa_dir: number[]
  regime_id: number[]
  anchor_time: (string | null)[]
  pivot_type: (string | null)[]
  pivot_price: (number | null)[]
}

/** 指标查询参数 */
export interface IndicatorQueryParams {
  timeframe?: string  // 1d | 15m | 1h | 1w | 1mo
  adj?: string        // qfq | none
  bars?: number       // 返回最近 N 根 bar 的指标
  force_refresh?: number  // 1 时跳过 Redis 指标缓存强制实时计算（截图链路使用）
}

/** 指标 API 响应 */
export interface IndicatorResponse {
  layers: ChartLayer[]
  // [DSA 数据契约] - data 值支持 string（anchor_time 为 ISO 字符串|null 数组，其余字段为 number|null）
  //   dsa_selector 键的值为 DsaSelectorData（含 visual_segments），其他策略保持泛型数组结构
  data: Record<string, DsaSelectorData | Record<string, (number | string | null)[]>>
  errors?: Record<string, string>
  // [DSA 数据源校验] - source_bar_times 指标计算所基于的 K 线时间序列，前端与当前 K 线时间比对，不一致则跳过 DSA 渲染
  source_bar_times?: string[]
  // [DSA 数据源校验] - source_bar_hash K 线时间序列哈希，便于调试与后端联调定位数据源漂移
  source_bar_hash?: string
}

/**
 * 查询指定标的的所有策略图表指标
 * 后端 indicators router 自带 prefix="/api/v1"，完整路径为 /api/v1/instruments/{id}/indicators
 * apiClient baseURL="/api" 会添加网关前缀，代理层处理后到达后端 /api/v1/instruments/{id}/indicators
 */
export async function getIndicators(
  instrumentId: string,
  params?: IndicatorQueryParams,
  options?: { signal?: AbortSignal },
): Promise<IndicatorResponse> {
  const { data } = await apiClient.get<IndicatorResponse>(
    `/api/v1/instruments/${instrumentId}/indicators`,
    { params, signal: options?.signal },
  )
  return data
}

// ============================================================
// ===== Calendar 端点 =====
// ============================================================

/** 查询交易日历（支持日期范围与市场筛选） */
export async function getCalendar(params?: CalendarQueryParams): Promise<CalendarListResponse> {
  const { data } = await apiClient.get<CalendarListResponse>('/calendar', { params })
  return data
}

/** 查询指定日期是否为交易日（三级降级：DB -> Mootdx -> weekday） */
export async function isTradingDay(targetDate: string): Promise<TradingDayResponse> {
  const { data } = await apiClient.get<TradingDayResponse>(`/calendar/is-trading-day/${targetDate}`)
  return data
}

// ============================================================
// ===== Market Status 端点 =====
// ============================================================

/** 查询当前 A 股市场状态（交易日/交易时段/状态文本） */
export async function getMarketStatus(): Promise<MarketStatus> {
  const { data } = await apiClient.get<MarketStatus>('/market/status')
  return data
}

// ============================================================
// ===== Market Stocks 端点（PRD §8.1 行情列表）=====
// ============================================================

/** 行情列表单行（对齐后端 MarketStockRow） */
export interface MarketStockRow {
  instrument_id: string
  symbol: string
  name: string
  latest_price: number | null
  change_pct: number | null
  industry: string | null
  concepts: string[]
  dsa_state: string | null
  structure_state: string | null
  latest_event_title: string | null
  latest_event_time: string | null
  is_watchlisted: boolean
}

/** 行情列表分页响应（对齐后端 MarketStocksResponse） */
export interface MarketStocksResponse {
  items: MarketStockRow[]
  page: number
  page_size: number
  total: number
  price_as_of: string | null
  state_as_of: string | null
  boards_as_of: string | null
}

/** 行情列表查询参数 */
export interface MarketStocksQueryParams {
  scope: 'market' | 'watchlist'
  query?: string
  page?: number
  page_size?: number
  sort?: string
  industry?: string
  concept?: string
  state?: string
}

/**
 * 查询行情列表（服务端分页 + 批量加载，禁止 N+1）。
 * GET /market/stocks?scope&query&page&page_size&sort&industry&concept&state
 * 每行一次返回页面所需全部字段（价格/涨跌幅/DSA状态/事件/自选）。
 */
export async function getMarketStocks(
  params: MarketStocksQueryParams,
  options?: { signal?: AbortSignal },
): Promise<MarketStocksResponse> {
  const { data } = await apiClient.get<MarketStocksResponse>('/market/stocks', { params, signal: options?.signal })
  return data
}


// ============================================================
// ===== Admin Membership 端点 =====
// ============================================================

/** 获取所有 active 套餐定义（公开端点，无需登录） */
export async function getPlans(): Promise<PlanResponse[]> {
  const { data } = await publicApiClient.get<PlanResponse[]>('/plans')
  return data
}

/** 生成邀请码（单个/批量，明文仅生成时返回） */
export async function createInviteCodes(payload: InviteCodeCreateRequest): Promise<InviteCode[]> {
  const { data } = await apiClient.post<InviteCode[]>('/admin/invite-codes', payload)
  return data
}

/** 查询邀请码列表（支持状态筛选 + 分页） */
export async function getInviteCodes(params?: {
  status?: string
  limit?: number
  offset?: number
}): Promise<InviteCodeListResponse> {
  const { data } = await apiClient.get<InviteCodeListResponse>('/admin/invite-codes', { params })
  return data
}

/** 作废邀请码（仅 unused 状态可作废） */
export async function revokeInviteCode(inviteCodeId: string): Promise<InviteCodeListItem> {
  const { data } = await apiClient.post<InviteCodeListItem>(
    `/admin/invite-codes/${inviteCodeId}/revoke`,
  )
  return data
}

/** 查询订阅账户列表（含订阅状态/到期时间/剩余天数/续期次数；MemberListResponse 为 V1.6 API 遗留命名） */
export async function getMembers(params?: PaginationParams): Promise<MemberListResponse> {
  const { data } = await apiClient.get<MemberListResponse>('/admin/members', { params })
  return data
}

/** 查询用户兑换记录 */
export async function getMemberRedemptions(userId: string): Promise<InviteRedemption[]> {
  const { data } = await apiClient.get<InviteRedemption[]>(`/admin/members/${userId}/redemptions`)
  return data
}

/** 查询用户列表（admin） */
export async function getAdminUsers(params?: PaginationParams): Promise<UserListResponse> {
  const { data } = await apiClient.get<UserListResponse>('/admin/users', { params })
  return data
}

/** 查询用户详情（admin） */
export async function getAdminUser(userId: string): Promise<UserResponse> {
  const { data } = await apiClient.get<UserResponse>(`/admin/users/${userId}`)
  return data
}

/** 启用用户账户（admin） */
export async function adminEnableUser(userId: string): Promise<UserResponse> {
  const { data } = await apiClient.post<UserResponse>(`/admin/users/${userId}/enable`)
  return data
}

/** 停用用户账户（admin） */
export async function adminDisableUser(userId: string): Promise<UserResponse> {
  const { data } = await apiClient.post<UserResponse>(`/admin/users/${userId}/disable`)
  return data
}

/** 管理员授予用户套餐 */
export async function adminGrantSubscription(
  userId: string,
  payload: GrantSubscriptionRequest,
): Promise<SubscriptionResponse> {
  const { data } = await apiClient.post<SubscriptionResponse>(
    `/admin/users/${userId}/subscriptions/grant`,
    payload,
  )
  return data
}

/** 管理员续期用户套餐 */
export async function adminRenewSubscription(
  userId: string,
  payload: RenewSubscriptionRequest,
): Promise<SubscriptionRenewResponse> {
  const { data } = await apiClient.post<SubscriptionRenewResponse>(
    `/admin/users/${userId}/subscriptions/renew`,
    payload,
  )
  return data
}

/** 管理员撤销用户套餐 */
export async function adminRevokeSubscription(userId: string): Promise<SubscriptionResponse> {
  const { data } = await apiClient.post<SubscriptionResponse>(
    `/admin/users/${userId}/subscriptions/revoke`,
  )
  return data
}

/** 管理员变更用户套餐 */
export async function adminChangeSubscriptionPlan(
  userId: string,
  payload: ChangePlanRequest,
): Promise<SubscriptionResponse> {
  const { data } = await apiClient.post<SubscriptionResponse>(
    `/admin/users/${userId}/subscriptions/change-plan`,
    payload,
  )
  return data
}

/** 查询管理员审计日志 */
export async function getAdminAuditLogs(params?: {
  target_user_id?: string
  action?: string
  limit?: number
  offset?: number
}): Promise<AuditLogListResponse> {
  const { data } = await apiClient.get<AuditLogListResponse>('/admin/audit-logs', { params })
  return data
}

// ============================================================
// Beta Application 领域类型（Task 4 - 管理员内测申请后台）
// ============================================================

/** 内测申请状态枚举 */
export type BetaApplicationStatus = 'new' | 'contacted' | 'approved' | 'rejected' | 'converted'

/** 内测申请理由代码枚举 */
export type BetaApplicationReasonCode = 'busy' | 'too_many' | 'forget' | 'quant' | 'other'

/** 盯盘数量区间 */
export type WatchStockRange = '1-10' | '11-20' | '21-50' | '50+'

/** 内测申请列表项（含完整字段，仅 admin 可见） */
export interface BetaApplicationListItem {
  id: string
  wechat: string | null
  phone: string | null
  watch_stock_count: number
  reason_code: BetaApplicationReasonCode
  reason_other: string | null
  status: BetaApplicationStatus
  source: string | null
  admin_note: string | null
  handled_by: string | null
  handled_at: string | null
  submitted_at: string
  updated_at: string
  feishu_delivery_status: string | null
}

/** 内测申请列表响应 */
export interface BetaApplicationListResponse {
  items: BetaApplicationListItem[]
  total: number
  limit: number
  offset: number
}

/** 内测申请详情响应（含飞书投递信息） */
export interface BetaApplicationDetail {
  id: string
  wechat: string | null
  phone: string | null
  watch_stock_count: number
  reason_code: BetaApplicationReasonCode
  reason_other: string | null
  status: BetaApplicationStatus
  source: string | null
  admin_note: string | null
  handled_by: string | null
  handled_at: string | null
  submitted_at: string
  updated_at: string
  ip_hash: string
  feishu_delivery_status: string | null
  feishu_delivered_at: string | null
  feishu_last_error: string | null
}

/** 内测申请统计响应 */
export interface BetaApplicationStats {
  total: number
  today: number
  last_7_days: number
  last_30_days: number
  by_status: Record<string, number>
  avg_watch_stock_count: number
  by_reason: Record<string, number>
  by_watch_range: Record<string, number>
}

/** 内测申请状态更新请求 */
export interface BetaApplicationPatchRequest {
  status: BetaApplicationStatus
  admin_note?: string | null
}

/** 重发飞书响应 */
export interface RetryFeishuResponse {
  id: string
  outbox_id: string
  message: string
}

/** 内测申请列表查询参数 */
export interface BetaApplicationQueryParams {
  status?: BetaApplicationStatus
  reason_code?: BetaApplicationReasonCode
  watch_stock_range?: WatchStockRange
  date_from?: string
  date_to?: string
  keyword?: string
  limit?: number
  offset?: number
}

// ============================================================
// ===== Admin Beta Applications 端点 =====
// ============================================================

/** 查询内测申请列表（分页+筛选+搜索） */
export async function getAdminBetaApplications(
  params?: BetaApplicationQueryParams,
): Promise<BetaApplicationListResponse> {
  const { data } = await apiClient.get<BetaApplicationListResponse>('/admin/beta-applications', { params })
  return data
}

/** 获取内测申请统计数据 */
export async function getAdminBetaApplicationStats(): Promise<BetaApplicationStats> {
  const { data } = await apiClient.get<BetaApplicationStats>('/admin/beta-applications/stats')
  return data
}

/** 获取内测申请详情 */
export async function getAdminBetaApplicationDetail(appId: string): Promise<BetaApplicationDetail> {
  const { data } = await apiClient.get<BetaApplicationDetail>(`/admin/beta-applications/${appId}`)
  return data
}

/** 修改内测申请状态（status + admin_note） */
export async function updateAdminBetaApplication(
  appId: string,
  payload: BetaApplicationPatchRequest,
): Promise<BetaApplicationDetail> {
  const { data } = await apiClient.patch<BetaApplicationDetail>(`/admin/beta-applications/${appId}`, payload)
  return data
}

/** 重发内测申请飞书通知 */
export async function retryAdminBetaApplicationFeishu(appId: string): Promise<RetryFeishuResponse> {
  const { data } = await apiClient.post<RetryFeishuResponse>(`/admin/beta-applications/${appId}/retry-feishu`)
  return data
}

/**
 * 导出内测申请为 CSV（带筛选条件）。
 * 返回下载 URL（浏览器原生打开触发下载，避免 axios 解析 CSV 文本）。
 */
export function buildBetaApplicationExportUrl(params?: Omit<BetaApplicationQueryParams, 'limit' | 'offset'>): string {
  const searchParams = new URLSearchParams()
  if (params?.status) searchParams.set('status', params.status)
  if (params?.reason_code) searchParams.set('reason_code', params.reason_code)
  if (params?.watch_stock_range) searchParams.set('watch_stock_range', params.watch_stock_range)
  if (params?.date_from) searchParams.set('date_from', params.date_from)
  if (params?.date_to) searchParams.set('date_to', params.date_to)
  if (params?.keyword) searchParams.set('keyword', params.keyword)
  const qs = searchParams.toString()
  return qs ? `/admin/beta-applications/export?${qs}` : '/admin/beta-applications/export'
}

// ============================================================
// ===== Admin System Overview 端点 =====
// ============================================================

// [SystemOverview] - 行情数据新鲜度（6 项，Phase 9）
export interface BarsFreshness {
  latest_daily_trade_date: string | null
  daily_coverage: number | null
  latest_15m_bar_time: string | null
  latest_60m_bar_time: string | null
  last_success_job_id: string | null
  is_behind_latest_trade_date: boolean
}

// [SystemOverview] - 选股策略新鲜度（7 项，Phase 9）
export interface StrategyFreshness {
  latest_compute_trade_date: string | null
  latest_published_trade_date: string | null
  strategy_run_id: string | null
  status: string | null
  total_instruments: number | null
  failed_count: number | null
  published_at: string | null
}

// [SystemOverview] - 数据新鲜度子结构（行情 + 选股两区块，Phase 9）
export interface DataFreshness {
  bars: BarsFreshness
  strategy: StrategyFreshness
}

/** 系统概览响应 */
export interface SystemOverview {
  active_users: number
  distinct_monitored_instruments: number
  evaluations_last_minute: number
  evaluations_success_rate: number
  notification_delivery_rate: number
  queue_backlog: number
  failed_retry_count: number
  latest_selector_run: {
    id: string
    status: string
    trade_date: string | null
    started_at: string | null
    finished_at: string | null
    total_instruments: number | null
    succeeded_count: number | null
    failed_count: number | null
  } | null
  worker_health: string
  scheduler_health: string
  recent_scheduler_jobs: RecentSchedulerJobSummary[]
  recent_anomalies: unknown[]
  // [系统概览] - 描述: 后端统一计算的服务端时间/业务日期/市场时段
  server_time: string
  business_date: string
  market_session:
    | 'NON_TRADING_DAY'
    | 'PRE_OPEN'
    | 'MORNING_SESSION'
    | 'LUNCH_BREAK'
    | 'AFTERNOON_SESSION'
    | 'MARKET_CLOSED'
  // [系统概览] - 描述: 盘中监控运行态（后端权威判定，前端直出）
  monitor_runtime: {
    status:
      | 'RUNNING'
      | 'IDLE_EXPECTED'
      | 'SESSION_COMPLETED'
      | 'DELAYED'
      | 'FAILED'
      | 'WORKER_OFFLINE'
      | 'NOT_APPLICABLE'
    heartbeat_at: string | null
    heartbeat_age_seconds: number | null
    business_date: string
    session_label: 'morning' | 'afternoon' | null
    session_job_status: 'running' | 'succeeded' | 'failed' | null
    last_cycle_at: string | null
    last_source_bar_time: string | null
    evaluated_count: number
    failed_count: number
    freshness_seconds: number | null
  }
  // [系统概览] - 描述: 盘后流水线状态（后端权威判定，前端直出）
  after_close_pipeline: {
    status:
      | 'NOT_STARTED'
      | 'BARS_RUNNING'
      | 'BARS_FAILED'
      | 'WAITING_DSA'
      | 'DSA_QUEUED'
      | 'DSA_RUNNING'
      | 'DSA_COMPLETED'
      | 'PUBLISHED'
      | 'DSA_FAILED'
      | 'STALE'
    bars_job: {
      status: string | null
      started_at: string | null
      finished_at: string | null
      error_message: string | null
    } | null
    dsa_run: {
      id: string | null
      status: string | null
      run_type: string | null
      attempt_no: number | null
      trade_date: string | null
      failed_count: number | null
      succeeded_count: number | null
      error_code: string | null
      error_message: string | null
      failure_stage: string | null
    } | null
    // [SystemOverview] - WAITING_DSA 细分原因（7 种之一，仅 DSA 未 published 时填充）
    waiting_dsa_reason: string | null
    // [SystemOverview] - 原因对应的人类可读建议（与 waiting_dsa_reason 配对）
    waiting_dsa_suggestion: string | null
    // [SystemOverview] - 数据新鲜度子结构（行情 + 选股两区块，Phase 9）
    data_freshness: DataFreshness
    // [AfterClose] - 当日 after_close_orchestrator 任务 ID（供进入任务详情/断点继续/判断冲突任务）
    job_run_id: string | null
    // [AfterClose] - 编排状态（queued/refreshing_daily/.../succeeded/failed）
    orchestrator_status: string | null
    // [AfterClose] - Worker 最后心跳（ISO 字符串，判断 worker 是否在线）
    heartbeat_at: string | null
    // [AfterClose] - 租约到期时间（ISO 字符串）
    lease_expires_at: string | null
    // [AfterClose] - 最后成功步骤（断点检查点）
    last_completed_step: string | null
  }
}

/** 最近定时任务摘要（系统概览） */
export interface RecentSchedulerJobSummary {
  job_name: string
  status: string
  business_date: string | null
  started_at: string | null
  finished_at: string | null
  progress: number | null
  succeeded_count: number | null
  failed_count: number | null
  error_message: string | null
}

/** 定时任务运行记录项 */
export interface SchedulerJobRunItem {
  id: string
  job_name: string
  business_date: string | null
  scheduled_at: string | null
  started_at: string | null
  finished_at: string | null
  status: string
  heartbeat_at: string | null
  lease_expires_at: string | null
  // [AdminJobs] - 描述: 领取该任务的 Worker 实例标识（与 worker_heartbeats.instance_id 对应）
  worker_instance_id: string | null
  // [AdminJobs] - 描述: Worker 最后一次循环时间（长任务周期性心跳）
  last_cycle_at: string | null
  total_count: number | null
  succeeded_count: number | null
  failed_count: number | null
  progress: number | null
  error_code: string | null
  error_message: string | null
  metadata_json: string | null
  created_at: string
  updated_at: string
}

/** 定时任务运行记录列表响应 */
export interface SchedulerJobRunListResponse {
  items: SchedulerJobRunItem[]
  total: number
  limit: number
  offset: number
}

/** 查询定时任务运行记录（admin） */
export async function getSchedulerJobRuns(params?: {
  job_name?: string
  business_date?: string
  status?: string
  limit?: number
  offset?: number
}): Promise<SchedulerJobRunListResponse> {
  const { data } = await apiClient.get<SchedulerJobRunListResponse>('/admin/scheduler-job-runs', { params })
  return data
}

/** Worker 心跳记录项（admin 只读，health_state 由后端计算） */
export interface WorkerHeartbeatItem {
  worker_name: string
  instance_id: string
  started_at: string
  heartbeat_at: string
  status: string // running/idle/stopped
  current_job_id: string | null
  build_sha: string | null
  metadata_json: string | null
  updated_at: string
  // 后端计算字段（避免前端复制业务规则）
  heartbeat_age_seconds: number
  health_state: string // fresh/stale/stopped
}

/** Worker 心跳列表响应 */
export interface WorkerHeartbeatListResponse {
  items: WorkerHeartbeatItem[]
  total: number
  limit: number
  offset: number
}

/** 查询 Worker 心跳记录（admin 只读） */
export async function getWorkerHeartbeats(params?: {
  status?: string
  worker_name?: string
  limit?: number
  offset?: number
}): Promise<WorkerHeartbeatListResponse> {
  const { data } = await apiClient.get<WorkerHeartbeatListResponse>(
    '/admin/worker-heartbeats',
    { params },
  )
  return data
}

/** 获取系统概览（admin） */
export async function getAdminSystemOverview(): Promise<SystemOverview> {
  const { data } = await apiClient.get<SystemOverview>('/admin/system-overview')
  return data
}

// ============================================================
// ===== AfterClose & JobRunEvents 端点 =====
// ============================================================

/** 任务执行事件（时间线条目） */
export interface JobRunEvent {
  id: string
  job_run_id: string
  step: string
  level: 'info' | 'warn' | 'error'
  message: string
  payload: Record<string, unknown> | null
  created_at: string
}

/** 任务事件时间线响应 */
export interface JobRunEventListResponse {
  items: JobRunEvent[]
  total: number
}

/** 盘后编排状态响应（含编排状态 + DSA run 状态 + 事件时间线 + [Phase7] 详情） */
export interface AfterCloseRunStatusResponse {
  job_run_id: string
  job_name: string
  business_date: string | null
  status: string
  orchestrator_status: string
  trade_date: string | null
  dsa_run_id: string | null
  dsa_run_status: string | null
  started_at: string | null
  finished_at: string | null
  error_message: string | null
  // [Phase7] - 详情字段（管理后台展示）
  worker_instance_id: string | null
  heartbeat_at: string | null
  lease_expires_at: string | null
  last_completed_step: string | null
  // [AfterClose] - 跳过原因（如 NON_TRADING_DAY 非交易日），供前端展示提示
  skip_reason: string | null
  interrupt_reason: string | null
  is_retryable: boolean
  heartbeat_stale: boolean
  events: JobRunEvent[]
}

/** 盘后编排创建/重试响应 */
export interface AfterCloseRunCreateResponse {
  job_run_id: string
  status: string
  orchestrator_status: string
  trade_date: string
  message: string
}

/** 查询任务执行事件时间线（按 created_at 倒序） */
export async function getJobRunEvents(
  runId: string,
  limit: number = 100,
): Promise<JobRunEventListResponse> {
  const { data } = await apiClient.get<JobRunEventListResponse>(
    `/admin/job-runs/${runId}/events`,
    { params: { limit } },
  )
  return data
}

/** 查询盘后编排状态（含事件时间线 + DSA run 状态） */
export async function getAfterCloseRunStatus(
  runId: string,
): Promise<AfterCloseRunStatusResponse> {
  const { data } = await apiClient.get<AfterCloseRunStatusResponse>(
    `/admin/after-close-runs/${runId}`,
  )
  return data
}

/** 创建并异步执行盘后编排 */
export async function createAfterCloseRun(
  tradeDate: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    '/admin/after-close-runs',
    { trade_date: tradeDate },
  )
  return data
}

/** [Phase6] 仅重算今日 DSA（要求当日日线覆盖率 ≥ 90%） */
export async function createDsaOnlyRun(
  tradeDate: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    '/admin/after-close-runs/dsa-only',
    { trade_date: tradeDate },
  )
  return data
}

/** 重试失败的盘后编排任务 */
export async function retryAfterCloseRun(
  runId: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    `/admin/after-close-runs/${runId}/retry`,
  )
  return data
}

/** [Phase6] 从失败步骤继续（保留断点检查点，不重复拉行情） */
export async function resumeAfterCloseRun(
  runId: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    `/admin/after-close-runs/${runId}/resume`,
  )
  return data
}

/** 强制重新执行盘后编排（非 failed 状态也可触发） */
export async function forceAfterCloseRun(
  runId: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    `/admin/after-close-runs/${runId}/force`,
  )
  return data
}

// ============================================================
// ===== AfterClose Pipeline 聚合状态端点（/admin/after-close/pipeline/*）=====
// ============================================================
//
// 与 backend/app/schemas/after_close_pipeline.py 严格对齐：
// - AfterClosePipelineResponse / PipelineStep / AfterCloseRunSummary
// - FeatureSnapshotRunSummary / PipelineEventItem / PipelineRunItem
// - AfterClosePipelineRunListResponse / AfterClosePipelineRunRequest / AfterClosePipelineRunResponse
//
// 复用已有类型：
// - DataFreshness / BarsFreshness / StrategyFreshness（同文件上方）
// - JobRunEvent（与 PipelineEventItem 字段完全一致，事件时间线条目）

/** 盘后流水线单步骤状态（对齐后端 PipelineStep） */
export interface PipelineStep {
  step: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped'
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  counts: Record<string, unknown>
  error_message: string | null
}

/** after_close_orchestrator job_run 摘要（对齐后端 AfterCloseRunSummary） */
export interface AfterCloseRunSummary {
  job_run_id: string
  status: string
  orchestrator_status: string | null
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
  lease_expires_at: string | null
  last_completed_step: string | null
  error_message: string | null
  worker_instance_id: string | null
  trade_date: string | null
}

/** stock_feature_snapshot_run 摘要（对齐后端 FeatureSnapshotRunSummary） */
export interface FeatureSnapshotRunSummary {
  run_id: string
  run_type: string
  status: string
  scope: string
  snapshot_count: number | null
  failed_count: number | null
  skipped_count: number | null
  expected_count: number | null
  published_at: string | null
  started_at: string | null
  finished_at: string | null
}

/**
 * 盘后流水线聚合状态响应（对齐后端 AfterClosePipelineResponse）。
 *
 * overall_status 枚举：
 * - not_started：当日尚无 after_close_orchestrator 运行
 * - running：编排任务正在运行
 * - succeeded：编排成功且 watchlist_ready=true
 * - failed：编排失败
 * - blocked：收盘后超过 30 分钟仍无运行（含 has_backfill_full 时不计入 blocked）
 * - skipped：非交易日跳过
 *
 * watchlist_ready 严格判定：status='succeeded' AND published_at IS NOT NULL AND metadata_.scope='full'
 * （sample backfill 不计入 watchlist_ready，仅作为参考展示）
 */
export interface AfterClosePipelineResponse {
  trade_date: string
  market_session: string
  overall_status:
    | 'not_started'
    | 'running'
    | 'succeeded'
    | 'failed'
    | 'blocked'
    | 'skipped'
  watchlist_ready: boolean
  watchlist_reason: string
  has_backfill_full: boolean
  after_close_run: AfterCloseRunSummary | null
  steps: PipelineStep[]
  data_freshness: DataFreshness
  feature_snapshot_run: FeatureSnapshotRunSummary | null
  events: JobRunEvent[]
}

/** 最近运行列表单条记录（after_close_orchestrator 或 snapshot_run，对齐后端 PipelineRunItem） */
export interface PipelineRunItem {
  kind: 'after_close_orchestrator' | 'snapshot_run'
  job_run_id: string | null
  run_id: string | null
  trade_date: string | null
  status: string
  orchestrator_status: string | null
  run_type: string | null
  scope: string | null
  snapshot_count: number | null
  failed_count: number | null
  published_at: string | null
  started_at: string | null
  finished_at: string | null
  error_message: string | null
  worker_instance_id: string | null
  last_completed_step: string | null
}

/** 最近运行列表响应（对齐后端 AfterClosePipelineRunListResponse） */
export interface AfterClosePipelineRunListResponse {
  items: PipelineRunItem[]
  total: number
}

/** POST /admin/after-close/pipeline/run 请求体（对齐后端 AfterClosePipelineRunRequest） */
export interface AfterClosePipelineRunRequest {
  trade_date: string
}

/** POST /admin/after-close/pipeline/run 响应（对齐后端 AfterClosePipelineRunResponse）。
 * is_new=false 表示同 trade_date 已有 queued/running/succeeded 任务，返回已存在记录。 */
export interface AfterClosePipelineRunResponse {
  job_run_id: string
  trade_date: string
  status: string
  orchestrator_status: string | null
  is_new: boolean
}

/**
 * 查询最近交易日的盘后流水线聚合状态（admin）。
 * 后端自动定位最近交易日（含今日）：GET /admin/after-close/pipeline/latest
 */
export async function getAfterClosePipelineLatest(): Promise<AfterClosePipelineResponse> {
  const { data } = await apiClient.get<AfterClosePipelineResponse>(
    '/admin/after-close/pipeline/latest',
  )
  return data
}

/**
 * 查询指定交易日的盘后流水线聚合状态（admin）。
 * GET /admin/after-close/pipeline?trade_date=YYYY-MM-DD
 */
export async function getAfterClosePipelineByDate(
  tradeDate: string,
): Promise<AfterClosePipelineResponse> {
  const { data } = await apiClient.get<AfterClosePipelineResponse>(
    '/admin/after-close/pipeline',
    { params: { trade_date: tradeDate } },
  )
  return data
}

/**
 * 查询最近 N 次运行（after_close_orchestrator + snapshot_run 混合列表，admin）。
 * GET /admin/after-close/pipeline/runs?limit=20
 */
export async function getAfterClosePipelineRuns(
  limit: number = 20,
): Promise<AfterClosePipelineRunListResponse> {
  const { data } = await apiClient.get<AfterClosePipelineRunListResponse>(
    '/admin/after-close/pipeline/runs',
    { params: { limit } },
  )
  return data
}

/**
 * 管理员触发指定交易日的 after_close 编排任务（admin，幂等）。
 * POST /admin/after-close/pipeline/run
 * 同 trade_date 已有 queued/running/succeeded 时返回 existing，不重复创建。
 */
export async function createAfterClosePipelineRun(
  payload: AfterClosePipelineRunRequest,
): Promise<AfterClosePipelineRunResponse> {
  const { data } = await apiClient.post<AfterClosePipelineRunResponse>(
    '/admin/after-close/pipeline/run',
    payload,
  )
  return data
}

// ============================================================
// ===== Structural Factors 端点 =====
// ============================================================

/**
 * 结构状态因子查询参数
 * - primary_timeframe: 主周期（默认 1d）
 * - secondary_timeframe: 副周期（默认 15m）
 * - adj: 复权方式（默认 qfq）
 * - as_of: 截止时间（默认 latest）
 */
export interface StructuralFactorQueryParams {
  primary_timeframe?: string
  secondary_timeframe?: string
  adj?: string
  as_of?: string
}

/**
 * 结构状态因子响应
 * 包含双周期 (1d + 15m) 5 组结构因子 + relation + meta
 *
 * V1.8：relation 移除 momentum_alignment，新增客观关系字段
 * 前端只渲染后端 DTO，严禁重新计算。
 */
export interface StructuralFactorResponse {
  primary: Record<string, Record<string, unknown> | null>
  secondary: Record<string, Record<string, unknown> | null>
  relation: {
    primary_dir?: number | null
    secondary_dir?: number | null
    trend_alignment?: string | null
    primary_swing_position?: number | null
    secondary_swing_position?: number | null
    primary_slope_atr?: number | null
    secondary_slope_atr?: number | null
    secondary_vs_primary_position_delta?: number | null
    notes?: string[]
  }
  meta: {
    as_of: string
    primary_lookback_bars: number
    secondary_lookback_bars: number
    degraded_reasons: string[]
    warmup_notes: string[]
  }
}

/**
 * 查询指定标的的双周期结构状态因子
 * 后端 structural_factors router prefix="/api/v1/instruments"
 * 完整路径: /api/v1/instruments/{id}/structural-factors
 */
export async function getStructuralFactors(
  instrumentId: string,
  params?: StructuralFactorQueryParams,
): Promise<StructuralFactorResponse> {
  const { data } = await apiClient.get<StructuralFactorResponse>(
    `/api/v1/instruments/${instrumentId}/structural-factors`,
    { params },
  )
  return data
}

// ============================================================
// Temporal Features V1（时序特征：daily_context + m15_response + derived_relation）
// ============================================================

/**
 * Temporal Features 查询参数。
 * V1 仅支持 as_of=latest（默认），历史回测 as_of 待 V2。
 */
export interface TemporalFeaturesQueryParams {
  as_of?: 'latest'
  primary_timeframe?: string
  secondary_timeframe?: string
  adj?: 'qfq' | 'none'
}

/**
 * 时序特征响应 DTO。
 * 与后端 temporal_feature_service.compute_temporal_features 返回结构严格对齐。
 * 前端只渲染 DTO，严禁重新计算。
 */
export interface TemporalFeaturesResponse {
  daily_context: {
    daily_dsa_dir: number | null
    daily_dsa_segment_duration_percentile: number | null
    daily_dsa_slope_atr_per_bar: number | null
    daily_dsa_efficiency_0_1: number | null
    daily_price_position_in_swing_0_1: number | null
    daily_distance_to_swing_high_atr: number | null
    daily_distance_to_node_above_atr: number | null
    daily_sqzmom_change_since_segment_start: number | null
    daily_volume_percentile_change_since_segment_start: number | null
  }
  m15_response: {
    m15_price_position_in_swing_0_1: number | null
    m15_position_change_since_swing_anchor: number | null
    m15_distance_to_swing_high_atr: number | null
    m15_distance_to_swing_low_atr: number | null
    m15_sqzmom_change_since_swing_anchor: number | null
    m15_sqzmom_abs_percentile: number | null
    m15_sqz_off: boolean | null
    m15_bb_bandwidth_change_since_swing_anchor: number | null
    m15_volume_percentile_change_since_swing_anchor: number | null
  }
  derived_relation: {
    m15_position_relative_to_daily: number | null
    m15_response_direction_relative_to_daily: string | null
    m15_response_intensity: number | null
  }
  meta: {
    as_of: string
    primary_timeframe: string
    secondary_timeframe: string
    degraded_reasons: string[]
    warmup_notes: string[]
  }
}

/**
 * 查询指定标的的时序特征 V1。
 * 后端 temporal_features router prefix="/api/v1/instruments"
 * 完整路径: /api/v1/instruments/{id}/temporal-features
 * V1 仅支持 as_of=latest，as_of=其他值后端返回 400。
 */
export async function getTemporalFeatures(
  instrumentId: string,
  params?: TemporalFeaturesQueryParams,
): Promise<TemporalFeaturesResponse> {
  const { data } = await apiClient.get<TemporalFeaturesResponse>(
    `/api/v1/instruments/${instrumentId}/temporal-features`,
    { params },
  )
  return data
}

// ============================================================
// ===== Table View Presets 端点 =====
// ============================================================
// [Presets] - 描述: 用户表格视图配置 CRUD（/me/table-view-presets）
// config 仅保存 keyword/sort/filters/hiddenColumns/pageSize，禁止保存 selectedKeys/page/activeRunId/rows

/** 表格视图配置内容（与后端 TableViewPresetConfig 对齐，extra=forbid） */
export interface TableViewPresetConfig {
  keyword?: string | null
  sort?: { key: string; direction: 'asc' | 'desc' } | null
  filters?: Array<{ key: string; op: string; value: string | number; value2?: string | number }> | null
  hiddenColumns?: string[] | null
  pageSize?: number | null
}

/** preset 响应（与后端 TableViewPresetResponse 对齐） */
export interface TableViewPreset {
  id: string
  user_id: string
  table_id: string
  strategy_key: string | null
  name: string
  config: Record<string, unknown>
  is_default: boolean
  created_at: string
  updated_at: string
}

/** preset 列表响应 */
export interface TableViewPresetListResponse {
  items: TableViewPreset[]
  total: number
}

/** 创建 preset 请求 */
export interface TableViewPresetCreateRequest {
  table_id: string
  strategy_key?: string | null
  name: string
  config: Record<string, unknown>
  is_default?: boolean
}

/** 更新 preset 请求（至少传一个字段） */
export interface TableViewPresetPatchRequest {
  name?: string
  config?: Record<string, unknown>
  is_default?: boolean
}

/** 查询当前用户的 preset 列表（按 table_id + strategy_key 过滤） */
export async function getTableViewPresets(
  tableId: string,
  strategyKey?: string,
): Promise<TableViewPresetListResponse> {
  const params: Record<string, string> = { table_id: tableId }
  if (strategyKey) params.strategy_key = strategyKey
  const { data } = await apiClient.get<TableViewPresetListResponse>('/me/table-view-presets', { params })
  return data
}

/** 创建 preset */
export async function createTableViewPreset(
  payload: TableViewPresetCreateRequest,
): Promise<TableViewPreset> {
  const { data } = await apiClient.post<TableViewPreset>('/me/table-view-presets', payload)
  return data
}

/** 更新 preset（name/config/is_default，user_id/table_id/strategy_key 不可修改） */
export async function updateTableViewPreset(
  id: string,
  payload: TableViewPresetPatchRequest,
): Promise<TableViewPreset> {
  const { data } = await apiClient.patch<TableViewPreset>(`/me/table-view-presets/${id}`, payload)
  return data
}

/** 删除 preset */
export async function deleteTableViewPreset(id: string): Promise<void> {
  await apiClient.delete(`/me/table-view-presets/${id}`)
}

// ============================================================
// ===== Stock Context 端点（PRD V1.1 §7.3）=====
// ============================================================

/** StateValue - code 与 label 分离的稳定状态值 */
export interface StateValue {
  code: string | null
  label: string
  value: number | null
  unit: string | null
  timeframe: string
  /** 来源字段名（管理员可见，用户接口为 null） */
  sourceField: string | null
}

/** Evidence - 事件证据项 */
export interface StateEvidence {
  fieldName: string
  code: string
  currentValue: string | null
  previousValue: string | null
  unit: string | null
  timeframe: string
}

/** StockStructure - 结构状态 */
export interface StockStructure {
  price: StateValue
  consensusRelation: StateValue
}

/** StockMomentum - 动量状态 */
export interface StockMomentum {
  macd: StateValue
  sqzmom: StateValue
  temporal: StateValue[]
}

/** StockVolatility - 波动状态 */
export interface StockVolatility {
  bollPosition: StateValue
}

/** StockState - 统一状态向量 */
export interface StockState {
  symbol: string
  asOf: string
  sourceRunId: string
  version: string
  computedAt: string
  structure: StockStructure
  momentum: StockMomentum
  volatility: StockVolatility
  evidence: StateEvidence[]
  degradedReasons: string[]
}

/** StateEventDTO - 状态变化事件 */
export interface StateEventDTO {
  id: string
  symbol: string
  occurredAt: string
  eventType: string
  title: string
  description: string
  evidence: StateEvidence[]
  changedFields: string[]
  previousAsOf: string | null
  currentAsOf: string
  /** 稳定幂等键（仅数据库/管理员可见，用户接口为 null） */
  idempotencyKey: string | null
}

/** StockContext 响应 - 用户侧只读 */
export interface StockContextResponse {
  state: StockState | null
  events: StateEventDTO[]
  dataQuality: {
    hasSucceededRun: boolean
    hasSnapshot: boolean
    degradedReasons: string[]
    runTradeDate: string | null
    runPublishedAt: string | null
    instrumentStatus: string
  }
}

/** Admin StockDebug 响应 - 含原始 payload */
export interface AdminStockDebugResponse extends StockContextResponse {
  rawDebug?: {
    structuralPayload: Record<string, unknown>
    temporalPayload: Record<string, unknown>
    summaryPayload: Record<string, unknown>
    sourcePrimaryBarTime: string | null
    sourceSecondaryBarTime: string | null
    runId: string
    runType: string
    runStartedAt: string | null
    runFinishedAt: string | null
  }
}

/**
 * 获取个股状态上下文（只读，需登录 + 有效订阅）。
 * GET /api/v1/stocks/{symbol}/context?as_of=YYYY-MM-DD
 * 返回 StockState + 最近事件 + 数据质量。
 */
export async function getStockContext(
  symbol: string,
  params?: { as_of?: string },
  options?: { signal?: AbortSignal },
): Promise<StockContextResponse> {
  const { data } = await apiClient.get<StockContextResponse>(
    `/api/v1/stocks/${symbol}/context`,
    { params, signal: options?.signal },
  )
  return data
}

/**
 * 管理员个股调试接口（需管理员身份）。
 * GET /api/v1/admin/stocks/{symbol}/debug?as_of=YYYY-MM-DD
 * 返回 StockState + 事件 + 原始 payload。
 */
export async function getAdminStockDebug(
  symbol: string,
  params?: { as_of?: string },
  options?: { signal?: AbortSignal },
): Promise<AdminStockDebugResponse> {
  const { data } = await apiClient.get<AdminStockDebugResponse>(
    `/api/v1/admin/stocks/${symbol}/debug`,
    { params, signal: options?.signal },
  )
  return data
}
