// [USER-FIX-3 / B] 修改密码表单校验契约测试（纯函数，node --test）
//
// 覆盖：
//   - 必填（三字段任一为空都不允许提交）
//   - 新密码长度策略 8-128（与后端 ChangePasswordRequest 一致）
//   - confirm 一致性（confirm 不发送给后端，但必须在前端拦住）
//   - 合法输入返回 null（可提交）
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import {
  CHANGE_PASSWORD_MAX_LENGTH,
  CHANGE_PASSWORD_MIN_LENGTH,
  validateChangePasswordForm,
} from '../changePasswordForm.ts'

const current = 'old-password-123'
const next = 'new-password-456'

test('B: 合法输入 → null（可提交）', () => {
  assert.equal(
    validateChangePasswordForm({
      currentPassword: current,
      newPassword: next,
      confirmPassword: next,
    }),
    null,
  )
})

test('B: 任一字段为空 → 拒绝提交', () => {
  for (const input of [
    { currentPassword: '', newPassword: next, confirmPassword: next },
    { currentPassword: current, newPassword: '', confirmPassword: '' },
    { currentPassword: current, newPassword: next, confirmPassword: '' },
  ]) {
    assert.equal(validateChangePasswordForm(input), '请填写全部字段')
  }
})

test('B: 新密码过短（<8）→ 拒绝', () => {
  const short = 'a'.repeat(CHANGE_PASSWORD_MIN_LENGTH - 1)
  const err = validateChangePasswordForm({
    currentPassword: current,
    newPassword: short,
    confirmPassword: short,
  })
  assert.ok(err && err.includes('8-128'), `期望长度错误，实际：${err}`)
})

test('B: 新密码过长（>128）→ 拒绝', () => {
  const long = 'a'.repeat(CHANGE_PASSWORD_MAX_LENGTH + 1)
  const err = validateChangePasswordForm({
    currentPassword: current,
    newPassword: long,
    confirmPassword: long,
  })
  assert.ok(err && err.includes('8-128'), `期望长度错误，实际：${err}`)
})

test('B: 边界长度 8 与 128 → 允许', () => {
  for (const pwd of ['a'.repeat(8), 'a'.repeat(128)]) {
    assert.equal(
      validateChangePasswordForm({
        currentPassword: current,
        newPassword: pwd,
        confirmPassword: pwd,
      }),
      null,
    )
  }
})

test('B: 两次新密码不一致 → 拒绝（confirm 属前端职责）', () => {
  assert.equal(
    validateChangePasswordForm({
      currentPassword: current,
      newPassword: next,
      confirmPassword: `${next}-mismatch`,
    }),
    '两次输入的新密码不一致',
  )
})

test('B: 新密码与当前密码相同 → 前端不拦（由后端判定 400）', () => {
  // 前端不做该判定，避免两套规则；后端返回 400 文案由页面展示。
  assert.equal(
    validateChangePasswordForm({
      currentPassword: current,
      newPassword: current,
      confirmPassword: current,
    }),
    null,
  )
})
