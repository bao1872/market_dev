// [R3E] Focused contract tests for G7 (momentum) + G8 (volume) parsers.
// High-value only: lock the P0 paths that would create a false user fact if
// broken. No synthetic malformed floods (defense handled by R3D raw-before-
// normalization lesson; here we lock the canonical production contract).

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  parseSqueezeState,
  parseCurrentOnlyMomentumDistribution,
  parseMomentumObservation,
  parseVolumeDistribution,
  parseVolumeObservation,
  fmtSqueezeRatio,
} from '../scopeMomentumVolumeContract'

// ---------------------------------------------------------------------------
// G7 — squeeze_state
// ---------------------------------------------------------------------------

// 1. Squeeze denominator=0 -> unavailable, NOT 0% / 0% / 0%
test('G7 squeeze denominator=0 -> unavailable, ratios null (NOT 0%)', () => {
  const vm = parseSqueezeState({
    squeeze_count: 0,
    squeeze_ratio: null,
    squeeze_release_count: 0,
    squeeze_release_ratio: null,
    non_squeeze_count: 0,
    non_squeeze_ratio: null,
    denominator: 0,
  })
  assert.equal(vm !== null, true)
  assert.equal(vm!.denominator, 0)
  assert.equal(vm!.unavailable, true)
  for (const c of vm!.categories) {
    assert.equal(c.ratio, null)
    assert.equal(c.count, 0)
  }
})

// Squeeze ratios displayed as %; null stays "—"
test('G7 squeeze ratio formatting: null stays —, value -> %', () => {
  assert.equal(fmtSqueezeRatio(null), '—')
  assert.equal(fmtSqueezeRatio(0.25), '25.0%')
})

// ---------------------------------------------------------------------------
// G7 — current-only distributions (bb_position / bb_width / release_volume_ratio)
// ---------------------------------------------------------------------------

// 2. BB Position no clamp: 1.12 and -0.15 stay raw
test('G7 bb_position median 1.12 / -0.15 preserved (no clamp)', () => {
  const hi = parseCurrentOnlyMomentumDistribution({ median: 1.12, p25: 1.0, p75: 1.25, valid_count: 50, denominator: 50 })
  assert.equal(hi!.median, 1.12)
  assert.equal(hi!.unavailable, false)
  const lo = parseCurrentOnlyMomentumDistribution({ median: -0.15, p25: -0.3, p75: 0.0, valid_count: 50, denominator: 50 })
  assert.equal(lo!.median, -0.15)
})

// 3. BB Width no x100: 0.0832 stays raw dimensionless
test('G7 bb_width 0.0832 stays raw (NOT 8.32%)', () => {
  const vm = parseCurrentOnlyMomentumDistribution({ median: 0.0832, p25: 0.05, p75: 0.12, valid_count: 50, denominator: 50 })
  assert.equal(vm!.median, 0.0832)
})

// 4. Release Volume Ratio -> 1.50× (multiple, no direction color)
test('G7 release_volume_ratio 1.5 -> 1.50×', () => {
  const vm = parseCurrentOnlyMomentumDistribution({ median: 1.5, p25: 1.2, p75: 1.9, valid_count: 50, denominator: 50 })
  assert.equal(vm!.median, 1.5)
  assert.equal(vm!.unavailable, false)
})

// 5. Current-only unavailable preserves reason
test('G7 current-only unavailable preserves reason + valid_count=0', () => {
  const vm = parseCurrentOnlyMomentumDistribution({
    status: 'unavailable',
    reason: 'CURRENT_SOURCE_UNAVAILABLE_BB_POSITION',
    valid_count: 0,
  })
  assert.equal(vm!.unavailable, true)
  assert.equal(vm!.reason, 'CURRENT_SOURCE_UNAVAILABLE_BB_POSITION')
  assert.equal(vm!.median, null)
  assert.equal(vm!.denominator, null)
})

// ---------------------------------------------------------------------------
// G8 — volume_anomaly participation distributions (NO status / NO denominator)
// ---------------------------------------------------------------------------

