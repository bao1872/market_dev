// [Auth] - 描述: 前端 AccessProfile 统一契约测试（Phase 2 Task 2.6）
// 用法：node --experimental-strip-types --test scripts/contract-tests/access-profile.test.ts
// 覆盖：
// 1. endpoints.ts 定义 AccessProfile 接口（11 字段，对齐后端 AccessProfileResponse）
// 2. LoginResponse 含 10 个 AccessProfile 字段（含 next_route）
// 3. LoginResponse 不再含 membership_expired 字段（已被 subscription_active 替代）
// 4. AuthUser 不再使用 role: 'admin' | 'member'，改用 is_admin/roles/subscription_active 等
// 5. App.tsx AdminRoute 使用 is_admin 而非 user.role
// 6. App.tsx 存在 SubscriberRoute 守卫（检查 subscription_active，admin 豁免）
// 7. LoginPage.tsx 不再引用 membership_expired
// 8. LoginPage.tsx 使用 next_route 跳转（对齐后端返回）
// 9. LoginPage.tsx 无 expired@quant.local 演示提示
// 10. endpoints.ts 存在 getMyAccess 函数调用 GET /me/access

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const ENDPOINTS_PATH = join(__dirname, '..', '..', 'src', 'api', 'endpoints.ts')
const AUTH_STORE_PATH = join(__dirname, '..', '..', 'src', 'store', 'auth.ts')
const APP_TSX_PATH = join(__dirname, '..', '..', 'src', 'App.tsx')
const LOGIN_PAGE_PATH = join(__dirname, '..', '..', 'src', 'pages', 'LoginPage.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. AccessProfile 接口定义（11 字段，对齐后端 AccessProfileResponse） =====
test('endpoints.ts 定义 export interface AccessProfile', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+interface\s+AccessProfile\s*\{/.test(src),
    'endpoints.ts 必须定义 export interface AccessProfile',
  )
  // [Auth] - 描述: 校验 11 个字段全部出现在接口体中
  const requiredFields = [
    'user_id',
    'account_status',
    'roles',
    'is_admin',
    'is_member',
    'subscription_active',
    'plan_code',
    'plan_display_name',
    'expires_at',
    'features',
    'limits',
  ]
  for (const field of requiredFields) {
    assert.ok(
      src.includes(`${field}:`),
      `AccessProfile 接口必须包含字段 ${field}`,
    )
  }
})

// ===== 2. LoginResponse 含 10 个 AccessProfile 字段（含 next_route） =====
test('LoginResponse 含 10 个 AccessProfile 字段（含 next_route）', () => {
  const src = readSource(ENDPOINTS_PATH)
  // [Auth] - 描述: 提取 LoginResponse 接口体
  const match = src.match(/export\s+interface\s+LoginResponse\s*\{([\s\S]*?)\}/)
  assert.ok(match, 'endpoints.ts 必须定义 LoginResponse 接口')
  const body = match[1]
  const requiredFields = [
    'is_admin',
    'roles',
    'subscription_required',
    'subscription_active',
    'plan_code',
    'plan_display_name',
    'expires_at',
    'features',
    'limits',
    'next_route',
  ]
  for (const field of requiredFields) {
    assert.ok(
      body.includes(`${field}:`),
      `LoginResponse 接口必须包含 AccessProfile 字段 ${field}`,
    )
  }
})

// ===== 3. LoginResponse 不再含 membership_expired 字段 =====
test('LoginResponse 不再含 membership_expired 字段', () => {
  const src = readSource(ENDPOINTS_PATH)
  const match = src.match(/export\s+interface\s+LoginResponse\s*\{([\s\S]*?)\}/)
  assert.ok(match, 'endpoints.ts 必须定义 LoginResponse 接口')
  const body = match[1]
  assert.ok(
    !/membership_expired/.test(body),
    'LoginResponse 接口禁止再包含 membership_expired 字段（已被 subscription_active 替代）',
  )
})

