// [MarketToolbarSearch] - 描述: MarketToolbar 搜索框契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/marketToolbarSearch.test.ts
//
// Round 2（R05）更新：Market 局部可见股票搜索框已迁移到 Global Header（GlobalStockSearch）。
// MarketToolbar 只保留 Industry / Concept 筛选，不再承载 stock search input。
// 覆盖：
// 1. MarketToolbar 不再接受 keyword/onKeywordChange/searchPlaceholder stock-search props
// 2. MarketToolbar 不渲染 aria-label="搜索股票" 的 input
// 3. Industry / Concept 筛选（BoardFilterCombobox）仍然存在
// 4. MarketWorkspacePage 仍向表格传递 externalKeyword + onKeywordChange（底层 query capability 保留）
// 5. MarketWorkspacePage 不存在第二个用户可见 stock search input

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const TOOLBAR_PATH = join(__dirname, '..', 'MarketToolbar.tsx')
const PAGE_PATH = join(__dirname, '..', 'MarketWorkspacePage.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. MarketToolbar 不再接受 stock-search 受控 props =====
test('MarketToolbar 不再接受 keyword/onKeywordChange/searchPlaceholder stock-search props', () => {
  const src = readSource(TOOLBAR_PATH)
  assert.ok(
    !src.includes('keyword: string') &&
      !src.includes('onKeywordChange') &&
      !src.includes('searchPlaceholder'),
    'MarketToolbar 必须移除 keyword/onKeywordChange/searchPlaceholder 股票搜索 props',
  )
})

// ===== 2. MarketToolbar 不渲染股票搜索 input =====
test('MarketToolbar 不渲染 aria-label="搜索股票" 的 input', () => {
  const src = readSource(TOOLBAR_PATH)
  assert.ok(
    !src.includes('aria-label="搜索股票"'),
    'MarketToolbar 不得渲染股票搜索 input（已迁移到 Global Header）',
  )
})

// ===== 3. Industry / Concept 筛选仍然存在 =====
test('MarketToolbar 保留 Industry / Concept 筛选（BoardFilterCombobox）', () => {
  const src = readSource(TOOLBAR_PATH)
  assert.ok(
    src.includes('BoardFilterCombobox') &&
      src.includes('ariaLabel="行业筛选"') &&
      src.includes('ariaLabel="概念筛选"'),
    'MarketToolbar 必须保留 Industry / Concept 筛选',
  )
})

// ===== 4. 底层 keyword query capability 保留（表格 externalKeyword） =====
test('MarketWorkspacePage 仍传递 externalKeyword + onKeywordChange（底层 query 保留）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    src.includes('externalKeyword={keyword}'),
    'MarketWorkspacePage 必须传递 externalKeyword={keyword}（底层 query capability 保留）',
  )
  assert.ok(
    src.includes('onKeywordChange={handleKeywordChange}'),
    'MarketWorkspacePage 必须传递 onKeywordChange={handleKeywordChange}',
  )
})

// ===== 5. 不存在第二个用户可见 stock search input =====
test('MarketWorkspacePage 不存在第二个用户可见 stock search input', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    !src.includes('aria-label="搜索股票"'),
    'MarketWorkspacePage 不得存在股票搜索 input（唯一入口在 Global Header）',
  )
})
