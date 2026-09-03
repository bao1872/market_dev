// [R3E] Momentum (G7) + Volume (G8) formal Observation contract owner
// (pure TS, no React).
//
// Single deterministic parser for canonical L2 groups G7 (momentum_squeeze_release)
// and G8 (volume_anomaly). It consumes:
//   detail.observationGroups  (L2 projection; facts are L1 objects by reference)
//
// It does NOT recompute, score, signal, rank, derive opportunity/risk, or
// implement detectFactKind / generic auto-render. Renderer components consume
// the typed ViewModels below, never scattered `as Record<string, unknown>` casts
// on the raw facts.
//
// CONTRACT-FIRST SOURCE (backend/app/domain/review/scope_observation.py):
//
// G7 momentum_squeeze_release (momentum group):
//   squeeze_state:    _categorical_state_distribution(...) ->
//                     {squeeze_count, squeeze_ratio, squeeze_release_count,
//                      squeeze_release_ratio, non_squeeze_count, non_squeeze_ratio,
//                      denominator}. denominator = #members with non-null squeeze.
//                     No status/envelope: always a distribution dict. denominator=0
//                     -> all counts 0, all ratios None (NOT a fake "0%/0%/0%").
//   bb_position:      _current_only_distribution(...) ->
//                     ready: {median, p25, p75, valid_count, denominator}
//                     unavailable: {status:"unavailable", reason:..., valid_count:0}
//                     (NO denominator when unavailable; reason preserved).
//   bb_width:         same shape as bb_position. UNIT: raw dimensionless ratio.
//                     DO NOT x100.
//   release_volume_ratio: same shape as bb_position (member-first median).
//                     UNIT: multiple ("1.50×"). No direction color.
//
// G8 volume_anomaly (participation group):
//   ratio20/ratio200/percentile20/percentile200/zscore20/zscore200:
//                     _participation_distribution(...) ->
//                     {p25, p50, p75, valid_count}. NO status, NO denominator.
//                     valid_count = 0 -> all p-values null -> unavailable display.
//                     The four facts use a SINGLE canonical key per (metric,window):
//                     volume_ratio20, volume_ratio200, volume_percentile20,
//                     volume_percentile200, volume_zscore20, volume_zscore200.
//
// 200D readiness is owned UPSTREAM (raw 200D facts are emitted only when the
// canonical 200D readiness contract is satisfied). The frontend MUST NOT inspect
// history length, recreate readiness, substitute 20D, or treat unavailable as 0.

import {
  formatMultipleNullable,
  formatRawDimensionlessNullable,
  formatPercentileNullable,
  formatZScoreNullable,
  formatNumberNullable,
  NULL_DISPLAY,
} from './reviewFormat'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null
}

// ---------------------------------------------------------------------------
// G7 squeeze_state
// ---------------------------------------------------------------------------

export type SqueezeStateCategory = 'Squeeze' | 'Squeeze_Release' | 'Non_Squeeze'

export interface SqueezeStateVM {
  /** denominator = #members with non-null squeeze category */
  denominator: number
  /** true when no valid members (denominator === 0) */
  unavailable: boolean
  categories: {
    category: SqueezeStateCategory
    count: number
    /** null when denominator === 0 (NOT 0%) */
    ratio: number | null
  }[]
}

// Squeeze ratio is a PERSISTED fact (backend computes count/denominator).
// Frontend MUST NOT recompute (no count/denominator or ratio/denominator math).
// Read each persisted ratio verbatim; null stays null (denominator=0 producer).

export function parseSqueezeState(raw: unknown): SqueezeStateVM | null {
  const o = asRecord(raw)
  if (!o) return null
  const denomRaw = o['denominator']
  const denominator = typeof denomRaw === 'number' && Number.isFinite(denomRaw) && denomRaw >= 0
    ? Math.floor(denomRaw)
    : 0

  const defs: { key: string; cat: SqueezeStateCategory }[] = [
    { key: 'squeeze', cat: 'Squeeze' },
    { key: 'squeeze_release', cat: 'Squeeze_Release' },
    { key: 'non_squeeze', cat: 'Non_Squeeze' },
  ]
  const categories = defs.map(({ key, cat }) => {
    const countRaw = o[`${key}_count`]
    const count = typeof countRaw === 'number' && Number.isFinite(countRaw) && countRaw >= 0
      ? Math.floor(countRaw)
      : 0
    const ratioRaw = o[`${key}_ratio`]
    const ratio = typeof ratioRaw === 'number' && Number.isFinite(ratioRaw) ? ratioRaw : null
    return {
      category: cat,
      count,
      ratio,
    }
  })

  return {
    denominator,
    unavailable: denominator === 0,
    categories,
  }
}

