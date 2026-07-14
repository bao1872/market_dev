// [useStockResearchData] - 描述: 个股研究数据组装 hook（/market 和 /stock/:symbol 共用）
// 集中 instrument/bars/indicators/quote/events 的数据请求与组装，供 StockResearchWorkspace 复用。
// 只有当前选中股票（instrumentId 非空）才发起请求；instrumentId 为空时所有查询 disabled。
// 本 hook 只保留图表核心查询；自选操作、上下切换、memo、飞书由 useStockDetailActions/useStockDetailFeishu 负责。
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
import {
  type DisplayTimeframe,
  BARS_COUNT_BY_TIMEFRAME,
} from './stockResearchTypes'

export interface StockResearchDataParams {
  symbol: string | null
  timeframe: DisplayTimeframe
}

// 行情摘要 ViewModel（供 StockDetailPage 价格条和 StockResearchWorkspace 状态条共用）
// CHANGE-20260713-010: 新增 totalMarketCap/floatMarketCap/marketCapAsOf
export interface PriceSummary {
  currentPrice: number | null
  openPrice: number | null
  highPrice: number | null
  lowPrice: number | null
  amountValue: number | null
  changePercent: number | null
  isUp: boolean
  totalMarketCap: number | null
  floatMarketCap: number | null
  marketCapAsOf: string | null
}

// 行情状态标签（统一 /market 和 /stock 的状态文案）
export interface QuoteStatus {
  label: string
  badgeClass: string
}

export interface BarsStatus {
  label: string
  reason: string | null
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
  priceSummary: PriceSummary
  quoteStatus: QuoteStatus
  barsStatus: BarsStatus | null
  // 截图模式就绪状态（由父组件传入 isCaptureMode 时使用）
  isRenderReady: boolean
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
      description: (e.payload?.description as string | undefined),
    }))
  }, [eventsQuery.data])

  // 5. 行情摘要（优先使用实时报价，降级到 barsQuery 最后一根 bar）
  const quote = quoteQuery.data
  const lastBar = barsQuery.data?.items?.[barsQuery.data.items.length - 1] || null
  const prevBar = barsQuery.data?.items?.[barsQuery.data.items.length - 2] || null
  const currentPrice = quote?.current_price ?? lastBar?.close ?? null
  const openPrice = quote?.open ?? lastBar?.open ?? null
  const highPrice = quote?.high ?? lastBar?.high ?? null
  const lowPrice = quote?.low ?? lastBar?.low ?? null
  const amountValue = quote?.amount ?? lastBar?.amount ?? null
  const changePercent = quote?.change_pct ?? (lastBar && prevBar
    ? ((lastBar.close - prevBar.close) / prevBar.close * 100)
    : null)
  const isUp = changePercent !== null ? changePercent >= 0 : true
  // CHANGE-20260713-010: 市值字段来自 quote（DB 无股本数据时为 null）
  const totalMarketCap = quote?.total_market_cap ?? null
  const floatMarketCap = quote?.float_market_cap ?? null
  const marketCapAsOf = quote?.market_cap_as_of ?? null

  const priceSummary: PriceSummary = useMemo(() => ({
    currentPrice,
    openPrice,
    highPrice,
    lowPrice,
    amountValue,
    changePercent,
    isUp,
    totalMarketCap,
    floatMarketCap,
    marketCapAsOf,
  }), [currentPrice, openPrice, highPrice, lowPrice, amountValue, changePercent, isUp, totalMarketCap, floatMarketCap, marketCapAsOf])

  // 6. 行情状态标签（非实时非降级时统一显示"行情回退"，禁止所有非 1d 周期显示"日线回退"）
  const quoteStatus: QuoteStatus = useMemo(() => {
    if (!quote) return { label: '加载中', badgeClass: 'status-pill neutral' }
    if (quote.degraded) return { label: '行情降级', badgeClass: 'status-pill warn' }
    if (quote.is_realtime && quote.source === 'pytdx' && quote.freshness_seconds <= 60) {
      return { label: '实时行情', badgeClass: 'status-pill ok' }
    }
    if (quote.source === 'daily_fallback') {
      return { label: '行情回退', badgeClass: 'status-pill neutral' }
    }
    return { label: '数据延迟', badgeClass: 'status-pill warn' }
  }, [quote])

  const barsStatus: BarsStatus | null = useMemo(() => {
    if (barsQuery.isLoading || !barsQuery.data) return null
    const data = barsQuery.data
    if (data.degraded) return { label: 'K线降级', reason: data.degraded_reason }
    if (data.is_partial) return { label: `K线含未完成 bar（${timeframe}）`, reason: null }
    return { label: `K线来源: ${data.data_source}`, reason: null }
  }, [barsQuery.data, barsQuery.isLoading, timeframe])

  // 7. 截图模式就绪状态（instrument + bars + indicators 全部成功即就绪）
  // 飞书截图额外校验 feishuLayersReady 由 StockResearchWorkspace 在 isCaptureMode 时计算
  const isRenderReady = instrumentQuery.isSuccess && barsQuery.isSuccess && indicatorsQuery.isSuccess

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
    priceSummary,
    quoteStatus,
    barsStatus,
    isRenderReady,
  }
}