// ===== 4. AuthUser 不再使用 role: 'admin' | 'member'，改用 is_admin 字段 =====
test('AuthUser 不再使用 role: UserRole 单值，改用 is_admin/roles 等字段', () => {
  const src = readSource(AUTH_STORE_PATH)
  const match = src.match(/export\s+interface\s+AuthUser\s*\{([\s\S]*?)\}/)
  assert.ok(match, 'store/auth.ts 必须定义 AuthUser 接口')
  const body = match[1]
  // [Auth] - 描述: 禁止再出现 role: UserRole 或 role: 'admin' | 'member' 单值字段
  assert.ok(
    !/role:\s*UserRole/.test(body) && !/role:\s*['"]admin['"]/.test(body),
    'AuthUser 接口禁止再使用 role: UserRole 单值字段',
  )
  // [Auth] - 描述: 必须包含 is_admin 布尔字段（替代旧 role 单值判断）
  assert.ok(
    /is_admin:\s*boolean/.test(body),
    'AuthUser 接口必须包含 is_admin: boolean 字段',
  )
  // [Auth] - 描述: 必须包含 roles 列表字段（对齐后端 roles[]）
  assert.ok(
    /roles:\s*string\[\]/.test(body),
    'AuthUser 接口必须包含 roles: string[] 字段',
  )
  // [Auth] - 描述: 必须包含 subscription_active 字段（路由守卫依赖）
  assert.ok(
    /subscription_active:\s*boolean/.test(body),
    'AuthUser 接口必须包含 subscription_active: boolean 字段',
  )
})

// ===== 5. App.tsx AdminRoute 使用 is_admin 而非 user.role =====
test('App.tsx AdminRoute 使用 is_admin 而非 user.role', () => {
  const src = readSource(APP_TSX_PATH)
  // [Auth] - 描述: AdminRoute 守卫必须改用 is_admin（禁止再用 user?.role）
  assert.ok(
    /is_admin/.test(src),
    'App.tsx 必须使用 is_admin 字段进行权限判断',
  )
  // [Auth] - 描述: AdminRoute 函数体内禁止出现 user?.role 或 user.role
  const adminRouteMatch = src.match(/function\s+AdminRoute\s*\(\s*\)\s*\{([\s\S]*?)\n\}/)
  assert.ok(adminRouteMatch, 'App.tsx 必须定义 AdminRoute 函数')
  assert.ok(
    !/user\?\.role/.test(adminRouteMatch[1]) && !/user\.role/.test(adminRouteMatch[1]),
    'AdminRoute 函数体禁止使用 user.role 或 user?.role（应改用 user.is_admin）',
  )
  assert.ok(
    /is_admin/.test(adminRouteMatch[1]),
    'AdminRoute 函数体必须使用 is_admin 字段',
  )
})

// ===== 6. App.tsx 存在 SubscriberRoute 守卫（检查 subscription_active，admin 豁免） =====
test('App.tsx 存在 SubscriberRoute 守卫检查 subscription_active', () => {
  const src = readSource(APP_TSX_PATH)
  // [Auth] - 描述: 必须定义 SubscriberRoute 函数
  assert.ok(
    /function\s+SubscriberRoute\s*\(\s*\)\s*\{/.test(src),
    'App.tsx 必须定义 SubscriberRoute 守卫函数',
  )
  // [Auth] - 描述: SubscriberRoute 函数体必须检查 subscription_active
  const subscriberRouteMatch = src.match(
    /function\s+SubscriberRoute\s*\(\s*\)\s*\{([\s\S]*?)\n\}/,
  )
  assert.ok(subscriberRouteMatch, 'App.tsx 必须定义 SubscriberRoute 函数体')
  assert.ok(
    /subscription_active/.test(subscriberRouteMatch[1]),
    'SubscriberRoute 函数体必须检查 subscription_active 字段',
  )
  // [Auth] - 描述: admin 用户豁免（is_admin=true 直接通过，不强制订阅）
  assert.ok(
    /is_admin/.test(subscriberRouteMatch[1]),
    'SubscriberRoute 函数体必须包含 is_admin 豁免逻辑（admin 直接通过）',
  )
  // [Auth] - 描述: 非订阅用户重定向到 /membership-expired
  assert.ok(
    /\/membership-expired/.test(subscriberRouteMatch[1]),
    'SubscriberRoute 函数体必须将非订阅用户重定向到 /membership-expired',
  )
})

// ===== 7. LoginPage.tsx 不再引用 membership_expired =====
test('LoginPage.tsx 不再引用 membership_expired', () => {
  const src = readSource(LOGIN_PAGE_PATH)
  assert.ok(
    !/membership_expired/.test(src),
    'LoginPage.tsx 禁止再引用 membership_expired（已由后端 next_route + subscription_active 替代）',
  )
})

// ===== 8. LoginPage.tsx 使用 next_route 跳转（对齐后端返回） =====
test('LoginPage.tsx 使用 next_route 跳转', () => {
  const src = readSource(LOGIN_PAGE_PATH)
  // [Auth] - 描述: 登录成功后必须使用后端返回的 next_route 进行跳转
  assert.ok(
    /next_route/.test(src),
    'LoginPage.tsx 必须使用 next_route 字段进行登录后跳转',
  )
  // [Auth] - 描述: navigate 调用必须使用 next_route（禁止硬编码 /overview）
  assert.ok(
    /navigate\s*\(\s*[^)]*next_route/.test(src),
    'LoginPage.tsx navigate 必须使用 next_route 参数',
  )
})

// ===== 9. LoginPage.tsx 无 expired@quant.local 演示提示 =====
test('LoginPage.tsx 无 expired@quant.local 演示提示', () => {
  const src = readSource(LOGIN_PAGE_PATH)
  assert.ok(
    !/expired@quant\.local/.test(src),
    'LoginPage.tsx 禁止出现 expired@quant.local 演示提示（演示数据需移除）',
  )
})

// ===== 10. endpoints.ts 存在 getMyAccess 函数调用 GET /me/access =====
test('endpoints.ts 存在 getMyAccess 函数调用 /me/access', () => {
  const src = readSource(ENDPOINTS_PATH)
  // [Auth] - 描述: 必须导出 getMyAccess 异步函数
  assert.ok(
    /export\s+async\s+function\s+getMyAccess\s*\(/.test(src),
    'endpoints.ts 必须导出 async function getMyAccess',
  )
  // [Auth] - 描述: 函数体内必须调用 /me/access 端点
  const match = src.match(
    /export\s+async\s+function\s+getMyAccess\s*\([^)]*\)\s*:?\s*[^{]*\{([\s\S]*?)\n\}/,
  )
  assert.ok(match, 'endpoints.ts 必须定义 getMyAccess 函数体')
  assert.ok(
    /\/me\/access/.test(match[1]),
    'getMyAccess 函数体必须调用 /me/access 端点',
  )
})
