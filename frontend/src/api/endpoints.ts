// API 端点定义层 - 类型安全的 API 访问函数
//
// 职责：
// 1. 定义所有 API 实体的 TypeScript 接口（与后端 Pydantic schema 对齐，字段使用 snake_case）
// 2. 导出按领域分组的 API 调用函数，每个函数使用 apiClient 发起请求并返回 response.data
//
// 约定：
// - 后端直接返回数据（FastAPI response_model 序列化），不包裹在 ApiResponse 中
// - 字段命名使用 snake_case 以匹配后端 JSON 格式（apiClient 无 camelCase 转换）
// - UUID / datetime / date 字段在 TS 中统一为 string（JSON 序列化后为字符串）
// - user_id 由认证上下文注入，不出现在请求体中（V1.1 安全约束）
// - 通知 API 当前使用 X-User-Id header（占位，后续接入 JWT）

import { apiClient } from './client'
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

/** 登录响应（含 token + 会员到期标记） */
export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  membership_expired: boolean
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

/** 会员状态响应 */
export interface MembershipResponse {
  status: string
  started_at: string
  expires_at: string
  remaining_days: number
  renewal_count: number
}

/** 注册成功响应 */
export interface RegisterSuccessResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  membership_started_at: string
  membership_expires_at: string
}

