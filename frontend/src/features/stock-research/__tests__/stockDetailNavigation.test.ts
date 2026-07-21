// [StockDetailNavigation] - 描述: 详情页导航唯一真源契约测试（CHANGE-20260716-006）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/stockDetailNavigation.test.ts
//
// 覆盖：
//   1. buildStockDetailUrl 生成 originScope + source + strategy + returnTo + timeframe
//   2. originScope=market → source=selection&strategy=dsa_selector
//   3. originScope=watchlist → source=watchlist&strategy=watchlist_monitor
//   4. [PRD V2.0 §4.4] originScope=direct → source=watchlist&strategy=watchlist_monitor
//   5. resolveStockDetailOrigin 显式 originScope 不被 returnTo.scope 覆盖
//   6. stale returnTo=watchlist + originScope=market → contextMismatch=true
//   7. [PRD V2.0 §7.3 CI门禁] originScope=market 不得静默回退 watchlist
//   8. [PRD V2.0 §4.4] originScope=direct 不参与冲突检测
//   9. 无 originScope 时兼容解析 returnTo.scope
//   10. 无任何来源默认 watchlist
//   11. returnTo/timeframe 可选
//   12. [PRD V2.0 §4.4] DetailEntryContext 唯一对象 + contextId 稳定性

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  buildStockDetailUrl,
  resolveStockDetailOrigin,
  sourceForOriginScope,
  strategyForOriginScope,
  buildDetailEntryContext,
  computeDetailEntryContextId,
} from '../stockDetailNavigation.ts'

test('buildStockDetailUrl: originScope=market → source=selection&strategy=dsa_selector', () => {
  const url = buildStockDetailUrl('000001.SZ', { originScope: 'market' })
  assert.ok(url.startsWith('/stock/000001.SZ?'), `URL should start with /stock/000001.SZ?, got: ${url}`)
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'market')
  assert.equal(params.get('source'), 'selection')
  assert.equal(params.get('strategy'), 'dsa_selector')
})

test('buildStockDetailUrl: originScope=watchlist → source=watchlist&strategy=watchlist_monitor', () => {
  const url = buildStockDetailUrl('000001.SZ', { originScope: 'watchlist' })
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'watchlist')
  assert.equal(params.get('source'), 'watchlist')
  assert.equal(params.get('strategy'), 'watchlist_monitor')
})

test('buildStockDetailUrl: [PRD V2.0 §4.4] originScope=direct → source=watchlist&strategy=watchlist_monitor', () => {
  const url = buildStockDetailUrl('000001.SZ', { originScope: 'direct' })
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('originScope'), 'direct')
  assert.equal(params.get('source'), 'watchlist')
  assert.equal(params.get('strategy'), 'watchlist_monitor')
})

test('buildStockDetailUrl: 保留 returnTo 和 timeframe', () => {
  const url = buildStockDetailUrl('000001.SZ', {
    originScope: 'market',
    returnTo: '/market?scope=market&selected=000001.SZ',
    timeframe: '1d',
  })
  const params = new URLSearchParams(url.split('?')[1])
  assert.equal(params.get('returnTo'), '/market?scope=market&selected=000001.SZ')
  assert.equal(params.get('timeframe'), '1d')
})

test('buildStockDetailUrl: 无 returnTo/timeframe 时不写入参数', () => {
  const url = buildStockDetailUrl('000001.SZ', { originScope: 'market' })
  const params = new URLSearchParams(url.split('?')[1])
  assert.ok(!params.has('returnTo'), 'returnTo should not be present')
  assert.ok(!params.has('timeframe'), 'timeframe should not be present')
})

test('sourceForOriginScope / strategyForOriginScope 映射正确（含 direct）', () => {
  assert.equal(sourceForOriginScope('market'), 'selection')
  assert.equal(sourceForOriginScope('watchlist'), 'watchlist')
  assert.equal(sourceForOriginScope('direct'), 'watchlist')
  assert.equal(strategyForOriginScope('market'), 'dsa_selector')
  assert.equal(strategyForOriginScope('watchlist'), 'watchlist_monitor')
  assert.equal(strategyForOriginScope('direct'), 'watchlist_monitor')
})

test('resolveStockDetailOrigin: 显式 originScope=market 立即点击仍为行情来源', () => {
  // stale returnTo=watchlist 不应覆盖显式 originScope=market
  const result = resolveStockDetailOrigin('market', '/market?scope=watchlist')
  assert.equal(result.originScope, 'market')
  assert.equal(result.contextMismatch, true, 'stale returnTo=watchlist + originScope=market → contextMismatch')
})

test('resolveStockDetailOrigin: originScope 与 returnTo.scope 一致 → 无冲突', () => {
  const result = resolveStockDetailOrigin('market', '/market?scope=market')
  assert.equal(result.originScope, 'market')
  assert.equal(result.contextMismatch, false)
})

