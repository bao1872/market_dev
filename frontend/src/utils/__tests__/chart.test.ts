// [Chart] - 描述: chart.ts 工具函数单元测试
// 用法：node --experimental-strip-types --test src/utils/__tests__/chart.test.ts
//   覆盖：mapBarsToBarData / mergeRealtimeQuoteIntoBars

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { mapBarsToBarData, mergeRealtimeQuoteIntoBars } from '../chart.ts'

// ===== 1. mapBarsToBarData =====
test('mapBarsToBarData: undefined 返回空数组并正确映射字段', () => {
  assert.deepEqual(mapBarsToBarData(undefined), [])

  const bars = [
    {
      trade_date: '2026-06-24',
      trade_time: undefined,
      open: 10,
      high: 11,
      low: 9,
      close: 10.5,
      volume: 1000,
    },
    {
      trade_time: '2026-06-25T10:30:00',
      trade_date: undefined,
      open: 10.5,
      high: 12,
      low: 10,
      close: 11.5,
      volume: 2000,
    },
  ] as any[]

  const result = mapBarsToBarData(bars)
  assert.equal(result.length, 2)
  assert.equal(result[0].time, '2026-06-24')
  assert.equal(result[0].open, 10)
  assert.equal(result[0].high, 11)
  assert.equal(result[0].low, 9)
  assert.equal(result[0].close, 10.5)
  assert.equal(result[0].volume, 1000)
  assert.equal(result[1].time, '2026-06-25T10:30:00')
})

// ===== 2. mergeRealtimeQuoteIntoBars：边界条件 =====
test('mergeRealtimeQuoteIntoBars: 空数组或无 quote 时原样返回', () => {
  const bars = [{ time: '2026-06-24', open: 10, high: 11, low: 9, close: 10.5, volume: 100 }]

  assert.deepEqual(mergeRealtimeQuoteIntoBars(bars, undefined), bars)
  assert.deepEqual(mergeRealtimeQuoteIntoBars([], { current_price: 11, update_time: '2026-06-25' } as any), [])
  assert.deepEqual(
    mergeRealtimeQuoteIntoBars(bars, { current_price: null, update_time: '2026-06-25' } as any),
    bars,
  )
})

// ===== 3. mergeRealtimeQuoteIntoBars：价格合并逻辑 =====
test('mergeRealtimeQuoteIntoBars: 将最新价合并到最后一根 K 线', () => {
  const bars = [
    { time: '2026-06-23', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
    { time: '2026-06-24', open: 10.5, high: 11, low: 10, close: 10.8, volume: 200 },
  ]

  const quote = { current_price: 11.5, update_time: '2026-06-24T15:00:00' }
  const merged = mergeRealtimeQuoteIntoBars(bars as any, quote as any)

  assert.equal(merged.length, 2)
  // 前一根不变
  assert.deepEqual(merged[0], bars[0])
  // 最后一根更新 close/high/time，low 保持不变
  assert.equal(merged[1].time, '2026-06-24T15:00:00')
  assert.equal(merged[1].close, 11.5)
  assert.equal(merged[1].high, 11.5)
  assert.equal(merged[1].low, 10)
  assert.equal(merged[1].open, 10.5)
  assert.equal(merged[1].volume, 200)
})

// ===== 4. mergeRealtimeQuoteIntoBars：低价更新 low/high =====
test('mergeRealtimeQuoteIntoBars: 实时价低于 last.low 时更新 low', () => {
  const bars = [{ time: '2026-06-24', open: 10, high: 11, low: 10, close: 10.5, volume: 100 }]

  const merged = mergeRealtimeQuoteIntoBars(bars as any, { current_price: 9.5, update_time: '2026-06-24T15:00:00' } as any)

  assert.equal(merged[0].close, 9.5)
  assert.equal(merged[0].low, 9.5)
  assert.equal(merged[0].high, 11)
})

// ===== 5. mergeRealtimeQuoteIntoBars：不修改原数组 =====
test('mergeRealtimeQuoteIntoBars: 返回新数组，不修改原数组', () => {
  const bars = [{ time: '2026-06-24', open: 10, high: 11, low: 9, close: 10.5, volume: 100 }]
  const original = bars[0]

  const merged = mergeRealtimeQuoteIntoBars(bars as any, { current_price: 11.2, update_time: '2026-06-24T15:00:00' } as any)

  assert.notEqual(merged, bars)
  assert.equal(original.close, 10.5)
  assert.equal(merged[0].close, 11.2)
})
