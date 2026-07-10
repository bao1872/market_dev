// [MarketInstrumentPane] - 描述: 行情工作区左栏（股票列表/搜索/筛选）
// scope=watchlist：使用 useWatchlistMonitorStatus 聚合数据
// scope=market：使用 useInstruments 搜索（至少 2 字符，限制结果数，不为每行发实时行情请求）
import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  useWatchlistMonitorStatus,
  useInstruments,
} from '@/hooks/useApi'
import type { WatchlistMonitorStatusItem, Instrument } from '@/api/endpoints'
import {
  adaptWatchlistMonitorStatusItem,
  type WatchlistMonitorRow,
} from '@/features/watchlist-monitor'
import clsx from 'clsx'
import styles from './MarketInstrumentPane.module.scss'

export interface MarketInstrumentPaneProps {
  scope: 'watchlist' | 'market'
  selectedSymbol: string | null
  onSelectSymbol: (symbol: string, instrumentId: string) => void
}

export function MarketInstrumentPane({ scope, selectedSymbol, onSelectSymbol }: MarketInstrumentPaneProps) {
  // watchlist scope：聚合监控状态
  const monitorStatusQuery = useWatchlistMonitorStatus()
  const watchlistRows: WatchlistMonitorRow[] = useMemo(
    () => (monitorStatusQuery.data?.items ?? []).map((item: WatchlistMonitorStatusItem) =>
      adaptWatchlistMonitorStatusItem(item),
    ),
    [monitorStatusQuery.data],
  )

  // market scope：搜索（至少 2 字符，限制 50 条，不为每行发实时行情请求）
  const [keyword, setKeyword] = useState('')
  const [debouncedKeyword, setDebouncedKeyword] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedKeyword(keyword), 250)
    return () => clearTimeout(timer)
  }, [keyword])

  const canSearch = debouncedKeyword.trim().length >= 2
  const instrumentsQuery = useInstruments({
    keyword: canSearch ? debouncedKeyword.trim() : undefined,
    page_size: 50,
  })
  const searchResults: Instrument[] = instrumentsQuery.data?.items ?? []

  const handleSelect = useCallback(
    (symbol: string, instrumentId: string) => {
      onSelectSymbol(symbol, instrumentId)
    },
    [onSelectSymbol],
  )

  return (
    <div className={styles.pane}>
      <div className={styles.header}>
        <span className={styles.title}>{scope === 'watchlist' ? '自选股' : '全市场搜索'}</span>
      </div>

      {scope === 'market' && (
        <div className={styles.searchWrap}>
          <input
            className={styles.searchInput}
            placeholder="输入代码或名称（至少 2 字符）"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            autoFocus
          />
          {!canSearch && keyword.length > 0 && (
            <div className={styles.hint}>请输入至少 2 个字符</div>
          )}
        </div>
      )}

      <div className={styles.list}>
        {scope === 'watchlist' && (
          <>
            {monitorStatusQuery.isLoading && <div className={styles.loading}>加载中…</div>}
            {monitorStatusQuery.isError && <div className={styles.error}>加载失败，请刷新重试</div>}
            {!monitorStatusQuery.isLoading && watchlistRows.length === 0 && (
              <div className={styles.empty}>暂无自选股票</div>
            )}
            {watchlistRows.map((row) => (
              <button
                key={row.instrument_id}
                className={clsx(
                  styles.row,
                  selectedSymbol === row.symbol && styles.rowActive,
                )}
                onClick={() => handleSelect(row.symbol, row.instrument_id)}
              >
                <div className={styles.rowMain}>
                  <span className={styles.rowName}>{row.name}</span>
                  <span className={styles.rowSymbol}>{row.symbol}</span>
                </div>
                {row.change_pct !== null && row.change_pct !== undefined && (
                  <span className={clsx(styles.rowChange, row.change_pct >= 0 ? styles.up : styles.down)}>
                    {row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(2)}%
                  </span>
                )}
              </button>
            ))}
          </>
        )}

        {scope === 'market' && (
          <>
            {canSearch && instrumentsQuery.isLoading && (
              <div className={styles.loading}>搜索中…</div>
            )}
            {canSearch && !instrumentsQuery.isLoading && searchResults.length === 0 && (
              <div className={styles.empty}>未找到匹配的股票</div>
            )}
            {!canSearch && (
              <div className={styles.empty}>输入关键词搜索股票</div>
            )}
            {searchResults.map((inst: Instrument) => (
              <button
                key={inst.id}
                className={clsx(
                  styles.row,
                  selectedSymbol === inst.symbol && styles.rowActive,
                )}
                onClick={() => handleSelect(inst.symbol, inst.id)}
              >
                <div className={styles.rowMain}>
                  <span className={styles.rowName}>{inst.name}</span>
                  <span className={styles.rowSymbol}>{inst.symbol}</span>
                </div>
                <span className={styles.rowMarket}>{inst.market}</span>
              </button>
            ))}
          </>
        )}
      </div>
    </div>
  )
}