// ---------------------------------------------------------------------------
// G7 current-only distributions (bb_position / bb_width / release_volume_ratio)
// ---------------------------------------------------------------------------

export interface CurrentOnlyDistributionVM {
  /** true when producer emitted {status:"unavailable", reason, valid_count:0} */
  unavailable: boolean
  /** preserved producer reason if unavailable */
  reason: string | null
  median: number | null
  p25: number | null
  p75: number | null
  validCount: number | null
  denominator: number | null
}

export function parseCurrentOnlyMomentumDistribution(raw: unknown): CurrentOnlyDistributionVM | null {
  const o = asRecord(raw)
  if (!o) return null
  if (o['status'] === 'unavailable') {
    return {
      unavailable: true,
      reason: strOrNull(o['reason']),
      median: null,
      p25: null,
      p75: null,
      validCount: typeof o['valid_count'] === 'number' ? (o['valid_count'] as number) : 0,
      denominator: null,
    }
  }
  const median = isFiniteNumber(o['median']) ? (o['median'] as number) : null
  const p25 = isFiniteNumber(o['p25']) ? (o['p25'] as number) : null
  const p75 = isFiniteNumber(o['p75']) ? (o['p75'] as number) : null
  const validCount = typeof o['valid_count'] === 'number' ? (o['valid_count'] as number) : null
  const denominator = typeof o['denominator'] === 'number' ? (o['denominator'] as number) : null
  return {
    unavailable: false,
    reason: null,
    median,
    p25,
    p75,
    validCount,
    denominator,
  }
}

// ---------------------------------------------------------------------------
// G7 momentum group
// ---------------------------------------------------------------------------

export interface MomentumObservationVM {
  squeeze: SqueezeStateVM | null
  bbPosition: CurrentOnlyDistributionVM | null
  bbWidth: CurrentOnlyDistributionVM | null
  releaseVolumeRatio: CurrentOnlyDistributionVM | null
}

export function parseMomentumObservation(raw: unknown): MomentumObservationVM {
  const o = asRecord(raw) ?? {}
  return {
    squeeze: parseSqueezeState(o['squeeze_state']),
    bbPosition: parseCurrentOnlyMomentumDistribution(o['bb_position']),
    bbWidth: parseCurrentOnlyMomentumDistribution(o['bb_width']),
    releaseVolumeRatio: parseCurrentOnlyMomentumDistribution(o['release_volume_ratio']),
  }
}

// ---------------------------------------------------------------------------
// G8 volume_anomaly — participation distributions (NO status, NO denominator)
// ---------------------------------------------------------------------------

export interface VolumeDistributionVM {
  /** true when valid_count === 0 (all p-values null) */
  unavailable: boolean
  p25: number | null
  p50: number | null
  p75: number | null
  validCount: number | null
}

export function parseVolumeDistribution(raw: unknown): VolumeDistributionVM | null {
  const o = asRecord(raw)
  if (!o) return null
  const p25 = isFiniteNumber(o['p25']) ? (o['p25'] as number) : null
  const p50 = isFiniteNumber(o['p50']) ? (o['p50'] as number) : null
  const p75 = isFiniteNumber(o['p75']) ? (o['p75'] as number) : null
  const validCountRaw = o['valid_count']
  const validCount = typeof validCountRaw === 'number' && Number.isFinite(validCountRaw) ? Math.floor(validCountRaw) : 0
  return {
    unavailable: validCount === 0,
    p25,
    p50,
    p75,
    validCount,
  }
}

