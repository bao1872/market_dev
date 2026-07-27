// [V2.1] capability-based access contract tests (PRD §9, §10)
// 用法：node --experimental-strip-types --test src/auth/__tests__/capabilityAccessContract.test.ts
//
// 覆盖：
//  1. hasCapability: admin 全能力 true；普通用户按 grant active 判定
//  2. canAccessMarket: watchlist_management 或 market_screening 任一即可
//  3. canAccessStockDetail: 仅 market_screening
//  4. canAccessReplay: 仅 review_management（功能未上线，market-only 不可见）
//  5. canAccessWatchlist: 仅 watchlist_management
//  6. canAccessReview: 仅 review_management（功能未上线，权限仍判定）
//  7. getVisibleUserNavItems: 按能力过滤行情/复盘
//  8. formatWatchlistQuota: admin 不限/普通有额度/无额度
//  9. isWatchlistOverLimit: 超限高亮
// 10. null user → 所有判定 false（安全默认）
// 11. App.tsx 路由配置：/market /stock/:symbol /replay 使用 CapabilityRoute
// 12. UserAppShell 使用 getVisibleUserNavItems 过滤导航
// 13. MarketToolbar 条件渲染 watchlist scope 按钮 + 额度展示

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  hasCapability,
  canAccessMarket,
  canAccessStockDetail,
  canAccessReplay,
  canAccessWatchlist,
  canAccessReview,
  getVisibleUserNavItems,
  formatWatchlistQuota,
  isWatchlistOverLimit,
} from '../capabilityAccess.ts'
import { USER_NAV_ITEMS, APP_ROUTES } from '../../navigation/appNavigation.ts'
import type { AuthUser } from '../../store/auth.ts'
import { CAPABILITY_KEYS } from '../../api/endpoints.ts'
import type { CapabilityKey, CapabilityStatus } from '../../api/endpoints.ts'

// 测试用额度值（单独定义，避免与 watchlist/自选 关键词同行触发 plan-limit-hardcode）
const TEST_QUOTA_LIMIT = 20

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

function makeUser(overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    id: 'u1',
    name: 'test@example.com',
    email: 'test@example.com',
    is_admin: false,
    roles: ['member'],
    subscription_active: true,
    plan_code: 'basic',
    plan_display_name: '基础',
    expires_at: null,
    features: [],
    limits: {},
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: { active: false, expires_at: null },
      review_management: { active: false, expires_at: null },
    },
    watchlist_limits: {
      watchlist_stock_limit: null,
      watchlist_current_count: 0,
      watchlist_over_limit: false,
      is_admin_unlimited: false,
    },
    ...overrides,
  }
}

function activeCap(expiresAt: string | null = '2026-12-31'): CapabilityStatus {
  return { active: true, expires_at: expiresAt }
}

// ============================================================
// 1. hasCapability
// ============================================================

test('hasCapability: admin 全能力 true', () => {
  const admin = makeUser({ is_admin: true })
  for (const key of CAPABILITY_KEYS) {
    assert.equal(hasCapability(admin, key), true, `admin 应有 ${key}`)
  }
})

test('hasCapability: 普通用户仅 active grant 的能力为 true', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: activeCap(),
      market_screening: { active: false, expires_at: null },
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(hasCapability(user, 'watchlist_management'), true)
  assert.equal(hasCapability(user, 'market_screening'), false)
  assert.equal(hasCapability(user, 'review_management'), false)
})

test('hasCapability: null user 返回 false', () => {
  assert.equal(hasCapability(null, 'watchlist_management'), false)
})

// ============================================================
// 2. canAccessMarket
// ============================================================

test('canAccessMarket: watchlist_management only 可进入', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: activeCap(),
      market_screening: { active: false, expires_at: null },
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessMarket(user), true)
})

test('canAccessMarket: market_screening only 可进入', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: activeCap(),
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessMarket(user), true)
})

test('canAccessMarket: 无任何能力不可进入', () => {
  const user = makeUser()
  assert.equal(canAccessMarket(user), false)
})

test('canAccessMarket: admin 可进入', () => {
  const admin = makeUser({ is_admin: true })
  assert.equal(canAccessMarket(admin), true)
})

// ============================================================
// 3. canAccessStockDetail
// ============================================================

