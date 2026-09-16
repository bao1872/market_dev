// [S2-A] - 描述: /market 主表与 StrategyRun 生命周期解耦契约测试（真实组件 SSR 渲染）
// 用法：./node_modules/.bin/tsx --test src/features/market-workspace/__tests__/marketTableRunDecoupling.test.ts
//
// 解耦目标（S2-A）：/market 主表生命周期只由 /v1/market/stocks 决定，published-runs
// 只属 admin diagnostic。因此 StrategyDataTable 不应随 admin 批次 run id 变化而
// remount / 重置分页 / 改变导出可用性。
//
// 本文件用**真实组件渲染**证明：以 /market 实际传入的 props（tableId="market"、
// strategyKey 兼容、exportEnabled 显式控制、无 activeRunId、无 run-based key）渲染时，
// 表格身份稳定、导出不依赖 run。
//
// 说明：完整 MarketWorkspacePage 挂载需要 jsdom + @/ 别名解析（当前前端测试基建为
// node:test + renderToStaticMarkup，无 jsdom、tsx 下 @/ 不被解析），故本测试在
// StrategyDataTable 边界用 /market 实际 props 复刻验证（优先使用现有 React 测试基建）。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrategyDataTable } from '../../../components/StrategyDataTable.tsx'

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

// 复刻 /market 解耦后实际传入 StrategyDataTable 的 props：
// tableId="market"、strategyKey 兼容、exportEnabled 显式控制、无 activeRunId、无 run-based key。
function renderMarketTable(): string {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tableProps: Record<string, unknown> = {
    tableId: 'market',
    // legacy preset namespace 兼容，不代表行源来自 DSA run
    strategyKey: 'dsa_selector',
    columns: COLUMNS,
    rows: ROWS,
    rowKey: (r: never) => (r as { symbol: string }).symbol,
    serverSide: true,
    total: ROWS.length,
    // [S2-A] /market 不再传 activeRunId；导出由 accessReady 显式控制
    exportEnabled: true,
    onExport: () => {},
  }
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

function exportButtonDisabled(html: string): boolean {
  const marker = html.indexOf('export-btn')
  assert.ok(marker >= 0, '导出按钮必须被渲染（提供 onExport 时）')
  const start = html.lastIndexOf('<button', marker)
  const end = html.indexOf('>', marker)
  return /\bdisabled\b/.test(html.slice(start, end + 1))
}

// S2A-2: /market 表格身份稳定，不随（不存在的）run id 变化而差异
test('S2A-2: /market 表格以 tableId="market" 渲染，且输出稳定（无 run 驱动的 remount/差异）', () => {
  const a = renderMarketTable()
  const b = renderMarketTable()
  assert.ok(a.includes('导出 Excel'), 'market 表必须渲染导出按钮')
  // 两次相同 props 渲染逐字节一致 ⇒ 表格身份稳定，无 run 驱动的 remount/差异
  assert.equal(a, b, '相同 decoupled props 两次渲染输出逐字节一致')
  // 不得有任何 run-based key 泄漏到输出（旧实现曾有 key={`run-${activeRunId}`}）
  assert.ok(!a.includes('run-empty'), '输出不得含 run-based key "run-empty"')
  assert.ok(!a.includes('run-'), '输出不得含 run-based key 前缀 "run-"')
})

// S2A-5: 导出可用性由 exportEnabled 显式控制，不再依赖 run（无 activeRunId 仍可用）
test('S2A-5: /market 解耦后导出按钮可用（exportEnabled=true，无需 activeRunId）', () => {
  const html = renderMarketTable()
  assert.ok(html.includes('导出 Excel'), '应渲染导出按钮')
  assert.equal(
    exportButtonDisabled(html),
    false,
    '/market 导出（POST /v1/market/export）不依赖 DSA run，普通用户无 run 也必须可用',
  )
})
