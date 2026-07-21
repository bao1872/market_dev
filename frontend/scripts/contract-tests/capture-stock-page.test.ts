// [Capture] - 描述: 专用 Capture 路由 /capture/stock/:symbol 契约测试
// 用法：node --experimental-strip-types --test scripts/contract-tests/capture-stock-page.test.ts
// 覆盖：
// 1. CaptureStockPage.tsx 文件存在
// 2. App.tsx 包含 /capture/stock/:symbol 路由，且不在 ProtectedLayout 守卫下（与公开路由同级缩进）
// 3. App.tsx 导入 CaptureStockPage 并以 <CaptureStockPage /> 作为 element
// 4. CaptureStockPage 使用 captureClient（不使用 apiClient）
// 5. CaptureStockPage 不加载 watchlist/events/memo/batchInstruments（无对应 hook 调用）
// 6. CaptureStockPage 设置 data-testid="stock-detail-capture" 与 data-render-ready
// 7. CaptureStockPage 的 isRenderReady 不依赖 eventsQuery（事件查询超时是 30s 截图失败根因）
// 8. stock_capture_service.py URL 已更新为 /capture/stock/{symbol} 并携带 instrument_id
// 9. CaptureStockPage 只调用 Snapshot API，不调用普通端点

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const FRONTEND_ROOT = join(__dirname, '..', '..')
const CAPTURE_PAGE_PATH = join(FRONTEND_ROOT, 'src', 'pages', 'CaptureStockPage.tsx')
const APP_TSX_PATH = join(FRONTEND_ROOT, 'src', 'App.tsx')
const CAPTURE_SERVICE_PATH = join(__dirname, '..', '..', '..', 'backend', 'app', 'services', 'stock_capture_service.py')
const STOCK_DETAIL_PAGE_PATH = join(FRONTEND_ROOT, 'src', 'pages', 'StockDetailPage.tsx')
const GLOBAL_SCSS_PATH = join(FRONTEND_ROOT, 'src', 'styles', 'global.scss')
// [PROMPT.md §5.3.1 V2] MobileIndicatorStage 截图根选择器
const MOBILE_STAGE_PATH = join(FRONTEND_ROOT, 'src', 'components', 'MobileIndicatorStage.tsx')
// [PROMPT.md §5.3.4 V2] chartRenderScale 集中字号/线宽缩放模块
const CHART_RENDER_SCALE_PATH = join(FRONTEND_ROOT, 'src', 'components', 'chartRenderScale.ts')
const STRATEGY_CHART_PATH = join(FRONTEND_ROOT, 'src', 'components', 'StrategyChart.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

interface ImportInfo {
  names: string[]
  module: string
}

/** 从源码中提取所有命名导入（支持多行、type 导入、as 别名、默认导入） */
function extractImports(src: string): ImportInfo[] {
  const result: ImportInfo[] = []
  let m: RegExpExecArray | null

  // 1. 命名导入：import { X, Y as Z } from 'mod'
  const namedRe = /import\s*(?:type\s+)?\{\s*([^}]+)\s*\}\s*from\s*['"]([^'"]+)['"]/g
  while ((m = namedRe.exec(src)) !== null) {
    const names = m[1]
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => {
        const withoutType = s.replace(/^type\s+/, '').trim()
        return withoutType.split(/\s+as\s+/)[0].trim()
      })
      .filter(Boolean)
    result.push({ names, module: m[2] })
  }

  // 2. 默认导入：import X from 'mod'（不匹配已处理的 import X, { Y } 形式）
  const defaultRe = /import\s+(\w+)\s+from\s*['"]([^'"]+)['"]/g
  while ((m = defaultRe.exec(src)) !== null) {
    result.push({ names: [m[1]], module: m[2] })
  }

  return result
}

/** 提取某字符串所在行的前导空格数（用于判断路由嵌套层级）
 * 跳过注释行（// 开头），避免注释中的路径字符串干扰缩进判断
 */
function getIndentOfLineContaining(src: string, needle: string): number {
  const lines = src.split('\n')
  // 跳过注释行（trim 后以 // 开头），找到第一个非注释的匹配行
  const line = lines.find((l) => l.includes(needle) && !l.trim().startsWith('//'))
  if (!line) return -1
  const match = line.match(/^(\s*)/)
  return match ? match[1].length : 0
}

// ===== 1. CaptureStockPage.tsx 文件存在 =====
test('CaptureStockPage.tsx 文件存在', () => {
  assert.ok(existsSync(CAPTURE_PAGE_PATH), `文件不存在: ${CAPTURE_PAGE_PATH}`)
})

