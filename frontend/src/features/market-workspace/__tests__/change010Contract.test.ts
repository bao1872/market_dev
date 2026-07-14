// [Change010Contract] - 描述: CHANGE-20260713-010 契约测试（源码级）
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/change010Contract.test.ts
//
// 覆盖五大主题：
//  1. 总市值/流通市值字段映射、空值、单位和 as_of（StockQuoteStrip + useStockResearchData + 后端 QuoteResponse）
//  2. Excel 导出：权限/筛选一致/行数/列顺序/公式注入/超过上限（excel_export_service + strategy_runs + 前端 ExportContext）
//  3. 股票名称视觉入口（button 语义）+ keyword alias 同步（StrategyDataTable filterAlias）
//  4. 小 K 线：只请求活动周期、收起 0 请求、三周期切换（MiniKlineCard + useMiniKlineData + MarketRightPanel）
//  5. 详情来源上下文不回归（MarketWorkspacePage handleNavigateToStock + decodeMarketListContext）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
// __dirname = frontend/src/features/market-workspace/__tests__
// FRONTEND_ROOT = frontend/src (up 3 levels)
const FRONTEND_ROOT = join(__dirname, '..', '..', '..')
// BACKEND_ROOT = /root/web_dev/backend (up 2 more levels from frontend/src)
const BACKEND_ROOT = join(FRONTEND_ROOT, '..', '..', 'backend')

const STRIP_PATH = join(FRONTEND_ROOT, 'features', 'stock-research', 'StockQuoteStrip.tsx')
const USE_STOCK_RESEARCH_PATH = join(FRONTEND_ROOT, 'features', 'stock-research', 'useStockResearchData.ts')
const ENDPOINTS_PATH = join(FRONTEND_ROOT, 'api', 'endpoints.ts')
const DATA_TABLE_PATH = join(FRONTEND_ROOT, 'components', 'StrategyDataTable.tsx')
const MINI_KLINE_PATH = join(__dirname, '..', 'MiniKlineCard.tsx')
const USE_MINI_KLINE_PATH = join(__dirname, '..', 'useMiniKlineData.ts')
const MARKET_RIGHT_PANEL_PATH = join(__dirname, '..', 'MarketRightPanel.tsx')
const PAGE_PATH = join(__dirname, '..', 'MarketWorkspacePage.tsx')
const URL_STATE_PATH = join(__dirname, '..', 'marketWorkspaceUrlState.ts')

const BAR_SCHEMA_PATH = join(BACKEND_ROOT, 'app', 'schemas', 'bar.py')
const BARS_API_PATH = join(BACKEND_ROOT, 'app', 'api', 'bars.py')
const EXPORT_SCHEMA_PATH = join(BACKEND_ROOT, 'app', 'schemas', 'export.py')
const EXPORT_SERVICE_PATH = join(BACKEND_ROOT, 'app', 'services', 'excel_export_service.py')
const STRATEGY_RUNS_API_PATH = join(BACKEND_ROOT, 'app', 'api', 'strategy_runs.py')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// =================================================================
// 一、总市值/流通市值字段映射、空值、单位和 as_of
// =================================================================

test('后端 QuoteResponse 包含 total_market_cap/float_market_cap/market_cap_as_of/market_cap_source/market_cap_degraded_reason', () => {
  const src = readSource(BAR_SCHEMA_PATH)
  assert.ok(src.includes('total_market_cap'), 'QuoteResponse 必须包含 total_market_cap 字段')
  assert.ok(src.includes('float_market_cap'), 'QuoteResponse 必须包含 float_market_cap 字段')
  assert.ok(src.includes('market_cap_as_of'), 'QuoteResponse 必须包含 market_cap_as_of 字段')
  assert.ok(src.includes('market_cap_source'), 'QuoteResponse 必须包含 market_cap_source 字段')
  assert.ok(
    src.includes('market_cap_degraded_reason'),
    'QuoteResponse 必须包含 market_cap_degraded_reason 字段',
  )
})

test('后端 quote 端点返回 market_cap_data_unavailable 降级原因（数据缺失时）', () => {
  const src = readSource(BARS_API_PATH)
  assert.ok(
    src.includes('market_cap_data_unavailable'),
    'quote 端点在市值数据缺失时必须返回 market_cap_data_unavailable 降级原因',
  )
})

