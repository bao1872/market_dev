// [R3C] Price + Trend contract tests (pure TS owner).
// Locks the numeric-scale, availability, cross-group, and open-category contracts.
// Uses node:test + node:assert (consistent with other Review contract tests).
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import {
  formatPercentagePointsNullable,
  formatPctPerBarNullable,
  formatMultipleNullable,
  formatRawSumNullable,
  parsePriceCapital,
  buildPriceCapitalVM,
  parseTrendState,
  buildTrendStateVM,
  parseTrendProgress,
  buildTrendProgressVM,
  parseTrendVolumeConfirmation,
} from '../scopePriceTrendContract'
import { formatPercentNullable } from '../reviewFormat'

test('R3C formatters — numeric scale (P0: no x100 errors)', () => {
  // EW return 0.015 -> 1.50% (decimal ratio * 100, 2dp locked by R3C)
  assert.equal(formatPercentNullable(0.015, 2), '1.50%')
  // EW return 0 -> 0.00% (valid zero, not unavailable)
  assert.equal(formatPercentNullable(0, 2), '0.00%')
  assert.equal(formatPercentNullable(null), '—')

  // Segment Change 4.2 -> 4.20% (already percentage points, NO x100)
  assert.equal(formatPercentagePointsNullable(4.2), '4.20%')
  // DSA-VWAP 0.7 -> 0.70% (percentage points, NO x100)
  assert.equal(formatPercentagePointsNullable(0.7), '0.70%')
  // Segment Slope 0.35 -> 0.35%/bar (NO x100)
  assert.equal(formatPctPerBarNullable(0.35), '0.35%/bar')
  // Volume Ratio 1.15 -> 1.15× (NO x100)
  assert.equal(formatMultipleNullable(1.15), '1.15×')

  // Total Amount raw number, NO unit suffix, NO x100
  assert.equal(formatRawSumNullable(1234567.89), '1,234,567.89')

  // null numeric -> — (no fake zero)
  assert.equal(formatPercentagePointsNullable(null), '—')
  assert.equal(formatPctPerBarNullable(null), '—')
  assert.equal(formatMultipleNullable(null), '—')
  assert.equal(formatRawSumNullable(null), '—')
})

test('R3C G1 — Total Amount availability (no recomputation)', () => {
  const baseFacts = {
    equal_weight_return: 0.015,
    amount_weighted_return: null,
    total_volume: 1000000,
    total_amount: 0,
    price_hhi: { raw_hhi: 0.3, normalized_hhi: 0.6, member_count: 42, status: 'ready' },
    amount_hhi: { raw_hhi: 0.2, normalized_hhi: 0.5, member_count: 38, status: 'ready' },
  }

  // total_amount=0 + valid_count=0 -> unavailable (NOT observed zero)
  const obs0 = { price: { amount: { valid_count: 0 } } }
  const vm0 = buildPriceCapitalVM(parsePriceCapital(baseFacts, obs0))
  assert.ok(vm0)
  assert.match(vm0!.amountAvailabilityNote ?? '', /不可用/)
  assert.equal(vm0!.totalAmount, '—')

  // valid_count=0 + total_amount null (no fake zero either)
  const vmNull = buildPriceCapitalVM(
    parsePriceCapital({ ...baseFacts, total_amount: null }, obs0),
  )
  assert.equal(vmNull!.totalAmount, '—')

  // total_amount=0 + valid_count>0 -> valid zero (no unavailable note)
  const obs30 = { price: { amount: { valid_count: 30 } } }
  const vm30 = buildPriceCapitalVM(parsePriceCapital(baseFacts, obs30))
  assert.equal(vm30!.amountAvailabilityNote, null)
  assert.equal(vm30!.totalAmount, '0.00')

  // total_amount=123 + valid_count>0 -> valid raw number
  const vm123 = buildPriceCapitalVM(
    parsePriceCapital({ ...baseFacts, total_amount: 123.45 }, obs30),
  )
  assert.equal(vm123!.totalAmount, '123.45')
})

test('R3C G1 — EW/AW direction tone', () => {
  const vm = buildPriceCapitalVM(
    parsePriceCapital(
      { equal_weight_return: 0.015, amount_weighted_return: -0.02 },
      undefined,
    ),
  )
  assert.equal(vm!.equalWeightReturnTone, 'up')
  assert.equal(vm!.amountWeightedReturnTone, 'down')
})

