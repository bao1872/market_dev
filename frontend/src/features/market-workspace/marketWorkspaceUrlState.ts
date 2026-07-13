// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 纯函数
// URL 格式：/market?scope=watchlist|market&selected=xxx
// scope/selected 由本模块管理；sort/dir/keyword/filters/page/page_size 由 StrategyDataTable 内置 screenerUrlState 管理。
// 右栏折叠和 viewport 留本地 state。
// 本文件为纯 TS（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。

export type MarketScope = 'watchlist' | 'market'

export interface MarketWorkspaceUrlState {
  scope: MarketScope
  selected: string | null
  industry: string | null
  concept: string | null
}

export const DEFAULT_MARKET_SCOPE: MarketScope = 'watchlist'
export const DEFAULT_PAGE = 1
export const DEFAULT_PAGE_SIZE = 50
export const MAX_PAGE_SIZE = 100

// 从 URLSearchParams 解析工作区状态（仅 scope + selected；sort/filters/page 由 StrategyDataTable 管理）
export function decodeMarketWorkspaceUrl(params: URLSearchParams): MarketWorkspaceUrlState {
  const rawScope = params.get('scope')
  const scope: MarketScope = rawScope === 'market' ? 'market' : 'watchlist'
  const selected = params.get('selected') ?? null
  const industry = params.get('industry') ?? null
  const concept = params.get('concept') ?? null
  return { scope, selected, industry, concept }
}

// 将工作区状态编码为 URLSearchParams（仅 scope + selected）
// 规则：scope 始终写入；selected 存在才写入。
export function encodeMarketWorkspaceUrl(state: MarketWorkspaceUrlState): URLSearchParams {
  const params = new URLSearchParams()
  params.set('scope', state.scope)
  if (state.selected) {
    params.set('selected', state.selected)
  }
  if (state.industry) {
    params.set('industry', state.industry)
  }
  if (state.concept) {
    params.set('concept', state.concept)
  }
  return params
}

// 将工作区状态编码为完整 URL query string（用于 navigate / returnTo）
export function buildMarketWorkspaceUrl(state: MarketWorkspaceUrlState): string {
  const params = encodeMarketWorkspaceUrl(state)
  const qs = params.toString()
  return qs ? `/market?${qs}` : '/market'
}

// 从列表中单击非链接区域选择股票时的状态转换（纯函数）。
// 设置 selected，保留 scope。
export function selectInstrumentInTable(
  state: MarketWorkspaceUrlState,
  symbol: string,
): MarketWorkspaceUrlState {
  return {
    ...state,
    selected: symbol,
  }
}

// 切换 scope 时的状态转换（纯函数）。
// 切换 scope 后清除 selected（PRD §6.1：筛选变化重置选中）。
// 保留 sort/filters/page（由 StrategyDataTable 管理，不在本 state 中）。
export function changeMarketScope(
  state: MarketWorkspaceUrlState,
  newScope: MarketScope,
): MarketWorkspaceUrlState {
  return {
    ...state,
    scope: newScope,
    selected: null,
  }
}

