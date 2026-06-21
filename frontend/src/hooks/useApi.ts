// React Query hooks 层 - 封装常用查询与变更操作
//
// 职责：
// 1. 将 endpoints.ts 中的 API 函数封装为 useQuery / useMutation hooks
// 2. 设置合理的缓存时间（strategies 5min, watchlist 1min, messages 0 stale）
// 3. 变更操作自动失效相关查询缓存
//
// 缓存策略说明：
// - staleTime=0：数据始终视为过期，每次组件挂载都重新请求（消息、用户信息等实时性要求高的数据）
// - staleTime=5min：5 分钟内不重复请求（策略目录等低频变更数据）
// - staleTime=1min：1 分钟内不重复请求（自选股、方案列表等中等频率变更数据）
// - staleTime=30s：30 秒内不重复请求（运行结果、状态等较高频率变更数据）

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import type { UseQueryOptions } from '@tanstack/react-query'
import * as api from '../api/endpoints'
import type {
  LoginRequest,
  RegisterRequest,
  TriggerRunRequest,
  WatchlistAddRequest,
  CreateChannelRequest,
  NotificationPreviewRequest,
  MonitoringPlanCreateRequest,
  MonitoringPlanUpdateRequest,
  SelectionPlanCreateRequest,
  SelectionPlanUpdateRequest,
  SelectionPlanCloneRequest,
  SelectionPlanRunRequest,
  SelectionPlanPreviewRequest,
  InviteCodeCreateRequest,
  InstrumentQueryParams,
  StrategyEventQueryParams,
  StrategyResultQueryParams,
  BarQueryParams,
  CalendarQueryParams,
  ConfigQueryParams,
} from '../api/endpoints'

// ============================================================
// 缓存时间常量
// ============================================================

const STALE_STRATEGIES = 5 * 60 * 1000 // 策略目录 5 分钟
const STALE_WATCHLIST = 60 * 1000 // 自选股 1 分钟
const STALE_MESSAGES = 0 // 消息始终刷新
const STALE_PLANS = 60 * 1000 // 方案列表 1 分钟
const STALE_REALTIME = 30 * 1000 // 实时数据 30 秒
const STALE_CALENDAR = 30 * 60 * 1000 // 日历 30 分钟（极少变更）
const STALE_CONFIG = 60 * 1000 // 配置 1 分钟

// ============================================================
// ===== Auth hooks =====
// ============================================================

/** 获取当前用户信息（始终刷新） */
export function useMe() {
  return useQuery({
    queryKey: ['me'],
    queryFn: api.getMe,
    staleTime: STALE_MESSAGES,
  })
}

/** 获取当前用户会员状态（始终刷新） */
export function useMyMembership() {
  return useQuery({
    queryKey: ['me', 'membership'],
    queryFn: api.getMyMembership,
    staleTime: STALE_MESSAGES,
  })
}

/** 登录变更 */
export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ email, password }: LoginRequest) => api.login(email, password),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me'] })
    },
  })
}

/** 注册变更 */
export function useRegister() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: RegisterRequest) => api.register(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me'] })
    },
  })
}

/** 续期变更 */
export function useRenew() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (inviteCode: string) => api.renew(inviteCode),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['me', 'membership'] })
    },
  })
}

/** Token 刷新变更 */
export function useRefreshToken() {
  return useMutation({
    mutationFn: (refreshToken: string) => api.refreshToken(refreshToken),
  })
}

// ============================================================
// ===== Instruments hooks =====
// ============================================================

/** 查询股票列表 */
export function useInstruments(params?: InstrumentQueryParams) {
  return useQuery({
    queryKey: ['instruments', params],
    queryFn: () => api.getInstruments(params),
    staleTime: STALE_PLANS,
  })
}

/** 按 ID 列表批量查询股票（最多 1000 个） */
export function useBatchInstruments(ids: string[] | undefined) {
  return useQuery({
    queryKey: ['instruments', 'batch', ids],
    queryFn: () => api.batchGetInstruments(ids!),
    enabled: !!ids && ids.length > 0,
    staleTime: STALE_PLANS,
  })
}

