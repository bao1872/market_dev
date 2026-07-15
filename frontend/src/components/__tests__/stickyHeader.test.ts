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
    /clsx\(\s*['"]table-shell['"]\s*,\s*stickyHeaderMode\s*===\s*['"]viewport['"]\s*&&\s*['"]viewport-sticky['"]\s*\)/,
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
  // CHANGE-20260715-005: table-wrap → table-shell；规则改为 .table-shell.viewport-sticky .table-scroll
  const viewportStickyBlock = globalScss.match(/\.table-shell\.viewport-sticky\s+\.table-scroll\s*\{[^}]*\}/s)
  assert.ok(viewportStickyBlock, '应存在 .table-shell.viewport-sticky .table-scroll 规则块')
  const block = viewportStickyBlock[0]
  const overflowValue = block.match(/overflow\s*:\s*([^;]+)/)?.[1]?.trim()
  assert.ok(
    overflowValue && !['auto', 'scroll', 'hidden'].includes(overflowValue),
    `viewport-sticky 模式的 overflow 不应为 auto/scroll/hidden，实际为 ${overflowValue}`,
  )
})

test('viewport-sticky 模式下 thead th 的 top 使用 var(--topbar)', () => {
  const viewportStickyThBlock = globalScss.match(
    /\.table-shell\.viewport-sticky\s+\.data-table\s+th\s*\{[^}]*\}/s,
  )
  assert.ok(
    viewportStickyThBlock,
    '应存在 .table-shell.viewport-sticky .data-table th 规则块',
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

// ===== CHANGE-20260715-007: 表格横向滚动与 sticky 四态不透明契约 =====

test('CHANGE-007-table-1: data-table 使用 border-collapse: separate; border-spacing: 0', () => {
  const dataTableBlock = globalScss.match(/\.data-table\s*\{[^}]*\}/s)
  assert.ok(dataTableBlock, '应存在 .data-table 规则块')
  assert.match(dataTableBlock[0], /border-collapse\s*:\s*separate/, 'data-table 必须使用 border-collapse: separate')
  assert.match(dataTableBlock[0], /border-spacing\s*:\s*0/, 'data-table 必须使用 border-spacing: 0')
  // 禁止 border-collapse: collapse
  assert.ok(!/border-collapse\s*:\s*collapse/.test(dataTableBlock[0]), '禁止 border-collapse: collapse')
})

test('CHANGE-007-table-2: table-shell flex 约束（flex:1 1 auto; width/max-width:100%; min-width:0; min-height:0; overflow:hidden）', () => {
  const shellBlock = globalScss.match(/\.table-shell\s*\{[^}]*\}/s)
  assert.ok(shellBlock, '应存在 .table-shell 规则块')
  const block = shellBlock[0]
  assert.match(block, /flex\s*:\s*1\s+1\s+auto/, 'table-shell 必须有 flex: 1 1 auto')
  assert.match(block, /width\s*:\s*100%/, 'table-shell 必须有 width: 100%')
  assert.match(block, /max-width\s*:\s*100%/, 'table-shell 必须有 max-width: 100%')
  assert.match(block, /min-width\s*:\s*0/, 'table-shell 必须有 min-width: 0')
  assert.match(block, /min-height\s*:\s*0/, 'table-shell 必须有 min-height: 0')
  assert.match(block, /overflow\s*:\s*hidden/, 'table-shell 必须有 overflow: hidden')
})

test('CHANGE-007-table-3: table-scroll flex 约束 + scrollbar-gutter: stable', () => {
  // 匹配行首的独立 .table-scroll 规则（排除 .table-shell.viewport-sticky .table-scroll 嵌套规则）
  const scrollBlock = globalScss.match(/(?:^|\n)\.table-scroll\s*\{[^}]*\}/)
  assert.ok(scrollBlock, '应存在独立的 .table-scroll 规则块（行首）')
  const block = scrollBlock[0]
  assert.match(block, /flex\s*:\s*1\s+1\s+auto/, 'table-scroll 必须有 flex: 1 1 auto')
  assert.match(block, /min-width\s*:\s*0/, 'table-scroll 必须有 min-width: 0')
  assert.match(block, /min-height\s*:\s*0/, 'table-scroll 必须有 min-height: 0')
  assert.match(block, /overflow\s*:\s*auto/, 'table-scroll 必须有 overflow: auto')
  assert.match(block, /scrollbar-gutter\s*:\s*stable/, 'table-scroll 必须有 scrollbar-gutter: stable')
})

