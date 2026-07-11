// [个股详情导航] - 描述: 趋势选股/消息中心进入行情工作区的 URL 构建 + 返回路径契约测试
// 用法：node --experimental-strip-types --test src/pages/__tests__/detailNavigation.test.ts
//
// 覆盖：
//   1. Screener → /market URL 含 scope=market&symbol&source=selection&strategy&returnTo
//   2. Messages → /market URL 含 symbol&event_id
//   3. /stock/:symbol 兼容路由 URL
//   4. resolveBackPath 优先 returnTo
//   5. resolveBackPath 无 returnTo 时按 source fallback

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  buildMarketEntryFromScreener,
  buildMarketEntryFromMessage,
  buildStockDetailUrl,
  buildStockDetailState,
  resolveBackPath,
} from '../detailNavigation.ts'

test('Screener → /market URL 含 scope=market&symbol&source=selection&strategy&returnTo', () => {
  const symbol = '000001.SZ'
  const strategyKey = 'dsa_selector'
  const returnTo = '/screener?strategy=dsa_selector&keyword=新能源&page=2'
  const url = buildMarketEntryFromScreener(symbol, strategyKey, returnTo)
  assert.ok(url.startsWith('/market?'), `URL should start with /market?, got: ${url}`)
  const params = new URLSearchParams(url.slice(7))
  assert.equal(params.get('scope'), 'market')
  assert.equal(params.get('symbol'), '000001.SZ')
  assert.equal(params.get('source'), 'selection')
  assert.equal(params.get('strategy'), 'dsa_selector')
  assert.equal(params.get('returnTo'), returnTo)
})

test('Messages → /market URL 含 symbol&event_id', () => {
  const url = buildMarketEntryFromMessage('300308.SZ', 'evt-123')
  assert.ok(url.startsWith('/market?'), `URL should start with /market?, got: ${url}`)
  const params = new URLSearchParams(url.slice(7))
  assert.equal(params.get('symbol'), '300308.SZ')
  assert.equal(params.get('event_id'), 'evt-123')
})

test('/stock/:symbol 兼容路由 URL', () => {
  const url = buildStockDetailUrl('000001.SZ', 'selection', 'dsa_selector')
  assert.equal(url, '/stock/000001.SZ?source=selection&strategy=dsa_selector')
})

test('buildStockDetailState 携带 returnTo', () => {
  const state = buildStockDetailState('/screener?page=1')
  assert.deepStrictEqual(state, { returnTo: '/screener?page=1' })
})

test('resolveBackPath 优先 returnTo', () => {
  assert.equal(resolveBackPath('/screener?strategy=dsa_selector&page=2', 'selection'), '/screener?strategy=dsa_selector&page=2')
  assert.equal(resolveBackPath('/market?scope=watchlist', 'watchlist'), '/market?scope=watchlist')
})

test('resolveBackPath 无 returnTo 时按 source fallback', () => {
  assert.equal(resolveBackPath(undefined, 'selection'), '/screener')
  assert.equal(resolveBackPath(undefined, 'watchlist'), '/market?scope=watchlist')
  assert.equal(resolveBackPath(null, 'selection'), '/screener')
  assert.equal(resolveBackPath('', 'watchlist'), '/market?scope=watchlist')
})