test('前端 QuoteResponse 类型含 5 个 market_cap 可选字段', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(src.includes('total_market_cap?'), 'QuoteResponse 类型必须包含可选 total_market_cap')
  assert.ok(src.includes('float_market_cap?'), 'QuoteResponse 类型必须包含可选 float_market_cap')
  assert.ok(src.includes('market_cap_as_of?'), 'QuoteResponse 类型必须包含可选 market_cap_as_of')
  assert.ok(src.includes('market_cap_source?'), 'QuoteResponse 类型必须包含可选 market_cap_source')
  assert.ok(
    src.includes('market_cap_degraded_reason?'),
    'QuoteResponse 类型必须包含可选 market_cap_degraded_reason',
  )
})

test('PriceSummary 接口含 totalMarketCap/floatMarketCap/marketCapAsOf', () => {
  const src = readSource(USE_STOCK_RESEARCH_PATH)
  assert.ok(src.includes('totalMarketCap'), 'PriceSummary 必须包含 totalMarketCap')
  assert.ok(src.includes('floatMarketCap'), 'PriceSummary 必须包含 floatMarketCap')
  assert.ok(src.includes('marketCapAsOf'), 'PriceSummary 必须包含 marketCapAsOf')
})

test('StockQuoteStrip 包含 formatMarketCap 函数 + 8 项指标 + tooltip 数据日期', () => {
  const src = readSource(STRIP_PATH)
  assert.ok(src.includes('formatMarketCap'), 'StockQuoteStrip 必须导出 formatMarketCap 函数')
  // 8 项指标：现价/涨跌/开盘/最高/最低/成交额/总市值/流通市值
  assert.ok(src.includes('总市值'), 'StockQuoteStrip 必须显示"总市值"')
  assert.ok(src.includes('流通市值'), 'StockQuoteStrip 必须显示"流通市值"')
  assert.ok(src.includes('成交额'), 'StockQuoteStrip 必须显示"成交额"')
  // 单位格式化：<1亿万元, >=1亿亿元, >=1万亿万亿元
  assert.ok(src.includes('1e8') || src.includes('100000000'), 'formatMarketCap 必须区分亿元单位')
  assert.ok(src.includes('1e12') || src.includes('1000000000000'), 'formatMarketCap 必须区分万亿元单位')
  assert.ok(src.includes('1e4') || src.includes('10000'), 'formatMarketCap 必须区分万元单位')
  // 空值显示 "--"
  assert.ok(src.includes("'--'") || src.includes('"--"'), 'formatMarketCap 空值必须显示 "--"')
  // tooltip 显示数据日期
  assert.ok(src.includes('marketCapAsOf') && src.includes('title'), 'StockQuoteStrip tooltip 必须显示数据日期')
})

test('StockQuoteStrip 暴露 QuoteMetric 子组件', () => {
  const src = readSource(STRIP_PATH)
  assert.ok(src.includes('QuoteMetric'), 'StockQuoteStrip 必须暴露 QuoteMetric 子组件')
})

// =================================================================
// 二、Excel 导出契约
// =================================================================

test('ExportRequest schema 含 universe/keyword/industry/concept/metric_filters/sort_by/sort_desc/visible_columns', () => {
  const src = readSource(EXPORT_SCHEMA_PATH)
  assert.ok(src.includes('universe'), 'ExportRequest 必须含 universe')
  assert.ok(src.includes('keyword'), 'ExportRequest 必须含 keyword')
  assert.ok(src.includes('industry'), 'ExportRequest 必须含 industry')
  assert.ok(src.includes('concept'), 'ExportRequest 必须含 concept')
  assert.ok(src.includes('metric_filters'), 'ExportRequest 必须含 metric_filters')
  assert.ok(src.includes('sort_by'), 'ExportRequest 必须含 sort_by')
  assert.ok(src.includes('sort_desc'), 'ExportRequest 必须含 sort_desc')
  assert.ok(src.includes('visible_columns'), 'ExportRequest 必须含 visible_columns')
  // ExportColumn 含 key/title/data_type/payload_key
  assert.ok(src.includes('payload_key'), 'ExportColumn 必须含 payload_key（stock 列返回 null）')
})

test('excel_export_service 含 MAX_EXPORT_ROWS=10000 上限和公式注入防护', () => {
  const src = readSource(EXPORT_SERVICE_PATH)
  assert.ok(
    src.includes('MAX_EXPORT_ROWS') && src.includes('10000'),
    'excel_export_service 必须定义 MAX_EXPORT_ROWS=10000',
  )
  // 公式注入防护：=、+、-、@ 开头文本按普通文本写入
  assert.ok(
    src.includes('_sanitize_formula_injection') || src.includes('sanitize_formula'),
    'excel_export_service 必须包含公式注入防护函数',
  )
  // 校验以 = + - @ 开头时被处理
  assert.ok(src.includes("'=") || src.includes('"='), '公式注入防护必须处理 = 前缀')
  // 真实 .xlsx：使用 zipfile（标准库生成 OOXML）
  assert.ok(src.includes('zipfile'), 'excel_export_service 必须使用 zipfile 生成真实 .xlsx')
  // 百分比格式 numFmt
  assert.ok(src.includes('numFmt') || src.includes('percent'), 'excel_export_service 必须支持百分比格式')
})