test('resolveStockDetailOrigin: [PRD V2.0 §4.4] 显式 originScope=direct → direct', () => {
  // direct 不参与冲突检测，即使 returnTo.scope=market 也不冲突
  const result = resolveStockDetailOrigin('direct', '/market?scope=market')
  assert.equal(result.originScope, 'direct')
  assert.equal(result.contextMismatch, false, 'direct 不参与冲突检测')
})

test('resolveStockDetailOrigin: [PRD V2.0 §4.4] originScope=direct 无 returnTo → direct', () => {
  const result = resolveStockDetailOrigin('direct', null)
  assert.equal(result.originScope, 'direct')
  assert.equal(result.contextMismatch, false)
})

test('resolveStockDetailOrigin: 无 originScope 时兼容解析 returnTo.scope', () => {
  const result = resolveStockDetailOrigin(null, '/market?scope=market')
  assert.equal(result.originScope, 'market')
  assert.equal(result.contextMismatch, false)
})

test('resolveStockDetailOrigin: 无任何来源默认 watchlist', () => {
  const result = resolveStockDetailOrigin(null, null)
  assert.equal(result.originScope, 'watchlist')
  assert.equal(result.contextMismatch, false)
})

test('resolveStockDetailOrigin: returnTo 非 /market 前缀不解析 scope', () => {
  const result = resolveStockDetailOrigin(null, '/screener?page=1')
  assert.equal(result.originScope, 'watchlist')
  assert.equal(result.contextMismatch, false)
})

// ===== [PRD V2.0 §7.3 CI门禁] market上下文不得回退watchlist =====

test('[PRD V2.0 §7.3 CI门禁] originScope=market + returnTo=watchlist → contextMismatch=true（不回退）', () => {
  // 这是 CI 门禁的核心测试：market 上下文失效时不得静默回退 watchlist
  // 调用方应根据 contextMismatch=true 显示"来源上下文失效"占位
  const result = resolveStockDetailOrigin('market', '/market?scope=watchlist')
  assert.equal(result.originScope, 'market', 'originScope 必须保持 market，不回退 watchlist')
  assert.equal(result.contextMismatch, true, '必须标记 contextMismatch=true')
})

test('[PRD V2.0 §7.3 CI门禁] originScope=market 无 returnTo → 不回退 watchlist', () => {
  // 显式 market 但无 returnTo（无 marketContext）→ 调用方应显示失效，不回退 watchlist
  const result = resolveStockDetailOrigin('market', null)
  assert.equal(result.originScope, 'market', 'originScope 必须保持 market')
  assert.equal(result.contextMismatch, false, '无 returnTo 时无冲突，但调用方应通过 sourceContextInvalid 处理')
})

// ===== [PRD V2.0 §4.4] DetailEntryContext 唯一对象 =====

test('buildDetailEntryContext: 构建 market 上下文', () => {
  const resolved = resolveStockDetailOrigin('market', '/market?scope=market')
  const ctx = buildDetailEntryContext(
    resolved,
    { runId: 'run-123', strategy: 'dsa_selector' },
    '/market?scope=market',
    '000001.SZ',
  )
  assert.equal(ctx.origin, 'market')
  assert.equal(ctx.selectedSymbol, '000001.SZ')
  assert.equal(ctx.returnTo, '/market?scope=market')
  assert.deepEqual(ctx.listQuery, { runId: 'run-123', strategy: 'dsa_selector' })
  assert.ok(ctx.contextId.length > 0, 'contextId 必须非空')
  assert.ok(ctx.contextId.includes('market'), 'contextId 必须包含 origin')
  assert.ok(ctx.contextId.includes('000001.SZ'), 'contextId 必须包含 selectedSymbol')
})

test('buildDetailEntryContext: 构建 direct 上下文', () => {
  const resolved = resolveStockDetailOrigin('direct', null)
  const ctx = buildDetailEntryContext(resolved, null, null, '600519.SH')
  assert.equal(ctx.origin, 'direct')
  assert.equal(ctx.selectedSymbol, '600519.SH')
  assert.equal(ctx.returnTo, null)
  assert.equal(ctx.listQuery, null)
})

test('computeDetailEntryContextId: 相同输入 → 相同 contextId（稳定性）', () => {
  const id1 = computeDetailEntryContextId('market', { runId: 'r1' }, '/market', '000001.SZ')
  const id2 = computeDetailEntryContextId('market', { runId: 'r1' }, '/market', '000001.SZ')
  assert.equal(id1, id2, '相同输入必须生成相同 contextId')
})

test('computeDetailEntryContextId: 不同 origin → 不同 contextId', () => {
  const idMarket = computeDetailEntryContextId('market', null, null, '000001.SZ')
  const idDirect = computeDetailEntryContextId('direct', null, null, '000001.SZ')
  assert.notEqual(idMarket, idDirect, '不同 origin 必须生成不同 contextId')
})

test('computeDetailEntryContextId: 不同 selectedSymbol → 不同 contextId', () => {
  const id1 = computeDetailEntryContextId('direct', null, null, '000001.SZ')
  const id2 = computeDetailEntryContextId('direct', null, null, '600519.SH')
  assert.notEqual(id1, id2, '不同 selectedSymbol 必须生成不同 contextId')
})
