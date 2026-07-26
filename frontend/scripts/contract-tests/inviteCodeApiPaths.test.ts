// [V2.1] - 描述: 邀请码 V2 前后端 API 路径对账契约测试（Phase I3）
// 用法：node --experimental-strip-types --test scripts/contract-tests/inviteCodeApiPaths.test.ts
//
// 验证 PRD §6 + §8.1：
// 1. 前端 endpoints.ts 定义三个 V2 函数：createInviteCodesV2 / getInviteCodesV2 / revokeInviteCodeV2
// 2. 前端 V2 路径统一使用 /admin/v2/invite-codes（POST 创建 / GET 列表 / POST {id}/revoke 撤销）
// 3. 前端不存在旧的 /admin/invite-codes-v2 错误路径
// 4. 前端 V2 函数不复用 V1 的 /admin/invite-codes 路径
// 5. 前端 V1 函数（createInviteCodes / getInviteCodes / revokeInviteCode）仍保留向后兼容
// 6. 前端 V2 函数返回类型对齐（InviteCodeV2Response / InviteCodeV2ListResponse / InviteCodeV2ListItem）
//
// 后端 OpenAPI 路径在 backend/tests/test_admin_invite_capability_v2.py 集成测试中验证，
// 此前端契约测试只做静态源码对账，确保前端调用路径与后端 router prefix 一致。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const ENDPOINTS_PATH = join(__dirname, '..', '..', 'src', 'api', 'endpoints.ts')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. V2 函数定义 =====

test('endpoints.ts 定义 createInviteCodesV2 函数', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+async\s+function\s+createInviteCodesV2\s*\(/.test(src),
    '必须定义 export async function createInviteCodesV2',
  )
  // 返回类型
  assert.ok(
    /createInviteCodesV2[^)]*\):\s*Promise<InviteCodeV2Response\[\]>/.test(src),
    'createInviteCodesV2 返回类型必须是 Promise<InviteCodeV2Response[]>',
  )
})

test('endpoints.ts 定义 getInviteCodesV2 函数', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+async\s+function\s+getInviteCodesV2\s*\(/.test(src),
    '必须定义 export async function getInviteCodesV2',
  )
  assert.ok(
    /getInviteCodesV2[^)]*\):\s*Promise<InviteCodeV2ListResponse>/.test(src),
    'getInviteCodesV2 返回类型必须是 Promise<InviteCodeV2ListResponse>',
  )
})

test('endpoints.ts 定义 revokeInviteCodeV2 函数', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+async\s+function\s+revokeInviteCodeV2\s*\(/.test(src),
    '必须定义 export async function revokeInviteCodeV2',
  )
  assert.ok(
    /revokeInviteCodeV2[^)]*\):\s*Promise<InviteCodeV2ListItem>/.test(src),
    'revokeInviteCodeV2 返回类型必须是 Promise<InviteCodeV2ListItem>',
  )
})

// ===== 2. V2 路径一致性 =====

test('createInviteCodesV2 使用 POST /admin/v2/invite-codes', () => {
  const src = readSource(ENDPOINTS_PATH)
  // 在 createInviteCodesV2 函数体内必须出现 '/admin/v2/invite-codes'
  const fnMatch = src.match(
    /async\s+function\s+createInviteCodesV2[\s\S]*?\n\}\n/,
  )
  assert.ok(fnMatch, '必须能匹配到 createInviteCodesV2 函数体')
  assert.ok(
    /apiClient\.post<InviteCodeV2Response\[\]>\(\s*['"]\/admin\/v2\/invite-codes['"]/.test(
      fnMatch[0],
    ),
    'createInviteCodesV2 必须使用 apiClient.post<InviteCodeV2Response[]>("/admin/v2/invite-codes", ...)',
  )
})

test('getInviteCodesV2 使用 GET /admin/v2/invite-codes', () => {
  const src = readSource(ENDPOINTS_PATH)
  const fnMatch = src.match(
    /async\s+function\s+getInviteCodesV2[\s\S]*?\n\}\n/,
  )
  assert.ok(fnMatch, '必须能匹配到 getInviteCodesV2 函数体')
  assert.ok(
    /apiClient\.get<InviteCodeV2ListResponse>\(\s*['"]\/admin\/v2\/invite-codes['"]/.test(
      fnMatch[0],
    ),
    'getInviteCodesV2 必须使用 apiClient.get<InviteCodeV2ListResponse>("/admin/v2/invite-codes", ...)',
  )
})

