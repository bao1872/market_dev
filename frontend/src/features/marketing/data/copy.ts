// 盘迹营销门户文案（Full Alignment V1）
// 原则（owner 裁决锁死）：
//   REFERENCE DESIGN 负责版式/比例/留白/卡片语言/信息密度/节奏；
//   PANJI PRODUCT FACTS 负责文案/功能/数据字段/用户路径。
//   禁止为视觉稿创造不存在的产品事实。
//   禁止未经 source-backed 的 claim：1000+ / 多市场实时同步 / 0:19。
//   公众号文章不出现；统一使用「小Z说事 / 雪球」。
//   产品语义（趋势/结构/动量/成交量/筹码/事件）必须与产品页一致，营销侧只消费不重定义。

export const BRAND = {
  name: '盘迹',
  slogan: '看一眼就知道怎么用',
  description:
    '从全市场发现变化，理解个股状态，把值得继续看的股票留在自选并持续观察。',
}

// 顶部导航：5 项真实入口 + 绿色「开始使用」CTA + 品牌副标。
// 锚点指向当前 section；不出现 99/字段级入口。
export const NAV = {
  tagline: '从零了解盘迹',
  items: [
    { label: '产品', href: '#hero' },
    { label: '怎么工作', href: '#how-it-works' },
    { label: '经典场景', href: '#strategy-lab' },
    { label: '小Z说事', href: '#xiaoz' },
    { label: '状态提醒', href: '#watch-notify' },
  ],
  ctaLabel: '开始使用',
  ctaHref: '/login',
}

export interface HeroStatusBadge {
  readonly dot: 'green' | 'blue'
  readonly text: string
}

// Hero：两栏。左侧文案 + 六个维度 proof + CTA；右侧盘迹式筛选表。
// 删除所有未经证实的 claim：1000+ 行业图 / 多市场实时同步 / 盘中持续刷新 / 0:19。
export const HERO = {
  eyebrow: '盘迹 · 全市场状态终端',
  title: '从全市场发现变化，\n把真正值得看的股票留下来。',
  subtitle:
    '盘迹不替你选股，只把几千只股票里真正发生变化的部分，压缩成几个值得继续看的状态。',
  // 六维 proof：不编造数字，只描述盘迹看什么
  proof: {
    label: '盘迹描述六个维度',
    dims: ['趋势', '结构', '动量', '成交量', '筹码', '事件'],
  },
  primaryCta: { label: '开始使用', href: '/login' },
  // 次级 CTA：真实锚点，无时长徽章（无 0:19 视频）
  secondaryCta: { label: '看盘迹怎么工作', href: '#how-it-works' },
  // 诚实状态条：演示数据 + 看状态不替判断（无实时同步假 claim）
  statusBadges: [
    { dot: 'green', text: '演示数据 · 非实时行情' },
    { dot: 'green', text: '看状态，不替你做判断' },
  ] as readonly HeroStatusBadge[],
}

// 三种机会入口：全市场 / 板块 / 小Z说事。
// 每张卡带 3 步 mini flow 用于可视化（非纯文字）。
export const DISCOVERY = {
  index: '01',
  eyebrow: '机会从哪里来',
  title: '机会通常从三个地方开始。',
  subtitle: '一种来自市场自己给出的变化，一种来自你关注的板块，一种来自别人已经讨论的方向。',
  entries: [
    {
      key: 'market',
      title: '全市场发现',
      desc: '按六个维度，把当天真正发生变化的地方挑出来。',
      flow: ['数千只股票', '条件筛选', '候选范围'],
    },
    {
      key: 'section',
      title: '从板块进入',
      desc: '从今天关注的板块出发，在板块内部继续用同一套维度筛选。',
      flow: ['今日关注板块', '限定概念', '板块内筛选'],
    },
    {
      key: 'story',
      title: '从小Z说事进入',
      desc: '别人已经在讨论的方向，直接落到对应板块继续找个股。',
      flow: ['每日早晚复盘', '发现市场方向', '在盘迹继续找个股'],
    },
  ],
}

