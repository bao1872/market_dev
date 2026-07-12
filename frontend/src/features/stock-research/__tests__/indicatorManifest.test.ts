// [ChartLayerManifest] - 描述: 图表图层 manifest 与偏好持久化契约测试（单一真源 v2）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/indicatorManifest.test.ts
//
// 覆盖：
//  1. CHART_LAYER_MANIFEST 包含 7 个条目
//  2. manifest 条目字段完整（id/name/kind/enabled/description）
//  3. 默认可见性：watchlist → node/boll/volume/macd=true；selection → trend/volume/macd=true
//  4. 主图/副图分组正确（主图 4 + 副图 3）
//  5. breakout 为 selectionOnly
//  6. chartLayersForSource 过滤 selectionOnly
//  7. loadChartLayerVisibility 空存储返回默认值
//  8. saveChartLayerVisibility/loadChartLayerVisibility 往返一致
//  9. 旧 key 迁移（detail-chart-strategy-groups-v3 → 新 key）
// 10. 旧 key 迁移（panji:indicator-visibility:v1 → 新 key）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  CHART_LAYER_MANIFEST,
  chartLayersForSource,
  defaultChartLayerVisibility,
} from '../stockResearchTypes.ts'
import { loadChartLayerVisibility, saveChartLayerVisibility } from '../indicatorPreferences.ts'

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

// ===== 1. manifest 包含 7 个条目 =====
test('CHART_LAYER_MANIFEST 包含 7 个条目', () => {
  assert.equal(CHART_LAYER_MANIFEST.length, 7)
})

// ===== 2. manifest 条目字段完整 =====
test('manifest 条目字段完整', () => {
  for (const entry of CHART_LAYER_MANIFEST) {
    assert.ok(typeof entry.id === 'string' && entry.id.length > 0, `id should be non-empty string: ${entry.id}`)
    assert.ok(typeof entry.name === 'string' && entry.name.length > 0, `name should be non-empty string: ${entry.id}`)
    assert.ok(entry.kind === 'main' || entry.kind === 'sub', `kind should be main|sub: ${entry.id}`)
    assert.equal(typeof entry.enabled, 'boolean', `enabled should be boolean: ${entry.id}`)
    assert.ok(typeof entry.description === 'string', `description should be string: ${entry.id}`)
  }
})

// ===== 3. 默认可见性 =====
test('默认可见性：watchlist → node/boll/volume/macd=true', () => {
  const vis = defaultChartLayerVisibility('watchlist')
  assert.equal(vis.trend, false)
  assert.equal(vis.node, true)
  assert.equal(vis.boll, true)
  assert.equal(vis.volume, true)
  assert.equal(vis.macd, true)
  assert.equal(vis.sqzmom, false)
  assert.equal(vis.breakout, false)
})

test('默认可见性：selection → trend/volume/macd=true', () => {
  const vis = defaultChartLayerVisibility('selection')
  assert.equal(vis.trend, true)
  assert.equal(vis.node, false)
  assert.equal(vis.boll, false)
  assert.equal(vis.volume, true)
  assert.equal(vis.macd, true)
  assert.equal(vis.sqzmom, false)
  assert.equal(vis.breakout, false)
})

// ===== 4. 主图/副图分组 =====
test('主图 4 个 + 副图 3 个', () => {
  const main = CHART_LAYER_MANIFEST.filter((e) => e.kind === 'main')
  const sub = CHART_LAYER_MANIFEST.filter((e) => e.kind === 'sub')
  assert.equal(main.length, 4, 'should have 4 main layers')
  assert.equal(sub.length, 3, 'should have 3 sub layers')
})

// ===== 5. breakout 为 selectionOnly =====
test('breakout 为 selectionOnly', () => {
  const breakout = CHART_LAYER_MANIFEST.find((e) => e.id === 'breakout')
  assert.ok(breakout, 'breakout entry should exist')
  assert.equal(breakout!.selectionOnly, true)
})

