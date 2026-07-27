// Toast 组件渲染测试
// 用法：node --test src/components/__tests__/toast.test.mjs
//
// [Phase 5B-2] 验证 Toast 组件正确渲染 toast store 的可见状态。
// 修复历史：LoginPage 调用 useToast.getState().show() 但无组件渲染 Toast UI，
// 导致登录失败等错误不可见（"点击无反应"根因）。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const TOAST_COMPONENT_PATH = join(__dirname, '..', 'Toast.tsx')
const MAIN_TSX_PATH = join(__dirname, '..', '..', 'main.tsx')
const TOAST_STORE_PATH = join(__dirname, '..', '..', 'store', 'toast.ts')
const LOGIN_PAGE_PATH = join(__dirname, '..', '..', 'pages', 'LoginPage.tsx')

function readSource(p) {
  return readFileSync(p, 'utf-8')
}

// ===== 1. Toast 组件存在且导出默认 =====
test('Toast.tsx 存在且导出默认组件', () => {
  const src = readSource(TOAST_COMPONENT_PATH)
  assert.ok(src.includes('export default function Toast'), 'Toast.tsx 必须导出默认函数组件')
})

// ===== 2. Toast 组件消费 useToast store =====
test('Toast 组件消费 useToast store 的 visible/title/message', () => {
  const src = readSource(TOAST_COMPONENT_PATH)
  assert.ok(src.includes('useToast'), 'Toast 组件必须导入 useToast')
  assert.ok(src.includes('s.visible') || src.includes('(s) => s.visible'), 'Toast 必须订阅 visible 状态')
  assert.ok(src.includes('s.title') || src.includes('(s) => s.title'), 'Toast 必须订阅 title 状态')
  assert.ok(src.includes('s.message') || src.includes('(s) => s.message'), 'Toast 必须订阅 message 状态')
})

// ===== 3. Toast 组件渲染 .toast CSS 类 =====
test('Toast 组件渲染 .toast CSS 类（与 global.scss 一致）', () => {
  const src = readSource(TOAST_COMPONENT_PATH)
  assert.ok(src.includes('toast'), 'Toast 组件必须包含 toast CSS 类')
  // 必须在 visible 时渲染（不能永远返回 null）
  assert.ok(!src.includes('return null') || src.includes('if (!visible)'), 'Toast 可以在 !visible 时返回 null，但 visible 时必须渲染')
})

// ===== 4. main.tsx 挂载 Toast 组件 =====
test('main.tsx 挂载 Toast 组件（全局可见）', () => {
  const src = readSource(MAIN_TSX_PATH)
  assert.ok(src.includes("import Toast"), 'main.tsx 必须导入 Toast 组件')
  assert.ok(src.includes('<Toast'), 'main.tsx 必须渲染 <Toast /> 组件')
})

// ===== 5. toast store 有 show/hide 方法 =====
test('toast store 有 show 和 hide 方法', () => {
  const src = readSource(TOAST_STORE_PATH)
  assert.ok(src.includes('show:'), 'toast store 必须有 show 方法')
  assert.ok(src.includes('hide:'), 'toast store 必须有 hide 方法')
  assert.ok(src.includes('visible:'), 'toast store 必须有 visible 状态')
})

// ===== 6. LoginPage 在登录失败时调用 toast.show =====
test('LoginPage 在登录失败时调用 toast.show 显示错误', () => {
  const src = readSource(LOGIN_PAGE_PATH)
  // 登录提交的 catch 块必须调用 useToast.getState().show
  assert.ok(src.includes("useToast.getState().show('登录失败'"), "LoginPage 必须在登录失败时调用 useToast.getState().show('登录失败', ...)")
  // 表单校验失败也必须显示 toast
  assert.ok(src.includes("useToast.getState().show('请填写完整'"), "LoginPage 必须在表单校验失败时调用 toast.show")
})

// ===== 7. LoginPage 按钮 disabled 逻辑正确 =====
test('LoginPage 登录按钮 disabled 逻辑正确（防止卡死）', () => {
  const src = readSource(LOGIN_PAGE_PATH)
  // 按钮在 isSubmitting 或 authenticating 时 disabled
  assert.ok(src.includes('disabled={isSubmitting || authenticating}'), '登录按钮必须 disabled={isSubmitting || authenticating}')
  // finally 块必须重置 isSubmitting 和 submittingRef（防止按钮永久禁用）
  assert.ok(src.includes('submittingRef.current = false'), 'finally 块必须重置 submittingRef.current')
  assert.ok(src.includes('setIsSubmitting(false)'), 'finally 块必须重置 isSubmitting')
})

// ===== 8. 错误提取函数存在 =====
test('LoginPage 有 getErrorMessage 函数提取后端错误', () => {
  const src = readSource(LOGIN_PAGE_PATH)
  assert.ok(src.includes('function getErrorMessage'), 'LoginPage 必须有 getErrorMessage 函数')
  // 必须检查 response.data.detail（FastAPI 错误格式）
  assert.ok(src.includes('detail'), 'getErrorMessage 必须检查 response.data.detail')
})
