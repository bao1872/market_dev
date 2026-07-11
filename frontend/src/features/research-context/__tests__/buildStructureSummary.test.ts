// [buildStructureSummary] - 描述: 结构状态摘要纯函数契约测试
// 用法：node --experimental-strip-types --test src/features/research-context/__tests__/buildStructureSummary.test.ts
//
// 覆盖：
//   1. null 输入 → hasData=false, daily/m15/costPosition 全 null
//   2. degraded_reasons 非空 → degraded=true
//   3. warmup_notes 非空 → warmup=true
//   4. 完整 fixture → daily/m15/costPosition 全部提取
//   5. cost_position DTO 路径正确（primary[timeframe].cost_position.position_0_1）
//   6. nearest_upper_node/lower_node 为对象时提取 price_mid
//   7. primary 为空对象 → costPosition=null
//   8. primary[timeframe] 为 null → costPosition=null
//   9. 非数字字段 → null
//  10. 合并 structural + temporal 的 degraded_reasons

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { buildStructureSummary } from '../buildStructureSummary.ts'
import type {
  StructuralFactorResponse,
  TemporalFeaturesResponse,
} from '../../../api/endpoints.ts'

function makeStructuralFixture(overrides?: {
  primary?: StructuralFactorResponse['primary']
  meta?: Partial<StructuralFactorResponse['meta']>
}): StructuralFactorResponse {
  return {
    primary: overrides?.primary ?? {
      '1d': {
        cost_position: {
          position_0_1: 0.65,
          price_vs_poc_atr: 1.2,
          poc_price: 10.5,
          nearest_upper_node: { price_mid: 11.2 },
          nearest_lower_node: { price_mid: 9.8 },
        },
        swing_position: {
          active_swing_dir: 1,
          active_swing_high: 11.5,
          active_swing_low: 9.5,
        },
      },
    },
    secondary: {},
    relation: {},
    meta: {
      as_of: '2026-07-10T15:00:00+08:00',
      primary_lookback_bars: 250,
      secondary_lookback_bars: 4000,
      degraded_reasons: overrides?.meta?.degraded_reasons ?? [],
      warmup_notes: overrides?.meta?.warmup_notes ?? [],
    },
  }
}

function makeTemporalFixture(overrides?: {
  meta?: Partial<TemporalFeaturesResponse['meta']>
}): TemporalFeaturesResponse {
  return {
    daily_context: {
      daily_dsa_dir: 1,
      daily_dsa_segment_duration_percentile: 0.5,
      daily_dsa_slope_atr_per_bar: 0.05,
      daily_dsa_efficiency_0_1: 0.7,
      daily_price_position_in_swing_0_1: 0.65,
      daily_distance_to_swing_high_atr: 1.5,
      daily_distance_to_node_above_atr: 1.2,
      daily_sqzmom_change_since_segment_start: 0.1,
      daily_volume_percentile_change_since_segment_start: 0.2,
    },
    m15_response: {
      m15_price_position_in_swing_0_1: 0.5,
      m15_position_change_since_swing_anchor: 0.1,
      m15_distance_to_swing_high_atr: 1.0,
      m15_distance_to_swing_low_atr: 1.0,
      m15_sqzmom_change_since_swing_anchor: 0.05,
      m15_sqzmom_abs_percentile: 0.6,
      m15_sqz_off: false,
      m15_bb_bandwidth_change_since_swing_anchor: 0.1,
      m15_volume_percentile_change_since_swing_anchor: 0.2,
    },
    derived_relation: {
      m15_position_relative_to_daily: 0.1,
      m15_response_direction_relative_to_daily: 'up',
      m15_response_intensity: 0.6,
    },
    meta: {
      as_of: '2026-07-10T15:00:00+08:00',
      primary_timeframe: '1d',
      secondary_timeframe: '15m',
      degraded_reasons: overrides?.meta?.degraded_reasons ?? [],
      warmup_notes: overrides?.meta?.warmup_notes ?? [],
    },
  }
}

test('null 输入 → hasData=false, daily/m15/costPosition 全 null', () => {
  const summary = buildStructureSummary({ structural: null, temporal: null })
  assert.equal(summary.hasData, false)
  assert.equal(summary.daily, null)
  assert.equal(summary.m15, null)
  assert.equal(summary.costPosition, null)
  assert.equal(summary.degraded, false)
  assert.equal(summary.warmup, false)
})

test('undefined 输入 → hasData=false', () => {
  const summary = buildStructureSummary({})
  assert.equal(summary.hasData, false)
})

test('degraded_reasons 非空 → degraded=true', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture({ meta: { degraded_reasons: ['pytdx_timeout'] } }),
    temporal: makeTemporalFixture(),
  })
  assert.equal(summary.degraded, true)
  assert.ok(summary.degradedReasons.includes('pytdx_timeout'))
})

test('warmup_notes 非空 → warmup=true', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture(),
    temporal: makeTemporalFixture({ meta: { warmup_notes: ['insufficient_bars'] } }),
  })
  assert.equal(summary.warmup, true)
  assert.ok(summary.warmupNotes.includes('insufficient_bars'))
})

