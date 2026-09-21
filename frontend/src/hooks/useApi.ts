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

import { useQuery } from '@tanstack/react-query'
import type { UseQueryOptions } from '@tanstack/react-query'
import * as api from '../api/endpoints'
import type { PlanResponse } from '../api/endpoints'

const STALE_PLANS = 60 * 1000 // 方案列表 1 分钟
const STALE_REALTIME = 30 * 1000 // 实时数据 30 秒

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
// Notification / StockData / BoardAnalysis / Preferences hooks —— 已迁至对应 owner
// ============================================================

export {
  useMessages,
  useUnreadCount,
  useMarkMessageRead,
  useReadAllMessages,
  useNotificationChannels,
  useCreateNotificationChannel,
  useUpdateNotificationChannel,
  useDeleteNotificationChannel,
  useVerifyNotificationChannel,
  useTestNotificationChannel,
  useTestNotificationChannelLatestEvent,
  usePreviewNotification
} from './useNotificationApi'

export {
  useInstruments,
  useBatchInstruments,
  useInstrument,
  useInstrumentBySymbol,
  useEventsSummary,
  useStockMemo,
  useUpsertStockMemo,
  useDeleteStockMemo,
  useBars,
  useIndicators,
  useRealtimeQuote,
  useChartSnapshot,
  useCalendar,
  useIsTradingDay,
  useStructuralFactors,
  useTemporalFeatures,
  useStockContext,
  useFirstPyramid
} from './useStockDataApi'

export {
  useMarketDashboard,
  useMarketRankings,
  useMarketScopeDetail,
  useMarketCompare,
  marketDashboardKeys
} from './useMarketDashboardApi'

export {
  useTableViewPresets,
  useCreateTableViewPreset,
  useUpdateTableViewPreset,
  useDeleteTableViewPreset
} from './usePreferencesApi'



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
// ===== Events Summary hooks =====
// ============================================================

/** 查询当前用户指定日期的策略事件汇总 */
// ===== Stock Memo hooks =====
// ============================================================

/** 查询当前用户对指定股票的备忘录 */
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
// ===== Table View Presets hooks =====
// ============================================================
// [Presets] - 描述: 用户表格视图配置 CRUD hooks（/me/table-view-presets）
// 缓存 key: ['table-view-presets', tableId, strategyKey]
// 变更后自动失效同维度缓存

/** 查询当前用户的 preset 列表（按 table_id + strategy_key 过滤） */
// ===== Stock Context hooks (PRD V1.1 §7.3) =====
// ============================================================
// [StockContext] - 描述: 用户侧只读状态向量接口
// 单一接口替代 structural/temporal/event 多请求链
// 右栏关闭时通过 enabled=false 停止请求


// ============================================================
// 类型重导出（方便页面直接引用）
// ============================================================

export type { UseQueryOptions }
export type { QuoteResponse } from '../api/stockData'
export type { StructuralFactorQueryParams, StructuralFactorResponse } from '../api/stockData'
export type { TemporalFeaturesQueryParams, TemporalFeaturesResponse } from '../api/stockData'
export type {
  AtomicFactsContextResponse,
  AtomicFactItem,
  AtomicFactAvailability,
  AtomicFactChange,
  StockContextDataQuality,
} from '../api/stockData'
export type { AdminStockDebugResponse, AdminAtomicFactDebugItem } from '../api/admin'
