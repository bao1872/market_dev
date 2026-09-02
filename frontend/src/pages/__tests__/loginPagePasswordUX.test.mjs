// [LoginPage] - 描述: 登录/注册密码 UX 源码契约测试（[CHANGE-20260902] A）
// 用法：node --experimental-strip-types --test src/pages/__tests__/loginPagePasswordUX.test.mjs
//
// 覆盖：
// 1. 三个密码框各自独立显示/隐藏按钮（登录/注册密码/注册确认密码）
// 2. 切换使用独立 state（showLoginPassword / showRegPassword / showRegPassword2）
// 3. 按钮 type="button" + aria-label/title
// 4. 忘记密码为真实 <button type="button"> 且 onClick=handleForgotPassword
// 5. 注册表单单列（gridTemplateColumns: '1fr'）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const SRC_PATH = join(__dirname, '..', 'LoginPage.tsx')

function src() {
  return readFileSync(SRC_PATH, 'utf-8')
}

test('三个密码显示/隐藏使用独立 state', () => {
  const s = src()
  assert.ok(s.includes('showLoginPassword'), '登录密码需独立 state showLoginPassword')
  assert.ok(s.includes('showRegPassword'), '注册密码需独立 state showRegPassword')
  assert.ok(s.includes('showRegPassword2'), '注册确认密码需独立 state showRegPassword2')
})

test('密码切换按钮为 type="button" 且带 aria-label/title', () => {
  const s = src()
  // 至少出现 3 个 password-toggle 按钮
  const toggleCount = (s.match(/className="password-toggle"/g) || []).length
  assert.ok(toggleCount >= 3, `应至少有 3 个密码切换按钮（实际 ${toggleCount}）`)
  assert.ok(s.includes('type="button"'), '切换按钮必须为 type="button"')
  assert.ok(s.includes('aria-label'), '切换按钮必须提供 aria-label')
  assert.ok(s.includes('title='), '切换按钮必须提供 title')
  // 不应修改 value（仅切换 type）
  assert.ok(s.includes("type={showLoginPassword ? 'text' : 'password'}"), '登录密码应基于 showLoginPassword 切换 type')
})

test('忘记密码为真实按钮并调用 handleForgotPassword', () => {
  const s = src()
  assert.ok(
    /<button[^>]*className="forgot-link"[^>]*onClick=\{handleForgotPassword\}/.test(s),
    '忘记密码必须是 <button type="button" className="forgot-link" onClick={handleForgotPassword}>',
  )
  assert.ok(s.includes('function handleForgotPassword'), '必须定义 handleForgotPassword 处理函数')
  // 不应假装发送邮件成功 / 不新建后端 API：提示文案应为暂不支持自助找回
  assert.ok(s.includes('暂不支持自助找回'), '忘记密码提示标题应为“暂不支持自助找回”')
  assert.ok(s.includes('请联系管理员重置密码'), '忘记密码提示说明应为“请联系管理员重置密码”')
})

test('注册表单清晰单列（gridTemplateColumns: 1fr）', () => {
  const s = src()
  assert.ok(
    /auth-form-grid"[^>]*style=\{\{[^}]*gridTemplateColumns:\s*'1fr'/.test(s),
    '注册表单必须单列（gridTemplateColumns: 1fr），邮箱/密码/确认密码/邀请码垂直对齐',
  )
})