/** 按 ID 查询单个股票 */
export function useInstrument(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['instruments', instrumentId],
    queryFn: () => api.getInstrumentById(instrumentId!),
    enabled: !!instrumentId,
    staleTime: STALE_STRATEGIES,
  })
}

/** 按 symbol 查询股票 */
export function useInstrumentBySymbol(symbol: string | undefined) {
  return useQuery({
    queryKey: ['instruments', 'by-symbol', symbol],
    queryFn: () => api.getInstrumentBySymbol(symbol!),
    enabled: !!symbol,
    staleTime: STALE_STRATEGIES,
  })
}

// ============================================================
// ===== Strategies hooks =====
// ============================================================

/** 获取策略列表（5 分钟缓存） */
export function useStrategies(kind?: string) {
  return useQuery({
    queryKey: ['strategies', kind],
    queryFn: () => api.getStrategies(kind),
    staleTime: STALE_STRATEGIES,
  })
}

/** 获取策略详情（5 分钟缓存） */
export function useStrategy(strategyKey: string | undefined) {
  return useQuery({
    queryKey: ['strategies', strategyKey],
    queryFn: () => api.getStrategy(strategyKey!),
    enabled: !!strategyKey,
    staleTime: STALE_STRATEGIES,
  })
}

/** 获取策略的所有版本（5 分钟缓存） */
export function useStrategyVersions(strategyKey: string | undefined) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'versions'],
    queryFn: () => api.getStrategyVersions(strategyKey!),
    enabled: !!strategyKey,
    staleTime: STALE_STRATEGIES,
  })
}

/** 获取策略版本的 schema（5 分钟缓存） */
export function useStrategyVersionSchema(strategyKey: string | undefined, version: string | undefined) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'versions', version, 'schema'],
    queryFn: () => api.getStrategyVersionSchema(strategyKey!, version!),
    enabled: !!strategyKey && !!version,
    staleTime: STALE_STRATEGIES,
  })
}

// ============================================================
// ===== Strategy Runs hooks =====
// ============================================================

/** 查询策略运行历史 */
export function useStrategyRuns(
  strategyKey: string | undefined,
  params?: { status?: string; limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'runs', params],
    queryFn: () => api.getStrategyRuns(strategyKey!, params),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

/** 查询已发布的运行批次（普通用户可访问） */
export function usePublishedRuns(
  strategyKey: string | undefined,
  params?: { limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'published-runs', params],
    queryFn: () => api.getPublishedRuns(strategyKey!, params),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

/** 查询运行结果（分页+筛选+排序） */
export function useStrategyRunResults(runId: string | undefined, params?: StrategyResultQueryParams) {
  return useQuery({
    queryKey: ['strategy-runs', runId, 'results', params],
    queryFn: () => api.getStrategyRunResults(runId!, params),
    enabled: !!runId,
    staleTime: STALE_REALTIME,
  })
}

/** 触发策略运行变更（admin） */
export function useTriggerStrategyRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ strategyKey, payload }: { strategyKey: string; payload: TriggerRunRequest }) =>
      api.triggerStrategyRun(strategyKey, payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['strategies', variables.strategyKey, 'runs'] })
    },
  })
}

// ============================================================
// ===== Monitor States hooks =====
// ============================================================

/** 查询某股票的所有监控策略状态 */
export function useInstrumentMonitorStates(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['instruments', instrumentId, 'monitor-states'],
    queryFn: () => api.getInstrumentMonitorStates(instrumentId!),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询某策略的所有股票状态（支持 version 过滤） */
export function useStrategyMonitorStates(strategyKey: string | undefined, version?: string) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'monitor-states', version],
    queryFn: () => api.getStrategyMonitorStates(strategyKey!, version),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================
// ===== Strategy Events hooks =====
// ============================================================

