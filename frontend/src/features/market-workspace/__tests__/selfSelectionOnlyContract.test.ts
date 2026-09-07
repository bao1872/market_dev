// [SelfSelectionOnlyContract] - 描述: self_selection-only 用户完整自选 workspace 契约测试（源码级）
// 用法：./node_modules/.bin/tsx --test src/features/market-workspace/__tests__/selfSelectionOnlyContract.test.ts
//
// 背景（Commit A 权限模型纠偏，base 4c4f0fd3）：
//   上一轮把 self_selection-only 用户错误地砍成了 metadata-only 表格（早退分支），并把
//   /market/stocks?scope=watchlist、monitor-status、详情端点全部叠上 market_data AND 约束，
//   导致 self_selection-only 用户无法使用完整自选体验。Commit A 恢复正确语义：
//   - scope=market → market_data；scope=watchlist → self_selection（universe 由服务端
//     current user + active UserWatchlistItem 强制，不存在跨用户泄漏）。
//   - self_selection-only 用户在行情页使用完整 StrategyDataTable（scope 强制 watchlist），
//     不再有 metadata-only 早退分支；自选股票的个股详情走后端 resource guard 放行。
//
// 契约：
//   1. 禁止 isSelfSelectionOnly metadata-only 早退分支（if (isSelfSelectionOnly) return 极简表格）
//   2. useMarketStocks enabled 仅含 accessReady（self-only 也发 watchlist scope 请求）
//   3. 不存在 handleRemoveSelfSelectionWatchlist（随早退分支删除）
//   4. scope 归一化强制：self_selection-only（无 market_data）→ scope=watchlist
//   5. canAccessStockDetail = isAdmin || hasMarketData || (hasSelfSelection && scope==='watchlist')
//   6. 不渲染 WatchlistMonitorTable / useWatchlistMonitorStatus / adaptWatchlistMonitorStatusItem
//      （完整 workspace 使用 StrategyDataTable + MarketRightPanel）

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

test('SELFONLY-1: 禁止 isSelfSelectionOnly 变量与 metadata-only 早退分支', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    !/const isSelfSelectionOnly\s*=/.test(src),
    'Commit A 已删除 isSelfSelectionOnly（self-only 用户走完整 workspace，不再降级 metadata-only）',
  )
  assert.ok(
    !/if\s*\(\s*isSelfSelectionOnly\s*\)/.test(src),
    '必须不存在 if (isSelfSelectionOnly) 早退分支',
  )
})

test('SELFONLY-2: useMarketStocks enabled 仅含 accessReady（self-only 也发 watchlist scope）', () => {
  const src = readSource(PAGE_PATH)
  const m = src.match(/useMarketStocks\(marketStocksParams,\s*\{[\s\S]*?enabled:\s*(accessReady)[\s\S]*?\}\)/)
  assert.ok(m, 'useMarketStocks 的 enabled 必须为 accessReady（不得含 !isSelfSelectionOnly）')
  assert.ok(
    !/enabled:\s*accessReady\s*&&\s*!isSelfSelectionOnly/.test(src),
    '不得再把 self-only 排除在 /market/stocks 之外',
  )
})

test('SELFONLY-3: 不存在 handleRemoveSelfSelectionWatchlist / selfSelectionOnlyWatchlistQuery（随早退分支删除）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(!/handleRemoveSelfSelectionWatchlist/.test(src), 'metadata-only 早退分支已删除，不得残留其回调')
  assert.ok(!/selfSelectionOnlyWatchlistQuery/.test(src), '不得残留 selfSelectionOnlyWatchlistQuery')
  assert.ok(!/selfSelectionOnlyItems/.test(src), '不得残留 selfSelectionOnlyItems')
})

test('SELFONLY-4: scope 归一化强制 self-only → watchlist（无 market_data 不得渲染全市场）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    /urlState\.scope === 'market'\s*&&\s*hasSelfSelection\s*&&\s*!hasMarketData/.test(src),
    'self_selection-only（无 market_data）必须把 scope 强制为 watchlist',
  )
})

test('SELFONLY-5: canAccessStockDetail 允许 self-only + watchlist scope（A6）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    /const canAccessStockDetail\s*=\s*isAdmin\s*\|\|\s*hasMarketData\s*\|\|\s*\(hasSelfSelection\s*&&\s*scope === 'watchlist'\)/.test(src),
    'canAccessStockDetail = isAdmin || hasMarketData || (hasSelfSelection && scope === "watchlist")',
  )
})

test('SELFONLY-6: 完整 workspace 使用 StrategyDataTable + MarketRightPanel；禁止 metadata-only 组件', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(/<StrategyDataTable/.test(src), '主渲染必须包含 StrategyDataTable（完整行情表格）')
  assert.ok(/<MarketRightPanel/.test(src), '主渲染必须包含 MarketRightPanel（右栏）')
  assert.ok(!/WatchlistMonitorTable/.test(src), '禁止 import/使用 WatchlistMonitorTable')
  assert.ok(!/useWatchlistMonitorStatus/.test(src), '禁止 import/使用 useWatchlistMonitorStatus')
  assert.ok(!/adaptWatchlistMonitorStatusItem/.test(src), '禁止 import/使用 adaptWatchlistMonitorStatusItem')
  assert.ok(
    !/self-selection-watchlist-table/.test(src),
    '不得残留 metadata-only 极简表格样式类',
  )
})
