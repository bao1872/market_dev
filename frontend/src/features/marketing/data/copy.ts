// 盘迹营销门户文案（M1 骨架）
// 文案真源：现有静态门户 public/landing/index.html 的产品语言（slogan / description / fields / feishu）
// M1 阶段全部 deterministic，不接任何实时数据、不接后端。
// 约束：产品语义（趋势/结构/动量/成交量/筹码/事件）必须与产品页一致，不得在营销侧重新定义。

export const BRAND = {
  name: '盘迹',
  slogan: '看一眼就知道怎么用',
  description:
    '从全市场发现变化，理解个股状态，把值得继续看的股票留在自选并持续观察。',
}

// 顶部导航（参考图 #1 对齐）：
// - 5 项菜单 + 品牌副标 "从零了解盘迹" + 绿色 CTA "立即使用"。
// - 锚点指向当前 section；公众号文章为占位（M4 接入真实外链）。
// - 不得出现 99/字段级入口（营销契约测试守住，详见 __tests__/marketingCopy.test.ts）。
export const NAV = {
  tagline: '从零了解盘迹',
  items: [
    { label: '产品', href: '#hero' },
    { label: '特性速览', href: '#hero-screener' },
    { label: '标的语境', href: '#discovery' },
    { label: '监控自选', href: '#workflow' },
    { label: '公众号文章', href: '#feishu-articles', note: 'M4 接入' },
  ],
  ctaLabel: '立即使用',
  ctaHref: '/login',
}

// Hero（参考图 #2 对齐）：
// - 左侧标题 + 副标题 + 大字 stat "1000+ 行业图" + 2 CTA；
// - 右侧 HeroScreener 静态表（见 data/heroScreener.ts）；
// - 底部 statusBadges：双行状态条（多市场同步 / 持续数据刷新）。
// 硬约束：表格为演示数据（heroScreener.NOTE），不接实时行情。
export interface HeroStatusBadge {
  readonly dot: 'green' | 'blue'
  readonly text: string
}

export interface HeroStat {
  readonly value: string
  readonly label: string
}

export const HERO = {
  eyebrow: BRAND.slogan,
  title: '从全市场发现变化，\n把真正值得看的股票留下来。',
  subtitle: BRAND.description,
  stat: {
    value: '1000+',
    label: '行业图',
  } as HeroStat,
  primaryCta: { label: '开始体验', href: '/login' },
  secondaryCta: {
    label: '查看完整演示',
    duration: '0:19',
    href: '#workflow',
  },
  statusBadges: [
    { dot: 'green', text: '多市场状态实时同步' },
    { dot: 'blue', text: '盘中持续数据刷新' },
  ] as readonly HeroStatusBadge[],
}

// 两种机会入口：全市场扫描 + 小Z说事（内容驱动）
export const DISCOVERY = {
  index: '01',
  eyebrow: '两种入口',
  title: '机会通常从两个地方开始。',
  subtitle: '一种是市场自己给出的变化，一种是别人已经讨论起来的方向。',
  entries: [
    {
      key: 'market',
      title: '从全市场扫描',
      desc: '按趋势、结构、动量、成交量、筹码、事件六个维度，把当天真正发生变化的地方挑出来。',
      bullets: ['全市场', '板块', '自选范围'],
    },
    {
      key: 'story',
      title: '从小Z说事进入',
      desc: '别人已经在讨论的方向，直接落到对应板块，再用同一套维度继续筛。',
      bullets: ['内容 → 板块', '板块 → 个股', '同一套筛选条件'],
    },
  ],
}

// 产品方法论流程（评审 IA），区别于现有门户的"上手四步"
export const WORKFLOW = {
  index: '02',
  eyebrow: '一条路径',
  title: '真正需要记住的流程只有一条。',
  subtitle: '发现 → 筛选 → 理解 → 自选 → 跟踪。日常只需要这一条路径。',
  steps: [
    { key: 'discover', title: '发现', desc: '全市场扫描出当天发生变化的范围。' },
    { key: 'filter', title: '筛选', desc: '用六个维度把范围收窄到值得看的几只。' },
    { key: 'understand', title: '理解', desc: '看清楚它现在处于什么状态，而不是只看涨跌。' },
    { key: 'watchlist', title: '自选', desc: '把值得继续看的留下来，其余的不用再盯。' },
    { key: 'track', title: '跟踪', desc: '状态变化时再提醒你，不用一直盯着盘。' },
  ],
}

