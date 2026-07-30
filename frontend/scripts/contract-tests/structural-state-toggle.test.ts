// [第一金字塔可折叠] - 描述: StockDetailPage 第一金字塔折叠/展开契约测试
// 用法：node --experimental-strip-types --test scripts/contract-tests/structural-state-toggle.test.ts
//
// [P0 2026-07-30] 原 eventPanelCollapsed/AtomicFactsDrawer 已在 CHANGE-20260730-012 删除。
// 本测试更新为验证新的第一金字塔可折叠契约：
//   1. 拆分 firstPyramidAvailable（是否有资格显示）和 firstPyramidCollapsed（用户折叠偏好）
//   2. firstPyramidAvailable = !isCaptureMode && !!symbol
//   3. firstPyramidCollapsed 持久化到 panji:first-pyramid-detail-collapsed:v1，默认展开（false）
//   4. capture 模式完全隐藏（firstPyramidAvailable=false）
//   5. showRightPanel/rightPanelCollapsed/onRightPanelCollapsedChange 传入 StockResearchWorkspace
//   6. StockResearchWorkspace 在 onRightPanelCollapsedChange 提供时渲染收起/展开按钮
//   7. 切股/刷新后保留用户折叠偏好（localStorage 持久化）
//   8. 旧 AtomicFacts/eventPanelCollapsed 逻辑不得恢复

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const FRONTEND_ROOT = join(__dirname, '..', '..')
const PAGE_PATH = join(FRONTEND_ROOT, 'src', 'pages', 'StockDetailPage.tsx')
const WORKSPACE_PATH = join(FRONTEND_ROOT, 'src', 'features', 'stock-research', 'StockResearchWorkspace.tsx')
const PREF_PATH = join(FRONTEND_ROOT, 'src', 'features', 'stock-research', 'firstPyramidCollapsePreference.ts')

function readSource(): string {
  return readFileSync(PAGE_PATH, 'utf-8')
}

function readWorkspaceSource(): string {
  return readFileSync(WORKSPACE_PATH, 'utf-8')
}

function readPrefSource(): string {
  return readFileSync(PREF_PATH, 'utf-8')
}

// ===== 1. 拆分允许显示和用户折叠状态 =====
test('firstPyramidAvailable = !isCaptureMode && !!symbol (decoupled from collapse state)', () => {
  const src = readSource()
  // 必须有 firstPyramidAvailable 变量（不再是 showFirstPyramidDetail）
  assert.ok(src.includes('firstPyramidAvailable'), 'StockDetailPage 必须有 firstPyramidAvailable')
  // 必须基于 isCaptureMode 和 symbol
  assert.ok(
    /firstPyramidAvailable\s*=\s*!isCaptureMode\s*&&\s*!!symbol/.test(src),
    'firstPyramidAvailable = !isCaptureMode && !!symbol',
  )
  // 旧 showFirstPyramidDetail 不得存在
  assert.ok(!src.includes('showFirstPyramidDetail'), '旧 showFirstPyramidDetail 必须删除')
})

// ===== 2. firstPyramidCollapsed 状态 + localStorage 持久化 =====
test('firstPyramidCollapsed persisted to panji:first-pyramid-detail-collapsed:v1', () => {
  const src = readSource()
  const prefSrc = readPrefSource()
  // StockDetailPage 必须使用 loadFirstPyramidCollapsed/saveFirstPyramidCollapsed
  assert.ok(src.includes('loadFirstPyramidCollapsed'), '必须 import loadFirstPyramidCollapsed')
  assert.ok(src.includes('saveFirstPyramidCollapsed'), '必须 import saveFirstPyramidCollapsed')
  // 持久化键
  assert.ok(
    prefSrc.includes('panji:first-pyramid-detail-collapsed:v1'),
    '持久化键必须为 panji:first-pyramid-detail-collapsed:v1',
  )
  // firstPyramidCollapsed state
  assert.ok(
    /firstPyramidCollapsed/.test(src),
    '必须有 firstPyramidCollapsed state',
  )
})

// ===== 3. 默认展开（false） =====
test('Default state is expanded (false)', () => {
  const prefSrc = readPrefSource()
  // loadFirstPyramidCollapsed 无值时返回 false
  assert.ok(
    /return false/.test(prefSrc),
    'loadFirstPyramidCollapsed 默认返回 false（展开）',
  )
})

// ===== 4. capture 模式完全隐藏 =====
test('capture mode hides first pyramid panel (firstPyramidAvailable=false)', () => {
  const src = readSource()
  // firstPyramidAvailable 基于 !isCaptureMode
  assert.ok(
    /!isCaptureMode/.test(src),
    'firstPyramidAvailable 必须基于 !isCaptureMode',
  )
})