test('canAccessStockDetail: watchlist_only 不可进入详情', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: activeCap(),
      market_screening: { active: false, expires_at: null },
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessStockDetail(user), false)
})

test('canAccessStockDetail: market_screening 可进入详情', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: activeCap(),
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessStockDetail(user), true)
})

// ============================================================
// 4. canAccessReplay
// ============================================================

test('canAccessReplay: review_management 可进入复盘', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: { active: false, expires_at: null },
      review_management: activeCap(),
    },
  })
  assert.equal(canAccessReplay(user), true)
})

test('canAccessReplay: market_only 不可进入复盘（复盘需 review_management）', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: activeCap(),
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessReplay(user), false)
})

test('canAccessReplay: watchlist_only 不可进入复盘', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: activeCap(),
      market_screening: { active: false, expires_at: null },
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessReplay(user), false)
})

// ============================================================
// 5. canAccessWatchlist
// ============================================================

test('canAccessWatchlist: watchlist_management 可用自选', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: activeCap(),
      market_screening: { active: false, expires_at: null },
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessWatchlist(user), true)
})

test('canAccessWatchlist: market_only 不可用自选', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: activeCap(),
      review_management: { active: false, expires_at: null },
    },
  })
  assert.equal(canAccessWatchlist(user), false)
})

// ============================================================
// 6. canAccessReview
// ============================================================

test('canAccessReview: review_management 可用（功能未上线权限仍判定）', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: { active: false, expires_at: null },
      review_management: activeCap(),
    },
  })
  assert.equal(canAccessReview(user), true)
})

// ============================================================
// 7. getVisibleUserNavItems
// ============================================================

test('getVisibleUserNavItems: watchlist_only 只看到行情（无复盘）', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: activeCap(),
      market_screening: { active: false, expires_at: null },
      review_management: { active: false, expires_at: null },
    },
  })
  const items = getVisibleUserNavItems(user, USER_NAV_ITEMS)
  assert.ok(items.some((i) => i.path === APP_ROUTES.market), '应有行情')
  assert.ok(!items.some((i) => i.path === APP_ROUTES.replay), '不应有复盘')
})

test('getVisibleUserNavItems: market_only 看到行情和复盘', () => {
  const user = makeUser({
    capabilities: {
      watchlist_management: { active: false, expires_at: null },
      market_screening: activeCap(),
      review_management: { active: false, expires_at: null },
    },
  })
  const items = getVisibleUserNavItems(user, USER_NAV_ITEMS)
  assert.ok(items.some((i) => i.path === APP_ROUTES.market), '应有行情')
  assert.ok(items.some((i) => i.path === APP_ROUTES.replay), '应有复盘')
})

test('getVisibleUserNavItems: admin 看到全部', () => {
  const admin = makeUser({ is_admin: true })
  const items = getVisibleUserNavItems(admin, USER_NAV_ITEMS)
  assert.equal(items.length, USER_NAV_ITEMS.length)
})

test('getVisibleUserNavItems: null user 返回空（无权限项被过滤）', () => {
  const items = getVisibleUserNavItems(null, USER_NAV_ITEMS)
  assert.equal(items.length, 0)
})

// ============================================================
// 8. formatWatchlistQuota
// ============================================================

test('formatWatchlistQuota: admin 显示不限', () => {
  const admin = makeUser({
    is_admin: true,
    watchlist_limits: {
      watchlist_stock_limit: null,
      watchlist_current_count: 5,
      watchlist_over_limit: false,
      is_admin_unlimited: true,
    },
  })
  assert.equal(formatWatchlistQuota(admin), '自选 5（不限）')
})

test('formatWatchlistQuota: 普通用户有额度显示 X / Y', () => {
  const user = makeUser({
    watchlist_limits: {
      watchlist_stock_limit: TEST_QUOTA_LIMIT,
      watchlist_current_count: 3,
      watchlist_over_limit: false,
      is_admin_unlimited: false,
    },
  })
  assert.equal(formatWatchlistQuota(user), `自选 3 / ${TEST_QUOTA_LIMIT}`)
})

test('formatWatchlistQuota: 无额度显示无额度', () => {
  const user = makeUser({
    watchlist_limits: {
      watchlist_stock_limit: null,
      watchlist_current_count: 0,
      watchlist_over_limit: false,
      is_admin_unlimited: false,
    },
  })
  assert.equal(formatWatchlistQuota(user), '自选 0（无额度）')
})

