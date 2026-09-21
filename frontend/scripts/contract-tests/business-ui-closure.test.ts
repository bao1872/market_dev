import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { canonicalizeFilterOperator } from '../../src/components/filterOperators.ts'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
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
  // [S3-B] market-stocks 失效逻辑随 watchlist mutation 迁至 useWatchlistApi.ts（useApi 仅兼容 re-export）
  const hooks = source('src/hooks/useWatchlistApi.ts')
  assert.ok(market.includes('JSON.stringify(marketStocksParams)'))
  assert.ok(detail.includes('useMarketStocks(marketStocksParams, { enabled: useMcq })'))
  assert.ok(detail.includes('marketCanonicalQuery: marketCanonicalQueryRaw'))
  assert.ok(market.includes('watchlistOverrides'))
  assert.ok(detail.includes('watchlistOverride ?? serverInWatchlist'))
  assert.ok(hooks.includes("invalidateQueries({ queryKey: ['market-stocks'] })"))
})

test('structured chip unavailable reasons remain visible in stock detail', () => {
  const panel = source('src/features/stock-research/FirstPyramidPanel.tsx')
  assert.ok(panel.includes("chipStatus?.reasonCode === 'M15_BARS_INSUFFICIENT'"))
  assert.ok(panel.includes('chipStatus?.reasonText'))
  assert.ok(panel.includes('actualBars'))
  assert.ok(panel.includes('requiredBars'))
})

// Slice F retired the legacy FilterDiscoveryPanel / EvidenceDrawer / MarketScanPanel
// components. [REVIEW-V2-R1] 旧 Review 复盘工作台（src/features/review/**）已随产品退役
// 物理删除，上述 legacy 组件与对应契约测试一并移除；复盘现由 Market Dashboard（/review）承载。

test('[Slice F] retired legacy FilterDiscoveryPanel/EvidenceDrawer/MarketScanPanel tests are gone', () => {
  // Legacy source files must no longer exist
  for (const p of [
    'src/features/review/FilterDiscoveryPanel.tsx',
    'src/features/review/EvidenceDrawer.tsx',
    'src/features/review/MarketScanPanel.tsx',
  ]) {
    assert.ok(!existsSync(resolve(ROOT, p)), `${p} 应已在 Slice F 物理删除`)
  }
})
