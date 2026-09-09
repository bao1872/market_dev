// 营销 Hero 区右侧盘迹式筛选表（确定性 mock 数据）
// 目的：展示盘迹"看什么"与生产行情软件的差异——不是代码/价格/涨跌幅，
//       而是 趋势 / 结构 / 动量 / 量能 / 筹码 / 最近变化 这类状态字段。
// 硬约束：不接实时行情、不接后端；标红/标绿按 A 股惯例（涨红 / 跌绿）仅用于价格/涨跌语义。
//
// 文案与设计决策：
// - 6 行 deterministic mock；不随时间变化。
// - 行名称为「示例 A..F」（不声明对应真实标的）。
// - 所有状态字段为预置确定性字符串，不调用任何实时/随机逻辑。
// - 顶部条带 NOTE 明确标注"演示数据，非实时行情"。

export type HeroScreenerTrend = '上行' | '震荡' | '下行'
export type HeroScreenerDir = 'up' | 'down' | 'flat'

export interface HeroScreenerRow {
  readonly name: string
  readonly trend: HeroScreenerTrend
  readonly structure: string
  readonly momentum: string
  readonly volume: string
  readonly chip: string
  /** 最近变化：绿色高亮提示，表示状态近期发生变化 */
  readonly recentChange: string | null
  readonly dir: HeroScreenerDir
}

// 6 行 deterministic mock；不接实时行情。
export const HERO_SCREENER_ROWS: readonly HeroScreenerRow[] = [
  {
    name: '示例 A',
    trend: '上行',
    structure: '主要结构偏强',
    momentum: '正向',
    volume: '放大',
    chip: '重心上移',
    recentChange: '结构近期变化',
    dir: 'up',
  },
  {
    name: '示例 B',
    trend: '上行',
    structure: '短线转强',
    momentum: '增强',
    volume: '正常',
    chip: '密集区稳定',
    recentChange: '短线结构转强',
    dir: 'up',
  },
  {
    name: '示例 C',
    trend: '震荡',
    structure: '主要未变',
    momentum: '转弱',
    volume: '缩量',
    chip: '尚未迁移',
    recentChange: null,
    dir: 'flat',
  },
  {
    name: '示例 D',
    trend: '下行',
    structure: '主要转弱',
    momentum: '负向',
    volume: '放大',
    chip: '重心下移',
    recentChange: '主要结构确认向下',
    dir: 'down',
  },
  {
    name: '示例 E',
    trend: '上行',
    structure: '短线转强',
    momentum: '正向',
    volume: '温和放大',
    chip: '重心上移',
    recentChange: null,
    dir: 'up',
  },
  {
    name: '示例 F',
    trend: '震荡',
    structure: '区间整理',
    momentum: '中性',
    volume: '正常',
    chip: '密集区稳定',
    recentChange: null,
    dir: 'flat',
  },
]

// 表格下方提示文字：营销演示数据 / 非实时。
export const HERO_SCREENER_NOTE: string = '演示数据，非实时行情。'
