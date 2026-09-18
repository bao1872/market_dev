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

test('A3 新邀请码列表可展示/复制；历史码显示不可恢复', () => {
  // [PANJI-BIZ-FIX Commit B2] 旧合同「列表不得显示明文」已退休
  assert.ok(SRC.includes('历史码不可恢复'), 'code=null 时必须显示"历史码不可恢复"')
  assert.ok(
    SRC.includes('handleCopyCode(row.code as string)'),
    'code!=null 时必须可复制（传 row.code，不传空串）',
  )
  assert.ok(!SRC.includes("handleCopyCode('')"), '不得传空串（必然失败）')
})

test('A4 生成结果里的复制按钮保留（持有明文）', () => {
  assert.ok(
    SRC.includes('handleCopyCode(code.code)'),
    '生成弹窗内必须保留复制（持有明文）',
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

// =============================================================================
// D. 邀请码管理（[PANJI-BIZ-FIX Commit B1/B2/B3]）
// =============================================================================

test('D1 effective 最大自选优先 capability.watchlist_limit（render/sortValue 共用 helper）', () => {
  assert.ok(SRC.includes('function getInviteWatchlistLimit'), '必须有唯一 helper')
  // helper 定义 + render + sortValue 至少 3 处引用
  const calls = SRC.split('getInviteWatchlistLimit(').length - 1
  assert.ok(calls >= 3, `helper 应被定义并复用（当前引用 ${calls} 处）`)
  assert.ok(
    !SRC.includes('sortValue: (row) => row.monitor_limit ?? 0'),
    'sortValue 不得再直接用 row.monitor_limit',
  )
})

test('D2 生成邀请码默认 watchlist=5 / days=30', () => {
  assert.ok(SRC.includes('INVITE_DEFAULT_WATCHLIST_LIMIT = 5'), '默认自选上限 5')
  assert.ok(SRC.includes('INVITE_DEFAULT_GRANT_DAYS = 30'), '默认有效期 30')
  assert.ok(
    SRC.includes('setCapWatchlistLimit(INVITE_DEFAULT_WATCHLIST_LIMIT)'),
    '打开弹窗重置为 5',
  )
  assert.ok(
    SRC.includes('setGenerateGrantDays(INVITE_DEFAULT_GRANT_DAYS)'),
    '打开弹窗重置为 30',
  )
})

test('D3 数字输入允许中间空值，提交时才校验', () => {
  assert.ok(SRC.includes('function parseIntegerInput'), '必须有 parseIntegerInput helper')
  assert.ok(
    SRC.includes("setCapWatchlistLimit(raw === '' ? '' : Number(raw))"),
    'watchlist onChange 只保存输入（允许空值）',
  )
  assert.ok(
    SRC.includes("setGenerateGrantDays(raw === '' ? '' : Number(raw))"),
    'grant_days onChange 只保存输入（允许空值）',
  )
})

test('D4 管理员直接 grant capability 的默认值未被改动', () => {
  assert.ok(
    SRC.includes(
      'const [capGrantWatchlistLimit, setCapGrantWatchlistLimit] = useState(OBSERVE_PLAN_DEFAULT)',
    ),
    '直接 grant capability 默认仍为套餐默认，不随邀请码弹窗改动',
  )
})
