// [DetailSourceLoadingContract] - 描述: 详情页来源列表 loading/error/empty/invalid 占位契约（CHANGE-20260715-004/005）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/detailSourceLoadingContract.test.ts
//
// 覆盖 Bug 1+2 根治契约：
//   1. useStockDetailActions 暴露 sourceListLoading/sourceListError/sourceListEmpty/sourceContextInvalid 字段
//   2. StockDetailPage 在 sourceListLoading=true 时渲染 loading 占位
//   3. StockDetailPage 在 sourceListError=true 时渲染 error 占位
//   4. StockDetailPage 在 sourceContextInvalid=true 时渲染 invalid 占位
//   5. StockDetailPage 在 sourceListEmpty=true 时渲染 empty 占位
//   6. StockDetailPage 列表渲染条件排除 loading/error/invalid/empty 状态
//   7. loading 占位包含 data-testid="detail-source-list-loading"
//   8. loading 占位显示来源类型 header（行情来源/自选来源）
//   9. CSS .tv-source-list-placeholder 存在
//  10. MarketWorkspacePage.handleNavigateToStock 显式传 source 和 strategy
//  11. source=selection → sourceListKind=market（即使 returnTo 无效也不回退 watchlist）
//  12. sourceListLoading 不再用 !activeRunId 作为永久 loading
//  13. 上一只/下一只保留 source/strategy/returnTo

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

// 测试文件位于 src/features/stock-research/__tests__/
// 各目标文件相对路径：
// - useStockDetailActions.ts: ../useStockDetailActions.ts
// - StockDetailPage.tsx: ../../../pages/StockDetailPage.tsx (3 levels up to src/, then pages/)
// - MarketWorkspacePage.tsx: ../../market-workspace/MarketWorkspacePage.tsx (2 levels up to features/, then market-workspace/)
// - global.scss: ../../../styles/global.scss (3 levels up to src/, then styles/)
const USE_DETAIL_ACTIONS = join(__dirname, '..', 'useStockDetailActions.ts')
const STOCK_DETAIL_PAGE = join(__dirname, '..', '..', '..', 'pages', 'StockDetailPage.tsx')
const MARKET_WORKSPACE_PAGE = join(__dirname, '..', '..', 'market-workspace', 'MarketWorkspacePage.tsx')
const GLOBAL_SCSS = join(__dirname, '..', '..', '..', 'styles', 'global.scss')
// CHANGE-20260715-007: resolveDetailSourceContext 唯一真源已移至 detailSourceContext.ts
const DETAIL_SOURCE_CONTEXT = join(__dirname, '..', 'detailSourceContext.ts')
const MARKET_WORKSPACE_URL_STATE = join(__dirname, '..', '..', 'market-workspace', 'marketWorkspaceUrlState.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf8')
}

test('CHANGE-005-1: useStockDetailActions 暴露 sourceListLoading/Error/Empty/ContextInvalid 字段', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // 接口字段
  assert.ok(/sourceListLoading:\s*boolean/.test(src), '接口必须包含 sourceListLoading: boolean')
  assert.ok(/sourceListError:\s*boolean/.test(src), '接口必须包含 sourceListError: boolean')
  assert.ok(/sourceListEmpty:\s*boolean/.test(src), '接口必须包含 sourceListEmpty: boolean')
  assert.ok(/sourceContextInvalid:\s*boolean/.test(src), '接口必须包含 sourceContextInvalid: boolean')
  // 返回值
  assert.ok(/sourceListLoading,/.test(src), '返回对象必须包含 sourceListLoading')
  assert.ok(/sourceListError,/.test(src), '返回对象必须包含 sourceListError')
  assert.ok(/sourceListEmpty,/.test(src), '返回对象必须包含 sourceListEmpty')
  assert.ok(/sourceContextInvalid,/.test(src), '返回对象必须包含 sourceContextInvalid')
})

