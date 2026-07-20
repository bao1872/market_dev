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
  extractDisplayFrameFields,
  isFrameMatched,
  shouldIncludeNodeInPriceRange,
  shouldIncludeSmcTrailingInPriceRange,
} from '../chartRenderFrame.ts'
import type { DisplayFrame } from '../../api/endpoints.ts'

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

// ===== 8. display_frame 匹配路径（PROMPT.md §二.1 展示帧/算法输入帧分离） =====

// 构造测试用 DisplayFrame 辅助函数
function makeDisplayFrame(overrides: Partial<DisplayFrame> = {}): DisplayFrame {
  return {
    instrument_id: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    display_times: ['2026-07-01', '2026-07-02', '2026-07-03'],
    display_hash: 'display-hash-abc',
    completed_through: '2026-07-03',
    ...overrides,
  }
}

test('extractDisplayFrameFields: 完整 display_frame 提取 hash + rangeKey', () => {
  const df = makeDisplayFrame()
  const { displayHash, displayRangeKey } = extractDisplayFrameFields(df)
  assert.equal(displayHash, 'display-hash-abc')
  assert.equal(displayRangeKey, '2026-07-01|2026-07-03')
})

test('extractDisplayFrameFields: null/undefined 返回 null/null（触发降级）', () => {
  assert.deepEqual(extractDisplayFrameFields(null), { displayHash: null, displayRangeKey: null })
  assert.deepEqual(extractDisplayFrameFields(undefined), { displayHash: null, displayRangeKey: null })
})

test('extractDisplayFrameFields: display_hash 空串视为 null（后端空 DataFrame 路径）', () => {
  const df = makeDisplayFrame({ display_hash: '' })
  const { displayHash, displayRangeKey } = extractDisplayFrameFields(df)
  assert.equal(displayHash, null)
  // display_times 仍可构造 rangeKey（但 isFrameMatched 中 displayHash=null 会触发降级）
  assert.equal(displayRangeKey, '2026-07-01|2026-07-03')
})

test('extractDisplayFrameFields: display_times 为空数组返回 null rangeKey', () => {
  const df = makeDisplayFrame({ display_times: [] })
  const { displayHash, displayRangeKey } = extractDisplayFrameFields(df)
  assert.equal(displayHash, 'display-hash-abc')
  assert.equal(displayRangeKey, null)
})

test('buildBarsFrame: display_frame 字段提取到 displayHash/displayRangeKey', () => {
  const df = makeDisplayFrame({
    display_hash: 'bars-display-hash',
    display_times: ['2026-07-01', '2026-07-05'],
  })
  const frame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    displayFrame: df,
  })
  assert.ok(frame)
  assert.equal(frame!.displayHash, 'bars-display-hash')
  assert.equal(frame!.displayRangeKey, '2026-07-01|2026-07-05')
})

test('buildIndicatorsFrame: display_frame 字段提取到 displayHash/displayRangeKey', () => {
  const df = makeDisplayFrame({
    display_hash: 'ind-display-hash',
    display_times: ['2026-07-01', '2026-07-05'],
  })
  const frame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    displayFrame: df,
  })
  assert.ok(frame)
  assert.equal(frame!.displayHash, 'ind-display-hash')
  assert.equal(frame!.displayRangeKey, '2026-07-01|2026-07-05')
})

test('isFrameMatched: 双侧 display_frame 一致返回 true（优先路径）', () => {
  // 核心修复场景：1d 周期 bars.source_bar_hash（100根展示窗口）≠ indicators.source_bar_hash
  // （250根算法输入），但 display_frame 一致 → matched
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'bars-100-bars-hash',  // 展示窗口 100 根
    barTimes: ['2026-07-01', '2026-07-05'],
    displayFrame: makeDisplayFrame({
      display_hash: 'shared-display-hash',
      display_times: ['2026-07-01', '2026-07-05'],
    }),
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'ind-250-bars-hash',  // 算法输入 250 根，不同于 bars
    sourceBarTimes: ['2026-05-01', '2026-07-05'],  // 更长范围
    displayFrame: makeDisplayFrame({
      display_hash: 'shared-display-hash',  // 同一展示窗口
      display_times: ['2026-07-01', '2026-07-05'],
    }),
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), true)
})

test('isFrameMatched: display_frame 不一致返回 false（即使 source_bar_hash 一致）', () => {
  // display_frame 优先级高于 source_bar_hash：display_hash 不匹配则 mismatch
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'shared-source-hash',
    displayFrame: makeDisplayFrame({ display_hash: 'display-A' }),
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'shared-source-hash',
    displayFrame: makeDisplayFrame({ display_hash: 'display-B' }),
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), false)
})

test('isFrameMatched: display_hash 一致但 displayRangeKey 不一致返回 false', () => {
  // 防御性：hash 相同但窗口不同（理论上不应出现，但防止 hash 碰撞）
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    displayFrame: makeDisplayFrame({
      display_hash: 'shared-hash',
      display_times: ['2026-07-01', '2026-07-05'],
    }),
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    displayFrame: makeDisplayFrame({
      display_hash: 'shared-hash',
      display_times: ['2026-07-01', '2026-07-10'],  // 末时间不同
    }),
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), false)
})

test('isFrameMatched: bars 有 display_frame，indicators 无 → mismatch（不对称）', () => {
  // API 升级过渡期：bars 已返回 display_frame，indicators 未返回 → 不应静默降级
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'abc',
    displayFrame: makeDisplayFrame({ display_hash: 'bars-display' }),
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'abc',  // source_bar_hash 一致
    sourceBarTimes: ['2026-07-01', '2026-07-05'],
    // display_frame 未传入
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), false)
})

test('isFrameMatched: 双侧 display_frame 缺失，降级到 source_bar_hash 一致返回 true', () => {
  // 向后兼容：旧后端未返回 display_frame，仍按 source_bar_hash 比对
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'shared-source-hash',
    barTimes: ['2026-07-01', '2026-07-05'],
    // display_frame 未传入
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'shared-source-hash',
    sourceBarTimes: ['2026-07-01', '2026-07-05'],
    // display_frame 未传入
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), true)
})

test('isFrameMatched: 双侧 display_frame 缺失，source_bar_hash 不一致返回 false', () => {
  // 降级路径仍严格比对 source_bar_hash
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'hash-A',
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'hash-B',
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), false)
})

test('E2E: 1d 周期 Node 算法输入 250 根，bars 展示窗口 100 根，display_frame 一致 → matched', () => {
  // PROMPT.md §二.1 核心修复场景：之前永久 mismatch，现在 display_frame 一致 → matched
  const barsFrame = buildBarsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'bars-100-hash',  // 100 根展示窗口
    barTimes: Array.from({ length: 100 }, (_, i) => `2026-${String(Math.floor(i / 30) + 1).padStart(2, '0')}-${String((i % 28) + 1).padStart(2, '0')}`),
    displayFrame: makeDisplayFrame({
      display_hash: 'display-100-bars',
      display_times: ['2026-03-01', '2026-07-05'],  // 100 根展示窗口
    }),
  })
  const indFrame = buildIndicatorsFrame({
    instrumentId: 'inst-001',
    timeframe: '1d',
    adj: 'qfq',
    sourceBarHash: 'ind-250-hash',  // 250 根算法输入（含 Node warmup）
    sourceBarTimes: ['2025-09-01', '2026-07-05'],  // 250 根范围
    displayFrame: makeDisplayFrame({
      display_hash: 'display-100-bars',  // 同一展示窗口
      display_times: ['2026-03-01', '2026-07-05'],
    }),
  })
  assert.equal(isFrameMatched(barsFrame, indFrame), true)
})