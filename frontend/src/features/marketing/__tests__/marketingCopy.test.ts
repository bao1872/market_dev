// 营销门户文案契约测试
// 目的：防止营销侧重新定义产品语义、防止公开门户越界暴露内部能力（评审 M0 / M1 审查要求）。
// Full Alignment V1 更新：6 步工作流、3 卡发现、删除未证实 claim（1000+ / 多市场实时 / 0:19 / 公众号文章）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  CHIP_CONSENSUS_STORY,
  DISCOVERY,
  FINAL_CTA,
  FIRST_PYRAMID,
  FOOTER,
  HERO,
  MARKETING_MEDIA,
  MARKET_LANGUAGE,
  NAV,
  STRATEGY_LAB,
  STRUCTURE_STORY,
  WATCH_NOTIFY,
  WORKFLOW,
  XUEQIU_PROFILE_URL,
} from '../data/copy'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

// 盘迹产品语言六维度：顺序与命名由产品侧定义，营销侧只能消费不能重定义
const EXPECTED_DIMENSIONS = ['趋势', '结构', '动量', '成交量', '筹码', '事件']

// 历史遗留语义，禁止在营销门户回流（CHANGE 记录中已被替换的旧表述）
const FORBIDDEN_LEGACY_TERMS = ['BOS', 'CHoCH', 'Order Block', ' breaker']

// 不对外开放、门户不得宣传的内部路由
const INTERNAL_ROUTES = ['/review', '/auction']

// 旧使用说明 Help Center 已彻底退役（frontend/public/portal 已删除，nginx 301 到 /）；
// 营销 footer 不得再链接任何 /portal/ 入口
const LEGACY_HELP_ROUTES = ['/portal/']

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

test('工作流六步顺序固定为 发现→筛选→理解→加入自选→持续跟踪→状态提醒', () => {
  assert.deepEqual(
    WORKFLOW.steps.map((s) => s.title),
    ['发现', '筛选', '理解', '加入自选', '持续跟踪', '状态提醒'],
  )
})

