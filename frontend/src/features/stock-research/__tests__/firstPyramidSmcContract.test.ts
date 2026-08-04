import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import type { ChipStatus, DimensionResult, FirstPyramidSnapshot } from '@/api/endpoints'
import { buildFirstPyramidVM } from '../firstPyramidViewModel.ts'

/**
 * [QM-63 2026-08-04] chip 七态 + run 级溯源的展示合同。
 * 通过 view-model 断言：VM 必须透传/推导出七态语义与溯源字段，
 * 不得丢弃、不得把 unavailable/interrupted/partial 静默归并为普通不可用。
 */
test('chip 七态：partial/interrupted/stale 在 VM 中保留原始 state', () => {
  const states: ChipStatus['state'][] = [
    'pending', 'ready', 'unavailable', 'failed',
    'interrupted', 'stale', 'partial',
  ]
  for (const state of states) {
    const payload = canonicalPayload()
    payload.chipStatus = {
      state,
      reasonCode: state === 'unavailable' ? 'DAILY_BARS_INSUFFICIENT' : `CHIP_${state.toUpperCase()}`,
      reasonText: `reason-${state}`,
      computedAt: null,
    }
    const vm = buildFirstPyramidVM(payload, 'detail')
    assert.equal(
      vm.chipStatus?.state, state,
      `chip 七态 ${state} 必须原样透传到 VM（不得被归一）`,
    )
  }
})

test('run 级溯源：批量 run 推导 fromBatchRun，单股即时计算显式标注', () => {
  const batch = canonicalPayload()
  batch.calculatedAt = '2026-08-03T15:30:00+08:00'
  batch.sourceRunId = 'run-abc-123'
  const vm = buildFirstPyramidVM(batch, 'detail')
  assert.equal(vm.provenance.fromBatchRun, true)
  assert.equal(vm.provenance.sourceRunId, 'run-abc-123')
  assert.equal(vm.provenance.calculatedAt, '2026-08-03T15:30:00+08:00')
  assert.equal(vm.provenance.algorithmVersion, 'synthetic-v1')

  const adhoc = canonicalPayload()
  delete adhoc.sourceRunId
  delete adhoc.calculatedAt
  const vm2 = buildFirstPyramidVM(adhoc, 'detail')
  assert.equal(vm2.provenance.fromBatchRun, false)
  assert.equal(vm2.provenance.sourceRunId, null)
  assert.equal(vm2.provenance.calculatedAt, null)
})

test('chip 溯源透传：sourceRunId/jobId/freshness/coverage 不丢失', () => {
  const payload = canonicalPayload()
  payload.chipStatus = {
    state: 'partial',
    reasonCode: 'CHIP_PARTIAL_COVERAGE',
    reasonText: '部分维度可用',
    computedAt: '2026-08-03T15:35:00+08:00',
    sourceRunId: 'chip-run-x',
    jobId: 'job-9',
    freshness: 1,
    coverage: 0.6,
  }
  const vm = buildFirstPyramidVM(payload, 'detail')
  assert.equal(vm.chipStatus?.state, 'partial')
  assert.equal(vm.chipStatus?.sourceRunId, 'chip-run-x')
  assert.equal(vm.chipStatus?.jobId, 'job-9')
  assert.equal(vm.chipStatus?.freshness, 1)
  assert.equal(vm.chipStatus?.coverage, 0.6)
})

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
