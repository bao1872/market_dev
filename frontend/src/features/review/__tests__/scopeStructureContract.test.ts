// [R3D] scopeStructureContract registered contract test.
// Locks: no frontend recomputation, three event availability states, leveled key
// opaqueness, structure level, event-type validity, extremes, direction tone,
// alignment, trailing distance scale, G5/G6 shared denominator, formal wiring.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseStructureBreakTurn,
  parseStructureEvolutionPosition,
  parseStructureAlignment,
  parseCurrentOnlyDistance,
  directionTone,
  formatMemberRatioNullable,
  formatTrailingDistanceNullable,
  type StructureBreakTurnVM,
  type StructureEvolutionPositionVM,
} from '../scopeStructureContract'

// ----- §27 event denominator (no recompute) -----
test('member_ratio is persisted, never event_count/denominator recompute', () => {
  const g5 = {
    bos_choch_events: {
      status: 'ready',
      denominator: 40,
      cells: {
        leveled: {
          'opaque-cell-123': {
            event_type: 'BOS',
            direction: 'bullish',
            structure_level: 'Swing',
            event_count: 7, // intentionally inconsistent with member_count/ratio
            member_count: 5,
            member_ratio: 0.37, // deliberately NOT 5/40 or 7/40
          },
        },
        extreme: {},
      },
    },
  }
  const vm = parseStructureBreakTurn(g5) as StructureBreakTurnVM
  assert.equal(vm.availability, 'ready')
  assert.equal(vm.denominator, 40)
  assert.equal(vm.leveled.length, 1)
  // persisted ratio kept verbatim
  assert.equal(vm.leveled[0].memberRatio, 0.37)
  assert.equal(formatMemberRatioNullable(vm.leveled[0].memberRatio), '37.0%')
  // member_count / denominator is display composition only
  assert.equal(vm.leveled[0].memberCount, 5)
  assert.equal(vm.leveled[0].eventCount, 7)
})

// ----- §1/§2/§3 real producer event states (G5) -----
test('G5 real event states: unavailable / ready-empty / ready-events / ready-denom=0 fail-closed', () => {
  const unavailable = parseStructureBreakTurn({
    bos_choch_events: { status: 'unavailable', denominator: null, cells: { leveled: {}, extreme: {} } },
  }) as StructureBreakTurnVM
  assert.equal(unavailable.availability, 'unavailable')
  assert.equal(unavailable.contractInvalid, false)
  assert.equal(unavailable.denominator, null)
  assert.equal(unavailable.leveled.length, 0)

  const zeroEvent = parseStructureBreakTurn({
    bos_choch_events: { status: 'ready', denominator: 40, cells: { leveled: {}, extreme: {} } },
  }) as StructureBreakTurnVM
  assert.equal(zeroEvent.availability, 'ready')
  assert.equal(zeroEvent.zeroEventToday, true)
  assert.equal(zeroEvent.denominator, 40)

  const withEvents = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 40,
      cells: {
        leveled: { c1: { event_type: 'BOS', direction: 'up', structure_level: 'Swing', event_count: 1, member_count: 1, member_ratio: 0.08 } },
        extreme: {},
      },
    },
  }) as StructureBreakTurnVM
  assert.equal(withEvents.zeroEventToday, false)
  assert.equal(withEvents.leveled.length, 1)

  // status=ready + denominator=0 is NOT a production state -> fail closed (R3D-V §1/§3)
  const zeroDenom = parseStructureBreakTurn({
    bos_choch_events: { status: 'ready', denominator: 0, cells: { leveled: {}, extreme: {} } },
  }) as StructureBreakTurnVM
  assert.equal(zeroDenom.contractInvalid, true)
  assert.equal(zeroDenom.denominator, 0)
})

// ----- §2 outer status fail-closed (G5) -----
test('G5 outer status must be ready/unavailable; missing/unknown -> contract invalid', () => {
  const missing = parseStructureBreakTurn({
    bos_choch_events: { denominator: 40, cells: { leveled: {}, extreme: {} } },
  }) as StructureBreakTurnVM
  assert.equal(missing.contractInvalid, true)

  const unknown = parseStructureBreakTurn({
    bos_choch_events: { status: 'weird', denominator: 40, cells: { leveled: {}, extreme: {} } },
  }) as StructureBreakTurnVM
  assert.equal(unknown.contractInvalid, true)
})

