// [ReviewQueryKeys] - 描述: React Query key 工厂（PRD §15）
// 规则：query key 必须包含 reviewRunId/tradeDate/resource/id/filters
// 切换 signal 时通过 queryKey 隔离，自动取消无效请求
//
// [CANONICAL] Scope-first 合同（Slice C）：
// - dates/latest/overview/scopes/scopeDetail 为 canonical Review API surface。
// - scopeDetail key 必须包含 tradeDate + scopeType + scopeKey + includePartial（不能只靠 scopeKey）。
// [LEGACY] 信号/归因/个股/追踪/Discovery 仅剩 legacy 消费者，Slice F 删除。
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

  // [CANONICAL] 日期与总览
  dates: () => [...reviewKeys.all, 'dates'] as const,
  latest: () => [...reviewKeys.all, 'latest'] as const,
  overview: (tradeDate: string, includePartial = false) =>
    [...reviewKeys.all, 'overview', tradeDate, { includePartial }] as const,

  // [CANONICAL] Scope-first 列表（PRD §12.2）
  scopes: (tradeDate: string, filters: ReviewScopeListParams = {}) =>
    [...reviewKeys.all, 'scopes', tradeDate, filters] as const,

  // [CANONICAL] 单 Scope 详情；identity 必须含 tradeDate + scopeType + scopeKey + includePartial
  scopeDetail: (
    tradeDate: string,
    scopeType: string,
    scopeKey: string,
    includePartial = false,
  ) =>
    [
      ...reviewKeys.all,
      'scopeDetail',
      tradeDate,
      scopeType,
      scopeKey,
      { includePartial },
    ] as const,

  // [LEGACY] 信号（PRD §12.3；Slice F 删除）
  signals: (tradeDate: string, filters: ReviewSignalListParams = {}) =>
    [...reviewKeys.all, 'signals', tradeDate, filters] as const,
  signal: (signalId: string, includePartial = false) =>
    [...reviewKeys.all, 'signal', signalId, { includePartial }] as const,

  // [LEGACY] 归因与个股（PRD §12.4；Slice F 删除）
  attributions: (signalId: string, filters: ReviewAttributionListParams = {}) =>
    [...reviewKeys.all, 'attributions', signalId, filters] as const,
  instruments: (signalId: string, filters: ReviewInstrumentListParams = {}) =>
    [...reviewKeys.all, 'instruments', signalId, filters] as const,

  // [LEGACY] 追踪（PRD §12.5；Slice F 删除）
  trackings: (filters: ReviewTrackingListParams = {}) =>
    [...reviewKeys.all, 'trackings', filters] as const,
  tracking: (trackingId: string) =>
    [...reviewKeys.all, 'tracking', trackingId] as const,
  evaluations: (trackingId: string, filters: { page?: number; page_size?: number } = {}) =>
    [...reviewKeys.all, 'evaluations', trackingId, filters] as const,

  // [LEGACY] [V2] Discovery（Slice F 删除）
  discoveries: (tradeDate: string, filters: Record<string, unknown> = {}) =>
    [...reviewKeys.all, 'discoveries', tradeDate, filters] as const,
  discovery: (discoveryId: string, tradeDate?: string) =>
    [...reviewKeys.all, 'discovery', discoveryId, tradeDate] as const,
} as const

/** 导出方便组件使用 */
export { reviewKeys as queryKeys }
