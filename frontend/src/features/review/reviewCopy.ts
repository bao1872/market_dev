// [ReviewCopy] - 描述: Review 用户可见中文字案与 tooltip 集中 owner（REVIEW-UX-CN-01）
//
// 合同：
// - 只做展示层映射：中文 label / tooltip / phase 展示 label / readiness 展示 label / 排序 label / tab label。
// - canonical 值（URL 参数、API 字段、enum 值、group_key、字段 key）一律保持不变；
//   用户看到的英文/中文只通过本文件 + ReviewTerm 的展示映射产生。
// - 禁止价值判断词（利好/利空/机会/风险/健康/聪明钱/看多/看空/强烈建议），tooltip 只解释
//   指标测什么、方向/量级怎么读（facts 中立措辞），不引入新 score。
// - 纯 TS（无 React / SCSS 依赖），可被 node --test 直接运行。
//
// 边界：本文件不包含 canonical 8-group（price_capital/trend_state/...）的中文 label map，
// 那部分必须来自 backend group.label（R3B §7）。这里只包含 UI IA 标题与展示术语。
//
// [REVIEW-UX-TERMINOLOGY-01 / T1] 术语语义校正：
// - Position 不是"股价历史位置"，是"当前等权涨跌幅相对自身历史观察的分位"；
// - Velocity / Acceleration 不是价格的一阶/二阶导数，是分位序列的动能与动能偏离；
// - Capital Tilt 不是净资金流，是两种加权方式下的表现差；
// - Leadership 是算法主导成员集合，不是市场"龙头股"评价。
// canonical 字段名（position/velocity/acceleration/capital_tilt/migration）一律不变。

export interface ReviewTerm {
  /** 中文显示名 */
  label: string
  /** 中性事实性 tooltip 解释（可选；无 help 时 ReviewTerm 只渲染 label） */
  help?: string
}

