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
  InviteCodeCreateRequest,
  InstrumentQueryParams,
  StrategyEventQueryParams,
  StrategyResultQueryParams,
  BarQueryParams,
  CalendarQueryParams,
  IndicatorQueryParams,
  StockMemoUpsertRequest,
  MarketStatus,
  DeliveryStatus,
  BetaApplicationQueryParams,
  BetaApplicationPatchRequest,
  PlanResponse,
  GrantSubscriptionRequest,
  RenewSubscriptionRequest,
  ChangePlanRequest,
  PaginationParams,
  StructuralFactorQueryParams,
  TemporalFeaturesQueryParams,
  AfterClosePipelineRunRequest,
  TableViewPresetCreateRequest,
  TableViewPresetPatchRequest,
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

// ============================================================
// 市场状态缓存（由 AppShell 轮询 /market/status 后通过 setCachedMarketStatus 更新）
// ============================================================
// 设计说明：isInTradingHours() 是同步函数（用于 refetchInterval 回调），
// 无法直接 await 后端 API。通过模块级缓存 + AppShell 30s 轮询更新，
// 使交易时段判断与后端保持一致；缓存未填充时使用 Intl 上海时区 fallback。
let _cachedMarketStatus: MarketStatus | null = null

/** 更新市场状态缓存（由 AppShell 的轮询逻辑调用） */
export function setCachedMarketStatus(status: MarketStatus | null): void {
  _cachedMarketStatus = status
}

/** 获取当前缓存的市场状态（可用于 UI 显示） */
export function getCachedMarketStatus(): MarketStatus | null {
  return _cachedMarketStatus
}

/** 上海时区 fallback：使用 Intl.DateTimeFormat 固定 Asia/Shanghai 判断交易时段 */
function isInTradingHoursShanghaiFallback(): boolean {
  // 使用 en-US 获取稳定的 weekday 缩写，避免 zh-CN 在不同平台的差异
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const parts = fmt.formatToParts(new Date())
  const weekday = parts.find((p) => p.type === 'weekday')?.value ?? ''
  const hourStr = parts.find((p) => p.type === 'hour')?.value ?? '0'
  const minuteStr = parts.find((p) => p.type === 'minute')?.value ?? '0'
  // hour 可能是 "24"（午夜），归一化为 0
  const hour = parseInt(hourStr, 10) % 24
  const minute = parseInt(minuteStr, 10)
  const dayMap: Record<string, number> = {
    Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6,
  }
  const day = dayMap[weekday] ?? -1
  const isWeekday = day >= 1 && day <= 5
  const timeVal = hour * 60 + minute
  const isMorningSession = timeVal >= 570 && timeVal <= 690 // 9:30-11:30
  const isAfternoonSession = timeVal >= 780 && timeVal <= 900 // 13:00-15:00
  return isWeekday && (isMorningSession || isAfternoonSession)
}

/**
 * 判断当前是否在 A 股交易时段（周一至周五 9:30-11:30 / 13:00-15:00，上海时间）
 *
 * 优先级：
 * 1. 后端 /market/status 缓存（由 AppShell 30s 轮询更新，包含交易日判断）
 * 2. Intl.DateTimeFormat 固定 Asia/Shanghai 时区的本地 fallback（仅 weekday+时间，不含节假日）
 */
export function isInTradingHours(): boolean {
  if (_cachedMarketStatus) {
    return _cachedMarketStatus.is_trading_hours
  }
  return isInTradingHoursShanghaiFallback()
}

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

/** 查询策略运行历史（admin，/admin 前缀路径） */
export function useAdminStrategyRuns(
  strategyKey: string | undefined,
  params?: { status?: string; limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['admin', 'strategies', strategyKey, 'runs', params],
    queryFn: () => api.getAdminStrategyRuns(strategyKey!, params),
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
      queryClient.invalidateQueries({ queryKey: ['admin', 'strategies', variables.strategyKey, 'runs'] })
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

/** 查询某策略的所有股票状态（支持 version 过滤，交易时段 30s 自动刷新） */
export function useStrategyMonitorStates(strategyKey: string | undefined, version?: string) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'monitor-states', version],
    queryFn: () => api.getStrategyMonitorStates(strategyKey!, version),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
    refetchInterval: () => isInTradingHours() ? 30000 : false,
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

/** 最近事件实测变更 */
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
export function useMessageDeliveries(params?: {
  status?: DeliveryStatus
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['admin', 'message-deliveries', params],
    queryFn: () => api.getMessageDeliveries(params),
    staleTime: STALE_REALTIME,
  })
}

