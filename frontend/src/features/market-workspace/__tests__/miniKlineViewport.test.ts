// [MiniKlineViewport] - 描述: 小 K 线 viewport 纯函数测试（CHANGE-20260715-007）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/miniKlineViewport.test.ts
//
// CHANGE-20260715-007 覆盖：
//  1. 五周期目标根数：15m=48、60m=44、日=40、周=36、月=30
//  2. 极窄宽度时 visibleBars 按 MIN_BAR_SPACING clamp 减少
//  3. visibleBars 不超过 dataLength
//  4. contentWidth 整数化（亚像素不抖动）
//  5. 价格轴宽度固定 56
//  6. effectivePlotWidth = floor(contentWidth) - 56
//  7. clipBarsToVisible：返回最后 visibleBars 根
//  8. computeViewportRange：from=-2, to=dataLength-1+3
//  9. computeVisiblePriceRange 正确计算 min/max
// 10. computeAutoscaleRange 上方 12%、下方 15% 扩展

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  computeMiniKlineViewport,
  computeViewportRange,
  clipBarsToVisible,
  computeVisiblePriceRange,
  computeAutoscaleRange,
  MIN_PRICE_SCALE_WIDTH,
  RIGHT_PADDING_BARS,
  LEFT_PADDING_BARS,
  type MiniKlineTimeframe,
} from '../miniKlineViewport.ts'

