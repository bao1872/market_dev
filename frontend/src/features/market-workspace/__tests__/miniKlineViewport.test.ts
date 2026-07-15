// [MiniKlineViewport] - 描述: 小 K 线 viewport 纯函数测试（无截图契约）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/miniKlineViewport.test.ts
//
// CHANGE-20260715-002 覆盖：
//  1. 五周期目标根数：15m=48、60m=44、日=40、周=36、月=30
//  2. barSpacing clamp 5.5–8px
//  3. from = max(-2, dataLength - visibleBars - 1)（左侧 1-2 根留白）
//  4. to = dataLength - 1 + 3（右侧 3 bar 留白）
//  5. 空数据返回零区间
//  6. contentWidth 整数化（亚像素不抖动）
//  7. 价格轴宽度固定 56
//  8. computeVisiblePriceRange 正确计算 min/max
//  9. computeAutoscaleRange 上方 12%、下方 15% 扩展
// 10. barSpacing 字段正确返回

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  computeMiniKlineViewport,
  computeVisiblePriceRange,
  computeAutoscaleRange,
  MIN_PRICE_SCALE_WIDTH,
  RIGHT_PADDING_BARS,
  LEFT_PADDING_BARS,
  type MiniKlineTimeframe,
} from '../miniKlineViewport.ts'

// ===== 1. 五周期目标根数（大宽度不增加根数）=====
test('大宽度时五周期返回目标根数：15m=48, 1h=44, 1d=40, 1w=36, 1mo=30', () => {
  // 用极大 contentWidth，barSpacing 都 > 8，但保持目标根数
  const cases: Array<{ tf: MiniKlineTimeframe; expected: number }> = [
    { tf: '15m', expected: 48 },
    { tf: '1h', expected: 44 },
    { tf: '1d', expected: 40 },
    { tf: '1w', expected: 36 },
    { tf: '1mo', expected: 30 },
  ]
  for (const { tf, expected } of cases) {
    const vp = computeMiniKlineViewport(200, tf, 5000)
    assert.equal(
      vp.visibleBars,
      expected,
      `${tf} 大宽度时应为目标根数 ${expected}，实际 ${vp.visibleBars}`,
    )
  }
})

// ===== 2. 极窄宽度触发 barSpacing 下界 clamp =====
test('极窄宽度时 barSpacing 不低于 5.5px', () => {
  // contentWidth=100, effectivePlotWidth=44
  // 所有周期的 barSpacing < 5.5，减少根数
  for (const tf of ['15m', '1h', '1d', '1w', '1mo'] as MiniKlineTimeframe[]) {
    const vp = computeMiniKlineViewport(200, tf, 100)
    assert.ok(
      vp.barSpacing >= 5.5 || vp.visibleBars <= 1,
      `${tf} barSpacing 应 >= 5.5（或 visibleBars<=1），实际 barSpacing=${vp.barSpacing} visibleBars=${vp.visibleBars}`,
    )
  }
})

// ===== 3. visibleBars 按 barSpacing clamp 计算 =====
test('contentWidth=340 时各周期 visibleBars 正确', () => {
  // contentWidth=340, effectivePlotWidth=284
  // 15m: barSpacing=284/48=5.92 (>=5.5), visibleBars=48
  // 1h: barSpacing=284/44=6.45 (>=5.5), visibleBars=44
  // 1d: barSpacing=284/40=7.1 (>=5.5), visibleBars=40
  // 1w: barSpacing=284/36=7.89 (>=5.5), visibleBars=36
  // 1mo: barSpacing=284/30=9.47 (>8, 保持), visibleBars=30
  const cases: Array<{ tf: MiniKlineTimeframe; expected: number }> = [
    { tf: '15m', expected: 48 },
    { tf: '1h', expected: 44 },
    { tf: '1d', expected: 40 },
    { tf: '1w', expected: 36 },
    { tf: '1mo', expected: 30 },
  ]
  for (const { tf, expected } of cases) {
    const vp = computeMiniKlineViewport(200, tf, 340)
    assert.equal(
      vp.visibleBars,
      expected,
      `${tf} contentWidth=340 时 visibleBars 应为 ${expected}，实际 ${vp.visibleBars}`,
    )
  }
})

