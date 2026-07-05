// [结构状态隐藏开关] - 描述: StockDetailPage 结构状态面板隐藏开关契约测试
// 用法：node --experimental-strip-types --test scripts/contract-tests/structural-state-toggle.test.ts
// 覆盖：
// 1. 面板默认隐藏（localStorage.getItem 仅在 'true' 时显示）
// 2. 开关按钮存在（structural-state-toggle className + 结构状态 文案）
// 3. localStorage 持久化用户选择
// 4. hideStructuralState=1 强制隐藏
// 5. capture=1 强制隐藏
// 6. capture=feishu 强制隐藏（保留现有 isCaptureMode 逻辑）
// 7. 强制隐藏时禁用 toggle 按钮（early return）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const FRONTEND_ROOT = join(__dirname, '..', '..')
const PAGE_PATH = join(FRONTEND_ROOT, 'src', 'pages', 'StockDetailPage.tsx')

function readSource(): string {
  return readFileSync(PAGE_PATH, 'utf-8')
}

// ===== 1. 面板默认隐藏 =====
test('Panel is hidden by default (localStorage only shows on "true")', () => {
  const src = readSource()

  // 必须读取 localStorage，且只在 'true' 时显示（默认 null/其他值时隐藏）
  assert.ok(
    /localStorage\.getItem\(\s*['"]showStructuralState['"]\s*\)/.test(src),
    'StockDetailPage 必须读取 localStorage "showStructuralState"',
  )
  // 必须显式比较为 'true' 才显示，即默认 falsy
  assert.ok(
    /localStorage\.getItem\(\s*['"]showStructuralState['"]\s*\)\s*===?\s*['"]true['"]/.test(src),
    'showStructuralState 默认 falsy（localStorage 仅在 "true" 时显示）',
  )
})

// ===== 2. 开关按钮存在 =====
test('Toggle button exists with structural-state-toggle className', () => {
  const src = readSource()

  // 必须含 toggle 按钮 className
  assert.ok(
    /structural-state-toggle/.test(src),
    'StockDetailPage 必须含 "structural-state-toggle" className 的开关按钮',
  )
  // 必须含「结构状态」文案
  assert.ok(
    /结构状态/.test(src),
    'StockDetailPage 开关按钮必须含「结构状态」文案',
  )
})

// ===== 3. localStorage 持久化用户选择 =====
test('localStorage persists user choice via setItem', () => {
  const src = readSource()

  // 必须 setItem 写入 localStorage
  assert.ok(
    /localStorage\.setItem\(\s*['"]showStructuralState['"]/.test(src),
    'StockDetailPage 必须通过 localStorage.setItem 持久化 "showStructuralState" 选择',
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
  // 匹配模式：toggleStructuralState useCallback 内含 if (hideStructuralStateParam) return
  const toggleBlockMatch = src.match(/toggleStructuralState\s*=\s*useCallback\(\s*\(\)\s*=>\s*\{([\s\S]*?)\},\s*\[hideStructuralStateParam\]\s*\)/)
  assert.ok(toggleBlockMatch, 'StockDetailPage 必须含 toggleStructuralState useCallback 且依赖 hideStructuralStateParam')

  const toggleBody = toggleBlockMatch![1]
  assert.ok(
    /if\s*\(\s*hideStructuralStateParam\s*\)\s*return/.test(toggleBody),
    'toggleStructuralState 回调必须在 hideStructuralStateParam=true 时 early return（强制隐藏时禁用 toggle）',
  )
})

// ===== 8. toggle 按钮在 tv-chart-column 内部（定位上下文） =====
test('Toggle button is inside tv-chart-column for stable absolute positioning', () => {
  const src = readSource()

  // 源码中可能有多个 tv-chart-column section，用位置检测：
  // 1. 找到 structural-state-toggle 按钮的位置
  // 2. 向前查找最近的 <section className="tv-chart-column">
  // 3. 向后查找最近的 </section>
  // 4. 验证 toggle 在两者之间
  const toggleIdx = src.indexOf('structural-state-toggle')
  assert.ok(toggleIdx > 0, 'StockDetailPage 必须含 structural-state-toggle')

  const sectionOpenIdx = src.lastIndexOf('<section className="tv-chart-column">', toggleIdx)
  assert.ok(sectionOpenIdx > 0, 'structural-state-toggle 必须在 <section className="tv-chart-column"> 之后')

  const sectionCloseIdx = src.indexOf('</section>', toggleIdx)
  assert.ok(sectionCloseIdx > toggleIdx, 'structural-state-toggle 必须在 </section> 之前')

  // 同时确认 tv-chart-column 有 position: relative（在 global.scss 中）
  const scssPath = join(FRONTEND_ROOT, 'src', 'styles', 'global.scss')
  const scss = readFileSync(scssPath, 'utf-8')
  const chartColRuleMatch = scss.match(/\.tv-chart-column\s*\{[^}]*position:\s*relative[^}]*\}/)
  assert.ok(
    chartColRuleMatch,
    '.tv-chart-column 必须含 position: relative（作为 toggle 按钮的定位上下文）',
  )
})