/** 立即重试消息投递记录（admin） */
export function useRetryMessageDelivery() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (deliveryId: string) => api.retryMessageDelivery(deliveryId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'message-deliveries'] })
    },
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

/** 查询自选股+监控状态聚合数据（交易时段 30s 自动刷新） */
export function useWatchlistMonitorStatus() {
  return useQuery({
    queryKey: ['watchlist', 'monitor-status'],
    queryFn: api.getWatchlistMonitorStatus,
    staleTime: STALE_REALTIME,
    refetchInterval: () => isInTradingHours() ? 30000 : false,
  })
}

/** 查询定时任务运行记录（admin，10 秒轮询保持任务页 live） */
export function useSchedulerJobRuns(params?: {
  job_name?: string
  business_date?: string
  status?: string
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['admin', 'scheduler-job-runs', params],
    queryFn: () => api.getSchedulerJobRuns(params),
    staleTime: STALE_REALTIME,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  })
}

/** 查询 Worker 心跳记录（admin 只读，10 秒轮询同 useSchedulerJobRuns） */
export function useWorkerHeartbeats(params?: {
  status?: string
  worker_name?: string
  limit?: number
  offset?: number
}) {
  return useQuery({
    queryKey: ['admin', 'worker-heartbeats', params],
    queryFn: () => api.getWorkerHeartbeats(params),
    staleTime: STALE_REALTIME,
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  })
}

/** 加入自选变更（自动失效 watchlist + monitor-status 缓存） */
export function useAddToWatchlist() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: WatchlistAddRequest) => api.addToWatchlist(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      queryClient.invalidateQueries({ queryKey: ['watchlist', 'monitor-status'] })
    },
  })
}

/** 移除自选变更（自动失效 watchlist + monitor-status 缓存） */
export function useRemoveFromWatchlist() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (instrumentId: string) => api.removeFromWatchlist(instrumentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      queryClient.invalidateQueries({ queryKey: ['watchlist', 'monitor-status'] })
    },
  })
}

// ============================================================
// ===== Events Summary hooks =====
// ============================================================

/** 查询当前用户指定日期的策略事件汇总 */
export function useEventsSummary(date: string | undefined) {
  return useQuery({
    queryKey: ['me', 'events', 'summary', date],
    queryFn: () => api.getEventsSummary(date!),
    enabled: !!date,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================
// ===== Stock Memo hooks =====
// ============================================================

/** 查询当前用户对指定股票的备忘录 */
export function useStockMemo(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['stock-memo', instrumentId],
    queryFn: () => api.getStockMemo(instrumentId!),
    enabled: !!instrumentId,
    staleTime: 0,
  })
}

/** 创建/更新备忘录 */
export function useUpsertStockMemo() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ instrumentId, payload }: { instrumentId: string; payload: StockMemoUpsertRequest }) =>
      api.upsertStockMemo(instrumentId, payload),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['stock-memo', variables.instrumentId] })
    },
  })
}

/** 删除备忘录 */
export function useDeleteStockMemo() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (instrumentId: string) => api.deleteStockMemo(instrumentId),
    onSuccess: (_, instrumentId) => {
      queryClient.invalidateQueries({ queryKey: ['stock-memo', instrumentId] })
    },
  })
}

// ============================================================
// ===== Bars hooks =====
// ============================================================