/** 主要研究术语：label + 可选 tooltip（由 ReviewTerm 统一渲染） */
export const REVIEW_TERMS = {
  scope: {
    label: '板块',
    help: '当前复盘对象，可以是一级行业、二级行业、三级行业或概念板块',
  },
  phase: {
    label: '动态阶段',
    help: '根据板块等权涨跌分位及其分位动能、动能偏离状态，由后端计算得到的当前阶段',
  },
  position: {
    label: '等权涨跌分位',
    help: '当前等权涨跌幅在自身历史观察窗口中的相对分位，范围 0–100。'
      + '数值越高表示当前等权涨跌幅处于自身历史较高区域；'
      + '它描述的是涨跌幅的分位，不是股价位置，也不表示上涨概率',
  },
  velocity: {
    label: '分位动能',
    help: 'EMA5(等权涨跌分位) - EMA20(等权涨跌分位)，'
      + '即近期分位快速平滑水平相对较慢历史基准的差异。'
      + '正值表示近期分位水平高于较慢基准，负值表示低于较慢基准。',
  },
  acceleration: {
    label: '动能偏离',
    help: '分位动能 - EMA5(分位动能)，'
      + '即当前分位动能相对其自身近期均值的偏离。用于观察分位上移或下移的动能是否正在增强或衰减',
  },
  equalWeightReturn: {
    label: '等权涨跌幅',
    help: '每只成员股票赋予相同权重后的平均涨跌幅，避免少数大市值成员主导结果',
  },
  capitalTilt: {
    label: '成交加权差',
    help: '成交额加权涨跌幅 - 等权涨跌幅。'
      + '正值表示成交额权重较高的成员整体表现高于等权平均，负值表示低于等权平均。'
      + '它是不同加权口径下的表现差，不表示净资金流入或流出。',
  },
  breadth: {
    label: '成员涨跌分布',
    help: '板块内上涨、下跌和平盘成员的占比',
  },
  leadershipMigration: {
    label: '主导集合更替率',
    help: '前一交易日与当前交易日主导成员集合的变化程度。'
      + '只描述集合是否发生更替，不评价更替的好坏',
  },
  coverage: {
    label: '成员数据覆盖率',
    help: '当前指标实际获得有效数据的成员，占应有成员的比例',
  },
  freshness: {
    label: '近期技术事件',
    help: '最近发生的技术事件数量与时间衰减后的密度',
  },
  technical: {
    label: '技术强度结构',
    help: '技术强度的集中度（强度集中度）、前5强度占比、技术强度最高成员与中位成员差距等事实汇总',
  },
  amountWeightedReturn: {
    label: '成交额加权涨跌幅',
    help: '按成员成交额分配权重后的板块涨跌幅',
  },
  totalVolume: {
    label: '总成交量',
    help: '当前板块有效成员的成交量合计',
  },
  totalAmount: {
    label: '总成交额（百亿元）',
    help: '当前板块有效成员的成交额合计；展示单位百亿元（1 百亿元 = 10^10 元，canonical 单位为元）',
  },
  priceConcentration: {
    label: '涨跌幅集中度',
    help: '衡量成员当日涨跌幅绝对变化的分布是否集中在少数成员上（权重来自 abs(return_1d) / Σabs(return_1d)）',
  },
  amountConcentration: {
    label: '成交额集中度',
    help: '衡量成交额是否集中在少数成员上',
  },
  rawHhi: {
    label: '原始 HHI',
    help: '底层指标为 HHI（Herfindahl–Hirschman Index）。数值越高表示分布越集中在少数成员',
  },
  normalizedHhi: {
    label: '标准化 HHI',
    help: '对成员数量影响进行标准化后的 HHI，更适合不同规模板块之间比较',
  },
  sampleCount: {
    label: '有效成员数',
    help: '本次集中度计算实际使用的有效成员数量',
  },
  status: {
    label: '数据状态',
    help: '当前事实的状态：可用、历史数据不足或当日数据不可用',
  },
  phaseCurrent: {
    label: '当前动态阶段',
    help: '根据板块等权涨跌分位及其分位动能、动能偏离状态，由后端计算得到的当前阶段',
  },
  upperOccupancy: {
    label: '近20日高分位占比',
    help: '固定 20 日窗口内处于高分位区域的占比，直接展示后端计算结果',
  },
  lowerOccupancy: {
    label: '近20日低分位占比',
    help: '固定 20 日窗口内处于低分位区域的占比，直接展示后端计算结果',
  },
  // ===== [Slice C] Explorer 原子化列词条（复合 Breadth/Freshness/Technical 拆分后新增）=====
  advanceRatio: {
    label: '上涨占比',
    help: '板块内当日上涨成员占比（persisted advance_ratio），缺失显示 —',
  },
  declineRatio: {
    label: '下跌占比',
    help: '板块内当日下跌成员占比（persisted decline_ratio），缺失显示 —',
  },
  unchangedRatio: {
    label: '平盘占比',
    help: '板块内当日平盘成员占比（persisted unchanged_ratio），缺失显示 —',
  },
  freshnessDensity: {
    label: '事件密度',
    help: '近期技术事件的衰减加权密度（persisted decay_weighted_density），缺失显示 —',
  },
  freshnessTodayCount: {
    label: '今日事件数',
    help: '当日技术事件计数（persisted today_count）；0 是有效零事件，不是缺失',
  },
  technicalHhi: {
    label: '技术集中度',
    help: '技术状态强度的 HHI 集中度（persisted hhi），缺失显示 —',
  },
  technicalTop5Ratio: {
    label: '前5强度占比',
    help: '前 5 成员技术强度占比（numerator / denominator，由前端 ViewModel 单一 owner 换算）',
  },
  technicalLeaderMedianGap: {
    label: '最高-中位强度差',
    help: '最高技术强度成员与中位强度之差（persisted leader_median_gap），缺失显示 —',
  },
  technicalLeaderSymbol: {
    label: '最高强度成员',
    help: '技术强度最高的成员展示符号（字符串，不参与数值排序）',
  },
  returnCapital: {
    label: '涨跌与成交',
    help: '板块等权涨跌幅、成交额加权涨跌幅与成交加权差',
  },
  concentration: {
    label: '集中度',
    help: '衡量板块内成员在价格或成交额侧的分布集中程度',
  },
  priceHhi: {
    label: '涨跌幅集中度',
    help: '衡量成员当日涨跌幅绝对变化的分布是否集中在少数成员上（权重来自 abs(return_1d) / Σabs(return_1d)）',
  },
  amountHhi: {
    label: '成交额集中度',
    help: '衡量成交额是否集中在少数成员上',
  },
  returnDispersion: {
    label: '涨跌离散度',
    help: '板块内部成员涨跌幅之间的分散程度。数值越大，说明成员之间表现差异越明显',
  },
  dispersion: {
    label: '涨跌离散度',
    help: '成员涨跌幅之间的分散程度，数值越大差异越明显',
  },
  advance: {
    label: '上涨',
    help: '板块内上涨成员占有效成员的比例',
  },
  decline: {
    label: '下跌',
    help: '板块内下跌成员占有效成员的比例',
  },
  unchanged: {
    label: '平盘',
    help: '板块内平盘成员占有效成员的比例',
  },
  tMinus1Leaders: {
    label: '前日主导成员',
    help: '前一交易日的主导成员集合',
  },
  tLeaders: {
    label: '当日主导成员',
    help: '当前交易日的主导成员集合',
  },
  direction: {
    label: '板块方向',
    help: '前一日与当前交易日板块整体方向，'
      + '由板块等权涨跌幅的方向决定（+1 上涨 / -1 下跌），'
      + '不是主导成员自身涨跌方向',
  },
  prevDir: {
    label: '前日方向',
    help: '前一交易日板块整体方向，来源于板块等权涨跌幅方向',
  },
  currDir: {
    label: '当日方向',
    help: '当前交易日板块整体方向，来源于板块等权涨跌幅方向',
  },
  transition: {
    label: '主导成员变化',
    help: '前一交易日到当前交易日主导成员的留存、新进入与退出',
  },
  retained: {
    label: '留存成员',
    help: '前一日与当日均属于主导成员集合的成员',
  },
  entrants: {
    label: '新进入成员',
    help: '当日新进入主导成员集合、前一日不在集合中的成员',
  },
  exits: {
    label: '退出成员',
    help: '前一日在主导成员集合中、当日退出集合的成员',
  },
  metrics: {
    label: '集合变化指标',
    help: '衡量主导成员集合变化程度的指标',
  },
  prevRetention: {
    label: '前日主导留存率',
    help: '前一交易日主导成员中当日仍留在集合中的比例',
  },
  jaccardStability: {
    label: '主导集合重合度',
    help: '比较前一日和当日主导成员集合的重合程度。越高表示集合越稳定',
  },
  migration: {
    label: '主导集合更替率',
    help: '描述主导成员集合发生变化的程度。越高表示主导成员更替越明显，'
      + '只描述变化大小，不评价好坏',
  },
  // ---- P0-4/P0-5：趋势 / 量能指标中文简称 + 解释（单一展示 owner，统一 tooltip） ----
  trendStrength: {
    label: '趋势强度',
    help: '衡量当前趋势方向的强弱程度。数值越高表示趋势方向越明确、持续性越强',
  },
  dsaVwapDev: {
    label: '趋势段 VWAP 偏离',
    help: '当前趋势连续段相对 VWAP 均价的偏离百分比。正值表示价格位于均价上方，负值表示下方',
  },
  segmentBars: {
    label: '趋势段长度',
    help: '当前趋势连续段包含的 K 线数量，反映趋势已持续的时间长度',
  },
  segmentChange: {
    label: '趋势段涨跌幅',
    help: '当前趋势连续段内的累计涨跌幅',
  },
  segmentSlope: {
    label: '趋势段斜率',
    help: '趋势连续段的价格变化速率，反映趋势推进的陡缓',
  },
  vwapRetTotal: {
    label: 'VWAP 累计收益',
    help: '自趋势段开始以来相对 VWAP 均价的累计收益',
  },
  volumeRatio: {
    label: '成交量比',
    help: '当前成交量与近期平均成交量的比值，反映成交量相对活跃程度',
  },
  amountRatio: {
    label: '成交额比',
    help: '当前成交额与近期平均成交额的比值，反映成交额相对活跃程度',
  },
  zScore: {
    label: 'Z 分数',
    help: '指标值相对其历史分布的标准化偏离程度（标准差倍数），用于判断当前处于常态还是极端',
  },
  bbPosition: {
    label: '布林带位置',
    help: '当前价格处在布林带中的相对位置（0=下轨，1=上轨）',
  },
  bbWidth: {
    label: '布林带宽度',
    help: '布林带上下轨之间的宽度，反映波动扩张或收缩',
  },
  vwapTotalReturn: {
    label: 'VWAP 累计收益',
    help: '相对 VWAP 均价的累计收益',
  },
  percentile: {
    label: '历史分位',
    help: '指标值在其历史分布中所处的百分位置（0–100），用于判断当前处于常态还是极端区间',
  },
  // ---- 展示层专用拆分 term（与量能异源指标区分，避免同名异义；canonical field 不变） ----
  segmentVolumeMeanRatio: {
    label: '趋势段均量比',
    help: '当前趋势连续段的平均成交量与上一已完成趋势段的平均成交量之比，反映趋势段量能是否放大',
  },
  segmentAmountMeanRatio: {
    label: '趋势段均额比',
    help: '当前趋势连续段的平均成交额与上一已完成趋势段的平均成交额之比，反映趋势段资金是否放大',
  },
  volumePercentile: {
    label: '成交量历史分位',
    help: '当前成交量在其自身历史分布中的百分位置（0–100），用于判断当前成交处于常态还是极端区间',
  },
  volumeZScore: {
    label: '成交量 Z 分数',
    help: '成交量相对其历史分布的标准化偏离程度（标准差倍数），用于判断当前成交处于常态还是极端',
  },
  releaseVolumeRatio: {
    label: '释放量能比',
    help: '压缩释放时段的量能倍数（×），反映压缩突破时的放量程度，无方向配色',
  },
} as const

