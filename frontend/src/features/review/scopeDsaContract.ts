// [R3C-DSA] DSA research-page canonical Observation contract owner (pure TS).
//
// Single deterministic parser for the DSA (趋势与结构研究) page. It consumes the
// canonical L1 Observation payload produced by `scope_observation.compute_scope_
// observation` and returns typed facts + ViewModel strings. It does NOT recompute,
// score, or re-derive; it ONLY reads the canonical keys and formats them with the
// shared numeric-scale contract owned by `scopePriceTrendContract` / `reviewFormat`.
//
// NUMERIC SCALE CONTRACT (P0 — no x100 errors, same as R3C):
//   - regime_strength: unitless scalar -> raw number (formatNumberNullable).
//   - dsa_vwap_dev_pct: ALREADY percentage points -> NO x100
//     (formatPercentagePointsNullable).
//   - segment_change_pct: ALREADY percentage points -> NO x100.
//   - segment_slope: %/bar -> NO x100 (formatPctPerBarNullable).
//   - segment_volume/amount_mean_ratio: dimensionless multiple -> NO x100
//     (formatMultipleNullable).
//   - trend.state up/neutral/down ratios: stored ratios -> *100
//     (formatPercentNullable). The Sideways state maps to `neutral_ratio`
//     (NOT `range_ratio` — that key never exists in the canonical payload).
//
// The component (ScopeDsaPanel) must NOT deepGet the raw payload or re-implement
// any formatter; it consumes this VM only.
import {
  formatPercentagePointsNullable,
  formatPctPerBarNullable,
  formatMultipleNullable,
} from './scopePriceTrendContract'
import {
  formatPercentNullable,
  formatNumberNullable,
  NULL_DISPLAY,
} from './reviewFormat'

type Json = Record<string, unknown>

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function deepGet(root: unknown, path: readonly string[]): unknown {
  let node: unknown = root
  for (const key of path) {
    if (node === null || typeof node !== 'object' || !(key in (node as Json))) {
      return undefined
    }
    node = (node as Json)[key]
  }
  return node
}

// ---------------------------------------------------------------------------
// Facts (raw canonical reads)
// ---------------------------------------------------------------------------

export interface DsaTrendFacts {
  regimeStrength: number | null
  dsaVwapDevPct: number | null
  segmentBars: number | null
  segmentChangePct: number | null
  segmentSlope: number | null
  segVolRatio: number | null
  segAmtRatio: number | null
}

export interface DsaStateFacts {
  upRatio: number | null
  neutralRatio: number | null
  downRatio: number | null
  denominator: number | null
}

export interface DsaDistributionBucket {
  label: string
  count: number
  ratio: number | null
}

export interface DsaDistributionFacts {
  p25: number | null
  p50: number | null
  p75: number | null
  mean: number | null
  validCount: number | null
  buckets: DsaDistributionBucket[] | null
}

export interface DsaTransitionFact {
  key: string
  ratio: number | null
  count: number | null
}

export interface DsaChangedMember {
  memberId: string
  previousState: string
  currentState: string
}

export interface DsaObservationFacts {
  trend: DsaTrendFacts | null
  state: DsaStateFacts | null
  trendStrengthDist: DsaDistributionFacts | null
  dsaVwapDevDist: DsaDistributionFacts | null
  dsaDirBarsDist: DsaDistributionFacts | null
  transitionDenominator: number | null
  transitions: DsaTransitionFact[]
  changedMembers: DsaChangedMember[]
}

// ---------------------------------------------------------------------------
// Parser (canonical keys only)
// ---------------------------------------------------------------------------

function parseTrend(observation: Json | undefined): DsaTrendFacts | null {
  const continuous = deepGet(observation, ['trend', 'continuous'])
  if (!continuous || typeof continuous !== 'object') return null
  const c = continuous as Json
  const num = (k: string): number | null =>
    isFiniteNumber(c[k]) ? (c[k] as number) : null
  return {
    regimeStrength: num('regime_strength'),
    dsaVwapDevPct: num('dsa_vwap_dev_pct'),
    segmentBars: num('segment_bars'),
    segmentChangePct: num('segment_change_pct'),
    segmentSlope: num('segment_slope'),
    segVolRatio: num('segment_volume_mean_ratio'),
    segAmtRatio: num('segment_amount_mean_ratio'),
  }
}