// 6. Volume Ratio p50=1.25 -> 1.25×
test('G8 ratio p50=1.25 -> 1.25×', () => {
  const vm = parseVolumeDistribution({ p25: 0.9, p50: 1.25, p75: 1.6, valid_count: 40 })
  assert.equal(vm!.p50, 1.25)
  assert.equal(vm!.unavailable, false)
})

// 7. Percentile p50=72.5 -> 72.5 (NOT x100)
test('G8 percentile p50=72.5 -> 72.5 (NOT 7250%)', () => {
  const vm = parseVolumeDistribution({ p25: 60, p50: 72.5, p75: 85, valid_count: 40 })
  assert.equal(vm!.p50, 72.5)
})

// 8. Z-score p50=-1.35 -> -1.35 (no percent / direction color)
test('G8 zscore p50=-1.35 -> -1.35', () => {
  const vm = parseVolumeDistribution({ p25: -2.0, p50: -1.35, p75: -0.2, valid_count: 40 })
  assert.equal(vm!.p50, -1.35)
})

// 9. 200D unavailable (valid_count=0) -> unavailable, 20D still ready
test('G8 200D unavailable valid_count=0, 20D displays', () => {
  const vm = parseVolumeObservation({
    volume_ratio20: { p25: 0.9, p50: 1.25, p75: 1.6, valid_count: 40 },
    volume_ratio200: { p25: null, p50: null, p75: null, valid_count: 0 },
    volume_percentile20: { p25: 60, p50: 72.5, p75: 85, valid_count: 40 },
    volume_percentile200: { p25: null, p50: null, p75: null, valid_count: 0 },
    volume_zscore20: { p25: -2.0, p50: -1.35, p75: -0.2, valid_count: 40 },
    volume_zscore200: { p25: null, p50: null, p75: null, valid_count: 0 },
  })
  assert.equal(vm.ratio20!.unavailable, false)
  assert.equal(vm.ratio20!.p50, 1.25)
  assert.equal(vm.ratio200!.unavailable, true)
  assert.equal(vm.ratio200!.p50, null)
  assert.equal(vm.percentile20!.unavailable, false)
  assert.equal(vm.percentile200!.unavailable, true)
  assert.equal(vm.zscore20!.unavailable, false)
  assert.equal(vm.zscore200!.unavailable, true)
})

// 10. G8 parser has NO denominator field (backend _participation_distribution
//     returns {p25,p50,p75,valid_count} only — frontend must not invent one).
test('G8 volume distribution has no denominator field', () => {
  const vm = parseVolumeDistribution({ p25: 0.9, p50: 1.25, p75: 1.6, valid_count: 40 })
  assert.equal('denominator' in (vm as object), false)
})

// ---------------------------------------------------------------------------
// Momentum group parser wiring (G7 fact key)
// ---------------------------------------------------------------------------

test('G7 momentum group: parseMomentumObservation reads momentum_squeeze_release fact', () => {
  const vm = parseMomentumObservation({
    squeeze_state: {
      squeeze_count: 5,
      squeeze_ratio: 0.1,
      squeeze_release_count: 3,
      squeeze_release_ratio: 0.06,
      non_squeeze_count: 42,
      non_squeeze_ratio: 0.84,
      denominator: 50,
    },
    bb_position: { median: 0.5, p25: 0.2, p75: 0.8, valid_count: 50, denominator: 50 },
    bb_width: { median: 0.0832, p25: 0.05, p75: 0.12, valid_count: 50, denominator: 50 },
    release_volume_ratio: { median: 1.5, p25: 1.2, p75: 1.9, valid_count: 50, denominator: 50 },
  })
  assert.equal(vm.squeeze!.denominator, 50)
  assert.equal(vm.bbPosition!.median, 0.5)
  assert.equal(vm.bbWidth!.median, 0.0832)
  assert.equal(vm.releaseVolumeRatio!.median, 1.5)
})

