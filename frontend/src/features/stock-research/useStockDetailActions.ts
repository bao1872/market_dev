// [useStockDetailActions] - 描述: StockDetailPage 专属 actions hook
// 负责自选列表查询、加入/移出自选、上下切换、memo 读取/保存/删除。
// 这些操作是详情页专属，不得进入 /market 的 useStockResearchData 核心 hook。
//
// [CHANGE-20260731-SAME-SOURCE] 详情左栏改用 /market/stocks 同源查询（mcq 新合同）：
//   - market/watchlist 有 mcq → useMarketStocks(mcq)，左栏顺序与 /market 当前页完全一致
//   - 无 mcq 但有 sourceRunId+cq → backward 兼容旧 DSA useStrategyRunResults（deprecated）
//   - useWatchlistMonitorStatus 仅用于 inWatchlist 状态，禁止充当来源列表数据源
//   - direct 来源无来源列表（UI 隐藏左栏）
//   - 失效（sourceContextInvalid）时显示 invalid 占位，禁止静默回退自选或另一来源
import { useState, useMemo, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  useWatchlistMonitorStatus,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useStockMemo,
  useUpsertStockMemo,
  useDeleteStockMemo,
  useStrategyRunResults,
  useMarketStocks,
} from '@/hooks/useApi'
import {
  type StrategyResultQuery,
} from '@/features/market-workspace/marketWorkspaceUrlState'
import {
  adaptStrategyResultToTrendRow,
  adaptMarketStockToTrendRow,
  getStockDisplay,
} from '@/features/trend-selection'
import { useToast } from '@/store/toast'
import type { ResearchSource } from './stockResearchTypes'
import type { StrategyResultQueryParams, MarketStocksQueryParams } from '@/api/endpoints'
import { buildStockDetailUrl, type OriginScope, type MarketCanonicalQuery } from './stockDetailNavigation'

export interface StockDetailActionsParams {
  instrumentId: string | undefined
  symbol: string | undefined
  // [DetailSourceContextV2] origin 替代 V1 source，为来源唯一真源
  origin: OriginScope
  // [DEPRECATED 20260731-SAME-SOURCE] 旧 DSA sourceRunId，仅 backward 兼容（推荐用 mcq）
  sourceRunId: string | null
  // [DEPRECATED 20260731-SAME-SOURCE] 旧 DSA canonicalQuery，仅 backward 兼容（推荐用 mcq）
  canonicalQuery: StrategyResultQuery | null
  // [DEPRECATED 20260731-SAME-SOURCE] 旧 DSA canonicalQueryRaw（切股时原样透传，兼容旧链接）
  canonicalQueryRaw: string | null
  // [CHANGE-20260731-SAME-SOURCE] Market Canonical Query（解析后对象，/market/stocks 同源查询参数）
  marketCanonicalQuery: MarketCanonicalQuery | null
  // [CHANGE-20260731-SAME-SOURCE] mcq 原始 JSON 字符串（切股时原样透传到导航 URL）
  marketCanonicalQueryRaw: string | null
  // [DetailSourceContextV2] 来源上下文失效（market/watchlist 缺 mcq/scope不匹配/冲突 或 旧合同缺 runId/cq）
  sourceContextInvalid: boolean
  // returnTo URL（来自详情页 URL 参数），用于上一只/下一只导航保留来源上下文
  returnTo?: string | null
  // 当前 timeframe，用于上一只/下一只导航保留周期
  timeframe?: string | null
}

// 来源股票列表项（左栏统一渲染结构）
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
  sourceStocks: SourceStockItem[]
  sourceListKind: SourceListKind
  // 来源列表加载状态（DSA results 加载中）
  sourceListLoading: boolean
  // 来源列表错误状态（DSA results 查询失败）
  sourceListError: boolean
  // 来源列表空状态（非 loading 非 error 但 sourceStocks 为空）
  sourceListEmpty: boolean
  // 来源上下文失效（market/watchlist 缺 runId/cq/universe不匹配/冲突）
  sourceContextInvalid: boolean
  // 兼容旧接口（= sourceStocks，仅当 sourceListKind==='watchlist' 时有值）
  watchlistStocks: SourceStockItem[]
}