export type ReviewTermKey = keyof typeof REVIEW_TERMS

/** Phase canonical value → 中文展示 label（canonical value 不变） */
export const PHASE_LABELS: Readonly<Record<string, string>> = {
  'Early Lift': '低分位启动',
  Strengthening: '动能增强',
  Sustained: '高分位延续',
  Decelerating: '高分位减速',
  Weakening: '动能走弱',
  Repairing: '低分位修复',
}

/** Readiness canonical value → 中文展示 label（canonical value 不变） */
export const READINESS_LABELS: Readonly<Record<string, string>> = {
  ready: '可用',
  insufficient_history: '历史数据不足',
  unavailable_current: '当日数据不可用',
}

/** 排序字段 canonical value → 中文展示 label（canonical value 不变） */
export const SORT_LABELS: Readonly<Record<string, string>> = {
  velocity_desc: '分位动能 ↓',
  acceleration_desc: '动能偏离 ↓',
  position_desc: '等权涨跌分位 ↓',
  equal_weight_return_desc: '等权涨跌幅 ↓',
  capital_tilt_desc: '成交加权差 ↓',
  migration_desc: '主导集合更替率 ↓',
  coverage_desc: '成员数据覆盖率 ↓',
  freshness_density_desc: '事件衰减密度 ↓',
  freshness_today_desc: '今日技术事件数 ↓',
  technical_hhi_desc: '技术强度集中度 ↓',
  leader_median_gap_desc: '最高-中位强度差 ↓',
}