export interface VolumeObservationVM {
  ratio20: VolumeDistributionVM | null
  ratio200: VolumeDistributionVM | null
  percentile20: VolumeDistributionVM | null
  percentile200: VolumeDistributionVM | null
  zscore20: VolumeDistributionVM | null
  zscore200: VolumeDistributionVM | null
}

export function parseVolumeObservation(raw: unknown): VolumeObservationVM {
  const o = asRecord(raw) ?? {}
  return {
    ratio20: parseVolumeDistribution(o['volume_ratio20']),
    ratio200: parseVolumeDistribution(o['volume_ratio200']),
    percentile20: parseVolumeDistribution(o['volume_percentile20']),
    percentile200: parseVolumeDistribution(o['volume_percentile200']),
    zscore20: parseVolumeDistribution(o['volume_zscore20']),
    zscore200: parseVolumeDistribution(o['volume_zscore200']),
  }
}

// Canonical L1 participation.volume 使用无前缀键（ratio20 / percentile20 / ...），
// 与 L2 volume_anomaly 组的 volume_ratio20 前缀不同。
export function parseParticipationVolume(raw: unknown): VolumeObservationVM {
  const o = asRecord(raw) ?? {}
  return {
    ratio20: parseVolumeDistribution(o['ratio20']),
    ratio200: parseVolumeDistribution(o['ratio200']),
    percentile20: parseVolumeDistribution(o['percentile20']),
    percentile200: parseVolumeDistribution(o['percentile200']),
    zscore20: parseVolumeDistribution(o['zscore20']),
    zscore200: parseVolumeDistribution(o['zscore200']),
  }
}

// ---------------------------------------------------------------------------
// Display formatters (explicit, non-overlapping semantics)
// ---------------------------------------------------------------------------

export function fmtSqueezeRatio(ratio: number | null): string {
  // null when denominator === 0 -> NOT "0%"
  if (ratio === null) return NULL_DISPLAY
  return `${(ratio * 100).toFixed(1)}%`
}

export { formatMultipleNullable, formatRawDimensionlessNullable, formatPercentileNullable, formatZScoreNullable, formatNumberNullable, NULL_DISPLAY }

// Squeeze category canonical label (producer SQUEEZE/RELEASED/NORMAL -> display).
export function fmtSqueezeCategory(cat: SqueezeStateCategory): string {
  return SQUEEZE_LABELS_INV[cat] ?? cat
}
const SQUEEZE_LABELS_INV: Record<SqueezeStateCategory, string> = {
  Squeeze: 'Squeeze',
  Squeeze_Release: 'Squeeze Release',
  Non_Squeeze: 'Non Squeeze',
}

// ---------------------------------------------------------------------------
// Helpers (extended)
// ---------------------------------------------------------------------------

import type { ReviewScopeHistoryDTO } from './types'

function deepGet(root: unknown, path: readonly string[]): unknown {
  let node: unknown = root
  for (const key of path) {
    if (node === null || typeof node !== 'object' || !(key in (node as Record<string, unknown>))) {
      return undefined
    }
    node = (node as Record<string, unknown>)[key]
  }
  return node
}

function numOrZero(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0
}

function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** 比率 -> 百分比显示；null 保持 "—"（绝不 0%）。 */
export function fmtRatioPct(ratio: number | null): string {
  if (ratio === null) return NULL_DISPLAY
  return `${(ratio * 100).toFixed(1)}%`
}

// ---------------------------------------------------------------------------
// A. Momentum State（Expanding / Flat / Contracting）
// ---------------------------------------------------------------------------

export type MomentumStateCategory = 'Expanding' | 'Flat' | 'Contracting'

export interface MomentumStateCategoryVM {
  category: MomentumStateCategory
  count: number
  /** null when denominator === 0（NOT 0%） */
  ratio: number | null
}

export interface MomentumStateVM {
  denominator: number | null
  unavailable: boolean
  categories: MomentumStateCategoryVM[]
}