// ----- §4 G6 availability preserved from persisted status -----
test('G6 event availability A-F (unavailable / ready-empty / ready-events / ready-denom=0 / missing / unknown)', () => {
  const mk = (payload: unknown) => {
    const g6 = parseStructureEvolutionPosition({
      ob_and_eq_events: payload,
    }) as StructureEvolutionPositionVM
    return g6.events!
  }

  // A. unavailable + denominator null + empty
  const a = mk({ status: 'unavailable', denominator: null, cells: { leveled: {}, extreme: {} } })
  assert.equal(a.availability, 'unavailable')
  assert.equal(a.contractInvalid, false)
  assert.equal(a.denominator, null)
  assert.equal(a.zeroEventToday, false)

  // B. ready + denominator 40 + empty -> zeroEventToday
  const b = mk({ status: 'ready', denominator: 40, cells: { leveled: {}, extreme: {} } })
  assert.equal(b.availability, 'ready')
  assert.equal(b.zeroEventToday, true)
  assert.equal(b.denominator, 40)

  // C. ready + denominator 40 + events
  const c = mk({
    status: 'ready',
    denominator: 40,
    cells: {
      leveled: { c1: { event_type: 'OB_CREATED', direction: 'bullish', structure_level: 'Swing', event_count: 2, member_count: 2, member_ratio: 0.05 } },
      extreme: { EQH: { event_count: 1, member_count: 1, member_ratio: 0.025 } },
    },
  })
  assert.equal(c.availability, 'ready')
  assert.equal(c.zeroEventToday, false)
  assert.equal(c.leveled.length, 1)
  assert.equal(c.extreme.length, 1)

  // D. ready + denominator 0 -> fail closed
  const d = mk({ status: 'ready', denominator: 0, cells: { leveled: {}, extreme: {} } })
  assert.equal(d.contractInvalid, true)

  // E. missing status -> invalid
  const e = mk({ denominator: 40, cells: { leveled: {}, extreme: {} } })
  assert.equal(e.contractInvalid, true)

  // F. unknown status -> invalid
  const f = mk({ status: 'pending', denominator: 40, cells: { leveled: {}, extreme: {} } })
  assert.equal(f.contractInvalid, true)
})

// ----- §29 leveled key opaqueness -----
test('parser uses cell fields, not the opaque outer key', () => {
  const vm = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 30,
      cells: {
        leveled: {
          'opaque-cell-xyz-789': {
            event_type: 'CHoCH',
            direction: 'down',
            structure_level: 'Internal',
            event_count: 3,
            member_count: 2,
            member_ratio: 0.5,
          },
        },
        extreme: {},
      },
    },
  }) as StructureBreakTurnVM
  const cell = vm.leveled[0]
  assert.equal(cell.eventType, 'CHoCH')
  assert.equal(cell.direction, 'down')
  assert.equal(cell.structureLevel, 'Internal')
  assert.equal(cell.cellKey, 'opaque-cell-xyz-789')
  assert.equal(directionTone(cell.direction), 'down')
})

// ----- §30 structure level -----
test('structure_level Swing/Internal; missing fails closed', () => {
  const swing = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 10,
      cells: { leveled: { c1: { event_type: 'BOS', direction: 'up', structure_level: 'Swing', event_count: 1, member_count: 1, member_ratio: 0.1 } }, extreme: {} },
    },
  }) as StructureBreakTurnVM
  assert.equal(swing.leveled[0].structureLevel, 'Swing')
  assert.equal(swing.leveled[0].malformed, false)

  const internal = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 10,
      cells: { leveled: { c1: { event_type: 'BOS', direction: 'up', structure_level: 'Internal', event_count: 1, member_count: 1, member_ratio: 0.1 } }, extreme: {} },
    },
  }) as StructureBreakTurnVM
  assert.equal(internal.leveled[0].structureLevel, 'Internal')

  // missing structure_level -> malformed, fail closed (no default Swing/Internal)
  const missing = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 10,
      cells: { leveled: { c1: { event_type: 'BOS', direction: 'up', event_count: 1, member_count: 1, member_ratio: 0.1 } }, extreme: {} },
    },
  }) as StructureBreakTurnVM
  assert.equal(missing.leveled[0].structureLevel, null)
  assert.equal(missing.leveled[0].malformed, true)
})

