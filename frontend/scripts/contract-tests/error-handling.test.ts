// [Auth] - 描述: 前端 401/403 错误处理隔离契约测试（Phase 3 Task 3.5）
// 用法：node --experimental-strip-types --test scripts/contract-tests/error-handling.test.ts
// 覆盖：
// 1. 403 响应不会调用 logout（不清除登录态）
// 2. 403 响应不会修改 window.location（不跳转 /login）
// 3. 401 响应触发 refresh token 流程（调用 refreshTokenSingleton）
// 4. 401 refresh 失败时清除登录态（refreshTokenSingleton catch 调用 logout）
// 5. 401 与 403 处理逻辑完全隔离（401 分支与 reject 分支明确分离）
// 6. 403 响应显示全局 toast 通知（友好提示权限不足）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const CLIENT_PATH = join(__dirname, '..', '..', 'src', 'api', 'client.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// 提取响应拦截器 error handler 函数体（async (error) => { ... }）
// 用于精准定位 401/403 处理逻辑所在区域
function extractResponseErrorHandlerBody(src: string): string {
  const match = src.match(
    /apiClient\.interceptors\.response\.use\([\s\S]*?async\s*\(error[^)]*\)\s*=>\s*\{([\s\S]*?)\n\s*\},?\s*\)/,
  )
  assert.ok(match, 'client.ts 必须定义 apiClient.interceptors.response.use 响应拦截器')
  return match[1]
}

// 提取 refreshTokenSingleton 函数体（用于验证 401 refresh 失败时 logout）
function extractRefreshSingletonBody(src: string): string {
  const match = src.match(
    /async\s+function\s+refreshTokenSingleton\s*\([^)]*\)\s*:?\s*[^{]*\{([\s\S]*?)\n\}/,
  )
  assert.ok(match, 'client.ts 必须定义 async function refreshTokenSingleton')
  return match[1]
}

