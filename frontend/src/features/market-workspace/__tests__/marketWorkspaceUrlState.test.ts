// [MarketWorkspaceUrlState] - 描述: /market URL 状态 encode/decode 契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/marketWorkspaceUrlState.test.ts
//
// 覆盖：
//   1. decode 默认值（无参数时 scope=watchlist, query='', page=1, pageSize=DEFAULT_PAGE_SIZE, sort=null, selected=null, industry=null, concept=null, state=null）
//   2. decode scope=market
//   3. decode query + page + page_size + sort + selected
//   4. 非法 page 回退 1
//   5. page_size 超过 100 回退 50
//   6. decode industry/concept/state
//   7. decode 非法 state 回退 null
//   8. encode→decode 往返一致（含 industry/concept/state）
//   9. query='' 时 encode 不包含 query
//  10. page=1（默认）时 encode 省略 page
//  11. selected=null 时 encode 不包含 selected
//  12. industry=null 时 encode 不包含 industry
//  13. buildMarketWorkspaceUrl 生成完整 URL
//  14. selectInstrumentInTable：设置 selected，保留 scope/query/page/pageSize/sort/industry/concept/state
//  15. changeMarketScope：切换 scope 后重置 page=1、清除 selected
//  16. changeMarketScope：保留 query 和 sort
//  17. changeMarketFilter：重置 page=1、清除 selected，保留其他筛选
//  18. normalizeInternalReturnTo: 仅允许 /screener /market /messages 前缀，拒绝 /stock
//  19. normalizeInternalReturnTo: 拒绝外部 URL/双斜杠/javascript/超长值

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  buildMarketWorkspaceUrl,
  selectInstrumentInTable,
  changeMarketScope,
  changeMarketFilter,
  normalizeInternalReturnTo,
  DEFAULT_MARKET_SCOPE,
  DEFAULT_PAGE,
  DEFAULT_PAGE_SIZE,
  type MarketWorkspaceUrlState,
} from '../marketWorkspaceUrlState.ts'

test('decode 默认值（无参数时 scope=watchlist, query="", page=1, pageSize=DEFAULT_PAGE_SIZE, sort=null, selected=null, industry=null, concept=null, state=null）', () => {
  const state = decodeMarketWorkspaceUrl(new URLSearchParams())
  assert.equal(state.scope, DEFAULT_MARKET_SCOPE)
  assert.equal(state.query, '')
  assert.equal(state.page, DEFAULT_PAGE)
  assert.equal(state.pageSize, DEFAULT_PAGE_SIZE)
  assert.equal(state.sort, null)
  assert.equal(state.selected, null)
  assert.equal(state.industry, null)
  assert.equal(state.concept, null)
  assert.equal(state.state, null)
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

test('decode industry/concept/state', () => {
  const params = new URLSearchParams('scope=market&industry=银行&concept=新能源&state=up')
  const state = decodeMarketWorkspaceUrl(params)
  assert.equal(state.industry, '银行')
  assert.equal(state.concept, '新能源')
  assert.equal(state.state, 'up')
})

test('decode 非法 state 回退 null', () => {
  const params = new URLSearchParams('state=invalid')
  const state = decodeMarketWorkspaceUrl(params)
  assert.equal(state.state, null)
})

test('encode→decode 往返一致（含 industry/concept/state）', () => {
  const original: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'symbol:asc',
    selected: '000001',
    industry: '银行',
    concept: '新能源',
    state: 'up',
  }
  const encoded = encodeMarketWorkspaceUrl(original)
  const decoded = decodeMarketWorkspaceUrl(encoded)
  assert.deepEqual(decoded, original)
})

test('query="" 时 encode 不包含 query', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', query: '', page: 1, pageSize: 50, sort: null, selected: null, industry: null, concept: null, state: null })
  assert.equal(params.has('query'), false)
})

test('page=1（默认）时 encode 省略 page', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', query: '', page: 1, pageSize: 50, sort: null, selected: null, industry: null, concept: null, state: null })
  assert.equal(params.has('page'), false)
})

test('selected=null 时 encode 不包含 selected', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', query: '', page: 1, pageSize: 50, sort: null, selected: null, industry: null, concept: null, state: null })
  assert.equal(params.has('selected'), false)
})

test('industry=null 时 encode 不包含 industry', () => {
  const params = encodeMarketWorkspaceUrl({ scope: 'market', query: '', page: 1, pageSize: 50, sort: null, selected: null, industry: null, concept: null, state: null })
  assert.equal(params.has('industry'), false)
  assert.equal(params.has('concept'), false)
  assert.equal(params.has('state'), false)
})

test('buildMarketWorkspaceUrl 生成完整 URL', () => {
  const url = buildMarketWorkspaceUrl({ scope: 'market', query: '茅台', page: 2, pageSize: 50, sort: null, selected: '600519', industry: null, concept: null, state: null })
  assert.ok(url.startsWith('/market?'))
  assert.ok(url.includes('scope=market'))
  assert.ok(url.includes('query='))
  assert.ok(url.includes('selected=600519'))
})

test('selectInstrumentInTable：设置 selected，保留 scope/query/page/pageSize/sort/industry/concept/state', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'symbol:asc',
    selected: null,
    industry: '银行',
    concept: '新能源',
    state: 'up',
  }
  const newState = selectInstrumentInTable(state, '000001')
  assert.equal(newState.selected, '000001')
  assert.equal(newState.scope, 'market')
  assert.equal(newState.query, '银行')
  assert.equal(newState.page, 3)
  assert.equal(newState.pageSize, 20)
  assert.equal(newState.sort, 'symbol:asc')
  assert.equal(newState.industry, '银行')
  assert.equal(newState.concept, '新能源')
  assert.equal(newState.state, 'up')
})

test('changeMarketScope：切换 scope 后重置 page=1、清除 selected', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'symbol:asc',
    selected: '000001',
    industry: '银行',
    concept: '新能源',
    state: 'up',
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
    industry: null,
    concept: null,
    state: null,
  }
  const newState = changeMarketScope(state, 'watchlist')
  assert.equal(newState.query, '银行')
  assert.equal(newState.sort, 'name:desc')
})

test('changeMarketFilter：重置 page=1、清除 selected，保留其他筛选', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '银行',
    page: 3,
    pageSize: 20,
    sort: 'symbol:asc',
    selected: '000001',
    industry: '银行',
    concept: '新能源',
    state: 'up',
  }
  const newState = changeMarketFilter(state, { state: 'down' })
  assert.equal(newState.page, 1)
  assert.equal(newState.selected, null)
  assert.equal(newState.state, 'down')
  assert.equal(newState.industry, '银行')
  assert.equal(newState.concept, '新能源')
  assert.equal(newState.query, '银行')
  assert.equal(newState.sort, 'symbol:asc')
})

test('changeMarketFilter：清除 industry 时设为 null', () => {
  const state: MarketWorkspaceUrlState = {
    scope: 'market',
    query: '',
    page: 1,
    pageSize: 50,
    sort: null,
    selected: '600519',
    industry: '银行',
    concept: null,
    state: null,
  }
  const newState = changeMarketFilter(state, { industry: null })
  assert.equal(newState.industry, null)
  assert.equal(newState.page, 1)
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