/** 查询某股票的策略事件 */
export function useInstrumentEvents(instrumentId: string | undefined, params?: StrategyEventQueryParams) {
  return useQuery({
    queryKey: ['instruments', instrumentId, 'events', params],
    queryFn: () => api.getInstrumentEvents(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询某策略的事件 */
export function useStrategyEvents(
  strategyKey: string | undefined,
  params?: { version?: string } & StrategyEventQueryParams,
) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'events', params],
    queryFn: () => api.getStrategyEvents(strategyKey!, params),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

/** 查询事件详情（含 snapshot 快照） */
export function useStrategyEventDetail(eventId: string | undefined) {
  return useQuery({
    queryKey: ['strategy-events', eventId],
    queryFn: () => api.getStrategyEventDetail(eventId!),
    enabled: !!eventId,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================
// ===== Notifications hooks =====
// ============================================================

/** 获取用户消息列表（始终刷新） */
export function useMessages(params?: { unread_only?: boolean; limit?: number; offset?: number }) {
  return useQuery({
    queryKey: ['messages', params],
    queryFn: () => api.getMessages(params),
    staleTime: STALE_MESSAGES,
  })
}

/** 标记消息已读变更 */
export function useMarkMessageRead() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (messageId: string) => api.markMessageRead(messageId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['messages'] })
    },
  })
}

/** 获取用户通知渠道列表 */
export function useNotificationChannels() {
  return useQuery({
    queryKey: ['notification-channels'],
    queryFn: api.getNotificationChannels,
    staleTime: STALE_PLANS,
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
  return useMutation({
    mutationFn: (channelId: string) => api.testNotificationChannel(channelId),
  })
}

/** 消息预览变更 */
export function usePreviewNotification() {
  return useMutation({
    mutationFn: (payload: NotificationPreviewRequest) => api.previewNotification(payload),
  })
}

// ============================================================
// ===== Watchlist hooks =====
// ============================================================

/** 查询当前用户的自选列表（1 分钟缓存） */
export function useWatchlist() {
  return useQuery({
    queryKey: ['watchlist'],
    queryFn: api.getWatchlist,
    staleTime: STALE_WATCHLIST,
  })
}

/** 加入自选变更（自动失效 watchlist 缓存） */
export function useAddToWatchlist() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: WatchlistAddRequest) => api.addToWatchlist(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    },
  })
}

/** 移除自选变更（自动失效 watchlist 缓存） */
export function useRemoveFromWatchlist() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (instrumentId: string) => api.removeFromWatchlist(instrumentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
    },
  })
}

// ============================================================
// ===== Monitoring Plans hooks =====
// ============================================================

/** 查询当前用户的监控方案列表（1 分钟缓存） */
export function useMonitoringPlans(status?: string) {
  return useQuery({
    queryKey: ['monitoring-plans', status],
    queryFn: () => api.getMonitoringPlans(status),
    staleTime: STALE_PLANS,
  })
}

/** 查询方案详情（含当前 revision + 成员） */
export function useMonitoringPlan(planId: string | undefined) {
  return useQuery({
    queryKey: ['monitoring-plans', planId],
    queryFn: () => api.getMonitoringPlan(planId!),
    enabled: !!planId,
    staleTime: STALE_PLANS,
  })
}

/** 创建监控方案变更 */
export function useCreateMonitoringPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: MonitoringPlanCreateRequest) => api.createMonitoringPlan(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['monitoring-plans'] })
    },
  })
}

/** 更新方案变更（失效方案列表 + 当前方案详情） */
export function useUpdateMonitoringPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, payload }: { planId: string; payload: MonitoringPlanUpdateRequest }) =>
      api.updateMonitoringPlan(planId, payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['monitoring-plans'] })
      queryClient.invalidateQueries({ queryKey: ['monitoring-plans', variables.planId] })
    },
  })
}

/** 验证方案变更 */
export function useValidateMonitoringPlan() {
  return useMutation({
    mutationFn: (planId: string) => api.validateMonitoringPlan(planId),
  })
}

