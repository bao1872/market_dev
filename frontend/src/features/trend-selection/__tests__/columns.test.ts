// [趋势选股] - 描述: columns.tsx 列定义测试
// 用法：node --experimental-strip-types --test src/features/trend-selection/__tests__/columns.test.ts
//
// 覆盖：
// 1. change_pct 独立列存在（key=change_pct, title=当日涨跌幅, shortTitle=涨跌幅）
// 2. change_pct 列属性（dataType=percent, sortable=true, filterable=true, width≈86）
// 3. change_pct 列渲染使用 fmtChange + 涨红跌绿颜色
// 4. change_pct 列 sortValue 正确读取 payload
// 5. change_pct 列位于 stock 列之后（用户体验：股票-涨跌幅-趋势...）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const COLUMNS_PATH = join(__dirname, '..', 'columns.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

/** 从源码中提取指定 key 列定义的代码块（key: 'xxx' 到下一个 key: 'yyy' 之前） */
function extractColumnBlock(src: string, key: string): string {
  const startMarker = `key: '${key}'`
  const startIdx = src.indexOf(startMarker)
  if (startIdx < 0) {
    return ''
  }
  // 找到下一个 key: '...' 的位置（同层级的下一列）
  const afterStart = src.substring(startIdx + startMarker.length)
  const nextKeyMatch = afterStart.match(/\n\s*key:\s*['"][^'"]+['"]/)
  const endIdx = nextKeyMatch
    ? startIdx + startMarker.length + nextKeyMatch.index!
    : src.length
  return src.substring(startIdx, endIdx)
}

// ===== 1. change_pct 独立列存在 =====
test('columns.tsx 包含 change_pct 独立列定义', () => {
  const src = readSource(COLUMNS_PATH)
  assert.ok(
    src.includes("key: 'change_pct'"),
    'columns.tsx 必须包含 key="change_pct" 的独立列定义',
  )
})