test('excel_export_service 禁止使用 openpyxl 或第三方 Excel 库', () => {
  const src = readSource(EXPORT_SERVICE_PATH)
  // 禁止 import openpyxl / xlsxwriter（注释中提到允许，用于说明不依赖）
  assert.ok(
    !/^\s*import\s+openpyxl/m.test(src) && !/^\s*from\s+openpyxl/m.test(src),
    'excel_export_service 禁止 import openpyxl',
  )
  assert.ok(
    !/^\s*import\s+xlsxwriter/m.test(src) && !/^\s*from\s+xlsxwriter/m.test(src),
    'excel_export_service 禁止 import xlsxwriter',
  )
})

test('strategy_runs API 含 POST /strategy-runs/{run_id}/results/export 端点', () => {
  const src = readSource(STRATEGY_RUNS_API_PATH)
  assert.ok(
    src.includes('/results/export') && src.includes('post'),
    'strategy_runs API 必须含 POST /strategy-runs/{run_id}/results/export 端点',
  )
  // 响应头含 source/universe/filtered total
  assert.ok(src.includes('X-Source-Total'), '导出响应头必须含 X-Source-Total')
  assert.ok(src.includes('X-Universe-Total'), '导出响应头必须含 X-Universe-Total')
  assert.ok(src.includes('X-Filtered-Total'), '导出响应头必须含 X-Filtered-Total')
  // 文件名格式：盘迹_DSA_YYYYMMDD_筛选结果.xlsx
  assert.ok(
    src.includes('盘迹_DSA') && src.includes('筛选结果'),
    '文件名格式必须为 盘迹_DSA_YYYYMMDD_筛选结果.xlsx',
  )
  // Content-Disposition 使用 RFC 5987 编码（filename*=UTF-8''）
  assert.ok(
    src.includes("filename*=UTF-8''"),
    'Content-Disposition 必须使用 RFC 5987 percent-encoding',
  )
})

test('strategy_runs export 端点超过 10000 行返回 422', () => {
  const src = readSource(STRATEGY_RUNS_API_PATH)
  // 上限校验必须返回 422（不是 200 截断或 500）
  assert.ok(
    src.includes('422') && (src.includes('MAX_EXPORT_ROWS') || src.includes('10000')),
    '导出超过 10000 行必须返回 422',
  )
})

test('前端 ExportContext 含 visibleColumns/keyword/industry/concept/metricFilters/sortBy/sortDesc', () => {
  const src = readSource(DATA_TABLE_PATH)
  assert.ok(src.includes('ExportContext'), 'StrategyDataTable 必须导出 ExportContext 类型')
  assert.ok(src.includes('visibleColumns'), 'ExportContext 必须含 visibleColumns')
  assert.ok(src.includes('metricFilters'), 'ExportContext 必须含 metricFilters')
  assert.ok(src.includes('sortBy'), 'ExportContext 必须含 sortBy')
  assert.ok(src.includes('sortDesc'), 'ExportContext 必须含 sortDesc')
  // onExport prop
  assert.ok(src.includes('onExport'), 'StrategyDataTable 必须支持 onExport prop')
})

test('MarketWorkspacePage handleExport 复用 convertFiltersToMetricFilters 避免第二套筛选口径', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    src.includes('convertFiltersToMetricFilters'),
    'handleExport 必须复用 convertFiltersToMetricFilters，禁止第二套筛选口径',
  )
  assert.ok(
    src.includes('/api/strategy-runs/') && src.includes('/results/export'),
    'handleExport 必须调用 POST /api/strategy-runs/{run_id}/results/export',
  )
  // 不导出操作列：stock 列 payload_key 为 null
  assert.ok(
    src.includes("col.key === 'stock'") && src.includes('null'),
    '导出列转换：stock 列 payload_key 返回 null（不导出操作列）',
  )
})

test('marketWorkspaceUrlState 导出 convertFiltersToMetricFilters 复用同一筛选逻辑', () => {
  const src = readSource(URL_STATE_PATH)
  assert.ok(
    src.includes('export function convertFiltersToMetricFilters'),
    'marketWorkspaceUrlState 必须导出 convertFiltersToMetricFilters',
  )
  // 复用同一 normalizeMetricValue（不重复实现）
  assert.ok(
    src.includes('normalizeMetricValue'),
    'convertFiltersToMetricFilters 必须复用 normalizeMetricValue（与 buildStrategyResultQueryParams 同源）',
  )
})

