// [ChartRenderFrame Test] - 描述: 周期切换原子渲染门禁 + 纵轴 domain policy 单元测试
// 用法：node --experimental-strip-types --test src/utils/__tests__/chartRenderFrame.test.ts
//
// 覆盖：
//   1. computeSourceBarRangeKey: 首末时间拼接 / 空数组 / null
//   2. buildBarsFrame / buildIndicatorsFrame: 字段提取 / null 处理
//   3. isFrameMatched: 一致 / 各字段不一致 / null 处理 / hash 降级
//   4. computeVisiblePriceBounds: 区间 + 容差 / 空 display / range=0
//   5. shouldIncludeNodeInPriceRange: 相交 / 不相交 / 边界 / bounds=null 降级
//   6. shouldIncludeSmcTrailingInPriceRange: 同上策略
//
// 修复根因（PROMPT.md §五.255-307）：
//   - drawTrading 之前直接 push 全部 Node lo/hi，远端 Node 把纵轴拉大
//   - Bars/Indicators 没有原子切换门禁，短暂出现"新K线 + 旧指标"

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  buildBarsFrame,
  buildIndicatorsFrame,
  computeSourceBarRangeKey,
  computeVisiblePriceBounds,
  isFrameMatched,
  shouldIncludeNodeInPriceRange,
  shouldIncludeSmcTrailingInPriceRange,
} from '../chartRenderFrame.ts'

// ===== 1. computeSourceBarRangeKey =====

test('computeSourceBarRangeKey: 首末时间拼接（多元素）', () => {
  const key = computeSourceBarRangeKey(['2026-07-01', '2026-07-02', '2026-07-03'])
  assert.equal(key, '2026-07-01|2026-07-03')
})

test('computeSourceBarRangeKey: 单元素时首末相同', () => {
  const key = computeSourceBarRangeKey(['2026-07-01'])
  assert.equal(key, '2026-07-01|2026-07-01')
})

test('computeSourceBarRangeKey: 空数组返回 null', () => {
  assert.equal(computeSourceBarRangeKey([]), null)
})

test('computeSourceBarRangeKey: null/undefined 返回 null', () => {
  assert.equal(computeSourceBarRangeKey(null), null)
  assert.equal(computeSourceBarRangeKey(undefined), null)
})

test('computeSourceBarRangeKey: 含空字符串元素返回 null', () => {
  // 防御性：后端误返回 ['', '2026-07-01'] 时不应构造无效 key
  assert.equal(computeSourceBarRangeKey(['', '2026-07-01']), null)
})

// ===== 2. buildBarsFrame / buildIndicatorsFrame =====

test('buildBarsFrame: 完整字段构造帧', () => {
  const frame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'abc123',
    marketDataContractVersion: 'v2',
    barTimes: ['2026-07-01', '2026-07-02', '2026-07-03'],
  })
  assert.ok(frame)
  assert.equal(frame!.instrumentId, 'inst-001')
  assert.equal(frame!.timeframe, '1d')
  assert.equal(frame!.adj, 'qfq')
  assert.equal(frame!.sourceBarHash, 'abc123')
  assert.equal(frame!.sourceBarRangeKey, '2026-07-01|2026-07-03')
  assert.equal(frame!.marketDataContractVersion, 'v2')
})

test('buildBarsFrame: instrumentId 缺失返回 null', () => {
  const frame = buildBarsFrame({
    instrumentId: null,
    timeframe: '1d',
    adj: 'qfq',
  })
  assert.equal(frame, null)
})

test('buildBarsFrame: timeframe/adj 缺失返回 null', () => {
  assert.equal(
    buildBarsFrame({ instrumentId: 'inst-001', timeframe: '', adj: 'qfq' }),
    null,
  )
  assert.equal(
    buildBarsFrame({ instrumentId: 'inst-001', timeframe: '1d', adj: '' }),
    null,
  )
})

test('buildBarsFrame: 可选字段缺失降级为 null（不阻塞）', () => {
  const frame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
  })
  assert.ok(frame)
  assert.equal(frame!.sourceBarHash, null)
  assert.equal(frame!.sourceBarRangeKey, null)
  assert.equal(frame!.marketDataContractVersion, null)
})