/** 暂停方案变更 */
export function usePauseMonitoringPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (planId: string) => api.pauseMonitoringPlan(planId),
    onSuccess: (_data, planId) => {
      queryClient.invalidateQueries({ queryKey: ['monitoring-plans'] })
      queryClient.invalidateQueries({ queryKey: ['monitoring-plans', planId] })
    },
  })
}

/** 恢复方案变更 */
export function useResumeMonitoringPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (planId: string) => api.resumeMonitoringPlan(planId),
    onSuccess: (_data, planId) => {
      queryClient.invalidateQueries({ queryKey: ['monitoring-plans'] })
      queryClient.invalidateQueries({ queryKey: ['monitoring-plans', planId] })
    },
  })
}

/** 查询方案状态（当前 revision 下的所有股票状态） */
export function useMonitoringPlanStates(planId: string | undefined, status?: string) {
  return useQuery({
    queryKey: ['monitoring-plans', planId, 'states', status],
    queryFn: () => api.getMonitoringPlanStates(planId!, status),
    enabled: !!planId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询方案下的组合事件 */
export function useMonitoringPlanEvents(
  planId: string | undefined,
  params?: { event_type?: string; start_time?: string; end_time?: string; limit?: number },
) {
  return useQuery({
    queryKey: ['monitoring-plans', planId, 'events', params],
    queryFn: () => api.getMonitoringPlanEvents(planId!, params),
    enabled: !!planId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询个股组合状态 */
export function useInstrumentCompositeState(instrumentId: string | undefined, planId?: string) {
  return useQuery({
    queryKey: ['instruments', instrumentId, 'composite-state', planId],
    queryFn: () => api.getInstrumentCompositeState(instrumentId!, planId),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询组合事件详情（含 evidence） */
export function useCompositeEventDetail(eventId: string | undefined) {
  return useQuery({
    queryKey: ['composite-events', eventId],
    queryFn: () => api.getCompositeEventDetail(eventId!),
    enabled: !!eventId,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================
// ===== Selection Plans hooks =====
// ============================================================

/** 查询当前用户的选股方案列表（1 分钟缓存） */
export function useSelectionPlans() {
  return useQuery({
    queryKey: ['selection-plans'],
    queryFn: api.getSelectionPlans,
    staleTime: STALE_PLANS,
  })
}

/** 获取选股方案详情（含当前 revision + members + conditions） */
export function useSelectionPlan(planId: string | undefined) {
  return useQuery({
    queryKey: ['selection-plans', planId],
    queryFn: () => api.getSelectionPlan(planId!),
    enabled: !!planId,
    staleTime: STALE_PLANS,
  })
}

/** 创建选股方案变更 */
export function useCreateSelectionPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: SelectionPlanCreateRequest) => api.createSelectionPlan(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['selection-plans'] })
    },
  })
}

/** 更新选股方案变更 */
export function useUpdateSelectionPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, payload }: { planId: string; payload: SelectionPlanUpdateRequest }) =>
      api.updateSelectionPlan(planId, payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['selection-plans'] })
      queryClient.invalidateQueries({ queryKey: ['selection-plans', variables.planId] })
    },
  })
}

/** 克隆选股方案变更 */
export function useCloneSelectionPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, payload }: { planId: string; payload: SelectionPlanCloneRequest }) =>
      api.cloneSelectionPlan(planId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['selection-plans'] })
    },
  })
}

/** 验证选股方案变更 */
export function useValidateSelectionPlan() {
  return useMutation({
    mutationFn: (planId: string) => api.validateSelectionPlan(planId),
  })
}

/** 预览选股方案结果变更（不落库） */
export function usePreviewSelectionPlan() {
  return useMutation({
    mutationFn: ({ planId, payload }: { planId: string; payload: SelectionPlanPreviewRequest }) =>
      api.previewSelectionPlan(planId, payload),
  })
}

/** 执行选股方案变更（幂等） */
export function useRunSelectionPlan() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ planId, payload }: { planId: string; payload: SelectionPlanRunRequest }) =>
      api.runSelectionPlan(planId, payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: ['selection-plans', variables.planId, 'runs'],
      })
    },
  })
}

