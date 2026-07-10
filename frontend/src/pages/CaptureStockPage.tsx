// [Capture] - 描述: 专用 Capture 页面 - 截图模式专用，不经过 ProtectedLayout/AppShell
//
// 用法：路由 /capture/stock/:symbol?capture=feishu&token=xxx&instrument_id=xxx
//
// 设计要点（修复 C.7 调查发现的 30s 截图超时根因）：
// 1. 不经过 ProtectedLayout / SubscriberRoute / AppShell（避免认证守卫与全局布局副作用）
// 2. 只使用 captureClient（不使用 apiClient），capture token 由本页自行写入 CAPTURE_TOKEN_KEY
// 3. 只发起一个业务数据请求：GET /api/v1/capture/stocks/{instrument_id}/snapshot
//    后端 Snapshot 一次返回 instrument / bars / indicators / events / quote
//    不加载 watchlist / memo / events / batchInstruments（避免不必要查询阻塞渲染）
// 4. data-render-ready 只依赖 bars + indicators 加载完成（不依赖 events）
//    历史根因：事件查询接口超时导致 data-render-ready 永远为 false，capture worker 30s 超时返回 502
// 5. 全屏渲染图表区域，无侧栏/导航/操作按钮/模态框
// 6. 复用 StockDetailPage 的图表组件（StrategyChart）与策略配置（resolveStrategy）

import { useEffect, useMemo, useState, useCallback } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { captureClient } from '@/api/client'
import { CAPTURE_TOKEN_KEY } from '@/store/auth'
import StrategyChart from '@/components/StrategyChart'
import type { ChartViewport } from '@/components/chartViewport'
import type { CaptureSnapshotResponse } from '@/api/endpoints'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import { MARKET_LABELS, formatAmount } from '@/utils/market'
import { mapBarsToBarData } from '@/utils/chart'
import { formatShanghaiTimeShort } from '@/utils/datetime'

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

  // 解析 URL 参数：instrument_id 由 capture worker URL 传入；strategy 默认 watchlist_monitor
  const instrumentId = searchParams.get('instrument_id') || undefined
  const source = 'watchlist' as const
  const strategy = searchParams.get('strategy') || STRATEGY_KEYS.WATCHLIST_MONITOR

  // 策略定义（复用 StockDetailPage 的策略解析逻辑）
  const strategyDef = useMemo(() => resolveStrategy(source, strategy), [source, strategy])

  // [capture-realtime] - 截图周期优先使用 URL 传入的 timeframe（默认 1d），支持盘中 15m 等
  const timeframeParam = searchParams.get('timeframe') || '1d'
  const [timeframe] = useState<string>(timeframeParam)
  const sourceBarTime = searchParams.get('source_bar_time') || undefined
  // [chartViewport] - 每个周期独立保存 viewport（截图模式仅日线，保留结构以复用 StrategyChart 受控 viewport）
  const [viewportByTimeframe, setViewportByTimeframe] = useState<Record<string, ChartViewport>>({})
  const handleViewportChange = useCallback((vp: ChartViewport) => {
    setViewportByTimeframe((prev) => ({ ...prev, [timeframe]: vp }))
  }, [timeframe])

  // [Capture] - 描述: 截图模式唯一业务数据请求
  // 通过 Capture Token 访问专用 Snapshot API，不调用普通业务端点
  const snapshotQuery = useQuery({
    queryKey: ['capture', 'snapshot', instrumentId],
    queryFn: async () => {
      if (!instrumentId) throw new Error('缺少 instrument_id 参数')
      const { data } = await captureClient.get<CaptureSnapshotResponse>(
        `/api/v1/capture/stocks/${instrumentId}/snapshot`,
        {
          params: {
            timeframe,
            ...(sourceBarTime ? { source_bar_time: sourceBarTime } : {}),
            // 截图链路固定强制实时计算，跳过 Redis 指标缓存，不复用旧指标
            force_refresh: 1,
            capture: 1,
          },
        },
      )
      return data
    },
    enabled: !!instrumentId,
    staleTime: 5 * 60 * 1000,
    refetchInterval: false, // 截图为静态场景，不轮询
  })

  const snapshot = snapshotQuery.data
  const inst = snapshot?.instrument
  const barsResponse = snapshot?.bars
  const indicatorsResponse = snapshot?.indicators

  // 转换 Bar 数据为 StrategyChart 需要的 BarData 格式
  const bars = useMemo(() => mapBarsToBarData(barsResponse?.items), [barsResponse])

  // 最新报价（Snapshot 当前未单独返回 quote，使用 bars 最后一根 bar）
  const lastBar = barsResponse?.items?.[barsResponse.items.length - 1] || null
  const prevBar = barsResponse?.items?.[barsResponse.items.length - 2] || null
  const currentPrice = lastBar?.close ?? null
  const openPrice = lastBar?.open ?? null
  const highPrice = lastBar?.high ?? null
  const lowPrice = lastBar?.low ?? null
  const amountValue = (lastBar as { amount?: number } | null)?.amount ?? null
  const changePercent = lastBar && prevBar
    ? ((lastBar.close - prevBar.close) / prevBar.close * 100)
    : null
  const isUp = changePercent !== null ? changePercent >= 0 : true

  // [feishu-capture] - 描述: 截图模式渲染就绪标志
  // 只依赖 bars + indicators 加载完成（不依赖 events）
  // 历史根因：事件查询接口超时导致 data-render-ready 永远为 false，capture worker 30s 超时返回 502
  const isRenderReady = !!barsResponse?.items?.length && !!indicatorsResponse

  // 加载状态：股票信息加载中
  if (snapshotQuery.isLoading) {
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

  // 股票不存在、缺少 instrument_id 或查询出错
  if (!inst) {
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
                {!instrumentId
                  ? '缺少 instrument_id 参数'
                  : snapshotQuery.isError
                    ? '股票信息查询失败，请稍后重试'
                    : '请检查股票代码是否正确'}
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  const metaParts = [
    MARKET_LABELS[inst.market] || inst.market,
    '人民币',
    '实时行情',
  ].filter(Boolean)

  return (
    <div
      className="tv-content"
    >
      {/* ===== 股票信息栏（精简：名称+代码+报价，无操作按钮） ===== */}
      <div className="tv-symbol-bar">
        <div className="tv-symbol-left">
          <div>
              <div className="tv-symbol-title">
                <span>{inst.name}（{inst.symbol}）</span>
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
        <section
          className="tv-chart-column"
          data-testid="stock-detail-capture"
          data-render-ready={isRenderReady ? 'true' : 'false'}
        >
          {bars.length === 0 ? (
            <div className="tv-chart-loading">行情数据加载中...</div>
          ) : (
            <>
              {/* StrategyChart 内部渲染：工具栏 + 策略图示区 + 画布区 */}
              <StrategyChart
                symbol={inst.symbol}
                displayName={inst.name}
                bars={bars}
                indicators={indicatorsResponse}
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
                {barsResponse?.data_source && (
                  <span>K线来源: {barsResponse.data_source}</span>
                )}
                {barsResponse?.is_partial && <span>含未完成 bar</span>}
                {snapshot?.last_live_bar_time && (
                  <span>实时bar: {formatShanghaiTimeShort(snapshot.last_live_bar_time)}</span>
                )}
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
