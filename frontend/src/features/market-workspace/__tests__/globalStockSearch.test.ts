// [GlobalStockSearch] - 描述: Global Stock Search 契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/globalStockSearch.test.ts
//
// 覆盖（Round 2 R01/R03/R04/R15/R16 + Round 2.1 修复 + 顶部搜索数据源修正）：
// 1. GlobalStockSearch 组件存在并被 UserAppShell 挂载于 Global Header
// 2. 主点击进入 Market-source 个股详情（buildStockDetailUrl originScope='market'）
// 3. 主点击不触发 watchlist mutation
// 4. ☆/★ 与股票主点击为两个独立 action，不触发 navigation
// 5. ☆/★ 复用 canonical watchlist mutation（useAddToWatchlist/useRemoveFromWatchlist）
// 6. 无 market_data 时主点击不可导航（权限 split）
// 7. 无 self_selection 时 ☆/★ 不可操作（权限 split）
// 8. 查询源为 instrument discovery（useInstruments / GET /v1/instruments），
//    禁止 useMarketStocks（/v1/market/stocks 要求 market_data，self_selection-only 会 403）
// 9. useInstruments 参数 keyword/page/page_size 映射 + searchQuery.length>0 门控
// A. useWatchlist 受 canManageWatchlist 门控（无 self_selection 不请求 /watchlist）
// B. /v1/instruments 返回 Instrument.id，直接消费 item.id（key/has/add/remove），
//    禁止 unsafe cast 掩盖 id/instrument_id 字段差异
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
// fail-closed：先断言两个 handler 真实存在且顺序正确，
// 否则 slice 会得到空串/错误区间，导致假阳性 PASS。
test('GlobalStockSearch 主点击不调用 add/remove watchlist mutation', () => {
  const src = readSource(SEARCH_PATH)
  const mainClickIdx = src.indexOf('handleMainClick =')
  const starIdx = src.indexOf('handleStarClick =')
  assert.ok(mainClickIdx >= 0, '必须找到 handleMainClick handler 定义')
  assert.ok(starIdx > mainClickIdx, 'handleStarClick 必须位于 handleMainClick 之后')
  const region = src.slice(mainClickIdx, starIdx)
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

// ===== 8. 查询源 = instrument discovery（禁止 /market/stocks）=====
test('GlobalStockSearch 查询源为 useInstruments，禁止 useMarketStocks', () => {
  const src = readSource(SEARCH_PATH)
  assert.ok(
    src.includes('useInstruments'),
    'GlobalStockSearch 必须使用 useInstruments（GET /v1/instruments）',
  )
  // 精确断言 import/调用层（组件注释可解释性文字不受误伤）
  assert.ok(
    !/import\s*\{[^}]*useMarketStocks/.test(src) &&
      !/useMarketStocks\s*\(/.test(src),
    'GlobalStockSearch 禁止 import 或调用 useMarketStocks（/v1/market/stocks）',
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

// ===== 9. useInstruments 参数映射 + searchQuery.length>0 门控 =====
test('GlobalStockSearch useInstruments 受 searchQuery.length>0 门控且 keyword 映射正确', () => {
  const src = readSource(SEARCH_PATH)
  const m = src.match(/useInstruments\s*\([^)]*?keyword:\s*searchQuery\s*\|\|\s*undefined[^)]*?page:\s*1[^)]*?page_size:\s*8[^)]*?enabled:\s*searchQuery\.length\s*>\s*0/)
  assert.ok(m, 'useInstruments 参数必须为 { keyword: searchQuery || undefined, page: 1, page_size: 8 } 且 enabled: searchQuery.length > 0')
  assert.ok(
    src.includes('const searchQuery = input.trim()'),
    'GlobalStockSearch 必须基于输入 trim 计算 searchQuery',
  )
})

// ===== B. Instrument.id 字段处理（禁止 unsafe cast）=====
test('GlobalStockSearch 直接消费 Instrument.id，禁止 id/instrument_id unsafe cast', () => {
  const src = readSource(SEARCH_PATH)
  // 直接消费 Instrument 类型
  assert.ok(
    /const results:\s*Instrument\[\]\s*=\s*data\?\.items\s*\?\?\s*\[\]/.test(src),
    'results 必须声明为 Instrument[]（直接消费 /v1/instruments 返回类型）',
  )
  // 禁止 as StockSearchItem[] cast 掩盖字段差异
  assert.ok(
    !/as\s+StockSearchItem/.test(src),
    '禁止 as StockSearchItem[] 掩盖 id/instrument_id 字段差异',
  )
  // item.id 统一用于 key / 自选判定 / add / remove
  assert.ok(
    src.includes('key={item.id}') &&
      src.includes('watchlistInstrumentIds.has(item.id)') &&
      src.includes('removeFromWatchlist.mutate(item.id') &&
      src.includes('{ instrument_id: item.id }'),
    '必须统一使用 item.id 作为 key/has/remove 标识，add 用 { instrument_id: item.id }',
  )
  // 不得残留 item.instrument_id（undefined 泄漏点）。
  // 注意：useWatchlist 自选摘要项（watchlistData 循环）的 instrument_id 是合法字段，
  // 因此仅检查搜索消费区域（handleMainClick 之后）。
  const searchRegion = src.slice(src.indexOf('const handleMainClick ='))
  assert.ok(
    searchRegion.includes('const handleMainClick') &&
      !searchRegion.includes('item.instrument_id'),
    '搜索结果消费区禁止残留 item.instrument_id（/v1/instruments 无此字段，会变 undefined）',
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