/** 续期成功响应 */
export interface RenewSuccessResponse {
  membership_status: string
  started_at: string
  old_expires_at: string | null
  new_expires_at: string
  remaining_days: number
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
export interface StrategyResult {
  id: string
  run_id: string
  strategy_version_id: string
  instrument_id: string
  instrument_symbol?: string
  instrument_name?: string
  instrument_market?: string
  trade_date: string
  payload: Record<string, unknown>
  created_at: string
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

/** 消息投递记录 */
export interface MessageDelivery {
  id: string
  channel_id: string
  notification_message_id: string
  adapter_type: string
  display_name: string
  status: 'pending' | 'success' | 'failed' | 'retrying'
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

/** 自选股+监控状态聚合项 */
export interface WatchlistMonitorStatusItem {
  watchlist_item_id: string
  instrument_id: string
  symbol: string
  name: string
  market: string
  watchlist_created_at: string
  monitor_status: 'PRE_MARKET' | 'TRADING' | 'LUNCH_BREAK' | 'AFTER_MARKET' | 'NON_TRADING_DAY' | 'WAITING_FIRST_RUN' | 'SUCCEEDED' | 'FAILED' | 'STALE'
  evaluation_status: string | null
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

/** 行情列表响应（服务端分页） */
export interface BarListResponse {
  items: Bar[]
  total: number
  page: number
  page_size: number
  timeframe: string
  adj: string
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

/** 邀请码响应（含明文，仅生成时返回） */
export interface InviteCode {
  id: string
  code: string
  grant_days: number
  note: string | null
  created_at: string
}

/** 邀请码列表项（不含明文） */
export interface InviteCodeListItem {
  id: string
  status: string
  grant_days: number
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

/** 会员账户列表项 */
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

/** 市场状态 */
export interface MarketStatus {
  is_trading_day: boolean
  is_trading_hours: boolean
  status_text: string  // "交易中" / "已收盘" / "休市" / "盘前"
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

/** 邀请码生成请求 */
export interface InviteCodeCreateRequest {
  count?: number
  note?: string
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

/** 用户登录 - 返回 access + refresh token + 会员到期标记 */
export async function login(email: string, password: string): Promise<LoginResponse> {
  const { data } = await apiClient.post<LoginResponse>('/auth/login', { email, password })
  return data
}

/** 邀请码注册 - 原子操作创建账户 + 开通 30 天会员 */
export async function register(payload: RegisterRequest): Promise<RegisterSuccessResponse> {
  const { data } = await apiClient.post<RegisterSuccessResponse>('/auth/register', payload)
  return data
}

/** 邀请码续期 - 未到期顺延 / 已到期从当天计算 */
export async function renew(inviteCode: string): Promise<RenewSuccessResponse> {
  const { data } = await apiClient.post<RenewSuccessResponse>('/auth/renew', { invite_code: inviteCode })
  return data
}

/** 使用 refresh token 刷新，返回新的 access + refresh token */
export async function refreshToken(refreshToken: string): Promise<TokenResponse> {
  const { data } = await apiClient.post<TokenResponse>('/auth/refresh', null, {
    params: { refresh_token: refreshToken },
  })
  return data
}

/** 获取当前用户信息（含角色列表） */
export async function getMe(): Promise<UserResponse> {
  const { data } = await apiClient.get<UserResponse>('/me')
  return data
}

/** 获取当前用户会员状态 */
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
): Promise<StrategyEventListResponse> {
  const { data } = await apiClient.get<StrategyEventListResponse>(
    `/instruments/${instrumentId}/events`,
    { params },
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
  status?: 'pending' | 'success' | 'failed' | 'retrying'
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
export async function getBars(instrumentId: string, params?: BarQueryParams): Promise<BarListResponse> {
  const { data } = await apiClient.get<BarListResponse>(
    `/api/v1/instruments/${instrumentId}/bars`,
    { params },
  )
  return data
}

// ============================================================
// ===== Quote 端点 =====
// ============================================================

/** 实时报价响应（pytdx 实时 / 数据库降级） */
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
  is_realtime: boolean
  amount?: number
}

/** 查询指定标的的实时报价（交易时段 pytdx 实时，非交易时段降级到数据库最新日线） */
export async function getQuote(instrumentId: string): Promise<QuoteResponse> {
  const { data } = await apiClient.get<QuoteResponse>(
    `/api/v1/instruments/${instrumentId}/quote`,
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
  renderer: string  // line | price_zone | marker | band
  pane: string      // price | volume | separate
  color?: string
  direction_colored?: boolean
  direction_up_color?: string
  direction_down_color?: string
  fields: string[]
  hover_fields: string[]
}

/** 指标查询参数 */
export interface IndicatorQueryParams {
  timeframe?: string  // 1d | 15m | 1h | 1w | 1mo
  adj?: string        // qfq | none
  bars?: number       // 返回最近 N 根 bar 的指标
}

/** 指标 API 响应 */
export interface IndicatorResponse {
  layers: ChartLayer[]
  data: Record<string, Record<string, (number | null)[]>>
  errors?: Record<string, string>
}

/**
 * 查询指定标的的所有策略图表指标
 * 后端 indicators router 自带 prefix="/api/v1"，完整路径为 /api/v1/instruments/{id}/indicators
 * apiClient baseURL="/api" 会添加网关前缀，代理层处理后到达后端 /api/v1/instruments/{id}/indicators
 */
export async function getIndicators(
  instrumentId: string,
  params?: IndicatorQueryParams,
): Promise<IndicatorResponse> {
  const { data } = await apiClient.get<IndicatorResponse>(
    `/api/v1/instruments/${instrumentId}/indicators`,
    { params },
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

/** 查询指定日期是否为交易日（三级降级：DB -> Tushare -> weekday） */
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
// ===== Admin Membership 端点 =====
// ============================================================

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

/** 查询会员账户列表（含会员状态/到期时间/剩余天数/续期次数） */
export async function getMembers(params?: PaginationParams): Promise<MemberListResponse> {
  const { data } = await apiClient.get<MemberListResponse>('/admin/members', { params })
  return data
}

/** 查询用户兑换记录 */
export async function getMemberRedemptions(userId: string): Promise<InviteRedemption[]> {
  const { data } = await apiClient.get<InviteRedemption[]>(`/admin/members/${userId}/redemptions`)
  return data
}

// ============================================================
// ===== Admin System Overview 端点 =====
// ============================================================

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

/** 获取系统概览（admin） */
export async function getAdminSystemOverview(): Promise<SystemOverview> {
  const { data } = await apiClient.get<SystemOverview>('/admin/system-overview')
  return data
}
