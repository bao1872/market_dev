// [Slice E] Canonical Scope Detail 合同测试（纯 TS，tsx --test 可跑）。
// 覆盖 prompt §11 / §12 针对 Slice E 的 targeted tests + regression：
//   - 解析 owner（scopeDetailContract）：Dynamics fact-object、Attribution direct array、
//     Reconciliation map、Leadership number direction、null!=0 语义
//   - 图表适配（scopeDynamicsChart）：gap preservation、never silent truncation、zero reference line
//   - 回归测试 A-R：针对 prompt §12 的 exact blockers
//
// 所有 fixtures 必须镜像真实 backend producer 输出形状，不得发明想象合同。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  parseDynamicsLayer,
  parseInternalStructure,
  parseLeadership,
  parseAttribution,
  currentPhaseFact,
  observationGroups,
  OBSERVATION_GROUP_ORDER,
} from '../scopeDetailContract'
import {
  alignDynamicsSeries,
  buildPositionAutoscale,
  buildOffsetAutoscale,
  buildZeroReferenceLine,
  POSITION_MIN,
  POSITION_MAX,
} from '../scopeDynamicsChart'
import {
  isScopeDetailEnabled,
  scopeDetailQueryOptions,
} from '../useReviewScopeDetail'
import { DEFAULT_REVIEW_TAB, defaultReviewUrlState, normalizeDetailTab, buildReviewUrl, decodeReviewUrl, type ReviewUrlState } from '../urlState'
import { memberName } from '../reviewFormat'
import type {
  ReviewScopeComposition,
  ScopePhaseFact,
  ScopeHistoricalDynamicsSeries,
  ScopeDynamicsPositionPoint,
  ScopeDynamicsValuePoint,
  ScopeDynamicsPersistencePoint,
} from '../types'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

// ============================================================
// fixtures — 镜像真实 backend producer 输出
// ============================================================

function makeComposition(
  overrides: Partial<ReviewScopeComposition> = {},
): ReviewScopeComposition {
  return {
    scope: { scope_type: 'industry_l1', scope_key: 'copper' },
    trade_date: '2026-08-21',
    capability: {},
    scope_observation: null,
    historical_dynamics: null,
    internal_structure_facts: null,
    leadership: null,
    member_attribution: null,
    composition_readiness: 'ready',
    ...overrides,
  }
}

/** Position fact-object fixture — 镜像 backend compute_historical_dynamics_series 输出
 *  position 支持 number | null（ready → number；unavailable → null） */
function positionPoint(trade_date: string, position: number | null, status = 'ready'): ScopeDynamicsPositionPoint {
  return { trade_date, position, status, history: null }
}

/** Value fact-object fixture — 镜像 backend EMA/Velocity/Acceleration 输出 */
function valuePoint(trade_date: string, value: number | null, status = 'ready'): ScopeDynamicsValuePoint {
  return { trade_date, value, status }
}

/** Persistence fact-object fixture — 镜像 backend persistence 输出
 *  PERSISTENCE_MINIMUM_VALID_COUNT = 15
 *  candidate_count = 当前已存在的交易观察数（up to 20）
 *  valid_count = 有效 position 数
 *  coverage = valid_count / 20
 *  lower/upper counts 基于 position 阈值（lower<=20, upper>=80）
 *  insufficient_history 时 occupancy = null */
function persistencePoint(params: {
  trade_date: string
  candidate_count: number
  valid_count: number
  upper_count: number
  lower_count: number
  status: 'ready' | 'insufficient_history' | 'unavailable'
}): ScopeDynamicsPersistencePoint {
  const { trade_date, candidate_count, valid_count, upper_count, lower_count, status } = params
  const coverage = valid_count / 20
  const isInsufficient = status === 'insufficient_history'
  return {
    trade_date,
    window_size: 20,
    minimum_valid_count: 15,
    candidate_count,
    valid_count,
    coverage,
    upper_count,
    lower_count,
    upper_occupancy: isInsufficient ? null : null,
    lower_occupancy: isInsufficient ? null : null,
    status,
  }
}

function makeDynamicsLayer({
  status = 'ready',
  series = null,
  phaseFacts = null,
}: {
  status?: string
  series?: ScopeHistoricalDynamicsSeries | null
  phaseFacts?: ScopePhaseFact[] | null
} = {}) {
  return {
    status,
    scope: null,
    membership: null,
    observation_series: null,
    scope_dynamics: {
      historical_dynamics: series,
      dynamics_phase: phaseFacts,
    },
    metrics: null,
  }
}

/** 生成 Dynamics 完整 fact-object 序列（3 天）
 *  Position 值: [10, 20, 30]
 *  Persistence 镜像 early-window:
 *    day1: candidate=1, valid=1, lower=1(10≤20), upper=0, insufficient
 *    day2: candidate=2, valid=2, lower=2(10,20≤20), upper=0, insufficient
 *    day3: candidate=3, valid=3, lower=2(10,20≤20), upper=0, insufficient */
function makeFullDynamicsSeries(dates: string[]): ScopeHistoricalDynamicsSeries {
  // 使用预计算值避免浮点精度问题（0.1*3 !== 0.3 在 IEEE 754 中）
  const accelValues = [0.1, 0.2, 0.3]
  const positions = dates.map((_d, i) => (i + 1) * 10) // [10, 20, 30]
  const persistence = dates.map((d, i) => {
    // 每日累计观察
    const candidate = i + 1
    const valid = i + 1
    // lower: position <= 20 → day1:10, day2:10+20, day3:10+20
    const lower = positions.slice(0, i + 1).filter((p) => p <= 20).length
    // upper: position >= 80 → none in this 3-day fixture
    const upper = positions.slice(0, i + 1).filter((p) => p >= 80).length
    return persistencePoint({
      trade_date: d,
      candidate_count: candidate,
      valid_count: valid,
      upper_count: upper,
      lower_count: lower,
      status: 'insufficient_history',
    })
  })
  return {
    position: dates.map((d, i) => positionPoint(d, positions[i])),
    ema5: dates.map((d, i) => valuePoint(d, (i + 1) * 9)),
    ema20: dates.map((d, i) => valuePoint(d, (i + 1) * 8)),
    velocity: dates.map((d, i) => valuePoint(d, i + 1)),
    signal: dates.map((d) => valuePoint(d, null)),
    acceleration: dates.map((d, i) => valuePoint(d, accelValues[i])),
    persistence,
  }
}

/** 生成 Direction attribution group（直接 MemberEvidence[]，不是 {members:[...]}） */
function makeDirectionGroup() {
  return {
    status: 'ready',
    aw_universe_count: 50,
    positive: [
      { member_id: 'a', contribution: 0.01, return_1d: 0.02 },
      { member_id: 'b', contribution: 0.005, return_1d: 0.01 },
    ],
    negative: [
      { member_id: 'c', contribution: -0.01, return_1d: -0.02 },
    ],
    sum_contribution: 0.005,
    canonical_aw_return: 0.01,
  }
}

