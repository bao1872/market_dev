// [MarketWorkspacePage] - 描述: 行情页（服务端分页股票表 + 可收起右栏）
// PRD §6.1：只由工具栏、服务端分页股票表和可收起 EventStatePanel 组成。
// 明确禁止：不得挂载 StockResearchWorkspace、StrategyChart 或任何K线。
// URL 状态：scope/query/page/page_size/sort/selected 进 URL（可分享、刷新恢复）；右栏折叠留本地。
// 单击非链接区域更新 selected 并刷新右栏；名称进入 /stock/:symbol?returnTo=...；星标保持筛选/分页。
import { useState, useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { MarketToolbar } from './MarketToolbar'
import { MarketStockTable } from './MarketStockTable'
import { EventStatePanel } from '@/features/research-context/EventStatePanel'
import { useMarketStocks } from '@/hooks/useApi'
import {
  decodeMarketWorkspaceUrl,
  encodeMarketWorkspaceUrl,
  selectInstrumentInTable,
  changeMarketScope,
  changeMarketFilter,
  type MarketScope,
  type MarketWorkspaceUrlState,
} from './marketWorkspaceUrlState'
import styles from './MarketWorkspace.module.scss'

export default function MarketWorkspacePage() {
  const [searchParams, setSearchParams] = useSearchParams()

  // 从 URL 解析状态（唯一真源）
  const urlState = useMemo(() => decodeMarketWorkspaceUrl(searchParams), [searchParams])
  const scope: MarketScope = urlState.scope
  const selected = urlState.selected

  // 右栏折叠状态（本地，不进 URL）
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false)

  // 行情列表查询（服务端分页 + 批量加载）
  const marketStocksQuery = useMarketStocks({
    scope,
    query: urlState.query || undefined,
    page: urlState.page,
    page_size: urlState.pageSize,
    sort: urlState.sort ?? undefined,
    industry: urlState.industry ?? undefined,
    concept: urlState.concept ?? undefined,
    state: urlState.state ?? undefined,
  })

  // selected symbol 直接用于右栏 StockContext API（PRD V1.1 §7.3）
  const selectedSymbol = selected || undefined

  // 通用 URL 更新函数
  const updateUrl = useCallback(
    (newState: MarketWorkspaceUrlState) => {
      setSearchParams(encodeMarketWorkspaceUrl(newState), { replace: false })
    },
    [setSearchParams],
  )

  // 切换 scope：重置 page=1、清除 selected
  const handleScopeChange = useCallback(
    (newScope: MarketScope) => {
      updateUrl(changeMarketScope(urlState, newScope))
    },
    [urlState, updateUrl],
  )

  // 搜索：重置 page=1
  const handleQueryChange = useCallback(
    (query: string) => {
      updateUrl({ ...urlState, query, page: 1, selected: null })
    },
    [urlState, updateUrl],
  )

  // 筛选变化：重置 page=1、清除 selected
  const handleFilterChange = useCallback(
    (patch: { industry?: string | null; concept?: string | null; state?: MarketWorkspaceUrlState['state'] }) => {
      updateUrl(changeMarketFilter(urlState, patch))
    },
    [urlState, updateUrl],
  )

  // 翻页
  const handlePageChange = useCallback(
    (page: number) => {
      updateUrl({ ...urlState, page })
    },
    [urlState, updateUrl],
  )

  // 单击行非链接区域：更新 selected
  const handleSelectRow = useCallback(
    (symbol: string) => {
      updateUrl(selectInstrumentInTable(urlState, symbol))
    },
    [urlState, updateUrl],
  )

  // 重试
  const handleRetry = useCallback(() => {
    marketStocksQuery.refetch()
  }, [marketStocksQuery])

  return (
    <div className={styles.marketPage}>
      <MarketToolbar
        scope={scope}
        query={urlState.query}
        industry={urlState.industry}
        concept={urlState.concept}
        state={urlState.state}
        onScopeChange={handleScopeChange}
        onQueryChange={handleQueryChange}
        onFilterChange={handleFilterChange}
      />
      <div className={styles.tableArea}>
        <div className={styles.tableWrapper}>
          <MarketStockTable
            data={marketStocksQuery.data}
            isLoading={marketStocksQuery.isLoading}
            isError={marketStocksQuery.isError}
            onRetry={handleRetry}
            selected={selected}
            onSelectRow={handleSelectRow}
            scope={scope}
            urlState={urlState}
            onPageChange={handlePageChange}
          />
        </div>
        {/* 右栏：研究上下文面板（可收起；收起时不挂载、不请求数据） */}
        {!rightPanelCollapsed && (
          <aside className={styles.rightPane}>
            <div className={styles.rightPaneHeader}>
              <span className={styles.rightPaneTitle}>事件与状态</span>
              <button
                className={styles.collapseBtn}
                onClick={() => setRightPanelCollapsed(true)}
                aria-label="收起右栏"
              >
                ›
              </button>
            </div>
            {selectedSymbol && (
              <EventStatePanel
                symbol={selectedSymbol}
                eventId={urlState.eventId}
              />
            )}
            {!selectedSymbol && (
              <div className={styles.rightPaneEmpty}>
                <div className={styles.emptyIcon}>◎</div>
                <div className={styles.emptyText}>单击表格中的股票查看事件与状态</div>
              </div>
            )}
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
      </div>
    </div>
  )
}
