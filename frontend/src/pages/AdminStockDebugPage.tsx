// [AdminStockDebugPage] - 描述: 管理员个股调试页面（独立 /admin/stock-debug 路由）
// 位于 AdminAppShell + AdminRoute 下，普通用户不可访问。
// 复用 MarketInstrumentPane（股票搜索）、useStockResearchData（bars/indicators/quote/events）、
// StockResearchWorkspace（K线研究区）、useResearchContext（event/structural/temporal）、
// AdminFactorDebugPanel（原始 factor/feature/JSON）、StockStructuralStatePanel（详细因子，debug=true）。
// 普通用户 /market 不展示任何原始因子或 JSON。
import { useState, useCallback, useMemo } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { MarketInstrumentPane } from '@/features/market-workspace/MarketInstrumentPane'
import { StockResearchWorkspace } from '@/features/stock-research/StockResearchWorkspace'
import { useStockResearchData } from '@/features/stock-research/useStockResearchData'
import { useResearchContext } from '@/features/research-context/useResearchContext'
import { AdminFactorDebugPanel } from '@/features/research-context/AdminFactorDebugPanel'
import { StockStructuralStatePanel } from '@/components/StockStructuralStatePanel'
import { type DisplayTimeframe, normalizeDisplayTimeframe } from '@/features/stock-research/stockResearchTypes'
import debugStyles from '@/features/research-context/ResearchContext.module.scss'
import workspaceStyles from '@/features/market-workspace/MarketWorkspace.module.scss'
import clsx from 'clsx'

export default function AdminStockDebugPage() {
  const { symbol: routeSymbol } = useParams<{ symbol?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false)

  const symbol = routeSymbol || searchParams.get('symbol') || null
  const timeframe = useMemo(() => normalizeDisplayTimeframe(searchParams.get('timeframe')), [searchParams])
  const eventId = searchParams.get('event_id') || null

  const handleSelectSymbol = useCallback(
    (newSymbol: string, _instrumentId: string) => {
      const params = new URLSearchParams(searchParams)
      params.set('symbol', newSymbol)
      setSearchParams(params, { replace: false })
    },
    [searchParams, setSearchParams],
  )

  const handleTimeframeChange = useCallback(
    (newTimeframe: DisplayTimeframe) => {
      const params = new URLSearchParams(searchParams)
      params.set('timeframe', newTimeframe)
      setSearchParams(params, { replace: false })
    },
    [searchParams, setSearchParams],
  )

  const researchData = useStockResearchData({ symbol, timeframe })
  const instrumentId = researchData.instrumentId

  const { eventDetail, structural, temporal } = useResearchContext({
    instrumentId,
    eventId,
    enabled: !!instrumentId,
  })

  return (
    <div className={workspaceStyles.workspace}>
      {/* 左栏：股票搜索 */}
      <div className={workspaceStyles.leftPane}>
        <div className={workspaceStyles.scopeTabs}>
          <span className={clsx(workspaceStyles.scopeTab, workspaceStyles.scopeTabActive)}>调试</span>
        </div>
        <div className={workspaceStyles.leftPaneContent}>
          <MarketInstrumentPane
            scope="market"
            selectedSymbol={symbol ?? null}
            onSelectSymbol={handleSelectSymbol}
          />
        </div>
      </div>

      {/* 中栏 + 右栏 */}
      <div className={workspaceStyles.centerRight}>
        {symbol ? (
          <>
            <div className={workspaceStyles.centerPane}>
              <StockResearchWorkspace
                data={researchData}
                timeframe={timeframe}
                onTimeframeChange={handleTimeframeChange}
                source="selection"
                strategyKey="dsa_selector"
                rightPanelCollapsed={rightPanelCollapsed}
              />
            </div>
            {/* 右栏：管理员调试面板 */}
            {!rightPanelCollapsed && instrumentId && (
              <aside className={clsx(workspaceStyles.rightPane, workspaceStyles.debugMode)}>
                <div className={workspaceStyles.rightPaneHeader}>
                  <span className={workspaceStyles.rightPaneTitle}>调试面板</span>
                  <span className={workspaceStyles.debugBadge}>调试模式</span>
                  <button
                    className={workspaceStyles.collapseBtn}
                    onClick={() => setRightPanelCollapsed(true)}
                    aria-label="收起右栏"
                  >
                    ›
                  </button>
                </div>
                <AdminFactorDebugPanel
                  eventDetailQuery={eventDetail}
                  structuralQuery={structural}
                  temporalQuery={temporal}
                  eventId={eventId}
                  styles={debugStyles}
                />
                <StockStructuralStatePanel instrumentId={instrumentId} debug={true} />
              </aside>
            )}
            {rightPanelCollapsed && (
              <button
                className={workspaceStyles.expandBtn}
                onClick={() => setRightPanelCollapsed(false)}
                aria-label="展开右栏"
              >
                ‹
              </button>
            )}
          </>
        ) : (
          <div className={workspaceStyles.emptyCenter}>
            <div className={workspaceStyles.emptyIcon}>◎</div>
            <div className={workspaceStyles.emptyText}>从左侧搜索一只股票开始调试</div>
          </div>
        )}
      </div>
    </div>
  )
}
