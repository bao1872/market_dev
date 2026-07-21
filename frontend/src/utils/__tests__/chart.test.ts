// [Chart] - 描述: chart.ts 工具函数单元测试
// 用法：node --experimental-strip-types --test src/utils/__tests__/chart.test.ts
//   覆盖：mapBarsToBarData
//
// [CH-03 fix] 已移除 mergeRealtimeQuoteIntoBars 测试（函数已删除）。
// PRD §3.3: 前端 quote 不再构造/修改 K 线，MDAS 是唯一 Bar 真源。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import type { Bar } from '@/api/endpoints'
import { mapBarsToBarData } from '../chart.ts'

// ===== 1. mapBarsToBarData =====
test('mapBarsToBarData: undefined 返回空数组并正确映射字段', () => {
  assert.deepEqual(mapBarsToBarData(undefined), [])

  const bars: Bar[] = [
    {
      instrument_id: 'id',
      trade_date: '2026-06-24',
      trade_time: null,
      open: 10,
      high: 11,
      low: 9,
      close: 10.5,
      volume: 1000,
      amount: 10000,
      adj_factor: 1,
    },
    {
      instrument_id: 'id',
      trade_time: '2026-06-25T10:30:00',
      trade_date: null,
      open: 10.5,
      high: 12,
      low: 10,
      close: 11.5,
      volume: 2000,
      amount: 20000,
      adj_factor: 1,
    },
  ]

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

// ===== 2. mapBarsToBarData: 空数组返回空数组 =====
test('mapBarsToBarData: 空数组返回空数组', () => {
  assert.deepEqual(mapBarsToBarData([]), [])
})
