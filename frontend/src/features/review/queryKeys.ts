// [ReviewQueryKeys] - 描述: React Query key 工厂（PRD §15）
// 规则：query key 必须包含 reviewRunId/tradeDate/resource/id/filters
// 切换 signal 时通过 queryKey 隔离，自动取消无效请求
import type {
  ReviewScopeListParams,
  ReviewSignalListParams,
  ReviewAttributionListParams,
  ReviewInstrumentListParams,
  ReviewTrackingListParams,
} from './types'

/** 复盘模块统一 key 前缀 */
export const reviewKeys = {
  all: ['review'] as const,

  // 日期与总览
  dates: () => [...reviewKeys.all, 'dates'] as const,
  latest: () => [...reviewKeys.all, 'latest'] as const,
  overview: (tradeDate: string, includePartial = false) =>
    [...reviewKeys.all, 'overview', tradeDate, { includePartial }] as const,

  // 市场扫描（PRD §12.2）
  scopes: (tradeDate: string, filters: ReviewScopeListParams = {}) =>
    [...reviewKeys.all, 'scopes', tradeDate, filters] as const,

  // 信号（PRD §12.3）
  signals: (tradeDate: string, filters: ReviewSignalListParams = {}) =>
    [...reviewKeys.all, 'signals', tradeDate, filters] as const,
  signal: (signalId: string, includePartial = false) =>
    [...reviewKeys.all, 'signal', signalId, { includePartial }] as const,

  // 归因与个股（PRD §12.4）
  attributions: (signalId: string, filters: ReviewAttributionListParams = {}) =>
    [...reviewKeys.all, 'attributions', signalId, filters] as const,
  instruments: (signalId: string, filters: ReviewInstrumentListParams = {}) =>
    [...reviewKeys.all, 'instruments', signalId, filters] as const,

  // 追踪（PRD §12.5）
  trackings: (filters: ReviewTrackingListParams = {}) =>
    [...reviewKeys.all, 'trackings', filters] as const,
  tracking: (trackingId: string) =>
    [...reviewKeys.all, 'tracking', trackingId] as const,
  evaluations: (trackingId: string, filters: { page?: number; page_size?: number } = {}) =>
    [...reviewKeys.all, 'evaluations', trackingId, filters] as const,

  // [V2] Discovery
  discoveries: (tradeDate: string, filters: Record<string, unknown> = {}) =>
    [...reviewKeys.all, 'discoveries', tradeDate, filters] as const,
  discovery: (discoveryId: string) =>
    [...reviewKeys.all, 'discovery', discoveryId] as const,
} as const

/** 导出方便组件使用 */
export { reviewKeys as queryKeys }
