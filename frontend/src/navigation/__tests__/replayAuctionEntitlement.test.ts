// [CHANGE-20260802-002] research_replay = 复盘与竞价 前端权限合同测试
// 用法：node --experimental-strip-types --test src/navigation/__tests__/replayAuctionEntitlement.test.ts
//
// 覆盖（前端 1~7 项）：
//   1. research_replay 用户同时看到复盘和竞价
//   2. 无 research_replay 用户两者同时隐藏
//   3. 三个竞价路由均受 capability 守卫保护
//   4. 直接访问竞价子路由无权限时被拦截（守卫祖先链存在）
//   5. 邀请码创建结果显示「复盘与竞价」
//   6. 邀请码列表显示实际 capabilities
//   7. 不存在独立 auction capability

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  APP_ROUTES,
  USER_NAV_ITEMS,
  WATCHLIST_NAV_PATH,
  filterNavItemsByCapability,
} from '../appNavigation.ts'
import {
  CAPABILITY_KEYS,
  CAPABILITY_LABELS,
  REPLAY_AND_AUCTION_CAPABILITY,
  capabilityLabel,
  formatCapabilityGrants,
  hasCapability,
} from '../capabilities.ts'
import { ROUTE_STRUCTURE, findRouteNode } from '../routeStructure.ts'

const ACTIVE = { active: true }
const EXPIRED = { active: false }

// ============================================================
// 1~2. 导航可见性：复盘与竞价同显同隐
// ============================================================

test('research_replay 用户同时看到复盘和竞价', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { research_replay: ACTIVE, self_selection: ACTIVE, market_data: ACTIVE },
    false,
  ).map((i) => i.path)
  assert.ok(paths.includes(APP_ROUTES.review), '复盘应可见')
  assert.ok(paths.includes(APP_ROUTES.auction), '竞价应可见')
})

test('无 research_replay 用户复盘和竞价同时隐藏', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { market_data: ACTIVE, self_selection: ACTIVE },
    false,
  ).map((i) => i.path)
  assert.ok(!paths.includes(APP_ROUTES.review), '复盘应隐藏')
  assert.ok(!paths.includes(APP_ROUTES.auction), '竞价应隐藏')
  // 行情与自选不受影响
  assert.ok(paths.includes(APP_ROUTES.market))
  assert.ok(paths.includes(WATCHLIST_NAV_PATH))
})

test('research_replay 过期时复盘和竞价同时隐藏', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { research_replay: EXPIRED },
    false,
  ).map((i) => i.path)
  assert.ok(!paths.includes(APP_ROUTES.review))
  assert.ok(!paths.includes(APP_ROUTES.auction))
})

test('无 self_selection 时仅隐藏自选，不影响复盘与竞价', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { research_replay: ACTIVE, market_data: ACTIVE },
    false,
  ).map((i) => i.path)
  assert.ok(!paths.includes(WATCHLIST_NAV_PATH), '自选应隐藏')
  assert.ok(paths.includes(APP_ROUTES.review))
  assert.ok(paths.includes(APP_ROUTES.auction))
})

test('admin 无 capability 行时仍可见全部一级导航（豁免行为不回归）', () => {
  const paths = filterNavItemsByCapability(USER_NAV_ITEMS, {}, true).map((i) => i.path)
  assert.deepStrictEqual(paths, USER_NAV_ITEMS.map((i) => i.path))
})

test('复盘与竞价导航项声明同一 capability', () => {
  const review = USER_NAV_ITEMS.find((i) => i.path === APP_ROUTES.review)
  const auction = USER_NAV_ITEMS.find((i) => i.path === APP_ROUTES.auction)
  assert.equal(review?.requiredCapability, REPLAY_AND_AUCTION_CAPABILITY)
  assert.equal(auction?.requiredCapability, REPLAY_AND_AUCTION_CAPABILITY)
  assert.equal(review?.requiredCapability, auction?.requiredCapability)
})

// ============================================================
// 3~4. 路由守卫
// ============================================================

