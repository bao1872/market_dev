// Admin 领域 React Query hooks owner
//
// [S3-D] 由 useApi.ts 迁出。useApi.ts 仅兼容 barrel 重新导出（caller import 零改动）。
// AfterClose hooks 单独在 ./useAdminAfterCloseApi。query key/staleTime/invalidation 逐字等价。

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as adminApi from '../api/admin'
import type { CapabilityKey } from '../navigation/capabilities'
import type { CreateChannelRequest, DeliveryStatus } from '../api/notification'
import type { PaginationParams } from '../api/endpoints'
import type {
  TriggerRunRequest,
  InviteCodeCreateRequest,
  GrantSubscriptionRequest,
  RenewSubscriptionRequest,
  ChangePlanRequest,
  GrantCapabilityRequest,
  BetaApplicationQueryParams,
  BetaApplicationPatchRequest,
} from '../api/admin'

const STALE_REALTIME = 30 * 1000 // 实时数据 30 秒
const STALE_PLANS = 60 * 1000 // 方案列表 1 分钟

// ===== Admin Strategy hooks（留在本文件，admin domain）=====
// ============================================================

/** 查询策略运行历史（admin，/admin 前缀路径） */
export function useAdminStrategyRuns(
  strategyKey: string | undefined,
  params?: { status?: string; limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['admin', 'strategies', strategyKey, 'runs', params],
    queryFn: () => adminApi.getAdminStrategyRuns(strategyKey!, params),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

// [S3-C] usePublishedRuns / useStrategyRunResults 已迁至 ./useStrategyApi（见上方兼容 re-export）。

/** 触发策略运行变更（admin） */
export function useTriggerStrategyRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ strategyKey, payload }: { strategyKey: string; payload: TriggerRunRequest }) =>
      adminApi.triggerStrategyRun(strategyKey, payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['strategies', variables.strategyKey, 'runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'strategies', variables.strategyKey, 'runs'] })
    },
  })
}

// [S3-C] useInstrumentMonitorStates / useStrategyMonitorStates / useInstrumentEvents /
// useStrategyEvents / useStrategyEventDetail 已迁至 ./useStrategyApi（见上方兼容 re-export）。

// ============================================================
// ===== Notifications hooks =====
// ============================================================

/** 获取用户消息列表（始终刷新） */

/** 管理员查看指定用户的通知渠道列表 */
export function useAdminUserChannels(userId: string | null, enabled: boolean = true) {
  return useQuery({
    queryKey: ['admin', 'users', userId, 'notification-channels'],
    queryFn: () => adminApi.adminListUserChannels(userId as string),
    staleTime: STALE_PLANS,
    enabled: enabled && !!userId,
  })
}

/** 管理员为指定用户创建通知渠道 */
export function useAdminCreateUserChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { userId: string; data: CreateChannelRequest }) =>
      adminApi.adminCreateUserChannel(params.userId, params.data),
    onSuccess: (_data, params) => {
      queryClient.invalidateQueries({
        queryKey: ['admin', 'users', params.userId, 'notification-channels'],
      })
    },
  })
}

/** 管理员更新指定用户的通知渠道 */
export function useAdminUpdateUserChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: {
      userId: string
      channelId: string
      data: { display_name?: string; target_config?: Record<string, unknown> }
    }) => adminApi.adminUpdateUserChannel(params.userId, params.channelId, params.data),
    onSuccess: (_data, params) => {
      queryClient.invalidateQueries({
        queryKey: ['admin', 'users', params.userId, 'notification-channels'],
      })
    },
  })
}

/** 管理员删除指定用户的通知渠道 */
export function useAdminDeleteUserChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { userId: string; channelId: string }) =>
      adminApi.adminDeleteUserChannel(params.userId, params.channelId),
    onSuccess: (_data, params) => {
      queryClient.invalidateQueries({
        queryKey: ['admin', 'users', params.userId, 'notification-channels'],
      })
    },
  })
}

/** 管理员验证指定用户的通知渠道 */
export function useAdminVerifyUserChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { userId: string; channelId: string }) =>
      adminApi.adminVerifyUserChannel(params.userId, params.channelId),
    onSuccess: (_data, params) => {
      queryClient.invalidateQueries({
        queryKey: ['admin', 'users', params.userId, 'notification-channels'],
      })
    },
  })
}

/** 管理员对指定用户的通知渠道发送测试消息 */
export function useAdminTestUserChannel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { userId: string; channelId: string }) =>
      adminApi.adminTestUserChannel(params.userId, params.channelId),
    onSuccess: (_data, params) => {
      queryClient.invalidateQueries({
        queryKey: ['admin', 'users', params.userId, 'notification-channels'],
      })
    },
  })
}

/** 最近事件实测变更 */

export function useInviteCodes(params?: { status?: string; limit?: number; offset?: number }) {
  return useQuery({
    queryKey: ['admin', 'invite-codes', params],
    queryFn: () => adminApi.getInviteCodes(params),
    staleTime: STALE_REALTIME,
  })
}