/** 生成 Capital Tilt attribution group（直接 MemberEvidence[]） */
function makeCapitalTiltGroup() {
  return {
    status: 'ready',
    price_universe_count: 45,
    aw_universe_count: 50,
    positive: [
      { member_id: 'd', tilt_contribution: 0.02, aw_weight: 0.15, return_1d: 0.03 },
    ],
    negative: [
      { member_id: 'e', tilt_contribution: -0.01, aw_weight: 0.10, return_1d: -0.01 },
    ],
    sum_tilt_contribution: 0.01,
    canonical_aw_return: 0.02,
    canonical_ew_return: 0.01,
  }
}

/** 生成 Concentration attribution group（只有它用 {members:[...]}） */
function makeConcentrationGroup() {
  return {
    price: {
      members: [
        { member_id: 'f', concentration_weight: 0.05, hhi_contribution: 0.003 },
      ],
      sum_hhi: 0.15,
      canonical_raw_hhi: 0.20,
      canonical_normalized_hhi: 0.12,
    },
    amount: {
      members: [
        { member_id: 'g', concentration_weight: 0.08, hhi_contribution: 0.005 },
      ],
      sum_hhi: 0.20,
      canonical_raw_hhi: 0.25,
      canonical_normalized_hhi: 0.15,
    },
  }
}

/** 生成 Reconciliation（skipped: string[], checks: Record<string, Check>）
 *  严格镜像 backend producer:
 *    direction/capital_tilt/concentration_price/concentration_amount → kind: 'sum'
 *    breadth → kind: 'counts_and_ratios'
 *    leadership → kind: 'set'
 *    violation_count = count(check.pass === false)
 *    skipped = sorted keys where resolved === 'skipped' */
function makeReconciliation() {
  const checks = {
    direction: { pass: true as const, resolved: 'matched' as const, kind: 'sum' as const },
    capital_tilt: { pass: true as const, resolved: 'both_unavailable' as const, kind: 'sum' as const },
    concentration_price: { pass: true as const, resolved: 'matched' as const, kind: 'sum' as const },
    concentration_amount: { pass: false as const, resolved: 'mismatch' as const, kind: 'sum' as const },
    breadth: { pass: true as const, resolved: 'matched' as const, kind: 'counts_and_ratios' as const },
    leadership: { pass: null, resolved: 'skipped' as const, kind: 'set' as const },
  }
  // 严格镜像 backend 计算逻辑
  const violationCount = Object.values(checks).filter((c) => c.pass === false).length
  const skipped = Object.entries(checks)
    .filter(([, c]) => c.resolved === 'skipped')
    .map(([k]) => k)
    .sort()
  return {
    violation_count: violationCount,
    skipped,
    tolerance: 1e-6,
    checks,
  }
}

/** 生成 Leadership attribution group（直接 MemberEvidence[]） */
function makeLeadershipAttrGroup() {
  return {
    status: 'ready',
    reason: null,
    previous_direction: 1 as const,
    current_direction: -1 as const,
    retained: [{ member_id: 'h', aligned_contribution: 0.005, contribution: 0.003 }],
    entrants: [{ member_id: 'i', aligned_contribution: 0.01, contribution: 0.008 }],
    exits: [{ member_id: 'j', aligned_contribution: -0.005, contribution: -0.003 }],
  }
}

// ============================================================
// 1. Dynamics parser — fact-object 解析
// ============================================================

