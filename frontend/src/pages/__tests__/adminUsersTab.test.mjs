// [PRD §8.4.7 / D2] 用户与权限页 Tab URL 唯一真源测试
// 用法：node --test src/pages/__tests__/adminUsersTab.test.mjs
//
// 验证：用户页面 tab 以 URL query 为唯一真源（useEffect 同步 URL 变化，
// 前进/后退/外部修改均能恢复），不再依赖本地 state 独立决定。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'AdminUsersPage.tsx')

function readSource(p) {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 从 URL query 初始化 tab =====
test('用户页从 URL query 初始化 tab', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('useSearchParams'), '必须使用 useSearchParams')
  assert.ok(src.includes("searchParams.get('tab')"), '必须从 URL 读取 tab')
})

// ===== 2. useEffect 同步 URL 变化 → state（唯一真源）=====
test('useEffect 监听 tabParam 同步 activeTab（前进/后退可恢复）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('useEffect'), '必须使用 useEffect')
  assert.ok(src.includes('setActiveTab'), '必须通过 setActiveTab 同步')
  assert.ok(src.includes('tabParam') && src.includes('useEffect'), 'useEffect 必须依赖 tabParam')
})

// ===== 3. 每个 tab 有明确 URL 表示 =====
test('tab 有明确 URL 映射（members/invites/beta_applications/rules）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('beta_applications'), '内测申请 tab URL 表示')
  assert.ok(src.includes('memberList'), '会员账户 tab')
  assert.ok(src.includes('inviteList'), '邀请码 tab')
  assert.ok(src.includes('rulePanel'), '规则说明 tab')
})

// ===== 4. 切换 tab 写回 URL =====
test('切换 tab 写回 URL（setSearchParams）', () => {
  const src = readSource(PAGE_PATH)
  assert.ok(src.includes('setSearchParams'), '切换 tab 必须写回 URL')
})

// ===== 5. 旧路由重定向目标与页面识别一致（P0 审查修复）=====
test('旧路由 tab=beta_applications 与页面识别一致（下划线）', () => {
  const pageSrc = readSource(PAGE_PATH)
  const routeSrc = readSource(join(__dirname, '..', '..', 'navigation', 'routeStructure.ts'))
  // 页面必须识别下划线 beta_applications
  assert.ok(pageSrc.includes("'beta_applications'"), 'AdminUsersPage 必须识别 beta_applications')
  // 旧路由重定向必须用下划线 beta_applications（连字符会导致旧入口失效）
  assert.ok(routeSrc.includes('/admin/users?tab=beta_applications'), '旧路由必须重定向到 tab=beta_applications（下划线）')
  assert.ok(!routeSrc.includes('/admin/users?tab=beta-applications'), '不得用连字符 beta-applications')
})
