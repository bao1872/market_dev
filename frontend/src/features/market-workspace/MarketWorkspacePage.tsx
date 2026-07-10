// [MarketWorkspacePage] - 描述: 统一行情工作区第一版（三栏布局）
// 左栏：股票列表/搜索（MarketInstrumentPane）
// 中栏：唯一 K 线研究区（StockResearchWorkspace，复用 useStockResearchData）
// 右栏：StockStructuralStatePanel（可收起；收起时不挂载、不请求 structural/temporal 数据）
// URL 状态：scope/symbol/timeframe 进 URL；右栏折叠和 viewport 留本地。
// 切换股票不整页刷新（改 URL symbol 参数，React Query 缓存复用）。
// 只有当前选中股票请求 bars/indicators/quote/events；左栏不发 N+1 请求。
import { useState, useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { MarketInstrumentPane } from './MarketInstrumentPane'
import { StockResearchWorkspace } from '@/features/stock-research/StockResearchWorkspace'
import { useStockResearchData } from '@/features/stock-research/useStockResearchData'
import { StockStructuralStatePanel } from '@/components/StockStructuralStatePanel'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  type MarketScope,
} from './marketWorkspaceUrlState'
import clsx from 'clsx'
import styles from './MarketWorkspace.module.scss'

export default function MarketWorkspacePage() {
  const [searchParams, setSearchParams] = useSearchParams()

  // 从 URL 解析状态
  const urlState = useMemo(() => decodeMarketWorkspaceUrl(searchParams), [searchParams])
  const scope: MarketScope = urlState.scope
  const symbol = urlState.symbol
  const timeframe = urlState.timeframe

  // 右栏折叠状态（本地，不进 URL）
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false)

  // 选中股票改变时更新 URL（不整页刷新）
  const handleSelectSymbol = useCallback(
    (newSymbol: string, _instrumentId: string) => {
      const newState = { scope, symbol: newSymbol, timeframe }
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [scope, timeframe, setSearchParams],
  )

  // 切换 scope
  const handleScopeChange = useCallback(
    (newScope: MarketScope) => {
      const newState = { scope: newScope, symbol, timeframe }
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [symbol, timeframe, setSearchParams],
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
                source="watchlist"
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