// ===== 2. App.tsx 包含 /capture/stock/:symbol 路由 =====
test('App.tsx 包含 /capture/stock/:symbol 路由', () => {
  const src = readSource(APP_TSX_PATH)
  assert.ok(
    src.includes("'/capture/stock/:symbol'") || src.includes('"/capture/stock/:symbol"'),
    'App.tsx 必须包含 /capture/stock/:symbol 路由',
  )
})

// ===== 3. App.tsx 导入 CaptureStockPage =====
test('App.tsx 导入 CaptureStockPage', () => {
  const src = readSource(APP_TSX_PATH)
  const imports = extractImports(src)
  const hasCaptureImport = imports.some(
    (i) => i.names.includes('CaptureStockPage') && i.module.includes('CaptureStockPage'),
  )
  assert.ok(hasCaptureImport, 'App.tsx 必须导入 CaptureStockPage')
})

// ===== 4. App.tsx 中 /capture/stock/:symbol 路由使用 <CaptureStockPage /> 作为 element =====
test('App.tsx 中 /capture/stock/:symbol 路由使用 <CaptureStockPage /> 作为 element', () => {
  const src = readSource(APP_TSX_PATH)
  const lines = src.split('\n')
  // 跳过注释行，找到实际的路由定义行（含 path: 与 element:）
  const captureLineIdx = lines.findIndex(
    (l) => l.includes('/capture/stock/:symbol') && !l.trim().startsWith('//'),
  )
  assert.ok(captureLineIdx >= 0, '找不到 /capture/stock/:symbol 路由行')
  const captureLine = lines[captureLineIdx]
  assert.ok(
    captureLine.includes('<CaptureStockPage'),
    '/capture/stock/:symbol 路由的 element 必须是 <CaptureStockPage />',
  )
})

// ===== 5. App.tsx 中 /capture/stock/:symbol 不在 ProtectedLayout 守卫下（与公开路由同级缩进）=====
test('App.tsx 中 /capture/stock/:symbol 与公开路由 /login 同级缩进（不在 ProtectedLayout 下）', () => {
  const src = readSource(APP_TSX_PATH)
  const loginIndent = getIndentOfLineContaining(src, "path: '/login'")
  const captureIndent = getIndentOfLineContaining(src, '/capture/stock/:symbol')
  assert.ok(loginIndent >= 0, '找不到 /login 路由（用于缩进基准）')
  assert.ok(captureIndent >= 0, '找不到 /capture/stock/:symbol 路由')
  assert.equal(
    captureIndent,
    loginIndent,
    `/capture/stock/:symbol 缩进（${captureIndent} 空格）必须与公开路由 /login（${loginIndent} 空格）相同，` +
      '确保不在 ProtectedLayout / SubscriberRoute 守卫下',
  )
})

// ===== 6. CaptureStockPage 使用 captureClient（不使用 apiClient）=====
test('CaptureStockPage 导入 captureClient 且不导入 apiClient', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  const imports = extractImports(src)
  const hasCaptureClient = imports.some((i) => i.names.includes('captureClient'))
  assert.ok(hasCaptureClient, 'CaptureStockPage 必须导入 captureClient（来自 @/api/client）')
  const hasApiClient = imports.some((i) => i.names.includes('apiClient'))
  assert.ok(!hasApiClient, 'CaptureStockPage 禁止导入 apiClient（必须使用 captureClient）')
})

// ===== 7. CaptureStockPage 不加载 watchlist =====
test('CaptureStockPage 不导入 useWatchlist（截图不需要自选列表）', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  const imports = extractImports(src)
  const hasWatchlist = imports.some((i) => i.names.includes('useWatchlist'))
  assert.ok(!hasWatchlist, 'CaptureStockPage 禁止导入 useWatchlist（截图不需要自选列表）')
})

// ===== 8. CaptureStockPage 不加载 events =====
test('CaptureStockPage 不导入 useInstrumentEvents（事件查询超时是 30s 截图失败根因）', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  const imports = extractImports(src)
  const hasEvents = imports.some((i) => i.names.includes('useInstrumentEvents'))
  assert.ok(!hasEvents, 'CaptureStockPage 禁止导入 useInstrumentEvents（事件查询超时是 30s 截图失败根因）')
})