// ===== 1. 五周期目标根数（大宽度不增加根数）=====
test('大宽度时五周期返回目标根数：15m=48, 1h=44, 1d=40, 1w=36, 1mo=30', () => {
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

// ===== 2. 极窄宽度触发 MIN_BAR_SPACING clamp =====
test('极窄宽度时 visibleBars 减少且不低于 1', () => {
  for (const tf of ['15m', '1h', '1d', '1w', '1mo'] as MiniKlineTimeframe[]) {
    const vp = computeMiniKlineViewport(200, tf, 100)
    assert.ok(
      vp.visibleBars >= 1 && vp.visibleBars <= 48,
      `${tf} 极窄宽度 visibleBars 应在 1-48 之间，实际 ${vp.visibleBars}`,
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
test('contentWidth=300 时 15m visibleBars 减少', () => {
  // contentWidth=300, effectivePlotWidth=244
  // 15m: barSpacing=244/48=5.08 (<5.5), 减少: floor(244/5.5)=44
  const vp15m = computeMiniKlineViewport(200, '15m', 300)
  assert.equal(vp15m.visibleBars, 44, '15m contentWidth=300 应减少到 44')

  // 1h: barSpacing=244/44=5.55 (>=5.5), 保持 44
  const vp1h = computeMiniKlineViewport(200, '1h', 300)
  assert.equal(vp1h.visibleBars, 44, '1h contentWidth=300 应保持 44')

  // 1d: barSpacing=244/40=6.1 (>=5.5), 保持 40
  const vp1d = computeMiniKlineViewport(200, '1d', 300)
  assert.equal(vp1d.visibleBars, 40, '1d contentWidth=300 应保持 40')
})

// ===== 5. 空数据返回 visibleBars=0 =====
test('dataLength=0 返回 visibleBars=0', () => {
  const vp = computeMiniKlineViewport(0, '1d', 340)
  assert.equal(vp.visibleBars, 0)
})

// ===== 6. contentWidth 整数化（亚像素不抖动） =====
test('contentWidth 浮点数被 floor 整数化', () => {
  const vp1 = computeMiniKlineViewport(200, '1d', 340.7)
  const vp2 = computeMiniKlineViewport(200, '1d', 340.2)
  assert.equal(vp1.visibleBars, vp2.visibleBars, '340.7 和 340.2 应产生相同结果')
  assert.equal(vp1.effectivePlotWidth, vp2.effectivePlotWidth, 'effectivePlotWidth 应一致')
})

// ===== 7. 价格轴宽度固定 56 =====
test('priceScaleWidth 固定为 MIN_PRICE_SCALE_WIDTH=56', () => {
  for (const tf of ['15m', '1h', '1d', '1w', '1mo'] as MiniKlineTimeframe[]) {
    const vp = computeMiniKlineViewport(200, tf, 340)
    assert.equal(vp.priceScaleWidth, MIN_PRICE_SCALE_WIDTH, `${tf} priceScaleWidth 应为 56`)
  }
})

// ===== 8. effectivePlotWidth = floor(contentWidth) - priceScaleWidth =====
test('effectivePlotWidth = floor(contentWidth) - 56', () => {
  const vp = computeMiniKlineViewport(200, '1d', 340)
  assert.equal(vp.effectivePlotWidth, 340 - MIN_PRICE_SCALE_WIDTH, 'effectivePlotWidth 应为 284')
})

// ===== 9. visibleBars 不超过 dataLength =====
test('visibleBars 不超过 dataLength', () => {
  // dataLength=20, 1d targetRoots=40, 但 dataLength < targetRoots
  // visibleBars = min(40, 20) = 20
  const vp = computeMiniKlineViewport(20, '1d', 340)
  assert.equal(vp.visibleBars, 20, 'visibleBars 不应超过 dataLength')
})

// ===== 10. computeMiniKlineViewport 不返回 from/to/barSpacing（已删除半实现）=====
test('computeMiniKlineViewport 返回对象不包含 from/to/barSpacing 字段', () => {
  const vp = computeMiniKlineViewport(80, '1d', 340)
  assert.ok(!('from' in vp), '不应包含 from 字段（已移至 computeViewportRange）')
  assert.ok(!('to' in vp), '不应包含 to 字段（已移至 computeViewportRange）')
  assert.ok(!('barSpacing' in vp), '不应包含 barSpacing 字段（半实现已删除）')
})

// ===== 11. clipBarsToVisible：返回最后 visibleBars 根 =====
test('clipBarsToVisible 返回最后 visibleBars 根数据', () => {
  const bars = Array.from({ length: 80 }, (_, i) => ({ idx: i }))
  const clipped = clipBarsToVisible(bars, 40)
  assert.equal(clipped.length, 40, '应返回 40 根')
  assert.equal(clipped[0].idx, 40, '第一根应为原数组第 40 项')
  assert.equal(clipped[39].idx, 79, '最后一根应为原数组第 79 项')
})

// ===== 12. clipBarsToVisible：visibleBars >= dataLength 时返回全部 =====
test('clipBarsToVisible 在 visibleBars >= dataLength 时返回全部（新数组）', () => {
  const bars = [{ a: 1 }, { a: 2 }, { a: 3 }]
  const clipped = clipBarsToVisible(bars, 10)
  assert.equal(clipped.length, 3, '应返回全部 3 根')
  assert.notEqual(clipped, bars, '应返回新数组而非原引用')
  assert.deepEqual(clipped, bars, '内容应一致')
})

// ===== 13. clipBarsToVisible：空数据或 visibleBars<=0 返回空数组 =====
test('clipBarsToVisible 空数据或 visibleBars<=0 返回空数组', () => {
  assert.deepEqual(clipBarsToVisible([], 40), [])
  assert.deepEqual(clipBarsToVisible([{ a: 1 }], 0), [])
  assert.deepEqual(clipBarsToVisible([{ a: 1 }], -1), [])
})

// ===== 14. computeViewportRange：from=-2, to=dataLength-1+3 =====
test('computeViewportRange 返回 from=-2, to=dataLength-1+3', () => {
  // dataLength=40: from=-2, to=40-1+3=42
  const range = computeViewportRange(40)
  assert.equal(range.from, -LEFT_PADDING_BARS, 'from 应为 -2')
  assert.equal(range.to, 40 - 1 + RIGHT_PADDING_BARS, 'to 应为 42')
})

// ===== 15. computeViewportRange：空数据返回 {from:0, to:0} =====
test('computeViewportRange 空数据返回 {from:0, to:0}', () => {
  const range = computeViewportRange(0)
  assert.equal(range.from, 0)
  assert.equal(range.to, 0)
})

// ===== 16. computeViewportRange：各周期裁剪后 range 验证 =====
test('五周期裁剪后 computeViewportRange 正确（contentWidth=340, dataLength=80）', () => {
  const contentWidth = 340
  const dataLength = 80
  const cases: Array<{
    tf: MiniKlineTimeframe
    expectedVisible: number
    expectedFrom: number
    expectedTo: number
  }> = [
    // 15m: visibleBars=48, clippedLength=48, from=-2, to=48-1+3=50
    { tf: '15m', expectedVisible: 48, expectedFrom: -2, expectedTo: 50 },
    // 1h: visibleBars=44, clippedLength=44, from=-2, to=44-1+3=46
    { tf: '1h', expectedVisible: 44, expectedFrom: -2, expectedTo: 46 },
    // 1d: visibleBars=40, clippedLength=40, from=-2, to=40-1+3=42
    { tf: '1d', expectedVisible: 40, expectedFrom: -2, expectedTo: 42 },
    // 1w: visibleBars=36, clippedLength=36, from=-2, to=36-1+3=38
    { tf: '1w', expectedVisible: 36, expectedFrom: -2, expectedTo: 38 },
    // 1mo: visibleBars=30, clippedLength=30, from=-2, to=30-1+3=32
    { tf: '1mo', expectedVisible: 30, expectedFrom: -2, expectedTo: 32 },
  ]

  for (const { tf, expectedVisible, expectedFrom, expectedTo } of cases) {
    const vp = computeMiniKlineViewport(dataLength, tf, contentWidth)
    assert.equal(vp.visibleBars, expectedVisible, `${tf} visibleBars`)
    const clipped = clipBarsToVisible(Array.from({ length: dataLength }), vp.visibleBars)
    const range = computeViewportRange(clipped.length)
    assert.equal(range.from, expectedFrom, `${tf} from`)
    assert.equal(range.to, expectedTo, `${tf} to`)
  }
})

// ===== 17. computeVisiblePriceRange 正确计算 min/max =====
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

// ===== 18. computeAutoscaleRange 上方 12%、下方 15% 扩展 =====
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

// ===== 19. LEFT_PADDING_BARS / RIGHT_PADDING_BARS 常量 =====
test('LEFT_PADDING_BARS = 2, RIGHT_PADDING_BARS = 3', () => {
  assert.equal(LEFT_PADDING_BARS, 2, '左侧留白应为 2 根')
  assert.equal(RIGHT_PADDING_BARS, 3, '右侧留白应为 3 根')
})