// ===== 4. contentWidth=300 时 15m 触发 barSpacing 下界 =====
test('contentWidth=300 时 15m barSpacing < 5.5，减少根数', () => {
  // contentWidth=300, effectivePlotWidth=244
  // 15m: barSpacing=244/48=5.08 (<5.5), 减少: floor(244/5.5)=44
  const vp15m = computeMiniKlineViewport(200, '15m', 300)
  assert.equal(vp15m.visibleBars, 44, '15m contentWidth=300 应减少到 44')
  assert.ok(vp15m.barSpacing >= 5.5, `15m barSpacing 应 >= 5.5，实际 ${vp15m.barSpacing}`)

  // 1h: barSpacing=244/44=5.55 (>=5.5), 保持 44
  const vp1h = computeMiniKlineViewport(200, '1h', 300)
  assert.equal(vp1h.visibleBars, 44, '1h contentWidth=300 应保持 44')

  // 1d: barSpacing=244/40=6.1 (>=5.5), 保持 40
  const vp1d = computeMiniKlineViewport(200, '1d', 300)
  assert.equal(vp1d.visibleBars, 40, '1d contentWidth=300 应保持 40')
})

// ===== 5. from = max(-LEFT_PADDING_BARS, dataLength - visibleBars - 1) =====
test('from 包含左侧 1-2 根留白', () => {
  // dataLength=80, visibleBars=40 (1d, contentWidth=340)
  // from = max(-2, 80-40-1) = max(-2, 39) = 39
  const vp1 = computeMiniKlineViewport(80, '1d', 340)
  assert.equal(vp1.from, 39, 'from 应为 max(-2, 80-40-1)=39')

  // dataLength=30, visibleBars=30 (1mo, contentWidth=340)
  // from = max(-2, 30-30-1) = max(-2, -1) = -1
  const vp2 = computeMiniKlineViewport(30, '1mo', 340)
  assert.equal(vp2.from, -1, '数据不足时 from 应为 -1（左侧留白）')

  // dataLength=10, visibleBars=10 (1mo, contentWidth=340)
  // from = max(-2, 10-10-1) = max(-2, -1) = -1
  const vp3 = computeMiniKlineViewport(10, '1mo', 340)
  assert.equal(vp3.from, -1, '数据很少时 from 应为 -1')
})

// ===== 6. to = dataLength - 1 + 3（右侧 3 bar 留白） =====
test('to = dataLength - 1 + RIGHT_PADDING_BARS', () => {
  const vp = computeMiniKlineViewport(80, '1d', 340)
  assert.equal(vp.to, 80 - 1 + RIGHT_PADDING_BARS, 'to 应为 82')
  const rightPadding = vp.to - (80 - 1)
  assert.equal(rightPadding, RIGHT_PADDING_BARS, '右侧空位应为 3')
})

// ===== 7. 空数据返回零区间 =====
test('dataLength=0 返回 visibleBars=0, from=0, to=0', () => {
  const vp = computeMiniKlineViewport(0, '1d', 340)
  assert.equal(vp.visibleBars, 0)
  assert.equal(vp.from, 0)
  assert.equal(vp.to, 0)
  assert.equal(vp.barSpacing, 0)
})

// ===== 8. contentWidth 整数化（亚像素不抖动） =====
test('contentWidth 浮点数被 floor 整数化', () => {
  const vp1 = computeMiniKlineViewport(200, '1d', 340.7)
  const vp2 = computeMiniKlineViewport(200, '1d', 340.2)
  assert.equal(vp1.visibleBars, vp2.visibleBars, '340.7 和 340.2 应产生相同结果')
  assert.equal(vp1.effectivePlotWidth, vp2.effectivePlotWidth, 'effectivePlotWidth 应一致')
})

// ===== 9. 价格轴宽度固定 56 =====
test('priceScaleWidth 固定为 MIN_PRICE_SCALE_WIDTH=56', () => {
  for (const tf of ['15m', '1h', '1d', '1w', '1mo'] as MiniKlineTimeframe[]) {
    const vp = computeMiniKlineViewport(200, tf, 340)
    assert.equal(vp.priceScaleWidth, MIN_PRICE_SCALE_WIDTH, `${tf} priceScaleWidth 应为 56`)
  }
})

// ===== 10. computeVisiblePriceRange 正确计算 min/max =====
test('computeVisiblePriceRange 返回 min(low) 和 max(high)', () => {
  const bars = [
    { high: 10.5, low: 9.8 },
    { high: 11.2, low: 10.1 },
    { high: 10.8, low: 9.5 },
  ]
  const range = computeVisiblePriceRange(bars)
  assert.ok(range, '应返回非空 range')
  assert.equal(range!.minLow, 9.5, 'minLow 应为 9.5')
  assert.equal(range!.maxHigh, 11.2, 'maxHigh 应为 11.2')
})

