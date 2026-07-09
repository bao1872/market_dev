// [个股详情导航] - 描述: 趋势选股进入详情与返回的 URL/state 契约测试
// 用法：node --experimental-strip-types --test src/pages/__tests__/detailNavigation.test.ts
//
// 覆盖：
//   1. Screener goDetail 生成正确的个股详情 URL 并携带 returnTo state
//   2. StockDetail 返回优先使用 location.state.returnTo
//   3. StockDetail 无 returnTo 时按 source fallback

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  buildStockDetailUrl,
  buildStockDetailState,
  resolveBackPath,
} from '../detailNavigation.ts'

test('Screener goDetail 生成个股详情 URL 并携带当前 URL 作为 returnTo', () => {
  const symbol = '000001.SZ'
  const source = 'selection'
  const strategyKey = 'dsa_selector'
  const returnTo = '/screener?strategy=dsa_selector&keyword=新能源&page=2'

  const url = buildStockDetailUrl(symbol, source, strategyKey)
  const state = buildStockDetailState(returnTo)

  assert.equal(url, '/stock/000001.SZ?source=selection&strategy=dsa_selector')
  assert.deepStrictEqual(state, { returnTo })
})

test('StockDetail 返回优先使用 location.state.returnTo', () => {
  assert.equal(resolveBackPath('/screener?strategy=dsa_selector&page=2', 'selection'), '/screener?strategy=dsa_selector&page=2')
  assert.equal(resolveBackPath('/watchlist?tab=active', 'watchlist'), '/watchlist?tab=active')
})

test('StockDetail 无 returnTo 时按 source fallback', () => {
  assert.equal(resolveBackPath(undefined, 'selection'), '/screener')
  assert.equal(resolveBackPath(undefined, 'watchlist'), '/watchlist')
  assert.equal(resolveBackPath('', 'selection'), '/screener')
  assert.equal(resolveBackPath('', 'watchlist'), '/watchlist')
})