test('buildIndicatorsFrame: 完整字段构造帧', () => {
  const frame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '15m',
    adj: 'qfq',
    sourceBarHash: 'def456',
    sourceBarTimes: ['2026-07-03T09:45:00', '2026-07-03T10:00:00'],
  })
  assert.ok(frame)
  assert.equal(frame!.instrumentId, 'inst-001')
  assert.equal(frame!.timeframe, '15m')
  assert.equal(frame!.sourceBarHash, 'def456')
  assert.equal(frame!.sourceBarRangeKey, '2026-07-03T09:45:00|2026-07-03T10:00:00')
})

test('buildIndicatorsFrame: instrumentId 缺失返回 null', () => {
  assert.equal(
    buildIndicatorsFrame({ instrumentId: undefined, timeframe: '1d', adj: 'qfq' }),
    null,
  )
})

// ===== 3. isFrameMatched =====

test('isFrameMatched: 两帧 null 返回 false（保护性拒绝）', () => {
  assert.equal(isFrameMatched(null, null), false)
  assert.equal(isFrameMatched(null, buildIndicatorsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
  })), false)
  assert.equal(isFrameMatched(buildBarsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
  }), null), false)
})

test('isFrameMatched: 全字段一致返回 true', () => {
  const bars = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'abc123',
    marketDataContractVersion: 'v2',
    barTimes: ['2026-07-01', '2026-07-02'],
  })
  const ind = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'abc123',
    sourceBarTimes: ['2026-07-01', '2026-07-02'],
  })
  assert.equal(isFrameMatched(bars, ind), true)
})

test('isFrameMatched: instrumentId 不一致返回 false', () => {
  const bars = buildBarsFrame({ instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq' })
  const ind = buildIndicatorsFrame({ instrumentId: 'inst-002', timeframe: '1d', adj: 'qfq' })
  assert.equal(isFrameMatched(bars, ind), false)
})

test('isFrameMatched: timeframe 不一致返回 false（核心场景：周期切换）', () => {
  // 切换周期过程中可能短暂出现：新周期 K线（1d）+ 旧周期指标（15m）
  const bars = buildBarsFrame({ instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq' })
  const ind = buildIndicatorsFrame({ instrumentId: 'inst-001', timeframe: '15m', adj: 'qfq' })
  assert.equal(isFrameMatched(bars, ind), false)
})

test('isFrameMatched: adj 不一致返回 false', () => {
  const bars = buildBarsFrame({ instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq' })
  const ind = buildIndicatorsFrame({ instrumentId: 'inst-001', timeframe: '1d', adj: 'none' })
  assert.equal(isFrameMatched(bars, ind), false)
})

test('isFrameMatched: sourceBarHash 不一致返回 false', () => {
  const bars = buildBarsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq', sourceBarHash: 'abc',
  })
  const ind = buildIndicatorsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq', sourceBarHash: 'xyz',
  })
  assert.equal(isFrameMatched(bars, ind), false)
})

test('isFrameMatched: sourceBarRangeKey 不一致返回 false（hash 缺失时降级）', () => {
  // 后端若未返回 source_bar_hash，前端降级到 range key 比对
  const bars = buildBarsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
    barTimes: ['2026-07-01', '2026-07-03'],
  })
  const ind = buildIndicatorsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
    sourceBarTimes: ['2026-07-01', '2026-07-02'],  // 末时间不同
  })
  assert.equal(isFrameMatched(bars, ind), false)
})

test('isFrameMatched: hash 缺失但 range key 一致返回 true（降级匹配）', () => {
  const bars = buildBarsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
    barTimes: ['2026-07-01', '2026-07-02'],
  })
  const ind = buildIndicatorsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
    sourceBarTimes: ['2026-07-01', '2026-07-02'],
  })
  assert.equal(isFrameMatched(bars, ind), true)
})

