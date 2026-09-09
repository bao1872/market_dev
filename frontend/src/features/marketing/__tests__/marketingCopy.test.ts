// 营销门户文案契约测试
// 目的：防止营销侧重新定义产品语义、防止公开门户越界暴露内部能力（评审 M0 / M1 审查要求）。
// M1.1 新增：公开产品边界（不得暴露 /review、/auction、/portal/index.html）、
//           第一金字塔渐进披露（不得出现在主导航、不得作为 numbered section）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CHIP_CONSENSUS_STORY,
  DISCOVERY,
  FIRST_PYRAMID,
  FOOTER,
  HERO,
  MARKET_LANGUAGE,
  NAV,
  STRUCTURE_STORY,
  WORKFLOW,
} from '../data/copy'

// 盘迹产品语言六维度：顺序与命名由产品侧定义，营销侧只能消费不能重定义
const EXPECTED_DIMENSIONS = ['趋势', '结构', '动量', '成交量', '筹码', '事件']

// 历史遗留语义，禁止在营销门户回流（CHANGE 记录中已被替换的旧表述）
const FORBIDDEN_LEGACY_TERMS = ['BOS', 'CHoCH', 'Order Block', ' breaker']

// 不对外开放、门户不得宣传的内部路由
const INTERNAL_ROUTES = ['/review', '/auction']

// 使用说明已裁定融合进门户，不再把用户送回旧 Help Center
const LEGACY_HELP_ROUTES = ['/portal/index.html']

function footerHrefs(): string[] {
  return FOOTER.columns
    .flatMap((column) => column.links)
    .map((link) => link.href ?? '')
    .filter((href) => href.length > 0)
}

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

test('公开营销门户不暴露内部研究路由', () => {
  const hrefs = footerHrefs()
  for (const route of INTERNAL_ROUTES) {
    assert.ok(!hrefs.includes(route), `公开 footer 不得暴露内部路由: ${route}`)
  }
})

test('公开营销门户不再把用户送回旧 Help Center', () => {
  const hrefs = footerHrefs()
  for (const route of LEGACY_HELP_ROUTES) {
    assert.ok(!hrefs.includes(route), `使用说明已融合进门户，不得再链回: ${route}`)
  }
})

test('第一金字塔不出现在主导航（渐进披露）', () => {
  for (const item of NAV.items) {
    assert.ok(
      !item.label.includes('99'),
      `主导航不得出现 99 字段入口: ${item.label}`,
    )
    assert.ok(
      !item.href.startsWith('#fields'),
      `主导航不得指向字段 section: ${item.href}`,
    )
  }
})

test('第一金字塔不再作为首页 numbered section', () => {
  const serialized = JSON.stringify(FIRST_PYRAMID)
  assert.ok(
    !serialized.includes('"index"'),
    'FIRST_PYRAMID 不得再持有 section index',
  )
  // 仍保留 Drawer 外壳与维度分组
  assert.ok(FIRST_PYRAMID.drawer.groups.length > 0)
  assert.ok(FIRST_PYRAMID.drawer.ariaLabel.length > 0)
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

test('公开文案整体不含历史遗留语义术语', () => {
  const raw = JSON.stringify({
    nav: NAV,
    hero: HERO,
    discovery: DISCOVERY,
    workflow: WORKFLOW,
    structureStory: STRUCTURE_STORY,
    chipConsensusStory: CHIP_CONSENSUS_STORY,
    marketLanguage: MARKET_LANGUAGE,
    footer: FOOTER,
    firstPyramid: FIRST_PYRAMID,
  })
  for (const term of FORBIDDEN_LEGACY_TERMS) {
    assert.ok(!raw.includes(term), `营销文案不得出现旧语义术语: ${term}`)
  }
})

test('首页 numbered section 序号严格为 01→05', () => {
  const indexes = [
    DISCOVERY.index,
    WORKFLOW.index,
    STRUCTURE_STORY.index,
    CHIP_CONSENSUS_STORY.index,
    MARKET_LANGUAGE.index,
  ]
  assert.deepEqual(indexes, ['01', '02', '03', '04', '05'])
})

// ===== Slice A：Hero + Nav 视觉对齐（参考图 #1 / #2）=====

test('NAV 含 5 项菜单 + 副标 + 立即使用 CTA', () => {
  assert.equal(NAV.tagline, '从零了解盘迹')
  assert.equal(NAV.ctaLabel, '立即使用')
  assert.equal(NAV.ctaHref, '/login')
  assert.equal(NAV.items.length, 5)
  const labels = NAV.items.map((i) => i.label)
  for (const expected of [
    '产品',
    '特性速览',
    '标的语境',
    '监控自选',
    '公众号文章',
  ]) {
    assert.ok(
      labels.includes(expected),
      `导航菜单缺少: ${expected}`,
    )
  }
})

test('HERO 含 1000+ 大字 stat + 开始体验 + 0:19 演示', () => {
  assert.equal(HERO.stat.value, '1000+')
  assert.equal(HERO.stat.label, '行业图')
  assert.equal(HERO.primaryCta.label, '开始体验')
  assert.equal(HERO.secondaryCta.label, '查看完整演示')
  assert.equal(HERO.secondaryCta.duration, '0:19')
})

test('HERO 底部两条状态徽章（多市场同步 / 盘中持续刷新）', () => {
  assert.equal(HERO.statusBadges.length, 2)
  const texts = HERO.statusBadges.map((b) => b.text)
  assert.ok(
    texts.some((t) => t.includes('多市场')),
    '缺少"多市场"相关徽章',
  )
  assert.ok(
    texts.some((t) => t.includes('持续')),
    '缺少"持续数据刷新"相关徽章',
  )
})

test('公开文案整体不含历史遗留语义术语（Slice A 后仍守住）', () => {
  // 旧测试已覆盖；这里重复一遍以确保新增 HERO.stat / statusBadges 不引入禁用词
  const raw = JSON.stringify({
    nav: NAV,
    hero: HERO,
    statusBadges: HERO.statusBadges,
  })
  for (const term of ['BOS', 'CHoCH', 'Order Block', ' breaker']) {
    assert.ok(!raw.includes(term), `营销文案不得出现旧语义术语: ${term}`)
  }
})
