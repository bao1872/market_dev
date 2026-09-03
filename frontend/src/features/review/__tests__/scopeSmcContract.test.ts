import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  parseSmcObservation,
  buildSmcVM,
  parseSmcHistory,
  smcDisplayMember,
  classifyTransition,
} from '../scopeSmcContract'
import type { ReviewScopeSmcHistoryDTO, ReviewStructureEvents } from '../types'
import type { MemberDirectory } from '../reviewFormat'

type Json = Record<string, unknown>

// canonical observation.structure fixture（真实 shape：field=field，状态 up/neutral/down）
const obs: Json = {
  structure: {
    swing: {
      state: { up_count: 6, up_ratio: 0.6, neutral_count: 2, neutral_ratio: 0.2, down_count: 2, down_ratio: 0.2, denominator: 10 },
      transition: {
        'Up→Down': { count: 1, ratio: 0.1 },
        'Neutral→Up': { count: 1, ratio: 0.1 },
        denominator: 10,
        changed_members: [
          { member_id: 'm001', previous_state: 'Neutral', current_state: 'Up' },
          { member_id: 'm002', previous_state: 'Up', current_state: 'Down' },
        ],
      },
    },
    internal: {
      state: { up_count: 4, up_ratio: 0.4, neutral_count: 3, neutral_ratio: 0.3, down_count: 3, down_ratio: 0.3, denominator: 10 },
      transition: {
        denominator: 10,
        changed_members: [{ member_id: 'm003', previous_state: 'Down', current_state: 'Up' }],
      },
    },
    alignment: { aligned_count: 7, aligned_ratio: 0.7, divergent_count: 3, divergent_ratio: 0.3, denominator: 10 },
    distance_to_trailing_top_pct: { median: 2.5, p25: 1.0, p75: 4.0, valid_count: 9, denominator: 10 },
    distance_to_trailing_bottom_pct: { median: -1.5, p25: -3.0, p75: -0.5, valid_count: 9, denominator: 10 },
    events: {
      status: 'ready',
      denominator: 5,
      cells: {
        leveled: {
          BOS_Up_Swing: { event_type: 'BOS', direction: 'Up', structure_level: 'Swing', event_count: 2, member_count: 1, member_ratio: 0.2 },
          CHOCH_Down_Internal: { event_type: 'CHoCH', direction: 'Down', structure_level: 'Internal', event_count: 1, member_count: 1, member_ratio: 0.2 },
          OB_CREATED_Up_Swing: { event_type: 'OB_CREATED', direction: 'Up', structure_level: 'Swing', event_count: 3, member_count: 2, member_ratio: 0.4 },
        },
        extreme: {
          EQH: { event_count: 1, member_count: 1, member_ratio: 0.2 },
          EQL: { event_count: 2, member_count: 2, member_ratio: 0.4 },
        },
      },
    },
  },
}

function eventsObs(events: Json): Json {
  return { structure: { swing: { state: {}, transition: {} }, internal: { state: {}, transition: {} }, events } }
}

test('SMC 1: Swing/Internal state consumes canonical up/neutral/down keys', () => {
  const facts = parseSmcObservation(obs)
  assert.ok(facts.swingState)
  assert.equal(facts.swingState!.upRatio, 0.6)
  assert.equal(facts.swingState!.neutralRatio, 0.2)
  assert.equal(facts.swingState!.downRatio, 0.2)
  assert.equal(facts.swingState!.denominator, 10)
  assert.ok(facts.internalState)
  assert.equal(facts.internalState!.upRatio, 0.4)
  assert.equal(facts.internalState!.downRatio, 0.3)
})

test('SMC 2: BOS/CHoCH level+direction decode (event_type/direction/structure_level)', () => {
  const vm = buildSmcVM(parseSmcObservation(obs))
  assert.ok(vm.events)
  const bosUpSwing = vm.events!.bosChoch.find(
    (c) => c.eventType === 'BOS' && c.direction === 'Up' && c.structureLevel === 'Swing',
  )
  assert.ok(bosUpSwing, 'BOS Up Swing 必须被解码为 primary')
  const chochInternal = vm.events!.bosChoch.find(
    (c) => c.eventType === 'CHoCH' && c.structureLevel === 'Internal',
  )
  assert.ok(chochInternal, 'CHoCH Internal 必须被解码为 primary')
  // OB_CREATED 属于 secondary，绝不能混入 BOS/CHoCH 主事件
  assert.equal(
    vm.events!.bosChoch.find((c) => c.eventType === 'OB_CREATED'),
    undefined,
    'OB_CREATED 不得混入 BOS/CHoCH 主事件',
  )
})

test('SMC 3: member_count ≠ event_count contract (必须分别保留，不得混成一个数字)', () => {
  const vm = buildSmcVM(parseSmcObservation(obs))
  const bosUpSwing = vm.events!.bosChoch.find((c) => c.eventType === 'BOS' && c.structureLevel === 'Swing')!
  // 同一次事件：2 次 tick，来自 1 个成员
  assert.equal(bosUpSwing.eventCount, 2)
  assert.equal(bosUpSwing.memberCount, 1)
  assert.notEqual(bosUpSwing.eventCount, bosUpSwing.memberCount, 'member_count 与 event_count 不可相等混用')
  const obSec = vm.events!.secondary.find((c) => c.eventType === 'OB_CREATED')!
  assert.equal(obSec.eventCount, 3)
  assert.equal(obSec.memberCount, 2)
})

