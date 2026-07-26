// [SourceListVisibilityContract] - 描述: 详情页左栏来源列表可见性合同（fix/stock-detail-source-context-visible-v1）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/sourceListVisibilityContract.test.ts
//
// 覆盖修复合同（fix/stock-detail-source-context-visible-v1）：
//   1. 缺 originScope + 无 /market returnTo → missing_origin invalid（不静默单列，合同5）
//   2. 显式 direct → 无左栏（单列，合同1）
//   3. invalid 占位必须显示 reason（data-invalid-reason 属性，合同5）
//   4. 所有列表入口使用 buildStockDetailUrl 并携带完整 originScope/returnTo/sourceRunId/cq（合同2/3）
//   5. market/watchlist 正常入口不得被解析成 direct（合同4）
//   6. 真实列表渲染条件：!loading && !error && !invalid && !empty && length>0（禁止 invalid/empty/direct 判为 PASS）
//   7. 来源查询固定入口 run/query（hasValidSourceContext 逻辑，合同6）
//   8. 防闪烁合同（c9fbe2b）：tv-detail-layout 始终挂载，来源列表不因 instrumentQuery 卸载（合同7）
//
// 验证方式：静态源码合同（readFileSync + regex）+ V2 纯函数契约。
// 行为模拟由代码结构保证：missing_origin invalid → showSourceList=true → invalid 占位显示 reason。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { resolveDetailSourceContextV2 } from '../detailSourceContext.ts'
import { buildStockDetailUrl } from '../stockDetailNavigation.ts'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const STOCK_DETAIL_PAGE = join(__dirname, '..', '..', '..', 'pages', 'StockDetailPage.tsx')
const MARKET_WORKSPACE_PAGE = join(__dirname, '..', '..', 'market-workspace', 'MarketWorkspacePage.tsx')
const MESSAGES_PAGE = join(__dirname, '..', '..', '..', 'pages', 'MessagesPage.tsx')
const USE_STOCK_DETAIL_ACTIONS = join(__dirname, '..', 'useStockDetailActions.ts')
const DETAIL_SOURCE_CONTEXT = join(__dirname, '..', 'detailSourceContext.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf8')
}

// ===== 1. resolveDetailSourceContextV2: missing_origin invalid 合同 =====

test('VIS-1a: 缺 originScope + 无 returnTo → missing_origin invalid（不静默单列）', () => {
  const ctx = resolveDetailSourceContextV2(null, null, null, null)
  assert.equal(ctx.sourceContextInvalid, true, '必须 invalid')
  assert.equal(ctx.invalidReason, 'missing_origin', 'reason 必须为 missing_origin')
})

test('VIS-1b: 缺 originScope + 非 /market returnTo → missing_origin invalid', () => {
  const ctx = resolveDetailSourceContextV2(null, '/messages', null, null)
  assert.equal(ctx.sourceContextInvalid, true, '必须 invalid')
  assert.equal(ctx.invalidReason, 'missing_origin')
})

test('VIS-1c: 缺 originScope + /market returnTo → 从 returnTo.scope 推导（兼容旧链接，不 invalid）', () => {
  const ctx = resolveDetailSourceContextV2(null, '/market?scope=market&keyword=600519', 'run-1', '{"universe":"all"}')
  assert.equal(ctx.origin, 'market')
  assert.equal(ctx.sourceContextInvalid, false, '有 /market returnTo 不应 missing_origin')
})

test('VIS-1d: 显式 originScope=direct → direct，永不失效', () => {
  const ctx = resolveDetailSourceContextV2('direct', null, null, null)
  assert.equal(ctx.origin, 'direct')
  assert.equal(ctx.sourceContextInvalid, false, '显式 direct 永不失效')
})

test('VIS-1e: 显式 originScope=market + 完整 sourceRunId + cq universe=all → 不失效', () => {
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', 'run-1', '{"universe":"all"}')
  assert.equal(ctx.sourceContextInvalid, false, 'market 完整上下文不失效')
  assert.equal(ctx.origin, 'market')
})

