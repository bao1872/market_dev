// 策略 UI Manifest（V1.5.2）
// 对应原型 assets/strategy-manifest.js
// 定义所有策略图层、策略映射、计算窗口
// 图层面板由 Manifest 动态生成，页面不写死具体策略

export type StrategyKind = 'selection' | 'monitor'
export type LayerRenderer =
  | 'histogram'
  | 'line'
  | 'line_pair'
  | 'price_zone'
  | 'price_line'
  | 'marker'
  | 'band'
  | 'histogram_line'
  | 'horizontal_profile'
  | 'macd'
  | 'dsa_polyline'
export type LayerPane = 'price' | 'price_right' | 'volume' | 'delta' | 'macd'

export interface LayerDef {
  id: string
  name: string
  shortName: string
  group: string
  renderer: LayerRenderer
  pane: LayerPane
  color: string
  description: string
  defaultVisible: boolean
  dependsOn?: string[]
}

export interface StrategyDef {
  id: string
  name: string
  kind: StrategyKind
  version: string
  layers: string[]
  defaultLayers: string[]
}

export interface CalculationWindow {
  bars: number
  label: string
}

// ===== 图层定义（对应原型 LAYERS）=====
export const LAYERS: Record<string, LayerDef> = {
  volume: {
    id: 'volume',
    name: '成交量',
    shortName: 'VOL',
    group: '基础图层',
    renderer: 'histogram',
    pane: 'volume',
    color: '#53637e',
    description: '逐根成交量柱',
    defaultVisible: true,
  },
  dsa: {
    id: 'dsa',
    name: '趋势参考价',
    shortName: '趋势',
    group: '选股策略',
    renderer: 'dsa_polyline',
    pane: 'price',
    color: '#ff1744',
    description: '趋势参考价线与 Pine 标签',
    defaultVisible: false,
  },
  macd: {
    id: 'macd',
    name: 'MACD',
    shortName: 'MACD',
    group: '技术指标',
    renderer: 'macd',
    pane: 'macd',
    color: '#f4c430',
    description: 'MACD 副图（DIF/DEA/Histogram），后端统一计算',
    defaultVisible: false,
  },
  breakout: {
    id: 'breakout',
    name: '突破压力区',
    shortName: 'BREAKOUT',
    group: '选股策略',
    renderer: 'price_zone',
    pane: 'price',
    color: '#ef5350',
    description: '结构压力与突破确认区',
    defaultVisible: false,
  },
  selection: {
    id: 'selection',
    name: '选股命中证据',
    shortName: 'HIT',
    group: '选股策略',
    renderer: 'marker',
    pane: 'price',
    color: '#2fd0c2',
    description: '选股方案命中时点与成员证据',
    defaultVisible: false,
  },
  node: {
    id: 'node',
    name: '成交量节点',
    shortName: '节点',
    group: '监控策略',
    renderer: 'price_zone',
    pane: 'price',
    color: '#4f7cff',
    description: '由固定计算窗口的成交量分布识别高成交节点',
    defaultVisible: false,
  },
  poc: {
    id: 'poc',
    name: 'POC',
    shortName: 'POC',
    group: '监控策略',
    renderer: 'price_line',
    pane: 'price',
    color: '#ff9800',
    description: '固定计算窗口内最大成交量价格',
    defaultVisible: false,
    dependsOn: ['profile'],
  },
  profile: {
    id: 'profile',
    name: '成交量分布',
    shortName: 'PROFILE',
    group: '监控策略',
    renderer: 'horizontal_profile',
    pane: 'price_right',
    color: '#9cb3ff',
    description: 'Volume Profile；不等同于持仓成本筹码分布',
    defaultVisible: false,
  },
  atr: {
    id: 'atr',
    name: 'ATR Rope',
    shortName: 'ATR ROPE',
    group: '监控策略',
    renderer: 'band',
    pane: 'price',
    color: '#82a0ff',
    description: 'EMA 中轴与 ATR 上下轨形成的趋势带',
    defaultVisible: false,
  },
  bb: {
    id: 'bb',
    name: '布林带',
    shortName: 'BB',
    group: '监控策略',
    renderer: 'band',
    pane: 'price',
    color: 'rgba(156,39,176,0.15)',
    description: '布林带：SMA(20) ± 2×标准差',
    defaultVisible: false,
  },
  delta: {
    id: 'delta',
    name: 'Volume Delta / CVD',
    shortName: 'DELTA',
    group: '监控策略',
    renderer: 'histogram_line',
    pane: 'delta',
    color: '#26a69a',
    description: '估算主动成交量差与累计 Delta',
    defaultVisible: false,
  },
  events: {
    id: 'events',
    name: '策略事件',
    shortName: 'EVENTS',
    group: '事件',
    renderer: 'marker',
    pane: 'price',
    color: '#f4c430',
    description: '选股命中、Node 碰触事件',
    defaultVisible: false,
  },
}

