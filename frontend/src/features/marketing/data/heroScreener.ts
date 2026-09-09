// 营销 Hero 区右侧股票筛选表（确定性 mock 数据）
// 目的：参考图 #2 右侧表格的视觉化对照——6 行静态示例，明确标注"非实时行情"。
// 硬约束：不接实时行情、不接后端；标红/标绿按 A 股惯例（涨红 / 跌绿）。
//
// 文案与设计决策（plan §Slice A 范围）：
// - 行数 6，与参考图一致；
// - 代码取自参考图视觉稿（002823/002822/002816/002819/002829/002830），
//   但**不**声明这些代码对应真实标的；名称占位为「示例 A/B/C/...」。
// - changePct 为预置数字，不随时间变化；价格同理。
// - 顶部条带 NOTE 文字在 Hero 渲染时直接接在表格下方，避免误读为实时数据。

export type HeroScreenerDirection = 'up' | 'down'

export interface HeroScreenerRow {
  readonly code: string
  readonly name: string
  readonly price: number
  readonly changePct: number
  readonly direction: HeroScreenerDirection
}

// 6 行 deterministic mock；不接实时行情（plan §硬约束 #2）。
// 排序固定，与参考图视觉顺序对齐（002823 居首）。
export const HERO_SCREENER_ROWS: readonly HeroScreenerRow[] = [
  { code: '002823', name: '示例 A', price: 24.50, changePct: 5.70, direction: 'up' },
  { code: '002822', name: '示例 B', price: 14.36, changePct: -2.80, direction: 'down' },
  { code: '002816', name: '示例 C', price: 27.84, changePct: 3.56, direction: 'up' },
  { code: '002819', name: '示例 D', price: 45.92, changePct: -0.87, direction: 'down' },
  { code: '002829', name: '示例 E', price: 12.07, changePct: 2.34, direction: 'up' },
  { code: '002830', name: '示例 F', price: 38.50, changePct: 4.12, direction: 'up' },
]

// 表格下方提示文字：营销演示数据 / 非实时。
export const HERO_SCREENER_NOTE: string = '演示数据，非实时行情。'