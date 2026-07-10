// [StockResearchWorkspace] - 描述: 中栏 K 线研究区组件（从 StockDetailPage 抽取）
// 接收 useStockResearchData 返回的已组装数据，渲染 StrategyChart + 行情状态条。
// 当前仅由 MarketWorkspacePage 使用；StockDetailPage 仍保留独立实现，下一独立 PR 迁移复用。
// 右栏（StockStructuralStatePanel）的显示/隐藏由父组件控制，本组件只渲染中栏。
// timeframe 为受控状态：由父组件（MarketWorkspacePage）从 URL 解析并传入，工具栏切换通过 onTimeframeChange 回调写回 URL。
// viewport 按 timeframe 本地保存（不进 URL 避免噪音），切换周期时各周期 viewport 独立不串台。
import { useState, useCallback } from 'react'
import StrategyChart from '@/components/StrategyChart'
import { createDefaultViewport, type ChartViewport } from '@/components/chartViewport'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import { resolveStrategy } from '@/lib/strategy-manifest'
import type { ResearchSource, DisplayTimeframe } from '@/features/market-workspace/marketWorkspaceUrlState'
import { ALLOWED_TIMEFRAMES } from '@/features/market-workspace/marketWorkspaceUrlState'
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
  // 是否截图模式（CaptureStockPage 独立，不经过本组件；但保留兼容）
  isCaptureMode?: boolean
  // 右栏是否收起（收起时中栏扩展）
  rightPanelCollapsed?: boolean
  // 图表高度
  height?: number
}

// 默认视口状态（按 timeframe 存储，本地 state，不进 URL）
function makeDefaultViewport(): ChartViewport {
  return createDefaultViewport(0)
}

// 根据 timeframe 生成 K线状态文案（partial 文案包含当前周期，避免所有周期都显示"日线"）
function barsStatusLabel(tf: DisplayTimeframe, isPartial: boolean): string {
  const labelMap: Record<DisplayTimeframe, string> = {
    '1d': '日线',
    '15m': '15分钟K线',
    '1h': '1小时K线',
    '1w': '周线',
    '1mo': '月线',
  }
  const period = labelMap[tf] ?? '周期数据'
  return isPartial ? `盘中 partial bar（${tf}）` : `完整${period}`
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
    backendIsPartial,
  } = data

  const inst = instrumentQuery.data
  const quote = quoteQuery.data

  // 行情状态（简化版，完整状态条逻辑保留在 StockDetailPage 中）
  // 非实时非降级时统一显示"行情回退"，避免在 15m/1h/1w/1mo 下误显示"日线回退"
  const quoteStatus = {
    label: quote?.is_realtime ? '实时行情' : quote?.degraded ? '行情降级' : '行情回退',
    badgeClass: quote?.is_realtime ? 'tag ok' : quote?.degraded ? 'tag warn' : 'tag',
  }

  const barsStatus = barsQuery.data
    ? {
        label: barsStatusLabel(timeframe, backendIsPartial),
        reason: barsQuery.data.degraded_reason ?? undefined,
      }
    : null

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
    <div className={clsx('tv-workspace', rightPanelCollapsed && 'hide-structural-state')}>
      <div className="tv-chart-column">
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
      </div>
    </div>
  )
}
