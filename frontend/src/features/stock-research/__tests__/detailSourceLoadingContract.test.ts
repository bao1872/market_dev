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
  // 必须使用 publishedRunsQuery.isLoading 和 sourceResultsQuery.isLoading
  assert.ok(/publishedRunsQuery\.isLoading/.test(src), '必须使用 publishedRunsQuery.isLoading')
  assert.ok(/sourceResultsQuery\.isLoading/.test(src), '必须使用 sourceResultsQuery.isLoading')
})

test('CHANGE-005-3: 尊重显式 source 参数（source=selection → sourceListKind=market）', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // source=selection → sourceListKind=market（不依赖 returnTo）
  assert.ok(/source === 'selection' \? 'market' : 'watchlist'/.test(src), 'sourceListKind 必须基于显式 source 参数')
  // hasMarketContext 必须包含 source === 'selection' 条件
  assert.ok(/source === 'selection' && marketContext !== null/.test(src), 'hasMarketContext 必须要求 source === selection && marketContext !== null')
  // CHANGE-20260715-007: sourceContextInvalid 推导已移至 resolveDetailSourceContext（detailSourceContext.ts）
  // useStockDetailActions 不再自行推导，只接收参数；接口必须包含 sourceContextInvalid 字段
  assert.ok(/sourceContextInvalid:\s*boolean/.test(src), 'StockDetailActionsParams 必须接收 sourceContextInvalid: boolean 参数')
  // resolveDetailSourceContext 中 sourceContextInvalid 逻辑：source=selection 且 marketContext=null 时为 true
  // （不再要求 !!returnTo：source=selection 本身声明用户意图来自市场，无上下文即为失效，不静默回退自选）
  const detailSrc = readSource(DETAIL_SOURCE_CONTEXT)
  assert.ok(/const sourceContextInvalid = source === 'selection'/.test(detailSrc), 'resolveDetailSourceContext 必须在 source=selection 且无 marketContext 时设置 sourceContextInvalid=true')
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

test('CHANGE-005-6: loading 占位显示来源类型 header', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  const matches = src.match(/sourceListKind === 'market' \? '行情来源' : '自选来源'/g)
  assert.ok(matches && matches.length >= 2, 'loading 占位和列表都需显示 header')
})

