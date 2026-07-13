// [MessagesCounts] - 描述: 消息数量一致性与跳转契约测试（源码级）
// 用法：node --experimental-strip-types --test src/pages/__tests__/messagesCounts.test.ts
//
// 覆盖：
// 1. MessagesPage 使用 useUnreadCount 作为未读 SSOT
// 2. "全部"使用后端 total（非 items.length）
// 3. 页头显示"共 X 条 · 未读 Y 条"
// 4. selection/price/system/process 不显示误导数字
// 5. 单只股票消息跳转 /stock/:symbol?event_id=...&returnTo=/messages
// 6. selection_composite 跳转 /market
// 7. AccountMenu unread>0 时消息链接为 /messages?filter=unread
// 8. AccountMenu 消息项显示未读数

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const MESSAGES_PATH = join(__dirname, '..', 'MessagesPage.tsx')
const ACCOUNT_PATH = join(__dirname, '..', '..', 'components', 'AccountMenu.tsx')

function readSource(p: string): string {
  return readFileSync(p, 'utf-8')
}

// ===== 1. MessagesPage 使用 useUnreadCount =====
test('MessagesPage 使用 useUnreadCount 作为未读 SSOT', () => {
  const src = readSource(MESSAGES_PATH)
  assert.ok(
    src.includes('useUnreadCount'),
    'MessagesPage 必须使用 useUnreadCount hook',
  )
  assert.ok(
    src.includes('unreadQuery'),
    'MessagesPage 必须有 unreadQuery 变量',
  )
})

// ===== 2. "全部"使用后端 total =====
test('"全部"计数使用后端 total（非 items.length）', () => {
  const src = readSource(MESSAGES_PATH)
  assert.ok(
    src.includes('messagesQuery.data?.total'),
    '"全部"必须使用 messagesQuery.data?.total（SSOT）',
  )
  assert.ok(
    src.includes('totalCount'),
    '必须有 totalCount 变量',
  )
  // 不应使用 allMessages.length 作为 all 计数
  assert.ok(
    !src.includes('all: allMessages.length'),
    '不应使用 allMessages.length 作为"全部"计数（受 limit/筛选影响）',
  )
})

// ===== 3. 页头显示"共 X 条 · 未读 Y 条" =====
test('页头显示"共 X 条 · 未读 Y 条"', () => {
  const src = readSource(MESSAGES_PATH)
  assert.ok(
    src.includes('共') && src.includes('条') && src.includes('未读') && src.includes('totalCount') && src.includes('unreadCount'),
    '页头必须显示"共 {totalCount} 条 · 未读 {unreadCount} 条"',
  )
})

// ===== 4. selection/price/system/process 不显示误导数字 =====
test('selection/price/system/process 不显示误导数字（仅 all/unread 显示计数）', () => {
  const src = readSource(MESSAGES_PATH)
  assert.ok(
    src.includes("opt.value === 'all' || opt.value === 'unread'"),
    '仅 all 和 unread 筛选项显示计数',
  )
  assert.ok(
    src.includes('showCount'),
    '必须有 showCount 逻辑控制计数显示',
  )
})

// ===== 5. 单只股票消息跳转 /stock/:symbol?event_id=...&returnTo=/messages =====
test('单只股票消息跳转 /stock/:symbol?event_id=...&returnTo=/messages', () => {
  const src = readSource(MESSAGES_PATH)
  assert.ok(
    src.includes('/stock/'),
    '单只股票消息必须跳转到 /stock/:symbol',
  )
  assert.ok(
    src.includes("params.set('returnTo', '/messages')"),
    '跳转必须携带 returnTo=/messages',
  )
  assert.ok(
    src.includes("params.set('event_id'"),
    '跳转必须携带 event_id 参数',
  )
  // 不应跳转到旧 /market?symbol=
  assert.ok(
    !src.includes('/market?symbol='),
    '不应跳转到旧 /market?symbol= 路径（已改为 /stock/:symbol）',
  )
})

// ===== 6. selection_composite 跳转 /market =====
test('selection_composite 跳转 /market（非 /screener）', () => {
  const src = readSource(MESSAGES_PATH)
  assert.ok(
    src.includes("navigateTarget = '/market'"),
    'selection_composite 必须跳转到 /market',
  )
  // 不应跳转到旧 /screener
  assert.ok(
    !src.includes("navigateTarget = '/screener'"),
    'selection_composite 不应跳转到 /screener（已改为 /market）',
  )
})

// ===== 7. AccountMenu unread>0 时消息链接为 /messages?filter=unread =====
test('AccountMenu unread>0 时消息链接为 /messages?filter=unread', () => {
  const src = readSource(ACCOUNT_PATH)
  assert.ok(
    src.includes('?filter=unread'),
    'AccountMenu 在 unread>0 时消息链接必须为 /messages?filter=unread',
  )
  assert.ok(
    src.includes('isMessages') && src.includes('unread > 0'),
    'AccountMenu 必须有 isMessages 和 unread > 0 判断逻辑',
  )
})

// ===== 8. AccountMenu 消息项显示未读数 =====
test('AccountMenu 消息项显示未读数（itemBadge）', () => {
  const src = readSource(ACCOUNT_PATH)
  assert.ok(
    src.includes('itemBadge'),
    'AccountMenu 必须有 itemBadge 元素显示未读数',
  )
  assert.ok(
    src.includes('isMessages && unread > 0'),
    'itemBadge 仅在消息项且 unread>0 时显示',
  )
})