export function parseMomentumState(raw: unknown): MomentumStateVM | null {
  const o = asRecord(raw)
  if (!o) return null
  const denomRaw = o['denominator']
  const denominator = typeof denomRaw === 'number' && Number.isFinite(denomRaw) && denomRaw >= 0
    ? Math.floor(denomRaw)
    : null
  const defs: { key: string; cat: MomentumStateCategory }[] = [
    { key: 'expanding', cat: 'Expanding' },
    { key: 'flat', cat: 'Flat' },
    { key: 'contracting', cat: 'Contracting' },
  ]
  const categories = defs.map(({ key, cat }) => ({
    category: cat,
    count: numOrZero(o[`${key}_count`]),
    ratio: isFiniteNumber(o[`${key}_ratio`]) ? (o[`${key}_ratio`] as number) : null,
  }))
  return {
    denominator,
    unavailable: denominator === null || denominator === 0,
    categories,
  }
}

// ---------------------------------------------------------------------------
// B. Momentum Change（enhancing / weakening / flat）
// ---------------------------------------------------------------------------

export type MomentumChangeCategory = 'Enhancing' | 'Flat' | 'Weakening'

export interface MomentumChangeCategoryVM {
  category: MomentumChangeCategory
  count: number
  /** 纯展示派生（count / denominator），非 canonical 指标；denominator=0 时为 null */
  ratio: number | null
}

export interface MomentumChangeVM {
  enhancingCount: number
  weakeningCount: number
  flatCount: number
  /** Board parity：missing/unrecognized momentum_change 已计入 flat，不得重定义 */
  denominator: number | null
  /** 展示用类别（Panel 只 render，不自行做 n / denominator） */
  categories: MomentumChangeCategoryVM[]
}

export function parseMomentumChange(raw: unknown): MomentumChangeVM | null {
  const o = asRecord(raw)
  if (!o) return null
  const denominator = numOrNull(o['denominator'])
  const counts: { category: MomentumChangeCategory; key: string }[] = [
    { category: 'Enhancing', key: 'enhancing_count' },
    { category: 'Flat', key: 'flat_count' },
    { category: 'Weakening', key: 'weakening_count' },
  ]
  const denom = denominator != null && denominator > 0 ? denominator : null
  return {
    enhancingCount: numOrZero(o['enhancing_count']),
    weakeningCount: numOrZero(o['weakening_count']),
    flatCount: numOrZero(o['flat_count']),
    denominator,
    categories: counts.map(({ category, key }) => {
      const count = numOrZero(o[key])
      return { category, count, ratio: denom == null ? null : count / denom }
    }),
  }
}

// ---------------------------------------------------------------------------
// E. SqzMom（mean + valid_count）
// ---------------------------------------------------------------------------

export interface SqzmomVM {
  mean: number | null
  validCount: number | null
}

export function parseSqzmom(raw: unknown): SqzmomVM | null {
  const o = asRecord(raw)
  if (!o) return null
  return {
    mean: numOrNull(o['mean']),
    validCount: numOrNull(o['valid_count']),
  }
}

// ---------------------------------------------------------------------------
// F. Momentum × Volume Relation（OPEN categorical，verbatim，不建固定 enum）
// ---------------------------------------------------------------------------

export interface RelationCategoryVM {
  category: string
  count: number
  /** null when denominator === 0 */
  ratio: number | null
}

export interface MomentumVolumeRelationVM {
  denominator: number | null
  unavailable: boolean
  categories: RelationCategoryVM[]
}

export function parseMomentumVolumeRelation(raw: unknown): MomentumVolumeRelationVM | null {
  const o = asRecord(raw)
  if (!o) return null
  if (o['status'] === 'unavailable') {
    return { denominator: 0, unavailable: true, categories: [] }
  }
  const denominator = numOrNull(o['denominator'])
  const categories: RelationCategoryVM[] = []
  for (const key of Object.keys(o)) {
    if (!key.endsWith('_count')) continue
    const category = key.slice(0, -'_count'.length)
    categories.push({
      category,
      count: numOrZero(o[key]),
      ratio: isFiniteNumber(o[`${category}_ratio`]) ? (o[`${category}_ratio`] as number) : null,
    })
  }
  // 稳定排序，未知 category 也保留（不丢）
  categories.sort((a, b) => a.category.localeCompare(b.category))
  return {
    denominator,
    unavailable: denominator === null || denominator === 0,
    categories,
  }
}