// ----- §31 event type validity -----
test('unexpected event types fail closed (G5 vs G6 separation)', () => {
  // SQZ_RELEASE inside G5 -> contract invalidity
  const g5bad = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 10,
      cells: { leveled: { c1: { event_type: 'SQZ_RELEASE', direction: 'up', structure_level: 'Swing', event_count: 1, member_count: 1, member_ratio: 0.1 } }, extreme: {} },
    },
  }) as StructureBreakTurnVM
  assert.equal(g5bad.hasContractInvalidity, true)
  assert.equal(g5bad.leveled.length, 0)

  // OB_CREATED inside G5 -> contract invalidity
  const g5ob = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 10,
      cells: { leveled: { c1: { event_type: 'OB_CREATED', direction: 'up', structure_level: 'Swing', event_count: 1, member_count: 1, member_ratio: 0.1 } }, extreme: {} },
    },
  }) as StructureBreakTurnVM
  assert.equal(g5ob.hasContractInvalidity, true)

  // OB_CREATED accepted inside G6 leveled
  const g6 = parseStructureEvolutionPosition({
    ob_and_eq_events: {
      status: 'ready',
      denominator: 20,
      cells: {
        leveled: { c1: { event_type: 'OB_CREATED', direction: 'bullish', structure_level: 'Swing', event_count: 2, member_count: 2, member_ratio: 0.1 } },
        extreme: {},
      },
    },
    structure_alignment: { aligned_count: 6, aligned_ratio: 0.6, divergent_count: 4, divergent_ratio: 0.4, denominator: 10 },
    distance_to_trailing_top_pct: { median: 4.2, p25: 2.1, p75: 6.5, valid_count: 38, denominator: 40 },
    distance_to_trailing_bottom_pct: { median: -6.1, p25: -8.0, p75: -3.0, valid_count: 38, denominator: 40 },
  }) as StructureEvolutionPositionVM
  assert.equal(g6.events?.leveled.length, 1)
  assert.equal(g6.events?.leveled[0].eventType, 'OB_CREATED')
})

// ----- §32 extremes EQH/EQL -----
test('extreme EQH/EQL: key is event type, no direction/level invented', () => {
  const g6 = parseStructureEvolutionPosition({
    ob_and_eq_events: {
      status: 'ready',
      denominator: 20,
      cells: {
        leveled: {},
        extreme: {
          EQH: { event_count: 3, member_count: 2, member_ratio: 0.15 },
          EQL: { event_count: 5, member_count: 3, member_ratio: 0.25 },
        },
      },
    },
  }) as StructureEvolutionPositionVM
  assert.equal(g6.events?.extreme.length, 2)
  const eqh = g6.events?.extreme.find((e) => e.eventType === 'EQH')!
  const eql = g6.events?.extreme.find((e) => e.eventType === 'EQL')!
  assert.equal(eqh.memberRatio, 0.15)
  assert.equal(eqh.memberCount, 2)
  assert.equal(eqh.eventCount, 3)
  assert.equal(eql.memberRatio, 0.25)
  assert.equal(formatMemberRatioNullable(eql.memberRatio), '25.0%')
})

// ----- §33 direction tone -----
test('direction token verbatim; tone maps bullish/up->red, bearish/down->green, unknown->neutral', () => {
  assert.equal(directionTone('bullish'), 'up')
  assert.equal(directionTone('up'), 'up')
  assert.equal(directionTone('bearish'), 'down')
  assert.equal(directionTone('down'), 'down')
  assert.equal(directionTone('custom-token'), 'neutral')
  assert.equal(directionTone(null), 'neutral')
  // display token stays verbatim (no up->bullish relabel)
  const vm = parseStructureBreakTurn({
    bos_choch_events: {
      status: 'ready',
      denominator: 10,
      cells: { leveled: { c1: { event_type: 'BOS', direction: 'up', structure_level: 'Swing', event_count: 1, member_count: 1, member_ratio: 0.1 } }, extreme: {} },
    },
  }) as StructureBreakTurnVM
  assert.equal(vm.leveled[0].direction, 'up')
})

// ----- §34 alignment -----
test('alignment persisted ratios; denominator 0 -> no valid members', () => {
  const ready = parseStructureAlignment({
    structure_alignment: { aligned_count: 18, aligned_ratio: 0.6, divergent_count: 12, divergent_ratio: 0.4, denominator: 30 },
  })
  assert.equal(ready?.alignedRatio, 0.6)
  assert.equal(ready?.divergentRatio, 0.4)
  assert.equal(ready?.zeroDenominator, false)

  // Real producer: categorical_state_distribution with denominator=0 -> ratios null
  const zero = parseStructureAlignment({
    structure_alignment: { aligned_count: 0, aligned_ratio: null, divergent_count: 0, divergent_ratio: null, denominator: 0 },
  })
  assert.equal(zero?.zeroDenominator, true)
  assert.equal(zero?.alignedRatio, null)
  assert.equal(zero?.divergentRatio, null)
})