/** 生成邀请码变更 */
export function useCreateInviteCodes() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: InviteCodeCreateRequest) => adminApi.createInviteCodes(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'invite-codes'] })
    },
  })
}

/** 作废邀请码变更 */
export function useRevokeInviteCode() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (inviteCodeId: string) => adminApi.revokeInviteCode(inviteCodeId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'invite-codes'] })
    },
  })
}


export function useMembers(params?: { limit?: number; offset?: number }) {
  return useQuery({
    queryKey: ['admin', 'members', params],
    queryFn: () => adminApi.getMembers(params),
    staleTime: STALE_REALTIME,
  })
}

/** 查询用户兑换记录 */
export function useMemberRedemptions(userId: string | undefined) {
  return useQuery({
    queryKey: ['admin', 'members', userId, 'redemptions'],
    queryFn: () => adminApi.getMemberRedemptions(userId!),
    enabled: !!userId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询用户列表（admin） */
export function useAdminUsers(params?: PaginationParams) {
  return useQuery({
    queryKey: ['admin', 'users', params],
    queryFn: () => adminApi.getAdminUsers(params),
    staleTime: STALE_REALTIME,
  })
}

/** 查询用户详情（admin） */
export function useAdminUser(userId: string | undefined) {
  return useQuery({
    queryKey: ['admin', 'users', userId],
    queryFn: () => adminApi.getAdminUser(userId!),
    enabled: !!userId,
    staleTime: STALE_REALTIME,
  })
}

/** 启用用户账户（admin） */
export function useAdminEnableUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) => adminApi.adminEnableUser(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** 停用用户账户（admin） */
export function useAdminDisableUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) => adminApi.adminDisableUser(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** 管理员重置用户密码（设置新密码；不读取、不返回旧密码） */
export function useAdminResetUserPassword() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, newPassword }: { userId: string; newPassword: string }) =>
      adminApi.adminResetUserPassword(userId, { new_password: newPassword }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'audit-logs'] })
    },
  })
}

/** 管理员授予用户套餐 */
export function useAdminGrantSubscription() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      userId,
      payload,
    }: {
      userId: string
      payload: GrantSubscriptionRequest
    }) => adminApi.adminGrantSubscription(userId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** 管理员续期用户套餐 */
export function useAdminRenewSubscription() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      userId,
      payload,
    }: {
      userId: string
      payload: RenewSubscriptionRequest
    }) => adminApi.adminRenewSubscription(userId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** 管理员撤销用户套餐 */
export function useAdminRevokeSubscription() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) => adminApi.adminRevokeSubscription(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** 管理员变更用户套餐 */
export function useAdminChangeSubscriptionPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      userId,
      payload,
    }: {
      userId: string
      payload: ChangePlanRequest
    }) => adminApi.adminChangeSubscriptionPlan(userId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** [Gate2 PRD60] 查询用户 capabilities（四类独立权限状态） */
export function useUserCapabilities(userId: string | undefined, enabled: boolean = true) {
  return useQuery({
    queryKey: ['admin', 'users', userId, 'capabilities'],
    queryFn: () => adminApi.getUserCapabilities(userId!),
    enabled: !!userId && enabled,
    staleTime: STALE_REALTIME,
  })
}

/** [Gate2 PRD60 PA-20] 管理员直接授予/修改用户 capability */
export function useAdminGrantCapability() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      userId,
      payload,
    }: {
      userId: string
      payload: GrantCapabilityRequest
    }) => adminApi.adminGrantCapability(userId, payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users', variables.userId, 'capabilities'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** [Gate2 PRD60 PA-20] 管理员撤销用户 capability */
export function useAdminRevokeCapability() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      userId,
      capability,
    }: {
      userId: string
      capability: CapabilityKey
    }) => adminApi.adminRevokeCapability(userId, capability),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users', variables.userId, 'capabilities'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
    },
  })
}

/** 查询管理员审计日志 */
export function useAdminAuditLogs(
  params?: {
    target_user_id?: string
    action?: string
    limit?: number
    offset?: number
  },
  enabled: boolean = true,
) {
  return useQuery({
    queryKey: ['admin', 'audit-logs', params],
    queryFn: () => adminApi.getAdminAuditLogs(params),
    staleTime: STALE_REALTIME,
    enabled,
  })
}

// ============================================================

// ===== Admin Beta Applications hooks（Task 4） =====
// ============================================================

/** 查询内测申请列表（分页+筛选+搜索） */
export function useAdminBetaApplications(params?: BetaApplicationQueryParams) {
  return useQuery({
    queryKey: ['admin', 'beta-applications', params],
    queryFn: () => adminApi.getAdminBetaApplications(params),
    staleTime: STALE_REALTIME,
  })
}

/** 获取内测申请统计数据 */
export function useAdminBetaApplicationStats() {
  return useQuery({
    queryKey: ['admin', 'beta-applications', 'stats'],
    queryFn: () => adminApi.getAdminBetaApplicationStats(),
    staleTime: STALE_REALTIME,
  })
}

