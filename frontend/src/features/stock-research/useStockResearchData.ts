// [useStockResearchData] - 描述: 个股研究数据组装 hook（从 StockDetailPage 抽取）
// 集中 bars/indicators/quote/events/monitor states/memo 的数据请求与组装，
// 供 StockResearchWorkspace 和 MarketWorkspacePage 复用，禁止复制请求链路。
// 只有当前选中股票（instrumentId 非空）才发起请求；instrumentId 为空时所有查询 disabled。
import { useMemo } from 'react'
import {
  useInstrumentBySymbol,
  useBars,
  useIndicators,
  useInstrumentEvents,
  useRealtimeQuote,
  useWatchlist,
  useBatchInstruments,
  useStockMemo,
} from '@/hooks/useApi'
import { mapBarsToBarData, mergeRealtimeQuoteIntoBars } from '@/utils/chart'
import type { ChartEvent } from '@/components/StrategyChart'
import type { IndicatorResponse } from '@/api/endpoints'

// 按 timeframe 映射请求根数（与 Node Cluster / indicator_contract 对齐）
const BARS_COUNT_BY_TIMEFRAME: Record<string, number> = {
  '1d': 250,
  '15m': 4000,
  '1h': 1200,
  '1w': 260,
  '1mo': 120,
}

export interface StockResearchDataParams {
  symbol: string | null
  timeframe: string
}

export interface StockResearchData {
  instrumentId: string | undefined
  instrumentQuery: ReturnType<typeof useInstrumentBySymbol>
  barsQuery: ReturnType<typeof useBars>
  indicatorsQuery: ReturnType<typeof useIndicators>
  quoteQuery: ReturnType<typeof useRealtimeQuote>
  eventsQuery: ReturnType<typeof useInstrumentEvents>
  watchlistQuery: ReturnType<typeof useWatchlist>
  stockMemoQuery: ReturnType<typeof useStockMemo>
  // 组装后的数据
  baseBars: ReturnType<typeof mapBarsToBarData>
  displayBars: ReturnType<typeof mapBarsToBarData>
  indicators: IndicatorResponse | undefined
  events: ChartEvent[]
  // 监控状态（批量查询自选 instrument 信息）
  batchInstrumentsQuery: ReturnType<typeof useBatchInstruments>
  // 行情状态
  isBarsLoading: boolean
  backendIsPartial: boolean
}

export function useStockResearchData({ symbol, timeframe }: StockResearchDataParams): StockResearchData {
  // 1. 按 symbol 查询 instrument（获取 instrumentId）
  const instrumentQuery = useInstrumentBySymbol(symbol ?? '')
  const instrumentId = instrumentQuery.data?.id

  const barsCount = BARS_COUNT_BY_TIMEFRAME[timeframe] ?? 250

  // 2. 行情/指标/事件/quote/memo 查询（instrumentId 为空时由 hook 内部 disabled）
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

  // 3. 自选列表（用于判断当前股票是否在自选中）
  const watchlistQuery = useWatchlist()
  const watchlistInstrumentIds = useMemo(
    () => watchlistQuery.data?.items.map((item) => item.instrument_id) ?? [],
    [watchlistQuery.data],
  )
  const batchInstrumentsQuery = useBatchInstruments(watchlistInstrumentIds)

  // 4. 备忘录
  const stockMemoQuery = useStockMemo(instrumentId)

  // 5. 数据组装：bars → BarData + quote 合并
  const baseBars = useMemo(() => mapBarsToBarData(barsQuery.data?.items), [barsQuery.data])
  const backendIsPartial = barsQuery.data?.is_partial === true
  const displayBars = useMemo(
    () => mergeRealtimeQuoteIntoBars(baseBars, quoteQuery.data, timeframe, backendIsPartial),
    [baseBars, quoteQuery.data, timeframe, backendIsPartial],
  )

  // 6. events → ChartEvent
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
    watchlistQuery,
    stockMemoQuery,
    baseBars,
    displayBars,
    indicators: indicatorsQuery.data,
    events,
    batchInstrumentsQuery,
    isBarsLoading: barsQuery.isLoading,
    backendIsPartial,
  }
}
