import React, { useState, useRef, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useInstruments } from '../../hooks/useApi'
import { useWatchlist, useAddToWatchlist, useRemoveFromWatchlist } from '../../hooks/useApi'
import { useAuthStore } from '../../store/auth'
import { useToast } from '../../store/toast'
import { buildStockDetailUrl } from '../../features/stock-research/stockDetailNavigation'
import type { Instrument } from '../../api/endpoints'
import styles from './GlobalStockSearch.module.scss'

export function GlobalStockSearch() {
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [input, setInput] = useState('')
  const [open, setOpen] = useState(false)

  const isAdmin = useAuthStore((s) => s.user?.is_admin === true)
  const capabilities = useAuthStore((s) => s.user?.capabilities)
  // [Commit A A8] resource-scope 详情导航：
  // - market_data（或 admin）任意结果可进详情（originScope=market，左栏 /market 上下文）
  // - self_selection-only：仅「已自选」结果可进详情（originScope=direct，隐藏左栏，后端 resource guard 放行 own watchlist）；
  //   未自选结果主体点击不导航（只保留 ☆ 加自选）
  const hasMarketData = !!capabilities?.market_data?.active
  const hasSelfSelection = !!capabilities?.self_selection?.active
  const canManageWatchlist = isAdmin || hasSelfSelection

  const addToWatchlist = useAddToWatchlist()
  const removeFromWatchlist = useRemoveFromWatchlist()

  // 顶部搜索 = instrument discovery（股票身份 metadata），不是行情搜索：
  // 数据源用 GET /v1/instruments（仅 id/symbol/name/market 等 metadata），
  // 禁止复用 /v1/market/stocks（要求 market_data，self_selection-only 会 403）。
  const searchQuery = input.trim()

  const { data, isFetching } = useInstruments(
    {
      keyword: searchQuery || undefined,
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

  // /v1/instruments 返回 Instrument（id 字段），直接消费，不做 id→instrument_id cast
  const results: Instrument[] = data?.items ?? []

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [])

  const handleMainClick = (item: Instrument) => {
    // [Commit A A8] market_data/admin：任意结果可进详情
    if (isAdmin || hasMarketData) {
      const url = buildStockDetailUrl(item.symbol, { originScope: 'market' })
      navigate(url)
      return
    }
    // self_selection-only：仅已自选结果可进详情（direct：隐藏左栏，后端 resource guard 校验 own watchlist）
    if (hasSelfSelection && watchlistInstrumentIds.has(item.id)) {
      const url = buildStockDetailUrl(item.symbol, { originScope: 'direct' })
      navigate(url)
      return
    }
    // 未自选（或完全无权限）：主体不进详情，仅保留 ☆ 加自选能力
    useToast
      .getState()
      .show(hasSelfSelection ? '未在自选列表' : '无权限', '加入自选后可查看该股票详情')
  }

  const handleStarClick = (item: Instrument, e: React.MouseEvent) => {
    e.stopPropagation()
    if (!canManageWatchlist) {
      useToast.getState().show('无权限', '当前账户无权管理自选股')
      return
    }
    const isWatched = watchlistInstrumentIds.has(item.id)
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
      removeFromWatchlist.mutate(item.id, { onError })
    } else {
      addToWatchlist.mutate(
        { instrument_id: item.id },
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
              const watched = watchlistInstrumentIds.has(item.id)
              return (
                <div
                  key={item.id}
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
