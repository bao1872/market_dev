// [MarketDashboard][R3B] - 大盘页纯配置（无 React / DOM 依赖，可单测）
//
// 冻结内容：
//   1. 时间范围 20 / 60 / 120 / 250（默认 250）——**窗口由 server 返回**，
//      点击范围直接 `useMarketDashboard(days)`；禁止「先取 250 再前端 slice」。
//   2. KPI 卡片顺序锁死：MA5 / MA20 / MA50 / EW（**无** MA10 / MA120 card）。
//   3. 统一图 6 条 series（顺序即契约）：MA5 → MA10 → MA20 → MA50 → MA120（left，
//      breadth 0..1 呈现为 0%..100%）+ EW（right，text.primary，更粗）。
//   4. ranking summary：industry(L1) / concept(无层级)，lookback 5、limit 5。
//   5. 未来 Explorer URL（R3B **只生成**链接，R3C 才消费 board_id / hierarchy_level）。
//   6. [PANJI-MARKET-OVERVIEW] Layer 1 快照 6 卡顺序锁死；Layer 3 历史 2×2 面板 series 规格。
import { EW_LINE_WIDTH, REVIEW_TOKENS, SERIES_LINE_WIDTH } from './chartTheme'
import type { FixedScaleRange, LineWidth } from './lineSeriesController'
import type { HierarchyLevel, ScopeType, BreadthPoint } from './types'

// ===========================================================================
// 1. 时间范围
// ===========================================================================
export const MARKET_OVERVIEW_RANGES = [20, 60, 120, 250] as const
export type MarketOverviewRange = (typeof MARKET_OVERVIEW_RANGES)[number]
export const MARKET_OVERVIEW_DEFAULT_RANGE: MarketOverviewRange = 250

// ===========================================================================
// 2. KPI 卡片（顺序锁死）
// ===========================================================================
export const MARKET_CARD_ORDER = ['ma5', 'ma20', 'ma50', 'ew'] as const
export type MarketCardKey = (typeof MARKET_CARD_ORDER)[number]

export const MARKET_CARD_LABELS: Record<MarketCardKey, string> = {
  ma5: 'MA5 上方占比',
  ma20: 'MA20 上方占比',
  ma50: 'MA50 上方占比',
  ew: '全市场等权指数',
}

/** EW index 按当前显示窗口 rebased（区间起点 = 100）——仅辅助说明，不是行情判断。 */
export const EW_INDEX_BASE_HINT = '区间起点 = 100'

// ===========================================================================
// 3. 统一图 series（breadth 0..1 → 左轴 0%..100%）
// ===========================================================================
/** breadth 源数据恒为 0..1；左轴固定 [0,1]（只改 presentation，绝不 ×100 入数据）。 */
export const BREADTH_FIXED_RANGE: FixedScaleRange = { min: 0, max: 1 }

export type MarketOverviewField = 'ma5' | 'ma10' | 'ma20' | 'ma50' | 'ma120' | 'ew_index'

export interface MarketOverviewSeriesSpec {
  field: MarketOverviewField
  label: string
  scale: 'left' | 'right'
  lineWidth: LineWidth
  /** 省略 → shared non-direction palette 按顺序分配。 */
  color?: string
  breadthPercent?: boolean
  fixedScaleRange?: FixedScaleRange
}

const BREADTH_SERIES = (field: MarketOverviewField, label: string): MarketOverviewSeriesSpec => ({
  field,
  label,
  scale: 'left',
  lineWidth: SERIES_LINE_WIDTH,
  breadthPercent: true,
  fixedScaleRange: BREADTH_FIXED_RANGE,
})

/** 顺序即契约：MA5 → MA10 → MA20 → MA50 → MA120 → EW。 */
export const MARKET_OVERVIEW_SERIES: readonly MarketOverviewSeriesSpec[] = [
  BREADTH_SERIES('ma5', 'MA5'),
  BREADTH_SERIES('ma10', 'MA10'),
  BREADTH_SERIES('ma20', 'MA20'),
  BREADTH_SERIES('ma50', 'MA50'),
  BREADTH_SERIES('ma120', 'MA120'),
  {
    field: 'ew_index',
    label: 'EW',
    scale: 'right',
    lineWidth: EW_LINE_WIDTH,
    color: REVIEW_TOKENS.text.primary,
  },
]

// ===========================================================================
// 4. Ranking summary（industry L1 / concept 无层级）
// ===========================================================================
export const RANKING_SUMMARY_LOOKBACK = 5
export const RANKING_SUMMARY_LIMIT = 5

export interface MarketRankingSummaryConfig {
  key: 'industry' | 'concept'
  title: string
  scopeType: ScopeType
  hierarchyLevel: HierarchyLevel | null
  lookback: number
  limit: number
  /** 「查看全部」链接。 */
  seeAllLink: string
  /** 单行 → 未来 Explorer 链接（R3C 消费）。 */
  explorerLink: (boardId: string) => string
}

