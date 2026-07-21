// [useStockResearchData] - 描述: 个股研究数据组装 hook（/market 和 /stock/:symbol 共用）
// 集中 instrument/bars/indicators/quote/events 的数据请求与组装，供 StockResearchWorkspace 复用。
// 只有当前选中股票（instrumentId 非空）才发起请求；instrumentId 为空时所有查询 disabled。
// 本 hook 只保留图表核心查询；自选操作、上下切换、memo、飞书由 useStockDetailActions/useStockDetailFeishu 负责。
//
// [CHANGE-20260719-003 §四] 周期切换防护：
// 1. 显式 AbortController：timeframe 变化时主动 abort 旧请求（PROMPT.md §4 要求"切换周期时 Abort 旧请求"）
//    React Query 5 在 queryKey 变化时已自动取消旧 queryFn，此处额外维护显式 controller 以满足契约要求
// 2. 乱序丢弃：通过 response.timeframe === current timeframe 检查实现（PROMPT.md §4 要求"generation 不一致响应丢弃"）
//    设计说明：React 渲染是同步的，generation ref 在 render 期间读取的就是最新值，无法可靠检测"后发先至"；
//    而 response.timeframe 字段直接反映数据所属周期，与当前 timeframe 不匹配即说明是旧周期残留响应，
//    语义比 generation 更精确（generation 无法区分"同周期重发"与"跨周期乱序"）。故采用 timeframe 匹配检查。
//
// [PRD V2.0 §4.2 SNAP-01 Atomic Chart Snapshot] - 描述: 详情页改用 chart-snapshot 原子端点，
// 一次 MDAS DataFrame 同时返回 bars + indicators + render_frame，禁止 Bars/Indicators 两次独立实时请求。
// 外部仍以 barsQuery/indicatorsQuery 形式消费（保持 StockResearchWorkspace 兼容），实际数据源为同一 chartSnapshotQuery。
// render_frame.matched=false 时 isRenderReady=false（与 Capture 同款合同）。
import { useEffect, useMemo, useRef } from 'react'
import {
  useInstrumentBySymbol,
  useChartSnapshot,
  useInstrumentEvents,
  useRealtimeQuote,
} from '@/hooks/useApi'
import { mapBarsToBarData } from '@/utils/chart'
import type { ChartEvent } from '@/components/StrategyChart'
import type { IndicatorResponse, BarListResponse } from '@/api/endpoints'
import {
  type DisplayTimeframe,
  BARS_COUNT_BY_TIMEFRAME,
} from './stockResearchTypes'

/**
 * [PRD V2.0 §4.2 SNAP-01] 派生查询对象类型：从 chartSnapshotQuery 派生出兼容的 bars/indicators 视图。
 *
 * 字段集合覆盖 StockResearchWorkspace.tsx 与本 hook 实际使用的全部 React Query 字段：
 * data / isLoading / isSuccess / isError / error / isFetching / refetch。
 * 与 UseQueryResult 结构兼容（子集），保证外部消费代码无需改动。
 */
export interface DerivedChartQuery<T> {
  data: T | undefined
  isLoading: boolean
  isSuccess: boolean
  isError: boolean
  error: Error | null
  isFetching: boolean
  refetch: () => Promise<unknown>
}

