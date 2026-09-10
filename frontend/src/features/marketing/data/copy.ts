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
    { label: '交流', href: '#community' },
    { label: '状态提醒', href: '#watch-notify' },
  ],
  ctaLabel: '开始使用',
  ctaHref: '/login',
}

// 营销页媒体资产统一前缀：/marketing-assets/media/
// 真实图片在 build:marketing-site 步骤 cp 到 dist-marketing-site/media/，
// 由 panji-marketing-site-deploy rsync 到生产目录并 verify 200。
// 不再依赖旧 landing 静态目录里的图片（轻部署通道不发布该目录）。
// [V1.2] 媒体/回放数据 SSOT：Marketing 组件只允许消费这里声明的路径，
//   不得自己写 media/data URL。ref/ 被 .gitignore 排除，无法作为 runtime asset，
//   必须引用这些入库的 canonical Marketing media。
export const MARKETING_MEDIA = {
  feishuPoster: '/marketing-assets/media/poster_img1.webp',
  desktopProduct: '/marketing-assets/media/panji-desktop-product.png',
  mobileResearch: '/marketing-assets/media/panji-mobile-research.jpg',
  // [V1.5] 真实玩法案例截图（真实产品截图，仅裁边/resize/WebP，不造界面）。
  caseGuochuangDow123:
    '/marketing-assets/media/case-guochuang-002377-dow123.webp',
  caseNanyaTrend:
    '/marketing-assets/media/case-nanya-688519-trend.webp',
  caseJingzhidaDoubleBottom:
    '/marketing-assets/media/case-jingzhida-688627-double-bottom.webp',
  // [V1.5] 社区出口二维码（QQ 群 + 雪球主页，真实可扫）。
  communityQqQr: '/marketing-assets/media/community-qq-group-qr.png',
  communityXueqiuQr: '/marketing-assets/media/community-xueqiu-qr.png',
  // [V1.2] 中际旭创真实结构回放 frozen JSON。轻部署只 serve /marketing-assets/media/
  //   （deploy rsync BUILD_DIR}/media -> SITE_ASSET_TARGET}/media），不放 /data/ 以免 404。
  structureReplay:
    '/marketing-assets/media/zhongji-xuchuang-300308-1d-2y.json',
  // [V1.4] 近岸蛋白真实筹码共识回放 frozen JSON（production node_cluster canonical）。
  chipConsensusReplay:
    '/marketing-assets/media/nearshore-protein-688137-chip-consensus-1d-250d.json',
} as const

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
  // 诚实状态条：真实产品界面 + 历史示例仅用于功能说明（看状态不替判断，无实时同步假 claim）
  statusBadges: [
    { dot: 'green', text: '真实产品界面' },
    { dot: 'green', text: '历史示例仅用于功能说明' },
  ] as readonly HeroStatusBadge[],
}

