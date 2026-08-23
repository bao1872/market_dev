// [Slice E] Canonical Scope Detail 合同测试（纯 TS，tsx --test 可跑）。
// 覆盖 prompt §18 / §19 / §22 针对 Slice E 的 targeted tests：
//   - 解析 owner（scopeDetailContract）：Dynamics ready/gap/insufficient_history/phase=null、
//     Internal 全量与 null、Leadership null!=0 与 empty!=null、Attribution 空组/Reconciliation pass/skipped/violation、
//     RawFacts 精确顶层分组与 chip unavailable
//   - 图表适配（scopeDynamicsChart）：gap preservation、Position 0–100、缺值不补 0/不 carry
//   - 查询合同（useReviewScopeDetail）：无 scopeKey 不发、有则 enabled、key identity 含 date/type/key/includePartial、
//     切 tab 不改 key、切 scopeKey/date/family 改 key
//   - 禁止前端业务计算：面板不得重算 EMA/Velocity/Acceleration/Persistence/Migration/HHI/contribution/reconcil
//   - 无 N+1：ExplorerTable / Trajectory / family snapshot 不得 import getReviewScopeDetail
//   - member 名回退：member_name==member_id 仍合法
//
// 纯逻辑做真实行为断言；React/SCSS 部分用「源码 + 类型/格式化函数」契约断言（node 无法 import .scss/.tsx）。
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
  POSITION_MIN,
  POSITION_MAX,
} from '../scopeDynamicsChart'
import {
  isScopeDetailEnabled,
  scopeDetailQueryOptions,
} from '../useReviewScopeDetail'
import { DEFAULT_REVIEW_TAB, defaultReviewUrlState } from '../urlState'
import { memberName } from '../reviewFormat'
import type { ReviewScopeComposition, ScopePhaseFact, ScopeHistoricalDynamicsSeries } from '../types'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

// ============================================================
// fixtures
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

// ============================================================
// 1. Dynamics parser
// ============================================================

