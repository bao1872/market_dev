// [Round 2026-07-28-3] 第一金字塔 ViewModel
// 职责：DTO → 展示模型转换，类型安全字段提取。
// 禁止：重新计算量化指标；解析 statusText 推断多空或事件类型。
// 优先读取 continuousFactors 中的结构化字段；缺少字段时不显示，不猜测。
import type {
  DimensionResult,
  FirstPyramidSnapshot,
  PyramidEvent,
  VolumeContextSchema,
} from '@/api/endpoints'

/** 方向：1=偏多/上行, -1=偏空/下行, 0=中性/未形成 */
export type Direction = 1 | -1 | 0

export interface VolumeWaterLevelVM {
  ready: boolean
  percentile20: number | null
  percentile200: number | null
  zscore20: number | null
  zscore200: number | null
  badge: string | null
}

export interface StructureEventVM {
  typeLabel: string
  directionLabel: string
  levelLabel: string | null
  freshnessLabel: string
  occurredAt: string | null
  price: number | null
  volumeBadge: string | null
}

export interface TrendVM {
  available: boolean
  statusText: string
  direction: Direction
  continuousBars: number | null
  currentVsPrevVolumeRatio: number | null
  freshnessLabel: string
  /** 距 DSA VWAP 偏离百分比（null=缺失） */
  vwapDeviationPct: number | null
  /** 当前段量比（与 currentVsPrevVolumeRatio 同字段，语义保留） */
  segmentVolumeRatio: number | null
  /** 趋势强度标签（null=缺失） */
  trendStrength: string | null
}

export interface StructureVM {
  available: boolean
  statusText: string
  swingDirection: Direction
  internalDirection: Direction
  events: StructureEventVM[]
}

export interface MomentumVM {
  available: boolean
  statusText: string
  squeezeOn: boolean
  direction: Direction
  /** SQZMOM 直方图值（>0 偏多，<0 偏空，null=缺失） */
  sqzmomVal: number | null
  /** SQZMOM 前值（用于计算变化方向） */
  sqzmomPrev: number | null
  /** BB 带宽（null=缺失） */
  bbWidth: number | null
  /** BB 位置 0~1（0=下轨，0.5=中轨，1=上轨，null=缺失） */
  bbPosition: number | null
  /** 动量变化标签（增强/减弱/转多/转空/持平） */
  momentumChangeLabel: string | null
  releaseVsSqueezeRatio: number | null
  volDivergence: string | null
}

export interface ChipConsensusVM {
  available: boolean
  statusText: string
  /** 价格相对 POC 位置标签（null=无 POC 或不可用） */
  positionLabel: string | null
  /** POC 价格（null=无有效 POC） */
  pocPrice: number | null
  /** 最新收盘价（null=缺失） */
  lastClose: number | null
  /** 距 POC 偏离百分比（null=无 POC 或收盘价） */
  distancePct: number | null
  /** 筹码峰数量（null=缺失） */
  nPeakNodes: number | null
}

export interface FirstPyramidVM {
  symbol: string
  tradeDate: string
  statusText: string
  volumeWaterLevel: VolumeWaterLevelVM
  trend: TrendVM
  structure: StructureVM
  momentum: MomentumVM
  chipConsensus: ChipConsensusVM | null
}

// ===== 类型安全字段提取 =====

function asNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  return null
}

function asString(v: unknown): string | null {
  if (typeof v === 'string' && v.length > 0) return v
  return null
}

function asBool(v: unknown): boolean | null {
  if (typeof v === 'boolean') return v
  return null
}

function directionFromSign(n: number | null): Direction {
  if (n === null) return 0
  if (n > 0) return 1
  if (n < 0) return -1
  return 0
}

function directionFromInt(v: unknown): Direction {
  const n = asNumber(v)
  if (n === null) return 0
  return directionFromSign(n)
}

// ===== 中文标签映射 =====