test('R3C G1 — HHI status honored, not scored', () => {
  const vm = buildPriceCapitalVM(
    parsePriceCapital(
      { price_hhi: { raw_hhi: null, normalized_hhi: null, member_count: 0, status: 'insufficient' } },
      undefined,
    ),
  )
  assert.equal(vm!.priceHhi!.status, 'insufficient')
  assert.equal(vm!.priceHhi!.rawHhi, null)
})

test('R3C G2 — Trend direction denominator semantics', () => {
  const readyDir = {
    trend_direction_member_ratio: {
      up_count: 25,
      up_ratio: 0.6,
      neutral_count: 8,
      neutral_ratio: 0.2,
      down_count: 8,
      down_ratio: 0.2,
      denominator: 41,
    },
  }

  // denominator 0 -> unavailable, NOT three 0% bars
  const vmZero = buildTrendStateVM(
    parseTrendState({ trend_direction_member_ratio: { denominator: 0 } }),
  )
  assert.equal(vmZero!.denominatorZero, true)

  // denominator > 0 -> persisted ratios shown
  const vmReady = buildTrendStateVM(parseTrendState(readyDir))
  assert.equal(vmReady!.denominatorZero, false)
  assert.equal(vmReady!.direction!.upRatio, 0.6)
  assert.equal(vmReady!.direction!.downRatio, 0.2)

  // trend_strength unitless neutral; dsa_vwap_dev signed directional
  const vmSigned = buildTrendStateVM(
    parseTrendState({ ...readyDir, trend_strength: 0.7, dsa_vwap_dev_pct: 0.7 }),
  )
  assert.equal(vmSigned!.trendStrength, '0.7')
  // 0.7 percentage points -> 0.70%, NOT 70%
  assert.equal(vmSigned!.dsaVwapDevPct, '0.70%')
  assert.equal(vmSigned!.dsaVwapDevTone, 'up')

  // dsa_vwap_dev negative -> down tone (green)
  const vmNeg = buildTrendStateVM(parseTrendState({ dsa_vwap_dev_pct: -1.2 }))
  assert.equal(vmNeg!.dsaVwapDevTone, 'down')
})

test('R3C G3 — Trend progress numeric scale', () => {
  const g3 = {
    current_segment_bars: 14.5,
    segment_change_pct: 4.2,
    segment_slope: 0.35,
    segment_volume_mean_ratio: 1.15,
    segment_amount_mean_ratio: 0.9,
    vwap_ret_total: -2.1,
  }

  // locks scale: 4.2->4.20%, 0.35->0.35%/bar, 1.15->1.15×, -2.1->-2.10%
  const vm = buildTrendProgressVM(parseTrendProgress(g3))
  assert.equal(vm!.segmentChangePct, '4.20%')
  assert.equal(vm!.segmentSlope, '0.35%/bar')
  assert.equal(vm!.volumeRatio, '1.15×')
  assert.equal(vm!.amountRatio, '0.90×')
  assert.equal(vm!.vwapRetTotal, '-2.10%')

  // segment bars may be fractional (no forced integer)
  assert.equal(vm!.segmentBars, '14.5')

  // signed directional tones: change/slope/vwap
  assert.equal(vm!.segmentChangeTone, 'up')
  assert.equal(vm!.segmentSlopeTone, 'up')
  assert.equal(vm!.vwapRetTotalTone, 'down')

  // null segment fact -> — (not 0)
  const vmNull = buildTrendProgressVM(parseTrendProgress({ segment_change_pct: null }))
  assert.equal(vmNull!.segmentChangePct, '—')

  // zero segment fact -> valid zero
  const vmZero = buildTrendProgressVM(parseTrendProgress({ segment_change_pct: 0 }))
  assert.equal(vmZero!.segmentChangePct, '0.00%')
})

test('R3C G3/G4 — shared segment ratios (same L1 source)', () => {
  const shared = {
    segment_volume_mean_ratio: 1.15,
    segment_amount_mean_ratio: 0.9,
  }
  const g3 = buildTrendProgressVM(parseTrendProgress(shared))
  const g4 = parseTrendVolumeConfirmation(shared)
  assert.equal(g3!.volumeRatio, g4!.volumeRatio)
  assert.equal(g3!.amountRatio, g4!.amountRatio)
  assert.equal(g4!.volumeRatio, '1.15×')
  assert.equal(g4!.amountRatio, '0.90×')
})