// ===== 5. StockResearchWorkspace 接收三个新 props =====
test('StockResearchWorkspace receives showRightPanel/rightPanelCollapsed/onRightPanelCollapsedChange', () => {
  const src = readSource()
  // showRightPanel={firstPyramidAvailable}
  assert.ok(
    /showRightPanel=\{firstPyramidAvailable\}/.test(src),
    'showRightPanel={firstPyramidAvailable}',
  )
  // rightPanelCollapsed={firstPyramidCollapsed}
  assert.ok(
    /rightPanelCollapsed=\{firstPyramidCollapsed\}/.test(src),
    'rightPanelCollapsed={firstPyramidCollapsed}',
  )
  // onRightPanelCollapsedChange={handleFirstPyramidCollapsedChange}
  assert.ok(
    /onRightPanelCollapsedChange=\{handleFirstPyramidCollapsedChange\}/.test(src),
    'onRightPanelCollapsedChange={handleFirstPyramidCollapsedChange}',
  )
})

// ===== 6. StockResearchWorkspace 渲染收起/展开按钮 =====
test('StockResearchWorkspace renders collapse/expand buttons when onRightPanelCollapsedChange provided', () => {
  const wsSrc = readWorkspaceSource()
  // 必须有 onRightPanelCollapsedChange prop
  assert.ok(
    wsSrc.includes('onRightPanelCollapsedChange'),
    'StockResearchWorkspace 必须有 onRightPanelCollapsedChange prop',
  )
  // 必须有收起按钮 className
  assert.ok(
    wsSrc.includes('tv-side-collapse-btn'),
    '必须有收起按钮 (tv-side-collapse-btn)',
  )
  // 必须有展开按钮 className
  assert.ok(
    wsSrc.includes('tv-side-expand-btn'),
    '必须有展开按钮 (tv-side-expand-btn)',
  )
  // 折叠时展开按钮在 chart-column 显示
  assert.ok(
    /rightPanelCollapsed\s*&&\s*onRightPanelCollapsedChange/.test(wsSrc),
    '折叠时显示展开按钮',
  )
})

// ===== 7. capture 模式不显示折叠/展开按钮 =====
test('capture mode does not render collapse/expand buttons', () => {
  const wsSrc = readWorkspaceSource()
  // 展开按钮条件必须包含 !isCaptureMode
  assert.ok(
    /onRightPanelCollapsedChange\s*&&\s*!isCaptureMode/.test(wsSrc),
    '展开按钮条件必须包含 !isCaptureMode',
  )
})

// ===== 8. 旧 AtomicFacts/eventPanelCollapsed 不得恢复 =====
test('Old AtomicFacts/eventPanelCollapsed logic must not be restored', () => {
  const src = readSource()
  // 不得有 eventPanelCollapsed 作为变量/状态（注释中提及可以）
  assert.ok(
    !/const\s+eventPanelCollapsed|let\s+eventPanelCollapsed|eventPanelCollapsed\s*=/.test(src),
    '不得恢复 eventPanelCollapsed 作为变量',
  )
  // 不得有 AtomicFactsDrawer 的 import 或 JSX
  assert.ok(
    !/import.*AtomicFactsDrawer|<AtomicFactsDrawer/.test(src),
    '不得恢复 AtomicFactsDrawer 组件',
  )
  // 不得有旧 localStorage key panji:event-panel:v1 的实际调用
  assert.ok(
    !/localStorage\.(getItem|setItem)\(\s*['"]panji:event-panel:v1['"]/.test(src),
    '不得恢复旧 localStorage key 实际调用',
  )
  // 不得有 structural-state-toggle-btn（旧开关按钮 className）
  assert.ok(!/className.*structural-state-toggle-btn/.test(src), '不得恢复旧 structural-state-toggle-btn')
})

// ===== 9. 无 onRightPanelCollapsedChange 时保持旧行为（兼容 AdminStockDebugPage） =====
test('StockResearchWorkspace falls back to old behavior without onRightPanelCollapsedChange', () => {
  const wsSrc = readWorkspaceSource()
  // 必须有条件判断 onRightPanelCollapsedChange 是否提供
  assert.ok(
    /onRightPanelCollapsedChange\s*&&\s*!isCaptureMode\s*\?/.test(wsSrc),
    '无 onRightPanelCollapsedChange 时回退到旧行为（直接渲染 rightPanel）',
  )
})
