// [Chart] - 描述: chart.ts 工具函数单元测试
// 用法：node --experimental-strip-types --test src/utils/__tests__/chart.test.ts
//   覆盖：mapBarsToBarData / mergeRealtimeQuoteIntoBars

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import type { Bar, QuoteResponse } from '@/api/endpoints'
import type { BarData } from '@/components/StrategyChart'
import { mapBarsToBarData, mergeRealtimeQuoteIntoBars } from '../chart.ts'

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

// ===== 2. mergeRealtimeQuoteIntoBars：边界条件 =====
test('mergeRealtimeQuoteIntoBars: 空数组或无 quote 时原样返回', () => {
  const bars: BarData[] = [
    { time: '2026-06-24', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
  ]

  assert.deepEqual(mergeRealtimeQuoteIntoBars(bars, undefined), bars)
  assert.deepEqual(
    mergeRealtimeQuoteIntoBars([], {
      current_price: 11,
      update_time: '2026-06-25',
    } as QuoteResponse),
    [],
  )
  assert.deepEqual(
    mergeRealtimeQuoteIntoBars(bars, {
      current_price: null,
      update_time: '2026-06-25',
    } as unknown as QuoteResponse),
    bars,
  )
})

// [QuoteTrust] - 描述: 仅当 quote.is_realtime=true && source=pytdx && freshness_seconds<=60 时才合并
test('mergeRealtimeQuoteIntoBars: 不可信 quote 不合并到 K 线', () => {
  const bars: BarData[] = [
    { time: '2026-06-24', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
  ]

  // daily_fallback 不可信
  assert.deepEqual(
    mergeRealtimeQuoteIntoBars(bars, {
      current_price: 11.5,
      update_time: '2026-06-24T15:00:00',
      source: 'daily_fallback',
      is_realtime: false,
      freshness_seconds: 0,
    } as QuoteResponse),
    bars,
  )

  // is_realtime=false 不可信
  assert.deepEqual(
    mergeRealtimeQuoteIntoBars(bars, {
      current_price: 11.5,
      update_time: '2026-06-24T15:00:00',
      source: 'pytdx',
      is_realtime: false,
      freshness_seconds: 0,
    } as QuoteResponse),
    bars,
  )

  // freshness_seconds > 60 不可信
  assert.deepEqual(
    mergeRealtimeQuoteIntoBars(bars, {
      current_price: 11.5,
      update_time: '2026-06-24T15:00:00',
      source: 'pytdx',
      is_realtime: true,
      freshness_seconds: 61,
    } as QuoteResponse),
    bars,
  )

  // source 不是 pytdx 不可信
  assert.deepEqual(
    mergeRealtimeQuoteIntoBars(bars, {
      current_price: 11.5,
      update_time: '2026-06-24T15:00:00',
      source: 'other' as 'pytdx',
      is_realtime: true,
      freshness_seconds: 10,
    } as QuoteResponse),
    bars,
  )
})

// ===== 3. mergeRealtimeQuoteIntoBars：1d 保留日期语义 =====
test('mergeRealtimeQuoteIntoBars: 1d 保留日期语义，不改成 intraday timestamp', () => {
  const bars: BarData[] = [
    { time: '2026-06-23', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
    { time: '2026-06-24', open: 10.5, high: 11, low: 10, close: 10.8, volume: 200 },
  ]

  const quote = {
    instrument_id: 'id',
    symbol: 'TEST',
    name: 'Test',
    current_price: 11.5,
    open: 10,
    high: 12,
    low: 9,
    close: 11.5,
    volume: 1000,
    prev_close: 10,
    change_pct: 15,
    update_time: '2026-06-24T15:00:00',
    source: 'pytdx',
    is_realtime: true,
    freshness_seconds: 10,
    degraded: false,
    degraded_reason: null,
  } satisfies QuoteResponse

  // 默认 timeframe='1d'
  const merged = mergeRealtimeQuoteIntoBars(bars, quote)

  assert.equal(merged.length, 2)
  // 前一根不变
  assert.deepEqual(merged[0], bars[0])
  // 最后一根更新 close/high/low，但 1d 保留原日期
  assert.equal(merged[1].time, '2026-06-24')
  assert.equal(merged[1].close, 11.5)
  assert.equal(merged[1].high, 11.5)
  assert.equal(merged[1].low, 10)
  assert.equal(merged[1].open, 10.5)
  assert.equal(merged[1].volume, 200)
})

// ===== 3b. mergeRealtimeQuoteIntoBars：intraday 使用 quote.update_time =====
test('mergeRealtimeQuoteIntoBars: 15m 使用 quote.update_time 更新最后一根时间', () => {
  const bars: BarData[] = [
    { time: '2026-06-23', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
    { time: '2026-06-24T10:30:00', open: 10.5, high: 11, low: 10, close: 10.8, volume: 200 },
  ]

  const quote = {
    instrument_id: 'id',
    symbol: 'TEST',
    name: 'Test',
    current_price: 11.5,
    open: 10,
    high: 12,
    low: 9,
    close: 11.5,
    volume: 1000,
    prev_close: 10,
    change_pct: 15,
    update_time: '2026-06-24T15:00:00',
    source: 'pytdx',
    is_realtime: true,
    freshness_seconds: 10,
    degraded: false,
    degraded_reason: null,
  } satisfies QuoteResponse

  const merged = mergeRealtimeQuoteIntoBars(bars, quote, '15m')

  assert.equal(merged.length, 2)
  assert.deepEqual(merged[0], bars[0])
  assert.equal(merged[1].time, '2026-06-24T15:00:00')
  assert.equal(merged[1].close, 11.5)
  assert.equal(merged[1].high, 11.5)
  assert.equal(merged[1].low, 10)
  assert.equal(merged[1].open, 10.5)
  assert.equal(merged[1].volume, 200)
})

// ===== 3c. mergeRealtimeQuoteIntoBars：1d 跨日追加实时 bar =====
test('mergeRealtimeQuoteIntoBars: 1d 跨日时追加新实时 bar', () => {
  const bars: BarData[] = [
    { time: '2026-06-23', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
    { time: '2026-06-24', open: 10.5, high: 11, low: 10, close: 10.8, volume: 200 },
  ]

  const quote = {
    instrument_id: 'id',
    symbol: 'TEST',
    name: 'Test',
    current_price: 11.5,
    open: 10,
    high: 12,
    low: 9,
    close: 11.5,
    volume: 1000,
    prev_close: 10,
    change_pct: 15,
    update_time: '2026-06-25T10:30:00',
    source: 'pytdx',
    is_realtime: true,
    freshness_seconds: 10,
    degraded: false,
    degraded_reason: null,
  } satisfies QuoteResponse

  const merged = mergeRealtimeQuoteIntoBars(bars, quote, '1d')

  assert.equal(merged.length, 3)
  assert.deepEqual(merged[0], bars[0])
  assert.deepEqual(merged[1], bars[1])
  assert.equal(merged[2].time, '2026-06-25')
  assert.equal(merged[2].open, 10.8)
  assert.equal(merged[2].close, 11.5)
  assert.equal(merged[2].high, 11.5)
  assert.equal(merged[2].low, 10.8)
  assert.equal(merged[2].volume, 0)
})

// ===== 4. mergeRealtimeQuoteIntoBars：低价更新 low/high =====
test('mergeRealtimeQuoteIntoBars: 实时价低于 last.low 时更新 low', () => {
  const bars: BarData[] = [
    { time: '2026-06-24', open: 10, high: 11, low: 10, close: 10.5, volume: 100 },
  ]

  const merged = mergeRealtimeQuoteIntoBars(
    bars,
    {
      instrument_id: 'id',
      symbol: 'TEST',
      name: 'Test',
      current_price: 9.5,
      open: 10,
      high: 11,
      low: 9,
      close: 9.5,
      volume: 1000,
      prev_close: 10,
      change_pct: -5,
      update_time: '2026-06-24T15:00:00',
      source: 'pytdx',
      is_realtime: true,
      freshness_seconds: 10,
      degraded: false,
      degraded_reason: null,
    } satisfies QuoteResponse,
  )

  assert.equal(merged[0].close, 9.5)
  assert.equal(merged[0].low, 9.5)
  assert.equal(merged[0].high, 11)
})

// ===== 5. mergeRealtimeQuoteIntoBars：不修改原数组 =====
test('mergeRealtimeQuoteIntoBars: 返回新数组，不修改原数组', () => {
  const bars: BarData[] = [
    { time: '2026-06-24', open: 10, high: 11, low: 9, close: 10.5, volume: 100 },
  ]
  const original = bars[0]

  const merged = mergeRealtimeQuoteIntoBars(
    bars,
    {
      instrument_id: 'id',
      symbol: 'TEST',
      name: 'Test',
      current_price: 11.2,
      open: 10,
      high: 11,
      low: 9,
      close: 11.2,
      volume: 1000,
      prev_close: 10,
      change_pct: 12,
      update_time: '2026-06-24T15:00:00',
      source: 'pytdx',
      is_realtime: true,
      freshness_seconds: 10,
      degraded: false,
      degraded_reason: null,
    } satisfies QuoteResponse,
  )

  assert.notEqual(merged, bars)
  assert.equal(original.close, 10.5)
  assert.equal(merged[0].close, 11.2)
})