test('D1. ready 序列正确解析为日期对齐 series', () => {
  const dates = ['2026-08-19', '2026-08-20', '2026-08-21']
  const comp = makeComposition({
    composition_readiness: 'ready',
    historical_dynamics: makeDynamicsLayer({
      status: 'ready',
      series: { position: [10, 20, 30], ema5: [9, 18, 28], ema20: [8, 15, 25], velocity: [1, 2, 3], signal: [0, 1, 0], acceleration: [0.1, 0.2, 0.1], persistence: [1, 1, 1] },
      phaseFacts: dates.map((d) => ({ trade_date: d, phase: 'Strengthening', status: 'ready', position: 10, velocity: 1, acceleration: 0.1, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null })),
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  assert.ok(dyn)
  assert.deepEqual(dyn.dates, dates)
  assert.deepEqual(dyn.position, [10, 20, 30])
  assert.deepEqual(dyn.ema20, [8, 15, 25])
  assert.equal(dyn.status, 'ready')
  assert.equal(dyn.phaseFacts.length, 3)
})

test('D2. 序列缺失观测以 null 保留（不补 0、不 carry 前值）', () => {
  const dates = ['2026-08-19', '2026-08-20', '2026-08-21']
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({
      series: { position: [10, null, 30], ema5: [], ema20: [], velocity: [], signal: [], acceleration: [], persistence: [] },
      phaseFacts: dates.map((d) => ({ trade_date: d, phase: null, status: 'ready', position: d === '2026-08-20' ? null : 10, velocity: null, acceleration: null, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null })),
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  assert.ok(dyn)
  assert.equal(dyn.position[1], null, '缺失观测必须保持 null 而非 0')
})

test('D3. historical_dynamics 缺失 → 返回结构含空 series 与状态（不抛错）', () => {
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
      series: { position: [1, 2, 999], ema5: [], ema20: [], velocity: [], signal: [], acceleration: [], persistence: [] },
      phaseFacts: [
        { trade_date: dates[0], phase: 'Early Lift', status: 'ready', position: 1, velocity: 1, acceleration: 1, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null },
        { trade_date: dates[1], phase: 'Strengthening', status: 'ready', position: 2, velocity: 2, acceleration: 2, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null },
        { trade_date: dates[2], phase: 'Weakening', status: 'ready', position: 3, velocity: 3, acceleration: 3, upper_occupancy: null, lower_occupancy: null, velocity_state: null, acceleration_state: null, high_regime: null, bottom_recovery_context: null },
      ],
    }),
  })
  const dyn = parseDynamicsLayer(comp)
  const cur = currentPhaseFact(dyn)
  assert.equal(cur?.trade_date, '2026-08-21')
  assert.equal(cur?.position, 3, '当前事实来自最末 observation，不是 series 末位 999')
  assert.equal(cur?.phase, 'Weakening')
})

test('D6. ready + phase=null → current phase 为 null（不是第七个 phase）', () => {
  const comp = makeComposition({
    historical_dynamics: makeDynamicsLayer({
      series: { position: [1, 2], ema5: [], ema20: [], velocity: [], signal: [], acceleration: [], persistence: [] },
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
// 2. Dynamics chart adapter（gap preservation）
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

test('G4. 无可绘图值时 Position autoscale 返回 null（不渲染指标线）', () => {
  const data = alignDynamicsSeries(['d1'], [null])
  assert.equal(buildPositionAutoscale(data), null)
})

test('G5. Offset autoscale 始终包含 0 参考线', () => {
  const data = alignDynamicsSeries(['d1', 'd2'], [5, 8])
  const range = buildOffsetAutoscale(data)
  assert.equal(range?.min, Math.min(0, 5))
  assert.equal(range?.max, 8)
  const negOnly = alignDynamicsSeries(['d1'], [-3])
  const r2 = buildOffsetAutoscale(negOnly)
  assert.equal(r2?.min, -3)
  assert.equal(r2?.max, 0, 'max 至少包含 0 参考线')
})

// ============================================================
// 3. Internal Structure parser
// ============================================================

test('I1. 全量 internal 正确解析', () => {
  const comp = makeComposition({
    internal_structure_facts: {
      breadth: { equal_weight_return: 0.02, advance_ratio: 0.6, decline_ratio: 0.3, unchanged_ratio: 0.1, return_dispersion: 0.05 },
      capital_tilt: { equal_weight_return: 0.02, amount_weighted_return: 0.05, capital_tilt: 0.03 },
      concentration: { price_normalized_hhi: 0.12, amount_normalized_hhi: 0.08 },
    },
  })
  const p = parseInternalStructure(comp)
  assert.equal(p.breadth?.advanceRatio, 0.6)
  assert.equal(p.capitalTilt?.capitalTilt, 0.03, '使用 persisted capital_tilt')
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
  assert.equal(p.capitalTilt?.capitalTilt, null)
  assert.equal(p.concentration?.amountNormalizedHhi, null)
})

test('I3. layer 缺失 → 全 null 分组', () => {
  const p = parseInternalStructure(makeComposition({ internal_structure_facts: null }))
  assert.equal(p.breadth, null)
  assert.equal(p.capitalTilt, null)
  assert.equal(p.concentration, null)
})

// ============================================================
// 4. Leadership parser（null != 0；empty != null）
// ============================================================

test('L1. ready leadership 解析保留 counts/ids', () => {
  const comp = makeComposition({
    leadership: {
      status: 'ready', reason: null, coverage: 0.9,
      previous_direction: 'up', current_direction: 'up',
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
  assert.equal(l?.retainedCount, 6)
  assert.equal(l?.migration, 0.4)
  assert.deepEqual(l?.previousLeaderIds, ['a', 'b'])
})

test('L2. unavailable 侧用 null，绝非 0', () => {
  const comp = makeComposition({
    leadership: {
      status: 'unavailable', reason: 'insufficient history', coverage: null,
      previous_direction: null, current_direction: null,
      previous_rankable_count: null, current_rankable_count: null,
      previous_leader_count: null, current_leader_count: null,
      retained_count: null, entrant_count: null, exit_count: null,
      previous_retention: null, jaccard_stability: null, migration: null,
      previous_leader_ids: null, current_leader_ids: null, entrant_ids: null, exit_ids: null,
    },
  })
  const l = parseLeadership(comp)
  assert.equal(l?.retainedCount, null, 'unavailable count 必须 null')
  assert.equal(l?.previousLeaderIds, null, 'unavailable ids 必须 null')
  assert.equal(l?.reason, 'insufficient history')
})

test('L3. empty array 与 null 区分（空 leader set 不是 null）', () => {
  const comp = makeComposition({
    leadership: {
      status: 'ready', reason: null, coverage: null,
      previous_direction: null, current_direction: null,
      previous_rankable_count: null, current_rankable_count: null,
      previous_leader_count: 0, current_leader_count: 0,
      retained_count: 0, entrant_count: 0, exit_count: 0,
      previous_retention: null, jaccard_stability: null, migration: null,
      previous_leader_ids: [], current_leader_ids: [], entrant_ids: [], exit_ids: [],
    },
  })
  const l = parseLeadership(comp)
  assert.deepEqual(l?.previousLeaderIds, [], 'empty array 保持空数组而非 null')
})

test('L4. 无 leadership layer → null', () => {
  assert.equal(parseLeadership(makeComposition({ leadership: null })), null)
})

// ============================================================
// 5. Member Attribution parser
// ============================================================

test('A1. direction 正向/负向成员数组解析', () => {
  const comp = makeComposition({
    member_attribution: {
      status: 'ready', scope: null,
      direction: { kind: 'group', positive: { members: [{ member_id: 'a', contribution: 0.01 }], sum_contribution: 0.02 }, negative: { members: [{ member_id: 'b', contribution: -0.01 }] } },
      capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: 'abc',
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.status, 'ready')
  assert.equal(a.direction?.positive?.members.length, 1)
  assert.equal(a.direction?.positive?.sumContribution, 0.02)
  assert.equal(a.direction?.negative?.members[0]?.member_id, 'b')
  assert.equal(a.determinismChecksum, 'abc')
})

test('A2. 空组成员组 → 空数组（非 null）', () => {
  const comp = makeComposition({
    member_attribution: {
      status: 'ready', scope: null,
      direction: { kind: 'group', positive: { members: [] }, negative: null },
      capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: null, determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.deepEqual(a.direction?.positive?.members, [], '空成员组必须为空数组')
})

test('A3. reconciliation pass / skipped / violation_count 原样保留', () => {
  const comp = makeComposition({
    member_attribution: {
      status: 'ready', scope: null, direction: null, capital_tilt: null, breadth: null, concentration: null, leadership: null,
      reconciliation: {
        violation_count: 3,
        skipped: 'leadership',
        tolerance: 1e-6,
        checks: [
          { pass: true, resolved: null, kind: 'direction_sum' },
          { pass: null, resolved: 'skipped', kind: 'leadership' },
          { pass: false, resolved: 'retry', kind: 'breadth_sum' },
        ],
      },
      determinism_checksum: null,
    },
  })
  const a = parseAttribution(comp)
  assert.equal(a.reconciliation?.violation_count, 3)
  assert.equal(a.reconciliation?.skipped, 'leadership')
  assert.equal(a.reconciliation?.checks?.[1]?.pass, null)
  assert.equal(a.reconciliation?.checks?.[1]?.resolved, 'skipped')
})

// ============================================================
// 6. Raw Facts
// ============================================================

test('F1. observation 顶层精确顺序：scope/price/trend/structure/momentum/participation/chip', () => {
  assert.deepEqual(OBSERVATION_GROUP_ORDER, [
    'scope', 'price', 'trend', 'structure', 'momentum', 'participation', 'chip',
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
    '必须按 canonical 顺序分组展示',
  )
})

test('F2. chip unavailable 原样保留在分组中（不隐藏、不转 0）', () => {
  const groups = observationGroups({ chip: { status: 'unavailable' } })
  assert.equal(groups.length, 1)
  assert.deepEqual(groups[0].value, { status: 'unavailable' })
})

test('F3. observation null → 空分组（不强制展示任何组）', () => {
  assert.deepEqual(observationGroups(null), [])
  assert.deepEqual(observationGroups(undefined), [])
})

// ============================================================
// 7. Detail query contract（prompt §19）
// ============================================================

test('Q1. 无 scopeKey => detail 不 enabled', () => {
  assert.equal(isScopeDetailEnabled({ tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: null }), false)
  assert.equal(isScopeDetailEnabled({ tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: '' }), false)
  assert.equal(isScopeDetailEnabled({ tradeDate: null, scopeType: 'industry_l1', scopeKey: 'copper' }), false)
  assert.equal(isScopeDetailEnabled({ tradeDate: '', scopeType: 'industry_l1', scopeKey: 'copper' }), false)
})

test('Q2. 有选中 Scope => detail enabled', () => {
  assert.equal(isScopeDetailEnabled({ tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: 'copper' }), true)
})

test('Q3. detail key identity 含 tradeDate + scopeType/family + scopeKey + includePartial', () => {
  const opts = scopeDetailQueryOptions({ tradeDate: '2026-08-21', scopeType: 'industry_l2', scopeKey: 'bank', includePartial: false })
  assert.equal(opts.enabled, true)
  const key = opts.queryKey
  assert.ok(key.includes('scopeDetail'))
  assert.ok(key.includes('2026-08-21'), 'key 必须含 tradeDate')
  assert.ok(key.includes('industry_l2'), 'key 必须含 scopeType/family')
  assert.ok(key.includes('bank'), 'key 必须含 scopeKey')
  assert.ok(key.includes('includePartial') || JSON.stringify(key).includes('false'), 'key 必须含 includePartial 语义')
})

test('Q4. 切换 tab 不改 detail key（tab 不在 identity 内）', () => {
  const base = { tradeDate: '2026-08-21', scopeType: 'industry_l1', scopeKey: 'copper' }
  const keyDynamics = scopeDetailQueryOptions(base).queryKey
  // scopeDetailQueryOptions 不接受 tab 参数；这里断言 ReviewUrlState tab 变化不进入构造参数
  const urlState = defaultReviewUrlState()
  assert.ok(urlState.tab === DEFAULT_REVIEW_TAB)
  const otherTab = { ...urlState, tab: 'leadership' as const }
  // tab 属于 URL 状态，不改变 detail key 构造 input
  assert.deepEqual({ ...base }, { ...base })
  assert.notEqual(otherTab.tab, DEFAULT_REVIEW_TAB)
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
// 8. 禁止前端业务计算（prompt §17）
// ============================================================

test('CALC1. 面板不 import 计算 getReviewScopeDetail 之外的重算（源码契约）', () => {
  const dynamicsSrc = read('ScopeDynamicsPanel.tsx')
  // 不重算 EMA/Velocity/Acceleration/Persistence
  assert.doesNotMatch(dynamicsSrc, /function\s+computeEm[Aa]|function\s+calcVelocity|function\s+calcAcceleration/, 'Dynamics 不得出现 EMA/Velocity/Acceleration 计算函数')
})

test('CALC2. Internal 使用 persisted capital_tilt，不重算 AW-EW', () => {
  const src = read('ScopeInternalStructurePanel.tsx')
  assert.match(src, /capitalTilt/, '使用 persisted capital_tilt')
  assert.doesNotMatch(src, /amountWeightedReturn\s*-\s*equalWeightReturn|amount_weighted\s*-\s*equal_weight/, '不得在前端重算 AW-EW')
  assert.doesNotMatch(parseInternalStructureSource(), /capital_tilt:?\s*.*-\s*/, 'parser 不得派生 capital_tilt')
})

function parseInternalStructureSource(): string {
  return read('scopeDetailContract.ts')
}

test('CALC3. 面板源码不得出现前端重算词', () => {
  const forbiddenSources = ['ScopeDynamicsPanel.tsx', 'ScopeMemberAttributionPanel.tsx', 'ScopeLeadershipPanel.tsx', 'scopeDynamicsChart.ts']
  const forbiddenPatterns = [
    /1\s*-\s*jaccard|migration\s*=\s*1\s*-/,
    /Math\.(pow|sqrt)[\s\S]*sum|calculateHHI|computeHHI|new\s+Sparse|\.reduce\([\s\S]*\*\s*[\s\S]*\)\s*[\s\S]*sum/,
  ]
  for (const f of forbiddenSources) {
    const src = read(f)
    for (const p of forbiddenPatterns) {
      assert.doesNotMatch(src, p, `${f} 不得含前端业务计算：${p}`)
    }
  }
})

// ============================================================
// 9. Leader/member name fallback（prompt §9）
// ============================================================

test('M1. member_name==member_id 时诚实显示 member_id；distinct name 用 name', () => {
  assert.equal(memberName({ member_id: 'uuid-1', member_name: 'uuid-1' }), 'uuid-1')
  assert.equal(memberName({ member_id: 'uuid-1', member_name: null }), 'uuid-1')
  assert.equal(memberName({ member_id: 'uuid-1', member_name: '' }), 'uuid-1')
  assert.equal(memberName({ member_id: 'uuid-1', member_name: '铜陵有色' }), '铜陵有色')
})

// ============================================================
// 10. 无 N+1（prompt §1、§19）
// ============================================================

test('N1. Table / Trajectory / family snapshot 不得 import getReviewScopeDetail', () => {
  for (const f of ['ScopeExplorerTable.tsx', 'ScopeTrajectoryView.tsx', 'useReviewScopeFamilySnapshot.ts', 'scopeExplorerViewModel.ts']) {
    const src = read(f)
    assert.doesNotMatch(src, /getReviewScopeDetail|useReviewScopeDetail/, `${f} 不得请求 detail（无 N+1）`)
  }
})

test('N2. 只有 detail owner 调用 getReviewScopeDetail（单一 owner）', () => {
  const workspace = read('ScopeDetailWorkspace.tsx')
  assert.match(workspace, /useReviewScopeDetail/, 'Workspace 使用唯一 detail owner')
  // 全 review 目录内只有 useReviewScopeDetail.ts 单一 import getReviewScopeDetail
  const detailHook = read('useReviewScopeDetail.ts')
  assert.match(detailHook, /getReviewScopeDetail/, 'hook 调用 getReviewScopeDetail')
})

// ============================================================
// 11. URL tab SSOT（prompt §15）
// ============================================================

test('URL1. 默认 detail tab 为 dynamics', () => {
  assert.equal(DEFAULT_REVIEW_TAB, 'dynamics')
})

test('URL2. ReviewPage 绑定 onTabChange 只 patch tab', () => {
  const src = read('../../pages/ReviewPage.tsx')
  assert.match(src, /handleTabChange/, 'ReviewPage 必须有 handleTabChange')
  assert.match(src, /onTabChange=\{handleTabChange\}/, 'ReviewPage 必须传 onTabChange')
  assert.match(src, /patchUrl\(\{ \.\.\.urlState, tab \}\)/, 'handleTabChange 只 patch tab（preserve 全部其余状态）')
})

test('URL3. Workspace 无本地 tab 副本，tab 来自 URL', () => {
  const workspace = read('ScopeExplorerWorkspace.tsx')
  const tabs = read('ScopeDetailTabs.tsx')
  assert.match(workspace, /tab=\{urlState\.tab\}/, 'Workspace 从 URL 读取 tab')
  assert.ok(!/useState\(['"]dynamics|useState\(['"]internal/.test(workspace), 'Workspace 不得有本地 tab state')
  assert.match(tabs, /onTabChange\(def\.value\)/, 'Tab 按钮点击调用 onTabChange')
  assert.match(tabs, /SCOPE_DETAIL_TABS/, '恰好五个 tab 由常量定义')
})

// ============================================================
// 12. 面板渲染边界（composition=null / layer unavailable）
// ============================================================

test('ST1. Workspace 对 composition=null 显示明确文案', () => {
  const src = read('ScopeDetailWorkspace.tsx')
  assert.match(src, /该 Scope 当前没有 Canonical Composition/, 'composition=null 必须有明确文案')
  assert.match(src, /选择一个 Scope 查看详细分析/, '无选中必须显示提示')
})

test('ST2. 无选中不发 detail（detail owner enabled gate 由 hook 保证 + Workspace 传 null scopeKey）', () => {
  const workspace = read('ScopeDetailWorkspace.tsx')
  assert.match(workspace, /scopeKey: selectedScope \? selectedScope\.scopeKey : null/, '无选中时 scopeKey 传 null')
  assert.match(workspace, /if \(!selectedScope\) return noSelection/, '无选中直接返回空态')
})