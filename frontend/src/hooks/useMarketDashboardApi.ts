// [MarketDashboard] - React Query hooks（server state 唯一来源，不引入 Zustand store）
import { useQuery } from '@tanstack/react-query'
import {
  getMarketDashboard,
  getMarketRankings,
  getMarketScopeDetail,
  getMarketCompare,
  getMarketScopeExplorer,
} from '@/api/marketDashboard'
import {
  buildScopeExplorerParams,
  scopeExplorerQueryKey,
  type ScopeExplorerQuery,
} from '@/features/market-dashboard/scopeExplorerQuery'
import type { ScopeType, HierarchyLevel } from '@/features/market-dashboard/types'

export const marketDashboardKeys = {
  market: (days: number) => ['market-dashboard', 'market', days] as const,
  rankings: (scopeType: ScopeType, hierarchyLevel: HierarchyLevel | null, lookback: number, limit: number) =>
    ['market-dashboard', 'rankings', scopeType, hierarchyLevel ?? 'none', lookback, limit] as const,
  scope: (boardId: string, days: number) => ['market-dashboard', 'scope', boardId, days] as const,
  compare: (boardIds: string[], days: number) =>
    ['market-dashboard', 'compare', [...boardIds].sort().join(','), days] as const,
  // [R3A] explorer key 由纯模块统一生成：必须覆盖全部 server-side state。
  scopeExplorer: (query: ScopeExplorerQuery) => scopeExplorerQueryKey(query),
}

const STALE = 30 * 1000

export function useMarketDashboard(days = 250) {
  return useQuery({
    queryKey: marketDashboardKeys.market(days),
    queryFn: () => getMarketDashboard(days),
    staleTime: STALE,
  })
}

export function useMarketRankings(
  scopeType: ScopeType,
  hierarchyLevel: HierarchyLevel | null,
  lookback = 5,
  limit = 10,
) {
  return useQuery({
    queryKey: marketDashboardKeys.rankings(scopeType, hierarchyLevel, lookback, limit),
    queryFn: () =>
      getMarketRankings({ scope_type: scopeType, hierarchy_level: hierarchyLevel ?? undefined, lookback, limit }),
    // concept 始终就绪；industry 需已选层级（默认 L1，故基本总是就绪）。
    enabled: scopeType === 'concept' || hierarchyLevel !== null,
    staleTime: STALE,
  })
}

export function useMarketScopeDetail(boardId: string | null, days = 250) {
  return useQuery({
    queryKey: marketDashboardKeys.scope(boardId ?? '', days),
    queryFn: () => getMarketScopeDetail(boardId as string, days),
    enabled: !!boardId,
    staleTime: STALE,
  })
}

export function useMarketCompare(boardIds: string[], days = 10) {
  return useQuery({
    queryKey: marketDashboardKeys.compare(boardIds, days),
    queryFn: () => getMarketCompare(boardIds, days),
    enabled: boardIds.length > 0 && boardIds.length <= 20,
    staleTime: STALE,
  })
}

/**
 * [R3A] Scope Explorer（行业/概念**全集**，filter / sort / pagination 全部 server-side）。
 *
 * query key 覆盖全部 server-side state（见 `scopeExplorerQueryKey`），因此换页 / 换排序 /
 * 换 filter 都不会命中上一份缓存；**不做**任何前端全量 filter/sort/page。
 */
export function useMarketScopeExplorer(query: ScopeExplorerQuery) {
  return useQuery({
    queryKey: marketDashboardKeys.scopeExplorer(query),
    queryFn: () => getMarketScopeExplorer(buildScopeExplorerParams(query)),
    staleTime: STALE,
  })
}
