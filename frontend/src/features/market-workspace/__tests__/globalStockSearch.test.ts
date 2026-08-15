// [GlobalStockSearch] - 描述: Global Stock Search 契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/globalStockSearch.test.ts
//
// 覆盖（Round 2 R01/R03/R04/R15/R16）：
// 1. GlobalStockSearch 组件存在并被 UserAppShell 挂载于 Global Header
// 2. 主点击进入 Market-source 个股详情（buildStockDetailUrl originScope='market'）
// 3. 主点击不触发 watchlist mutation
// 4. ☆/★ 与股票主点击为两个独立 action，不触发 navigation
// 5. ☆/★ 复用 canonical watchlist mutation（useAddToWatchlist/useRemoveFromWatchlist）
// 6. 无 market_data 时主点击不可导航（权限 split）
// 7. 无 self_selection 时 ☆/★ 不可操作（权限 split）
// 8. 查询源固定为 Market（useMarketStocks scope='market'），不依赖 workspace scope

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const SEARCH_PATH = join(__dirname, '..', 'GlobalStockSearch.tsx')
const SHELL_PATH = join(__dirname, '..', '..', '..', 'layouts', 'UserAppShell.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 组件存在并被 UserAppShell 挂载 =====
test('GlobalStockSearch 存在且被 UserAppShell 挂载', () => {
  const searchSrc = readSource(SEARCH_PATH)
  const shellSrc = readSource(SHELL_PATH)
  assert.ok(
    searchSrc.includes('export default function GlobalStockSearch'),
    'GlobalStockSearch 组件必须存在',
  )
  assert.ok(
    shellSrc.includes('GlobalStockSearch') &&
      !shellSrc.includes('// GlobalStockSearch'),
    'UserAppShell 必须挂载 GlobalStockSearch',
  )
})

// ===== 2. 主点击进入 Market-source 详情 =====
test('GlobalStockSearch 主点击使用 originScope=market 的 canonical navigation', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes("originScope: 'market'") &&
      src.includes('buildStockDetailUrl'),
    'GlobalStockSearch 主点击必须复用 buildStockDetailUrl(symbol, { originScope: "market" })',
  )
})

// ===== 3. 主点击不触发 watchlist mutation =====
test('GlobalStockSearch 主点击不调用 add/remove watchlist mutation', () => {
  const src = readSource(SEARCH_PATH)
  // handleMainClick 内不应包含 mutation 调用
  const mainClickIdx = src.indexOf('function handleMainClick')
  const watchlistToggleIdx = src.indexOf('function handleWatchlistToggle')
  const region = mainClickIdx >= 0 ? src.slice(mainClickIdx, watchlistToggleIdx) : ''
  assert.ok(
    !region.includes('addToWatchlist.mutate') &&
      !region.includes('removeFromWatchlist.mutate'),
    'handleMainClick 不得触发 watchlist mutation',
  )
})

// ===== 4. ☆/★ 不触发 navigation =====
test('GlobalStockSearch ☆/★ 不触发 navigation（独立 action）', () => {
  const src = readSource(SEARCH_PATH)
  const start = src.indexOf('function handleWatchlistToggle')
  const region = start >= 0 ? src.slice(start) : ''
  assert.ok(
    !region.includes('navigate('),
    'handleWatchlistToggle 不得触发 navigate',
  )
  assert.ok(
    region.includes('event.stopPropagation()'),
    'handleWatchlistToggle 必须 stopPropagation',
  )
})

// ===== 5. ☆/★ 复用 canonical mutation =====
test('GlobalStockSearch ☆/★ 复用 useAddToWatchlist/useRemoveFromWatchlist', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes('useAddToWatchlist') &&
      src.includes('useRemoveFromWatchlist') &&
      src.includes('addToWatchlist.mutate') &&
      src.includes('removeFromWatchlist.mutate'),
    'GlobalStockSearch 必须复用 canonical watchlist mutation hooks',
  )
})

// ===== 6. market_data 权限 split（主点击） =====
test('GlobalStockSearch 无 market_data 时主点击不可导航', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes('canAccessStockDetail') &&
      src.includes('market_data') &&
      (src.includes("canAccessStockDetail)") || src.includes('!canAccessStockDetail')),
    'GlobalStockSearch 必须按 market_data（或 admin）控制主点击导航',
  )
})

// ===== 7. self_selection 权限 split（☆/★） =====
test('GlobalStockSearch 无 self_selection 时 ☆/★ 不可操作', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes('canManageWatchlist') &&
      src.includes('self_selection'),
    'GlobalStockSearch 必须按 self_selection（或 admin）控制 ☆/★ 操作',
  )
})

// ===== 8. 查询源固定为 Market =====
test('GlobalStockSearch 查询源固定为 Market（scope=market）', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes("useMarketStocks") && src.includes("scope: 'market'"),
    'GlobalStockSearch 必须复用 useMarketStocks(scope="market")',
  )
})
