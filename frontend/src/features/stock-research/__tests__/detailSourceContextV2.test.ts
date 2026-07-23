// [DetailSourceContextV2] - 描述: 详情页来源同源同序合同 V2 纯函数契约测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/detailSourceContextV2.test.ts
//
// 覆盖 V2 根因修复合同：
//   1. buildStockDetailUrl 编码 sourceRunId + cq（入口快照载体）
//   2. computeStableContextIdV2 不含 selectedSymbol（切股不变）
//   3. resolveDetailSourceContextV2 origin 解析优先级（显式 originScope > /market returnTo.scope > direct）
//   4. resolveDetailSourceContextV2 失效规则（market/watchlist 缺 runId/cq/universe不匹配/冲突；direct 永不失效）
//   5. buildMarketReturnToUrl 与 decodeMarketListContext 互逆（完整筛选/排序/分页往返）
//
// 禁止（V2 硬规则）：
//   - useWatchlistMonitorStatus 充当来源列表数据源
//   - market/watchlist 缺 sourceRunId 时静默回退自选
//   - direct 伪造行情来源
//   - stableContextId 纳入 selectedSymbol

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  buildStockDetailUrl,
  computeStableContextIdV2,
} from '../stockDetailNavigation.ts'
import {
  resolveDetailSourceContextV2,
} from '../detailSourceContext.ts'
import {
  buildMarketReturnToUrl,
  decodeMarketListContext,
  buildStrategyResultQueryParams,
  type MarketListContext,
  type StrategyResultQuery,
} from '../../market-workspace/marketWorkspaceUrlState.ts'

// ===== 1. buildStockDetailUrl 编码 sourceRunId + cq =====

test('V2-1: buildStockDetailUrl 编码 sourceRunId + cq', () => {
  const cq: StrategyResultQuery = { universe: 'all', sort_by: 'change_pct', sort_desc: true }
  const url = buildStockDetailUrl('600519', {
    originScope: 'market',
    sourceRunId: 'run-abc-123',
    canonicalQuery: JSON.stringify(cq),
    returnTo: '/market?scope=market&selected=600519',
    timeframe: '1d',
  })
  assert.ok(url.includes('sourceRunId=run-abc-123'), 'URL 必须包含 sourceRunId')
  assert.ok(url.includes('cq='), 'URL 必须包含 cq 参数')
  assert.ok(url.includes('originScope=market'), 'URL 必须包含 originScope')
  assert.ok(url.includes('source=selection'), 'URL 必须包含 source=selection')
  assert.ok(url.includes('strategy=dsa_selector'), 'URL 必须包含 strategy=dsa_selector')
  assert.ok(url.includes('returnTo='), 'URL 必须包含 returnTo')
  assert.ok(url.includes('timeframe=1d'), 'URL 必须包含 timeframe')
})

test('V2-1b: buildStockDetailUrl 不传 sourceRunId/cq 时不编码（direct 场景）', () => {
  const url = buildStockDetailUrl('600519', {
    originScope: 'direct',
    returnTo: null,
  })
  assert.ok(!url.includes('sourceRunId='), 'direct 场景不应包含 sourceRunId')
  assert.ok(!url.includes('cq='), 'direct 场景不应包含 cq')
  assert.ok(url.includes('originScope=direct'), '必须包含 originScope=direct')
})

// ===== 2. computeStableContextIdV2 不含 selectedSymbol =====

test('V2-2: computeStableContextIdV2 不含 selectedSymbol 和 returnTo（切股不变）', () => {
  const id1 = computeStableContextIdV2('market', 'run-1', '{"universe":"all"}')
  const id2 = computeStableContextIdV2('market', 'run-1', '{"universe":"all"}')
  // 同一来源上下文 → 同一 stableContextId
  assert.equal(id1, id2, '同一来源上下文 stableContextId 必须相等')
  // 不同 sourceRunId → 不同 stableContextId
  const id3 = computeStableContextIdV2('market', 'run-2', '{"universe":"all"}')
  assert.notEqual(id1, id3, '不同 sourceRunId stableContextId 必须不同')
  // 不同 canonicalQuery → 不同 stableContextId
  const id4 = computeStableContextIdV2('market', 'run-1', '{"universe":"all","sort_by":"x"}')
  assert.notEqual(id1, id4, '不同 canonicalQuery stableContextId 必须不同')
  // 不同 origin → 不同 stableContextId
  const id5 = computeStableContextIdV2('watchlist', 'run-1', '{"universe":"all"}')
  assert.notEqual(id1, id5, '不同 origin stableContextId 必须不同')
  // 函数签名不含 returnTo，returnTo 不影响 stableContextId（合同：returnTo 仅用于返回导航）
  // 此处无法传入 returnTo，天然保证不变性
})

