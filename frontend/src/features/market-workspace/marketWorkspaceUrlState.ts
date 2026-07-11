// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 纯函数
// URL 格式：/market?scope=watchlist|market&symbol=xxx&timeframe=1d&source=watchlist|selection&strategy=xxx&event_id=xxx&returnTo=xxx
// scope/symbol/timeframe/source/strategy/event_id/returnTo 进入 URL（可分享、刷新恢复）；右栏折叠和 viewport 留本地 state。
// 非法 timeframe 回退 1d；source 默认 watchlist；strategy 默认根据 source 推导（watchlist→watchlist_monitor, selection→dsa_selector）。
// returnTo 为来源页 URL（如 /screener?... ），左栏选股或切 scope 时清除；仅允许 /screener /market /messages 前缀（normalizeInternalReturnTo 校验）。
// debug 已从普通 market URL 移除：管理员调试使用独立 /admin/stock-debug/:symbol 路由。
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
  returnTo: string | null
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
  const returnTo = params.get('returnTo') || null
  return { scope, symbol, timeframe, source, strategy, eventId, returnTo }
}

// 将工作区状态编码为 URLSearchParams（用于 setSearchParams）
// 规则：scope 始终写入；symbol 存在才写入；timeframe 非默认才写入；
//       source 非默认才写入；strategy 非默认（不等于 source 对应默认）才写入；event_id 存在才写入；
//       returnTo 存在才写入。
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
  if (state.returnTo) {
    params.set('returnTo', state.returnTo)
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
// 选择自选/市场搜索结果中的股票属于 watchlist 上下文：重置 source=watchlist、strategy=watchlist_monitor、eventId=null、returnTo=null。
// 保留 scope 和 timeframe。
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
    returnTo: null,
  }
}

// 切换 scope 时的状态转换（纯函数）。
// 切换到 watchlist/market scope 即退出 selection 上下文：重置 source=watchlist、strategy=watchlist_monitor、eventId=null、returnTo=null。
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
    returnTo: null,
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
