// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts
//
// 覆盖（CHANGE-20260713-004：简化为仅 scope + selected）：
//   1. decode 默认值（无参数时 scope=watchlist, selected=null）
//   2. decode scope=market
//   3. decode selected
//   4. encode→decode 往返一致
//   5. selected=null 时 encode 不包含 selected
//   6. buildMarketWorkspaceUrl 生成完整 URL
//   7. selectInstrumentInTable：设置 selected，保留 scope
//   8. changeMarketScope：切换 scope 后清除 selected
//   9. normalizeInternalReturnTo: 仅允许 /screener /market /messages 前缀，拒绝 /stock
//  10. normalizeInternalReturnTo: 拒绝外部 URL/双斜杠/javascript/超长值

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  buildMarketWorkspaceUrl,
  selectInstrumentInTable,
  changeMarketScope,
  normalizeInternalReturnTo,
  DEFAULT_MARKET_SCOPE,
  type MarketWorkspaceUrlState,
} from '../marketWorkspaceUrlState.ts'

test('decode 默认值（无参数时 scope=watchlist, selected=null）', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams())
  assert.equal(state.scope, DEFAULT_MARKET_SCOPE)
  assert.equal(state.selected, null)
})

test('decode scope=market', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('scope=market'))
  assert.equal(state.scope, 'market')
})

test('decode selected', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('scope=market&selected=600519'))
  assert.equal(state.scope, 'market')
  assert.equal(state.selected, '600519')
})

test('encode→decode 往返一致', () => {
  const original: MarketWorkspaceUrlState = {
    scope: 'market',
    selected: '000001',
    industry: null,
    concept: null,
  }
  const encoded = encodeMarketWorkspaceUrl(original)
  const decoded = decodeMarketWorkspaceUrl(encoded)
  assert.deepEqual(decoded, original)
})

test('selected=null 时 encode 不包含 selected', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', selected: null, industry: null, concept: null })
  assert.equal(params.has('selected'), false)
})

test('scope 始终写入 encode', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'watchlist', selected: null, industry: null, concept: null })
  assert.equal(params.get('scope'), 'watchlist')
})

test('buildMarketWorkspaceUrl 生成完整 URL', () => {
  const url = buildMarketWorkspaceUrl({ scope: 'market', selected: '600519', industry: null, concept: null })
  assert.ok(url.startsWith('/market?'))
  assert.ok(url.includes('scope=market'))
  assert.ok(url.includes('selected=600519'))
})

test('buildMarketWorkspaceUrl 无 selected 时仍含 scope', () => {
  const url = buildMarketWorkspaceUrl({ scope: 'watchlist', selected: null, industry: null, concept: null })
  assert.ok(url.includes('scope=watchlist'))
  assert.ok(!url.includes('selected'))
})

test('selectInstrumentInTable：设置 selected，保留 scope', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    selected: null,
    industry: null,
    concept: null,
  }
  const newState = selectInstrumentInTable(state, '000001')
  assert.equal(newState.selected, '000001')
  assert.equal(newState.scope, 'market')
})

test('changeMarketScope：切换 scope 后清除 selected', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    selected: '000001',
    industry: null,
    concept: null,
  }
  const newState = changeMarketScope(state, 'watchlist')
  assert.equal(newState.scope, 'watchlist')
  assert.equal(newState.selected, null)
})

test('normalizeInternalReturnTo: 仅允许 /screener /market /messages 前缀，拒绝 /stock', () => {
  assert.equal(normalizeInternalReturnTo('/market'), '/market')
  assert.equal(normalizeInternalReturnTo('/market?scope=watchlist'), '/market?scope=watchlist')
  assert.equal(normalizeInternalReturnTo('/screener'), '/screener')
  assert.equal(normalizeInternalReturnTo('/messages'), '/messages')
  assert.equal(normalizeInternalReturnTo('/stock/600519'), null)
  assert.equal(normalizeInternalReturnTo('/stock/600519?returnTo=/market'), null)
})

test('normalizeInternalReturnTo: 拒绝外部 URL/双斜杠/javascript/超长值', () => {
  assert.equal(normalizeInternalReturnTo('https://evil.com'), null)
  assert.equal(normalizeInternalReturnTo('http://evil.com'), null)
  assert.equal(normalizeInternalReturnTo('//evil.com'), null)
  assert.equal(normalizeInternalReturnTo('javascript:alert(1)'), null)
  assert.equal(normalizeInternalReturnTo('a'.repeat(201)), null)
  assert.equal(normalizeInternalReturnTo('/admin'), null)
  assert.equal(normalizeInternalReturnTo('/unknown'), null)
  assert.equal(normalizeInternalReturnTo(null), null)
  assert.equal(normalizeInternalReturnTo(''), null)
})