test('CHANGE-007-table-4: sticky-col 和 table-select-column 四态背景完全不透明（禁止 rgba）', () => {
  // 提取所有 sticky-col 和 table-select-column 的 background 规则
  // 四态：normal / hover / row-active / row-active+hover
  // normal
  const normalSticky = globalScss.match(/tbody\s+td\.sticky-col\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(normalSticky, '应存在 tbody td.sticky-col background 规则')
  assert.ok(!normalSticky![1].includes('rgba'), `normal sticky-col background 不得为 rgba，实际: ${normalSticky![1]}`)
  const normalSelect = globalScss.match(/tbody\s+td\.table-select-column\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(normalSelect, '应存在 tbody td.table-select-column background 规则')
  assert.ok(!normalSelect![1].includes('rgba'), `normal select background 不得为 rgba，实际: ${normalSelect![1]}`)
  // hover
  const hoverSticky = globalScss.match(/tr:hover\s+td\.sticky-col\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(hoverSticky, '应存在 tr:hover td.sticky-col background 规则')
  assert.ok(!hoverSticky![1].includes('rgba'), `hover sticky-col background 不得为 rgba，实际: ${hoverSticky![1]}`)
  const hoverSelect = globalScss.match(/tr:hover\s+td\.table-select-column\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(hoverSelect, '应存在 tr:hover td.table-select-column background 规则')
  assert.ok(!hoverSelect![1].includes('rgba'), `hover select background 不得为 rgba，实际: ${hoverSelect![1]}`)
  // row-active
  const activeSticky = globalScss.match(/tr\.row-active\s+td\.sticky-col\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(activeSticky, '应存在 tr.row-active td.sticky-col background 规则')
  assert.ok(!activeSticky![1].includes('rgba'), `row-active sticky-col background 不得为 rgba，实际: ${activeSticky![1]}`)
  const activeSelect = globalScss.match(/tr\.row-active\s+td\.table-select-column\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(activeSelect, '应存在 tr.row-active td.table-select-column background 规则')
  assert.ok(!activeSelect![1].includes('rgba'), `row-active select background 不得为 rgba，实际: ${activeSelect![1]}`)
  // row-active + hover
  const activeHoverSticky = globalScss.match(/tr\.row-active:hover\s+td\.sticky-col\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(activeHoverSticky, '应存在 tr.row-active:hover td.sticky-col background 规则')
  assert.ok(!activeHoverSticky![1].includes('rgba'), `row-active:hover sticky-col background 不得为 rgba，实际: ${activeHoverSticky![1]}`)
  const activeHoverSelect = globalScss.match(/tr\.row-active:hover\s+td\.table-select-column\s*\{[^}]*background\s*:\s*([^;]+)/)
  assert.ok(activeHoverSelect, '应存在 tr.row-active:hover td.table-select-column background 规则')
  assert.ok(!activeHoverSelect![1].includes('rgba'), `row-active:hover select background 不得为 rgba，实际: ${activeHoverSelect![1]}`)
})

test('CHANGE-007-table-5: tbody checkbox stopPropagation 防止误触发行点击', () => {
  // checkbox <td> 必须有 onClick stopPropagation
  assert.match(
    strategyDataTableSrc,
    /<td\s+className="table-select-column"\s+onClick=\{[^}]*stopPropagation/,
    'tbody checkbox td 必须有 onClick stopPropagation',
  )
})

test('CHANGE-007-table-6: 列筛选按钮 stopPropagation', () => {
  // th-filter 按钮的 onClick 必须包含 stopPropagation
  const filterBtnMatch = strategyDataTableSrc.match(/'th-filter'[\s\S]*?onClick=\{[^}]*stopPropagation/)
  assert.ok(filterBtnMatch, '列筛选按钮 onClick 必须包含 stopPropagation')
})