/** 详情 Tab canonical value → 中文 label */
export const DETAIL_TAB_LABELS: Readonly<Record<string, string>> = {
  dsa: '趋势与结构',
  smc: '结构演化',
  momentum: '动量与量能',
  price: '涨跌幅分布',
  current: '当日事实',
  dynamics: '等权涨跌动态',
  internal: '横截面结构',
  leadership: '主导成员更替',
  attribution: '成员贡献',
  facts: '底层事实',
}

/** 详情 Tab tooltip（hover tab 显示） */
export const DETAIL_TAB_HELP: Readonly<Record<string, string>> = {
  dsa: 'DSA 趋势：Regime Strength、趋势段 VWAP 偏离、趋势成员构成与 T-1→T 迁移，叠加 20D 滚动位置与横截面分位',
  smc: 'SMC 结构演化：swing/internal 状态、对齐、BOS/CHoCH/OB 事件时间线（建设中）',
  momentum: '动量与量能：方向/状态、增强/减弱、 squeeze、BB 位置/宽度、量能 Z 与释放量（建设中）',
  price: '涨跌幅分布：等权/金额加权收益、涨跌/走平比、收益离散度、集中度、主导迁移与 Jaccard（建设中）',
  current: '当前交易日已落库的价格、趋势、结构、动量与成交量事实',
  dynamics: '板块等权涨跌分位，以及分位动能与动能偏离随时间的演变',
  internal: '成员涨跌分布、成交加权差与集中度等横截面结构',
  leadership: '前一日到当日主导成员集合的留存、新进入与退出',
  attribution: '哪些成员对板块涨跌、成交加权差、集中度与主导结构贡献最大',
  facts: '后端保存的底层原始事实，主要用于研究与诊断',
}

/** Attribution 子 Tab canonical value → 中文 label */
export const ATTRIBUTION_SUBTAB_LABELS: Readonly<Record<string, string>> = {
  direction: '涨跌贡献',
  capital: '成交加权差贡献',
  breadth: '成员涨跌',
  concentration: '集中度贡献',
  leadership: '主导贡献',
}

/** Raw Facts 顶层 observation 分组 canonical key → 中文别名（展示专用，canonical key 不变） */
export const FACT_GROUP_ALIASES: Readonly<Record<string, string>> = {
  scope: '范围',
  price: '价格',
  trend: '趋势',
  structure: '结构',
  momentum: '动量',
  participation: '参与度',
  chip: '筹码',
  freshness: '新鲜度',
}
