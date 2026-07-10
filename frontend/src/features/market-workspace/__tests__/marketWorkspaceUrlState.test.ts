// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts
//
// 覆盖：
//   1. decode 默认值（无参数时 scope=watchlist, symbol=null, timeframe=1d）
//   2. decode scope=market
//   3. decode symbol + timeframe
//   4. encode→decode 往返一致
//   5. symbol=null 时 encode 不包含 symbol 参数
//   6. timeframe=1d（默认）时 encode 省略 timeframe
//   7. buildMarketWorkspaceUrl 生成完整 URL

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  buildMarketWorkspaceUrl,
  DEFAULT_MARKET_SCOPE,
  DEFAULT_TIMEFRAME,
  type MarketWorkspaceUrlState,
} from '../marketWorkspaceUrlState.ts'

test('decode 默认值（无参数时 scope=watchlist, symbol=null, timeframe=1d）', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams())
  assert.equal(state.scope, DEFAULT_MARKET_SCOPE)
  assert.equal(state.symbol, null)
  assert.equal(state.timeframe, DEFAULT_TIMEFRAME)
})

test('decode scope=market', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('scope=market'))
  assert.equal(state.scope, 'market')
})

test('decode symbol + timeframe', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('scope=watchlist&symbol=000001.SZ&timeframe=15m'))
  assert.equal(state.scope, 'watchlist')
  assert.equal(state.symbol, '000001.SZ')
  assert.equal(state.timeframe, '15m')
})

test('encode→decode 往返一致', () => {
  const original: MarketWorkspaceUrlState = {
    scope: 'market',
    symbol: '600519.SH',
    timeframe: '1h',
  }
  const encoded = encodeMarketWorkspaceUrl(original)
  const decoded = decodeMarketWorkspaceUrl(encoded)
  assert.deepStrictEqual(decoded, original)
})

test('symbol=null 时 encode 不包含 symbol 参数', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'watchlist', symbol: null, timeframe: '1d' })
  assert.ok(!params.has('symbol'))
})

test('timeframe=1d（默认）时 encode 省略 timeframe', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'watchlist', symbol: '000001.SZ', timeframe: '1d' })
  assert.ok(!params.has('timeframe'))
})

test('buildMarketWorkspaceUrl 生成完整 URL', () => {
  const url = buildMarketWorkspaceUrl({ scope: 'market', symbol: '000001.SZ', timeframe: '15m' })
  assert.equal(url, '/market?scope=market&symbol=000001.SZ&timeframe=15m')
})

test('buildMarketWorkspaceUrl 无 symbol 时生成简洁 URL', () => {
  const url = buildMarketWorkspaceUrl({ scope: 'watchlist', symbol: null, timeframe: '1d' })
  assert.equal(url, '/market?scope=watchlist')
})
