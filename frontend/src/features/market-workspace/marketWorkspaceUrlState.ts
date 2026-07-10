// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 纯函数
// URL 格式：/market?scope=watchlist|market&symbol=xxx&timeframe=1d&source=watchlist|selection&strategy=xxx&event_id=xxx
// scope/symbol/timeframe/source/strategy/event_id 进入 URL（可分享、刷新恢复）；右栏折叠和 viewport 留本地 state。
// 非法 timeframe 回退 1d；source 默认 watchlist；strategy 默认根据 source 推导（watchlist→watchlist_monitor, selection→dsa_selector）。
// event_id 本轮仅解析、保留与传递，尚未被工作区消费（不实现自然语言事件解释）。
// 本文件为纯 TS（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。
// 策略 key 常量与 @/constants/strategyKeys 的 STRATEGY_KEYS 对齐（'dsa_selector' / 'watchlist_monitor'）。

export type MarketScope = 'watchlist' | 'market'

// 图表工具栏允许的显示周期（与 Node Cluster 输入契约对齐：1d=250/15m=4000/1h=1200/1w=260/1mo=120）
export type DisplayTimeframe = '15m' | '1h' | '1d' | '1w' | '1mo'

export const ALLOWED_TIMEFRAMES: readonly DisplayTimeframe[] = ['15m', '1h', '1d', '1w', '1mo']

// 研究来源（watchlist=自选/市场搜索；selection=趋势选股结果进入）
export type ResearchSource = 'watchlist' | 'selection'

export interface MarketWorkspaceUrlState {
  scope: MarketScope
  symbol: string | null
  timeframe: DisplayTimeframe
  source: ResearchSource
  strategy: string
  eventId: string | null
}

export const DEFAULT_MARKET_SCOPE: MarketScope = 'watchlist'
export const DEFAULT_TIMEFRAME: DisplayTimeframe = '1d'
export const DEFAULT_SOURCE: ResearchSource = 'watchlist'

// 根据 source 推导默认策略 key（watchlist/market → watchlist_monitor；selection → dsa_selector）
// 值与 @/constants/strategyKeys 的 STRATEGY_KEYS 对齐
export function defaultStrategyForSource(source: ResearchSource): string {
  return source === 'selection' ? 'dsa_selector' : 'watchlist_monitor'
}

// 校验 timeframe 是否为允许值，非法回退 1d
function normalizeTimeframe(raw: string | null): DisplayTimeframe {
  if (raw && (ALLOWED_TIMEFRAMES as readonly string[]).includes(raw)) {
    return raw as DisplayTimeframe
  }
  return DEFAULT_TIMEFRAME
}

// 校验 source 是否为允许值，非法回退 watchlist
function normalizeSource(raw: string | null): ResearchSource {
  return raw === 'selection' ? 'selection' : 'watchlist'
}

// 从 URLSearchParams 解析工作区状态
export function decodeMarketWorkspaceUrl(params: URLSearchParams): MarketWorkspaceUrlState {
  const rawScope = params.get('scope')
  const scope: MarketScope = rawScope === 'market' ? 'market' : 'watchlist'
  const symbol = params.get('symbol') || null
  const timeframe = normalizeTimeframe(params.get('timeframe'))
  const source = normalizeSource(params.get('source'))
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
