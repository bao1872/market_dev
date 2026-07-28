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
  releaseVsSqueezeRatio: number | null
  volDivergence: string | null
}

export interface ChipConsensusVM {
  available: boolean
  statusText: string
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
  const continuousBars = asNumber(cf['continuous_bars'])
  const ratio = asNumber(cf['current_vs_prev_volume_ratio'])
  return {
    available: dim.available,
    statusText: dim.statusText,
    direction: directionFromSign(regimeValue),
    continuousBars: continuousBars,
    currentVsPrevVolumeRatio: ratio,
    freshnessLabel: FRESHNESS_LABEL(continuousBars),
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
  const releaseRatio = asNumber(cf['release_vs_squeeze_volume_ratio'])
  const volDiv = asString(cf['vol_divergence'])
  return {
    available: dim.available,
    statusText: dim.statusText,
    squeezeOn,
    // squeeze_on=false 视为释放（动量向上）；true 视为挤压中（无方向）
    direction: squeezeOn ? 0 : 1,
    releaseVsSqueezeRatio: releaseRatio,
    volDivergence: volDiv,
  }
}

function buildChipConsensus(dim: DimensionResult | null): ChipConsensusVM | null {
  if (!dim) return null
  return {
    available: dim.available,
    statusText: dim.statusText,
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
