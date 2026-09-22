// [CHANGE-20260802-002 / REVIEW-V2-R1] research_replay = 竞价分析 前端权限合同测试
// 用法：node --experimental-strip-types --test src/navigation/__tests__/replayAuctionEntitlement.test.ts
//
// 覆盖：
//   1. research_replay 用户可见「竞价」；「复盘」由独立 market_review 守卫
//   2. 无 research_replay 用户隐藏「竞价」
//   3. 竞价三级路由均受 capability 守卫保护
//   4. /review 与 /auction 不再共用同一 capability 守卫节点
//   5. 邀请码创建/列表显示「竞价分析」
//   6. 不存在独立 auction capability（market_review 为独立复盘 capability，非 auction）

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
  AUCTION_CAPABILITY,
  capabilityLabel,
  formatCapabilityGrants,
  hasCapability,
} from '../capabilities.ts'
import { ROUTE_STRUCTURE, findRouteNode } from '../routeStructure.ts'

const ACTIVE = { active: true }
const EXPIRED = { active: false }

// ============================================================
// 1~2. 导航可见性
// ============================================================

test('research_replay 用户可见竞价；复盘由独立 market_review 守卫', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { research_replay: ACTIVE, market_review: ACTIVE },
    false,
  ).map((i) => i.path)
  assert.ok(paths.includes(APP_ROUTES.auction), '竞价应可见')
  assert.ok(paths.includes(APP_ROUTES.review), '复盘（market_review）应可见')
})

test('无 research_replay 用户隐藏竞价；复盘由独立 market_review 决定', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { market_data: ACTIVE, self_selection: ACTIVE, market_review: ACTIVE },
    false,
  ).map((i) => i.path)
  assert.ok(!paths.includes(APP_ROUTES.auction), '竞价应隐藏')
  assert.ok(paths.includes(APP_ROUTES.review), '复盘由独立 market_review 决定，应可见')
  assert.ok(paths.includes(APP_ROUTES.market))
  assert.ok(paths.includes(WATCHLIST_NAV_PATH))
})

test('research_replay 过期时竞价隐藏；复盘由独立 market_review 不受影响', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { research_replay: EXPIRED, market_review: ACTIVE },
    false,
  ).map((i) => i.path)
  assert.ok(!paths.includes(APP_ROUTES.auction))
  assert.ok(paths.includes(APP_ROUTES.review))
})

test('无 market_data / market_review 时隐藏行情与复盘，不影响竞价', () => {
  const paths = filterNavItemsByCapability(
    USER_NAV_ITEMS,
    { research_replay: ACTIVE, self_selection: ACTIVE },
    false,
  ).map((i) => i.path)
  assert.ok(!paths.includes(APP_ROUTES.market), '行情应隐藏')
  assert.ok(!paths.includes(APP_ROUTES.review), '复盘应隐藏')
  assert.ok(paths.includes(APP_ROUTES.auction))
})

test('admin 无 capability 行时仍可见全部一级导航（豁免行为不回归）', () => {
  const paths = filterNavItemsByCapability(USER_NAV_ITEMS, {}, true).map((i) => i.path)
  assert.deepStrictEqual(paths, USER_NAV_ITEMS.map((i) => i.path))
})

test('竞价导航项声明 research_replay；复盘导航项声明独立 market_review', () => {
  const review = USER_NAV_ITEMS.find((i) => i.path === APP_ROUTES.review)
  const auction = USER_NAV_ITEMS.find((i) => i.path === APP_ROUTES.auction)
  assert.equal(review?.requiredCapability, 'market_review')
  assert.equal(auction?.requiredCapability, AUCTION_CAPABILITY)
  assert.notEqual(review?.requiredCapability, auction?.requiredCapability)
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

test('/review 与 /auction 使用不同 capability 守卫节点（不再共用权益）', () => {
  const review = findRouteNode(ROUTE_STRUCTURE, '/review')
  const auction = findRouteNode(ROUTE_STRUCTURE, '/auction')
  assert.ok(review && auction)
  const reviewGuard = review.ancestors.find((a) => a.guard === 'capability')
  const auctionGuard = auction.ancestors.find((a) => a.guard === 'capability')
  assert.ok(reviewGuard && auctionGuard)
  assert.notEqual(reviewGuard, auctionGuard, '/review 与 /auction 必须挂在不同 capability 守卫节点下')
})

// ============================================================
// 5~6. 邀请码权限展示
// ============================================================

test('research_replay 中文标签为「竞价分析」', () => {
  assert.equal(CAPABILITY_LABELS.research_replay, '竞价分析')
  assert.equal(capabilityLabel('research_replay'), '竞价分析')
})

test('邀请码创建结果显示实际权限组合含「竞价分析」', () => {
  const text = formatCapabilityGrants([
    { capability: 'self_selection', days: 1 },
    { capability: 'market_data', days: 1 },
    { capability: 'research_replay', days: 1 },
  ])
  assert.equal(text, '自选管理 · 行情数据 · 竞价分析')
})

test('邀请码列表按固定顺序展示，后端顺序变化不影响结果', () => {
  const text = formatCapabilityGrants([
    { capability: 'research_replay', days: 1 },
    { capability: 'self_selection', days: 1 },
  ])
  assert.equal(text, '自选管理 · 竞价分析')
})

test('无对应权限时不显示该标签', () => {
  const text = formatCapabilityGrants([{ capability: 'market_data', days: 1 }])
  assert.equal(text, '行情数据')
  assert.ok(!text.includes('竞价分析'))
  assert.ok(!text.includes('自选管理'))
})

test('旧模式邀请码（capabilities 为 null/空）返回空串由调用方兜底', () => {
  assert.equal(formatCapabilityGrants(null), '')
  assert.equal(formatCapabilityGrants(undefined), '')
  assert.equal(formatCapabilityGrants([]), '')
})

test('未知 capability 机器值原样展示，不静默吞掉后端新增值', () => {
  const text = formatCapabilityGrants([
    { capability: 'research_replay', days: 1 },
    { capability: 'future_cap', days: 1 },
  ])
  assert.equal(text, '竞价分析 · future_cap')
})

// ============================================================
// 7. 不存在独立 auction capability
// ============================================================

test('不存在独立 auction capability', () => {
  assert.deepStrictEqual(CAPABILITY_KEYS, [
    'self_selection',
    'market_data',
    'market_review',
    'research_replay',
  ])
  assert.ok(!(CAPABILITY_KEYS as readonly string[]).includes('auction'))
  assert.ok(!Object.prototype.hasOwnProperty.call(CAPABILITY_LABELS, 'auction'))
})

test('拥有 research_replay 即拥有竞价访问权（无需第二个 capability）', () => {
  const caps = { research_replay: ACTIVE }
  assert.equal(hasCapability(caps, AUCTION_CAPABILITY, false), true)
  // 不存在 auction capability，查询它必然为 false，证明未引入第二道门槛
  assert.equal(hasCapability(caps, 'auction', false), false)
})