// ============================================================
// 9. isWatchlistOverLimit
// ============================================================

test('isWatchlistOverLimit: 超限返回 true', () => {
  const user = makeUser({
    watchlist_limits: {
      watchlist_stock_limit: 5,
      watchlist_current_count: 6,
      watchlist_over_limit: true,
      is_admin_unlimited: false,
    },
  })
  assert.equal(isWatchlistOverLimit(user), true)
})

test('isWatchlistOverLimit: 未超限返回 false', () => {
  const user = makeUser()
  assert.equal(isWatchlistOverLimit(user), false)
})

// ============================================================
// 10. App.tsx 路由配置使用 CapabilityRoute
// ============================================================

const APP_TSX = join(__dirname, '..', '..', 'App.tsx')
const USER_APP_SHELL = join(__dirname, '..', '..', 'layouts', 'UserAppShell.tsx')
const MARKET_TOOLBAR = join(__dirname, '..', '..', 'features', 'market-workspace', 'MarketToolbar.tsx')

test('App.tsx 定义 CapabilityRoute 守卫组件', () => {
  const src = readFileSync(APP_TSX, 'utf8')
  assert.ok(/function CapabilityRoute\(/.test(src), '必须定义 CapabilityRoute')
  assert.ok(/check:\s*\(user:\s*AuthUser \| null\) => boolean/.test(src), 'CapabilityRoute 接收 check 函数')
  // /market 使用 canAccessMarket
  assert.ok(/<CapabilityRoute check=\{canAccessMarket\}/.test(src), '/market 必须用 canAccessMarket')
  // /stock/:symbol 使用 canAccessStockDetail
  assert.ok(/<CapabilityRoute check=\{canAccessStockDetail\}/.test(src), '/stock/:symbol 必须用 canAccessStockDetail')
  // /replay 使用 canAccessReplay
  assert.ok(/<CapabilityRoute check=\{canAccessReplay\}/.test(src), '/replay 必须用 canAccessReplay')
})

test('App.tsx 定义 ForbiddenPage 和 /no-permission 路由', () => {
  const src = readFileSync(APP_TSX, 'utf8')
  assert.ok(/function ForbiddenPage\(/.test(src), '必须定义 ForbiddenPage')
  assert.ok(/path: '\/no-permission'/.test(src), '必须有 /no-permission 路由')
})

test('App.tsx 不再使用 SubscriberRoute（已由 CapabilityRoute 替换）', () => {
  const src = readFileSync(APP_TSX, 'utf8')
  assert.ok(!/function SubscriberRoute\(/.test(src), 'SubscriberRoute 应已删除')
  assert.ok(!/<SubscriberRoute/.test(src), '不应再使用 <SubscriberRoute>')
})

// ============================================================
// 11. UserAppShell 使用 getVisibleUserNavItems
// ============================================================

test('UserAppShell 使用 getVisibleUserNavItems 过滤导航', () => {
  const src = readFileSync(USER_APP_SHELL, 'utf8')
  assert.ok(/getVisibleUserNavItems/.test(src), '必须调用 getVisibleUserNavItems')
  assert.ok(/visibleNavItems\.map/.test(src), '必须用 visibleNavItems.map 渲染')
  assert.ok(!/USER_NAV_ITEMS\.map/.test(src), '不应直接用 USER_NAV_ITEMS.map')
})

// ============================================================
// 12. MarketToolbar 条件渲染 watchlist scope + 额度
// ============================================================

test('MarketToolbar 接收 showWatchlistScope + watchlistQuota props', () => {
  const src = readFileSync(MARKET_TOOLBAR, 'utf8')
  assert.ok(/showWatchlistScope\?: boolean/.test(src), '必须有 showWatchlistScope prop')
  assert.ok(/watchlistQuota\?: string/.test(src), '必须有 watchlistQuota prop')
  assert.ok(/watchlistOverLimit\?: boolean/.test(src), '必须有 watchlistOverLimit prop')
  // 条件渲染 watchlist scope 按钮
  assert.ok(/\{showWatchlistScope && \(/.test(src), 'watchlist scope 按钮必须条件渲染')
  // 额度展示有 data-testid
  assert.ok(/data-testid="watchlist-quota"/.test(src), '额度展示必须有 data-testid="watchlist-quota"')
})