test('revokeInviteCodeV2 使用 POST /admin/v2/invite-codes/{id}/revoke', () => {
  const src = readSource(ENDPOINTS_PATH)
  const fnMatch = src.match(
    /async\s+function\s+revokeInviteCodeV2[\s\S]*?\n\}\n/,
  )
  assert.ok(fnMatch, '必须能匹配到 revokeInviteCodeV2 函数体')
  assert.ok(
    /apiClient\.post<InviteCodeV2ListItem>\(\s*['"`]\/admin\/v2\/invite-codes\/\$\{inviteCodeId\}\/revoke['"`]/.test(
      fnMatch[0],
    ),
    'revokeInviteCodeV2 必须使用 apiClient.post<InviteCodeV2ListItem>(`/admin/v2/invite-codes/${inviteCodeId}/revoke`, ...)',
  )
})

// ===== 3. 不存在旧的 /admin/invite-codes-v2 错误路径 =====

test('endpoints.ts 不存在 /admin/invite-codes-v2 错误路径', () => {
  const src = readSource(ENDPOINTS_PATH)
  // 任何 /admin/invite-codes-v2 出现都是错误的（应为 /admin/v2/invite-codes）
  assert.ok(
    !/['"`]\/admin\/invite-codes-v2/.test(src),
    '不应存在 /admin/invite-codes-v2 错误路径（V2 路径为 /admin/v2/invite-codes）',
  )
})

test('endpoints.ts 不存在 /admin/v1/invite-codes 错误路径', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    !/['"`]\/admin\/v1\/invite-codes/.test(src),
    '不应存在 /admin/v1/invite-codes 错误路径',
  )
})

// ===== 4. V1 函数向后兼容 =====

test('endpoints.ts 仍保留 V1 createInviteCodes（向后兼容）', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+async\s+function\s+createInviteCodes\s*\(/.test(src),
    'V1 createInviteCodes 必须保留（向后兼容）',
  )
  // V1 使用 /admin/invite-codes（不带 v2）
  const fnMatch = src.match(
    /async\s+function\s+createInviteCodes\s*\([\s\S]*?\n\}\n/,
  )
  assert.ok(fnMatch, '必须能匹配到 V1 createInviteCodes 函数体')
  assert.ok(
    /apiClient\.post<InviteCode\[\]>\(\s*['"]\/admin\/invite-codes['"]/.test(
      fnMatch[0],
    ),
    'V1 createInviteCodes 必须使用 /admin/invite-codes',
  )
})

test('endpoints.ts 仍保留 V1 getInviteCodes（向后兼容）', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+async\s+function\s+getInviteCodes\s*\(/.test(src),
    'V1 getInviteCodes 必须保留',
  )
})

test('endpoints.ts 仍保留 V1 revokeInviteCode（向后兼容）', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+async\s+function\s+revokeInviteCode\s*\(/.test(src),
    'V1 revokeInviteCode 必须保留',
  )
})

// ===== 5. V2 类型定义对齐 =====

test('endpoints.ts 定义 InviteCodeV2Response 类型', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+interface\s+InviteCodeV2Response\s*\{/.test(src),
    '必须定义 export interface InviteCodeV2Response',
  )
})

test('endpoints.ts 定义 InviteCodeV2ListResponse 类型', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+interface\s+InviteCodeV2ListResponse\s*\{/.test(src),
    '必须定义 export interface InviteCodeV2ListResponse',
  )
})

test('endpoints.ts 定义 InviteCodeV2ListItem 类型', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+interface\s+InviteCodeV2ListItem\s*\{/.test(src),
    '必须定义 export interface InviteCodeV2ListItem',
  )
})

test('endpoints.ts 定义 InviteCodeV2CreateRequest 类型', () => {
  const src = readSource(ENDPOINTS_PATH)
  assert.ok(
    /export\s+interface\s+InviteCodeV2CreateRequest\s*\{/.test(src),
    '必须定义 export interface InviteCodeV2CreateRequest',
  )
})
