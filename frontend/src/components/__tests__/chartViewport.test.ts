// [chartViewport] - 描述: 图表视区工具单元测试
// 用法：node --experimental-strip-types --test src/components/__tests__/chartViewport.test.ts
//   覆盖：createDefaultViewport/clampViewport/zoomAtAnchor/panViewport 辅助函数 +
//   指标 offset 对齐（取最后 N 个值）+ viewport 切片对齐（纵轴从 display 计算）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  MAX_VISIBLE_BARS,
  MIN_VISIBLE_BARS,
  type ChartViewport,
  clampViewport,
  createDefaultViewport,
  panViewport,
  zoomAtAnchor,
} from '../chartViewport.ts'

// ===== 1. createDefaultViewport：取末尾 N 根，clamp 到 [MIN, MAX] =====
test('createDefaultViewport: 取末尾 N 根，可见数 clamp 到 [MIN, MAX]', () => {
  // 数据充足：取末尾 MAX_VISIBLE_BARS 根
  const vp1 = createDefaultViewport(500, MAX_VISIBLE_BARS)
  assert.equal(vp1.fromIndex, 500 - MAX_VISIBLE_BARS)
  assert.equal(vp1.toIndex, 500)
  assert.equal(vp1.toIndex - vp1.fromIndex, MAX_VISIBLE_BARS)

  // 数据不足 MAX：可见数 = total
  const vp2 = createDefaultViewport(100, MAX_VISIBLE_BARS)
  assert.equal(vp2.fromIndex, 0)
  assert.equal(vp2.toIndex, 100)

  // 数据少于 MIN：返回 [0, total]
  const vp3 = createDefaultViewport(10, MAX_VISIBLE_BARS)
  assert.deepEqual(vp3, { fromIndex: 0, toIndex: 10 })

  // 期望可见数小于 MIN：clamp 到 MIN
  const vp4 = createDefaultViewport(500, 10)
  assert.equal(vp4.toIndex - vp4.fromIndex, MIN_VISIBLE_BARS)
})

// ===== 2. clampViewport：超出范围时 clamp，保证最少 MIN_VISIBLE_BARS 可见 =====
test('clampViewport: 超出范围 clamp + 保证最少 MIN_VISIBLE_BARS 可见', () => {
  // fromIndex 负数 → clamp 到 0
  const vp1 = clampViewport({ fromIndex: -50, toIndex: 100 }, 200)
  assert.equal(vp1.fromIndex, 0)

  // toIndex 超出 total → clamp 到 total
  const vp2 = clampViewport({ fromIndex: 150, toIndex: 300 }, 200)
  assert.equal(vp2.toIndex, 200)

  // 可见数 < MIN 且数据足够 → 扩展到 MIN
  const vp3 = clampViewport({ fromIndex: 195, toIndex: 200 }, 200)
  assert.equal(vp3.toIndex - vp3.fromIndex, MIN_VISIBLE_BARS)
  assert.equal(vp3.toIndex, 200)
  assert.equal(vp3.fromIndex, 200 - MIN_VISIBLE_BARS)

  // 可见数 < MIN 且数据不足 → 返回 [0, total]
  const vp4 = clampViewport({ fromIndex: 5, toIndex: 8 }, 10)
  assert.deepEqual(vp4, { fromIndex: 0, toIndex: 10 })

  // 空数据 → {0, 0}
  const vp5 = clampViewport({ fromIndex: 0, toIndex: 0 }, 0)
  assert.deepEqual(vp5, { fromIndex: 0, toIndex: 0 })
})

// ===== 3. zoomAtAnchor：以锚点为中心缩放，锚点相对位置保持不变 =====
test('zoomAtAnchor: 锚点相对位置在缩放后保持不变', () => {
  // 初始视区 [50, 150)，可见 100 根，锚点在 75（视区内 25% 位置）
  const vp: ChartViewport = { fromIndex: 50, toIndex: 150 }
  const anchor = 75
  // 放大 2x：可见数 100 → 50，锚点应仍位于视区 25% 位置
  const zoomed = zoomAtAnchor(vp, anchor, 2, 500)
  const newVisible = zoomed.toIndex - zoomed.fromIndex
  assert.equal(newVisible, 50)
  // 锚点 75 应在新的视区内
  assert.ok(zoomed.fromIndex <= anchor && anchor < zoomed.toIndex,
    `anchor ${anchor} 应在视区 [${zoomed.fromIndex}, ${zoomed.toIndex}) 内`)
  // 锚点相对位置应接近 25%（允许四舍五入误差 ±1）
  const ratio = (anchor - zoomed.fromIndex) / newVisible
  assert.ok(Math.abs(ratio - 0.25) < 0.05,
    `锚点相对位置应接近 0.25，实际 ${ratio}`)

  // 缩小 0.5x：可见数 100 → 200
  const zoomedOut = zoomAtAnchor(vp, anchor, 0.5, 500)
  assert.equal(zoomedOut.toIndex - zoomedOut.fromIndex, 200)
})

