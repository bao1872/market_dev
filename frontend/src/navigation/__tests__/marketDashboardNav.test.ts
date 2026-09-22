// [MarketDashboard] - 导航契约：[PANJI-REVIEW-CAPABILITY-SPLIT] 复盘入口能力 = 独立 market_review，
// 四个 canonical 子页面（大盘/行业/概念/比较）共用同一入口高亮。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { USER_NAV_ITEMS, APP_ROUTES, resolveActiveNav } from '../appNavigation'

test('复盘导航项存在，使用独立 market_review 能力，指向 canonical /review', () => {
  const item = USER_NAV_ITEMS.find((i) => i.path === APP_ROUTES.review)
  assert.ok(item, '复盘导航项应存在')
  assert.equal(item!.requiredCapability, 'market_review')
  assert.equal(item!.path, '/review')
})

test('不存在重复的「市场复盘」二级导航入口', () => {
  const reviewItems = USER_NAV_ITEMS.filter((i) => i.path.startsWith('/review'))
  assert.equal(reviewItems.length, 1, '复盘只保留一个一级导航入口')
  assert.ok(!USER_NAV_ITEMS.some((i) => i.path === '/review/dashboard/market'))
})

test('复盘子路径（industry / concept / compare）高亮同一入口；行情/竞价路径不高亮', () => {
  assert.equal(resolveActiveNav('/review', '', APP_ROUTES.review), true)
  assert.equal(resolveActiveNav('/review/industry', '', APP_ROUTES.review), true)
  assert.equal(resolveActiveNav('/review/concept', '', APP_ROUTES.review), true)
  assert.equal(resolveActiveNav('/review/compare', '', APP_ROUTES.review), true)
  assert.equal(resolveActiveNav('/market', '', APP_ROUTES.review), false)
  assert.equal(resolveActiveNav('/auction', '', APP_ROUTES.review), false)
})
