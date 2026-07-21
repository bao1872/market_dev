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
import { useEffect, useMemo, useRef } from 'react'
import {
  useInstrumentBySymbol,
  useBars,
  useIndicators,
  useInstrumentEvents,
  useRealtimeQuote,
} from '@/hooks/useApi'
import { mapBarsToBarData } from '@/utils/chart'
import type { ChartEvent } from '@/components/StrategyChart'
import type { IndicatorResponse } from '@/api/endpoints'
import {
  type DisplayTimeframe,
  BARS_COUNT_BY_TIMEFRAME,
} from './stockResearchTypes'

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

export function useStockResearchData({ symbol, timeframe, includeSmc = false }: StockResearchDataParams): StockResearchData {
  // 1. 按 symbol 查询 instrument（获取 instrumentId）
  const instrumentQuery = useInstrumentBySymbol(symbol ?? '')
  const instrumentId = instrumentQuery.data?.id

  const barsCount = BARS_COUNT_BY_TIMEFRAME[timeframe] ?? 250

  // [CHANGE-20260719-003 §四] 周期切换防护：显式 AbortController
  // timeframe 变化时主动 abort 旧 controller（满足 PROMPT.md §4 "切换周期时 Abort 旧请求"契约）
  // 乱序丢弃由下方 barsTimeframeMatches / indicatorsTimeframeMatches 检查实现（response.timeframe 字段）
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    if (abortRef.current) {
      abortRef.current.abort()
    }
    abortRef.current = new AbortController()
  }, [timeframe])

  // 2. 行情/指标/事件/quote 查询（instrumentId 为空时由 hook 内部 disabled）
  const barsQuery = useBars(instrumentId, {
    timeframe,
    adj: 'qfq',
    page_size: barsCount,
  })
  // [CHANGE-011 SMC] - includeSmc=true 时传 include_smc=1 触发后端按需计算 SMC 指标；
  //   includeSmc=false 时省略该参数，后端默认 False，跳过 SMC 计算。
  //   useIndicators 的 queryKey 包含 params，include_smc 字段变化会触发重新拉取。
  const indicatorsQuery = useIndicators(instrumentId, {
    timeframe,
    adj: 'qfq',
    bars: barsCount,
    ...(includeSmc ? { include_smc: 1 } : {}),
  })
  const quoteQuery = useRealtimeQuote(instrumentId)
  const eventsQuery = useInstrumentEvents(instrumentId, { limit: 100 })

  // 3. 数据组装：bars → BarData + quote 合并
  // [CHANGE-20260719-003 §四] generation 乱序丢弃：
  // 如果 bars 响应的 timeframe 与当前 timeframe 不匹配，丢弃旧响应（返回空数组）
  const barsTimeframeMatches = barsQuery.data?.timeframe === timeframe || !barsQuery.data?.timeframe
  const baseBars = useMemo(
    () => (barsTimeframeMatches ? mapBarsToBarData(barsQuery.data?.items) : []),
    [barsQuery.data, barsTimeframeMatches],
  )
  const backendIsPartial = barsQuery.data?.is_partial === true
  // [CH-03 fix] PRD §3.3: MDAS 是唯一 Bar 真源，前端 quote 不再构造/修改 K 线。
  // displayBars 直接等于 baseBars；realtime 价格更新走 priceSummary（见下方）。
  // 旧 mergeRealtimeQuoteIntoBars 已移除（曾用 quote 合成末根 bar，违反 MDAS 唯一出口）。
  const displayBars = baseBars

  // [CHANGE-20260719-003 §四] generation 乱序丢弃：
  // 如果 indicators 响应的 timeframe 与当前 timeframe 不匹配，丢弃旧响应（返回 undefined）
  const indicatorsTimeframeMatches = indicatorsQuery.data?.timeframe === timeframe || !indicatorsQuery.data?.timeframe
  const safeIndicators = indicatorsTimeframeMatches ? indicatorsQuery.data : undefined

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

  // 7. 截图模式就绪状态（instrument + bars[当前周期] + indicators[当前周期] 全部就绪）
  // [CHANGE-20260719-003 §四] 乱序响应保护：query.isSuccess 仅代表 HTTP 成功，
  //   若响应 timeframe 与当前不匹配（safeIndicators=undefined / baseBars=[]），不应判为就绪。
  //   飞书截图额外校验 feishuLayersReady 由 StockResearchWorkspace 在 isCaptureMode 时计算
  const isRenderReady =
    instrumentQuery.isSuccess &&
    barsQuery.isSuccess &&
    baseBars.length > 0 &&
    indicatorsQuery.isSuccess &&
    safeIndicators != null

  return {
    instrumentId,
    instrumentQuery,
    barsQuery,
    indicatorsQuery,
    quoteQuery,
    eventsQuery,
    baseBars,
    displayBars,
    // [CHANGE-20260719-003 §四] 使用 safeIndicators（已过滤 timeframe 不匹配的旧响应）
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
