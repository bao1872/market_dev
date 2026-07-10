// [StockResearchWorkspace] - 描述: 中栏唯一 K 线研究区组件（从 StockDetailPage 抽取）
// 接收 useStockResearchData 返回的已组装数据，渲染 StrategyChart + 行情状态条。
// 被 StockDetailPage 和 MarketWorkspacePage 复用，禁止复制 StrategyChart 或请求链路。
// 右栏（StockStructuralStatePanel）的显示/隐藏由父组件控制，本组件只渲染中栏。
import { useState, useCallback } from 'react'
import StrategyChart from '@/components/StrategyChart'
import { createDefaultViewport, type ChartViewport } from '@/components/chartViewport'
import { formatShanghaiTimeShort } from '@/utils/datetime'
import { resolveStrategy } from '@/lib/strategy-manifest'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import clsx from 'clsx'
import type { StockResearchData } from './useStockResearchData'

export interface StockResearchWorkspaceProps {
  data: StockResearchData
  // source 标记（'selection' | 'watchlist'）
  source: string
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

export function StockResearchWorkspace({
  data,
  source,
  isCaptureMode = false,
  rightPanelCollapsed = false,
  height = 655,
}: StockResearchWorkspaceProps) {
  const [timeframe, setTimeframe] = useState('1d')
  // viewport 按 timeframe 存储（本地 state，不进 URL 避免噪音）
  const [viewportByTimeframe, setViewportByTimeframe] = useState<Record<string, ChartViewport>>({})

  const handleTimeframeChange = useCallback((tf: string) => {
    setTimeframe(tf)
  }, [])

  const handleViewportChange = useCallback((vp: ChartViewport) => {
    setViewportByTimeframe((prev) => ({ ...prev, [timeframe]: vp }))
  }, [timeframe])

  // 策略定义（用于 StrategyChart 的 strategyId）
  const strategyDef = resolveStrategy(source === 'selection' ? 'selection' : 'watchlist', STRATEGY_KEYS.WATCHLIST_MONITOR)

  const {
    displayBars,
    events,
    indicators,
    instrumentQuery,
    quoteQuery,
    barsQuery,
    isBarsLoading,
    backendIsPartial,
  } = data

  const inst = instrumentQuery.data
  const quote = quoteQuery.data

  // 行情状态（简化版，完整状态条逻辑保留在 StockDetailPage 中）
  const quoteStatus = {
    label: quote?.is_realtime ? '实时行情' : quote?.degraded ? '行情降级' : '日线回退',
    badgeClass: quote?.is_realtime ? 'tag ok' : quote?.degraded ? 'tag warn' : 'tag',
  }

  const barsStatus = barsQuery.data
    ? {
        label: backendIsPartial ? '盘中 partial bar' : '完整日线',
        reason: barsQuery.data.degraded_reason ?? undefined,
      }
    : null

  if (!inst) {
    return (
      <div className="tv-chart-loading">
        {instrumentQuery.isLoading ? '加载股票信息中...' : '未找到股票'}
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
              onTimeframeChange={handleTimeframeChange}
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