// ---------------------------------------------------------------------------
// H. Volume Badge（high / low / normal / unknown）
// ---------------------------------------------------------------------------

export type VolumeBadgeCategory = 'High' | 'Normal' | 'Low' | 'Unknown'

export interface VolumeBadgeCategoryVM {
  category: VolumeBadgeCategory
  count: number
  /** 纯展示派生（count / total），非 canonical 指标；total=0 时为 null */
  ratio: number | null
}

export interface VolumeBadgeVM {
  highCount: number
  lowCount: number
  normalCount: number
  unknownCount: number
  total: number | null
  /** 展示用类别（Panel 只 render，不自行做 count / total） */
  entries: VolumeBadgeCategoryVM[]
}

export function parseVolumeBadge(raw: unknown): VolumeBadgeVM | null {
  const o = asRecord(raw)
  if (!o) return null
  const high = numOrZero(o['high_count'])
  const low = numOrZero(o['low_count'])
  const normal = numOrZero(o['normal_count'])
  const unknown = numOrZero(o['unknown_count'])
  const total = high + low + normal + unknown
  const entries: VolumeBadgeCategoryVM[] = [
    { category: 'High', count: high, ratio: total > 0 ? high / total : null },
    { category: 'Normal', count: normal, ratio: total > 0 ? normal / total : null },
    { category: 'Low', count: low, ratio: total > 0 ? low / total : null },
    { category: 'Unknown', count: unknown, ratio: total > 0 ? unknown / total : null },
  ]
  return { highCount: high, lowCount: low, normalCount: normal, unknownCount: unknown, total, entries }
}

// ---------------------------------------------------------------------------
// I. Percentile histogram（canonical 5 bins）
// ---------------------------------------------------------------------------

export interface VolumeHistogramBin {
  label: string
  count: number
}

export interface VolumeHistogramVM {
  bins: VolumeHistogramBin[]
}

const HIST_BINS: ReadonlyArray<readonly [string, string]> = [
  ['lt20', '0–20'],
  ['20_40', '20–40'],
  ['40_60', '40–60'],
  ['60_80', '60–80'],
  ['gte80', '80–100'],
]

export function parseVolumeHistogram(raw: unknown): VolumeHistogramVM | null {
  const o = asRecord(raw)
  if (!o) return null
  return { bins: HIST_BINS.map(([k, label]) => ({ label, count: numOrZero(o[k]) })) }
}

// ---------------------------------------------------------------------------
// Combined VM（detail momentum tab 唯一解析 owner）
// ---------------------------------------------------------------------------

export interface MomentumVolumeVM {
  state: MomentumStateVM | null
  change: MomentumChangeVM | null
  squeeze: SqueezeStateVM | null
  bbPosition: CurrentOnlyDistributionVM | null
  bbWidth: CurrentOnlyDistributionVM | null
  releaseVolumeRatio: CurrentOnlyDistributionVM | null
  sqzmom: SqzmomVM | null
  relation: MomentumVolumeRelationVM | null
  volume: VolumeObservationVM | null
  volumeBadge: VolumeBadgeVM | null
  ratio20Mean: number | null
  ratio200Mean: number | null
  percentile20Histogram: VolumeHistogramVM | null
  percentile200Histogram: VolumeHistogramVM | null
}