/** 查询指定标的的行情数据（交易时段内 30s 轮询，响应式检测交易时段） */
export function useBars(instrumentId: string | undefined, params?: BarQueryParams, options?: { refetchInterval?: number | false }) {
  return useQuery({
    queryKey: ['bars', instrumentId, params],
    queryFn: () => api.getBars(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: options?.refetchInterval ?? (() => isInTradingHours() ? 30000 : false),
    refetchIntervalInBackground: false,
  })
}

/** 查询指定标的的所有策略图表指标（交易时段内 30s 轮询，响应式检测交易时段；页面隐藏时停止轮询） */
export function useIndicators(
  instrumentId: string | undefined,
  params?: IndicatorQueryParams,
  options?: { refetchInterval?: number | false },
) {
  return useQuery({
    // [DSA 数据契约] - queryKey 新增 'v3' 版本标识：后端响应新增 source_bar_times/source_bar_hash/visual_segments，
    //   旧缓存（无版本标识）结构不兼容，强制重新拉取
    queryKey: ['indicators', 'v3', instrumentId, params],
    queryFn: () => api.getIndicators(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: options?.refetchInterval ?? (() => isInTradingHours() ? 30000 : false),
    refetchIntervalInBackground: false,
  })
}

/** 查询指定标的的实时报价（交易时段内 10s 轮询，响应式检测交易时段；页面隐藏时停止轮询） */
export function useRealtimeQuote(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['quote', instrumentId],
    queryFn: () => api.getQuote(instrumentId!),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
    refetchInterval: () => isInTradingHours() ? 10000 : false,
    refetchIntervalInBackground: false,
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
// ===== Admin Membership hooks =====
// ============================================================

/** 查询所有 active 套餐定义（1 分钟缓存，公开端点） */
export function usePlans() {
  return useQuery<PlanResponse[], Error>({
    queryKey: ['plans'],
    queryFn: api.getPlans,
    staleTime: STALE_PLANS,
  })
}

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

/** 查询用户列表（admin） */
export function useAdminUsers(params?: PaginationParams) {
  return useQuery({
    queryKey: ['admin', 'users', params],
    queryFn: () => api.getAdminUsers(params),
    staleTime: STALE_REALTIME,
  })
}

/** 查询用户详情（admin） */
export function useAdminUser(userId: string | undefined) {
  return useQuery({
    queryKey: ['admin', 'users', userId],
    queryFn: () => api.getAdminUser(userId!),
    enabled: !!userId,
    staleTime: STALE_REALTIME,
  })
}

/** 启用用户账户（admin） */
export function useAdminEnableUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) => api.adminEnableUser(userId),
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
    mutationFn: (userId: string) => api.adminDisableUser(userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'members'] })
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
    }) => api.adminGrantSubscription(userId, payload),
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
    }) => api.adminRenewSubscription(userId, payload),
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
    mutationFn: (userId: string) => api.adminRevokeSubscription(userId),
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
    }) => api.adminChangeSubscriptionPlan(userId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
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
    queryFn: () => api.getAdminAuditLogs(params),
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
    queryFn: () => api.getAdminBetaApplications(params),
    staleTime: STALE_REALTIME,
  })
}

/** 获取内测申请统计数据 */
export function useAdminBetaApplicationStats() {
  return useQuery({
    queryKey: ['admin', 'beta-applications', 'stats'],
    queryFn: () => api.getAdminBetaApplicationStats(),
    staleTime: STALE_REALTIME,
  })
}

/** 获取内测申请详情 */
export function useAdminBetaApplicationDetail(appId: string | undefined) {
  return useQuery({
    queryKey: ['admin', 'beta-applications', appId, 'detail'],
    queryFn: () => api.getAdminBetaApplicationDetail(appId!),
    enabled: !!appId,
    staleTime: STALE_REALTIME,
  })
}

/** 修改内测申请状态（status + admin_note） */
export function useUpdateAdminBetaApplication() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ appId, payload }: { appId: string; payload: BetaApplicationPatchRequest }) =>
      api.updateAdminBetaApplication(appId, payload),
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
    mutationFn: (appId: string) => api.retryAdminBetaApplicationFeishu(appId),
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
    queryFn: api.getAdminSystemOverview,
    enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: enabled ? 15_000 : false,
    refetchIntervalInBackground: false,
  })
}

// ============================================================
// ===== Health hooks =====
// ============================================================

/** 获取后端健康状态（30 秒缓存，失败不阻断） */
export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: api.getHealth,
    staleTime: STALE_REALTIME,
    retry: false,
  })
}

