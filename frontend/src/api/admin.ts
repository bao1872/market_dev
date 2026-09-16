// Admin 领域 API owner
//
// [S3-D] 由 endpoints.ts 迁出。endpoints.ts 仅保留兼容 barrel 重新导出，caller import 零改动。
//
// 边界：所有 /v1/admin/* 端点（admin strategy / users / membership / capability / audit /
// beta / notification-channels(代管) / message-deliveries / invite-codes / visitors /
// system-overview / scheduler / worker / readiness / boards-compute）。
// AfterClose 单独在 ./adminAfterClose（对齐 S5 AfterClose 拆核）。
// AdminStockDebug（耦合 AtomicFacts 上下文类型）暂留 endpoints.ts（见 residual）。
//
// 共享类型（NotificationChannel*/DeliveryStatus/MessageDelivery/PlanCode/PaginationParams）
// 仍由 endpoints.ts 定义，此处 type-only 引用（不构成运行时循环）。

import { apiClient } from './client'
import type { StrategyVersion, StrategyRun, StrategyRunListResponse } from './strategy'
import type { UserResponse } from './auth'
import type {
  NotificationChannel,
  NotificationChannelListResponse,
  ChannelTestResponse,
  CreateChannelRequest,
  UpdateChannelRequest,
  DeliveryStatus,
  MessageDelivery,
} from './notification'
import type { PlanCode, PaginationParams } from './endpoints'
import type { AtomicFactsContextResponse } from './stockData'

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

// [S3-A] MembershipResponse / RegisterSuccessResponse / RenewSuccessResponse
// 已迁至 ./auth（见顶部兼容 re-export）。

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

export interface InviteCode {
  id: string
  code: string
  grant_days: number
  plan_code: PlanCode | null
  monitor_limit: number | null

  note: string | null
  created_at: string
  /**
   * [Gate2 PRD60 PA-20] capability 组合。后端始终返回该字段：
   * null 表示旧模式邀请码（按 plan_code 兑换），不是后端漏传。
   */
  capabilities: CapabilityGrantInput[] | null
}

/** 邀请码列表项（不含明文）+ 套餐快照 */
export interface InviteCodeListItem {
  id: string
  status: string
  grant_days: number
  plan_code: PlanCode | null
  monitor_limit: number | null

