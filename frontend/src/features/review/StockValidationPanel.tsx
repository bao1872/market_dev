// [StockValidationPanel] - 描述: 阶段四个股验证（PRD §14.6）
// 精简个股表 + 跳转：打开 /stock/:symbol；加入/移除自选；加入本信号追踪；"查看全部"跳转 /market
// 复盘页不重新实现 99 字段列设置和导出（PRD §16）
// 自选操作复用现有 useWatchlist/useAddToWatchlist/useRemoveFromWatchlist
import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  useWatchlist,
  useAddToWatchlist,
  useRemoveFromWatchlist,
} from '@/hooks/useApi'
import { getSignalInstruments, createReviewTracking, extractReviewError } from './api'
import { reviewKeys } from './queryKeys'
import ReviewInstrumentTable from './ReviewInstrumentTable'
import type { ReviewInstrument, ReviewInstrumentListParams } from './types'
import styles from './review.module.scss'

const PAGE_SIZE = 20

const BOARD_ROLE_OPTIONS = [
  { value: '', label: '全部角色' },
  { value: 'core', label: '龙头' },
  { value: 'second_line', label: '二线' },
  { value: 'elasticity', label: '弹性' },
  { value: 'follower', label: '跟随' },
  { value: 'laggard', label: '滞涨' },
  { value: 'unclassified', label: '未分类' },
]

export interface StockValidationPanelProps {
  signalId: string | null
  tradeDate: string
  /** source core run id（跳转 /market 透传） */
  sourceCoreRunId?: string | null
  /** board id（跳转 /market 透传） */
  boardId?: string | null
  /** 选中股票（高亮行） */
  activeSymbol?: string | null
  /** 打开证据抽屉 */
  onOpenInstrumentEvidence?: (inst: ReviewInstrument) => void
  /** toast 提示（由页面提供，避免直接耦合 toast store） */
  showToast?: (title: string, desc: string) => void
}

