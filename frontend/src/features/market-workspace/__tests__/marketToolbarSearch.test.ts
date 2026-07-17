// [MarketToolbarSearch] - 描述: MarketToolbar 搜索框契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/marketToolbarSearch.test.ts
//
// 覆盖：
// 1. MarketToolbar 接受 keyword/onKeywordChange 受控 props
// 2. placeholder 文案为"搜索股票代码/名称/拼音首字母"
// 3. onChange 更新本地输入（不直接提交）
// 4. Enter 提交（onKeywordChange 调用）
// 5. 失焦提交（onBlur → onKeywordChange）
// 6. 清空立即提交（空串 → onKeywordChange）
// 7. searchable=false 在 MarketWorkspacePage 中关闭表格内置搜索

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

// ===== 1. MarketToolbar 接受受控 keyword props =====
test('MarketToolbar 接受 keyword/onKeywordChange 受控 props', () => {
  const src = readSource(TOOLBAR_PATH)
  assert.ok(
    src.includes('keyword: string') && src.includes('onKeywordChange'),
    'MarketToolbar 必须接受 keyword + onKeywordChange 受控 props',
  )
})

// ===== 2. placeholder 文案 =====
test('MarketToolbar 默认 placeholder 为"搜索股票代码/名称/拼音首字母"', () => {
  const src = readSource(TOOLBAR_PATH)
  assert.ok(
    src.includes('搜索股票代码/名称/拼音首字母'),
    'MarketToolbar placeholder 必须为"搜索股票代码/名称/拼音首字母"',
  )
})

// ===== 3. Enter 提交 =====
test('MarketToolbar Enter 键触发 onKeywordChange', () => {
  const src = readSource(TOOLBAR_PATH)
  assert.ok(
    src.includes("e.key === 'Enter'") && src.includes('onKeywordChange(keywordInput)'),
    'MarketToolbar Enter 键必须触发 onKeywordChange(keywordInput)',
  )
})

// ===== 4. 失焦提交 =====
test('MarketToolbar onBlur 触发 onKeywordChange', () => {
  const src = readSource(TOOLBAR_PATH)
  assert.ok(
    src.includes('onBlur=') && src.includes('onKeywordChange(keywordInput)'),
    'MarketToolbar onBlur 必须触发 onKeywordChange',
  )
})

// ===== 5. 清空立即提交 =====
test('MarketToolbar 清空（空串）立即触发 onKeywordChange', () => {
  const src = readSource(TOOLBAR_PATH)
  // onChange 中 v === '' 时调用 onKeywordChange
  assert.ok(
    src.includes("v === ''") && src.includes("onKeywordChange('')"),
    'MarketToolbar 清空（空串）必须立即触发 onKeywordChange("")',
  )
})

// ===== 6. MarketWorkspacePage searchable=false =====
test('MarketWorkspacePage 传递 searchable={false} 关闭表格内置搜索', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    src.includes('searchable={false}'),
    'MarketWorkspacePage 必须传递 searchable={false}（顶部搜索框是唯一入口）',
  )
})

// ===== 7. MarketWorkspacePage 传递 externalKeyword + onKeywordChange =====
test('MarketWorkspacePage 传递 externalKeyword + onKeywordChange 受控 keyword', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    src.includes('externalKeyword={keyword}'),
    'MarketWorkspacePage 必须传递 externalKeyword={keyword}',
  )
  assert.ok(
    src.includes('onKeywordChange={handleKeywordChange}'),
    'MarketWorkspacePage 必须传递 onKeywordChange={handleKeywordChange}',
  )
})

// ===== 8. 单一搜索状态真源 =====
test('MarketWorkspacePage 只有一个搜索状态（keyword state + MarketToolbar），无重复搜索 input', () => {
  const src = readSource(PAGE_PATH)
  // 不应存在第二个独立搜索 input（表格内置搜索已通过 searchable=false 关闭）
  // keyword state 仅声明一次
  const keywordStateCount = (src.match(/useState<string>.*keyword/g) || []).length
  assert.ok(
    keywordStateCount <= 1,
    `MarketWorkspacePage keyword state 应仅声明一次，实际 ${keywordStateCount}`,
  )
})