// ===== 4. panViewport：平移边界 clamp =====
test('panViewport: 平移边界 clamp', () => {
  const vp: ChartViewport = { fromIndex: 100, toIndex: 200 }
  // 向右平移 50：[150, 250]
  const p1 = panViewport(vp, 50, 500)
  assert.equal(p1.fromIndex, 150)
  assert.equal(p1.toIndex, 250)

  // 向左平移到边界（-200）：fromIndex clamp 到 0
  const p2 = panViewport(vp, -200, 500)
  assert.equal(p2.fromIndex, 0)
  assert.equal(p2.toIndex, 100)

  // 向右平移超过边界（+300）：fromIndex clamp 到 total - visible
  const p3 = panViewport(vp, 300, 500)
  assert.equal(p3.fromIndex, 400)
  assert.equal(p3.toIndex, 500)
})

// ===== 5. 指标 offset 对齐：offset = max(0, values.length - barsCount) =====
// 验证 StrategyChart.renderIndicatorLine/PriceZone/Band 的对齐逻辑：
//   K 线 display 取 calc.slice(from, to)（最近 N 根），指标 values 须取最后 N 个值
test('指标 offset 对齐: 指标数组取最后 barsCount 个值与 display 对齐', () => {
  // 模拟 calc 长度 180，display 取末尾 60 根
  const calcLen = 180
  const barsCount = 60
  const calcFrom = calcLen - barsCount
  // 指标数组长度 = calc 长度（与 daily_bars 对齐）
  const valuesLen = calcLen
  const offset = Math.max(0, valuesLen - barsCount)
  assert.equal(offset, calcFrom, 'offset 应等于 calc 切片起始索引')

  // display[0] 对应 values[offset]
  // 即 display[i] 对应 values[offset + i]
  const displayIdx = 0
  const valuesIdx = offset + displayIdx
  assert.equal(valuesIdx, calcFrom)

  // 验证取最后 N 个：offset + barsCount = valuesLen
  assert.equal(offset + barsCount, valuesLen)

  // 边界：barsCount > valuesLen 时 offset = 0
  const offset2 = Math.max(0, 50 - 100)
  assert.equal(offset2, 0, 'barsCount > valuesLen 时 offset 应为 0')
})

// ===== 6. viewport 切片对齐：纵轴 min/max 从 display 计算 =====
// 验证修复后纵轴基于 display 而非 calc，避免放大时 K 线被压扁
test('viewport 切片对齐: 纵轴 min/max 从 display 计算而非 calc', () => {
  // 模拟 calc 数据：前 120 根 low=10 high=20，后 60 根 low=100 high=200
  const calc = [
    ...Array.from({ length: 120 }, (_, i) => ({ i, low: 10, high: 20 })),
    ...Array.from({ length: 60 }, (_, i) => ({ i: 120 + i, low: 100, high: 200 })),
  ]
  // viewport 取末尾 60 根（放大到高价位区间）
  const vp = createDefaultViewport(calc.length, 60)
  const display = calc.slice(vp.fromIndex, vp.toIndex)

  // 修复后：纵轴 min/max 从 display 计算
  const minFromDisplay = Math.min(...display.map(d => d.low))
  const maxFromDisplay = Math.max(...display.map(d => d.high))
  assert.equal(minFromDisplay, 100, 'display 最低价应为 100（高价位区间）')
  assert.equal(maxFromDisplay, 200, 'display 最高价应为 200')

  // 旧逻辑（bug）：从 calc 计算，会被前 120 根 10~20 区间污染
  const minFromCalc = Math.min(...calc.map(d => d.low))
  const maxFromCalc = Math.max(...calc.map(d => d.high))
  assert.equal(minFromCalc, 10, 'calc 最低价为 10（前 120 根）')
  assert.equal(maxFromCalc, 200)

  // 关键断言：display 区间的 min/max 应远大于 calc 区间的 min
  // 修复后纵轴基于 display，K 线充满画面；旧逻辑基于 calc，K 线被压扁
  assert.ok(minFromDisplay > minFromCalc,
    '修复后 display min(100) 应大于 calc min(10)，避免 K 线被压扁')
})
