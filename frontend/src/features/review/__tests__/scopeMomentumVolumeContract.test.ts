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
import type { ReviewScopeHistoryDTO } from '../types'

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
