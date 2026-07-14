// [MiniKlineViewport] - 描述: 小 K 线 viewport 纯函数测试（无截图契约）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/miniKlineViewport.test.ts
//
// 覆盖：
//  1. 五周期 clamp 区间正确
//  2. visible bars 按 floor((width - 56) / 5) 计算
//  3. from = max(0, dataLength - visibleBars)
//  4. to = dataLength - 1 + 3（右侧 3 bar 留白）
//  5. 空数据返回零区间
//  6. contentWidth 整数化（亚像素不抖动）
//  7. 价格轴宽度固定 56
//  8. computeVisiblePriceRange 正确计算 min/max

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  computeMiniKlineViewport,
  computeVisiblePriceRange,
  MIN_PRICE_SCALE_WIDTH,
  RIGHT_PADDING_BARS,
  type MiniKlineTimeframe,
} from '../miniKlineViewport.ts'

// ===== 1. 五周期 clamp 区间正确 =====
test('15m/1h clamp 50-64，1d 48-58，1w 40-52，1mo 30-40', () => {
  // 用极大 contentWidth 触发上界 clamp
  const cases: Array<{ tf: MiniKlineTimeframe; expected: number }> = [
    { tf: '15m', expected: 64 },
    { tf: '1h', expected: 64 },
    { tf: '1d', expected: 58 },
    { tf: '1w', expected: 52 },
    { tf: '1mo', expected: 40 },
  ]
  for (const { tf, expected } of cases) {
    const vp = computeMiniKlineViewport(200, tf, 5000)
    assert.equal(
      vp.visibleBars,
      expected,
      `${tf} 上界应为 ${expected}，实际 ${vp.visibleBars}`,
    )
  }
})

test('极窄宽度触发下界 clamp', () => {
  const cases: Array<{ tf: MiniKlineTimeframe; expected: number }> = [
    { tf: '15m', expected: 50 },
    { tf: '1h', expected: 50 },
    { tf: '1d', expected: 48 },
    { tf: '1w', expected: 40 },
    { tf: '1mo', expected: 30 },
  ]
  for (const { tf, expected } of cases) {
    const vp = computeMiniKlineViewport(200, tf, 10)
    assert.equal(
      vp.visibleBars,
      expected,
      `${tf} 下界应为 ${expected}，实际 ${vp.visibleBars}`,
    )
  }
})

// ===== 2. visible bars 按 floor((width - 56) / 5) 计算 =====
test('visibleBars = floor((contentWidth - 56) / 5)，落在 clamp 区间内', () => {
  // 选 contentWidth=400，effectivePlotWidth = 344，rawVisible = floor(344/5) = 68
  // 1d clamp [48,58] → 58；1mo clamp [30,40] → 40
  const vp1d = computeMiniKlineViewport(200, '1d', 400)
  assert.equal(vp1d.visibleBars, 58, '1d 应被 clamp 到 58')

  // 选 contentWidth=300，effectivePlotWidth = 244，rawVisible = floor(244/5) = 48
  // 1d clamp [48,58] → 48（恰好下界）
  const vp1d300 = computeMiniKlineViewport(200, '1d', 300)
  assert.equal(vp1d300.visibleBars, 48, '1d contentWidth=300 应得 48（下界）')

  // 1mo contentWidth=300: rawVisible=48，clamp [30,40] → 40
  const vp1mo = computeMiniKlineViewport(200, '1mo', 300)
  assert.equal(vp1mo.visibleBars, 40, '1mo contentWidth=300 应被 clamp 到 40')
})

// ===== 3. from = max(0, dataLength - visibleBars) =====
test('from = max(0, dataLength - visibleBars)，左侧数据不足时为 0', () => {
  // dataLength=200, visibleBars=58（1d contentWidth=400）→ from = 142
  const vp1 = computeMiniKlineViewport(200, '1d', 400)
  assert.equal(vp1.from, 200 - 58, 'from 应为 142')

  // dataLength=30, visibleBars=58 → from = 0（数据不足）
  const vp2 = computeMiniKlineViewport(30, '1d', 400)
  assert.equal(vp2.from, 0, '数据不足时 from 应为 0')
})

// ===== 4. to = dataLength - 1 + 3（右侧 3 bar 留白） =====
test('to = dataLength - 1 + RIGHT_PADDING_BARS，最新 K 线右侧保留 3 bar 空位', () => {
  const vp = computeMiniKlineViewport(80, '1d', 350)
  // dataLength=80 → to = 79 + 3 = 82
  assert.equal(vp.to, 80 - 1 + RIGHT_PADDING_BARS, 'to 应为 dataLength - 1 + 3')
  // latest logical index = dataLength - 1 = 79
  // 右侧空位 = to - latest = 82 - 79 = 3
  const latestIdx = 80 - 1
  const rightPadding = vp.to - latestIdx
  assert.ok(
    rightPadding >= 2 && rightPadding <= 4,
    `右侧空位应在 2-4 之间，实际 ${rightPadding}`,
  )
})