/** 查询方案的运行历史 */
export function useSelectionPlanRuns(
  planId: string | undefined,
  params?: { status?: string; limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['selection-plans', planId, 'runs', params],
    queryFn: () => api.getSelectionPlanRuns(planId!, params),
    enabled: !!planId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询运行结果（分页） */
export function useSelectionPlanRunResults(
  runId: string | undefined,
  params?: { matched_only?: boolean; limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['selection-plan-runs', runId, 'results', params],
    queryFn: () => api.getSelectionPlanRunResults(runId!, params),
    enabled: !!runId,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================
// ===== Bars hooks =====
// ============================================================

/** 查询指定标的的行情数据 */
export function useBars(instrumentId: string | undefined, params?: BarQueryParams) {
  return useQuery({
    queryKey: ['bars', instrumentId, params],
    queryFn: () => api.getBars(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
  })
}

// ============================================================
// ===== Calendar hooks =====
// ============================================================

/** 查询交易日历（30 分钟缓存，极少变更） */
export function useCalendar(params?: CalendarQueryParams) {
  return useQuery({
    queryKey: ['calendar', params],
    queryFn: () => api.getCalendar(params),
    staleTime: STALE_CALENDAR,
  })
}

/** 查询指定日期是否为交易日 */
export function useIsTradingDay(targetDate: string | undefined) {
  return useQuery({
    queryKey: ['calendar', 'is-trading-day', targetDate],
    queryFn: () => api.isTradingDay(targetDate!),
    enabled: !!targetDate,
    staleTime: STALE_CALENDAR,
  })
}

// ============================================================
// ===== Admin Config hooks =====
// ============================================================

/** 查询配置列表（1 分钟缓存） */
export function useAdminConfigs(params?: ConfigQueryParams) {
  return useQuery({
    queryKey: ['admin', 'config', params],
    queryFn: () => api.getAdminConfigs(params),
    staleTime: STALE_CONFIG,
  })
}

/** 查询单个配置 */
export function useAdminConfig(configKey: string | undefined) {
  return useQuery({
    queryKey: ['admin', 'config', configKey],
    queryFn: () => api.getAdminConfig(configKey!),
    enabled: !!configKey,
    staleTime: STALE_CONFIG,
  })
}

/** 更新配置值变更 */
export function useUpdateAdminConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ configKey, currentValue }: { configKey: string; currentValue: unknown }) =>
      api.updateAdminConfig(configKey, currentValue),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'config'] })
    },
  })
}

// ============================================================
// ===== Admin Membership hooks =====
// ============================================================

/** 查询邀请码列表 */
export function useInviteCodes(params?: { status?: string; limit?: number; offset?: number }) {
  return useQuery({
    queryKey: ['admin', 'invite-codes', params],
    queryFn: () => api.getInviteCodes(params),
    staleTime: STALE_REALTIME,
  })
}

/** 生成邀请码变更 */
export function useCreateInviteCodes() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: InviteCodeCreateRequest) => api.createInviteCodes(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'invite-codes'] })
    },
  })
}

/** 作废邀请码变更 */
export function useRevokeInviteCode() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (inviteCodeId: string) => api.revokeInviteCode(inviteCodeId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'invite-codes'] })
    },
  })
}

/** 查询会员账户列表 */
export function useMembers(params?: { limit?: number; offset?: number }) {
  return useQuery({
    queryKey: ['admin', 'members', params],
    queryFn: () => api.getMembers(params),
    staleTime: STALE_REALTIME,
  })
}

/** 查询用户兑换记录 */
export function useMemberRedemptions(userId: string | undefined) {
  return useQuery({
    queryKey: ['admin', 'members', userId, 'redemptions'],
    queryFn: () => api.getMemberRedemptions(userId!),
    enabled: !!userId,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================
// 类型重导出（方便页面直接引用）
// ============================================================

export type { UseQueryOptions }