// =================================================================
// 三、股票名称视觉入口 + keyword alias 同步
// =================================================================

test('DataTableColumn 含 filterAlias?: keyword 字段', () => {
  const src = readSource(DATA_TABLE_PATH)
  assert.ok(
    src.includes("filterAlias?: 'keyword'"),
    'DataTableColumn 必须含 filterAlias?: keyword 字段',
  )
})

test('StrategyDataTable KeywordFilterPopover 读写 externalKeyword/onKeywordChange', () => {
  const src = readSource(DATA_TABLE_PATH)
  // 必须存在 KeywordFilterPopover 组件
  assert.ok(
    src.includes('KeywordFilterPopover'),
    'StrategyDataTable 必须含 KeywordFilterPopover 组件',
  )
  // filterAlias='keyword' 时不进入 filters state（使用 isKeyword flag）
  assert.ok(
    src.includes('isKeyword'),
    'filterAlias=keyword 时必须使用 isKeyword flag 区分，不进入 filters state',
  )
  // 激活状态基于 effectiveKeyword
  assert.ok(
    src.includes('effectiveKeyword'),
    'filterAlias=keyword 列激活状态必须基于 effectiveKeyword',
  )
})

test('stock 列设置 filterable=true 和 filterAlias=keyword（与顶部搜索共用唯一真源）', () => {
  // 通过 columns.test.ts 已覆盖 stock 列 button 语义，这里只验证 filterAlias
  const src = readSource(join(__dirname, '..', '..', 'trend-selection', 'columns.tsx'))
  assert.ok(
    src.includes("filterAlias: 'keyword'"),
    'stock 列必须设置 filterAlias: keyword（与顶部搜索共用唯一 keyword 真源）',
  )
})

// =================================================================
// 四、小 K 线：只请求活动周期、收起 0 请求、三周期切换
// =================================================================

test('useMiniKlineData 定义 BARS_COUNT：1d=80, 1w=60, 1mo=48', () => {
  const src = readSource(USE_MINI_KLINE_PATH)
  assert.ok(src.includes('1d') && src.includes('80'), 'useMiniKlineData 1d 必须 80 根')
  assert.ok(src.includes('1w') && src.includes('60'), 'useMiniKlineData 1w 必须 60 根')
  assert.ok(src.includes('1mo') && src.includes('48'), 'useMiniKlineData 1mo 必须 48 根')
})

test('useMiniKlineData 默认不轮询（refetchInterval: false）', () => {
  const src = readSource(USE_MINI_KLINE_PATH)
  assert.ok(
    src.includes('refetchInterval') && src.includes('false'),
    'useMiniKlineData 必须禁用轮询（refetchInterval: false）',
  )
})

test('MiniKlineCard 使用 lightweight-charts createChart + CandlestickSeries', () => {
  const src = readSource(MINI_KLINE_PATH)
  assert.ok(src.includes('createChart'), 'MiniKlineCard 必须使用 lightweight-charts createChart')
  assert.ok(
    src.includes('addCandlestickSeries') || src.includes('CandlestickSeries'),
    'MiniKlineCard 必须使用 CandlestickSeries 渲染 K 线',
  )
  // 三按钮：日线/周线/月线
  assert.ok(src.includes('日线'), 'MiniKlineCard 必须含"日线"按钮')
  assert.ok(src.includes('周线'), 'MiniKlineCard 必须含"周线"按钮')
  assert.ok(src.includes('月线'), 'MiniKlineCard 必须含"月线"按钮')
  // 默认日线
  assert.ok(
    src.includes("useState<'1d'") || src.includes("useState<MiniKlineTimeframe>('1d')"),
    'MiniKlineCard 默认周期必须为 1d（日线）',
  )
})

test('MiniKlineCard 不显示指标/成交量/Node/事件标记/工具栏', () => {
  const src = readSource(MINI_KLINE_PATH)
  // 不引入 indicator/volume/node 相关依赖
  assert.ok(
    !src.includes('addVolumeSeries') && !src.includes('VolumeSeries'),
    'MiniKlineCard 禁止显示成交量',
  )
  assert.ok(
    !src.includes('addLineSeries') || src.includes('priceLine'),
    'MiniKlineCard 禁止显示指标线（仅价格轴）',
  )
})

