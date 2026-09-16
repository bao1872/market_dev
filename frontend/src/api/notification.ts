// Notification 领域 API owner（[S3-E] 由 endpoints.ts 迁出；endpoints.ts 仅兼容 barrel）。
// 用户侧消息 + 通知渠道 + 飞书分享 + 通知预览。admin 代管渠道/投递仍属 ./admin。

import { apiClient } from './client'
import { useAuthStore } from '../store/auth'

/**
 * 获取当前用户 ID（从 auth store 读取），用于通知 API 的 X-User-Id header。
 * 通知 API 当前使用 X-User-Id 占位，后续接入 JWT 后可移除。
 */
function getUserIdHeader(): Record<string, string> {
  const user = useAuthStore.getState().user
  return user ? { 'X-User-Id': user.id } : {}
}

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

export type ShareDeliveryStatus =
  | 'pending'
  | 'sending'
  | 'success'
  | 'failed'
  | 'retrying'
  | 'dead'
  | 'not_created'

// [CHANGE-20260728-010] 指标视图共享枚举（与后端 app.constants.indicator_view 对齐）
// 历史值：node_cluster / bollinger / smc（仅历史回读兼容）
// 新业务固定值：structure_node（结构 + 筹码共识组合视图）
// 前端不再展示选择器，固定发送 structure_node 组合图

export interface StockDetailFeishuCreateResponse {
  test_run_id: string
  message_group_id: string
  message_id: string
  image_message_id: string | null
  status: 'pending' | 'failed'
}

