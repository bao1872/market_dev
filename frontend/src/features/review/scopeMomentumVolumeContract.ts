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
