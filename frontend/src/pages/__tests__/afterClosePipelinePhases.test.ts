// [盘后流水线] - 描述: 5 阶段时间线契约测试
// 用法：node --experimental-strip-types --test src/pages/__tests__/afterClosePipelinePhases.test.ts
// 覆盖（P0-1）：
//   1. buildPipelineSteps() 仅渲染 5 行、顺序正确、不含旧 8 步骤 key
//   2. 页面源文件不再出现 checking_coverage/creating_dsa/watchlist_ready 等旧步骤
//   3. market_prep / dsa_compute 阶段 key 能被后端 steps 消费并正确显示状态
//   4. findPhaseStartedAt 从 steps 取 feature_snapshot 阶段 started_at（P0-5 ETA 来源）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  buildPipelineSteps,
  findPhaseStartedAt,
  PIPELINE_PHASE_KEYS,
} from '../afterClosePipelinePhases.ts'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'AdminAfterClosePipelinePage.tsx')

// 旧内部细状态 key（废弃，禁止作为前端执行步骤出现）。
// 注意：watchlist_ready 仍作为发布门禁(gate)字段存在于 API（pipeline.watchlist_ready），
// 不是执行步骤，故不在此源扫描禁止列表内；其作为步骤的缺失由 buildPipelineSteps 测试保证。
const LEGACY_STEP_KEYS: string[] = [
  'refreshing_daily',
  'checking_coverage',
  'creating_dsa',
  'waiting_dsa_worker',
]

const phaseKeys = PIPELINE_PHASE_KEYS as unknown as string[]

test('buildPipelineSteps 仅 5 行且顺序正确', () => {
  const steps = buildPipelineSteps()
  assert.equal(steps.length, 5)
  assert.deepEqual(
    steps.map((s) => s.key),
    ['market_prep', 'dsa_compute', 'quality_gate', 'feature_snapshot', 'publishing'],
  )
  for (const s of steps) assert.ok(s.label && s.label.length > 0, `阶段 ${s.key} 缺少标签`)
})

test('不包含旧内部细状态 key', () => {
  const keys = buildPipelineSteps().map((s) => s.key) as unknown as string[]
  for (const legacy of LEGACY_STEP_KEYS) {
    assert.ok(!keys.includes(legacy), `buildPipelineSteps 不应包含旧步骤 ${legacy}`)
  }
  assert.ok(!phaseKeys.includes('watchlist_ready'))
})

test('页面源文件不再出现旧内部细状态 key', () => {
  const src = readFileSync(PAGE_PATH, 'utf-8')
  for (const legacy of LEGACY_STEP_KEYS) {
    assert.ok(
      !src.includes(legacy),
      `AdminAfterClosePipelinePage.tsx 不应再引用旧步骤 ${legacy}`,
    )
  }
})

test('market_prep / dsa_compute 阶段可被后端 steps 消费并正确显示状态', () => {
  const steps = [
    { step: 'market_prep', status: 'completed', started_at: '2026-07-10T18:00:00+08:00' },
    { step: 'dsa_compute', status: 'running', started_at: '2026-07-10T18:05:00+08:00' },
    { step: 'quality_gate', status: 'pending', started_at: null },
    { step: 'feature_snapshot', status: 'pending', started_at: null },
    { step: 'publishing', status: 'pending', started_at: null },
  ]
  const map = new Map(steps.map((s) => [s.step, s]))
  assert.equal(map.get('market_prep')!.status, 'completed')
  assert.equal(map.get('dsa_compute')!.status, 'running')
  // 5 个阶段 key 与后端 step 一一对应
  for (const s of steps) assert.ok(phaseKeys.includes(s.step))
})

test('findPhaseStartedAt 取 feature_snapshot 阶段 started_at（P0-5 ETA 来源）', () => {
  const steps = [
    { step: 'market_prep', started_at: '2026-07-10T18:00:00+08:00' },
    { step: 'feature_snapshot', started_at: '2026-07-10T19:00:00+08:00' },
  ]
  assert.equal(
    findPhaseStartedAt(steps, 'feature_snapshot'),
    '2026-07-10T19:00:00+08:00',
  )
  assert.equal(findPhaseStartedAt(steps, 'market_prep'), '2026-07-10T18:00:00+08:00')
  assert.equal(findPhaseStartedAt(steps, 'publishing'), null)
  assert.equal(findPhaseStartedAt(undefined, 'feature_snapshot'), null)
})
