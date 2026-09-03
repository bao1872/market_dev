// [R3C-DSA] DSA observation contract tests (pure TS owner).
// Locks the numeric-scale, neutral_ratio key, distribution, transition decode,
// and sparkline gap contracts (P1-3 correction).
import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import {
  parseDsaObservation,
  buildDsaVM,
  splitSeriesByGap,
} from '../scopeDsaContract'
import { displayMember } from '../reviewFormat'

// canonical observation payload (matches scope_observation.compute_scope_observation)
const OBS = {
  trend: {
    continuous: {
      regime_strength: 0.7,
      dsa_vwap_dev_pct: 4.2, // percentage points
      segment_bars: 14,
      segment_change_pct: 4.2, // percentage points
      segment_slope: 0.35, // %/bar
      segment_volume_mean_ratio: 1.15,
      segment_amount_mean_ratio: 0.9,
    },
    state: {
      up_count: 25,
      up_ratio: 0.6,
      neutral_count: 8,
      neutral_ratio: 0.2, // canonical key (NOT range_ratio)
      down_count: 8,
      down_ratio: 0.2,
      denominator: 41,
    },
    trend_strength_distribution: {
      p25: 0.3,
      p50: 0.7,
      p75: 0.9,
      valid_count: 100,
      mean: 0.68,
    },
    dsa_vwap_dev_pct_distribution: {
      p25: 1.0,
      p50: 4.2,
      p75: 8.0,
      valid_count: 100,
      mean: 4.5,
    },
    dsa_dir_bars_distribution: {
      p25: 2,
      p50: 8,
      p75: 20,
      valid_count: 100,
      mean: 9.5,
      buckets: [
        { label: '≤2', count: 10, ratio: 0.1 },
        { label: '3-4', count: 20, ratio: 0.2 },
        { label: '5-9', count: 30, ratio: 0.3 },
        { label: '10-19', count: 25, ratio: 0.25 },
        { label: '20-29', count: 10, ratio: 0.1 },
        { label: '30-59', count: 4, ratio: 0.04 },
        { label: '≥60', count: 1, ratio: 0.01 },
      ],
    },
    transition: {
      denominator: 41,
      // 真实 UUID-like member ID（实盘即内部 instrument id，必须经 memberDirectory 解析）
      changed_members: [
        { member_id: '0473e2f3-91b4-4526-abcc-a5e12cfb9fc1', previous_state: 'Neutral', current_state: 'Up' },
        { member_id: '9b1f2c7a-3d58-4e10-9c2f-1a7b6c5d4e3f', previous_state: 'Down', current_state: 'Neutral' },
      ],
      'Neutral→Up': { count: 5, ratio: 0.12 },
      'Up→Down': { count: 3, ratio: 0.07 },
    },
  },
}

test('DSA numeric scale — no x100 (P1-3 #1/#2/#3)', () => {
  const vm = buildDsaVM(parseDsaObservation(OBS as unknown as Record<string, unknown>))
  // 4.2 percentage points -> 4.20%, NOT 420.0%
  assert.equal(vm.dsaVwapDevPct, '4.20%')
  assert.equal(vm.segmentChangePct, '4.20%')
  // 0.35 %/bar -> 0.35%/bar
  assert.equal(vm.segmentSlope, '0.35%/bar')
})

test('DSA neutral key = neutral_ratio (P1-3 #4), not range_ratio', () => {
  // canonical payload uses neutral_ratio
  const vm = buildDsaVM(parseDsaObservation(OBS as unknown as Record<string, unknown>))
  assert.equal(vm.neutralRatio, '20.00%')

  // A payload that only has the WRONG key (range_ratio) must NOT be read as neutral
  const wrong = {
    trend: {
      state: { up_ratio: 0.6, range_ratio: 0.2, down_ratio: 0.2, denominator: 41 },
    },
  }
  const vmWrong = buildDsaVM(parseDsaObservation(wrong as unknown as Record<string, unknown>))
  assert.equal(vmWrong.neutralRatio, '—', 'range_ratio 不得被当成 neutral')

  // ratios formatted as percent (*100), not percentage points
  assert.equal(vm.upRatio, '60.00%')
  assert.equal(vm.downRatio, '20.00%')
})

test('DSA distributions read canonical percentiles (P1-4)', () => {
  const vm = buildDsaVM(parseDsaObservation(OBS as unknown as Record<string, unknown>))
  // median(P50) + mean 突出；P25/P75 辅助；n 显式
  assert.equal(vm.trendStrengthDist, '中位数 0.70 · 均值 0.68 · P25 0.30 · P75 0.90 · n=100')
  assert.equal(vm.dsaVwapDevDist, '中位数 4.20 · 均值 4.50 · P25 1.00 · P75 8.00 · n=100')
})