// 三种机会入口：全市场 / 板块 / 小Z说事。
// [V1.4] 三卡严格同构（编号 + 标题 + 三步 flow + 解释）：一律用 mini flow，entries 无 media 字段。
// [V1.5] 独立 XiaozToPanji section 已删除，雪球不再独占页面；社区出口统一收束进 Footer 双二维码。
export const DISCOVERY = {
  index: '01',
  eyebrow: '机会从哪里来',
  title: '机会通常从三个地方开始。',
  subtitle: '一种来自市场自己给出的变化，一种来自你关注的板块，一种来自别人已经讨论的方向。',
  entries: [
    {
      key: 'market',
      title: '全市场发现',
      desc: '不知道今天看什么时，从全市场发生的变化开始。',
      flow: ['市场发生变化', '六维条件筛选', '留下候选'],
    },
    {
      key: 'section',
      title: '从板块进入',
      desc: '已经有关注方向时，直接在板块内部继续筛。',
      flow: ['今天关注板块', '限定板块范围', '板块内找个股'],
    },
    {
      key: 'story',
      title: '从小Z说事进入',
      desc: '复盘先回答今天市场在交易什么，再把方向带进盘迹。',
      flow: ['复盘发现方向', '进入对应板块', '按自己的标准找个股'],
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

// 真实结构回放（V1.3）：不再使用 synthetic 教学 K 线，
// 而播放中际旭创 300308 近两年真实日线 + canonical SMC（盘迹真实结构计算代码）。
// V1.3 达到「动画讲成故事」：平滑 K 线推进 + 右侧动态解释当前结构状态。
export const STRUCTURE_STORY = {
  index: '03',
  eyebrow: '结构怎么形成',
  title: '用中际旭创近两年的真实日线，\n看结构怎样一步一步被确认。',
  subtitle:
    '播放使用盘迹真实图表和真实结构计算结果。历史演示只用于理解产品，不代表未来走势。',
  instrumentLabel: '中际旭创 · 300308',
  timeframeLabel: '日线 · 近2年',
  dataLabel: '真实历史数据',
  // 播放控制（V1.3：用户理解的是「关键节点」，不再暴露 canonical frame）。
  playLabel: '播放',
  pauseLabel: '暂停',
  replayLabel: '重新播放',
  previousKeyLabel: '上一个关键节点',
  nextKeyLabel: '下一个关键节点',
  // 右侧解释面板标题。
  narrationTitle: '现在发生什么',
  // 进度标签（V1.3：显示百分比，不再显示 32/101 内部编号）。
  progressLabel: '播放进度',
  currentDateLabel: '当前日期',
}

// 真实筹码共识回放（V1.4）：不再使用 synthetic 教学 K 线，
// 而播放近岸蛋白 688137 近 250 个交易日的真实日线 + canonical node_cluster
// （盘迹真实筹码共识计算代码）。播放 42s、64 个真实 node snapshot。
// 原则：只解释 production POC 区间关系（区域重叠/迁移），不用固定价格阈值定义状态。
export const CHIP_CONSENSUS_STORY = {
  index: '04',
  eyebrow: '共识怎么形成',
  title: '用近岸蛋白的真实历史成交，\n看市场共识怎样一点一点迁移。',
  subtitle:
    '成交密集价来自历史成交分布。它描述市场交易最集中的位置，不等同于股东真实持仓成本。',
  instrumentLabel: '近岸蛋白 · 688137',
  timeframeLabel: '日线 · 250个交易日',
  dataLabel: '真实历史数据',
  // 播放控制：关键节点 = narrative beat（不是 canonical frame）。
  playLabel: '播放',
  pauseLabel: '暂停',
  replayLabel: '重新播放',
  previousKeyLabel: '上一个关键节点',
  nextKeyLabel: '下一个关键节点',
  // 右侧解释面板标题。
  narrationTitle: '现在发生什么',
  progressLabel: '播放进度',
  currentDateLabel: '当前日期',
  // 顶部指标条（M17：语义是区域关系，不是百分比）。
  currentPriceLabel: '当前价',
  consensusPriceLabel: '主要成交密集价',
  positionLabel: '位置关系',
  positionAbove: '高于共识区',
  positionInside: '位于共识区',
  positionBelow: '低于共识区',
  distancePrefix: '距共识价',
  // 底部共识轨迹（M19：真实 POC 的离散 step track）。
  consensusTrackLabel: '主要成交密集价轨迹',
  caption: '历史数据演示 · 使用盘迹真实筹码共识计算代码（不构成投资建议）',
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

// 真实玩法案例（StrategyLab V1.5）：不再是「教学式逻辑 + 筛选 chips + 候选漏斗」。
// 改为 4 个玩法 tab + 一张大案例舞台：前三为真实产品截图案例，第四为「更多玩法待探索」。
// 边界（owner 裁决）：不宣称精确实现经典道氏123，统一用「道氏123风格 / 思路」；
// 不虚构候选数量、不声明固定策略；盘迹是一套状态语言，不是一套策略。
export type RealStrategyCase = {
  readonly id: string
  readonly kind: 'case'
  readonly tabLabel: string
  readonly stock: string
  readonly symbol: string
  readonly playbook: string
  readonly imageSrc: string
  readonly imageAlt: string
  readonly lead: string
  readonly what: string
  readonly panji: string
  readonly note: string
}

export type ExploreStrategyCase = {
  readonly id: string
  readonly kind: 'explore'
  readonly tabLabel: string
  readonly playbook: string
  readonly lead: string
  readonly text: string
  readonly examples: readonly string[]
}

export type StrategyCase = RealStrategyCase | ExploreStrategyCase

export const STRATEGY_LAB = {
  index: '06',
  eyebrow: '真实玩法',
  title: '同一套盘迹，\n不止一种玩法。',
  subtitle:
    '趋势、结构、动量、成交量和筹码不是一套固定答案。不同的人，可以用同一套状态语言表达自己的交易思路。',
  cases: [
    {
      id: 'dow123',
      kind: 'case',
      tabLabel: '道氏123风格',
      stock: '国创高新',
      symbol: '002377',
      playbook: '道氏123风格 · 反转确认',
      imageSrc: MARKETING_MEDIA.caseGuochuangDow123,
      imageAlt: '盘迹国创高新道氏123风格历史案例截图',
      lead: '先不猜底，等市场自己给出变化。',
      what: '这个案例用盘迹表达道氏123的思路：先观察原有趋势是否开始失去延续性，再看关键结构有没有改变，最后观察新的方向能不能继续维持。',
      panji: '盘迹不是因为出现一个点就给出答案，而是把趋势、结构、动量和成交量放到一起，让反转确认更容易被观察。',
      note: '历史案例只用于说明方法，不代表后续走势。',
    },
    {
      id: 'trend',
      kind: 'case',
      tabLabel: '趋势跟踪',
      stock: '南亚新材',
      symbol: '688519',
      playbook: '趋势跟踪',
      imageSrc: MARKETING_MEDIA.caseNanyaTrend,
      imageAlt: '盘迹南亚新材趋势跟踪历史案例截图',
      lead: '趋势出现以后，重点不是继续猜还能涨多少，而是判断它有没有结束。',
      what: '主图用趋势变化表达方向，下方成交量和动量帮助观察这段趋势有没有继续得到市场参与。',
      panji: '趋势跟踪关注的是状态还在不在。盘迹把趋势、量能和动量放在同一个工作区持续观察，而不是每天重新预测涨跌。',
      note: '历史案例只用于说明方法，不代表后续走势。',
    },
    {
      id: 'double-bottom',
      kind: 'case',
      tabLabel: '双底支撑',
      stock: '精智达',
      symbol: '688627',
      playbook: '双底支撑 · 形态筛选',
      imageSrc: MARKETING_MEDIA.caseJingzhidaDoubleBottom,
      imageAlt: '盘迹精智达双底支撑历史案例截图',
      lead: '有些机会不是从趋势开始，而是从形态开始。',
      what: '这个案例先从双底和支撑区域找到值得研究的位置，再观察第二次回踩以后，关键区域有没有守住、结构有没有改善。',
      panji: '形态只是入口。盘迹继续用结构、成交量和动量帮助验证这个形态是否仍然值得观察。',
      note: '历史案例只用于说明方法，不代表后续走势。',
    },
    {
      id: 'explore',
      kind: 'explore',
      tabLabel: '更多玩法',
      playbook: '更多玩法，待你来探索',
      lead: '盘迹不是一套固定策略，而是一套描述市场状态的语言。',
      text: '相同的六个维度，可以组合成完全不同的观察方法。你的选股审美，决定盘迹怎么被使用。',
      examples: [
        '板块内寻找机会',
        '新趋势 + 异常放量',
        '筹码重心迁移',
        '结构事件近期发生',
        '趋势 + 量能验证',
        '自己的条件组合',
      ],
    },
  ],
} as const

// 自选 + 通知（Watch + Notify）：流程 + 真实产品产出截图。
export const WATCH_NOTIFY = {
  index: '07',
  eyebrow: '不用一直盯着',
  title: '不用一直盯着盘迹。',
  subtitle: '值得重新看的时候，再把它送到你面前。',
  steps: ['发现候选', '加入自选', '状态变化', '生成研究图片', '推送飞书'],
  // 真实飞书推送研究图（build:marketing-site 拷贝至 /marketing-assets/media/）。
  imageSrc: MARKETING_MEDIA.feishuPoster,
  imageAlt: '盘迹推送到飞书的研究图片（真实产出示意）',
  imageNote: '图片为盘迹真实产出示意，非产品截图合成。',
}

// 营销页对外链接（雪球搜索「小Z说事」）。
export const XUEQIU_SEARCH_URL = 'https://xueqiu.com/k?q=%E5%B0%8FZ%E8%AF%B4%E4%BA%8B'

// [V1.5] 雪球主页精确 URL（Owner 指定，用于社区二维码与整图可点击链接）。
export const XUEQIU_PROFILE_URL =
  'https://xueqiu.com/u/6601870666?scene=1036&share_uid=6601870666'

// 最终 CTA（在大 footer 之前）：只做产品转化 + 社区轻入口。
// [V1.5] 次级 CTA 改为「加入交流」锚点，不再重复雪球搜索入口（社区出口已收束进 Footer 双二维码）。
export const FINAL_CTA = {
  title: '开始使用盘迹',
  subtitle: '盘迹负责压缩信息，不替你做判断。',
  primaryCta: { label: '开始使用', href: '/login' },
  secondaryCta: {
    label: '加入交流',
    href: '#community',
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

// [V1.5] 社区二维码卡：href 存在时整图可点击（PC 用户不扫码也能打开）。
export type FooterCommunityCard = {
  id: string
  title: string
  subtitle: string
  note: string
  imageSrc: string
  imageAlt: string
  href?: string
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
    title: '关注小Z说事',
    desc: '盘迹每日复盘在雪球发布，搜索「小Z说事」即可关注。',
  },
  // [V1.5] 社区出口：两张真实可扫的二维码（QQ 群 + 雪球主页），收束「小Z说事 / QQ 邀请码」。
  //   card.href 存在时整图可点击（PC 用户不扫码也能打开）。
  community: {
    id: 'community',
    title: '一起交流',
    desc: '交流盘迹的使用方法，也可以在群里获取邀请码。',
    cards: [
      {
        id: 'qq',
        title: '加入盘迹交流群',
        subtitle: 'QQ群 · 364121472',
        note: '交流使用方法 · 获取邀请码',
        imageSrc: MARKETING_MEDIA.communityQqQr,
        imageAlt: '小Z说股事QQ群二维码，群号364121472',
      },
      {
        id: 'xueqiu',
        title: '关注小Z说股事',
        subtitle: '雪球 · 每日市场复盘',
        note: '扫码或点击打开雪球主页',
        imageSrc: MARKETING_MEDIA.communityXueqiuQr,
        imageAlt: '小Z说股事雪球主页二维码',
        href: XUEQIU_PROFILE_URL,
      },
    ] as FooterCommunityCard[],
  },
  copyright: '盘迹 · 看一眼就知道怎么用',
}
