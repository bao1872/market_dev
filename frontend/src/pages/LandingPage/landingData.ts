// [门户] - 描述: 盘迹门户页演示数据（非实时行情，仅用于展示动画与文案）
// 所有数据均为硬编码演示用途，不调用任何受保护 API

// ===== Hero 区场景：3 种典型价格变化 =====
export interface Scenario {
  title: string
  total: number
  eventTime: string
  triggerType: 'breakout' | 'fail' | 'rebound'
}

export const scenarios: Scenario[] = [
  { title: '突破成交集中区，继续创新高', total: 72, eventTime: '14:12', triggerType: 'breakout' },
  { title: '上涨到达集中区，但未能突破', total: 72, eventTime: '13:48', triggerType: 'fail' },
  { title: '回踩下方集中区，再向上反弹', total: 78, eventTime: '14:26', triggerType: 'rebound' },
]

// ===== 飞书通知区：3 只示例股票详情 =====
export interface DetailCase {
  name: string
  code: string
  type: 'breakout' | 'fail' | 'rebound'
  price: string
  change: string
  state: string
  zone: string
  event: string
  logic: string
  // 消息卡片元数据
  notifyTime: string
  consensus: string
  messageLogic: string
}

export const detailCases: DetailCase[] = [
  {
    name: '示例股票 A',
    code: '60****',
    type: 'breakout',
    price: '26.18',
    change: '+4.82%',
    state: '突破高共识度区',
    zone: '股价共识度区 24.60–25.10',
    event: '放量突破上沿并创出阶段新高',
    logic: '行业需求向上，订单兑现开始加速；重点验证业绩增速能否继续抬升。',
    notifyTime: '14:12',
    consensus: '24.60–25.10',
    messageLogic: '行业需求向上，订单兑现开始加速；重点验证业绩增速能否继续抬升。',
  },
  {
    name: '示例股票 B',
    code: '30****',
    type: 'fail',
    price: '18.46',
    change: '-1.36%',
    state: '上冲后未能站稳',
    zone: '股价共识度区 18.30–18.75',
    event: '冲击上沿后回落，未能形成有效突破',
    logic: '供需改善仍待订单验证，短期催化已有部分定价；机会高度取决于利润兑现。',
    notifyTime: '13:48',
    consensus: '18.30–18.75',
    messageLogic: '供需改善仍待订单验证，短期催化已有部分定价；机会高度取决于利润兑现。',
  },
  {
    name: '示例股票 C',
    code: '00****',
    type: 'rebound',
    price: '34.88',
    change: '+2.15%',
    state: '回踩后重新向上',
    zone: '股价共识度区 31.40–32.10 / 35.20–36.00',
    event: '回踩下方区域企稳，重新接近上方共识区',
    logic: '基本面趋势未变，订单节奏稳定；继续观察上方空间能否被打开。',
    notifyTime: '14:26',
    consensus: '31.40–32.10 / 35.20–36.00',
    messageLogic: '基本面趋势未变，订单节奏稳定；继续观察上方空间能否被打开。',
  },
]

// ===== 工作流：3 阶段步骤 =====
export interface WorkflowStep {
  index: number
  title: string
  desc: string
  output: string
}

export const workflowSteps: WorkflowStep[] = [
  { index: 1, title: '机会发现', desc: '通过图形筛选，找到值得研究的股票', output: '候选目标' },
  { index: 2, title: '机会验证', desc: '用产业逻辑判断机会的斜率和高度', output: '是否值得追踪' },
  { index: 3, title: '持续追踪', desc: '验证通过后，持续跟踪关键变化', output: '变化提醒' },
]

export interface WorkflowCopy {
  label: string
  title: string
  desc: string
  bullets: string[]
  terminal: string
  state: string
  output: string
  next: string
}

