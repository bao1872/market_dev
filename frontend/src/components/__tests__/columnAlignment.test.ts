// [ColumnAlignment] - 描述: P0 表头与值对齐契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/columnAlignment.test.ts
//
// 覆盖（P0 列对齐契约）：
//   1. reorderVisibleColumns 纯函数：默认顺序、隐藏列、columnOrder 重排、action 列固定末尾
//   2. 每行 td 数 = 可见 th 数 = visibleColumns.length（源码契约）
//   3. th/td/colgroup 三者从同一 visibleColumns 派生（源码契约）
//   4. 单元格按 col.key 取值，不依赖数组下标（源码契约）
//   5. action/select 列固定 id，不参与重排（源码契约）
//   6. 明显不同的测试值逐列断言（纯函数验证）

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { test } from 'node:test'
import { reorderVisibleColumns, type ColumnLike } from '../columnOrdering.ts'

function readSrc(...segments: string[]): string {
  return readFileSync(resolve(import.meta.dirname, '..', '..', ...segments), 'utf-8')
}

const strategyDataTableSrc = readSrc('components', 'StrategyDataTable.tsx')

// ===== 1. reorderVisibleColumns 纯函数测试 =====

interface TestCol extends ColumnLike {
  key: string
  isAction?: boolean
  isSelect?: boolean
}

function makeCols(): TestCol[] {
  return [
    { key: 'stock' },
    { key: 'change_pct' },
    { key: 'dsa_dir_bars' },
    { key: 'vwap_ret_avg' },
    { key: 'offset_mean' },
    { key: 'price' },
    { key: 'action', isAction: true },
  ]
}

test('reorderVisibleColumns: 默认顺序（无 columnOrder）按 columns 原始顺序', () => {
  const cols = makeCols()
  const result = reorderVisibleColumns(cols, new Set(), null)
  assert.equal(result.length, cols.length)
  assert.deepEqual(
    result.map((r) => r.col.key),
    ['stock', 'change_pct', 'dsa_dir_bars', 'vwap_ret_avg', 'offset_mean', 'price', 'action'],
  )
  // originalIndex 保留
  assert.deepEqual(
    result.map((r) => r.originalIndex),
    [0, 1, 2, 3, 4, 5, 6],
  )
})

test('reorderVisibleColumns: 空 columnOrder 也按原始顺序', () => {
  const cols = makeCols()
  const result = reorderVisibleColumns(cols, new Set(), [])
  assert.equal(result.length, cols.length)
  assert.deepEqual(
    result.map((r) => r.col.key),
    ['stock', 'change_pct', 'dsa_dir_bars', 'vwap_ret_avg', 'offset_mean', 'price', 'action'],
  )
})

test('reorderVisibleColumns: hiddenColumns 过滤对应列', () => {
  const cols = makeCols()
  const hidden = new Set(['change_pct', 'offset_mean'])
  const result = reorderVisibleColumns(cols, hidden, null)
  assert.equal(result.length, 5)
  assert.deepEqual(
    result.map((r) => r.col.key),
    ['stock', 'dsa_dir_bars', 'vwap_ret_avg', 'price', 'action'],
  )
  // originalIndex 保留原始索引（不是新数组索引）
  assert.deepEqual(
    result.map((r) => r.originalIndex),
    [0, 2, 3, 5, 6],
  )
})

test('reorderVisibleColumns: columnOrder 重排管理列，action 列固定末尾', () => {
  const cols = makeCols()
  // 将 price 移到最前，stock 移到 price 之后
  const order = ['price', 'stock', 'change_pct', 'dsa_dir_bars', 'vwap_ret_avg', 'offset_mean']
  const result = reorderVisibleColumns(cols, new Set(), order)
  assert.equal(result.length, 7)
  assert.deepEqual(
    result.map((r) => r.col.key),
    ['price', 'stock', 'change_pct', 'dsa_dir_bars', 'vwap_ret_avg', 'offset_mean', 'action'],
  )
  // action 列的 originalIndex 仍为 6
  const actionEntry = result.find((r) => r.col.key === 'action')
  assert.equal(actionEntry?.originalIndex, 6)
})

