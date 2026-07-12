// [事件状态面板开关] - 描述: StockDetailPage 事件状态面板开关契约测试
// 用法：node --experimental-strip-types --test scripts/contract-tests/structural-state-toggle.test.ts
// 覆盖：
// 1. 面板首次默认收起（P0-4: eventPanelCollapsed 默认 true；localStorage 持久化用户选择）
// 2. 开关按钮默认渲染（非截图模式 + symbol 存在时）
// 3. localStorage 持久化用户选择（panji:event-panel:v1）
// 4. hideStructuralState=1 强制隐藏按钮和面板
// 5. capture=1 强制隐藏按钮和面板
// 6. capture=feishu 强制隐藏（保留现有 isCaptureMode 逻辑）
// 7. 强制隐藏时禁用 toggle 按钮（early return）
// 8. toggle 按钮在 tv-chart-column 内部（定位上下文）
// 9. 按钮文案动态切换（显示事件状态 / 隐藏事件状态）
// 10. shouldShowPanel=true 时渲染 EventStatePanel
// 11. shouldShowPanel=true 时渲染 EventStatePanel
// 12. Temporal Features 卡片在 StockStructuralStatePanel 内（useTemporalFeatures 引用）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const FRONTEND_ROOT = join(__dirname, '..', '..')
const PAGE_PATH = join(FRONTEND_ROOT, 'src', 'pages', 'StockDetailPage.tsx')
const PANEL_PATH = join(FRONTEND_ROOT, 'src', 'components', 'StockStructuralStatePanel.tsx')

function readSource(): string {
  return readFileSync(PAGE_PATH, 'utf-8')
}

function readPanelSource(): string {
  return readFileSync(PANEL_PATH, 'utf-8')
}

