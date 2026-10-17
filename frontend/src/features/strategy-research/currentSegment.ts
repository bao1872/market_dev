/** Presentation gate only. Direction comes from the exact TV ZigZag owner. */
import type { StrategyHistory, PriceSegment } from '@/api/strategyCandidates'
export type CurrentSegment = PriceSegment
export function currentSegmentAsOf(history: StrategyHistory, expectedTradeDate: string): CurrentSegment | null {
  const segment = history.segment, structure = history.structure
  if (!segment || !structure || history.as_of !== expectedTradeDate || structure.as_of !== expectedTradeDate ||
    segment.asOf !== expectedTradeDate || segment.directionBasis !== 'TV_LAST_SEGMENT' || history.price_adjustment !== 'qfq' || history.source_policy !== 'MDAS_DB_ONLY' ||
    history.tv_zigzag?.asOf !== expectedTradeDate || history.tv_zigzag.algorithmSha !== segment.algorithmSha ||
    structure.source_bar_hash !== history.source_bar_hash || structure.adj_factor_hash !== history.adj_factor_hash ||
    ![segment.totalReturnPct,segment.sessions].every(Number.isFinite) ||
    (segment.slopePctPerSession != null && !Number.isFinite(segment.slopePctPerSession))) return null
  return segment
}