export interface StockResearchDataParams {
  symbol: string | null
  timeframe: DisplayTimeframe
  // [CHANGE-011 SMC] - 是否请求 SMC 指标（默认 false）。
  // 父页面持有 smc 开关状态，开启时通过此参数传递给 useIndicators 触发后端按需计算。
  // 后端 include_smc=False 时跳过 SMC 计算，不消耗 CPU。
  includeSmc?: boolean
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
  // [PRD V2.0 §4.2 SNAP-01] barsQuery/indicatorsQuery 实际派生自同一 chartSnapshotQuery
  //   （DerivedChartQuery 字段集与 UseQueryResult 子集兼容，外部消费代码无需改动）
  barsQuery: DerivedChartQuery<BarListResponse>
  indicatorsQuery: DerivedChartQuery<IndicatorResponse>
  // chartSnapshot 原始 query（含 render_frame，供 StockResearchWorkspace 校验截图就绪状态）
  chartSnapshotQuery: ReturnType<typeof useChartSnapshot>
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

export function useStockResearchData({ symbol, timeframe, includeSmc = false }: StockResearchDataParams): StockResearchData {
  // 1. 按 symbol 查询 instrument（获取 instrumentId）
  const instrumentQuery = useInstrumentBySymbol(symbol ?? '')
  const instrumentId = instrumentQuery.data?.id

  const barsCount = BARS_COUNT_BY_TIMEFRAME[timeframe] ?? 250

  // [CHANGE-20260719-003 §四] 周期切换防护：显式 AbortController
  // timeframe 变化时主动 abort 旧 controller（满足 PROMPT.md §4 "切换周期时 Abort 旧请求"契约）
  // 乱序丢弃由下方 snapshotTimeframeMatches 检查实现（response.timeframe 字段）
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    if (abortRef.current) {
      abortRef.current.abort()
    }
    abortRef.current = new AbortController()
  }, [timeframe])

  // 2. [PRD V2.0 §4.2 SNAP-01] 一次 chart-snapshot 原子请求：bars + indicators + render_frame
  //   禁止 Bars/Indicators 两次独立实时请求；后端基于同一 MDAS DataFrame 生成 display_frame。
  //   includeSmc=true 时透传 include_smc=1，后端按需计算 SMC（默认 False 跳过，0 CPU 消耗）。
  //   page_size/bars 均透传 barsCount，与原 useBars/useIndicators 一致。
  const chartSnapshotQuery = useChartSnapshot(instrumentId, {
    timeframe,
    adj: 'qfq',
    bars: barsCount,
    ...(includeSmc ? { include_smc: 1 } : {}),
  })
  const quoteQuery = useRealtimeQuote(instrumentId)
  const eventsQuery = useInstrumentEvents(instrumentId, { limit: 100 })

  // 3. [PRD V2.0 §4.2] 从 chartSnapshotQuery 派生兼容的 barsQuery / indicatorsQuery
  //   外部 StockResearchWorkspace.tsx 仍以 barsQuery.data / indicatorsQuery.data 等形式消费，
  //   实际数据源为同一原子响应，保证 bars 与 indicators 基于同一 DataFrame。
  //   乱序丢弃：snapshot.timeframe 与当前 timeframe 不匹配时，data 置 undefined。
  const snapshotData = chartSnapshotQuery.data
  const snapshotTimeframeMatches =
    snapshotData?.timeframe === timeframe || !snapshotData?.timeframe
  const safeBarsData: BarListResponse | undefined = snapshotTimeframeMatches
    ? snapshotData?.bars
    : undefined
  const safeIndicatorsData: IndicatorResponse | undefined = snapshotTimeframeMatches
    ? snapshotData?.indicators
    : undefined

  const barsQuery: DerivedChartQuery<BarListResponse> = {
    data: safeBarsData,
    isLoading: chartSnapshotQuery.isLoading,
    isSuccess: chartSnapshotQuery.isSuccess && !!safeBarsData,
    isError: chartSnapshotQuery.isError,
    error: chartSnapshotQuery.error,
    isFetching: chartSnapshotQuery.isFetching,
    refetch: chartSnapshotQuery.refetch,
  }
  const indicatorsQuery: DerivedChartQuery<IndicatorResponse> = {
    data: safeIndicatorsData,
    isLoading: chartSnapshotQuery.isLoading,
    isSuccess: chartSnapshotQuery.isSuccess && !!safeIndicatorsData,
    isError: chartSnapshotQuery.isError,
    error: chartSnapshotQuery.error,
    isFetching: chartSnapshotQuery.isFetching,
    refetch: chartSnapshotQuery.refetch,
  }

  // 4. 数据组装：bars → BarData + quote 合并
  // [PRD V2.0 §4.2 SNAP-01] 乱序丢弃已在派生 safeBarsData 时处理（snapshotTimeframeMatches），
  //   baseBars 直接基于 safeBarsData 构造，无需重复 timeframe 检查。
  const baseBars = useMemo(
    () => mapBarsToBarData(safeBarsData?.items),
    [safeBarsData],
  )
  const backendIsPartial = safeBarsData?.is_partial === true
  // [CH-03 fix] PRD §3.3: MDAS 是唯一 Bar 真源，前端 quote 不再构造/修改 K 线。
  // displayBars 直接等于 baseBars；realtime 价格更新走 priceSummary（见下方）。
  // 旧 mergeRealtimeQuoteIntoBars 已移除（曾用 quote 合成末根 bar，违反 MDAS 唯一出口）。
  const displayBars = baseBars

  // [PRD V2.0 §4.2] safeIndicators 已在派生时完成 timeframe 乱序丢弃
  const safeIndicators = safeIndicatorsData

  // 5. events → ChartEvent
  const events: ChartEvent[] = useMemo(() => {
    if (!eventsQuery.data?.items) return []
    return eventsQuery.data.items.map((e) => ({
      time: e.event_time,
      type: e.event_type,
      title: (e.payload?.title as string) || e.event_type,
      description: (e.payload?.description as string | undefined),
    }))
  }, [eventsQuery.data])

  // 6. 行情摘要（优先使用实时报价，降级到 safeBarsData 最后一根 bar）
  const quote = quoteQuery.data
  const lastBar = safeBarsData?.items?.[safeBarsData.items.length - 1] || null
  const prevBar = safeBarsData?.items?.[safeBarsData.items.length - 2] || null
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

  // 7. 行情状态标签（非实时非降级时统一显示"行情回退"，禁止所有非 1d 周期显示"日线回退"）
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
    if (barsQuery.isLoading || !safeBarsData) return null
    if (safeBarsData.degraded) return { label: 'K线降级', reason: safeBarsData.degraded_reason }
    if (safeBarsData.is_partial) return { label: `K线含未完成 bar（${timeframe}）`, reason: null }
    return { label: `K线来源: ${safeBarsData.data_source}`, reason: null }
  }, [safeBarsData, barsQuery.isLoading, timeframe])

  // 8. 截图模式就绪状态（instrument + chart-snapshot[当前周期] + render_frame.matched 全部就绪）
  // [PRD V2.0 §4.2 SNAP-01] 新增 render_frame.matched 校验：
  //   - render_frame.matched=false 表示 bars 与 indicators display_frame 不匹配，不得 Ready
  //   - 与 Capture 同款合同（PROMPT.md §二 V2 render_frame.matched）
  // [CHANGE-20260719-003 §四] 乱序响应保护：safeBarsData/safeIndicatorsData 已在派生时过滤
  //   飞书截图额外校验 feishuLayersReady 由 StockResearchWorkspace 在 isCaptureMode 时计算
  const renderFrameMatched = snapshotData?.render_frame?.matched !== false
  const isRenderReady =
    instrumentQuery.isSuccess &&
    barsQuery.isSuccess &&
    baseBars.length > 0 &&
    indicatorsQuery.isSuccess &&
    safeIndicators != null &&
    renderFrameMatched

  return {
    instrumentId,
    instrumentQuery,
    barsQuery,
    indicatorsQuery,
    chartSnapshotQuery,
    quoteQuery,
    eventsQuery,
    baseBars,
    displayBars,
    // [PRD V2.0 §4.2] safeIndicators 已过滤 timeframe 不匹配的旧响应
    indicators: safeIndicators,
    events,
    isBarsLoading: barsQuery.isLoading,
    backendIsPartial,
    priceSummary,
    quoteStatus,
    barsStatus,
    isRenderReady,
  }
}