// ===== 2. change_pct 列 title/shortTitle =====
test('change_pct 列 title=当日涨跌幅, shortTitle=涨跌幅', () => {
  const src = readSource(COLUMNS_PATH)
  const block = extractColumnBlock(src, 'change_pct')
  assert.ok(block, '找不到 change_pct 列定义')
  assert.ok(
    /title:\s*['"]当日涨跌幅['"]/.test(block),
    'change_pct 列 title 必须为 "当日涨跌幅"',
  )
  assert.ok(
    /shortTitle:\s*['"]涨跌幅['"]/.test(block),
    'change_pct 列 shortTitle 必须为 "涨跌幅"',
  )
})

// ===== 3. change_pct 列 dataType/sortable/filterable/width =====
test('change_pct 列 dataType=percent, sortable=true, filterable=true, width 约 86', () => {
  const src = readSource(COLUMNS_PATH)
  const block = extractColumnBlock(src, 'change_pct')
  assert.ok(block, '找不到 change_pct 列定义')
  assert.ok(
    /dataType:\s*['"]percent['"]/.test(block),
    'change_pct 列 dataType 必须为 percent',
  )
  assert.ok(
    /sortable:\s*true/.test(block),
    'change_pct 列 sortable 必须为 true',
  )
  assert.ok(
    /filterable:\s*true/.test(block),
    'change_pct 列 filterable 必须为 true',
  )
  // [趋势选股] - 描述: width 约 86（允许 80-90 范围）
  const widthMatch = block.match(/width:\s*(\d+)/)
  assert.ok(widthMatch, 'change_pct 列必须定义 width')
  const width = parseInt(widthMatch[1], 10)
  assert.ok(
    width >= 80 && width <= 90,
    `change_pct 列 width 应在 80-90 范围内，实际 ${width}`,
  )
})

// ===== 4. change_pct 列渲染使用 fmtChange + 涨红跌绿 =====
test('change_pct 列渲染使用 fmtChange + changePctColorClass（涨红跌绿）', () => {
  const src = readSource(COLUMNS_PATH)
  const block = extractColumnBlock(src, 'change_pct')
  assert.ok(block, '找不到 change_pct 列定义')
  assert.ok(
    block.includes('fmtChange'),
    'change_pct 列渲染必须使用 fmtChange（正数带 + 号）',
  )
  assert.ok(
    block.includes('changePctColorClass'),
    'change_pct 列渲染必须使用 changePctColorClass（涨红跌绿颜色）',
  )
})

// ===== 5. change_pct 列 sortValue 正确读取 payload =====
test('change_pct 列 sortValue 从 payload 读取 change_pct 字段', () => {
  const src = readSource(COLUMNS_PATH)
  const block = extractColumnBlock(src, 'change_pct')
  assert.ok(block, '找不到 change_pct 列定义')
  // [趋势选股] - 描述: sortValue 必须从 payload 读取 change_pct/pct_change/change_percent 候选 key
  assert.ok(
    /sortValue:\s*\(row\)/.test(block),
    'change_pct 列必须定义 sortValue 函数',
  )
  // 验证读取的候选 key 至少包含 change_pct
  assert.ok(
    block.includes('change_pct') || block.includes('CHANGE_PCT_KEYS'),
    'change_pct 列 sortValue 必须读取 change_pct 字段（或复用 CHANGE_PCT_KEYS）',
  )
})

// ===== 6. change_pct 列位于 stock 列之后 =====
test('change_pct 列位于 stock 列之后（用户体验：股票-涨跌幅-趋势...）', () => {
  const src = readSource(COLUMNS_PATH)
  const stockIdx = src.indexOf("key: 'stock'")
  const changePctIdx = src.indexOf("key: 'change_pct'")
  assert.ok(stockIdx >= 0, '必须存在 stock 列')
  assert.ok(changePctIdx >= 0, '必须存在 change_pct 列')
  assert.ok(
    changePctIdx > stockIdx,
    'change_pct 列必须位于 stock 列之后',
  )
  // [趋势选股] - 描述: change_pct 应紧跟 stock 之后，中间不应有其他列
  // 检查 stock 与 change_pct 之间没有其他 key: 'xxx' 出现
  const between = src.substring(stockIdx, changePctIdx)
  const otherKeyInBetween = between.match(/\n\s*key:\s*['"][^'"]+['"]/g)
  assert.ok(
    !otherKeyInBetween || otherKeyInBetween.length === 0,
    `stock 与 change_pct 之间不应有其他列，实际存在: ${JSON.stringify(otherKeyInBetween)}`,
  )
})

// ===== 7. action 列按钮 stopPropagation 防止行选中副作用 =====
test('action 列 onDetail 按钮 onClick 调用 e.stopPropagation() 防止冒泡到 tr onClick', () => {
  const src = readSource(COLUMNS_PATH)
  // 找到 onDetail 按钮的 onClick
  const detailBtnMatch = src.match(/onClick=\{[^}]*onDetail[^}]*\}/)
  assert.ok(detailBtnMatch, '必须存在 onDetail 按钮 onClick')
  assert.ok(
    detailBtnMatch![0].includes('stopPropagation'),
    `onDetail 按钮 onClick 必须调用 e.stopPropagation() 防止冒泡到 <tr onClick>，实际: ${detailBtnMatch![0]}`,
  )
})

// ===== 8. action 列 onAddToWatchlist 按钮 stopPropagation =====
test('action 列 onAddToWatchlist 按钮 onClick 调用 e.stopPropagation() 防止冒泡到 tr onClick', () => {
  const src = readSource(COLUMNS_PATH)
  // 找到 onAddToWatchlist 按钮的 onClick
  const watchlistBtnMatch = src.match(/onClick=\{[^}]*onAddToWatchlist[^}]*\}/)
  assert.ok(watchlistBtnMatch, '必须存在 onAddToWatchlist 按钮 onClick')
  assert.ok(
    watchlistBtnMatch![0].includes('stopPropagation'),
    `onAddToWatchlist 按钮 onClick 必须调用 e.stopPropagation() 防止冒泡到 <tr onClick>，实际: ${watchlistBtnMatch![0]}`,
  )
})

