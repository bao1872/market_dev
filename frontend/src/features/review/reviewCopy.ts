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

export interface ReviewTerm {
  /** 中文显示名 */
  label: string
  /** 中性事实性 tooltip 解释（可选；无 help 时 ReviewTerm 只渲染 label） */
  help?: string
}

/** 主要研究术语：label + 可选 tooltip（由 ReviewTerm 统一渲染） */
export const REVIEW_TERMS = {
  scope: {
    label: '板块 / 概念',
    help: '当前复盘对象，可以是一级行业、二级行业、三级行业或概念板块',
  },
  phase: {
    label: '阶段',
    help: '根据板块历史位置及其变化状态，由后端计算得到的当前阶段',
  },
  position: {
    label: '历史位置',
    help: '当前状态在历史窗口中的相对位置，范围 0–100。数值越高表示当前位于自身历史较高区域',
  },
  velocity: {
    label: '变化速度',
    help: '历史位置最近变化的速度。正值表示位置在上移，负值表示位置在下移',
  },
  acceleration: {
    label: '变化加速度',
    help: '变化速度本身的变化，用于观察上移或下移的速度是否正在加快或放缓',
  },
  equalWeightReturn: {
    label: '等权收益',
    help: '每只成员股票赋予相同权重后的平均收益，避免少数大股票主导结果',
  },
  capitalTilt: {
    label: '资金偏向',
    help: '成交额加权收益与等权收益之间的差异，反映成交额更多集中在哪些涨跌成员上',
  },
  breadth: {
    label: '涨跌分布',
    help: '板块内上涨、下跌和平盘股票的占比',
  },
  leadershipMigration: {
    label: '龙头更替率',
    help: '前一交易日与当前交易日领先成员名单的变化程度',
  },
  coverage: {
    label: '数据覆盖率',
    help: '当前指标实际获得有效数据的成员，占应有成员的比例',
  },
  freshness: {
    label: '事件新鲜度',
    help: '最近发生的技术事件数量与时间衰减后的密度',
  },
  technical: {
    label: '技术特征',
    help: '技术事件的集中度、头部贡献、龙头与中位成员差距等事实汇总',
  },
  amountWeightedReturn: {
    label: '成交额加权收益',
    help: '按成员成交额分配权重后的板块收益',
  },
  totalVolume: {
    label: '总成交量',
    help: '当前板块有效成员的成交量合计',
  },
  totalAmount: {
    label: '总成交额',
    help: '当前板块有效成员的成交额合计',
  },
  priceConcentration: {
    label: '价格集中度',
    help: '衡量价格侧贡献是否集中在少数成员上',
  },
  amountConcentration: {
    label: '成交额集中度',
    help: '衡量成交额是否集中在少数成员上',
  },
  rawHhi: {
    label: '原始集中度',
    help: '底层指标为 HHI（Herfindahl–Hirschman Index）。数值越高表示分布越集中在少数成员',
  },
  normalizedHhi: {
    label: '标准化集中度',
    help: '对成员数量影响进行标准化后的 HHI，更适合不同规模板块之间比较',
  },
  sampleCount: {
    label: '样本数',
    help: '本次集中度计算实际使用的有效成员数量',
  },
  status: {
    label: '数据状态',
    help: '当前事实的状态：可用、历史数据不足或当日数据不可用',
  },
  phaseCurrent: {
    label: '当前阶段',
    help: '根据板块历史位置及其变化状态，由后端计算得到的当前阶段',
  },
  upperOccupancy: {
    label: '高位占比',
    help: '分析窗口内处于上方区域的占比，直接展示后端计算结果',
  },
  lowerOccupancy: {
    label: '低位占比',
    help: '分析窗口内处于下方区域的占比，直接展示后端计算结果',
  },
  returnCapital: {
    label: '收益与资金',
    help: '板块等权收益、成交额加权收益与资金偏向',
  },
  concentration: {
    label: '集中度',
    help: '衡量板块内成员在价格或成交额侧的分布集中程度',
  },
  priceHhi: {
    label: '价格集中度',
    help: '衡量价格侧贡献是否集中在少数成员上',
  },
  amountHhi: {
    label: '成交额集中度',
    help: '衡量成交额是否集中在少数成员上',
  },
  returnDispersion: {
    label: '收益离散度',
    help: '板块内部成员收益之间的分散程度。数值越大，说明成员之间表现差异越明显',
  },
  dispersion: {
    label: '离散程度',
    help: '成员收益之间的分散程度，数值越大差异越明显',
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
    label: '前一日龙头',
    help: '前一交易日领先成员名单',
  },
  tLeaders: {
    label: '当日龙头',
    help: '当前交易日领先成员名单',
  },
  direction: {
    label: '方向变化',
    help: '前一日与当前交易日领先成员的方向状态',
  },
  prevDir: {
    label: '前一日方向',
    help: '前一日领先成员的方向',
  },
  currDir: {
    label: '当日方向',
    help: '当前交易日领先成员的方向',
  },
  transition: {
    label: '龙头更替',
    help: '前一交易日到当前交易日龙头成员的留存、新进入与退出',
  },
  retained: {
    label: '留存成员',
    help: '前一日与当日均属于领先名单的成员',
  },
  entrants: {
    label: '新进入成员',
    help: '当日新进入领先名单、前一日不在名单中的成员',
  },
  exits: {
    label: '退出成员',
    help: '前一日在领先名单中、当日退出名单的成员',
  },
  metrics: {
    label: '更替指标',
    help: '衡量龙头名单变化程度的指标',
  },
  prevRetention: {
    label: '前日龙头留存率',
    help: '前一交易日龙头成员中当日仍留在名单中的比例',
  },
  jaccardStability: {
    label: '龙头名单稳定度',
    help: '比较前一日和当日龙头集合的重合程度。越高表示名单越稳定',
  },
  migration: {
    label: '龙头更替率',
    help: '描述领先成员名单发生变化的程度。越高表示龙头成员更替越明显',
  },
  // ---- P0-4/P0-5：趋势 / 量能指标中文简称 + 解释（单一展示 owner，统一 tooltip） ----
  trendStrength: {
    label: '趋势强度',
    help: '衡量当前趋势方向的强弱程度。数值越高表示趋势方向越明确、持续性越强',
  },
  dsaVwapDev: {
    label: '均价偏离',
    help: 'DSA（动态结构分析）相对 VWAP 均价的偏离百分比。正值表示价格位于均价上方，负值表示下方',
  },
  segmentBars: {
    label: '持续K数',
    help: '当前趋势连续段包含的 K 线数量，反映趋势已持续的时间长度',
  },
  segmentChange: {
    label: '区间涨跌',
    help: '当前趋势连续段内的累计涨跌幅',
  },
  segmentSlope: {
    label: '趋势斜率',
    help: '趋势连续段的价格变化速率，反映趋势推进的陡缓',
  },
  vwapRetTotal: {
    label: '均价累计收益',
    help: '自趋势段开始以来相对 VWAP 均价的累计收益',
  },
  volumeRatio: {
    label: '量比',
    help: '当前成交量与近期平均成交量的比值，反映成交量相对活跃程度',
  },
  amountRatio: {
    label: '额比',
    help: '当前成交额与近期平均成交额的比值，反映资金活跃程度',
  },
  zScore: {
    label: 'Z分数',
    help: '指标值相对其历史分布的标准化偏离程度（标准差倍数），用于判断当前处于常态还是极端',
  },
  bbPosition: {
    label: '布林位置',
    help: '当前价格处在布林带中的相对位置（0=下轨，1=上轨）',
  },
  bbWidth: {
    label: '布林宽度',
    help: '布林带上下轨之间的宽度，反映波动扩张或收缩',
  },
  vwapTotalReturn: {
    label: '均价累计收益',
    help: '相对 VWAP 均价的累计收益',
  },
  percentile: {
    label: '分位数',
    help: '指标值在其历史分布中所处的百分位置（0–100），用于判断当前处于常态还是极端区间',
  },
} as const