// ===== 3. resolveDetailSourceContextV2 origin 解析优先级 =====

test('V2-3a: 显式 originScope=market 优先于 returnTo.scope', () => {
  const cq: StrategyResultQuery = { universe: 'all' }
  const ctx = resolveDetailSourceContextV2(
    'market',
    '/market?scope=watchlist',
    'run-1',
    JSON.stringify(cq),
  )
  assert.equal(ctx.origin, 'market', '显式 originScope=market 必须优先')
  // returnTo.scope=watchlist 与 origin=market 冲突 → context_mismatch
  assert.equal(ctx.sourceContextInvalid, true, 'market vs returnTo.scope=watchlist 冲突 → invalid')
  assert.equal(ctx.invalidReason, 'context_mismatch')
})

test('V2-3b: 无显式 originScope + /market returnTo scope=market → 推导为 market', () => {
  const cq: StrategyResultQuery = { universe: 'all' }
  const ctx = resolveDetailSourceContextV2(
    null,
    '/market?scope=market&keyword=600519',
    'run-1',
    JSON.stringify(cq),
  )
  assert.equal(ctx.origin, 'market', '无 originScope + /market?scope=market → market')
  assert.equal(ctx.sourceContextInvalid, false)
  assert.equal(ctx.canonicalQuery!.universe, 'all')
})

test('V2-3c: 无显式 originScope + /market returnTo scope=watchlist → 推导为 watchlist', () => {
  const cq: StrategyResultQuery = { universe: 'watchlist' }
  const ctx = resolveDetailSourceContextV2(
    null,
    '/market?scope=watchlist',
    'run-1',
    JSON.stringify(cq),
  )
  assert.equal(ctx.origin, 'watchlist')
  assert.equal(ctx.sourceContextInvalid, false)
})

test('V2-3d: 无显式 originScope + 非 /market returnTo（如 /messages）→ direct（不伪造行情来源）', () => {
  const ctx = resolveDetailSourceContextV2(null, '/messages', null, null)
  assert.equal(ctx.origin, 'direct', '/messages returnTo → direct（不默认 watchlist）')
  assert.equal(ctx.sourceContextInvalid, false, 'direct 永不失效')
  assert.equal(ctx.sourceRunId, null)
  assert.equal(ctx.canonicalQuery, null)
})

test('V2-3e: 无显式 originScope + 无 returnTo → direct（直接访问不伪造行情来源）', () => {
  const ctx = resolveDetailSourceContextV2(null, null, null, null)
  assert.equal(ctx.origin, 'direct')
  assert.equal(ctx.sourceContextInvalid, false)
})

test('V2-3f: 显式 originScope=direct → direct，不校验 sourceRunId/cq', () => {
  const ctx = resolveDetailSourceContextV2('direct', null, null, null)
  assert.equal(ctx.origin, 'direct')
  assert.equal(ctx.sourceContextInvalid, false, 'direct 永不失效，即使无 sourceRunId/cq')
})

// ===== 4. resolveDetailSourceContextV2 失效规则 =====

test('V2-4a: market 有效（sourceRunId + cq universe=all）→ 不失效', () => {
  const cq: StrategyResultQuery = { universe: 'all', keyword: '茅台', sort_by: 'change_pct', sort_desc: true }
  const ctx = resolveDetailSourceContextV2(
    'market',
    '/market?scope=market',
    'run-abc',
    JSON.stringify(cq),
  )
  assert.equal(ctx.origin, 'market')
  assert.equal(ctx.sourceContextInvalid, false)
  assert.equal(ctx.sourceRunId, 'run-abc')
  assert.equal(ctx.canonicalQuery!.universe, 'all')
  assert.equal(ctx.canonicalQuery!.keyword, '茅台')
  assert.equal(ctx.canonicalQuery!.sort_by, 'change_pct')
  assert.equal(ctx.canonicalQuery!.sort_desc, true)
  assert.equal(ctx.invalidReason, 'none')
})

test('V2-4b: watchlist 有效（sourceRunId + cq universe=watchlist）→ 不失效', () => {
  const cq: StrategyResultQuery = { universe: 'watchlist' }
  const ctx = resolveDetailSourceContextV2(
    'watchlist',
    '/market?scope=watchlist',
    'run-xyz',
    JSON.stringify(cq),
  )
  assert.equal(ctx.origin, 'watchlist')
  assert.equal(ctx.sourceContextInvalid, false)
  assert.equal(ctx.canonicalQuery!.universe, 'watchlist')
})