// ===== 9. onNavigateToStock 链接 stopPropagation =====
test('股票名称链接 onNavigate 调用 e.stopPropagation() 防止冒泡到 tr onClick', () => {
  const src = readSource(COLUMNS_PATH)
  // 找到 onNavigate 链接的 onClick
  const navMatch = src.match(/onClick=\{[^}]*onNavigate[^}]*\}/)
  assert.ok(navMatch, '必须存在 onNavigate 链接 onClick')
  assert.ok(
    navMatch![0].includes('stopPropagation'),
    `onNavigate 链接 onClick 必须调用 e.stopPropagation() 防止冒泡到 <tr onClick>，实际: ${navMatch![0]}`,
  )
})

// ===== 10. onToggleWatchlist 按钮 stopPropagation + 加入/移除自选 =====
test('action 列 onToggleWatchlist 模式：按钮 onClick 调用 e.stopPropagation()，显示加入/移除自选', () => {
  const src = readSource(COLUMNS_PATH)
  // 验证 onToggleWatchlist 按钮存在且 stopPropagation
  const toggleMatches = src.matchAll(/onClick=\{[^}]*onToggleWatchlist[^}]*\}/g)
  const toggles = [...toggleMatches]
  assert.ok(toggles.length > 0, '必须存在 onToggleWatchlist 按钮 onClick')
  for (const t of toggles) {
    assert.ok(
      t[0].includes('stopPropagation'),
      `onToggleWatchlist 按钮 onClick 必须调用 e.stopPropagation()，实际: ${t[0]}`,
    )
  }
  // 验证显示"加入自选"和"移除自选"文案
  assert.ok(src.includes('加入自选'), 'action 列必须显示"加入自选"按钮')
  assert.ok(src.includes('移除自选'), 'action 列必须显示"移除自选"按钮')
})

// ===== 11. onToggleWatchlist 模式下列标题为"自选" =====
test('action 列 onToggleWatchlist 模式下 title 动态为"自选"', () => {
  const src = readSource(COLUMNS_PATH)
  // 验证 title 根据 onToggleWatchlist 动态切换
  assert.ok(
    src.includes("onToggleWatchlist ? '自选' : '操作'"),
    'action 列 title 必须在 onToggleWatchlist 模式下为"自选"',
  )
})

// ===== 12. onNavigateToStock 链接使用 <a> 标签 + preventDefault =====
test('股票名称链接使用 <a> 标签并 preventDefault 避免页面跳转', () => {
  const src = readSource(COLUMNS_PATH)
  // 验证 renderStock 函数中使用 <a 标签
  assert.ok(
    src.includes('e.preventDefault()'),
    '股票名称链接必须调用 e.preventDefault() 防止默认页面跳转',
  )
})

// ===== 13. 股票单元格不再显示行内涨跌幅（独立涨跌幅列保留） =====
test('renderStock 函数只显示名称/代码/市场，不渲染行内涨跌幅', () => {
  const src = readSource(COLUMNS_PATH)
  // 提取 renderStock 函数体
  const renderStockMatch = src.match(/function renderStock[\s\S]*?\n\}/)
  assert.ok(renderStockMatch, '必须存在 renderStock 函数')
  const body = renderStockMatch![0]
  // renderStock 不应包含 change_pct / changePct / fmtChange
  assert.ok(
    !body.includes('change_pct') && !body.includes('changePct') && !body.includes('fmtChange'),
    'renderStock 不应渲染行内涨跌幅（独立涨跌幅列已保留）',
  )
  // 应包含 name, symbol, market
  assert.ok(body.includes('name'), 'renderStock 必须显示名称')
  assert.ok(body.includes('symbol'), 'renderStock 必须显示代码')
  assert.ok(body.includes('market'), 'renderStock 必须显示市场')
})
