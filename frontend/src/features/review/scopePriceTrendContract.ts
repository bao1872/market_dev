// [R3C] Price + Trend formal Observation contract owner (pure TS, no React).
//
// Single deterministic parser for canonical L2 groups G1–G4 (price_capital,
// trend_state, trend_progress, trend_volume_confirmation). It consumes:
//   detail.observationGroups  (L2 projection; facts are L1 objects by reference)
// + supporting persisted L1 metadata from detail.observation where L2 alone
//   cannot truthfully distinguish availability (e.g. Total Amount validity).
//
// It does NOT recompute, score, signal, rank, or derive opportunity/risk.
// It does NOT implement detectFactKind / inferDistribution / generic auto-renderer.
// Renderer components consume the typed ViewModels below, never scattered
// `as Record<string, unknown>` casts on the raw facts.
//
// NUMERIC SCALE CONTRACT (P0 — no x100 errors):
//   - EW/AW Return: decimal return ratio -> *100 (formatPercentNullable).
//   - Categorical stored ratios (trend direction, open category): already
//     ratio -> *100 (formatPercentNullable).
//   - DSA-VWAP dev / Segment Change / VWAP Return Total: ALREADY percentage
//     points -> NO x100 (formatPercentagePointsNullable).
//   - Segment Slope: %/bar -> NO x100 (formatPctPerBarNullable).
//   - Segment Volume/Amount Ratio: dimensionless multiple -> NO x100
//     (formatMultipleNullable).
//   - Total Volume / Total Amount: RAW canonical sums, physical unit NOT frozen
//     -> raw formatted number, no unit suffix, NO x100.

import {
  formatPercentNullable,
  formatNumberNullable,
  NULL_DISPLAY,
} from './reviewFormat'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function deepGet(payload: unknown, path: readonly string[]): unknown {
  let node: unknown = payload
  for (const key of path) {
    if (node === null || typeof node !== 'object' || !(key in (node as Record<string, unknown>))) {
      return undefined
    }
    node = (node as Record<string, unknown>)[key]
  }
  return node
}

// ---------------------------------------------------------------------------
// Formatters (explicit, non-overlapping semantics)
// ---------------------------------------------------------------------------

/** Percentage points already (4.2 -> "4.20%"). NO x100. */
export function formatPercentagePointsNullable(
  value: number | null | undefined,
  digits = 2,
): string {
  if (!isFiniteNumber(value)) return NULL_DISPLAY
  return `${value.toFixed(digits)}%`
}

/** Per-bar slope (0.35 -> "0.35%/bar"). NO x100. */
export function formatPctPerBarNullable(
  value: number | null | undefined,
  digits = 2,
): string {
  if (!isFiniteNumber(value)) return NULL_DISPLAY
  return `${value.toFixed(digits)}%/bar`
}

/** Dimensionless multiple (1.15 -> "1.15×"). NO x100. */
export function formatMultipleNullable(
  value: number | null | undefined,
  digits = 2,
): string {
  if (!isFiniteNumber(value)) return NULL_DISPLAY
  return `${value.toFixed(digits)}×`
}

/** Raw canonical scalar sum — physical unit NOT frozen, no suffix. */
export function formatRawSumNullable(
  value: number | null | undefined,
  decimals = 2,
): string {
  if (!isFiniteNumber(value)) return NULL_DISPLAY
  const fixed = value.toFixed(decimals)
  const [intPart, frac] = fixed.split('.')
  const withSep = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return frac ? `${withSep}.${frac}` : withSep
}

// ---------------------------------------------------------------------------
// Direction color eligibility (A-share: positive red / negative green)
// ---------------------------------------------------------------------------

export type DirectionTone = 'up' | 'down' | 'neutral'

export function signedTone(value: number | null | undefined): DirectionTone {
  if (!isFiniteNumber(value)) return 'neutral'
  if (value > 0) return 'up'
  if (value < 0) return 'down'
  return 'neutral'
}

