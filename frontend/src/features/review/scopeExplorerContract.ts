// [SLICE 5 / Explorer] 横截面对比列表 typed owner（唯一 display / 单位 owner）。
//
// 只消费 row.compareFacts（backend 单一 batch read-model 已提供一切）。
//
// 禁止（全部由 backend list DTO 提供）：
// - 读取 raw Observation / 调用 detail
// - 计算 cross-sectional peer percentile
// - Capital Tilt（AW - EW）
// - Migration（由集合差异推导）
// - Momentum ratio（count / denominator）
// - SMC 事件选择 / 优先级
// - 任何综合评分、加权排序、机会/风险判断公式
//
// 允许：单位 formatter、方向色、null/unavailable 文案、sort 值抽取、SMC compact label。
import { NULL_DISPLAY } from './reviewFormat'
import type { ReviewScopeCompareFacts, ReviewScopeCompareSmc } from './types'

// ---------------------------------------------------------------------------
// 单位（§3 严格锁）
// ---------------------------------------------------------------------------

function fin(v: number | null | undefined): boolean {
  return v !== null && v !== undefined && Number.isFinite(v)
}

/** DSA Strength：无量纲原值 0.72 → "0.72" */
export function formatDsaStrength(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  const v = value as number
  // 保留两位小数，去掉无意义的尾随 0（0.72 → "0.72"）
  return String(Math.round(v * 100) / 100)
}

/**
 * peer percentile：canonical 0–100 position evidence。
 * 82.4 → "P82"（绝不 ×100，绝不带 %）。
 */
export function formatPeerPercentile(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  return `P${Math.round(value as number)}`
}

/**
 * DSA Duration：canonical median，可能是 .5。
 * 12 → "12"；12.5 → "12.5"（不假装一定是整数）。
 */
export function formatDurationBars(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  const v = value as number
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 10) / 10)
}

/**
 * DSA VWAP Dev：canonical 已是 percentage points。
 * 4.2 → "4.20%"（绝不能 ×100 变 420%）。
 */