// ===== 1. 面板首次默认收起（P0-4） =====
test('Panel is collapsed by default for first-time users (eventPanelCollapsed defaults to true)', () => {
  const src = readSource()

  // 必须读取 localStorage panji:event-panel:v1
  assert.ok(
    /localStorage\.getItem\(\s*['"]panji:event-panel:v1['"]\s*\)/.test(src),
    'StockDetailPage 必须读取 localStorage "panji:event-panel:v1"',
  )
  // P0-4: 首次默认收起 — saved===null 时返回 true（收起）；有 saved 值时按 saved==='collapsed' 判断
  assert.ok(
    /saved\s*===\s*null\s*\?\s*true/.test(src),
    'eventPanelCollapsed 首次（saved===null）默认 true（收起）',
  )
  assert.ok(
    /saved\s*===\s*['"]collapsed['"]/.test(src),
    'eventPanelCollapsed 有 localStorage 值时按 saved==="collapsed" 判断',
  )
})

// ===== 2. 开关按钮默认渲染 =====
test('Toggle button is rendered by default (non-capture mode + symbol)', () => {
  const src = readSource()

  // 必须含 toggle 按钮 className（V1: structural-state-toggle-btn）
  assert.ok(
    /structural-state-toggle-btn/.test(src),
    'StockDetailPage 必须含 "structural-state-toggle-btn" className 的开关按钮',
  )
  // 必须含「显示事件状态」/「隐藏事件状态」文案
  assert.ok(
    /显示事件状态/.test(src) && /隐藏事件状态/.test(src),
    'StockDetailPage 开关按钮必须含「显示事件状态」/「隐藏事件状态」文案',
  )
  // 必须在 !hideStructuralStateParam && symbol 时渲染按钮
  assert.ok(
    /!hideStructuralStateParam\s*&&\s*symbol/.test(src),
    'StockDetailPage 必须在 !hideStructuralStateParam && symbol 时渲染 toggle 按钮',
  )
})

// ===== 3. localStorage 持久化用户选择 =====
test('localStorage persists user choice via setItem', () => {
  const src = readSource()

  // 必须 setItem 写入 localStorage panji:event-panel:v1
  assert.ok(
    /localStorage\.setItem\(\s*['"]panji:event-panel:v1['"]/.test(src),
    'StockDetailPage 必须通过 localStorage.setItem 持久化 "panji:event-panel:v1" 选择',
  )
})

// ===== 4. hideStructuralState=1 强制隐藏 =====
test('hideStructuralState=1 URL param forces hide', () => {
  const src = readSource()

  // 必须检测 hideStructuralState 参数
  assert.ok(
    /searchParams\.get\(\s*['"]hideStructuralState['"]\s*\)/.test(src),
    'StockDetailPage 必须检测 URL 参数 "hideStructuralState"',
  )
  // 必须与 '1' 比较
  assert.ok(
    /searchParams\.get\(\s*['"]hideStructuralState['"]\s*\)\s*===?\s*['"]1['"]/.test(src),
    'hideStructuralState 必须与 "1" 严格比较触发强制隐藏',
  )
})

// ===== 5. capture=1 强制隐藏 =====
test('capture=1 URL param forces hide', () => {
  const src = readSource()

  // 必须检测 capture 参数与 '1' 比较（与 feishu 截图模式区分）
  assert.ok(
    /searchParams\.get\(\s*['"]capture['"]\s*\)\s*===?\s*['"]1['"]/.test(src),
    'StockDetailPage 必须检测 URL 参数 "capture" 与 "1" 严格比较触发强制隐藏',
  )
})

// ===== 6. capture=feishu 强制隐藏（保留现有 isCaptureMode 逻辑）=====
test('capture=feishu forces hide (preserves existing isCaptureMode)', () => {
  const src = readSource()

  // 现有 isCaptureMode 逻辑必须保持
  assert.ok(
    /isCaptureMode\s*=\s*searchParams\.get\(\s*['"]capture['"]\s*\)\s*===?\s*['"]feishu['"]/.test(src),
    'StockDetailPage 必须保留 isCaptureMode 逻辑（capture=feishu）',
  )
  // hideStructuralStateParam 必须包含 isCaptureMode 引用
  assert.ok(
    /hideStructuralStateParam\s*=\s*[\s\S]*isCaptureMode/.test(src),
    'hideStructuralStateParam 必须引用 isCaptureMode（capture=feishu 也触发强制隐藏）',
  )
})

// ===== 7. 强制隐藏时禁用 toggle =====
test('Force-hide disables toggle (early return in toggle callback)', () => {
  const src = readSource()

  // toggle 回调必须在 hideStructuralStateParam 为 true 时 early return
  const toggleBlockMatch = src.match(/toggleEventPanel\s*=\s*useCallback\(\s*\(\)\s*=>\s*\{([\s\S]*?)\},\s*\[hideStructuralStateParam\]\s*\)/)
  assert.ok(toggleBlockMatch, 'StockDetailPage 必须含 toggleEventPanel useCallback 且依赖 hideStructuralStateParam')

  const toggleBody = toggleBlockMatch![1]
  assert.ok(
    /if\s*\(\s*hideStructuralStateParam\s*\)\s*return/.test(toggleBody),
    'toggleEventPanel 回调必须在 hideStructuralStateParam=true 时 early return（强制隐藏时禁用 toggle）',
  )
})

// ===== 8. toggle 按钮在 tv-chart-column 内部（定位上下文） =====
// Phase 3 重构后，toggle 按钮作为 toolbar prop 传递给 StockResearchWorkspace，
// StockResearchWorkspace 在 <section className="tv-chart-column"> 内部渲染 {toolbar}。
// 本测试验证：
//   1. StockDetailPage 定义含 structural-state-toggle-btn 的 toolbar 并传给 StockResearchWorkspace
//   2. StockResearchWorkspace 在 tv-chart-column section 内部渲染 {toolbar}
//   3. .tv-chart-column 有 position: relative（在 global.scss 中）
test('Toggle button is inside tv-chart-column for stable absolute positioning', () => {
  const src = readSource()

  // StockDetailPage 必须定义含 structural-state-toggle-btn 的 toolbar 变量
  const toggleIdx = src.indexOf('structural-state-toggle-btn')
  assert.ok(toggleIdx > 0, 'StockDetailPage 必须含 structural-state-toggle-btn')

  // toolbar 变量必须作为 toolbar prop 传给 StockResearchWorkspace
  assert.ok(
    /toolbar=\{structuralToolbar\}/.test(src),
    'StockDetailPage 必须将 structuralToolbar 作为 toolbar prop 传给 StockResearchWorkspace',
  )

  // StockResearchWorkspace 必须在 tv-chart-column section 内部渲染 {toolbar}
  const workspacePath = join(FRONTEND_ROOT, 'src', 'features', 'stock-research', 'StockResearchWorkspace.tsx')
  const workspaceSrc = readFileSync(workspacePath, 'utf-8')
  const wsToggleIdx = workspaceSrc.indexOf('{toolbar}')
  assert.ok(wsToggleIdx > 0, 'StockResearchWorkspace 必须渲染 {toolbar}')

  const wsSectionOpenIdx = workspaceSrc.lastIndexOf('<section', wsToggleIdx)
  assert.ok(wsSectionOpenIdx > 0, '{toolbar} 必须在 <section> 之后')

  const wsSectionCloseIdx = workspaceSrc.indexOf('</section>', wsToggleIdx)
  assert.ok(wsSectionCloseIdx > wsToggleIdx, '{toolbar} 必须在 </section> 之前')

  // 确认该 section 含 tv-chart-column className
  const sectionText = workspaceSrc.slice(wsSectionOpenIdx, wsToggleIdx)
  assert.ok(
    /className="tv-chart-column"/.test(sectionText),
    '{toolbar} 所在的 section 必须含 className="tv-chart-column"',
  )

  // 同时确认 tv-chart-column 有 position: relative（在 global.scss 中）
  const scssPath = join(FRONTEND_ROOT, 'src', 'styles', 'global.scss')
  const scss = readFileSync(scssPath, 'utf-8')
  const chartColRuleMatch = scss.match(/\.tv-chart-column\s*\{[^}]*position:\s*relative[^}]*\}/)
  assert.ok(
    chartColRuleMatch,
    '.tv-chart-column 必须含 position: relative（作为 toggle 按钮的定位上下文）',
  )
})

// ===== 9. 按钮文案动态切换（显示/隐藏事件状态） =====
test('Toggle button label switches between "显示事件状态" and "隐藏事件状态"', () => {
  const src = readSource()

  // 必须含三元表达式：eventPanelCollapsed ? '显示事件状态' : '隐藏事件状态'
  assert.ok(
    /eventPanelCollapsed\s*\?\s*['"]显示事件状态['"]\s*:\s*['"]隐藏事件状态['"]/.test(src),
    'StockDetailPage toggle 按钮文案必须根据 eventPanelCollapsed 动态切换（收起时显示"显示事件状态"，展开时显示"隐藏事件状态"）',
  )
})

// ===== 10. shouldShowPanel 控制渲染（P0-4: 默认收起，用户展开后渲染）=====
test('EventStatePanel is rendered when shouldShowPanel=!eventPanelCollapsed && !hideStructuralStateParam', () => {
  const src = readSource()

  // shouldShowPanel 必须基于 !eventPanelCollapsed && !hideStructuralStateParam
  assert.ok(
    /shouldShowPanel\s*=\s*!eventPanelCollapsed\s*&&\s*!hideStructuralStateParam/.test(src),
    'StockDetailPage 必须有 shouldShowPanel = !eventPanelCollapsed && !hideStructuralStateParam',
  )
  // EventStatePanel 必须在 shouldShowPanel && symbol 时渲染
  assert.ok(
    /shouldShowPanel\s*&&\s*symbol/.test(src),
    'EventStatePanel 必须在 shouldShowPanel && symbol 时渲染',
  )
})

// ===== 11. shouldShowPanel=true 时渲染 EventStatePanel =====
test('EventStatePanel is rendered when shouldShowPanel=true', () => {
  const src = readSource()

  // 必须导入 EventStatePanel
  assert.ok(
    /import\s*\{[^}]*EventStatePanel[^}]*\}\s*from\s*['"]@\/features\/research-context\/EventStatePanel['"]/.test(src),
    'StockDetailPage 必须导入 EventStatePanel',
  )
  // 必须渲染 <EventStatePanel symbol={symbol} />
  assert.ok(
    /<EventStatePanel\s+symbol=\{symbol\}\s*\/>/.test(src),
    'StockDetailPage 必须渲染 <EventStatePanel symbol={symbol} />',
  )
})

// ===== 12. Temporal Features 卡片在 StockStructuralStatePanel 内 =====
test('TemporalFeaturesCard is rendered inside StockStructuralStatePanel (uses useTemporalFeatures)', () => {
  const panelSrc = readPanelSource()

  // StockStructuralStatePanel 必须导入 useTemporalFeatures
  assert.ok(
    /useTemporalFeatures/.test(panelSrc),
    'StockStructuralStatePanel 必须导入并使用 useTemporalFeatures hook',
  )
  // 必须渲染 <TemporalFeaturesCard instrumentId={instrumentId} />
  assert.ok(
    /<TemporalFeaturesCard\s+instrumentId=\{instrumentId\}\s*\/>/.test(panelSrc),
    'StockStructuralStatePanel 必须渲染 <TemporalFeaturesCard instrumentId={instrumentId} />',
  )
  // 必须含「时序特征 V1」文案
  assert.ok(
    /时序特征 V1/.test(panelSrc),
    'StockStructuralStatePanel 必须含「时序特征 V1」折叠卡片标题',
  )
  // 必须含三个分组：daily_context / m15_response / derived_relation
  assert.ok(/daily_context/.test(panelSrc), 'TemporalFeaturesCard 必须含 daily_context 分组')
  assert.ok(/m15_response/.test(panelSrc), 'TemporalFeaturesCard 必须含 m15_response 分组')
  assert.ok(/derived_relation/.test(panelSrc), 'TemporalFeaturesCard 必须含 derived_relation 分组')
})

// ===== 13. capture=feishu 隐藏结构面板（hideStructuralStateParam 含 isCaptureMode）=====
test('test_capture_mode_hides_panel', () => {
  const src = readSource()

  // hideStructuralStateParam 必须包含 isCaptureMode 引用
  // capture=feishu 时 isCaptureMode=true → hideStructuralStateParam=true → 面板强制隐藏
  assert.ok(
    /hideStructuralStateParam\s*=\s*[\s\S]*?isCaptureMode/.test(src),
    'hideStructuralStateParam 必须包含 isCaptureMode（capture=feishu 触发结构面板隐藏）',
  )

  // shouldShowPanel 必须基于 !eventPanelCollapsed && !hideStructuralStateParam
  // 当 isCaptureMode=true 时 hideStructuralStateParam=true，shouldShowPanel=false（面板不渲染）
  assert.ok(
    /shouldShowPanel\s*=\s*!eventPanelCollapsed\s*&&\s*!hideStructuralStateParam/.test(src),
    'shouldShowPanel 必须基于 !eventPanelCollapsed && !hideStructuralStateParam（capture=feishu 时面板强制隐藏）',
  )
})

// ===== 14. capture=feishu 无侧列（testid 落在 tv-chart-column，不在 tv-content）=====
// Phase 3 重构后，主渲染路径的 testid 通过 chartColumnProps prop 传递给 StockResearchWorkspace，
// StockResearchWorkspace 将其应用到 <section className="tv-chart-column"> 上。
// 加载/错误状态仍在 StockDetailPage 中直接设置 testid。
test('test_capture_mode_no_side_column', () => {
  const src = readSource()

  // StockDetailPage 必须通过 chartColumnProps 传递 data-testid="stock-detail-capture"
  assert.ok(
    /chartColumnProps=\{\{[\s]*['"]data-testid['"]:[\s]*['"]stock-detail-capture['"][\s]*\}\}/.test(src),
    'StockDetailPage 主渲染路径必须通过 chartColumnProps 传递 data-testid="stock-detail-capture" 给 StockResearchWorkspace',
  )

  // 源码必须包含 data-testid="stock-detail-capture"（加载/错误状态 + chartColumnProps）
  assert.ok(
    src.includes('data-testid="stock-detail-capture"'),
    'StockDetailPage 必须设置 data-testid="stock-detail-capture"（capture worker 通过该选择器截图）',
  )

  // StockResearchWorkspace 必须将 chartColumnProps 的 data-testid 应用到 tv-chart-column section
  const workspacePath = join(FRONTEND_ROOT, 'src', 'features', 'stock-research', 'StockResearchWorkspace.tsx')
  const workspaceSrc = readFileSync(workspacePath, 'utf-8')
  assert.ok(
    /<section\s+className="tv-chart-column"[\s\S]{0,200}?data-testid=\{chartColumnProps\?\.\[['"]data-testid['"]\]\}/.test(workspaceSrc),
    'StockResearchWorkspace 必须在 <section className="tv-chart-column"> 上应用 chartColumnProps 的 data-testid（capture worker 截图选择器落在图表列）',
  )
})