// 产品方法论：6 步 icon-grid（id=how-it-works 供次级 CTA 锚定）。
// 与 IA 流程一致：发现 → 筛选 → 理解 → 加入自选 → 持续跟踪 → 状态提醒。
export const WORKFLOW = {
  index: '02',
  eyebrow: '一条路径',
  id: 'how-it-works',
  title: '每天不需要重新从几千只股票开始。',
  subtitle: '发现 → 筛选 → 理解 → 加入自选 → 持续跟踪 → 状态提醒。',
  steps: [
    { key: 'discover', icon: 'radar', title: '发现', desc: '全市场扫描出当天发生变化的范围。' },
    { key: 'filter', icon: 'sliders', title: '筛选', desc: '用六个维度把范围收窄到值得看的几只。' },
    { key: 'understand', icon: 'eye', title: '理解', desc: '看清楚它现在处于什么状态，而不是只看涨跌。' },
    { key: 'watchlist', icon: 'bookmark', title: '加入自选', desc: '把值得继续看的留下来，其余的不用再盯。' },
    { key: 'track', icon: 'pulse', title: '持续跟踪', desc: '状态变化时再提醒你，不用一直盯着盘。' },
    { key: 'notify', icon: 'bell', title: '状态提醒', desc: '达到你关心的条件时，推送到飞书。' },
  ],
}

// 两个教学动画（M2）：deterministic，不接实时行情、不复制生产算法。
export const STRUCTURE_STORY = {
  index: '03',
  eyebrow: '结构怎么形成',
  title: '结构不是一个标签，\n是价格一步一步走出来的。',
  subtitle: '同一段行情，逐根看，才能看清状态是怎么变化的。',
  playLabel: '播放结构演示',
  pauseLabel: '暂停结构演示',
  eventLabel: '当前事件',
}

export const CHIP_CONSENSUS_STORY = {
  index: '04',
  eyebrow: '共识怎么形成',
  title: '筹码共识不是画出来的一条线，\n是成交一点一点堆出来的。',
  subtitle: '成交出现在什么价位，共识就在什么价位慢慢形成。',
  playLabel: '播放筹码共识演示',
  pauseLabel: '暂停筹码共识演示',
  consensusLabel: '主要成交密集价',
}

