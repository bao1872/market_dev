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
  CreateChannelRequest,
  NotificationPreviewRequest,
  InstrumentQueryParams,
  BarQueryParams,
  CalendarQueryParams,
  IndicatorQueryParams,
  StockMemoUpsertRequest,
  PlanResponse,
  StructuralFactorQueryParams,
  TemporalFeaturesQueryParams,
  TableViewPresetCreateRequest,
  TableViewPresetPatchRequest,
  ChartSnapshotQueryParams,
} from '../api/endpoints'

// [S3-B] 市场时钟单向引入（供本文件保留的 hooks 使用；实现 owner 为 ./marketRuntime）。
import { isInTradingHours } from './marketRuntime'

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
// 市场时钟（isInTradingHours / cached market status）
// ============================================================
//
// [S3-B] 实现已迁至 ./marketRuntime（唯一 owner）。此处 re-export 保持既有
// `import { setCachedMarketStatus } from '@/hooks/useApi'` 调用方零改动。

export { setCachedMarketStatus, getCachedMarketStatus, isInTradingHours } from './marketRuntime'

// ============================================================
// ===== Auth hooks =====
// ============================================================
//
// [S3-A] 实现已迁至 ./useAuthApi（唯一 owner），此处以兼容 barrel 重新导出，
// 保持既有 `import { useMe } from '@/hooks/useApi'` 调用方零改动。

export {
  useMe,
  useMyMembership,
  useLogin,
  useRegister,
  useRenew,
  useRefreshToken,
} from './useAuthApi'

// ============================================================
// ===== Instruments hooks =====
// ============================================================

/** 查询股票列表 */
export function useInstruments(
  params?: InstrumentQueryParams,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['instruments', params],
    queryFn: () => api.getInstruments(params),
    staleTime: STALE_PLANS,
    enabled: options?.enabled ?? true,
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

// ============================================================
// ===== Market hooks =====
// ============================================================
//
// [S3-B] 实现已迁至 ./useMarketApi（唯一 owner），此处以兼容 barrel 重新导出。

export {
  useMarketStocks,
  useMarketBoards,
  useMarketFilterSpecs,
  useMarketSessionReactive,
} from './useMarketApi'

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
// ===== Strategy hooks =====
// ============================================================
//
// [S3-C] 非 admin strategy hooks 已迁至 ./useStrategyApi（唯一 owner），
// 此处以兼容 barrel 重新导出。

export {
  useStrategies,
  useStrategy,
  useStrategyVersions,
  useStrategyVersionSchema,
  useStrategyRuns,
  usePublishedRuns,
  useStrategyRunResults,
  useInstrumentMonitorStates,
  useStrategyMonitorStates,
  useInstrumentEvents,
  useStrategyEvents,
  useStrategyEventDetail,
} from './useStrategyApi'

// ============================================================
// Admin / AfterClose hooks —— 实现已迁至 ./useAdminApi 与 ./useAdminAfterCloseApi
// ============================================================

export {
  useAdminStrategyRuns,
  useTriggerStrategyRun,
  useAdminUserChannels,
  useAdminCreateUserChannel,
  useAdminUpdateUserChannel,
  useAdminDeleteUserChannel,
  useAdminVerifyUserChannel,
  useAdminTestUserChannel,
  useInviteCodes,
  useCreateInviteCodes,
  useRevokeInviteCode,
  useMembers,
  useMemberRedemptions,
  useAdminUsers,
  useAdminUser,
  useAdminEnableUser,
  useAdminDisableUser,
  useAdminResetUserPassword,
  useAdminGrantSubscription,
  useAdminRenewSubscription,
  useAdminRevokeSubscription,
  useAdminChangeSubscriptionPlan,
  useUserCapabilities,
  useAdminGrantCapability,
  useAdminRevokeCapability,
  useAdminAuditLogs,
  useAdminBetaApplications,
  useAdminBetaApplicationStats,
  useAdminBetaApplicationDetail,
  useUpdateAdminBetaApplication,
  useRetryAdminBetaApplicationFeishu,
  useAdminSystemOverview,
  useAdminProductReadiness,
  useAdminStockDebug,
  useMessageDeliveries,
  useRetryMessageDelivery,
  useSchedulerJobRuns,
  useWorkerHeartbeats,
  useAdminVisitors,
  useTriggerComputeBoard,
  useTriggerComputeAllBoards
} from './useAdminApi'

export {
  useJobRunEvents,
  useAfterCloseRunStatus,
  useCreateAfterCloseRun,
  useForceAfterCloseRun,
  useRetryAfterCloseRun,
  useResumeAfterCloseRun,
  useAfterClosePipelineLatest,
  useAfterClosePipelineByDate,
  useAfterClosePipelineRuns,
  useCreateAfterClosePipelineRun,
  useCancelAfterCloseRun,
  useReconcileAfterCloseRun,
  useRestartAfterCloseRun,
  useForceRestartAfterCloseRun
} from './useAdminAfterCloseApi'


// ============================================================
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
// ===== Watchlist hooks =====
// ============================================================
//
// [S3-B] 实现已迁至 ./useWatchlistApi（唯一 owner），此处以兼容 barrel 重新导出。

export {
  useWatchlist,
  useWatchlistMonitorStatus,
  useAddToWatchlist,
  useRemoveFromWatchlist,
} from './useWatchlistApi'

/** 查询定时任务运行记录（admin，10 秒轮询保持任务页 live） */

/** 查询 Worker 心跳记录（admin 只读，10 秒轮询同 useSchedulerJobRuns） */

/** [Gate5] 查询访问统计报告（admin only，5 分钟刷新一次） */

// [CHANGE-20260730-011] 板块分析 V1 hooks
/** 查询板块分析列表 */
export function useBoardAnalysisList(
  params?: api.BoardAnalysisListParams,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['board-analysis', 'list', params],
    queryFn: ({ signal }) => api.getBoardAnalysisList(params, { signal }),
    staleTime: 60 * 1000, // 1 分钟
    enabled: options?.enabled ?? true,
  })
}

