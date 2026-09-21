// [RouteStructure] - 描述: 路由层级契约测试（基于纯结构 ROUTE_STRUCTURE 断言）
// 用法：node --experimental-strip-types --test src/navigation/__tests__/routeStructure.test.ts
//
// 覆盖（PRD V1.0 阶段一路由与壳层）：
//   1. Capture 路由位于 ProtectedLayout 之外（无 protected/capability/admin 守卫祖先）
//   2. /market /review /stock/:symbol 经过 UserAppShell + CapabilityRoute
//   3. /messages /settings 经过 UserAppShell 但不经过 CapabilityRoute
//   4. /admin/* 经过 AdminRoute + AdminAppShell
//   5. /overview /watchlist /screener /replay 为兼容重定向
//   6. 兜底重定向到 /market
//   7. Capture 路由不渲染任何壳层（user/admin 均不在祖先链）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import {
  ROUTE_STRUCTURE,
  findRouteNode,
  hasGuardInChain,
  hasShellInChain,
} from '../routeStructure.ts'

test('Capture 路由位于 ProtectedLayout 之外（无 protected 守卫祖先）', () => {
  const result = findRouteNode(ROUTE_STRUCTURE, '/capture/stock/:symbol')
  assert.ok(result, 'Capture 路由必须存在')
  assert.equal(result.node.guard, 'capture')
  // 祖先链中不应有 protected/capability/admin 守卫
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'protected'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'capability'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'admin'))
})

test('Capture 路由不渲染任何壳层（user/admin 均不在祖先链）', () => {
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'user'))
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'admin'))
})

test('/market 经过 UserAppShell + CapabilityRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/market', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/market', 'capability'))
})

test('/review 经过 UserAppShell + CapabilityRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/review', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/review', 'capability'))
})

test('/screener 为兼容重定向（不再为独立页面）', () => {
  const screener = findRouteNode(ROUTE_STRUCTURE, '/screener')
  assert.ok(screener, '/screener 重定向路由必须存在')
  assert.equal(screener.node.guard, 'redirect')
  assert.equal(screener.node.redirectTo, '/market')
  // /screener 不再经过用户壳层或 capability 守卫
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/screener', 'user'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/screener', 'capability'))
})

test('/stock/:symbol 经过 UserAppShell + CapabilityRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/stock/:symbol', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/stock/:symbol', 'capability'))
})

test('/messages 经过 UserAppShell 但不经过 CapabilityRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/messages', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/messages', 'protected'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/messages', 'capability'))
})

test('/settings 经过 UserAppShell 但不经过 CapabilityRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/settings', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/settings', 'protected'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/settings', 'capability'))
})

test('/admin/* 经过 AdminRoute + AdminAppShell', () => {
  // [管理后台优化 PRD §6] 含目标一级路由 + 旧路由（旧路由为 redirect 守卫，但仍在 admin 祖先链内）
  const adminPaths = [
    '/admin',
    '/admin/overview',
    '/admin/data-production',
    '/admin/tasks',
    '/admin/users',
    '/admin/diagnostics',
    '/admin/jobs',
    '/admin/after-close',
    '/admin/stocks',
    '/admin/stocks/:symbol/debug',
    '/admin/visitors',
  ]
  for (const p of adminPaths) {
    assert.ok(hasGuardInChain(ROUTE_STRUCTURE, p, 'admin'), `${p} 应经过 AdminRoute`)
    assert.ok(hasShellInChain(ROUTE_STRUCTURE, p, 'admin'), `${p} 应经过 AdminAppShell`)
    assert.ok(!hasShellInChain(ROUTE_STRUCTURE, p, 'user'), `${p} 不应经过 UserAppShell`)
  }
})

// [管理后台优化 PRD §6] 目标一级路由必须是非重定向的 admin 页面
test('目标一级路由均为 admin 页面（非重定向）', () => {
  const canonical = ['/admin/overview', '/admin/data-production', '/admin/tasks', '/admin/users', '/admin/diagnostics']
  for (const p of canonical) {
    const node = findRouteNode(ROUTE_STRUCTURE, p)
    assert.ok(node, `${p} 路由应存在`)
    assert.notEqual(node!.node.guard, 'redirect', `${p} 应为 admin 页面而非重定向`)
  }
})

// [管理后台优化 PRD §6.2] 旧管理路由 → 新路由兼容重定向
test('旧管理路由兼容重定向到新一级路由', () => {
  const redirects: Record<string, string> = {
    '/admin': '/admin/overview',
    '/admin/jobs': '/admin/tasks',
    '/admin/after-close': '/admin/data-production?tab=after-close',
    '/admin/stocks': '/admin/diagnostics?tab=stock',
    '/admin/visitors': '/admin/diagnostics?tab=visitors',
    '/admin/strategies': '/admin/data-production',
  }
  for (const [from, to] of Object.entries(redirects)) {
    const node = findRouteNode(ROUTE_STRUCTURE, from)
    assert.ok(node, `${from} 路由应存在`)
    assert.strictEqual(node!.node.guard, 'redirect', `${from} 应为 redirect 守卫`)
    assert.strictEqual(node!.node.redirectTo, to, `${from} 应重定向到 ${to}`)
  }
})

test('/admin/stocks/:symbol/debug 调试路由位于管理员壳层（不暴露给普通用户）', () => {
  // [Phase4] 路由从 /admin/stock-debug/:symbol 改为 /admin/stocks/:symbol/debug（前后端统一 symbol）
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/admin/stocks', 'admin'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/admin/stocks/:symbol/debug', 'admin'))
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/admin/stocks', 'admin'))
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/admin/stocks/:symbol/debug', 'admin'))
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/admin/stocks', 'user'))
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/admin/stocks/:symbol/debug', 'user'))
})