// ============================================================
// ===== AfterClose & JobRunEvents hooks =====
// ============================================================

/** 查询任务执行事件时间线（抽屉打开时按需加载） */
export function useJobRunEvents(runId: string | null | undefined) {
  return useQuery({
    queryKey: ['job-runs', runId, 'events'],
    queryFn: () => api.getJobRunEvents(runId!),
    enabled: !!runId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询盘后编排状态（10 秒轮询，含事件时间线 + DSA run 状态） */
export function useAfterCloseRunStatus(runId: string | null | undefined, enabled: boolean = true) {
  return useQuery({
    queryKey: ['after-close-runs', runId],
    queryFn: () => api.getAfterCloseRunStatus(runId!),
    enabled: !!runId && enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: enabled ? 10_000 : false,
    refetchIntervalInBackground: false,
  })
}

/** 创建盘后编排变更 */
export function useCreateAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (tradeDate: string) => api.createAfterCloseRun(tradeDate),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

/** [Phase6] 仅重算今日 DSA 变更（要求当日日线覆盖率 ≥ 90%） */
export function useDsaOnlyRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (tradeDate: string) => api.createDsaOnlyRun(tradeDate),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

/** 重试盘后编排变更 */
export function useRetryAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => api.retryAfterCloseRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

/** [Phase6] 从失败步骤继续变更（保留断点检查点） */
export function useResumeAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => api.resumeAfterCloseRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

/** 强制重新执行盘后编排变更 */
export function useForceAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => api.forceAfterCloseRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

// ============================================================
// ===== AfterClose Pipeline 聚合状态 hooks（/admin/after-close/pipeline/*）=====
// ============================================================
//
// 轮询策略（遵循用户规范）：
// - running 状态 10 秒轮询
// - 非 running 状态 60 秒轮询
// - 页面不可见暂停轮询（refetchIntervalInBackground=false）
// - queryKey 与 useAdminSystemOverview / useAfterCloseRunStatus 隔离，避免缓存串扰

// [AfterClosePipeline] - 轮询间隔常量
const PIPELINE_POLL_RUNNING = 10_000 // running 状态 10s
const PIPELINE_POLL_IDLE = 60_000 // 非 running 状态 60s

/**
 * 查询最近交易日的盘后流水线聚合状态（admin）。
 * overall_status==='running' 时 10s 轮询，其余 60s 轮询，页面不可见暂停。
 * @param enabled 是否启用查询（默认 true，可用于页面卸载或权限不足时停止）
 */
export function useAfterClosePipelineLatest(enabled: boolean = true) {
  return useQuery({
    queryKey: ['after-close-pipeline', 'latest'],
    queryFn: api.getAfterClosePipelineLatest,
    enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: (query) => {
      const status = query.state.data?.overall_status
      return status === 'running' ? PIPELINE_POLL_RUNNING : PIPELINE_POLL_IDLE
    },
    refetchIntervalInBackground: false,
  })
}

/**
 * 查询指定交易日的盘后流水线聚合状态（admin）。
 * overall_status==='running' 时 10s 轮询，其余 60s 轮询，页面不可见暂停。
 * @param tradeDate 交易日（YYYY-MM-DD），undefined/null 时不启用查询
 * @param enabled 是否启用查询（默认 true）
 */
export function useAfterClosePipelineByDate(
  tradeDate: string | null | undefined,
  enabled: boolean = true,
) {
  return useQuery({
    queryKey: ['after-close-pipeline', 'by-date', tradeDate],
    queryFn: () => api.getAfterClosePipelineByDate(tradeDate!),
    enabled: !!tradeDate && enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: (query) => {
      const status = query.state.data?.overall_status
      return status === 'running' ? PIPELINE_POLL_RUNNING : PIPELINE_POLL_IDLE
    },
    refetchIntervalInBackground: false,
  })
}

/**
 * 查询最近 N 次运行列表（after_close_orchestrator + snapshot_run 混合）。
 * 60s 轮询，页面不可见暂停（列表非实时关键数据，统一 60s）。
 * @param limit 最多返回条数（默认 20，后端上限 100）
 * @param enabled 是否启用查询
 */
export function useAfterClosePipelineRuns(
  limit: number = 20,
  enabled: boolean = true,
) {
  return useQuery({
    queryKey: ['after-close-pipeline', 'runs', limit],
    queryFn: () => api.getAfterClosePipelineRuns(limit),
    enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: PIPELINE_POLL_IDLE,
    refetchIntervalInBackground: false,
  })
}

/**
 * 管理员触发指定交易日的 after_close 编排任务（admin，幂等）。
 * 同 trade_date 已有 queued/running/succeeded 时返回 existing，不重复创建。
 * 成功后失效 pipeline latest/by-date/runs 与 system-overview 缓存。
 */
export function useCreateAfterClosePipelineRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: AfterClosePipelineRunRequest) =>
      api.createAfterClosePipelineRun(payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ['after-close-pipeline', 'latest'] })
      queryClient.invalidateQueries({
        queryKey: ['after-close-pipeline', 'by-date', variables.trade_date],
      })
      queryClient.invalidateQueries({ queryKey: ['after-close-pipeline', 'runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

// ============================================================
// ===== Structural Factors hooks =====
// ============================================================

/**
 * 查询指定标的的双周期结构状态因子（1d + 15m）。
 * 交易时段 60s 轮询，非交易时段不轮询。
 * 前端只渲染后端 DTO，严禁重新计算。
 */
export function useStructuralFactors(
  instrumentId: string | undefined,
  params?: StructuralFactorQueryParams,
) {
  return useQuery({
    queryKey: ['structural-factors', instrumentId, params],
    queryFn: () => api.getStructuralFactors(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: () => isInTradingHours() ? 60000 : false,
  })
}

// ============================================================
// ===== Temporal Features hooks =====
// ============================================================

/**
 * 查询指定标的的时序特征 V1（daily_context + m15_response + derived_relation）。
 * 交易时段 60s 轮询，非交易时段不轮询。
 * 前端只渲染后端 DTO，严禁重新计算。
 */
export function useTemporalFeatures(
  instrumentId: string | undefined,
  params?: TemporalFeaturesQueryParams,
) {
  return useQuery({
    queryKey: ['temporal-features', instrumentId, params],
    queryFn: () => api.getTemporalFeatures(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: () => isInTradingHours() ? 60000 : false,
  })
}

// ============================================================
// ===== Table View Presets hooks =====
// ============================================================
// [Presets] - 描述: 用户表格视图配置 CRUD hooks（/me/table-view-presets）
// 缓存 key: ['table-view-presets', tableId, strategyKey]
// 变更后自动失效同维度缓存

/** 查询当前用户的 preset 列表（按 table_id + strategy_key 过滤） */
export function useTableViewPresets(tableId: string | undefined, strategyKey?: string | null) {
  return useQuery({
    queryKey: ['table-view-presets', tableId, strategyKey ?? null],
    queryFn: () => api.getTableViewPresets(tableId!, strategyKey ?? undefined),
    enabled: !!tableId,
    staleTime: 30000,
  })
}

/** 创建 preset（自动失效同维度缓存） */
export function useCreateTableViewPreset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: TableViewPresetCreateRequest) => api.createTableViewPreset(payload),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: ['table-view-presets', variables.table_id, variables.strategy_key ?? null],
      })
    },
  })
}

/** 更新 preset（自动失效同维度缓存） */
export function useUpdateTableViewPreset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: TableViewPresetPatchRequest }) =>
      api.updateTableViewPreset(id, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['table-view-presets'] })
    },
  })
}

/** 删除 preset（自动失效同维度缓存） */
export function useDeleteTableViewPreset() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteTableViewPreset(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['table-view-presets'] })
    },
  })
}

// ============================================================
// 类型重导出（方便页面直接引用）
// ============================================================

export type { UseQueryOptions }
export type { QuoteResponse } from '../api/endpoints'
export type { StructuralFactorQueryParams, StructuralFactorResponse } from '../api/endpoints'
export type { TemporalFeaturesQueryParams, TemporalFeaturesResponse } from '../api/endpoints'
