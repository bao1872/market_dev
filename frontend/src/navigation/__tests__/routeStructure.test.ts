// [RouteStructure] - 描述: 路由层级契约测试（基于纯结构 ROUTE_STRUCTURE 断言）
// 用法：node --experimental-strip-types --test src/navigation/__tests__/routeStructure.test.ts
//
// 覆盖（PRD V1.0 阶段一路由与壳层）：
//   1. Capture 路由位于 ProtectedLayout 之外（无 protected/subscriber/admin 守卫祖先）
//   2. /market /replay /stock/:symbol 经过 UserAppShell + SubscriberRoute
//   3. /messages /settings 经过 UserAppShell 但不经过 SubscriberRoute
//   4. /admin/* 经过 AdminRoute + AdminAppShell
//   5. /overview /watchlist /screener 为兼容重定向
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
  // 祖先链中不应有 protected/subscriber/admin 守卫
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'protected'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'subscriber'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'admin'))
})

test('Capture 路由不渲染任何壳层（user/admin 均不在祖先链）', () => {
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'user'))
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/capture/stock/:symbol', 'admin'))
})

test('/market 经过 UserAppShell + SubscriberRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/market', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/market', 'subscriber'))
})

test('/replay 经过 UserAppShell + SubscriberRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/replay', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/replay', 'subscriber'))
})

test('/screener 为兼容重定向（不再为独立页面）', () => {
  const screener = findRouteNode(ROUTE_STRUCTURE, '/screener')
  assert.ok(screener, '/screener 重定向路由必须存在')
  assert.equal(screener.node.guard, 'redirect')
  assert.equal(screener.node.redirectTo, '/market')
  // /screener 不再经过用户壳层或订阅守卫
  assert.ok(!hasShellInChain(ROUTE_STRUCTURE, '/screener', 'user'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/screener', 'subscriber'))
})

test('/stock/:symbol 经过 UserAppShell + SubscriberRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/stock/:symbol', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/stock/:symbol', 'subscriber'))
})

test('/messages 经过 UserAppShell 但不经过 SubscriberRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/messages', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/messages', 'protected'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/messages', 'subscriber'))
})

test('/settings 经过 UserAppShell 但不经过 SubscriberRoute', () => {
  assert.ok(hasShellInChain(ROUTE_STRUCTURE, '/settings', 'user'))
  assert.ok(hasGuardInChain(ROUTE_STRUCTURE, '/settings', 'protected'))
  assert.ok(!hasGuardInChain(ROUTE_STRUCTURE, '/settings', 'subscriber'))
})

test('/admin/* 经过 AdminRoute + AdminAppShell', () => {
  const adminPaths = ['/admin', '/admin/overview', '/admin/users', '/admin/beta-applications', '/admin/strategies', '/admin/jobs', '/admin/after-close', '/admin/stocks', '/admin/stocks/:symbol/debug']
  for (const p of adminPaths) {
    assert.ok(hasGuardInChain(ROUTE_STRUCTURE, p, 'admin'), `${p} 应经过 AdminRoute`)
    assert.ok(hasShellInChain(ROUTE_STRUCTURE, p, 'admin'), `${p} 应经过 AdminAppShell`)
    assert.ok(!hasShellInChain(ROUTE_STRUCTURE, p, 'user'), `${p} 不应经过 UserAppShell`)
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

test('/market 不是详情页（guard=subscriber，shell=user，无 redirectTo）', () => {
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
