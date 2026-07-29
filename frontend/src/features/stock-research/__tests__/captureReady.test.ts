// [CHANGE-20260728-010 P0 修复] Capture 组合视图 Ready 纯函数单元测试
//
// 用法：node --experimental-strip-types --test src/features/stock-research/__tests__/captureReady.test.ts
//   本地 Node 20.10 不支持 --experimental-strip-types，本测试集仅在 CI 环境运行（Node 22.6+）。
//
// 覆盖场景：
//   a. 空 events/order_blocks + swing_bias=0 应 Ready（SMC 结构存在但无事件）
//   b. swing_bias=1 应 Ready
//   c. swing_bias=-1 应 Ready
//   d. 缺 SMC 应 false
//   e. 缺 Node 应 false
//   f. swing_bias 类型错误（数组）应 false（P0 根因回归）
//   g. swing_bias 为 NaN 应 false
//   h. params 为 null 应 false
//   i. indicators 为 undefined 应 false
//   j. 兼容旧命名空间 watchlist_monitor / volume_node_monitor

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { computeCombinedReady } from '../captureReady.ts'
import type { IndicatorResponse } from '@/api/endpoints'

/** 构造 Node Ready 的 indicators 响应（仅用于测试） */
function makeNodeReadyIndicator(nodeKey: string = 'node_cluster'): IndicatorResponse {
  return {
    layers: [],
    data: {
      [nodeKey]: {
        profile_rows: [{ price: 10.0, volume: 1000 }],
        node_regions_hash: 'abc123',
        node_regions: [],
      },
    },
  } as unknown as IndicatorResponse
}

/** 合并 SMC 字段到 indicators.data */
function withSmc(indicators: IndicatorResponse, smc: Record<string, unknown>): IndicatorResponse {
  const data = { ...(indicators.data as Record<string, unknown>), smc }
  return { ...indicators, data } as unknown as IndicatorResponse
}

test('a. 空 events/order_blocks + swing_bias=0 应 Ready（SMC 结构存在但无事件）', () => {
  const indicators = withSmc(makeNodeReadyIndicator(), {
    events: [],
    order_blocks: [],
    swing_bias: 0,
    params: { swings_length: 50 },
  })
  assert.equal(computeCombinedReady(indicators), true)
})

test('b. swing_bias=1 应 Ready', () => {
  const indicators = withSmc(makeNodeReadyIndicator(), {
    events: [{ type: 'BOS' }],
    order_blocks: [],
    swing_bias: 1,
    params: { swings_length: 50 },
  })
  assert.equal(computeCombinedReady(indicators), true)
})

test('c. swing_bias=-1 应 Ready', () => {
  const indicators = withSmc(makeNodeReadyIndicator(), {
    events: [],
    order_blocks: [{ high: 11.0, low: 10.5 }],
    swing_bias: -1,
    params: { swings_length: 50 },
  })
  assert.equal(computeCombinedReady(indicators), true)
})

test('d. 缺 SMC 应 false', () => {
  const indicators = makeNodeReadyIndicator()
  assert.equal(computeCombinedReady(indicators), false)
})

test('e. 缺 Node 应 false', () => {
  const indicators = withSmc(
    { layers: [], data: {} } as unknown as IndicatorResponse,
    {
      events: [],
      order_blocks: [],
      swing_bias: 0,
      params: { swings_length: 50 },
    },
  )
  assert.equal(computeCombinedReady(indicators), false)
})

test('f. swing_bias 类型错误（数组）应 false（P0 根因回归）', () => {
  // 旧错误实现要求 Array.isArray(swing_bias)，导致组合截图永远无法 Ready
  // 正确实现：swing_bias 必须是 number
  const indicators = withSmc(makeNodeReadyIndicator(), {
    events: [],
    order_blocks: [],
    swing_bias: [1] as unknown as number, // 错误类型：数组而非 number
    params: { swings_length: 50 },
  })
  assert.equal(computeCombinedReady(indicators), false)
})

test('g. swing_bias 为 NaN 应 false', () => {
  const indicators = withSmc(makeNodeReadyIndicator(), {
    events: [],
    order_blocks: [],
    swing_bias: Number.NaN,
    params: { swings_length: 50 },
  })
  assert.equal(computeCombinedReady(indicators), false)
})

test('h. params 为 null 应 false', () => {
  const indicators = withSmc(makeNodeReadyIndicator(), {
    events: [],
    order_blocks: [],
    swing_bias: 0,
    params: null,
  })
  assert.equal(computeCombinedReady(indicators), false)
})

test('i. indicators 为 undefined 应 false', () => {
  assert.equal(computeCombinedReady(undefined), false)
})

test('j. 兼容旧命名空间 watchlist_monitor', () => {
  const indicators = withSmc(makeNodeReadyIndicator('watchlist_monitor'), {
    events: [],
    order_blocks: [],
    swing_bias: 0,
    params: { swings_length: 50 },
  })
  assert.equal(computeCombinedReady(indicators), true)
})

test('k. 兼容旧命名空间 volume_node_monitor', () => {
  const indicators = withSmc(makeNodeReadyIndicator('volume_node_monitor'), {
    events: [],
    order_blocks: [],
    swing_bias: 0,
    params: { swings_length: 50 },
  })
  assert.equal(computeCombinedReady(indicators), true)
})

test('l. profile_rows 为空数组应 false', () => {
  const indicators = withSmc(
    {
      layers: [],
      data: {
        node_cluster: {
          profile_rows: [],
          node_regions_hash: 'abc123',
          node_regions: [],
        },
      },
    } as unknown as IndicatorResponse,
    {
      events: [],
      order_blocks: [],
      swing_bias: 0,
      params: { swings_length: 50 },
    },
  )
  assert.equal(computeCombinedReady(indicators), false)
})

test('m. node_regions_hash 和 profile_hash 均为空字符串应 false', () => {
  const indicators = withSmc(
    {
      layers: [],
      data: {
        node_cluster: {
          profile_rows: [{ price: 10.0 }],
          node_regions_hash: '',
          profile_hash: '',
          node_regions: [],
        },
      },
    } as unknown as IndicatorResponse,
    {
      events: [],
      order_blocks: [],
      swing_bias: 0,
      params: { swings_length: 50 },
    },
  )
  assert.equal(computeCombinedReady(indicators), false)
})