const EVENT_TYPE_LABEL: Record<string, string> = {
  BOS: '结构突破',
  CHoCH: '结构转折',
  OB_ENTRY: '进入订单区域',
  EQH: '连续高点',
  EQL: '连续低点',
}

const DIRECTION_LABEL: Record<string, string> = {
  up: '上行',
  down: '下行',
}

const STRUCTURE_LEVEL_LABEL: Record<string, string> = {
  swing: '主要级别',
  internal: '短线级别',
}

const FRESHNESS_LABEL = (bars: number | null): string => {
  if (bars === null) return ''
  if (bars === 0) return '今日'
  if (bars === 1) return '1根前'
  return `${bars}根前`
}

// ===== ViewModel 构造 =====

function buildVolumeWaterLevel(vc: VolumeContextSchema | null | undefined): VolumeWaterLevelVM {
  if (!vc || !vc.readiness) {
    return {
      ready: false,
      percentile20: null,
      percentile200: null,
      zscore20: null,
      zscore200: null,
      badge: null,
    }
  }
  return {
    ready: true,
    percentile20: asNumber(vc.volumePercentile20),
    percentile200: asNumber(vc.volumePercentile200),
    zscore20: asNumber(vc.volumeZscore20),
    zscore200: asNumber(vc.volumeZscore200),
    badge: asString(vc.badge),
  }
}

function buildStructureEvent(e: PyramidEvent): StructureEventVM {
  const levelRaw = e.extra?.['structure_level']
  return {
    typeLabel: EVENT_TYPE_LABEL[e.type] ?? e.type,
    directionLabel: e.direction ? (DIRECTION_LABEL[e.direction] ?? e.direction) : '—',
    levelLabel: typeof levelRaw === 'string' ? (STRUCTURE_LEVEL_LABEL[levelRaw] ?? null) : null,
    freshnessLabel: FRESHNESS_LABEL(asNumber(e.freshnessBars)),
    occurredAt: e.occurredAt,
    price: asNumber(e.price),
    volumeBadge: asString(e.volumeBadge),
  }
}

function buildTrend(dim: DimensionResult): TrendVM {
  const cf = dim.continuousFactors
  const regimeValue = asNumber(cf['regime_value'])
  // 持续根数读取 dsa_dir_bars（后端实际字段名）
  const continuousBars = asNumber(cf['dsa_dir_bars'])
  const ratio = asNumber(cf['current_vs_prev_volume_ratio'])
  // P0 修复：后端无独立"趋势变化新鲜度"字段，不得用 continuous_bars 充当
  const trendChangeFreshness = asNumber(cf['trend_change_freshness_bars'])
  const vwapDevPct = asNumber(cf['dsa_vwap_dev_pct'])
  const strength = asString(cf['trend_strength'])
  return {
    available: dim.available,
    statusText: dim.statusText,
    direction: directionFromSign(regimeValue),
    continuousBars: continuousBars,
    currentVsPrevVolumeRatio: ratio,
    freshnessLabel: FRESHNESS_LABEL(trendChangeFreshness),
    vwapDeviationPct: vwapDevPct,
    segmentVolumeRatio: ratio,
    trendStrength: strength,
  }
}

function buildStructure(dim: DimensionResult, maxEvents: number): StructureVM {
  const cf = dim.continuousFactors
  const events = Array.isArray(dim.events)
    ? dim.events.slice(-maxEvents).reverse().map(buildStructureEvent)
    : []
  return {
    available: dim.available,
    statusText: dim.statusText,
    swingDirection: directionFromInt(cf['swing_direction']),
    internalDirection: directionFromInt(cf['internal_direction']),
    events,
  }
}

