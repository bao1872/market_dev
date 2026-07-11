// [StockResearchTypes] - 描述: 股票研究核心共享类型与常量
// /market 和 /stock/:symbol 共用的研究数据类型定义，避免 stock-research 反向依赖 market-workspace。
// 纯 TS 文件（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。
// 策略 key 常量与 @/constants/strategyKeys 的 STRATEGY_KEYS 对齐（'dsa_selector' / 'watchlist_monitor'）。

// 图表工具栏允许的显示周期（与 Node Cluster 输入契约对齐：1d=250/15m=4000/1h=1200/1w=260/1mo=120）
export type DisplayTimeframe = '15m' | '1h' | '1d' | '1w' | '1mo'

export const ALLOWED_TIMEFRAMES: readonly DisplayTimeframe[] = ['15m', '1h', '1d', '1w', '1mo']

export const DEFAULT_TIMEFRAME: DisplayTimeframe = '1d'

// 研究来源（watchlist=自选/市场搜索；selection=趋势选股结果进入）
export type ResearchSource = 'watchlist' | 'selection'

export const DEFAULT_SOURCE: ResearchSource = 'watchlist'

// 按 timeframe 映射请求根数（与 Node Cluster / indicator_contract 对齐）
export const BARS_COUNT_BY_TIMEFRAME: Record<DisplayTimeframe, number> = {
  '1d': 250,
  '15m': 4000,
  '1h': 1200,
  '1w': 260,
  '1mo': 120,
}

// 根据 source 推导默认策略 key（watchlist/market → watchlist_monitor；selection → dsa_selector）
// 值与 @/constants/strategyKeys 的 STRATEGY_KEYS 对齐
export function defaultStrategyForSource(source: ResearchSource): string {
  return source === 'selection' ? 'dsa_selector' : 'watchlist_monitor'
}

// 校验 timeframe 是否为允许值，非法回退 1d
export function normalizeDisplayTimeframe(raw: string | null): DisplayTimeframe {
  if (raw && (ALLOWED_TIMEFRAMES as readonly string[]).includes(raw)) {
    return raw as DisplayTimeframe
  }
  return DEFAULT_TIMEFRAME
}

// 校验 source 是否为允许值，非法回退 watchlist
export function normalizeResearchSource(raw: string | null): ResearchSource {
  return raw === 'selection' ? 'selection' : 'watchlist'
}