test('CHANGE-005-2: sourceListLoading 不再用 !activeRunId 作为永久 loading', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // 提取 sourceListLoading 赋值行（禁止在该行使用 !activeRunId 作为 loading 条件）
  const loadingLine = src.match(/const sourceListLoading = [^\n]+/)?.[0] ?? ''
  assert.ok(loadingLine.length > 0, '必须存在 sourceListLoading 赋值')
  assert.ok(!/!activeRunId/.test(loadingLine), 'sourceListLoading 不得使用 !activeRunId（会导致永久 loading）')
  // [DetailSourceContextV2] V2 用固定 sourceRunId（入口快照），不再 fresh usePublishedRuns 推导 activeRunId
  // sourceListLoading 基于 sourceResultsQuery.isLoading（market/watchlist 有效时）
  assert.ok(/sourceResultsQuery\.isLoading/.test(src), '必须使用 sourceResultsQuery.isLoading')
  assert.ok(!/publishedRunsQuery/.test(src), 'V2 禁止使用 publishedRunsQuery（用固定 sourceRunId 替代）')
})

test('CHANGE-005-3: 尊重显式 origin（origin=market → sourceListKind=market）', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // [DetailSourceContextV2] sourceListKind 基于 origin（origin=market → market；watchlist/direct → watchlist）
  assert.ok(/origin === 'market' \? 'market' : 'watchlist'/.test(src), 'sourceListKind 必须基于显式 origin 参数')
  // hasValidSourceContext 必须包含 origin === 'market' || origin === 'watchlist' 条件
  assert.ok(/origin === 'market' \|\| origin === 'watchlist'/.test(src), 'hasValidSourceContext 必须要求 origin 为 market 或 watchlist')
  // useStockDetailActions 接收 sourceContextInvalid 参数
  assert.ok(/sourceContextInvalid:\s*boolean/.test(src), 'StockDetailActionsParams 必须接收 sourceContextInvalid: boolean 参数')
  // [DetailSourceContextV2] V2 resolver 必须存在并处理失效规则
  const detailSrc = readSource(DETAIL_SOURCE_CONTEXT)
  assert.ok(/resolveDetailSourceContextV2/.test(detailSrc), 'detailSourceContext 必须定义 resolveDetailSourceContextV2')
  assert.ok(/invalidReason/.test(detailSrc), 'V2 必须返回 invalidReason')
})

test('CHANGE-005-4: StockDetailPage 渲染 loading/error/invalid/empty 四种占位', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // loading 占位
  assert.ok(/data-testid="detail-source-list-loading"/.test(src), '必须渲染 loading 占位')
  // error 占位
  assert.ok(/data-testid="detail-source-list-error"/.test(src), '必须渲染 error 占位')
  // invalid 占位
  assert.ok(/data-testid="detail-source-list-invalid"/.test(src), '必须渲染 invalid 占位')
  // empty 占位
  assert.ok(/data-testid="detail-source-list-empty"/.test(src), '必须渲染 empty 占位')
})

test('CHANGE-005-5: StockDetailPage 列表渲染条件排除所有非正常状态', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 列表渲染必须显式排除 loading/error/invalid/empty 状态
  assert.ok(/!detailActions\.sourceListLoading && !detailActions\.sourceListError && !detailActions\.sourceContextInvalid && !detailActions\.sourceListEmpty && detailActions\.sourceStocks\.length > 0/.test(src), '列表渲染条件必须排除 loading/error/invalid/empty')
})

test('CHANGE-005-6: 来源徽标文案行为正确（computeSourceBadge 纯函数）', async () => {
  // 行为测试：验证 computeSourceBadge 在各 origin + invalid 组合下返回正确文案
  // 不依赖源码字符串匹配，直接测试函数行为
  const { computeSourceBadge } = await import('../stockDetailNavigation.ts')
  assert.equal(computeSourceBadge('market', false), '行情来源')
  assert.equal(computeSourceBadge('watchlist', false), '自选来源')
  assert.equal(computeSourceBadge('direct', false), '直接访问')
  assert.equal(computeSourceBadge('market', true), '来源失效')
  assert.equal(computeSourceBadge('watchlist', true), '来源失效')
  assert.equal(computeSourceBadge('direct', true), '来源失效')
})