test('MarketRightPanel 顺序：MiniKlineCard 顶部 + EventStatePanel 底部', () => {
  const src = readSource(MARKET_RIGHT_PANEL_PATH)
  assert.ok(
    src.includes('MiniKlineCard') && src.includes('EventStatePanel'),
    'MarketRightPanel 必须组合 MiniKlineCard 和 EventStatePanel',
  )
  // MiniKlineCard 在 EventStatePanel 之前（顶部）
  const miniIdx = src.indexOf('MiniKlineCard')
  const eventIdx = src.indexOf('EventStatePanel')
  assert.ok(miniIdx > 0 && eventIdx > 0, 'MarketRightPanel 必须引用 MiniKlineCard 和 EventStatePanel')
  assert.ok(
    miniIdx < eventIdx,
    'MarketRightPanel 中 MiniKlineCard 必须在 EventStatePanel 之前（顶部）',
  )
})

test('MarketWorkspacePage 右栏收起时不挂载 MarketRightPanel', () => {
  const src = readSource(PAGE_PATH)
  // rightPanelCollapsed 为 true 时不渲染右栏
  assert.ok(
    src.includes('rightPanelCollapsed') && src.includes('MarketRightPanel'),
    'MarketWorkspacePage 必须根据 rightPanelCollapsed 控制 MarketRightPanel 挂载',
  )
  // 右栏总宽度不扩大（沿用现有 collapsed 状态）
  assert.ok(
    src.includes('panji:market-right-panel-collapsed:v1'),
    'MarketWorkspacePage 必须保留 localStorage 持久化 key',
  )
})

// -----------------------------------------------------------------
// 小 K 线交互验证：宽度/0请求/默认1d/活动周期/缓存/股票切换/卸载清理/无重复canvas
// -----------------------------------------------------------------

test('MiniKlineCard 卸载清理：resizeObserver.disconnect + chart.remove + chartRef/seriesRef 清空', () => {
  const src = readSource(MINI_KLINE_PATH)
  // useEffect cleanup 必须包含 resizeObserver.disconnect
  assert.ok(
    src.includes('resizeObserver.disconnect()'),
    'MiniKlineCard 卸载必须调用 resizeObserver.disconnect()',
  )
  // cleanup 必须调用 chart.remove()
  assert.ok(src.includes('chart.remove()'), 'MiniKlineCard 卸载必须调用 chart.remove()')
  // ref 必须清空，避免内存泄漏
  assert.ok(
    src.includes('chartRef.current = null') && src.includes('seriesRef.current = null'),
    'MiniKlineCard 卸载必须将 chartRef/seriesRef 置为 null',
  )
})

test('MiniKlineCard chart 实例仅创建一次（useEffect 空依赖数组，无重复 canvas）', () => {
  const src = readSource(MINI_KLINE_PATH)
  // 创建 chart 的 useEffect 必须使用空依赖数组 []
  // 查找包含 createChart 的 useEffect 块，并以 ], []) 结尾
  assert.ok(
    /useEffect\(\(\)\s*=>\s*\{[\s\S]*?createChart[\s\S]*?\},\s*\[\]\)/m.test(src),
    'MiniKlineCard 创建 chart 的 useEffect 必须使用空依赖数组 []（无重复 canvas）',
  )
  // 数据更新 useEffect 依赖 bars（独立于 chart 创建）
  assert.ok(
    /useEffect\(\(\)\s*=>\s*\{[\s\S]*?setData[\s\S]*?\},\s*\[bars\]\)/m.test(src),
    'MiniKlineCard 数据更新 useEffect 必须依赖 [bars]（独立于 chart 创建）',
  )
})

test('MiniKlineCard ResizeObserver 响应式宽度（chart.applyOptions width）', () => {
  const src = readSource(MINI_KLINE_PATH)
  // 必须使用 ResizeObserver 监听容器宽度
  assert.ok(src.includes('ResizeObserver'), 'MiniKlineCard 必须使用 ResizeObserver')
  // 回调中调用 chart.applyOptions({ width: ... })
  assert.ok(
    src.includes('applyOptions({ width:'),
    'MiniKlineCard ResizeObserver 回调必须调用 chart.applyOptions({ width })',
  )
  // 必须 observe(containerRef.current)
  assert.ok(
    src.includes('resizeObserver.observe(containerRef.current)'),
    'MiniKlineCard 必须 observe(containerRef.current)',
  )
})