test('VIS-1f: 显式 originScope=watchlist + 完整 sourceRunId + cq universe=watchlist → 不失效', () => {
  const ctx = resolveDetailSourceContextV2('watchlist', '/market?scope=watchlist', 'run-1', '{"universe":"watchlist"}')
  assert.equal(ctx.sourceContextInvalid, false, 'watchlist 完整上下文不失效')
  assert.equal(ctx.origin, 'watchlist')
})

// ===== 2. StockDetailPage: showSourceList 逻辑合同 =====

test('VIS-2a: showSourceList 在 origin=direct 时为 false（单列，合同1）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // showSourceList 必须检查 origin !== 'direct'
  assert.ok(
    /showSourceList\s*=\s*!isCaptureMode\s*&&\s*sourceCtxV2\.origin\s*!==\s*['"]direct['"]/.test(src),
    'showSourceList 必须在 origin=direct 时为 false',
  )
})

test('VIS-2b: missing_origin invalid 时 showSourceList 为 true（origin=watchlist 占位，显示 invalid 占位）', () => {
  // resolveDetailSourceContextV2 在 missing_origin 时设 origin=watchlist
  // showSourceList = !isCaptureMode && origin !== 'direct' → true
  const ctx = resolveDetailSourceContextV2(null, null, null, null)
  // origin 为 watchlist（占位），不是 direct
  assert.notEqual(ctx.origin, 'direct', 'missing_origin 时 origin 不应为 direct')
  // sourceContextInvalid 为 true
  assert.equal(ctx.sourceContextInvalid, true)
})

// ===== 3. invalid 占位显示 reason 合同（合同5）=====

test('VIS-3a: invalid 占位必须有 data-invalid-reason 属性', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // invalid 占位 aside 必须有 data-invalid-reason={sourceCtxV2.invalidReason}
  assert.ok(
    /data-invalid-reason=\{sourceCtxV2\.invalidReason\}/.test(src),
    'invalid 占位必须暴露 data-invalid-reason 属性',
  )
})

test('VIS-3b: invalid 占位文案必须根据 invalidReason 变化（非硬编码）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 必须存在 INVALID_REASON_LABELS 映射
  assert.ok(/INVALID_REASON_LABELS/.test(src), '必须存在 INVALID_REASON_LABELS 映射')
  // 占位文案必须引用 INVALID_REASON_LABELS[sourceCtxV2.invalidReason]
  assert.ok(
    /INVALID_REASON_LABELS\[sourceCtxV2\.invalidReason\]/.test(src),
    'invalid 占位文案必须根据 invalidReason 变化',
  )
})

test('VIS-3c: INVALID_REASON_LABELS 必须包含 missing_origin', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  assert.ok(/missing_origin:/.test(src), 'INVALID_REASON_LABELS 必须包含 missing_origin 文案')
})

test('VIS-3d: INVALID_REASON_LABELS 必须包含所有 invalid reason', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  for (const reason of ['context_mismatch', 'missing_run_id', 'missing_canonical_query', 'canonical_query_parse_failed', 'universe_mismatch', 'missing_origin']) {
    assert.ok(src.includes(reason), `INVALID_REASON_LABELS 必须包含 ${reason}`)
  }
})

// ===== 4. 入口矩阵合同：所有列表入口使用 buildStockDetailUrl（合同2/3）=====

test('VIS-4a: MarketWorkspacePage 股票名称点击使用 buildStockDetailUrl', () => {
  const src = readSource(MARKET_WORKSPACE_PAGE)
  assert.ok(/import.*buildStockDetailUrl.*from/.test(src), '必须 import buildStockDetailUrl')
  // 定位 handleNavigateToStock = useCallback 函数体（跳过注释中的提及）
  const defIdx = src.indexOf('handleNavigateToStock = useCallback')
  assert.ok(defIdx >= 0, '必须存在 handleNavigateToStock = useCallback 定义')
  // 取定义后 800 字符（覆盖整个函数体）
  const handleBlock = src.slice(defIdx, defIdx + 800)
  assert.ok(/buildStockDetailUrl/.test(handleBlock), 'handleNavigateToStock 必须使用 buildStockDetailUrl')
  assert.ok(/originScope:\s*scope/.test(handleBlock), '必须传 originScope: scope')
  assert.ok(/returnTo[:,]/.test(handleBlock), '必须传 returnTo')
  assert.ok(/sourceRunId[:,]/.test(handleBlock), '必须传 sourceRunId')
  assert.ok(/canonicalQuery[:,]/.test(handleBlock), '必须传 canonicalQuery')
})