/** 行业摘要默认看 L1（与 R3C 冻结语义一致）。 */
export const INDUSTRY_DEFAULT_HIERARCHY_LEVEL: HierarchyLevel = 'L1'

export const INDUSTRY_ALL_LINK = `/review/industry?hierarchy_level=${INDUSTRY_DEFAULT_HIERARCHY_LEVEL}`
export const CONCEPT_ALL_LINK = '/review/concept'

/** 大盘摘要点击板块 → 统一生成 R3C 会消费的 URL（R3C 无需再改 R3B 链接）。 */
export function buildIndustryExplorerLink(
  boardId: string,
  hierarchyLevel: HierarchyLevel = INDUSTRY_DEFAULT_HIERARCHY_LEVEL,
): string {
  return `/review/industry?hierarchy_level=${hierarchyLevel}&board_id=${encodeURIComponent(boardId)}`
}

export function buildConceptExplorerLink(boardId: string): string {
  return `/review/concept?board_id=${encodeURIComponent(boardId)}`
}

export const MARKET_RANKING_SUMMARIES: readonly MarketRankingSummaryConfig[] = [
  {
    key: 'industry',
    title: '行业',
    scopeType: 'industry',
    hierarchyLevel: INDUSTRY_DEFAULT_HIERARCHY_LEVEL,
    lookback: RANKING_SUMMARY_LOOKBACK,
    limit: RANKING_SUMMARY_LIMIT,
    seeAllLink: INDUSTRY_ALL_LINK,
    explorerLink: buildIndustryExplorerLink,
  },
  {
    key: 'concept',
    title: '概念',
    scopeType: 'concept',
    hierarchyLevel: null,
    lookback: RANKING_SUMMARY_LOOKBACK,
    limit: RANKING_SUMMARY_LIMIT,
    seeAllLink: CONCEPT_ALL_LINK,
    explorerLink: buildConceptExplorerLink,
  },
]

// ===========================================================================
// 6. [PANJI-MARKET-OVERVIEW] Layer 1 快照 6 卡（顺序锁死）
// ===========================================================================
export const SNAPSHOT_CARD_ORDER = ['sse', 'szse', 'chinext', 'breadth', 'turnover', 'limit'] as const
export type SnapshotCardKey = (typeof SNAPSHOT_CARD_ORDER)[number]

export const SNAPSHOT_CARD_LABELS: Record<SnapshotCardKey, string> = {
  sse: '上证指数',
  szse: '深证成指',
  chinext: '创业板指',
  breadth: '涨跌家数',
  turnover: '全市场成交额',
  limit: '涨停 / 跌停',
}

// ===========================================================================
// 7. [PANJI-MARKET-OVERVIEW] Layer 3 历史 2×2 面板 series 规格（只读 API，前端不重算）
// ===========================================================================
export interface HistoryLineSpec {
  field: keyof BreadthPoint
  label: string
  /** 省略则由 shared palette 分配（普通序列绝不使用涨跌色）。 */
  color?: string
  /** 仅用于 chart 呈现（如成交额元->亿），不改原始数据。 */
  transform?: (v: number) => number
}

export interface HistoryChartSpec {
  key: string
  title: string
  series: readonly HistoryLineSpec[]
}

/** 元 -> 亿（仅 chart 呈现用）。 */
const TO_YI = (v: number) => v / 1e8

// 顺序即面板布局：指数 → 涨跌家数 → 成交额 → 涨停/跌停。
export const MARKET_HISTORY_CHARTS: readonly HistoryChartSpec[] = [
  {
    key: 'index',
    title: '三大指数相对走势（首有效点 = 100，分别归一）',
    series: [
      { field: 'sse_rebased', label: '上证', color: REVIEW_TOKENS.status.info },
      { field: 'szse_rebased', label: '深证', color: REVIEW_TOKENS.status.purple },
      { field: 'chinext_rebased', label: '创业板', color: REVIEW_TOKENS.brand.primary },
    ],
  },
  {
    key: 'breadth',
    title: '涨跌家数',
    series: [
      { field: 'advance_count', label: '上涨', color: REVIEW_TOKENS.market.up },
      { field: 'decline_count', label: '下跌', color: REVIEW_TOKENS.market.down },
    ],
  },
  {
    key: 'turnover',
    title: '全市场成交额（亿元）',
    series: [{ field: 'turnover_amount', label: '成交额', color: REVIEW_TOKENS.brand.primary, transform: TO_YI }],
  },
  {
    key: 'limit',
    title: '涨停 / 跌停家数',
    series: [
      { field: 'limit_up_count', label: '涨停', color: REVIEW_TOKENS.market.up },
      { field: 'limit_down_count', label: '跌停', color: REVIEW_TOKENS.market.down },
    ],
  },
]