/** 查询单板块分析详情 */
export function useBoardAnalysisDetail(
  boardId: string | null,
  params?: { trade_date?: string },
) {
  return useQuery({
    queryKey: ['board-analysis', 'detail', boardId, params],
    queryFn: ({ signal }) =>
      api.getBoardAnalysisDetail(boardId!, params, { signal }),
    enabled: !!boardId,
    staleTime: 60 * 1000,
  })
}

/** [Admin] 触发单板块分析计算 */

/** [Admin] 触发批量板块分析计算 */

// [S3-B] useAddToWatchlist / useRemoveFromWatchlist 已迁至 ./useWatchlistApi
// （见本文件 "Watchlist hooks" 兼容 re-export）。

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
    queryFn: ({ signal }) => api.getBars(instrumentId!, params, { signal }),
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
    queryFn: ({ signal }) => api.getIndicators(instrumentId!, params, { signal }),
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
    queryFn: ({ signal }) => api.getQuote(instrumentId!, { signal }),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
    refetchInterval: () => isInTradingHours() ? 10000 : false,
    refetchIntervalInBackground: false,
  })
}

/**
 * [PRD V2.0 §4.2 SNAP-01 Atomic Chart Snapshot] 个股详情页原子图表快照。
 *
 * 一次 MDAS DataFrame 同时返回 bars + indicators + render_frame，
 * 替代详情页独立的 useBars + useIndicators 两次实时请求，
 * 保证前端拿到的 bars 与 indicators 基于同一份后端 DataFrame 生成。
 *
 * render_frame.matched=false 时前端不得 Ready（与 Capture 同款合同）。
 *
 * 轮询策略与 useBars/useIndicators 一致：交易时段 30s，非交易时段不轮询。
 */
export function useChartSnapshot(
  instrumentId: string | undefined,
  params?: ChartSnapshotQueryParams,
  options?: { refetchInterval?: number | false },
) {
  return useQuery({
    // queryKey 包含 'v1' 版本标识，便于未来响应结构变更时强制刷新缓存
    queryKey: ['chart-snapshot', 'v1', instrumentId, params],
    queryFn: ({ signal }) => api.getChartSnapshot(instrumentId!, params, { signal }),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: options?.refetchInterval ?? (() => isInTradingHours() ? 30000 : false),
    refetchIntervalInBackground: false,
    // [P0-8] hidden 恢复时立即刷新（React Query 默认 refetchOnWindowFocus=true）
    refetchOnWindowFocus: true,
  })
}

// [S3-B] useMarketSessionReactive 已迁至 ./useMarketApi（见 "Market hooks" 兼容 re-export）。

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
/** 查询会员账户列表 */
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
// ===== Stock Context hooks (PRD V1.1 §7.3) =====
// ============================================================
// [StockContext] - 描述: 用户侧只读状态向量接口
// 单一接口替代 structural/temporal/event 多请求链
// 右栏关闭时通过 enabled=false 停止请求

/** 查询用户侧 StockContext（/v1/stocks/{symbol}/context） */
export function useStockContext(
  symbol: string | undefined,
  params?: { as_of?: string },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['stock-context', symbol, params ?? null],
    queryFn: ({ signal }) => api.getStockContext(symbol!, params, { signal }),
    enabled: !!symbol && (options?.enabled ?? true),
    staleTime: STALE_REALTIME,
  })
}

/**
 * [Phase 5B-2] 查询第一金字塔统一快照（/v1/stocks/{symbol}/first-pyramid）。
 * - 固定维度顺序：trend → structure → momentum → chip_consensus
 * - 前三维必选，chip_consensus 可选（无有效峰时为 null）
 * - [CHANGE-20260730-012] 只读已发布 stock_core 快照；无快照返回结构化 unavailable
 * - retry=1 避免无限重试；明确 loading/error/empty 状态
 */
export function useFirstPyramid(
  symbol: string | undefined,
  params?: { as_of?: string },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['first-pyramid', symbol, params ?? null],
    queryFn: ({ signal }) => api.getFirstPyramid(symbol!, params, { signal }),
    enabled: !!symbol && (options?.enabled ?? true),
    staleTime: STALE_REALTIME,
    retry: 1,
  })
}

// ============================================================
// 类型重导出（方便页面直接引用）
// ============================================================

export type { UseQueryOptions }
export type { QuoteResponse } from '../api/endpoints'
export type { StructuralFactorQueryParams, StructuralFactorResponse } from '../api/endpoints'
export type { TemporalFeaturesQueryParams, TemporalFeaturesResponse } from '../api/endpoints'
export type {
  AtomicFactsContextResponse,
  AdminStockDebugResponse,
  AtomicFactItem,
  AtomicFactAvailability,
  AtomicFactChange,
  AdminAtomicFactDebugItem,
  StockContextDataQuality,
} from '../api/endpoints'
