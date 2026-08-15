import React, { useState, useRef, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMarketStocks } from '../../hooks/useApi'
import { useWatchlist, useAddToWatchlist, useRemoveFromWatchlist } from '../../hooks/useApi'
import { useAuthStore } from '../../store/auth'
import { useToast } from '../../store/toast'
import { buildStockDetailUrl } from '../../features/stock-research/stockDetailNavigation'
import styles from './GlobalStockSearch.module.scss'

interface StockSearchItem {
  symbol: string
  name: string
  instrument_id: string
  market?: string
}

export function GlobalStockSearch() {
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [input, setInput] = useState('')
  const [open, setOpen] = useState(false)

  const isAdmin = useAuthStore((s) => s.user?.is_admin === true)
  const capabilities = useAuthStore((s) => s.user?.capabilities)
  const canAccessStockDetail = isAdmin || !!capabilities?.market_data?.active
  const canManageWatchlist = isAdmin || !!capabilities?.self_selection?.active

  const addToWatchlist = useAddToWatchlist()
  const removeFromWatchlist = useRemoveFromWatchlist()

  const searchQuery = input.trim()

  const { data, isFetching } = useMarketStocks(
    {
      scope: 'market',
      query: searchQuery || undefined,
      page: 1,
      page_size: 8,
    },
    {
      enabled: searchQuery.length > 0,
    },
  )

  const { data: watchlistData } = useWatchlist({
    enabled: canManageWatchlist,
  })

  const watchlistInstrumentIds = useMemo(() => {
    const set = new Set<string>()
    for (const item of watchlistData?.items ?? []) {
      if (item.instrument_id) set.add(item.instrument_id)
    }
    return set
  }, [watchlistData?.items])

  const results: StockSearchItem[] = (data?.items ?? []) as StockSearchItem[]

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  const handleMainClick = (item: StockSearchItem) => {
    if (!canAccessStockDetail) {
      useToast.getState().show('无权限', '当前账户无权查看个股详情')
      return
    }
    const url = buildStockDetailUrl(item.symbol, { originScope: 'market' })
    navigate(url)
  }

  const handleStarClick = (item: StockSearchItem, e: React.MouseEvent) => {
    e.stopPropagation()
    if (!canManageWatchlist) {
      useToast.getState().show('无权限', '当前账户无权管理自选股')
      return
    }
    const isWatched = watchlistInstrumentIds.has(item.instrument_id)
    const onError = (err: unknown) => {
      let message = '自选操作失败'
      const resp = (err as { response?: { data?: { detail?: string; message?: string } } })?.response
      const payload = resp?.data
      if (payload) {
        message = payload.detail ?? payload.message ?? '自选操作失败'
      }
      useToast.getState().show('操作失败', message)
    }
    if (isWatched) {
      removeFromWatchlist.mutate(item.instrument_id, { onError })
    } else {
      addToWatchlist.mutate(
        { instrument_id: item.instrument_id },
        { onError },
      )
    }
  }

  const showStar = canManageWatchlist
  const showList = open && searchQuery.length > 0

  return (
    <div className={styles.container} ref={containerRef}>
      <div className={styles.inputWrap}>
        <input
          ref={inputRef}
          className={styles.input}
          placeholder="搜索股票 / 代码"
          value={input}
          onChange={(e) => {
            setInput(e.target.value)
            setOpen(true)
          }}
          onFocus={() => setOpen(true)}
        />
        {isFetching && <span className={styles.spinner} />}
      </div>

      {showList && (
        <div className={styles.resultPanel} role="listbox">
          {results.length === 0 ? (
            <div className={styles.empty}>无匹配结果</div>
          ) : (
            results.map((item) => {
              const watched = watchlistInstrumentIds.has(item.instrument_id)
              return (
                <div
                  key={item.instrument_id}
                  className={styles.resultItem}
                  role="option"
                  aria-selected={false}
                  onClick={() => handleMainClick(item)}
                >
                  <span className={styles.symbol}>{item.symbol}</span>
                  <span className={styles.name}>{item.name}</span>
                  {showStar && (
                    <button
                      type="button"
                      className={styles.starBtn}
                      aria-label={watched ? '取消自选' : '加入自选'}
                      onClick={(e) => handleStarClick(item, e)}
                    >
                      {watched ? '★' : '☆'}
                    </button>
                  )}
                </div>
              )
            })
          )}
        </div>
      )}
    </div>
  )
}

export default GlobalStockSearch