export function formatDsaVwapDev(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  const v = value as number
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

/** EW / Capital Tilt：decimal return → 百分比，带符号（0.012 → "+1.20%"） */
export function formatDecimalReturnSigned(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  const v = value as number
  return `${v >= 0 ? '+' : '-'}${Math.abs(v * 100).toFixed(2)}%`
}

/** Volume Ratio20：ratio → "1.35×"（不 ×100） */
export function formatVolumeRatio20(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  return `${(value as number).toFixed(2)}×`
}

/** Breadth advance ratio：0.62 → "62%" */
export function formatBreadthRatio(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  return `${Math.round((value as number) * 100)}%`
}

/** Momentum ratio：0.42 → "42%" */
export function formatMomentumRatio(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  return `${Math.round((value as number) * 100)}%`
}

/** SMC member ratio：0.12 → "12%" */
export function formatSmcRatio(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  return `${Math.round((value as number) * 100)}%`
}

/**
 * Migration：沿现有 Leadership contract，raw 0–1 numeric。
 * 0.58 → "0.580"（不擅自改成 58%）。
 */
export function formatMigration(value: number | null | undefined): string {
  if (!fin(value)) return NULL_DISPLAY
  return (value as number).toFixed(3)
}

// ---------------------------------------------------------------------------
// 方向色（§12：只表达方向/数值语义，不表达“好坏”）
// ---------------------------------------------------------------------------

export type DirectionTone = 'positive' | 'negative' | 'neutral'

/** A 股：positive → 红，negative → 绿，0/null → neutral */
export function directionTone(value: number | null | undefined): DirectionTone {
  if (!fin(value) || (value as number) === 0) return 'neutral'
  return (value as number) > 0 ? 'positive' : 'negative'
}

export const DIRECTION_COLOR: Record<DirectionTone, string> = {
  positive: '#dc2626',
  negative: '#16a34a',
  neutral: '#475569',
}

// ---------------------------------------------------------------------------
// SMC compact label（§4：backend 已完成 selection，前端只 render）
// ---------------------------------------------------------------------------

const LEVEL_ABBR: Record<string, string> = { Swing: 'S', Internal: 'I' }
const DIRECTION_ARROW: Record<string, string> = { Up: '↑', Down: '↓' }

export interface SmcDisplay {
  primary: string
  secondary: string
  availability: string
  tone: DirectionTone
}

/**
 * availability unavailable → "—"（不写“无事件”）
 * availability ready && 无事件 → "无"
 * ready + 事件 → "S-CHoCH ↑" / 次行 "12%"
 */
export function smcCompactLabel(smc: ReviewScopeCompareSmc | null | undefined): SmcDisplay {
  if (!smc) return { primary: NULL_DISPLAY, secondary: '', availability: 'unavailable', tone: 'neutral' }
  if (smc.availability !== 'ready') {
    return { primary: NULL_DISPLAY, secondary: '', availability: smc.availability, tone: 'neutral' }
  }
  if (!smc.eventType) {
    return { primary: '无', secondary: '', availability: 'ready', tone: 'neutral' }
  }
  // 只显示 BOS / CHoCH；OB / EQH / EQL / SQZ_RELEASE 由 backend 过滤，这里再兜一层
  if (smc.eventType !== 'BOS' && smc.eventType !== 'CHoCH') {
    return { primary: '无', secondary: '', availability: 'ready', tone: 'neutral' }
  }
  const level = LEVEL_ABBR[smc.structureLevel ?? ''] ?? smc.structureLevel ?? ''
  const arrow = DIRECTION_ARROW[smc.direction ?? ''] ?? ''
  return {
    primary: `${level}-${smc.eventType} ${arrow}`.trim(),
    secondary: formatSmcRatio(smc.memberRatio),
    availability: 'ready',
    tone: smc.direction === 'Up' ? 'positive' : smc.direction === 'Down' ? 'negative' : 'neutral',
  }
}

// ---------------------------------------------------------------------------
// Display VM
// ---------------------------------------------------------------------------

export interface ExplorerRowVM {
  dsaStrength: number | null
  dsaStrengthText: string
  dsaPeerText: string

  dsaDuration: number | null
  dsaDurationText: string

  dsaVwapDev: number | null
  dsaVwapDevText: string

  smcMemberRatio: number | null
  smcPrimaryText: string
  smcSecondaryText: string
  smcAvailability: string
  smcTone: DirectionTone

  momentumEnhancing: number | null
  momentumWeakening: number | null
  momentumText: string
  momentumSecondaryText: string

  volumeRatio20: number | null
  volumeRatio20Text: string

  equalWeightReturn: number | null
  equalWeightReturnText: string
  ewPeerText: string

  advanceRatio: number | null
  advanceRatioText: string

  capitalTilt: number | null
  capitalTiltText: string

  migration: number | null
  migrationText: string
}

/** compareFacts → display VM（只做单位/文案/方向，不做任何业务重算） */
export function buildExplorerRowVM(c: ReviewScopeCompareFacts | null | undefined): ExplorerRowVM {
  const smc = smcCompactLabel(c?.smc)
  const dsaStrength = c?.dsa?.regimeStrength ?? null
  const dsaDuration = c?.dsa?.durationBars ?? null
  const dsaVwapDev = c?.dsa?.vwapDevPct ?? null
  const momentumEnhancing = c?.momentum?.enhancingRatio ?? null
  const momentumWeakening = c?.momentum?.weakeningRatio ?? null
  const volumeRatio20 = c?.volume?.ratio20 ?? null
  const equalWeightReturn = c?.price?.equalWeightReturn ?? null
  const advanceRatio = c?.price?.advanceRatio ?? null
  const capitalTilt = c?.composition?.capitalTilt ?? null
  const migration = c?.composition?.migration ?? null

  return {
    dsaStrength,
    dsaStrengthText: formatDsaStrength(dsaStrength),
    dsaPeerText: formatPeerPercentile(c?.dsa?.regimeStrengthPeerPercentile),

    dsaDuration,
    dsaDurationText: formatDurationBars(dsaDuration),

    dsaVwapDev,
    dsaVwapDevText: formatDsaVwapDev(dsaVwapDev),

    smcMemberRatio: c?.smc?.availability === 'ready' ? (c?.smc?.memberRatio ?? null) : null,
    smcPrimaryText: smc.primary,
    smcSecondaryText: smc.secondary,
    smcAvailability: smc.availability,
    smcTone: smc.tone,

    momentumEnhancing,
    momentumWeakening,
    momentumText: `增强 ${formatMomentumRatio(momentumEnhancing)}`,
    momentumSecondaryText: `减弱 ${formatMomentumRatio(momentumWeakening)}`,

    volumeRatio20,
    volumeRatio20Text: formatVolumeRatio20(volumeRatio20),

    equalWeightReturn,
    equalWeightReturnText: formatDecimalReturnSigned(equalWeightReturn),
    ewPeerText: formatPeerPercentile(c?.price?.equalWeightReturnPeerPercentile),

    advanceRatio,
    advanceRatioText: formatBreadthRatio(advanceRatio),

    capitalTilt,
    capitalTiltText: formatDecimalReturnSigned(capitalTilt),

    migration,
    migrationText: formatMigration(migration),
  }
}

// ---------------------------------------------------------------------------
// Visible sort（§11：visible 列只读 compareFacts）
// ---------------------------------------------------------------------------

export type VisibleSortKey =
  | 'dsa_strength'
  | 'dsa_duration'
  | 'dsa_vwap_dev'
  | 'smc_member_ratio'
  | 'momentum_enhancing'
  | 'volume_ratio20'
  | 'equal_weight_return'
  | 'advance_ratio'
  | 'capital_tilt'
  | 'migration'

export const VISIBLE_SORT_KEYS: readonly VisibleSortKey[] = [
  'dsa_strength',
  'dsa_duration',
  'dsa_vwap_dev',
  'smc_member_ratio',
  'momentum_enhancing',
  'volume_ratio20',
  'equal_weight_return',
  'advance_ratio',
  'capital_tilt',
  'migration',
]

export function isVisibleSortKey(key: string): key is VisibleSortKey {
  return (VISIBLE_SORT_KEYS as readonly string[]).includes(key)
}

/** 从 compareFacts 抽取排序数值（null → null，由 sortScopes 统一沉底） */
export function compareSortValue(
  c: ReviewScopeCompareFacts | null | undefined,
  key: VisibleSortKey,
): number | null {
  if (!c) return null
  switch (key) {
    case 'dsa_strength':
      return fin(c.dsa?.regimeStrength) ? (c.dsa?.regimeStrength as number) : null
    case 'dsa_duration':
      return fin(c.dsa?.durationBars) ? (c.dsa?.durationBars as number) : null
    case 'dsa_vwap_dev':
      return fin(c.dsa?.vwapDevPct) ? (c.dsa?.vwapDevPct as number) : null
    case 'smc_member_ratio':
      // unavailable 与「无事件」都沉底，不参与排序竞争
      return c.smc?.availability === 'ready' && fin(c.smc.memberRatio) ? (c.smc.memberRatio as number) : null
    case 'momentum_enhancing':
      return fin(c.momentum?.enhancingRatio) ? (c.momentum?.enhancingRatio as number) : null
    case 'volume_ratio20':
      return fin(c.volume?.ratio20) ? (c.volume?.ratio20 as number) : null
    case 'equal_weight_return':
      return fin(c.price?.equalWeightReturn) ? (c.price?.equalWeightReturn as number) : null
    case 'advance_ratio':
      return fin(c.price?.advanceRatio) ? (c.price?.advanceRatio as number) : null
    case 'capital_tilt':
      return fin(c.composition?.capitalTilt) ? (c.composition?.capitalTilt as number) : null
    case 'migration':
      return fin(c.composition?.migration) ? (c.composition?.migration as number) : null
    default:
      return null
  }
}
