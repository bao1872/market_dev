// [IndicatorManifest] - 描述: 指标图层 manifest 与偏好持久化契约测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/indicatorManifest.test.ts
//
// 覆盖：
//  1. INDICATOR_LAYER_MANIFEST 包含 5 个条目
//  2. manifest 条目字段完整（id/name/kind/defaultVisible/enabled/dependencies/renderOrder）
//  3. 默认可见性：consensus_zone=false (Phase 3 纠偏，禁用), price_structure=true, volume=true, boll=false, macd=false
//  4. 主图/副图分组正确（3 主图 + 2 副图）
//  5. defaultIndicatorVisibility() 返回与 manifest 默认值一致
//  6. loadIndicatorVisibility 空存储返回默认值
//  7. saveIndicatorVisibility/loadIndicatorVisibility 往返一致
//  8. 版本不匹配时重置为默认值
//  9. 存储损坏（非 JSON）时回退默认值
// 10. consensus_zone 在 Phase 5 前禁用（enabled=false, defaultVisible=false, name="成交量分布"）

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
    assert.equal(typeof entry.enabled, 'boolean', `enabled should be boolean: ${entry.id}`)
    assert.ok(Array.isArray(entry.dependencies), `dependencies should be array: ${entry.id}`)
    assert.equal(typeof entry.renderOrder, 'number', `renderOrder should be number: ${entry.id}`)
  }
})

// ===== 3. 默认可见性 =====
test('默认可见性：consensus_zone=false (Phase 3 禁用), price_structure=true, volume=true, boll=false, macd=false', () => {
  const byId = Object.fromEntries(INDICATOR_LAYER_MANIFEST.map((e) => [e.id, e]))
  // Phase 3 纠偏：consensus_zone 在 Phase 5 实现真实筹码共识区前禁用
  // defaultVisible=false（不默认显示），enabled=false（开关不可点击）
  assert.equal(byId.consensus_zone.defaultVisible, false)
  assert.equal(byId.price_structure.defaultVisible, true)
  assert.equal(byId.volume.defaultVisible, true)
  assert.equal(byId.boll.defaultVisible, false)
  assert.equal(byId.macd.defaultVisible, false)
})

// ===== 10. consensus_zone 在 Phase 5 前禁用 =====
test('consensus_zone 在 Phase 5 前禁用（enabled=false, name="成交量分布"）', () => {
  const byId = Object.fromEntries(INDICATOR_LAYER_MANIFEST.map((e) => [e.id, e]))
  const cz = byId.consensus_zone
  assert.equal(cz.enabled, false, 'consensus_zone enabled 必须为 false（Phase 5 前禁用开关）')
  assert.equal(cz.defaultVisible, false, 'consensus_zone defaultVisible 必须为 false（不默认显示）')
  assert.equal(cz.name, '成交量分布', 'consensus_zone name 必须为"成交量分布"（非"筹码共识区"，避免误导用户）')
  // 其他图层 enabled 必须为 true（可正常开关）
  for (const entry of INDICATOR_LAYER_MANIFEST) {
    if (entry.id === 'consensus_zone') continue
    assert.equal(entry.enabled, true, `${entry.id} enabled 必须为 true（仅 consensus_zone 禁用）`)
  }
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
