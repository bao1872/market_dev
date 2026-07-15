// [ChartLayerManifest] - 描述: 图表图层 manifest 与偏好持久化契约测试（单一真源 v2）
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/indicatorManifest.test.ts
//
// 覆盖：
//  1. CHART_LAYER_MANIFEST 包含 8 个条目
//  2. manifest 条目字段完整（id/name/kind/enabled/description）
//  3. 默认可见性：watchlist → node/boll/volume=true, macd/smc=false；selection → trend/volume=true, macd/smc=false
//  4. 主图/副图分组正确（主图 5 + 副图 3）
//  5. breakout 为 selectionOnly
//  6. chartLayersForSource 过滤 selectionOnly
//  7. loadChartLayerVisibility 空存储返回默认值
//  8. saveChartLayerVisibility/loadChartLayerVisibility 往返一致
//  9. 旧 key 迁移（detail-chart-strategy-groups-v3 → 新 key）
// 10. 旧 key 迁移（panji:indicator-visibility:v1 → 新 key）
// 11. 用户文案：sqzmom/node/smc
// 12. 内部 key 不变
// 13. [CHANGE-011 SMC] smc 默认关闭，include_smc=false 不计算 SMC

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

// ===== 1. manifest 包含 8 个条目（CHANGE-011: 新增 smc，7→8）=====
test('CHART_LAYER_MANIFEST 包含 8 个条目', () => {
  assert.equal(CHART_LAYER_MANIFEST.length, 8)
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
// P0-6: MACD 是辅助技术指标，watchlist/selection 默认均关闭
// [CHANGE-011 SMC] smc 默认关闭，不开启时后端不计算 SMC
test('默认可见性：watchlist → node/boll/volume=true, macd/smc=false', () => {
  const vis = defaultChartLayerVisibility('watchlist')
  assert.equal(vis.trend, false)
  assert.equal(vis.node, true)
  assert.equal(vis.boll, true)
  assert.equal(vis.volume, true)
  assert.equal(vis.macd, false)
  assert.equal(vis.sqzmom, false)
  assert.equal(vis.breakout, false)
  assert.equal(vis.smc, false)
})

test('默认可见性：selection → trend/volume=true, macd/smc=false', () => {
  const vis = defaultChartLayerVisibility('selection')
  assert.equal(vis.trend, true)
  assert.equal(vis.node, false)
  assert.equal(vis.boll, false)
  assert.equal(vis.volume, true)
  assert.equal(vis.macd, false)
  assert.equal(vis.sqzmom, false)
  assert.equal(vis.breakout, false)
  assert.equal(vis.smc, false)
})

// ===== 4. 主图/副图分组 =====
// [CHANGE-011 SMC] smc 是主图，主图 4→5，副图仍 3
test('主图 5 个 + 副图 3 个', () => {
  const main = CHART_LAYER_MANIFEST.filter((e) => e.kind === 'main')
  const sub = CHART_LAYER_MANIFEST.filter((e) => e.kind === 'sub')
  assert.equal(main.length, 5, 'should have 5 main layers (trend/node/boll/breakout/smc)')
  assert.equal(sub.length, 3, 'should have 3 sub layers (volume/macd/sqzmom)')
})

// ===== 5. breakout 为 selectionOnly =====
test('breakout 为 selectionOnly', () => {
  const breakout = CHART_LAYER_MANIFEST.find((e) => e.id === 'breakout')
  assert.ok(breakout, 'breakout entry should exist')
  assert.equal(breakout!.selectionOnly, true)
})

// ===== 6. chartLayersForSource 过滤 selectionOnly =====
// [CHANGE-011 SMC] watchlist 过滤 breakout 后剩 7 个（8-1）
test('chartLayersForSource watchlist 过滤 breakout', () => {
  const layers = chartLayersForSource(CHART_LAYER_MANIFEST, 'watchlist')
  assert.equal(layers.length, 7)
  assert.ok(!layers.find((e) => e.id === 'breakout'), 'breakout should be hidden for watchlist')
  // smc 应在 watchlist 中可见（开关存在，但默认关闭）
  assert.ok(layers.find((e) => e.id === 'smc'), 'smc should be visible for watchlist')
})

// [CHANGE-011 SMC] selection 包含 breakout 共 8 个
test('chartLayersForSource selection 包含 breakout', () => {
  const layers = chartLayersForSource(CHART_LAYER_MANIFEST, 'selection')
  assert.equal(layers.length, 8)
  assert.ok(layers.find((e) => e.id === 'breakout'), 'breakout should be visible for selection')
  assert.ok(layers.find((e) => e.id === 'smc'), 'smc should be visible for selection')
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
test('旧 chart key 迁移：12 键 → 8 键', () => {
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
  // [CHANGE-011 SMC] smc 不在旧 chart key 中，使用默认值 false
  assert.equal(loaded.smc, false, 'smc should use default (false)')
})

// ===== 10. 旧 toolbar key 迁移（panji:indicator-visibility:v1 → 新 key） =====
test('旧 toolbar key 迁移：4 键 → 8 键', () => {
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
  // [CHANGE-011 SMC] smc 不在旧 toolbar 中，使用默认值 false
  assert.equal(loaded.smc, false, 'smc should use default (false)')
})

// ===== 11. 用户文案：sqzmom 显示为"挤压动量"，node 显示为"筹码共识价"，smc 显示为"智能资金" =====
// [文案契约] - 描述: 仅改用户可见文案，不改内部 id/DTO/算法
// sqzmom 内部 key 不变，但 manifest.name 必须为"挤压动量"
// node 内部 key 不变，但 manifest.name 必须为"筹码共识价"
// smc 内部 key 不变，但 manifest.name 必须为"智能资金"
test('manifest 用户文案：sqzmom → "挤压动量"，node → "筹码共识价"，smc → "智能资金"', () => {
  const sqzmom = CHART_LAYER_MANIFEST.find((e) => e.id === 'sqzmom')
  assert.ok(sqzmom, '必须存在 sqzmom 条目')
  assert.equal(sqzmom!.name, '挤压动量', 'sqzmom manifest.name 必须为"挤压动量"')
  assert.ok(
    sqzmom!.description.includes('波动收窄'),
    'sqzmom description 应包含"波动收窄"（tooltip: 波动收窄后的方向与强弱）',
  )

  const node = CHART_LAYER_MANIFEST.find((e) => e.id === 'node')
  assert.ok(node, '必须存在 node 条目')
  assert.equal(node!.name, '筹码共识价', 'node manifest.name 必须为"筹码共识价"')
  assert.ok(
    node!.description.includes('估算代理'),
    'node description 应注明"估算代理"（非股东真实持仓成本）',
  )

  // [CHANGE-011 SMC] smc 文案契约
  const smc = CHART_LAYER_MANIFEST.find((e) => e.id === 'smc')
  assert.ok(smc, '必须存在 smc 条目')
  assert.equal(smc!.name, '智能资金', 'smc manifest.name 必须为"智能资金"')
  assert.ok(
    smc!.description.includes('FVG'),
    'smc description 应注明"完全排除 FVG"（FVG 完全排除契约）',
  )
})

// ===== 12. 内部 key 不变（sqzmom/node/smc 字段名保留） =====
test('内部 ChartLayerKey 不变：sqzmom/node/smc 仍为内部 id', () => {
  const ids = CHART_LAYER_MANIFEST.map((e) => e.id)
  assert.ok(ids.includes('sqzmom'), '内部 id "sqzmom" 必须保留（不改 DTO/算法）')
  assert.ok(ids.includes('node'), '内部 id "node" 必须保留（不改 DTO/算法）')
  assert.ok(ids.includes('smc'), '内部 id "smc" 必须保留（CHANGE-011 SMC）')
  // 不应出现"成交量节点"或"SQZMOM"作为 name（已改为中文文案）
  for (const entry of CHART_LAYER_MANIFEST) {
    assert.ok(
      entry.name !== 'SQZMOM' && entry.name !== '成交量节点',
      `不应保留旧文案 "${entry.name}"（id=${entry.id}）`,
    )
  }
})

// ===== 13. [CHANGE-011 SMC] smc 默认关闭契约 =====
test('[CHANGE-011 SMC] smc 默认关闭（watchlist 和 selection 均 false）', () => {
  const watchlistVis = defaultChartLayerVisibility('watchlist')
  assert.equal(watchlistVis.smc, false, 'watchlist smc 默认应为 false')
  const selectionVis = defaultChartLayerVisibility('selection')
  assert.equal(selectionVis.smc, false, 'selection smc 默认应为 false')
  // smc 不是 selectionOnly（watchlist 和 selection 都能切换）
  const smcEntry = CHART_LAYER_MANIFEST.find((e) => e.id === 'smc')
  assert.ok(smcEntry, 'smc entry should exist')
  assert.notEqual(smcEntry!.selectionOnly, true, 'smc 不应为 selectionOnly')
})