// ===== 9. CaptureStockPage 不加载 memo =====
test('CaptureStockPage 不导入 useStockMemo（截图不需要备忘录）', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  const imports = extractImports(src)
  const hasMemo = imports.some((i) => i.names.includes('useStockMemo'))
  assert.ok(!hasMemo, 'CaptureStockPage 禁止导入 useStockMemo（截图不需要备忘录）')
})

// ===== 10. CaptureStockPage 不加载 batchInstruments =====
test('CaptureStockPage 不导入 useBatchInstruments（截图不需要批量查询）', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  const imports = extractImports(src)
  const hasBatch = imports.some((i) => i.names.includes('useBatchInstruments'))
  assert.ok(!hasBatch, 'CaptureStockPage 禁止导入 useBatchInstruments（截图不需要批量查询）')
})

// ===== 11. CaptureStockPage 源码不出现 eventsQuery 标识符 =====
test('CaptureStockPage 源码不出现 eventsQuery 标识符', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  assert.ok(
    !/\beventsQuery\b/.test(src),
    'CaptureStockPage 禁止出现 eventsQuery 标识符（isRenderReady 不应依赖事件查询）',
  )
})

// ===== 12. CaptureStockPage 设置 data-testid="stock-detail-capture" =====
// [PROMPT.md §5.3.1 V2] 截图根选择器移到 MobileIndicatorStage 根节点（通过 captureRoot=true）
//   CaptureStockPage 不再直接写 data-testid="stock-detail-capture"，
//   而是通过 MobileIndicatorStage 默认 captureRoot=true 间接设置。
//   测试接受两条路径之一：CaptureStockPage 源码或 MobileIndicatorStage 源码包含该 testid。
test('CaptureStockPage 设置 data-testid="stock-detail-capture"（直接或经 MobileIndicatorStage）', () => {
  const captureSrc = readSource(CAPTURE_PAGE_PATH)
  const stageSrc = readSource(MOBILE_STAGE_PATH)
  const hasTestId =
    captureSrc.includes('data-testid="stock-detail-capture"') ||
    captureSrc.includes("data-testid='stock-detail-capture'") ||
    stageSrc.includes('data-testid="stock-detail-capture"') ||
    stageSrc.includes("data-testid='stock-detail-capture'")
  assert.ok(
    hasTestId,
    'CaptureStockPage 或 MobileIndicatorStage 必须设置 data-testid="stock-detail-capture"（capture worker 通过该选择器截图）',
  )

  // 同时校验 CaptureStockPage 在正常态使用 MobileIndicatorStage 包裹图表（captureRoot 默认 true）
  assert.ok(
    /<MobileIndicatorStage[\s\S]*?renderReady=\{isRenderReady\}/.test(captureSrc),
    'CaptureStockPage 正常态必须使用 <MobileIndicatorStage renderReady={isRenderReady}> 包裹（通过 captureRoot 默认 true 设置 testid）',
  )
})

// ===== 13. CaptureStockPage 设置 data-render-ready =====
test('CaptureStockPage 设置 data-render-ready 属性', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  assert.ok(
    src.includes('data-render-ready'),
    'CaptureStockPage 必须设置 data-render-ready 属性（capture worker 等待该属性为 true）',
  )
})