test('V2-4c: market 缺 sourceRunId → 失效 missing_run_id（禁止静默回退自选）', () => {
  const cq: StrategyResultQuery = { universe: 'all' }
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', null, JSON.stringify(cq))
  assert.equal(ctx.sourceContextInvalid, true, 'market 缺 sourceRunId 必须失效')
  assert.equal(ctx.invalidReason, 'missing_run_id')
  assert.equal(ctx.canonicalQuery, null, '失效时 canonicalQuery 必须置 null')
})

test('V2-4d: watchlist 缺 sourceRunId → 失效 missing_run_id', () => {
  const cq: StrategyResultQuery = { universe: 'watchlist' }
  const ctx = resolveDetailSourceContextV2('watchlist', '/market?scope=watchlist', null, JSON.stringify(cq))
  assert.equal(ctx.sourceContextInvalid, true)
  assert.equal(ctx.invalidReason, 'missing_run_id')
})

test('V2-4e: market 缺 canonicalQuery → 失效 missing_canonical_query', () => {
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', 'run-1', null)
  assert.equal(ctx.sourceContextInvalid, true)
  assert.equal(ctx.invalidReason, 'missing_canonical_query')
})

test('V2-4f: canonicalQuery JSON 解析失败 → 失效 canonical_query_parse_failed', () => {
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', 'run-1', '{invalid json')
  assert.equal(ctx.sourceContextInvalid, true)
  assert.equal(ctx.invalidReason, 'canonical_query_parse_failed')
})

test('V2-4g: canonicalQuery 为数组（非对象）→ 失效 canonical_query_parse_failed', () => {
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', 'run-1', '[1,2,3]')
  assert.equal(ctx.sourceContextInvalid, true)
  assert.equal(ctx.invalidReason, 'canonical_query_parse_failed')
})

test('V2-4h: market + cq universe=watchlist → 失效 universe_mismatch', () => {
  const cq: StrategyResultQuery = { universe: 'watchlist' }
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', 'run-1', JSON.stringify(cq))
  assert.equal(ctx.sourceContextInvalid, true, 'market 来源 cq.universe 必须为 all')
  assert.equal(ctx.invalidReason, 'universe_mismatch')
})

test('V2-4i: watchlist + cq universe=all → 失效 universe_mismatch', () => {
  const cq: StrategyResultQuery = { universe: 'all' }
  const ctx = resolveDetailSourceContextV2('watchlist', '/market?scope=watchlist', 'run-1', JSON.stringify(cq))
  assert.equal(ctx.sourceContextInvalid, true, 'watchlist 来源 cq.universe 必须为 watchlist')
  assert.equal(ctx.invalidReason, 'universe_mismatch')
})

test('V2-4j: market + returnTo.scope=watchlist 冲突 → 失效 context_mismatch（优先于其他检查）', () => {
  const cq: StrategyResultQuery = { universe: 'all' }
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=watchlist', 'run-1', JSON.stringify(cq))
  assert.equal(ctx.sourceContextInvalid, true)
  assert.equal(ctx.invalidReason, 'context_mismatch', '冲突检测必须优先于 runId/cq 检查')
})

test('V2-4k: direct 即使有 sourceRunId/cq 也不失效（不校验）', () => {
  const ctx = resolveDetailSourceContextV2('direct', null, null, null)
  assert.equal(ctx.sourceContextInvalid, false)
  // direct 即使传了 sourceRunId/cq 也不校验 universe
  const ctx2 = resolveDetailSourceContextV2('direct', null, 'run-1', '{"universe":"all"}')
  assert.equal(ctx2.sourceContextInvalid, false, 'direct 不校验 sourceRunId/cq')
})

// ===== 5. stableContextId 切股不变性（端到端验证）=====

test('V2-5: 切换股票时 stableContextId 不变（同一来源上下文，不同入口 symbol）', () => {
  const cq: StrategyResultQuery = { universe: 'all', sort_by: 'change_pct', sort_desc: true }
  const cqRaw = JSON.stringify(cq)
  // 入口时刻 returnTo（含 selected=入口symbol，但 stableContextId 不含 returnTo）
  const returnTo600519 = '/market?scope=market&selected=600519'
  const returnTo000001 = '/market?scope=market&selected=000001'
  // 从 /market 进入 600519，再切换到 000001：sourceRunId/cq 不变 → stableContextId 不变
  // 即使 returnTo 不同（selected 不同），stableContextId 也不变（returnTo 不参与计算）
  const ctx1 = resolveDetailSourceContextV2('market', returnTo600519, 'run-1', cqRaw)
  const ctx2 = resolveDetailSourceContextV2('market', returnTo000001, 'run-1', cqRaw)
  assert.equal(ctx1.stableContextId, ctx2.stableContextId, '不同入口 symbol（returnTo.selected 不同）stableContextId 必须不变')
  // 关键不变性：computeStableContextIdV2 不接收 selectedSymbol 和 returnTo 参数
  const idFor600519 = computeStableContextIdV2('market', 'run-1', cqRaw)
  const idFor000001 = computeStableContextIdV2('market', 'run-1', cqRaw)
  assert.equal(idFor600519, idFor000001, 'computeStableContextIdV2 不含 selectedSymbol/returnTo，切股不变')
  // 不同 sourceRunId → 不同 stableContextId（验证函数对真实变化敏感）
  const idDiffRun = computeStableContextIdV2('market', 'run-2', cqRaw)
  assert.notEqual(idFor600519, idDiffRun, '不同 sourceRunId 必须产生不同 stableContextId')
})

// ===== 6. buildMarketReturnToUrl 与 decodeMarketListContext 互逆 =====

test('V2-6a: buildMarketReturnToUrl → decodeMarketListContext 往返一致（含完整筛选/排序/分页）', () => {
  const original: MarketListContext = {
    scope: 'market',
    keyword: '茅台',
    industry: '白酒',
    concept: '消费',
    sort: { key: 'change_pct', direction: 'desc' },
    filters: [
      { key: 'vwap_ret_avg', operator: 'gt', value: 0.05 },
      { key: 'offset_percentile', operator: 'between', value: 0.2, value2: 0.8 },
    ],
    page: 2,
    page_size: 50,
    preset: null,
  }
  const url = buildMarketReturnToUrl(original, '600519')
  assert.ok(url.startsWith('/market?'), 'returnTo 必须以 /market? 开头')
  assert.ok(url.includes('selected=600519'), 'returnTo 必须包含 selected')
  // 往返解码
  const decoded = decodeMarketListContext(url)
  assert.notEqual(decoded, null)
  assert.equal(decoded!.scope, 'market')
  assert.equal(decoded!.keyword, '茅台')
  assert.equal(decoded!.industry, '白酒')
  assert.equal(decoded!.concept, '消费')
  assert.equal(decoded!.sort!.key, 'change_pct')
  assert.equal(decoded!.sort!.direction, 'desc')
  assert.equal(decoded!.filters!.length, 2)
  assert.equal(decoded!.filters![0].key, 'vwap_ret_avg')
  assert.equal(decoded!.filters![0].operator, 'gt')
  assert.equal(decoded!.filters![1].key, 'offset_percentile')
  assert.equal(decoded!.filters![1].operator, 'between')
  assert.equal(decoded!.page, 2)
  assert.equal(decoded!.page_size, 50)
})

test('V2-6b: buildMarketReturnToUrl → buildStrategyResultQueryParams 与原 ctx 一致', () => {
  const ctx: MarketListContext = {
    scope: 'watchlist',
    keyword: null,
    industry: null,
    concept: null,
    sort: { key: 'change_pct', direction: 'asc' },
    filters: null,
    page: 1,
    page_size: 50,
    preset: null,
  }
  const url = buildMarketReturnToUrl(ctx, null)
  const decoded = decodeMarketListContext(url)!
  const queryFromDecoded = buildStrategyResultQueryParams(decoded)
  const queryFromOriginal = buildStrategyResultQueryParams(ctx)
  assert.equal(queryFromDecoded.universe, queryFromOriginal.universe)
  assert.equal(queryFromDecoded.sort_by, queryFromOriginal.sort_by)
  assert.equal(queryFromDecoded.sort_desc, queryFromOriginal.sort_desc)
  assert.equal(queryFromDecoded.page, queryFromOriginal.page)
  assert.equal(queryFromDecoded.page_size, queryFromOriginal.page_size)
})

test('V2-6c: buildMarketReturnToUrl selectedSymbol=null 时不编码 selected', () => {
  const ctx: MarketListContext = {
    scope: 'market',
    keyword: null,
    industry: null,
    concept: null,
    sort: null,
    filters: null,
    page: null,
    page_size: null,
    preset: null,
  }
  const url = buildMarketReturnToUrl(ctx, null)
  assert.ok(!url.includes('selected='), 'selectedSymbol=null 时不应编码 selected')
  assert.ok(url.includes('scope=market'), 'scope 始终编码')
})
