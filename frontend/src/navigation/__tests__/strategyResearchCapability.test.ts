import assert from 'node:assert/strict'
import { test } from 'node:test'
import { CAPABILITY_KEYS, computeDefaultRoute, hasCapability } from '../capabilities.ts'
import { USER_NAV_ITEMS, filterNavItemsByCapability } from '../appNavigation.ts'
import { ROUTE_STRUCTURE, findRouteNode, hasGuardInChain } from '../routeStructure.ts'

test('fifth independent strategy capability and default route', () => {
  assert.ok(CAPABILITY_KEYS.includes('strategy_research'))
  assert.equal(computeDefaultRoute({ strategy_research: true }), '/strategy')
  assert.equal(computeDefaultRoute({ strategy_research: true, market_review: true }), '/review')
  assert.equal(computeDefaultRoute({ research_replay: true }), '/auction')
})

test('strategy is independent of market, replay, and self selection', () => {
  assert.equal(hasCapability({ market_data:{active:true}, research_replay:{active:true} }, 'strategy_research', false), false)
  assert.equal(hasCapability({ strategy_research:{active:true} }, 'market_data', false), false)
  assert.equal(hasCapability({ strategy_research:{active:false} }, 'strategy_research', false), false)
  assert.equal(hasCapability({ strategy_research:{active:true} }, 'strategy_research', false), true)
  assert.equal(hasCapability({}, 'strategy_research', true), true)
  const visible = filterNavItemsByCapability(USER_NAV_ITEMS, { strategy_research:{active:true} }, false)
  assert.deepEqual(visible.map(x=>x.label), ['策略'])
})

test('strategy page is protected by capability guard in user shell', () => {
  const route=findRouteNode(ROUTE_STRUCTURE, '/strategy')
  assert.ok(route)
  assert.equal(route.node.shell,'user')
  assert.equal(hasGuardInChain(ROUTE_STRUCTURE, '/strategy', 'capability'), true)
})
