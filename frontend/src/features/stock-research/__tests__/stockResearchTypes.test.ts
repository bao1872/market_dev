// [StockResearchTypes] - 描述: 股票研究共享类型与纯函数契约测试
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/stockResearchTypes.test.ts
//
// 覆盖：
//   1. ALLOWED_TIMEFRAMES 包含 5 个允许值且顺序固定
//   2. DEFAULT_TIMEFRAME = '1d'
//   3. DEFAULT_SOURCE = 'watchlist'
//   4. BARS_COUNT_BY_TIMEFRAME 与 Node Cluster 输入契约对齐（1d=250, 15m=4000, 1h=1200, 1w=260, 1mo=120）
//   5. defaultStrategyForSource：watchlist→watchlist_monitor, selection→dsa_selector
//   6. normalizeDisplayTimeframe：合法值原样返回
//   7. normalizeDisplayTimeframe：null 回退 1d
//   8. normalizeDisplayTimeframe：非法值回退 1d
//   9. normalizeDisplayTimeframe：空字符串回退 1d
//  10. normalizeResearchSource：selection 原样返回
//  11. normalizeResearchSource：null 回退 watchlist
//  12. normalizeResearchSource：非法值回退 watchlist
//  13. normalizeResearchSource：空字符串回退 watchlist

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  ALLOWED_TIMEFRAMES,
  DEFAULT_TIMEFRAME,
  DEFAULT_SOURCE,
  BARS_COUNT_BY_TIMEFRAME,
  defaultStrategyForSource,
  normalizeDisplayTimeframe,
  normalizeResearchSource,
  INDICATOR_VIEW_LAYER_PRESETS,
  INDICATOR_VIEW_LABELS,
  INDICATOR_VIEW_VALUES,
  normalizeIndicatorView,
  getIndicatorViewLayerPreset,
} from '../stockResearchTypes.ts'

test('ALLOWED_TIMEFRAMES 包含 5 个允许值且顺序固定', () => {
  assert.deepEqual([...ALLOWED_TIMEFRAMES], ['15m', '1h', '1d', '1w', '1mo'])
})

test('DEFAULT_TIMEFRAME = 1d', () => {
  assert.equal(DEFAULT_TIMEFRAME, '1d')
})

test('DEFAULT_SOURCE = watchlist', () => {
  assert.equal(DEFAULT_SOURCE, 'watchlist')
})

test('BARS_COUNT_BY_TIMEFRAME 与 Node Cluster 输入契约对齐', () => {
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1d'], 250)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['15m'], 4000)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1h'], 1200)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1w'], 260)
  assert.equal(BARS_COUNT_BY_TIMEFRAME['1mo'], 120)
})

test('defaultStrategyForSource：watchlist→watchlist_monitor, selection→dsa_selector', () => {
  assert.equal(defaultStrategyForSource('watchlist'), 'watchlist_monitor')
  assert.equal(defaultStrategyForSource('selection'), 'dsa_selector')
})

test('normalizeDisplayTimeframe：合法值原样返回', () => {
  for (const tf of ALLOWED_TIMEFRAMES) {
    assert.equal(normalizeDisplayTimeframe(tf), tf)
  }
})

test('normalizeDisplayTimeframe：null 回退 1d', () => {
  assert.equal(normalizeDisplayTimeframe(null), '1d')
})

test('normalizeDisplayTimeframe：非法值回退 1d', () => {
  assert.equal(normalizeDisplayTimeframe('5min'), '1d')
  assert.equal(normalizeDisplayTimeframe('2d'), '1d')
  assert.equal(normalizeDisplayTimeframe('daily'), '1d')
})

test('normalizeDisplayTimeframe：空字符串回退 1d', () => {
  assert.equal(normalizeDisplayTimeframe(''), '1d')
})

test('normalizeResearchSource：selection 原样返回', () => {
  assert.equal(normalizeResearchSource('selection'), 'selection')
})

test('normalizeResearchSource：null 回退 watchlist', () => {
  assert.equal(normalizeResearchSource(null), 'watchlist')
})

test('normalizeResearchSource：非法值回退 watchlist', () => {
  assert.equal(normalizeResearchSource('invalid'), 'watchlist')
  assert.equal(normalizeResearchSource('watchlist'), 'watchlist')
})

test('normalizeResearchSource：空字符串回退 watchlist', () => {
  assert.equal(normalizeResearchSource(''), 'watchlist')
})

// ===== [CHANGE-20260720-Phase4 §四] indicator_view 预设测试 =====

test('INDICATOR_VIEW_VALUES 包含 3 个允许值且顺序固定', () => {
  assert.deepEqual([...INDICATOR_VIEW_VALUES], ['node_cluster', 'bollinger', 'smc'])
})

test('INDICATOR_VIEW_LABELS：三个视图对应中文文案', () => {
  assert.equal(INDICATOR_VIEW_LABELS.node_cluster, '筹码共识价')
  assert.equal(INDICATOR_VIEW_LABELS.bollinger, '布林带')
  assert.equal(INDICATOR_VIEW_LABELS.smc, '结构')
})

