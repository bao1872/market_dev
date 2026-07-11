// [IndicatorManifest] - 描述: 指标图层 manifest 与偏好持久化契约测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/indicatorManifest.test.ts
//
// 覆盖：
//  1. INDICATOR_LAYER_MANIFEST 包含 5 个条目
//  2. manifest 条目字段完整（id/name/kind/defaultVisible/dependencies/renderOrder）
//  3. 默认可见性：consensus_zone=true, price_structure=true, volume=true, boll=false, macd=false
//  4. 主图/副图分组正确（3 主图 + 2 副图）
//  5. defaultIndicatorVisibility() 返回与 manifest 默认值一致
//  6. loadIndicatorVisibility 空存储返回默认值
//  7. saveIndicatorVisibility/loadIndicatorVisibility 往返一致
//  8. 版本不匹配时重置为默认值
//  9. 存储损坏（非 JSON）时回退默认值

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  INDICATOR_LAYER_MANIFEST,
  defaultIndicatorVisibility,
} from '../stockResearchTypes.ts'
import { loadIndicatorVisibility, saveIndicatorVisibility } from '../indicatorPreferences.ts'

// localStorage mock（函数内访问 globalThis.localStorage）
function createLocalStorageMock() {
  const store = new Map<string, string>()
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => { store.set(key, value) },
    removeItem: (key: string) => { store.delete(key) },
    clear: () => { store.clear() },
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() { return store.size },
  } as unknown as Storage
}

// ===== 1. manifest 包含 5 个条目 =====
test('INDICATOR_LAYER_MANIFEST 包含 5 个条目', () => {
  assert.equal(INDICATOR_LAYER_MANIFEST.length, 5)
})

// ===== 2. manifest 条目字段完整 =====
test('manifest 条目字段完整', () => {
  for (const entry of INDICATOR_LAYER_MANIFEST) {
    assert.ok(typeof entry.id === 'string' && entry.id.length > 0, `id should be non-empty string: ${entry.id}`)
    assert.ok(typeof entry.name === 'string' && entry.name.length > 0, `name should be non-empty string: ${entry.id}`)
    assert.ok(entry.kind === 'main' || entry.kind === 'sub', `kind should be main|sub: ${entry.id}`)
    assert.equal(typeof entry.defaultVisible, 'boolean', `defaultVisible should be boolean: ${entry.id}`)
    assert.ok(Array.isArray(entry.dependencies), `dependencies should be array: ${entry.id}`)
    assert.equal(typeof entry.renderOrder, 'number', `renderOrder should be number: ${entry.id}`)
  }
})

// ===== 3. 默认可见性 =====
test('默认可见性：consensus_zone=true, price_structure=true, volume=true, boll=false, macd=false', () => {
  const byId = Object.fromEntries(INDICATOR_LAYER_MANIFEST.map((e) => [e.id, e]))
  assert.equal(byId.consensus_zone.defaultVisible, true)
  assert.equal(byId.price_structure.defaultVisible, true)
  assert.equal(byId.volume.defaultVisible, true)
  assert.equal(byId.boll.defaultVisible, false)
  assert.equal(byId.macd.defaultVisible, false)
})

// ===== 4. 主图/副图分组 =====
test('主图 3 个 + 副图 2 个', () => {
  const main = INDICATOR_LAYER_MANIFEST.filter((e) => e.kind === 'main')
  const sub = INDICATOR_LAYER_MANIFEST.filter((e) => e.kind === 'sub')
  assert.equal(main.length, 3, 'should have 3 main layers')
  assert.equal(sub.length, 2, 'should have 2 sub layers')
})

// ===== 5. defaultIndicatorVisibility 与 manifest 默认值一致 =====
test('defaultIndicatorVisibility() 与 manifest 默认值一致', () => {
  const defaults = defaultIndicatorVisibility()
  for (const entry of INDICATOR_LAYER_MANIFEST) {
    assert.equal(defaults[entry.id], entry.defaultVisible, `mismatch for ${entry.id}`)
  }
})

// ===== 6. loadIndicatorVisibility 空存储返回默认值 =====
test('loadIndicatorVisibility 空存储返回默认值', () => {
  globalThis.localStorage = createLocalStorageMock()
  const result = loadIndicatorVisibility()
  const defaults = defaultIndicatorVisibility()
  assert.deepEqual(result, defaults)
})

// ===== 7. save/load 往返一致 =====
test('saveIndicatorVisibility/loadIndicatorVisibility 往返一致', () => {
  globalThis.localStorage = createLocalStorageMock()
  const custom = { ...defaultIndicatorVisibility(), boll: true, macd: true, volume: false }
  saveIndicatorVisibility(custom)
  const loaded = loadIndicatorVisibility()
  assert.deepEqual(loaded, custom)
})

// ===== 8. 版本不匹配时重置默认值 =====
test('版本不匹配时重置为默认值', () => {
  globalThis.localStorage = createLocalStorageMock()
  // 写入旧版本数据
  globalThis.localStorage.setItem('panji:indicator-visibility:v1', JSON.stringify({
    version: 999,
    visibility: { boll: true, macd: true },
  }))
  const loaded = loadIndicatorVisibility()
  assert.deepEqual(loaded, defaultIndicatorVisibility())
})

// ===== 9. 存储损坏（非 JSON）时回退默认值 =====
test('存储损坏时回退默认值', () => {
  globalThis.localStorage = createLocalStorageMock()
  globalThis.localStorage.setItem('panji:indicator-visibility:v1', 'not-json{{{')
  const loaded = loadIndicatorVisibility()
  assert.deepEqual(loaded, defaultIndicatorVisibility())
})