const AUCTION_ROUTES = ['/auction', '/auction/board/:boardId', '/auction/stock/:symbol']

for (const path of AUCTION_ROUTES) {
  test(`竞价路由 ${path} 受 capability 守卫保护`, () => {
    const found = findRouteNode(ROUTE_STRUCTURE, path)
    assert.ok(found, `${path} 必须存在于路由结构中`)
    assert.equal(found.node.guard, 'capability')
    // 直接输入 URL 时由祖先守卫节点拦截（无权限 → /forbidden）
    const hasCapabilityAncestor = found.ancestors.some((a) => a.guard === 'capability')
    assert.ok(hasCapabilityAncestor, `${path} 必须位于 capability 守卫节点之下`)
  })
}

test('竞价路由与复盘路由共用同一守卫节点（不复制第二套守卫）', () => {
  const review = findRouteNode(ROUTE_STRUCTURE, '/review')
  const auction = findRouteNode(ROUTE_STRUCTURE, '/auction')
  assert.ok(review && auction)
  const reviewGuard = review.ancestors.find((a) => a.guard === 'capability')
  const auctionGuard = auction.ancestors.find((a) => a.guard === 'capability')
  assert.ok(reviewGuard)
  assert.equal(reviewGuard, auctionGuard, '复盘与竞价必须挂在同一 capability 守卫节点下')
})

// ============================================================
// 5~6. 邀请码权限展示
// ============================================================

test('research_replay 中文标签为「复盘与竞价」', () => {
  assert.equal(CAPABILITY_LABELS.research_replay, '复盘与竞价')
  assert.equal(capabilityLabel('research_replay'), '复盘与竞价')
})

test('邀请码创建结果显示实际权限组合含「复盘与竞价」', () => {
  const text = formatCapabilityGrants([
    { capability: 'self_selection', months: 1 },
    { capability: 'market_data', months: 1 },
    { capability: 'research_replay', months: 1 },
  ])
  assert.equal(text, '自选管理 · 行情数据 · 复盘与竞价')
})

test('邀请码列表按固定顺序展示，后端顺序变化不影响结果', () => {
  const text = formatCapabilityGrants([
    { capability: 'research_replay', months: 1 },
    { capability: 'self_selection', months: 1 },
  ])
  assert.equal(text, '自选管理 · 复盘与竞价')
})

test('无对应权限时不显示该标签', () => {
  const text = formatCapabilityGrants([{ capability: 'market_data', months: 1 }])
  assert.equal(text, '行情数据')
  assert.ok(!text.includes('复盘与竞价'))
  assert.ok(!text.includes('自选管理'))
})

test('旧模式邀请码（capabilities 为 null/空）返回空串由调用方兜底', () => {
  assert.equal(formatCapabilityGrants(null), '')
  assert.equal(formatCapabilityGrants(undefined), '')
  assert.equal(formatCapabilityGrants([]), '')
})

test('未知 capability 机器值原样展示，不静默吞掉后端新增值', () => {
  const text = formatCapabilityGrants([
    { capability: 'research_replay', months: 1 },
    { capability: 'future_cap', months: 1 },
  ])
  assert.equal(text, '复盘与竞价 · future_cap')
})

// ============================================================
// 7. 不存在独立 auction capability
// ============================================================

test('不存在独立 auction capability', () => {
  assert.deepStrictEqual(CAPABILITY_KEYS, [
    'self_selection',
    'market_data',
    'research_replay',
  ])
  assert.ok(!(CAPABILITY_KEYS as readonly string[]).includes('auction'))
  assert.ok(!Object.prototype.hasOwnProperty.call(CAPABILITY_LABELS, 'auction'))
})

test('拥有 research_replay 即拥有竞价访问权（无需第二个 capability）', () => {
  const caps = { research_replay: ACTIVE }
  assert.equal(hasCapability(caps, REPLAY_AND_AUCTION_CAPABILITY, false), true)
  // 不存在 auction capability，查询它必然为 false，证明未引入第二道门槛
  assert.equal(hasCapability(caps, 'auction', false), false)
})