test('SMC 4: event denominator unavailable vs zero-event 必须区分', () => {
  // unavailable：denominator == null（覆盖不可用）
  const unavailable = parseSmcObservation(
    eventsObs({ status: 'unavailable', denominator: null, cells: { leveled: {}, extreme: {} } }),
  )
  assert.ok(unavailable.events)
  assert.equal(unavailable.events!.status, 'unavailable')
  assert.equal(unavailable.events!.denominator, null, 'unavailable 的 denominator 必须为 null（不是 0）')

  // ready + 空 cells：denominator == 0，但状态是 ready（今日无事件，不是不可用）
  const zeroEvent = parseSmcObservation(
    eventsObs({ status: 'ready', denominator: 0, cells: { leveled: {}, extreme: {} } }),
  )
  assert.ok(zeroEvent.events)
  assert.equal(zeroEvent.events!.status, 'ready')
  assert.equal(zeroEvent.events!.denominator, 0)
  assert.equal(zeroEvent.events!.bosChoch.length, 0)
})

test('SMC 5: changed members deterministic by member_id + memberDirectory 统一展示', () => {
  const facts = parseSmcObservation(obs)
  // Swing changed_members 按 member_id 稳定排序（m001 在 m002 前）
  assert.deepEqual(
    facts.swingChangedMembers.map((m) => m.memberId),
    ['m001', 'm002'],
  )
  assert.equal(facts.swingChangedMembers[0].previousState, 'Neutral')
  assert.equal(facts.swingChangedMembers[0].currentState, 'Up')
  assert.deepEqual(
    facts.internalChangedMembers.map((m) => m.memberId),
    ['m003'],
  )

  const dir: MemberDirectory = {
    m001: { symbol: '600000', name: '浦发银行' },
    m002: { symbol: '600036', name: '招商银行' },
    m003: { symbol: '601398', name: '工商银行' },
  }
  assert.equal(smcDisplayMember('m001', dir), '浦发银行 · 600000')
  assert.equal(smcDisplayMember('m002', dir), '招商银行 · 600036')
})

test('SMC 6: null / unavailable / gap 必须保留（不得写成 0 或“无事件”）', () => {
  // 完全无 observation
  const empty = parseSmcObservation(null)
  assert.equal(empty.swingState, null)
  assert.equal(empty.events, null)
  assert.deepEqual(empty.swingChangedMembers, [])

  // history 全 null
  const histNull = parseSmcHistory(null)
  assert.deepEqual(histNull.dates, [])
  assert.deepEqual(histNull.eventTape, [])

  // history 某日 payload 缺失（gap）vs 某日 events unavailable
  const histFixture: ReviewScopeSmcHistoryDTO = {
    dates: ['2024-01-02', '2024-01-03'],
    swing_state: [null, { up_ratio: 0.5, up_count: 5, neutral_ratio: 0.3, neutral_count: 3, down_ratio: 0.2, down_count: 2, denominator: 10 }],
    internal_state: [null, null],
    event_tape: [
      null, // gap：该日正式 run 无 fact
      { status: 'unavailable', denominator: null, cells: { leveled: {}, extreme: {} } } as ReviewStructureEvents,
    ],
  }
  const h = parseSmcHistory(histFixture)
  assert.equal(h.eventTape[0].vm, null, 'gap 日 vm 必须为 null（保留 date slot）')
  assert.equal(h.eventTape[1].vm?.status, 'unavailable', 'unavailable 日状态必须保留')
  assert.equal(h.swingState[0].vm, null, 'gap 日 swing_state vm 必须为 null')
})

test('SMC 7: SQZ_RELEASE 等 Momentum 事件不得进入 SMC 任何投影（既非 bosChoch 也非 secondary）', () => {
  // canonical 仍允许保存 SQZ_RELEASE（不在此处改）；但 SMC 投影按 whitelist 过滤。
  const vm = buildSmcVM(
    parseSmcObservation(
      eventsObs({
        status: 'ready',
        denominator: 3,
        cells: {
          leveled: {
            SQZ_RELEASE_Up_Swing: { event_type: 'SQZ_RELEASE', direction: 'Up', structure_level: 'Swing', event_count: 1, member_count: 1, member_ratio: 0.3 },
          },
          extreme: {},
        },
      }),
    ),
  )
  // SQZ_RELEASE（Momentum 事件，不在 SMC whitelist）既不进入 primary（BOS/CHoCH），也不进入 secondary。
  assert.equal(
    vm.events!.bosChoch.find((c) => c.eventType === 'SQZ_RELEASE'),
    undefined,
    'SQZ_RELEASE 不得作为 SMC 主事件 (bosChoch)',
  )
  assert.equal(
    vm.events!.secondary.find((c) => c.eventType === 'SQZ_RELEASE'),
    undefined,
    'SQZ_RELEASE 不得进入 SMC secondary（canonical raw Observation 仍允许保存该事件）',
  )
})