/** 获取内测申请详情 */
export function useAdminBetaApplicationDetail(appId: string | undefined) {
  return useQuery({
    queryKey: ['admin', 'beta-applications', appId, 'detail'],
    queryFn: () => adminApi.getAdminBetaApplicationDetail(appId!),
    enabled: !!appId,
    staleTime: STALE_REALTIME,
  })
}

/** 修改内测申请状态（status + admin_note） */
export function useUpdateAdminBetaApplication() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ appId, payload }: { appId: string; payload: BetaApplicationPatchRequest }) =>
      adminApi.updateAdminBetaApplication(appId, payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'beta-applications'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'beta-applications', variables.appId, 'detail'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'beta-applications', 'stats'] })
    },
  })
}

/** 重发内测申请飞书通知 */
export function useRetryAdminBetaApplicationFeishu() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (appId: string) => adminApi.retryAdminBetaApplicationFeishu(appId),
    onSuccess: (_data, appId) => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'beta-applications', appId, 'detail'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'beta-applications'] })
    },
  })
}

// ============================================================

// ===== Admin System Overview hooks =====
// ============================================================

/** 获取系统概览（30 秒缓存，15 秒轮询，管理后台首页使用）
 *  enabled: 仅管理员启用，避免普通用户触发 403 无权限请求（AppShell 全局调用时传入角色判断）
 */
export function useAdminSystemOverview(enabled: boolean = true) {
  return useQuery({
    queryKey: ['admin', 'system-overview'],
    queryFn: adminApi.getAdminSystemOverview,
    enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: enabled ? 15_000 : false,
    refetchIntervalInBackground: false,
  })
}

// ============================================================
// ===== [BOARD-LOCAL-OWNERSHIP-01] 板块/概念手动同步状态 =====
// ============================================================
// 独立于 after-close：query key 不含 after-close，不随盘后流水线失效。
// 只读；无「立即同步」动作；无 age/SLA 轮询（普通 stale 行为即可）。
export function useAdminBoardSyncStatus(enabled: boolean = true) {
  return useQuery({
    queryKey: ['admin', 'board-sync', 'status'],
    queryFn: adminApi.getAdminBoardSyncStatus,
    enabled,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================

export function useAdminProductReadiness(
  tradeDate: string | null | undefined,
  enabled: boolean = true,
) {
  return useQuery({
    queryKey: ['admin', 'readiness', tradeDate],
    queryFn: () => adminApi.getAdminProductReadiness(tradeDate!),
    enabled: !!tradeDate && enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: enabled ? 15_000 : false,
    refetchIntervalInBackground: false,
  })
}

/** 查询盘后编排状态（10 秒轮询，含事件时间线 + DSA run 状态） */

export function useAdminStockDebug(
  symbol: string | undefined,
  params?: { as_of?: string },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['admin', 'stock-debug', symbol, params ?? null],
    queryFn: ({ signal }) => adminApi.getAdminStockDebug(symbol!, params, { signal }),
    enabled: !!symbol && (options?.enabled ?? true),
    staleTime: STALE_REALTIME,
  })
}

// ===== Admin: message-deliveries / scheduler / worker / visitors / board-compute hooks（S3-D 补齐） =====

export function useMessageDeliveries(params?: {
  status?: DeliveryStatus
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['admin', 'message-deliveries', params],
    queryFn: () => adminApi.getMessageDeliveries(params),
    staleTime: STALE_REALTIME,
  })
}

export function useRetryMessageDelivery() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (deliveryId: string) => adminApi.retryMessageDelivery(deliveryId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'message-deliveries'] })
    },
  })
}

export function useSchedulerJobRuns(params?: {
  job_name?: string
  business_date?: string
  status?: string
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['admin', 'scheduler-job-runs', params],
    queryFn: () => adminApi.getSchedulerJobRuns(params),
    staleTime: STALE_REALTIME,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  })
}

export function useWorkerHeartbeats(params?: {
  status?: string
  worker_name?: string
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['admin', 'worker-heartbeats', params],
    queryFn: () => adminApi.getWorkerHeartbeats(params),
    staleTime: STALE_REALTIME,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  })
}

export function useAdminVisitors() {
  return useQuery({
    queryKey: ['admin', 'visitors'],
    queryFn: adminApi.getAdminVisitors,
    staleTime: 5 * 60 * 1000, // 5 分钟
    refetchInterval: 5 * 60 * 1000,
    refetchIntervalInBackground: false,
  })
}

export function useTriggerComputeBoard() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      boardId,
      params,
    }: {
      boardId: string
      params?: { trade_date?: string; publish?: boolean }
    }) => adminApi.triggerComputeBoard(boardId, params),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board-analysis'] })
    },
  })
}

export function useTriggerComputeAllBoards() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params?: {
      trade_date?: string
      board_type?: 'industry' | 'concept'
      limit?: number
      publish?: boolean
    }) => adminApi.triggerComputeAllBoards(params),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['board-analysis'] })
    },
  })
}
