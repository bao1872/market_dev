import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMarketStocks } from '@/hooks/useApi'
import {
  useWatchlist,
  useAddToWatchlist,
  useRemoveFromWatchlist,
} from '@/hooks/useApi'
import { useAuthStore } from '@/store/auth'
import { useToast } from '@/store/toast'
import { buildStockDetailUrl } from '@/features/stock-research/stockDetailNavigation'
import styles from './GlobalStockSearch.module.scss'

/**
 * GlobalStockSearch — Global Header 单一股票搜索入口。
 *
 * 职责边界（PRD 40 MX-07）：
 * - 位于 Global Header，跨模块存在；
 * - 查询源固定为 Market（universe=market），不依赖当前 workspace scope；
 * - 主点击进入 Market-source 个股详情，复用 canonical buildStockDetailUrl；
 * - ☆/★ 与股票主点击为两个独立 action，复用 canonical watchlist mutation；
 * - 不写回任何 workspace keyword / filter / sort / page。
 */
export default function GlobalStockSearch() {
  const navigate = useNavigate()
  const [input, setInput] = useState('')
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const isAdmin = useAuthStore((s) => s.user?.is_admin === true)
  const capabilities = useAuthStore((s) => s.user?.capabilities)
  const canAccessStockDetail = isAdmin || !!capabilities?.market_data?.active
  const canManageWatchlist = isAdmin || !!capabilities?.self_selection?.active

  const { data, isFetching } = useMarketStocks({
    scope: 'market',
    query: input || undefined,
    page: 1,
    page_size: 8,
  })

  const { data: watchlistData } = useWatchlist()
  const addToWatchlist = useAddToWatchlist()
  const removeFromWatchlist = useRemoveFromWatchlist()

  const watchlistInstrumentIds = useMemo(() => {
    const items = watchlistData?.items ?? []
    return new Set(
      items.filter((item) => item.active).map((item) => item.instrument_id),
    )
  }, [watchlistData])

  const results = data?.items ?? []

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        containerRef.current &&
        !containerRef.current.contains(event.target as Node)
      ) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  function handleMainClick(symbol: string) {
    if (!canAccessStockDetail) {
      useToast
        .getState()
        .show({ title: '无权限', message: '当前账户无权查看个股详情' })
      return
    }
    setOpen(false)
    setInput('')
    navigate(buildStockDetailUrl(symbol, { originScope: 'market' }))
  }

  function handleWatchlistToggle(
    event: React.MouseEvent,
    instrumentId: string,
  ) {
    event.stopPropagation()
    if (!canManageWatchlist) {
      useToast
        .getState()
        .show({ title: '无权限', message: '当前账户无权管理自选股' })
      return
    }
    if (watchlistInstrumentIds.has(instrumentId)) {
      removeFromWatchlist.mutate(instrumentId)
    } else {
      addToWatchlist.mutate(
        { instrument_id: instrumentId },
        {
          onError: (err: unknown) => {
            const message =
              (err as { response?: { data?: { message?: string } } })?.response
                ?.data?.message ?? '自选操作失败'
            useToast.getState().show({ title: '操作失败', message })
          },
        },
      )
    }
  }

  return (
    <div className={styles.container} ref={containerRef}>
      <input
        className={styles.input}
        type="text"
        placeholder="搜索股票代码 / 名称"
        value={input}
        onChange={(e) => {
          setInput(e.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
      />
      {open && input.trim() !== '' && (
        <div className={styles.resultPanel}>
          {isFetching && <div className={styles.hint}>搜索中…</div>}
          {!isFetching && results.length === 0 && (
            <div className={styles.hint}>无匹配结果</div>
          )}
          {!isFetching &&
            results.map((stock) => {
              const isStarred = watchlistInstrumentIds.has(stock.instrument_id)
              return (
                <div
                  key={stock.instrument_id}
                  className={styles.resultItem}
                  onClick={() => handleMainClick(stock.symbol)}
                >
                  <span className={styles.symbol}>{stock.symbol}</span>
                  <span className={styles.name}>{stock.name}</span>
                  <button
                    type="button"
                    className={styles.starButton}
                    disabled={!canManageWatchlist}
                    title={isStarred ? '移除自选' : '加入自选'}
                    onClick={(e) => handleWatchlistToggle(e, stock.instrument_id)}
                  >
                    {isStarred ? '★' : '☆'}
                  </button>
                </div>
              )
            })}
        </div>
      )}
    </div>
  )
}
