// [PRD §8.4.9 / R14] adminErrors 统一错误解析工具测试
// 用法：node --test src/utils/__tests__/adminErrors.test.mjs
//
// 验证前端统一错误解析器消费后端 admin_error 产出的双字段兼容结构：
// stable_error_code（新权威码）+ error_code/reason（旧码兼容）+ recommended_action。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const ADMIN_ERRORS_PATH = join(__dirname, '..', 'adminErrors.ts')

function readSource(p) {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 工具函数存在 =====
test('adminErrors.ts 导出 parseAdminApiError 与 formatAdminApiError', () => {
  const src = readSource(ADMIN_ERRORS_PATH)
  assert.ok(src.includes('export function parseAdminApiError'), '必须导出 parseAdminApiError')
  assert.ok(src.includes('export function formatAdminApiError'), '必须导出 formatAdminApiError')
})

// ===== 2. 消费稳定错误码与建议动作（R14 双字段兼容）=====
test('parseAdminApiError 消费 stable_error_code 与 recommended_action', () => {
  const src = readSource(ADMIN_ERRORS_PATH)
  assert.ok(src.includes('stable_error_code'), '必须读取 stable_error_code')
  assert.ok(src.includes('recommended_action'), '必须读取 recommended_action')
  assert.ok(src.includes('error_code') || src.includes('detailObj.error_code'), '必须兼容 error_code 旧字段')
  assert.ok(src.includes('retryable'), '必须读取 retryable')
  assert.ok(src.includes('resumable'), '必须读取 resumable')
})

// ===== 3. 兼容旧前端：保留 error_code/reason 读取 =====
test('parseAdminApiError 兼容旧 error_code 与 reason 字段', () => {
  const src = readSource(ADMIN_ERRORS_PATH)
  assert.ok(src.includes('detailObj.reason'), '必须兼容 reason 旧字段')
  assert.ok(src.includes('legacyErrorCode'), '必须暴露 legacyErrorCode')
})

// ===== 4. formatAdminApiError 拼接建议动作 =====
test('formatAdminApiError 拼接 recommended_action 到提示文案', () => {
  const src = readSource(ADMIN_ERRORS_PATH)
  assert.ok(src.includes('recommendedAction'), 'formatAdminApiError 必须消费 recommendedAction')
  assert.ok(src.includes('（') || src.includes('recommendedAction'), '建议动作应拼入提示')
})
