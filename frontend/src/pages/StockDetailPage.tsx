// 个股详情页（受保护路由，动态参数 :symbol）
// 对应原型：stock-detail.html (V1.6.3)
// 图表工作台核心页面：以 K 线图及图上策略可视化为核心
//
// 用法：路由 /stock/:symbol?source=watchlist&strategy=node&capture=feishu
//   - source: selection（选股结果）/ watchlist（自选监控），默认 watchlist
//   - strategy: 策略标识（dsa/breakout/node/atr/volume/combined），默认 node
//   - capture: feishu 时进入截图模式，隐藏侧栏与用户信息，并暴露 data-render-ready 属性
//
// V1.6.3 精简：无"策略当前计算结果"模块、无"事件时间轴"模块

import { useState, useMemo, useRef, useEffect, useCallback } from 'react'
import { useParams, useSearchParams, useNavigate, useLocation } from 'react-router-dom'
import clsx from 'clsx'
import StrategyChart from '@/components/StrategyChart'
import { StockStructuralStatePanel } from '@/components/StockStructuralStatePanel'
import type { ChartEvent } from '@/components/StrategyChart'
import type { ChartViewport } from '@/components/chartViewport'
import type { IndicatorResponse } from '@/api/endpoints'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import { MARKET_LABELS, formatAmount } from '@/utils/market'
import { resolveBackPath } from './detailNavigation'
import { mapBarsToBarData, mergeRealtimeQuoteIntoBars } from '@/utils/chart'
import {
  useInstrumentBySymbol,
  useBars,
  useIndicators,
  useInstrumentEvents,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useWatchlist,
  useBatchInstruments,
  useStockMemo,
  useUpsertStockMemo,
  useDeleteStockMemo,
  useRealtimeQuote,
} from '@/hooks/useApi'
import { useMutation } from '@tanstack/react-query'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import { useToast } from '@/store/toast'
import { sendStockDetailFeishu, getStockDetailFeishuStatus } from '@/api/endpoints'
import type { StockDetailFeishuCreateResponse, StockDetailFeishuStatusResponse } from '@/api/endpoints'