test('CHANGE-005-7: CSS .tv-source-list-placeholder 存在', () => {
  const src = readSource(GLOBAL_SCSS)
  assert.ok(/\.tv-source-list-placeholder\s*\{/.test(src), 'global.scss 必须定义 .tv-source-list-placeholder')
})

test('CHANGE-005-8: MarketWorkspacePage.handleNavigateToStock 通过 buildStockDetailUrl 传递 originScope + returnTo', () => {
  const src = readSource(MARKET_WORKSPACE_PAGE)
  assert.ok(/handleNavigateToStock/.test(src), 'MarketWorkspacePage 必须实现 handleNavigateToStock')
  // CHANGE-20260716-006: 必须从 stockDetailNavigation.ts 导入 buildStockDetailUrl（统一构建）
  assert.ok(/from '@[/]features[/]stock-research[/]stockDetailNavigation'/.test(src), '必须从 stockDetailNavigation 导入 buildStockDetailUrl')
  // 必须调用 buildStockDetailUrl 并传入 originScope: scope（market|watchlist）
  assert.ok(/buildStockDetailUrl\(\s*symbol,\s*\{\s*originScope:\s*scope,/.test(src), 'handleNavigateToStock 必须调用 buildStockDetailUrl(symbol, { originScope: scope, ... })')
  // 必须传入 returnTo（基于当前 searchParams 副本构造，强制写入 scope 和 selected）
  assert.ok(/returnToParams\.set\('scope',\s*scope\)/.test(src), 'returnTo 必须强制写入 scope')
  assert.ok(/returnToParams\.set\('selected',\s*symbol\)/.test(src), 'returnTo 必须强制写入 selected')
  // 间接验证 source/strategy 推导：scope=market → source=selection + strategy=dsa_selector
  // （buildStockDetailUrl 内部通过 sourceForOriginScope/strategyForOriginScope 推导，由 stockDetailNavigation.test.ts 守护）
  const navSrc = readSource(join(__dirname, '..', 'stockDetailNavigation.ts'))
  assert.ok(/originScope === 'market' \? 'selection' : 'watchlist'/.test(navSrc), 'stockDetailNavigation.sourceForOriginScope: market → selection')
  assert.ok(/originScope === 'market' \? 'dsa_selector' : 'watchlist_monitor'/.test(navSrc), 'stockDetailNavigation.strategyForOriginScope: market → dsa_selector')
})

test('CHANGE-005-9: buildStockDetailUrl 统一生成 source + strategy + returnTo 完整 URL', () => {
  // CHANGE-20260716-006: URL 构建合同已统一到 stockDetailNavigation.ts buildStockDetailUrl
  // 3 个导航点（MarketWorkspacePage / useStockDetailActions / StockDetailPage 左栏）必须全部使用此函数
  const navSrc = readSource(join(__dirname, '..', 'stockDetailNavigation.ts'))
  // 必须生成 /stock/:symbol?originScope=...&source=...&strategy=... 模式
  assert.ok(/`[/]stock[/]\$\{symbol\}\?\$\{params\.toString\(\)\}`/.test(navSrc), 'buildStockDetailUrl 必须返回 /stock/:symbol?params 模式')
  // params 必须包含 originScope + source + strategy
  assert.ok(/originScope:\s*opts\.originScope/.test(navSrc), 'params 必须包含 originScope')
  assert.ok(/source,/.test(navSrc), 'params 必须包含 source')
  assert.ok(/strategy,/.test(navSrc), 'params 必须包含 strategy')
  // returnTo 通过 params.set 编码（URLSearchParams 自动编码）
  assert.ok(/params\.set\('returnTo',\s*opts\.returnTo\)/.test(navSrc), 'returnTo 必须通过 params.set 编码')
  // MarketWorkspacePage 必须调用 buildStockDetailUrl 且传 returnTo
  const marketSrc = readSource(MARKET_WORKSPACE_PAGE)
  assert.ok(/buildStockDetailUrl\(symbol,\s*\{[\s\S]*?returnTo,[\s\S]*?\}\)/.test(marketSrc), 'MarketWorkspacePage 必须调用 buildStockDetailUrl 并传 returnTo')
})

test('CHANGE-005-10: useStockDetailActions 不使用 useMarketStocks', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  assert.ok(!/useMarketStocks\s*\(/.test(src), '禁止使用旧 useMarketStocks 函数调用')
  assert.ok(/usePublishedRuns\('dsa_selector'/.test(src), '必须使用 usePublishedRuns("dsa_selector")')
  assert.ok(/useStrategyRunResults\(/.test(src), '必须使用 useStrategyRunResults')
  // CHANGE-20260715-007: decodeMarketListContext 调用已移至 resolveDetailSourceContext（detailSourceContext.ts）
  // useStockDetailActions 不再自行调用 decodeMarketListContext(returnTo)，改为接收 marketContext 参数
  assert.ok(!/decodeMarketListContext\(returnTo\)/.test(src), 'useStockDetailActions 不再自行调用 decodeMarketListContext(returnTo)')
  assert.ok(/marketContext:\s*MarketListContext \| null/.test(src), 'StockDetailActionsParams 必须接收 marketContext: MarketListContext | null')
  assert.ok(/buildStrategyResultQueryParams\(marketContext\)/.test(src), '必须使用 buildStrategyResultQueryParams(marketContext)')
  // resolveDetailSourceContext 内部必须调用 decodeMarketListContext(returnTo)
  const detailSrc = readSource(DETAIL_SOURCE_CONTEXT)
  assert.ok(/decodeMarketListContext\(returnTo\)/.test(detailSrc), 'resolveDetailSourceContext 必须调用 decodeMarketListContext(returnTo)')
})

test('CHANGE-005-11: 上一只/下一只通过 buildStockDetailUrl 保留 originScope/returnTo/timeframe', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // CHANGE-20260716-006: 上一只/下一只必须使用 buildStockDetailUrl 统一构建
  // useStockDetailActions.ts 与 stockDetailNavigation.ts 同目录，使用相对路径 './stockDetailNavigation'
  assert.ok(/from '\.\/stockDetailNavigation'/.test(src), '必须从 ./stockDetailNavigation 导入 buildStockDetailUrl')
  // 必须调用 buildStockDetailUrl(target.symbol, { originScope, returnTo, timeframe })
  assert.ok(/buildStockDetailUrl\(target\.symbol,\s*\{[\s\S]*?originScope,[\s\S]*?returnTo,[\s\S]*?timeframe,[\s\S]*?\}\)/.test(src), '上一只/下一只必须调用 buildStockDetailUrl 并传 originScope/returnTo/timeframe')
  // originScope 必须基于 source 推导（source=selection → market；source=watchlist → watchlist）
  // 实际代码：const originScope: OriginScope = source === 'selection' ? 'market' : 'watchlist'
  assert.ok(/originScope[^=]*=\s*source === 'selection' \? 'market' : 'watchlist'/.test(src), 'originScope 必须基于 source 推导（source=selection → market）')
  // buildStockDetailUrl 内部必须处理 timeframe（由 stockDetailNavigation.ts 守护）
  const navSrc = readSource(join(__dirname, '..', 'stockDetailNavigation.ts'))
  assert.ok(/if \(opts\.timeframe\)\s*\{[\s\S]*?params\.set\('timeframe',\s*opts\.timeframe\)/.test(navSrc), 'buildStockDetailUrl 必须处理 timeframe 参数')
  assert.ok(/if \(opts\.returnTo\)\s*\{[\s\S]*?params\.set\('returnTo',\s*opts\.returnTo\)/.test(navSrc), 'buildStockDetailUrl 必须处理 returnTo 参数')
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
