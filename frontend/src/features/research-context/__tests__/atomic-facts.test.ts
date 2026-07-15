// [AtomicFactsContract] - 描述: Atomic Fact Contract V1 前端契约测试
// 用法：node --experimental-strip-types --test src/features/research-context/__tests__/atomic-facts.test.ts
// 覆盖：
// 1. 后端 Canonical Registry 14/10/1、顺序、ID 唯一
// 2. V1 永久缺席（rejected 且不在 core/aux）
// 3. T3/T6 默认隐藏（auxiliary default_ui_enabled=false）
// 4. 前端 endpoints.ts 定义 AtomicFactsContextResponse 且含合同字段
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
// __dirname = frontend/src/features/research-context/__tests__
// FRONTEND_ROOT = frontend/src（上 3 级）
const FRONTEND_ROOT = join(__dirname, '..', '..', '..')
// BACKEND_ROOT = /root/web_dev/backend（再上 2 级）
const BACKEND_ROOT = join(FRONTEND_ROOT, '..', '..', 'backend')
const CONTRACT_PATH = join(BACKEND_ROOT, 'app', 'contracts', 'atomic_fact_contract_v1.json')
const ENDPOINTS_PATH = join(FRONTEND_ROOT, 'api', 'endpoints.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

const contract = JSON.parse(readSource(CONTRACT_PATH))

test('Canonical Registry: 14 core / 10 auxiliary / 1 rejected', () => {
  assert.equal(contract.core_facts.length, 14, 'core 必须 14 项')
  assert.equal(contract.auxiliary_facts.length, 10, 'auxiliary 必须 10 项')
  assert.equal(contract.rejected_facts.length, 1, 'rejected 必须 1 项')
})

test('fact ID 唯一且 core 顺序固定', () => {
  const coreIds = contract.core_facts.map((f: { id: string }) => f.id)
  const auxIds = contract.auxiliary_facts.map((f: { id: string }) => f.id)
  const rejIds = contract.rejected_facts.map((f: { id: string }) => f.id)
  const all = [...coreIds, ...auxIds, ...rejIds]
  assert.equal(new Set(all).size, 25, 'fact ID 必须唯一（共 25）')

  const expectedOrder = [
    'T1_trend_direction', 'T2_aligned_slope', 'T4_trend_age', 'T5_slope_ratio',
    'M1_momentum_alignment', 'M2_aligned_momentum', 'M3_aligned_momentum_delta', 'M5_squeeze_state',
    'S1_confirmed_boundary_relation', 'S2_active_dir_relation', 'S3_active_position',
    'S7_dist_favorable_boundary', 'S8_dist_adverse_boundary',
    'V3_avg_volume_ratio',
  ]
  assert.deepEqual(coreIds, expectedOrder, 'core ID 顺序必须固定（趋势4/动量4/结构5/成交1）')
})

test('V1 永久缺席（rejected 且不在 core/aux）', () => {
  const rej = contract.rejected_facts[0]
  assert.equal(rej.id, 'V1_cumulative_volume_ratio', 'rejected 必须是 V1 累计成交量比')
  const coreIds = contract.core_facts.map((f: { id: string }) => f.id)
  const auxIds = contract.auxiliary_facts.map((f: { id: string }) => f.id)
  assert.ok(
    !coreIds.includes(rej.id) && !auxIds.includes(rej.id),
    'V1 不得出现在 core/aux（永不进入 UI/摘要）',
  )
})

test('T3/T6 默认隐藏（default_ui_enabled=false）', () => {
  const t3 = contract.auxiliary_facts.find((f: { id: string }) => f.id === 'T3_trend_efficiency')
  const t6 = contract.auxiliary_facts.find((f: { id: string }) => f.id === 'T6_efficiency_delta')
  assert.ok(t3, 'contract 必须含 T3')
  assert.ok(t6, 'contract 必须含 T6')
  assert.equal(t3.default_ui_enabled, false, 'T3 默认隐藏')
  assert.equal(t6.default_ui_enabled, false, 'T6 默认隐藏')
})

test('前端 endpoints.ts 定义 AtomicFactsContextResponse 契约字段', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export interface AtomicFactsContextResponse/.test(src),
    'endpoints.ts 必须定义 AtomicFactsContextResponse',
  )
  for (const field of [
    'contractVersion', 'asOf', 'core', 'auxiliary', 'availability', 'recentChanges', 'dataQuality',
  ]) {
    assert.ok(new RegExp(`\\b${field}\\b`).test(src), `endpoints.ts 必须包含字段 ${field}`)
  }
  assert.ok(/coreDenominator/.test(src), 'availability 必须含 coreDenominator（固定 14）')
})