/** GET /stock-detail-feishu/{test_run_id}/status 响应 - 查询投递状态 */
export interface StockDetailFeishuStatusResponse {
  test_run_id: string
  message_group_id: string | null
  card_status: ShareDeliveryStatus
  capture_status: ShareDeliveryStatus
  image_upload_status: ShareDeliveryStatus
  image_status: ShareDeliveryStatus
  overall_status: 'pending' | 'success' | 'failed'
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

// [S3-B] Watchlist 领域类型已迁至 ./watchlist（见顶部兼容 re-export）。

// ============================================================
// Bar 领域类型
// ============================================================

/** 单条行情数据 */

export interface CreateChannelRequest {
  adapter_type: string
  display_name: string
  target_config: Record<string, unknown>
  secret_ref?: string
}

/** 更新通知渠道请求（字段均可选；target_config 未提交的敏感字段由后端保留） */
export interface UpdateChannelRequest {
  display_name?: string
  target_config?: Record<string, unknown>
  secret_ref?: string
}

/** 消息预览请求 */
export interface NotificationPreviewRequest {
  message_type: string
  context: Record<string, unknown>
  locale?: string
}

/** 邀请码生成请求 - plan_code/grant_days 由前端提交，monitor_limit 由后端按 plan_code 计算 */

export async function getMessages(params?: {
  unread_only?: boolean
  limit?: number
  offset?: number
}): Promise<NotificationMessageListResponse> {
  const { data } = await apiClient.get<NotificationMessageListResponse>('/v1/messages', {
    params,
    headers: getUserIdHeader(),
  })
  return data
}

/** 标记消息已读 */
export async function markMessageRead(messageId: string): Promise<NotificationMessage> {
  const { data } = await apiClient.post<NotificationMessage>(
    `/v1/messages/${messageId}/read`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

// [Messages] - 描述: 未读消息计数，角标专用（避免 list 接口 total 字段语义混淆）
/** 获取当前用户未读消息总数（角标专用） */
export async function getUnreadCount(): Promise<UnreadCountResponse> {
  const { data } = await apiClient.get<UnreadCountResponse>('/v1/messages/unread-count', {
    headers: getUserIdHeader(),
  })
  return data
}

// [Messages] - 描述: 批量标记当前用户所有未读消息为已读
/** 批量标记当前用户所有未读消息为已读 */
export async function readAllMessages(): Promise<ReadAllMessagesResponse> {
  const { data } = await apiClient.post<ReadAllMessagesResponse>(
    '/v1/messages/read-all',
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

/** 获取用户通知渠道列表 */
export async function getNotificationChannels(): Promise<NotificationChannelListResponse> {
  const { data } = await apiClient.get<NotificationChannelListResponse>('/v1/notification-channels', {
    headers: getUserIdHeader(),
  })
  return data
}

/** 创建通知渠道 */
export async function createNotificationChannel(payload: CreateChannelRequest): Promise<NotificationChannel> {
  const { data } = await apiClient.post<NotificationChannel>('/v1/notification-channels', payload, {
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
    `/v1/notification-channels/${channelId}`,
    data,
  )
  return res.data
}

/** 删除通知渠道 */
export async function deleteNotificationChannel(
  channelId: string,
): Promise<NotificationChannel> {
  const res = await apiClient.delete<NotificationChannel>(
    `/v1/notification-channels/${channelId}`,
  )
  return res.data
}

/** 验证通知渠道配置 */
export async function verifyNotificationChannel(channelId: string): Promise<NotificationChannel> {
  const { data } = await apiClient.post<NotificationChannel>(
    `/v1/notification-channels/${channelId}/verify`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

/** 测试渠道投递（发送测试消息到渠道） */
export async function testNotificationChannel(channelId: string): Promise<ChannelTestResponse> {
  const { data } = await apiClient.post<ChannelTestResponse>(
    `/v1/notification-channels/${channelId}/test`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

/** 最近事件实测（发送最近事件到渠道并返回诊断结果） */
export async function testNotificationChannelLatestEvent(channelId: string): Promise<ChannelLatestEventTestResponse> {
  const { data } = await apiClient.post<ChannelLatestEventTestResponse>(
    `/v1/notification-channels/${channelId}/test-latest-event`,
    null,
    { headers: getUserIdHeader() },
  )
  return data
}

// [StockDetailFeishu] - 描述: 创建异步投递任务（Outbox 链路），返回 test_run_id 供轮询
// [CHANGE-20260728-010] 前端不再选择 indicator_view，固定发送空 body（后端使用 structure_node）。
// [CHANGE-20260728-010 P0] 移除无效 payload 参数（旧实现 payload ? {} : {} 无意义）。
export async function sendStockDetailFeishu(
  instrumentId: string,
): Promise<StockDetailFeishuCreateResponse> {
  const { data } = await apiClient.post<StockDetailFeishuCreateResponse>(
    `/v1/instruments/${instrumentId}/send-feishu`,
    // [CHANGE-20260728-010] 不再透传 indicator_view，后端固定使用 FEISHU_CAPTURE_VIEW
    {},
  )
  return data
}

// [StockDetailFeishu] - 描述: 轮询投递状态（card_status / image_status / overall_status）
export async function getStockDetailFeishuStatus(
  testRunId: string,
): Promise<StockDetailFeishuStatusResponse> {
  const { data } = await apiClient.get<StockDetailFeishuStatusResponse>(
    `/v1/stock-detail-feishu/${testRunId}/status`,
  )
  return data
}

/** 消息预览 - 返回渠道无关 DTO + 站内渲染 + 飞书 card JSON */
export async function previewNotification(payload: NotificationPreviewRequest): Promise<NotificationPreviewResponse> {
  const { data } = await apiClient.post<NotificationPreviewResponse>('/v1/notification-previews', payload)
  return data
}

// ============================================================
// ===== Watchlist 端点 =====
// ============================================================
//
// [S3-B] getWatchlist / addToWatchlist / removeFromWatchlist / getWatchlistMonitorStatus
// 的实现已迁至 ./watchlist（唯一 owner），并在本文件顶部以兼容 barrel 重新导出。

// ============================================================
// ===== Stock Memo 端点 =====
// ============================================================

/** 个股备忘录 */

