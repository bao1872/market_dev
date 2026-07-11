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

// ===== 指标图层 Manifest（PRD §6.2）=====
// 描述 5 个用户可显隐的指标图层：id、名称、主/副图、默认值、依赖数据和渲染顺序。
// 用户只能显隐，不得修改窗口、阈值等算法参数。
// 与 StrategyChart 内部 LayerVisibility 的映射关系：
//   consensus_zone → profile + node + poc（筹码共识区 / Volume Profile）
//   price_structure → dsa + selection（价格结构 / 趋势参考价）
//   boll → bb（布林带）
//   volume → volume（成交量）
//   macd → macd（MACD 副图）

export type IndicatorLayerKind = 'main' | 'sub'

export interface IndicatorLayerManifestEntry {
  id: string
  name: string
  kind: IndicatorLayerKind
  defaultVisible: boolean
  // enabled=false 时图层开关禁用（灰显不可点击），用于尚未实现的图层
  // Phase 5 实现真实 ConsensusZone 后将 enabled 改回 true
  enabled: boolean
  dependencies: string[]
  renderOrder: number
}

// [consensus_zone-disabled] - Phase 3 纠偏：真实筹码共识区尚未实现（Phase 5），
// 当前 consensus_zone 映射的 VolumeProfile 不等同于筹码共识区。
// 在 Phase 5 落地前：name 改为"成交量分布"（实际渲染内容）、defaultVisible=false、enabled=false（禁用开关）。
// StrategyChart effectiveLayers 中 consensus_zone → profile+node+poc 的映射保留（VolumeProfile 渲染代码不删除）。
export const INDICATOR_LAYER_MANIFEST: IndicatorLayerManifestEntry[] = [
  { id: 'consensus_zone', name: '成交量分布', kind: 'main', defaultVisible: false, enabled: false, dependencies: ['volume_profile'], renderOrder: 10 },
  { id: 'price_structure', name: '价格结构', kind: 'main', defaultVisible: true, enabled: true, dependencies: ['structural_factors'], renderOrder: 20 },
  { id: 'boll', name: '布林带', kind: 'main', defaultVisible: false, enabled: true, dependencies: ['boll_bands'], renderOrder: 30 },
  { id: 'volume', name: '成交量', kind: 'sub', defaultVisible: true, enabled: true, dependencies: ['bars.volume'], renderOrder: 10 },
  { id: 'macd', name: 'MACD', kind: 'sub', defaultVisible: false, enabled: true, dependencies: ['macd'], renderOrder: 20 },
]

export type IndicatorVisibility = Record<string, boolean>

// 从 manifest 默认值生成 IndicatorVisibility
export function defaultIndicatorVisibility(): IndicatorVisibility {
  const result: IndicatorVisibility = {}
  for (const entry of INDICATOR_LAYER_MANIFEST) {
    result[entry.id] = entry.defaultVisible
  }
  return result
}
