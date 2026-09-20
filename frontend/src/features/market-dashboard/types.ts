// [MarketDashboard] - feature 层 UI 类型（与 F2 API DTO 对齐 + 页面语义类型）
import type {
  MarketDashboardResponse,
  MarketDashboardCard,
  MarketDashboardPoint,
  RankingsResponse,
  RankingItem,
  BreadthPair,
  ScopeDetailResponse,
  ScopeMetadata,
  ScopePoint,
  CompareResponse,
  CompareBoard,
  ComparePoint,
} from '@/api/marketDashboard'

export type {
  MarketDashboardResponse,
  MarketDashboardCard,
  MarketDashboardPoint,
  RankingsResponse,
  RankingItem,
  BreadthPair,
  ScopeDetailResponse,
  ScopeMetadata,
  ScopePoint,
  CompareResponse,
  CompareBoard,
  ComparePoint,
}

export type ScopeType = 'industry' | 'concept'
export type HierarchyLevel = 'L1' | 'L2' | 'L3'

export const MARKET_DASHBOARD_ROUTES = {
  market: '/review/dashboard/market',
  industry: '/review/dashboard/industry',
  concept: '/review/dashboard/concept',
} as const

// 大盘 point 与板块详情 point 字段完全一致，共用同一图表数据点形状。
export interface BreadthPoint {
  trade_date: string
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma50: number | null
  ma120: number | null
  ew_index: number | null
}