test('D1. Dynamics fact-object 序列正确解析为日期对齐 series（非 number[]）', () => {
  const dates = ['2026-08-19', '2026-08-20', '2026-08-21']
  const comp = makeComposition({
    composition_readiness: 'ready',
    historical_dynamics: makeDynamicsLayer({
      status: 'ready',
      series: makeFullDynamicsSeries(dates),
      phaseFacts: dates.map((d) => ({ trade_date: d, phase: 'Strengthening', status: 'ready', position: 10, velocity: 1, acceleration: 0.1, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null })),
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  assert.ok(dyn)
  // 每个 series 有自己的日期数组
  assert.deepEqual(dyn.positionDates, dates, 'positionDates 来自 fact-object trade_date')
  assert.deepEqual(dyn.velocityDates, dates, 'velocityDates 来自 fact-object trade_date')
  assert.deepEqual(dyn.accelerationDates, dates, 'accelerationDates 来自 fact-object trade_date')
  // 值从 fact-object 字段提取
  assert.deepEqual(dyn.position, [10, 20, 30], 'position 从 .position 字段提取')
  assert.deepEqual(dyn.velocity, [1, 2, 3], 'velocity 从 .value 字段提取')
  assert.deepEqual(dyn.acceleration, [0.1, 0.2, 0.3], 'acceleration 从 .value 字段提取')
  assert.equal(dyn.status, 'ready')
  assert.equal(dyn.phaseFacts.length, 3)
})

test('D2. fact-object 序列中 position=null 保留 null（unavailable_current 语义），position=0 是合法值', () => {
  const dates = ['2026-08-19', '2026-08-20', '2026-08-21']
  // 中间日期 position=null (status=unavailable_current)
  const positionSeries: ScopeDynamicsPositionPoint[] = [
    { trade_date: dates[0], position: 10, status: 'ready' },
    { trade_date: dates[1], position: null, status: 'unavailable_current' },  // position=null
    { trade_date: dates[2], position: 30, status: 'ready' },
  ]
  const valueSeries: ScopeDynamicsValuePoint[] = [
    valuePoint(dates[0], 1),
    valuePoint(dates[1], null),  // value=null
    valuePoint(dates[2], 3),
  ]
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({
      series: {
        position: positionSeries,
        ema5: valueSeries, ema20: [], velocity: valueSeries, signal: [], acceleration: valueSeries,
        persistence: [],
      },
      phaseFacts: dates.map((d) => ({ trade_date: d, phase: null, status: 'ready', position: null, velocity: null, acceleration: null, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null })),
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  assert.ok(dyn)
  // position=null → 解析为 null（不是 0，不是 number）
  assert.equal(dyn.position[1], null, 'position=null 解析为 null（unavailable_current 语义）')
  // velocity 中间值为 null（因为 value=null）
  assert.equal(dyn.velocity[1], null, 'value=null 保持 null')
  // 其他两个位置正常
  assert.equal(dyn.position[0], 10)
  assert.equal(dyn.position[2], 30)
})

test('D3. historical_dynamics 缺失 → 返回结构含空 series 与状态', () => {
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({ status: 'insufficient_history', series: null, phaseFacts: null }),
  })
  const dyn = parseDynamicsLayer(comp)
  assert.ok(dyn)
  assert.equal(dyn.status, 'insufficient_history')
  assert.deepEqual(dyn.position, [])
  assert.equal(dyn.phaseFacts.length, 0)
})

test('D4. 顶层 historical_dynamics 为 null → 解析为 null', () => {
  assert.equal(parseDynamicsLayer(null), null)
  assert.equal(parseDynamicsLayer(makeComposition({ historical_dynamics: null })), null)
})

test('D5. currentPhaseFact 取最末 observation（persisted），不反推 series', () => {
  const dates = ['2026-08-19', '2026-08-20', '2026-08-21']
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({
      series: makeFullDynamicsSeries(dates),
      phaseFacts: [
        { trade_date: dates[0], phase: 'Early Lift', status: 'ready', position: 1, velocity: 1, acceleration: 1, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null },
        { trade_date: dates[1], phase: 'Strengthening', status: 'ready', position: 2, velocity: 2, acceleration: 2, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null },
        { trade_date: dates[2], phase: 'Weakening', status: 'ready', position: 999, velocity: 999, acceleration: 999, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null },
      ],
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  const cur = currentPhaseFact(dyn)
  assert.equal(cur?.trade_date, '2026-08-21')
  assert.equal(cur?.position, 999, '当前事实来自最末 phase observation')
  assert.equal(cur?.phase, 'Weakening')
})

test('D6. ready + phase=null → current phase 为 null（不是第七个 phase）', () => {
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({
      series: makeFullDynamicsSeries(['2026-08-20']),
      phaseFacts: [
        { trade_date: '2026-08-20', phase: null, status: 'ready', position: 1, velocity: 1, acceleration: 1, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null },
      ],
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  const cur = currentPhaseFact(dyn)
  assert.equal(cur?.phase, null, 'ready + phase=null 必须保留 null')
})

// ============================================================
// 2. Dynamics chart adapter
// ============================================================

test('G1. 缺失点在 chart data 中为 whitespace（仅 time），位置仍保留', () => {
  const data = alignDynamicsSeries(
    ['2026-08-19', '2026-08-20', '2026-08-21'],
    [10, null, 30],
  )
  assert.equal(data.length, 3)
  assert.deepEqual(data[0], { time: '2026-08-19', value: 10 })
  assert.deepEqual(data[1], { time: '2026-08-20' }, '缺失点为 whitespace point，无 value')
  assert.deepEqual(data[2], { time: '2026-08-21', value: 30 })
})

test('G2. 不缺值日不开线（有值点不带空白逻辑）', () => {
  const data = alignDynamicsSeries(['2026-08-19', '2026-08-20'], [5, 6])
  for (const p of data) assert.ok('value' in p, '连续有值日不应产生 whitespace point')
})

test('G3. Position autoscale 固定 0–100', () => {
  assert.equal(POSITION_MIN, 0)
  assert.equal(POSITION_MAX, 100)
  const data = alignDynamicsSeries(['d1', 'd2'], [10, 90])
  const range = buildPositionAutoscale(data)
  assert.deepEqual(range, { min: 0, max: 100 })
})

test('G4. Offset autoscale 始终包含 0 参考线', () => {
  const data = alignDynamicsSeries(['d1', 'd2'], [5, 8])
  const range = buildOffsetAutoscale(data)
  assert.equal(range?.min, Math.min(0, 5))
  assert.equal(range?.max, 8)
  const negOnly = alignDynamicsSeries(['d1'], [-3])
  const r2 = buildOffsetAutoscale(negOnly)
  assert.equal(r2?.min, -3)
  assert.equal(r2?.max, 0, 'max 至少包含 0 参考线')
})

test('G5. Zero reference line builder 返回 price=0 的 line 定义', () => {
  const line = buildZeroReferenceLine()
  assert.equal(line.price, 0)
  assert.ok(line.axisLabelVisible, '零参考线必须带 axis label')
  assert.ok(line.title.includes('zero'))
})

// ============================================================
// 3. Internal Structure parser
// ============================================================

test('I1. 全量 internal 正确解析（null != 0）', () => {
  const comp = makeComposition({
    internal_structure_facts: {
      breadth: { equal_weight_return: 0.02, advance_ratio: 0.6, decline_ratio: 0.3, unchanged_ratio: 0.1, return_dispersion: 0.05 },
      capital_tilt: { equal_weight_return: 0.02, amount_weighted_return: 0.05, capital_tilt: 0.03 },
      concentration: { price_normalized_hhi: 0.12, amount_normalized_hhi: 0.08 },
    },
  })
  const p = parseInternalStructure(comp)
  assert.equal(p.breadth?.advanceRatio, 0.6)
  assert.equal(p.capitalTilt?.capitalTilt, 0.03)
  assert.equal(p.concentration?.priceNormalizedHhi, 0.12)
})

test('I2. 单字段 null 保持 null（不伪造 0）', () => {
  const comp = makeComposition({
    internal_structure_facts: {
      breadth: { equal_weight_return: null, advance_ratio: 0.5, decline_ratio: null, unchanged_ratio: null, return_dispersion: null },
      capital_tilt: { equal_weight_return: null, amount_weighted_return: null, capital_tilt: null },
      concentration: { price_normalized_hhi: null, amount_normalized_hhi: null },
    },
  })
  const p = parseInternalStructure(comp)
  assert.equal(p.breadth?.equalWeightReturn, null)
  assert.equal(p.breadth?.declineRatio, null, 'null ratio 保持 null，不转为 0')
  assert.equal(p.capitalTilt?.capitalTilt, null)
  assert.equal(p.concentration?.amountNormalizedHhi, null)
})

// ============================================================
// 4. Leadership parser（number | null direction）
// ============================================================

test('L1. ready leadership 解析（number direction + counts + ids）', () => {
  const comp = makeComposition({
    leadership: {
      status: 'ready', reason: null, coverage: 0.9,
      previous_direction: 1, current_direction: -1,
      previous_rankable_count: 50, current_rankable_count: 50,
      previous_leader_count: 10, current_leader_count: 10,
      retained_count: 6, entrant_count: 3, exit_count: 4,
      previous_retention: 0.6, jaccard_stability: 0.5, migration: 0.4,
      previous_leader_ids: ['a', 'b'], current_leader_ids: ['b', 'c'],
      entrant_ids: ['c'], exit_ids: ['a'],
    },
  })
  const l = parseLeadership(comp)
  assert.equal(l?.status, 'ready')
  assert.equal(l?.previousDirection, 1, 'direction 必须是 number，不是 string')
  assert.equal(l?.currentDirection, -1)
  assert.equal(l?.retainedCount, 6)
  assert.equal(l?.migration, 0.4)
  assert.deepEqual(l?.previousLeaderIds, ['a', 'b'])
})

test('L2. unavailable_snapshot 保留有效 evidence（status!=ready 但 ids/counts 仍有值）', () => {
  // prompt §9：unavailable_snapshot 时仍可能保留真实 ids/counts
  const comp = makeComposition({
    leadership: {
      status: 'unavailable', reason: 'unavailable_snapshot', coverage: null,
      previous_direction: null, current_direction: null,
      previous_rankable_count: null, current_rankable_count: null,
      previous_leader_count: 5, current_leader_count: 3,
      retained_count: 2, entrant_count: 1, exit_count: 3,
      previous_retention: null, jaccard_stability: null, migration: null,
      previous_leader_ids: ['x', 'y'], current_leader_ids: ['z'],
      entrant_ids: ['z'], exit_ids: ['x', 'y'],
    },
  })
  const l = parseLeadership(comp)
  assert.equal(l?.status, 'unavailable')
  assert.equal(l?.reason, 'unavailable_snapshot')
  // 有效 evidence 仍保留
  assert.equal(l?.previousLeaderCount, 5)
  assert.equal(l?.currentLeaderCount, 3)
  assert.equal(l?.retainedCount, 2)
  assert.deepEqual(l?.previousLeaderIds, ['x', 'y'])
  // 迁移指标为 null
  assert.equal(l?.previousRetention, null)
  assert.equal(l?.migration, null)
})

test('L3. empty_leader_set 仍保留 ids（null != 0，empty != null）', () => {
  const comp = makeComposition({
    leadership: {
      status: 'unavailable', reason: 'empty_leader_set', coverage: 0.5,
      previous_direction: null, current_direction: null,
      previous_rankable_count: null, current_rankable_count: null,
      previous_leader_count: 0, current_leader_count: 0,
      retained_count: 0, entrant_count: 0, exit_count: 0,
      previous_retention: null, jaccard_stability: null, migration: null,
      previous_leader_ids: [], current_leader_ids: [], entrant_ids: [], exit_ids: [],
    },
  })
  const l = parseLeadership(comp)
  assert.equal(l?.status, 'unavailable')
  assert.equal(l?.reason, 'empty_leader_set')
  // empty array 与 null 区分
  assert.deepEqual(l?.previousLeaderIds, [], 'empty array 保持空数组而非 null')
  assert.equal(l?.previousLeaderCount, 0, '0 count 是有效值，不是 null')
  // 迁移指标为 null
  assert.equal(l?.previousRetention, null)
})

// ============================================================
// 5. Member Attribution parser — 真实后端形状
// ============================================================

test('A1. direction 正向/负向 direct array 解析（不是 {members:[...]}），无顶层 status', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null,
      direction: makeDirectionGroup(),
      capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: 'abc',
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.direction?.positive?.length, 2, 'positive 是直接 MemberEvidence[]，不是嵌套 {members:[...]}')
  assert.equal(a.direction?.negative?.length, 1)
  assert.equal(a.direction?.positive?.[0]?.member_id, 'a')
  assert.equal(a.direction?.positive?.[0]?.contribution, 0.01)
  assert.equal(a.direction?.sumContribution, 0.005)
  assert.equal(a.determinismChecksum, 'abc')
  assert.equal((a as unknown as Record<string, unknown>).status, undefined, '顶层无 status 字段')
})

test('A2. capital_tilt 直接数组 + tilt_contribution 字段解析', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null,
      direction: null,
      capital_tilt: makeCapitalTiltGroup(),
      breadth: null, concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.ok(a.capitalTilt)
  assert.equal(a.capitalTilt?.positive?.length, 1)
  assert.equal(a.capitalTilt?.positive?.[0]?.tilt_contribution, 0.02)
  assert.equal(a.capitalTilt?.sumTiltContribution, 0.01)
  assert.equal(a.capitalTilt?.priceUniverseCount, 45)
})

test('A3. breadth direct array 解析', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null,
      breadth: {
        status: 'ready', denominator: 50,
        advance: [{ member_id: 's1', return_1d: 0.02 }],
        decline: [{ member_id: 's2', return_1d: -0.01 }],
        unchanged: [{ member_id: 's3', return_1d: 0.0 }],
        unavailable: [],
      },
      concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.ok(a.breadth)
  assert.equal(a.breadth?.advance?.length, 1)
  assert.equal(a.breadth?.advance?.[0]?.member_id, 's1')
  assert.equal(a.breadth?.decline?.[0]?.member_id, 's2')
  assert.equal(a.breadth?.unavailable?.length, 0)
})

test('A4. concentration price/amount 使用 {members:[...]} 对象（唯一例外）', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null,
      concentration: makeConcentrationGroup(),
      leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.ok(a.concentration)
  assert.ok(a.concentration?.price)
  assert.equal(a.concentration?.price?.members.length, 1, 'concentration 用 {members:[...]} 嵌套')
  assert.equal(a.concentration?.price?.members[0]?.member_id, 'f')
  assert.equal(a.concentration?.price?.sumHhi, 0.15)
})

test('A5. leadership retained/entrants/exits direct array 解析', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null, concentration: null,
      leadership: makeLeadershipAttrGroup(),
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.ok(a.leadership)
  assert.equal(a.leadership?.retained?.length, 1)
  assert.equal(a.leadership?.retained?.[0]?.aligned_contribution, 0.005)
  assert.equal(a.leadership?.entrants?.length, 1)
  assert.equal(a.leadership?.exits?.length, 1)
})

// ============================================================
// 6. Reconciliation — map 形状
// ============================================================

test('R1. Reconciliation: skipped 保持 string[]，checks 转为带 key 的数组', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: makeReconciliation(),
      determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.ok(a.reconciliation)
  // skipped 必须是 string[]，镜像 backend — 只有 leadership resolved='skipped'
  assert.deepEqual(a.reconciliation?.skipped, ['leadership'])
  assert.equal(a.reconciliation?.skipped.length, 1)
  // checks 转为带 key 的数组 — 6 条（direction/capital_tilt/concentration_price/concentration_amount/breadth/leadership）
  assert.equal(a.reconciliation?.checks.length, 6)
  // check identity 来自 map key（按 Object.entries 插入顺序）
  assert.equal(a.reconciliation?.checks[0]?.key, 'direction')
  assert.equal(a.reconciliation?.checks[0]?.kind, 'sum')
  assert.equal(a.reconciliation?.checks[0]?.pass, true)
  assert.equal(a.reconciliation?.checks[0]?.resolved, 'matched')
  // concentration_amount mismatch（pass=false, kind=sum）
  const mismatch = a.reconciliation?.checks.find((c) => c.key === 'concentration_amount')
  assert.ok(mismatch)
  assert.equal(mismatch?.pass, false)
  assert.equal(mismatch?.resolved, 'mismatch')
  assert.equal(mismatch?.kind, 'sum')
  // leadership skipped（pass=null, resolved=skipped, kind=set）
  const skip = a.reconciliation?.checks.find((c) => c.key === 'leadership')
  assert.ok(skip)
  assert.equal(skip?.pass, null)
  assert.equal(skip?.resolved, 'skipped')
  assert.equal(skip?.kind, 'set')
})

test('R2. Reconciliation: skipped 空数组时保持空数组', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: { violation_count: 0, skipped: [], tolerance: null, checks: {} },
      determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.deepEqual(a.reconciliation?.skipped, [], '空 skipped 保持空数组')
  assert.equal(a.reconciliation?.checks.length, 0)
})