test('/overview 和 /watchlist 为兼容重定向', () => {
  const overview = findRouteNode(ROUTE_STRUCTURE, '/overview')
  assert.ok(overview)
  assert.equal(overview.node.guard, 'redirect')
  assert.equal(overview.node.redirectTo, '/market')

  const watchlist = findRouteNode(ROUTE_STRUCTURE, '/watchlist')
  assert.ok(watchlist)
  assert.equal(watchlist.node.guard, 'redirect')
  assert.equal(watchlist.node.redirectTo, '/market?scope=watchlist')
})

test('/replay 为兼容重定向到 /review（复盘占位路由已由正式工作台替代）', () => {
  const replay = findRouteNode(ROUTE_STRUCTURE, '/replay')
  assert.ok(replay, '/replay 重定向路由必须存在')
  assert.equal(replay.node.guard, 'redirect')
  assert.equal(replay.node.redirectTo, '/review')
  // /replay 不再经过用户壳层或 capability 守卫
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/replay', 'user'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/replay', 'capability'))
})

test('兜底路由重定向到 /market', () => {
  const fallback = findRouteNode(ROUTE_STRUCTURE, '*')
  assert.ok(fallback)
  assert.equal(fallback.node.guard, 'redirect')
  assert.equal(fallback.node.redirectTo, '/market')
})

// PRD V1.1 纠偏测试：/stock/:symbol 是唯一个股详情 K线入口，不得重定向到 /market
test('/stock/:symbol 是详情页（非重定向），不是 /market 的别名', () => {
  const stock = findRouteNode(ROUTE_STRUCTURE, '/stock/:symbol')
  assert.ok(stock, '/stock/:symbol 路由必须存在')
  assert.notEqual(stock.node.guard, 'redirect', '/stock/:symbol 不得是重定向')
  assert.equal(stock.node.shell, 'user')
  assert.ok(!stock.node.redirectTo, '/stock/:symbol 不得有 redirectTo')
})

test('/market 不是详情页（guard=capability，shell=user，无 redirectTo）', () => {
  const market = findRouteNode(ROUTE_STRUCTURE, '/market')
  assert.ok(market)
  assert.notEqual(market.node.guard, 'redirect')
  assert.equal(market.node.shell, 'user')
  assert.ok(!market.node.redirectTo)
})

// /stock/:symbol 和 /market 是两个独立路由，不能互为别名
test('/stock/:symbol 与 /market 是两个独立路由节点', () => {
  const stock = findRouteNode(ROUTE_STRUCTURE, '/stock/:symbol')
  const market = findRouteNode(ROUTE_STRUCTURE, '/market')
  assert.ok(stock && market)
  assert.notEqual(stock.node.path, market.node.path)
})

// [REVIEW-V2-R1] canonical 复盘路由：capability 守卫（旧 /review/dashboard/* 已改 redirect）
for (const p of ['/review', '/review/industry', '/review/concept', '/review/compare']) {
  test(`复盘路由存在且走 capability 守卫：${p}`, () => {
    const node = findRouteNode(ROUTE_STRUCTURE, p)
    assert.ok(node, `${p} 路由必须存在`)
    assert.equal(node!.node.guard, 'capability')
    assert.ok(hasShellInChain(ROUTE_STRUCTURE, p, 'user'))
  })
}

// 旧 Market Dashboard 路由统一 redirect 到 canonical 复盘路由
test('旧 Market Dashboard 路由为兼容重定向', () => {
  const redirects: Record<string, string> = {
    '/review/dashboard/market': '/review',
    '/review/dashboard/industry': '/review/industry',
    '/review/dashboard/concept': '/review/concept',
  }
  for (const [from, to] of Object.entries(redirects)) {
    const node = findRouteNode(ROUTE_STRUCTURE, from)
    assert.ok(node, `${from} 路由应存在`)
    assert.equal(node!.node.guard, 'redirect')
    assert.equal(node!.node.redirectTo, to)
    assert.ok(!hasShellInChain(ROUTE_STRUCTURE, from, 'user'))
  }
})

// 旧 Board Analysis 已退役：/boards* 仍存在（迁移提示页），不再依赖旧后端
test('/boards 与 /boards/:boardId 仍在用户壳层 + capability 守卫内', () => {
  for (const p of ['/boards', '/boards/:boardId']) {
    assert.ok(hasShellInChain(ROUTE_STRUCTURE, p, 'user'))
    assert.ok(hasGuardInChain(ROUTE_STRUCTURE, p, 'capability'))
  }
})

// [REVIEW-V2-R1] /review（market_data）与 /auction（research_replay）不再共用同一守卫节点
test('/review 与 /auction 使用不同 capability 守卫节点', () => {
  const review = findRouteNode(ROUTE_STRUCTURE, '/review')
  const auction = findRouteNode(ROUTE_STRUCTURE, '/auction')
  assert.ok(review && auction)
  const reviewGuard = review.ancestors.find((a) => a.guard === 'capability')
  const auctionGuard = auction.ancestors.find((a) => a.guard === 'capability')
  assert.ok(reviewGuard && auctionGuard)
  assert.notEqual(reviewGuard, auctionGuard, '/review 与 /auction 必须挂在不同 capability 守卫节点下')
})