  note: string | null
  created_by: string
  created_at: string
  used_by: string | null
  used_at: string | null
  usage_type: string | null
  /**
   * [Gate2 PRD60 PA-20] capability 组合。后端始终返回该字段：
   * null 表示旧模式邀请码（按 plan_code 兑换），不是后端漏传。
   */
  capabilities: CapabilityGrantInput[] | null
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
  /** [Gate2 PRD60] 三类独立权限状态（与 AccessContext.capabilities 对齐） */
  capabilities?: Record<string, UserCapabilityInfo>
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

export interface TriggerRunRequest {
  trade_date?: string
  instrument_ids?: string[]
  run_type?: string
}

// [S3-B] WatchlistAddRequest 已迁至 ./watchlist（见顶部兼容 re-export）。

/** 创建通知渠道请求 */

export interface InviteCodeCreateRequest {
  count?: number
  note?: string
  plan_code?: PlanCode
  grant_days?: number
  /** [Gate2 PRD60 PA-20] capability 组合（新模式）；提供时优先于 plan_code */
  capabilities?: CapabilityGrantInput[]
}

/** [Gate2 PRD60 PA-20] 单个 capability 授权配置（与 backend CapabilityGrant 对齐） */
export interface CapabilityGrantInput {
  capability: 'self_selection' | 'market_data' | 'research_replay'
  days: number  // 有效天数（1-365，1 = 1 天）
  watchlist_limit?: number  // 仅 self_selection 必填（1-500）
}

/** [Gate2 PRD60] 管理员直接授予用户 capability 请求 */
export interface GrantCapabilityRequest {
  capability: 'self_selection' | 'market_data' | 'research_replay'
  days: number  // 有效天数（1 = 1 天）
  watchlist_limit?: number  // 仅 self_selection 必填
}

/** [Gate2 PRD60] 用户 capability 信息（与 backend AccessContext.capabilities 对齐） */
export interface UserCapabilityInfo {
  active: boolean
  expires_at: string | null
  watchlist_limit: number | null
}

/** 管理员授予用户套餐请求 */
export interface GrantSubscriptionRequest {
  plan_code: PlanCode
  grant_days: number
}

/** 管理员续期用户套餐请求 */
export interface RenewSubscriptionRequest {
  grant_days: number
}

/** 管理员变更用户套餐请求 */
export interface ChangePlanRequest {
  plan_code: PlanCode
  grant_days: number
}

/** 管理员变更用户账户状态请求 */
export interface ChangeAccountStatusRequest {
  status: 'active' | 'disabled'
}


// ============================================================
// 查询参数类型
// ============================================================

/** 股票列表查询参数 */

export async function createStrategy(
  manifest: Record<string, unknown>,
  strategySchema?: Record<string, unknown>,
): Promise<StrategyVersion> {
  const { data } = await apiClient.post<StrategyVersion>('/v1/admin/strategies', {
    manifest,
    schema: strategySchema,
  })
  return data
}

/** 发布策略版本（admin）- draft -> released */
export async function releaseStrategyVersion(strategyKey: string, version: string): Promise<StrategyVersion> {
  const { data } = await apiClient.post<StrategyVersion>(
    `/v1/admin/strategies/${strategyKey}/versions/${version}/release`,
  )
  return data
}

/** 归档策略版本（admin）- released -> archived */
export async function archiveStrategyVersion(strategyKey: string, version: string): Promise<StrategyVersion> {
  const { data } = await apiClient.post<StrategyVersion>(
    `/v1/admin/strategies/${strategyKey}/versions/${version}/archive`,
  )
  return data
}

// ============================================================
// ===== Strategy Runs 端点 =====
// ============================================================

/** 触发策略运行（admin） */
export async function triggerStrategyRun(strategyKey: string, payload: TriggerRunRequest): Promise<StrategyRun> {
  const { data } = await apiClient.post<StrategyRun>(
    `/v1/admin/strategies/${strategyKey}/run`,
    payload,
  )
  return data
}

// [S3-C] getStrategyRuns 已迁至 ./strategy（见顶部兼容 re-export）。

/** 查询策略运行历史（admin，/admin 前缀路径） */
export async function getAdminStrategyRuns(
  strategyKey: string,
  params?: { status?: string; limit?: number; offset?: number },
): Promise<StrategyRunListResponse> {
  const { data } = await apiClient.get<StrategyRunListResponse>(
    `/v1/admin/strategies/${strategyKey}/runs`,
    { params },
  )
  return data
}

// [S3-C] getPublishedRuns / getStrategyRunResults / getStrategyRunResultDetail /
// getInstrumentMonitorStates / getStrategyMonitorStates / getInstrumentEvents /
// getStrategyEvents / getStrategyEventDetail 已迁至 ./strategy（见顶部兼容 re-export）。

// ============================================================
// ===== Notifications 端点 =====
// ============================================================

/** 获取用户消息列表（支持 unread_only 过滤） */

// ============================================================
// ===== Admin Message Deliveries 端点 =====
// ============================================================

/** 查询消息投递记录（admin） */
export async function getMessageDeliveries(params?: {
  status?: DeliveryStatus
  limit?: number
  offset?: number
}): Promise<MessageDelivery[]> {
  const { data } = await apiClient.get<MessageDelivery[]>('/v1/admin/message-deliveries', { params })
  return data
}

/** 立即重试指定消息投递记录（admin） */
export async function retryMessageDelivery(deliveryId: string): Promise<MessageDelivery> {
  const { data } = await apiClient.post<MessageDelivery>(`/v1/admin/message-deliveries/${deliveryId}/retry`)
  return data
}


/** 生成邀请码（单个/批量，明文仅生成时返回） */
export async function createInviteCodes(payload: InviteCodeCreateRequest): Promise<InviteCode[]> {
  const { data } = await apiClient.post<InviteCode[]>('/v1/admin/invite-codes', payload)
  return data
}

/** 查询邀请码列表（支持状态筛选 + 分页） */
export async function getInviteCodes(params?: {
  status?: string
  limit?: number
  offset?: number
}): Promise<InviteCodeListResponse> {
  const { data } = await apiClient.get<InviteCodeListResponse>('/v1/admin/invite-codes', { params })
  return data
}

/** 作废邀请码（仅 unused 状态可作废） */
export async function revokeInviteCode(inviteCodeId: string): Promise<InviteCodeListItem> {
  const { data } = await apiClient.post<InviteCodeListItem>(
    `/v1/admin/invite-codes/${inviteCodeId}/revoke`,
  )
  return data
}

// ===== 管理员代管用户通知渠道（薄包装 notification_service，target_config 已脱敏） =====

/** 查询指定用户的通知渠道列表（admin） */
export async function adminListUserChannels(
  userId: string,
): Promise<NotificationChannelListResponse> {
  const { data } = await apiClient.get<NotificationChannelListResponse>(
    `/v1/admin/users/${userId}/notification-channels`,
  )
  return data
}

/** 为指定用户创建通知渠道（admin） */
export async function adminCreateUserChannel(
  userId: string,
  payload: CreateChannelRequest,
): Promise<NotificationChannel> {
  const { data } = await apiClient.post<NotificationChannel>(
    `/v1/admin/users/${userId}/notification-channels`,
    payload,
  )
  return data
}

/** 更新指定用户的通知渠道（admin） */
export async function adminUpdateUserChannel(
  userId: string,
  channelId: string,
  payload: UpdateChannelRequest,
): Promise<NotificationChannel> {
  const { data } = await apiClient.put<NotificationChannel>(
    `/v1/admin/users/${userId}/notification-channels/${channelId}`,
    payload,
  )
  return data
}

/** 删除指定用户的通知渠道（admin，软删除） */
export async function adminDeleteUserChannel(
  userId: string,
  channelId: string,
): Promise<NotificationChannel> {
  const { data } = await apiClient.delete<NotificationChannel>(
    `/v1/admin/users/${userId}/notification-channels/${channelId}`,
  )
  return data
}

/** 验证指定用户的通知渠道（admin） */
export async function adminVerifyUserChannel(
  userId: string,
  channelId: string,
): Promise<NotificationChannel> {
  const { data } = await apiClient.post<NotificationChannel>(
    `/v1/admin/users/${userId}/notification-channels/${channelId}/verify`,
  )
  return data
}

/** 对指定用户的通知渠道发送测试消息（admin） */
export async function adminTestUserChannel(
  userId: string,
  channelId: string,
): Promise<ChannelTestResponse> {
  const { data } = await apiClient.post<ChannelTestResponse>(
    `/v1/admin/users/${userId}/notification-channels/${channelId}/test`,
  )
  return data
}

/** 查询订阅账户列表（含订阅状态/到期时间/剩余天数/续期次数；MemberListResponse 为 V1.6 API 遗留命名） */
export async function getMembers(params?: PaginationParams): Promise<MemberListResponse> {
  const { data } = await apiClient.get<MemberListResponse>('/v1/admin/members', { params })
  return data
}

/** 查询用户兑换记录 */
export async function getMemberRedemptions(userId: string): Promise<InviteRedemption[]> {
  const { data } = await apiClient.get<InviteRedemption[]>(`/v1/admin/members/${userId}/redemptions`)
  return data
}

/** 查询用户列表（admin） */
export async function getAdminUsers(params?: PaginationParams): Promise<UserListResponse> {
  const { data } = await apiClient.get<UserListResponse>('/v1/admin/users', { params })
  return data
}

/** 查询用户详情（admin） */
export async function getAdminUser(userId: string): Promise<UserResponse> {
  const { data } = await apiClient.get<UserResponse>(`/v1/admin/users/${userId}`)
  return data
}

/** 启用用户账户（admin） */
export async function adminEnableUser(userId: string): Promise<UserResponse> {
  const { data } = await apiClient.post<UserResponse>(`/v1/admin/users/${userId}/enable`)
  return data
}

/** 停用用户账户（admin） */
export async function adminDisableUser(userId: string): Promise<UserResponse> {
  const { data } = await apiClient.post<UserResponse>(`/v1/admin/users/${userId}/disable`)
  return data
}

/** 管理员重置用户密码（设置新密码，不读取旧密码） */
export async function adminResetUserPassword(
  userId: string,
  payload: { new_password: string },
): Promise<{ user_id: string; message: string }> {
  const { data } = await apiClient.post<{ user_id: string; message: string }>(
    `/v1/admin/users/${userId}/reset-password`,
    payload,
  )
  return data
}

/** 管理员授予用户套餐 */
export async function adminGrantSubscription(
  userId: string,
  payload: GrantSubscriptionRequest,
): Promise<SubscriptionResponse> {
  const { data } = await apiClient.post<SubscriptionResponse>(
    `/v1/admin/users/${userId}/subscriptions/grant`,
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
    `/v1/admin/users/${userId}/subscriptions/renew`,
    payload,
  )
  return data
}

/** 管理员撤销用户套餐 */
export async function adminRevokeSubscription(userId: string): Promise<SubscriptionResponse> {
  const { data } = await apiClient.post<SubscriptionResponse>(
    `/v1/admin/users/${userId}/subscriptions/revoke`,
  )
  return data
}

/** 管理员变更用户套餐 */
export async function adminChangeSubscriptionPlan(
  userId: string,
  payload: ChangePlanRequest,
): Promise<SubscriptionResponse> {
  const { data } = await apiClient.post<SubscriptionResponse>(
    `/v1/admin/users/${userId}/subscriptions/change-plan`,
    payload,
  )
  return data
}

/** [Gate2 PRD60] 用户 capabilities 响应 */
export interface UserCapabilitiesResponse {
  user_id: string
  capabilities: Record<string, UserCapabilityInfo>
}

/** [Gate2 PRD60] 查询用户 capabilities（三类独立权限状态） */
export async function getUserCapabilities(
  userId: string,
): Promise<UserCapabilitiesResponse> {
  const { data } = await apiClient.get<UserCapabilitiesResponse>(
    `/v1/admin/users/${userId}/capabilities`,
  )
  return data
}

/** [Gate2 PRD60 PA-20] 管理员直接授予/修改用户 capability */
export async function adminGrantCapability(
  userId: string,
  payload: GrantCapabilityRequest,
): Promise<UserCapabilitiesResponse> {
  const { data } = await apiClient.post<UserCapabilitiesResponse>(
    `/v1/admin/users/${userId}/capabilities`,
    payload,
  )
  return data
}

/** [Gate2 PRD60 PA-20] 管理员撤销用户 capability */
export async function adminRevokeCapability(
  userId: string,
  capability: 'self_selection' | 'market_data' | 'research_replay',
): Promise<UserCapabilitiesResponse> {
  const { data } = await apiClient.delete<UserCapabilitiesResponse>(
    `/v1/admin/users/${userId}/capabilities/${capability}`,
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
  const { data } = await apiClient.get<AuditLogListResponse>('/v1/admin/audit-logs', { params })
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
  const { data } = await apiClient.get<BetaApplicationListResponse>('/v1/admin/beta-applications', { params })
  return data
}

/** 获取内测申请统计数据 */
export async function getAdminBetaApplicationStats(): Promise<BetaApplicationStats> {
  const { data } = await apiClient.get<BetaApplicationStats>('/v1/admin/beta-applications/stats')
  return data
}

/** 获取内测申请详情 */
export async function getAdminBetaApplicationDetail(appId: string): Promise<BetaApplicationDetail> {
  const { data } = await apiClient.get<BetaApplicationDetail>(`/v1/admin/beta-applications/${appId}`)
  return data
}

/** 修改内测申请状态（status + admin_note） */
export async function updateAdminBetaApplication(
  appId: string,
  payload: BetaApplicationPatchRequest,
): Promise<BetaApplicationDetail> {
  const { data } = await apiClient.patch<BetaApplicationDetail>(`/v1/admin/beta-applications/${appId}`, payload)
  return data
}

/** 重发内测申请飞书通知 */
export async function retryAdminBetaApplicationFeishu(appId: string): Promise<RetryFeishuResponse> {
  const { data } = await apiClient.post<RetryFeishuResponse>(`/v1/admin/beta-applications/${appId}/retry-feishu`)
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
  return qs ? `/v1/admin/beta-applications/export?${qs}` : '/v1/admin/beta-applications/export'
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
    // [Phase8A] - 计划启动时间（16:00 调度创建时写入）
    scheduled_at: string | null
    // [Phase8A] - 实际启动时间（Worker 领取时写入）
    started_at: string | null
    // [Phase8A] - 当前执行步骤（与 orchestrator_status 同义，便于前端直接读取）
    current_step: string | null
  }
  // [PRD §8.1/8.2] 统一数据生产与发布状态摘要（后端直出，前端只展示不判定）
  summary: {
    overall_status: 'ok' | 'attention' | 'blocked'
    quality_gate: 'not_applicable' | 'passed' | 'failed' | 'pending'
    publication_status: {
      status: 'published' | 'unpublished' | 'pending' | 'failed'
      latest_published_trade_date: string | null
      latest_compute_trade_date: string | null
      is_current: boolean
      quality_gate_passed: boolean | null
    }
    today_must_process: Array<{
      key: string
      error_code: string
      severity: 'error' | 'warning' | 'info'
      message: string
      retryable: boolean
      resumable: boolean
      recommended_action: string
      target_route: string | null
    }>
    production_chain: Array<{
      key: string
      label: string
      status: 'ok' | 'pending' | 'running' | 'failed' | 'stale' | 'attention' | 'not_applicable'
      detail: string
      trade_date: string | null
      run_id: string | null
      quality_gate: 'not_applicable' | 'passed' | 'failed' | 'pending'
      publication_status: 'published' | 'unpublished' | 'pending' | 'failed' | 'not_applicable'
      blocking_reason: string | null
      recommended_action: string | null
    }>
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
  /** 服务端诊断字段；前端不得根据时间戳自行推导 */
  processed_count?: number | null
  last_progress_at?: string | null
  heartbeat_age_seconds?: number | null
  lease_remaining_seconds?: number | null
  elapsed_seconds?: number | null
  retry_count?: number | null
  max_retries?: number | null
  retryable?: boolean
  publication_status?: string | null
  partial_success?: boolean
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
  const { data } = await apiClient.get<SchedulerJobRunListResponse>('/v1/admin/scheduler-job-runs', { params })
  return data
}

/** Worker 心跳记录项（admin 只读，health_state 由后端计算） */
export interface WorkerHeartbeatItem {
  worker_name: string
  instance_id: string
  started_at: string
  heartbeat_at: string
  status: string // running/idle/stopped
  stopped_at: string | null // Gate4: Worker 停止时间（null=运行中或历史记录无此字段）
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
    '/v1/admin/worker-heartbeats',
    { params },
  )
  return data
}

/** 获取系统概览（admin） */
export async function getAdminSystemOverview(): Promise<SystemOverview> {
  const { data } = await apiClient.get<SystemOverview>('/v1/admin/system-overview')
  return data
}

// ============================================================
// ===== [Commit G] ProductReadiness 就绪状态 + 治理报告 =====
// ============================================================

/** 单个产品的就绪状态（九节点之一） */
export interface ProductReadinessItem {
  product: string
  readiness: string
  freshness: string
  isMandatory: boolean
  isTerminal: boolean
  isConsumable: boolean
  dataSource: string
  /** [Corrective-3 §三] 统一结构真实数据血缘（缺失字段显式为 null） */
  lineage: Record<string, unknown>
  /** [Corrective-3 §三] 当前状态原因码（pending 节点也必给出） */
  reasonCode: string
  /** [Corrective-3 §四] 后端权威判定：是否可重试 */
  retryable: boolean
  /** [Corrective-3 §四] 后端输出的推荐恢复动作，前端只展示 */
  recommendedAction: string
  /** [Corrective-3 §四] 对应的可执行治理操作标识 */
  operation: string
  /** [Corrective-3 §四] 治理操作的目标 run id */
  targetRunId: string | null
}

/** 闭包评估问题项 */
export interface BenefitsIssue {
  product: string
  code: string
  severity: string
  recommendedAction: string
}

/** 治理报告（Commit G，已修正真实 lineage） */
export interface GovernanceReport {
  /** 每个产品的真实数据血缘 dict（run_id/publication_id/pointer/coverage/reason_code 等） */
  pointerLineage: Record<string, Record<string, unknown>>
  staleChildren: string[]
  unmatchedActiveChildren: string[]
  readyProducts: string[]
  pendingProducts: string[]
  blockedProducts: string[]
  unavailableProducts: string[]
  degradedReasons: BenefitsIssue[]
}

/** ProductReadiness 响应（GET /v1/admin/readiness/{trade_date}） */
// [CHANGE-20260806-005 / Phase 4 / 六态] 新增 productionClosure / allProductsReady /
// unreconciledChildren；前端只按 DTO 字段展示，禁止猜 readiness。
export interface ProductReadinessResponse {
  tradeDate: string
  closure: string
  productionClosure: string
  mandatoryProductsReady: boolean
  mandatoryProductsFullyFresh: boolean
  enhancementJobsTerminal: boolean
  allProductsReady: boolean
  unreconciledChildren: number
  products: ProductReadinessItem[]
  governance: GovernanceReport
}

/** 查询指定交易日的产品就绪状态 + 治理报告（admin） */
export async function getAdminProductReadiness(
  tradeDate: string,
): Promise<ProductReadinessResponse> {
  const { data } = await apiClient.get<ProductReadinessResponse>(
    `/v1/admin/readiness/${tradeDate}`,
  )
  return data
}

// ============================================================
// ===== [Gate5] GoAccess 访问统计 =====
// ============================================================

/** [Gate5] 访问统计指标项 */
export interface VisitorMetricItem {
  label: string
  count: number
  percentage: number | null
}

/** [Gate5] 访问汇总（单时间窗口） */
export interface VisitorSummary {
  pv: number
  uv: number
  top_pages: VisitorMetricItem[]
  top_referrers: VisitorMetricItem[]
  status_codes: VisitorMetricItem[]
  devices: VisitorMetricItem[]
  browsers: VisitorMetricItem[]
  hourly_trend: VisitorMetricItem[]
}

/** [CHANGE-20260730-010] /admin/visitors 响应体（Umami 数据源） */
export interface VisitorReport {
  today: VisitorSummary
  seven_days: VisitorSummary
  thirty_days: VisitorSummary
  generated_at: string | null
  data_source: 'umami' | 'empty' | 'error'
  error_message: string | null
}

/** [Gate5] 查询访问统计报告（admin only） */
export async function getAdminVisitors(): Promise<VisitorReport> {
  const { data } = await apiClient.get<VisitorReport>('/v1/admin/visitors')
  return data
}


export async function triggerComputeBoard(
  boardId: string,
  params?: { trade_date?: string; publish?: boolean },
): Promise<{
  board_id: string
  trade_date: string
  status: string
  coverage_ratio: number
  eligible_count: number
  ready_count: number
  published: boolean
  snapshot_id: string
}> {
  const { data } = await apiClient.post(
    `/v1/admin/boards/${boardId}/analysis/compute`,
    null,
    { params },
  )
  return data
}

/** [Admin] 触发批量板块分析计算（canary + 全量） */
export async function triggerComputeAllBoards(
  params?: {
    trade_date?: string
    board_type?: 'industry' | 'concept'
    limit?: number
    publish?: boolean
  },
): Promise<{
  trade_date: string
  board_type_filter: string | null
  succeeded: number
  failed: number
  published: number
  coverage_below_threshold: number
  details: Array<{
    board_id: string
    board_name: string
    board_type: string
    status: string
    coverage: number
    published: boolean
  }>
  errors: Array<{ board_id: string; board_name: string; error: string }>
}> {
  const { data } = await apiClient.post(
    '/v1/admin/boards/analysis/compute-all',
    null,
    { params },
  )
  return data
}



// ===== Admin Stock Debug（[S3-E] 从 endpoints.ts 迁入；依赖 stockData 的 AtomicFacts 类型） =====
/** AdminAtomicFactDebugItem - 管理员调试：单事实可追溯信息（保留内部 ID / 路径） */
export interface AdminAtomicFactDebugItem {
  factId: string
  publicKey: string
  sourcePath: string | null
  rawValue: number | null
  thresholdRef: string | null
  thresholdEnabled: boolean
  featureFlag: boolean
  missing: boolean
}

/** AdminStockDebugResponse - 在原子事实响应基础上补充原始 payload 与可追溯信息 */
export interface AdminStockDebugResponse extends AtomicFactsContextResponse {
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
  atomicFactsDebug?: AdminAtomicFactDebugItem[]
}

/**
 * 管理员个股调试接口（需管理员身份）。
 * GET /v1/admin/stocks/{symbol}/debug?as_of=YYYY-MM-DD
 * 返回 Atomic Fact Contract V1 上下文 + 原始 payload 与可追溯信息。
 */
export async function getAdminStockDebug(
  symbol: string,
  params?: { as_of?: string },
  options?: { signal?: AbortSignal },
): Promise<AdminStockDebugResponse> {
  const { data } = await apiClient.get<AdminStockDebugResponse>(
    `/v1/admin/stocks/${symbol}/debug`,
    { params, signal: options?.signal },
  )
  return data
}
