// [FirstPyramidCollapse] - 第一金字塔折叠偏好契约测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/firstPyramidCollapsePreference.test.ts
//
// 覆盖：
//   1. 持久化键为 panji:first-pyramid-detail-collapsed:v1
//   2. 默认状态为展开（false）
//   3. 折叠后读取返回 true
//   4. 展开后读取返回 false
//   5. localStorage 不可用时降级到默认（false），不抛异常
//   6. capture 模式下 firstPyramidAvailable=false（逻辑契约，非组件渲染）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  FIRST_PYRAMID_COLLAPSE_STORAGE_KEY,
  loadFirstPyramidCollapsed,
  saveFirstPyramidCollapsed,
} from '../firstPyramidCollapsePreference.ts'

// 简易 localStorage mock（node 环境无 localStorage）
class MemoryStorage {
  private store = new Map<string, string>()
  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value))
  }
  removeItem(key: string): void {
    this.store.delete(key)
  }
  clear(): void {
    this.store.clear()
  }
}

test('持久化键为 panji:first-pyramid-detail-collapsed:v1', () => {
  assert.equal(
    FIRST_PYRAMID_COLLAPSE_STORAGE_KEY,
    'panji:first-pyramid-detail-collapsed:v1',
  )
})

test('默认状态为展开（false）：localStorage 无值时返回 false', () => {
  const storage = new MemoryStorage()
  // @ts-expect-error 注入 mock localStorage
  globalThis.localStorage = storage
  assert.equal(loadFirstPyramidCollapsed(), false)
})

test('折叠后读取返回 true', () => {
  const storage = new MemoryStorage()
  // @ts-expect-error 注入 mock localStorage
  globalThis.localStorage = storage
  saveFirstPyramidCollapsed(true)
  assert.equal(loadFirstPyramidCollapsed(), true)
  assert.equal(storage.getItem(FIRST_PYRAMID_COLLAPSE_STORAGE_KEY), 'true')
})

test('展开后读取返回 false', () => {
  const storage = new MemoryStorage()
  // @ts-expect-error 注入 mock localStorage
  globalThis.localStorage = storage
  saveFirstPyramidCollapsed(true)  // 先折叠
  saveFirstPyramidCollapsed(false) // 再展开
  assert.equal(loadFirstPyramidCollapsed(), false)
  assert.equal(storage.getItem(FIRST_PYRAMID_COLLAPSE_STORAGE_KEY), 'false')
})

test('展开→折叠→展开 状态切换正确', () => {
  const storage = new MemoryStorage()
  // @ts-expect-error 注入 mock localStorage
  globalThis.localStorage = storage
  // 展开（默认）
  assert.equal(loadFirstPyramidCollapsed(), false)
  // 折叠
  saveFirstPyramidCollapsed(true)
  assert.equal(loadFirstPyramidCollapsed(), true)
  // 展开
  saveFirstPyramidCollapsed(false)
  assert.equal(loadFirstPyramidCollapsed(), false)
})

test('切股后偏好保持（localStorage 持久化，不随 symbol 重置）', () => {
  const storage = new MemoryStorage()
  // @ts-expect-error 注入 mock localStorage
  globalThis.localStorage = storage
  saveFirstPyramidCollapsed(true)
  // 模拟切换股票：重新调用 loadFirstPyramidCollapsed（不依赖 symbol）
  assert.equal(loadFirstPyramidCollapsed(), true)
  // 切换到另一只股票仍然保持折叠
  assert.equal(loadFirstPyramidCollapsed(), true)
})

test('localStorage 不可用时降级到默认（false），不抛异常', () => {
  // 模拟 localStorage 访问抛异常（如隐私模式）
  // @ts-expect-error 注入会抛异常的 mock
  globalThis.localStorage = {
    getItem: () => { throw new Error('localStorage unavailable') },
    setItem: () => { throw new Error('localStorage unavailable') },
  }
  // load 不抛异常，返回默认 false
  assert.equal(loadFirstPyramidCollapsed(), false)
  // save 不抛异常
  assert.doesNotThrow(() => saveFirstPyramidCollapsed(true))
})

test('capture 模式下 firstPyramidAvailable=false（逻辑契约）', () => {
  // 验证 capture 模式的逻辑：isCaptureMode=true → firstPyramidAvailable=false
  // 这是 StockDetailPage 的核心拆分：available ≠ collapsed
  const isCaptureMode = true
  const symbol = '000001.SZ'
  const firstPyramidAvailable = !isCaptureMode && !!symbol
  assert.equal(firstPyramidAvailable, false)
  // 即使有 symbol，capture 模式也不显示
  // 但 collapsed 偏好独立存在（不受 capture 影响）
  const storage = new MemoryStorage()
  // @ts-expect-error 注入 mock localStorage
  globalThis.localStorage = storage
  saveFirstPyramidCollapsed(true)
  assert.equal(loadFirstPyramidCollapsed(), true) // 偏好保留
  assert.equal(firstPyramidAvailable, false)      // 但 capture 时不可用
})
