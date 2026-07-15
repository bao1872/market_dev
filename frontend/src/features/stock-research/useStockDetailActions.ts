// [useStockDetailActions] - 描述: StockDetailPage 专属 actions hook
// 负责自选列表查询、加入/移出自选、上下切换、memo 读取/保存/删除。
// 这些操作是详情页专属，不得进入 /market 的 useStockResearchData 核心 hook。
//
// CHANGE-20260713-009: 来源列表复用 published DSA results 链（usePublishedRuns + useStrategyRunResults），
// 禁止继续使用 useMarketStocks。MarketWorkspacePage 和本 hook 共用 decodeMarketListContext + buildStrategyResultQueryParams。
//
// CHANGE-20260714-001: 无 returnTo 的自选 fallback 改用 useWatchlistMonitorStatus 单次聚合请求
// （替代旧 useWatchlist + useBatchInstruments 两段查询，避免逐行 quote 和 N+1）。
// 每行附带 changePct：DSA 来源读 payload.change_pct，自选来源读 metrics.change_pct。
import { useState, useMemo, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  useWatchlistMonitorStatus,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useStockMemo,
  useUpsertStockMemo,
  useDeleteStockMemo,
  usePublishedRuns,
  useStrategyRunResults,
} from '@/hooks/useApi'
import {
  decodeMarketListContext,
  buildStrategyResultQueryParams,
  type MarketListContext,
} from '@/features/market-workspace/marketWorkspaceUrlState'
import {
  adaptStrategyResultToTrendRow,
  getStockDisplay,
  pickPayload,
  toNum,
  CHANGE_PCT_KEYS,
} from '@/features/trend-selection'
import { useToast } from '@/store/toast'
import type { ResearchSource } from './stockResearchTypes'
import type { StrategyResultQueryParams } from '@/api/endpoints'

export interface StockDetailActionsParams {
  instrumentId: string | undefined
  symbol: string | undefined
  source: ResearchSource
  strategy: string
  // returnTo URL（来自详情页 URL 参数），用于恢复来源列表的 scope/query/page/sort 上下文
  // 当 returnTo 指向 /market?scope=market&keyword=xxx&page=2&sort=xxx 时，左栏优先展示该市场搜索结果
  // returnTo 缺失或非 /market 前缀时回退到自选列表
  returnTo?: string | null
}

// 来源股票列表项（左栏统一渲染结构）
// CHANGE-20260714-001: 新增 changePct 字段（最近交易日涨跌幅，百分比数值，可空）
export interface SourceStockItem {
  symbol: string
  name: string
  changePct: number | null
}

// 来源列表类型（决定左栏标题与点击导航行为）
export type SourceListKind = 'market' | 'watchlist'

export interface StockDetailActions {
  // 自选状态
  inWatchlist: boolean
  handleToggleWatchlist: () => void
  addWatchlistPending: boolean
  removeWatchlistPending: boolean
  // 上下切换
  canNavigate: boolean
  navigateToStock: (direction: number) => void
  // 备忘录
  memoOpen: boolean
  setMemoOpen: (open: boolean) => void
  memoContent: string
  setMemoContent: (content: string) => void
  memoNotify: boolean
  setMemoNotify: (notify: boolean) => void
  stockMemoQuery: ReturnType<typeof useStockMemo>
  upsertMemo: ReturnType<typeof useUpsertStockMemo>
  deleteMemo: ReturnType<typeof useDeleteStockMemo>
  hasMemo: boolean
  // 来源股票列表（用于详情页左栏显示）
  // 优先 returnTo 上下文恢复的市场搜索结果，回退到自选列表
  sourceStocks: SourceStockItem[]
  sourceListKind: SourceListKind
  // CHANGE-20260715-004: 来源列表加载状态（DSA results 加载中或 published runs 加载中）
  // 用于 StockDetailPage 渲染 loading 占位，避免空白后突然出现列表
  sourceListLoading: boolean
  // 兼容旧接口（= sourceStocks，仅当 sourceListKind==='watchlist' 时有值）
  watchlistStocks: SourceStockItem[]
}