test('R3C G4 — open categorical momentum/volume relation', () => {
  // preserves arbitrary upstream tokens verbatim (no hardcoded vocabulary)
  const g4 = parseTrendVolumeConfirmation({
    segment_volume_mean_ratio: 1.15,
    segment_amount_mean_ratio: 0.9,
    momentum_volume_relation: {
      foo_count: 12,
      foo_ratio: 0.4,
      bar_count: 18,
      bar_ratio: 0.6,
      denominator: 30,
    },
  })
  assert.ok(g4!.momentumRelation)
  const cats = g4!.momentumRelation!.categories.map((c) => c.category).sort()
  assert.deepEqual(cats, ['bar', 'foo'])
  const foo = g4!.momentumRelation!.categories.find((c) => c.category === 'foo')!
  assert.equal(foo.count, 12)
  assert.equal(foo.ratio, 0.4)
  assert.equal((foo.ratio! * 100).toFixed(0), '40')

  // unavailable shape -> reason preserved, no bullish/bearish
  const g4Unavail = parseTrendVolumeConfirmation({
    momentum_volume_relation: { status: 'unavailable', reason: 'no valid members', denominator: 0 },
  })
  assert.equal(g4Unavail!.momentumRelation!.status, 'unavailable')
  assert.equal(g4Unavail!.momentumRelation!.reason, 'no valid members')
  assert.equal(g4Unavail!.momentumRelation!.categories.length, 0)

  // does not depend on closed Review vocabulary (共振/背离/etc.)
  const g4Vocab = parseTrendVolumeConfirmation({
    momentum_volume_relation: {
      divergence_count: 5,
      divergence_ratio: 0.5,
      confirming_count: 5,
      confirming_ratio: 0.5,
      denominator: 10,
    },
  })
  const vocabCats = g4Vocab!.momentumRelation!.categories.map((c) => c.category)
  assert.ok(vocabCats.includes('divergence'))
  assert.ok(vocabCats.includes('confirming'))

  // C. foo pair + denominator>0 -> accepted
  const g4C = parseTrendVolumeConfirmation({
    momentum_volume_relation: { foo_count: 12, foo_ratio: 0.4, denominator: 30 },
  })
  assert.ok(g4C!.momentumRelation)
  assert.equal(g4C!.momentumRelation!.categories.length, 1)
  assert.equal(g4C!.momentumRelation!.categories[0].category, 'foo')
  assert.equal(g4C!.momentumRelation!.categories[0].count, 12)
  assert.equal(g4C!.momentumRelation!.categories[0].ratio, 0.4)

  // D. foo_count only -> fail closed (no ratio)
  const g4D = parseTrendVolumeConfirmation({
    momentum_volume_relation: { foo_count: 12, denominator: 30 },
  })
  assert.equal(g4D!.momentumRelation, null)

  // E. foo_ratio only -> fail closed (no count)
  const g4E = parseTrendVolumeConfirmation({
    momentum_volume_relation: { foo_ratio: 0.4, denominator: 30 },
  })
  assert.equal(g4E!.momentumRelation, null)

  // F. foo pair + missing denominator -> fail closed
  const g4F = parseTrendVolumeConfirmation({
    momentum_volume_relation: { foo_count: 12, foo_ratio: 0.4 },
  })
  assert.equal(g4F!.momentumRelation, null)

  // G. foo pair + denominator=0 -> fail closed (ready requires >0)
  const g4G = parseTrendVolumeConfirmation({
    momentum_volume_relation: { foo_count: 12, foo_ratio: 0.4, denominator: 0 },
  })
  assert.equal(g4G!.momentumRelation, null)

  // G2. foo pair + denominator negative -> fail closed
  const g4Gneg = parseTrendVolumeConfirmation({
    momentum_volume_relation: { foo_count: 12, foo_ratio: 0.4, denominator: -5 },
  })
  assert.equal(g4Gneg!.momentumRelation, null)

  // H. status=unavailable + denominator=0 -> accepted, reason preserved
  const g4H = parseTrendVolumeConfirmation({
    momentum_volume_relation: { status: 'unavailable', reason: 'no valid members', denominator: 0 },
  })
  assert.equal(g4H!.momentumRelation!.status, 'unavailable')
  assert.equal(g4H!.momentumRelation!.reason, 'no valid members')
  assert.equal(g4H!.momentumRelation!.categories.length, 0)

  // no recomputation: ratio stays persisted (not count/denominator derived)
  assert.equal(g4C!.momentumRelation!.categories[0].ratio, 0.4)
})