// ---------------------------------------------------------------------------
// HHI object (price.concentration / price.amount.concentration)
// ---------------------------------------------------------------------------

export interface HhiFacts {
  rawHhi: number | null
  normalizedHhi: number | null
  memberCount: number
  status: string
}

function parseHhi(fact: unknown): HhiFacts | null {
  if (fact === null || typeof fact !== 'object') return null
  const o = fact as Record<string, unknown>
  const raw = o['raw_hhi']
  const norm = o['normalized_hhi']
  const n = o['member_count']
  const status = typeof o['status'] === 'string' ? (o['status'] as string) : ''
  if (!isFiniteNumber(n)) return null
  return {
    rawHhi: isFiniteNumber(raw) ? raw : null,
    normalizedHhi: isFiniteNumber(norm) ? norm : null,
    memberCount: n,
    status,
  }
}

// ---------------------------------------------------------------------------
// G1 — Price & Capital
// ---------------------------------------------------------------------------

export interface PriceCapitalFacts {
  equalWeightReturn: number | null
  amountWeightedReturn: number | null
  totalVolume: number | null
  totalAmount: number | null
  priceHhi: HhiFacts | null
  amountHhi: HhiFacts | null
  /** supporting persisted L1 metadata for Total Amount availability. */
  amountValidCount: number | null
  amountAvailability: 'unavailable' | 'valid-zero' | 'valid'
}

export interface PriceCapitalVM {
  equalWeightReturn: string
  equalWeightReturnTone: DirectionTone
  amountWeightedReturn: string
  amountWeightedReturnTone: DirectionTone
  totalVolume: string
  totalAmount: string
  amountAvailabilityNote: string | null
  priceHhi: HhiFacts | null
  amountHhi: HhiFacts | null
}

export function parsePriceCapital(
  groupFacts: Record<string, unknown> | undefined,
  observation: Record<string, unknown> | undefined,
): PriceCapitalFacts | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const ew = groupFacts['equal_weight_return']
  const aw = groupFacts['amount_weighted_return']
  const vol = groupFacts['total_volume']
  const amt = groupFacts['total_amount']
  const pHhi = parseHhi(groupFacts['price_hhi'])
  const aHhi = parseHhi(groupFacts['amount_hhi'])

  // Supporting persisted L1: observation.price.amount.valid_count
  const validCount = deepGet(observation, ['price', 'amount', 'valid_count'])

  let availability: PriceCapitalFacts['amountAvailability'] = 'valid'
  let amountValidCount: number | null = null
  if (isFiniteNumber(validCount)) {
    amountValidCount = validCount
    if (validCount === 0) {
      availability = 'unavailable'
    } else if (amt === 0 || amt === null) {
      // valid_count>0 且 total_amount==0 -> observed zero; total_amount null
      // with valid members -> still "valid" (displayed as raw —, no fake zero).
      availability = amt === 0 ? 'valid-zero' : 'valid'
    }
  }

  return {
    equalWeightReturn: isFiniteNumber(ew) ? ew : null,
    amountWeightedReturn: isFiniteNumber(aw) ? aw : null,
    totalVolume: isFiniteNumber(vol) ? vol : null,
    totalAmount: isFiniteNumber(amt) ? amt : null,
    priceHhi: pHhi,
    amountHhi: aHhi,
    amountValidCount,
    amountAvailability: availability,
  }
}

export function buildPriceCapitalVM(facts: PriceCapitalFacts | null): PriceCapitalVM | null {
  if (!facts) return null
  let note: string | null = null
  if (facts.amountAvailability === 'unavailable') {
    note = '成交额不可用（无有效成交额成员）'
  }
  return {
    equalWeightReturn: formatPercentNullable(facts.equalWeightReturn, 2),
    equalWeightReturnTone: signedTone(facts.equalWeightReturn),
    amountWeightedReturn: formatPercentNullable(facts.amountWeightedReturn, 2),
    amountWeightedReturnTone: signedTone(facts.amountWeightedReturn),
    totalVolume: formatRawSumNullable(facts.totalVolume),
    totalAmount: formatRawSumNullable(facts.totalAmount),
    amountAvailabilityNote: note,
    priceHhi: facts.priceHhi,
    amountHhi: facts.amountHhi,
  }
}