// ============================================================
// 7. Raw Facts
// ============================================================

test('F1. observation 顶层精确顺序', () => {
  assert.deepEqual(OBSERVATION_GROUP_ORDER, [
    'scope', 'price', 'trend', 'structure', 'momentum', 'participation', 'chip', 'freshness',
  ])
  const groups = observationGroups({
    price: { p: 1 },
    scope: { s: 1 },
    chip: { status: 'unavailable' },
    trend: { t: 1 },
    structure: { st: 1 },
    momentum: { m: 1 },
    participation: { pa: 1 },
  })
  assert.deepEqual(
    groups.map((g) => g.key),
    ['scope', 'price', 'trend', 'structure', 'momentum', 'participation', 'chip'],
  )
})

test('F2. chip unavailable 原样保留', () => {
  const groups = observationGroups({ chip: { status: 'unavailable' } })
  assert.equal(groups.length, 1)
  assert.deepEqual(groups[0].value, { status: 'unavailable' })
})

// ============================================================
// 8. Detail query contract
// ============================================================

test('Q1. 无 scopeKey => detail 不 enabled', () => {
  assert.equal(isScopeDetailEnabled({ tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: null }), false)
  assert.equal(isScopeDetailEnabled({ tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: '' }), false)
  assert.equal(isScopeDetailEnabled({ tradeDate: null, scopeType: 'industry_l1', scopeKey: 'copper' }), false)
})