// 盘迹产品语言六维度（与产品页一致，营销侧不重新定义）。
// 视觉：居中一句 + 横向六维（不再六个同样的 card）。
export const MARKET_LANGUAGE = {
  index: '05',
  eyebrow: '盘迹看什么',
  title: '六个维度',
  centerSentence: '盘迹不是给股票打一个分，而是描述它现在处于什么状态。',
  subtitle: '趋势、结构、动量、成交量、筹码、事件——每个维度描述它现在的位置。',
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
// V1：Drawer 外壳 + 搜索框 + 8 个维度分组 tab（具体 99 字段由 M3.5 共享 registry 接入）。
// 明确标注 DATA PENDING，不伪造 99 项。
export const FIRST_PYRAMID = {
  drawer: {
    ariaLabel: '第一金字塔字段字典',
    triggerLabel: '第一金字塔字段字典',
    title: '第一金字塔字段字典',
    subtitle: '按维度折叠，需要时再展开。平时只看到结论，需要追细节的时候再一层层打开。',
    closeLabel: '关闭',
    searchPlaceholder: '搜索字段…',
    pendingNote: '字段内容由产品侧统一维护（M3.5 shared registry），此处仅展示分组外壳，DATA PENDING。',
    // 8 个维度分组（非字段级复制）
    groups: [
      { key: 'snapshot', label: '快照' },
      { key: 'trend', label: '趋势' },
      { key: 'structure', label: '结构' },
      { key: 'structureEvent', label: '结构事件' },
      { key: 'momentum', label: '动量' },
      { key: 'momentumEvent', label: '动量事件' },
      { key: 'chip', label: '筹码' },
      { key: 'volume', label: '量能' },
    ],
  },
}

// 经典场景（StrategyLab）：首页核心交互。4 个场景，每个含
// 左：逻辑说明；中：实际 filter chips（绿色=已选）；右：候选数量漏斗。
// 约束：不宣称「精确实现经典道氏123」，统一写「道氏123风格」（现有字段为可编程近似）。
export const STRATEGY_LAB = {
  index: '06',
  eyebrow: '经典场景',
  title: '把范围压缩到值得继续看的那几只。',
  subtitle: '选几个经典场景，看盘迹怎么把全市场一步步收窄。',
  note: '以上为可编程近似，不是对经典理论的精确复刻；具体字段以产品内为准。',
  scenarios: [
    {
      id: 'dow123',
      icon: 'trend',
      name: '道氏123风格 · 反转确认',
      logic: '价格形成主要高低点，短线结构先转强，主要结构再被突破——对应一段可能的反转。',
      chips: [
        { label: '趋势方向 = 上行', active: true },
        { label: '结构事件近期发生', active: true },
        { label: '趋势连续周期 <= 3', active: false },
        { label: '成交量异常', active: false },
      ],
      funnel: [
        { label: '全市场', count: 5267 },
        { label: '趋势方向 = 上行', count: 683 },
        { label: '结构事件近期发生', count: 147 },
        { label: '趋势连续周期 <= 3', count: 42 },
        { label: '成交量异常', count: 18 },
      ],
    },
    {
      id: 'trend-follow',
      icon: 'filter',
      name: '趋势跟踪',
      logic: '已经形成主要上行结构，且短线结构仍在顺着原方向推进的标的。',
      chips: [
        { label: '趋势方向 = 上行', active: true },
        { label: '主要结构确认', active: true },
        { label: '短线结构未转弱', active: true },
        { label: '成交量未明显萎缩', active: false },
      ],
      funnel: [
        { label: '全市场', count: 5267 },
        { label: '趋势方向 = 上行', count: 683 },
        { label: '主要结构确认', count: 312 },
        { label: '短线结构未转弱', count: 198 },
        { label: '成交量未明显萎缩', count: 121 },
      ],
    },
    {
      id: 'new-trend-volume',
      icon: 'bolt',
      name: '新趋势 + 异常放量',
      logic: '价格刚突破原有结构，同时成交量明显放大，变化更有可能是真的。',
      chips: [
        { label: '结构事件近期发生', active: true },
        { label: '成交量异常', active: true },
        { label: '趋势方向 = 上行', active: true },
        { label: '短线结构转强', active: false },
      ],
      funnel: [
        { label: '全市场', count: 5267 },
        { label: '结构事件近期发生', count: 147 },
        { label: '成交量异常', count: 18 },
        { label: '趋势方向 = 上行', count: 9 },
      ],
    },
    {
      id: 'sector',
      icon: 'layers',
      name: '板块内寻找机会',
      logic: '先锁定一个板块，再在板块内部用同一套维度，找出相对更强的几只。',
      chips: [
        { label: '限定板块', active: true },
        { label: '趋势方向 = 上行', active: true },
        { label: '结构一致', active: true },
        { label: '成交未明显萎缩', active: false },
      ],
      funnel: [
        { label: '机器人板块', count: 86 },
        { label: '趋势上行', count: 34 },
        { label: '结构一致', count: 16 },
        { label: '成交未明显萎缩', count: 9 },
      ],
    },
  ],
}

// 小Z说事 → 盘迹（雪球内容入口，非盘迹 /review 产品能力；公开页禁止暴露 /review）。
export const XIAOZ = {
  index: '07',
  eyebrow: '复盘之后',
  title: '复盘发现方向以后，下一步怎么办？',
  article: {
    source: '小Z说事 · 雪球',
    badge: '今日观察',
    snippet: '机器人方向重新活跃，资金从高位向低位扩散，板块内部开始出现结构分化……',
    ctaLabel: '在盘迹查看机器人',
  },
  // 漏斗：从板块到「值得研究」
  funnel: [
    { label: '机器人', count: 86, unit: '只' },
    { label: '+ 趋势上行', count: 34 },
    { label: '+ 结构一致', count: 16 },
    { label: '+ 成交未明显萎缩', count: 9 },
  ],
  highlight: '9 只值得进一步研究',
  core: '不是替你选答案，而是把范围压缩。',
}

// 自选 + 通知（Watch + Notify）：流程 + 真实产品产出截图。
export const WATCH_NOTIFY = {
  index: '08',
  eyebrow: '不用一直盯着',
  title: '不用一直盯着盘迹。',
  subtitle: '值得重新看的时候，再把它送到你面前。',
  steps: ['发现候选', '加入自选', '状态变化', '生成研究图片', '推送飞书'],
  // 优先真实飞书截图 poster_img1；备选 intraday-monitor。
  imageSrc: '/landing/assets/images/poster_img1.webp',
  imageAlt: '盘迹推送到飞书的研究图片（真实产出示意）',
  imageNote: '图片为盘迹真实产出示意，非产品截图合成。',
}

// 营销页对外链接（雪球搜索「小Z说事」）。
export const XUEQIU_SEARCH_URL = 'https://xueqiu.com/k?q=%E5%B0%8FZ%E8%AF%B4%E4%BA%8B'

// 最终 CTA（在大 footer 之前）。
export const FINAL_CTA = {
  title: '开始使用盘迹',
  subtitle: '盘迹负责压缩信息，不替你做判断。',
  primaryCta: { label: '开始使用', href: '/login' },
  secondaryCta: {
    label: '雪球搜索「小Z说事」',
    href: XUEQIU_SEARCH_URL,
    external: true,
  },
  invite: {
    title: 'QQ 邀请码',
    imageSrc: '/landing/assets/images/qq_shot.png',
    imageAlt: '扫码添加 QQ 获取邀请码',
    note: '截图裁剪展示邀请码区域。',
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

// 内容入口唯一对外链接见上方 XUEQIU_SEARCH_URL 声明。

export const FOOTER = {
  brand: BRAND,
  columns: [
    {
      title: '产品',
      links: [
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
