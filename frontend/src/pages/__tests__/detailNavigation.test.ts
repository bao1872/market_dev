// [个股详情导航] - 描述: 趋势选股/消息中心进入行情工作区的 URL 构建 + 返回路径契约测试
// 用法：node --experimental-strip-types --test src/pages/__tests__/detailNavigation.test.ts
//
// 覆盖：
//   1. Screener → /market URL 含 scope=market&symbol&source=selection&strategy&returnTo
//   2. Messages → /market URL 含 symbol&event_id
//   3. /stock/:symbol 兼容路由 URL
//   4. resolveBackPath 优先 returnTo
//   5. resolveBackPath 无 returnTo 时按 source fallback
//   6. buildMarketEntryFromScreener returnTo 安全校验（normalizeInternalReturnTo）
//   7. resolveBackPath returnTo 安全校验（normalizeInternalReturnTo）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import * as detailNav from '../detailNavigation.ts'
import {
  buildMarketEntryFromScreener,
  buildMarketEntryFromMessage,
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

test('/stock/:symbol URL 构建已迁移至 stockDetailNavigation.ts（CHANGE-20260716-006）', () => {
  // 旧 buildStockDetailUrl 已删除；详情页 URL 唯一入口为
  // frontend/src/features/stock-research/stockDetailNavigation.ts:buildStockDetailUrl
  // 此处仅验证旧 API 不再导出（禁止第二套 URL 拼接）
  assert.ok(
    !('buildStockDetailUrl' in detailNav),
    '旧 buildStockDetailUrl 必须删除（禁止第二套 URL 拼接）',
  )
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

// ===== returnTo 安全校验（normalizeInternalReturnTo）=====

test('buildMarketEntryFromScreener: 白名单 returnTo 通过', () => {
  // /screener /market /messages 前缀通过
  const cases = [
    '/screener?page=1',
    '/market?scope=watchlist',
    '/messages#inbox',
    '/screener?keyword=新能源',
  ]
  for (const rt of cases) {
    const url = buildMarketEntryFromScreener('000001.SZ', 'dsa_selector', rt)
    const params = new URLSearchParams(url.slice(7))
    assert.equal(params.get('returnTo'), rt, `returnTo should pass for: ${rt}`)
  }
})

test('buildMarketEntryFromScreener: 拒绝外部 URL，returnTo 不写入 URL', () => {
  // 外部 http/https/双斜杠/javascript 应被剔除 → URL 不含 returnTo 参数
  const maliciousCases = [
    'http://evil.com/screener',
    'https://evil.com/market',
    '//evil.com/screener',
    'javascript:alert(1)',
  ]
  for (const rt of maliciousCases) {
    const url = buildMarketEntryFromScreener('000001.SZ', 'dsa_selector', rt)
    const params = new URLSearchParams(url.slice(7))
    assert.ok(!params.has('returnTo'), `returnTo should be rejected for: ${rt}, got URL: ${url}`)
  }
})

test('buildMarketEntryFromScreener: 拒绝非白名单前缀', () => {
  // /admin /login /capture/stock 等不应被允许作为 returnTo
  const nonWhitelist = ['/admin', '/login', '/capture/stock/000001', '/settings', '/stock/000001']
  for (const rt of nonWhitelist) {
    const url = buildMarketEntryFromScreener('000001.SZ', 'dsa_selector', rt)
    const params = new URLSearchParams(url.slice(7))
    assert.ok(!params.has('returnTo'), `non-whitelist returnTo should be rejected: ${rt}`)
  }
})

test('buildMarketEntryFromScreener: 拒绝超长 returnTo', () => {
  // CHANGE-20260713-009 将长度限制提升到 4096；CHANGE-20260716-006 修正过时用例
  const long = '/screener?' + 'x'.repeat(5000)
  const url = buildMarketEntryFromScreener('000001.SZ', 'dsa_selector', long)
  const params = new URLSearchParams(url.slice(7))
  assert.ok(!params.has('returnTo'), 'overly long returnTo should be rejected')
})

test('resolveBackPath: 外部 URL returnTo 时按 source fallback', () => {
  // 危险 returnTo 应被剔除，使用 fallback
  assert.equal(resolveBackPath('http://evil.com/screener', 'selection'), '/screener')
  assert.equal(resolveBackPath('//evil.com/market', 'watchlist'), '/market?scope=watchlist')
  assert.equal(resolveBackPath('javascript:alert(1)', 'selection'), '/screener')
})

test('resolveBackPath: 非白名单 returnTo 时按 source fallback', () => {
  assert.equal(resolveBackPath('/admin', 'selection'), '/screener')
  assert.equal(resolveBackPath('/login', 'watchlist'), '/market?scope=watchlist')
  assert.equal(resolveBackPath('/capture/stock/000001', 'selection'), '/screener')
})
