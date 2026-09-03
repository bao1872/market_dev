// [SLICE 4 / PRICE] Focused contract tests for the Price typed owner.
// Locks the §十三 frontend contract list (14 items): units, variance %²,
// mean±std (never mean±variance), null gap, persisted Capital Tilt,
// leadership unavailable preservation, and "component only consumes typed owner".
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import {
  parsePriceAnalysis,
  parsePriceCurrent,
  parsePriceRolling,
  parseCapitalTilt,
  parseLeadershipHistory,
  parsePriceBreadth,
  buildPriceEwChart,
  formatDecimalReturn,
  formatReturnVariancePctSquared,
  formatReturnZScore,
  formatRatioPct,
  formatReturnDispersion,
} from '../scopePriceAnalysisContract'
import { splitSeriesByGap } from '../scopeDsaContract'
import { formatPosition } from '../reviewFormat'
import type { ReviewScopeHistoryDTO } from '../types'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const panelSrc = readFileSync(join(__dirname, '..', 'ScopePriceAnalysisPanel.tsx'), 'utf8')

const DATES = ['2024-01-02', '2024-01-03', '2024-01-04', '2024-01-05']

function field(over: Partial<{
  series: Array<number | null>
  mean20: Array<number | null>
  variance20: Array<number | null>
  std20: Array<number | null>
  zscore20: Array<number | null>
  baselineCount: Array<number | null>
}> = {}) {
  return {
    key: 'equal_weight_return',
    label: '等权收益',
    unit: 'pct',
    series: over.series ?? [0.010, 0.012, null, 0.0123],
    mean20: over.mean20 ?? [null, 0.009, 0.008, 0.008],
    variance20: over.variance20 ?? [null, null, 0.0004, 0.0004],
    std20: over.std20 ?? [null, null, 0.02, 0.02],
    zscore20: over.zscore20 ?? [null, null, null, 0.215],
    baselineCount: over.baselineCount ?? [0, 1, 2, 3],
  } as unknown as ReviewScopeHistoryDTO['fields'][string]
}

// 1. EW decimal return -> percent
test('PRICE 1: EW 0.0123 -> 1.23%', () => {
  assert.equal(formatDecimalReturn(0.0123), '1.23%')
  assert.equal(formatDecimalReturn(null), '—')
})

// 2. mean / std same percent scale
test('PRICE 2: mean/std 与 EW 同 percent scale（0.008 -> 0.80%）', () => {
  assert.equal(formatDecimalReturn(0.008), '0.80%')
  assert.equal(formatDecimalReturn(0.02), '2.00%')
})

// 3. variance 0.0004 -> 4.00 %²（x10000；不是 0.04 也不是 0.0004）
test('PRICE 3: variance 0.0004 -> 4.00 %²', () => {
  assert.equal(formatReturnVariancePctSquared(0.0004), '4.00 %²')
  assert.notEqual(formatReturnVariancePctSquared(0.0004), '0.04 %²')
  assert.notEqual(formatReturnVariancePctSquared(0.0004), '0.0004 %²')
  assert.equal(formatReturnVariancePctSquared(null), '—')
})

// 4. Z no %
test('PRICE 4: Z 无 %', () => {
  const z = formatReturnZScore(1.35)
  assert.equal(z, '1.35')
  assert.equal(z.includes('%'), false)
  assert.equal(formatReturnZScore(-1.35), '-1.35')
  assert.equal(formatReturnZScore(null), '—')
})

// 5. band = mean ± std，绝不 mean ± variance
test('PRICE 5: band = Mean ± Std（绝不 Mean ± Variance）', () => {
  const f = field({
    series: [0.01, 0.02],
    mean20: [0.01, 0.01],
    variance20: [0.0004, 0.0004],
    std20: [0.02, 0.02],
    zscore20: [0, 0.5],
    baselineCount: [10, 10],
  })
  const chart = buildPriceEwChart(['d1', 'd2'], f)
  assert.equal(chart.upperBand[0], 0.03) // 0.01 + 0.02
  assert.equal(chart.lowerBand[0], -0.01) // 0.01 - 0.02
  // 若误用 variance：0.01 + 0.0004 = 0.0104 —— 必须不等
  assert.notEqual(chart.upperBand[0], 0.0104)
})

