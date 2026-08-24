// [R3B] Current Observation Workspace contract 测试（纯 tsx --test）。
//
// 覆盖（R3B spec FE-5/6/8/9/10/11/12/14/16/18）：
// - 8 canonical group 恰好 present、key 自洽、label 非空、facts 为对象
// - UI 区域 → canonical group 映射（Price/Trend/Structure/Momentum/Volume/Context）
// - facts verbatim 保留、不重算
// - chip unavailable 如实识别（不伪造 ready）
// - Observation Context 仅从 observation（L1）读取，不依赖 Composition
// - backend group_key mismatch → fail-closed（ObservationGroupContractError）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  CANONICAL_GROUP_KEYS,
  OBSERVATION_WORKSPACE_AREAS,
  validateCanonicalGroups,
  buildObservationWorkspaceModel,
  extractObservationContext,
  ObservationGroupContractError,
} from '../scopeObservationWorkspaceContract'
import type { ObservationGroup, ObservationGroups } from '../types'

function makeGroup(key: string, label: string, facts: Record<string, unknown> = {}): ObservationGroup {
  return { group_key: key, label, facts }
}

function makeValidGroups(): ObservationGroups {
  return {
    price_capital: makeGroup('price_capital', '价格与资金表现', { equal_weight_return: 0.01 }),
    trend_state: makeGroup('trend_state', '趋势状态', { trend_direction_member_ratio: {} }),
    trend_progress: makeGroup('trend_progress', '趋势进程', { segment_bars: 14 }),
    trend_volume_confirmation: makeGroup('trend_volume_confirmation', '趋势量能确认', { segment_volume_mean_ratio: 1.2 }),
    structure_break_turn: makeGroup('structure_break_turn', '结构突破与转折', { bos_choch_events: {} }),
    structure_evolution_position: makeGroup('structure_evolution_position', '结构演化与位置', { ob_and_eq_events: {} }),
    momentum_squeeze_release: makeGroup('momentum_squeeze_release', '动量与压缩释放', { squeeze_state: 'NONE' }),
    volume_anomaly: makeGroup('volume_anomaly', '量能异常', { volume_ratio20: 1.0 }),
  }
}

// R3B-FE-5：all 8 canonical groups represented exactly once
test('R3B-FE-5: exactly 8 canonical groups present', () => {
  const v = validateCanonicalGroups(makeValidGroups())
  assert.equal(v.exactCount, true)
  assert.equal(v.valid, true)
  const model = buildObservationWorkspaceModel(makeValidGroups())
  assert.equal(model.allGroups.length, 8)
  const keys = model.allGroups.map((g) => g.group_key)
  assert.deepEqual(keys, CANONICAL_GROUP_KEYS)
})

// R3B-FE-6：group heading uses backend group.label
test('R3B-FE-6: group label comes from backend', () => {
  const model = buildObservationWorkspaceModel(makeValidGroups())
  assert.equal(model.allGroups[0].label, '价格与资金表现')
  assert.equal(model.allGroups[1].label, '趋势状态')
})

// R3B-FE-8：Trend contains exactly trend_state/trend_progress/trend_volume_confirmation
test('R3B-FE-8: Trend area owns exactly 3 canonical groups', () => {
  const trend = OBSERVATION_WORKSPACE_AREAS.find((a) => a.areaKey === 'trend')
  assert.ok(trend)
  assert.deepEqual([...trend.groupKeys], ['trend_state', 'trend_progress', 'trend_volume_confirmation'])
})

// R3B-FE-9：Structure contains exactly structure_break_turn/structure_evolution_position
test('R3B-FE-9: Structure area owns exactly 2 canonical groups', () => {
  const structure = OBSERVATION_WORKSPACE_AREAS.find((a) => a.areaKey === 'structure')
  assert.ok(structure)
  assert.deepEqual([...structure.groupKeys], ['structure_break_turn', 'structure_evolution_position'])
})

// R3B-FE-10：Price/Momentum/Volume each own their expected canonical group
test('R3B-FE-10: Price/Momentum/Volume single-group ownership', () => {
  const byKey = Object.fromEntries(OBSERVATION_WORKSPACE_AREAS.map((a) => [a.areaKey, a.groupKeys]))
  assert.deepEqual([...byKey.price], ['price_capital'])
  assert.deepEqual([...byKey.momentum], ['momentum_squeeze_release'])
  assert.deepEqual([...byKey.volume], ['volume_anomaly'])
})