// ===== 6. chartLayersForSource 过滤 selectionOnly =====
test('chartLayersForSource watchlist 过滤 breakout', () => {
  const layers = chartLayersForSource(CHART_LAYER_MANIFEST, 'watchlist')
  assert.equal(layers.length, 6)
  assert.ok(!layers.find((e) => e.id === 'breakout'), 'breakout should be hidden for watchlist')
})

test('chartLayersForSource selection 包含 breakout', () => {
  const layers = chartLayersForSource(CHART_LAYER_MANIFEST, 'selection')
  assert.equal(layers.length, 7)
  assert.ok(layers.find((e) => e.id === 'breakout'), 'breakout should be visible for selection')
})

// ===== 7. loadChartLayerVisibility 空存储返回默认值 =====
test('loadChartLayerVisibility 空存储返回默认值', () => {
  globalThis.localStorage = createLocalStorageMock()
  const result = loadChartLayerVisibility('watchlist', 'watchlist_monitor')
  const defaults = defaultChartLayerVisibility('watchlist')
  assert.deepEqual(result, defaults)
})

// ===== 8. save/load 往返一致 =====
test('saveChartLayerVisibility/loadChartLayerVisibility 往返一致', () => {
  globalThis.localStorage = createLocalStorageMock()
  const custom = { ...defaultChartLayerVisibility('watchlist'), trend: true, macd: false }
  saveChartLayerVisibility('watchlist', 'watchlist_monitor', custom)
  const loaded = loadChartLayerVisibility('watchlist', 'watchlist_monitor')
  assert.deepEqual(loaded, custom)
})

// ===== 9. 旧 chart key 迁移（detail-chart-strategy-groups-v3 → 新 key） =====
test('旧 chart key 迁移：12 键 → 7 键', () => {
  globalThis.localStorage = createLocalStorageMock()
  // 写入旧 chart key（12 键 LayerVisibility）
  globalThis.localStorage.setItem(
    'detail-chart-strategy-groups-v3:watchlist:watchlist_monitor',
    JSON.stringify({ volume: false, dsa: true, macd: true, breakout: false, selection: true, node: false, poc: true, profile: false, bb: true, delta: false, events: true, sqzmom: true }),
  )
  const loaded = loadChartLayerVisibility('watchlist', 'watchlist_monitor')
  // dsa=true || selection=true → trend=true
  assert.equal(loaded.trend, true, 'trend should be true from dsa||selection')
  // node=false || profile=false || poc=true → node=true
  assert.equal(loaded.node, true, 'node should be true from poc')
  assert.equal(loaded.boll, true, 'boll should be true from bb')
  assert.equal(loaded.volume, false, 'volume should be false')
  assert.equal(loaded.macd, true, 'macd should be true')
  assert.equal(loaded.sqzmom, true, 'sqzmom should be true')
})

// ===== 10. 旧 toolbar key 迁移（panji:indicator-visibility:v1 → 新 key） =====
test('旧 toolbar key 迁移：4 键 → 7 键', () => {
  globalThis.localStorage = createLocalStorageMock()
  // 写入旧 toolbar key（4 键 IndicatorVisibility）
  globalThis.localStorage.setItem(
    'panji:indicator-visibility:v1',
    JSON.stringify({ version: 1, visibility: { price_structure: true, boll: true, volume: false, macd: true } }),
  )
  const loaded = loadChartLayerVisibility('watchlist', 'watchlist_monitor')
  // price_structure=true → trend=true
  assert.equal(loaded.trend, true, 'trend should be true from price_structure')
  // node 不在旧 toolbar 中，使用默认值
  assert.equal(loaded.node, true, 'node should use default (true for watchlist)')
  assert.equal(loaded.boll, true, 'boll should be true')
  assert.equal(loaded.volume, false, 'volume should be false')
  assert.equal(loaded.macd, true, 'macd should be true')
  // sqzmom 不在旧 toolbar 中，使用默认值
  assert.equal(loaded.sqzmom, false, 'sqzmom should use default (false)')
})
