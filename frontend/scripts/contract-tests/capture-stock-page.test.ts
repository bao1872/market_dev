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

/** 提取某字符串所在行的前导空格数（用于判断路由嵌套层级） */
function getIndentOfLineContaining(src: string, needle: string): number {
  const lines = src.split('\n')
  const line = lines.find((l) => l.includes(needle))
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
test('CaptureStockPage 设置 data-testid="stock-detail-capture"', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  assert.ok(
    src.includes('data-testid="stock-detail-capture"') ||
      src.includes("data-testid='stock-detail-capture'"),
    'CaptureStockPage 必须设置 data-testid="stock-detail-capture"（capture worker 通过该选择器截图）',
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
test('CaptureStockPage 的 isRenderReady 表达式只依赖 bars + indicators', () => {
  const src = readSource(CAPTURE_PAGE_PATH)
  // 提取 isRenderReady 赋值表达式
  const m = src.match(/const\s+isRenderReady\s*=\s*([^;\n]+)/)
  assert.ok(m, 'CaptureStockPage 必须定义 isRenderReady 变量')
  const expr = m[1]
  assert.ok(
    !/\beventsQuery\b/.test(expr),
    `isRenderReady 表达式禁止依赖 eventsQuery，实际: ${expr}`,
  )
  assert.ok(
    /barsResponse/.test(expr),
    `isRenderReady 表达式必须依赖 barsResponse，实际: ${expr}`,
  )
  assert.ok(
    /indicatorsResponse/.test(expr),
    `isRenderReady 表达式必须依赖 indicatorsResponse，实际: ${expr}`,
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
