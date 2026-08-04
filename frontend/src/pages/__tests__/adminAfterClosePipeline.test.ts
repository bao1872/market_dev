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
  // [CHANGE-20260801-REVIEW-CLOSURE] 新 7 步（含 computing_review）
  assert.ok(keys.length === 7, `默认步骤应为 7 步（含 computing_review），实际: ${keys.length}`)
  assert.ok(
    keys.includes('computing_review'),
    'DEFAULT_STEP_ORDER 必须包含 computing_review（复盘阶段）',
  )
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

// [CHANGE-20260801-REVIEW-CLOSURE] 新增复盘阶段断言
test('2c2. computing_review: 存在于 DEFAULT_STEP_ORDER + STEP_LABELS 且中文标签正确', () => {
  assert.ok(
    DEFAULT_STEP_ORDER.includes('computing_review'),
    'DEFAULT_STEP_ORDER 必须包含 computing_review',
  )
  assert.strictEqual(
    STEP_LABELS['computing_review'],
    '复盘计算发布',
    'computing_review 中文标签应为"复盘计算发布"',
  )
  // computing_review 只出现一次
  const occurrences = DEFAULT_STEP_ORDER.filter((k) => k === 'computing_review').length
  assert.strictEqual(occurrences, 1, 'computing_review 在 DEFAULT_STEP_ORDER 中应仅出现一次')
  // 顺序：publishing 之后，watchlist_ready 之前
  const pubIdx = DEFAULT_STEP_ORDER.indexOf('publishing')
  const revIdx = DEFAULT_STEP_ORDER.indexOf('computing_review')
  const wlIdx = DEFAULT_STEP_ORDER.indexOf('watchlist_ready')
  assert.ok(revIdx > pubIdx, 'computing_review 应在 publishing 之后')
  assert.ok(revIdx < wlIdx, 'computing_review 应在 watchlist_ready 之前')
})

test('2d. 新状态机 7 步（含 computing_review）全部有中文标签', () => {
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

test('4d. stepStatusLabel: 新增真实状态有文字，未知状态明确为未知', () => {
  assert.strictEqual(stepStatusLabel('succeeded'), '已完成')
  assert.strictEqual(stepStatusLabel('skipped_unavailable'), '不可用，已跳过')
  assert.strictEqual(stepStatusLabel('cancelled'), '已终止')
  assert.strictEqual(stepStatusLabel('unknown'), '未知')
  assert.strictEqual(stepStatusLabel(''), '未知')
})

test('4e. stepStatusClass: 未知状态降级为空字符串', () => {
  assert.strictEqual(stepStatusClass('unknown'), '')
  assert.strictEqual(stepStatusClass(''), '')
})

// [AC-TERMINAL-01 2026-08-04] 终态必须如实显示，不得回落 '-'/'未知'
test('4f. overallStatusLabel: partial_success/cancelled/interrupted 有明确中文', () => {
  assert.strictEqual(overallStatusLabel('partial_success'), '部分成功')
  assert.strictEqual(overallStatusLabel('cancelled'), '已取消')
  assert.strictEqual(overallStatusLabel('interrupted'), '已中断')
})

test('4g. overallStatusPillClass: 终态样式区分失败与取消', () => {
  // 部分成功：核心已发布但有降级 → warn
  assert.strictEqual(overallStatusPillClass('partial_success'), 'warn')
  // 取消/中断不是失败 → 不标红
  assert.notStrictEqual(overallStatusPillClass('cancelled'), 'error')
  assert.notStrictEqual(overallStatusPillClass('interrupted'), 'error')
})

test('4h. stepStatusLabel: 步骤级终态 timed_out/unavailable/interrupted 有文字', () => {
  assert.strictEqual(stepStatusLabel('timed_out'), '超时')
  assert.strictEqual(stepStatusLabel('unavailable'), '不可用')
  assert.strictEqual(stepStatusLabel('interrupted'), '已中断')
})

test('4i. stepStatusClass: timed_out 视为错误，interrupted/unavailable 为跳过态', () => {
  assert.strictEqual(stepStatusClass('timed_out'), 'error')
  assert.strictEqual(stepStatusClass('unavailable'), 'skipped')
  assert.strictEqual(stepStatusClass('interrupted'), 'skipped')
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
// 补充: formatDurationSeconds 边界测试（含 TIMELINE-FIX 新语义）
// ============================================================

test('[TIMELINE-FIX] formatDurationSeconds: null/undefined + 非 running 返回 "-"', () => {
  assert.strictEqual(formatDurationSeconds(null, 'completed'), '-')
  assert.strictEqual(formatDurationSeconds(undefined, 'completed'), '-')
  assert.strictEqual(formatDurationSeconds(null), '-')
})

// [TIMELINE-FIX] running 状态：无 duration → "进行中"（不是 -），有≤0 也是进行中）
test('[TIMELINE-FIX] formatDurationSeconds: running + null/0/negative → "进行中"', () => {
  assert.strictEqual(formatDurationSeconds(null, 'running'), '进行中')
  assert.strictEqual(formatDurationSeconds(undefined, 'running'), '进行中')
  assert.strictEqual(formatDurationSeconds(0, 'running'), '进行中')
  assert.strictEqual(formatDurationSeconds(-1, 'running'), '进行中')
})

// [TIMELINE-FIX] 非正耗时（DB偏差）且非 running：不用 max(0,x) → 掩盖，返回"未知"
test('[TIMELINE-FIX] formatDurationSeconds: 0 或负秒数 且非 running → "未知"', () => {
  assert.strictEqual(formatDurationSeconds(0, 'completed'), '未知')
  assert.strictEqual(formatDurationSeconds(-5, 'completed'), '未知')
  assert.strictEqual(formatDurationSeconds(-0.1, 'failed'), '未知')
})

// [TIMELINE-FIX] warnings 含 invalid_order_or_zero_duration：即便 seconds 存在值，也优先"未知"
test('[TIMELINE-FIX] formatDurationSeconds: invalid_order warnings → "未知"（不掩盖）', () => {
  assert.strictEqual(
    formatDurationSeconds(120, 'completed', ['invalid_order_or_zero_duration']),
    '未知',
  )
  // 即使是 running 且 warnings，有警告优先展示警告
})

// 正常正数值仍可展示
test('formatDurationSeconds: 正数且无异常 → 正常格式化', () => {
  assert.strictEqual(formatDurationSeconds(5.5, 'completed'), '5.5s')
  assert.strictEqual(formatDurationSeconds(59.9, 'completed'), '59.9s')
  assert.strictEqual(formatDurationSeconds(60, 'completed'), '1m 0s')
  assert.strictEqual(formatDurationSeconds(125, 'succeeded'), '2m 5s')
  assert.strictEqual(formatDurationSeconds(3600, 'completed'), '60m 0s')
  // 无 warnings 参数也正常显示
  assert.strictEqual(formatDurationSeconds(125), '2m 5s')
})
