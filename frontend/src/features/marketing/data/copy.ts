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
    '先把全市场的变化找出来，\n再把值得继续看的股票留下。',
}

// 顶部导航：5 项真实入口 + 绿色「开始使用」CTA + 品牌副标。
// 锚点指向当前 section；不出现 99/字段级入口。
export const NAV = {
  tagline: '先看懂，再上手',
  items: [
    { label: '产品', href: '#hero' },
    { label: '怎么用', href: '#how-it-works' },
    { label: '真实案例', href: '#strategy-lab' },
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
  feishuPoster: '/marketing-assets/media/panji-watch-research.webp',
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

// [V1.6] 第二屏（不编号）：用户问题视角，放在 Hero 之后、Discovery 之前。
export const AUDIENCE_PROBLEMS = {
  eyebrow: '先说为什么需要它',
  title: '每天真正难的，\n往往不是看不懂一只股票，\n而是根本看不完。',
  subtitle: '你不是没方法，\n是每天几千只股票都在动，时间不够把每一个都研究一遍。',
  problems: [
    { title: '每天不知道先看谁', text: '全市场几千只股票都在动。一只只翻，时间很容易花在根本不值得研究的地方。', answer: '先把范围缩小，再研究。' },
    { title: '有自己的方法，但每天筛一遍太累', text: '你知道自己偏好趋势、结构、放量还是筹码变化，但没必要每天重新从几千只股票开始找。', answer: '把自己的标准留下来。' },
    { title: '自选越来越多，盘中根本盯不过来', text: '上班、开会、通勤时不可能一直看屏幕。自选十几只、几十只，不可能同时盯。', answer: '有变化的时候再回来。' },
    { title: '不想让软件替你下结论', text: '你需要的是更快找到候选、看清它现在是什么状态，不是让一个分数告诉你应该买还是卖。', answer: '判断仍然自己做。' },
  ],
  fit: ['每天看很多股票的人', '已经有自己选股方法的人'],
} as const

export const HERO = {
  eyebrow: '盘迹 · 全市场状态终端',
  title: '先把全市场的变化找出来，\n再盯真正值得盯的。',
  subtitle:
    '趋势、结构、动量、成交量、筹码、事件——\n盘迹先帮你把范围缩下来。\n最后看哪只、怎么做，按你自己的方法。',
  // 六维 proof：不编造数字，只描述盘迹看什么
  proof: {
    label: '看一只股票，盘迹主要看这六件事',
    dims: ['趋势', '结构', '动量', '成交量', '筹码', '事件'],
  },
  primaryCta: { label: '开始使用', href: '/login' },
  // 次级 CTA：真实锚点，无时长徽章（无 0:19 视频）
  secondaryCta: { label: '看盘迹怎么工作', href: '#how-it-works' },
  // 诚实状态条：真实产品界面 + 案例均来自历史数据
  statusBadges: [
    { dot: 'green', text: '真实产品界面' },
    { dot: 'green', text: '案例均来自历史数据' },
  ] as readonly HeroStatusBadge[],
}

// 三种机会入口：全市场 / 板块 / 小Z说事。
// [V1.4] 三卡严格同构（编号 + 标题 + 三步 flow + 解释）：一律用 mini flow，entries 无 media 字段。
// [V1.5] 独立 XiaozToPanji section 已删除，雪球不再独占页面；社区出口统一收束进 Footer 双二维码。
export const DISCOVERY = {
  index: '01',
  eyebrow: '机会从哪来',
  title: '今天看什么，可以从三个地方找。',
  subtitle:
    '没方向，就先扫全市场；\n有方向，就进板块；\n看到一条值得跟的复盘，\n也可以顺着方向继续往下找。',
  entries: [
    {
      key: 'market',
      title: '全市场找变化',
      desc: '先看今天哪些股票真的变了，\n别从几千只里一只只翻。',
      flow: ['扫全市场', '找出变化', '留下候选'],
    },
    {
      key: 'section',
      title: '从板块里找',
      desc: '已经知道今天想看哪个方向，\n就直接在板块里缩范围。',
      flow: ['确定板块', '按条件筛', '挑出个股'],
    },
    {
      key: 'story',
      title: '从复盘里找',
      desc: '小Z说股事先帮你看\n市场今天在交易什么，\n盘迹再帮你往个股里找。',
      flow: ['看复盘', '找到方向', '进盘迹筛个股'],
    },
  ],
}

// 产品方法论：6 步 icon-grid（id=how-it-works 供次级 CTA 锚定）。
// 与 IA 流程一致：发现 → 筛选 → 理解 → 加入自选 → 持续跟踪 → 状态提醒。
export const WORKFLOW = {
  index: '02',
  eyebrow: '怎么用',
  id: 'how-it-works',
  title: '每天其实就做这几步。',
  subtitle: '先找到变化，筛到几只，\n看懂状态，放进自选。\n以后只有状态变了，\n再回来处理。',
  steps: [
    { key: 'discover', icon: 'radar', title: '发现', desc: '先知道今天哪里在动。' },
    { key: 'filter', icon: 'sliders', title: '筛选', desc: '按自己的条件，把范围缩下来。' },
    { key: 'understand', icon: 'eye', title: '理解', desc: '看清这只票现在是什么状态。' },
    { key: 'watchlist', icon: 'bookmark', title: '加入自选', desc: '值得继续看的，就先留下。' },
    { key: 'track', icon: 'pulse', title: '持续跟踪', desc: '不用每天重新翻一遍。' },
    { key: 'notify', icon: 'bell', title: '状态提醒', desc: '有变化，再把它送到你面前。' },
  ],
}

// 真实结构回放（V1.3）：不再使用 synthetic 教学 K 线，
// 而播放中际旭创 300308 近两年真实日线 + canonical SMC（盘迹真实结构计算代码）。
// V1.3 达到「动画讲成故事」：平滑 K 线推进 + 右侧动态解释当前结构状态。
export const STRUCTURE_STORY = {
  index: '03',
  eyebrow: '结构怎么走出来',
  title: '结构不是一根K线突然变出来的。\n用中际旭创，看它怎么一步步走出来。',
  subtitle:
    '承接、压制、突破、转强或转弱，都要经过过程。\n这里按真实历史逐步重放，只看结构是怎么变化的。',
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
  eyebrow: '成交重心怎么挪',
  title: '股价先走，成交重心不一定马上跟。\n用近岸蛋白，看它怎么从35附近移到45附近。',
  subtitle:
    '股价涨上去了，不等于大家已经在新的位置充分成交。\n等新的区域真正堆出成交量，\n主要成交密集区才会跟着挪过去。',
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
  caption: '主要成交密集价来自历史成交分布，\n不等于股东真实持仓成本。',
}

// 盘迹产品语言六维度（与产品页一致，营销侧不重新定义）。
// 视觉：居中一句 + 横向六维（不再六个同样的 card）。
export const MARKET_LANGUAGE = {
  index: '05',
  eyebrow: '看一只票',
  title: '盘迹主要看六件事。',
  centerSentence: '六项分开看，\n比最后凑成一个总分更有用。',
  subtitle: '趋势往哪走，结构有没有变，\n动量强不强，量有没有跟，\n筹码在哪，最近又发生了什么。',
  dimensions: [
    { key: 'trend', title: '趋势', desc: '方向在哪，走了多久。' },
    { key: 'structure', title: '结构', desc: '高低点怎么走，关键位置有没有变。' },
    { key: 'momentum', title: '动量', desc: '这段推进是在变强，还是在变弱。' },
    { key: 'volume', title: '成交量', desc: '这次变化，有没有量跟上。' },
    { key: 'chip', title: '筹码', desc: '成交最密集的位置，现在在哪。' },
    { key: 'event', title: '事件', desc: '最近有没有值得重新看的变化。' },
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
    subtitle: '99个字段按8个维度整理。\n先看分组，需要时再查具体定义。',
    closeLabel: '关闭',
    searchPlaceholder: '搜索字段…',
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
  readonly closing: string
}

export type StrategyCase = RealStrategyCase | ExploreStrategyCase

export const STRATEGY_LAB = {
  index: '06',
  eyebrow: '真实案例',
  title: '盘迹怎么用？\n先看三个真实例子。',
  subtitle:
    '有人等反转确认，\n有人顺着趋势跟，\n也有人先从形态里找机会。\n工具一样，用法可以很不一样。',
  cases: [
    {
      id: 'dow123',
      kind: 'case',
      tabLabel: '道氏123思路',
      stock: '国创高新',
      symbol: '002377',
      playbook: '道氏123思路',
      imageSrc: MARKETING_MEDIA.caseGuochuangDow123,
      imageAlt: '盘迹国创高新道氏123思路历史案例截图',
      lead: '这张图先看一件事：原来的下跌还在不在。',
      what: '前面的下降趋势没被破坏之前，不急着猜底。\n\n等低点不再往下，关键结构被重新抬起来，再看新的方向能不能接上。',
      panji: '盘迹把趋势和结构的变化直接标在图上。\n\n这里看到的是原趋势被破坏、结构开始转向的过程，不是一个「反转已经完成」的结论。',
      note: '历史案例只用来说明盘迹怎么观察，不代表之后会怎么走。',
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
      lead: '这类票不找底，重点是跟住已经走出来的趋势。',
      what: '趋势向上时，主要看高低点有没有继续抬高，成交量和动量有没有明显掉下来。\n\n状态没坏，就不用每天重做一次判断。',
      panji: '盘迹把趋势、量能和动量放在一张图里。\n\n哪一项开始变弱，会比只盯每天的涨跌更容易看出来。',
      note: '历史案例只用来说明盘迹怎么观察，不代表之后会怎么走。',
    },
    {
      id: 'double-bottom',
      kind: 'case',
      tabLabel: '双底支撑',
      stock: '精智达',
      symbol: '688627',
      playbook: '双底支撑',
      imageSrc: MARKETING_MEDIA.caseJingzhidaDoubleBottom,
      imageAlt: '盘迹精智达双底支撑历史案例截图',
      lead: '双底只是把它放进候选，第二次回踩以后发生什么才更重要。',
      what: '两次回到相近区域，第二次没有继续破低，随后价格重新往上走。\n\n这才是这张图里真正值得看的地方。',
      panji: '可以先按形态找到它，再用结构、成交量和动量继续验证。\n\n盘迹不会替你判定「双底一定成立」。',
      note: '历史案例只用来说明盘迹怎么观察，不代表之后会怎么走。',
    },
    {
      id: 'explore',
      kind: 'explore',
      tabLabel: '更多用法',
      playbook: '还有很多用法，可以自己组合。',
      lead: '盘迹不规定一套标准答案。',
      text: '有人看趋势，有人看结构，\n也有人只想找突然放量、\n筹码迁移或刚发生结构变化的股票。\n\n六个维度都在，\n按自己的习惯组合就行。',
      examples: [
        '板块里找相对强的',
        '新趋势刚起来又放量',
        '筹码重心刚发生迁移',
        '结构刚出现变化',
        '趋势没坏但量能变弱',
        '自己组合条件',
      ],
      closing: '用法可以不同，\n判断标准由你自己定。',
    },
  ],
} as const

// 自选 + 通知（Watch + Notify）：流程 + 真实产品产出截图。
export const WATCH_NOTIFY = {
  index: '07',
  eyebrow: '盘中监控',
  title: '不方便盯盘的时候，\n只在真正有变化时回来。',
  subtitle: '把真正值得跟的留在自选，\n盘迹持续跟踪状态；有变化，再把研究图送到飞书。',
  promise: '你不用一直盯着屏幕。\n盘迹盯的是状态有没有变化。',
  scenarios: [
    { title: '人在上班，手没空看盘', text: '上班、开会、通勤时，不用隔几分钟切回来刷一次行情。' },
    { title: '自选十几只、几十只', text: '不可能一直盯着每一只。把真正需要跟踪的留下，盘迹继续看它们的状态。' },
    { title: '只想等值得重新研究的时刻', text: '没变化就不用处理；状态变了，再打开研究图看发生了什么。' },
  ],
  flow: ['加入自选','盘中持续跟踪','状态变化','生成研究图','飞书收到'],
  imageSrc: MARKETING_MEDIA.feishuPoster,
  imageAlt: '盘迹状态变化后发送到飞书的真实研究图片',
  imageNote: '盘迹真实研究图示例。',
} as const


export const XUEQIU_PROFILE_URL =
  'https://xueqiu.com/u/6601870666?scene=1036&share_uid=6601870666'

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

// 内容入口唯一对外链接：雪球主页精确 URL（XUEQIU_PROFILE_URL）。

export const FOOTER = {
  cta: {
    title: '先用几天，再看它合不合你的方法。',
    subtitle: '能不能少翻一点、看清一点，用自己的股票试最清楚。',
    button: { label: '开始使用', href: '/login' },
  },
  brand: BRAND,
  columns: [
    { title: '产品', links: [{ label: '行情', href: '/market' }, { label: '自选', href: '/market?scope=watchlist' }] },
    { title: '内容', links: [
        { label: '小Z说股事', href: XUEQIU_PROFILE_URL, external: true },
        { label: '关注每日早晚复盘', note: '每日早晚各一篇，讲当天市场发生了什么。' },
    ] },
  ] as FooterColumn[],
  community: {
    id: 'community',
    cards: [
      { id: 'qq', title: '盘迹交流群', subtitle: 'QQ群 364121472', note: '交流使用方法 · 获取邀请码',
        imageSrc: MARKETING_MEDIA.communityQqQr, imageAlt: '小Z说股事QQ群二维码，群号364121472' },
      { id: 'xueqiu', title: '小Z说股事', subtitle: '雪球 · 每日复盘', note: '扫码或点击打开主页',
        imageSrc: MARKETING_MEDIA.communityXueqiuQr, imageAlt: '小Z说股事雪球主页二维码', href: XUEQIU_PROFILE_URL },
    ] as FooterCommunityCard[],
  },
  copyright: '盘迹 · 看一眼就知道怎么用',
} as const
