// [StockDetailMarketDataContract] - 描述: 个股详情布局与行情唯一真源修复前端定向测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/stockDetailMarketDataContract.test.ts
//
// 覆盖 CHANGE-20260724-003 前端契约：
//   1. direct 单列布局契约（originScope=direct 生成正确 URL，无来源列表）
//   2. market/watchlist 双列布局契约（originScope 对应值正确）
//   3. 详情页无 useRealtimeQuote 请求（源码不导入）
//   4. freshness 文案映射（fresh/partial/stale/unavailable → 正确中文标签）
//   5. session/visibility 变化只刷新一次（effect 依赖正确）
//   6. Messages URL 使用 buildStockDetailUrl（不手拼 /stock/:symbol）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  buildStockDetailUrl,
  resolveStockDetailOrigin,
} from '../stockDetailNavigation.ts'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

// ===== 1. direct 单列布局契约 =====

test('P0-1: direct 访问生成 originScope=direct 的 URL（无来源列表 → 单列布局）', () => {
  const url = buildStockDetailUrl('000001.SZ', {
    originScope: 'direct',
    returnTo: '/messages',
  })
  assert.ok(url.startsWith('/stock/000001.SZ?'), `URL 应以 /stock/000001.SZ? 开头，实际：${url}`)
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'direct', 'direct 访问必须显式传 originScope=direct')
  assert.equal(params.get('returnTo'), '/messages')
})

test('P0-1: direct 访问 resolveStockDetailOrigin 返回 originScope=direct（不伪造行情来源）', () => {
  // direct 访问无来源上下文，UI 隐藏左栏，布局切换单列
  const resolved = resolveStockDetailOrigin('direct', undefined)
  assert.equal(resolved.originScope, 'direct', 'direct 访问不得伪造为 market/watchlist')
  assert.equal(resolved.contextMismatch, false, 'direct 不参与冲突检测')
})

// ===== 2. market/watchlist 双列布局契约 =====

test('P0-1: market 访问生成 originScope=market 的 URL（双列布局，显示行情来源）', () => {
  const url = buildStockDetailUrl('000001.SZ', {
    originScope: 'market',
    sourceRunId: 'run-abc',
  })
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'market')
  assert.equal(params.get('source'), 'selection')
  assert.equal(params.get('strategy'), 'dsa_selector')
})

test('P0-1: watchlist 访问生成 originScope=watchlist 的 URL（双列布局，显示自选来源）', () => {
  const url = buildStockDetailUrl('000001.SZ', {
    originScope: 'watchlist',
  })
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'watchlist')
  assert.equal(params.get('source'), 'watchlist')
  assert.equal(params.get('strategy'), 'watchlist_monitor')
})

// ===== 3. 详情页无 useRealtimeQuote 请求 =====

test('P0-7: useStockResearchData.ts 源码不导入 useRealtimeQuote（详情页唯一行情真源为 chart-snapshot）', () => {
  const sourcePath = join(__dirname, '..', 'useStockResearchData.ts')
  const source = readFileSync(sourcePath, 'utf-8')
  // 允许在注释中出现 "useRealtimeQuote"（说明已删除），但不得在 import 或调用中出现
  const importPattern = /import\s+\{[^}]*useRealtimeQuote[^}]*\}/
  const callPattern = /useRealtimeQuote\s*\(/
  assert.ok(!importPattern.test(source), 'useStockResearchData.ts 不得导入 useRealtimeQuote')
  assert.ok(!callPattern.test(source), 'useStockResearchData.ts 不得调用 useRealtimeQuote()')
  // 确认已显式声明删除
  assert.ok(
    source.includes('useRealtimeQuote 已删除') || source.includes('quoteQuery 已删除'),
    '源码应显式声明 useRealtimeQuote 已删除',
  )
})

// ===== 4. freshness 文案映射 =====

test('P0-7: useStockResearchData.ts 包含 freshness_state 文案映射（fresh/partial/stale/unavailable）', () => {
  const sourcePath = join(__dirname, '..', 'useStockResearchData.ts')
  const source = readFileSync(sourcePath, 'utf-8')
  // 验证 freshness_state 的 4 种状态都有对应文案
  const expectedLabels = ['当期未完成', '最近收盘', '数据延迟', '行情不可用']
  for (const label of expectedLabels) {
    assert.ok(
      source.includes(label),
      `freshness 文案应包含 "${label}"`,
    )
  }
  // 验证不包含旧"行情回退"文案
  assert.ok(
    !source.includes('行情回退'),
    '不得包含泛化的"行情回退"文案',
  )
  // 验证包含 freshness_state switch 分支
  assert.ok(source.includes("case 'partial'"), '应包含 case partial 分支')
  assert.ok(source.includes("case 'fresh'"), '应包含 case fresh 分支')
  assert.ok(source.includes("case 'stale'"), '应包含 case stale 分支')
  assert.ok(source.includes("case 'unavailable'"), '应包含 case unavailable 分支')
})