test('INDICATOR_VIEW_LAYER_PRESETS：node_cluster 只开启 node + volume', () => {
  const preset = INDICATOR_VIEW_LAYER_PRESETS.node_cluster
  assert.equal(preset.node, true, 'node_cluster 必须开启 node')
  assert.equal(preset.volume, true, '主图基线 volume 始终开启')
  assert.equal(preset.boll, false, 'node_cluster 不应开启 boll')
  assert.equal(preset.smc, false, 'node_cluster 不应开启 smc')
  assert.equal(preset.trend, false, '监控截图场景 trend 关闭')
  assert.equal(preset.macd, false, '副图 macd 关闭以节省垂直空间')
  assert.equal(preset.sqzmom, false, '副图 sqzmom 关闭以节省垂直空间')
  assert.equal(preset.breakout, false, 'breakout 是 selection 专属，监控场景关闭')
})

test('INDICATOR_VIEW_LAYER_PRESETS：bollinger 只开启 boll + volume', () => {
  const preset = INDICATOR_VIEW_LAYER_PRESETS.bollinger
  assert.equal(preset.boll, true, 'bollinger 必须开启 boll')
  assert.equal(preset.volume, true, '主图基线 volume 始终开启')
  assert.equal(preset.node, false, 'bollinger 不应开启 node')
  assert.equal(preset.smc, false, 'bollinger 不应开启 smc')
  assert.equal(preset.trend, false)
  assert.equal(preset.macd, false)
  assert.equal(preset.sqzmom, false)
  assert.equal(preset.breakout, false)
})

test('INDICATOR_VIEW_LAYER_PRESETS：smc 只开启 smc + volume', () => {
  const preset = INDICATOR_VIEW_LAYER_PRESETS.smc
  assert.equal(preset.smc, true, 'smc 必须开启 smc')
  assert.equal(preset.volume, true, '主图基线 volume 始终开启')
  assert.equal(preset.node, false, 'smc 不应开启 node')
  assert.equal(preset.boll, false, 'smc 不应开启 boll')
  assert.equal(preset.trend, false)
  assert.equal(preset.macd, false)
  assert.equal(preset.sqzmom, false)
  assert.equal(preset.breakout, false)
})

test('INDICATOR_VIEW_LAYER_PRESETS：三个视图互斥（每个视图只开启一个主图指标）', () => {
  // 核心约束：每张截图只渲染一个 indicator_view 对应的图层
  // node_cluster 开 node，bollinger 开 boll，smc 开 smc；三者不可同时为 true
  const nodePreset = INDICATOR_VIEW_LAYER_PRESETS.node_cluster
  const bollPreset = INDICATOR_VIEW_LAYER_PRESETS.bollinger
  const smcPreset = INDICATOR_VIEW_LAYER_PRESETS.smc

  // node_cluster 与 bollinger 不应同时开 node/boll
  assert.equal(nodePreset.node && bollPreset.node, false)
  assert.equal(nodePreset.boll && bollPreset.boll, false)
  // smc 与其他两个不应同时开 smc/node/boll
  assert.equal(smcPreset.smc && nodePreset.smc, false)
  assert.equal(smcPreset.smc && bollPreset.smc, false)
  assert.equal(smcPreset.node && nodePreset.node, false)
  assert.equal(smcPreset.boll && bollPreset.boll, false)
})

test('normalizeIndicatorView：合法值原样返回', () => {
  assert.equal(normalizeIndicatorView('node_cluster'), 'node_cluster')
  assert.equal(normalizeIndicatorView('bollinger'), 'bollinger')
  assert.equal(normalizeIndicatorView('smc'), 'smc')
})

test('normalizeIndicatorView：null 返回 null', () => {
  assert.equal(normalizeIndicatorView(null), null)
})

test('normalizeIndicatorView：空字符串返回 null', () => {
  assert.equal(normalizeIndicatorView(''), null)
})

test('normalizeIndicatorView：非法值返回 null', () => {
  assert.equal(normalizeIndicatorView('invalid'), null)
  assert.equal(normalizeIndicatorView('NODE_CLUSTER'), null) // 大小写敏感
  assert.equal(normalizeIndicatorView('fvg'), null) // FVG 已完全排除
})

test('getIndicatorViewLayerPreset：返回与直接索引相同的结果', () => {
  assert.deepEqual(
    getIndicatorViewLayerPreset('node_cluster'),
    INDICATOR_VIEW_LAYER_PRESETS.node_cluster,
  )
  assert.deepEqual(
    getIndicatorViewLayerPreset('bollinger'),
    INDICATOR_VIEW_LAYER_PRESETS.bollinger,
  )
  assert.deepEqual(
    getIndicatorViewLayerPreset('smc'),
    INDICATOR_VIEW_LAYER_PRESETS.smc,
  )
})
