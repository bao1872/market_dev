// [MarketWorkspacePage] - 描述: 统一行情工作区第一版（三栏布局）
// 左栏：股票列表/搜索（MarketInstrumentPane）
// 中栏：唯一 K 线研究区（StockResearchWorkspace，复用 useStockResearchData）
// 右栏：StockStructuralStatePanel（可收起；收起时不挂载、不请求 structural/temporal 数据）
// URL 状态：scope/symbol/timeframe/source/strategy/event_id 进 URL；右栏折叠和 viewport 留本地。
// timeframe 为唯一真源：URL → useStockResearchData（bars/indicators）→ StockResearchWorkspace（图表）三者始终使用同一值。
// 工具栏切换写回 URL；选择新股票清除旧 event_id；切换股票不整页刷新（改 URL symbol 参数，React Query 缓存复用）。
// 只有当前选中股票请求 bars/indicators/quote/events；左栏不发 N+1 请求；scope 互斥请求门控。
import { useState, useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { MarketInstrumentPane } from './MarketInstrumentPane'
import { StockResearchWorkspace } from '@/features/stock-research/StockResearchWorkspace'
import { useStockResearchData } from '@/features/stock-research/useStockResearchData'
import { StockStructuralStatePanel } from '@/components/StockStructuralStatePanel'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  selectInstrumentFromMarketPane,
  changeMarketScope,
  type MarketScope,
  type DisplayTimeframe,
  type ResearchSource,
} from './marketWorkspaceUrlState'
import clsx from 'clsx'
import styles from './MarketWorkspace.module.scss'

export default function MarketWorkspacePage() {
  const [searchParams, setSearchParams] = useSearchParams()

  // 从 URL 解析状态（唯一真源）
  const urlState = useMemo(() => decodeMarketWorkspaceUrl(searchParams), [searchParams])
  const scope: MarketScope = urlState.scope
  const symbol = urlState.symbol
  const timeframe: DisplayTimeframe = urlState.timeframe
  const source: ResearchSource = urlState.source
  const strategy = urlState.strategy
  const eventId = urlState.eventId

  // 右栏折叠状态（本地，不进 URL）
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false)

  // 从左栏选择股票：重置 source=watchlist、strategy=watchlist_monitor、eventId=null（退出 selection 上下文）。
  // 保留 scope 和 timeframe。状态转换由纯函数 selectInstrumentFromMarketPane 处理，避免散落拼对象。
  const handleSelectSymbol = useCallback(
    (newSymbol: string, _instrumentId: string) => {
      const newState = selectInstrumentFromMarketPane(urlState, newSymbol)
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [urlState, setSearchParams],
  )

  // 切换 scope：退出 selection 上下文，重置 source=watchlist、strategy=watchlist_monitor、eventId=null。
  // 保留 symbol 和 timeframe。状态转换由纯函数 changeMarketScope 处理。
  const handleScopeChange = useCallback(
    (newScope: MarketScope) => {
      const newState = changeMarketScope(urlState, newScope)
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [urlState, setSearchParams],
  )

  // 工具栏切换周期：写回 URL（保留 scope/symbol/source/strategy/event_id）
  const handleTimeframeChange = useCallback(
    (newTimeframe: DisplayTimeframe) => {
      const newState = { scope, symbol, timeframe: newTimeframe, source, strategy, eventId }
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [scope, symbol, source, strategy, eventId, setSearchParams],
  )

  // 研究数据 hook（只有 symbol 非空时才发请求）
  const researchData = useStockResearchData({
    symbol,
    timeframe,
  })

  const instrumentId = researchData.instrumentId

  return (
    <div className={styles.workspace}>
      {/* 左栏：股票列表/搜索 */}
      <div className={styles.leftPane}>
        <div className={styles.scopeTabs}>
          <button
            className={clsx(styles.scopeTab, scope === 'watchlist' && styles.scopeTabActive)}
            onClick={() => handleScopeChange('watchlist')}
          >
            自选
          </button>
          <button
            className={clsx(styles.scopeTab, scope === 'market' && styles.scopeTabActive)}
            onClick={() => handleScopeChange('market')}
          >
            搜索
          </button>
        </div>
        <div className={styles.leftPaneContent}>
          <MarketInstrumentPane
            scope={scope}
            selectedSymbol={symbol}
            onSelectSymbol={handleSelectSymbol}
          />
        </div>
      </div>

      {/* 中栏 + 右栏 */}
      <div className={styles.centerRight}>
        {symbol ? (
          <>
            <div className={styles.centerPane}>
              <StockResearchWorkspace
                data={researchData}
                timeframe={timeframe}
                onTimeframeChange={handleTimeframeChange}
                source={source}
                strategyKey={strategy}
                rightPanelCollapsed={rightPanelCollapsed}
              />
            </div>
            {/* 右栏：结构状态因子面板（可收起；收起时不挂载、不请求 structural/temporal 数据） */}
            {!rightPanelCollapsed && instrumentId && (
              <aside className={styles.rightPane}>
                <div className={styles.rightPaneHeader}>
                  <span className={styles.rightPaneTitle}>结构状态</span>
                  <button
                    className={styles.collapseBtn}
                    onClick={() => setRightPanelCollapsed(true)}
                    aria-label="收起右栏"
                  >
                    ›
                  </button>
                </div>
                <StockStructuralStatePanel instrumentId={instrumentId} />
              </aside>
            )}
            {rightPanelCollapsed && (
              <button
                className={styles.expandBtn}
                onClick={() => setRightPanelCollapsed(false)}
                aria-label="展开右栏"
              >
                ‹
              </button>
            )}
          </>
        ) : (
          <div className={styles.emptyCenter}>
            <div className={styles.emptyIcon}>◎</div>
            <div className={styles.emptyText}>从左侧选择或搜索一只股票开始研究</div>
          </div>
        )}
      </div>
    </div>
  )
}