export function useStockDetailActions({
  instrumentId,
  symbol,
  origin,
  sourceRunId,
  canonicalQuery,
  canonicalQueryRaw,
  marketCanonicalQuery,
  marketCanonicalQueryRaw,
  sourceContextInvalid,
  returnTo,
  timeframe,
}: StockDetailActionsParams): StockDetailActions {
  const navigate = useNavigate()
  const showToast = useToast((s) => s.show)

  // [DetailSourceContextV2] 来源列表类型：market → market；watchlist/direct → watchlist
  // direct 时 UI 隐藏左栏（StockDetailPage 根据 origin==='direct' 判断）
  const sourceListKind: SourceListKind = origin === 'market' ? 'market' : 'watchlist'
  // V1 兼容 source（用于 addWatchlist.mutate source 字段）
  const source: ResearchSource = origin === 'market' ? 'selection' : 'watchlist'

  // [CHANGE-20260731-SAME-SOURCE] 优先使用 mcq（Market Canonical Query）新合同：
  //   market/watchlist 有有效 mcq → useMarketStocks(mcq)，同源同序
  //   无 mcq 但有 sourceRunId+cq → backward 兼容旧 DSA useStrategyRunResults（deprecated）
  //   direct 或失效时不查询
  // 禁止 fresh usePublishedRuns 重新推导 activeRunId（避免新 run 发布后来源列表漂移）
  const useMcq = !sourceContextInvalid && (origin === 'market' || origin === 'watchlist') && !!marketCanonicalQuery
  const useLegacyDsa = !sourceContextInvalid && !useMcq && (origin === 'market' || origin === 'watchlist') && !!sourceRunId && !!canonicalQuery
  const hasValidSourceContext = useMcq || useLegacyDsa

  // ===== 新 mcq 合同：useMarketStocks =====
  // marketCanonicalQuery 已由 detailSourceContext 校验 scope/page/page_size 合法，直接透传
  const marketStocksParams: MarketStocksQueryParams = useMemo(
    () => ({
      scope: marketCanonicalQuery!.scope,
      query: marketCanonicalQuery!.query ?? undefined,
      industry: marketCanonicalQuery!.industry ?? undefined,
      concept: marketCanonicalQuery!.concept ?? undefined,
      fp_filter: marketCanonicalQuery!.fp_filter ?? undefined,
      fp_sort: marketCanonicalQuery!.fp_sort ?? undefined,
      page: marketCanonicalQuery!.page,
      page_size: marketCanonicalQuery!.page_size,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [marketCanonicalQueryRaw], // 依赖 raw JSON 字符串（入口时刻 URL 参数，切股时不变）
  )
  const marketStocksQuery = useMarketStocks(useMcq ? marketStocksParams : undefined)

  // ===== 旧 DSA 合同（backward 兼容）：useStrategyRunResults =====
  // [FIX max-update-depth] 稳定化 canonicalQuery 引用，避免 resolveDetailSourceContextV2 每次返回新对象
  //   导致 useStrategyRunResults 的 params 引用变化（虽然 React Query hashKey 做深比较，但稳定引用更安全）
  //   依赖 canonicalQueryRaw（入口时刻 URL 字符串，切股时不变），确保同来源上下文内 queryKey 稳定
  const stableCanonicalQuery = useMemo(
    () => (useLegacyDsa ? (canonicalQuery as StrategyResultQueryParams) : undefined),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [useLegacyDsa, canonicalQueryRaw, sourceRunId],
  )
  const sourceResultsQuery = useStrategyRunResults(
    useLegacyDsa ? sourceRunId! : undefined,
    stableCanonicalQuery,
  )

  // 来源列表行数据（统一：mcq→adaptMarketStockToTrendRow；legacy DSA→adaptStrategyResultToTrendRow）
  const sourceStocks = useMemo(() => {
    if (!hasValidSourceContext) return []
    if (useMcq) {
      if (!marketStocksQuery.data?.items) return []
      return marketStocksQuery.data.items.map((r) => {
        const row = adaptMarketStockToTrendRow(r)
        const display = getStockDisplay(row)
        return { symbol: display.symbol, name: display.name, changePct: row.latestChangePct }
      })
    }
    // legacy DSA
    if (!sourceResultsQuery.data?.items) return []
    return sourceResultsQuery.data.items.map((r) => {
      const row = adaptStrategyResultToTrendRow(r)
      const display = getStockDisplay(row)
      return { symbol: display.symbol, name: display.name, changePct: row.latestChangePct }
    })
  }, [hasValidSourceContext, useMcq, marketStocksQuery.data, sourceResultsQuery.data])

  // [DetailSourceContextV2] useWatchlistMonitorStatus 仅用于 inWatchlist 状态判断
  // 禁止用作来源列表数据源（V1 根因：watchlist 来源用 monitor-status API，与列表页 dsa_selector universe=watchlist 不同链）
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

  // [DetailSourceContextV2] 来源列表状态
  // - mcq 有效：marketStocksQuery.isLoading/error/empty
  // - legacy DSA 有效：sourceResultsQuery.isLoading/error/empty
  // - direct/失效：loading=false, error=false, empty=false（UI 不渲染列表）
  const sourceListLoading = hasValidSourceContext
    ? useMcq ? marketStocksQuery.isLoading : sourceResultsQuery.isLoading
    : false
  const sourceListError = hasValidSourceContext
    ? useMcq ? !!marketStocksQuery.error : !!sourceResultsQuery.error
    : false
  const sourceListEmpty =
    hasValidSourceContext &&
    !sourceListLoading &&
    !sourceListError &&
    sourceStocks.length === 0

  // 上一只/下一只基于 sourceStocks
  const currentIndex = symbol
    ? sourceStocks.findIndex((s) => s.symbol === symbol)
    : -1
  const canNavigate = sourceStocks.length >= 2 && currentIndex >= 0

  const navigateToStock = useCallback((direction: number) => {
    if (!canNavigate) return
    const nextIndex = (currentIndex + direction + sourceStocks.length) % sourceStocks.length
    const target = sourceStocks[nextIndex]
    if (!target?.symbol) return
    // [CHANGE-20260731-SAME-SOURCE] 切股时透传 mcq（优先）或旧 DSA 上下文，保持来源不变
    navigate(
      buildStockDetailUrl(target.symbol, {
        originScope: origin,
        returnTo,
        timeframe,
        sourceRunId,
        canonicalQuery: canonicalQueryRaw,
        marketCanonicalQuery: marketCanonicalQueryRaw,
      }),
    )
  }, [canNavigate, currentIndex, sourceStocks, navigate, origin, returnTo, timeframe, sourceRunId, canonicalQueryRaw, marketCanonicalQueryRaw])

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
    sourceListError,
    sourceListEmpty,
    sourceContextInvalid,
    watchlistStocks: sourceListKind === 'watchlist' ? sourceStocks : [],
  }
}
