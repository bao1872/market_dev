// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts
//
// 覆盖：
//   1. decode 默认值（无参数时 scope=watchlist, symbol=null, timeframe=1d, source=watchlist, strategy=watchlist_monitor, eventId=null）
//   2. decode scope=market
//   3. decode symbol + timeframe + source + strategy + event_id
//   4. 非法 timeframe 回退 1d
//   5. 非法 source 回退 watchlist
//   6. encode→decode 往返一致（含 source/strategy/event_id）
//   7. symbol=null 时 encode 不包含 symbol 参数
//   8. timeframe=1d（默认）时 encode 省略 timeframe
//   9. source=watchlist（默认）时 encode 省略 source
//  10. strategy 等于 source 默认值时 encode 省略 strategy
//  11. event_id=null 时 encode 不包含 event_id
//  12. buildMarketWorkspaceUrl 生成完整 URL
//  13. defaultStrategyForSource：watchlist→watchlist_monitor, selection→dsa_selector

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  buildMarketWorkspaceUrl,
  defaultStrategyForSource,
  DEFAULT_MARKET_SCOPE,
  DEFAULT_TIMEFRAME,
  DEFAULT_SOURCE,
  type MarketWorkspaceUrlState,
} from '../marketWorkspaceUrlState.ts'

test('decode 默认值（无参数时 scope=watchlist, symbol=null, timeframe=1d, source=watchlist, strategy=watchlist_monitor, eventId=null）', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams())
  assert.equal(state.scope, DEFAULT_MARKET_SCOPE)
  assert.equal(state.symbol, null)
  assert.equal(state.timeframe, DEFAULT_TIMEFRAME)
  assert.equal(state.source, DEFAULT_SOURCE)
  assert.equal(state.strategy, 'watchlist_monitor')
  assert.equal(state.eventId, null)
})

test('decode scope=market', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('scope=market'))
  assert.equal(state.scope, 'market')
})

test('decode symbol + timeframe + source + strategy + event_id', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams(
    'scope=watchlist&symbol=000001.SZ&timeframe=15m&source=selection&strategy=dsa_selector&event_id=evt-123',
  ))
  assert.equal(state.scope, 'watchlist')
  assert.equal(state.symbol, '000001.SZ')
  assert.equal(state.timeframe, '15m')
  assert.equal(state.source, 'selection')
  assert.equal(state.strategy, 'dsa_selector')
  assert.equal(state.eventId, 'evt-123')
})

test('非法 timeframe 回退 1d', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('timeframe=5min'))
  assert.equal(state.timeframe, '1d')
})

test('非法 source 回退 watchlist', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('source=invalid'))
  assert.equal(state.source, 'watchlist')
})

test('encode→decode 往返一致（含 source/strategy/event_id）', () => {
  const original: MarketWorkspaceUrlState = {
    scope: 'market',
    symbol: '600519.SH',
    timeframe: '1h',
    source: 'selection',
    strategy: 'dsa_selector',
    eventId: 'evt-456',
  }
  const encoded = encodeMarketWorkspaceUrl(original)
  const decoded = decodeMarketWorkspaceUrl(encoded)
  assert.deepStrictEqual(decoded, original)
})

test('symbol=null 时 encode 不包含 symbol 参数', () => {
  const params = encodeMarketWorkspaceUrl({
    scope: 'watchlist', symbol: null, timeframe: '1d', source: 'watchlist', strategy: 'watchlist_monitor', eventId: null,
  })
  assert.ok(!params.has('symbol'))
})

test('timeframe=1d（默认）时 encode 省略 timeframe', () => {
  const params = encodeMarketWorkspaceUrl({
    scope: 'watchlist', symbol: '000001.SZ', timeframe: '1d', source: 'watchlist', strategy: 'watchlist_monitor', eventId: null,
  })
  assert.ok(!params.has('timeframe'))
})

test('source=watchlist（默认）时 encode 省略 source', () => {
  const params = encodeMarketWorkspaceUrl({
    scope: 'watchlist', symbol: '000001.SZ', timeframe: '1d', source: 'watchlist', strategy: 'watchlist_monitor', eventId: null,
  })
  assert.ok(!params.has('source'))
})

test('strategy 等于 source 默认值时 encode 省略 strategy', () => {
  // source=watchlist 默认 strategy=watchlist_monitor → 省略
  const paramsWatchlist = encodeMarketWorkspaceUrl({
    scope: 'watchlist', symbol: '000001.SZ', timeframe: '1d', source: 'watchlist', strategy: 'watchlist_monitor', eventId: null,
  })
  assert.ok(!paramsWatchlist.has('strategy'))

  // source=selection 默认 strategy=dsa_selector → 省略
  const paramsSelection = encodeMarketWorkspaceUrl({
    scope: 'market', symbol: '000001.SZ', timeframe: '1d', source: 'selection', strategy: 'dsa_selector', eventId: null,
  })
  assert.ok(!paramsSelection.has('strategy'))
})

test('event_id=null 时 encode 不包含 event_id', () => {
  const params = encodeMarketWorkspaceUrl({
    scope: 'watchlist', symbol: '000001.SZ', timeframe: '1d', source: 'watchlist', strategy: 'watchlist_monitor', eventId: null,
  })
  assert.ok(!params.has('event_id'))
})

test('buildMarketWorkspaceUrl 生成完整 URL（strategy 等于 source 默认值时省略）', () => {
  const url = buildMarketWorkspaceUrl({
    scope: 'market', symbol: '000001.SZ', timeframe: '15m', source: 'selection', strategy: 'dsa_selector', eventId: 'evt-789',
  })
  // source=selection 默认 strategy=dsa_selector，等于默认值故省略 strategy 参数
  assert.equal(url, '/market?scope=market&symbol=000001.SZ&timeframe=15m&source=selection&event_id=evt-789')
})

test('buildMarketWorkspaceUrl strategy 非默认时写入 URL', () => {
  const url = buildMarketWorkspaceUrl({
    scope: 'market', symbol: '000001.SZ', timeframe: '15m', source: 'watchlist', strategy: 'dsa_selector', eventId: 'evt-789',
  })
  // source=watchlist 默认 strategy=watchlist_monitor，传入 dsa_selector 非默认故写入
  assert.equal(url, '/market?scope=market&symbol=000001.SZ&timeframe=15m&strategy=dsa_selector&event_id=evt-789')
})

test('buildMarketWorkspaceUrl 无 symbol 时生成简洁 URL', () => {
  const url = buildMarketWorkspaceUrl({
    scope: 'watchlist', symbol: null, timeframe: '1d', source: 'watchlist', strategy: 'watchlist_monitor', eventId: null,
  })
  assert.equal(url, '/market?scope=watchlist')
})

test('defaultStrategyForSource：watchlist→watchlist_monitor, selection→dsa_selector', () => {
  assert.equal(defaultStrategyForSource('watchlist'), 'watchlist_monitor')
  assert.equal(defaultStrategyForSource('selection'), 'dsa_selector')
})

test('选择新股票时清除旧 event_id（encode eventId=null 不写入 event_id）', () => {
  // 模拟 handleSelectSymbol：新 state eventId=null
  const params = encodeMarketWorkspaceUrl({
    scope: 'watchlist', symbol: '600519.SH', timeframe: '1d', source: 'watchlist', strategy: 'watchlist_monitor', eventId: null,
  })
  assert.ok(!params.has('event_id'))
  assert.equal(params.get('symbol'), '600519.SH')
})
