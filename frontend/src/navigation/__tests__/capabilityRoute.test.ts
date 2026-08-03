// [权限模型 V2] computeDefaultRoute 默认入口合同测试。
// 与后端 effective_access_service.compute_default_route 一致，供登录/邀请码预览/后台列表复用。
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  DEFAULT_ROUTE_FORBIDDEN,
  DEFAULT_ROUTE_MARKET,
  DEFAULT_ROUTE_MARKET_WATCHLIST,
  DEFAULT_ROUTE_REVIEW,
  computeDefaultRoute,
} from '../capabilities.ts'

describe('computeDefaultRoute', () => {
  it('无 active capability → /forbidden', () => {
    assert.equal(computeDefaultRoute({}), DEFAULT_ROUTE_FORBIDDEN)
  })
  it('仅 self_selection → /market?scope=watchlist', () => {
    assert.equal(computeDefaultRoute({ self_selection: true }), DEFAULT_ROUTE_MARKET_WATCHLIST)
  })
  it('仅 market_data → /market', () => {
    assert.equal(computeDefaultRoute({ market_data: true }), DEFAULT_ROUTE_MARKET)
  })
  it('仅 research_replay → /review', () => {
    assert.equal(computeDefaultRoute({ research_replay: true }), DEFAULT_ROUTE_REVIEW)
  })
  it('self_selection + market_data → /market', () => {
    assert.equal(
      computeDefaultRoute({ self_selection: true, market_data: true }),
      DEFAULT_ROUTE_MARKET,
    )
  })
  it('research_replay + market_data → /market', () => {
    assert.equal(
      computeDefaultRoute({ research_replay: true, market_data: true }),
      DEFAULT_ROUTE_MARKET,
    )
  })
  it('admin → /admin/overview', () => {
    assert.equal(computeDefaultRoute({}, true), '/admin/overview')
  })
})
