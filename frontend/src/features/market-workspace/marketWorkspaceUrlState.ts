// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 纯函数
// URL 格式：/market?scope=watchlist|market&symbol=xxx&timeframe=1d&source=watchlist|selection&strategy=xxx&event_id=xxx
// scope/symbol/timeframe/source/strategy/event_id 进入 URL（可分享、刷新恢复）；右栏折叠和 viewport 留本地 state。
// 非法 timeframe 回退 1d；source 默认 watchlist；strategy 默认根据 source 推导（watchlist→watchlist_monitor, selection→dsa_selector）。
// event_id 本轮仅解析、保留与传递，尚未被工作区消费（不实现自然语言事件解释）。
// 本文件为纯 TS（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。
// 共享类型（DisplayTimeframe/ResearchSource 等）从 stockResearchTypes 导入，避免 stock-research 反向依赖 market-workspace。

import {
  type DisplayTimeframe,
  type ResearchSource,
  DEFAULT_TIMEFRAME,
  DEFAULT_SOURCE,
  defaultStrategyForSource,
  normalizeDisplayTimeframe,
  normalizeResearchSource,
} from '../stock-research/stockResearchTypes.ts'

// 重新导出共享类型，保持 marketWorkspaceUrlState 现有导入兼容
export type { DisplayTimeframe, ResearchSource } from '../stock-research/stockResearchTypes.ts'
export { ALLOWED_TIMEFRAMES, DEFAULT_TIMEFRAME, DEFAULT_SOURCE, defaultStrategyForSource } from '../stock-research/stockResearchTypes.ts'

export type MarketScope = 'watchlist' | 'market'

export interface MarketWorkspaceUrlState {
  scope: MarketScope
  symbol: string | null
  timeframe: DisplayTimeframe
  source: ResearchSource
  strategy: string
  eventId: string | null
}

export const DEFAULT_MARKET_SCOPE: MarketScope = 'watchlist'

// 从 URLSearchParams 解析工作区状态
export function decodeMarketWorkspaceUrl(params: URLSearchParams): MarketWorkspaceUrlState {
  const rawScope = params.get('scope')
  const scope: MarketScope = rawScope === 'market' ? 'market' : 'watchlist'
  const symbol = params.get('symbol') || null
  const timeframe = normalizeDisplayTimeframe(params.get('timeframe'))
  const source = normalizeResearchSource(params.get('source'))
  const strategy = params.get('strategy') || defaultStrategyForSource(source)
  const eventId = params.get('event_id') || null
  return { scope, symbol, timeframe, source, strategy, eventId }
}

// 将工作区状态编码为 URLSearchParams（用于 setSearchParams）
// 规则：scope 始终写入；symbol 存在才写入；timeframe 非默认才写入；
//       source 非默认才写入；strategy 非默认（不等于 source 对应默认）才写入；event_id 存在才写入。
export function encodeMarketWorkspaceUrl(state: MarketWorkspaceUrlState): URLSearchParams {
  const params = new URLSearchParams()
  params.set('scope', state.scope)
  if (state.symbol) {
    params.set('symbol', state.symbol)
  }
  if (state.timeframe && state.timeframe !== DEFAULT_TIMEFRAME) {
    params.set('timeframe', state.timeframe)
  }
  if (state.source !== DEFAULT_SOURCE) {
    params.set('source', state.source)
  }
  const defaultStrategy = defaultStrategyForSource(state.source)
  if (state.strategy && state.strategy !== defaultStrategy) {
    params.set('strategy', state.strategy)
  }
  if (state.eventId) {
    params.set('event_id', state.eventId)
  }
  return params
}

// 将工作区状态编码为完整 URL query string（用于 navigate）
export function buildMarketWorkspaceUrl(state: MarketWorkspaceUrlState): string {
  const params = encodeMarketWorkspaceUrl(state)
  const qs = params.toString()
  return qs ? `/market?${qs}` : '/market'
}

// 从左栏（MarketInstrumentPane）选择股票时的状态转换（纯函数）。
// 选择自选/市场搜索结果中的股票属于 watchlist 上下文：重置 source=watchlist、strategy=watchlist_monitor、eventId=null。
// 保留 scope 和 timeframe（scope 可能是 watchlist 或 market，timeframe 不受选股影响）。
export function selectInstrumentFromMarketPane(
  state: MarketWorkspaceUrlState,
  newSymbol: string,
): MarketWorkspaceUrlState {
  return {
    scope: state.scope,
    symbol: newSymbol,
    timeframe: state.timeframe,
    source: 'watchlist',
    strategy: defaultStrategyForSource('watchlist'),
    eventId: null,
  }
}

// 切换 scope 时的状态转换（纯函数）。
// 切换到 watchlist/market scope 即退出 selection 上下文：重置 source=watchlist、strategy=watchlist_monitor、eventId=null。
// 保留 symbol 和 timeframe。
export function changeMarketScope(
  state: MarketWorkspaceUrlState,
  newScope: MarketScope,
): MarketWorkspaceUrlState {
  return {
    scope: newScope,
    symbol: state.symbol,
    timeframe: state.timeframe,
    source: 'watchlist',
    strategy: defaultStrategyForSource('watchlist'),
    eventId: null,
  }
}
