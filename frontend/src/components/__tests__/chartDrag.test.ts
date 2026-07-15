// [ChartDrag] - 描述: StrategyChart Pointer Events 拖拽契约测试（源码级）
// 用法：node --experimental-strip-types --test src/components/__tests__/chartDrag.test.ts
//
// 覆盖：
// 1. 使用 Pointer Events（pointerdown/pointermove/pointerup/pointercancel）
// 2. setPointerCapture / releasePointerCapture
// 3. dragRef 保存 startClientX + startViewport + pointerId
// 4. dragMovedRef 4px 阈值抑制 click
// 5. cursor 为 grab/grabbing
// 6. 不使用旧 mouse 事件（mousedown/mousemove/mouseup）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const CHART_PATH = join(__dirname, '..', 'StrategyChart.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 使用 Pointer Events =====
test('StrategyChart 使用 Pointer Events（pointerdown/pointermove/pointerup/pointercancel）', () => {
  const src = readSource(CHART_PATH)
  assert.ok(src.includes("'pointerdown'"), '必须监听 pointerdown 事件')
  assert.ok(src.includes("'pointermove'"), '必须监听 pointermove 事件')
  assert.ok(src.includes("'pointerup'"), '必须监听 pointerup 事件')
  assert.ok(src.includes("'pointercancel'"), '必须监听 pointercancel 事件')
})

// ===== 2. setPointerCapture / releasePointerCapture =====
test('StrategyChart 使用 setPointerCapture / releasePointerCapture', () => {
  const src = readSource(CHART_PATH)
  assert.ok(
    src.includes('setPointerCapture'),
    'pointerdown 必须调用 setPointerCapture 捕获指针',
  )
  assert.ok(
    src.includes('releasePointerCapture'),
    'pointerup/pointercancel 必须调用 releasePointerCapture 释放指针',
  )
})

// ===== 3. dragRef 保存 startClientX + startViewport + pointerId =====
test('dragRef 保存 startClientX + startViewport + pointerId', () => {
  const src = readSource(CHART_PATH)
  assert.ok(
    src.includes('startClientX') && src.includes('startViewport') && src.includes('pointerId'),
    'dragRef 必须保存 startClientX + startViewport + pointerId',
  )
  // 验证 dragRef 类型定义包含这三个字段（允许 | null）
  const dragRefMatch = src.match(/dragRef\s*=\s*useRef<\{[^}]*\}[^>]*>/)
  assert.ok(dragRefMatch, '必须存在 dragRef useRef 声明')
  assert.ok(dragRefMatch![0].includes('startClientX'), 'dragRef 类型必须包含 startClientX')
  assert.ok(dragRefMatch![0].includes('startViewport'), 'dragRef 类型必须包含 startViewport')
  assert.ok(dragRefMatch![0].includes('pointerId'), 'dragRef 类型必须包含 pointerId')
})

// ===== 4. dragMovedRef 4px 阈值 =====
test('dragMovedRef 4px 阈值抑制 click', () => {
  const src = readSource(CHART_PATH)
  assert.ok(src.includes('dragMovedRef'), '必须存在 dragMovedRef')
  // 4px 阈值
  assert.ok(
    src.includes('> 4') || src.includes('>= 4'),
    'dragMovedRef 必须使用 4px 阈值（> 4 或 >= 4）',
  )
  // handleClick 中检查 dragMovedRef
  assert.ok(
    src.includes('if (dragMovedRef.current) return'),
    'handleClick 必须在 dragMovedRef.current 为 true 时 return（抑制 click）',
  )
})

// ===== 5. cursor grab/grabbing =====
test('cursor 为 grab/grabbing', () => {
  const src = readSource(CHART_PATH)
  assert.ok(
    src.includes("'grab'") || src.includes('"grab"'),
    '默认 cursor 必须为 grab',
  )
  assert.ok(
    src.includes("'grabbing'") || src.includes('"grabbing"'),
    'pointerdown 时 cursor 必须切换为 grabbing',
  )
})

// ===== 6. 不使用旧 mouse 事件 =====
test('不使用旧 mouse 事件（mousedown/mousemove/mouseup 作为事件监听器）', () => {
  const src = readSource(CHART_PATH)
  // 不应使用 addEventListener('mousedown', ...) 等旧事件
  assert.ok(
    !src.includes("addEventListener('mousedown'") && !src.includes('addEventListener("mousedown"'),
    '不应使用 mousedown 事件监听器（已改为 pointerdown）',
  )
  assert.ok(
    !src.includes("addEventListener('mouseup'") && !src.includes('addEventListener("mouseup"'),
    '不应使用 mouseup 事件监听器（已改为 pointerup）',
  )
  // window.addEventListener('mousemove', ...) 也不应存在
  assert.ok(
    !src.includes("window.addEventListener('mousemove'") && !src.includes('window.addEventListener("mousemove"'),
    '不应在 window 上监听 mousemove（已改为 canvas pointermove + setPointerCapture）',
  )
})

// ===== 7. 从 startViewport 计算总位移（不在 stale viewport 上累计） =====
test('pointermove 从 startViewport 计算总位移（不在 stale viewport 上累计）', () => {
  const src = readSource(CHART_PATH)
  // handlePointerMove 应引用 dragRef.current.startViewport
  const moveSection = src.match(/handlePointerMove[\s\S]{0,500}?panViewport|handlePointerMove[\s\S]{0,800}?startViewport/)
  assert.ok(moveSection, '必须存在 handlePointerMove 函数')
  assert.ok(
    moveSection![0].includes('startViewport'),
    'handlePointerMove 必须从 dragRef.current.startViewport 计算位移（不累计到 stale viewport）',
  )
})