export type ReviewTermKey = keyof typeof REVIEW_TERMS

/** Phase canonical value → 中文展示 label（canonical value 不变） */
export const PHASE_LABELS: Readonly<Record<string, string>> = {
  'Early Lift': '初步抬升',
  Strengthening: '增强中',
  Sustained: '持续中',
  Decelerating: '动能放缓',
  Weakening: '走弱中',
  Repairing: '修复中',
}

/** Readiness canonical value → 中文展示 label（canonical value 不变） */
export const READINESS_LABELS: Readonly<Record<string, string>> = {
  ready: '可用',
  insufficient_history: '历史数据不足',
  unavailable_current: '当日数据不可用',
}

/** 排序字段 canonical value → 中文展示 label（canonical value 不变） */
export const SORT_LABELS: Readonly<Record<string, string>> = {
  velocity_desc: '变化速度 ↓',
  acceleration_desc: '变化加速度 ↓',
  position_desc: '历史位置 ↓',
  equal_weight_return_desc: '等权收益 ↓',
  capital_tilt_desc: '资金偏向 ↓',
  migration_desc: '龙头更替率 ↓',
  coverage_desc: '数据覆盖率 ↓',
  freshness_density_desc: '事件新鲜度 ↓',
  freshness_today_desc: '今日事件数 ↓',
  technical_hhi_desc: '技术集中度 ↓',
  leader_median_gap_desc: '龙头领先差 ↓',
}

/** 详情 Tab canonical value → 中文 label */
export const DETAIL_TAB_LABELS: Readonly<Record<string, string>> = {
  current: '当日观察',
  dynamics: '历史动态',
  internal: '内部结构',
  leadership: '龙头迁移',
  attribution: '成员归因',
  facts: '原始数据',
}

/** 详情 Tab tooltip（hover tab 显示） */
export const DETAIL_TAB_HELP: Readonly<Record<string, string>> = {
  current: '查看当前交易日的价格、趋势、结构、动量和成交量事实',
  dynamics: '查看板块历史位置、变化速度和变化加速度',
  internal: '查看板块内部涨跌分布、资金偏向和集中度',
  leadership: '查看前一日到当前交易日龙头成员的留存、新进入和退出',
  attribution: '查看哪些成员对收益、资金偏向、集中度和龙头结构产生了主要贡献',
  facts: '查看后端保存的 canonical 原始事实，主要用于研究和诊断',
}

/** Attribution 子 Tab canonical value → 中文 label */
export const ATTRIBUTION_SUBTAB_LABELS: Readonly<Record<string, string>> = {
  direction: '涨跌贡献',
  capital: '资金偏向贡献',
  breadth: '涨跌分布',
  concentration: '集中度贡献',
  leadership: '龙头贡献',
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
