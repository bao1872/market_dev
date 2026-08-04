import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import type { DimensionResult, FirstPyramidSnapshot } from '@/api/endpoints'
import { buildFirstPyramidVM } from '../firstPyramidViewModel.ts'

function dimension(overrides: Partial<DimensionResult> = {}): DimensionResult {
  return {
    name: 'structure',
    available: true,
    continuousFactors: {},
    events: [],
    statusText: '就绪',
    evidence: {},
    ...overrides,
  }
}

/** 固定 synthetic payload：模拟 canonical/stock_core/Review API 透传到前端的末端合同。 */
function canonicalPayload(): FirstPyramidSnapshot {
  return {
    symbol: '000001.SZ',
    tradeDate: '2026-08-03',
    orderedDimensions: ['trend', 'structure', 'momentum', 'chip_consensus'],
    trend: dimension({ name: 'trend' }),
    structure: dimension({
      continuousFactors: { swing_direction: 1, internal_direction: -1 },
      // [QM-63 canonical 2026-08-04] 正式字段：direction=bullish/bearish，
      // structureLevel/bias 为顶层字段（不再藏 extra）。
      events: [
        { type: 'BOS', direction: 'bullish', structureLevel: 'swing', bias: 1, occurredAt: '2026-08-03', barIndex: 1, price: 11, freshnessBars: 0 },
        { type: 'CHoCH', direction: 'bearish', structureLevel: 'internal', bias: -1, occurredAt: '2026-08-02', barIndex: 0, price: 10, freshnessBars: 1 },
        { type: 'EQH', direction: null, structureLevel: null, occurredAt: '2026-08-01', barIndex: 0, price: 12, freshnessBars: 2 },
      ],
    }),
    momentum: dimension({ name: 'momentum' }),
    chipConsensus: null,
    statusText: '就绪',
    volumeContext: null,
    inputHash: 'input',
    parameterHash: 'params',
    algorithmVersion: 'synthetic-v1',
  }
}

test('canonical/stock_core/Review payload 到 FirstPyramidVM 保持统一 SMC 语义', () => {
  const vm = buildFirstPyramidVM(canonicalPayload(), 'detail')
  assert.deepEqual(vm.structure.events.map(event => event.typeLabel), [
    '双顶压力',
    '短线·转弱拐点↓',
    '主要·多头突破↑',
  ])
  assert.equal(vm.structure.events[0].levelLabel, null, 'EQH 不得虚构 structureLevel')
  assert.equal(vm.structure.events[1].levelLabel, '短线级别')
  assert.equal(vm.structure.events[2].levelLabel, '主要级别')
})
