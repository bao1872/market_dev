// [useStockDetailActions] - 描述: StockDetailPage 专属 actions hook
// 负责自选列表查询、加入/移出自选、上下切换、memo 读取/保存/删除。
// 这些操作是详情页专属，不得进入 /market 的 useStockResearchData 核心 hook。
import { useState, useMemo, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  useWatchlist,
  useBatchInstruments,
  useAddToWatchlist,
  useRemoveFromWatchlist,
  useStockMemo,
  useUpsertStockMemo,
  useDeleteStockMemo,
  useMarketStocks,
} from '@/hooks/useApi'
import { normalizeInternalReturnTo } from '@/features/market-workspace/marketWorkspaceUrlState'
import { useToast } from '@/store/toast'
import type { ResearchSource } from './stockResearchTypes'

export interface StockDetailActionsParams {
  instrumentId: string | undefined
  symbol: string | undefined
  source: ResearchSource
  strategy: string
  // returnTo URL（来自详情页 URL 参数），用于恢复来源列表的 scope/query/page/sort 上下文
  // 当 returnTo 指向 /market?scope=market&query=xxx&page=2&sort=xxx 时，左栏优先展示该市场搜索结果
  // returnTo 缺失或非 /market 前缀时回退到自选列表
  returnTo?: string | null
}

// 来源股票列表项（左栏统一渲染结构）
export interface SourceStockItem {
  symbol: string
  name: string
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
  // 兼容旧接口（= sourceStocks，仅当 sourceListKind==='watchlist' 时有值）
  watchlistStocks: SourceStockItem[]
}

// 从 returnTo URL 解析市场列表查询参数（仅 /market 前缀有效）
// 返回 null 表示 returnTo 不指向 /market 或无有效查询参数（应回退到自选列表）
function parseMarketParamsFromReturnTo(
  returnTo: string | null | undefined,
): { scope: 'market'; query?: string; page?: number; sort?: string } | null {
  const safe = normalizeInternalReturnTo(returnTo)
  if (!safe) return null
  // 仅处理 /market 前缀（/screener /messages 不含市场列表参数）
  if (!safe.startsWith('/market')) return null
  const qs = safe.split('?')[1]
  if (!qs) return null
  const params = new URLSearchParams(qs)
  const scope = params.get('scope')
  // 仅 market scope 的搜索结果有意义恢复（watchlist scope 已由自选列表覆盖）
  if (scope !== 'market') return null
  const query = params.get('query') ?? undefined
  const pageRaw = params.get('page')
  const page = pageRaw ? parseInt(pageRaw, 10) : undefined
  const sort = params.get('sort') ?? undefined
  // 至少有一个可恢复的参数才返回（避免空查询拉全市场）
  if (!query && !page && !sort) return null
  return {
    scope: 'market',
    query: query || undefined,
    page: Number.isFinite(page) && (page as number) >= 1 ? page as number : undefined,
    sort: sort || undefined,
  }
}

export function useStockDetailActions({
  instrumentId,
  source,
  strategy,
  returnTo,
}: StockDetailActionsParams): StockDetailActions {
  const navigate = useNavigate()
  const showToast = useToast((s) => s.show)

  // [returnTo 上下文恢复] - 优先解析 returnTo 中的市场搜索参数
  // 当 returnTo 指向 /market?scope=market&query=xxx&page=2&sort=xxx 时，
  // 左栏展示该搜索结果列表（点击返回时回到来源页的同一上下文）
  const marketParams = useMemo(() => parseMarketParamsFromReturnTo(returnTo), [returnTo])
  const hasMarketContext = marketParams !== null
  const marketStocksQuery = useMarketStocks({
    scope: 'market',
    query: marketParams?.query,
    page: marketParams?.page,
    sort: marketParams?.sort,
    page_size: 50,
  })

  // 自选列表查询（用于判断当前股票是否在自选 + 上下切换 + returnTo 缺失时回退左栏）
  const watchlistQuery = useWatchlist()
  const watchlistInstrumentIds = useMemo(
    () => watchlistQuery.data?.items.map((item) => item.instrument_id) ?? [],
    [watchlistQuery.data],
  )
  const batchInstrumentsQuery = useBatchInstruments(watchlistInstrumentIds)
  const instrumentSymbolMap = useMemo(() => {
    const map = new Map<string, string>()
    if (!batchInstrumentsQuery.data?.items) return map
    for (const inst of batchInstrumentsQuery.data.items) {
      map.set(inst.id, inst.symbol)
    }
    return map
  }, [batchInstrumentsQuery.data])

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

  // 判断当前股票是否已在自选（active=true）
  const inWatchlist = useMemo(() => {
    if (!instrumentId || !watchlistQuery.data) return false
    return watchlistQuery.data.items.some(
      (item) => item.instrument_id === instrumentId && item.active,
    )
  }, [instrumentId, watchlistQuery.data])

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

  // 在自选列表中上下切换股票
  const watchlistItems = useMemo(
    () => watchlistQuery.data?.items ?? [],
    [watchlistQuery.data],
  )
  const currentIndex = instrumentId
    ? watchlistItems.findIndex((item) => item.instrument_id === instrumentId)
    : -1
  const canNavigate = watchlistItems.length >= 2 && currentIndex >= 0

  const navigateToStock = useCallback((direction: number) => {
    if (!canNavigate) return
    const nextIndex = (currentIndex + direction + watchlistItems.length) % watchlistItems.length
    const target = watchlistItems[nextIndex]
    const targetSymbol = instrumentSymbolMap.get(target.instrument_id)
    if (!targetSymbol) return
    navigate(`/stock/${targetSymbol}?source=watchlist&strategy=${strategy}`)
  }, [canNavigate, currentIndex, watchlistItems, instrumentSymbolMap, navigate, strategy])

  // 来源股票列表（active 自选，含 symbol + name，用于详情页左栏）
  const watchlistStocks = useMemo(() => {
    if (!watchlistQuery.data?.items || !batchInstrumentsQuery.data?.items) return []
    const instMap = new Map(batchInstrumentsQuery.data.items.map((i) => [i.id, i]))
    return watchlistQuery.data.items
      .filter((item) => item.active)
      .map((item) => {
        const inst = instMap.get(item.instrument_id)
        return inst ? { symbol: inst.symbol, name: inst.name } : null
      })
      .filter((x): x is SourceStockItem => x !== null)
  }, [watchlistQuery.data, batchInstrumentsQuery.data])

  // 市场搜索结果列表（returnTo 上下文恢复）
  const marketStocks = useMemo(() => {
    if (!hasMarketContext || !marketStocksQuery.data?.items) return []
    return marketStocksQuery.data.items.map((row) => ({
      symbol: row.symbol,
      name: row.name,
    }))
  }, [hasMarketContext, marketStocksQuery.data])

  // 统一来源列表：优先市场搜索结果，回退自选列表
  const sourceListKind: SourceListKind = hasMarketContext ? 'market' : 'watchlist'
  const sourceStocks = hasMarketContext ? marketStocks : watchlistStocks

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
    watchlistStocks,
  }
}
