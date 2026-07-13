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
