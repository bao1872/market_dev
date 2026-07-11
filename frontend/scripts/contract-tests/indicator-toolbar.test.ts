// [IndicatorToolbar] - 描述: 指标显示工具栏与图表集成契约测试（PRD §6.2）
// 用法：node --experimental-strip-types --test scripts/contract-tests/indicator-toolbar.test.ts
//
// 覆盖：
//  1. IndicatorToolbar 渲染 5 个开关（主图 3 + 副图 2）
//  2. IndicatorToolbar 调用 onToggle 回调
//  3. StrategyChart 接受 indicatorVisibility prop
//  4. StrategyChart 计算 effectiveLayers 覆盖 layers
//  5. 截图模式跳过 indicatorVisibility 覆盖
//  6. StockResearchWorkspace 管理 indicatorVisibility 状态
//  7. StockResearchWorkspace 渲染 IndicatorToolbar
//  8. StockResearchWorkspace 传递 indicatorVisibility 给图表
//  9. 右栏收起触发图表 resize（ResizeObserver + useEffect）
// 10. StockDetailPage 传递 rightPanelCollapsed
// 11. StockDetailPage 渲染左栏来源列表

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const FRONTEND_ROOT = join(__dirname, '..', '..')

function readSrc(relPath: string): string {
  return readFileSync(join(FRONTEND_ROOT, relPath), 'utf-8')
}

const TOOLBAR_PATH = 'src/features/stock-research/IndicatorToolbar.tsx'
const CHART_PATH = 'src/components/StrategyChart.tsx'
const WORKSPACE_PATH = 'src/features/stock-research/StockResearchWorkspace.tsx'
const DETAIL_PATH = 'src/pages/StockDetailPage.tsx'

// ===== 1. IndicatorToolbar 渲染 5 个开关 =====
test('IndicatorToolbar 渲染 5 个开关（主图 3 + 副图 2）', () => {
  const src = readSrc(TOOLBAR_PATH)
  assert.ok(src.includes('INDICATOR_LAYER_MANIFEST'), 'should import manifest')
  assert.ok(src.includes("kind === 'main'"), 'should filter main layers')
  assert.ok(src.includes("kind === 'sub'"), 'should filter sub layers')
  // 工具栏从 manifest 动态渲染开关（不硬编码指标名称）
  assert.ok(src.includes('mainLayers.map(renderToggle)'), 'should render main layers dynamically')
  assert.ok(src.includes('subLayers.map(renderToggle)'), 'should render sub layers dynamically')
})

// ===== 2. IndicatorToolbar 调用 onToggle 回调 =====
test('IndicatorToolbar 调用 onToggle 回调', () => {
  const src = readSrc(TOOLBAR_PATH)
  assert.ok(src.includes('onToggle'), 'should have onToggle prop')
  assert.ok(src.includes('handleIndicatorToggle') || src.includes('onToggle(entry.id'), 'should call onToggle with id and visible')
})

// ===== 3. StrategyChart 接受 indicatorVisibility prop =====
test('StrategyChart 接受 indicatorVisibility prop', () => {
  const src = readSrc(CHART_PATH)
  assert.ok(src.includes('indicatorVisibility?: IndicatorVisibility'), 'should have indicatorVisibility prop')
  assert.ok(src.includes("import type { IndicatorVisibility }"), 'should import IndicatorVisibility type')
})

// ===== 4. StrategyChart 计算 effectiveLayers =====
test('StrategyChart 计算 effectiveLayers 覆盖 layers', () => {
  const src = readSrc(CHART_PATH)
  assert.ok(src.includes('effectiveLayers'), 'should compute effectiveLayers')
  // 映射关系
  assert.ok(src.includes('consensus_zone'), 'should map consensus_zone')
  assert.ok(src.includes('price_structure'), 'should map price_structure')
  assert.ok(src.includes('indicatorVisibility.boll'), 'should map boll')
  assert.ok(src.includes('indicatorVisibility.volume'), 'should map volume')
  assert.ok(src.includes('indicatorVisibility.macd'), 'should map macd')
  // 在 dataRef 和 redraw effect 中使用
  assert.ok(src.includes('layers: effectiveLayers'), 'should use effectiveLayers in dataRef')
  assert.ok(src.includes('effectiveLayers, viewport'), 'should use effectiveLayers in redraw effect deps')
})

