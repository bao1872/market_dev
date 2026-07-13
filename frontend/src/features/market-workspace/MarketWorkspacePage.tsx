// [MarketWorkspacePage] - 描述: 行情页（DSA 已发布结果列表 + 可收起右栏）
// PRD §6.1 + AGENTS §12.2：/market 是 published DSA 结果的统一筛选入口。
// 数据流：usePublishedRuns(dsa_selector) → useStrategyRunResults(universe=all|watchlist) → adaptStrategyResultToTrendRow → StrategyDataTable + getTrendSelectionColumns
// 明确禁止：不得挂载 StockResearchWorkspace、StrategyChart 或任何K线。
// URL 状态：scope/selected 由本页管理；sort/dir/keyword/filters/page/page_size 由 StrategyDataTable 内置 screenerUrlState 管理。
// 顶部搜索框是 /market 唯一全文搜索入口（searchable=false 关闭表格内置搜索），keyword 通过 externalKeyword 受控注入。
// 右栏默认收起，收起时不挂载 EventStatePanel、不请求 context。
// 单击非链接区域更新 selected 并刷新右栏；股票名称链接进入 /stock/:symbol?returnTo=...。
// 自选操作列：单次 useWatchlist 请求按 instrument_id 建 Set；加入/移除复用 useAddToWatchlist/useRemoveFromWatchlist；按 instrument_id 维护 pending 防重复点击。
// 批次信息（数据日期/批次/状态）属调试信息：普通用户 DOM 中完全不渲染；仅 admin 可见，默认折叠为"批次信息"，展开后显示。
import { useState, useCallback, useMemo } from 'react'
import { useSearchParams, useNavigate, useLocation } from 'react-router-dom'
import { MarketToolbar } from './MarketToolbar'
import { EventStatePanel } from '@/features/research-context/EventStatePanel'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn, DataTableQuery } from '@/components/StrategyDataTable'
import {
  usePublishedRuns,
  useStrategyRunResults,
  useWatchlist,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useMarketBoards,
} from '@/hooks/useApi'
import { useAuthStore } from '@/store/auth'
import { useToast } from '@/store/toast'
import type { StrategyResultQueryParams } from '@/api/endpoints'
import {
  adaptStrategyResultToTrendRow,
  getTrendSelectionColumns,
  getStockDisplay,
  type TrendSelectionRow,
} from '@/features/trend-selection'
import {
  decodeMarketWorkspaceUrl,
  changeMarketScope,
  type MarketScope,
} from './marketWorkspaceUrlState'
import styles from './MarketWorkspace.module.scss'

// DSA 生产策略 key（AGENTS §12.2：当前生产只保留 dsa_selector）
const DSA_STRATEGY_KEY = 'dsa_selector'
const PAGE_SIZE = 50

// [MarketWorkspacePage] - 描述: 后端存储为小数的收益率/offset 类指标
const RATIO_METRICS = new Set([
  'vwap_ret_avg',
  'vwap_ret_total',
  'offset_mean',
  'offset_std',
  'offset_variance_rate',
])

// [MarketWorkspacePage] - 描述: 后端存储为 0~1 的百分位类指标
const PERCENTILE_METRICS = new Set([
  'offset_percentile',
  'short_position',
  'position_short',
  'short_pos',
])

/** 将用户输入的筛选值归一化为后端口径（与 ScreenerPage 一致） */
function normalizeMetricValue(
  key: string,
  raw: string | number | undefined,
): number | undefined {
  if (raw === undefined || raw === null || raw === '') return undefined
  const s = String(raw).replace(/,/g, '').trim()
  const hasPercent = s.includes('%')
  const n = parseFloat(s.replace(/%/g, ''))
  if (Number.isNaN(n)) return undefined
  if (RATIO_METRICS.has(key) || PERCENTILE_METRICS.has(key)) {
    return hasPercent ? n / 100 : n
  }
  return n
}