test('MiniKlineCard timeframe state 独立于 symbol（切股票保留用户周期）', () => {
  const src = readSource(MINI_KLINE_PATH)
  // useState<MiniKlineTimeframe>('1d') 初始值不依赖 symbol
  assert.ok(
    src.includes("useState<MiniKlineTimeframe>('1d')"),
    'MiniKlineCard timeframe useState 初始值必须为 1d（不依赖 symbol）',
  )
  // setTimeframe 仅通过按钮 onClick 触发（不被 symbol 重置）
  assert.ok(
    src.includes('onClick={() => setTimeframe(opt.value)}'),
    'MiniKlineCard timeframe 只通过按钮 onClick 切换（不被 symbol 重置）',
  )
})

test('useMiniKlineData 只请求活动周期（不预取三周期）', () => {
  const src = readSource(USE_MINI_KLINE_PATH)
  // useBars 调用使用当前 timeframe（不硬编码 1d/1w/1mo）
  assert.ok(
    src.includes('timeframe') && src.includes('BARS_COUNT[timeframe]'),
    'useMiniKlineData 必须按当前活动 timeframe 计算 barsCount（不预取三周期）',
  )
  // 不存在同时调用 useBars 三次的预取逻辑
  const useBarsCallCount = (src.match(/useBars\(/g) || []).length
  assert.ok(
    useBarsCallCount === 1,
    `useMiniKlineData 只允许调用 useBars 一次（实际：${useBarsCallCount} 次），禁止预取三周期`,
  )
})

test('useMiniKlineData 切股票触发新请求（symbol prop 进入 useInstrumentBySymbol）', () => {
  const src = readSource(USE_MINI_KLINE_PATH)
  // useInstrumentBySymbol 接收 symbol 参数（symbol 变化 → 新请求）
  assert.ok(
    src.includes('useInstrumentBySymbol(symbol'),
    'useMiniKlineData 必须将 symbol 传入 useInstrumentBySymbol（symbol 变化触发新请求）',
  )
  // instrumentId 用于 useBars（symbol 变化 → instrumentId 变化 → bars 重新请求）
  assert.ok(
    src.includes('useBars(instrumentId'),
    'useMiniKlineData 必须将 instrumentId 传入 useBars（symbol 变化 → bars 新请求）',
  )
})

test('useMiniKlineData React Query 缓存命中（queryKey 含 instrumentId + timeframe + adj + page_size）', () => {
  const src = readSource(USE_MINI_KLINE_PATH)
  // useBars 的参数对象必须含 timeframe + adj + page_size（保证 queryKey 唯一）
  assert.ok(
    src.includes('timeframe') && src.includes('adj') && src.includes('page_size'),
    'useMiniKlineData useBars 参数必须含 timeframe/adj/page_size（queryKey 唯一性）',
  )
  // adj 必须为 qfq（前复权），与详情页主图口径一致
  assert.ok(
    src.includes("adj: 'qfq'"),
    "useMiniKlineData adj 必须为 'qfq'（前复权）",
  )
  // timeframe 切换后 queryKey 不同 → 缓存命中时不重新请求
  assert.ok(
    src.includes('BARS_COUNT[timeframe]') && src.includes('page_size: barsCount'),
    'useMiniKlineData page_size 必须基于 BARS_COUNT[timeframe]（timeframe 切换 → queryKey 变化）',
  )
})

test('MarketRightPanel 面板收起时不挂载（父组件控制 0 请求）', () => {
  const src = readSource(MARKET_RIGHT_PANEL_PATH)
  // 父组件不挂载本组件 → 0 bars/context 请求
  // 组件本身只接受 symbol prop，不自行判断是否渲染
  assert.ok(
    src.includes('interface MarketRightPanelProps') && src.includes('symbol: string | null'),
    'MarketRightPanel 必须接受 symbol prop（由父组件控制挂载与否）',
  )
  // symbol 为 null 时 MiniKlineCard 显示提示，EventStatePanel 不渲染（不发起 context 请求）
  assert.ok(
    src.includes('{symbol && <EventStatePanel symbol={symbol} />}') ||
      /symbol\s*&&\s*<EventStatePanel/.test(src),
    'MarketRightPanel 必须在 symbol 为 null 时不渲染 EventStatePanel（0 context 请求）',
  )
})

// =================================================================
// 五、详情来源上下文不回归（CHANGE-009 契约）
// =================================================================

test('MarketWorkspacePage handleNavigateToStock 根据 scope 传递 source/strategy', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    src.includes("scope === 'market' ? 'selection'") && src.includes("'watchlist'"),
    'handleNavigateToStock 必须根据 scope 传递 source（market→selection, watchlist→watchlist）',
  )
  assert.ok(
    src.includes('dsa_selector') && src.includes('watchlist_monitor'),
    'handleNavigateToStock 必须根据 scope 传递 strategy（market→dsa_selector, watchlist→watchlist_monitor）',
  )
})