test('VIS-4b: StockDetailPage 左栏点击使用 buildStockDetailUrl 并透传完整上下文', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 左栏点击必须使用 buildStockDetailUrl
  const leftClickMatch = src.match(/onClick=\{\(\)\s*=>\s*navigate\(buildStockDetailUrl\([\s\S]*?\)\)\}/)
  assert.ok(leftClickMatch, '左栏点击必须使用 buildStockDetailUrl')
  const block = leftClickMatch[0]
  assert.ok(/originScope:\s*sourceCtxV2\.origin/.test(block), '必须传 originScope: sourceCtxV2.origin')
  assert.ok(/returnTo:\s*returnToParam/.test(block), '必须传 returnTo: returnToParam')
  assert.ok(/sourceRunId:\s*sourceCtxV2\.sourceRunId/.test(block), '必须传 sourceRunId')
  assert.ok(/canonicalQuery:\s*sourceCtxV2\.canonicalQueryRaw/.test(block), '必须传 canonicalQuery: canonicalQueryRaw')
})

test('VIS-4c: useStockDetailActions navigateToStock 使用 buildStockDetailUrl 并透传完整上下文', () => {
  const src = readSource(USE_STOCK_DETAIL_ACTIONS)
  const navIdx = src.indexOf('navigateToStock = useCallback')
  assert.ok(navIdx >= 0, '必须存在 navigateToStock')
  // 取 navigateToStock 后 800 字符（覆盖整个函数体）
  const block = src.slice(navIdx, navIdx + 800)
  assert.ok(/buildStockDetailUrl/.test(block), 'navigateToStock 必须使用 buildStockDetailUrl')
  assert.ok(/originScope:\s*origin/.test(block), '必须传 originScope: origin')
  assert.ok(/returnTo[:,]/.test(block), '必须传 returnTo')
  assert.ok(/sourceRunId[:,]/.test(block), '必须传 sourceRunId')
  assert.ok(/canonicalQuery:\s*canonicalQueryRaw/.test(block), '必须传 canonicalQuery: canonicalQueryRaw')
})

