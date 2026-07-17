// [StockDetailNavigation] - 描述: 详情页导航唯一真源契约测试（CHANGE-20260716-006）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/stockDetailNavigation.test.ts
//
// 覆盖：
//   1. buildStockDetailUrl 生成 originScope + source + strategy + returnTo + timeframe
//   2. originScope=market → source=selection&strategy=dsa_selector
//   3. originScope=watchlist → source=watchlist&strategy=watchlist_monitor
//   4. resolveStockDetailOrigin 显式 originScope 不被 returnTo.scope 覆盖
//   5. stale returnTo=watchlist + originScope=market → contextMismatch=true
//   6. 无 originScope 时兼容解析 returnTo.scope
//   7. 无任何来源默认 watchlist
//   8. returnTo/timeframe 可选

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  buildStockDetailUrl,
  resolveStockDetailOrigin,
  sourceForOriginScope,
  strategyForOriginScope,
} from '../stockDetailNavigation.ts'

test('buildStockDetailUrl: originScope=market → source=selection&strategy=dsa_selector', () => {
  const url = buildStockDetailUrl('000001.SZ', { originScope: 'market' })
  assert.ok(url.startsWith('/stock/000001.SZ?'), `URL should start with /stock/000001.SZ?, got: ${url}`)
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'market')
  assert.equal(params.get('source'), 'selection')
  assert.equal(params.get('strategy'), 'dsa_selector')
})

test('buildStockDetailUrl: originScope=watchlist → source=watchlist&strategy=watchlist_monitor', () => {
  const url = buildStockDetailUrl('000001.SZ', { originScope: 'watchlist' })
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'watchlist')
  assert.equal(params.get('source'), 'watchlist')
  assert.equal(params.get('strategy'), 'watchlist_monitor')
})

test('buildStockDetailUrl: 保留 returnTo 和 timeframe', () => {
  const url = buildStockDetailUrl('000001.SZ', {
    originScope: 'market',
    returnTo: '/market?scope=market&selected=000001.SZ',
    timeframe: '1d',
  })
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('returnTo'), '/market?scope=market&selected=000001.SZ')
  assert.equal(params.get('timeframe'), '1d')
})

test('buildStockDetailUrl: 无 returnTo/timeframe 时不写入参数', () => {
  const url = buildStockDetailUrl('000001.SZ', { originScope: 'market' })
  const params = new URLSearchParams(url.split('?')[1])
  assert.ok(!params.has('returnTo'), 'returnTo should not be present')
  assert.ok(!params.has('timeframe'), 'timeframe should not be present')
})

test('sourceForOriginScope / strategyForOriginScope 映射正确', () => {
  assert.equal(sourceForOriginScope('market'), 'selection')
  assert.equal(sourceForOriginScope('watchlist'), 'watchlist')
  assert.equal(strategyForOriginScope('market'), 'dsa_selector')
  assert.equal(strategyForOriginScope('watchlist'), 'watchlist_monitor')
})

test('resolveStockDetailOrigin: 显式 originScope=market 立即点击仍为行情来源', () => {
  // stale returnTo=watchlist 不应覆盖显式 originScope=market
  const result = resolveStockDetailOrigin('market', '/market?scope=watchlist')
  assert.equal(result.originScope, 'market')
  assert.equal(result.contextMismatch, true, 'stale returnTo=watchlist + originScope=market → contextMismatch')
})

test('resolveStockDetailOrigin: originScope 与 returnTo.scope 一致 → 无冲突', () => {
  const result = resolveStockDetailOrigin('market', '/market?scope=market')
  assert.equal(result.originScope, 'market')
  assert.equal(result.contextMismatch, false)
})

test('resolveStockDetailOrigin: 无 originScope 时兼容解析 returnTo.scope', () => {
  const result = resolveStockDetailOrigin(null, '/market?scope=market')
  assert.equal(result.originScope, 'market')
  assert.equal(result.contextMismatch, false)
})

test('resolveStockDetailOrigin: 无任何来源默认 watchlist', () => {
  const result = resolveStockDetailOrigin(null, null)
  assert.equal(result.originScope, 'watchlist')
  assert.equal(result.contextMismatch, false)
})

test('resolveStockDetailOrigin: returnTo 非 /market 前缀不解析 scope', () => {
  const result = resolveStockDetailOrigin(null, '/screener?page=1')
  assert.equal(result.originScope, 'watchlist')
  assert.equal(result.contextMismatch, false)
})