test('computeVisiblePriceRange 空数组返回 null', () => {
  const range = computeVisiblePriceRange([])
  assert.equal(range, null)
})

// ===== 11. computeAutoscaleRange 上方 12%、下方 15% 扩展 =====
test('computeAutoscaleRange 扩展价格范围：上方 12%，下方 15%', () => {
  // minLow=10, maxHigh=20, range=10
  // 上方 12%: maxHigh + 10*0.12 = 21.2
  // 下方 15%: minLow - 10*0.15 = 8.5
  const result = computeAutoscaleRange(10, 20)
  assert.ok(result, '应返回非空')
  assert.equal(result!.min, 8.5, 'min 应为 8.5（下方 15%）')
  assert.equal(result!.max, 21.2, 'max 应为 21.2（上方 12%）')
})

test('computeAutoscaleRange 价格无波动时扩展 1%', () => {
  const result = computeAutoscaleRange(15, 15)
  assert.ok(result, '应返回非空')
  assert.ok(result!.min < 15, 'min 应小于 15')
  assert.ok(result!.max > 15, 'max 应大于 15')
})

test('computeAutoscaleRange 无效输入返回 null', () => {
  assert.equal(computeAutoscaleRange(NaN, 20), null)
  assert.equal(computeAutoscaleRange(10, Infinity), null)
})

// ===== 12. 五周期完整 viewport 验证（无截图契约） =====
test('五周期完整 viewport 验证（contentWidth=340, dataLength=80）', () => {
  const contentWidth = 340
  const dataLength = 80

  // 各周期预期值
  const cases: Array<{
    tf: MiniKlineTimeframe
    expectedVisible: number
    expectedFrom: number
    expectedTo: number
  }> = [
    // 15m: visibleBars=48, from=max(-2,80-48-1)=31, to=82
    { tf: '15m', expectedVisible: 48, expectedFrom: 31, expectedTo: 82 },
    // 1h: visibleBars=44, from=max(-2,80-44-1)=35, to=82
    { tf: '1h', expectedVisible: 44, expectedFrom: 35, expectedTo: 82 },
    // 1d: visibleBars=40, from=max(-2,80-40-1)=39, to=82
    { tf: '1d', expectedVisible: 40, expectedFrom: 39, expectedTo: 82 },
    // 1w: visibleBars=36, from=max(-2,80-36-1)=43, to=82
    { tf: '1w', expectedVisible: 36, expectedFrom: 43, expectedTo: 82 },
    // 1mo: visibleBars=30, from=max(-2,80-30-1)=49, to=82
    { tf: '1mo', expectedVisible: 30, expectedFrom: 49, expectedTo: 82 },
  ]

  for (const { tf, expectedVisible, expectedFrom, expectedTo } of cases) {
    const vp = computeMiniKlineViewport(dataLength, tf, contentWidth)
    assert.equal(vp.visibleBars, expectedVisible, `${tf} visibleBars`)
    assert.equal(vp.from, expectedFrom, `${tf} from`)
    assert.equal(vp.to, expectedTo, `${tf} to`)
    // 右侧空位 = to - (dataLength - 1) = 3
    assert.equal(vp.to - (dataLength - 1), RIGHT_PADDING_BARS, `${tf} 右侧空位`)
  }
})

// ===== 13. effectivePlotWidth = floor(contentWidth) - priceScaleWidth =====
test('effectivePlotWidth = floor(contentWidth) - 56', () => {
  const vp = computeMiniKlineViewport(200, '1d', 340)
  assert.equal(vp.effectivePlotWidth, 340 - MIN_PRICE_SCALE_WIDTH, 'effectivePlotWidth 应为 284')
})

// ===== 14. visibleBars 不超过 dataLength =====
test('visibleBars 不超过 dataLength', () => {
  // dataLength=20, 1d targetRoots=40, 但 dataLength < targetRoots
  // visibleBars = min(40, 20) = 20
  const vp = computeMiniKlineViewport(20, '1d', 340)
  assert.equal(vp.visibleBars, 20, 'visibleBars 不应超过 dataLength')
  // from = max(-2, 20-20-1) = max(-2, -1) = -1
  assert.equal(vp.from, -1, '数据不足时 from 应为 -1')
})

// ===== 15. LEFT_PADDING_BARS 常量 =====
test('LEFT_PADDING_BARS = 2', () => {
  assert.equal(LEFT_PADDING_BARS, 2, '左侧留白应为 2 根')
})
