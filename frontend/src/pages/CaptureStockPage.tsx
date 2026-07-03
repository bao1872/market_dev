// [Capture] - 描述: 专用 Capture 页面 - 截图模式专用，不经过 ProtectedLayout/AppShell
//
// 用法：路由 /capture/stock/:symbol?source=watchlist&strategy=watchlist_monitor&capture=feishu&token=xxx
//
// 设计要点（修复 C.7 调查发现的 30s 截图超时根因）：
// 1. 不经过 ProtectedLayout / SubscriberRoute / AppShell（避免认证守卫与全局布局副作用）
// 2. 只使用 captureClient（不使用 apiClient），capture token 由本页自行写入 CAPTURE_TOKEN_KEY
// 3. 只加载截图必需数据：instrument / bars / indicators / quote
//    不加载 watchlist / memo / events / batchInstruments（避免不必要查询阻塞渲染）
// 4. data-render-ready 只依赖 bars + indicators 加载完成（不依赖 events）
//    历史问题：事件查询接口超时导致 data-render-ready 永远为 false，capture worker 30s 超时返回 502
// 5. 全屏渲染图表区域，无侧栏/导航/操作按钮/模态框
// 6. 复用 StockDetailPage 的图表组件（StrategyChart）与策略配置（resolveStrategy）

import { useEffect, useMemo, useState, useCallback } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { captureClient } from '@/api/client'
import { CAPTURE_TOKEN_KEY } from '@/store/auth'
import StrategyChart from '@/components/StrategyChart'
import type { BarData } from '@/components/StrategyChart'
import type { ChartViewport } from '@/components/chartViewport'
import type {
  Instrument,
  BarListResponse,
  IndicatorResponse,
  QuoteResponse,
} from '@/api/endpoints'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'

// 市场代码 -> 中文标签映射（与 StockDetailPage 保持一致）
const MARKET_LABELS: Record<string, string> = {
  A_SHARE: 'A股',
  STAR: '科创板',
  MAIN: '主板',
  SME: '中小板',
  GEM: '创业板',
  BSE: '北交所',
}

// 格式化成交额（元 -> 亿/万，与 StockDetailPage 保持一致）
function formatAmount(v: number): string {
  if (!v || v <= 0) return '--'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return v.toFixed(0)
}