// ----- §35/§36 trailing distance scale + passthrough -----
test('trailing distance is percentage points, no x100', () => {
  const top = parseCurrentOnlyDistance(
    { distance_to_trailing_top_pct: { median: 4.2, p25: 1.1, p75: 8.3, valid_count: 38, denominator: 40 } },
    'distance_to_trailing_top_pct',
  )
  assert.equal(formatTrailingDistanceNullable(top?.median), '4.20%')
  assert.equal(formatTrailingDistanceNullable(top?.p25), '1.10%')
  assert.equal(formatTrailingDistanceNullable(top?.p75), '8.30%')
  assert.equal(top?.validCount, 38)
  assert.equal(top?.denominator, 40)

  const bottom = parseCurrentOnlyDistance(
    { distance_to_trailing_bottom_pct: { median: -6.1, p25: -9.0, p75: -2.0, valid_count: 38, denominator: 40 } },
    'distance_to_trailing_bottom_pct',
  )
  assert.equal(formatTrailingDistanceNullable(bottom?.median), '-6.10%')

  const zero = parseCurrentOnlyDistance(
    { distance_to_trailing_top_pct: { median: 0, p25: 0, p75: 0, valid_count: 10, denominator: 10 } },
    'distance_to_trailing_top_pct',
  )
  assert.equal(formatTrailingDistanceNullable(zero?.median), '0.00%')

  const unavail = parseCurrentOnlyDistance(
    { distance_to_trailing_top_pct: { status: 'unavailable', reason: 'no coverage', valid_count: 0 } },
    'distance_to_trailing_top_pct',
  )
  assert.equal(unavail?.unavailable, true)
  assert.equal(formatTrailingDistanceNullable(unavail?.median), '—')
})

// ----- §37 G5/G6 shared denominator preserved -----
test('G5 and G6 each preserve their persisted denominator; no derived denominator', () => {
  const denom = 40
  const g5 = parseStructureBreakTurn({
    bos_choch_events: { status: 'ready', denominator: denom, cells: { leveled: { c1: { event_type: 'BOS', direction: 'up', structure_level: 'Swing', event_count: 7, member_count: 5, member_ratio: 0.37 } }, extreme: {} } },
  }) as StructureBreakTurnVM
  const g6 = parseStructureEvolutionPosition({
    ob_and_eq_events: {
      status: 'ready',
      denominator: denom,
      cells: {
        leveled: { c1: { event_type: 'OB_CREATED', direction: 'bullish', structure_level: 'Swing', event_count: 4, member_count: 3, member_ratio: 0.075 } },
        extreme: { EQH: { event_count: 3, member_count: 2, member_ratio: 0.05 } },
      },
    },
  }) as StructureEvolutionPositionVM
  assert.equal(g5.denominator, 40)
  assert.equal(g6.events?.denominator, 40)
  // frontend never derives a denominator from cells
  assert.equal(g5.leveled[0].memberRatio, 0.37)
  assert.equal(g6.events?.leveled[0].memberRatio, 0.075)
})

// ----- §38 formal wiring (renderer receives typed VM) -----
test('formal wiring produces typed VMs for both G5 and G6', () => {
  const g5Facts = {
    bos_choch_events: { status: 'ready', denominator: 12, cells: { leveled: { c1: { event_type: 'BOS', direction: 'up', structure_level: 'Swing', event_count: 1, member_count: 1, member_ratio: 0.08 } }, extreme: {} } },
  }
  const g6Facts = {
    ob_and_eq_events: {
      status: 'ready',
      denominator: 25,
      cells: {
        leveled: { c1: { event_type: 'OB_ENTERED', direction: 'down', structure_level: 'Internal', event_count: 2, member_count: 2, member_ratio: 0.08 } },
        extreme: { EQL: { event_count: 1, member_count: 1, member_ratio: 0.04 } },
      },
    },
    structure_alignment: { aligned_count: 10, aligned_ratio: 0.4, divergent_count: 15, divergent_ratio: 0.6, denominator: 25 },
    distance_to_trailing_top_pct: { median: 3.3, p25: 1.0, p75: 5.0, valid_count: 24, denominator: 25 },
    distance_to_trailing_bottom_pct: { median: -4.4, p25: -6.0, p75: -2.0, valid_count: 24, denominator: 25 },
  }
  const g5vm = parseStructureBreakTurn(g5Facts)
  const g6vm = parseStructureEvolutionPosition(g6Facts)
  assert.ok(g5vm)
  assert.ok(g6vm)
  assert.equal(g5vm.groupKey, 'structure_break_turn')
  assert.equal(g6vm.groupKey, 'structure_evolution_position')
})