test('reorderVisibleColumns: columnOrder 不完整时，未列出列追加到末尾（action 之前）', () => {
  const cols = makeCols()
  // 只列出部分列，其余按 9999 排序追加
  const order = ['price', 'stock']
  const result = reorderVisibleColumns(cols, new Set(), order)
  assert.equal(result.length, 7)
  // price, stock 在前，其余按原顺序，action 固定末尾
  assert.equal(result[0].col.key, 'price')
  assert.equal(result[1].col.key, 'stock')
  assert.equal(result[result.length - 1].col.key, 'action')
  // 未列出的列保持原始相对顺序
  const unlisted = result.slice(2, -1).map((r) => r.col.key)
  assert.deepEqual(unlisted, ['change_pct', 'dsa_dir_bars', 'vwap_ret_avg', 'offset_mean'])
})

test('reorderVisibleColumns: columnOrder 含陈旧 key 时被忽略', () => {
  const cols = makeCols()
  // 'nonexistent' 不在 columns 中，应被忽略
  const order = ['price', 'nonexistent', 'stock']
  const result = reorderVisibleColumns(cols, new Set(), order)
  assert.equal(result.length, 7)
  assert.equal(result[0].col.key, 'price')
  assert.equal(result[1].col.key, 'stock')
})

test('reorderVisibleColumns: hiddenColumns + columnOrder 组合', () => {
  const cols = makeCols()
  const hidden = new Set(['change_pct', 'vwap_ret_avg'])
  const order = ['price', 'stock', 'dsa_dir_bars', 'offset_mean']
  const result = reorderVisibleColumns(cols, hidden, order)
  assert.equal(result.length, 5)
  assert.deepEqual(
    result.map((r) => r.col.key),
    ['price', 'stock', 'dsa_dir_bars', 'offset_mean', 'action'],
  )
})

test('reorderVisibleColumns: select 列也固定末尾', () => {
  const cols: TestCol[] = [
    { key: 'stock' },
    { key: 'change_pct' },
    { key: 'select', isSelect: true },
    { key: 'action', isAction: true },
  ]
  const order = ['change_pct', 'stock']
  const result = reorderVisibleColumns(cols, new Set(), order)
  // select 和 action 都固定末尾，保持原顺序
  assert.deepEqual(
    result.map((r) => r.col.key),
    ['change_pct', 'stock', 'select', 'action'],
  )
})

test('reorderVisibleColumns: 空列返回空数组', () => {
  const result = reorderVisibleColumns([], new Set(), null)
  assert.equal(result.length, 0)
})

test('reorderVisibleColumns: 所有列隐藏返回空数组', () => {
  const cols = makeCols()
  const hidden = new Set(cols.map((c) => c.key))
  const result = reorderVisibleColumns(cols, hidden, null)
  assert.equal(result.length, 0)
})

// ===== 2. 明显不同测试值逐列断言 =====
//
// 验证：列定义中每列有唯一 key，reorderVisibleColumns 按 key 派生，
// 不会因数组下标或对象遍历顺序导致错位。

test('明显不同测试值逐列断言：每列 key 唯一且与 originalIndex 对应', () => {
  const cols = makeCols()
  const result = reorderVisibleColumns(cols, new Set(), null)
  // 每列 key 唯一
  const keys = result.map((r) => r.col.key)
  assert.equal(new Set(keys).size, keys.length, '所有列 key 应唯一')
  // originalIndex 与 cols 索引一一对应
  for (let i = 0; i < result.length; i++) {
    assert.equal(
      result[i].originalIndex,
      i,
      `列 ${result[i].col.key} 的 originalIndex 应为 ${i}，实际 ${result[i].originalIndex}`,
    )
    assert.equal(
      result[i].col.key,
      cols[i].key,
      `位置 ${i} 的列 key 应为 ${cols[i].key}，实际 ${result[i].col.key}`,
    )
  }
})

