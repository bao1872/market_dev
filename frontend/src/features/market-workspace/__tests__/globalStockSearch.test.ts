// [GlobalStockSearch] - 描述: Global Stock Search 契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/globalStockSearch.test.ts
//
// 覆盖（Round 2 R01/R03/R04/R15/R16 + Round 2.1 修复）：
// 1. GlobalStockSearch 组件存在并被 UserAppShell 挂载于 Global Header
// 2. 主点击进入 Market-source 个股详情（buildStockDetailUrl originScope='market'）
// 3. 主点击不触发 watchlist mutation
// 4. ☆/★ 与股票主点击为两个独立 action，不触发 navigation
// 5. ☆/★ 复用 canonical watchlist mutation（useAddToWatchlist/useRemoveFromWatchlist）
// 6. 无 market_data 时主点击不可导航（权限 split）
// 7. 无 self_selection 时 ☆/★ 不可操作（权限 split）
// 8. 查询源固定为 Market（useMarketStocks scope='market'），不依赖 workspace scope
// A. useWatchlist 受 canManageWatchlist 门控（无 self_selection 不请求 /watchlist）
// B. useMarketStocks 受 searchQuery.length>0 门控（空输入不请求 /market/stocks）
// C. Toast 使用 positional contract，不出现 .show({ 错误调用
// D. UserAppShell topbar z-index 高于 moduleNav

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const SEARCH_PATH = join(__dirname, '..', 'GlobalStockSearch.tsx')
const SHELL_PATH = join(__dirname, '..', '..', '..', 'layouts', 'UserAppShell.tsx')
const SHELL_SCSS_PATH = join(__dirname, '..', '..', '..', 'layouts', 'UserAppShell.module.scss')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 组件存在并被 UserAppShell 挂载 =====
test('GlobalStockSearch 存在且被 UserAppShell 挂载', () => {
  const searchSrc = readSource(SEARCH_PATH)
  const shellSrc = readSource(SHELL_PATH)
  assert.ok(
    searchSrc.includes('export function GlobalStockSearch'),
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
  const mainClickIdx = src.indexOf('function handleMainClick')
  const starIdx = src.indexOf('function handleStarClick')
  const region = mainClickIdx >= 0 ? src.slice(mainClickIdx, starIdx) : ''
  assert.ok(
    !region.includes('addToWatchlist.mutate') &&
      !region.includes('removeFromWatchlist.mutate'),
    'handleMainClick 不得触发 watchlist mutation',
  )
})

// ===== 4. ☆/★ 不触发 navigation =====
test('GlobalStockSearch ☆/★ 不触发 navigation（独立 action）', () => {
  const src = readSource(SEARCH_PATH)
  const start = src.indexOf('handleStarClick =')
  const region = start >= 0 ? src.slice(start) : ''
  assert.ok(
    !region.includes('navigate('),
    'handleStarClick 不得触发 navigate',
  )
  assert.ok(
    region.includes('e.stopPropagation()'),
    'handleStarClick 必须 stopPropagation',
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
      (src.includes('canAccessStockDetail)') || src.includes('!canAccessStockDetail')),
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
    src.includes('useMarketStocks') && src.includes("scope: 'market'"),
    'GlobalStockSearch 必须复用 useMarketStocks(scope="market")',
  )
})

// ===== A. watchlist read 受 canManageWatchlist 门控 =====
test('GlobalStockSearch useWatchlist 受 canManageWatchlist 门控', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes('useWatchlist({') &&
      src.includes('enabled: canManageWatchlist'),
    'GlobalStockSearch 必须按 canManageWatchlist 门控 useWatchlist（无 self_selection 不请求 /watchlist）',
  )
})

// ===== B. market search 受 searchQuery.length>0 门控 =====
test('GlobalStockSearch useMarketStocks 受 searchQuery.length>0 门控', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes('useMarketStocks(') &&
      src.includes('enabled: searchQuery.length > 0'),
    'GlobalStockSearch 必须在 searchQuery 非空时才请求 /market/stocks',
  )
  assert.ok(
    src.includes('const searchQuery = input.trim()'),
    'GlobalStockSearch 必须基于输入 trim 计算 searchQuery',
  )
})

// ===== C. Toast positional contract（不得出现 .show({） =====
test('GlobalStockSearch Toast 使用 positional contract', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    !src.includes('.show({'),
    'GlobalStockSearch 不得出现 useToast().show({ ... }) 错误调用模式',
  )
  assert.ok(
    src.includes("useToast.getState().show('无权限'") &&
      src.includes("useToast.getState().show('操作失败'"),
    'GlobalStockSearch 必须使用 useToast.getState().show(title, message) 形式',
  )
})

// ===== D. UserAppShell topbar z-index > moduleNav =====
test('UserAppShell topbar z-index 高于 moduleNav', () => {
  const scss = readSource(SHELL_SCSS_PATH)
  const topbarMatch = scss.match(/:global\(\.topbar\)\s*\{[^}]*z-index:\s*(\d+)/)
  const moduleNavMatch = scss.match(/\.moduleNav\s*\{[^}]*z-index:\s*(\d+)/)
  assert.ok(topbarMatch && moduleNavMatch, 'topbar 与 moduleNav 必须都声明 z-index')
  const topbarZ = Number(topbarMatch![1])
  const moduleNavZ = Number(moduleNavMatch![1])
  assert.ok(
    topbarZ > moduleNavZ,
    `UserAppShell topbar z-index (${topbarZ}) 必须高于 moduleNav (${moduleNavZ})`,
  )
})
