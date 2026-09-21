// [MarketDashboard][R3A] - Review V2 图表 token（真源 = src/styles/variables.scss，此处为 TS 镜像）
//
// 硬合同（R3A §5/§7）：
//   1. 普通 MA / EW series 的「身份色」**不得**使用 market.up / market.down。
//      红涨绿跌是**行情方向语义**，不是 series identity；品牌绿是 focus/action，也不是涨跌。
//   2. 80% / 20% 参考线只是阈值，不表达涨跌方向，使用 warning / muted 语义色。
//   3. 图表不得再散落 hard-coded 浅色主题 hex（背景/网格/文字/边框一律取 token）。
//
// 该模块**不** import lightweight-charts（保证可被 node:test 直接加载）。
import type { LineWidth } from './lineSeriesController'

export const REVIEW_TOKENS = {
  brand: {
    primary: '#00F6C2',
    primaryHover: '#39F5CF',
    deep: '#00B28A',
  },
  background: {
    base: '#0A0F14',
    secondary: '#111A23',
    card: '#161F29',
  },
  text: {
    primary: '#F2F6F8',
    secondary: '#98A1B3',
    muted: '#657281',
  },
  border: '#263440',
  // A 股语义：红涨绿跌（**仅**行情方向，绝不用作 series identity）
  market: {
    up: '#FF4D4F',
    down: '#22C55E',
  },
  status: {
    info: '#3882F6',
    warning: '#F59E0B',
    purple: '#8B5CF6',
  },
} as const

/** 行情方向色：显式列出，供 palette 守卫与测试引用。 */
export const MARKET_DIRECTION_COLORS: readonly string[] = [
  REVIEW_TOKENS.market.up,
  REVIEW_TOKENS.market.down,
]

/** 非方向性基准色板（品牌绿 / 信息蓝 / 辅助紫 / 预警橙 / 中性灰）。 */
const BASE_SERIES_COLORS: readonly string[] = [
  REVIEW_TOKENS.brand.primary,
  REVIEW_TOKENS.status.info,
  REVIEW_TOKENS.status.purple,
  REVIEW_TOKENS.status.warning,
  REVIEW_TOKENS.text.secondary,
]

function toRgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
}

function clampChannel(value: number): number {
  return Math.max(0, Math.min(255, Math.round(value)))
}

function toHex(rgb: readonly [number, number, number]): string {
  return `#${rgb.map((v) => clampChannel(v).toString(16).padStart(2, '0')).join('').toUpperCase()}`
}

/** 线性混色（ratio 0 = 原色，1 = 目标色）。纯函数，无新色板引入——派生自 token。 */
export function mixHex(hex: string, target: string, ratio: number): string {
  const a = toRgb(hex)
  const b = toRgb(target)
  const r = Math.max(0, Math.min(1, ratio))
  return toHex([a[0] + (b[0] - a[0]) * r, a[1] + (b[1] - a[1]) * r, a[2] + (b[2] - a[2]) * r])
}

/**
 * 由基准色板生成 rounds × base.length 个**可区分但同族**的颜色（向白提亮，保证深色底可见）。
 * 第 0 轮保留 token 原值（颜色身份可追溯到 token）。
 */
export function buildSeriesPalette(base: readonly string[], rounds: number): string[] {
  const out: string[] = []
  for (let round = 0; round < rounds; round += 1) {
    const ratio = round === 0 ? 0 : 0.16 + (round - 1) * 0.16
    for (const hex of base) {
      out.push(round === 0 ? hex : mixHex(hex, '#FFFFFF', ratio))
    }
  }
  return out
}

/** 20 = Compare 上限（HARD CAP 20）所需的最小可区分序列。 */
export const SERIES_PALETTE: readonly string[] = buildSeriesPalette(BASE_SERIES_COLORS, 4)

/** 按序列下标取色（超出自动回绕，保证永远有颜色）。 */
export function seriesColor(index: number): string {
  const i = ((index % SERIES_PALETTE.length) + SERIES_PALETTE.length) % SERIES_PALETTE.length
  return SERIES_PALETTE[i]
}

export const SERIES_LINE_WIDTH: LineWidth = 2
/** EW 等权指数默认更粗（视觉主线，但仍不使用涨跌色）。 */
export const EW_LINE_WIDTH: LineWidth = 3

export interface ChartReferenceLine {
  price: number
  color: string
  label: string
  dashed?: boolean
}

/** breadth 参考阈值：80% / 20%（warning + muted，不使用红涨绿跌）。 */
export const BREADTH_REFERENCE_LINES: readonly ChartReferenceLine[] = [
  { price: 0.8, color: REVIEW_TOKENS.status.warning, label: '80%', dashed: true },
  { price: 0.2, color: REVIEW_TOKENS.text.muted, label: '20%', dashed: true },
]

/** 深色 Review 图表主题（lightweight-charts options 的可序列化子集）。 */
export interface ChartThemeInput {
  hasLeftScale: boolean
  hasRightScale: boolean
  height: number
}

export function buildChartTheme({ hasLeftScale, hasRightScale, height }: ChartThemeInput) {
  return {
    autoSize: true as const,
    height,
    layout: {
      background: { color: REVIEW_TOKENS.background.base },
      textColor: REVIEW_TOKENS.text.secondary,
      fontFamily: 'SFMono-Regular, monospace',
      attributionLogo: false,
    },
    grid: {
      vertLines: { color: REVIEW_TOKENS.border },
      horzLines: { color: REVIEW_TOKENS.border },
    },
    leftPriceScale: { visible: hasLeftScale, borderColor: REVIEW_TOKENS.border },
    rightPriceScale: { visible: hasRightScale, borderColor: REVIEW_TOKENS.border },
    timeScale: { borderColor: REVIEW_TOKENS.border, timeVisible: false },
    crosshair: { mode: 0 as const },
  }
}