// 11. P0-3 — Persisted squeeze ratios are read VERBATIM, never recomputed.
//     Backend already computed count/denominator; frontend MUST NOT do ratio/denom.
//     This test MUST FAIL against the pre-fix code (which did persisted/denominator).
test('G7 squeeze persisted ratios preserved verbatim (NO recomputation)', () => {
  const squeeze = parseSqueezeState({
    squeeze_count: 5,
    squeeze_ratio: 0.1,
    squeeze_release_count: 3,
    squeeze_release_ratio: 0.06,
    non_squeeze_count: 42,
    non_squeeze_ratio: 0.84,
    denominator: 50,
  })!
  const byCat = Object.fromEntries(squeeze.categories.map((c) => [c.category, c]))
  assert.equal(byCat['Squeeze'].ratio, 0.1) // -> 10.0%, NOT 0.10/50=0.002
  assert.equal(byCat['Squeeze_Release'].ratio, 0.06) // -> 6.0%
  assert.equal(byCat['Non_Squeeze'].ratio, 0.84) // -> 84.0%
  assert.equal(fmtSqueezeRatio(byCat['Squeeze'].ratio), '10.0%')
  assert.equal(fmtSqueezeRatio(byCat['Squeeze_Release'].ratio), '6.0%')
  assert.equal(fmtSqueezeRatio(byCat['Non_Squeeze'].ratio), '84.0%')
})

// 12. REAL L2 WIRING — prove the workspace formal dispatch recognizes the
//     canonical backend ObservationGroup shape and keys. This is the wiring
//     that the pre-fix renderer registration (momentum/participation) missed.
test('R3E real L2 wiring: canonical group_key + direct facts dispatch', () => {
  // The canonical backend L2 group key is momentum_squeeze_release (NOT momentum).
  const g7: { group_key: string; label: string; facts: Record<string, unknown> } = {
    group_key: 'momentum_squeeze_release',
    label: '动量与压缩释放',
    facts: {
      squeeze_state: {
        squeeze_count: 5,
        squeeze_ratio: 0.1,
        squeeze_release_count: 3,
        squeeze_release_ratio: 0.06,
        non_squeeze_count: 42,
        non_squeeze_ratio: 0.84,
        denominator: 50,
      },
      bb_position: { median: 0.5, p25: 0.2, p75: 0.8, valid_count: 50, denominator: 50 },
      bb_width: { median: 0.0832, p25: 0.05, p75: 0.12, valid_count: 50, denominator: 50 },
      release_volume_ratio: { median: 1.5, p25: 1.2, p75: 1.9, valid_count: 50, denominator: 50 },
    },
  }
  // group.facts is passed DIRECTLY (no nested momentum_squeeze_release wrapper).
  const g7vm = parseMomentumObservation(g7.facts)
  assert.equal(g7vm.squeeze!.denominator, 50)
  assert.equal(g7vm.squeeze!.categories.length, 3)
  assert.equal(g7vm.bbWidth!.median, 0.0832)

  // The canonical backend L2 group key is volume_anomaly (NOT participation).
  const g8: { group_key: string; label: string; facts: Record<string, unknown> } = {
    group_key: 'volume_anomaly',
    label: '量能异常',
    facts: {
      volume_ratio20: { p25: 0.9, p50: 1.25, p75: 1.6, valid_count: 40 },
      volume_ratio200: { p25: null, p50: null, p75: null, valid_count: 0 },
      volume_percentile20: { p25: 60, p50: 72.5, p75: 85, valid_count: 40 },
      volume_percentile200: { p25: null, p50: null, p75: null, valid_count: 0 },
      volume_zscore20: { p25: -2.0, p50: -1.35, p75: -0.2, valid_count: 40 },
      volume_zscore200: { p25: null, p50: null, p75: null, valid_count: 0 },
    },
  }
  const g8vm = parseVolumeObservation(g8.facts)
  assert.equal(g8vm.ratio20!.p50, 1.25)
  assert.equal(g8vm.ratio200!.unavailable, true)
  assert.equal(g8vm.percentile20!.p50, 72.5)
  assert.equal(g8vm.zscore20!.p50, -1.35)
})