// ===== 5. 空数据返回零区间 =====
test('dataLength=0 返回 visibleBars=0, from=0, to=0', () => {
  const vp = computeMiniKlineViewport(0, '1d', 350)
  assert.equal(vp.visibleBars, 0)
  assert.equal(vp.from, 0)
  assert.equal(vp.to, 0)
})

// ===== 6. contentWidth 整数化（亚像素不抖动） =====
test('contentWidth 浮点数被 floor 整数化', () => {
  const vp1 = computeMiniKlineViewport(200, '1d', 350.7)
  const vp2 = computeMiniKlineViewport(200, '1d', 350.2)
  assert.equal(vp1.visibleBars, vp2.visibleBars, '350.7 和 350.2 应产生相同结果')
  // effectivePlotWidth = floor(350) - 56 = 294, rawVisible = floor(294/5) = 58
  assert.equal(vp1.visibleBars, 58, '350px 应得 58')
})

// ===== 7. 价格轴宽度固定 56 =====
test('priceScaleWidth 固定为 MIN_PRICE_SCALE_WIDTH=56', () => {
  for (const tf of ['15m', '1h', '1d', '1w', '1mo'] as MiniKlineTimeframe[]) {
    const vp = computeMiniKlineViewport(200, tf, 350)
    assert.equal(vp.priceScaleWidth, MIN_PRICE_SCALE_WIDTH, `${tf} priceScaleWidth 应为 56`)
  }
})

// ===== 8. computeVisiblePriceRange 正确计算 min/max =====
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

// ===== 9. 五周期分别验证（无截图契约） =====
test('五周期 viewport 边界验证', () => {
  // 模拟右栏 ~340px 宽度的常见场景
  const contentWidth = 340
  const dataLength = 80

  const cases: Array<{
    tf: MiniKlineTimeframe
    expectedVisible: number
    expectedFrom: number
    expectedTo: number
  }> = [
    // contentWidth=340, effectivePlotWidth = 284, rawVisible = floor(284/5) = 56
    // 15m clamp [50,64] → 56；dataLength=80 → from=24, to=82
    { tf: '15m', expectedVisible: 56, expectedFrom: 24, expectedTo: 82 },
    { tf: '1h', expectedVisible: 56, expectedFrom: 24, expectedTo: 82 },
    // 1d clamp [48,58] → 56；dataLength=80 → from=24, to=82
    { tf: '1d', expectedVisible: 56, expectedFrom: 24, expectedTo: 82 },
    // 1w clamp [40,52] → 52；dataLength=80 → from=28, to=82
    { tf: '1w', expectedVisible: 52, expectedFrom: 28, expectedTo: 82 },
    // 1mo clamp [30,40] → 40；dataLength=80 → from=40, to=82
    { tf: '1mo', expectedVisible: 40, expectedFrom: 40, expectedTo: 82 },
  ]

  for (const { tf, expectedVisible, expectedFrom, expectedTo } of cases) {
    const vp = computeMiniKlineViewport(dataLength, tf, contentWidth)
    assert.equal(
      vp.visibleBars,
      expectedVisible,
      `${tf} visibleBars 应为 ${expectedVisible}，实际 ${vp.visibleBars}`,
    )
    assert.equal(
      vp.from,
      expectedFrom,
      `${tf} from 应为 ${expectedFrom}，实际 ${vp.from}`,
    )
    assert.equal(
      vp.to,
      expectedTo,
      `${tf} to 应为 ${expectedTo}，实际 ${vp.to}`,
    )
    // 右侧空位 = to - (dataLength - 1) = 3
    const rightPadding = vp.to - (dataLength - 1)
    assert.equal(
      rightPadding,
      RIGHT_PADDING_BARS,
      `${tf} 右侧空位应为 ${RIGHT_PADDING_BARS}`,
    )
    // 校验 visible bars 在 clamp 区间内
    assert.ok(
      vp.visibleBars >= 30 && vp.visibleBars <= 64,
      `${tf} visibleBars 应在 [30, 64] 区间内`,
    )
  }
})

// ===== 10. effectivePlotWidth = floor(contentWidth) - priceScaleWidth =====
test('effectivePlotWidth = floor(contentWidth) - 56', () => {
  const vp = computeMiniKlineViewport(200, '1d', 350)
  assert.equal(vp.effectivePlotWidth, 350 - MIN_PRICE_SCALE_WIDTH, 'effectivePlotWidth 应为 294')
})