// ===== 5. 截图模式跳过 indicatorVisibility 覆盖 =====
test('截图模式跳过 indicatorVisibility 覆盖', () => {
  const src = readSrc(CHART_PATH)
  assert.ok(src.includes('isCaptureMode || !indicatorVisibility'), 'should guard with isCaptureMode')
})

// ===== 6. StockResearchWorkspace 管理 indicatorVisibility 状态 =====
test('StockResearchWorkspace 管理 indicatorVisibility 状态', () => {
  const src = readSrc(WORKSPACE_PATH)
  assert.ok(src.includes('loadIndicatorVisibility'), 'should load from localStorage')
  assert.ok(src.includes('saveIndicatorVisibility'), 'should save to localStorage')
  assert.ok(src.includes('useState<IndicatorVisibility>'), 'should have indicatorVisibility state')
  assert.ok(src.includes('handleIndicatorToggle'), 'should have toggle handler')
})

// ===== 7. StockResearchWorkspace 渲染 IndicatorToolbar =====
test('StockResearchWorkspace 渲染 IndicatorToolbar（非截图模式）', () => {
  const src = readSrc(WORKSPACE_PATH)
  assert.ok(src.includes('IndicatorToolbar'), 'should render IndicatorToolbar')
  assert.ok(src.includes('!isCaptureMode'), 'should hide in capture mode')
})

// ===== 8. StockResearchWorkspace 传递 indicatorVisibility 给图表 =====
test('StockResearchWorkspace 传递 indicatorVisibility 给图表', () => {
  const src = readSrc(WORKSPACE_PATH)
  assert.ok(src.includes('indicatorVisibility={isCaptureMode ? undefined : indicatorVisibility}'), 'should pass to chart')
})

// ===== 9. 右栏收起触发图表 resize =====
test('右栏收起触发图表 resize', () => {
  const chartSrc = readSrc(CHART_PATH)
  // ResizeObserver 存在
  assert.ok(chartSrc.includes('ResizeObserver'), 'chart should have ResizeObserver')
  assert.ok(chartSrc.includes('ro.observe(wrap)'), 'should observe wrap element')
  // StockResearchWorkspace 在 rightPanelCollapsed 变化时触发 resize
  const wsSrc = readSrc(WORKSPACE_PATH)
  assert.ok(wsSrc.includes('rightPanelCollapsed'), 'should depend on rightPanelCollapsed')
  assert.ok(wsSrc.includes('dispatchEvent') || wsSrc.includes('requestAnimationFrame'), 'should trigger resize on panel change')
})

// ===== 10. StockDetailPage 传递 rightPanelCollapsed =====
test('StockDetailPage 传递 rightPanelCollapsed', () => {
  const src = readSrc(DETAIL_PATH)
  assert.ok(src.includes('rightPanelCollapsed'), 'should pass rightPanelCollapsed')
  assert.ok(src.includes('shouldShowPanel'), 'should compute from shouldShowPanel')
})

// ===== 11. StockDetailPage 渲染左栏来源列表 =====
// Phase 3 纠偏后，左栏优先使用 sourceStocks（returnTo 上下文恢复），
// 回退到 watchlistStocks（自选列表）。两者由 useStockDetailActions 统一提供。
test('StockDetailPage 渲染左栏来源列表', () => {
  const src = readSrc(DETAIL_PATH)
  assert.ok(src.includes('tv-source-list') || src.includes('detail-source-list'), 'should render source list')
  assert.ok(src.includes('sourceStocks'), 'should use sourceStocks (returnTo context-aware source list)')
  assert.ok(src.includes('tv-detail-layout'), 'should have detail layout wrapper')
})
