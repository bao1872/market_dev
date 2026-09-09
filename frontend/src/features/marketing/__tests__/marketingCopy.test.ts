// 营销门户文案契约测试
// 目的：防止营销侧重新定义产品语义（评审 M0 明确要求"产品语义不得在营销侧漂移"）。
// M1 阶段：保护产品语言六维度、工作流顺序、以及"禁止复制 99 字段定义"这条架构约束。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DISCOVERY,
  FIRST_PYRAMID,
  MARKET_LANGUAGE,
  WORKFLOW,
} from '../data/copy'

// 盘迹产品语言六维度：顺序与命名由产品侧定义，营销侧只能消费不能重定义
const EXPECTED_DIMENSIONS = ['趋势', '结构', '动量', '成交量', '筹码', '事件']

// 历史遗留语义，禁止在营销门户回流（CHANGE 记录中已被替换的旧表述）
const FORBIDDEN_LEGACY_TERMS = ['BOS', 'CHoCH', 'Order Block', ' breaker']

test('市场语言六维度与产品语言完全一致', () => {
  assert.deepEqual(
    MARKET_LANGUAGE.dimensions.map((d) => d.title),
    EXPECTED_DIMENSIONS,
  )
})

test('市场语言每个维度都有非空描述', () => {
  for (const dim of MARKET_LANGUAGE.dimensions) {
    assert.ok(dim.key.length > 0, `维度 ${dim.title} 缺少 key`)
    assert.ok(dim.desc.length > 0, `维度 ${dim.title} 缺少描述`)
  }
})

test('工作流五步顺序固定为 发现→筛选→理解→自选→跟踪', () => {
  assert.deepEqual(
    WORKFLOW.steps.map((s) => s.title),
    ['发现', '筛选', '理解', '自选', '跟踪'],
  )
})

test('两种机会入口为 全市场 + 小Z说事', () => {
  assert.deepEqual(
    DISCOVERY.entries.map((e) => e.key),
    ['market', 'story'],
  )
})

test('M1 禁止复制 99 字段定义：Drawer 只含维度分组外壳', () => {
  const raw = JSON.stringify(FIRST_PYRAMID.drawer)
  // 分组标签本身允许出现（趋势/结构/动量/成交量/筹码）
  // 但不得出现任何具体字段名或旧语义术语
  for (const term of FORBIDDEN_LEGACY_TERMS) {
    assert.ok(!raw.includes(term), `Drawer 不得包含字段级定义或旧语义: ${term}`)
  }
  // 分组数量应保持"维度级"，不得膨胀为字段级（99 个字段会远超 12 个分组）
  assert.ok(
    FIRST_PYRAMID.drawer.groups.length <= 12,
    'Drawer 分组数量异常，可能存在字段级复制',
  )
})

test('各 section 序号唯一且非空', () => {
  const indexes = [
    DISCOVERY.index,
    WORKFLOW.index,
    MARKET_LANGUAGE.index,
    FIRST_PYRAMID.index,
  ]
  assert.equal(new Set(indexes).size, indexes.length, 'section 序号必须唯一')
  for (const i of indexes) {
    assert.ok(i.trim().length > 0, 'section 序号不得为空')
  }
})
