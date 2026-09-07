// [SelfSelectionOnlyContract] - 描述: self_selection-only 用户自选 UI 契约测试（源码级）
// 用法：./node_modules/.bin/tsx --test src/features/market-workspace/__tests__/selfSelectionOnlyContract.test.ts
//
// 背景（P0 后续任务）：
//   P0 授权修复后，/market/stocks（无论 scope=market 还是 scope=watchlist）要求 market_data，
//   self_selection-only 用户（有 self_selection、无 market_data）访问该端点会 403。
//   因此 MarketWorkspacePage 必须为这类用户提供独立自选视图，数据源改走
//   /v1/watchlist/monitor-status（仅要求 self_selection），不再发 /market/stocks。
//
// 契约：
//   1. 存在 isSelfSelectionOnly 判定（!isAdmin && hasSelfSelection && !hasMarketData）
//   2. self_selection-only 时 useMarketStocks 被禁用（enabled 含 !isSelfSelectionOnly），杜绝 403
//   3. self_selection-only 时 useWatchlistMonitorStatus 被启用（enabled 含 isSelfSelectionOnly）
//   4. self_selection-only 分支渲染 WatchlistMonitorTable，不渲染 MarketToolbar（行情专属 UI）
//   5. 移除自选走 handleRemoveSelfSelectionWatchlist（复用 /v1/watchlist/{id}，仅要求 self_selection）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'MarketWorkspacePage.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

test('SELFONLY-1: 存在 isSelfSelectionOnly 判定（!isAdmin && hasSelfSelection && !hasMarketData）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    /const isSelfSelectionOnly\s*=\s*!isAdmin\s*&&\s*hasSelfSelection\s*&&\s*!hasMarketData/.test(src),
    '必须定义 isSelfSelectionOnly = !isAdmin && hasSelfSelection && !hasMarketData',
  )
})

test('SELFONLY-2: self_selection-only 时禁用 useMarketStocks（enabled 含 !isSelfSelectionOnly）', () => {
  const src = readSource(PAGE_PATH)
  // useMarketStocks 的 enabled 必须包含 !isSelfSelectionOnly，从源头杜绝 /market/stocks 403
  const marketStocksMatch = src.match(/useMarketStocks\(marketStocksParams,\s*\{[\s\S]*?enabled:\s*(accessReady\s*&&\s*!isSelfSelectionOnly)[\s\S]*?\}\)/)
  assert.ok(marketStocksMatch, 'useMarketStocks 的 enabled 必须包含 accessReady && !isSelfSelectionOnly')
})

test('SELFONLY-3: self_selection-only 时启用 useWatchlistMonitorStatus（enabled 含 isSelfSelectionOnly）', () => {
  const src = readSource(PAGE_PATH)
  const monitorMatch = src.match(/useWatchlistMonitorStatus\(\{[\s\S]*?enabled:\s*isSelfSelectionOnly[\s\S]*?\}\)/)
  assert.ok(monitorMatch, 'useWatchlistMonitorStatus 的 enabled 必须为 isSelfSelectionOnly')
})

test('SELFONLY-4: self_selection-only 分支渲染 WatchlistMonitorTable 而非 MarketToolbar', () => {
  const src = readSource(PAGE_PATH)
  // 定位 isSelfSelectionOnly 早退分支（if (isSelfSelectionOnly) { return (...)}）
  const branchIdx = src.indexOf('if (isSelfSelectionOnly)')
  assert.ok(branchIdx >= 0, '必须存在 if (isSelfSelectionOnly) 早退分支')
  // 精确切片：从 if (isSelfSelectionOnly) 到主 return 之前（"\n  return (" 是主 return 的起始标记）
  const mainReturnIdx = src.indexOf('\n  return (', branchIdx)
  assert.ok(mainReturnIdx > branchIdx, '自选分支后必须存在主 return')
  const branchBlock = src.slice(branchIdx, mainReturnIdx)
  assert.ok(/<WatchlistMonitorTable/.test(branchBlock), '自选分支必须渲染 WatchlistMonitorTable')
  // 用 <MarketToolbar（JSX 元素，带 < 前缀）精确匹配，排除注释提及
  assert.ok(!/<MarketToolbar/.test(branchBlock), '自选分支不得渲染 MarketToolbar（行情专属 UI）')
  // 移除自选回调必须绑定
  assert.ok(/onRemove=\{handleRemoveSelfSelectionWatchlist\}/.test(branchBlock), '自选分支必须绑定 onRemove={handleRemoveSelfSelectionWatchlist}')
})

test('SELFONLY-5: 移除自选复用 removeMutation（/v1/watchlist/{id}，仅要求 self_selection）', () => {
  const src = readSource(PAGE_PATH)
  const defIdx = src.indexOf('handleRemoveSelfSelectionWatchlist = useCallback')
  assert.ok(defIdx >= 0, '必须存在 handleRemoveSelfSelectionWatchlist = useCallback 定义')
  const block = src.slice(defIdx, defIdx + 600)
  assert.ok(/removeMutation\.mutate\(row\.instrument_id/.test(block), '移除必须调用 removeMutation.mutate(row.instrument_id)')
})

test('SELFONLY-6: import WatchlistMonitorTable 与 adaptWatchlistMonitorStatusItem', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(/WatchlistMonitorTable/.test(src), '必须 import WatchlistMonitorTable')
  assert.ok(/adaptWatchlistMonitorStatusItem/.test(src), '必须 import adaptWatchlistMonitorStatusItem')
  assert.ok(/useWatchlistMonitorStatus/.test(src), '必须 import useWatchlistMonitorStatus')
})