// returnTo 安全校验：仅允许 /screener /market /messages 前缀的内部路径（含 query/hash）。
// 拒绝：外部 URL（http:// https:// //）、javascript:、超长字符串（>500）、非白名单前缀。
// CHANGE-20260713-009: 限制从 200 提升到 500，因为 /market URL 含 filters JSON 编码后可能超过 200 字符。
// 500 仍能防止滥用，同时允许真实的 /market?scope=market&filters=[...]&keyword=...&industry=... URL。
export function normalizeInternalReturnTo(raw: string | null | undefined): string | null {
  if (!raw) return null
  if (raw.length > 500) return null
  const trimmed = raw.trim()
  if (!trimmed) return null
  // 拒绝外部协议
  if (/^https?:\/\//i.test(trimmed) || trimmed.startsWith('//')) return null
  if (/^javascript:/i.test(trimmed)) return null
  // 仅允许白名单前缀
  const ALLOWED_PREFIXES = ['/screener', '/market', '/messages']
  const matched = ALLOWED_PREFIXES.some(
    (p) => trimmed === p || trimmed.startsWith(p + '?') || trimmed.startsWith(p + '#'),
  )
  return matched ? trimmed : null
}

// ===== 详情页来源上下文共享纯函数（CHANGE-20260713-009）=====
// MarketWorkspacePage 和 useStockDetailActions 共用，避免筛选口径漂移。
// 任意合法 /market URL 都必须识别为市场工作区上下文（不要求 keyword/page/sort 存在）。

// 后端存储为小数的收益率/offset 类指标
const RATIO_METRICS = new Set([
  'vwap_ret_avg',
  'vwap_ret_total',
  'offset_mean',
  'offset_std',
  'offset_variance_rate',
])

// 后端存储为 0~1 的百分位类指标
const PERCENTILE_METRICS = new Set([
  'offset_percentile',
  'short_position',
  'position_short',
  'short_pos',
])

// 将用户输入的筛选值归一化为后端口径
function normalizeMetricValue(
  key: string,
  raw: string | number | undefined,
): number | undefined {
  if (raw === undefined || raw === null || raw === '') return undefined
  const s = String(raw).replace(/,/g, '').trim()
  const hasPercent = s.includes('%')
  const n = parseFloat(s.replace(/%/g, ''))
  if (Number.isNaN(n)) return undefined
  if (RATIO_METRICS.has(key) || PERCENTILE_METRICS.has(key)) {
    return hasPercent ? n / 100 : n
  }
  return n
}

// 筛选条件（与 screenerUrlState 的 ScreenerUrlFilter 结构兼容）
export interface MarketListFilter {
  key: string
  operator: string
  value?: string | number
  value2?: string | number
}

// 解析后的 /market 列表上下文
export interface MarketListContext {
  scope: MarketScope
  keyword: string | null
  industry: string | null
  concept: string | null
  sort: { key: string; direction: 'asc' | 'desc' } | null
  filters: MarketListFilter[] | null
  page: number | null
  page_size: number | null
}

// 与 StrategyResultQueryParams 结构兼容的查询参数
export interface StrategyResultQuery {
  universe?: 'all' | 'watchlist'
  keyword?: string
  industry?: string
  concept?: string
  sort_by?: string
  sort_desc?: boolean
  metric_filters?: string
  page?: number
  page_size?: number
}

/**
 * 从 returnTo URL 解析 /market 列表上下文。
 * 任意合法 /market URL 都返回 MarketListContext（不要求 keyword/page/sort 存在）。
 * scope=market 和 scope=watchlist 都解析；industry/concept/filter-only URL 也有效。
 * 非 /market、外部 URL、非法 returnTo 返回 null。
 */
export function decodeMarketListContext(
  returnTo: string | null | undefined,
): MarketListContext | null {
  const safe = normalizeInternalReturnTo(returnTo)
  if (!safe) return null
  if (!safe.startsWith('/market')) return null
  const qs = safe.split('?')[1]
  const params = new URLSearchParams(qs)
  const rawScope = params.get('scope')
  const scope: MarketScope = rawScope === 'market' ? 'market' : 'watchlist'
  const keyword = params.get('keyword')
  const industry = params.get('industry')
  const concept = params.get('concept')
  const sortKey = params.get('sort')
  const sortDir = params.get('dir')
  let sort: { key: string; direction: 'asc' | 'desc' } | null = null
  if (sortKey && (sortDir === 'asc' || sortDir === 'desc')) {
    sort = { key: sortKey, direction: sortDir }
  }
  // filters 是 JSON 字符串（screenerUrlState 编码）
  let filters: MarketListFilter[] | null = null
  const filtersRaw = params.get('filters')
  if (filtersRaw) {
    try {
      const parsed: unknown = JSON.parse(filtersRaw)
      if (Array.isArray(parsed)) {
        filters = parsed
          .filter(
            (item): item is Record<string, unknown> =>
              item !== null && typeof item === 'object',
          )
          .map((item) => {
            const filter: MarketListFilter = {
              key: String(item.key ?? ''),
              operator: String(item.op ?? item.operator ?? ''),
            }
            if (item.value !== undefined) filter.value = item.value as string | number
            if (item.value2 !== undefined) filter.value2 = item.value2 as string | number
            return filter
          })
      }
    } catch {
      filters = null
    }
  }
  const pageRaw = params.get('page')
  const page = pageRaw ? parseInt(pageRaw, 10) : null
  const pageSizeRaw = params.get('page_size')
  let page_size: number | null = null
  if (pageSizeRaw) {
    const parsed = parseInt(pageSizeRaw, 10)
    if (Number.isFinite(parsed) && parsed >= 1 && parsed <= MAX_PAGE_SIZE) {
      page_size = parsed
    }
  }
  return {
    scope,
    keyword,
    industry,
    concept,
    sort,
    filters,
    page: page !== null && page >= 1 ? page : null,
    page_size,
  }
}

/**
 * 将 MarketListContext 转换为 StrategyResultQuery（与 StrategyResultQueryParams 结构兼容）。
 * scope=market → universe=all；scope=watchlist → universe=watchlist。
 * 包含 keyword/industry/concept/sort/metric_filters/page/page_size 完整转换。
 */
export function buildStrategyResultQueryParams(
  ctx: MarketListContext,
): StrategyResultQuery {
  const params: StrategyResultQuery = {
    universe: ctx.scope === 'market' ? 'all' : 'watchlist',
  }
  if (ctx.keyword) {
    params.keyword = ctx.keyword
  }
  if (ctx.industry) {
    params.industry = ctx.industry
  }
  if (ctx.concept) {
    params.concept = ctx.concept
  }
  if (ctx.sort) {
    params.sort_by = ctx.sort.key
    params.sort_desc = ctx.sort.direction === 'desc'
  }
  if (ctx.page) {
    params.page = ctx.page
  }
  if (ctx.page_size) {
    params.page_size = ctx.page_size
  }
  // 列筛选转 metric_filters（与 MarketWorkspacePage 原逻辑一致）
  if (ctx.filters && ctx.filters.length > 0) {
    const supportedOps = new Set(['gt', 'gte', 'lt', 'lte', 'eq', 'between'])
    const metricFilters = ctx.filters
      .filter((f) => supportedOps.has(f.operator) && f.key !== 'stock' && f.key !== 'action')
      .map((f) => {
        const value = normalizeMetricValue(f.key, f.value)
        if (value === undefined) return null
        if (f.operator === 'between') {
          const value2 = normalizeMetricValue(f.key, f.value2)
          if (value2 === undefined) return null
          return { metric_key: f.key, operator: f.operator, value1: value, value2 }
        }
        return { metric_key: f.key, operator: f.operator, value }
      })
      .filter((f): f is NonNullable<typeof f> => f !== null)
    if (metricFilters.length > 0) {
      params.metric_filters = JSON.stringify(metricFilters)
    }
  }
  return params
}