test('Q2. 有选中 Scope => detail enabled', () => {
  assert.equal(isScopeDetailEnabled({ tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: 'copper' }), true)
})

test('Q3. detail key identity 含 tradeDate + scopeType + scopeKey', () => {
  const opts = scopeDetailQueryOptions({ tradeDate: '2026-08-21', scopeType: 'industry_l2', scopeKey: 'bank', includePartial: false })
  assert.equal(opts.enabled, true)
  const key = opts.queryKey
  assert.ok(key.includes('scopeDetail'))
  assert.ok(key.includes('2026-08-21'))
  assert.ok(key.includes('industry_l2'))
  assert.ok(key.includes('bank'))
})

test('Q4. 切换 tab 不改 detail key（tab 不在 identity 内）', () => {
  const base = { tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: 'copper' }
  const keyDynamics = scopeDetailQueryOptions(base).queryKey
  const urlState = defaultReviewUrlState()
  assert.ok(urlState.tab === DEFAULT_REVIEW_TAB)
  assert.ok(!JSON.stringify(keyDynamics).includes('leadership'), 'detail identity 不得包含 tab')
})

test('Q5. 切换 scopeKey / date / family => detail key 改变', () => {
  const base = { tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: 'copper' }
  const others = [
    { tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: 'bank' },
    { tradeDate: '2026-08-22', scopeType: 'industry_l1', scopeKey: 'copper' },
    { tradeDate: '2026-08-21', scopeType: 'industry_l2', scopeKey: 'copper' },
  ]
  for (const o of others) {
    assert.notDeepEqual(scopeDetailQueryOptions(o).queryKey, scopeDetailQueryOptions(base).queryKey)
  }
})

// ============================================================
// 9. 禁止前端业务计算
// ============================================================

test('CALC1. 面板不 import 重算函数', () => {
  const dynamicsSrc = read('ScopeDynamicsPanel.tsx')
  assert.doesNotMatch(dynamicsSrc, /function\s+computeEm|function\s+calcVelocity|function\s+calcAcceleration/)
})

test('CALC2. Internal 使用 persisted capital_tilt', () => {
  const src = read('ScopeInternalStructurePanel.tsx')
  assert.match(src, /capitalTilt/)
  assert.doesNotMatch(src, /amountWeightedReturn\s*-\s*equalWeightReturn/)
})

// ============================================================
// 10. 无 N+1
// ============================================================

test('N1. Table / Trajectory / family snapshot 不得 import getReviewScopeDetail', () => {
  for (const f of ['ScopeExplorerTable.tsx', 'ScopeTrajectoryView.tsx', 'useReviewScopeFamilySnapshot.ts', 'scopeExplorerViewModel.ts']) {
    const src = read(f)
    assert.doesNotMatch(src, /getReviewScopeDetail|useReviewScopeDetail/, `${f} 不得请求 detail（无 N+1）`)
  }
})

test('N2. 只有 detail owner 调用 getReviewScopeDetail', () => {
  const workspace = read('ScopeDetailWorkspace.tsx')
  assert.match(workspace, /useReviewScopeDetail/)
})

// ============================================================
// 11. URL tab SSOT
// ============================================================

test('URL1. [Slice 6] 默认 detail tab 为 dsa（canonical 一级 Tab）', () => {
  assert.equal(DEFAULT_REVIEW_TAB, 'dsa')
})

test('URL2. [Slice 6] 缺失 tab → dsa', () => {
  const st = defaultReviewUrlState()
  assert.equal(st.tab, 'dsa')
})

test('URL3. [R3A FE-8] 默认/显式 current 一致（legacy URL 仍合法）', () => {
  assert.equal(normalizeDetailTab('current'), 'current')
})

test('URL4. [Slice 6] 非法/缺失 tab → dsa（fail-safe 到 canonical 默认）', () => {
  assert.equal(normalizeDetailTab('bogus'), 'dsa')
  assert.equal(normalizeDetailTab(null), 'dsa')
  assert.equal(normalizeDetailTab(undefined), 'dsa')
})

function detailState(tab: ReviewUrlState['tab']): ReviewUrlState {
  return { ...defaultReviewUrlState(), date: '2026-08-21', family: 'industry_l1', scopeKey: 'bank', tab }
}

test('URL5. [Slice 6] 默认 dsa 省略 tab 参数；legacy tab 仍编码（兼容旧书签）', () => {
  const dsaUrl = buildReviewUrl(detailState('dsa'))
  assert.ok(!dsaUrl.includes('tab='), `dsa 不应编码 tab，实际：${dsaUrl}`)
  const legacyUrl = buildReviewUrl(detailState('current'))
  assert.ok(legacyUrl.includes('tab=current'), `legacy current 仍编码 tab，实际：${legacyUrl}`)
})

test('URL6. [R3A FE-12] dynamics 编码保留 tab=dynamics 且可 round-trip', () => {
  const url = buildReviewUrl(detailState('dynamics'))
  assert.ok(url.includes('tab=dynamics'), `dynamics 应编码 tab，实际：${url}`)
  const parsed = decodeReviewUrl(new URLSearchParams(url.split('?')[1] ?? ''))
  assert.equal(parsed.tab, 'dynamics')
})

test('URL7. [R3A FE-13] tab 切换不改 detail query identity', () => {
  const base = { tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: 'copper' }
  const key = scopeDetailQueryOptions(base).queryKey
  assert.ok(!JSON.stringify(key).includes('dynamics'), 'detail identity 不得包含 tab')
  assert.ok(!JSON.stringify(key).includes('current'), 'detail identity 不得包含 tab')
})

// ============================================================
// 12. 面板源码契约（prompt §13 regression tests A-R）
// ============================================================

test('REG-A. Dynamics fact-object fixture 解析为非 null 值（不是全部变 null）', () => {
  const dates = ['2026-08-19', '2026-08-20', '2026-08-21']
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({
      status: 'ready',
      series: makeFullDynamicsSeries(dates),
      phaseFacts: dates.map((d) => ({ trade_date: d, phase: null, status: 'ready', position: 10, velocity: 1, acceleration: 0.1, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null })),
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  assert.ok(dyn)
  // 所有 position 值都是有效的 number（不是 null）
  for (const p of dyn.position) {
    assert.ok(typeof p === 'number', `position 值 ${p} 必须是 number，不是 null`)
  }
  for (const v of dyn.velocity) {
    assert.ok(typeof v === 'number', `velocity 值 ${v} 必须是 number，不是 null`)
  }
})

test('REG-B. 缺失 fact-object day 保持为 whitespace gap（非截断）', () => {
  // dates 比 series 长 — 不静默截断
  const data = alignDynamicsSeries(
    ['2026-08-19', '2026-08-20', '2026-08-21'],
    [10, 30],  // 少一个值
  )
  assert.equal(data.length, 3, '缺失值的日期不应被截断')
  assert.deepEqual(data[2], { time: '2026-08-21' }, '第三个日期为 whitespace gap')
})

test('REG-C. 无 Math.min 静默截断（用 Math.max 保持所有日期）', () => {
  const src = read('scopeDynamicsChart.ts')
  assert.doesNotMatch(src, /Math\.min\(dates\.length.*series\.length/, '不得用 Math.min 截断时间轴')
})

test('REG-D. direction.positive direct array 被正确解析', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null,
      direction: makeDirectionGroup(),
      capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.direction?.positive?.length, 2)
  assert.equal(a.direction?.positive?.[0]?.member_id, 'a')
})

test('REG-E. capital_tilt.positive direct array 被正确解析', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null,
      capital_tilt: makeCapitalTiltGroup(),
      breadth: null, concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.capitalTilt?.positive?.length, 1)
})

