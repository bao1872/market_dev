// [ReviewQueryKeys] - 描述: React Query key 工厂（PRD §15）
// 规则：query key 必须包含 reviewRunId/tradeDate/resource/id/filters
// 切换 signal 时通过 queryKey 隔离，自动取消无效请求
//
// [CANONICAL] Scope-first 合同（Slice C + Slice F 退休完成）：
// - dates/latest/overview/scopes/familySnapshot/scopeDetail 为 canonical Review API surface。
// - scopeDetail key 必须包含 tradeDate + scopeType + scopeKey + includePartial（不能只靠 scopeKey）。
import type {
  ReviewScopeListParams,
  ReviewScopeFamily,
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

  // [CANONICAL] 完整 family snapshot（transport aggregation，按 tradeDate + family 缓存）
  familySnapshot: (tradeDate: string, family: ReviewScopeFamily) =>
    [...reviewKeys.all, 'familySnapshot', tradeDate, family] as const,

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
} as const

/** 导出方便组件使用 */
export { reviewKeys as queryKeys }