export const workflowCopy: WorkflowCopy[] = [
  {
    label: '机会发现',
    title: '先用图形，从全市场中发现候选',
    desc: '图形不是结论，而是研究的入口。盘迹先把结构发生变化的股票筛出来，交给下一步产业验证。',
    bullets: ['从全市场中缩小研究范围', '识别值得进一步研究的价格结构', '发现后进入产业逻辑验证'],
    terminal: '图形筛选',
    state: '发现候选目标',
    output: '候选目标清单',
    next: '进入产业验证 →',
  },
  {
    label: '机会验证',
    title: '再用产业逻辑，判断机会的斜率和高度',
    desc: '斜率看需求、订单和业绩兑现得有多快；高度看市场空间、竞争格局和预期差还能走多远。图形负责发现，产业逻辑负责决定值不值得追。',
    bullets: ['斜率：判断业绩兑现速度', '高度：判断空间与预期差', '逻辑不成立，不进入持续追踪'],
    terminal: '产业逻辑验证',
    state: '评估斜率与高度',
    output: '验证结论与空间判断',
    next: '通过后加入追踪 →',
  },
  {
    label: '持续追踪',
    title: '验证通过后，再交给盘迹持续追踪',
    desc: '已经想清楚逻辑和空间的股票，才进入观察池。盘迹持续更新价格与股价共识度区，真正出现关键变化时再通知你。',
    bullets: ['只追踪已经验证过的目标', '持续更新股价共识度区', '变化发生后直达个股详情'],
    terminal: '目标追踪',
    state: '等待关键变化',
    output: '持续监控与变化提醒',
    next: '形成完整闭环',
  },
]

// ===== 产业验证链条：5 个节点 =====
export interface IndustryNode {
  index: string
  small: string
  strong: string
  em: string
  delay: number
}

export const industryNodes: IndustryNode[] = [
  { index: '01', small: '需求变化', strong: '行业景气是否真的向上', em: '终端需求、价格、产能利用率', delay: 0 },
  { index: '02', small: '客户验证', strong: '公司是否进入核心供应链', em: '客户结构、认证进度、份额变化', delay: 180 },
  { index: '03', small: '订单兑现', strong: '需求能否变成真实收入', em: '订单节奏、交付能力、价格传导', delay: 360 },
  { index: '04', small: '业绩释放', strong: '利润增速能否持续抬升', em: '收入、毛利率、经营杠杆', delay: 540 },
  { index: '05', small: '估值空间', strong: '市场还剩多少预期差', em: '行业空间、竞争格局、当前定价', delay: 720 },
]

export interface IndustryVerdict {
  small: string
  strong: string
  span: string
  cls?: string
}

export const industryVerdicts: IndustryVerdict[] = [
  { small: '机会斜率', strong: '看需求到业绩的传导速度', span: '订单与利润兑现加快', cls: 'slopeCard' },
  { small: '机会高度', strong: '看空间、份额与预期差', span: '仍有继续验证的空间', cls: 'heightCard' },
  { small: '验证结论', strong: '逻辑成立，进入持续追踪', span: '未通过的目标不会进入观察池', cls: 'validationResult' },
]

// ===== 特性条：4 项核心能力 =====
export interface Feature {
  iconCls: string
  title: string
  desc: string
}

export const features: Feature[] = [
  { iconCls: '', title: '关注重点更清晰', desc: '全市场筛选，找到值得跟踪的股票' },
  { iconCls: '', title: '关键区域自动盯', desc: '成交密集区监控，自动识别价格行为' },
  { iconCls: 'bell', title: '变化通知不遗漏', desc: '进入关键区域、突破、回撤，及时提醒' },
  { iconCls: 'clock', title: '记录完整可追溯', desc: '每一次触发都有记录，方便复盘' },
]

// ===== 价格区：2 档套餐 =====
// 套餐展示名与监控限额由后端 GET /plans 动态提供，此处仅保留价格与 key 映射
export interface PricingPlan {
  key: string
  monthly: number
  yearly: number
}

export const pricingPlans: PricingPlan[] = [
  { key: 'observe', monthly: 50, yearly: 480 },
  { key: 'research', monthly: 100, yearly: 960 },
]

// ===== 顶部导航锚点 =====
export interface NavLink {
  href: string
  label: string
  active?: boolean
}

export const navLinks: NavLink[] = [
  { href: '#home', label: '首页', active: true },
  { href: '#capability', label: '核心能力' },
  { href: '#audience', label: '适合谁用' },
  { href: '#workflow', label: '如何工作' },
  { href: '#about', label: '关于我们' },
]

// ===== 更新记录 =====
export interface UpdateRecord {
  version: string
  note: string
  date: string
}

export const updateRecords: UpdateRecord[] = [
  { version: 'v0.3.0', note: '筹码密集区算法优化', date: '2024-06-01' },
  { version: 'v0.2.0', note: '自选监控支持自定义区域', date: '2024-05-15' },
]

// ===== 法律条款：服务协议 / 隐私政策 / 风险提示 =====
export type LegalType = 'terms' | 'privacy' | 'risk'