export default function CaptureStockPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const [searchParams] = useSearchParams()

  // [capture-mode] 写入 capture token 到独立 storage key
  // 本页不经过 ProtectedLayout（ProtectedLayout 负责在 /stock/:symbol 路由写入 token），
  // 需自行将 URL token 写入 CAPTURE_TOKEN_KEY，captureClient 拦截器从该 key 读取并注入 Authorization
  useEffect(() => {
    const captureToken = searchParams.get('token')
    if (captureToken) {
      localStorage.setItem(CAPTURE_TOKEN_KEY, captureToken)
    }
  }, [searchParams])

  // 解析 URL 参数：source/strategy 默认与 capture worker 调用一致
  const source = 'watchlist' as const
  const strategy = searchParams.get('strategy') || STRATEGY_KEYS.WATCHLIST_MONITOR

  // 策略定义（复用 StockDetailPage 的策略解析逻辑）
  const strategyDef = useMemo(() => resolveStrategy(source, strategy), [source, strategy])

  // 截图模式锁定日线（与 StockDetailPage capture 模式行为一致）
  const [timeframe] = useState<string>('1d')
  // [chartViewport] - 每个周期独立保存 viewport（截图模式仅日线，保留结构以复用 StrategyChart 受控 viewport）
  const [viewportByTimeframe, setViewportByTimeframe] = useState<Record<string, ChartViewport>>({})
  const handleViewportChange = useCallback((vp: ChartViewport) => {
    setViewportByTimeframe((prev) => ({ ...prev, [timeframe]: vp }))
  }, [timeframe])

  // 数据查询：股票基本信息（使用 captureClient，不使用 apiClient）
  const instrumentQuery = useQuery({
    queryKey: ['instruments', 'by-symbol', symbol],
    queryFn: async () => {
      const { data } = await captureClient.get<Instrument>(
        `/instruments/by-symbol/${symbol}`,
      )
      return data
    },
    enabled: !!symbol,
    staleTime: 5 * 60 * 1000,
  })
  const instrumentId = instrumentQuery.data?.id

  // 数据查询：K 线行情（前复权，与 indicators 的 bars=250 对齐）
  const barsQuery = useQuery({
    queryKey: ['bars', instrumentId, { timeframe, adj: 'qfq', page_size: 250 }],
    queryFn: async () => {
      const { data } = await captureClient.get<BarListResponse>(
        `/api/v1/instruments/${instrumentId}/bars`,
        { params: { timeframe, adj: 'qfq', page_size: 250 } },
      )
      return data
    },
    enabled: !!instrumentId,
    staleTime: 60 * 1000,
    refetchInterval: false, // 截图为静态场景，不轮询
  })

  // 数据查询：策略图表指标（与 bars 同 timeframe/adj，拉取最近 250 根 bar 的指标）
  const indicatorsQuery = useQuery({
    queryKey: ['indicators', 'v3', instrumentId, { timeframe, adj: 'qfq', bars: 250 }],
    queryFn: async () => {
      const { data } = await captureClient.get<IndicatorResponse>(
        `/api/v1/instruments/${instrumentId}/indicators`,
        { params: { timeframe, adj: 'qfq', bars: 250 } },
      )
      return data
    },
    enabled: !!instrumentId,
    staleTime: 60 * 1000,
    refetchInterval: false,
  })

  // 数据查询：实时报价（非交易时段降级到数据库最新日线）
  const quoteQuery = useQuery({
    queryKey: ['quote', instrumentId],
    queryFn: async () => {
      const { data } = await captureClient.get<QuoteResponse>(
        `/api/v1/instruments/${instrumentId}/quote`,
      )
      return data
    },
    enabled: !!instrumentId,
    staleTime: 30 * 1000,
    refetchInterval: false,
  })

  // 转换 Bar 数据为 StrategyChart 需要的 BarData 格式
  const bars: BarData[] = useMemo(() => {
    if (!barsQuery.data?.items) return []
    return barsQuery.data.items.map((b) => ({
      time: b.trade_time || b.trade_date || '',
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.volume,
    }))
  }, [barsQuery.data])

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

  // [feishu-capture] - 描述: 截图模式渲染就绪标志
  // 只依赖 bars + indicators 加载完成（不依赖 events）
  // 历史根因：事件查询接口超时导致 data-render-ready 永远为 false，capture worker 30s 超时返回 502
  const isRenderReady = barsQuery.isSuccess && indicatorsQuery.isSuccess

  // 加载状态：股票信息加载中
  if (instrumentQuery.isLoading) {
    return (
      <div
        className="tv-content"
        data-testid="stock-detail-capture"
        data-render-ready="false"
      >
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
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
      >
        <div className="tv-symbol-bar">
          <div className="tv-symbol-left">
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
  const metaParts = [
    MARKET_LABELS[inst.market] || inst.market,
    '人民币',
    '实时行情',
  ].filter(Boolean)

  return (
    <div
      className="tv-content"
      data-testid="stock-detail-capture"
      data-render-ready={isRenderReady ? 'true' : 'false'}
    >
      {/* ===== 股票信息栏（精简：名称+代码+报价，无操作按钮） ===== */}
      <div className="tv-symbol-bar">
        <div className="tv-symbol-left">
          <div>
            <div className="tv-symbol-title">
              <span>{inst.name}</span>
              <span className="tv-code">{inst.symbol}</span>
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
      </div>

      {/* ===== 工作区：单列布局（全屏图表，无侧栏/导航） ===== */}
      <div className="tv-workspace">
        <section className="tv-chart-column">
          {bars.length === 0 ? (
            <div className="tv-chart-loading">行情数据加载中...</div>
          ) : (
            <>
              {/* StrategyChart 内部渲染：工具栏 + 策略图示区 + 画布区 */}
              <StrategyChart
                symbol={inst.symbol}
                bars={bars}
                indicators={indicatorsQuery.data}
                strategyId={strategyDef.id}
                source={source}
                height={655}
                timeframe={timeframe}
                viewport={viewportByTimeframe[timeframe]}
                onViewportChange={handleViewportChange}
                isCaptureMode
              />
              {/* 状态栏：复权/时区（不依赖运行时数据，精简展示） */}
              <div className="tv-chart-status">
                <span>复权：前复权</span>
                <span>时区：Asia/Shanghai</span>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
