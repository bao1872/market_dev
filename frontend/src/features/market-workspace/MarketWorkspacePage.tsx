// [MarketWorkspacePage] - 描述: 统一行情工作区（三栏布局）
// 左栏：股票列表/搜索（MarketInstrumentPane）
// 中栏：唯一 K 线研究区（StockResearchWorkspace，复用 useStockResearchData）
// 右栏：ResearchContextPanel（可收起；收起时不挂载、不请求 event/structural/temporal 数据）
// URL 状态：scope/symbol/timeframe/source/strategy/event_id/returnTo 进 URL；右栏折叠和 viewport 留本地。
// timeframe 为唯一真源：URL → useStockResearchData（bars/indicators）→ StockResearchWorkspace（图表）三者始终使用同一值。
// 工具栏切换写回 URL；选择新股票清除旧 event_id 和 returnTo；切换股票不整页刷新（改 URL symbol 参数，React Query 缓存复用）。
// 只有当前选中股票请求 bars/indicators/quote/events；左栏不发 N+1 请求；scope 互斥请求门控。
// 管理员调试使用独立 /admin/stock-debug/:symbol 路由；/market?debug=1 管理员重定向到调试页，普通用户忽略。
// returnTo 为来源页 URL（normalizeInternalReturnTo 校验），左栏选股或切 scope 时清除。
import { useState, useCallback, useMemo, useEffect } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { MarketInstrumentPane } from './MarketInstrumentPane'
import { StockResearchWorkspace } from '@/features/stock-research/StockResearchWorkspace'
import { useStockResearchData } from '@/features/stock-research/useStockResearchData'
import { ResearchContextPanel } from '@/features/research-context/ResearchContextPanel'
import { useAuthStore } from '@/store/auth'
import { resolveBackPath } from '@/pages/detailNavigation'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  selectInstrumentFromMarketPane,
  changeMarketScope,
  normalizeInternalReturnTo,
  type MarketScope,
  type DisplayTimeframe,
  type ResearchSource,
} from './marketWorkspaceUrlState'
import clsx from 'clsx'
import styles from './MarketWorkspace.module.scss'

export default function MarketWorkspacePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const isAdmin = useAuthStore((s) => s.user?.is_admin === true)

  // debug=1 兼容处理：管理员重定向到 /admin/stock-debug，普通用户清除 debug
  const rawDebug = searchParams.get('debug') === '1'
  useEffect(() => {
    if (!rawDebug) return
    if (isAdmin) {
      const sym = searchParams.get('symbol')
      navigate(sym ? `/admin/stock-debug/${sym}` : '/admin/stock-debug', { replace: true })
    } else {
      const params = new URLSearchParams(searchParams)
      params.delete('debug')
      setSearchParams(params, { replace: true })
    }
  }, [rawDebug, isAdmin, navigate, searchParams, setSearchParams])

  // 从 URL 解析状态（唯一真源）
  const urlState = useMemo(() => decodeMarketWorkspaceUrl(searchParams), [searchParams])
  const scope: MarketScope = urlState.scope
  const symbol = urlState.symbol
  const timeframe: DisplayTimeframe = urlState.timeframe
  const source: ResearchSource = urlState.source
  const strategy = urlState.strategy
  const eventId = urlState.eventId
  const returnTo = normalizeInternalReturnTo(urlState.returnTo)

  // 右栏折叠状态（本地，不进 URL）
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false)

  // 从左栏选择股票：重置 source=watchlist、strategy=watchlist_monitor、eventId=null、returnTo=null（退出 selection 上下文）。
  // 保留 scope 和 timeframe。状态转换由纯函数 selectInstrumentFromMarketPane 处理，避免散落拼对象。
  const handleSelectSymbol = useCallback(
    (newSymbol: string, _instrumentId: string) => {
      const newState = selectInstrumentFromMarketPane(urlState, newSymbol)
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [urlState, setSearchParams],
  )

  // 切换 scope：退出 selection 上下文，重置 source=watchlist、strategy=watchlist_monitor、eventId=null、returnTo=null。
  // 保留 symbol 和 timeframe。状态转换由纯函数 changeMarketScope 处理。
  const handleScopeChange = useCallback(
    (newScope: MarketScope) => {
      const newState = changeMarketScope(urlState, newScope)
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [urlState, setSearchParams],
  )

  // 工具栏切换周期：写回 URL（保留 scope/symbol/source/strategy/event_id/returnTo）
  const handleTimeframeChange = useCallback(
    (newTimeframe: DisplayTimeframe) => {
      const newState = { scope, symbol, timeframe: newTimeframe, source, strategy, eventId, returnTo }
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [scope, symbol, source, strategy, eventId, returnTo, setSearchParams],
  )

  // 返回按钮：优先 returnTo，其次按 source fallback
  const handleBack = useCallback(() => {
    navigate(resolveBackPath(returnTo ?? undefined, source))
  }, [navigate, returnTo, source])

  // 研究数据 hook（只有 symbol 非空时才发请求）
  const researchData = useStockResearchData({
    symbol,
    timeframe,
  })

  const instrumentId = researchData.instrumentId
  // 最新事件（来自 useStockResearchData 的 events 查询，无 event_id 时展示）
  const latestEvent = researchData.eventsQuery.data?.items?.[0] ?? null

  return (
    <div className={styles.workspace}>
      {/* 左栏：股票列表/搜索 */}
      <div className={styles.leftPane}>
        <div className={styles.scopeTabs}>
          <button
            className={clsx(styles.scopeTab, scope === 'watchlist' && styles.scopeTabActive)}
            onClick={() => handleScopeChange('watchlist')}
            aria-label="自选"
          >
            自选
          </button>
          <button
            className={clsx(styles.scopeTab, scope === 'market' && styles.scopeTabActive)}
            onClick={() => handleScopeChange('market')}
            aria-label="搜索"
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
              {returnTo && (
                <button className={styles.backBtn} onClick={handleBack} aria-label="返回">
                  ← 返回
                </button>
              )}
              <StockResearchWorkspace
                data={researchData}
                timeframe={timeframe}
                onTimeframeChange={handleTimeframeChange}
                source={source}
                strategyKey={strategy}
                rightPanelCollapsed={rightPanelCollapsed}
              />
            </div>
            {/* 右栏：研究上下文面板（可收起；收起时不挂载、不请求 event/structural/temporal 数据） */}
            {!rightPanelCollapsed && instrumentId && (
              <aside className={styles.rightPane}>
                <div className={styles.rightPaneHeader}>
                  <span className={styles.rightPaneTitle}>研究上下文</span>
                  <button
                    className={styles.collapseBtn}
                    onClick={() => setRightPanelCollapsed(true)}
                    aria-label="收起右栏"
                  >
                    ›
                  </button>
                </div>
                <ResearchContextPanel
                  instrumentId={instrumentId}
                  eventId={eventId}
                  latestEvent={latestEvent}
                />
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
