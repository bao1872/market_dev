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
  decodeMarketListContext,
  buildStrategyResultQueryParams,
  DEFAULT_MARKET_SCOPE,
  type MarketWorkspaceUrlState,
  type MarketListContext,
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
  // CHANGE-20260713-009: 限制从 200 提升到 500（/market URL 含 filters JSON 编码后可能超过 200）
  assert.equal(normalizeInternalReturnTo('a'.repeat(501)), null)
  assert.equal(normalizeInternalReturnTo('a'.repeat(500)), null) // 500 字符以 /market 等前缀开头才有效，纯 a 仍然被拒绝
  assert.equal(normalizeInternalReturnTo('/admin'), null)
  assert.equal(normalizeInternalReturnTo('/unknown'), null)
  assert.equal(normalizeInternalReturnTo(null), null)
  assert.equal(normalizeInternalReturnTo(''), null)
})

// ===== CHANGE-20260713-009: 详情页来源上下文共享纯函数契约测试 =====
// 覆盖 7 项关键场景：
//   1. 任意合法 /market URL 都识别为 market context（不要求 keyword/page/sort 存在）
//   2. scope=market → universe=all；scope=watchlist → universe=watchlist
//   3. 仅含 industry/concept 的 /market URL 也识别为 market context
//   4. filters JSON 字符串正确解码并转换为 metric_filters
//   5. RATIO/PERCENTILE 类指标百分号自动归一化
//   6. 非 /market、外部 URL、非法 returnTo 返回 null
//   7. buildStrategyResultQueryParams 保留完整 keyword/industry/concept/sort/page/page_size

test('CHANGE-009-1: 任意合法 /market URL 都识别为 market context（仅 scope=market，无其他参数）', () => {
  // 关键场景：旧逻辑要求 keyword/page/sort 才认为是 market context，导致纯 /market?scope=market 被识别为 null
  // 新逻辑：任意合法 /market URL 都识别为 market context
  const ctx = decodeMarketListContext('/market?scope=market')
  assert.notEqual(ctx, null)
  assert.equal(ctx!.scope, 'market')
  assert.equal(ctx!.keyword, null)
  assert.equal(ctx!.industry, null)
  assert.equal(ctx!.concept, null)
  assert.equal(ctx!.sort, null)
  assert.equal(ctx!.filters, null)
  assert.equal(ctx!.page, null)
  assert.equal(ctx!.page_size, null)
})

test('CHANGE-009-2: scope=market → universe=all；scope=watchlist → universe=watchlist', () => {
  const marketCtx = decodeMarketListContext('/market?scope=market&keyword=600519')
  const marketQuery = buildStrategyResultQueryParams(marketCtx!)
  assert.equal(marketQuery.universe, 'all')
  assert.equal(marketQuery.keyword, '600519')

  const watchlistCtx = decodeMarketListContext('/market?scope=watchlist')
  const watchlistQuery = buildStrategyResultQueryParams(watchlistCtx!)
  assert.equal(watchlistQuery.universe, 'watchlist')

  // 无 scope 参数默认 watchlist
  const defaultCtx = decodeMarketListContext('/market')
  assert.equal(defaultCtx!.scope, 'watchlist')
  const defaultQuery = buildStrategyResultQueryParams(defaultCtx!)
  assert.equal(defaultQuery.universe, 'watchlist')
})

test('CHANGE-009-3: 仅含 industry/concept 的 /market URL 也识别为 market context', () => {
  // 关键场景：用户在 /market 页只选了行业/概念筛选，没有 keyword/page/sort
  // 旧逻辑会返回 null，导致详情页回退到自选列表，与来源不一致
  const ctx = decodeMarketListContext('/market?scope=market&industry=半导体&concept=芯片')
  assert.notEqual(ctx, null)
  assert.equal(ctx!.scope, 'market')
  assert.equal(ctx!.industry, '半导体')
  assert.equal(ctx!.concept, '芯片')
  assert.equal(ctx!.keyword, null)

  const query = buildStrategyResultQueryParams(ctx!)
  assert.equal(query.universe, 'all')
  assert.equal(query.industry, '半导体')
  assert.equal(query.concept, '芯片')
})