test('SMC 9: Swing/Internal transition 必须按 denominator 区分 unavailable / no_changes / changed', () => {
  // 构造仅含 swing/internal transition 的最小 observation（state 留空不影响 classification）。
  function transitionObs(swing: Json, internal: Json): Json {
    return { structure: { swing: { state: {}, transition: swing }, internal: { state: {}, transition: internal } } }
  }
  const changed = [{ member_id: 'm001', previous_state: 'Up', current_state: 'Down' }]

  // 场景 A：denominator == 0（ready 但 T-1/T 共同有效成员不足）→ unavailable
  const zeroObs = transitionObs(
    { denominator: 0, changed_members: [] },
    { denominator: 0, changed_members: [] },
  )
  const zeroFacts = parseSmcObservation(zeroObs)
  assert.equal(classifyTransition(zeroFacts.transitionDenominator.swing, zeroFacts.swingChangedMembers), 'unavailable')
  assert.equal(classifyTransition(zeroFacts.transitionDenominator.internal, zeroFacts.internalChangedMembers), 'unavailable')

  // 场景 B：denominator > 0 且无变化成员 → no_changes
  const noChangeObs = transitionObs(
    { denominator: 8, changed_members: [] },
    { denominator: 8, changed_members: [] },
  )
  const noChangeFacts = parseSmcObservation(noChangeObs)
  assert.equal(classifyTransition(noChangeFacts.transitionDenominator.swing, noChangeFacts.swingChangedMembers), 'no_changes')
  assert.equal(classifyTransition(noChangeFacts.transitionDenominator.internal, noChangeFacts.internalChangedMembers), 'no_changes')

  // 场景 C：denominator > 0 且有变化成员 → changed
  const changedObs = transitionObs(
    { denominator: 8, changed_members: changed },
    { denominator: 8, changed_members: changed },
  )
  const changedFacts = parseSmcObservation(changedObs)
  assert.equal(classifyTransition(changedFacts.transitionDenominator.swing, changedFacts.swingChangedMembers), 'changed')
  assert.equal(classifyTransition(changedFacts.transitionDenominator.internal, changedFacts.internalChangedMembers), 'changed')
  assert.equal(changedFacts.swingChangedMembers[0].memberId, 'm001')
  assert.equal(changedFacts.internalChangedMembers[0].memberId, 'm001')

  // denominator == null 同样判为 unavailable（覆盖不可用）
  assert.equal(classifyTransition(null, []), 'unavailable')
})

test('SMC 10: SMC 事件 whitelist 路由（BOS/CHoCH→primary，OB_*/EQH/EQL→secondary）', () => {
  const vm = buildSmcVM(parseSmcObservation(obs))
  assert.ok(vm.events)
  // primary 仅含 whitelist primary
  const primaryTypes = vm.events!.bosChoch.map((c) => c.eventType).sort()
  assert.deepEqual(primaryTypes, ['BOS', 'CHoCH'])
  // secondary 含 whitelist secondary（OB_CREATED / EQH / EQL）
  const secondaryTypes = vm.events!.secondary.map((c) => c.eventType).sort()
  assert.deepEqual(secondaryTypes, ['EQH', 'EQL', 'OB_CREATED'])
  // 任何非 whitelist 事件都不得出现在 SMC VM
  for (const c of [...vm.events!.bosChoch, ...vm.events!.secondary]) {
    assert.ok(
      ['BOS', 'CHoCH', 'OB_CREATED', 'OB_ENTERED', 'OB_MITIGATED', 'EQH', 'EQL'].includes(c.eventType),
      `SMC VM 不得含非 whitelist 事件 ${c.eventType}`,
    )
  }
})

test('SMC 11: trailing distance 按 percentage points 显示（2.5 → "2.50%"，绝不 x100）', () => {
  const vm = buildSmcVM(parseSmcObservation(obs))
  // fixture: distance_to_trailing_top_pct.median = 2.5, p25 = 1.0, p75 = 4.0
  assert.equal(vm.trailingTop.median, '2.50%')
  assert.equal(vm.trailingTop.p25, '1.00%')
  assert.equal(vm.trailingTop.p75, '4.00%')
  // bottom 同样为 percentage points（负中位 -1.5 → "-1.50%"）
  assert.equal(vm.trailingBottom.median, '-1.50%')
})

test('SMC 8: ScopeSmcPanel 必须消费 scopeSmcContract（不得 deepGet raw observation）', () => {
  const panelSrc = readFileSync(new URL('../ScopeSmcPanel.tsx', import.meta.url), 'utf8')
  assert.ok(panelSrc.includes('parseSmcObservation('), 'ScopeSmcPanel 必须调用 contract parseSmcObservation')
  assert.ok(
    !/observation\s*\[\s*["']structure/.test(panelSrc),
    'ScopeSmcPanel 不得直接 deepGet raw observation.structure',
  )
})