test('isFrameMatched: bars hash 缺失 + indicators hash 存在 + range key 一致返回 true', () => {
  // 不对称降级：bars 没返回 hash（旧后端兼容），indicators 有 hash 但 range key 一致
  const bars = buildBarsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
    barTimes: ['2026-07-01', '2026-07-02'],
  })
  const ind = buildIndicatorsFrame({
    instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq',
    sourceBarHash: 'abc',  // bars 没返回 hash，indicators 有
    sourceBarTimes: ['2026-07-01', '2026-07-02'],
  })
  assert.equal(isFrameMatched(bars, ind), true)
})

test('isFrameMatched: 两端 hash 都缺失 + range key 也缺失返回 true', () => {
  // 极端降级：两端都没有 hash/range key，只比对严格字段（instrumentId/timeframe/adj）
  const bars = buildBarsFrame({ instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq' })
  const ind = buildIndicatorsFrame({ instrumentId: 'inst-001', timeframe: '1d', adj: 'qfq' })
  assert.equal(isFrameMatched(bars, ind), true)
})

// ===== 4. computeVisiblePriceBounds =====

test('computeVisiblePriceBounds: 正常区间 + 50% 容差', () => {
  const bounds = computeVisiblePriceBounds([9, 10, 11], [12, 13, 14])
  assert.ok(bounds)
  assert.equal(bounds!.low, 9)
  assert.equal(bounds!.high, 14)
  // range = 14 - 9 = 5, tolerance = 5 * 0.5 = 2.5
  assert.equal(bounds!.lowerBound, 6.5)
  assert.equal(bounds!.upperBound, 16.5)
})

test('computeVisiblePriceBounds: 空 display 返回 null', () => {
  assert.equal(computeVisiblePriceBounds([], []), null)
  assert.equal(computeVisiblePriceBounds([], [10]), null)
  assert.equal(computeVisiblePriceBounds([10], []), null)
})

test('computeVisiblePriceBounds: range=0 时容差基于 low（避免 0 容差）', () => {
  // 所有 K线价格相同：low=high=10, range=0, 容差 = max(0, 10*0.001) = 0.01
  // 实际容差 = 0.01 * 0.5 = 0.005
  const bounds = computeVisiblePriceBounds([10, 10], [10, 10])
  assert.ok(bounds)
  assert.equal(bounds!.low, 10)
  assert.equal(bounds!.high, 10)
  // lowerBound = 10 - 0.005 = 9.995
  assert.ok(bounds!.lowerBound < 10 && bounds!.lowerBound > 9.99)
  // upperBound = 10 + 0.005 = 10.005
  assert.ok(bounds!.upperBound > 10 && bounds!.upperBound < 10.01)
})

test('computeVisiblePriceBounds: 单根 K线', () => {
  const bounds = computeVisiblePriceBounds([10], [11])
  assert.ok(bounds)
  assert.equal(bounds!.low, 10)
  assert.equal(bounds!.high, 11)
  // range = 1, tolerance = 0.5
  assert.equal(bounds!.lowerBound, 9.5)
  assert.equal(bounds!.upperBound, 11.5)
})

// ===== 5. shouldIncludeNodeInPriceRange =====

test('shouldIncludeNodeInPriceRange: Node 与可见区间相交返回 true', () => {
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  // Node [12, 14] 完全在 [5, 20] 内
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 12, hi: 14 }, bounds), true)
})

test('shouldIncludeNodeInPriceRange: Node 部分相交返回 true', () => {
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  // Node [3, 8] 与 [5, 20] 部分相交
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 3, hi: 8 }, bounds), true)
  // Node [18, 25] 与 [5, 20] 部分相交
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 18, hi: 25 }, bounds), true)
})

test('shouldIncludeNodeInPriceRange: Node 完全在容差区间下方返回 false（远端历史低位）', () => {
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  // Node [1, 3] hi=3 < lowerBound=5
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 1, hi: 3 }, bounds), false)
})

test('shouldIncludeNodeInPriceRange: Node 完全在容差区间上方返回 false（远端历史高位）', () => {
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  // Node [25, 30] lo=25 > upperBound=20
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 25, hi: 30 }, bounds), false)
})