test('VIS-4d: MessagesPage 单股跳转使用 buildStockDetailUrl（originScope=direct）', () => {
  const src = readSource(MESSAGES_PAGE)
  assert.ok(/import.*buildStockDetailUrl.*from/.test(src), '必须 import buildStockDetailUrl')
  // 消息单股跳转必须使用 buildStockDetailUrl（originScope=direct）
  assert.ok(/buildStockDetailUrl\([^,]+,\s*\{[\s\S]*?originScope:\s*['"]direct['"]/.test(src), '消息跳转必须 originScope=direct')
})

// ===== 5. market/watchlist 正常入口不得被解析成 direct（合同4）=====

test('VIS-5a: market 入口（originScope=market + 完整上下文）不得为 direct', () => {
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', 'run-1', '{"universe":"all"}')
  assert.notEqual(ctx.origin, 'direct', 'market 入口不得为 direct')
  assert.equal(ctx.sourceContextInvalid, false)
})

test('VIS-5b: watchlist 入口（originScope=watchlist + 完整上下文）不得为 direct', () => {
  const ctx = resolveDetailSourceContextV2('watchlist', '/market?scope=watchlist', 'run-1', '{"universe":"watchlist"}')
  assert.notEqual(ctx.origin, 'direct', 'watchlist 入口不得为 direct')
  assert.equal(ctx.sourceContextInvalid, false)
})

test('VIS-5c: market 缺 sourceRunId → invalid（不回退 direct）', () => {
  const ctx = resolveDetailSourceContextV2('market', '/market?scope=market', null, '{"universe":"all"}')
  assert.notEqual(ctx.origin, 'direct', 'market 缺 sourceRunId 不得回退 direct')
  assert.equal(ctx.sourceContextInvalid, true)
  assert.equal(ctx.invalidReason, 'missing_run_id')
})

// ===== 6. 真实列表渲染条件（禁止 invalid/empty/direct 判为 PASS）=====

test('VIS-6a: 真实列表渲染条件排除 loading/error/invalid/empty（合同：禁止 invalid/empty/direct 判为 PASS）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 真实列表渲染条件必须包含所有排除项
  assert.ok(
    /showSourceList\s*&&\s*!detailActions\.sourceListLoading\s*&&\s*!detailActions\.sourceListError\s*&&\s*!detailActions\.sourceContextInvalid\s*&&\s*!detailActions\.sourceListEmpty\s*&&\s*detailActions\.sourceStocks\.length\s*>\s*0/.test(src),
    '真实列表渲染必须排除 loading/error/invalid/empty 且 length>0',
  )
})

test('VIS-6b: useStockDetailActions hasValidSourceContext 排除 invalid/direct/缺参', () => {
  const src = readSource(USE_STOCK_DETAIL_ACTIONS)
  // hasValidSourceContext 必须检查 !sourceContextInvalid && (market||watchlist) && sourceRunId && canonicalQuery
  assert.ok(
    /hasValidSourceContext\s*=\s*!sourceContextInvalid\s*&&\s*\(origin\s*===\s*['"]market['"]\s*\|\|\s*origin\s*===\s*['"]watchlist['"]\)\s*&&\s*!!sourceRunId\s*&&\s*!!canonicalQuery/.test(src),
    'hasValidSourceContext 必须排除 invalid/direct/缺参',
  )
})

test('VIS-6c: direct 时 hasValidSourceContext=false（不查询来源 results）', () => {
  // direct 时 origin !== 'market' && origin !== 'watchlist' → hasValidSourceContext=false
  const src = readSource(USE_STOCK_DETAIL_ACTIONS)
  // useStrategyRunResults 在 hasValidSourceContext=false 时 disabled
  assert.ok(/hasValidSourceContext\s*\?\s*sourceRunId!/.test(src), 'useStrategyRunResults 在 hasValidSourceContext=false 时 disabled')
})

// ===== 7. 来源查询固定入口 run/query（合同6）=====

test('VIS-7a: useStockDetailActions 使用 useStrategyRunResults（固定 sourceRunId，不重新推导）', () => {
  const src = readSource(USE_STOCK_DETAIL_ACTIONS)
  // 必须使用 useStrategyRunResults
  assert.ok(/useStrategyRunResults/.test(src), '必须使用 useStrategyRunResults')
  // 禁止实际调用 usePublishedRuns（注释提及允许，但不得 import 或调用）
  // 检查 import 语句中不含 usePublishedRuns
  const importMatch = src.match(/^import\s*\{[^}]*\}\s*from/gm) || []
  for (const imp of importMatch) {
    assert.ok(!/usePublishedRuns/.test(imp), `禁止 import usePublishedRuns: ${imp}`)
  }
  // 检查无 usePublishedRuns( 调用（允许在注释中出现）
  assert.ok(!/^[^/]*usePublishedRuns\(/.test(src.replace(/\/\/[^\n]*/g, '')), '禁止调用 usePublishedRuns()')
})

// ===== 8. 防闪烁合同（c9fbe2b，合同7）=====

test('VIS-8a: tv-detail-layout 始终挂载（不因 instrumentQuery 卸载）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 禁止页面级 instrumentQuery.isLoading 早退
  assert.ok(
    !/if\s*\(\s*researchData\.instrumentQuery\.isLoading\s*\)\s*\{[^}]*return\s*\(/.test(src),
    '禁止页面级 instrumentQuery.isLoading 早退',
  )
  // tv-detail-layout 必须在主 return 内
  const returnIdx = src.indexOf('return (')
  assert.ok(returnIdx >= 0)
  const afterReturn = src.slice(returnIdx)
  assert.ok(/tv-detail-layout/.test(afterReturn), 'tv-detail-layout 必须在主 return 内')
})

test('VIS-8b: 来源列表 aside 禁止 key={symbol}（保持同一 DOM 节点）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  const asideMatch = src.match(/data-testid="detail-source-list"[\s\S]*?<\/aside>/)
  assert.ok(asideMatch, '必须存在 detail-source-list aside 块')
  const asideBlock = asideMatch[0]
  assert.ok(!/key=\{symbol\}/.test(asideBlock), '来源列表 aside 禁止 key={symbol}')
})

test('VIS-8c: 来源列表项保持 key={s.symbol}（per-stock key）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  assert.ok(/key=\{s\.symbol\}/.test(src), '来源列表项必须保持 key={s.symbol}')
})

// ===== 9. V2 唯一真源合同（合同8）=====

test('VIS-9a: StockDetailPage 使用 resolveDetailSourceContextV2（非 V1）', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  assert.ok(/resolveDetailSourceContextV2/.test(src), '必须使用 resolveDetailSourceContextV2')
  assert.ok(!/resolveDetailSourceContext[^V]/.test(src), '禁止使用 V1 resolveDetailSourceContext')
})

test('VIS-9b: V1 resolveDetailSourceContext 标记为 deprecated', () => {
  const src = readSource(DETAIL_SOURCE_CONTEXT)
  // V1 必须有 @deprecated 标记
  const v1Match = src.match(/\/\*\*[\s\S]*?@deprecated[\s\S]*?\*\/\s*export function resolveDetailSourceContext\(/)
  assert.ok(v1Match, 'V1 resolveDetailSourceContext 必须标记 @deprecated')
})

// ===== 10. buildStockDetailUrl 完整性合同 =====

test('VIS-10a: buildStockDetailUrl market 入口 URL 包含完整字段', () => {
  const url = buildStockDetailUrl('600519', {
    originScope: 'market',
    returnTo: '/market?scope=market&selected=600519',
    sourceRunId: 'run-abc',
    canonicalQuery: '{"universe":"all"}',
    timeframe: '1d',
  })
  assert.ok(url.includes('originScope=market'))
  assert.ok(url.includes('source=selection'))
  assert.ok(url.includes('strategy=dsa_selector'))
  assert.ok(url.includes('returnTo='))
  assert.ok(url.includes('sourceRunId=run-abc'))
  assert.ok(url.includes('cq='))
  assert.ok(url.includes('timeframe=1d'))
})

test('VIS-10b: buildStockDetailUrl watchlist 入口 URL 包含完整字段', () => {
  const url = buildStockDetailUrl('600519', {
    originScope: 'watchlist',
    returnTo: '/market?scope=watchlist&selected=600519',
    sourceRunId: 'run-abc',
    canonicalQuery: '{"universe":"watchlist"}',
  })
  assert.ok(url.includes('originScope=watchlist'))
  assert.ok(url.includes('source=watchlist'))
  assert.ok(url.includes('strategy=watchlist_monitor'))
  assert.ok(url.includes('returnTo='))
  assert.ok(url.includes('sourceRunId=run-abc'))
  assert.ok(url.includes('cq='))
})

test('VIS-10c: buildStockDetailUrl direct 入口不强制 sourceRunId/cq', () => {
  const url = buildStockDetailUrl('600519', {
    originScope: 'direct',
    returnTo: '/messages',
  })
  assert.ok(url.includes('originScope=direct'))
  assert.ok(url.includes('source=watchlist'))
  assert.ok(url.includes('strategy=watchlist_monitor'))
  assert.ok(!url.includes('sourceRunId='), 'direct 不强制 sourceRunId')
  assert.ok(!url.includes('cq='), 'direct 不强制 cq')
})
