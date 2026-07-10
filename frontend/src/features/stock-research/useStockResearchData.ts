// [useStockResearchData] - 描述: 个股研究数据组装 hook（从 StockDetailPage 抽取）
// 集中 bars/indicators/quote/events 的数据请求与组装，供 StockResearchWorkspace 复用。
// 只有当前选中股票（instrumentId 非空）才发起请求；instrumentId 为空时所有查询 disabled。
// 本 hook 只保留图表核心查询；自选操作、上下切换、memo 继续留在 StockDetailPage（下一独立 PR 迁移）。
import { useMemo } from 'react'
import {
  useInstrumentBySymbol,
  useBars,
  useIndicators,
  useInstrumentEvents,
  useRealtimeQuote,
} from '@/hooks/useApi'
import { mapBarsToBarData, mergeRealtimeQuoteIntoBars } from '@/utils/chart'
import type { ChartEvent } from '@/components/StrategyChart'
import type { IndicatorResponse } from '@/api/endpoints'
import type { DisplayTimeframe } from '@/features/market-workspace/marketWorkspaceUrlState'

// 按 timeframe 映射请求根数（与 Node Cluster / indicator_contract 对齐）
const BARS_COUNT_BY_TIMEFRAME: Record<DisplayTimeframe, number> = {
  '1d': 250,
  '15m': 4000,
  '1h': 1200,
  '1w': 260,
  '1mo': 120,
}

export interface StockResearchDataParams {
  symbol: string | null
  timeframe: DisplayTimeframe
}

export interface StockResearchData {
  instrumentId: string | undefined
  instrumentQuery: ReturnType<typeof useInstrumentBySymbol>
  barsQuery: ReturnType<typeof useBars>
  indicatorsQuery: ReturnType<typeof useIndicators>
  quoteQuery: ReturnType<typeof useRealtimeQuote>
  eventsQuery: ReturnType<typeof useInstrumentEvents>
  // 组装后的数据
  baseBars: ReturnType<typeof mapBarsToBarData>
  displayBars: ReturnType<typeof mapBarsToBarData>
  indicators: IndicatorResponse | undefined
  events: ChartEvent[]
  // 行情状态
  isBarsLoading: boolean
  backendIsPartial: boolean
}

export function useStockResearchData({ symbol, timeframe }: StockResearchDataParams): StockResearchData {
  // 1. 按 symbol 查询 instrument（获取 instrumentId）
  const instrumentQuery = useInstrumentBySymbol(symbol ?? '')
  const instrumentId = instrumentQuery.data?.id

  const barsCount = BARS_COUNT_BY_TIMEFRAME[timeframe] ?? 250

  // 2. 行情/指标/事件/quote 查询（instrumentId 为空时由 hook 内部 disabled）
  const barsQuery = useBars(instrumentId, {
    timeframe,
    adj: 'qfq',
    page_size: barsCount,
  })
  const indicatorsQuery = useIndicators(instrumentId, {
    timeframe,
    adj: 'qfq',
    bars: barsCount,
  })
  const quoteQuery = useRealtimeQuote(instrumentId)
  const eventsQuery = useInstrumentEvents(instrumentId, { limit: 100 })

  // 3. 数据组装：bars → BarData + quote 合并
  const baseBars = useMemo(() => mapBarsToBarData(barsQuery.data?.items), [barsQuery.data])
  const backendIsPartial = barsQuery.data?.is_partial === true
  const displayBars = useMemo(
    () => mergeRealtimeQuoteIntoBars(baseBars, quoteQuery.data, timeframe, backendIsPartial),
    [baseBars, quoteQuery.data, timeframe, backendIsPartial],
  )

  // 4. events → ChartEvent
  const events: ChartEvent[] = useMemo(() => {
    if (!eventsQuery.data?.items) return []
    return eventsQuery.data.items.map((e) => ({
      time: e.event_time,
      type: e.event_type,
      title: (e.payload?.title as string) || e.event_type,
      description: e.payload?.description as string | undefined,
    }))
  }, [eventsQuery.data])

  return {
    instrumentId,
    instrumentQuery,
    barsQuery,
    indicatorsQuery,
    quoteQuery,
    eventsQuery,
    baseBars,
    displayBars,
    indicators: indicatorsQuery.data,
    events,
    isBarsLoading: barsQuery.isLoading,
    backendIsPartial,
  }
}
