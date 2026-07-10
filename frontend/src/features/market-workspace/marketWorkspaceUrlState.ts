// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 纯函数
// URL 格式：/market?scope=watchlist|market&symbol=xxx&timeframe=1d
// scope/symbol/timeframe 进入 URL（可分享、刷新恢复）；右栏折叠和 viewport 留本地 state。
// 本文件为纯 TS（无 React 依赖），可被 node --test 直接运行。

export type MarketScope = 'watchlist' | 'market'

export interface MarketWorkspaceUrlState {
  scope: MarketScope
  symbol: string | null
  timeframe: string
}

export const DEFAULT_MARKET_SCOPE: MarketScope = 'watchlist'
export const DEFAULT_TIMEFRAME = '1d'

// 从 URLSearchParams 解析工作区状态
export function decodeMarketWorkspaceUrl(params: URLSearchParams): MarketWorkspaceUrlState {
  const rawScope = params.get('scope')
  const scope: MarketScope = rawScope === 'market' ? 'market' : 'watchlist'
  const symbol = params.get('symbol') || null
  const timeframe = params.get('timeframe') || DEFAULT_TIMEFRAME
  return { scope, symbol, timeframe }
}

// 将工作区状态编码为 URLSearchParams（用于 setSearchParams）
export function encodeMarketWorkspaceUrl(state: MarketWorkspaceUrlState): URLSearchParams {
  const params = new URLSearchParams()
  params.set('scope', state.scope)
  if (state.symbol) {
    params.set('symbol', state.symbol)
  }
  if (state.timeframe && state.timeframe !== DEFAULT_TIMEFRAME) {
    params.set('timeframe', state.timeframe)
  }
  return params
}

// 将工作区状态编码为完整 URL query string（用于 navigate）
export function buildMarketWorkspaceUrl(state: MarketWorkspaceUrlState): string {
  const params = encodeMarketWorkspaceUrl(state)
  const qs = params.toString()
  return qs ? `/market?${qs}` : '/market'
}