test('明显不同测试值逐列断言：columnOrder 后 key 与位置严格对应', () => {
  const cols = makeCols()
  const order = ['price', 'stock', 'change_pct', 'dsa_dir_bars', 'vwap_ret_avg', 'offset_mean']
  const result = reorderVisibleColumns(cols, new Set(), order)
  // 前 6 项严格按 order 顺序
  for (let i = 0; i < order.length; i++) {
    assert.equal(
      result[i].col.key,
      order[i],
      `位置 ${i} 应为 ${order[i]}，实际 ${result[i].col.key}`,
    )
  }
  // 最后一项是 action
  assert.equal(result[result.length - 1].col.key, 'action')
  // originalIndex 仍指向 cols 中的原始位置
  const priceEntry = result.find((r) => r.col.key === 'price')
  assert.equal(priceEntry?.originalIndex, 5, 'price 的 originalIndex 应为 5')
  const stockEntry = result.find((r) => r.col.key === 'stock')
  assert.equal(stockEntry?.originalIndex, 0, 'stock 的 originalIndex 应为 0')
})

// ===== 3. 源码契约：th/td/colgroup 从同一 visibleColumns 派生 =====

test('源码契约：thead th 从 visibleColumns.map 派生', () => {
  assert.match(
    strategyDataTableSrc,
    /\{visibleColumns\.map\(\(\{ col, originalIndex: i \}\) =>/,
    'thead th 应从 visibleColumns.map 派生',
  )
})

test('源码契约：tbody td 从 visibleColumns.map 派生', () => {
  assert.match(
    strategyDataTableSrc,
    /\{visibleColumns\.map\(\(\{ col \}\) =>/,
    'tbody td 应从 visibleColumns.map 派生',
  )
})

test('源码契约：colgroup col 从 visibleColumns.map 派生', () => {
  assert.match(
    strategyDataTableSrc,
    /\{visibleColumns\.map\(\(\{ col \}\) => \(/,
    'colgroup col 应从 visibleColumns.map 派生',
  )
})

test('源码契约：td 按 col.key 取值（不依赖数组下标）', () => {
  // 验证 td 渲染使用 col.render 或 row[col.key]，不使用 idx/i
  assert.match(
    strategyDataTableSrc,
    /col\.render \? col\.render\(row\) : String\(row\[col\.key\] \?\? ''\)/,
    'td 应按 col.key 取值，不依赖数组下标',
  )
})

test('源码契约：td key 使用 col.key（不使用数组下标）', () => {
  // 验证 td 的 key 是 col.key，不是 idx 或 i
  const tdBlock = strategyDataTableSrc.match(
    /visibleColumns\.map\(\(\{ col \}\) => \{[\s\S]*?return \([\s\S]*?<td[\s\S]*?key=\{col\.key\}/,
  )
  assert.ok(tdBlock, 'td 的 key 应使用 col.key，不使用数组下标')
})

test('源码契约：th key 使用 col.key（不使用数组下标）', () => {
  // 验证 th 的 key 是 col.key
  assert.match(
    strategyDataTableSrc,
    /<th\s+key=\{col\.key\}/,
    'th 的 key 应使用 col.key',
  )
})

test('源码契约：colgroup col key 使用 col.key', () => {
  assert.match(
    strategyDataTableSrc,
    /<col\s+key=\{col\.key\}/,
    'colgroup col 的 key 应使用 col.key',
  )
})

test('源码契约：action 列 isAction 标记且 th 渲染跳过 sticky/sort', () => {
  // 验证 th 渲染中 isAction 列直接 return，不参与 sort/filter/sticky
  assert.match(
    strategyDataTableSrc,
    /if \(col\.isAction\) \{[\s\S]*?return \([\s\S]*?<th key=\{col\.key\} className="table-action-column">[\s\S]*?\)/,
    'isAction 列应渲染固定 th，不参与 sort/filter/sticky',
  )
})

test('源码契约：selectable 列 th/td 固定 id table-select-column', () => {
  // 验证 select 列使用固定 className，不偏移
  assert.match(
    strategyDataTableSrc,
    /<th className="table-select-column">/,
    'select 列 th 应使用固定 className table-select-column',
  )
  assert.match(
    strategyDataTableSrc,
    /<td className="table-select-column">/,
    'select 列 td 应使用固定 className table-select-column',
  )
})

test('源码契约：colSpan 使用 visibleColumns.length + (selectable ? 1 : 0)', () => {
  // 验证空态/加载态/错误态的 colSpan 与可见列数一致
  const matches = strategyDataTableSrc.match(/colSpan=\{visibleColumns\.length \+ \(selectable \? 1 : 0\)\}/g)
  assert.ok(matches && matches.length >= 3, `colSpan 应使用 visibleColumns.length + (selectable ? 1 : 0)，至少 3 处，实际 ${matches?.length ?? 0}`)
})

test('源码契约：min-width 使用 visibleColumnsWidthSum', () => {
  assert.match(
    strategyDataTableSrc,
    /minWidth: `\$\{visibleColumnsWidthSum \+ \(selectable \? 40 : 0\)\}px`/,
    'table min-width 应基于 visibleColumnsWidthSum',
  )
})

// ===== 4. columnOrder 持久化契约 =====

test('源码契约：columnOrder state 存在且默认 null', () => {
  assert.match(
    strategyDataTableSrc,
    /const \[columnOrder, setColumnOrder\] = useState<string\[\] \| null>\(null\)/,
    'columnOrder state 应存在且默认 null',
  )
})

test('源码契约：saveColumnOrder 持久化到 localStorage', () => {
  assert.match(
    strategyDataTableSrc,
    /localStorage\.setItem\(`table-column-order:\$\{tableId\}`, JSON\.stringify\(order\)\)/,
    'saveColumnOrder 应持久化到 localStorage',
  )
})

test('源码契约：onMoveUp/onMoveDown 交换相邻 key', () => {
  // 验证 onMoveUp 交换 idx-1 和 idx
  assert.match(
    strategyDataTableSrc,
    /onMoveUp=\{\(key\) => \{[\s\S]*?\[next\[idx - 1\], next\[idx\]\] = \[next\[idx\], next\[idx - 1\]\]/,
    'onMoveUp 应交换 idx-1 和 idx',
  )
  assert.match(
    strategyDataTableSrc,
    /onMoveDown=\{\(key\) => \{[\s\S]*?\[next\[idx \+ 1\], next\[idx\]\] = \[next\[idx\], next\[idx \+ 1\]\]/,
    'onMoveDown 应交换 idx 和 idx+1',
  )
})

test('源码契约：onReset 清除 hiddenColumns + columnOrder', () => {
  assert.match(
    strategyDataTableSrc,
    /onReset=\{\(\) => \{[\s\S]*?applyColumnVisibility\(new Set\(\)\)[\s\S]*?setColumnOrder\(null\)[\s\S]*?saveColumnOrder\(null\)/,
    'onReset 应清除 hiddenColumns 和 columnOrder',
  )
})

test('源码契约：currentConfig 包含 columnOrder', () => {
  assert.match(
    strategyDataTableSrc,
    /columnOrder: columnOrder \?\? null/,
    'currentConfig 应包含 columnOrder',
  )
})

test('源码契约：applyPresetConfig 应用 columnOrder', () => {
  assert.match(
    strategyDataTableSrc,
    /if \(config\.columnOrder && config\.columnOrder\.length > 0\) \{[\s\S]*?setColumnOrder\(config\.columnOrder\)[\s\S]*?saveColumnOrder\(config\.columnOrder\)/,
    'applyPresetConfig 应应用 columnOrder',
  )
})

test('源码契约：onRowClick/activeRowKey props 存在', () => {
  assert.match(
    strategyDataTableSrc,
    /onRowClick\?: \(row: Row\) => void/,
    '应声明 onRowClick prop',
  )
  assert.match(
    strategyDataTableSrc,
    /activeRowKey\?: string \| null/,
    '应声明 activeRowKey prop',
  )
})

test('源码契约：tr onClick + activeRowKey className', () => {
  assert.match(
    strategyDataTableSrc,
    /onClick=\{onRowClick \? \(\) => onRowClick\(row\) : undefined\}/,
    'tr 应绑定 onRowClick',
  )
  assert.match(
    strategyDataTableSrc,
    /className=\{clsx\(activeRowKey === key && 'row-active'\)\}/,
    'tr 应根据 activeRowKey 添加 row-active className',
  )
})