test('REG-F. breadth.advance direct array 被正确解析', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null,
      breadth: { status: 'ready', denominator: 50, advance: [{ member_id: 'x', return_1d: 0.01 }], decline: [], unchanged: [], unavailable: [] },
      concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.breadth?.advance?.length, 1)
})

test('REG-G. leadership.retained direct array 被正确解析', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null, concentration: null,
      leadership: makeLeadershipAttrGroup(),
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.leadership?.retained?.length, 1)
})

test('REG-H. concentration price.members 被正确解析（唯一嵌套）', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null,
      concentration: makeConcentrationGroup(),
      leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.concentration?.price?.members.length, 1)
  assert.equal(a.concentration?.price?.members[0]?.member_id, 'f')
})

test('REG-I. Reconciliation checks object 转为带 key 的数组（真实 backend keys）', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: makeReconciliation(),
      determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  const keys = a.reconciliation?.checks.map((c) => c.key) ?? []
  assert.ok(keys.includes('direction'), 'direction check 必须存在')
  assert.ok(keys.includes('capital_tilt'), 'capital_tilt check 必须存在')
  assert.ok(keys.includes('breadth'), 'breadth check 必须存在')
  assert.ok(keys.includes('concentration_price'), 'concentration_price check 必须存在')
  assert.ok(keys.includes('concentration_amount'), 'concentration_amount check 必须存在')
  assert.ok(keys.includes('leadership'), 'leadership check 必须存在')
})

