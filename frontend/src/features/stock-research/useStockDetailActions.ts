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
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import type { ResearchSource } from './stockResearchTypes'

export interface StockDetailActionsParams {
  instrumentId: string | undefined
  symbol: string | undefined
  source: ResearchSource
  strategy: string
}

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
}

export function useStockDetailActions({
  instrumentId,
  source,
  strategy,
}: StockDetailActionsParams): StockDetailActions {
  const navigate = useNavigate()
  const showToast = useToast((s) => s.show)

  // 自选列表查询（用于判断当前股票是否在自选 + 上下切换）
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
  }
}
