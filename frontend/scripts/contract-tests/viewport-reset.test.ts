// [viewport-reset] - P0-5 契约测试：个股详情初始 K 线定位到最新行情
// 用法：node --experimental-strip-types --test scripts/contract-tests/viewport-reset.test.ts
//
// 验证：
// 1. StockResearchWorkspace 不再使用 makeDefaultViewport（已删除）
// 2. StockResearchWorkspace 使用 `${symbol}:${timeframe}` 复合 key 存储 viewport
// 3. StockResearchWorkspace 在 symbol 变化时清空 viewport
// 4. StockResearchWorkspace 向 StrategyChart 传 undefined（无保存 viewport 时）
// 5. StrategyChart 有 auto-follow effect（新行情追加跟随）
// 6. StrategyChart range/reset 按钮使用 createDefaultViewport(calc.length)

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

const workspaceSrc = readFileSync(
  join(__dirname, '../../src/features/stock-research/StockResearchWorkspace.tsx'),
  'utf-8',
)
const chartSrc = readFileSync(
  join(__dirname, '../../src/components/StrategyChart.tsx'),
  'utf-8',
)

// ===== 1. makeDefaultViewport 已删除 =====
test('P0-5: StockResearchWorkspace 不再定义或调用 makeDefaultViewport', () => {
  assert.ok(!workspaceSrc.includes('makeDefaultViewport'),
    'makeDefaultViewport 应已删除（不再创建 {0,0} 假 viewport）')
  assert.ok(!workspaceSrc.includes('createDefaultViewport(0)'),
    '不应调用 createDefaultViewport(0) 创建假 viewport')
})

// ===== 2. 使用 `${symbol}:${timeframe}` 复合 key =====
test('P0-5: StockResearchWorkspace 使用 `${symbol}:${timeframe}` 复合 key', () => {
  assert.ok(workspaceSrc.includes('`${symbol}:${timeframe}`'),
    '应使用 `${symbol}:${timeframe}` 复合 key 存储 viewport')
})

// ===== 3. symbol 变化时清空 viewport =====
test('P0-5: StockResearchWorkspace 在 symbol 变化时清空 viewport', () => {
  // 查找 setViewportByKey({}) 在 symbol 依赖的 useEffect 中
  const clearEffectPattern = /useEffect\(\(\)\s*=>\s*\{[^}]*setViewportByKey\(\{\}\)[^}]*\},\s*\[symbol\]\)/
  assert.ok(clearEffectPattern.test(workspaceSrc),
    '应在 symbol 变化时调用 setViewportByKey({}) 清空所有保存的 viewport')
})

// ===== 4. 向 StrategyChart 传 undefined（无保存 viewport 时）=====
test('P0-5: StockResearchWorkspace 向 StrategyChart 传 viewportByKey[viewportKey]（可能为 undefined）', () => {
  // 不应使用 ?? makeDefaultViewport() 或 ?? createDefaultViewport(0)
  assert.ok(!workspaceSrc.includes('?? makeDefaultViewport'),
    '不应使用 ?? makeDefaultViewport() 回退')
  assert.ok(workspaceSrc.includes('viewport={viewportByKey[viewportKey]}'),
    '应传 viewportByKey[viewportKey]（无保存时为 undefined）')
})

// ===== 5. StrategyChart 有 auto-follow effect =====
test('P0-5: StrategyChart 有新行情追加 auto-follow effect', () => {
  assert.ok(chartSrc.includes('prevCalcLengthRef'),
    '应使用 prevCalcLengthRef 跟踪前一次 calc.length')
  assert.ok(chartSrc.includes('viewportProp.toIndex >= prevLen'),
    'auto-follow 条件：viewportProp.toIndex >= prevLen（用户位于最右端）')
  assert.ok(chartSrc.includes('calc.length <= prevLen'),
    '应检查 calc.length <= prevLen 时跳过（无增长）')
})

// ===== 6. StrategyChart range/reset 按钮使用 createDefaultViewport(calc.length) =====
test('P0-5: StrategyChart range/reset 按钮以 calc.length 为右边界', () => {
  // 复位按钮
  // [2026-07-21 反馈] 复位按钮使用 initialVisibleBars（= defaultVisibleBars ?? MAX_VISIBLE_BARS），
  //   飞书移动舞台传 defaultVisibleBars=90 时复位到 90 根，桌面端不传时复位到 MAX_VISIBLE_BARS
  assert.ok(chartSrc.includes('createDefaultViewport(calc.length, initialVisibleBars)'),
    '复位按钮应使用 createDefaultViewport(calc.length, initialVisibleBars)')
  // 范围按钮
  assert.ok(chartSrc.includes('createDefaultViewport(calc.length, Math.max(MIN_VISIBLE_BARS, visible))'),
    '范围按钮应使用 createDefaultViewport(calc.length, ...) 以最新 bar 为右边界')
})

// ===== 7. StrategyChart 无 viewportProp 时使用 createDefaultViewport(calc.length) =====
test('P0-5: StrategyChart 无 viewportProp 时回退到 createDefaultViewport(calc.length)', () => {
  // 查找 viewport useMemo 中的 fallback 逻辑
  assert.ok(chartSrc.includes('if (viewportProp) return clampViewport(viewportProp, calc.length)'),
    'viewportProp 存在时使用 clampViewport')
  assert.ok(chartSrc.includes('return createDefaultViewport(calc.length, visibleCount)'),
    'viewportProp 为 undefined 时回退到 createDefaultViewport(calc.length)')
})