test('DSA transition decode lists only real migrations (P1-4)', () => {
  const vm = buildDsaVM(parseDsaObservation(OBS as unknown as Record<string, unknown>))
  // sorted by ratio desc
  assert.deepEqual(
    vm.transitions.map((t) => t.key),
    ['Neutral→Up', 'Up→Down'],
  )
  assert.equal(vm.transitions[0].ratio, '12.00%')
  // canonical transition has NO member IDs — verified by absence; honest display
  // of ratio is the confirmed T-1→T change view.
})

test('DSA duration distribution reads canonical median+mean + 7 buckets (P1-4)', () => {
  const vm = buildDsaVM(parseDsaObservation(OBS as unknown as Record<string, unknown>))
  // median(P50) + mean 突出；P25/P75 辅助；n 显式
  assert.equal(vm.dsaDirBarsDist, '中位数 8.00 · 均值 9.50 · P25 2.00 · P75 20.00 · n=100')
  assert.equal(vm.dsaDirBarsBuckets.length, 7)
  assert.equal(vm.dsaDirBarsBuckets[0].label, '≤2')
  assert.equal(vm.dsaDirBarsBuckets[2].count, 30)
  assert.equal(vm.dsaDirBarsBuckets[2].ratio, '30.0%')
  assert.equal(vm.dsaDirBarsBuckets[6].label, '≥60')
})

test('DSA changed members parsed; denominator drives empty-state (P1-4 / D)', () => {
  const vm = buildDsaVM(parseDsaObservation(OBS as unknown as Record<string, unknown>))
  // denominator > 0 且存在变化成员
  assert.equal(vm.transitionDenominator, 41)
  assert.equal(vm.changedMembers.length, 2)
  // 真实 UUID-like member_id（实盘内部 ID），不假装成股票代码
  assert.equal(vm.changedMembers[0].memberId, '0473e2f3-91b4-4526-abcc-a5e12cfb9fc1')
  assert.equal(vm.changedMembers[0].previousState, 'Neutral')
  assert.equal(vm.changedMembers[0].currentState, 'Up')
  assert.equal(vm.changedMembers[1].currentState, 'Neutral')
})

test('DSA changed members resolved via memberDirectory displayMember (no raw UUID in UI)', () => {
  // 页面消费路径：UUID member_id + memberDirectory -> 股票名称·代码（不展示裸 UUID）。
  const vm = buildDsaVM(parseDsaObservation(OBS as unknown as Record<string, unknown>))
  const dir = {
    '0473e2f3-91b4-4526-abcc-a5e12cfb9fc1': { symbol: '600000', name: '浦发银行' },
    '9b1f2c7a-3d58-4e10-9c2f-1a7b6c5d4e3f': { symbol: '000001', name: '平安银行' },
  }
  assert.equal(
    displayMember(vm.changedMembers[0].memberId, dir),
    '浦发银行 · 600000',
  )
  assert.equal(
    displayMember(vm.changedMembers[1].memberId, dir),
    '平安银行 · 000001',
  )
  // ScopeDsaPanel 必须经 displayMember(m.memberId, memberDirectory) 渲染，而非裸 UUID
  const panelSrc = readFileSync(
    new URL('../ScopeDsaPanel.tsx', import.meta.url),
    'utf8',
  )
  assert.ok(
    panelSrc.includes('displayMember(m.memberId, memberDirectory)'),
    'ScopeDsaPanel 必须走 displayMember(memberId, memberDirectory) 展示变化成员',
  )
  assert.ok(
    !panelSrc.includes('>{m.memberId}</span>'),
    'ScopeDsaPanel 不得直接渲染裸 UUID',
  )
})

test('DSA transition denominator=0 => unavailable (not "stable") (D)', () => {
  const zeroDenom = {
    trend: {
      transition: {
        denominator: 0,
        changed_members: [],
        'Neutral→Up': { count: 0, ratio: 0 },
      },
    },
  }
  const vm = buildDsaVM(parseDsaObservation(zeroDenom as unknown as Record<string, unknown>))
  assert.equal(vm.transitionDenominator, 0)
  assert.equal(vm.changedMembers.length, 0)
  // 面板据此渲染“迁移数据不可用”，而非错误地显示“无成员发生状态变化”
})

test('Sparkline gap — null splits segments (P1-3 #5)', () => {
  const segs = splitSeriesByGap([1, 2, null, null, 8])
  assert.equal(segs.length, 2)
  assert.deepEqual(
    segs.map((s) => s.map((p) => p.i)),
    [[0, 1], [4]],
  )

  // a single null in the middle breaks the line
  const segs2 = splitSeriesByGap([5, null, 9])
  assert.equal(segs2.length, 2)
  assert.equal(segs2[0][0].i, 0)
  assert.equal(segs2[1][0].i, 2)

  // all null -> no segments (component renders —)
  assert.deepEqual(splitSeriesByGap([null, null]), [])
})