test('三种机会入口为 全市场 + 板块 + 小Z说事', () => {
  assert.deepEqual(
    DISCOVERY.entries.map((e) => e.key),
    ['market', 'section', 'story'],
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

// ===== Full Alignment V1：Hero + Nav 文案对齐（参考图 #1 / #2）=====

test('NAV 含 5 项真实菜单 + 副标 + 开始使用 CTA（无公众号文章）', () => {
  assert.equal(NAV.tagline, '先看懂，再上手')
  assert.equal(NAV.ctaLabel, '开始使用')
  assert.equal(NAV.ctaHref, '/login')
  assert.equal(NAV.items.length, 5)
  const labels = NAV.items.map((i) => i.label)
  for (const expected of [
    '产品',
    '怎么用',
    '真实案例',
    '交流',
    '状态提醒',
  ]) {
    assert.ok(labels.includes(expected), `导航菜单缺少: ${expected}`)
  }
  // [V1.5] 导航用「交流 → #community」替代「小Z说事 → #xiaoz」；雪球出口收束进 Footer 二维码
  assert.ok(!labels.includes('小Z说事'), '导航不得再出现「小Z说事」独立入口')
  const communityItem = NAV.items.find((i) => i.label === '交流')
  assert.ok(communityItem?.href === '#community', '「交流」必须指向 #community')
  // 禁止「公众号文章」回流
  assert.ok(
    !labels.includes('公众号文章'),
    '导航不得出现「公众号文章」，应使用「小Z说事 / 雪球」',
  )
})

test('HERO 无未证实 claim：无 1000+、无 0:19、次级 CTA 指向 #how-it-works', () => {
  // 删除 Slice A 假 claim
  assert.ok(
    !('stat' in HERO) || (HERO as Record<string, unknown>).stat === undefined,
    'HERO 不得再含 1000+ 行业图 stat',
  )
  assert.ok(
    !('duration' in (HERO.secondaryCta as Record<string, unknown>)),
    'HERO 次级 CTA 不得含 0:19 时长',
  )
  assert.equal(HERO.primaryCta.label, '开始使用')
  assert.equal(HERO.secondaryCta.label, '看盘迹怎么工作')
  assert.equal(HERO.secondaryCta.href, '#how-it-works')
  // 文案整体不得出现被禁止的未证实 claim
  const heroRaw = JSON.stringify(HERO)
  for (const claim of ['1000+', '多市场状态实时同步', '盘中持续数据刷新', '0:19']) {
    assert.ok(!heroRaw.includes(claim), `HERO 不得含未证实 claim: ${claim}`)
  }
})

test('HERO 底部两条诚实状态条（真实产品界面 / 历史示例仅用于功能说明）', () => {
  assert.equal(HERO.statusBadges.length, 2)
  const texts = HERO.statusBadges.map((b) => b.text)
  assert.ok(
    texts.some((t) => t.includes('真实产品界面')),
    '缺少「真实产品界面」诚实状态条',
  )
  assert.ok(
    texts.some((t) => t.includes('案例均来自历史数据')),
    '缺少「案例均来自历史数据」诚实状态条',
  )
  // 已删除旧假数据表语义：不再自称「演示数据 · 非实时行情」
  assert.ok(
    !texts.some((t) => t.includes('演示数据')),
    'Hero 已改真实产品大屏，不得再标注「演示数据 · 非实时行情」',
  )
})

test('公开文案不含未证实的产品 claim（Full Alignment V1 守住）', () => {
  const raw = JSON.stringify({
    nav: NAV,
    hero: HERO,
    statusBadges: HERO.statusBadges,
  })
  for (const term of ['BOS', 'CHoCH', 'Order Block', ' breaker']) {
    assert.ok(!raw.includes(term), `营销文案不得出现旧语义术语: ${term}`)
  }
  for (const claim of ['1000+', '多市场状态实时同步', '盘中持续数据刷新', '0:19', '公众号文章']) {
    assert.ok(!raw.includes(claim), `营销文案不得含未证实/越界 claim: ${claim}`)
  }
})

// ===== V1.5 真实玩法案例（StrategyLab）=====

test('V1.5-A. STRATEGY_LAB 恰好 4 个玩法 tab，前三为 case、第四为 explore', () => {
  assert.equal(STRATEGY_LAB.cases.length, 4)
  const kinds = STRATEGY_LAB.cases.map((c) => c.kind)
  assert.deepEqual(
    kinds,
    ['case', 'case', 'case', 'explore'],
    '前三必须为 case，第四为 explore',
  )
  const [g, n, j, explore] = STRATEGY_LAB.cases as [
    RealCase,
    RealCase,
    RealCase,
    ExploreCase,
  ]
  // 前三对应的真实个股与代码
  assert.equal(g.stock + g.symbol, '国创高新' + '002377')
  assert.equal(n.stock + n.symbol, '南亚新材' + '688519')
  assert.equal(j.stock + j.symbol, '精智达' + '688627')
  // 统一用「道氏123风格 / 思路」，不宣称精确实现经典
  assert.ok(g.playbook.includes('道氏123'), `国创高新玩法须含道氏123: ${g.playbook}`)
  assert.ok(!g.playbook.includes('经典道氏123'), '不得宣称精确实现经典道氏123')
  // 每一真实案例都消费 MARKETING_MEDIA 中存在的图像
  assert.ok(String(MARKETING_MEDIA.caseGuochuangDow123).includes(g.imageSrc))
  assert.ok(String(MARKETING_MEDIA.caseNanyaTrend).includes(n.imageSrc))
  assert.ok(String(MARKETING_MEDIA.caseJingzhidaDoubleBottom).includes(j.imageSrc))
  for (const c of [g, n, j]) {
    assert.ok(c.imageSrc.startsWith('/marketing-assets/media/'), `case 图须在 media 目录: ${c.imageSrc}`)
    assert.ok(c.note.length > 0, '每一真实案例必须有免责 note')
  }
  assert.equal(explore.kind, 'explore')
  assert.ok(Array.isArray(explore.examples) && explore.examples.length >= 6, 'explore 需给足示例组合')
  // 不得虚构候选数量漏斗
  const raw = JSON.stringify(STRATEGY_LAB)
  for (const term of ['funnel', 'candidateCount', '86→34']) {
    assert.ok(!raw.includes(term), `STRATEGY_LAB 不得包含候选漏斗语义: ${term}`)
  }
})

// ===== V1.5 页面顺序 / Footer 社区出口 / FinalCTA =====

const MARKETING_PAGE = readFileSync(
  resolve(FRONTEND_ROOT, 'src/features/marketing/MarketingPage.tsx'),
  'utf-8',
)

test('V1.5-B. MarketingPage 不再存在 XiaozToPanji / #xiaoz', () => {
  assert.ok(
    !MARKETING_PAGE.includes('XiaozToPanji'),
    'MarketingPage 不得再渲染 XiaozToPanji',
  )
  const raw = JSON.stringify(NAV)
  assert.ok(!raw.includes('#xiaoz'), 'NAV 不得再有 #xiaoz 死锚点')
})

test('V1.5-C. Footer is community；恰好 2 张社区二维码；雪球 URL 精确', () => {
  assert.equal(FOOTER.community.id, 'community')
  assert.equal(FOOTER.community.cards.length, 2)
  const qq = FOOTER.community.cards.find((c) => c.id === 'qq')
  const xq = FOOTER.community.cards.find((c) => c.id === 'xueqiu')
  assert.ok(qq, '必须存在 QQ 群卡')
  assert.ok(xq, '必须存在雪球卡')
  assert.ok(qq.subtitle.includes('364121472'), 'QQ 群号必须为 364121472')
  assert.equal(xq.href, XUEQIU_PROFILE_URL, '雪球 href 必须是精确 URL')
  for (const c of FOOTER.community.cards) {
    assert.ok(
      c.imageSrc.startsWith('/marketing-assets/media/'),
      `社区二维码须在 media 目录: ${c.imageSrc}`,
    )
  }
})

test('V1.5-D. FINAL_CTA 不再含 invite，次级 CTA 为「加入交流 → #community」', () => {
  const any = FINAL_CTA as { invite?: unknown }
  assert.equal(any.invite, undefined, 'FINAL_CTA.invite 必须删除')
  assert.equal(FINAL_CTA.secondaryCta.label, '加入交流群')
  assert.equal(FINAL_CTA.secondaryCta.href, '#community')
})

type RealCase = Extract<(typeof STRATEGY_LAB.cases)[number], { kind: 'case' }>
type ExploreCase = Extract<(typeof STRATEGY_LAB.cases)[number], { kind: 'explore' }>

// ===== V1.5.1 中文文案收口（只锁核心语义句，不锁全文案逐字）=====

const STRATEGY_LAB_SRC = readFileSync(
  resolve(FRONTEND_ROOT, 'src/features/marketing/sections/StrategyLab.tsx'),
  'utf-8',
)

test('V1.5.1-A. 全页核心中文语义句锁定（拒绝翻译腔回流）', () => {
  const raw = JSON.stringify({
    hero: HERO,
    workflow: WORKFLOW,
    chipConsensusStory: CHIP_CONSENSUS_STORY,
    strategyLab: STRATEGY_LAB,
    watchNotify: WATCH_NOTIFY,
    finalCta: FINAL_CTA,
    footer: FOOTER,
  })
  for (const phrase of [
    '先把全市场的变化找出来',
    '每天其实就做这几步',
    '股价先走，成交重心不一定马上跟',
    '盘迹怎么用',
    '先用起来，再看它合不合你的方法',
  ]) {
    assert.ok(raw.includes(phrase), `营销文案缺少 V1.5.1 核心语义句: ${phrase}`)
  }
})

test('V1.5.1-B. StrategyLab 中文栏目名：先看什么 / 盘迹里怎么看', () => {
  assert.ok(STRATEGY_LAB_SRC.includes('先看什么'), 'StrategyLab 缺「先看什么」栏目名')
  assert.ok(STRATEGY_LAB_SRC.includes('盘迹里怎么看'), 'StrategyLab 缺「盘迹里怎么看」栏目名')
  assert.ok(
    !STRATEGY_LAB_SRC.includes('这个案例在看什么'),
    'StrategyLab 不得再出现翻译腔栏目名「这个案例在看什么」',
  )
  assert.ok(
    !STRATEGY_LAB_SRC.includes('盘迹怎么参与'),
    'StrategyLab 不得再出现翻译腔栏目名「盘迹怎么参与」',
  )
})

test('V1.5.1-C. FOOTER.invitation 已删除，雪球出口不再用搜索表述', () => {
  assert.equal(
    (FOOTER as Record<string, unknown>).invitation,
    undefined,
    'FOOTER.invitation 必须删除',
  )
  const raw = JSON.stringify(FOOTER)
  assert.ok(!raw.includes('搜索小Z说事'), 'Footer 不得再用雪球搜索表述')
})
