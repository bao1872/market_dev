// [MarketDashboard] - 导航契约：市场复盘入口能力 = market_data，且三个子页面共用同一入口高亮
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { USER_NAV_ITEMS, APP_ROUTES, resolveActiveNav } from '../appNavigation'

test('市场复盘导航项存在，使用 market_data 能力，指向大盘页', () => {
  const item = USER_NAV_ITEMS.find((i) => i.path === APP_ROUTES.marketDashboard)
  assert.ok(item, '市场复盘导航项应存在')
  assert.equal(item!.requiredCapability, 'market_data')
  assert.equal(item!.path, '/review/dashboard/market')
})

test('新增市场复盘入口不改变现有 /review 复盘项（仍 research_replay）', () => {
  const review = USER_NAV_ITEMS.find((i) => i.path === APP_ROUTES.review)
  assert.ok(review, '/review 导航项应存在')
  assert.equal(review!.requiredCapability, 'research_replay')
})

test('市场复盘子路径（industry / concept）高亮同一入口；非 dashboard 路径不高亮', () => {
  assert.equal(resolveActiveNav('/review/dashboard/market', '', APP_ROUTES.marketDashboard), true)
  assert.equal(resolveActiveNav('/review/dashboard/industry', '', APP_ROUTES.marketDashboard), true)
  assert.equal(resolveActiveNav('/review/dashboard/concept', '', APP_ROUTES.marketDashboard), true)
  assert.equal(resolveActiveNav('/review', '', APP_ROUTES.marketDashboard), false)
})
