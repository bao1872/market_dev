// [DetailSourceLoadingContract] - 描述: 详情页来源列表 loading 占位契约（CHANGE-20260715-004）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/detailSourceLoadingContract.test.ts
//
// 覆盖 Bug 1 修复契约：
//   1. useStockDetailActions 暴露 sourceListLoading 字段
//   2. StockDetailPage 在 sourceListLoading=true 时渲染 loading 占位
//   3. StockDetailPage 在 sourceListLoading=false && sourceStocks.length>0 时渲染列表
//   4. 不在加载中时直接渲染空列表（避免突然出现）
//   5. loading 占位包含 data-testid="detail-source-list-loading"
//   6. loading 占位显示来源类型 header（行情来源/自选来源）
//   7. CSS .tv-source-list-placeholder 存在
//   8. MarketWorkspacePage.handleNavigateToStock 显式传 source 和 strategy
//   9. scope=market → source=selection&strategy=dsa_selector
//  10. scope=watchlist → source=watchlist&strategy=watchlist_monitor

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

function readSource(p: string): string {
  return readFileSync(p, 'utf8')
}

test('CHANGE-004-1: useStockDetailActions 暴露 sourceListLoading 字段', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // 接口字段
  assert.ok(/sourceListLoading:\s*boolean/.test(src), 'StockDetailActions 接口必须包含 sourceListLoading: boolean')
  // 返回值
  assert.ok(/sourceListLoading,/.test(src), 'useStockDetailActions 返回对象必须包含 sourceListLoading')
  // 实现逻辑
  assert.ok(/const sourceListLoading = hasMarketContext/.test(src), 'sourceListLoading 必须基于 hasMarketContext 计算')
  assert.ok(/publishedRunsQuery\.isLoading \|\| !activeRunId \|\| sourceResultsQuery\.isLoading/.test(src), 'sourceListLoading 必须考虑 publishedRunsQuery.isLoading + activeRunId 缺失 + sourceResultsQuery.isLoading')
  assert.ok(/monitorStatusQuery\.isLoading/.test(src), '无 marketContext 时使用 monitorStatusQuery.isLoading')
})

test('CHANGE-004-2: StockDetailPage 在 sourceListLoading=true 时渲染 loading 占位', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  assert.ok(/detailActions\.sourceListLoading && \(/.test(src), 'StockDetailPage 必须根据 sourceListLoading 渲染 loading 占位')
  assert.ok(/data-testid="detail-source-list-loading"/.test(src), 'loading 占位必须含 data-testid="detail-source-list-loading"')
  assert.ok(/tv-source-list-placeholder/.test(src), 'loading 占位必须含 tv-source-list-placeholder 类')
})

test('CHANGE-004-3: StockDetailPage 列表渲染条件排除 loading 状态', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // 列表渲染必须显式排除 loading 状态，避免同时渲染 loading + 列表
  assert.ok(/!detailActions\.sourceListLoading && detailActions\.sourceStocks\.length > 0/.test(src), '列表渲染条件必须为 !sourceListLoading && sourceStocks.length > 0')
})

test('CHANGE-004-4: loading 占位显示来源类型 header', () => {
  const src = readSource(STOCK_DETAIL_PAGE)
  // loading 占位和列表都应显示 header（行情来源/自选来源）
  const matches = src.match(/sourceListKind === 'market' \? '行情来源' : '自选来源'/g)
  assert.ok(matches && matches.length >= 2, 'loading 占位和列表都需显示 header')
})

test('CHANGE-004-5: CSS .tv-source-list-placeholder 存在', () => {
  const src = readSource(GLOBAL_SCSS)
  assert.ok(/\.tv-source-list-placeholder\s*\{/.test(src), 'global.scss 必须定义 .tv-source-list-placeholder')
})

test('CHANGE-004-6: MarketWorkspacePage.handleNavigateToStock 显式传 source/strategy', () => {
  const src = readSource(MARKET_WORKSPACE_PAGE)
  // 必须包含 handleNavigateToStock 函数
  assert.ok(/handleNavigateToStock/.test(src), 'MarketWorkspacePage 必须实现 handleNavigateToStock')
  // scope=market → source=selection
  assert.ok(/scope === 'market' \? 'selection' : 'watchlist'/.test(src), '必须根据 scope 显式传 source')
  // scope=market → strategy=dsa_selector
  assert.ok(/DSA_STRATEGY_KEY/.test(src), 'scope=market 时必须传 DSA_STRATEGY_KEY')
  assert.ok(/'watchlist_monitor'/.test(src), 'scope=watchlist 时必须传 watchlist_monitor')
  // 必须保留 returnTo 完整 URL
  assert.ok(/returnTo=\$\{encodeURIComponent\(returnTo\)\}/.test(src), '必须编码并保留 returnTo')
})

test('CHANGE-004-7: 来源 URL 包含完整 source + strategy + returnTo', () => {
  const src = readSource(MARKET_WORKSPACE_PAGE)
  // 必须使用模板字符串拼接 URL，包含 source、strategy、returnTo 三个参数
  assert.ok(/\/stock\/\$\{symbol\}\?source=\$\{src\}&strategy=\$\{strat\}&returnTo=/.test(src), '详情 URL 必须包含 source/strategy/returnTo')
})

test('CHANGE-004-8: useStockDetailActions 不使用 useMarketStocks', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // 禁止以函数调用形式使用 useMarketStocks（注释中可以提及）
  assert.ok(!/useMarketStocks\s*\(/.test(src), '禁止使用旧 useMarketStocks 函数调用')
  // 必须复用 publishedRuns + useStrategyRunResults
  assert.ok(/usePublishedRuns\('dsa_selector'/.test(src), '必须使用 usePublishedRuns("dsa_selector")')
  assert.ok(/useStrategyRunResults\(activeRunId/.test(src), '必须使用 useStrategyRunResults(activeRunId, sourceListParams)')
  // 必须使用 decodeMarketListContext + buildStrategyResultQueryParams
  assert.ok(/decodeMarketListContext\(returnTo\)/.test(src), '必须使用 decodeMarketListContext(returnTo)')
  assert.ok(/buildStrategyResultQueryParams\(marketContext\)/.test(src), '必须使用 buildStrategyResultQueryParams(marketContext)')
})

test('CHANGE-004-9: 上一只/下一只保留 source/strategy/returnTo', () => {
  const src = readSource(USE_DETAIL_ACTIONS)
  // navigateToStock 必须保留 source + strategy
  assert.ok(/\/stock\/\$\{target\.symbol\}\?source=\$\{source\}&strategy=\$\{strategy\}/.test(src), '上一只/下一只必须保留 source + strategy')
  // navigateToStock 必须保留 returnTo（通过 returnToParam 变量拼接）
  assert.ok(/returnToParam = returnTo \? `&returnTo=/.test(src), '上一只/下一只必须保留 returnTo')
})
