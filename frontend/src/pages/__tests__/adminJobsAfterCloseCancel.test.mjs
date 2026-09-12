// [管理后台] AdminJobsPage 精确 after_close run 终止 + query invalidation 修复
// 用法：node --test src/pages/__tests__/adminJobsAfterCloseCancel.test.mjs
//
// 背景（真实生产 bug）：
// 1. after-close mutation 成功后失效的 key 是 ['scheduler-job-runs']，
//    但任务列表实际使用 ['admin','scheduler-job-runs', params]，少了 'admin' 前缀
//    → cancel API 成功但任务页继续显示旧 running/queued，看起来像"没取消"。
// 2. AdminJobsPage 对 after_close_orchestrator 只有"盘后详情（四类操作）"跳转，
//    没有直接终止动作，且若按 business_date 反查 latest run，可能操作不到
//    任务列表中用户看到的那个 exact run（同一天可能有多个 attempt）。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const JOBS_PAGE = join(__dirname, '..', 'AdminJobsPage.tsx')
const USE_API = join(__dirname, '..', '..', 'hooks', 'useApi.ts')

const jobsSrc = readFileSync(JOBS_PAGE, 'utf-8')
const apiSrc = readFileSync(USE_API, 'utf-8')

// ---- F. query invalidation 修复（核心 bug）----
test('invalidateAfterCloseAdminQueries uses admin-prefixed scheduler-job-runs key', () => {
  assert.ok(
    apiSrc.includes("queryKey: ['admin', 'scheduler-job-runs']"),
    "必须以 ['admin','scheduler-job-runs'] 失效（React Query 前缀失效覆盖所有 params）",
  )
})

test('invalidateAfterCloseAdminQueries no longer uses bare scheduler-job-runs key', () => {
  const bare = /invalidateQueries\(\{\s*queryKey:\s*\['scheduler-job-runs'\]\s*\}\)/.test(apiSrc)
  assert.ok(!bare, "不得再使用缺少 'admin' 前缀的 ['scheduler-job-runs']")
})

test('invalidation still covers after-close-runs / pipeline / system-overview', () => {
  for (const key of ["['after-close-runs']", "['after-close-pipeline']", "['admin', 'system-overview']"]) {
    assert.ok(apiSrc.includes(key), `必须保留失效 ${key}`)
  }
})

// ---- A/B/C/D. 终止动作门禁 ----
test('A: cancel action gated on after_close_orchestrator job_name', () => {
  assert.ok(
    jobsSrc.includes("selectedRun?.job_name === 'after_close_orchestrator'"),
    '终止动作必须仅对 after_close_orchestrator 生效',
  )
})

test('B: cancel action gated on queued OR running only', () => {
  assert.ok(
    jobsSrc.includes("(selectedRun.status === 'queued' || selectedRun.status === 'running')"),
    '仅 queued / running 允许终止；succeeded / failed / cancelled 等终态不得允许',
  )
})

test('D: cancel button rendered only when gate passes (not for other jobs)', () => {
  assert.ok(
    jobsSrc.includes('{canCancelSelectedRun && ('),
    '终止按钮必须由 canCancelSelectedRun 门控（普通 bars_scheduler 等不得出现）',
  )
  assert.ok(jobsSrc.includes('终止任务'), '必须提供"终止任务"动作文案')
})

// ---- E. 必须操作 exact run ----
test('E: cancel uses exact selectedRun.id, never business_date lookup', () => {
  assert.ok(
    jobsSrc.includes('const runId = selectedRun.id'),
    '必须取 selectedRun.id 作为精确 run 标识',
  )
  assert.ok(
    jobsSrc.includes('reason: \'管理员从任务管理页终止\''),
    '必须携带明确 reason',
  )
  const idLookup = /job_run_id\s*:\s*selectedRun\.business_date/.test(jobsSrc)
  assert.ok(!idLookup, '禁止按 business_date 反查 run')
})

test('E: cancel requires second confirmation (no single-click cancel)', () => {
  assert.ok(jobsSrc.includes('window.confirm('), '必须二次确认，禁止单击直接取消')
  assert.ok(jobsSrc.includes('if (!confirmed) return'), '未确认必须直接返回')
})

// ---- G. 失败不得伪装 cancelled ----
test('G: API failure shows structured error and does NOT fake cancelled', () => {
  assert.ok(jobsSrc.includes("toast.show('终止失败', formatAdminApiError(err))"), '失败必须显示结构化错误')
  const optimistic = /setSelectedRunId\(\{[^}]*status:\s*'cancelled'/.test(jobsSrc)
  assert.ok(!optimistic, '不得在本地乐观地把状态改写成 cancelled')
})

test('G: success does not fake status either (relies on invalidation+refetch)', () => {
  assert.ok(jobsSrc.includes("toast.show('终止请求已提交', result.message)"), '成功应展示 API 返回消息')
})

// ---- H. 状态文案 ----
test('H: cancelled renders 已取消 (not raw english)', () => {
  assert.ok(jobsSrc.includes("if (s === 'cancelled') return '已取消'"), "cancelled 必须显示'已取消'")
})

test('H: resume_queued renders 等待恢复', () => {
  assert.ok(jobsSrc.includes("if (s === 'resume_queued') return '等待恢复'"), "resume_queued 必须显示'等待恢复'")
})

test('H: pill class default is neutral off (cancelled not bad/warn)', () => {
  assert.ok(jobsSrc.includes("return 'off'"), '未识别状态应回落为 off（neutral）')
})

// ---- 既有入口保留 ----
test('existing 盘后详情（四类操作）entry is preserved', () => {
  assert.ok(jobsSrc.includes('盘后详情（四类操作）'), '不得删除原有盘后详情跳转')
})
