// [S2-B] - 描述: StrategyDataTable 导出按钮可用性契约测试（真实组件 SSR 渲染）
// 用法：./node_modules/.bin/tsx --test src/components/__tests__/strategyDataTableExportButton.test.ts
//
// 背景（A2-8 / S2-B 解耦）：
//   导出按钮历史上 `disabled={!activeRunId}`，但 /market 的导出自 CHANGE-20260904 起
//   已改走 POST /v1/market/export（与 /market/stocks 同 scope 授权，
//   见 backend/app/api/market.py 的 require_market_export_access），**与 DSA run 无关**。
//   普通 market_data 用户（没有 published run）本就有合法导出能力。
//
// S2-B 收口：导出可用性唯一由显式 `exportEnabled` 控制（默认 false，fail-closed）；
// 历史 `Boolean(activeRunId)` 回退已彻底移除。导出按钮只由调用方显式授权，
// 与 run 生命周期完全解耦。
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

// ===== 导出可用性唯一由 exportEnabled 控制（与 run 无关）=====

test('S2-B: exportEnabled=true 且无需 activeRunId → 导出按钮可用', () => {
  const html = renderTable({ exportEnabled: true })
  assert.ok(html.includes('导出 Excel'), '应渲染导出按钮')
  assert.equal(
    isExportDisabled(html),
    false,
    '普通用户（无 run）显式授权 exportEnabled=true 时，导出（/v1/market/export）必须可用',
  )
})

// ===== 默认 fail-closed：未提供 exportEnabled ⇒ 禁用 =====

test('S2-B: 未提供 exportEnabled → 默认禁用（不再由 run 决定）', () => {
  const html = renderTable({})
  assert.equal(
    isExportDisabled(html),
    true,
    '未显式授权时，导出默认禁用；run 不再影响可用性',
  )
})

test('S2-B: exportEnabled=false 显式关闭 → 禁用', () => {
  const html = renderTable({ exportEnabled: false })
  assert.equal(
    isExportDisabled(html),
    true,
    '显式 exportEnabled=false 关闭导出（调用方真实可导出条件为准）',
  )
})

test('S2-B: 未提供 onExport 时不渲染导出按钮（不扩大按钮可见面）', () => {
  const html = renderTable({ exportEnabled: true, omitOnExport: true })
  assert.ok(!html.includes('导出 Excel'), '未提供 onExport 时不应出现导出按钮')
})
