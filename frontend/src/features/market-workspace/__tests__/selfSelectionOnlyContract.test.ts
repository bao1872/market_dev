// [SelfSelectionOnlyContract] - 描述: self_selection-only 用户自选 UI 契约测试（源码级）
// 用法：./node_modules/.bin/tsx --test src/features/market-workspace/__tests__/selfSelectionOnlyContract.test.ts
//
// 背景（P0 后续任务，外部审核修正）：
//   P0 授权修复后，/market/stocks（无论 scope）要求 market_data，self_selection-only
//   用户访问会 403。但 self_selection-only 不得通过 /v1/watchlist/monitor-status
//   （返回 full metrics/price/event）隐式重新获得 market_data —— 那是换端点重开泄露。
//
// 正确边界：self_selection-only 用户只能看到 metadata-only 自选列表
//   （GET /v1/watchlist，仅 symbol/name/market/加入时间），不展示任何行情/策略指标。
//
// 契约：
//   1. 存在 isSelfSelectionOnly 判定（!isAdmin && hasSelfSelection && !hasMarketData）
//   2. self_selection-only 时禁用 useMarketStocks（enabled 含 !isSelfSelectionOnly）
//   3. self_selection-only 时使用 metadata-only useWatchlist（不使用 useWatchlistMonitorStatus）
//   4. self_selection-only 分支渲染极简 metadata 表格，不渲染 WatchlistMonitorTable/MarketToolbar
//   5. 移除自选走 handleRemoveSelfSelectionWatchlist（复用 removeMutation）
//   6. 禁止 import/使用 WatchlistMonitorTable / useWatchlistMonitorStatus / adaptWatchlistMonitorStatusItem

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'MarketWorkspacePage.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

test('SELFONLY-1: 存在 isSelfSelectionOnly 判定（!isAdmin && hasSelfSelection && !hasMarketData）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    /const isSelfSelectionOnly\s*=\s*!isAdmin\s*&&\s*hasSelfSelection\s*&&\s*!hasMarketData/.test(src),
    '必须定义 isSelfSelectionOnly = !isAdmin && hasSelfSelection && !hasMarketData',
  )
})

test('SELFONLY-2: self_selection-only 时禁用 useMarketStocks（enabled 含 !isSelfSelectionOnly）', () => {
  const src = readSource(PAGE_PATH)
  const m = src.match(/useMarketStocks\(marketStocksParams,\s*\{[\s\S]*?enabled:\s*(accessReady\s*&&\s*!isSelfSelectionOnly)[\s\S]*?\}\)/)
  assert.ok(m, 'useMarketStocks 的 enabled 必须包含 accessReady && !isSelfSelectionOnly')
})

test('SELFONLY-3: self_selection-only 使用 metadata-only useWatchlist（不使用 useWatchlistMonitorStatus）', () => {
  const src = readSource(PAGE_PATH)
  // 必须存在为 self_selection-only 启用的 useWatchlist
  const w = src.match(/useWatchlist\(\{[\s\S]*?enabled:\s*isSelfSelectionOnly[\s\S]*?\}\)/)
  assert.ok(w, 'self_selection-only 必须使用 useWatchlist({ enabled: isSelfSelectionOnly })')
  // 禁止使用 useWatchlistMonitorStatus（返回 full metrics，会重开 self_selection→market_data 泄露）
  assert.ok(!/useWatchlistMonitorStatus/.test(src), '禁止使用 useWatchlistMonitorStatus')
})

test('SELFONLY-4: self_selection-only 分支渲染极简 metadata 表格，不渲染 WatchlistMonitorTable/MarketToolbar', () => {
  const src = readSource(PAGE_PATH)
  const branchIdx = src.indexOf('if (isSelfSelectionOnly)')
  assert.ok(branchIdx >= 0, '必须存在 if (isSelfSelectionOnly) 早退分支')
  const mainReturnIdx = src.indexOf('\n  return (', branchIdx)
  assert.ok(mainReturnIdx > branchIdx, '自选分支后必须存在主 return')
  const branchBlock = src.slice(branchIdx, mainReturnIdx)
  // 极简表格：展示 name/symbol/market，不展示行情字段
  assert.ok(/item\.name/.test(branchBlock), '自选分支必须渲染股票名称 item.name')
  assert.ok(/item\.symbol/.test(branchBlock), '自选分支必须渲染代码 item.symbol')
  assert.ok(/item\.market/.test(branchBlock), '自选分支必须渲染市场 item.market')
  // 禁止渲染行情/策略组件与字段
  assert.ok(!/WatchlistMonitorTable/.test(branchBlock), '自选分支禁止渲染 WatchlistMonitorTable')
  assert.ok(!/<MarketToolbar/.test(branchBlock), '自选分支禁止渲染 MarketToolbar（行情专属 UI）')
  // 移除自选回调必须绑定
  assert.ok(/onClick=\{\(\) => handleRemoveSelfSelectionWatchlist\(item\)\}/.test(branchBlock), '自选分支必须绑定 handleRemoveSelfSelectionWatchlist(item)')
})

test('SELFONLY-5: 移除自选复用 removeMutation（/v1/watchlist/{id}，仅要求 self_selection）', () => {
  const src = readSource(PAGE_PATH)
  const defIdx = src.indexOf('handleRemoveSelfSelectionWatchlist = useCallback')
  assert.ok(defIdx >= 0, '必须存在 handleRemoveSelfSelectionWatchlist = useCallback 定义')
  const block = src.slice(defIdx, defIdx + 600)
  assert.ok(/removeMutation\.mutate\(item\.instrument_id/.test(block), '移除必须调用 removeMutation.mutate(item.instrument_id)')
})

test('SELFONLY-6: 禁止 import WatchlistMonitorTable / useWatchlistMonitorStatus / adaptWatchlistMonitorStatusItem', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(!/WatchlistMonitorTable/.test(src), '禁止 import/使用 WatchlistMonitorTable')
  assert.ok(!/useWatchlistMonitorStatus/.test(src), '禁止 import/使用 useWatchlistMonitorStatus')
  assert.ok(!/adaptWatchlistMonitorStatusItem/.test(src), '禁止 import/使用 adaptWatchlistMonitorStatusItem')
})