// ===== 策略定义（对应原型 STRATEGIES）=====
export const STRATEGIES: Record<string, StrategyDef> = {
  dsa_selector: {
    id: 'dsa_selector',
    name: '趋势稳定性',
    kind: 'selection',
    // [DSA 数据契约] - 与后端 manifest v1.4.1 对齐（visual_segments 归属 data.dsa_selector）
    version: '1.4.1',
    layers: ['volume', 'dsa', 'selection'],
    defaultLayers: ['volume', 'dsa', 'selection', 'macd'],
  },
  breakout: {
    id: 'breakout',
    name: '突破强度',
    kind: 'selection',
    version: '1.2.1',
    layers: ['volume', 'breakout', 'selection'],
    defaultLayers: ['volume', 'breakout', 'selection'],
  },
  watchlist_monitor: {
    id: 'watchlist_monitor',
    name: '自选股监控',
    kind: 'monitor',
    version: '1.1.0',
    layers: ['volume', 'profile', 'node', 'poc', 'bb', 'events'],
    defaultLayers: ['volume', 'profile', 'node', 'poc', 'bb', 'events', 'macd'],
  },
}

// ===== 字段级描述（用于表头 helpText、文档生成等）=====
export const FIELD_DESCRIPTIONS: Record<string, string> = {
  dsa_dir_bars: '当前趋势方向已持续的 K 线根数；负值表示空头方向，正值表示多头方向。',
}

// [feishu-capture] - 描述: 飞书截图强制图层配置，capture=feishu 时强制开启这些图层且不可关闭
//   advice.md v6 第 2 条：截图模式下 dsa/bb/profile/node/poc 必须开启，忽略 localStorage 与策略默认值
export const FEISHU_CAPTURE_LAYERS = ['dsa', 'bb', 'profile', 'node', 'poc'] as const
export type FeishuCaptureLayer = typeof FEISHU_CAPTURE_LAYERS[number]

// ===== 计算窗口（对应原型 CALCULATION_WINDOWS）=====
export const CALCULATION_WINDOWS: Record<string, CalculationWindow> = {
  '15m': { bars: 500, label: '最近 500 根 15m Bar' },
  '1h': { bars: 320, label: '最近 320 根 1h Bar' },
  '1d': { bars: 300, label: '最近 300 个交易日' },
  '1w': { bars: 156, label: '最近 156 周' },
  '1mo': { bars: 48, label: '最近 48 个月' },
}

// ===== 辅助函数 =====
export function resolveStrategy(
  source: 'selection' | 'watchlist',
  strategy: string,
): StrategyDef {
  if (strategy === 'combined') {
    return source === 'selection' ? STRATEGIES.dsa_selector : STRATEGIES.watchlist_monitor
  }
  return STRATEGIES[strategy] || STRATEGIES.watchlist_monitor
}

export function availableLayerIds(
  source: 'selection' | 'watchlist',
  strategy: string,
): string[] {
  const selected = resolveStrategy(source, strategy)
  const ids = new Set(selected.layers)
  // 用户可以手动叠加已发布策略的图层；页面不写死具体策略。
  Object.values(STRATEGIES).forEach((s) => s.layers.forEach((id) => ids.add(id)))
  return [...ids]
}

// 获取图层分组（用于图层面板渲染）
export function getLayerGroups(layerIds: string[]): Record<string, LayerDef[]> {
  const groups: Record<string, LayerDef[]> = {}
  layerIds.forEach((id) => {
    const layer = LAYERS[id]
    if (!layer) return
    if (!groups[layer.group]) groups[layer.group] = []
    groups[layer.group].push(layer)
  })
  return groups
}

// ===== 策略图示分组（对应原型 DISPLAY_GROUPS）=====
// 个股详情页按策略统一控制显示。一个开关统一控制该策略的全部图层
export interface DisplayGroupDef {
  id: string
  name: string
  shortName: string
  section: string
  color: string
  description: string
  layers: string[]
  anchorLayer: string
}

export const DISPLAY_GROUPS: Record<string, DisplayGroupDef> = {
  dsa: { id: 'dsa', name: '趋势稳定性', shortName: '趋势', section: '选股策略', color: '#ff1744', description: '趋势参考价 · 选股命中标记', layers: ['dsa', 'selection'], anchorLayer: 'dsa' },
  breakout: { id: 'breakout', name: '突破强度', shortName: '突破', section: '选股策略', color: '#ef5350', description: '压力区 · 突破确认 · 选股命中标记', layers: ['breakout', 'selection'], anchorLayer: 'breakout' },
  node: { id: 'node', name: '成交量节点', shortName: '节点', section: '监控策略', color: '#4f7cff', description: '筹码峰 · 节点区间 · POC · 事件标记', layers: ['profile', 'node', 'poc'], anchorLayer: 'node' },
  bb: { id: 'bb', name: '布林带', shortName: 'BB', section: '监控策略', color: '#9c27b0', description: '布林带 · SMA(20) ± 2σ', layers: ['bb'], anchorLayer: 'bb' },
  macd: { id: 'macd', name: 'MACD', shortName: 'MACD', section: '技术指标', color: '#f4c430', description: 'MACD 副图 · DIF/DEA/Histogram', layers: ['macd'], anchorLayer: 'macd' },
}
