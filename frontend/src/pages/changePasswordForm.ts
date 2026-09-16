// [USER-FIX-3 / B] 修改密码表单的前端校验（纯函数，供 node --test 直接断言）
//
// 职责边界：
//   - 前端只做「可提交性」预校验（必填 / 长度 / 两次一致）与 confirm 一致性；
//   - 服务端仍是密码策略的唯一权威（schemas/user.py ChangePasswordRequest 8-128），
//     前端校验不替代服务端校验。
//   - confirm_password 仅前端使用，不发送给后端。

/** 与后端 ChangePasswordRequest 保持一致的密码长度策略（前端唯一来源） */
export const CHANGE_PASSWORD_MIN_LENGTH = 8
export const CHANGE_PASSWORD_MAX_LENGTH = 128

export interface ChangePasswordFormInput {
  currentPassword: string
  newPassword: string
  confirmPassword: string
}

/**
 * 校验修改密码表单。
 *
 * @returns 错误文案；返回 null 表示可提交。
 */
export function validateChangePasswordForm(
  input: ChangePasswordFormInput,
): string | null {
  const { currentPassword, newPassword, confirmPassword } = input
  if (!currentPassword || !newPassword || !confirmPassword) {
    return '请填写全部字段'
  }
  if (
    newPassword.length < CHANGE_PASSWORD_MIN_LENGTH ||
    newPassword.length > CHANGE_PASSWORD_MAX_LENGTH
  ) {
    return `新密码长度需为 ${CHANGE_PASSWORD_MIN_LENGTH}-${CHANGE_PASSWORD_MAX_LENGTH} 个字符`
  }
  if (newPassword !== confirmPassword) {
    return '两次输入的新密码不一致'
  }
  return null
}
