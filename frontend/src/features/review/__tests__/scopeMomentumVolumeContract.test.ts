// [R3E] Focused contract tests for G7 (momentum) + G8 (volume) parsers.
// High-value only: lock the P0 paths that would create a false user fact if
// broken. No synthetic malformed floods (defense handled by R3D raw-before-
// normalization lesson; here we lock the canonical production contract).

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  parseSqueezeState,
  parseCurrentOnlyMomentumDistribution,
  parseMomentumObservation,
  parseVolumeDistribution,
  parseVolumeObservation,
  fmtSqueezeRatio,
  parseMomentumVolumeObservation,
  parseMomentumVolumeHistory,
  parseMomentumState,
  parseMomentumChange,
  parseMomentumVolumeRelation,
  parseVolumeBadge,
  fmtSqueezeCategory,
} from '../scopeMomentumVolumeContract'
import { splitSeriesByGap } from '../scopeDsaContract'
import type { ReviewScopeHistoryDTO, ReviewCrossSectionFieldDTO } from '../types'

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

// 12. Canonical L2-shaped facts feed parsers directly (parser-level only —
//     does NOT exercise workspace FORMAL_RENDERERS dispatch; that wiring is
//     locked separately in scopeCurrentObservationWorkspace.test.ts).
test('R3E canonical L2-shaped facts feed parsers directly', () => {
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

// ===========================================================================
// R3E-SLICE3 — Momentum + Volume 详情页 contract（新增字段锁）
// ===========================================================================

// 锁 1: Momentum State 精确消费 expanding / flat / contracting 键
test('R3E-SLICE3 Momentum State 精确消费 expanding/flat/contracting 键', () => {
  const vm = parseMomentumState({
    expanding_count: 6, expanding_ratio: 0.3,
    flat_count: 8, flat_ratio: 0.4,
    contracting_count: 6, contracting_ratio: 0.3,
    denominator: 20,
  })
  assert.equal(vm!.denominator, 20)
  assert.equal(vm!.unavailable, false)
  assert.deepEqual(vm!.categories.map((c) => c.category), ['Expanding', 'Flat', 'Contracting'])
  assert.deepEqual(vm!.categories.map((c) => c.count), [6, 8, 6])
  assert.deepEqual(vm!.categories.map((c) => c.ratio), [0.3, 0.4, 0.3])
  // denominator === 0 -> unavailable（不 0%）
  const zero = parseMomentumState({ expanding_count: 0, expanding_ratio: null, flat_count: 0, flat_ratio: null, contracting_count: 0, contracting_ratio: null, denominator: 0 })
  assert.equal(zero!.unavailable, true)
})

// 锁 2: Momentum Change 精确消费 enhancing / weakening / flat + denominator
test('R3E-SLICE3 Momentum Change 精确消费 enhancing/weakening/flat + denominator', () => {
  const vm = parseMomentumChange({ enhancing_count: 5, weakening_count: 3, flat_count: 12, denominator: 20 })
  assert.equal(vm!.enhancingCount, 5)
  assert.equal(vm!.weakeningCount, 3)
  assert.equal(vm!.flatCount, 12)
  // frontend 不得重定义 denominator（Board parity 下 missing 已计入 flat，由 producer 给出）
  assert.equal(vm!.denominator, 20)
})

// 锁 3: Squeeze State 精确键 Squeeze / Squeeze_Release / Non_Squeeze
test('R3E-SLICE3 Squeeze State 精确键 Squeeze/Squeeze_Release/Non_Squeeze', () => {
  const vm = parseSqueezeState({
    squeeze_count: 2, squeeze_ratio: 0.1,
    squeeze_release_count: 1, squeeze_release_ratio: 0.05,
    non_squeeze_count: 17, non_squeeze_ratio: 0.85,
    denominator: 20,
  })
  assert.deepEqual(vm!.categories.map((c) => c.category), ['Squeeze', 'Squeeze_Release', 'Non_Squeeze'])
  assert.deepEqual(vm!.categories.map((c) => fmtSqueezeCategory(c.category)), ['Squeeze', 'Squeeze Release', 'Non Squeeze'])
})

// 锁 4: BB Position/Width 不 x100；Release Ratio 为 raw multiple
test('R3E-SLICE3 BB Position/Width 不 x100；Release Ratio 为 raw multiple', () => {
  const bb = parseCurrentOnlyMomentumDistribution({ median: 0.5, p25: 0.2, p75: 0.8, valid_count: 20, denominator: 20 })
  assert.equal(bb!.median, 0.5) // 不 50
  const width = parseCurrentOnlyMomentumDistribution({ median: 0.0832, p25: 0.05, p75: 0.12, valid_count: 20, denominator: 20 })
  assert.equal(width!.median, 0.0832) // 不 8.32
  const rel = parseCurrentOnlyMomentumDistribution({ median: 1.42, p25: 1.1, p75: 1.9, valid_count: 20, denominator: 20 })
  assert.equal(rel!.median, 1.42)
})

// 锁 5: momentum_volume_relation OPEN categorical，未知类别原样保留（不建固定 enum）
test('R3E-SLICE3 momentum_volume_relation OPEN categorical 未知类别原样保留', () => {
  const vm = parseMomentumVolumeRelation({
    '共振_count': 9, '共振_ratio': 0.45,
    '背离_count': 4, '背离_ratio': 0.2,
    '缩量挤压_count': 2, '缩量挤压_ratio': 0.1,
    'unknown_cat_x_count': 1, 'unknown_cat_x_ratio': 0.05,
    denominator: 20,
  })
  assert.equal(vm!.unavailable, false)
  const cats = vm!.categories.map((c) => c.category)
  assert.ok(cats.includes('共振'))
  assert.ok(cats.includes('背离'))
  assert.ok(cats.includes('缩量挤压'))
  assert.ok(cats.includes('unknown_cat_x'))
  const unk = vm!.categories.find((c) => c.category === 'unknown_cat_x')!
  assert.equal(unk.count, 1)
  assert.equal(unk.ratio, 0.05)
})

// 锁 6: ratio / percentile / zscore 不混单位（typed VM 只含数值，单位只在 formatter 层）
test('R3E-SLICE3 volume 分布 VM 只含数值，不预置混单位', () => {
  const ratio = parseVolumeDistribution({ p25: 0.9, p50: 1.25, p75: 1.6, valid_count: 40 })
  const pct = parseVolumeDistribution({ p25: 60, p50: 72.5, p75: 85, valid_count: 40 })
  const z = parseVolumeDistribution({ p25: -2.0, p50: -1.35, p75: -0.2, valid_count: 40 })
  for (const d of [ratio, pct, z]) {
    for (const k of ['p25', 'p50', 'p75'] as const) {
      const v = d![k]
      assert.equal(typeof v, 'number')
      assert.equal(String(v).includes('%'), false)
      assert.equal(String(v).includes('×'), false)
    }
  }
})

// 锁 7: 只暴露 ratio mean，不发明 percentile / zscore mean
test('R3E-SLICE3 VolumeObservation 只暴露 ratio mean，不发明 percentile/zscore mean', () => {
  const observation = {
    participation: {
      volume: {
        ratio20: { p25: 0.9, p50: 1.25, p75: 1.6, valid_count: 40 },
        ratio200: { p25: 0.8, p50: 1.1, p75: 1.4, valid_count: 40 },
        percentile20: { p25: 60, p50: 72.5, p75: 85, valid_count: 40 },
        percentile200: { p25: 55, p50: 68, p75: 80, valid_count: 40 },
        zscore20: { p25: -2.0, p50: -1.35, p75: -0.2, valid_count: 40 },
        zscore200: { p25: -1.5, p50: -0.9, p75: 0.1, valid_count: 40 },
        ratio20_mean: 1.27,
        ratio200_mean: 1.12,
      },
    },
  }
  const vm = parseMomentumVolumeObservation(observation)
  assert.equal(vm.ratio20Mean, 1.27)
  assert.equal(vm.ratio200Mean, 1.12)
  // percentile / zscore 不得发明 mean
  assert.equal('percentile20Mean' in vm, false)
  assert.equal('zscore20Mean' in vm, false)
})

// 锁 8: Volume Badge unknown_count 保留
test('R3E-SLICE3 Volume Badge unknown_count 保留', () => {
  const vm = parseVolumeBadge({ high_count: 4, low_count: 3, normal_count: 10, unknown_count: 1 })
  assert.equal(vm!.highCount, 4)
  assert.equal(vm!.lowCount, 3)
  assert.equal(vm!.normalCount, 10)
  assert.equal(vm!.unknownCount, 1)
  assert.equal(vm!.total, 18)
})

// 锁 9: 20D gap 保留 date slot（缺失为 null，不压缩）
test('R3E-SLICE3 History 20D gap 保留 date slot（缺失为 null）', () => {
  const stateFull = {
    expanding_count: 6, expanding_ratio: 0.3, flat_count: 8, flat_ratio: 0.4,
    contracting_count: 6, contracting_ratio: 0.3, denominator: 20,
  }
  const history = {
    dates: ['2024-01-02', '2024-01-03', '2024-01-04'],
    momentumVolume: {
      dates: ['2024-01-02', '2024-01-03', '2024-01-04'],
      momentum_state: [stateFull, null, stateFull],
      momentum_change: [null, null, null],
      squeeze_state: [null, null, null],
      release_volume_ratio: [null, null, null],
      momentum_volume_relation: [null, null, null],
      volume_percentile20: [null, null, null],
      volume_percentile200: [null, null, null],
      sqzmom_mean: [0.3, null, 0.31],
    },
  } as unknown as ReviewScopeHistoryDTO
  const vm = parseMomentumVolumeHistory(history)
  assert.deepEqual(vm.dates, ['2024-01-02', '2024-01-03', '2024-01-04'])
  assert.equal(vm.momentumState.length, 3)
  assert.ok(vm.momentumState[0].vm != null)
  assert.equal(vm.momentumState[1].vm, null) // gap 保持 null，但 slot 存在
  assert.ok(vm.momentumState[2].vm != null)
  assert.equal(vm.sqzmomMean.length, 3)
  assert.equal(vm.sqzmomMean[1].mean, null)
})

// 组合解析：momentum + participation.volume 走唯一解析 owner
test('R3E-SLICE3 parseMomentumVolumeObservation 组合解析 momentum + participation.volume', () => {
  const observation = {
    momentum: {
      state: { expanding_count: 6, expanding_ratio: 0.3, flat_count: 8, flat_ratio: 0.4, contracting_count: 6, contracting_ratio: 0.3, denominator: 20 },
      change: { enhancing_count: 5, weakening_count: 3, flat_count: 12, denominator: 20 },
      squeeze_state: { squeeze_count: 2, squeeze_ratio: 0.1, squeeze_release_count: 1, squeeze_release_ratio: 0.05, non_squeeze_count: 17, non_squeeze_ratio: 0.85, denominator: 20 },
      bb_position: { median: 0.5, p25: 0.2, p75: 0.8, valid_count: 20, denominator: 20 },
      bb_width: { median: 0.0832, p25: 0.05, p75: 0.12, valid_count: 20, denominator: 20 },
      release_volume_ratio: { median: 1.42, p25: 1.1, p75: 1.9, valid_count: 20, denominator: 20 },
      momentum_volume_relation: { '共振_count': 9, '共振_ratio': 0.45, denominator: 20 },
      sqzmom: { mean: 0.37, valid_count: 18 },
    },
    participation: {
      volume: {
        ratio20: { p25: 0.9, p50: 1.25, p75: 1.6, valid_count: 40 },
        ratio200: { p25: 0.8, p50: 1.1, p75: 1.4, valid_count: 40 },
        percentile20: { p25: 60, p50: 72.5, p75: 85, valid_count: 40 },
        percentile200: { p25: 55, p50: 68, p75: 80, valid_count: 40 },
        zscore20: { p25: -2.0, p50: -1.35, p75: -0.2, valid_count: 40 },
        zscore200: { p25: -1.5, p50: -0.9, p75: 0.1, valid_count: 40 },
        badge: { high_count: 4, low_count: 3, normal_count: 10, unknown_count: 1 },
        ratio20_mean: 1.27,
        ratio200_mean: 1.12,
        percentile20_histogram: { lt20: 1, '20_40': 3, '40_60': 6, '60_80': 5, gte80: 3 },
        percentile200_histogram: { lt20: 2, '20_40': 4, '40_60': 5, '60_80': 4, gte80: 3 },
      },
    },
  }
  const vm = parseMomentumVolumeObservation(observation)
  assert.equal(vm.state!.denominator, 20)
  assert.equal(vm.change!.enhancingCount, 5)
  assert.equal(vm.squeeze!.categories.length, 3)
  assert.equal(vm.bbPosition!.median, 0.5)
  assert.equal(vm.bbWidth!.median, 0.0832)
  assert.equal(vm.releaseVolumeRatio!.median, 1.42)
  assert.equal(vm.sqzmom!.mean, 0.37)
  assert.equal(vm.relation!.categories[0].category, '共振')
  assert.equal(vm.volume!.ratio20!.p50, 1.25)
  assert.equal(vm.volumeBadge!.unknownCount, 1)
  assert.equal(vm.ratio20Mean, 1.27)
  assert.equal(vm.percentile20Histogram!.bins.length, 5)
  assert.equal(vm.percentile200Histogram!.bins[0].label, '0–20')
})

// 锁 10 + 11: 组件只消费 typed VM；不 deepGet raw observation；横截面用 canonical `field`
const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const panelSrc = readFileSync(join(__dirname, '..', 'ScopeMomentumVolumePanel.tsx'), 'utf8')

test('R3E-SLICE3 ScopeMomentumVolumePanel 不 deepGet raw observation（解析在 contract）', () => {
  assert.equal(panelSrc.includes('deepGet'), false, '组件不得出现 deepGet')
  assert.ok(panelSrc.includes("from './scopeMomentumVolumeContract'"), '必须 import 解析 owner')
})

test('R3E-SLICE3 ScopeMomentumVolumePanel 横截面使用 canonical `field`（非 field_key）', () => {
  assert.equal(panelSrc.includes('field_key'), false, '不得用 field_key')
  assert.ok(/\.field\b/.test(panelSrc), '使用 crossSection 的 field 键')
})

// ===========================================================================
// R3E-SLICE3 CORRECTION — 审计 P1/P2 收口
// ===========================================================================

// P1-1: 历史折线不跨 null 连线（与 DSA 同一 gap helper）
test('R3E-SLICE3-CORR P1-1 历史图 null 处断开分段（不跨 gap 连线）', () => {
  const segs = splitSeriesByGap([1, 2, null, null, 8])
  assert.equal(segs.length, 2)
  assert.deepEqual(segs.map((s) => s.map((p) => p.i)), [[0, 1], [4]])
  // 单元 null 也断开
  const segs2 = splitSeriesByGap([5, null, 9])
  assert.equal(segs2.length, 2)
  // 组件必须以分段渲染，禁止单条 polyline 跨 gap
  assert.ok(panelSrc.includes('splitSeriesByGap'), 'MiniLine 必须使用 splitSeriesByGap')
  assert.ok(/segments\.map/.test(panelSrc), '每段独立 polyline')
  // 禁止 forward fill / interpolation
  assert.equal(/filter\(Boolean\)/.test(panelSrc), false, '不得用 filter(Boolean) 跳过 null 后连线')
})

// P1-2: 成交量矩阵 5 列（JSX 单元数 + SCSS grid 列数）
test('R3E-SLICE3-CORR P1-2 成交量矩阵 5 列：JSX 单元 + SCSS grid-template-columns', () => {
  // JSX：1 空表头 + P25/P50/P75/n = 5 列
  assert.equal((panelSrc.match(/styles\.mvMatrixHead/g) ?? []).length, 5)
  // 每行：1 label + 4 数值单元 = 5 列
  assert.equal((panelSrc.match(/styles\.mvMatrixCell/g) ?? []).length, 4)
  assert.equal((panelSrc.match(/styles\.mvMatrixRowLabel/g) ?? []).length, 2)
  // SCSS：label 列 + repeat(4) = 5 列（旧 scaffold 的 3 列必须已被修正）
  const scss = readFileSync(join(__dirname, '..', 'review.module.scss'), 'utf8')
  const mvMatrix = /\.mvMatrix\s*\{([^}]*)\}/.exec(scss)
  assert.ok(mvMatrix, '.mvMatrix 必须存在')
  assert.ok(/grid-template-columns:\s*minmax\(110px,\s*140px\)\s+repeat\(4,/.test(mvMatrix[1]), '.mvMatrix 必须为 5 列')
  assert.equal(/grid-template-columns:\s*120px\s+1fr\s+1fr/.test(mvMatrix[1]), false, '不得回退为 3 列')
})

// P1-3: CrossSection 完整 frontend contract（status / reason，unavailable 不被吞）
test('R3E-SLICE3-CORR P1-3 CrossSection ready / unavailable(reason) 完整契约', () => {
  const fields: ReviewCrossSectionFieldDTO[] = [
    {
      field: 'momentum.bb_position', value: 0.5, percentile: 80,
      peer_count: 20, valid_peer_count: 19, status: 'ready', reason: null,
    },
    {
      field: 'participation.volume.ratio20', value: 1.2, percentile: null,
      peer_count: 3, valid_peer_count: 2, status: 'unavailable', reason: 'INSUFFICIENT_PEER_SAMPLE',
    },
    {
      field: 'momentum.bb_width', value: null, percentile: null,
      peer_count: 0, valid_peer_count: 0, status: 'unavailable', reason: 'NO_PEERS',
    },
    {
      field: 'participation.volume.ratio200', value: null, percentile: null,
      peer_count: 20, valid_peer_count: 0, status: 'unavailable', reason: 'CURRENT_FIELD_UNAVAILABLE',
    },
  ]
  const ready = fields[0]
  assert.equal(ready.status, 'ready')
  assert.equal(ready.percentile, 80)
  // 三种 unavailable reason 全部保留，不得被吞成模糊的 P—
  const unavailable = fields.filter((f) => f.status === 'unavailable')
  assert.equal(unavailable.length, 3)
  assert.ok(unavailable.every((f) => f.percentile === null))
  assert.ok(unavailable.every((f) => f.reason !== null), 'unavailable 必须带 reason')
  assert.deepEqual(
    unavailable.map((f) => f.reason),
    ['INSUFFICIENT_PEER_SAMPLE', 'NO_PEERS', 'CURRENT_FIELD_UNAVAILABLE'],
  )
  // Panel 必须按 status 分支，并展示 reason 与 valid/total peers
  assert.ok(panelSrc.includes("f.status === 'unavailable'"), 'panel 必须按 status 分支')
  assert.ok(panelSrc.includes('f.reason'), 'panel 必须展示 reason')
  assert.ok(/valid peers \{f\.valid_peer_count\} \/ \{f\.peer_count\}/.test(panelSrc), '展示 valid/total peers')
  assert.equal(panelSrc.includes('field_key'), false, 'canonical key 为 field')
})

// P1-4: 删除假“20D Relation”（只显示最后一天）
test('R3E-SLICE3-CORR P1-4 页面不再存在伪 20D Relation 区块', () => {
  assert.equal(panelSrc.includes('20D Momentum × Volume Relation'), false, '不得再用 20D 标题展示单日关系')
  assert.equal(/historyVm\.relation\[/.test(panelSrc), false, '不得只取 relation 最后一天')
  // Current 的 OPEN categorical 关系仍完整保留
  assert.ok(panelSrc.includes('Momentum × Volume Relation'), 'Current 关系必须保留')
})

// P2-A: 展示 ratio 由 typed VM 提供，组件不做 n / denominator
test('R3E-SLICE3-CORR P2-A 展示 ratio 由 typed VM 提供（组件不做除法派生）', () => {
  const change = parseMomentumChange({ enhancing_count: 5, weakening_count: 3, flat_count: 12, denominator: 20 })
  const enh = change!.categories.find((c) => c.category === 'Enhancing')!
  assert.equal(enh.count, 5)
  assert.equal(enh.ratio, 0.25) // 5 / 20
  const wk = change!.categories.find((c) => c.category === 'Weakening')!
  assert.equal(wk.ratio, 0.15) // 3 / 20

  const badge = parseVolumeBadge({ high_count: 4, low_count: 3, normal_count: 10, unknown_count: 1 })
  const hi = badge!.entries.find((e) => e.category === 'High')!
  assert.equal(hi.count, 4)
  assert.equal(hi.ratio, 4 / 18)
  const unk = badge!.entries.find((e) => e.category === 'Unknown')!
  assert.equal(unk.count, 1)
  assert.equal(unk.ratio, 1 / 18)

  // denominator=0 / total=0 -> ratio null（不 0%）
  const zero = parseMomentumChange({ enhancing_count: 0, weakening_count: 0, flat_count: 0, denominator: 0 })
  assert.equal(zero!.categories[0].ratio, null)
  const emptyBadge = parseVolumeBadge({ high_count: 0, low_count: 0, normal_count: 0, unknown_count: 0 })
  assert.equal(emptyBadge!.entries[0].ratio, null)

  // 组件不得自行做业务除法派生
  assert.equal(/\/\s*denom/.test(panelSrc), false, '组件不得 n / denom')
  assert.equal(/\/\s*denominator/.test(panelSrc), false, '组件不得 n / denominator')
  assert.equal(/\/\s*total/.test(panelSrc), false, '组件不得 count / total')
  // 组件消费 VM 提供的 ratio
  assert.ok(/vm\.change\?\.categories/.test(panelSrc), 'change 消费 VM categories')
  assert.ok(/vm\.volumeBadge\s*\?\s*\n?\s*<div[\s\S]{0,80}volumeBadge\.entries|volumeBadge\.entries\.map/.test(panelSrc), 'badge 消费 VM entries')
  assert.ok(/e\.ratio/.test(panelSrc) && /c\.ratio/.test(panelSrc), '消费 VM ratio')
})

// P2-B: 不把开发状态写给产品用户
test('R3E-SLICE3-CORR P2-B 页面不含开发说明（deferred / 未接线 / architecture）', () => {
  assert.equal(/Historical Position/.test(panelSrc), false, '不得展示 Historical Position 开发说明')
  assert.equal(/deferred/.test(panelSrc), false, '不得展示 deferred 开发术语')
  assert.equal(/Dynamics architecture/.test(panelSrc), false, '不得展示架构说明')
  assert.equal(/未接线/.test(panelSrc), false, '不得展示 API 未接线说明')
})