test('合并 structural + temporal 的 degraded_reasons', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture({ meta: { degraded_reasons: ['struct_err'] } }),
    temporal: makeTemporalFixture({ meta: { degraded_reasons: ['temporal_err'] } }),
  })
  assert.equal(summary.degraded, true)
  assert.ok(summary.degradedReasons.includes('struct_err'))
  assert.ok(summary.degradedReasons.includes('temporal_err'))
})

test('完整 fixture → daily/m15/costPosition 全部提取', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture(),
    temporal: makeTemporalFixture(),
  })
  assert.equal(summary.hasData, true)
  assert.ok(summary.daily)
  assert.ok(summary.m15)
  assert.ok(summary.costPosition)
})

test('daily 字段提取正确', () => {
  const summary = buildStructureSummary({
    structural: null,
    temporal: makeTemporalFixture(),
  })
  assert.ok(summary.daily)
  assert.equal(summary.daily!.dir, 1)
  assert.equal(summary.daily!.position, 0.65)
  assert.equal(summary.daily!.distanceToNodeAbove, 1.2)
})

test('m15 字段提取正确', () => {
  const summary = buildStructureSummary({
    structural: null,
    temporal: makeTemporalFixture(),
  })
  assert.ok(summary.m15)
  assert.equal(summary.m15!.responseDir, 'up')
  assert.equal(summary.m15!.responseIntensity, 0.6)
  assert.equal(summary.m15!.position, 0.5)
})

test('cost_position DTO 路径正确（primary[timeframe].cost_position.position_0_1）', () => {
  // 关键：position_0_1 在 cost_position 子组内，不在 timeframe 顶层
  const summary = buildStructureSummary({
    structural: makeStructuralFixture(),
    temporal: null,
  })
  assert.ok(summary.costPosition)
  assert.equal(summary.costPosition!.position, 0.65)
  assert.equal(summary.costPosition!.distanceToPoc, 1.2)
  assert.equal(summary.costPosition!.poc, 10.5)
})

test('nearest_upper_node/lower_node 为对象时提取 price_mid', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture(),
    temporal: null,
  })
  assert.ok(summary.costPosition)
  assert.equal(summary.costPosition!.upperNode, 11.2)
  assert.equal(summary.costPosition!.lowerNode, 9.8)
})

test('primary 为空对象 → costPosition=null', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture({ primary: {} }),
    temporal: null,
  })
  assert.equal(summary.costPosition, null)
})

test('primary[timeframe] 为 null → costPosition=null', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture({ primary: { '1d': null } }),
    temporal: null,
  })
  assert.equal(summary.costPosition, null)
})

test('primary[timeframe] 无 cost_position 子组 → costPosition=null', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture({
      primary: { '1d': { swing_position: { active_swing_dir: 1 } } },
    }),
    temporal: null,
  })
  assert.equal(summary.costPosition, null)
})

test('非数字字段 → null', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture({
      primary: {
        '1d': {
          cost_position: {
            position_0_1: 'invalid',
            price_vs_poc_atr: null,
            poc_price: NaN,
            nearest_upper_node: 'not_object',
            nearest_lower_node: undefined,
          },
        },
      },
    }),
    temporal: null,
  })
  assert.ok(summary.costPosition)
  assert.equal(summary.costPosition!.position, null)  // 'invalid' 非数字
  assert.equal(summary.costPosition!.distanceToPoc, null)  // null
  assert.equal(summary.costPosition!.poc, null)  // NaN
  assert.equal(summary.costPosition!.upperNode, null)  // 非对象
  assert.equal(summary.costPosition!.lowerNode, null)  // undefined
})

test('daily_dsa_dir 为字符串时保留原值', () => {
  const temporal = makeTemporalFixture()
  temporal.daily_context.daily_dsa_dir = 'up' as unknown as number
  const summary = buildStructureSummary({ structural: null, temporal })
  assert.ok(summary.daily)
  assert.equal(summary.daily!.dir, 'up')
})

test('daily_dsa_dir 为 null 时 → null', () => {
  const temporal = makeTemporalFixture()
  temporal.daily_context.daily_dsa_dir = null
  const summary = buildStructureSummary({ structural: null, temporal })
  assert.ok(summary.daily)
  assert.equal(summary.daily!.dir, null)
})

test('m15_response 缺失 derived_relation 时 → m15=null', () => {
  const temporal = makeTemporalFixture()
  temporal.derived_relation = null as unknown as TemporalFeaturesResponse['derived_relation']
  const summary = buildStructureSummary({ structural: null, temporal })
  assert.equal(summary.m15, null)
})

test('仅 structural 有数据时 hasData=true', () => {
  const summary = buildStructureSummary({
    structural: makeStructuralFixture(),
    temporal: null,
  })
  assert.equal(summary.hasData, true)
  assert.equal(summary.daily, null)
  assert.equal(summary.m15, null)
  assert.ok(summary.costPosition)
})