test('CHANGE-009-4: filters JSON 字符串正确解码并转换为 metric_filters', () => {
  // filters 编码格式与 screenerUrlState 一致：[{key, op, value, value2}, ...]
  const filters = JSON.stringify([
    { key: 'vwap_ret_avg', op: 'gt', value: 0.05 },
    { key: 'offset_percentile', op: 'between', value: 0.2, value2: 0.8 },
  ])
  const ctx = decodeMarketListContext(`/market?scope=market&filters=${encodeURIComponent(filters)}`)
  assert.notEqual(ctx, null)
  assert.equal(ctx!.filters!.length, 2)
  assert.equal(ctx!.filters![0].key, 'vwap_ret_avg')
  assert.equal(ctx!.filters![0].operator, 'gt')
  assert.equal(ctx!.filters![1].key, 'offset_percentile')
  assert.equal(ctx!.filters![1].operator, 'between')

  const query = buildStrategyResultQueryParams(ctx!)
  assert.ok(query.metric_filters, 'metric_filters should be set')
  const parsed = JSON.parse(query.metric_filters!)
  assert.equal(parsed.length, 2)
  assert.equal(parsed[0].metric_key, 'vwap_ret_avg')
  assert.equal(parsed[0].operator, 'gt')
  assert.equal(parsed[0].value, 0.05)
  assert.equal(parsed[1].metric_key, 'offset_percentile')
  assert.equal(parsed[1].operator, 'between')
  assert.equal(parsed[1].value1, 0.2)
  assert.equal(parsed[1].value2, 0.8)
})

test('CHANGE-009-5: RATIO/PERCENTILE 类指标百分号自动归一化', () => {
  // 用户输入 5% 应转换为 0.05（vwap_ret_avg 是 RATIO_METRICS）
  // 用户输入 80% 应转换为 0.8（offset_percentile 是 PERCENTILE_METRICS）
  const filters = JSON.stringify([
    { key: 'vwap_ret_avg', op: 'gt', value: '5%' },
    { key: 'offset_percentile', op: 'lt', value: '80%' },
  ])
  const ctx = decodeMarketListContext(`/market?scope=market&filters=${encodeURIComponent(filters)}`)
  const query = buildStrategyResultQueryParams(ctx!)
  const parsed = JSON.parse(query.metric_filters!)
  assert.equal(parsed[0].value, 0.05)
  assert.equal(parsed[1].value, 0.8)

  // 不带 % 的值保持原样
  const filters2 = JSON.stringify([{ key: 'vwap_ret_avg', op: 'gt', value: 0.05 }])
  const ctx2 = decodeMarketListContext(`/market?scope=market&filters=${encodeURIComponent(filters2)}`)
  const query2 = buildStrategyResultQueryParams(ctx2!)
  const parsed2 = JSON.parse(query2.metric_filters!)
  assert.equal(parsed2[0].value, 0.05)
})

test('CHANGE-009-6: 非 /market、外部 URL、非法 returnTo 返回 null', () => {
  // 非 /market 前缀
  assert.equal(decodeMarketListContext('/screener'), null)
  assert.equal(decodeMarketListContext('/messages'), null)
  assert.equal(decodeMarketListContext('/admin'), null)
  assert.equal(decodeMarketListContext('/stock/600519'), null)

  // 外部 URL
  assert.equal(decodeMarketListContext('https://evil.com'), null)
  assert.equal(decodeMarketListContext('http://evil.com'), null)
  assert.equal(decodeMarketListContext('//evil.com'), null)
  assert.equal(decodeMarketListContext('javascript:alert(1)'), null)

  // 空/null/超长
  assert.equal(decodeMarketListContext(null), null)
  assert.equal(decodeMarketListContext(''), null)
  assert.equal(decodeMarketListContext('a'.repeat(501)), null)
})

test('CHANGE-009-7: buildStrategyResultQueryParams 保留完整 keyword/industry/concept/sort/page/page_size', () => {
  const ctx: MarketListContext = {
    scope: 'market',
    keyword: '茅台',
    industry: '白酒',
    concept: '消费',
    sort: { key: 'change_pct', direction: 'desc' },
    filters: null,
    page: 2,
    page_size: 50,
  }
  const query = buildStrategyResultQueryParams(ctx)
  assert.equal(query.universe, 'all')
  assert.equal(query.keyword, '茅台')
  assert.equal(query.industry, '白酒')
  assert.equal(query.concept, '消费')
  assert.equal(query.sort_by, 'change_pct')
  assert.equal(query.sort_desc, true)
  assert.equal(query.page, 2)
  assert.equal(query.page_size, 50)
  assert.equal(query.metric_filters, undefined) // filters 为 null 时不设置

  // 验证 sort=asc 时 sort_desc=false
  const ctxAsc: MarketListContext = {
    ...ctx,
    sort: { key: 'change_pct', direction: 'asc' },
  }
  const queryAsc = buildStrategyResultQueryParams(ctxAsc)
  assert.equal(queryAsc.sort_desc, false)
})