// R3B-FE-11：facts verbatim preserved, no recompute
test('R3B-FE-11: facts preserved verbatim (no normalize/recompute)', () => {
  const groups = makeValidGroups()
  const model = buildObservationWorkspaceModel(groups)
  const g1 = model.allGroups[0]
  assert.strictEqual(g1.facts, groups.price_capital.facts) // 同一引用，未复制
  assert.equal(g1.facts.equal_weight_return, 0.01)
})

// R3B-FE-16：chip unavailable remains visibly unavailable (not 0 / not ready)
test('R3B-FE-16: chip unavailable truthfully detected', () => {
  const ctxUnavail = extractObservationContext({ chip: { status: 'unavailable' } })
  assert.equal(ctxUnavail.chipAvailability, 'unavailable')
  const ctxAbsent = extractObservationContext({})
  assert.equal(ctxAbsent.chipAvailability, 'absent')
  // 无明确 available 标记 → 保守 unavailable，不伪造 ready
  const ctxAmbiguous = extractObservationContext({ chip: { foo: 1 } })
  assert.equal(ctxAmbiguous.chipAvailability, 'unavailable')
})

// R3B-FE-13/12：Observation Context reads Observation only
test('R3B-FE-13: observation context derives from observation (L1)', () => {
  const ctx = extractObservationContext({
    structure: { current_state: { board_ready_member_count: 10 } },
    freshness: { today_count: 0 },
  })
  assert.equal(ctx.hasCurrentState, true)
  assert.equal(ctx.hasFreshness, true)
  // 不依赖 composition 字段
  assert.equal(ctx.chipAvailability, 'absent')
})

// R3B-FE-18：backend group_key mismatch fails closed
test('R3B-FE-18: canonical contract violation fails closed (no silent relabel)', () => {
  const bad = makeValidGroups()
  // 故意破坏 group_key 自洽性（container key 与 group_key 不一致）
  bad.price_capital = makeGroup('WRONG_KEY', '价格与资金表现', {})
  assert.throws(() => buildObservationWorkspaceModel(bad), ObservationGroupContractError)

  const missing = makeValidGroups()
  delete (missing as Partial<ObservationGroups>).volume_anomaly
  assert.throws(() => buildObservationWorkspaceModel(missing), ObservationGroupContractError)

  const emptyLabel = makeValidGroups()
  emptyLabel.trend_state = makeGroup('trend_state', '   ', {})
  assert.throws(() => buildObservationWorkspaceModel(emptyLabel), ObservationGroupContractError)

  const badFacts = makeValidGroups()
  badFacts.momentum_squeeze_release = makeGroup('momentum_squeeze_release', '动量', null as unknown as Record<string, unknown>)
  assert.throws(() => buildObservationWorkspaceModel(badFacts), ObservationGroupContractError)
})

// R3B §8：null/undefined groups → invalid, not silently relabeled
test('R3B: null/undefined observationGroups invalid', () => {
  assert.equal(validateCanonicalGroups(null).valid, false)
  assert.equal(validateCanonicalGroups(undefined).valid, false)
  assert.equal(validateCanonicalGroups({} as ObservationGroups).valid, false)
})

// R3B-V A：ObservationGroup 只依赖 canonical 结构字段，无 group.status 依赖
test('R3B-V A: model does not depend on group.status', () => {
  const groups = makeValidGroups()
  // 故意加一个 group-level status（违反 backend contract），adapter 不应读取它
  ;(groups.price_capital as unknown as Record<string, unknown>).status = 'unavailable'
  const model = buildObservationWorkspaceModel(groups)
  // 模型成功构建且 facts verbatim，证明它没用 group.status 做决策
  assert.equal(model.allGroups.length, 8)
  assert.strictEqual(model.allGroups[0].facts, groups.price_capital.facts)
})

// R3B-V D：individual unavailable fact 经 adapter verbatim 保留（不解读）
test('R3B-V D: per-fact unavailable object preserved verbatim', () => {
  const groups = makeValidGroups()
  const unavailableFact = { status: 'unavailable', reason: 'readiness_200_not_met' }
  groups.volume_anomaly = makeGroup('volume_anomaly', '量能异常', { volume_ratio200: unavailableFact })
  const model = buildObservationWorkspaceModel(groups)
  const g8 = model.allGroups[7]
  const passed = (g8.facts as Record<string, unknown>).volume_ratio200
  assert.strictEqual(passed, unavailableFact) // 同引用，未复制/未解读
  assert.deepEqual(passed, { status: 'unavailable', reason: 'readiness_200_not_met' })
})
