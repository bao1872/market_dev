// [StockResearchTypes] - 描述: 股票研究共享类型与纯函数契约测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/stockResearchTypes.test.ts
//
// 覆盖：
//   1. ALLOWED_TIMEFRAMES 包含 5 个允许值且顺序固定
//   2. DEFAULT_TIMEFRAME = '1d'
//   3. DEFAULT_SOURCE = 'watchlist'
//   4. BARS_COUNT_BY_TIMEFRAME 与 Node Cluster 输入契约对齐（1d=250, 15m=4000, 1h=1200, 1w=260, 1mo=120）
//   5. defaultStrategyForSource：watchlist→watchlist_monitor, selection→dsa_selector
//   6. normalizeDisplayTimeframe：合法值原样返回
//   7. normalizeDisplayTimeframe：null 回退 1d
//   8. normalizeDisplayTimeframe：非法值回退 1d
//   9. normalizeDisplayTimeframe：空字符串回退 1d
//  10. normalizeResearchSource：selection 原样返回
//  11. normalizeResearchSource：null 回退 watchlist
//  12. normalizeResearchSource：非法值回退 watchlist
//  13. normalizeResearchSource：空字符串回退 watchlist

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  ALLOWED_TIMEFRAMES,
  DEFAULT_TIMEFRAME,
  DEFAULT_SOURCE,
  BARS_COUNT_BY_TIMEFRAME,
  defaultStrategyForSource,
  normalizeDisplayTimeframe,
  normalizeResearchSource,
} from '../stockResearchTypes.ts'

test('ALLOWED_TIMEFRAMES 包含 5 个允许值且顺序固定', () => {
  assert.deepEqual([...ALLOWED_TIMEFRAMES], ['15m', '1h', '1d', '1w', '1mo'])
})

test('DEFAULT_TIMEFRAME = 1d', () => {
  assert.equal(DEFAULT_TIMEFRAME, '1d')
})

test('DEFAULT_SOURCE = watchlist', () => {
  assert.equal(DEFAULT_SOURCE, 'watchlist')
})

test('BARS_COUNT_BY_TIMEFRAME 与 Node Cluster 输入契约对齐', () => {
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1d'], 250)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['15m'], 4000)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1h'], 1200)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1w'], 260)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1mo'], 120)
})

test('defaultStrategyForSource：watchlist→watchlist_monitor, selection→dsa_selector', () => {
  assert.equal(defaultStrategyForSource('watchlist'), 'watchlist_monitor')
  assert.equal(defaultStrategyForSource('selection'), 'dsa_selector')
})

test('normalizeDisplayTimeframe：合法值原样返回', () => {
  for (const tf of ALLOWED_TIMEFRAMES) {
    assert.equal(normalizeDisplayTimeframe(tf), tf)
  }
})

test('normalizeDisplayTimeframe：null 回退 1d', () => {
  assert.equal(normalizeDisplayTimeframe(null), '1d')
})

test('normalizeDisplayTimeframe：非法值回退 1d', () => {
  assert.equal(normalizeDisplayTimeframe('5min'), '1d')
  assert.equal(normalizeDisplayTimeframe('2d'), '1d')
  assert.equal(normalizeDisplayTimeframe('daily'), '1d')
})

test('normalizeDisplayTimeframe：空字符串回退 1d', () => {
  assert.equal(normalizeDisplayTimeframe(''), '1d')
})

test('normalizeResearchSource：selection 原样返回', () => {
  assert.equal(normalizeResearchSource('selection'), 'selection')
})

test('normalizeResearchSource：null 回退 watchlist', () => {
  assert.equal(normalizeResearchSource(null), 'watchlist')
})

test('normalizeResearchSource：非法值回退 watchlist', () => {
  assert.equal(normalizeResearchSource('invalid'), 'watchlist')
  assert.equal(normalizeResearchSource('watchlist'), 'watchlist')
})

test('normalizeResearchSource：空字符串回退 watchlist', () => {
  assert.equal(normalizeResearchSource(''), 'watchlist')
})