export function useStockDetailActions({
  instrumentId,
  symbol,
  source,
  strategy,
  returnTo,
}: StockDetailActionsParams): StockDetailActions {
  const navigate = useNavigate()
  const showToast = useToast((s) => s.show)

  // CHANGE-20260713-009: 使用共享 decodeMarketListContext 解析 returnTo
  // 任意合法 /market URL 都识别为市场工作区上下文（不要求 keyword/page/sort 存在）
  // scope=market → sourceListKind=market；scope=watchlist → sourceListKind=watchlist
  const marketContext: MarketListContext | null = useMemo(
    () => decodeMarketListContext(returnTo),
    [returnTo],
  )
  const hasMarketContext = marketContext !== null

  // DSA published run（与 MarketWorkspacePage 同一数据链）
  // scope=market 和 scope=watchlist 都复用 dsa_selector published run
  const publishedRunsQuery = usePublishedRuns('dsa_selector', { limit: 1 })
  const activeRunId = publishedRunsQuery.data?.items?.[0]?.id

  // 来源列表查询参数（与 MarketWorkspacePage 共用 buildStrategyResultQueryParams）
  const sourceListParams: StrategyResultQueryParams | undefined = useMemo(() => {
    if (!marketContext) return undefined
    return buildStrategyResultQueryParams(marketContext) as StrategyResultQueryParams
  }, [marketContext])

  // 来源列表 DSA results 查询（仅当有 marketContext 时启用）
  const sourceResultsQuery = useStrategyRunResults(activeRunId, sourceListParams)

  // 来源列表行数据（StrategyResult → TrendSelectionRow → SourceStockItem）
  // CHANGE-20260714-001: DSA 来源使用 latestChangePct（bars_daily 最新两根日线，与 payload 分离）
  const marketStocks = useMemo(() => {
    if (!hasMarketContext || !sourceResultsQuery.data?.items) return []
    return sourceResultsQuery.data.items.map((r) => {
      const row = adaptStrategyResultToTrendRow(r)
      const display = getStockDisplay(row)
      return { symbol: display.symbol, name: display.name, changePct: row.latestChangePct }
    })
  }, [hasMarketContext, sourceResultsQuery.data])

  // CHANGE-20260714-001: 自选 fallback 改用 useWatchlistMonitorStatus 单次聚合请求
  // 替代旧 useWatchlist + useBatchInstruments 两段查询，避免逐行 quote 和 N+1
  // 同时复用聚合结果判断 inWatchlist（monitor-status 仅返回 active 自选，故命中即 active）
  const monitorStatusQuery = useWatchlistMonitorStatus()

  // 自选变更操作
  const addWatchlist = useAddToWatchlist()
  const removeWatchlist = useRemoveFromWatchlist()

  // 备忘录
  const [memoOpen, setMemoOpen] = useState(false)
  const [memoContent, setMemoContent] = useState('')
  const [memoNotify, setMemoNotify] = useState(false)
  const stockMemoQuery = useStockMemo(instrumentId)
  const upsertMemo = useUpsertStockMemo()
  const deleteMemo = useDeleteStockMemo()

  // memo 数据同步
  useEffect(() => {
    if (stockMemoQuery.data) {
      setMemoContent(stockMemoQuery.data.content)
      setMemoNotify(stockMemoQuery.data.notify_feishu)
    } else {
      setMemoContent('')
      setMemoNotify(false)
    }
  }, [stockMemoQuery.data])

  // 判断当前股票是否已在自选（monitor-status 仅返回 active 自选，命中即 active）
  const inWatchlist = useMemo(() => {
    if (!instrumentId || !monitorStatusQuery.data) return false
    return monitorStatusQuery.data.items.some(
      (item) => item.instrument_id === instrumentId,
    )
  }, [instrumentId, monitorStatusQuery.data])

  // 操作：加入/移出自选
  const handleToggleWatchlist = useCallback(() => {
    if (!instrumentId) return
    if (inWatchlist) {
      removeWatchlist.mutate(instrumentId, {
        onSuccess: () => showToast('操作完成', '已移出自选'),
      })
    } else {
      addWatchlist.mutate(
        { instrument_id: instrumentId, source },
        { onSuccess: () => showToast('操作完成', '已加入自选') },
      )
    }
  }, [instrumentId, inWatchlist, removeWatchlist, addWatchlist, source, showToast])

  // 来源股票列表（active 自选 + changePct，单次聚合请求）
  // CHANGE-20260714-001: 从 monitor-status items 提取 symbol/name/changePct
  // changePct 来源：item.metrics.change_pct（来自 StockFeatureSnapshot.summary_payload）
  const watchlistStocks = useMemo(() => {
    if (!monitorStatusQuery.data?.items) return []
    return monitorStatusQuery.data.items.map((item) => {
      const metrics = (item.metrics ?? {}) as Record<string, unknown>
      const changePct = toNum(pickPayload(metrics, CHANGE_PCT_KEYS))
      return {
        symbol: item.symbol,
        name: item.name,
        changePct,
      }
    })
  }, [monitorStatusQuery.data])

  // 统一来源列表：优先市场搜索结果，回退自选列表
  // CHANGE-20260713-009: scope=market → sourceListKind=market；scope=watchlist → sourceListKind=watchlist
  const sourceListKind: SourceListKind = hasMarketContext
    ? (marketContext!.scope === 'market' ? 'market' : 'watchlist')
    : 'watchlist'
  const sourceStocks = hasMarketContext ? marketStocks : watchlistStocks
  // CHANGE-20260715-004: 来源列表加载状态
  // - hasMarketContext=true 时：published runs 加载中、或 activeRunId 仍缺失、或 DSA results 加载中
  // - hasMarketContext=false 时：monitorStatusQuery 加载中即为 loading
  const sourceListLoading = hasMarketContext
    ? publishedRunsQuery.isLoading || !activeRunId || sourceResultsQuery.isLoading
    : monitorStatusQuery.isLoading

  // C7: 上一只/下一只基于 sourceStocks（而非 watchlistItems）
  // 在来源列表中按 symbol 查找当前位置，支持市场搜索结果和自选列表两种来源
  const currentIndex = symbol
    ? sourceStocks.findIndex((s) => s.symbol === symbol)
    : -1
  const canNavigate = sourceStocks.length >= 2 && currentIndex >= 0

  const navigateToStock = useCallback((direction: number) => {
    if (!canNavigate) return
    const nextIndex = (currentIndex + direction + sourceStocks.length) % sourceStocks.length
    const target = sourceStocks[nextIndex]
    if (!target?.symbol) return
    // 保留 returnTo 上下文 + source + strategy，使切换后仍可返回来源列表
    const returnToParam = returnTo ? `&returnTo=${encodeURIComponent(returnTo)}` : ''
    navigate(`/stock/${target.symbol}?source=${source}&strategy=${strategy}${returnToParam}`)
  }, [canNavigate, currentIndex, sourceStocks, navigate, strategy, source, returnTo])

  return {
    inWatchlist,
    handleToggleWatchlist,
    addWatchlistPending: addWatchlist.isPending,
    removeWatchlistPending: removeWatchlist.isPending,
    canNavigate,
    navigateToStock,
    memoOpen,
    setMemoOpen,
    memoContent,
    setMemoContent,
    memoNotify,
    setMemoNotify,
    stockMemoQuery,
    upsertMemo,
    deleteMemo,
    hasMemo: !!stockMemoQuery.data,
    sourceStocks,
    sourceListKind,
    sourceListLoading,
    watchlistStocks,
  }
}
