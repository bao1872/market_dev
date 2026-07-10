// [StockResearchWorkspace] - 描述: K 线研究区组件（/market 和 /stock/:symbol 共用）
// 接收 useStockResearchData 返回的已组装数据，渲染 StrategyChart + 行情状态条。
// timeframe 为受控状态：由父组件从 URL 解析并传入，工具栏切换通过 onTimeframeChange 回调写回 URL。
// viewport 按 timeframe 本地保存（不进 URL 避免噪音），切换周期时各周期 viewport 独立不串台。
// 可选 toolbar/rightPanel/chartColumnProps 支持 StockDetailPage 的结构面板开关和截图模式属性。
import { useState, useCallback, type ReactNode } from 'react'
import StrategyChart from '@/components/StrategyChart'
import { createDefaultViewport, type ChartViewport } from '@/components/chartViewport'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import { resolveStrategy } from '@/lib/strategy-manifest'
import type { ResearchSource, DisplayTimeframe } from './stockResearchTypes'
import { ALLOWED_TIMEFRAMES } from './stockResearchTypes'
import clsx from 'clsx'
import type { StockResearchData } from './useStockResearchData'

export interface StockResearchWorkspaceProps {
  data: StockResearchData
  // timeframe 唯一真源：由父组件从 URL 解析传入（受控）
  timeframe: DisplayTimeframe
  // 工具栏切换回调：父组件负责写回 URL
  onTimeframeChange: (timeframe: DisplayTimeframe) => void
  // 研究来源（watchlist=自选/市场搜索；selection=趋势选股）
  source: ResearchSource
  // 策略 key（由父组件根据 URL 传入；watchlist→watchlist_monitor, selection→dsa_selector）
  strategyKey: string
  // 是否截图模式（CaptureStockPage 独立，不经过本组件；StockDetailPage capture=feishu 时传入）
  isCaptureMode?: boolean
  // 右栏是否收起（收起时中栏扩展）
  rightPanelCollapsed?: boolean
  // 图表高度
  height?: number
  // 可选 toolbar 渲染在图表上方（如结构状态开关按钮）
  toolbar?: ReactNode
  // 可选右栏内容渲染为 tv-chart-column 的兄弟（如 StockStructuralStatePanel）
  rightPanel?: ReactNode
  // 是否显示右栏
  showRightPanel?: boolean
  // chart column 的额外 data 属性（如 data-testid, data-render-ready）
  chartColumnProps?: Record<string, string>
}

// 默认视口状态（按 timeframe 存储，本地 state，不进 URL）
function makeDefaultViewport(): ChartViewport {
  return createDefaultViewport(0)
}

export function StockResearchWorkspace({
  data,
  timeframe,
  onTimeframeChange,
  source,
  strategyKey,
  isCaptureMode = false,
  rightPanelCollapsed = false,
  height = 655,
  toolbar,
  rightPanel,
  showRightPanel = false,
  chartColumnProps,
}: StockResearchWorkspaceProps) {
  // viewport 按 timeframe 存储（本地 state，不进 URL 避免噪音）
  const [viewportByTimeframe, setViewportByTimeframe] = useState<Record<string, ChartViewport>>({})

  const handleViewportChange = useCallback((vp: ChartViewport) => {
    setViewportByTimeframe((prev) => ({ ...prev, [timeframe]: vp }))
  }, [timeframe])

  // StrategyChart 工具栏按钮 id 限定为 DisplayTimeframe 允许值；非法值忽略
  const handleChartTimeframeChange = useCallback((tf: string) => {
    if ((ALLOWED_TIMEFRAMES as readonly string[]).includes(tf)) {
      onTimeframeChange(tf as DisplayTimeframe)
    }
  }, [onTimeframeChange])

  // 策略定义（用于 StrategyChart 的 strategyId）
  const strategyDef = resolveStrategy(source, strategyKey)

  const {
    displayBars,
    events,
    indicators,
    instrumentQuery,
    quoteQuery,
    barsQuery,
    indicatorsQuery,
    isBarsLoading,
    isRenderReady,
    quoteStatus,
    barsStatus,
  } = data

  const inst = instrumentQuery.data
  const quote = quoteQuery.data

  // 截图模式：飞书图层就绪校验（bb_upper + visual_segments 非空数组）
  const feishuLayersReady = isCaptureMode
    ? (() => {
        const indicatorData = indicatorsQuery.data?.data
        if (!indicatorData) return false
        const watchlist = indicatorData.watchlist_monitor as
          | Record<string, (number | string | null)[]>
          | undefined
        if (!watchlist) return false
        const bbUpper = watchlist.bb_upper
        if (!Array.isArray(bbUpper) || bbUpper.length === 0) return false
        const dsaSelector = indicatorData.dsa_selector
        if (!dsaSelector || typeof dsaSelector !== 'object') return false
        const segments = (dsaSelector as { visual_segments?: unknown[] }).visual_segments
        if (!Array.isArray(segments)) return false
        return true
      })()
    : true

  const captureRenderReady = isCaptureMode && isRenderReady && feishuLayersReady

  // 错误状态：instrument/bars/indicators 失败时显示明确错误，不伪装为空图
  if (instrumentQuery.isError) {
    return (
      <div className="tv-chart-loading tv-chart-error">
        股票信息加载失败：{instrumentQuery.error?.message ?? '未知错误'}
        <button onClick={() => instrumentQuery.refetch()} className="tv-chart-retry">重试</button>
      </div>
    )
  }

  if (!inst) {
    return (
      <div className="tv-chart-loading">
        {instrumentQuery.isLoading ? '加载股票信息中...' : '未找到股票'}
      </div>
    )
  }

  if (barsQuery.isError) {
    return (
      <div className="tv-chart-loading tv-chart-error">
        K线数据加载失败：{barsQuery.error?.message ?? '未知错误'}
        <button onClick={() => barsQuery.refetch()} className="tv-chart-retry">重试</button>
      </div>
    )
  }

  if (indicatorsQuery.isError) {
    return (
      <div className="tv-chart-loading tv-chart-error">
        指标数据加载失败：{indicatorsQuery.error?.message ?? '未知错误'}
        <button onClick={() => indicatorsQuery.refetch()} className="tv-chart-retry">重试</button>
      </div>
    )
  }

  return (
    <div className={clsx('tv-workspace', rightPanelCollapsed && 'hide-structural-state', isCaptureMode && 'capture-mode')}>
      <section
        className="tv-chart-column"
        data-testid={chartColumnProps?.['data-testid']}
        data-render-ready={isCaptureMode ? (captureRenderReady ? 'true' : 'false') : undefined}
      >
        {toolbar}
        {isBarsLoading ? (
          <div className="tv-chart-loading">行情数据加载中...</div>
        ) : (
          <>
            <StrategyChart
              symbol={inst.symbol}
              displayName={inst.name}
              bars={displayBars}
              events={events}
              indicators={indicators}
              strategyId={strategyDef.id}
              source={source}
              height={height}
              timeframe={timeframe}
              onTimeframeChange={handleChartTimeframeChange}
              viewport={viewportByTimeframe[timeframe] ?? makeDefaultViewport()}
              onViewportChange={handleViewportChange}
              isCaptureMode={isCaptureMode}
            />
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
      {showRightPanel && rightPanel}
    </div>
  )
}