// 两个教学动画（M2）：deterministic 教学剧本，不接实时行情、不复制生产算法。
// 阶段文案与 demo/structureStory.ts 的 STRUCTURE_STAGES 一一对应。
export const STRUCTURE_STORY = {
  index: '03',
  eyebrow: '结构怎么形成',
  title: '结构不是一个标签，\n是价格一步一步走出来的。',
  subtitle: '同一段行情，逐根看，才能看清状态是怎么变化的。',
  playLabel: '播放结构演示',
  pauseLabel: '暂停结构演示',
  eventLabel: '当前事件',
}

// 阶段文案与 demo/chipConsensusStory.ts 的 CHIP_STAGES 一一对应。
export const CHIP_CONSENSUS_STORY = {
  index: '04',
  eyebrow: '共识怎么形成',
  title: '筹码共识不是画出来的一条线，\n是成交一点一点堆出来的。',
  subtitle: '成交出现在什么价位，共识就在什么价位慢慢形成。',
  playLabel: '播放筹码共识演示',
  pauseLabel: '暂停筹码共识演示',
  consensusLabel: '主要成交密集价',
}

// 盘迹产品语言六维度（与产品页一致，营销侧不重新定义）
export const MARKET_LANGUAGE = {
  index: '05',
  eyebrow: '盘迹看什么',
  title: '盘迹看的不是孤立的涨跌数字，\n而是完整的市场状态。',
  subtitle: '六个维度，描述一只股票现在到底处在什么位置。',
  dimensions: [
    { key: 'trend', title: '趋势', desc: '方向在哪，持续了多久。' },
    { key: 'structure', title: '结构', desc: '高低点怎么移动，关键位置有没有被改变。' },
    { key: 'momentum', title: '动量', desc: '推进的力度是变强还是变弱。' },
    { key: 'volume', title: '成交量', desc: '这个变化有没有成交量支撑。' },
    { key: 'chip', title: '筹码', desc: '成交集中在什么价位，共识在哪里形成。' },
    { key: 'event', title: '事件', desc: '发生了什么，什么时候发生的。' },
  ],
}

// 第一金字塔：渐进披露，不作为首页 numbered section，也不出现在主导航。
// M1.1 只保留 Drawer 外壳与维度分组；M3.5 从产品侧共享 presentation registry 接入具体字段。
export const FIRST_PYRAMID = {
  drawer: {
    ariaLabel: '第一金字塔字段字典',
    triggerLabel: '第一金字塔字段字典',
    title: '99 个字段不用背',
    subtitle: '按维度折叠，需要时再展开。平时只看到结论，需要追细节的时候再一层层打开。',
    closeLabel: '关闭',
    // M1.1 占位：仅展示维度分组外壳，具体字段由 M3.5 共享 registry 提供
    groups: [
      { key: 'trend', label: '趋势' },
      { key: 'structure', label: '结构' },
      { key: 'momentum', label: '动量' },
      { key: 'volume', label: '成交量' },
      { key: 'chip', label: '筹码' },
    ],
    placeholder: '字段定义由产品侧统一维护，这里不复制第二套。',
  },
}

// 营销页对外链接。href 缺失时渲染为纯文本说明，不制造假链接。
export type MarketingLink = {
  label: string
  href?: string
  external?: boolean
  note?: string
}

type FooterColumn = {
  title: string
  links: MarketingLink[]
}

// 内容入口唯一对外链接：雪球搜索「小Z说事」。
// 不做雪球抓取、不做自动 NLP——这里只是一个跳转，内容场景由 reviewExamples 维护。
export const XUEQIU_SEARCH_URL = 'https://xueqiu.com/k?q=%E5%B0%8FZ%E8%AF%B4%E4%BA%8B'

export const FOOTER = {
  brand: BRAND,
  columns: [
    {
      title: '产品',
      links: [
        // 「复盘」仅作为内容品牌语境出现（见下方内容栏），不作为产品路由对外暴露：
        // /review 与 /auction 不对外开放，公开门户不宣传。
        { label: '行情', href: '/market' },
        { label: '自选', href: '/market?scope=watchlist' },
      ],
    },
    {
      title: '内容',
      links: [
        { label: '雪球搜索「小Z说事」', href: XUEQIU_SEARCH_URL, external: true },
        {
          label: '关注每日早晚复盘',
          note: '每日早晚各一篇，讲当天市场发生了什么。',
        },
      ],
    },
  ] as FooterColumn[],
  invitation: {
    title: '需要邀请码？',
    desc: '扫码添加 QQ 联系。（二维码资产在后续视觉阶段接入）',
  },
  copyright: '盘迹 · 看一眼就知道怎么用',
}