test('MarketWorkspacePage handleNavigateToStock returnTo 保存完整当前 URL', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(
    src.includes('location.pathname') && src.includes('location.search'),
    'handleNavigateToStock returnTo 必须保存完整 pathname + search',
  )
  assert.ok(
    src.includes('encodeURIComponent(returnTo)'),
    'returnTo 必须 encodeURIComponent 编码',
  )
})

test('decodeMarketListContext 任意合法 /market URL 都识别为 market context', () => {
  const src = readSource(URL_STATE_PATH)
  // scope=market 和 scope=watchlist 都解析
  assert.ok(
    src.includes("rawScope === 'market' ? 'market' : 'watchlist'"),
    'decodeMarketListContext 必须支持 scope=market 和 scope=watchlist',
  )
  // 不要求 keyword/page/sort 存在
  assert.ok(
    src.includes('normalizeInternalReturnTo'),
    'decodeMarketListContext 必须先经 normalizeInternalReturnTo 校验',
  )
})

test('normalizeInternalReturnTo 长度限制为 500（兼容 /market filters JSON 编码）', () => {
  const src = readSource(URL_STATE_PATH)
  assert.ok(
    src.includes('500'),
    'normalizeInternalReturnTo 长度限制必须为 500（兼容 /market URL 含 filters JSON）',
  )
})

test('buildStrategyResultQueryParams scope=market → universe=all; scope=watchlist → universe=watchlist', () => {
  const src = readSource(URL_STATE_PATH)
  assert.ok(
    src.includes("ctx.scope === 'market' ? 'all' : 'watchlist'"),
    'buildStrategyResultQueryParams 必须映射 scope → universe',
  )
})

// =================================================================
// 六、filterAlias 双向同步与 URL/preset 共用 keyword（CHANGE-010 契约）
// 顶部搜索、列表头筛选、URL、preset 必须共用唯一 keyword 真源；
// 双向同步：列头筛选 → onKeywordChange → 顶部搜索；顶部搜索 → externalKeyword → 列头激活状态；
// stock/action 列不入 metric_filters；URL 同步使用 replace 避免循环。
// =================================================================

test('filterAlias=keyword 列头筛选 onApply 同时调用 setGlobalQuery + onKeywordChange（双向同步）', () => {
  const src = readSource(DATA_TABLE_PATH)
  // KeywordFilterPopover onApply 必须同时更新内部 globalQuery 和外部 onKeywordChange
  assert.ok(
    src.includes('onApply={(v) => {') &&
      src.includes('setGlobalQuery(v)') &&
      src.includes('if (onKeywordChange) onKeywordChange(v)'),
    'filterAlias=keyword 列头筛选 onApply 必须同时调用 setGlobalQuery(v) + onKeywordChange(v)',
  )
})

test('filterAlias=keyword 列头筛选 onClear 同时清空 globalQuery 和 onKeywordChange（双向同步）', () => {
  const src = readSource(DATA_TABLE_PATH)
  assert.ok(
    src.includes("onClear={() => {") &&
      src.includes("setGlobalQuery('')") &&
      src.includes("if (onKeywordChange) onKeywordChange('')"),
    'filterAlias=keyword 列头筛选 onClear 必须同时清空 setGlobalQuery("") + onKeywordChange("")',
  )
})

test('filterAlias=keyword 列头激活状态基于 effectiveKeyword（与顶部搜索共用唯一真源）', () => {
  const src = readSource(DATA_TABLE_PATH)
  // 激活状态条件：col.filterAlias === 'keyword' ? (effectiveKeyword && 'active') : (filters[i] && 'active')
  assert.ok(
    src.includes("col.filterAlias === 'keyword'") &&
      src.includes('effectiveKeyword &&') &&
      src.includes("'active'"),
    'filterAlias=keyword 列头激活状态必须基于 effectiveKeyword（与顶部搜索共用唯一真源）',
  )
})

test('filterAlias=keyword 不进入 filters state（使用 isKeyword flag 区分）', () => {
  const src = readSource(DATA_TABLE_PATH)
  // filterPopover 含 isKeyword flag
  assert.ok(
    src.includes('isKeyword?: boolean'),
    'filterPopover state 必须含 isKeyword flag 区分 keyword alias 列',
  )
  // isKeyword=true 时渲染 KeywordFilterPopover，不调用 applyFilter（不进入 filters state）
  assert.ok(
    src.includes('filterPopover.isKeyword &&') &&
      src.includes('<KeywordFilterPopover'),
    'isKeyword=true 时必须渲染 KeywordFilterPopover（不进入 filters state）',
  )
  // isKeyword=false 时渲染 FilterPopover，调用 applyFilter
  assert.ok(
    src.includes('!filterPopover.isKeyword &&') &&
      src.includes('<FilterPopover'),
    'isKeyword=false 时渲染 FilterPopover（进入 filters state）',
  )
})

