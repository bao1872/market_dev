// [MarketWorkspacePage] - 描述: 行情页（/market/stocks 统一数据源 + 可收起右栏）
// PRD §6.1 + AGENTS §12.2：/market 是 published DSA 结果的统一筛选入口。
// [CHANGE-20260729-009] 数据流：useMarketStocks(scope=all|watchlist) → adaptMarketStockToTrendRow → StrategyDataTable + getTrendSelectionColumns(inlineWatchlistToggle=true)
//   旧双分页架构（useStrategyRunResults + useMarketStocks 按 instrument_id 合并）已删除。
//   usePublishedRuns 仅用于导出 activeRunId（导出仍走 /strategy-runs/{run_id}/results/export）。
// 明确禁止：不得挂载 StockResearchWorkspace、StrategyChart 或任何K线。
// URL 状态：scope/selected 由本页管理；sort/dir/keyword/filters/page/page_size 由 StrategyDataTable 内置 screenerUrlState 管理。
// 顶部搜索框是 /market 唯一全文搜索入口（searchable=false 关闭表格内置搜索），keyword 通过 externalKeyword 受控注入。
// 右栏默认收起，收起时不挂载 EventStatePanel、不请求 context。
// 单击非链接区域更新 selected 并刷新右栏；股票名称链接进入 /stock/:symbol?returnTo=...。
// 自选操作：股票名称旁 22×22 +/- 内联按钮（inlineWatchlistToggle）；单次 useWatchlist 请求按 instrument_id 建 Set；
//   加入/移除复用 useAddToWatchlist/useRemoveFromWatchlist；按 instrument_id 维护 pending 防重复点击。
// 批次信息（数据日期/批次/状态）属调试信息：普通用户 DOM 中完全不渲染；仅 admin 可见，默认折叠为"批次信息"，展开后显示。
import { useState, useCallback, useMemo, useRef } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { MarketToolbar } from './MarketToolbar'
import { MarketRightPanel } from './MarketRightPanel'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn, DataTableQuery } from '@/components/StrategyDataTable'
import {
  usePublishedRuns,
  useWatchlist,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useMarketBoards,
  useMarketStocks,
  useMarketFilterSpecs,
} from '@/hooks/useApi'
import { useAuthStore } from '@/store/auth'
import { useToast } from '@/store/toast'
import { apiClient } from '@/api/client'
import type { MarketStocksQueryParams } from '@/api/endpoints'
import {
  adaptMarketStockToTrendRow,
  getTrendSelectionColumns,
  getStockDisplay,
  type TrendSelectionRow,
} from '@/features/trend-selection'
import type { ExportContext } from '@/components/StrategyDataTable'
import { buildStockDetailUrl } from '@/features/stock-research/stockDetailNavigation'
import {
  decodeMarketWorkspaceUrl,
  buildMarketReturnToUrl,
  convertFiltersToMetricFilters,
  extractStockNameFilter,
  type MarketScope,
  type MarketListContext,
} from './marketWorkspaceUrlState'
import {
  getFirstPyramidColumns,
  getDefaultHiddenFpKeys,
} from './firstPyramidColumns'
import { serializeFpFilters, serializeFpSort, isFpKey } from './firstPyramidQuerySerializer'
import styles from './MarketWorkspace.module.scss'

// DSA 生产策略 key（AGENTS §12.2：当前生产只保留 dsa_selector）
const DSA_STRATEGY_KEY = 'dsa_selector'
const PAGE_SIZE = 50

