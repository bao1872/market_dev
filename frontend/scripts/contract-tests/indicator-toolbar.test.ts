// [IndicatorToolbar] - 描述: 图表图层工具栏与图表集成契约测试（PRD §6.2 — 单一真源 v2）
// 用法：node --experimental-strip-types --test scripts/contract-tests/indicator-toolbar.test.ts
//
// 覆盖：
//  1. IndicatorToolbar 渲染 7 个开关（主图 4 + 副图 3）
//  2. IndicatorToolbar 调用 onToggle 回调
//  3. StrategyChart 接受 layerVisibility prop（受控组件）
//  4. StrategyChart 不再内部管理 layers state（无 useState<LayerVisibility>）
//  5. StrategyChart 不再读写 detail-chart-strategy-groups-v3
//  6. StockResearchWorkspace 管理 ChartLayerVisibility 状态
//  7. StockResearchWorkspace 渲染 IndicatorToolbar
//  8. StockResearchWorkspace 传递 layerVisibility 给图表
//  9. 右栏收起触发图表 resize（ResizeObserver + useEffect）
// 10. StockDetailPage 传递 rightPanelCollapsed
// 11. StockDetailPage 渲染左栏来源列表
// 12. tv-strategy-legend 为只读（无 onClick）
// 13. 新 localStorage key 为 panji:chart-layer-visibility:v2

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
const PREFS_PATH = 'src/features/stock-research/indicatorPreferences.ts'
const TYPES_PATH = 'src/features/stock-research/stockResearchTypes.ts'

// ===== 1. IndicatorToolbar 渲染 7 个开关 =====
test('IndicatorToolbar 从 CHART_LAYER_MANIFEST 渲染开关', () => {
  const src = readSrc(TOOLBAR_PATH)
  assert.ok(src.includes('CHART_LAYER_MANIFEST'), 'should import manifest')
  assert.ok(src.includes('chartLayersForSource'), 'should filter by source')
  assert.ok(src.includes("kind === 'main'"), 'should filter main layers')
  assert.ok(src.includes("kind === 'sub'"), 'should filter sub layers')
  assert.ok(src.includes('mainLayers.map(renderToggle)'), 'should render main layers dynamically')
  assert.ok(src.includes('subLayers.map(renderToggle)'), 'should render sub layers dynamically')
})

// ===== 2. IndicatorToolbar 调用 onToggle 回调 =====
test('IndicatorToolbar 调用 onToggle 回调', () => {
  const src = readSrc(TOOLBAR_PATH)
  assert.ok(src.includes('onToggle'), 'should have onToggle prop')
  assert.ok(src.includes('onToggle(entry.id'), 'should call onToggle with id and visible')
})

// ===== 3. StrategyChart 接受 layerVisibility prop =====
test('StrategyChart 接受 layerVisibility prop（受控组件）', () => {
  const src = readSrc(CHART_PATH)
  assert.ok(src.includes('layerVisibility?: ChartLayerVisibility'), 'should have layerVisibility prop')
  assert.ok(src.includes('import type { ChartLayerVisibility }'), 'should import ChartLayerVisibility type')
})

// ===== 4. StrategyChart 不再内部管理 layers state =====
test('StrategyChart 不再内部管理 layers state（无 useState<LayerVisibility>）', () => {
  const src = readSrc(CHART_PATH)
  assert.ok(!src.includes('useState<LayerVisibility>'), 'should not have internal layers state')
  assert.ok(!src.includes('setLayers'), 'should not have setLayers')
  assert.ok(src.includes('chartLayerVisibilityToInternal'), 'should have mapping function')
})

// ===== 5. StrategyChart 不再读写 detail-chart-strategy-groups-v3 =====
test('StrategyChart 不再读写 detail-chart-strategy-groups-v3', () => {
  const src = readSrc(CHART_PATH)
  assert.ok(!src.includes('detail-chart-strategy-groups-v3'), 'should not reference old localStorage key')
  assert.ok(!src.includes("localStorage.setItem(storageKey"), 'should not persist layers to localStorage')
})

// ===== 6. StockResearchWorkspace 管理 ChartLayerVisibility 状态 =====
test('StockResearchWorkspace 管理 ChartLayerVisibility 状态', () => {
  const src = readSrc(WORKSPACE_PATH)
  assert.ok(src.includes('loadChartLayerVisibility'), 'should load from localStorage')
  assert.ok(src.includes('saveChartLayerVisibility'), 'should save to localStorage')
  assert.ok(src.includes('useState<ChartLayerVisibility>'), 'should have ChartLayerVisibility state')
  assert.ok(src.includes('handleLayerToggle'), 'should have toggle handler')
})

