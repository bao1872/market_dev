import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'

import { canonicalizeFilterOperator } from '../../src/components/filterOperators.ts'
import { decodeReviewUrl, encodeReviewUrl } from '../../src/features/review/urlState.ts'

const ROOT = resolve(import.meta.dirname, '../..')
const source = (path: string) => readFileSync(resolve(ROOT, path), 'utf8')

test('legacy filter aliases normalize to canonical output names', () => {
  assert.deepEqual(
    ['ne', 'is_empty', 'is_not_empty', 'contains_any', 'contains_all'].map(
      canonicalizeFilterOperator,
    ),
    ['neq', 'empty', 'not_empty', 'has_any', 'has_all'],
  )
})

test('market and detail navigation share market-stocks query and optimistic watchlist state', () => {
  const market = source('src/features/market-workspace/MarketWorkspacePage.tsx')
  const detail = source('src/features/stock-research/useStockDetailActions.ts')
  const hooks = source('src/hooks/useApi.ts')
  assert.ok(market.includes('JSON.stringify(marketStocksParams)'))
  assert.ok(detail.includes('useMarketStocks(marketStocksParams, { enabled: useMcq })'))
  assert.ok(detail.includes('marketCanonicalQuery: marketCanonicalQueryRaw'))
  assert.ok(market.includes('watchlistOverrides'))
  assert.ok(detail.includes('watchlistOverride ?? serverInWatchlist'))
  assert.ok(hooks.includes("invalidateQueries({ queryKey: ['market-stocks'] })"))
})

test('Review renders D family and exposes source, denominator, version, and readiness', () => {
  const discovery = source('src/features/review/FilterDiscoveryPanel.tsx')
  const drawer = source('src/features/review/EvidenceDrawer.tsx')
  assert.ok(discovery.includes("D: 'D 第二金字塔偏差'"))
  assert.ok(discovery.includes("['A', 'B', 'C', 'D']"))
  assert.ok(drawer.includes('c.fieldSource'))
  assert.ok(drawer.includes('c.denominator'))
  assert.ok(drawer.includes('算法版本'))
  assert.ok(drawer.includes('payload?.readiness?.raw_ready'))
  assert.ok(drawer.includes('payload.readiness?.reason'))
})

test('Review scope hierarchy and signal query survive URL hydration', () => {
  const params = new URLSearchParams({
    date: '2026-08-01',
    stage: 'signals',
    scopeType: 'industry_l2',
    scopeKey: 'l2-technology',
    scopeName: '科技硬件',
    parentScopeType: 'industry_l1',
    parentScopeKey: 'l1-technology',
  })
  const state = decodeReviewUrl(params)
  assert.deepEqual(decodeReviewUrl(encodeReviewUrl(state)), state)

  const discovery = source('src/features/review/FilterDiscoveryPanel.tsx')
  const scan = source('src/features/review/MarketScanPanel.tsx')
  assert.ok(discovery.includes('scope_type: scopeType || undefined'))
  assert.ok(discovery.includes('scope_key: scopeKey || undefined'))
  for (const scopeType of ['industry_l1', 'industry_l2', 'industry_l3', 'concept']) {
    assert.ok(scan.includes(`value: '${scopeType}'`))
  }
})

test('structured chip unavailable reasons remain visible in stock detail', () => {
  const panel = source('src/features/stock-research/FirstPyramidPanel.tsx')
  assert.ok(panel.includes("chipStatus?.reasonCode === 'M15_BARS_INSUFFICIENT'"))
  assert.ok(panel.includes('chipStatus?.reasonText'))
  assert.ok(panel.includes('actualBars'))
  assert.ok(panel.includes('requiredBars'))
})