export default function StockValidationPanel({
  signalId,
  tradeDate,
  sourceCoreRunId,
  boardId,
  activeSymbol,
  onOpenInstrumentEvidence,
  showToast,
}: StockValidationPanelProps) {
  const queryClient = useQueryClient()
  const [boardRole, setBoardRole] = useState('')
  const [page, setPage] = useState(1)

  const params: ReviewInstrumentListParams = {
    board_role: boardRole || undefined,
    page,
    page_size: PAGE_SIZE,
  }

  const query = useQuery({
    queryKey: reviewKeys.instruments(signalId ?? '', params),
    queryFn: () => getSignalInstruments(signalId!, params),
    enabled: !!signalId,
    staleTime: 60 * 1000,
  })

  // 自选列表（复用现有 hook）
  const watchlistQuery = useWatchlist()
  const watchlistInstrumentIds = useMemo(() => {
    const set = new Set<string>()
    for (const item of watchlistQuery.data?.items ?? []) {
      if (item.instrument_id) set.add(item.instrument_id)
    }
    return set
  }, [watchlistQuery.data?.items])

  const addMutation = useAddToWatchlist()
  const removeMutation = useRemoveFromWatchlist()
  const [pendingIds, setPendingIds] = useState<Set<string>>(() => new Set())

  const createTrackingMutation = useMutation({
    mutationFn: (inst: ReviewInstrument) =>
      createReviewTracking({
        tracking_type: 'instrument',
        source_signal_id: signalId ?? undefined,
        instrument_id: inst.instrumentId,
        idempotency_key: `review-inst-${inst.instrumentId}-${Date.now()}`,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: reviewKeys.trackings() })
    },
  })

  const handleToggleWatchlist = (inst: ReviewInstrument, add: boolean) => {
    if (pendingIds.has(inst.instrumentId)) return
    setPendingIds((prev) => {
      const next = new Set(prev)
      next.add(inst.instrumentId)
      return next
    })
    const onSettled = () => {
      setPendingIds((prev) => {
        const next = new Set(prev)
        next.delete(inst.instrumentId)
        return next
      })
    }
    if (add) {
      addMutation.mutate(
        { instrument_id: inst.instrumentId },
        {
          onSettled,
          onSuccess: () => showToast?.('已加入自选', inst.symbol),
          onError: () => showToast?.('加入自选失败', '请稍后重试'),
        },
      )
    } else {
      removeMutation.mutate(inst.instrumentId, {
        onSettled,
        onSuccess: () => showToast?.('已移除自选', inst.symbol),
        onError: () => showToast?.('移除自选失败', '请稍后重试'),
      })
    }
  }

  const handleAddTracking = (inst: ReviewInstrument) => {
    createTrackingMutation.mutate(inst, {
      onSuccess: () => showToast?.('已加入追踪', inst.symbol),
      onError: (err) => {
        const e = extractReviewError(err)
        showToast?.('加入追踪失败', e.message)
      },
    })
  }

  const handleNavigateToStock = (inst: ReviewInstrument) => {
    // PRD §16：Review 只传 from=review / signalId / boardId / tradeDate
    const sp = new URLSearchParams()
    sp.set('from', 'review')
    if (signalId) sp.set('signalId', signalId)
    if (boardId) sp.set('boardId', boardId)
    if (tradeDate) sp.set('tradeDate', tradeDate)
    window.open(`/stock/${inst.symbol}?${sp.toString()}`, '_blank')
  }

  const handleViewAllInMarket = () => {
    // PRD §16：reviewSignalId / tradeDate / sourceCoreRunId / boardId
    const sp = new URLSearchParams()
    if (signalId) sp.set('reviewSignalId', signalId)
    if (tradeDate) sp.set('tradeDate', tradeDate)
    if (sourceCoreRunId) sp.set('sourceCoreRunId', sourceCoreRunId)
    if (boardId) sp.set('boardId', boardId)
    window.open(`/market?${sp.toString()}`, '_blank')
  }

  if (!signalId) {
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>未选择信号</div>
        <div className={styles.stateDesc}>
          请在「筛选发现」阶段选择一条信号，查看其代表股票与第一金字塔验证
        </div>
      </div>
    )
  }

  if (query.isError) {
    const err = extractReviewError(query.error)
    return (
      <div className={styles.stateBox}>
        <div className={styles.stateTitle}>代表股票加载失败</div>
        <div className={styles.stateDesc}>{err.message}</div>
        {err.requestId && <div className={styles.stateRequestId}>request_id={err.requestId}</div>}
      </div>
    )
  }

  return (
    <div className={styles.panel}>
      <div className={styles.toolbar}>
        <select
          className={styles.select}
          value={boardRole}
          onChange={(e) => {
            setBoardRole(e.target.value)
            setPage(1)
          }}
        >
          {BOARD_ROLE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <span className={styles.pagination}>共 {query.data?.total ?? 0} 只代表股票</span>
      </div>
      {query.isLoading ? (
        <div className={styles.stateBox}>
          <div className={styles.stateDesc}>加载代表股票...</div>
        </div>
      ) : (
        <ReviewInstrumentTable
          items={query.data?.items ?? []}
          activeSymbol={activeSymbol}
          watchlistInstrumentIds={watchlistInstrumentIds}
          watchlistPendingIds={pendingIds}
          onNavigateToStock={(inst) => {
            // 单击股票名：既跳转个股页，也可触发证据预览
            onOpenInstrumentEvidence?.(inst)
            handleNavigateToStock(inst)
          }}
          onToggleWatchlist={handleToggleWatchlist}
          onAddTracking={handleAddTracking}
          onViewAllInMarket={handleViewAllInMarket}
        />
      )}
      {(query.data?.total ?? 0) > PAGE_SIZE && (
        <div className={styles.pagination}>
          <button
            type="button"
            className={styles.btn}
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            上一页
          </button>
          <span>第 {page} 页</span>
          <button
            type="button"
            className={styles.btn}
            disabled={!(query.data?.has_more ?? false)}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