export default function MarketWorkspacePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const location = useLocation()
  const toast = useToast.getState()
  // 批次信息仅管理员可见（使用真实 is_admin，非 role store 视图切换）
  const isAdmin = useAuthStore((s) => s.user?.is_admin === true)

  // 从 URL 解析状态（仅 scope + selected；sort/filters/page 由 StrategyDataTable 管理）
  const urlState = useMemo(() => decodeMarketWorkspaceUrl(searchParams), [searchParams])
  const scope: MarketScope = urlState.scope
  const selected = urlState.selected

  // 顶部搜索框 keyword（单一真源，通过 externalKeyword 注入表格）
  // 初始值从 URL keyword 读取（表格 mount 时也会 hydration 并通过 onKeywordChange 回调同步）
  const [keyword, setKeyword] = useState<string>(() => searchParams.get('keyword') ?? '')

  // 行业/概念筛选（CHANGE-20260713-006：从 URL 读取，与 scope/selected 同级管理）
  const industry = urlState.industry ?? ''
  const concept = urlState.concept ?? ''

  // 板块目录（只请求一次，available=false 时禁用输入）
  const boardsQuery = useMarketBoards()
  const boards = boardsQuery.data
    ? { items: boardsQuery.data.items, available: boardsQuery.data.available }
    : undefined

  // 右栏折叠状态（本地，不进 URL）
  // 首次访问默认收起，保留用户 localStorage 选择
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState<boolean>(() => {
    if (typeof window === 'undefined') return true
    const saved = window.localStorage.getItem('panji:market-right-panel-collapsed:v1')
    return saved === null ? true : saved === '1'
  })
  const handleToggleRightPanel = useCallback((collapsed: boolean) => {
    setRightPanelCollapsed(collapsed)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('panji:market-right-panel-collapsed:v1', collapsed ? '1' : '0')
    }
  }, [])

  // 批次信息折叠状态（仅 admin 可见，默认折叠）
  const [batchMetaExpanded, setBatchMetaExpanded] = useState(false)

  // DSA 已发布运行批次（仅最新一个快照）
  const runsQuery = usePublishedRuns(DSA_STRATEGY_KEY, { limit: 1 })
  const runs = runsQuery.data?.items ?? []
  const activeRunId = runs[0]?.id || ''
  const activeRun = runs[0]

  // 自选列表（页面只请求一次，按 instrument_id 建 Set）
  // watchlist scope 依赖此数据；market scope 也需要判断行是否已自选
  const watchlistQuery = useWatchlist()
  const watchlistInstrumentIds = useMemo(() => {
    const set = new Set<string>()
    for (const item of watchlistQuery.data?.items ?? []) {
      if (item.instrument_id) set.add(item.instrument_id)
    }
    return set
  }, [watchlistQuery.data?.items])

  // 自选操作 pending 状态（按 instrument_id 维护，防重复点击）
  const [watchlistPendingIds, setWatchlistPendingIds] = useState<Set<string>>(() => new Set())
  const addMutation = useAddToWatchlist()
  const removeMutation = useRemoveFromWatchlist()

  // 加入/移除自选
  const handleToggleWatchlist = useCallback(
    (row: TrendSelectionRow, add: boolean) => {
      const instrumentId = row.instrumentId
      if (!instrumentId) return
      // 已在 pending 中，忽略重复点击
      if (watchlistPendingIds.has(instrumentId)) return

      setWatchlistPendingIds((prev) => {
        const next = new Set(prev)
        next.add(instrumentId)
        return next
      })

      const onSettled = () => {
        setWatchlistPendingIds((prev) => {
          const next = new Set(prev)
          next.delete(instrumentId)
          return next
        })
      }
      const onSuccess = () => {
        // useAddToWatchlist/useRemoveFromWatchlist 的 onSuccess 已 invalidate:
        // ['watchlist'] + ['watchlist','monitor-status'] + ['strategy-runs']
        // watchlist scope 下移除自选后该行立即消失（strategy-runs 失效后重新请求 universe=watchlist）
        toast.show(add ? '已加入自选' : '已移除自选', '')
      }
      const onError = () => {
        toast.show(add ? '加入自选失败' : '移除自选失败', '请稍后重试')
      }

      if (add) {
        addMutation.mutate(
          { instrument_id: instrumentId },
          { onSettled, onSuccess, onError },
        )
      } else {
        removeMutation.mutate(instrumentId, { onSettled, onSuccess, onError })
      }
    },
    [watchlistPendingIds, addMutation, removeMutation, toast],
  )

  // 服务端分页/筛选/排序状态（由 StrategyDataTable 通过 onQueryChange 回调驱动）
  const [query, setQuery] = useState<DataTableQuery>({
    page: 1,
    pageSize: PAGE_SIZE,
    filters: [],
  })

  // scope → universe 映射：scope=market → universe=all, scope=watchlist → universe=watchlist
  const universe: 'all' | 'watchlist' = scope === 'market' ? 'all' : 'watchlist'

  // 运行结果查询参数
  const resultParams: StrategyResultQueryParams = useMemo(() => {
    const params: StrategyResultQueryParams = {
      page: query.page,
      page_size: query.pageSize,
      universe,
    }
    if (query.sort) {
      params.sort_by = query.sort.key
      params.sort_desc = query.sort.direction === 'desc'
    }
    if (query.keyword) {
      params.keyword = query.keyword
    }
    // 行业/概念筛选（CHANGE-20260713-006）
    if (industry) {
      params.industry = industry
    }
    if (concept) {
      params.concept = concept
    }
    // 列筛选转 metric_filters（与 ScreenerPage 一致）
    const supportedOps = new Set(['gt', 'gte', 'lt', 'lte', 'eq', 'between'])
    const metricFilters = query.filters
      .filter((f) => supportedOps.has(f.operator) && f.key !== 'stock' && f.key !== 'action')
      .map((f) => {
        const value = normalizeMetricValue(f.key, f.value)
        if (value === undefined) return null
        if (f.operator === 'between') {
          const value2 = normalizeMetricValue(f.key, f.value2)
          if (value2 === undefined) return null
          return {
            metric_key: f.key,
            operator: f.operator,
            value1: value,
            value2,
          }
        }
        return {
          metric_key: f.key,
          operator: f.operator,
          value,
        }
      })
      .filter((f): f is NonNullable<typeof f> => f !== null)
    if (metricFilters.length > 0) {
      params.metric_filters = JSON.stringify(metricFilters)
    }
    return params
  }, [query, universe, industry, concept])

  const resultsQuery = useStrategyRunResults(activeRunId || undefined, resultParams)
  const totalResults = resultsQuery.data?.total ?? 0

  // 行数据：StrategyResult → TrendSelectionRow
  const rows: TrendSelectionRow[] = useMemo(
    () => (resultsQuery.data?.items ?? []).map((r) => adaptStrategyResultToTrendRow(r)),
    [resultsQuery.data?.items],
  )

  // 通用 URL 更新函数（仅更新 scope + selected，保留 StrategyDataTable 管理的 sort/filters/page 等）
  const updateUrl = useCallback(
    (newState: { scope: MarketScope; selected: string | null; industry?: string | null; concept?: string | null }) => {
      const params = new URLSearchParams(searchParams)
      // 更新 scope + selected
      params.set('scope', newState.scope)
      if (newState.selected) {
        params.set('selected', newState.selected)
      } else {
        params.delete('selected')
      }
      // 行业/概念（可选，不传时保留原值）
      if (newState.industry !== undefined) {
        if (newState.industry) {
          params.set('industry', newState.industry)
        } else {
          params.delete('industry')
        }
      }
      if (newState.concept !== undefined) {
        if (newState.concept) {
          params.set('concept', newState.concept)
        } else {
          params.delete('concept')
        }
      }
      setSearchParams(params, { replace: false })
    },
    [searchParams, setSearchParams],
  )

  // 行业/概念变更：更新 URL + 重置 page=1（CHANGE-20260713-006）
  const handleIndustryChange = useCallback(
    (newIndustry: string) => {
      setQuery((prev) => ({ ...prev, page: 1 }))
      const params = new URLSearchParams(searchParams)
      if (newIndustry) {
        params.set('industry', newIndustry)
      } else {
        params.delete('industry')
      }
      setSearchParams(params, { replace: false })
    },
    [searchParams, setSearchParams],
  )
  const handleConceptChange = useCallback(
    (newConcept: string) => {
      setQuery((prev) => ({ ...prev, page: 1 }))
      const params = new URLSearchParams(searchParams)
      if (newConcept) {
        params.set('concept', newConcept)
      } else {
        params.delete('concept')
      }
      setSearchParams(params, { replace: false })
    },
    [searchParams, setSearchParams],
  )

  // 切换 scope：清除 selected，保留 sort/filters/page（由 StrategyDataTable 管理）
  const handleScopeChange = useCallback(
    (newScope: MarketScope) => {
      const next = changeMarketScope(urlState, newScope)
      updateUrl(next)
    },
    [urlState, updateUrl],
  )

  // 单击行非链接区域：更新 selected（保留 scope + StrategyDataTable 的 URL 状态）
  const handleRowClick = useCallback(
    (row: TrendSelectionRow) => {
      const { symbol } = getStockDisplay(row)
      if (!symbol || symbol === '-') return
      const params = new URLSearchParams(searchParams)
      params.set('selected', symbol)
      setSearchParams(params, { replace: true })
    },
    [searchParams, setSearchParams],
  )

  // 股票名称链接：进入 /stock/:symbol?returnTo=<当前 /market URL>
  const handleNavigateToStock = useCallback(
    (row: TrendSelectionRow) => {
      const { symbol } = getStockDisplay(row)
      if (!symbol || symbol === '-') return
      const returnTo = `${location.pathname}${location.search}`
      navigate(`/stock/${symbol}?returnTo=${encodeURIComponent(returnTo)}`)
    },
    [navigate, location.pathname, location.search],
  )

  // 服务端查询变更
  const handleQueryChange = useCallback((newQuery: DataTableQuery) => {
    setQuery(newQuery)
  }, [])

  // keyword 变更（来自顶部搜索框 Enter/blur/clear 或表格 URL hydration/preset 应用）
  const handleKeywordChange = useCallback((newKeyword: string) => {
    setKeyword(newKeyword)
  }, [])

  // 列定义：DSA 列（复用 features/trend-selection 共享模块）
  // onNavigateToStock: 股票名称链接进入详情
  // onToggleWatchlist + watchlistInstrumentIds + watchlistPendingIds: 自选操作列
  const columns: DataTableColumn<TrendSelectionRow>[] = useMemo(
    () =>
      getTrendSelectionColumns({
        onNavigateToStock: handleNavigateToStock,
        onToggleWatchlist: handleToggleWatchlist,
        watchlistInstrumentIds,
        watchlistPendingIds,
      }),
    [handleNavigateToStock, handleToggleWatchlist, watchlistInstrumentIds, watchlistPendingIds],
  )

  // 批次元数据（调试信息：仅 admin 可见）
  const batchMeta = useMemo(() => {
    if (!activeRun) return null
    const statusLabel = activeRun.status === 'published' ? '已发布' : activeRun.status
    return {
      runId: activeRun.id.slice(0, 8),
      status: statusLabel,
      tradeDate: activeRun.trade_date ?? '-',
    }
  }, [activeRun])

  // selected symbol 用于右栏 EventStatePanel
  const selectedSymbol = selected || undefined

  return (
    <div className={styles.marketPage}>
      <MarketToolbar
        scope={scope}
        onScopeChange={handleScopeChange}
        keyword={keyword}
        onKeywordChange={handleKeywordChange}
        industry={industry}
        onIndustryChange={handleIndustryChange}
        concept={concept}
        onConceptChange={handleConceptChange}
        boards={boards}
      />
      <div className={styles.tableArea}>
        <div className={styles.tableWrapper}>
          {/* 批次信息：调试信息，仅 admin 可见，默认折叠为"批次信息"标题，展开后显示 */}
          {isAdmin && batchMeta && (
            <div
              className="batch-meta-bar"
              style={{
                padding: '6px 16px',
                borderBottom: '1px solid #232838',
                fontSize: 12,
                color: '#8ea0b7',
                display: 'flex',
                alignItems: 'center',
                gap: 12,
              }}
            >
              <button
                type="button"
                onClick={() => setBatchMetaExpanded((v) => !v)}
                style={{
                  border: 0,
                  background: 'transparent',
                  color: '#8ea0b7',
                  cursor: 'pointer',
                  fontSize: 12,
                  padding: 0,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                }}
                aria-expanded={batchMetaExpanded}
                aria-label="切换批次信息"
              >
                <span>{batchMetaExpanded ? '▾' : '▸'}</span>
                <span>批次信息</span>
              </button>
              {batchMetaExpanded && (
                <>
                  <span>数据日期: <b style={{ color: '#d7e3f2' }}>{batchMeta.tradeDate}</b></span>
                  <span>批次: <b style={{ color: '#d7e3f2' }}>{batchMeta.runId}</b></span>
                  <span>状态: <b style={{ color: batchMeta.status === '已发布' ? '#22c55e' : '#f5a623' }}>{batchMeta.status}</b></span>
                </>
              )}
            </div>
          )}
          <StrategyDataTable
            key={activeRunId ? `run-${activeRunId}` : 'run-empty'}
            tableId="market"
            strategyKey={DSA_STRATEGY_KEY}
            activeRunId={activeRunId}
            columns={columns}
            rows={rows}
            rowKey={(row) => row.symbol === '-' ? row.instrumentId : row.symbol}
            total={totalResults}
            serverSide
            onQueryChange={handleQueryChange}
            loading={resultsQuery.isLoading || runsQuery.isLoading}
            error={resultsQuery.isError ? '运行结果加载失败' : runsQuery.isError ? '运行批次加载失败' : null}
            emptyText={resultsQuery.isError ? '运行结果加载失败' : '本批次无选股结果'}
            initialPageSize={PAGE_SIZE}
            tableClassName="compact-table"
            stickyHeaderMode="container"
            onRowClick={handleRowClick}
            activeRowKey={selected}
            // /market 顶部搜索框是唯一全文搜索入口，关闭表格内置搜索
            searchable={false}
            // 受控 keyword：顶部搜索框 → externalKeyword → 表格 URL sync + API query
            externalKeyword={keyword}
            onKeywordChange={handleKeywordChange}
            // CHANGE-20260713-006: 受控 industry/concept（顶部板块筛选 → URL + preset 持久化）
            externalIndustry={industry ?? ''}
            onIndustryChange={handleIndustryChange}
            externalConcept={concept ?? ''}
            onConceptChange={handleConceptChange}
          />
        </div>
        {/* 右栏：研究上下文面板（可收起；收起时不挂载、不请求数据） */}
        {!rightPanelCollapsed && (
          <aside className={styles.rightPane}>
            <div className={styles.rightPaneHeader}>
              <span className={styles.rightPaneTitle}>事件与状态</span>
              <button
                className={styles.collapseBtn}
                onClick={() => handleToggleRightPanel(true)}
                aria-label="收起右栏"
              >
                ›
              </button>
            </div>
            {selectedSymbol && (
              <EventStatePanel symbol={selectedSymbol} />
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
            onClick={() => handleToggleRightPanel(false)}
            aria-label="展开右栏"
          >
            ‹
          </button>
        )}
      </div>
    </div>
  )
}
