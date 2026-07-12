// [MarketStockTable] - 描述: 服务端分页股票表格
// PRD §6.1：列表字段 + 交互 + 明确禁止 K线。
// 单击非链接区域更新 selected 并刷新右栏；名称进入详情；星标更新自选并保持筛选/分页。
import { useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAddToWatchlist, useRemoveFromWatchlist } from '@/hooks/useApi'
import type { MarketStockRow, MarketStocksResponse } from '@/api/endpoints'
import { encodeMarketWorkspaceUrl, type MarketWorkspaceUrlState } from './marketWorkspaceUrlState'
import clsx from 'clsx'
import styles from './MarketWorkspace.module.scss'

interface MarketStockTableProps {
  data: MarketStocksResponse | undefined
  isLoading: boolean
  isError: boolean
  onRetry: () => void
  selected: string | null
  onSelectRow: (symbol: string) => void
  scope: 'market' | 'watchlist'
  urlState: MarketWorkspaceUrlState
  onPageChange: (page: number) => void
}


export function MarketStockTable({
  data,
  isLoading,
  isError,
  onRetry,
  selected,
  onSelectRow,
  scope,
  urlState,
  onPageChange,
}: MarketStockTableProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const addWatchlist = useAddToWatchlist()
  const removeWatchlist = useRemoveFromWatchlist()

  const items = data?.items ?? []
  const total = data?.total ?? 0
  const page = data?.page ?? urlState.page
  const pageSize = data?.page_size ?? urlState.pageSize
  const totalPages = total > 0 ? Math.ceil(total / pageSize) : 0

  // 收盘快照判断：price_as_of 日期早于今天 → 价格来自日线收盘而非实时
  const priceAsOf = data?.price_as_of ?? null
  const isClosingSnapshot = (() => {
    if (!priceAsOf) return false
    const snapDate = priceAsOf.slice(0, 10)
    const today = new Date().toISOString().slice(0, 10)
    return snapDate < today
  })()

  // 点击股票名称：进入 /stock/:symbol?returnTo=<编码后的当前URL>
  const handleNavigateToDetail = useCallback(
    (symbol: string) => {
      const currentUrl = `/market?${encodeMarketWorkspaceUrl(urlState).toString()}`
      const returnTo = encodeURIComponent(currentUrl)
      navigate(`/stock/${symbol}?returnTo=${returnTo}`)
    },
    [navigate, urlState],
  )

  // 星标切换：更新自选并保持当前筛选/分页
  const handleToggleWatchlist = useCallback(
    (row: MarketStockRow) => {
      const queryKey = ['market-stocks', {
        scope,
        query: urlState.query || undefined,
        page: urlState.page,
        page_size: urlState.pageSize,
        sort: urlState.sort ?? undefined,
        industry: urlState.industry ?? undefined,
        concept: urlState.concept ?? undefined,
        state: urlState.state ?? undefined,
      }]
      if (row.is_watchlisted) {
        removeWatchlist.mutate(row.instrument_id, {
          onSuccess: () => {
            queryClient.invalidateQueries({ queryKey })
          },
        })
      } else {
        addWatchlist.mutate(
          { instrument_id: row.instrument_id, source: 'manual' },
          {
            onSuccess: () => {
              queryClient.invalidateQueries({ queryKey })
            },
          },
        )
      }
    },
    [addWatchlist, removeWatchlist, queryClient, scope, urlState],
  )

  // 涨跌幅颜色（A 股：红涨绿跌）
  const changePctColor = useCallback((pct: number | null) => {
    if (pct === null) return styles.priceNeutral
    return pct > 0 ? styles.priceUp : pct < 0 ? styles.priceDown : styles.priceNeutral
  }, [])

  const skeletonRows = useMemo(() => Array.from({ length: 8 }, (_, i) => i), [])

  // 空态判断
  const isEmpty = !isLoading && items.length === 0
  const isWatchlistEmpty = isEmpty && scope === 'watchlist' && !urlState.query

  if (isError) {
    return (
      <div className={styles.tableError}>
        <div className={styles.errorText}>数据加载失败</div>
        <button className={styles.retryBtn} onClick={onRetry} aria-label="重试">
          重试
        </button>
      </div>
    )
  }

  return (
    <div className={styles.tableContainer}>
      <div className={styles.tableScroll}>
        <table className={styles.stockTable}>
          <thead>
            <tr>
              <th className={styles.colName}>股票名称/代码</th>
              <th className={styles.colPrice}>
                最新价
                {isClosingSnapshot && (
                  <span
                    className={styles.closingSnapshotBadge}
                    title={priceAsOf ? `截至${priceAsOf.slice(0, 10)}收盘` : '收盘快照'}
                  >
                    收盘快照
                  </span>
                )}
              </th>
              <th className={styles.colChange}>涨跌幅</th>
              <th className={styles.colIndustry}>行业</th>
              <th className={styles.colConcept}>概念</th>
              <th className={styles.colState}>形态状态</th>
              <th className={styles.colDsa}>DSA状态</th>
              <th className={styles.colEvent}>最近事件</th>
              <th className={styles.colStar}>自选</th>
            </tr>
          </thead>
          <tbody>
            {isLoading &&
              skeletonRows.map((i) => (
                <tr key={`skeleton-${i}`} className={styles.skeletonRow}>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                  <td><div className={styles.skeletonCell} /></td>
                </tr>
              ))}
            {!isLoading &&
              items.map((row) => (
                <tr
                  key={row.instrument_id}
                  className={clsx(styles.dataRow, selected === row.symbol && styles.dataRowSelected)}
                  onClick={() => onSelectRow(row.symbol)}
                >
                  <td className={styles.colName}>
                    <div className={styles.nameCell}>
                      <button
                        className={styles.nameLink}
                        onClick={(e) => {
                          e.stopPropagation()
                          handleNavigateToDetail(row.symbol)
                        }}
                        aria-label={`查看${row.name}详情`}
                      >
                        {row.name}
                      </button>
                      <span className={styles.symbolText}>{row.symbol}</span>
                    </div>
                  </td>
                  <td className={styles.colPrice}>
                    {row.latest_price !== null ? row.latest_price.toFixed(2) : '—'}
                  </td>
                  <td className={clsx(styles.colChange, changePctColor(row.change_pct))}>
                    {row.change_pct !== null
                      ? `${row.change_pct > 0 ? '+' : ''}${row.change_pct.toFixed(2)}%`
                      : '—'}
                  </td>
                  <td className={styles.colIndustry}>
                    {row.industry ?? '—'}
                  </td>
                  <td
                    className={styles.colConcept}
                    title={row.concepts.length > 0 ? row.concepts.join(', ') : undefined}
                  >
                    {row.concepts.length > 0
                      ? row.concepts.slice(0, 2).join(', ') +
                        (row.concepts.length > 2 ? ` +${row.concepts.length - 2}` : '')
                      : '—'}
                  </td>
                  <td className={styles.colState}>
                    {row.structure_state ?? '—'}
                  </td>
                  <td className={styles.colDsa}>
                    {row.dsa_state ?? '—'}
                  </td>
                  <td className={styles.colEvent}>
                    {row.latest_event_title ? (
                      <div className={styles.eventCell}>
                        <div className={styles.eventTitle}>{row.latest_event_title}</div>
                        {row.latest_event_time && (
                          <div className={styles.eventTime}>{row.latest_event_time}</div>
                        )}
                      </div>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className={styles.colStar}>
                    <button
                      className={clsx(styles.starBtn, row.is_watchlisted && styles.starActive)}
                      onClick={(e) => {
                        e.stopPropagation()
                        handleToggleWatchlist(row)
                      }}
                      aria-label={row.is_watchlisted ? '取消自选' : '加入自选'}
                    >
                      {row.is_watchlisted ? '★' : '☆'}
                    </button>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      {isWatchlistEmpty && (
        <div className={styles.emptyState}>
          <div className={styles.emptyIcon}>◎</div>
          <div className={styles.emptyText}>自选为空，去行情中添加股票到自选</div>
        </div>
      )}
      {isEmpty && !isWatchlistEmpty && (
        <div className={styles.emptyState}>
          <div className={styles.emptyIcon}>◎</div>
          <div className={styles.emptyText}>无匹配结果</div>
        </div>
      )}

      {!isEmpty && total > 0 && (
        <div className={styles.pagination}>
          <span className={styles.pageInfo}>
            共 {total} 条，第 {page}/{totalPages} 页
          </span>
          <div className={styles.pageControls}>
            <button
              className={styles.pageBtn}
              disabled={page <= 1}
              onClick={() => onPageChange(page - 1)}
              aria-label="上一页"
            >
              ‹
            </button>
            <button
              className={styles.pageBtn}
              disabled={page >= totalPages}
              onClick={() => onPageChange(page + 1)}
              aria-label="下一页"
            >
              ›
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
