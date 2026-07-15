// [StockResearchTypes] - 描述: 股票研究核心共享类型与常量
// /market 和 /stock/:symbol 共用的研究数据类型定义，避免 stock-research 反向依赖 market-workspace。
// 纯 TS 文件（无 React 依赖，无 @/ 别名依赖），可被 node --test 直接运行。
// 策略 key 常量与 @/constants/strategyKeys 的 STRATEGY_KEYS 对齐（'dsa_selector' / 'watchlist_monitor'）。
//
// CHANGE-20260715-007: ResearchSource / normalizeResearchSource / defaultStrategyForSource
// 的唯一权威实现已移至 ./detailSourceContext.ts。本文件 re-export 以保持向后兼容，
// 禁止复制 source/strategy 映射。

// 图表工具栏允许的显示周期（与 Node Cluster 输入契约对齐：1d=250/15m=4000/1h=1200/1w=260/1mo=120）
export type DisplayTimeframe = '15m' | '1h' | '1d' | '1w' | '1mo'

export const ALLOWED_TIMEFRAMES: readonly DisplayTimeframe[] = ['15m', '1h', '1d', '1w', '1mo']

export const DEFAULT_TIMEFRAME: DisplayTimeframe = '1d'

// CHANGE-20260715-007: 从 detailSourceContext.ts re-export（消除重复真源）
export {
  type ResearchSource,
  DEFAULT_SOURCE,
  normalizeResearchSource,
  defaultStrategyForSource,
} from './detailSourceContext.ts'

// 本模块内部使用的类型（从 detailSourceContext 导入，不再本地声明）
import type { ResearchSource } from './detailSourceContext.ts'

// 按 timeframe 映射请求根数（与 Node Cluster / indicator_contract 对齐）
export const BARS_COUNT_BY_TIMEFRAME: Record<DisplayTimeframe, number> = {
  '1d': 250,
  '15m': 4000,
  '1h': 1200,
  '1w': 260,
  '1mo': 120,
}

// 校验 timeframe 是否为允许值，非法回退 1d
export function normalizeDisplayTimeframe(raw: string | null): DisplayTimeframe {
  if (raw && (ALLOWED_TIMEFRAMES as readonly string[]).includes(raw)) {
    return raw as DisplayTimeframe
  }
  return DEFAULT_TIMEFRAME
}

// ===== 图表图层 Manifest（PRD §6.2 — 单一真源 v2）=====
// 用户可显隐的 8 个图表图层开关。StockResearchWorkspace 持有唯一 ChartLayerVisibility state，
// StrategyChart 作为受控组件接收 layerVisibility prop，不再内部管理 layers state。
// 与 StrategyChart 内部 LayerVisibility（13 键）的映射在 StrategyChart.chartLayerVisibilityToInternal 完成：
//   trend    → dsa + selection（趋势参考价 / 选股命中标记）
//   node     → profile + node + poc（成交量分布 / 节点区间 / POC）
//   boll     → bb（布林带）
//   volume   → volume（成交量副图）
//   macd     → macd（MACD 副图）
//   sqzmom   → sqzmom（SQZMOM 副图）
//   breakout → breakout（突破标记，仅 selection 来源可用）
//   smc      → smc（SMC 智能资金概念，CHANGE-011 新增，默认关闭，按需开启）
// [CHANGE-011 SMC] - smc 是按需计算的独立图层（BOS/CHoCH/OB/EQH/EQL/trailing），
//   默认关闭；不开启时后端不计算 SMC（include_smc=false），不消耗 CPU；
//   完全排除 FVG；不进入 DSA、Node 监控、Capture 或右栏 context。

export type ChartLayerKey = 'trend' | 'node' | 'boll' | 'volume' | 'macd' | 'sqzmom' | 'breakout' | 'smc'

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

// [ChartLayerManifest] - 描述: 用户可见图层名（仅展示文案，不改内部 id/DTO/算法）
// - node 显示为"筹码共识价"：基于历史成交量分布的估算代理，不是股东真实持仓成本
// - sqzmom 显示为"挤压动量"：波动收窄后的方向与强弱（LazyBear Pine 复刻）
// - smc 显示为"智能资金"：SMC 智能资金概念（BOS/CHoCH/订单块/等高/等低），默认关闭
// 内部 ChartLayerKey 'node'/'sqzmom'/'smc' 不变，profile/node/poc 字段名不变
export const CHART_LAYER_MANIFEST: ChartLayerManifestEntry[] = [
  { id: 'trend', name: '趋势', kind: 'main', enabled: true, description: '趋势参考价 · 选股命中标记' },
  { id: 'node', name: '筹码共识价', kind: 'main', enabled: true, description: '成交量分布估算 · 节点区间 · POC（基于历史成交量分布的估算代理，非股东真实持仓成本）' },
  { id: 'boll', name: '布林带', kind: 'main', enabled: true, description: '布林带 · SMA(20) ± 2σ' },
  { id: 'breakout', name: '突破', kind: 'main', enabled: true, selectionOnly: true, description: '压力区 · 突破确认' },
  { id: 'smc', name: '智能资金', kind: 'main', enabled: true, description: 'SMC 智能资金概念 · BOS/CHoCH/订单块/等高/等低（按需计算，默认关闭；完全排除 FVG）' },
  { id: 'volume', name: '成交量', kind: 'sub', enabled: true, description: '成交量副图' },
  { id: 'macd', name: 'MACD', kind: 'sub', enabled: true, description: 'MACD 副图 · DIF/DEA/Histogram' },
  { id: 'sqzmom', name: '挤压动量', kind: 'sub', enabled: true, description: '波动收窄后的方向与强弱 · LazyBear Pine 复刻' },
]

// 按 source 生成默认 ChartLayerVisibility
// watchlist（watchlist_monitor 策略）：volume/node/boll 默认开；macd/smc 默认关（辅助技术指标，按需开启）
// selection（dsa_selector 策略）：volume/trend 默认开；macd/smc 默认关（辅助技术指标，按需开启）
// P0-6: MACD 是 feature_snapshot_service 附加的日线辅助指标，不是 bar 因子；
//   watchlist 和 selection 默认均关闭 MACD 副图，减少噪音；用户可通过 IndicatorToolbar 显式开启。
// [CHANGE-011 SMC] - smc 默认关闭；不开启时后端不计算 SMC（include_smc=false）。
export function defaultChartLayerVisibility(source: ResearchSource): ChartLayerVisibility {
  if (source === 'selection') {
    return { trend: true, node: false, boll: false, volume: true, macd: false, sqzmom: false, breakout: false, smc: false }
  }
  return { trend: false, node: true, boll: true, volume: true, macd: false, sqzmom: false, breakout: false, smc: false }
}

// 返回 source 适用的 manifest 条目（过滤 selectionOnly）
export function chartLayersForSource(
  manifest: ChartLayerManifestEntry[],
  source: ResearchSource,
): ChartLayerManifestEntry[] {
  return manifest.filter((e) => !e.selectionOnly || source === 'selection')
}
