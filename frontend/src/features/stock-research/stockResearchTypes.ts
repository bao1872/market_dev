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

// ===== 图表图层 Manifest（PRD §6.2 — 单一真源 v2）=====
// 用户可显隐的 7 个图表图层开关。StockResearchWorkspace 持有唯一 ChartLayerVisibility state，
// StrategyChart 作为受控组件接收 layerVisibility prop，不再内部管理 layers state。
// 与 StrategyChart 内部 LayerVisibility（12 键）的映射在 StrategyChart.chartLayerVisibilityToInternal 完成：
//   trend    → dsa + selection（趋势参考价 / 选股命中标记）
//   node     → profile + node + poc（成交量分布 / 节点区间 / POC）
//   boll     → bb（布林带）
//   volume   → volume（成交量副图）
//   macd     → macd（MACD 副图）
//   sqzmom   → sqzmom（SQZMOM 副图）
//   breakout → breakout（突破标记，仅 selection 来源可用）

export type ChartLayerKey = 'trend' | 'node' | 'boll' | 'volume' | 'macd' | 'sqzmom' | 'breakout'

export type ChartLayerVisibility = Record<ChartLayerKey, boolean>

export type ChartLayerKind = 'main' | 'sub'

export interface ChartLayerManifestEntry {
  id: ChartLayerKey
  name: string
  kind: ChartLayerKind
  enabled: boolean
  // selectionOnly=true 时仅 selection 来源显示该开关（breakout）
  selectionOnly?: boolean
  description: string
}

export const CHART_LAYER_MANIFEST: ChartLayerManifestEntry[] = [
  { id: 'trend', name: '趋势', kind: 'main', enabled: true, description: '趋势参考价 · 选股命中标记' },
  { id: 'node', name: '成交量节点', kind: 'main', enabled: true, description: '成交量分布 · 节点区间 · POC' },
  { id: 'boll', name: '布林带', kind: 'main', enabled: true, description: '布林带 · SMA(20) ± 2σ' },
  { id: 'breakout', name: '突破', kind: 'main', enabled: true, selectionOnly: true, description: '压力区 · 突破确认' },
  { id: 'volume', name: '成交量', kind: 'sub', enabled: true, description: '成交量副图' },
  { id: 'macd', name: 'MACD', kind: 'sub', enabled: true, description: 'MACD 副图 · DIF/DEA/Histogram' },
  { id: 'sqzmom', name: 'SQZMOM', kind: 'sub', enabled: true, description: 'Squeeze Momentum · LazyBear Pine 复刻' },
]

// 按 source 生成默认 ChartLayerVisibility
// watchlist（watchlist_monitor 策略）：volume/node/boll/macd 默认开
// selection（dsa_selector 策略）：volume/trend/macd 默认开
export function defaultChartLayerVisibility(source: ResearchSource): ChartLayerVisibility {
  if (source === 'selection') {
    return { trend: true, node: false, boll: false, volume: true, macd: true, sqzmom: false, breakout: false }
  }
  return { trend: false, node: true, boll: true, volume: true, macd: true, sqzmom: false, breakout: false }
}

// 返回 source 适用的 manifest 条目（过滤 selectionOnly）
export function chartLayersForSource(
  manifest: ChartLayerManifestEntry[],
  source: ResearchSource,
): ChartLayerManifestEntry[] {
  return manifest.filter((e) => !e.selectionOnly || source === 'selection')
}