function parseState(observation: Json | undefined): DsaStateFacts | null {
  const state = deepGet(observation, ['trend', 'state'])
  if (!state || typeof state !== 'object') return null
  const s = state as Json
  // Sideways -> Neutral. Canonical producer emits up/neutral/down_ratio.
  // `range_ratio` does NOT exist; reading it is a P1 bug (never used here).
  const num = (k: string): number | null =>
    isFiniteNumber(s[k]) ? (s[k] as number) : null
  return {
    upRatio: num('up_ratio'),
    neutralRatio: num('neutral_ratio'),
    downRatio: num('down_ratio'),
    denominator: num('denominator'),
  }
}

function parseDistribution(observation: Json | undefined, path: readonly string[]): DsaDistributionFacts | null {
  const node = deepGet(observation, path)
  if (!node || typeof node !== 'object') return null
  const d = node as Json
  const num = (k: string): number | null =>
    isFiniteNumber(d[k]) ? (d[k] as number) : null
  let buckets: DsaDistributionBucket[] | null = null
  if (Array.isArray(d['buckets'])) {
    buckets = (d['buckets'] as Json[]).map((b) => ({
      label: typeof b['label'] === 'string' ? (b['label'] as string) : '',
      count: isFiniteNumber(b['count']) ? (b['count'] as number) : 0,
      ratio: isFiniteNumber(b['ratio']) ? (b['ratio'] as number) : null,
    }))
  }
  // 兼容：无 buckets 字段的分布返回 null（UI 仅 percentile 展示）
  return {
    p25: num('p25'),
    p50: num('p50'),
    p75: num('p75'),
    mean: num('mean'),
    validCount: num('valid_count'),
    buckets,
  }
}

interface ParsedTransition {
  denominator: number | null
  transitions: DsaTransitionFact[]
  changedMembers: DsaChangedMember[]
}

function parseTransition(observation: Json | undefined): ParsedTransition {
  const node = deepGet(observation, ['trend', 'transition'])
  if (!node || typeof node !== 'object') {
    return { denominator: null, transitions: [], changedMembers: [] }
  }
  const t = node as Json
  const denominator = isFiniteNumber(t['denominator']) ? (t['denominator'] as number) : null
  const transitions: DsaTransitionFact[] = []
  for (const [key, value] of Object.entries(t)) {
    if (key === 'denominator' || key === 'changed_members') continue
    if (!value || typeof value !== 'object') continue
    const item = value as Json
    transitions.push({
      key,
      ratio: isFiniteNumber(item['ratio']) ? (item['ratio'] as number) : null,
      count: isFiniteNumber(item['count']) ? (item['count'] as number) : null,
    })
  }
  // 按 ratio 降序，仅展示真实发生的迁移（canonical transition 是 sparse，无 key=0）
  transitions.sort((a, b) => (b.ratio ?? -1) - (a.ratio ?? -1))
  const rawMembers = Array.isArray(t['changed_members']) ? (t['changed_members'] as Json[]) : []
  const changedMembers: DsaChangedMember[] = rawMembers.map((m) => ({
    memberId:
      typeof m['member_id'] === 'string'
        ? (m['member_id'] as string)
        : String(m['member_id'] ?? ''),
    previousState: typeof m['previous_state'] === 'string' ? (m['previous_state'] as string) : '',
    currentState: typeof m['current_state'] === 'string' ? (m['current_state'] as string) : '',
  }))
  return { denominator, transitions, changedMembers }
}

export function parseDsaObservation(observation: Json | null | undefined): DsaObservationFacts {
  if (!observation || typeof observation !== 'object') {
    return {
      trend: null,
      state: null,
      trendStrengthDist: null,
      dsaVwapDevDist: null,
      dsaDirBarsDist: null,
      transitionDenominator: null,
      transitions: [],
      changedMembers: [],
    }
  }
  const tr = parseTransition(observation)
  return {
    trend: parseTrend(observation),
    state: parseState(observation),
    trendStrengthDist: parseDistribution(observation, ['trend', 'trend_strength_distribution']),
    dsaVwapDevDist: parseDistribution(observation, ['trend', 'dsa_vwap_dev_pct_distribution']),
    dsaDirBarsDist: parseDistribution(observation, ['trend', 'dsa_dir_bars_distribution']),
    transitionDenominator: tr.denominator,
    transitions: tr.transitions,
    changedMembers: tr.changedMembers,
  }
}

