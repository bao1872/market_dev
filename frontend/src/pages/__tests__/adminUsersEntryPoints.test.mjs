// 管理后台三个入口契约测试（源码层最小检查，沿用既有 node --test 体系）
// 用法：node --test src/pages/__tests__/adminUsersEntryPoints.test.mjs
//
// 覆盖本轮三处改动的可观察契约：
//   A. 邀请码入口：＋ 生成邀请码 必须页面常显（不再受 activeTab 限制）
//      且列表页不得再出现必然失败的 handleCopyCode('')
//   B. 管理员重置密码：抽屉需有独立「重置密码」入口与 modal（新密码 + 确认新密码）
//   C. 管理员代管飞书：抽屉需有「飞书通知」tab，且不得把脱敏 secret 当真实密钥提交

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const PAGE_PATH = join(__dirname, '..', 'AdminUsersPage.tsx')
const SRC = readFileSync(PAGE_PATH, 'utf-8')

// =============================================================================
// A. 邀请码入口
// =============================================================================

test('A1 生成邀请码按钮不再受 activeTab 条件限制', () => {
  // 精确检查页头 actions 区块（tab 自身的 active 类名不受影响）
  const at = SRC.indexOf('<div className="actions">')
  assert.ok(at >= 0, '必须存在页头 actions 区块')
  const actionsBlock = SRC.slice(at, at + 400)
  assert.ok(!actionsBlock.includes('activeTab'), '页头 actions 不得再依赖 activeTab')
  assert.ok(actionsBlock.includes('＋ 生成邀请码'), 'actions 内必须有生成邀请码按钮')
})

test('A2 生成按钮仍复用现有 handleOpenModal 与既有 modal/API', () => {
  assert.ok(SRC.includes('＋ 生成邀请码'), '页面必须保留"＋ 生成邀请码"按钮')
  assert.ok(SRC.includes('onClick={handleOpenModal}'), '点击必须复用现有 handleOpenModal')
  assert.ok(SRC.includes('createInviteCodes.mutate'), '生成仍走既有 mutation')
})

test('A3 邀请码列表不再有必然失败的复制按钮', () => {
  assert.ok(
    !SRC.includes("handleCopyCode('')"),
    "列表页 handleCopyCode('') 传空串必然失败，必须删除",
  )
})

test('A4 生成结果里的复制按钮保留（持有明文）', () => {
  assert.ok(
    SRC.includes('handleCopyCode(code.code)'),
    '生成弹窗内必须保留复制（唯一持有明文的地方）',
  )
})

test('A5 未新增邀请码使用次数等新能力', () => {
  // 注意：expires_at 在本页已用于"会员到期时间"（既有能力），此处只约束邀请码新字段
  assert.ok(!SRC.includes('max_uses'), '本轮不引入邀请码使用次数')
  assert.ok(!SRC.includes('used_count'), '本轮不引入邀请码已用次数')
  assert.ok(!SRC.includes('valid_until'), '本轮不引入邀请码有效期')
})

// =============================================================================
// B. 管理员重置密码
// =============================================================================

test('B1 账户信息面板提供独立重置密码入口', () => {
  assert.ok(SRC.includes('重置密码'), '账户信息需有重置密码入口')
  assert.ok(SRC.includes('handleOpenResetPassword'), '需有打开重置密码弹窗的 handler')
})

test('B2 重置密码弹窗包含新密码与确认新密码', () => {
  assert.ok(SRC.includes('setResetPwdNew'), '需有新密码输入')
  assert.ok(SRC.includes('setResetPwdConfirm'), '需有确认新密码输入')
  assert.ok(SRC.includes('resetPwdNew !== resetPwdConfirm'), 'confirm 必须前端校验')
})

test('B3 confirm 不发送到后端', () => {
  // 只提交 newPassword；不得出现 confirm 作为请求字段
  assert.ok(
    !SRC.includes('confirmPassword') && !SRC.includes('confirm_password'),
    '确认密码不得作为请求字段发送',
  )
  assert.ok(SRC.includes('newPassword: resetPwdNew'), '只提交 newPassword')
})

test('B4 提示已登录会话不会立即退出', () => {
  assert.ok(
    SRC.includes('现有已登录会话不会立即退出'),
    '需明示 JWT 无状态、旧 token 不会立即失效',
  )
})

// =============================================================================
// C. 管理员代管飞书
// =============================================================================

test('C1 抽屉存在飞书通知 tab', () => {
  assert.ok(SRC.includes("drawerTab === 'feishu'"), '需有飞书通知 tab 面板')
  assert.ok(SRC.includes('飞书通知'), '需有"飞书通知"tab 标签')
})

test('C2 支持查看/新增/编辑/验证/测试/删除', () => {
  for (const fn of [
    'handleOpenFeishuForm',
    'handleSubmitFeishu',
    'handleVerifyFeishu',
    'handleTestFeishu',
    'handleDeleteFeishu',
  ]) {
    assert.ok(SRC.includes(fn), `缺少 ${fn}`)
  }
})

test('C3 不得把脱敏 secret 当作真实 app_secret 提交', () => {
  // 脱敏值以 **** 开头；提交时必须剔除
  assert.ok(
    SRC.includes("feishuForm.app_secret.startsWith('****')"),
    '必须识别 **** 前缀的脱敏值',
  )
  assert.ok(
    SRC.includes('target_config.app_secret = feishuForm.app_secret'),
    '仅非脱敏且非空时才提交 app_secret',
  )
})

test('C4 字段沿用 feishu_platform_app 现有字段集', () => {
  for (const f of [
    'display_name',
    'app_id',
    'app_secret',
    'receive_id',
    'receive_id_type',
  ]) {
    assert.ok(SRC.includes(f), `缺少字段 ${f}`)
  }
  assert.ok(SRC.includes("adapter_type: 'feishu_platform_app'"), '必须使用平台应用模式')
})

test('C5 未使用已废弃的 feishu_webhook', () => {
  assert.ok(!SRC.includes('feishu_webhook'), '不得再使用已废弃的 feishu_webhook')
})
