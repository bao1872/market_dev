// [自选监控] - 描述: UI 调整契约测试
// 用法：node --experimental-strip-types --test src/features/watchlist-monitor/__tests__/columns.test.ts
// 覆盖：
//   1. 桌面端表格不再包含 status 列（每行状态栏移除）
//   2. 数据列开启表头过滤（filterable=true）
//   3. MonitorStatusBadge 支持 UNKNOWN 占位
//   4. 移动端卡片不再显示每行状态徽章
//   5. WatchlistMonitorTable 使用 compact-table 与趋势选股页对齐
//
// 注意：WatchlistPage.tsx 已删除（统一行情工作区改造），
//   原 WatchlistPage 页眉市场状态测试已移除。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const COLUMNS_PATH = join(__dirname, '..', 'columns.tsx')
const CARDS_PATH = join(__dirname, '..', 'WatchlistMonitorCards.tsx')
const TABLE_PATH = join(__dirname, '..', 'WatchlistMonitorTable.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

/** 从 columns.tsx 源码中提取所有列定义的 key（按出现顺序） */
function extractColumnKeys(src: string): string[] {
  const keys: string[] = []
  const re = /^\s*key:\s*['"]([^'"]+)['"]/gm
  let m: RegExpExecArray | null
  while ((m = re.exec(src)) !== null) {
    keys.push(m[1])
  }
  return keys
}

// ===== 1. 桌面端表格不再包含 status 列 =====
test('columns.tsx 不再包含每行状态栏 status 列', () => {
  const src = readSource(COLUMNS_PATH)
  const keys = extractColumnKeys(src)
  assert.ok(!keys.includes('status'), `status 列应已移除，实际 keys=${JSON.stringify(keys)}`)
})

// ===== 2. 数据列开启表头过滤 =====
test('columns.tsx 数据列均开启 filterable', () => {
  const src = readSource(COLUMNS_PATH)
  const keys = extractColumnKeys(src)
  // 操作列不强制过滤；其余列都应 filterable
  const dataKeys = keys.filter((k) => k !== 'action')
  assert.ok(dataKeys.length >= 8, `数据列至少 8 列，实际 ${dataKeys.length} 列`)
  for (const k of dataKeys) {
    const idx = src.indexOf(`key: '${k}'`)
    assert.ok(idx >= 0, `找不到 key="${k}" 的列定义`)
    const block = src.substring(idx, idx + 600)
    assert.ok(
      /filterable:\s*true/.test(block),
      `数据列 key="${k}" 必须 filterable=true（恢复表头过滤）`,
    )
  }
})

// ===== 3. MonitorStatusBadge 支持 UNKNOWN 全局状态占位 =====
test('MonitorStatusBadge 支持 UNKNOWN 占位状态', () => {
  const src = readSource(COLUMNS_PATH)
  assert.ok(
    src.includes("status: MonitorStatus | 'UNKNOWN'"),
    'MonitorStatusBadge props 应接受 UNKNOWN 占位',
  )
  assert.ok(src.includes("case 'UNKNOWN':"), 'MonitorStatusBadge switch 应处理 UNKNOWN')
})

// ===== 4. 移动端卡片不再显示每行状态徽章 =====
test('WatchlistMonitorCards 不再渲染每行状态徽章', () => {
  const src = readSource(CARDS_PATH)
  assert.ok(
    !src.includes('MonitorStatusBadge'),
    'WatchlistMonitorCards 不应再使用 MonitorStatusBadge',
  )
})

// ===== 5. WatchlistMonitorTable 使用 compact-table 对齐趋势选股页 =====
test('WatchlistMonitorTable 使用 compact-table', () => {
  const src = readSource(TABLE_PATH)
  assert.ok(
    src.includes('tableClassName="compact-table"'),
    'WatchlistMonitorTable 应使用 compact-table 统一布局',
  )
})