// ---------------------------------------------------------------------------
// ViewModel strings (format with the shared numeric-scale contract)
// ---------------------------------------------------------------------------

export interface DsaBucketVM {
  label: string
  count: number
  ratio: string
}

export interface DsaVM {
  regimeStrength: string
  dsaVwapDevPct: string
  segmentBars: string
  segmentChangePct: string
  segmentSlope: string
  segVolRatio: string
  segAmtRatio: string
  upRatio: string
  neutralRatio: string
  downRatio: string
  trendStrengthDist: string
  dsaVwapDevDist: string
  dsaDirBarsDist: string
  dsaDirBarsBuckets: DsaBucketVM[]
  transitionDenominator: number | null
  transitions: Array<{ key: string; ratio: string }>
  changedMembers: DsaChangedMember[]
}

export function buildDsaVM(facts: DsaObservationFacts): DsaVM {
  const t = facts.trend
  const s = facts.state
  const fmtDist = (d: DsaDistributionFacts | null): string => {
    if (!d) return NULL_DISPLAY
    if (d.p50 == null) return NULL_DISPLAY
    const parts: string[] = []
    if (d.p25 != null) parts.push(`P25 ${d.p25.toFixed(2)}`)
    parts.push(`P50 ${d.p50.toFixed(2)}`)
    if (d.p75 != null) parts.push(`P75 ${d.p75.toFixed(2)}`)
    return parts.join(' · ')
  }
  const fmtBuckets = (d: DsaDistributionFacts | null): DsaBucketVM[] => {
    if (!d?.buckets) return []
    return d.buckets.map((b) => ({
      label: b.label,
      count: b.count,
      ratio: b.ratio == null ? NULL_DISPLAY : formatPercentNullable(b.ratio, 1),
    }))
  }
  return {
    // unitless / percentage-points / per-bar / multiple — explicit, no x100 surprise
    regimeStrength: t?.regimeStrength == null ? NULL_DISPLAY : formatNumberNullable(t.regimeStrength, 2),
    dsaVwapDevPct: formatPercentagePointsNullable(t?.dsaVwapDevPct),
    segmentBars: t?.segmentBars == null ? NULL_DISPLAY : formatNumberNullable(t.segmentBars, 1),
    segmentChangePct: formatPercentagePointsNullable(t?.segmentChangePct),
    segmentSlope: formatPctPerBarNullable(t?.segmentSlope),
    segVolRatio: formatMultipleNullable(t?.segVolRatio),
    segAmtRatio: formatMultipleNullable(t?.segAmtRatio),
    // ratios -> *100 (2dp locked by R3C scale contract)
    upRatio: formatPercentNullable(s?.upRatio, 2),
    neutralRatio: formatPercentNullable(s?.neutralRatio, 2),
    downRatio: formatPercentNullable(s?.downRatio, 2),
    trendStrengthDist: fmtDist(facts.trendStrengthDist),
    dsaVwapDevDist: fmtDist(facts.dsaVwapDevDist),
    dsaDirBarsDist: fmtDist(facts.dsaDirBarsDist),
    dsaDirBarsBuckets: fmtBuckets(facts.dsaDirBarsDist),
    transitionDenominator: facts.transitionDenominator,
    transitions: facts.transitions.map((tr) => ({
      key: tr.key,
      ratio: formatPercentNullable(tr.ratio, 2),
    })),
    changedMembers: facts.changedMembers,
  }
}

// ---------------------------------------------------------------------------
// Sparkline gap helper (P1-3): split a series into segments at null so the
// chart never connects across a missing slot. Pure + unit-testable.
// ---------------------------------------------------------------------------

export interface SparkSegment {
  i: number
  v: number
}

export function splitSeriesByGap(series: Array<number | null>): SparkSegment[][] {
  const segments: SparkSegment[][] = []
  let cur: SparkSegment[] = []
  for (let i = 0; i < series.length; i++) {
    const v = series[i]
    if (v == null) {
      if (cur.length > 0) {
        segments.push(cur)
        cur = []
      }
      continue
    }
    cur.push({ i, v })
  }
  if (cur.length > 0) segments.push(cur)
  return segments
}
