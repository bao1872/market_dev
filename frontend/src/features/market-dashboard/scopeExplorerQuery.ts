// [MarketDashboard][R3A] - Scope Explorer 纯 query 语义（无 React / axios / DOM 依赖，可单测）
//
// 与 backend `app/services/market_dashboard_scope_explorer_service.py` **1:1**：
//   - SORT_FIELDS：API 层字段名是 "name"（不是 "board_name"）
//   - RANGE_PAIRS：ma5 / ma10 / ma5_delta / ma10_delta / member_count 的 min+max
//   - backend 默认值：page=1、page_size=20、sort="ma5"、direction="desc"
//   - concept **不得**发送 hierarchy_level（backend 对 concept+hierarchy_level 是显式 422，
//     **不是** rankings 那种静默忽略）
//
// 硬约束：前端只做「参数序列化 / query key」，绝不重算 ratio / delta / 排序 / 分页。
import type { HierarchyLevel, ScopeType } from './types'

export type ScopeExplorerSort =
  | 'name'
  | 'member_count'
  | 'ma5'
  | 'ma10'
  | 'ma20'
  | 'ma50'
  | 'ma120'
  | 'ma5_delta'
  | 'ma10_delta'

export type SortDirection = 'asc' | 'desc'

/** 与 backend SORT_FIELDS 逐项一致（顺序也保持一致，便于对照）。 */
export const SCOPE_EXPLORER_SORT_FIELDS: readonly ScopeExplorerSort[] = [
  'name',
  'member_count',
  'ma5',
  'ma10',
  'ma20',
  'ma50',
  'ma120',
  'ma5_delta',
  'ma10_delta',
]

export const SCOPE_EXPLORER_PAGE_SIZE_DEFAULT = 20
export const SCOPE_EXPLORER_PAGE_SIZE_MAX = 100
export const SCOPE_EXPLORER_SORT_DEFAULT: ScopeExplorerSort = 'ma5'
export const SCOPE_EXPLORER_DIRECTION_DEFAULT: SortDirection = 'desc'

/** 与 backend RANGE_PAIRS 对应的全部区间参数（顺序稳定，供 query key 复用）。 */
export const SCOPE_EXPLORER_RANGE_KEYS = [
  'ma5_min',
  'ma5_max',
  'ma10_min',
  'ma10_max',
  'ma5_delta_min',
  'ma5_delta_max',
  'ma10_delta_min',
  'ma10_delta_max',
  'member_count_min',
  'member_count_max',
] as const

export type ScopeExplorerRangeKey = (typeof SCOPE_EXPLORER_RANGE_KEYS)[number]

/** 前端侧 query（server-side state 全量；page/page_size/sort/direction 可省略走默认）。 */
export interface ScopeExplorerQuery {
  scope_type: ScopeType
  hierarchy_level?: HierarchyLevel | null
  q?: string | null
  page?: number
  page_size?: number
  sort?: ScopeExplorerSort
  direction?: SortDirection
  ma5_min?: number | null
  ma5_max?: number | null
  ma10_min?: number | null
  ma10_max?: number | null
  ma5_delta_min?: number | null
  ma5_delta_max?: number | null
  ma10_delta_min?: number | null
  ma10_delta_max?: number | null
  member_count_min?: number | null
  member_count_max?: number | null
}

export const DEFAULT_SCOPE_EXPLORER_QUERY = {
  page: 1,
  page_size: SCOPE_EXPLORER_PAGE_SIZE_DEFAULT,
  sort: SCOPE_EXPLORER_SORT_DEFAULT,
  direction: SCOPE_EXPLORER_DIRECTION_DEFAULT,
} as const

/**
 * 序列化为 axios params（snake_case，与 backend Query 参数名一致）。
 *
 * 规则：
 *   - page/page_size/sort/direction 未给 → 用 backend 默认值（语义等价，但保证 key 稳定）；
 *   - hierarchy_level 仅 industry 且明确给出时才发送（concept 一律不发送）；
 *   - q 仅非空（trim 后）时发送；
 *   - 区间参数 null/undefined 一律不发送（不发送 ≠ 发送 0）。
 *
 * 注意：这里**不**做业务校验（min>max 等由 backend 返回 422，前端不静默改写语义）。
 */
export function buildScopeExplorerParams(query: ScopeExplorerQuery): Record<string, string | number> {
  const params: Record<string, string | number> = {
    scope_type: query.scope_type,
    page: query.page ?? DEFAULT_SCOPE_EXPLORER_QUERY.page,
    page_size: query.page_size ?? DEFAULT_SCOPE_EXPLORER_QUERY.page_size,
    sort: query.sort ?? DEFAULT_SCOPE_EXPLORER_QUERY.sort,
    direction: query.direction ?? DEFAULT_SCOPE_EXPLORER_QUERY.direction,
  }
  if (query.scope_type === 'industry' && query.hierarchy_level) {
    params.hierarchy_level = query.hierarchy_level
  }
  const q = query.q?.trim()
  if (q) params.q = q
  for (const key of SCOPE_EXPLORER_RANGE_KEYS) {
    const value = query[key]
    if (value !== null && value !== undefined) params[key] = value
  }
  return params
}

/**
 * React Query key：必须覆盖**全部** server-side state。
 *
 * 任一字段变化都必须产生新 key（否则缓存串味：换页/换排序/换 filter 会命中上一份数据）。
 * 缺失值使用稳定占位符（'none' / '' / 'any'），避免 undefined 与 0 混淆。
 */
export function scopeExplorerQueryKey(query: ScopeExplorerQuery): readonly (string | number)[] {
  const params = buildScopeExplorerParams(query)
  return [
    'market-dashboard',
    'scopes',
    String(params.scope_type),
    params.hierarchy_level === undefined ? 'none' : String(params.hierarchy_level),
    params.q === undefined ? '' : String(params.q),
    params.page,
    params.page_size,
    params.sort,
    params.direction,
    ...SCOPE_EXPLORER_RANGE_KEYS.map((key) => (params[key] === undefined ? 'any' : params[key])),
  ]
}