// ===== 7. StockResearchWorkspace 渲染 IndicatorToolbar =====
test('StockResearchWorkspace 渲染 IndicatorToolbar（非截图模式）', () => {
  const src = readSrc(WORKSPACE_PATH)
  assert.ok(src.includes('IndicatorToolbar'), 'should render IndicatorToolbar')
  assert.ok(src.includes('!isCaptureMode'), 'should hide in capture mode')
})

// ===== 8. StockResearchWorkspace 传递 layerVisibility 给图表 =====
test('StockResearchWorkspace 传递 layerVisibility 给图表', () => {
  const src = readSrc(WORKSPACE_PATH)
  assert.ok(src.includes('layerVisibility={isCaptureMode ? undefined : layerVisibility}'), 'should pass to chart')
})

// ===== 9. 右栏收起触发图表 resize =====
test('右栏收起触发图表 resize', () => {
  const chartSrc = readSrc(CHART_PATH)
  assert.ok(chartSrc.includes('ResizeObserver'), 'chart should have ResizeObserver')
  assert.ok(chartSrc.includes('ro.observe(wrap)'), 'should observe wrap element')
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
test('StockDetailPage 渲染左栏来源列表', () => {
  const src = readSrc(DETAIL_PATH)
  assert.ok(src.includes('tv-source-list') || src.includes('detail-source-list'), 'should render source list')
  assert.ok(src.includes('sourceStocks'), 'should use sourceStocks (returnTo context-aware source list)')
  assert.ok(src.includes('tv-detail-layout'), 'should have detail layout wrapper')
})

// ===== 12. tv-strategy-legend 为只读（无 onClick） =====
test('tv-strategy-legend 为只读（无 onClick，无 tv-mini-switch）', () => {
  const src = readSrc(CHART_PATH)
  assert.ok(src.includes('tv-strategy-legend'), 'should have legend element')
  // 提取 legend 渲染区块（从 tv-strategy-legend 到下一个闭合 div）
  const legendStart = src.indexOf('{/* 策略图示区')
  assert.ok(legendStart > 0, 'should find legend comment')
  const legendEnd = src.indexOf('</div>', legendStart)
  assert.ok(legendEnd > legendStart, 'should find legend end')
  const legendSection = src.substring(legendStart, legendEnd)
  assert.ok(!legendSection.includes('onClick'), 'legend should not have onClick')
  assert.ok(!legendSection.includes('tv-mini-switch'), 'legend should not have toggle switch')
  assert.ok(legendSection.includes('只读'), 'legend should have read-only comment')
})

// ===== 13. 新 localStorage key =====
test('indicatorPreferences 使用新 localStorage key panji:chart-layer-visibility:v', () => {
  const src = readSrc(PREFS_PATH)
  // 使用模板常量 panji:chart-layer-visibility:v${PREF_VERSION}
  assert.ok(src.includes('panji:chart-layer-visibility:v'), 'should use new key prefix')
  assert.ok(src.includes('PREF_VERSION = 2'), 'should use version 2')
  assert.ok(src.includes('loadChartLayerVisibility'), 'should export load function')
  assert.ok(src.includes('saveChartLayerVisibility'), 'should export save function')
  // 旧 key 迁移引用
  assert.ok(src.includes('detail-chart-strategy-groups-v3'), 'should reference old chart key for migration')
  assert.ok(src.includes('panji:indicator-visibility:v1'), 'should reference old toolbar key for migration')
})

// ===== 14. ChartLayerVisibility 类型定义 =====
test('stockResearchTypes 导出 ChartLayerVisibility 类型和 manifest', () => {
  const src = readSrc(TYPES_PATH)
  assert.ok(src.includes('export type ChartLayerKey'), 'should export ChartLayerKey')
  assert.ok(src.includes('export type ChartLayerVisibility'), 'should export ChartLayerVisibility')
  assert.ok(src.includes('export const CHART_LAYER_MANIFEST'), 'should export CHART_LAYER_MANIFEST')
  assert.ok(src.includes('export function defaultChartLayerVisibility'), 'should export defaultChartLayerVisibility')
  assert.ok(src.includes('export function chartLayersForSource'), 'should export chartLayersForSource')
  // 不应再导出旧类型
  assert.ok(!src.includes('export type IndicatorVisibility'), 'should not export old IndicatorVisibility')
  assert.ok(!src.includes('export const INDICATOR_LAYER_MANIFEST'), 'should not export old INDICATOR_LAYER_MANIFEST')
})