export function parseMomentumVolumeObservation(observation: Record<string, unknown> | null | undefined): MomentumVolumeVM {
  const momentum = asRecord(deepGet(observation, ['momentum']))
  const participation = asRecord(deepGet(observation, ['participation']))
  const vol = asRecord(participation ? participation['volume'] : null)
  return {
    state: momentum ? parseMomentumState(momentum['state']) : null,
    change: momentum ? parseMomentumChange(momentum['change']) : null,
    squeeze: momentum ? parseSqueezeState(momentum['squeeze_state']) : null,
    bbPosition: momentum ? parseCurrentOnlyMomentumDistribution(momentum['bb_position']) : null,
    bbWidth: momentum ? parseCurrentOnlyMomentumDistribution(momentum['bb_width']) : null,
    releaseVolumeRatio: momentum ? parseCurrentOnlyMomentumDistribution(momentum['release_volume_ratio']) : null,
    sqzmom: momentum ? parseSqzmom(momentum['sqzmom']) : null,
    relation: momentum ? parseMomentumVolumeRelation(momentum['momentum_volume_relation']) : null,
    volume: parseParticipationVolume(vol),
    volumeBadge: parseVolumeBadge(vol ? vol['badge'] : null),
    ratio20Mean: vol ? numOrNull(vol['ratio20_mean']) : null,
    ratio200Mean: vol ? numOrNull(vol['ratio200_mean']) : null,
    percentile20Histogram: parseVolumeHistogram(vol ? vol['percentile20_histogram'] : null),
    percentile200Histogram: parseVolumeHistogram(vol ? vol['percentile200_histogram'] : null),
  }
}

// ---------------------------------------------------------------------------
// History（history.momentumVolume direct projection）
// ---------------------------------------------------------------------------

export interface MomentumStateHistoryEntry {
  date: string
  vm: MomentumStateVM | null
}
export interface MomentumChangeHistoryEntry {
  date: string
  vm: MomentumChangeVM | null
}
export interface SqueezeStateHistoryEntry {
  date: string
  vm: SqueezeStateVM | null
}
export interface CurrentOnlyHistoryEntry {
  date: string
  vm: CurrentOnlyDistributionVM | null
}
export interface VolumeHistoryEntry {
  date: string
  vm: VolumeDistributionVM | null
}
export interface MomentumVolumeRelationHistoryEntry {
  date: string
  vm: MomentumVolumeRelationVM | null
}
export interface SqzmomHistoryEntry {
  date: string
  mean: number | null
}

export interface MomentumVolumeHistoryVM {
  dates: string[]
  momentumState: MomentumStateHistoryEntry[]
  momentumChange: MomentumChangeHistoryEntry[]
  squeezeState: SqueezeStateHistoryEntry[]
  releaseVolumeRatio: CurrentOnlyHistoryEntry[]
  relation: MomentumVolumeRelationHistoryEntry[]
  volumePercentile20: VolumeHistoryEntry[]
  volumePercentile200: VolumeHistoryEntry[]
  sqzmomMean: SqzmomHistoryEntry[]
}

function at<T>(arr: T[] | undefined, i: number): T | null {
  return arr && i < arr.length ? arr[i] : null
}

export function parseMomentumVolumeHistory(history: ReviewScopeHistoryDTO | null | undefined): MomentumVolumeHistoryVM {
  if (!history || typeof history !== 'object') {
    return {
      dates: [],
      momentumState: [],
      momentumChange: [],
      squeezeState: [],
      releaseVolumeRatio: [],
      relation: [],
      volumePercentile20: [],
      volumePercentile200: [],
      sqzmomMean: [],
    }
  }
  const dates = Array.isArray(history.dates) ? history.dates : []
  const mv = history.momentumVolume
  return {
    dates,
    momentumState: dates.map((d, i) => ({ date: d, vm: parseMomentumState(at(mv?.momentum_state, i)) })),
    momentumChange: dates.map((d, i) => ({ date: d, vm: parseMomentumChange(at(mv?.momentum_change, i)) })),
    squeezeState: dates.map((d, i) => ({ date: d, vm: parseSqueezeState(at(mv?.squeeze_state, i)) })),
    releaseVolumeRatio: dates.map((d, i) => ({ date: d, vm: parseCurrentOnlyMomentumDistribution(at(mv?.release_volume_ratio, i)) })),
    relation: dates.map((d, i) => ({ date: d, vm: parseMomentumVolumeRelation(at(mv?.momentum_volume_relation, i)) })),
    volumePercentile20: dates.map((d, i) => ({ date: d, vm: parseVolumeDistribution(at(mv?.volume_percentile20, i)) })),
    volumePercentile200: dates.map((d, i) => ({ date: d, vm: parseVolumeDistribution(at(mv?.volume_percentile200, i)) })),
    sqzmomMean: dates.map((d, i) => ({ date: d, mean: at(mv?.sqzmom_mean, i) })),
  }
}