test('CHANGE-005-7: CSS .tv-source-list-placeholder 存在', () => {
  const src = readSource(GLOBAL_SCSS)
  assert.ok(/\.tv-source-list-placeholder\s*\{/.test(src), 'global.scss 必须定义 .tv-source-list-placeholder')
})

test('CHANGE-005-8: MarketWorkspacePage.handleNavigateToStock 传递 originScope + returnTo + mcq（PRD MX-62）', () => {
  const src = readSource(MARKET_WORKSPACE_PAGE)
  assert.ok(/handleNavigateToStock/.test(src), 'MarketWorkspacePage 必须实现 handleNavigateToStock')
  // CHANGE-20260716-006: 必须从 stockDetailNavigation.ts 导入 buildStockDetailUrl（统一构建）
  assert.ok(/from '@[/]features[/]stock-research[/]stockDetailNavigation'/.test(src), '必须从 stockDetailNavigation 导入 buildStockDetailUrl')
  // 必须调用 buildStockDetailUrl 并传入 originScope: scope
  assert.ok(/buildStockDetailUrl\(\s*symbol,\s*\{\s*originScope:\s*scope,/.test(src), 'handleNavigateToStock 必须调用 buildStockDetailUrl(symbol, { originScope: scope, ... })')
  // returnTo 必须从 buildMarketReturnToUrl 构建（禁止 searchParams 副本）
  assert.ok(/buildMarketReturnToUrl\(marketListCtx,\s*symbol\)/.test(src), 'returnTo 必须用 buildMarketReturnToUrl(marketListCtx, symbol) 构建')
  assert.ok(!/returnToParams\.set\('scope'/.test(src), '禁止用 returnToParams.set 构造 returnTo（改用 buildMarketReturnToUrl）')
  // [PRD MX-62 / CHANGE-20260731-SAME-SOURCE] mcq = JSON.stringify(marketStocksParams)（/market/stocks 同源查询快照）
  assert.ok(/JSON\.stringify\(marketStocksParams\)/.test(src), 'mcq 必须为 JSON.stringify(marketStocksParams)（同源查询快照）')
  assert.ok(/marketCanonicalQuery,?\s*\n?\s*\}\)/.test(src), '必须透传 marketCanonicalQuery 给 buildStockDetailUrl')
  // [PRD MX-62] 禁止再传 DSA 旧格式 sourceRunId/canonicalQuery
  assert.ok(!/sourceRunId:\s*activeRunId/.test(src), 'PRD MX-62 禁止传 sourceRunId（DSA 旧格式）')
  assert.ok(!/canonicalQuery:\s*JSON\.stringify/.test(src), 'PRD MX-62 禁止传 canonicalQuery（DSA 旧格式）')
  // 间接验证 source/strategy 推导：scope=market → source=selection + strategy=dsa_selector
  const navSrc = readSource(join(__dirname, '..', 'stockDetailNavigation.ts'))
  assert.ok(/originScope === 'market' \? 'selection' : 'watchlist'/.test(navSrc), 'stockDetailNavigation.sourceForOriginScope: market → selection')
  assert.ok(/originScope === 'market' \? 'dsa_selector' : 'watchlist_monitor'/.test(navSrc), 'stockDetailNavigation.strategyForOriginScope: market → dsa_selector')
})

test('CHANGE-005-9: buildStockDetailUrl 统一生成 originScope + returnTo + mcq 完整 URL（PRD MX-62）', () => {
  // CHANGE-20260716-006: URL 构建合同已统一到 stockDetailNavigation.ts buildStockDetailUrl
  // 3 个导航点（MarketWorkspacePage / useStockDetailActions / StockDetailPage 左栏）必须全部使用此函数
  const navSrc = readSource(join(__dirname, '..', 'stockDetailNavigation.ts'))
  // 必须生成 /stock/:symbol?originScope=...&mcq=... 模式
  assert.ok(/`[/]stock[/]\$\{symbol\}\?\$\{params\.toString\(\)\}`/.test(navSrc), 'buildStockDetailUrl 必须返回 /stock/:symbol?params 模式')
  // params 必须包含 originScope（source/strategy 由 originScope 在消费端推导，不写入 URL）
  assert.ok(/originScope:\s*opts\.originScope/.test(navSrc), 'params 必须包含 originScope')
  // returnTo 通过 params.set 编码（URLSearchParams 自动编码）
  assert.ok(/params\.set\('returnTo',\s*opts\.returnTo\)/.test(navSrc), 'returnTo 必须通过 params.set 编码')
  // [PRD MX-62] mcq 通过 params.set('mcq', ...) 编码
  assert.ok(/params\.set\('mcq',\s*opts\.marketCanonicalQuery\)/.test(navSrc), 'mcq 必须通过 params.set 编码')
  // [PRD MX-62] 禁止写入 DSA 旧格式参数
  assert.ok(!/params\.set\('sourceRunId'/.test(navSrc), 'PRD MX-62 禁止写入 sourceRunId（DSA 旧格式）')
  assert.ok(!/params\.set\('cq'/.test(navSrc), 'PRD MX-62 禁止写入 cq（DSA 旧格式）')
  // MarketWorkspacePage 必须调用 buildStockDetailUrl 且传 returnTo + marketCanonicalQuery
  const marketSrc = readSource(MARKET_WORKSPACE_PAGE)
  assert.ok(/buildStockDetailUrl\(symbol,\s*\{[\s\S]*?returnTo,[\s\S]*?marketCanonicalQuery[\s\S]*?\}\)/.test(marketSrc), 'MarketWorkspacePage 必须调用 buildStockDetailUrl 并传 returnTo + marketCanonicalQuery')
})

test('CHANGE-005-10: useStockDetailActions 以 mcq 为来源列表数据源（PRD MX-62），接收 mcq context', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // [PRD MX-62 / CHANGE-20260731-SAME-SOURCE] mcq 优先：有 mcq 时用 useMarketStocks 同源查询
  assert.ok(/useMarketStocks\s*\(/.test(src), '必须使用 useMarketStocks（mcq 同源查询）')
  assert.ok(/useMcq/.test(src), '必须实现 useMcq 判定（mcq 优先于旧 DSA 路径）')
  // 禁止 fresh usePublishedRuns 重新推导 activeRunId（避免新 run 发布后来源列表漂移）
  assert.ok(!/usePublishedRuns\(/.test(src), '禁止使用 usePublishedRuns（用固定入口快照替代）')
  // useWatchlistMonitorStatus 仅用于 inWatchlist，禁止充当来源列表数据源
  assert.ok(/useWatchlistMonitorStatus\(\)/.test(src), '必须使用 useWatchlistMonitorStatus（仅用于 inWatchlist）')
  // 接收 origin + mcq 参数（旧 DSA sourceRunId/canonicalQuery 仅 deprecated 兼容保留）
  assert.ok(/origin:\s*OriginScope/.test(src), 'StockDetailActionsParams 必须接收 origin: OriginScope')
  assert.ok(/marketCanonicalQuery:\s*MarketCanonicalQuery \| null/.test(src), 'StockDetailActionsParams 必须接收 marketCanonicalQuery: MarketCanonicalQuery | null')
  assert.ok(/marketCanonicalQueryRaw:\s*string \| null/.test(src), 'StockDetailActionsParams 必须接收 marketCanonicalQueryRaw: string | null')
  assert.ok(/DEPRECATED/.test(src) && /sourceRunId:\s*string \| null/.test(src), '旧 DSA sourceRunId 仅允许 deprecated 兼容保留')
  // 不再接收 marketContext 参数
  assert.ok(!/marketContext:\s*MarketListContext \| null/.test(src), '禁止接收 marketContext 参数（用 mcq 替代）')
  assert.ok(!/buildStrategyResultQueryParams\(marketContext\)/.test(src), '禁止调用 buildStrategyResultQueryParams(marketContext)')
  // resolveDetailSourceContextV2 内部必须调用 decodeMarketListContext(returnTo)
  const detailSrc = readSource(DETAIL_SOURCE_CONTEXT)
  assert.ok(/decodeMarketListContext\(returnTo\)/.test(detailSrc), 'resolveDetailSourceContextV2 必须调用 decodeMarketListContext(returnTo)')
})

test('CHANGE-005-11: 上一只/下一只透传 origin/mcq（PRD MX-62）', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // CHANGE-20260716-006: 上一只/下一只必须使用 buildStockDetailUrl 统一构建
  assert.ok(/from '\.\/stockDetailNavigation'/.test(src), '必须从 ./stockDetailNavigation 导入 buildStockDetailUrl')
  // [PRD MX-62] 必须调用 buildStockDetailUrl 并透传 originScope: origin + marketCanonicalQuery: marketCanonicalQueryRaw
  assert.ok(/buildStockDetailUrl\(target\.symbol,\s*\{[\s\S]*?originScope:\s*origin,[\s\S]*?returnTo,[\s\S]*?timeframe,[\s\S]*?marketCanonicalQuery:\s*marketCanonicalQueryRaw,[\s\S]*?\}\)/.test(src), '上一只/下一只必须透传 originScope: origin + marketCanonicalQuery: marketCanonicalQueryRaw（入口 mcq 快照原样透传）')
  // 切股时不得重新推导 mcq（禁止 JSON.stringify 新查询）
  assert.ok(!/marketCanonicalQuery:\s*JSON\.stringify/.test(src), '切股禁止重新序列化 mcq（必须原样透传入口快照）')
  // buildStockDetailUrl 内部必须处理 timeframe + returnTo + mcq
  const navSrc = readSource(join(__dirname, '..', 'stockDetailNavigation.ts'))
  assert.ok(/if \(opts\.timeframe\)\s*\{[\s\S]*?params\.set\('timeframe',\s*opts\.timeframe\)/.test(navSrc), 'buildStockDetailUrl 必须处理 timeframe 参数')
  assert.ok(/if \(opts\.returnTo\)\s*\{[\s\S]*?params\.set\('returnTo',\s*opts\.returnTo\)/.test(navSrc), 'buildStockDetailUrl 必须处理 returnTo 参数')
  assert.ok(/if \(opts\.marketCanonicalQuery\)\s*\{[\s\S]*?params\.set\('mcq',\s*opts\.marketCanonicalQuery\)/.test(navSrc), 'buildStockDetailUrl 必须处理 marketCanonicalQuery (mcq) 参数')
})

test('CHANGE-005-12: normalizeInternalReturnTo 上限为 4096', () => {
  const src = readSource(join(__dirname, '..', '..', 'market-workspace', 'marketWorkspaceUrlState.ts'))
  assert.ok(/raw\.length > 4096/.test(src), 'normalizeInternalReturnTo 上限必须为 4096')
  assert.ok(!/raw\.length > 500/.test(src), '不得再使用 500 字符上限')
})

// CHANGE-20260715-007: 消除重复真源 — detailSourceContext.ts 为唯一权威实现
test('CHANGE-007-dedup: detailSourceContext.ts 为 normalizeResearchSource/defaultStrategyForSource 唯一定义点', () => {
  const detailSrc = readSource(DETAIL_SOURCE_CONTEXT)
  // detailSourceContext.ts 必须定义这两个函数（不是 re-export）
  assert.ok(/export function normalizeResearchSource\(/.test(detailSrc), 'detailSourceContext.ts 必须定义 normalizeResearchSource')
  assert.ok(/export function defaultStrategyForSource\(/.test(detailSrc), 'detailSourceContext.ts 必须定义 defaultStrategyForSource')
  assert.ok(/export function resolveDetailSourceContext\(/.test(detailSrc), 'detailSourceContext.ts 必须定义 resolveDetailSourceContext')

  // marketWorkspaceUrlState.ts 不得再定义本地副本（只能 re-export）
  const urlStateSrc = readSource(MARKET_WORKSPACE_URL_STATE)
  assert.ok(!/function normalizeResearchSourceLocal\(/.test(urlStateSrc), 'marketWorkspaceUrlState.ts 不得定义 normalizeResearchSourceLocal')
  assert.ok(!/function defaultStrategyForSourceLocal\(/.test(urlStateSrc), 'marketWorkspaceUrlState.ts 不得定义 defaultStrategyForSourceLocal')
  // 必须从 detailSourceContext.ts re-export
  assert.ok(/from '\.\.\/stock-research\/detailSourceContext\.ts'/.test(urlStateSrc), 'marketWorkspaceUrlState.ts 必须从 detailSourceContext.ts re-export')

  // stockResearchTypes.ts 也不得再定义本地副本（只能 re-export）
  const typesSrc = readSource(join(__dirname, '..', 'stockResearchTypes.ts'))
  assert.ok(!/^export function normalizeResearchSource\(/m.test(typesSrc), 'stockResearchTypes.ts 不得定义 normalizeResearchSource（应 re-export）')
  assert.ok(!/^export function defaultStrategyForSource\(/m.test(typesSrc), 'stockResearchTypes.ts 不得定义 defaultStrategyForSource（应 re-export）')
  assert.ok(/from '\.\/detailSourceContext\.ts'/.test(typesSrc), 'stockResearchTypes.ts 必须从 detailSourceContext.ts re-export')
})