// ---------------------------------------------------------------------------
// G2 — Trend State (distribution + strength + dsa-vwap deviation)
// ---------------------------------------------------------------------------

export interface TrendDirectionFacts {
  upCount: number | null
  upRatio: number | null
  neutralCount: number | null
  neutralRatio: number | null
  downCount: number | null
  downRatio: number | null
  denominator: number | null
}

export interface TrendStateFacts {
  direction: TrendDirectionFacts | null
  trendStrength: number | null
  dsaVwapDevPct: number | null
}

export interface TrendStateVM {
  direction: TrendDirectionFacts | null
  denominatorZero: boolean
  trendStrength: string
  dsaVwapDevPct: string
  dsaVwapDevTone: DirectionTone
}

export function parseTrendState(groupFacts: Record<string, unknown> | undefined): TrendStateFacts | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const dir = groupFacts['trend_direction_member_ratio']
  let direction: TrendDirectionFacts | null = null
  if (dir && typeof dir === 'object') {
    const d = dir as Record<string, unknown>
    const num = (k: string): number | null => {
      const v = d[k]
      return isFiniteNumber(v) ? v : null
    }
    direction = {
      upCount: num('up_count'),
      upRatio: num('up_ratio'),
      neutralCount: num('neutral_count'),
      neutralRatio: num('neutral_ratio'),
      downCount: num('down_count'),
      downRatio: num('down_ratio'),
      denominator: num('denominator'),
    }
  }
  const strength = groupFacts['trend_strength']
  const dsa = groupFacts['dsa_vwap_dev_pct']
  return {
    direction,
    trendStrength: isFiniteNumber(strength) ? strength : null,
    dsaVwapDevPct: isFiniteNumber(dsa) ? dsa : null,
  }
}

export function buildTrendStateVM(facts: TrendStateFacts | null): TrendStateVM | null {
  if (!facts) return null
  return {
    direction: facts.direction,
    denominatorZero: (facts.direction?.denominator ?? 0) === 0,
    trendStrength: formatNumberNullable(facts.trendStrength, 1),
    dsaVwapDevPct: formatPercentagePointsNullable(facts.dsaVwapDevPct),
    dsaVwapDevTone: signedTone(facts.dsaVwapDevPct),
  }
}

// ---------------------------------------------------------------------------
// Shared segment ratio (G3 & G4 reuse the EXACT same L1 source path)
// ---------------------------------------------------------------------------

export interface SegmentRatioFacts {
  volumeRatio: number | null
  amountRatio: number | null
}

function parseSegmentRatio(groupFacts: Record<string, unknown> | undefined): SegmentRatioFacts {
  const v = groupFacts ? groupFacts['segment_volume_mean_ratio'] : undefined
  const a = groupFacts ? groupFacts['segment_amount_mean_ratio'] : undefined
  return {
    volumeRatio: isFiniteNumber(v) ? v : null,
    amountRatio: isFiniteNumber(a) ? a : null,
  }
}

// ---------------------------------------------------------------------------
// G3 — Trend Progress
// ---------------------------------------------------------------------------

export interface TrendProgressFacts {
  segmentBars: number | null
  segmentChangePct: number | null
  segmentSlope: number | null
  segmentVolumeRatio: number | null
  segmentAmountRatio: number | null
  vwapRetTotal: number | null
}

export interface TrendProgressVM {
  segmentBars: string
  segmentChangePct: string
  segmentChangeTone: DirectionTone
  segmentSlope: string
  segmentSlopeTone: DirectionTone
  volumeRatio: string
  amountRatio: string
  vwapRetTotal: string
  vwapRetTotalTone: DirectionTone
}

