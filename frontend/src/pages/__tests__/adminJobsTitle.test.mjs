// [管理后台优化 PRD §6 / D1] 任务中心页面标题测试
// 用法：node --test src/pages/__tests__/adminJobsTitle.test.mjs
//
// 验证：/admin/tasks 包装的任务页标题为"任务中心"（不再显示"任务与事件"）。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const JOBS_PAGE_PATH = join(__dirname, '..', 'AdminJobsPage.tsx')
const TASKS_PAGE_PATH = join(__dirname, '..', 'AdminTasksPage.tsx')

function readSource(p) {
  return readFileSync(p, 'utf-8')
}

test('任务中心页面标题为"任务中心"', () => {
  const jobsSrc = readSource(JOBS_PAGE_PATH)
  assert.ok(jobsSrc.includes('任务中心'), 'AdminJobsPage 页面标题必须为"任务中心"')
  assert.ok(!jobsSrc.includes('<h1 className="page-title">任务与事件</h1>'), '不得再显示"任务与事件"标题')
})

test('AdminTasksPage 包装 AdminJobsPage', () => {
  const tasksSrc = readSource(TASKS_PAGE_PATH)
  assert.ok(tasksSrc.includes('AdminJobsPage'), 'AdminTasksPage 必须包装 AdminJobsPage')
  assert.ok(tasksSrc.includes('export default function AdminTasksPage'), '必须导出 AdminTasksPage')
})
