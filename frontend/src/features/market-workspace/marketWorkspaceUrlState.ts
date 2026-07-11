// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 纯函数
// URL 格式：/market?scope=watchlist|market&query=xxx&page=1&page_size=<DEFAULT_PAGE_SIZE>&sort=symbol:asc&selected=xxx&industry=xxx&concept=xxx&state=up
// scope/query/page/page_size/sort/selected/industry/concept/state 进入 URL（可分享、刷新恢复）；右栏折叠和 viewport 留本地 state。
// PRD §6.1：排序、筛选、分页均由服务端执行；浏览器前进/后退应恢复 scope、筛选、排序、页码和选中股票。
// 单击非链接区域更新 selected 并刷新右栏，不进入详情；点击股票名称进入 /stock/:symbol?returnTo=<编码后的当前URL>。
// 本文件为纯 TS（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。

export type MarketScope = 'watchlist' | 'market'
export type MarketStateFilter = 'up' | 'down' | 'sideways' | null

export interface MarketWorkspaceUrlState {
  scope: MarketScope
  query: string
  page: number
  pageSize: number
  sort: string | null
  selected: string | null
  industry: string | null
  concept: string | null
  state: MarketStateFilter
  eventId: string | null
}

export const DEFAULT_MARKET_SCOPE: MarketScope = 'watchlist'
export const DEFAULT_PAGE = 1
export const DEFAULT_PAGE_SIZE = 50
export const MAX_PAGE_SIZE = 100

const VALID_STATE_FILTERS = new Set(['up', 'down', 'sideways'])

// 从 URLSearchParams 解析工作区状态
export function decodeMarketWorkspaceUrl(params: URLSearchParams): MarketWorkspaceUrlState {
  const rawScope = params.get('scope')
  const scope: MarketScope = rawScope === 'market' ? 'market' : 'watchlist'
  const query = params.get('query') ?? ''
  const rawPage = parseInt(params.get('page') ?? '1', 10)
  const page = Number.isFinite(rawPage) && rawPage >= 1 ? rawPage : DEFAULT_PAGE
  const rawPageSize = parseInt(params.get('page_size') ?? String(DEFAULT_PAGE_SIZE), 10)
  const pageSize =
    Number.isFinite(rawPageSize) && rawPageSize >= 1 && rawPageSize <= MAX_PAGE_SIZE
      ? rawPageSize
      : DEFAULT_PAGE_SIZE
  const sort = params.get('sort') ?? null
  const selected = params.get('selected') ?? null
  const industry = params.get('industry') || null
  const concept = params.get('concept') || null
  const rawState = params.get('state')
  const state: MarketStateFilter = rawState && VALID_STATE_FILTERS.has(rawState) ? rawState as MarketStateFilter : null
  const eventId = params.get('event_id') || null
  return { scope, query, page, pageSize, sort, selected, industry, concept, state, eventId }
}

// 将工作区状态编码为 URLSearchParams（用于 setSearchParams）
// 规则：scope 始终写入；query 非空才写入；page 非默认才写入；
//       page_size 非默认才写入；sort 存在才写入；selected 存在才写入；
//       industry/concept 非空才写入；state 非空才写入。
export function encodeMarketWorkspaceUrl(state: MarketWorkspaceUrlState): URLSearchParams {
  const params = new URLSearchParams()
  params.set('scope', state.scope)
  if (state.query) {
    params.set('query', state.query)
  }
  if (state.page !== DEFAULT_PAGE) {
    params.set('page', String(state.page))
  }
  if (state.pageSize !== DEFAULT_PAGE_SIZE) {
    params.set('page_size', String(state.pageSize))
  }
  if (state.sort) {
    params.set('sort', state.sort)
  }
  if (state.selected) {
    params.set('selected', state.selected)
  }
  if (state.industry) {
    params.set('industry', state.industry)
  }
  if (state.concept) {
    params.set('concept', state.concept)
  }
  if (state.state) {
    params.set('state', state.state)
  }
  if (state.eventId) {
    params.set('event_id', state.eventId)
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
// 设置 selected，清除 eventId（退出事件定位上下文），保留 scope/query/page/pageSize/sort。
export function selectInstrumentInTable(
  state: MarketWorkspaceUrlState,
  symbol: string,
): MarketWorkspaceUrlState {
  return {
    ...state,
    selected: symbol,
    eventId: null,
  }
}

// 切换 scope 时的状态转换（纯函数）。
// 切换 scope 后重置 page=1、清除 selected 和 eventId（PRD §6.1：筛选变化重置分页）。
// 保留 query 和 sort。
export function changeMarketScope(
  state: MarketWorkspaceUrlState,
  newScope: MarketScope,
): MarketWorkspaceUrlState {
  return {
    ...state,
    scope: newScope,
    page: DEFAULT_PAGE,
    selected: null,
    eventId: null,
  }
}

// 筛选条件变化时的状态转换（纯函数）。
// 筛选变化后重置 page=1、清除 selected 和 eventId（PRD §6.1：筛选变化重置分页）。
// 保留 scope/query/sort 和其他筛选字段。
export function changeMarketFilter(
  state: MarketWorkspaceUrlState,
  patch: Partial<Pick<MarketWorkspaceUrlState, 'industry' | 'concept' | 'state'>>,
): MarketWorkspaceUrlState {
  return {
    ...state,
    ...patch,
    page: DEFAULT_PAGE,
    selected: null,
    eventId: null,
  }
}

// returnTo 安全校验：仅允许 /screener /market /messages 前缀的内部路径（含 query/hash）。
// 拒绝：外部 URL（http:// https:// //）、javascript:、超长字符串（>200）、非白名单前缀。
export function normalizeInternalReturnTo(raw: string | null | undefined): string | null {
  if (!raw) return null
  if (raw.length > 200) return null
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
