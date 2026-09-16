// [USER-FIX-3 / A2] - 描述: StrategyDataTable 导出按钮可用性契约测试（真实组件 SSR 渲染）
// 用法：./node_modules/.bin/tsx --test src/components/__tests__/strategyDataTableExportButton.test.ts
//
// 背景（A2-8 旧耦合）：
//   导出按钮历史上 `disabled={!activeRunId}`。但 /market 的导出自 CHANGE-20260904 起
//   已改走 POST /v1/market/export（与 /market/stocks 同 scope 授权，
//   见 backend/app/api/market.py 的 require_market_export_access），**与 DSA run 无关**。
//   于是普通 market_data 用户（没有 published run ⇒ activeRunId === ''）会莫名失去
//   本来合法的导出能力。
//
// 修复：新增显式 prop `exportEnabled`，默认 `exportEnabled ?? Boolean(activeRunId)`，
// 既解除 /market 的旧耦合，又保持其他 caller 的历史默认行为不变。
//
// 本文件渲染**真实组件**并断言按钮的实际 disabled 属性，不是字符串 grep。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrategyDataTable } from '../StrategyDataTable.tsx'

// StrategyDataTable 渲染期会读取 window（列宽/预设菜单），SSR 下按仓库既有惯例打桩
;(globalThis as unknown as { window: unknown }).window = {
  innerWidth: 1280,
  innerHeight: 800,
  addEventListener: () => {},
  removeEventListener: () => {},
}

const COLUMNS = [
  { key: 'symbol', title: '股票', dataType: 'text' },
  { key: 'change_pct', title: '涨跌幅', dataType: 'percent' },
]
const ROWS = [
  { symbol: '000001', change_pct: 1.2 },
  { symbol: '600519', change_pct: -0.4 },
]

function renderTable(props: {
  activeRunId?: string
  exportEnabled?: boolean
  /** true = 完全不传 onExport（验证未提供回调时不渲染按钮） */
  omitOnExport?: boolean
}): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tableProps: Record<string, unknown> = {
    tableId: 'export-btn-contract',
    columns: COLUMNS,
    rows: ROWS,
    rowKey: (r: never) => (r as { symbol: string }).symbol,
    serverSide: true,
    total: ROWS.length,
    activeRunId: props.activeRunId,
    exportEnabled: props.exportEnabled,
  }
  if (!props.omitOnExport) tableProps.onExport = () => {}

  return renderToStaticMarkup(
    createElement(
      QueryClientProvider as never,
      { client: qc },
      createElement(
        MemoryRouter as never,
        {},
        createElement(StrategyDataTable as never, tableProps as never),
      ),
    ),
  )
}

/** 取出真实的导出按钮标签（含属性），用于断言 disabled 属性本身 */
function exportButtonTag(html: string): string {
  const marker = html.indexOf('export-btn')
  assert.ok(marker >= 0, '导出按钮必须被渲染（提供 onExport 时）')
  const start = html.lastIndexOf('<button', marker)
  const end = html.indexOf('>', marker)
  return html.slice(start, end + 1)
}

function isExportDisabled(html: string): boolean {
  return /\bdisabled\b/.test(exportButtonTag(html))
}

// ===== /market 场景：无 activeRunId 也必须可导出 =====

test('A2-8: /market 场景 activeRunId="" 且 exportEnabled=true → 导出按钮可用', () => {
  const html = renderTable({ activeRunId: '', exportEnabled: true })
  assert.ok(html.includes('导出 Excel'), '应渲染导出按钮')
  assert.equal(
    isExportDisabled(html),
    false,
    '普通 market_data 用户没有 published run 时，导出（/v1/market/export）必须仍可用',
  )
})

// ===== 其他 caller 历史默认行为不得改变 =====

test('A2-8: 未提供 exportEnabled + activeRunId="" → 保持历史语义（禁用）', () => {
  const html = renderTable({ activeRunId: '' })
  assert.equal(
    isExportDisabled(html),
    true,
    '未显式解耦时，默认行为必须与历史 `Boolean(activeRunId)` 完全一致',
  )
})

test('A2-8: 未提供 exportEnabled + activeRunId="run-x" → 保持历史语义（可用）', () => {
  const html = renderTable({ activeRunId: 'run-6f2a1c33' })
  assert.equal(isExportDisabled(html), false, '有 run 时导出仍可用（无回归）')
})

test('A2-8: exportEnabled=false 显式关闭时，即使有 activeRunId 也禁用', () => {
  const html = renderTable({ activeRunId: 'run-6f2a1c33', exportEnabled: false })
  assert.equal(
    isExportDisabled(html),
    true,
    '显式 exportEnabled=false 优先级高于 activeRunId（调用方真实可导出条件为准）',
  )
})

test('A2-8: 未提供 onExport 时不渲染导出按钮（不扩大按钮可见面）', () => {
  const html = renderTable({ activeRunId: '', exportEnabled: true, omitOnExport: true })
  assert.ok(!html.includes('导出 Excel'), '未提供 onExport 时不应出现导出按钮')
})