test('shouldIncludeNodeInPriceRange: Node 恰好接触边界返回 true', () => {
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  // Node hi 恰好等于 lowerBound
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 1, hi: 5 }, bounds), true)
  // Node lo 恰好等于 upperBound
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 20, hi: 25 }, bounds), true)
})

test('shouldIncludeNodeInPriceRange: bounds=null 降级到全部纳入（保持旧行为）', () => {
  // 防御性：bounds 缺失时不应破坏现有渲染（全部 Node 纳入）
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 1, hi: 3 }, null), true)
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 100, hi: 200 }, null), true)
})

test('shouldIncludeNodeInPriceRange: Node 跨越整个可见区间返回 true', () => {
  // 大 Node 包含整个可见区间（如长期成交密集区）
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  assert.equal(shouldIncludeNodeInPriceRange({ lo: 0, hi: 100 }, bounds), true)
})

// ===== 6. shouldIncludeSmcTrailingInPriceRange =====

test('shouldIncludeSmcTrailingInPriceRange: 在容差区间内返回 true', () => {
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  assert.equal(shouldIncludeSmcTrailingInPriceRange(12, bounds), true)
  assert.equal(shouldIncludeSmcTrailingInPriceRange(5, bounds), true)
  assert.equal(shouldIncludeSmcTrailingInPriceRange(20, bounds), true)
})

test('shouldIncludeSmcTrailingInPriceRange: 在容差区间外返回 false', () => {
  const bounds = { low: 10, high: 15, lowerBound: 5, upperBound: 20 }
  assert.equal(shouldIncludeSmcTrailingInPriceRange(4, bounds), false)
  assert.equal(shouldIncludeSmcTrailingInPriceRange(21, bounds), false)
})

test('shouldIncludeSmcTrailingInPriceRange: bounds=null 降级到全部纳入', () => {
  assert.equal(shouldIncludeSmcTrailingInPriceRange(4, null), true)
  assert.equal(shouldIncludeSmcTrailingInPriceRange(1000, null), true)
})

// ===== 7. 端到端场景：周期切换短暂 mismatch =====

test('E2E: 周期切换 1d→15m 短暂出现新K线+旧指标，frame mismatch 拒绝渲染', () => {
  // 用户切换 1d → 15m：
  //   bars 已返回 15m 数据，indicators 仍是 1d 数据（React Query 并行 refetch）
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '15m',  // bars 已切换到 15m
    adj: 'qfq',
    sourceBarHash: '15m-hash',
    barTimes: ['2026-07-03T09:45:00', '2026-07-03T10:00:00'],
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',  // indicators 仍是 1d（旧数据）
    adj: 'qfq',
    sourceBarHash: '1d-hash',
    sourceBarTimes: ['2026-07-01', '2026-07-03'],
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), false)
})

test('E2E: 周期切换完成（indicators 也返回 15m）frame matched 允许渲染', () => {
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '15m',
    adj: 'qfq',
    sourceBarHash: '15m-hash',
    barTimes: ['2026-07-03T09:45:00', '2026-07-03T10:00:00'],
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '15m',
    adj: 'qfq',
    sourceBarHash: '15m-hash',
    sourceBarTimes: ['2026-07-03T09:45:00', '2026-07-03T10:00:00'],
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), true)
})

test('E2E: Node domain policy 过滤远端历史高位（PROMPT.md §五.255-282）', () => {
  // 场景：当前 K线在 10-15 区间，但后端返回了历史高位 Node [80, 85]
  //   旧行为：直接 push 80/85 → 纵轴拉到 [9, 85] → K线被压缩到很小范围
  //   新行为：shouldIncludeNodeInPriceRange 返回 false → 不纳入纵轴候选
  const bounds = computeVisiblePriceBounds([9, 10, 11], [12, 13, 14])
  assert.ok(bounds)

  // 历史高位 Node 被过滤
  const farHighNode = { lo: 80, hi: 85 }
  assert.equal(shouldIncludeNodeInPriceRange(farHighNode, bounds), false)

  // 当前区间 Node 保留
  const nearNode = { lo: 12, hi: 14 }
  assert.equal(shouldIncludeNodeInPriceRange(nearNode, bounds), true)
})