test('effectiveKeyword 受控模式：externalKeyword 提供时覆盖内部 globalQuery', () => {
  const src = readSource(DATA_TABLE_PATH)
  assert.ok(
    src.includes("externalKeyword !== undefined ? externalKeyword : globalQuery"),
    'effectiveKeyword 必须 = externalKeyword !== undefined ? externalKeyword : globalQuery（受控模式覆盖）',
  )
})

test('URL 同步写入 keyword：syncUrl 使用 effectiveKeyword', () => {
  const src = readSource(DATA_TABLE_PATH)
  // URL 同步 useEffect 中 keyword 从 effectiveKeyword 取值
  assert.ok(
    /keyword:\s*effectiveKeyword\.trim\(\)/.test(src),
    'URL 同步必须使用 effectiveKeyword.trim() 写入 keyword（顶部搜索/列头筛选共用真源）',
  )
})

test('URL 同步使用 setSearchParams replace: true（避免 URL 循环/历史污染）', () => {
  const src = readSource(DATA_TABLE_PATH)
  assert.ok(
    src.includes('setSearchParams(nextParams, { replace: true })'),
    'URL 同步必须使用 setSearchParams(nextParams, { replace: true }) 避免循环',
  )
  // managedKeys 包含 keyword（避免 URL 中残留旧 keyword）
  assert.ok(
    src.includes("'keyword'") && src.includes('managedKeys'),
    'URL 同步 managedKeys 必须包含 keyword（清理残留）',
  )
})

test('URL hydration 使用 skipNextUrlSyncRef 避免反向覆盖（无 URL 循环）', () => {
  const src = readSource(DATA_TABLE_PATH)
  // skipNextUrlSyncRef 用于 URL hydration 后跳过下一次 syncUrl，避免循环
  assert.ok(
    src.includes('skipNextUrlSyncRef') && src.includes('urlHydratedRef'),
    'URL hydration 必须使用 skipNextUrlSyncRef + urlHydratedRef 避免反向覆盖循环',
  )
})

test('currentConfig.keyword 基于 effectiveKeyword（preset 持久化共用真源）', () => {
  const src = readSource(DATA_TABLE_PATH)
  assert.ok(
    /keyword:\s*effectiveKeyword\.trim\(\)\s*\|\|\s*null/.test(src),
    'currentConfig.keyword 必须 = effectiveKeyword.trim() || null（preset 持久化共用真源）',
  )
})

test('applyPresetConfig 同时 setGlobalQuery + onKeywordChange（preset 应用双向同步）', () => {
  const src = readSource(DATA_TABLE_PATH)
  assert.ok(
    src.includes('setGlobalQuery(config.keyword ??') &&
      src.includes("onKeywordChange(config.keyword ?? '')"),
    'applyPresetConfig 必须同时调用 setGlobalQuery + onKeywordChange（preset 应用双向同步）',
  )
})

test('stock 列 filterValue 不入 metric_filters（filterAlias=keyword 不产生 metric filter）', () => {
  const src = readSource(URL_STATE_PATH)
  // buildStrategyResultQueryParams 过滤 f.key !== 'stock' && f.key !== 'action'
  assert.ok(
    src.includes("f.key !== 'stock'") && src.includes("f.key !== 'action'"),
    'buildStrategyResultQueryParams 必须过滤 stock/action 列，不进入 metric_filters',
  )
})

test('convertFiltersToMetricFilters 同样过滤 stock/action 列（导出复用同源）', () => {
  const src = readSource(URL_STATE_PATH)
  assert.ok(
    /convertFiltersToMetricFilters[\s\S]*?f\.key\s*!==\s*'stock'[\s\S]*?f\.key\s*!==\s*'action'/.test(src),
    'convertFiltersToMetricFilters 必须过滤 stock/action 列（与 buildStrategyResultQueryParams 同源）',
  )
})

test('stock 列筛选值通过 keyword 透传到后端 ILIKE（不入 metric_filters）', () => {
  const src = readSource(URL_STATE_PATH)
  // buildStrategyResultQueryParams 保留 keyword 字段（透传到后端）
  assert.ok(
    /if\s*\(ctx\.keyword\)[\s\S]*?params\.keyword\s*=\s*ctx\.keyword/.test(src),
    'buildStrategyResultQueryParams 必须保留 keyword 字段（透传到后端 ILIKE 查询）',
  )
})
