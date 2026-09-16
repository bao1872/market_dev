// Notification 领域 hooks owner（[S3-E] 由 useApi.ts 迁出；useApi.ts 仅兼容 barrel）。

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as api from '../api/notification'
import type { CreateChannelRequest, NotificationPreviewRequest } from '../api/notification'

const STALE_MESSAGES = 0 // 消息始终刷新
const STALE_PLANS = 60 * 1000 // 渠道列表 1 分钟

export function useMessages(params?: { unread_only?: boolean; limit?: number; offset?: number }) {
  return useQuery({
    queryKey: ['messages', params],
    queryFn: () => api.getMessages(params),
    staleTime: STALE_MESSAGES,
  })
}

// [Messages] - 描述: 未读消息计数，角标专用（queryKey 挂在 messages 下，标记已读/全部已读后自动失效）
/** 获取当前用户未读消息总数（角标专用，始终刷新） */
export function useUnreadCount() {
  return useQuery({
    queryKey: ['messages', 'unread-count'],
    queryFn: api.getUnreadCount,
    staleTime: STALE_MESSAGES,
  })
}

/** 标记消息已读变更（自动失效消息列表与未读计数） */
export function useMarkMessageRead() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (messageId: string) => api.markMessageRead(messageId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['messages'] })
    },
  })
}

// [Messages] - 描述: 批量标记所有未读为已读，成功后失效消息列表与未读计数
/** 批量标记当前用户所有未读消息为已读变更 */
export function useReadAllMessages() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: api.readAllMessages,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['messages'] })
    },
  })
}

/** 获取用户通知渠道列表
 * [capture-mode] 截图模式下禁用：通知渠道列表需要 admin 权限，
 * capture token 无 admin 角色，调用会触发 401 拦截器跳转登录页，
 * 导致 StockDetailPage 卸载、data-render-ready 永远为 false、截图超时 502
 */
export function useNotificationChannels(enabled: boolean = true) {
  return useQuery({
    queryKey: ['notification-channels'],
    queryFn: api.getNotificationChannels,
    staleTime: STALE_PLANS,
    enabled,
  })
}

/** 创建通知渠道变更 */
export function useCreateNotificationChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: CreateChannelRequest) => api.createNotificationChannel(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notification-channels'] })
    },
  })
}

/** 更新通知渠道变更 */
export function useUpdateNotificationChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { channelId: string; data: { display_name?: string; target_config?: Record<string, unknown> } }) =>
      api.updateNotificationChannel(params.channelId, params.data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notification-channels'] })
    },
  })
}

/** 删除通知渠道变更 */
export function useDeleteNotificationChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (channelId: string) => api.deleteNotificationChannel(channelId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notification-channels'] })
    },
  })
}

/** 验证通知渠道变更 */
export function useVerifyNotificationChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (channelId: string) => api.verifyNotificationChannel(channelId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notification-channels'] })
    },
  })
}

/** 测试渠道投递变更 */
export function useTestNotificationChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (channelId: string) => api.testNotificationChannel(channelId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notification-channels'] })
    },
  })
}

// ============================================================================
// 管理员代管用户通知渠道（per-user Feishu）
// 与用户自助 hook 的区别：作用域是管理员指定的 target user_id，
// 后端是同一套 notification_service 的薄包装，target_config 由后端统一脱敏。
// ============================================================================

export function useTestNotificationChannelLatestEvent() {
  return useMutation({
    mutationFn: (channelId: string) => api.testNotificationChannelLatestEvent(channelId),
  })
}

/** 消息预览变更 */
export function usePreviewNotification() {
  return useMutation({
    mutationFn: (payload: NotificationPreviewRequest) => api.previewNotification(payload),
  })
}

/** 查询消息投递记录（admin） */

/** 立即重试消息投递记录（admin） */

// ============================================================