export function parseTrendProgress(groupFacts: Record<string, unknown> | undefined): TrendProgressFacts | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const numOrNull = (k: string): number | null => {
    const v = groupFacts[k]
    return isFiniteNumber(v) ? v : null
  }
  const ratio = parseSegmentRatio(groupFacts)
  return {
    segmentBars: numOrNull('current_segment_bars'),
    segmentChangePct: numOrNull('segment_change_pct'),
    segmentSlope: numOrNull('segment_slope'),
    segmentVolumeRatio: ratio.volumeRatio,
    segmentAmountRatio: ratio.amountRatio,
    vwapRetTotal: numOrNull('vwap_ret_total'),
  }
}

export function buildTrendProgressVM(facts: TrendProgressFacts | null): TrendProgressVM | null {
  if (!facts) return null
  return {
    segmentBars: formatNumberNullable(facts.segmentBars, 1),
    segmentChangePct: formatPercentagePointsNullable(facts.segmentChangePct),
    segmentChangeTone: signedTone(facts.segmentChangePct),
    segmentSlope: formatPctPerBarNullable(facts.segmentSlope),
    segmentSlopeTone: signedTone(facts.segmentSlope),
    volumeRatio: formatMultipleNullable(facts.segmentVolumeRatio),
    amountRatio: formatMultipleNullable(facts.segmentAmountRatio),
    vwapRetTotal: formatPercentagePointsNullable(facts.vwapRetTotal),
    vwapRetTotalTone: signedTone(facts.vwapRetTotal),
  }
}

// ---------------------------------------------------------------------------
// G4 — Trend × Volume Confirmation
// ---------------------------------------------------------------------------

export interface OpenCategoryEntry {
  category: string
  count: number | null
  ratio: number | null
}

export interface MomentumVolumeRelationFacts {
  status: string | null
  reason: string | null
  denominator: number | null
  categories: OpenCategoryEntry[]
}

export interface TrendVolumeConfirmationVM {
  volumeRatio: string
  amountRatio: string
  momentumRelation: MomentumVolumeRelationFacts | null
}

export function parseMomentumVolumeRelation(
  groupFacts: Record<string, unknown> | undefined,
): MomentumVolumeRelationFacts | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const raw = groupFacts['momentum_volume_relation']
  if (raw === null || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const status = typeof o['status'] === 'string' ? (o['status'] as string) : null
  const reason = typeof o['reason'] === 'string' ? (o['reason'] as string) : null
  const denom = o['denominator']
  const denominator = isFiniteNumber(denom) ? denom : null

  // Preserve upstream category tokens verbatim; pair *_count and *_ratio.
  // No hardcoded Review vocabulary. Fail closed on malformed shape.
  const categories: OpenCategoryEntry[] = []
  const countMap = new Map<string, number | null>()
  const ratioMap = new Map<string, number | null>()
  for (const [k, v] of Object.entries(o)) {
    const m = /^(.*)_count$/.exec(k)
    if (m) countMap.set(m[1], isFiniteNumber(v) ? v : null)
    const r = /^(.*)_ratio$/.exec(k)
    if (r) ratioMap.set(r[1], isFiniteNumber(v) ? v : null)
  }
  if (countMap.size === 0 && ratioMap.size === 0) {
    // No category pairs present (and not a clean unavailable) -> malformed.
    if (status !== 'unavailable') return null
  }
  for (const category of new Set([...countMap.keys(), ...ratioMap.keys()])) {
    categories.push({
      category,
      count: countMap.get(category) ?? null,
      ratio: ratioMap.get(category) ?? null,
    })
  }
  categories.sort((a, b) => a.category.localeCompare(b.category))

  return { status, reason, denominator, categories }
}

export function parseTrendVolumeConfirmation(
  groupFacts: Record<string, unknown> | undefined,
): TrendVolumeConfirmationVM | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const ratio = parseSegmentRatio(groupFacts)
  const momentum = parseMomentumVolumeRelation(groupFacts)
  return {
    volumeRatio: formatMultipleNullable(ratio.volumeRatio),
    amountRatio: formatMultipleNullable(ratio.amountRatio),
    momentumRelation: momentum,
  }
}