export default function StockDetailPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  const showToast = useToast((s) => s.show)

  // 解析 URL 参数
  const source = (searchParams.get('source') || 'watchlist') as 'selection' | 'watchlist'
  const strategy = searchParams.get('strategy') || STRATEGY_KEYS.WATCHLIST_MONITOR
  const isCaptureMode = searchParams.get('capture') === 'feishu'
  // [结构状态隐藏开关] - 描述: hideStructuralState=1 / capture=1 / capture=feishu 强制隐藏面板
  const hideStructuralStateParam =
    searchParams.get('hideStructuralState') === '1' ||
    searchParams.get('capture') === '1' ||
    isCaptureMode

  // [结构状态开关] - 描述: 默认隐藏，用户点击显示，localStorage 持久化；强制隐藏时忽略 localStorage
  const [showStructuralState, setShowStructuralState] = useState<boolean>(() => {
    if (hideStructuralStateParam) return false
    return localStorage.getItem('showStructuralState') === 'true'
  })
  const toggleStructuralState = useCallback(() => {
    if (hideStructuralStateParam) return
    setShowStructuralState(prev => {
      const next = !prev
      localStorage.setItem('showStructuralState', String(next))
      return next
    })
  }, [hideStructuralStateParam])
  const shouldShowPanel = showStructuralState && !hideStructuralStateParam

  // 根据 source + strategy 调用 manifest.resolveStrategy 确定默认图层集
  const strategyDef = useMemo(() => resolveStrategy(source, strategy), [source, strategy])

  // 本地状态：当前周期（由 StrategyChart 工具栏联动）
  const [timeframe, setTimeframe] = useState<string>('1d')
  // [chartViewport] - 每个周期独立保存 viewport，切换周期时重置目标周期 viewport
  //   key=timeframe, value=ChartViewport（未保存的周期由 StrategyChart 内部计算默认值）
  //   避免 15m/1h/1w/1mo 切换时 viewport 串台（advice.md 第三节问题 3）
  const [viewportByTimeframe, setViewportByTimeframe] = useState<Record<string, ChartViewport>>({})
  // 全屏查看容器
  const containerRef = useRef<HTMLDivElement>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)

  // [chartViewport] - viewport 变化回调：更新当前周期的 viewport（按周期独立保存）
  const handleViewportChange = useCallback((vp: ChartViewport) => {
    setViewportByTimeframe(prev => ({ ...prev, [timeframe]: vp }))
  }, [timeframe])

  // [chartViewport] - 周期切换：清空目标周期的 viewport 记录，
  //   让 StrategyChart 回退到默认末尾视区（advice.md 第三节问题 3）
  const handleTimeframeChange = useCallback((tf: string) => {
    // [feishu-capture] - 描述: 截图模式锁定日线，禁用周期切换（advice.md v6 第 2 条）
    if (isCaptureMode) return
    setViewportByTimeframe(prev => {
      if (!(tf in prev)) return prev  // 目标周期未保存，无需清空
      const next = { ...prev }
      delete next[tf]
      return next
    })
    setTimeframe(tf)
  }, [isCaptureMode])

  // 数据查询：股票基本信息
  const instrumentQuery = useInstrumentBySymbol(symbol)
  const instrumentId = instrumentQuery.data?.id

  // [Chart] - 按 timeframe 请求对应根数，与 Node Cluster / indicator_contract 对齐
  // 当前后端 bars API 支持 1d/15m/1h/1w/1mo；1m 不在工具栏暴露
  const barsCountByTimeframe: Record<string, number> = {
    '1d': 250,
    '15m': 4000,
    '1h': 1200,
    '1w': 260,
    '1mo': 120,
  }
  const barsCount = barsCountByTimeframe[timeframe] ?? 250

  // 数据查询：K 线行情（依赖 instrumentId，前复权，与 indicators 的 bars 对齐避免数据范围不匹配）
  const barsQuery = useBars(instrumentId, {
    timeframe,
    adj: 'qfq',
    page_size: barsCount,
  })

  // 数据查询：策略图表指标（依赖 instrumentId，与 bars 同 timeframe/adj，拉取相同根数的指标）
  const indicatorsQuery = useIndicators(instrumentId, {
    timeframe,
    adj: 'qfq',
    bars: barsCount,
  })
  const indicators: IndicatorResponse | undefined = indicatorsQuery.data

  // 数据查询：实时报价（交易时段内 10s 轮询）
  const quoteQuery = useRealtimeQuote(instrumentId)

  // 数据查询：策略事件
  const eventsQuery = useInstrumentEvents(instrumentId, { limit: 100 })

  // 数据查询：自选列表（用于判断当前股票是否已在自选）
  const watchlistQuery = useWatchlist()

  // 批量查询自选对应的股票信息，用于将 instrument_id 映射为 symbol 以支持上下切换
  const watchlistInstrumentIds = useMemo(
    () => watchlistQuery.data?.items.map((item) => item.instrument_id) ?? [],
    [watchlistQuery.data],
  )
  const batchInstrumentsQuery = useBatchInstruments(watchlistInstrumentIds)
  const instrumentSymbolMap = useMemo(() => {
    const map = new Map<string, string>()
    if (!batchInstrumentsQuery.data?.items) return map
    for (const inst of batchInstrumentsQuery.data.items) {
      map.set(inst.id, inst.symbol)
    }
    return map
  }, [batchInstrumentsQuery.data])

  // 自选变更操作
  const addWatchlist = useAddToWatchlist()
  const removeWatchlist = useRemoveFromWatchlist()

  // 备忘录
  const [memoOpen, setMemoOpen] = useState(false)
  const [memoContent, setMemoContent] = useState('')
  const [memoNotify, setMemoNotify] = useState(false)
  const stockMemoQuery = useStockMemo(instrumentId)
  const upsertMemo = useUpsertStockMemo()
  const deleteMemo = useDeleteStockMemo()

  // [StockDetailFeishu] - 描述: 异步 Outbox 链路（POST 创建 + 1s 轮询 + 30s 超时）
  const [feishuOpen, setFeishuOpen] = useState(false)
  const [feishuResult, setFeishuResult] = useState<StockDetailFeishuCreateResponse | null>(null)
  const [feishuStatus, setFeishuStatus] = useState<StockDetailFeishuStatusResponse | null>(null)
  const [feishuPolling, setFeishuPolling] = useState(false)
  const feishuPollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const feishuPollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const sendFeishuMutation = useMutation<
    StockDetailFeishuCreateResponse,
    Error,
    { instrId: string }
  >({
    mutationFn: ({ instrId }) => sendStockDetailFeishu(instrId),
  })

  // [StockDetailFeishu] - 描述: 清理轮询定时器（卸载 / 关闭模态框 / 轮询结束均需调用）
  const stopFeishuPolling = useCallback(() => {
    if (feishuPollIntervalRef.current) {
      clearInterval(feishuPollIntervalRef.current)
      feishuPollIntervalRef.current = null
    }
    if (feishuPollTimeoutRef.current) {
      clearTimeout(feishuPollTimeoutRef.current)
      feishuPollTimeoutRef.current = null
    }
    setFeishuPolling(false)
  }, [])

  // 组件卸载时清理轮询定时器，避免内存泄漏
  useEffect(() => {
    return () => stopFeishuPolling()
  }, [stopFeishuPolling])

  useEffect(() => {
    if (stockMemoQuery.data) {
      setMemoContent(stockMemoQuery.data.content)
      setMemoNotify(stockMemoQuery.data.notify_feishu)
    } else {
      setMemoContent('')
      setMemoNotify(false)
    }
  }, [stockMemoQuery.data])

  // 判断当前股票是否已在自选（active=true）
  const inWatchlist = useMemo(() => {
    if (!instrumentId || !watchlistQuery.data) return false
    return watchlistQuery.data.items.some(
      (item) => item.instrument_id === instrumentId && item.active,
    )
  }, [instrumentId, watchlistQuery.data])

  // 转换 Bar 数据为 StrategyChart 需要的 BarData 格式
  // baseBars 用于 indicators 计算（不污染指标）
  // displayBars 合并实时行情，仅用于图表显示；后端已返回 1d partial bar 时不得覆盖
  const baseBars = useMemo(() => mapBarsToBarData(barsQuery.data?.items), [barsQuery.data])
  const backendIsPartial = barsQuery.data?.is_partial === true
  const displayBars = useMemo(
    () => mergeRealtimeQuoteIntoBars(baseBars, quoteQuery.data, timeframe, backendIsPartial),
    [baseBars, quoteQuery.data, timeframe, backendIsPartial],
  )

  // 转换策略事件为 StrategyChart 需要的 ChartEvent 格式
  const events: ChartEvent[] = useMemo(() => {
    if (!eventsQuery.data?.items) return []
    return eventsQuery.data.items.map((e) => ({
      time: e.event_time,
      type: e.event_type,
      title: (e.payload?.title as string) || e.event_type,
      description: e.payload?.description as string | undefined,
    }))
  }, [eventsQuery.data])

  // 最新报价（优先使用实时报价，降级到 barsQuery 最后一根 bar）
  const quote = quoteQuery.data
  const lastBar = barsQuery.data?.items?.[barsQuery.data.items.length - 1] || null
  const currentPrice = quote?.current_price ?? lastBar?.close ?? null
  const openPrice = quote?.open ?? lastBar?.open ?? null
  const highPrice = quote?.high ?? lastBar?.high ?? null
  const lowPrice = quote?.low ?? lastBar?.low ?? null
  const amountValue = quote?.amount ?? lastBar?.amount ?? null
  const changePercent = quote?.change_pct ?? (lastBar && barsQuery.data?.items?.[barsQuery.data.items.length - 2]
    ? ((lastBar.close - barsQuery.data.items[barsQuery.data.items.length - 2].close) / barsQuery.data.items[barsQuery.data.items.length - 2].close * 100)
    : null)
  const isUp = changePercent !== null ? changePercent >= 0 : true

  // [QuoteTrust] - 行情状态：根据 quote 来源/实时性/新鲜度/降级状态展示真实数据状态
  const quoteStatus = useMemo(() => {
    if (!quote) return { label: '加载中', badgeClass: 'status-pill neutral' }
    if (quote.degraded) return { label: '行情降级', badgeClass: 'status-pill warn' }
    if (quote.is_realtime && quote.source === 'pytdx' && quote.freshness_seconds <= 60) {
      return { label: '实时行情', badgeClass: 'status-pill ok' }
    }
    if (quote.source === 'daily_fallback') {
      return { label: '日线回退', badgeClass: 'status-pill neutral' }
    }
    return { label: '数据延迟', badgeClass: 'status-pill warn' }
  }, [quote])

  const barsStatus = useMemo(() => {
    if (barsQuery.isLoading || !barsQuery.data) return null
    const data = barsQuery.data
    if (data.degraded) return { label: 'K线降级', reason: data.degraded_reason }
    if (data.is_partial) return { label: 'K线含未完成 bar', reason: null }
    return { label: `K线来源: ${data.data_source}`, reason: null }
  }, [barsQuery.data, barsQuery.isLoading])

  // 加载状态：股票信息加载中
  const isInstrumentLoading = instrumentQuery.isLoading
  // 行情数据加载中（首次加载且无缓存数据）
  const isBarsLoading = !!instrumentId && barsQuery.isLoading && baseBars.length === 0

  // 截图模式：股票信息 + K线 + 指标加载成功后标记可渲染
  // [advice.md] 事件历史加载失败不应阻止截图（事件仅用于图表标注，非截图必要条件）
  // 历史问题：eventsQuery.isSuccess 必填导致事件接口超时时 data-render-ready 永远为 false，
  // capture worker 等待 30s 超时返回 502，图片无法投递
  //
  // [feishu-capture] - 描述: 截图模式必须等待 K 线 + 指标 + 所有强制图层数据加载完成
  //   advice.md v6 第 2 条：FEISHU_CAPTURE_LAYERS 中的图层（dsa/bb/profile/node/poc）数据必须就绪
  //   indicators 接口返回所有策略数据（compute_all_indicators 遍历 _registry），
  //   所以 watchlist_monitor（bb/profile/node/poc）和 dsa_selector（dsa）数据都在同一个响应中
  const feishuLayersReady = isCaptureMode
    ? (() => {
        const data = indicatorsQuery.data?.data
        if (!data) return false
        // watchlist_monitor 提供 bb/profile/node/poc 数据，校验 bb_upper 非空数组
        const watchlist = data.watchlist_monitor as
          | Record<string, (number | string | null)[]>
          | undefined
        if (!watchlist) return false
        const bbUpper = watchlist.bb_upper
        if (!Array.isArray(bbUpper) || bbUpper.length === 0) return false
        // dsa_selector 提供 dsa 数据，校验 visual_segments 非空数组
        const dsaSelector = data.dsa_selector
        if (!dsaSelector || typeof dsaSelector !== 'object') return false
        const segments = (dsaSelector as { visual_segments?: unknown[] }).visual_segments
        if (!Array.isArray(segments)) return false
        return true
      })()
    : true

  const isRenderReady =
    isCaptureMode &&
    instrumentQuery.isSuccess &&
    barsQuery.isSuccess &&
    indicatorsQuery.isSuccess &&
    feishuLayersReady

  // 来源徽章与返回链接
  const sourceBadge = source === 'selection' ? '选股结果' : '自选监控'

  /** 统一返回按钮：优先使用导航时传入的 returnTo，否则按 source fallback */
  const handleBack = useCallback(() => {
    const returnTo = (location.state as { returnTo?: string } | undefined)?.returnTo
    navigate(resolveBackPath(returnTo, source))
  }, [location.state, navigate, source])

  // 操作：加入/移出自选
  const handleToggleWatchlist = () => {
    if (!instrumentId) return
    if (inWatchlist) {
      removeWatchlist.mutate(instrumentId, {
        onSuccess: () => showToast('操作完成', '已移出自选'),
      })
    } else {
      addWatchlist.mutate(
        { instrument_id: instrumentId, source },
        { onSuccess: () => showToast('操作完成', '已加入自选') },
      )
    }
  }

  // [StockDetailFeishu] - 描述: POST 创建异步任务 → toast 提示入队 → 1s 轮询至 success/failed 或 30s 超时
  const handleSendFeishu = () => {
    if (!instrumentId) return
    setFeishuResult(null)
    setFeishuStatus(null)
    stopFeishuPolling()
    sendFeishuMutation.mutate(
      { instrId: instrumentId },
      {
        onSuccess: (res) => {
          setFeishuResult(res)
          // [StockDetailFeishu] - 描述: 创建成功，提示已入队（展示 test_run_id 前 8 位）
          showToast('已进入发送队列', `test_run_id: ${res.test_run_id.slice(0, 8)}`)
          setFeishuPolling(true)
          // 30s 超时兜底：超时后停止轮询并提示用户去消息中心查看
          feishuPollTimeoutRef.current = setTimeout(() => {
            stopFeishuPolling()
            showToast('发送超时', '请到消息中心查看最终状态')
          }, 30000)
          // 每 1s 轮询状态，命中终态（success/failed）即停止
          feishuPollIntervalRef.current = setInterval(async () => {
            let status: StockDetailFeishuStatusResponse
            try {
              status = await getStockDetailFeishuStatus(res.test_run_id)
            } catch (e) {
              // [StockDetailFeishu] - 描述: 轮询查询失败，停止轮询并报错（不静默兜底继续跑）
              stopFeishuPolling()
              showToast('状态查询失败', e instanceof Error ? e.message : '请重试')
              return
            }
            setFeishuStatus(status)
            if (status.overall_status === 'success' || status.overall_status === 'failed') {
              stopFeishuPolling()
              if (status.overall_status === 'success') {
                // [StockDetailFeishu] - 描述: 成功分别展示 card/image 状态；image 为 not_created 时只展示卡片
                const parts: string[] = []
                parts.push(
                  `卡片${status.card_status === 'success' ? '已送达' : status.card_status}`,
                )
                if (status.image_status !== 'not_created') {
                  parts.push(
                    `图片${status.image_status === 'success' ? '已送达' : status.image_status}`,
                  )
                }
                showToast('发送成功', parts.join(' · '))
              } else {
                // [StockDetailFeishu] - 描述: 失败展示 failed_step / error_code / error_message
                showToast(
                  '发送失败',
                  `${status.failed_step ?? '未知步骤'} · ${status.error_code ?? ''} · ${status.error_message ?? ''}`.trim(),
                )
              }
            }
          }, 1000)
        },
        onError: () => showToast('发送失败', '请重试'),
      },
    )
  }

  // 全屏查看
  const handleFullscreen = () => {
    if (!document.fullscreenElement) {
      containerRef.current?.requestFullscreen().catch(() => {})
    } else {
      document.exitFullscreen().catch(() => {})
    }
  }
  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', handler)
    return () => document.removeEventListener('fullscreenchange', handler)
  }, [])

  // 在自选列表中上下切换股票
  const watchlistItems = watchlistQuery.data?.items ?? []
  const currentIndex = instrumentId
    ? watchlistItems.findIndex((item) => item.instrument_id === instrumentId)
    : -1
  const canNavigate =
    watchlistItems.length >= 2 && currentIndex >= 0
  const navigateToStock = (direction: number) => {
    if (!canNavigate) return
    const nextIndex = (currentIndex + direction + watchlistItems.length) % watchlistItems.length
    const target = watchlistItems[nextIndex]
    const targetSymbol = instrumentSymbolMap.get(target.instrument_id)
    if (!targetSymbol) return
    navigate(`/stock/${targetSymbol}?source=watchlist&strategy=${strategy}`)
  }

  // 股票信息加载中
  if (isInstrumentLoading) {
    return (
      <div
        className="tv-content"
        data-testid="stock-detail-capture"
        data-render-ready="false"
        ref={containerRef}
      >
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
            <button className="icon-btn tv-back" onClick={handleBack} title="返回">←</button>
            <div>
              <div className="tv-symbol-title">
                <span>加载中...</span>
                <span className="tv-code">{symbol || ''}</span>
              </div>
              <div className="tv-symbol-meta">正在获取股票数据</div>
            </div>
          </div>
        </div>
        <div className="tv-workspace">
          <section className="tv-chart-column">
            <div className="tv-chart-loading">行情数据加载中...</div>
          </section>
        </div>
      </div>
    )
  }

  // 股票不存在或查询出错
  if (!instrumentQuery.data) {
    return (
      <div
        className="tv-content"
        data-testid="stock-detail-capture"
        data-render-ready="false"
        ref={containerRef}
      >
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
            <button className="icon-btn tv-back" onClick={handleBack} title="返回">←</button>
            <div>
              <div className="tv-symbol-title">
                <span>未找到股票</span>
                <span className="tv-code">{symbol || ''}</span>
              </div>
              <div className="tv-symbol-meta">
                {instrumentQuery.isError ? '股票信息查询失败，请稍后重试' : '请检查股票代码是否正确'}
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const inst = instrumentQuery.data
  // [QuoteTrust] - 元信息：市场 · 人民币 · 行情状态 · update_time · K线状态
  const metaParts = [
    MARKET_LABELS[inst.market] || inst.market,
    '人民币',
    quoteStatus.label,
    quote?.update_time ? `更新 ${formatShanghaiTimeShort(quote.update_time)}` : null,
    barsStatus ? barsStatus.label : null,
  ].filter(Boolean)

  return (
    <div
      className="tv-content"
      ref={containerRef}
    >
      {/* ===== 股票信息栏 ===== */}
      <div className="tv-symbol-bar">
        <div className="tv-symbol-left">
          <button className="icon-btn tv-back" onClick={handleBack} title="返回">←</button>
          <div>
            <div className="tv-symbol-title">
              <span>{inst.name}</span>
              <span className="tv-code">{inst.symbol}</span>
              <span className="status-pill ok">{sourceBadge}</span>
            </div>
            <div className="tv-symbol-meta">{metaParts.join(' · ')}</div>
          </div>
        </div>
        {/* 报价条：现价/涨跌/开盘/最高/最低/成交额 */}
        <div className="tv-quote-strip">
          <div>
            <span>现价</span>
            <b className={isUp ? 'market-up' : 'market-down'}>{currentPrice !== null ? currentPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>涨跌</span>
            <b className={isUp ? 'market-up' : 'market-down'}>
              {changePercent !== null ? `${isUp ? '+' : ''}${changePercent.toFixed(2)}%` : '--'}
            </b>
          </div>
          <div>
            <span>开盘</span>
            <b>{openPrice !== null ? openPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最高</span>
            <b>{highPrice !== null ? highPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>最低</span>
            <b>{lowPrice !== null ? lowPrice.toFixed(2) : '--'}</b>
          </div>
          <div>
            <span>成交额</span>
            <b>{amountValue !== null ? formatAmount(amountValue) : '--'}</b>
          </div>
        </div>
        {/* 操作：加入/移出自选、切换、全屏（截图模式隐藏全部按钮） */}
        {!isCaptureMode && (
          <div className="actions">
            <button
              className={clsx('btn', inWatchlist ? 'danger' : 'primary')}
              onClick={handleToggleWatchlist}
              disabled={!instrumentId || addWatchlist.isPending || removeWatchlist.isPending}
            >
              {inWatchlist ? '移出自选' : '加入自选'}
            </button>
            <button className="btn small" onClick={() => navigateToStock(-1)} disabled={!canNavigate}>
              上一只
            </button>
            <button className="btn small" onClick={() => navigateToStock(1)} disabled={!canNavigate}>
              下一只
            </button>
            <button className="btn small" onClick={handleFullscreen}>
              {isFullscreen ? '退出全屏' : '全屏查看'}
            </button>
            <button className="btn small" onClick={() => setMemoOpen(true)}>
              备忘录
            </button>
            <button
              className="btn small"
              onClick={() => {
                stopFeishuPolling()
                setFeishuResult(null)
                setFeishuStatus(null)
                setFeishuOpen(true)
              }}
              disabled={!instrumentId}
            >
              发送到飞书
            </button>
          </div>
        )}
      </div>

      {/* 备忘录模态框 */}
      {memoOpen && (
        <div className="modal-backdrop open" onClick={() => setMemoOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 500 }}>
            <div className="modal-head">
              <h3>备忘录 - {inst.name}</h3>
              <button className="icon-btn" onClick={() => setMemoOpen(false)}>×</button>
            </div>
            <div className="modal-body">
              <textarea
                className="memo-textarea"
                value={memoContent}
                onChange={(e) => setMemoContent(e.target.value)}
                placeholder="输入备忘录内容..."
                rows={6}
              />
              <label className="memo-switch">
                <input
                  type="checkbox"
                  checked={memoNotify}
                  onChange={(e) => setMemoNotify(e.target.checked)}
                />
                <span>当该股票盘中触发监控事件时，在飞书通知中附带此备忘录</span>
              </label>
            </div>
            <div className="modal-foot">
              {stockMemoQuery.data && (
                <button
                  className="btn danger"
                  onClick={() => {
                    deleteMemo.mutate(instrumentId!, {
                      onSuccess: () => {
                        showToast('已删除', '备忘录已删除')
                        setMemoOpen(false)
                        setMemoContent('')
                        setMemoNotify(false)
                      },
                    })
                  }}
                  disabled={deleteMemo.isPending}
                >
                  删除
                </button>
              )}
              <button
                className="btn primary"
                onClick={() => {
                  if (!memoContent.trim()) {
                    showToast('提示', '备忘录内容不能为空')
                    return
                  }
                  upsertMemo.mutate(
                    { instrumentId: instrumentId!, payload: { content: memoContent, notify_feishu: memoNotify } },
                    {
                      onSuccess: () => {
                        showToast('已保存', '备忘录已保存')
                        setMemoOpen(false)
                      },
                      onError: () => showToast('保存失败', '请重试'),
                    },
                  )
                }}
                disabled={upsertMemo.isPending || !memoContent.trim()}
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}

      {/* [StockDetailFeishu] - 发送到飞书模态框：后端自动选择唯一 active 渠道 + 异步轮询状态 */}
      {feishuOpen && (
        <div
          className="modal-backdrop open"
          onClick={() => {
            if (!sendFeishuMutation.isPending && !feishuPolling) {
              stopFeishuPolling()
              setFeishuOpen(false)
            }
          }}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 500 }}>
            <div className="modal-head">
              <h3>发送到飞书 - {inst.name}</h3>
              <button
                className="icon-btn"
                onClick={() => {
                  if (!sendFeishuMutation.isPending && !feishuPolling) {
                    stopFeishuPolling()
                    setFeishuOpen(false)
                  }
                }}
              >
                ×
              </button>
            </div>
            <div className="modal-body">
              <p className="feishu-channel-label">
                点击发送将把当前个股详情（含备忘录）推送到您已启用的飞书渠道。
              </p>

              {/* [StockDetailFeishu] - 描述: 轮询中展示状态，image 为 not_created 时只展示卡片 */}
              {(feishuResult || feishuStatus || feishuPolling) && (
                <div className="feishu-status-box">
                  {feishuPolling && !feishuStatus && (
                    <div className="feishu-status-polling">投递中...</div>
                  )}
                  {feishuStatus && (
                    <>
                      <div className="feishu-status-row">
                        卡片投递:{' '}
                        <b className={`feishu-status-${feishuStatus.card_status}`}>
                          {feishuStatus.card_status}
                        </b>
                      </div>
                      {feishuStatus.image_status !== 'not_created' && (
                        <div className="feishu-status-row">
                          图片投递:{' '}
                          <b className={`feishu-status-${feishuStatus.image_status}`}>
                            {feishuStatus.image_status}
                          </b>
                        </div>
                      )}
                      {feishuStatus.overall_status === 'failed' && (
                        <div className="feishu-status-error">
                          <div>失败步骤: {feishuStatus.failed_step ?? '-'}</div>
                          <div>错误码: {feishuStatus.error_code ?? '-'}</div>
                          <div>错误信息: {feishuStatus.error_message ?? '-'}</div>
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
            <div className="modal-foot">
              <button
                className="btn primary"
                disabled={
                  sendFeishuMutation.isPending ||
                  feishuPolling ||
                  !instrumentId
                }
                onClick={handleSendFeishu}
              >
                {sendFeishuMutation.isPending
                  ? '发送中...'
                  : feishuPolling
                    ? '投递中...'
                    : '发送'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ===== 工作区：双列布局（图表 + 结构状态因子面板） ===== */}
      <div className={clsx('tv-workspace', { 'capture-mode': isCaptureMode, 'hide-structural-state': !shouldShowPanel })}>
        {/* 图表区（同时承载 toggle 按钮的定位上下文） */}
        <section
          className="tv-chart-column"
          data-testid="stock-detail-capture"
          data-render-ready={isRenderReady ? 'true' : 'false'}
        >
          {/* 结构状态开关 toolbar：放在图表上方，按钮右对齐，默认可见 */}
          {!hideStructuralStateParam && instrumentId && (
            <div className="structural-state-toolbar">
              <button
                type="button"
                className="structural-state-toggle-btn"
                onClick={toggleStructuralState}
                aria-label="切换结构状态面板"
              >
                {showStructuralState ? '隐藏结构状态' : '显示结构状态'}
              </button>
            </div>
          )}
          {isBarsLoading ? (
            <div className="tv-chart-loading">行情数据加载中...</div>
          ) : (
            <>
              {/* StrategyChart 内部渲染：工具栏 + 策略图示区 + 画布区 */}
              <StrategyChart
                symbol={inst.symbol}
                bars={displayBars}
                events={events}
                indicators={indicators}
                strategyId={strategyDef.id}
                source={source}
                height={655}
                timeframe={timeframe}
                onTimeframeChange={handleTimeframeChange}
                viewport={viewportByTimeframe[timeframe]}
                onViewportChange={handleViewportChange}
                isCaptureMode={isCaptureMode}
              />
              {/* [QuoteTrust] - 状态栏：行情来源/实时性/新鲜度、K线数据源/as_of/is_partial/degraded */}
              <div className="tv-chart-status">
                <span className={quoteStatus.badgeClass}>{quoteStatus.label}</span>
                {quote?.update_time && <span>quote更新: {formatShanghaiTimeShort(quote.update_time)}</span>}
                {quote?.freshness_seconds !== undefined && (
                  <span>新鲜度: {quote.freshness_seconds.toFixed(1)}s</span>
                )}
                {barsStatus && (
                  <span title={barsStatus.reason ?? undefined}>
                    {barsStatus.label}
                    {barsStatus.reason ? ` · ${barsStatus.reason}` : ''}
                  </span>
                )}
                <span>复权：前复权</span>
                <span>时区：Asia/Shanghai</span>
              </div>
            </>
          )}
        </section>
        {/* 结构状态因子面板（默认隐藏，用户开关控制；截图模式强制隐藏；Temporal Features 卡片随之显示） */}
        {shouldShowPanel && instrumentId && (
          <aside className="tv-side-column">
            <StockStructuralStatePanel instrumentId={instrumentId} />
          </aside>
        )}
      </div>
    </div>
  )
}