export default function MarketWorkspacePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const toast = useToast.getState()
  // 批次信息仅管理员可见（使用真实 is_admin，非 role store 视图切换）
  const isAdmin = useAuthStore((s) => s.user?.is_admin === true)
  // [Gate2 PRD60 PA-10/11/13] 三类独立 capability 决定 UI 可见性
  // Hooks 必须无条件调用（不能与 isAdmin 短路），再在普通 JS 层做 || 合并
  const hasMarketData = useAuthStore((s) => !!s.user?.capabilities?.market_data?.active)
  const hasSelfSelection = useAuthStore((s) => !!s.user?.capabilities?.self_selection?.active)
  // - canAccessStockDetail: market_data capability（详情按钮可点击；false 时股票名仅展示文本）
  // - canAccessWatchlist: self_selection capability（显示自选 scope + 自选操作列；false 时隐藏）
  const canAccessStockDetail = isAdmin || hasMarketData
  const canAccessWatchlist = isAdmin || hasSelfSelection

  // 从 URL 解析状态（仅 scope + selected；sort/filters/page 由 StrategyDataTable 管理）
  const urlState = useMemo(() => decodeMarketWorkspaceUrl(searchParams), [searchParams])
  // [Gate2 PRD60 PA-11] 无自选权限时强制 scope=market（禁止 watchlist scope）
  const scope: MarketScope = (!canAccessWatchlist && urlState.scope === 'watchlist') ? 'market' : urlState.scope
  const selected = urlState.selected

  // 顶部搜索框 keyword（单一真源，通过 externalKeyword 注入表格）
  // 初始值从 URL keyword 读取（表格 mount 时也会 hydration 并通过 onKeywordChange 回调同步）
  const [keyword, setKeyword] = useState<string>(() => searchParams.get('keyword') ?? '')

  // 行业/概念筛选（CHANGE-20260713-006：从 URL 读取，与 scope/selected 同级管理）
  const industry = urlState.industry ?? ''
  const concept = urlState.concept ?? ''

  // 板块目录（只请求一次，available=false 时禁用输入）
  const boardsQuery = useMarketBoards()
  const boards = useMemo(() => {
    if (!boardsQuery.data) return undefined
    return {
      items: boardsQuery.data.items,
      available: boardsQuery.data.available,
      stale: boardsQuery.data.stale,
    }
  }, [boardsQuery.data])

  // CHANGE-20260713-006: 板块校验集合（preset 应用时检测失效字段）
  const boardsValidation = useMemo(() => {
    if (!boards) return null
    const industryNames = new Set<string>()
    const conceptNames = new Set<string>()
    for (const b of boards.items) {
      if (b.type === 'industry') industryNames.add(b.name)
      else if (b.type === 'concept') conceptNames.add(b.name)
    }
    return { available: boards.available, industryNames, conceptNames }
  }, [boards])

  // CHANGE-20260713-006: preset 应用时失效字段 toast（每个字段 toast 一次，不重复）
  const staleFieldToastShownRef = useRef(false)
  const handlePresetStaleField = useCallback(
    (field: 'industry' | 'concept', value: string) => {
      // 避免同一轮 preset 应用重复 toast（applyPresetConfig 已对每个字段调用一次）
      if (staleFieldToastShownRef.current) return
      const label = field === 'industry' ? '行业' : '概念'
      toast.show(
        `${label}「${value}」已不在当前板块目录`,
        '已忽略该筛选条件，请重新选择',
      )
      staleFieldToastShownRef.current = true
      // 下一轮重置（允许后续 preset 再次 toast）
      setTimeout(() => {
        staleFieldToastShownRef.current = false
      }, 0)
    },
    [toast],
  )

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

  // CHANGE-20260713-010: 导出 Excel（POST /strategy-runs/{run_id}/results/export）
  // 必须导出当前完整筛选结果（filtered_total），不是当前页；通过 ExportContext 收集可见列与查询状态。
  // 复用 convertFiltersToMetricFilters 与 buildStrategyResultQueryParams 同口径转换，避免第二套筛选逻辑。
  const handleExport = useCallback(
    async (ctx: ExportContext) => {
      if (!activeRunId) {
        toast.show('无可导出的批次', '请先选择已发布的运行批次')
        return
      }
      try {
        const visibleColumns = ctx.visibleColumns.map((col) => ({
          key: col.key,
          title: col.title,
          data_type:
            col.dataType === 'number' ? 'number' : col.dataType === 'percent' ? 'percent' : 'text',
          payload_key: col.key === 'stock' ? null : col.key,
        }))
        const metricFilters = convertFiltersToMetricFilters(
          ctx.metricFilters.map((f) => ({
            key: f.key,
            operator: f.operator,
            value: f.value,
            value2: f.value2,
          })),
        )
        // CHANGE-20260713-011: 剥离 stock 列筛选，转为 stock_name + stock_name_op
        const stockNameFilter = extractStockNameFilter(
          ctx.metricFilters.map((f) => ({
            key: f.key,
            operator: f.operator,
            value: f.value,
            value2: f.value2,
          })),
        )
        const body = {
          universe: scope === 'watchlist' ? 'watchlist' : 'all',
          keyword: ctx.keyword || null,
          industry: ctx.industry || null,
          concept: ctx.concept || null,
          metric_filters: metricFilters.length > 0 ? metricFilters : null,
          stock_name: stockNameFilter?.stock_name ?? null,
          stock_name_op: stockNameFilter?.stock_name_op ?? null,
          sort_by: ctx.sortBy,
          sort_desc: ctx.sortDesc,
          visible_columns: visibleColumns,
        }
        const resp = await apiClient.post(
          `/v1/strategy-runs/${activeRunId}/results/export`,
          body,
          { responseType: 'blob' },
        )
        const contentDisp = resp.headers['content-disposition'] || ''
        let filename = '导出结果.xlsx'
        const match = contentDisp.match(/filename\*=UTF-8''([^;]+)/)
        if (match) {
          filename = decodeURIComponent(match[1])
        }
        const blob = new Blob([resp.data], {
          type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
      } catch (err: unknown) {
        const e = err as { response?: { status?: number; data?: Blob | unknown }; message?: string }
        if (e.response?.status === 422) {
          const detailText = e.response.data instanceof Blob
            ? await e.response.data.text()
            : JSON.stringify(e.response.data)
          try {
            const parsed = JSON.parse(detailText)
            toast.show('导出失败', parsed.detail || '筛选结果超过 10000 行上限')
          } catch {
            toast.show('导出失败', '筛选结果超过 10000 行上限，请缩小范围')
          }
        } else {
          toast.show('导出失败', e.message || '请稍后重试')
        }
      }
    },
    [activeRunId, scope, toast],
  )

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
        // watchlist / watchlist+monitor-status / strategy-runs 三个 queryKey
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

  // 运行结果查询参数
  // CHANGE-20260713-009: 使用共享 buildStrategyResultQueryParams 纯函数
  // MarketWorkspacePage 和 useStockDetailActions 共用同一转换逻辑，避免筛选口径漂移
  // scope=market → universe=all；scope=watchlist → universe=watchlist（在 buildStrategyResultQueryParams 内映射）
  //
  // [DetailSourceContextV2] marketListCtx 为入口时刻列表上下文快照，
  // handleNavigateToStock 用它构建 returnTo（buildMarketReturnToUrl）和 canonicalQuery。
  // 禁止从 searchParams 副本构建 returnTo（可能滞后于内存 query 状态）。
  const marketListCtx: MarketListContext = useMemo(() => {
    return {
      scope,
      keyword: query.keyword || null,
      industry: industry || null,
      concept: concept || null,
      sort: query.sort ? { key: query.sort.key, direction: query.sort.direction } : null,
      filters: query.filters.map((f) => ({
        key: f.key,
        operator: f.operator,
        value: f.value,
        value2: f.value2,
      })),
      page: query.page,
      page_size: query.pageSize,
      // CHANGE-20260713-011: preset=none 透传（不影响查询，仅用于默认 preset 自动应用门控）
      preset: urlState.preset,
    }
  }, [query, scope, industry, concept, urlState.preset])

  // [CHANGE-20260729-009] /market/stocks 作为列表唯一数据源（删除双分页合并架构）。
  // [CHANGE-20260730-012] 字段注册表：基础字段→sort，fp_*→fp_sort。禁止同时发送 sort 和 fp_sort。
  const marketStocksParams: MarketStocksQueryParams = useMemo(
    () => {
      const sortKey = query.sort?.key
      const isFpField = sortKey ? isFpKey(sortKey) : false
      return {
        scope,
        query: keyword || undefined,
        page: query.page,
        page_size: query.pageSize,
        // 基础字段→sort；fp_* 字段不发送 sort（只走 fp_sort）
        sort: query.sort && !isFpField ? `${query.sort.key}:${query.sort.direction}` : undefined,
        industry: industry || undefined,
        concept: concept || undefined,
        // fp_* 字段→fp_sort；基础字段不发送 fp_sort
        fp_filter: serializeFpFilters(query.filters),
        fp_sort: serializeFpSort(query.sort),
      }
    },
    [scope, keyword, query.page, query.pageSize, query.sort, query.filters, industry, concept],
  )
  const marketStocksQuery = useMarketStocks(marketStocksParams)

  // 行数据：MarketStockRow → TrendSelectionRow（单次转换，包含 first_pyramid/payload/chip_status）
  const rows: TrendSelectionRow[] = useMemo(
    () => (marketStocksQuery.data?.items ?? []).map((ms) => adaptMarketStockToTrendRow(ms)),
    [marketStocksQuery.data?.items],
  )
  const totalResults = marketStocksQuery.data?.total ?? 0

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

  // 股票名称链接：进入 /stock/:symbol?originScope=...&returnTo=...&mcq=...
  // CHANGE-20260716-006: 使用 buildStockDetailUrl 统一构建，originScope 为来源唯一真源
  // [DetailSourceContextV2] returnTo 从当前内存 marketListCtx 构建（buildMarketReturnToUrl），
  //   禁止从 searchParams 副本构建（可能滞后于内存 query 状态）。
  // [CHANGE-20260731-SAME-SOURCE] mcq（Market Canonical Query）固定入口时刻 /market/stocks 查询快照，
  //   详情左栏用此 mcq 原样查询 /market/stocks，同源同序，禁止重新推导。
  //   [CHANGE-20260801-URL-CLEAN] 停止写入旧 DSA sourceRunId/cq/strategy/source 到 URL：
  //   新合同仅 originScope + returnTo + mcq（可选 timeframe）。旧链接仍由 resolveDetailSourceContextV2 向后兼容。
  const handleNavigateToStock = useCallback(
    (row: TrendSelectionRow) => {
      const { symbol } = getStockDisplay(row)
      if (!symbol || symbol === '-') return
      // V2: returnTo 从当前内存 marketListCtx 构建（含完整筛选/排序/分页），selected 写入入口股票
      const returnTo = buildMarketReturnToUrl(marketListCtx, symbol)
      // [CHANGE-20260731-SAME-SOURCE] Market Canonical Query（/market/stocks 同源查询参数快照）
      // 直接序列化 marketStocksParams：scope/query/industry/concept/fp_filter/fp_sort/page/page_size
      const marketCanonicalQuery = JSON.stringify(marketStocksParams)
      navigate(
        buildStockDetailUrl(symbol, {
          originScope: scope,
          returnTo,
          marketCanonicalQuery,
        }),
      )
    },
    [navigate, marketListCtx, scope, marketStocksParams],
  )

  // 服务端查询变更
  const handleQueryChange = useCallback((newQuery: DataTableQuery) => {
    setQuery(newQuery)
  }, [])

  // keyword 变更（来自顶部搜索框 Enter/blur/clear 或表格 URL hydration/preset 应用）
  const handleKeywordChange = useCallback((newKeyword: string) => {
    setKeyword(newKeyword)
  }, [])

  // [CHANGE-20260730-013] 拉取第一金字塔 99 字段筛选元数据（staleTime 24h，随部署更新）
  // 用于 FilterPopover 按 data_type/input_control/enum_values 动态生成类型化控件：
  // enum → 下拉单选/多选；boolean → true/false 下拉；datetime → 日期选择器；number → 数字输入
  const filterSpecsQuery = useMarketFilterSpecs()
  const filterSpecs = filterSpecsQuery.data ?? null

  // 列定义：DSA 列（复用 features/trend-selection 共享模块） + 99 个第一金字塔列
  // [Gate2 PRD60 PA-10/11/13] capability 决定回调传递：
  // - canAccessStockDetail=false（仅 self_selection）：不传 onNavigateToStock，股票名渲染为纯文本
  // - canAccessWatchlist=false（仅 market_data）：不传 onToggleWatchlist，操作列返回 null
  //
  // [PRD §三 列表视图第一金字塔全量字段] 在基础列后追加 99 个 fp_ 列，操作列固定末尾。
  // 99 列默认通过 defaultHiddenColumns 隐藏非核心键；列设置面板可显隐/拖拽排序。
  // [CHANGE-20260730-013] 将 filterSpecs 注入 fp_ 列，启用类型化筛选器（enum 下拉、日期选择器等）
  const columns: DataTableColumn<TrendSelectionRow>[] = useMemo(
    () => {
      // [CHANGE-20260729-009] inlineWatchlistToggle=true：
      // 股票名称旁渲染 +/- 按钮，getTrendSelectionColumns 已自动移除 action 列。
      const baseColumns = getTrendSelectionColumns({
        onNavigateToStock: canAccessStockDetail ? handleNavigateToStock : undefined,
        onToggleWatchlist: canAccessWatchlist ? handleToggleWatchlist : undefined,
        watchlistInstrumentIds,
        watchlistPendingIds,
        inlineWatchlistToggle: canAccessWatchlist,
      })
      // inlineWatchlistToggle=true 时无 action 列；false 时拆分后追加 fp_ 列 + action 末尾
      const actionCol = baseColumns.find((c) => c.isAction)
      const nonActionBaseCols = baseColumns.filter((c) => !c.isAction)
      // [CHANGE-20260730-013] 传入 filterSpecs，每个 fp_ 列携带 filterSpec 元数据
      const fpCols = getFirstPyramidColumns(filterSpecs)
      const merged = actionCol
        ? [...nonActionBaseCols, ...fpCols, actionCol]
        : [...nonActionBaseCols, ...fpCols]
      // [Gate2 PRD60 PA-11] 仅 market_data 用户：无自选权限时移除操作列（避免空列占用宽度）
      if (!canAccessWatchlist) {
        return merged.filter((col) => col.key !== 'action')
      }
      return merged
    },
    [handleNavigateToStock, handleToggleWatchlist, watchlistInstrumentIds, watchlistPendingIds, canAccessStockDetail, canAccessWatchlist, filterSpecs],
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

  // selected symbol 用于右栏 AtomicFactsPanel
  const selectedSymbol = selected || undefined

  return (
    <div className={styles.marketPage}>
      <MarketToolbar
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
            // [PRD §三] 默认隐藏 79 个非核心 fp_ 列；preset 应用后由 preset.hiddenColumns 覆盖
            defaultHiddenColumns={getDefaultHiddenFpKeys()}
            rows={rows}
            rowKey={(row) => row.symbol === '-' ? row.instrumentId : row.symbol}
            total={totalResults}
            serverSide
            onQueryChange={handleQueryChange}
            loading={marketStocksQuery.isLoading || runsQuery.isLoading}
            // [CHANGE-20260730-012] 显示后端 422 detail 和 500 request_id，不再统一"行情列表加载失败"
            error={marketStocksQuery.isError
              ? (() => {
                  const err = marketStocksQuery.error as { response?: { status?: number; data?: { detail?: string }; headers?: { get: (k: string) => string | null } } }
                  const status = err?.response?.status
                  if (status === 422) {
                    return `筛选/排序参数无效：${err?.response?.data?.detail ?? '未知错误'}`
                  }
                  if (status === 500) {
                    const reqId = err?.response?.headers?.get('x-request-id')
                    return `服务器错误${reqId ? `（request_id=${reqId}）` : ''}`
                  }
                  return `行情列表加载失败：${err?.response?.status ?? '网络错误'}`
                })()
              : runsQuery.isError ? '运行批次加载失败' : null}
            emptyText={marketStocksQuery.isError ? '行情列表加载失败，请检查筛选/排序参数' : '本页无数据'}
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
            // CHANGE-20260713-006: preset 应用时校验失效板块字段并 toast
            boardsValidation={boardsValidation}
            onPresetStaleField={handlePresetStaleField}
            // CHANGE-20260713-010: 导出 Excel
            onExport={handleExport}
          />
        </div>
        {/* 右栏：小 K 线 + 研究上下文面板（可收起；收起时不挂载、不请求数据） */}
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
            <MarketRightPanel symbol={selectedSymbol ?? null} />
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
