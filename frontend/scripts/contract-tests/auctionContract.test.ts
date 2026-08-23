// [Auction] - 描述: 竞价分析前端合同测试（PRD75 §3）
// 用法：node --experimental-strip-types --test scripts/contract-tests/auctionContract.test.ts
// 覆盖：
// 1. AnchorItem / InstrumentResult / EventTracking 类型含 symbol 和 name 字段
// 2. API 调用使用 /auction/stock/:symbol（非 UUID）
// 3. EVENT_LIFECYCLE_LABELS 覆盖 formed/confirmed/continued/weakened/failed/transformed/expired
// 4. 用户一级导航含 /auction 入口
// 5. useAuctionBackflow hook 存在（ReviewPage 第二金字塔数据源）
// 6. ReviewPage 集成 AuctionBackflowPanel（stage=auction）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const TYPES_PATH = join(__dirname, '..', '..', 'src', 'features', 'auction', 'types.ts')
const API_PATH = join(__dirname, '..', '..', 'src', 'features', 'auction', 'api.ts')
const NAV_PATH = join(__dirname, '..', '..', 'src', 'navigation', 'appNavigation.ts')
const REVIEW_PAGE_PATH = join(__dirname, '..', '..', 'src', 'pages', 'ReviewPage.tsx')
const BACKFLOW_PANEL_PATH = join(__dirname, '..', '..', 'src', 'features', 'review', 'AuctionBackflowPanel.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

test('AnchorItem 类型含 symbol 和 name 字段', () => {
  const src = readSource(TYPES_PATH)
  // AnchorItem 接口必须包含 symbol 和 name（可空，但必须声明）
  assert.ok(src.includes('symbol:'), 'types.ts 必须包含 symbol 字段')
  assert.ok(src.includes('name:'), 'types.ts 必须包含 name 字段')
})

test('InstrumentResult 类型含 symbol 和 name 字段', () => {
  const src = readSource(TYPES_PATH)
  assert.ok(src.includes('symbol:'), 'InstrumentResult 必须含 symbol')
  assert.ok(src.includes('name:'), 'InstrumentResult 必须含 name')
})

test('最终竞价 DTO 含来源、原始证据、采集时间和 final 标记', () => {
  const src = readSource(TYPES_PATH)
  for (const field of [
    'final_price:',
    'prev_close:',
    'volume:',
    'amount:',
    'source_timestamp:',
    'source_server:',
    'raw_payload:',
    'capture_time:',
    'is_final_auction:',
  ]) {
    assert.ok(src.includes(field), `AuctionFinalQuote 必须含 ${field}`)
  }
})

test('EventTracking 类型含 symbol 和 name 字段', () => {
  const src = readSource(TYPES_PATH)
  assert.ok(src.includes('symbol:'), 'EventTracking 必须含 symbol')
  assert.ok(src.includes('name:'), 'EventTracking 必须含 name')
})

test('API 个股页面使用 /auction/stock/:symbol（非 UUID）', () => {
  const src = readSource(API_PATH)
  assert.ok(
    src.includes('/auction/stock/${symbol}'),
    'API 必须使用 /auction/stock/:symbol 路径',
  )
  // 确保不使用 instrument_id 或 UUID 作为 URL 参数
  assert.ok(
    !src.includes('/auction/stock/${instrument_id}'),
    '禁止使用 instrument_id 作为 URL',
  )
})

test('EVENT_LIFECYCLE_LABELS 覆盖完整生命周期', () => {
  const src = readSource(TYPES_PATH)
  const requiredStates = [
    'formed',
    'confirmed',
    'continued',
    'weakened',
    'failed',
    'transformed',
    'expired',
  ]
  for (const state of requiredStates) {
    // 键可能使用引号 'formed': 或无引号 formed:
    const hasQuoted = src.includes(`'${state}':`)
    const hasUnquoted = src.includes(`  ${state}:`)
    assert.ok(
      hasQuoted || hasUnquoted,
      `EVENT_LIFECYCLE_LABELS 必须包含 ${state}`,
    )
  }
})

test('用户一级导航含 /auction 入口', () => {
  const src = readSource(NAV_PATH)
  assert.ok(src.includes("auction: '/auction'"), 'APP_ROUTES 必须含 auction 路由')
  assert.ok(
    src.includes("path: APP_ROUTES.auction"),
    'USER_NAV_ITEMS 必须含 auction 导航项',
  )
})

test('useAuctionBackflow hook 存在', () => {
  const src = readSource(API_PATH)
  assert.ok(
    src.includes('export function useAuctionBackflow'),
    'api.ts 必须导出 useAuctionBackflow hook',
  )
  assert.ok(
    src.includes('/auction/backflow/'),
    'API 必须调用 /auction/backflow/{trade_date} 端点',
  )
})

test('ReviewPage 不再集成 AuctionBackflowPanel（canonical cutover，Slice D）', () => {
  // Slice D 起 /review 为 canonical Scope-first runtime；auction 从 /review 退休，
  // AuctionBackflowPanel 物理文件保留至 Slice F 删除，但 ReviewPage 不得再导入/渲染。
  const src = readSource(REVIEW_PAGE_PATH)
  assert.ok(
    !src.includes('import AuctionBackflowPanel'),
    'ReviewPage 不得导入 AuctionBackflowPanel',
  )
  assert.ok(
    !src.includes("case 'auction'"),
    'ReviewPage 不得保留 auction stage 分支',
  )
  assert.ok(
    !src.includes('<AuctionBackflowPanel'),
    'ReviewPage 不得渲染 AuctionBackflowPanel 组件',
  )
})

test('AuctionBackflowPanel 使用 symbol 导航（非 UUID）', () => {
  const src = readSource(BACKFLOW_PANEL_PATH)
  assert.ok(
    src.includes('/auction/stock/${ev.symbol}'),
    'AuctionBackflowPanel 必须使用 symbol 进行导航',
  )
  assert.ok(
    !src.includes('/auction/stock/${ev.instrument_id}'),
    'AuctionBackflowPanel 禁止使用 instrument_id 导航',
  )
})

test('AuctionBackflowPanel 含四维度数据展示', () => {
  const src = readSource(BACKFLOW_PANEL_PATH)
  // 四维度：分布、迁移、新鲜度、集中度
  assert.ok(src.includes('event_type_distribution'), '必须展示事件类型分布')
  assert.ok(src.includes('lifecycle_distribution'), '必须展示生命周期分布')
  assert.ok(src.includes('event_migrations'), '必须展示迁移')
  assert.ok(src.includes('anchor_freshness_buckets'), '必须展示新鲜度')
  assert.ok(src.includes('market_concentration'), '必须展示集中度')
  assert.ok(src.includes('backflow_events'), '必须展示竞价事件回流')
})
