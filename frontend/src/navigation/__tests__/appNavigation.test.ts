// [Navigation] - 描述: 路由/导航常量与兼容重定向契约测试
// 用法：node --experimental-strip-types --test src/navigation/__tests__/appNavigation.test.ts
//
// 覆盖（PRD V1.0 阶段一路由与壳层）：
//   1. 用户一级导航仅含 行情/复盘，不含消息/设置
//   2. 管理后台入口仅管理员可见（账户菜单按 isAdmin 过滤）
//   3. 旧路由兼容重定向：/overview → /market，/watchlist → /market?scope=watchlist，/screener → /market
//   4. 管理员路由集中于 /admin/*（ADMIN_NAV_ITEMS）
//   5. Capture 路由位于两套壳层之外（不在用户/管理员导航中）
//   6. 默认登录/兜底入口为 /market

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  APP_ROUTES,
  DEFAULT_ENTRY,
  USER_NAV_ITEMS,
  ADMIN_NAV_ITEMS,
  ACCOUNT_MENU_ITEMS,
  getAccountMenuItems,
  getAccountMenuItemsForVariant,
  LEGACY_REDIRECTS,
  legacyRedirectEntries,
} from '../appNavigation.ts'

test('用户一级导航仅含行情与复盘，不含消息/设置', () => {
  const paths = USER_NAV_ITEMS.map((i) => i.path)
  assert.deepStrictEqual(paths, ['/market', '/replay'])
  assert.ok(!paths.includes('/messages'))
  assert.ok(!paths.includes('/settings'))
  assert.ok(!paths.includes('/overview'))
  assert.ok(!paths.includes('/watchlist'))
  assert.ok(!paths.includes('/screener'))
})

test('管理后台入口仅管理员可见（账户菜单按 isAdmin 过滤）', () => {
  // 普通用户：无管理后台项
  const userItems = getAccountMenuItems(false).map((i) => i.path)
  assert.ok(!userItems.includes('/admin'))
  assert.ok(userItems.includes('/messages'))
  assert.ok(userItems.includes('/settings'))

  // 管理员：额外显示管理后台
  const adminItems = getAccountMenuItems(true).map((i) => i.path)
  assert.ok(adminItems.includes('/admin'))

  // 原始定义中管理后台项标记了 adminOnly
  const adminEntry = ACCOUNT_MENU_ITEMS.find((i) => i.path === '/admin')
  assert.ok(adminEntry?.adminOnly === true)
})

test('旧路由兼容重定向：/overview → /market，/watchlist → /market?scope=watchlist，/screener → /market', () => {
  assert.equal(LEGACY_REDIRECTS['/overview'], '/market')
  assert.equal(LEGACY_REDIRECTS['/watchlist'], '/market?scope=watchlist')
  assert.equal(LEGACY_REDIRECTS['/screener'], '/market')
  // [Phase4] 旧管理员调试路由 → 新路由（前后端统一使用 symbol）
  assert.equal(LEGACY_REDIRECTS['/admin/stock-debug'], '/admin/stocks')
  const entries = legacyRedirectEntries()
  assert.deepStrictEqual(entries, [
    { path: '/overview', to: '/market' },
    { path: '/watchlist', to: '/market?scope=watchlist' },
    { path: '/screener', to: '/market' },
    { path: '/admin/stock-debug', to: '/admin/stocks' },
  ])
})

test('管理员路由集中于 /admin/*（独立壳层承载）', () => {
  for (const item of ADMIN_NAV_ITEMS) {
    assert.ok(item.path.startsWith('/admin'), `管理员导航项应位于 /admin/*: ${item.path}`)
  }
})

test('Capture 路由位于两套壳层之外（不在用户/管理员导航中）', () => {
  assert.equal(APP_ROUTES.capture, '/capture/stock/:symbol')
  const userPaths = USER_NAV_ITEMS.map((i) => i.path)
  const adminPaths = ADMIN_NAV_ITEMS.map((i) => i.path)
  assert.ok(!userPaths.includes(APP_ROUTES.capture))
  assert.ok(!adminPaths.includes(APP_ROUTES.capture))
  // 账户菜单也不应包含 capture 路由
  const accountPaths = ACCOUNT_MENU_ITEMS.map((i) => i.path)
  assert.ok(!accountPaths.includes(APP_ROUTES.capture))
})

test('默认登录/兜底入口为 /market', () => {
  assert.equal(DEFAULT_ENTRY, '/market')
})

test('getAccountMenuItemsForVariant: variant=user + isAdmin=false → 只有消息+设置', () => {
  const items = getAccountMenuItemsForVariant(false, 'user')
  const paths = items.map((i) => i.path)
  assert.deepStrictEqual(paths, ['/messages', '/settings'])
  assert.ok(!paths.includes('/admin'))
  assert.ok(!paths.includes('/market'))
})

test('getAccountMenuItemsForVariant: variant=user + isAdmin=true → 仅消息+设置（不暴露管理后台）', () => {
  const items = getAccountMenuItemsForVariant(true, 'user')
  const paths = items.map((i) => i.path)
  assert.deepStrictEqual(paths, ['/messages', '/settings'])
  assert.ok(!paths.includes('/admin'), '用户壳层不应暴露管理后台入口')
})

test('getAccountMenuItemsForVariant: variant=admin → 消息+设置+返回行情（无管理后台）', () => {
  const items = getAccountMenuItemsForVariant(false, 'admin')
  const paths = items.map((i) => i.path)
  assert.deepStrictEqual(paths, ['/messages', '/settings', '/market'])
  assert.ok(!paths.includes('/admin'))
  // 最后一项标签应为"返回行情"
  assert.equal(items[items.length - 1].label, '返回行情')
})

test('getAccountMenuItemsForVariant: variant=admin + isAdmin=true 仍不显示管理后台', () => {
  const items = getAccountMenuItemsForVariant(true, 'admin')
  const paths = items.map((i) => i.path)
  assert.ok(!paths.includes('/admin'))
  assert.ok(paths.includes('/market'))
  assert.equal(items[items.length - 1].label, '返回行情')
})