function buildMomentum(dim: DimensionResult): MomentumVM {
  const cf = dim.continuousFactors
  const squeezeOn = asBool(cf['squeeze_on']) ?? false
  const sqzmomVal = asNumber(cf['sqzmom_val'])
  const sqzmomPrev = asNumber(cf['sqzmom_val_prev'])
  const bbWidth = asNumber(cf['bb_width'])
  const bbPosition = asNumber(cf['bb_position'])
  const releaseRatio = asNumber(cf['release_vs_squeeze_volume_ratio'])
  const volDiv = asString(cf['vol_divergence'])
  // P0 修复：动量方向由 sqzmom_val 符号决定，不得用 squeeze_on 推断
  return {
    available: dim.available,
    statusText: dim.statusText,
    squeezeOn,
    direction: directionFromSign(sqzmomVal),
    sqzmomVal,
    sqzmomPrev,
    bbWidth,
    bbPosition,
    momentumChangeLabel: computeMomentumChangeLabel(sqzmomVal, sqzmomPrev),
    releaseVsSqueezeRatio: releaseRatio,
    volDivergence: volDiv,
  }
}

/** 比较当前与前值，生成动量变化标签 */
function computeMomentumChangeLabel(
  curr: number | null,
  prev: number | null,
): string | null {
  if (curr === null || prev === null) return null
  if (prev === 0) return curr > 0 ? '转多' : curr < 0 ? '转空' : '持平'
  // 符号翻转
  if (prev > 0 && curr < 0) return '转空'
  if (prev < 0 && curr > 0) return '转多'
  // 同符号
  if (Math.abs(curr) > Math.abs(prev)) return '增强'
  if (Math.abs(curr) < Math.abs(prev)) return '减弱'
  return '持平'
}

function buildChipConsensus(dim: DimensionResult | null): ChipConsensusVM | null {
  if (!dim) return null
  const cf = dim.continuousFactors
  const pocPrice = asNumber(cf['poc_price'])
  const lastClose = asNumber(cf['last_close'])
  const nPeakNodes = asNumber(cf['n_peak_nodes'])
  // P0 修复：筹码 available 不等于偏多；显示真实价格相对 POC 位置或中性"可用"
  let positionLabel: string | null = null
  let distancePct: number | null = null
  if (pocPrice !== null && lastClose !== null) {
    if (lastClose > pocPrice) positionLabel = 'POC上方'
    else if (lastClose < pocPrice) positionLabel = 'POC下方'
    else positionLabel = '贴合POC'
    distancePct = ((lastClose - pocPrice) / pocPrice) * 100
  }
  return {
    available: dim.available,
    statusText: dim.statusText,
    positionLabel,
    pocPrice,
    lastClose,
    distancePct,
    nPeakNodes,
  }
}

export function buildFirstPyramidVM(
  data: FirstPyramidSnapshot,
  variant: 'compact' | 'detail',
): FirstPyramidVM {
  const maxEvents = variant === 'compact' ? 3 : 5
  return {
    symbol: data.symbol,
    tradeDate: data.tradeDate,
    statusText: data.statusText,
    volumeWaterLevel: buildVolumeWaterLevel(data.volumeContext),
    trend: buildTrend(data.trend),
    structure: buildStructure(data.structure, maxEvents),
    momentum: buildMomentum(data.momentum),
    chipConsensus: buildChipConsensus(data.chipConsensus),
  }
}

/** 方向 → CSS class（A股语义：偏多=红，偏空=绿，中性=muted） */
export function directionClass(dir: Direction): string {
  if (dir === 1) return 'dir-up'
  if (dir === -1) return 'dir-down'
  return 'dir-neutral'
}

/** 方向 → 中文标签 */
export function directionLabel(dir: Direction): string {
  if (dir === 1) return '偏多'
  if (dir === -1) return '偏空'
  return '未形成'
}

/** 量能徽标 CSS class */
export function volumeBadgeClass(badge: string | null): string {
  if (badge === '放量') return 'vol-up'
  if (badge === '缩量') return 'vol-down'
  if (badge === '正常') return 'vol-normal'
  return 'vol-unknown'
}