// ===== 14. CaptureStockPage 的 isRenderReady 不依赖事件查询 =====
// [PROMPT.md §二 V2 + §5.3.5 V2] isRenderReady 现在通过中间变量组合：
//   const hasBaseData = !!barsResponse?.items?.length && !!indicatorsResponse
//   const isFrameMatched = renderFrame?.matched === true
//   const isTypeReady = computeTypeSpecificReady(indicatorView, indicatorsResponse)
//   const isRenderReady = hasBaseData && isFrameMatched && isTypeReady
//
// 校验策略：
//   1. isRenderReady 表达式本身不得直接引用 eventsQuery（V1 不变）
//   2. hasBaseData 行必须引用 barsResponse + indicatorsResponse（数据存在性门禁）
//   3. isFrameMatched 行必须引用 render_frame.matched（V2 新增门禁）
//   4. isTypeReady 行必须调用 computeTypeSpecificReady（V2 类型特定 Ready）
test('CaptureStockPage 的 isRenderReady 不依赖事件查询且包含 V2 frame+类型 Ready 门禁', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  // 提取 isRenderReady 赋值表达式
  const m = src.match(/const\s+isRenderReady\s*=\s*([^;\n]+)/)
  assert.ok(m, 'CaptureStockPage 必须定义 isRenderReady 变量')
  const expr = m[1]
  assert.ok(
    !/\beventsQuery\b/.test(expr),
    `isRenderReady 表达式禁止依赖 eventsQuery，实际: ${expr}`,
  )
  // V2: isRenderReady 必须包含 frame matched + 类型 Ready 门禁
  assert.ok(
    /isFrameMatched/.test(expr),
    `isRenderReady 表达式必须依赖 isFrameMatched（V2 render_frame.matched 门禁），实际: ${expr}`,
  )
  assert.ok(
    /isTypeReady/.test(expr),
    `isRenderReady 表达式必须依赖 isTypeReady（V2 类型特定 Ready），实际: ${expr}`,
  )
  assert.ok(
    /hasBaseData/.test(expr),
    `isRenderReady 表达式必须依赖 hasBaseData（bars+indicators 存在性），实际: ${expr}`,
  )

  // 校验 hasBaseData 行引用 barsResponse + indicatorsResponse
  const baseDataM = src.match(/const\s+hasBaseData\s*=\s*([^;\n]+)/)
  assert.ok(baseDataM, 'CaptureStockPage 必须定义 hasBaseData 变量')
  const baseExpr = baseDataM[1]
  assert.ok(
    /barsResponse/.test(baseExpr),
    `hasBaseData 表达式必须引用 barsResponse，实际: ${baseExpr}`,
  )
  assert.ok(
    /indicatorsResponse/.test(baseExpr),
    `hasBaseData 表达式必须引用 indicatorsResponse，实际: ${baseExpr}`,
  )

  // 校验 isFrameMatched 引用 render_frame.matched
  const frameM = src.match(/const\s+isFrameMatched\s*=\s*([^;\n]+)/)
  assert.ok(frameM, 'CaptureStockPage 必须定义 isFrameMatched 变量')
  const frameExpr = frameM[1]
  assert.ok(
    /renderFrame/.test(frameExpr) && /matched/.test(frameExpr),
    `isFrameMatched 表达式必须引用 renderFrame.matched，实际: ${frameExpr}`,
  )

  // 校验 isTypeReady 调用 computeTypeSpecificReady
  const typeM = src.match(/const\s+isTypeReady\s*=\s*([^;\n]+)/)
  assert.ok(typeM, 'CaptureStockPage 必须定义 isTypeReady 变量')
  const typeExpr = typeM[1]
  assert.ok(
    /computeTypeSpecificReady/.test(typeExpr),
    `isTypeReady 表达式必须调用 computeTypeSpecificReady，实际: ${typeExpr}`,
  )
})

// ===== 15. stock_capture_service.py URL 已更新为 /capture/stock/{symbol} 并携带 instrument_id =====
test('stock_capture_service.py URL 已更新为 /capture/stock/{symbol} 并携带 instrument_id', () => {
  const src = readSource(CAPTURE_SERVICE_PATH)
  assert.ok(
    src.includes('/capture/stock/{symbol}'),
    'stock_capture_service.py 的 URL 必须使用 /capture/stock/{symbol}（专用 Capture 路由）',
  )
  // 同时验证旧 URL 已移除（URL 构建行不应再使用 /stock/{symbol}?）
  const urlBuildLine = src
    .split('\n')
    .find((l) => l.includes('frontend_base_url.rstrip') && l.includes('symbol'))
  if (urlBuildLine) {
    assert.ok(
      urlBuildLine.includes('/capture/stock/'),
      `URL 构建行必须使用 /capture/stock/，实际: ${urlBuildLine.trim()}`,
    )
  }
  assert.ok(
    src.includes('instrument_id={instrument_id}'),
    'stock_capture_service.py URL 必须携带 instrument_id 参数',
  )
})

// ===== 16. CaptureStockPage 只调用 Snapshot API，不调用普通业务端点 =====
test('CaptureStockPage 只调用 Snapshot API，不调用普通业务端点', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  // 必须包含 Snapshot API 路径
  assert.ok(
    src.includes('/api/v1/capture/stocks/'),
    'CaptureStockPage 必须调用 /api/v1/capture/stocks/{instrument_id}/snapshot',
  )
  // 禁止调用普通端点
  const forbiddenEndpoints = [
    '/instruments/by-symbol/',
    '/api/v1/instruments/',
    '/api/v1/instruments/{instrumentId}/bars',
    '/api/v1/instruments/{instrumentId}/indicators',
    '/api/v1/instruments/{instrumentId}/quote',
  ]
  for (const endpoint of forbiddenEndpoints) {
    assert.ok(
      !src.includes(endpoint),
      `CaptureStockPage 禁止调用普通业务端点: ${endpoint}`,
    )
  }
})

