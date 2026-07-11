// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts
//
// 覆盖：
//   1. decode 默认值（无参数时 scope=watchlist, query='', page=1, pageSize=DEFAULT_PAGE_SIZE, sort=null, selected=null）
//   2. decode scope=market
//   3. decode query + page + page_size + sort + selected
//   4. 非法 page 回退 1
//   5. page_size 超过 100 回退 50
//   6. encode→decode 往返一致
//   7. query='' 时 encode 不包含 query
//   8. page=1（默认）时 encode 省略 page
//   9. selected=null 时 encode 不包含 selected
//  10. buildMarketWorkspaceUrl 生成完整 URL
//  11. selectInstrumentInTable：设置 selected，保留 scope/query/page/pageSize/sort
//  12. changeMarketScope：切换 scope 后重置 page=1、清除 selected
//  13. changeMarketScope：保留 query 和 sort
//  14. normalizeInternalReturnTo: 仅允许 /screener /market /messages 前缀，拒绝 /stock
//  15. normalizeInternalReturnTo: 拒绝外部 URL/双斜杠/javascript/超长值

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
  DEFAULT_PAGE,
  DEFAULT_PAGE_SIZE,
  type MarketWorkspaceUrlState,
} from '../marketWorkspaceUrlState.ts'

test('decode 默认值（无参数时 scope=watchlist, query="", page=1, pageSize=DEFAULT_PAGE_SIZE, sort=null, selected=null）', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams())
  assert.equal(state.scope, DEFAULT_MARKET_SCOPE)
  assert.equal(state.query, '')
  assert.equal(state.page, DEFAULT_PAGE)
  assert.equal(state.pageSize, DEFAULT_PAGE_SIZE)
  assert.equal(state.sort, null)
  assert.equal(state.selected, null)
})

test('decode scope=market', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('scope=market'))
  assert.equal(state.scope, 'market')
})

test('decode query + page + page_size + sort + selected', () => {
  const params = new URLSearchParams('scope=market&query=茅台&page=2&page_size=20&sort=name:desc&selected=600519')
  const state = decodeMarketWorkspaceUrl(params)
  assert.equal(state.query, '茅台')
  assert.equal(state.page, 2)
  assert.equal(state.pageSize, 20)
  assert.equal(state.sort, 'name:desc')
  assert.equal(state.selected, '600519')
})

test('非法 page 回退 1', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('page=0'))
  assert.equal(state.page, 1)
  const state2 = decodeMarketWorkspaceUrl(new URLSearchParams('page=abc'))
  assert.equal(state2.page, 1)
})

test('page_size 超过 100 回退 50', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams('page_size=200'))
  assert.equal(state.pageSize, 50)
})

test('encode→decode 往返一致（含 query/page/sort/selected）', () => {
  const original: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'symbol:asc',
    selected: '000001',
  }
  const encoded = encodeMarketWorkspaceUrl(original)
  const decoded = decodeMarketWorkspaceUrl(encoded)
  assert.deepEqual(decoded, original)
})

test('query="" 时 encode 不包含 query', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', query: '', page: 1, pageSize: 50, sort: null, selected: null })
  assert.equal(params.has('query'), false)
})

test('page=1（默认）时 encode 省略 page', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', query: '', page: 1, pageSize: 50, sort: null, selected: null })
  assert.equal(params.has('page'), false)
})

test('selected=null 时 encode 不包含 selected', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', query: '', page: 1, pageSize: 50, sort: null, selected: null })
  assert.equal(params.has('selected'), false)
})

test('buildMarketWorkspaceUrl 生成完整 URL', () => {
  const url = buildMarketWorkspaceUrl({ scope: 'market', query: '茅台', page: 2, pageSize: 50, sort: null, selected: '600519' })
  assert.ok(url.startsWith('/market?'))
  assert.ok(url.includes('scope=market'))
  assert.ok(url.includes('query='))
  assert.ok(url.includes('selected=600519'))
})

test('selectInstrumentInTable：设置 selected，保留 scope/query/page/pageSize/sort', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'symbol:asc',
    selected: null,
  }
  const newState = selectInstrumentInTable(state, '000001')
  assert.equal(newState.selected, '000001')
  assert.equal(newState.scope, 'market')
  assert.equal(newState.query, '银行')
  assert.equal(newState.page, 3)
  assert.equal(newState.pageSize, 20)
  assert.equal(newState.sort, 'symbol:asc')
})

test('changeMarketScope：切换 scope 后重置 page=1、清除 selected', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'symbol:asc',
    selected: '000001',
  }
  const newState = changeMarketScope(state, 'watchlist')
  assert.equal(newState.scope, 'watchlist')
  assert.equal(newState.page, 1)
  assert.equal(newState.selected, null)
})

test('changeMarketScope：保留 query 和 sort', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'name:desc',
    selected: null,
  }
  const newState = changeMarketScope(state, 'watchlist')
  assert.equal(newState.query, '银行')
  assert.equal(newState.sort, 'name:desc')
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
