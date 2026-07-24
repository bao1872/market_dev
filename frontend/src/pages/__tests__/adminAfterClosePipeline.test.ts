// [AdminAfterClosePipeline] - 描述: 盘后流水线页面定向测试（纯函数 + 源码级）
// 用法：node --experimental-strip-types --test src/pages/__tests__/adminAfterClosePipeline.test.ts
//
// 覆盖（Phase 8A 纠偏要求）：
// 1. API返回新steps时按返回顺序展示（getStepKeys 以 API 顺序为主）
// 2. computing_features显示正确（stepLabel + DEFAULT_STEP_ORDER）
// 3. legacy四状态映射后不重复生成四个步骤（LEGACY_STEP_KEYS 不在默认顺序/标签中）
// 4. 未知状态安全降级（stepLabel/overallStatusLabel/stepStatusLabel 回退到 key 或 '-'）
// 5. watchlist_ready独立于overall_status（源码级验证独立字段）
// 6. running/terminal轮询间隔和hidden暂停保持正确（getPipelinePollInterval + refetchIntervalInBackground）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import type { PipelineStep } from '@/api/endpoints'
import {
  STEP_LABELS,
  DEFAULT_STEP_ORDER,
  LEGACY_STEP_KEYS,
  stepLabel,
  overallStatusLabel,
  overallStatusPillClass,
  stepStatusLabel,
  stepStatusClass,
  formatDurationSeconds,
  getStepKeys,
  PIPELINE_POLL_RUNNING,
  PIPELINE_POLL_IDLE,
  getPipelinePollInterval,
} from '../adminAfterClosePipelineHelpers.ts'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'AdminAfterClosePipelinePage.tsx')
const USE_API_PATH = join(__dirname, '..', '..', 'hooks', 'useApi.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// 辅助：构造 PipelineStep
function makeStep(step: string, status: PipelineStep['status'] = 'completed'): PipelineStep {
  return {
    step,
    status,
    started_at: null,
    finished_at: null,
    duration_seconds: null,
    counts: {},
    error_message: null,
  }
}

// ============================================================
// 1. API返回新steps时按返回顺序展示
// ============================================================

test('1a. getStepKeys: API 返回非空 steps 时按 API 顺序返回', () => {
  const apiSteps: PipelineStep[] = [
    makeStep('refreshing_daily', 'completed'),
    makeStep('syncing_boards', 'completed'),
    makeStep('checking_coverage', 'completed'),
    makeStep('computing_features', 'running'),
    makeStep('publishing', 'pending'),
  ]
  const keys = getStepKeys(apiSteps)
  assert.deepEqual(keys, [
    'refreshing_daily',
    'syncing_boards',
    'checking_coverage',
    'computing_features',
    'publishing',
  ])
})

test('1b. getStepKeys: API 返回乱序 steps 时保持 API 顺序（不重排）', () => {
  const apiSteps: PipelineStep[] = [
    makeStep('publishing', 'pending'),
    makeStep('computing_features', 'running'),
    makeStep('refreshing_daily', 'completed'),
  ]
  const keys = getStepKeys(apiSteps)
  // 顺序与 API 返回一致，前端不自行重排
  assert.deepEqual(keys, ['publishing', 'computing_features', 'refreshing_daily'])
})

test('1c. getStepKeys: API 返回空数组时用 DEFAULT_STEP_ORDER 兜底', () => {
  const keys = getStepKeys([])
  assert.deepEqual(keys, DEFAULT_STEP_ORDER)
  assert.ok(keys.length === 6, `默认步骤应为 6 步，实际: ${keys.length}`)
})

// ============================================================
// 2. computing_features显示正确
// ============================================================

test('2a. stepLabel("computing_features") 返回中文标签 "统一特征计算"', () => {
  assert.strictEqual(stepLabel('computing_features'), '统一特征计算')
})

test('2b. DEFAULT_STEP_ORDER 包含 computing_features', () => {
  assert.ok(
    DEFAULT_STEP_ORDER.includes('computing_features'),
    'DEFAULT_STEP_ORDER 必须包含 computing_features',
  )
})

test('2c. STEP_LABELS 包含 computing_features 映射', () => {
  assert.strictEqual(STEP_LABELS['computing_features'], '统一特征计算')
})

test('2d. 新状态机 6 步全部有中文标签', () => {
  for (const key of DEFAULT_STEP_ORDER) {
    assert.ok(
      STEP_LABELS[key],
      `步骤 "${key}" 应有中文标签，实际: ${STEP_LABELS[key] ?? 'undefined'}`,
    )
  }
})

// ============================================================
// 3. legacy四状态映射后不重复生成四个步骤
// ============================================================

test('3a. LEGACY_STEP_KEYS 不出现在 DEFAULT_STEP_ORDER 中', () => {
  for (const legacy of LEGACY_STEP_KEYS) {
    assert.ok(
      !DEFAULT_STEP_ORDER.includes(legacy),
      `legacy 状态 "${legacy}" 不应出现在 DEFAULT_STEP_ORDER 中（已收敛为 computing_features）`,
    )
  }
})

test('3b. LEGACY_STEP_KEYS 不出现在 STEP_LABELS 中（后端映射后前端只识别 computing_features）', () => {
  for (const legacy of LEGACY_STEP_KEYS) {
    assert.ok(
      !(legacy in STEP_LABELS),
      `legacy 状态 "${legacy}" 不应出现在 STEP_LABELS 中（避免前端渲染为独立步骤）`,
    )
  }
})

test('3c. DEFAULT_STEP_ORDER 中 computing_features 只出现一次（不重复生成四步）', () => {
  const count = DEFAULT_STEP_ORDER.filter((k) => k === 'computing_features').length
  assert.strictEqual(count, 1, 'computing_features 在默认顺序中应只出现一次')
})

test('3d. LEGACY_STEP_KEYS 包含旧四状态', () => {
  assert.deepEqual(LEGACY_STEP_KEYS, [
    'creating_dsa',
    'waiting_dsa_worker',
    'quality_gate',
    'feature_snapshot',
  ])
})

// ============================================================
// 4. 未知状态安全降级
// ============================================================

test('4a. stepLabel: 未知 step key 降级为 key 本身', () => {
  assert.strictEqual(stepLabel('unknown_future_step'), 'unknown_future_step')
  assert.strictEqual(stepLabel(''), '')
})

test('4b. overallStatusLabel: undefined/未知状态降级为 "-"', () => {
  assert.strictEqual(overallStatusLabel(undefined), '-')
  assert.strictEqual(overallStatusLabel('unknown_status'), '-')
})

test('4c. overallStatusPillClass: undefined/未知状态降级为 "off"', () => {
  assert.strictEqual(overallStatusPillClass(undefined), 'off')
  assert.strictEqual(overallStatusPillClass('unknown'), 'off')
})

test('4d. stepStatusLabel: 未知状态降级为 "-"', () => {
  assert.strictEqual(stepStatusLabel('unknown'), '-')
  assert.strictEqual(stepStatusLabel(''), '-')
})

test('4e. stepStatusClass: 未知状态降级为空字符串', () => {
  assert.strictEqual(stepStatusClass('unknown'), '')
  assert.strictEqual(stepStatusClass(''), '')
})

test('4f. getStepKeys: API 返回含未知 step 时不报错，原样保留', () => {
  const apiSteps: PipelineStep[] = [
    makeStep('refreshing_daily', 'completed'),
    makeStep('future_unknown_step', 'running'),
  ]
  const keys = getStepKeys(apiSteps)
  assert.deepEqual(keys, ['refreshing_daily', 'future_unknown_step'])
})

// ============================================================
// 5. watchlist_ready独立于overall_status
// ============================================================

test('5a. 源码级: AfterClosePipelineResponse 类型定义中 watchlist_ready 是独立字段', () => {
  // 验证 endpoints.ts 中 AfterClosePipelineResponse 包含 watchlist_ready: boolean
  const endpointsSrc = readSource(join(__dirname, '..', '..', 'api', 'endpoints.ts'))
  assert.ok(
    endpointsSrc.includes('watchlist_ready: boolean'),
    'AfterClosePipelineResponse 必须包含 watchlist_ready: boolean 独立字段',
  )
  // overall_status 和 watchlist_ready 是不同字段
  assert.ok(
    endpointsSrc.includes('overall_status:'),
    'AfterClosePipelineResponse 必须包含 overall_status 字段',
  )
})

test('5b. 源码级: 页面渲染 watchlist_ready 时不依赖 overall_status', () => {
  const pageSrc = readSource(PAGE_PATH)
  // 页面直接读取 pipeline?.watchlist_ready，不是从 overall_status 派生
  assert.ok(
    pageSrc.includes('pipeline?.watchlist_ready'),
    '页面应直接读取 pipeline.watchlist_ready 字段',
  )
  // watchlist_ready 可以在 overall_status='failed' 时仍为 false（独立判定）
  // 验证页面不使用 overall_status === 'succeeded' 来推断 watchlist_ready
  assert.ok(
    !pageSrc.includes("overall_status === 'succeeded' && watchlist_ready"),
    'watchlist_ready 不应从 overall_status===succeeded 派生',
  )
})

test('5c. 源码级: overall_status 和 watchlist_ready 在不同 toggle-row 中独立展示', () => {
  const pageSrc = readSource(PAGE_PATH)
  // overall_status 在"整体状态"行
  assert.ok(
    pageSrc.includes('整体状态') && pageSrc.includes('overallStatusLabel(overallStatus)'),
    'overall_status 应在"整体状态"行独立展示',
  )
  // watchlist_ready 在"自选可用"行
  assert.ok(
    pageSrc.includes('自选可用') && pageSrc.includes('pipeline?.watchlist_ready'),
    'watchlist_ready 应在"自选可用"行独立展示',
  )
})

// ============================================================
// 6. running/terminal轮询间隔和hidden暂停保持正确
// ============================================================

test('6a. PIPELINE_POLL_RUNNING = 10000ms (10s)', () => {
  assert.strictEqual(PIPELINE_POLL_RUNNING, 10_000)
})

test('6b. PIPELINE_POLL_IDLE = 60000ms (60s)', () => {
  assert.strictEqual(PIPELINE_POLL_IDLE, 60_000)
})

test('6c. getPipelinePollInterval: running 状态返回 10s', () => {
  assert.strictEqual(getPipelinePollInterval('running'), 10_000)
})

test('6d. getPipelinePollInterval: terminal 状态返回 60s', () => {
  assert.strictEqual(getPipelinePollInterval('succeeded'), 60_000)
  assert.strictEqual(getPipelinePollInterval('failed'), 60_000)
  assert.strictEqual(getPipelinePollInterval('blocked'), 60_000)
  assert.strictEqual(getPipelinePollInterval('not_started'), 60_000)
  assert.strictEqual(getPipelinePollInterval('skipped'), 60_000)
})

test('6e. getPipelinePollInterval: undefined 返回 60s（安全降级）', () => {
  assert.strictEqual(getPipelinePollInterval(undefined), 60_000)
})

test('6f. 源码级: useAfterClosePipelineLatest 设置 refetchIntervalInBackground=false', () => {
  const src = readSource(USE_API_PATH)
  assert.ok(
    src.includes('refetchIntervalInBackground: false'),
    'useAfterClosePipelineLatest/ByDate 必须设置 refetchIntervalInBackground: false（页面隐藏时暂停轮询）',
  )
})

test('6g. 源码级: useAfterClosePipelineByDate 使用 getPipelinePollInterval', () => {
  const src = readSource(USE_API_PATH)
  assert.ok(
    src.includes('getPipelinePollInterval(query.state.data?.overall_status)'),
    'hook 应使用 getPipelinePollInterval 根据 overall_status 返回轮询间隔',
  )
})

// ============================================================
// 补充: formatDurationSeconds 边界测试
// ============================================================

test('formatDurationSeconds: null/undefined 返回 "-"', () => {
  assert.strictEqual(formatDurationSeconds(null), '-')
  assert.strictEqual(formatDurationSeconds(undefined), '-')
})

test('formatDurationSeconds: <60s 返回 "X.Xs"', () => {
  assert.strictEqual(formatDurationSeconds(0), '0.0s')
  assert.strictEqual(formatDurationSeconds(5.5), '5.5s')
  assert.strictEqual(formatDurationSeconds(59.9), '59.9s')
})

test('formatDurationSeconds: >=60s 返回 "Xm Ys"', () => {
  assert.strictEqual(formatDurationSeconds(60), '1m 0s')
  assert.strictEqual(formatDurationSeconds(125), '2m 5s')
  assert.strictEqual(formatDurationSeconds(3600), '60m 0s')
})