// 6. null chart gap（保留 slot，不插值）
test('PRICE 6: null 保持 gap（序列 null + 分段渲染）', () => {
  const f = field()
  const chart = buildPriceEwChart(DATES, f)
  // index 2 的 mean 非空但 series 为 null —— EW 线在该处断开
  assert.equal(chart.ew[2], null)
  // mean 为空的 index 0 -> band 两侧皆 null（不成带）
  assert.equal(chart.upperBand[0], null)
  assert.equal(chart.lowerBand[0], null)
  // 分段渲染
  const segs = splitSeriesByGap([1, 2, null, null, 8])
  assert.equal(segs.length, 2)
  assert.ok(panelSrc.includes('splitSeriesByGap'), 'panel 必须使用 gap helper 分段渲染')
})

// 7. Position 0–100（不 x100）
test('PRICE 7: Position 0–100 原值展示（不 x100）', () => {
  assert.equal(formatPosition(75), '75')
  assert.notEqual(formatPosition(75), '7500')
  assert.notEqual(formatPosition(0.75), '75')
  assert.equal(formatPosition(null), '—')
})

// 8. 不前端重算 Dynamics
test('PRICE 8: 不前端重算 Dynamics（复用共享三图 renderer）', () => {
  assert.ok(panelSrc.includes("from './ScopeDynamicsCharts'"), '必须复用共享三图 renderer')
  // 不得出现 EMA / slope / 差分式重算
  assert.equal(/ema5|ema20|slope\(/.test(panelSrc), false, '不得重算 EMA')
  assert.equal(/velocity\[i\]\s*-|acceleration\[i\]\s*-/.test(panelSrc), false, '不得差分重算 V/A')
  // Price 页不是 Phase 页：不得渲染/读取 phase 事实
  assert.equal(
    /currentPhaseFact|formatPhaseLabel|phaseCurrent|Early Lift|Strengthening|Sustained|Decelerating|Repairing/.test(panelSrc),
    false,
    'Price 页不得展示 Phase / 阶段标签',
  )
})

// 9. Capital Tilt 读 persisted fact（绝不 AW - EW）
test('PRICE 9: Capital Tilt 读 persisted fact（不是 AW - EW）', () => {
  const tilt = parseCapitalTilt(DATES, { dates: DATES, capital_tilt: [0.004, null, -0.002, null], leadership: [] }, 0.004, 0.010, 0.016)
  // persisted fact = 0.004；AW - EW 会是 0.006
  assert.equal(tilt.current, 0.004)
  assert.notEqual(tilt.current, 0.006)
  assert.equal(tilt.currentText, '0.40%')
  // 历史 verbatim（null 保持）
  assert.deepEqual(tilt.values, [0.004, null, -0.002, null])
})

// 10. Panel 内不得出现 AW - EW
test('PRICE 10: Panel 不做 AW - EW', () => {
  assert.equal(/amountWeightedReturn\s*-\s*equalWeightReturn/.test(panelSrc), false)
  assert.equal(/\baw\s*-\s*ew\b/i.test(panelSrc), false)
  assert.equal(/amount_weighted_return\s*-\s*equal_weight_return/.test(panelSrc), false)
})

// 11. Breadth null 保留（绝不 0）
test('PRICE 11: Breadth null 保留（null 不是 0）', () => {
  const adv = { ...field({ series: [0.5, null, 0.4, 0.6] }) } as never
  const dec = { ...field({ series: [0.3, null, 0.4, 0.2] }) } as never
  const unc = { ...field({ series: [0.2, null, 0.2, 0.2] }) } as never
  const b = parsePriceBreadth(DATES, adv, dec, unc)
  assert.equal(b.points[1].advance, null)
  assert.equal(b.points[1].decline, null)
  assert.equal(b.points[1].unchanged, null)
  assert.equal(b.points[0].advance, 0.5)
  // ratio -> percent formatter
  assert.equal(formatRatioPct(0.5), '50.0%')
})

// 12. leadership unavailable / status / reason 保留
test('PRICE 12: leadership status/reason/unavailable 保留（不吞掉）', () => {
  const vm = parseLeadershipHistory(DATES, {
    dates: DATES,
    capital_tilt: [],
    leadership: [
      { status: 'unavailable', reason: 'CURRENT_LEADER_SET_UNAVAILABLE', jaccard_stability: null, migration: null, current_leader_count: 0, current_leader_ids: [] },
      { status: 'ready', reason: null, jaccard_stability: 0.42, migration: 0.58, current_leader_count: 2, current_leader_ids: ['a', 'b'] },
      null,
      { status: 'ready', reason: null, jaccard_stability: 0.1, migration: 0.9, current_leader_count: 0, current_leader_ids: [] },
    ],
  })
  assert.equal(vm.points[0].unavailable, true)
  assert.equal(vm.points[0].reason, 'CURRENT_LEADER_SET_UNAVAILABLE')
  assert.equal(vm.points[1].unavailable, false)
  assert.equal(vm.points[1].jaccardStability, 0.42)
  assert.equal(vm.points[1].migration, 0.58)
  assert.deepEqual(vm.points[1].currentLeaderIds, ['a', 'b'])
  // 缺失日 -> null 且 unavailable（slot 保留）
  assert.equal(vm.points[2].status, null)
  assert.equal(vm.points[2].unavailable, true)
  // [] 空 leader set 与 null 语义不同
  assert.deepEqual(vm.points[3].currentLeaderIds, [])
  assert.notEqual(vm.points[3].currentLeaderIds, null)
  assert.equal(vm.points[2].currentLeaderIds, null)
})

// 13. leader 名称经 memberDirectory（ONE bulk query，不逐个请求）
test('PRICE 13: leader 展示经 memberDirectory / displayMember（不逐个成员请求）', () => {
  assert.ok(panelSrc.includes('memberDirectory'), '必须消费 memberDirectory')
  assert.equal(/fetch\(|useQuery|axios/.test(panelSrc), false, '不得在组件内发成员请求')
  // 成员展示唯一 owner = displayMember（名称 · 代码；目录缺失才 fallback 裸 id）
  assert.ok(/displayMember\(/.test(panelSrc), '必须复用 displayMember 展示 owner')
})

// 14. 组件只消费 typed owner
test('PRICE 14: 组件只消费 typed owner（不 deepGet / 不自建单位）', () => {
  assert.ok(panelSrc.includes('parsePriceAnalysis'), '必须消费 typed owner')
  assert.equal(panelSrc.includes('deepGet'), false, '组件不得 deepGet canonical payload')
  assert.equal(/\/ *100\b/.test(panelSrc), false, '组件不得自行做单位 x100 换算')
  assert.equal(/10000/.test(panelSrc), false, '组件不得自行做 %² 换算')
})

// 组合解析：当前事实 + rolling + history.price 一次成形
test('PRICE 组合：parsePriceAnalysis 消费 persisted facts', () => {
  const observation = {
    price: {
      equal_weight_return: 0.0123,
      amount_weighted_return: 0.016,
      breadth: { advance_ratio: 0.5, decline_ratio: 0.3, unchanged_ratio: 0.2 },
      return_dispersion: 0.031,
    },
  }
  const history = {
    dates: DATES,
    fields: {
      equal_weight_return: field(),
      amount_weighted_return: { ...field({ series: [0.012, 0.014, null, 0.016] }) } as never,
      advance_ratio: { ...field({ series: [0.5, null, 0.4, 0.6] }) } as never,
      decline_ratio: { ...field({ series: [0.3, null, 0.4, 0.2] }) } as never,
      unchanged_ratio: { ...field({ series: [0.2, null, 0.2, 0.2] }) } as never,
      return_dispersion: { ...field({ series: [0.03, null, 0.031, 0.028] }) } as never,
    },
    price: {
      dates: DATES,
      capital_tilt: [0.004, null, -0.002, null],
      leadership: [
        { status: 'ready', reason: null, jaccard_stability: 0.42, migration: 0.58, current_leader_count: 2, current_leader_ids: ['a', 'b'] },
        null,
        { status: 'unavailable', reason: 'NO_PEERS', jaccard_stability: null, migration: null, current_leader_count: 0, current_leader_ids: [] },
        null,
      ],
    },
  } as unknown as ReviewScopeHistoryDTO

  const vm = parsePriceAnalysis({
    dates: DATES,
    observation,
    history,
    currentTilt: 0.004,
    crossSection: null,
  })
  assert.equal(vm.current.equalWeightReturn, 0.0123)
  assert.equal(vm.current.equalWeightReturnText, '1.23%')
  assert.equal(vm.current.returnDispersionText, formatReturnDispersion(0.031))
  assert.equal(vm.rolling?.variance20, 0.0004)
  assert.equal(vm.rolling?.variance20Text, '4.00 %²')
  assert.equal(vm.rolling?.baselineCount, 3)
  assert.deepEqual(vm.capitalTilt.values, [0.004, null, -0.002, null])
  assert.equal(vm.leadership.points[0].jaccardStability, 0.42)
  assert.equal(vm.leadership.points[2].reason, 'NO_PEERS')
  // disperson 不 x100
  assert.equal(vm.dispersion.values[0], 0.03)
})

// ===========================================================================
// PRICE FINAL CORRECTION — 审计 3 个 P1 + 3 个 P2
// ===========================================================================

// P1-2: return_dispersion 与 EW/AW 同处 decimal-return 空间
test('PRICE-CORR P1-2: return_dispersion 0.03 → 3.00%（不是 0.03 / 300.00%）', () => {
  assert.equal(formatReturnDispersion(0.03), '3.00%')
  assert.notEqual(formatReturnDispersion(0.03), '0.03', '不得按无量纲原值展示')
  assert.notEqual(formatReturnDispersion(0.03), '300.00%', '不得多乘一次 100')
  assert.equal(formatReturnDispersion(null), '—')
  // 与 EW 同一 formatter 口径（同 decimal-return 空间）
  assert.equal(formatReturnDispersion(0.03), formatDecimalReturn(0.03))
})

// P1-3: 复用共享三图 renderer 后必须保留 TradingView attribution
test('PRICE-CORR P1-3: 复用 DynamicsCharts 时保留真实 TradingView attribution', () => {
  const engineSrc = readFileSync(join(__dirname, '..', 'ScopeDynamicsCharts.tsx'), 'utf8')
  // 共享 renderer 关闭了 logo
  assert.ok(/attributionLogo:\s*false/.test(engineSrc), '共享 renderer 关闭了 TV logo')
  // 因此每个复用者都必须保留真实 <a> 归属
  assert.ok(panelSrc.includes("from './ScopeDynamicsCharts'"), 'Price 页复用了共享 renderer')
  assert.ok(
    /href="https:\/\/www\.tradingview\.com\/"/.test(panelSrc),
    'Price 页缺少 TradingView 真实链接（P1-F contract）',
  )
  assert.ok(
    /<a[\s\S]*TradingView Lightweight Charts[\s\S]*<\/a>/.test(panelSrc),
    'Price 页 attribution 不是真实 <a> 元素',
  )
  assert.ok(/chartAttribution/.test(panelSrc), 'Price 页必须保留归属容器')
})

// P2-A: Breadth 不得 null→0 再重新归一化
test('PRICE-CORR P2-A: Breadth null 保留 gap（不 null→0、不重新归一化）', () => {
  // 三项本身就是 canonical ratio，直接使用
  assert.equal(formatRatioPct(0.5), '50.0%')
  // renderer 契约：缺少任一项 → 该列渲染为 gap 容器，不是归一化后的 0 高柱
  assert.ok(/data-breadth-gap/.test(panelSrc), '缺失日期必须渲染为 gap 列')
  // 不得存在「先 fmtPct(null)->0 再按 total 归一化」的旧实现
  assert.equal(/fmtPct/.test(panelSrc), false, '不得再用 null→0 的 fmtPct')
  assert.equal(/1\s*\/\s*total|total\s*>\s*0\s*\?\s*1/.test(panelSrc), false, '不得重新归一化三项')
  // 仍保留 clamp01（仅用于已确认非 null 的 ratio）
  assert.ok(/clamp01/.test(panelSrc), '保留 [0,1] 收敛（不用于 null）')
})

// P2-B: A 股 positive 红 / negative 绿
test('PRICE-CORR P2-B: A 股配色（Advance 红 / Decline 绿 / Unchanged neutral）', () => {
  const adv = /ADV_COLOR\s*=\s*'#dc2626'/.test(panelSrc)
  const dec = /DEC_COLOR\s*=\s*'#16a34a'/.test(panelSrc)
  assert.ok(adv, 'Advance 必须为红（A 股 positive red）')
  assert.ok(dec, 'Decline 必须为绿（A 股 negative green）')
  // 不得仍是 Advance 绿 / Decline 红
  assert.equal(/ADV_COLOR\s*=\s*'#16a34a'/.test(panelSrc), false, '不得仍是 Advance 绿')
  assert.equal(/DEC_COLOR\s*=\s*'#dc2626'/.test(panelSrc), false, '不得仍是 Decline 红')
})

test('PRICE: parsePriceCurrent 缺失保持 null（不 0）', () => {
  const vm = parsePriceCurrent({ price: {} })
  assert.equal(vm.equalWeightReturn, null)
  assert.equal(vm.equalWeightReturnText, '—')
  assert.equal(vm.advanceRatio, null)
  assert.equal(vm.returnDispersion, null)
})

test('PRICE: parsePriceRolling 缺失 field -> null', () => {
  assert.equal(parsePriceRolling(null), null)
})