// ===== 1. 403 不清除登录态 =====
test('test_403_does_not_clear_login_state', () => {
  const src = readSource(CLIENT_PATH)
  const handlerBody = extractResponseErrorHandlerBody(src)
  // [Auth] - 描述: 403 走 "非 401 直接 reject" 分支，该分支不得调用 logout
  // 验证：reject 分支（status !== 401）只包含 Promise.reject，不含 logout 调用
  const rejectBranchMatch = handlerBody.match(
    /if\s*\(\s*error\.response\?\.status\s*!==\s*401[^{]*\{([^}]*?)\}/,
  )
  assert.ok(rejectBranchMatch, '响应拦截器必须存在 status !== 401 的 reject 分支')
  const rejectBranch = rejectBranchMatch[1]
  assert.ok(
    !/logout\s*\(/.test(rejectBranch),
    '403（非 401）reject 分支禁止调用 logout（不得清除登录态）',
  )
  assert.ok(
    /Promise\.reject/.test(rejectBranch),
    '403（非 401）reject 分支必须调用 Promise.reject(error)',
  )
})

// ===== 2. 403 不跳转到 /login =====
test('test_403_does_not_redirect_to_login', () => {
  const src = readSource(CLIENT_PATH)
  const handlerBody = extractResponseErrorHandlerBody(src)
  // [Auth] - 描述: 403 走 reject 分支，不得修改 window.location
  const rejectBranchMatch = handlerBody.match(
    /if\s*\(\s*error\.response\?\.status\s*!==\s*401[^{]*\{([^}]*?)\}/,
  )
  assert.ok(rejectBranchMatch, '响应拦截器必须存在 status !== 401 的 reject 分支')
  const rejectBranch = rejectBranchMatch[1]
  assert.ok(
    !/window\.location/.test(rejectBranch),
    '403（非 401）reject 分支禁止修改 window.location（不得跳转登录页）',
  )
  assert.ok(
    !/\/login/.test(rejectBranch),
    '403（非 401）reject 分支禁止出现 /login 跳转',
  )
})

// ===== 3. 401 触发 refresh token 流程 =====
test('test_401_triggers_refresh_flow', () => {
  const src = readSource(CLIENT_PATH)
  const handlerBody = extractResponseErrorHandlerBody(src)
  // [Auth] - 描述: 401 分支必须调用 refreshTokenSingleton 触发刷新流程
  assert.ok(
    /refreshTokenSingleton\s*\(/.test(handlerBody),
    '响应拦截器 401 处理路径必须调用 refreshTokenSingleton 触发 refresh token 流程',
  )
  // 验证 status === 401 的判断存在
  assert.ok(
    /error\.response\?\.status\s*!==\s*401/.test(handlerBody) ||
      /error\.response\?\.status\s*===\s*401/.test(handlerBody),
    '响应拦截器必须根据 status === 401 区分处理',
  )
})

// ===== 4. 401 refresh 失败清除登录态 =====
test('test_401_refresh_failure_clears_login', () => {
  const src = readSource(CLIENT_PATH)
  const refreshBody = extractRefreshSingletonBody(src)
  // [Auth] - 描述: refreshTokenSingleton catch 块必须调用 useAuthStore.getState().logout()
  const catchMatch = refreshBody.match(/catch\s*\(\s*\w+\s*\)\s*\{([\s\S]*?)\}/)
  assert.ok(catchMatch, 'refreshTokenSingleton 必须包含 catch 块处理刷新失败')
  const catchBody = catchMatch[1]
  assert.ok(
    /useAuthStore\.getState\(\)\.logout\s*\(/.test(catchBody),
    'refreshTokenSingleton 刷新失败时必须调用 useAuthStore.getState().logout() 清除登录态',
  )
})

// ===== 5. 401 与 403 处理逻辑完全隔离 =====
test('test_401_and_403_handling_are_separate', () => {
  const src = readSource(CLIENT_PATH)
  const handlerBody = extractResponseErrorHandlerBody(src)
  // [Auth] - 描述: 响应拦截器必须首先用 status !== 401 把 403/其他错误隔离到 reject 分支
  assert.ok(
    /if\s*\(\s*error\.response\?\.status\s*!==\s*401/.test(handlerBody),
    '响应拦截器必须以 status !== 401 作为首个分流条件，隔离 401 与 403/其他错误',
  )
  // [Auth] - 描述: 401 分支（refresh 路径）的 logout 调用只能在 refresh 失败 catch 中
  // 不能出现在 reject 分支，确保 403 不会误触发 401 的 logout 逻辑
  const rejectBranchMatch = handlerBody.match(
    /if\s*\(\s*error\.response\?\.status\s*!==\s*401[^{]*\{([^}]*?)\}/,
  )
  assert.ok(rejectBranchMatch, '响应拦截器必须存在 status !== 401 的 reject 分支')
  const rejectBranch = rejectBranchMatch[1]
  assert.ok(
    !/refreshTokenSingleton/.test(rejectBranch),
    '403（非 401）reject 分支禁止调用 refreshTokenSingleton（401/403 处理必须隔离）',
  )
})

// ===== 6. 403 显示全局 toast 通知（友好提示权限不足） =====
test('test_403_shows_toast_notification', () => {
  const src = readSource(CLIENT_PATH)
  // [Auth] - 描述: 必须导入 useToast 以便 403 显示友好提示
  assert.ok(
    /import\s*\{[^}]*useToast[^}]*\}\s*from\s*['"][^'"]*toast['"]/.test(src),
    'client.ts 必须从 toast store 导入 useToast',
  )
  // [Auth] - 描述: 响应拦截器必须存在显式 403 处理分支，调用 useToast.getState().show()
  const handlerBody = extractResponseErrorHandlerBody(src)
  assert.ok(
    /error\.response\?\.status\s*===\s*403/.test(handlerBody),
    '响应拦截器必须存在 status === 403 的显式处理分支',
  )
  assert.ok(
    /useToast\.getState\(\)\.show\s*\(/.test(handlerBody),
    '403 处理分支必须调用 useToast.getState().show() 显示友好 toast 通知',
  )
})