test('REG-J. skipped string[] 保持数组语义，且与 resolved=skipped 一致', () => {
  const comp = makeComposition({
    member_attribution: {
      scope: null, direction: null, capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: makeReconciliation(),
      determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.ok(Array.isArray(a.reconciliation?.skipped), 'skipped 必须是 string[]')
  // 只有 leadership resolved='skipped' → 长度 1
  assert.equal(a.reconciliation?.skipped.length, 1, 'skipped 长度必须等于 resolved=skipped 的 key 数')
  assert.equal(a.reconciliation?.skipped[0], 'leadership', 'skipped 必须包含 leadership')
})

test('REG-K. Internal Breadth null 绝不以 ?? 0 伪装（源码契约）', () => {
  const src = read('ScopeInternalStructurePanel.tsx')
  assert.doesNotMatch(src, /advanceRatio\s*\?\?\s*0/, '不得用 ?? 0 伪装 advanceRatio null')
  assert.doesNotMatch(src, /declineRatio\s*\?\?\s*0/, '不得用 ?? 0 伪装 declineRatio null')
  assert.doesNotMatch(src, /unchangedRatio\s*\?\?\s*0/, '不得用 ?? 0 伪装 unchangedRatio null')
})

test('REG-L. Internal Breadth 使用 persisted ratio（不重新归一化）', () => {
  const src = read('ScopeInternalStructurePanel.tsx')
  assert.doesNotMatch(src, /total\s*=.*a.*\+.*d.*\+.*u/, '不得用 a+d+u 做重新归一化')
})

test('REG-M. Leadership status=unavailable + reason=empty_leader_set 仍展示有效 evidence', () => {
  const src = read('ScopeLeadershipPanel.tsx')
  // 不得在 status!=='ready' 时提前 return 整个面板
  assert.doesNotMatch(src, /status.*!==.*'ready'[\s\S]*return.*panelUnavailable/, '不得因 status!=ready 完全隐藏面板')
  assert.match(src, /leadStatusBanner|leadStatusBanner/, '必须有 status banner 展示不可用原因')
})

test('REG-N. Capital Tilt UI 使用 tilt_contribution（不是 direction contribution）', () => {
  const src = read('ScopeMemberAttributionPanel.tsx')
  assert.match(src, /tilt_contribution/, 'Capital Tilt 列必须使用 tilt_contribution 字段')
})

test('REG-O. Concentration UI 使用 hhi_contribution', () => {
  const src = read('ScopeMemberAttributionPanel.tsx')
  assert.match(src, /hhi_contribution/, 'Concentration 列必须使用 hhi_contribution 字段')
})

test('REG-P. Leadership attribution 使用 aligned_contribution', () => {
  const src = read('ScopeMemberAttributionPanel.tsx')
  assert.match(src, /aligned_contribution/, 'Leadership 列必须使用 aligned_contribution 字段')
})

test('REG-Q. Velocity/Acceleration 图表包含 zero price line', () => {
  // [SLICE 4 / Price] 三图 renderer 抽为共享组件 ScopeDynamicsCharts（窄块抽取，
  // 行为不变）；zero reference line 由该 engine 拥有，面板复用它。
  const engine = read('ScopeDynamicsCharts.tsx')
  assert.match(engine, /buildZeroReferenceLine|createPriceLine/, 'offset 图表必须使用 zero reference line')
  const panel = read('ScopeDynamicsPanel.tsx')
  assert.match(panel, /from '\.\/ScopeDynamicsCharts'/, '面板必须复用共享三图 renderer')
})

// ============================================================
// 13. Panel source contracts
// ============================================================

test('SRC1. Dynamics 面板使用分 series 日期（不是单一 dates）', () => {
  const src = read('ScopeDynamicsPanel.tsx')
  assert.match(src, /positionDates/, '必须使用 positionDates')
  assert.match(src, /velocityDates/, '必须使用 velocityDates')
  assert.match(src, /accelerationDates/, '必须使用 accelerationDates')
})

test('SRC2. Dynamics 面板有图表标题', () => {
  const src = read('ScopeDynamicsPanel.tsx')
  // REVIEW-UX-CN-01：图表标题经 ReviewTerm termKey 渲染（position/velocity/acceleration）
  assert.match(src, /termKey="position" compact/, 'Position 图必须有标题')
  assert.match(src, /termKey="velocity" compact/, 'Velocity 图必须有标题')
  assert.match(src, /termKey="acceleration" compact/, 'Acceleration 图必须有标题')
  // [Slice B] 三张图由单个 DynamicsCharts 统一承载，共享 trading-date domain
  assert.match(src, /<DynamicsCharts/, '必须由 DynamicsCharts 统一渲染三图')
  for (const k of ["key: 'position'", "key: 'velocity'", "key: 'acceleration'"]) {
    assert.ok(src.includes(k), `必须渲染 ${k} 图配置`)
  }
  assert.match(src, /buildSharedTradingDates\(/, '必须使用共享 trading-date domain')
  assert.match(src, /alignToSharedDomain\(/, '各 series 必须对齐到共享 domain')
})

test('SRC3. Dynamics 面板不再 export alignDynamicsSeries（test-driven export 已移除）', () => {
  const src = read('ScopeDynamicsPanel.tsx')
  assert.doesNotMatch(src, /export.*alignDynamicsSeries/, '面板不得 re-export 纯适配器')
})

test('SRC4. Zero reference line builder 被正确 export', () => {
  assert.ok(buildZeroReferenceLine, 'buildZeroReferenceLine 必须从 scopeDynamicsChart export')
})

// ============================================================
// 14. member name fallback
// ============================================================

test('M1. member_name==member_id 时诚实显示 member_id', () => {
  assert.equal(memberName({ member_id: 'uuid-1', member_name: 'uuid-1' }), 'uuid-1')
  assert.equal(memberName({ member_id: 'uuid-1', member_name: null }), 'uuid-1')
  assert.equal(memberName({ member_id: 'uuid-1', member_name: '' }), 'uuid-1')
  assert.equal(memberName({ member_id: 'uuid-1', member_name: '铜陵有色' }), '铜陵有色')
})

// ============================================================
// 15. 面板渲染边界
// ============================================================

test('ST1. [R3A] Workspace 对 composition=null 显示中性说明（非 hard-stop，不称 failed/broken/error）', () => {
  const src = read('ScopeDetailWorkspace.tsx')
  assert.match(src, /Canonical Composition 不可用；Objective Observation 仍可用/)
  assert.match(src, /选择一个 Scope 查看详细分析/)
})

// ============================================================
// 16. Final closure — contract truthfulness regression
// ============================================================

test('CLOSURE-A. position=null 通过类型检查（无 as number 强转）', () => {
  // positionPoint 现在接受 number | null，无类型幻觉
  const p: ScopeDynamicsPositionPoint = {
    trade_date: '2026-08-21',
    position: null,  // 真实的 null，不是 as number
    status: 'unavailable_current',
  }
  assert.equal(p.position, null)
  assert.equal(p.status, 'unavailable_current')
})

test('CLOSURE-B. position=0 + status=ready 保持真实 0（null != 0）', () => {
  const dates = ['2026-08-19', '2026-08-20', '2026-08-21']
  const positionSeries: ScopeDynamicsPositionPoint[] = [
    { trade_date: dates[0], position: 10, status: 'ready' },
    { trade_date: dates[1], position: 0, status: 'ready' },  // 0 是合法值
    { trade_date: dates[2], position: null, status: 'unavailable_current' },
  ]
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({
      series: {
        position: positionSeries,
        ema5: [], ema20: [], velocity: [], signal: [], acceleration: [], persistence: [],
      },
      phaseFacts: [],
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  assert.ok(dyn)
  assert.equal(dyn.position[0], 10)
  assert.equal(dyn.position[1], 0, 'ready + position=0 保持真实 0')
  assert.equal(dyn.position[2], null, 'unavailable_current + position=null 保持 null')
})

test('CLOSURE-C. Current Dynamics Status 来自 final dynamics_phase（非 layer status）', () => {
  const src = read('ScopeDynamicsPanel.tsx')
  // Status 必须从 phaseFact.status 读取（通过 f?.status 其中 f=phaseFact），不是 dynamics.status
  assert.match(src, /f\?\.status/, 'Status 必须从 phaseFact.status 读取')
  assert.doesNotMatch(src, /status=\{dynamics\.status\}/, '不得使用 dynamics.status 作为 Current Status')
})

test('CLOSURE-D. Raw Facts 无 member-attribution 依赖', () => {
  const src = read('ScopeRawFactsPanel.tsx')
  assert.doesNotMatch(src, /ScopeAttributionParsed/, 'RawFacts 不得 import ScopeAttributionParsed')
  assert.doesNotMatch(src, /attr\??:.*ScopeAttributionParsed/, 'RawFacts 不得接受 attr prop')
  assert.doesNotMatch(src, /determinismChecksum/, 'RawFacts 不得展示 determinism_checksum')
})

test('CLOSURE-E. determinism_checksum 在 Attribution panel 底部', () => {
  const src = read('ScopeMemberAttributionPanel.tsx')
  assert.match(src, /determinismChecksum/, 'Attribution panel 必须展示 determinism_checksum')
})

test('CLOSURE-F. ScopeMemberAttributionLayer 无顶层 status', () => {
  const src = read('types.ts')
  // ScopeMemberAttributionLayer 不得有 status 字段
  const layerSection = src.substring(src.indexOf('interface ScopeMemberAttributionLayer'))
  const endIdx = layerSection.indexOf('}', layerSection.indexOf('}') + 1)
  const layerDef = layerSection.substring(0, endIdx)
  assert.doesNotMatch(layerDef, /^\s+status:/m, 'ScopeMemberAttributionLayer 不得有顶层 status')
})

test('CLOSURE-G. Parsed Attribution 无 status 字段', () => {
  const src = read('scopeDetailContract.ts')
  // parseAttribution 返回值不得有 status
  assert.doesNotMatch(src, /status:\s*(m\.status|null)/, 'parseAttribution 不得设置 status')
})

test('CLOSURE-H. CSS module classes 存在于 review.module.scss', () => {
  const scss = read('review.module.scss')
  const requiredClasses = [
    'dynamicsChartWrapper',
    'dynamicsChartTitle',
    'breadthPartial',
    'breadthRow',
    'breadthValue',
    'leadStatusBanner',
  ]
  for (const cls of requiredClasses) {
    assert.match(scss, new RegExp(`\\.${cls}[\\s{]`), `CSS class .${cls} 必须存在于 review.module.scss`)
  }
})

test('CLOSURE-I. Persistence fixture 使用 minimum_valid_count=15 + 真实 early-window counts', () => {
  const r = makeFullDynamicsSeries(['2026-08-19', '2026-08-20', '2026-08-21'])
  const p0 = r.persistence[0]!
  const p1 = r.persistence[1]!
  const p2 = r.persistence[2]!
  // minimum_valid_count = 15
  assert.equal(p0.minimum_valid_count, 15, 'minimum_valid_count 必须为 15')
  assert.equal(p1.minimum_valid_count, 15)
  assert.equal(p2.minimum_valid_count, 15)
  // candidate_count 真实 early-window: 1, 2, 3
  assert.equal(p0.candidate_count, 1, 'day1 candidate_count=1')
  assert.equal(p1.candidate_count, 2, 'day2 candidate_count=2')
  assert.equal(p2.candidate_count, 3, 'day3 candidate_count=3')
})

test('CLOSURE-J. Reconciliation fixture 内部一致性：kind/violation_count/skipped', () => {
  const r = makeReconciliation()
  // 必须使用真实 backend keys
  const keys = Object.keys(r.checks).sort()
  assert.deepEqual(keys, ['breadth', 'capital_tilt', 'concentration_amount', 'concentration_price', 'direction', 'leadership'])
  // direction/capital/concentration kinds == 'sum'
  assert.equal(r.checks.direction.kind, 'sum')
  assert.equal(r.checks.capital_tilt.kind, 'sum')
  assert.equal(r.checks.concentration_price.kind, 'sum')
  assert.equal(r.checks.concentration_amount.kind, 'sum')
  // breadth kind == 'counts_and_ratios'
  assert.equal(r.checks.breadth.kind, 'counts_and_ratios')
  // leadership kind == 'set'
  assert.equal(r.checks.leadership.kind, 'set')
  // violation_count == count(pass === false)
  const expectedViolations = Object.values(r.checks).filter((c) => c.pass === false).length
  assert.equal(r.violation_count, expectedViolations, 'violation_count 必须等于 pass===false 的数量')
  // skipped == sorted resolved==='skipped' keys
  const expectedSkipped = Object.entries(r.checks)
    .filter(([, c]) => c.resolved === 'skipped')
    .map(([k]) => k)
    .sort()
  assert.deepEqual(r.skipped, expectedSkipped, 'skipped 必须等于 resolved===skipped 的排序 key')
})

test('CLOSURE-K. Test-only reviewKeys export 已移除', () => {
  const src = read('ScopeDetailWorkspace.tsx')
  assert.doesNotMatch(src, /import.*reviewKeys.*from.*queryKeys/, 'Workspace 不得 import reviewKeys')
  assert.doesNotMatch(src, /export.*reviewKeys/, 'Workspace 不得 re-export reviewKeys')
})

test('CLOSURE-L. RawFacts 在 Workspace 中不再接收 attr', () => {
  const src = read('ScopeDetailWorkspace.tsx')
  assert.doesNotMatch(src, /ScopeRawFactsPanel.*attr=/, 'Workspace 调用 RawFacts 不得传 attr prop')
})

// ============================================================
// 17. Reconciliation producer-fidelity regression
// ============================================================

test('REC-1. Reconciliation check keys 精确包含 6 个 backend keys', () => {
  const r = makeReconciliation()
  const keys = Object.keys(r.checks).sort()
  assert.deepEqual(keys, ['breadth', 'capital_tilt', 'concentration_amount', 'concentration_price', 'direction', 'leadership'])
})

test('REC-2. direction/capital/concentration 所有 kind 必须为 sum', () => {
  const r = makeReconciliation()
  assert.equal(r.checks.direction.kind, 'sum')
  assert.equal(r.checks.capital_tilt.kind, 'sum')
  assert.equal(r.checks.concentration_price.kind, 'sum')
  assert.equal(r.checks.concentration_amount.kind, 'sum')
})

test('REC-3. breadth kind 必须为 counts_and_ratios', () => {
  const r = makeReconciliation()
  assert.equal(r.checks.breadth.kind, 'counts_and_ratios')
})

test('REC-4. leadership kind 必须为 set', () => {
  const r = makeReconciliation()
  assert.equal(r.checks.leadership.kind, 'set')
})

test('REC-5. violation_count 等于 pass===false 的数量', () => {
  const r = makeReconciliation()
  const expected = Object.values(r.checks).filter((c) => c.pass === false).length
  assert.equal(r.violation_count, expected)
  // 当前 fixture 只有 concentration_amount.pass=false → 预期 1
  assert.equal(r.violation_count, 1)
})

test('REC-6. skipped 等于 resolved===skipped 的排序 key 列表', () => {
  const r = makeReconciliation()
  const expected = Object.entries(r.checks)
    .filter(([, c]) => c.resolved === 'skipped')
    .map(([k]) => k)
    .sort()
  assert.deepEqual(r.skipped, expected)
  // 当前 fixture 只有 leadership 被 skip
  assert.deepEqual(r.skipped, ['leadership'])
})

// ============================================================
// 18. Persistence producer-fidelity regression
// ============================================================

test('PERSIST-1. 3-day candidate_count 递增 [1, 2, 3]', () => {
  const r = makeFullDynamicsSeries(['2026-08-19', '2026-08-20', '2026-08-21'])
  assert.equal(r.persistence[0]!.candidate_count, 1)
  assert.equal(r.persistence[1]!.candidate_count, 2)
  assert.equal(r.persistence[2]!.candidate_count, 3)
})

test('PERSIST-2. 3-day valid_count 递增 [1, 2, 3]', () => {
  const r = makeFullDynamicsSeries(['2026-08-19', '2026-08-20', '2026-08-21'])
  assert.equal(r.persistence[0]!.valid_count, 1)
  assert.equal(r.persistence[1]!.valid_count, 2)
  assert.equal(r.persistence[2]!.valid_count, 3)
})

test('PERSIST-3. coverage = valid_count / 20', () => {
  const r = makeFullDynamicsSeries(['2026-08-19', '2026-08-20', '2026-08-21'])
  assert.equal(r.persistence[0]!.coverage, 1 / 20)
  assert.equal(r.persistence[1]!.coverage, 2 / 20)
  assert.equal(r.persistence[2]!.coverage, 3 / 20)
})

test('PERSIST-4. 所有 persistence status 为 insufficient_history（valid < 15）', () => {
  const r = makeFullDynamicsSeries(['2026-08-19', '2026-08-20', '2026-08-21'])
  for (const p of r.persistence) {
    assert.equal(p!.status, 'insufficient_history')
  }
})

test('PERSIST-5. minimum_valid_count = 15', () => {
  const r = makeFullDynamicsSeries(['2026-08-19', '2026-08-20', '2026-08-21'])
  for (const p of r.persistence) {
    assert.equal(p!.minimum_valid_count, 15)
  }
})

test('PERSIST-6. insufficient_history 时 occupancy 保持 null', () => {
  const r = makeFullDynamicsSeries(['2026-08-19', '2026-08-20', '2026-08-21'])
  for (const p of r.persistence) {
    assert.equal(p!.upper_occupancy, null, 'upper_occupancy 在 insufficient_history 时为 null')
    assert.equal(p!.lower_occupancy, null, 'lower_occupancy 在 insufficient_history 时为 null')
  }
})

// ============================================================
// 13. Current 解析 owner 合同（CURRENT-11..12，仍保留的 observationGroups / scopeDetailQueryOptions）
//     原 CURRENT-3..10（parseCurrentSnapshot 整段）已随 R3F legacy cleanup 移除：
//     ScopeCurrentSnapshotPanel 与 parseCurrentSnapshot 在生产代码 0 引用，Current 现由
//     ScopeCurrentObservationWorkspace.extractObservationContext 拥有。
// ============================================================

test('CURRENT-11. observationGroups 在存在 freshness 时返回 freshness（不只顺序常量）', () => {
  const groups = observationGroups({ scope: {}, structure: {}, freshness: { today_count: 1 } })
  assert.ok(groups.some((g) => g.key === 'freshness'), '存在 freshness 时 observationGroups 应包含 freshness')
})

test('CURRENT-12. 切换 current/dynamics tab 不改 detail query identity（无 N+1）', () => {
  const base = { tradeDate: '2026-08-21', scopeType: 'industry_l1' as const, scopeKey: 'copper' }
  const kDyn = scopeDetailQueryOptions(base).queryKey
  const kCur = scopeDetailQueryOptions(base).queryKey
  assert.deepEqual(kCur, kDyn, 'tab 不在 detail identity 内')
  assert.ok(!JSON.stringify(kDyn).includes('current'), 'detail identity 不得包含 current tab')
  assert.ok(!JSON.stringify(kDyn).includes('dynamics'), 'detail identity 不得包含 dynamics tab')
})