// ===== 5. session/visibility 变化只刷新一次 =====

test('P0-10: useStockResearchData.ts 包含 visibilitychange 监听（hidden → visible 刷新一次）', () => {
  const sourcePath = join(__dirname, '..', 'useStockResearchData.ts')
  const source = readFileSync(sourcePath, 'utf-8')
  assert.ok(
    source.includes('visibilitychange'),
    '应监听 visibilitychange 事件以实现 hidden → visible 刷新',
  )
  assert.ok(
    source.includes("document.visibilityState === 'visible'"),
    '应在 visibilityState 变为 visible 时触发刷新',
  )
  assert.ok(
    source.includes('removeEventListener'),
    '应在 cleanup 中移除事件监听，防止重复触发',
  )
})

test('P0-10: useStockResearchData.ts 包含 market_session 响应式依赖', () => {
  const sourcePath = join(__dirname, '..', 'useStockResearchData.ts')
  const source = readFileSync(sourcePath, 'utf-8')
  assert.ok(
    source.includes('useMarketSessionReactive'),
    '应使用 useMarketSessionReactive hook 实现市场阶段响应式',
  )
  assert.ok(
    source.includes('prevMarketSessionRef'),
    '应使用 ref 记录前一次 market_session，避免重复 invalidate',
  )
  assert.ok(
    source.includes("queryClient.invalidateQueries"),
    'market_session 变化时应调用 invalidateQueries',
  )
})

// ===== 6. Messages URL 使用 buildStockDetailUrl =====

test('P0-3: MessagesPage.tsx 不手拼 /stock/:symbol，统一使用 buildStockDetailUrl', () => {
  const sourcePath = join(__dirname, '..', '..', '..', 'pages', 'MessagesPage.tsx')
  const source = readFileSync(sourcePath, 'utf-8')
  // 禁止手拼 /stock/${...} 路径
  const manualPattern = /['"`]\/stock\/\$\{/
  assert.ok(
    !manualPattern.test(source),
    'MessagesPage.tsx 不得手拼 /stock/:symbol 路径，必须使用 buildStockDetailUrl',
  )
  // 必须导入并使用 buildStockDetailUrl
  assert.ok(
    source.includes("import { buildStockDetailUrl }"),
    'MessagesPage.tsx 必须导入 buildStockDetailUrl',
  )
  assert.ok(
    source.includes('buildStockDetailUrl('),
    'MessagesPage.tsx 必须调用 buildStockDetailUrl()',
  )
  // 验证使用 originScope=direct（消息跳转无来源列表）
  assert.ok(
    source.includes("originScope: 'direct'"),
    '消息跳转应使用 originScope=direct（无来源列表）',
  )
})

// ===== 补充: ChartSnapshotResponse 类型包含 quote/freshness_state 字段 =====

test('P0-7: endpoints.ts 的 ChartSnapshotResponse 包含 quote/freshness_state 扩展字段', () => {
  const sourcePath = join(__dirname, '..', '..', '..', 'api', 'endpoints.ts')
  const source = readFileSync(sourcePath, 'utf-8')
  // 验证 ChartSnapshotResponse 接口包含扩展字段
  assert.ok(source.includes('freshness_state'), 'ChartSnapshotResponse 应包含 freshness_state 字段')
  assert.ok(source.includes('market_session'), 'ChartSnapshotResponse 应包含 market_session 字段')
  assert.ok(source.includes('actual_latest_bar_time'), 'ChartSnapshotResponse 应包含 actual_latest_bar_time 字段')
  assert.ok(source.includes('expected_latest_bar_time'), 'ChartSnapshotResponse 应包含 expected_latest_bar_time 字段')
  assert.ok(source.includes('is_partial'), 'ChartSnapshotResponse 应包含 is_partial 字段')
  assert.ok(source.includes('degraded_reason'), 'ChartSnapshotResponse 应包含 degraded_reason 字段')
  assert.ok(source.includes('as_of'), 'ChartSnapshotResponse 应包含 as_of 字段')
  // freshness_state 类型应为联合类型
  assert.ok(
    source.includes("'fresh'") && source.includes("'partial'") && source.includes("'stale'") && source.includes("'unavailable'"),
    'freshness_state 类型应包含 fresh|partial|stale|unavailable 联合类型',
  )
})