export interface LegalDoc {
  title: string
  html: string
}

export const legal: Record<LegalType, LegalDoc> = {
  terms: {
    title: '服务协议',
    html: `<p>欢迎使用盘迹。盘迹向用户提供市场数据整理、全市场筛选、自选股票跟踪、事件记录与消息提醒等软件服务。用户在注册、登录或使用服务前，应当完整阅读并同意本协议。</p><h4>一、服务内容</h4><p>盘迹根据公开或依法取得的市场数据，按照既定计算规则生成数据展示、筛选结果和监控记录。盘迹不接受用户委托操作证券账户，不代替用户作出投资决策，也不对任何证券未来价格表现作出承诺。</p><h4>二、账户与使用规则</h4><p>用户应提供真实、准确、有效的注册信息，并妥善保管账户和登录凭证。用户不得利用盘迹从事违法活动、攻击系统、绕过权限限制、批量抓取数据或侵犯他人合法权益。</p><h4>三、订阅与费用</h4><p>正式上线后，收费项目、服务期限、监控数量和续费方式以购买页面展示为准。除法律另有规定或平台明确承诺外，已实际提供的数字化服务不适用无理由退款。因平台原因导致服务长期无法使用的，平台将根据实际影响提供补偿或退款方案。</p><h4>四、服务变更与中断</h4><p>行情源、网络、服务器、第三方通知渠道或不可抗力可能造成数据延迟、中断或错误。平台将采取合理措施恢复服务，但不保证服务始终无中断或完全无误。</p><h4>五、责任边界</h4><p>用户应独立核验信息并自行承担投资决策及结果。盘迹不因用户依据数据展示、筛选结果、提醒记录或相关内容作出的交易行为承担投资损失责任。</p>`,
  },
  privacy: {
    title: '隐私政策',
    html: `<p>盘迹重视个人信息保护，并遵循合法、正当、必要和诚信原则处理个人信息。</p><h4>一、我们可能收集的信息</h4><p>为完成注册、登录、订阅、客户支持和安全保障，正式服务可能收集手机号、电子邮箱、登录日志、设备与浏览器信息、订阅记录、用户主动创建的自选股和备忘录。除非用户主动提供或法律另有要求，平台不收集证券账户密码、交易密码或银行卡密码。</p><h4>二、处理目的与范围</h4><p>相关信息仅用于提供账户服务、执行用户选择的监控任务、发送提醒、处理订单、保障系统安全、改进产品体验及履行法定义务。平台不会以与上述目的无关的方式过度收集信息。</p><h4>三、共享与第三方服务</h4><p>正式服务可能使用云服务器、支付、短信、邮件或消息通知等第三方服务。平台将根据适用法律与合作方约定数据保护义务，并在正式上线前公布具体第三方清单。</p><h4>四、保存与安全</h4><p>信息将在实现处理目的所需的最短期限内保存。平台将采取访问控制、传输加密、日志审计和备份等合理措施保护信息，但互联网环境无法保证绝对安全。</p><h4>五、用户权利</h4><p>用户可以依法查询、更正、复制、删除个人信息，撤回授权或注销账户。相关请求可通过正式版公布的客服渠道提交。</p>`,
  },
  risk: {
    title: '风险提示',
    html: `<p>使用盘迹前，请充分理解以下风险：</p><h4>一、投资风险</h4><p>证券市场价格可能大幅波动。盘迹展示的市场数据、指标、筹码密集区、筛选结果和监控记录均不构成证券投资建议、收益承诺或买卖指令。</p><h4>二、模型与数据风险</h4><p>任何量化计算均基于历史与当前数据，不能保证预测未来。数据源可能出现缺失、复权差异、延迟、异常或错误，计算结果也可能因市场结构变化而失效。</p><h4>三、提醒风险</h4><p>站内、短信、邮件或第三方消息通知可能因网络、设备、账号设置或第三方服务异常而延迟或未送达。用户不应将提醒作为执行交易的唯一依据。</p><h4>四、用户责任</h4><p>用户应根据自身风险承受能力、资金状况和投资目标独立判断，并对自己的投资决策与结果承担责任。在作出重大投资决定前，建议咨询具备合法资质的专业机构或人士。</p>`,
  },
}

// ===== 工作流产业验证面板状态文案 =====
export const industryPanelHead = {
  small: '机会验证',
  h4: '产业逻辑决定机会的斜率和高度',
  status: '验证进行中',
}