// ===== 17. capture=feishu 模式无操作按钮（StockDetailPage）=====
test('test_capture_mode_no_buttons', () => {
  const src = readSource(STOCK_DETAIL_PAGE_PATH)

  // 必须包含 !isCaptureMode 条件（截图模式隐藏操作按钮）
  assert.ok(
    src.includes('!isCaptureMode'),
    'StockDetailPage 必须包含 !isCaptureMode 条件（截图模式隐藏操作按钮）',
  )

  // 验证 !isCaptureMode 包裹按钮组（actions div）
  // 模式：{!isCaptureMode && ( ... <div className="actions"> ... )}
  const actionsWrapped = /\{!isCaptureMode\s*&&\s*\([\s\S]*?<div className="actions">/.test(src)
  assert.ok(
    actionsWrapped,
    '操作按钮组（<div className="actions">）必须被 {!isCaptureMode && (...)} 包裹（截图模式隐藏全部操作按钮）',
  )
})

// ===== 18. capture=feishu 单列布局（global.scss）=====
test('test_capture_mode_single_column_layout', () => {
  const scss = readSource(GLOBAL_SCSS_PATH)

  // .tv-workspace.capture-mode .tv-side-column { display: none; }
  // 截图模式隐藏侧栏，不占位
  assert.ok(
    /\.tv-workspace\.capture-mode\s+\.tv-side-column[\s\S]*?display:\s*none/.test(scss),
    '.tv-workspace.capture-mode .tv-side-column 必须设置 display: none（截图模式隐藏侧栏，不占位）',
  )

  // .tv-workspace.capture-mode .tv-chart-column { width: 100%; }
  // 截图模式图表占满宽度
  assert.ok(
    /\.tv-workspace\.capture-mode\s+\.tv-chart-column[\s\S]*?width:\s*100%/.test(scss),
    '.tv-workspace.capture-mode .tv-chart-column 必须设置 width: 100%（截图模式图表占满宽度）',
  )
})

// ============================================================================
// [PROMPT.md §5.3.4 V2] Phase 4.4-4.6 契约测试
// chartRenderScale 模块、renderDensity prop、global.scss 字号规范、类型特定 Ready
// ============================================================================

// ===== 19. chartRenderScale.ts 模块存在并导出 V2 缩放常量 =====
test('chartRenderScale.ts 导出 RenderDensity 类型与 DESKTOP_SCALE/MOBILE_CAPTURE_SCALE 常量', () => {
  const src = readSource(CHART_RENDER_SCALE_PATH)
  // 导出 RenderDensity 类型（'desktop' | 'mobile_capture'）
  assert.ok(
    /export\s+type\s+RenderDensity\s*=\s*['"]desktop['"]\s*\|\s*['"]mobile_capture['"]/.test(src),
    'chartRenderScale.ts 必须导出 RenderDensity = "desktop" | "mobile_capture"',
  )
  // 导出 DESKTOP_SCALE / MOBILE_CAPTURE_SCALE / getRenderScale
  assert.ok(
    /export\s+const\s+DESKTOP_SCALE/.test(src),
    'chartRenderScale.ts 必须导出 DESKTOP_SCALE 常量',
  )
  assert.ok(
    /export\s+const\s+MOBILE_CAPTURE_SCALE/.test(src),
    'chartRenderScale.ts 必须导出 MOBILE_CAPTURE_SCALE 常量',
  )
  assert.ok(
    /export\s+function\s+getRenderScale/.test(src),
    'chartRenderScale.ts 必须导出 getRenderScale 函数',
  )
  // 三个规格接口：ChartTypography / ChartStrokeScale / ChartGeometryScale
  assert.ok(/export\s+interface\s+ChartTypography/.test(src), '必须导出 ChartTypography 接口')
  assert.ok(/export\s+interface\s+ChartStrokeScale/.test(src), '必须导出 ChartStrokeScale 接口')
  assert.ok(/export\s+interface\s+ChartGeometryScale/.test(src), '必须导出 ChartGeometryScale 接口')
})

// ===== 20. MOBILE_CAPTURE_SCALE 符合 PROMPT.md §5.3.4 规范表 =====
test('MOBILE_CAPTURE_SCALE 数值符合 PROMPT.md §5.3.4 规范表', () => {
  const src = readSource(CHART_RENDER_SCALE_PATH)
  // 截取 MOBILE_CAPTURE_SCALE 对象字面量
  const m = src.match(/export\s+const\s+MOBILE_CAPTURE_SCALE[\s\S]*?^}/m)
  assert.ok(m, '找不到 MOBILE_CAPTURE_SCALE 定义')
  const block = m[0]

  // 字号断言（解析 Npx 前的数字）
  const fontPx = (key: string): number | null => {
    const re = new RegExp(`${key}:\\s*\`?(\\d+)px`)
    const mm = block.match(re)
    return mm ? parseInt(mm[1], 10) : null
  }

  const axisLabel = fontPx('axisLabel')
  assert.ok(axisLabel !== null && axisLabel >= 32, `axisLabel 必须 ≥32px（实际 ${axisLabel}）`)

  const paneLabel = fontPx('paneLabel')
  assert.ok(paneLabel !== null && paneLabel >= 30, `paneLabel 必须 ≥30px（实际 ${paneLabel}）`)

  const nodeLabel = fontPx('nodeLabel')
  assert.ok(nodeLabel !== null && nodeLabel >= 34 && nodeLabel <= 40,
    `nodeLabel 必须在 34-40px（实际 ${nodeLabel}）`)

  const pocLabel = fontPx('pocLabel')
  assert.ok(pocLabel !== null && pocLabel >= 30, `pocLabel 必须 ≥30px（实际 ${pocLabel}）`)

  const smcInternalLabel = fontPx('smcInternalLabel')
  assert.ok(smcInternalLabel !== null && smcInternalLabel >= 28,
    `smcInternalLabel 必须 ≥28px（实际 ${smcInternalLabel}）`)

  const smcSwingLabel = fontPx('smcSwingLabel')
  assert.ok(smcSwingLabel !== null && smcSwingLabel >= 34,
    `smcSwingLabel 必须 ≥34px（实际 ${smcSwingLabel}）`)

  const legend = fontPx('legend')
  assert.ok(legend !== null && legend >= 30 && legend <= 34,
    `legend 必须在 30-34px（实际 ${legend}）`)

  // 线宽断言
  const strokeNum = (key: string): number | null => {
    const re = new RegExp(`${key}:\\s*([0-9.]+)`)
    const mm = block.match(re)
    return mm ? parseFloat(mm[1]) : null
  }

  const grid = strokeNum('grid')
  assert.ok(grid !== null && grid >= 1.5 && grid <= 2,
    `grid 线宽必须在 1.5-2px（实际 ${grid}）`)

  const bbLine = strokeNum('bbLine')
  assert.ok(bbLine !== null && bbLine >= 2.5 && bbLine <= 3.5,
    `bbLine 线宽必须在 2.5-3.5px（实际 ${bbLine}）`)

  const pocLine = strokeNum('pocLine')
  assert.ok(pocLine !== null && pocLine >= 3 && pocLine <= 4,
    `pocLine 线宽必须在 3-4px（实际 ${pocLine}）`)

  const candleBodyMin = strokeNum('candleBodyMin')
  assert.ok(candleBodyMin !== null && candleBodyMin >= 4,
    `candleBodyMin 必须 ≥4px（实际 ${candleBodyMin}）`)

  const smcSwing = strokeNum('smcSwing')
  assert.ok(smcSwing !== null && smcSwing >= 2.5 && smcSwing <= 3.5,
    `smcSwing 线宽必须在 2.5-3.5px（实际 ${smcSwing}）`)
})

// ===== 21. StrategyChart 声明 renderDensity prop =====
test('StrategyChart 声明 renderDensity?: RenderDensity prop', () => {
  const src = readSource(STRATEGY_CHART_PATH)
  // StrategyChartProps 接口必须包含 renderDensity?: RenderDensity
  assert.ok(
    /renderDensity\?\s*:\s*RenderDensity/.test(src),
    'StrategyChartProps 必须声明 renderDensity?: RenderDensity',
  )
  // 必须导入 RenderDensity / getRenderScale
  assert.ok(
    /import\s*\{[\s\S]*?RenderDensity[\s\S]*?\}\s*from\s*['"]\.\/chartRenderScale['"]/.test(src) ||
    /import\s+type\s*\{[\s\S]*?RenderDensity[\s\S]*?\}\s*from\s*['"]\.\/chartRenderScale['"]/.test(src),
    'StrategyChart 必须从 ./chartRenderScale 导入 RenderDensity 类型',
  )
  assert.ok(
    /getRenderScale/.test(src),
    'StrategyChart 必须调用 getRenderScale(renderDensity) 计算 scale',
  )
  // ChartState 必须包含 scale: ChartRenderScale 字段
  assert.ok(
    /scale\s*:\s*ChartRenderScale/.test(src),
    'ChartState 必须包含 scale: ChartRenderScale 字段（drawTrading 与子函数统一读取）',
  )
})

// ===== 22. CaptureStockPage 向 StrategyChart 传递 renderDensity="mobile_capture" =====
test('CaptureStockPage 向 StrategyChart 传递 renderDensity="mobile_capture"', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  // 在 <StrategyChart .../> 调用块中必须包含 renderDensity="mobile_capture"
  const m = src.match(/<StrategyChart[\s\S]*?\/>/)
  assert.ok(m, 'CaptureStockPage 必须渲染 <StrategyChart />')
  const callSite = m[0]
  assert.ok(
    /renderDensity\s*=\s*['"]mobile_capture['"]/.test(callSite),
    `<StrategyChart> 调用必须显式传递 renderDensity="mobile_capture"，实际: ${callSite}`,
  )
})

// ===== 23. StrategyChart 源码不包含硬编码字号（'8px'/'9px'/'10px'/'11px'）=====
test('StrategyChart 源码不包含硬编码字号（必须经 scale.fonts.* 读取）', () => {
  const src = readSource(STRATEGY_CHART_PATH)
  // 移除注释行（// 开头）后检查
  const codeOnly = src.split('\n')
    .filter(l => !l.trim().startsWith('//'))
    .join('\n')
  // 禁止 '8px / '9px / '10px / '11px 后跟 monospace/sans-serif（drawText 字号硬编码）
  const forbidden = /['"`](8|9|10|11)px\s+(monospace|sans-serif|ui-monospace)/
  assert.ok(
    !forbidden.test(codeOnly),
    'StrategyChart 禁止硬编码 "8px/9px/10px/11px monospace|sans-serif"，必须使用 scale.fonts.*',
  )
  // 禁止 bold 9px / bold 10px / bold 11px
  const forbiddenBold = /['"`]bold\s+(8|9|10|11)px\s+/
  assert.ok(
    !forbiddenBold.test(codeOnly),
    'StrategyChart 禁止硬编码 "bold 8/9/10/11px ..."，必须使用 scale.fonts.legendBold 等',
  )
})

// ===== 24. computeTypeSpecificReady 函数定义存在并按 indicatorView 分支 =====
test('computeTypeSpecificReady 按 indicatorView (node_cluster/bollinger/smc) 分支检查', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  // 函数定义存在
  const m = src.match(/function\s+computeTypeSpecificReady\s*\([\s\S]*?\)\s*:\s*boolean\s*\{([\s\S]*?)^}/m)
  assert.ok(m, 'CaptureStockPage 必须定义 computeTypeSpecificReady 函数')
  const body = m[1]

  // node_cluster 分支：检查 profile_rows + profile_hash + node_regions
  assert.ok(/indicatorView\s*===\s*['"]node_cluster['"]/.test(body),
    'computeTypeSpecificReady 必须有 node_cluster 分支')
  assert.ok(/profile_rows/.test(body), 'node_cluster 分支必须检查 profile_rows')
  assert.ok(/profile_hash/.test(body), 'node_cluster 分支必须检查 profile_hash')
  assert.ok(/node_regions/.test(body), 'node_cluster 分支必须检查 node_regions')

  // bollinger 分支：检查 upper/middle/lower 三轨
  assert.ok(/indicatorView\s*===\s*['"]bollinger['"]/.test(body),
    'computeTypeSpecificReady 必须有 bollinger 分支')
  assert.ok(/upper/.test(body) && /middle/.test(body) && /lower/.test(body),
    'bollinger 分支必须检查 upper/middle/lower 三轨')

  // smc 分支：检查 algorithm_version + DTO
  assert.ok(/indicatorView\s*===\s*['"]smc['"]/.test(body),
    'computeTypeSpecificReady 必须有 smc 分支')
  assert.ok(/algorithm_version/.test(body), 'smc 分支必须检查 algorithm_version')
})

// ===== 25. global.scss mobile-stage 字号符合 PROMPT.md §5.3.4 规范 =====
test('global.scss mobile-stage 元素字号符合 PROMPT.md §5.3.4 规范表', () => {
  const scss = readSource(GLOBAL_SCSS_PATH)

  // 辅助：从 .selector { font-size: Npx; } 提取数值
  const fontPx = (selector: string, child?: string): number | null => {
    const re = child
      ? new RegExp(`\\.mobile-stage-${selector}\\s+\\{[\\s\\S]*?${child}\\s+\\{[\\s\\S]*?font-size:\\s*(\\d+)px`)
      : new RegExp(`\\.mobile-stage-${selector}\\s+\\{[\\s\\S]*?font-size:\\s*(\\d+)px`)
    const m = scss.match(re)
    return m ? parseInt(m[1], 10) : null
  }

  // 股票名称 72-80px
  const stockName = fontPx('stock-identity', 'strong')
  assert.ok(stockName !== null && stockName >= 72 && stockName <= 80,
    `股票名称字号必须在 72-80px（实际 ${stockName}）`)

  // 股票代码 34-38px
  const stockCode = fontPx('stock-identity', 'span')
  assert.ok(stockCode !== null && stockCode >= 34 && stockCode <= 38,
    `股票代码字号必须在 34-38px（实际 ${stockCode}）`)

  // 品牌名 44-48px
  const brandName = fontPx('brand-text', 'strong')
  assert.ok(brandName !== null && brandName >= 44 && brandName <= 48,
    `品牌名字号必须在 44-48px（实际 ${brandName}）`)

  // 品牌副标题 24-28px
  const brandSub = fontPx('brand-text', 'span')
  assert.ok(brandSub !== null && brandSub >= 24 && brandSub <= 28,
    `品牌副标题字号必须在 24-28px（实际 ${brandSub}）`)

  // 涨跌幅数值 72-84px
  const returnVal = fontPx('return-summary', 'b')
  assert.ok(returnVal !== null && returnVal >= 72 && returnVal <= 84,
    `涨跌幅数值字号必须在 72-84px（实际 ${returnVal}）`)

  // 当前价 44-56px
  const priceVal = fontPx('price-summary', 'b')
  assert.ok(priceVal !== null && priceVal >= 44 && priceVal <= 56,
    `当前价字号必须在 44-56px（实际 ${priceVal}）`)

  // 图表标题 32-36px
  const chartTitle = fontPx('chart-title')
  assert.ok(chartTitle !== null && chartTitle >= 32 && chartTitle <= 36,
    `图表标题字号必须在 32-36px（实际 ${chartTitle}）`)

  // 行情截止时间 30-34px (mobile-stage-chart-head time)
  const chartTime = fontPx('chart-head', 'time')
  assert.ok(chartTime !== null && chartTime >= 30 && chartTime <= 34,
    `行情截止时间字号必须在 30-34px（实际 ${chartTime}）`)

  // 发送时间 30-34px
  const sendTime = fontPx('send-time')
  assert.ok(sendTime !== null && sendTime >= 30 && sendTime <= 34,
    `发送时间字号必须在 30-34px（实际 ${sendTime}）`)

  // 风险提示 30-32px + 透明度 ≥ 0.72
  //   透明度可由 `opacity:` 属性 或 `rgba(..., α)` 颜色 alpha 通道实现（CSS 等价可读性）
  const riskM = scss.match(/\.mobile-stage-risk-notice\s*\{[\s\S]*?font-size:\s*(\d+)px[\s\S]*?\}/)
  assert.ok(riskM, 'mobile-stage-risk-notice 必须设置 font-size')
  const riskPx = parseInt(riskM[1], 10)
  assert.ok(riskPx >= 30 && riskPx <= 32,
    `风险提示字号必须在 30-32px（实际 ${riskPx}）`)
  // 提取 opacity 属性 或 rgba alpha（取较大者作为有效透明度）
  const riskBlock = riskM[0]
  const opacityPropM = riskBlock.match(/opacity:\s*([0-9.]+)/)
  const rgbaAlphaM = riskBlock.match(/rgba?\([^)]*,\s*([0-9.]+)\s*\)/)
  const effectiveOpacity = Math.max(
    opacityPropM ? parseFloat(opacityPropM[1]) : 0,
    rgbaAlphaM ? parseFloat(rgbaAlphaM[1]) : 0,
  )
  assert.ok(effectiveOpacity >= 0.72,
    `风险提示透明度（opacity 属性或 rgba alpha）必须 ≥0.72（实际 ${effectiveOpacity}）`)

  // 模块/指标标签 30-34px
  const moduleLabel = fontPx('module-label')
  assert.ok(moduleLabel !== null && moduleLabel >= 30 && moduleLabel <= 34,
    `模块标签字号必须在 30-34px（实际 ${moduleLabel}）`)
})
