// [useReviewScopeDetail] - 描述: Canonical Scope Detail 唯一 React Query owner（Slice E）
//
// 合同（prompt §1、§19）：
// - 一个已选 Scope → 恰好一个 detail 请求。
// - scopeKey == null → 不发 detail 请求（enabled=false）。
// - 切换 tab 不得产生不同 detail query identity（tab 不是 detail key 的 input）。
// - 切换 Scope / date / family 自然改变 query identity（key 含 tradeDate+scopeType+scopeKey+includePartial）。
// - Table / Trajectory 行绝不发 detail：detail 只由已选 Scope 驱动。
// - 不放 Zustand server state。
//
// 纯逻辑部分 scopeDetailQueryOptions / isScopeDetailEnabled 可被 node --test 直接测试。
// 组件仅消费 parseXxx 解析后的 ViewModel，不在面板内散落 `as SomeType`。
import { useQuery } from '@tanstack/react-query'
import { getReviewScopeDetail } from './api'
import { reviewKeys } from './queryKeys'
import type {
  ReviewScopeCompositionDetailResponse,
  ReviewScopeFamily,
} from './types'

export interface ScopeDetailQueryInput {
  tradeDate: string | null
  /** 详情 endpoint 的 scope_type：canonical 下等于 URL family */
  scopeType: ReviewScopeFamily | string | null
  scopeKey: string | null
  includePartial?: boolean
}

/** 纯判定：是否应发起 detail 请求（scopeKey/date/type 齐全才启用）。
 *  可用于测试证明「无 scopeKey => 不发 detail」与「有选中 Scope => enabled」。 */
export function isScopeDetailEnabled(input: ScopeDetailQueryInput): boolean {
  return !!input.tradeDate && !!input.scopeType && !!input.scopeKey
}

/** 纯构造 detail React Query options（不含 tab：切换 tab 不改变 key）。
 *  返回可序列化对象，便于 node --test 校验 identity。 */
export function scopeDetailQueryOptions(input: ScopeDetailQueryInput) {
  const tradeDate = input.tradeDate ?? ''
  const scopeType = input.scopeType ?? ''
  const scopeKey = input.scopeKey ?? ''
  const includePartial = input.includePartial ?? false
  return {
    queryKey: reviewKeys.scopeDetail(tradeDate, scopeType, scopeKey, includePartial),
    queryFn: (): Promise<ReviewScopeCompositionDetailResponse> =>
      getReviewScopeDetail(tradeDate, scopeType, scopeKey, includePartial),
    enabled: isScopeDetailEnabled(input),
    staleTime: 5 * 60 * 1000,
  }
}

/** 已选 Scope 的唯一 detail owner。scopeKey 变化 → key 变化 → 新请求；tab 变化 → 同 key。 */
export function useReviewScopeDetail(input: ScopeDetailQueryInput) {
  return useQuery(scopeDetailQueryOptions(input))
}

export type UseReviewScopeDetailResult = ReturnType<typeof useReviewScopeDetail>