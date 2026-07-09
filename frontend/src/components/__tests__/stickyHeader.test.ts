// [StickyHeader] - 描述: 趋势选股页 viewport sticky 表头契约源码测试
// 用法：node --experimental-strip-types --test src/components/__tests__/stickyHeader.test.ts
//
// 覆盖：
//   1. StrategyDataTable 支持 stickyHeaderMode="viewport" prop，并为 viewport 模式附加 viewport-sticky class
//   2. ScreenerPage 对 StrategyDataTable 传入 stickyHeaderMode="viewport"
//   3. global.scss 中 viewport-sticky 模式不抢占滚动容器（overflow 不为 auto/scroll/hidden）
//   4. global.scss 中 thead th 的 top 使用 var(--topbar)

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { test } from 'node:test'

function readSrc(...segments: string[]): string {
  return readFileSync(resolve(import.meta.dirname, '..', '..', ...segments), 'utf-8')
}

const strategyDataTableSrc = readSrc('components', 'StrategyDataTable.tsx')
const screenerPageSrc = readSrc('pages', 'ScreenerPage.tsx')
const globalScss = readSrc('styles', 'global.scss')

test('StrategyDataTable 支持 stickyHeaderMode 并给 viewport 模式附加 viewport-sticky class', () => {
  assert.match(
    strategyDataTableSrc,
    /stickyHeaderMode\?:\s*['"]viewport['"]\s*\|\s*['"]container['"]/,
    '应声明 stickyHeaderMode?: "viewport" | "container"',
  )
  assert.match(
    strategyDataTableSrc,
    /clsx\(\s*['"]table-wrap['"]\s*,\s*stickyHeaderMode\s*===\s*['"]viewport['"]\s*&&\s*['"]viewport-sticky['"]\s*\)/,
    '应在 stickyHeaderMode === "viewport" 时附加 viewport-sticky class',
  )
})

test('ScreenerPage 使用 stickyHeaderMode="viewport"', () => {
  assert.match(
    screenerPageSrc,
    /stickyHeaderMode\s*=\s*['"]viewport['"]/,
    'ScreenerPage 应传入 stickyHeaderMode="viewport"',
  )
})

test('viewport-sticky 模式不抢占滚动容器（overflow 不为 auto/scroll/hidden）', () => {
  const viewportStickyBlock = globalScss.match(/\.table-wrap\.viewport-sticky\s*\{[^}]*\}/s)
  assert.ok(viewportStickyBlock, '应存在 .table-wrap.viewport-sticky 规则块')
  const block = viewportStickyBlock[0]
  const overflowValue = block.match(/overflow\s*:\s*([^;]+)/)?.[1]?.trim()
  assert.ok(
    overflowValue && !['auto', 'scroll', 'hidden'].includes(overflowValue),
    `viewport-sticky 模式的 overflow 不应为 auto/scroll/hidden，实际为 ${overflowValue}`,
  )
})

test('viewport-sticky 模式下 thead th 的 top 使用 var(--topbar)', () => {
  const viewportStickyThBlock = globalScss.match(
    /\.table-wrap\.viewport-sticky\s+\.data-table\s+th\s*\{[^}]*\}/s,
  )
  assert.ok(
    viewportStickyThBlock,
    '应存在 .table-wrap.viewport-sticky .data-table th 规则块',
  )
  const block = viewportStickyThBlock[0]
  assert.match(
    block,
    /top\s*:\s*var\(--topbar\)/,
    'viewport-sticky 模式下表头 th 的 top 应使用 var(--topbar)',
  )
  assert.match(
    block,
    /z-index\s*:\s*18/,
    'viewport-sticky 模式下表头 th 的 z-index 应为 18',
  )
})
