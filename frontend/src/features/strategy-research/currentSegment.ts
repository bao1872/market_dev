/** Read-only, descriptive price-segment display. NEVER a model feature or a
 * candidate label. Uses existing MDAS QFQ bars and visible confirmed pivots.
 * No provisional ZigZag candidate, unconfirmed pivot or future bar is used.
 */
import type { StrategyHistory, StructurePivot } from '@/api/strategyCandidates'

export type CurrentSegment = {
  direction: '向上' | '向下'
  anchorDate: string
  lastDate: string
  sessions: number
  totalReturnPct: number
  slopePctPerSession: number
  contrarySlope: boolean
}
const day = (date: string): string => String(date).slice(0, 10)

export function currentSegmentAsOf(history: StrategyHistory, expectedTradeDate: string): CurrentSegment | null {
  const bars = history.bars
  const structure = history.structure
  const asOf = day(expectedTradeDate)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(asOf) || !structure || history.price_adjustment !== 'qfq' ||
      history.source_policy !== 'MDAS_DB_ONLY' || !bars || bars.length < 2 ||
      day(history.as_of) !== asOf || day(structure.as_of) !== asOf ||
      history.source_bar_hash !== structure.source_bar_hash ||
      history.adj_factor_hash !== structure.adj_factor_hash ||
      bars[bars.length - 1]?.date !== asOf) return null
  for (let i = 0; i < bars.length; i++) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(bars[i].date) || (i && bars[i].date <= bars[i - 1].date) ||
      !Number.isFinite(bars[i].close) || bars[i].close <= 0) return null
  }
  const visible = (structure.confirmed_pivots ?? []).filter((p: StructurePivot) =>
    p.kind === 'L' || p.kind === 'H'
  ).filter(p => p.anchor_date <= asOf && day(p.confirmed_at) <= asOf &&
    day(p.confirmed_at) >= p.anchor_date && Number.isFinite(p.price) && p.price > 0)
  if (!visible.length) return null
  visible.sort((a, b) => b.anchor_date.localeCompare(a.anchor_date) || b.confirmed_at.localeCompare(a.confirmed_at))
  const pivot = visible[0]
  const start = bars.findIndex(b => b.date === pivot.anchor_date)
  if (start < 0 || start >= bars.length - 1) return null
  const window = bars.slice(start)
  // Descriptive trend slope: OLS slope of log(adjusted close) on trading-session
  // index; exp(beta)-1 converts the fitted one-session change into %/session.
  const n = window.length
  const meanX = (n - 1) / 2
  const meanLog = window.reduce((sum, bar) => sum + Math.log(bar.close), 0) / n
  let cov = 0
  let varX = 0
  window.forEach((bar, i) => { const d = i - meanX; cov += d * (Math.log(bar.close) - meanLog); varX += d * d })
  if (!(varX > 0)) return null
  const slope = Math.expm1(cov / varX) * 100
  const total = (window[n - 1].close / pivot.price - 1) * 100
  if (!Number.isFinite(slope) || !Number.isFinite(total)) return null
  // User-facing *current* direction follows observable segment drift, NOT
  // the candidate kind and NOT an assumed direction from a pivot alone.
  if (Math.abs(slope) < 1e-8) return null // cannot assert an up/down trend
  const direction = slope > 0 ? '向上' : '向下'
  const pivotImplied = pivot.kind === 'L' ? '向上' : '向下'
  return { direction, anchorDate: pivot.anchor_date, lastDate: asOf, sessions: n - 1,
    totalReturnPct: total, slopePctPerSession: slope,
    contrarySlope: direction !== pivotImplied }
}
