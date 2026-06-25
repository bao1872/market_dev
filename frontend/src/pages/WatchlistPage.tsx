// 自选股监控页
// 用法：展示用户自选股票池的统一监控状态（聚合端点 /watchlist/monitor-status）
// 路由：/watchlist
// 依赖 hooks：useWatchlistMonitorStatus / useInstruments / useAddToWatchlist / useRemoveFromWatchlist
import { useState, useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useToast } from '@/store/toast'
import {
  useWatchlistMonitorStatus,
  useInstruments,
  useAddToWatchlist,
  useRemoveFromWatchlist,
} from '@/hooks/useApi'
import type { WatchlistMonitorStatusItem } from '@/api/endpoints'
import {
  WatchlistMonitorTable,
  WatchlistMonitorCards,
  adaptWatchlistMonitorStatusItem,
} from '@/features/watchlist-monitor'
import type { WatchlistMonitorRow } from '@/features/watchlist-monitor'

// ===== 添加自选弹窗 =====
function AddStockModal({
  watchlistIds,
  onClose,
}: {
  watchlistIds: Set<string>
  onClose: () => void
}) {
  const [keyword, setKeyword] = useState('')
  const toast = useToast.getState()
  const addMutation = useAddToWatchlist()

  const instrumentsQuery = useInstruments({
    keyword: keyword.trim() || undefined,
    page_size: 20,
  })
  const instruments = instrumentsQuery.data?.items ?? []

  const handleAdd = useCallback(
    async (instrumentId: string, name: string) => {
      try {
        await addMutation.mutateAsync({
          instrument_id: instrumentId,
          source: 'manual',
        })
        toast.show('已加入自选', `${name} 已加入自选`)
        onClose()
      } catch {
        toast.show('加入失败', '请稍后重试')
      }
    },
    [addMutation, toast, onClose],
  )

  return (
    <div className="modal-backdrop open" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <b>搜索并添加自选</b>
          <button className="icon-btn" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="modal-body">
          <div className="field search">
            <input
              className="input search modal-full-search"
              placeholder="代码 / 名称 / 拼音"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              autoFocus
            />
          </div>
          <div className="notice modal-stack">加入后可查看统一监控状态。</div>
          <div className="list modal-stack">
            {instrumentsQuery.isLoading && <div className="notice">加载中…</div>}
            {!instrumentsQuery.isLoading && instruments.length === 0 && (
              <div className="notice">未找到匹配的股票</div>
            )}
            {instruments.map((inst) => {
              const isWatched = watchlistIds.has(inst.id)
              return (
                <div className="list-item" key={inst.id}>
                  <div className="list-main">
                    <div className="list-title">
                      {inst.name} <span className="muted">{inst.symbol}</span>
                    </div>
                    <div className="list-meta">{inst.market}</div>
                  </div>
                  {isWatched ? (
                    <span className="tag info">已自选</span>
                  ) : (
                    <button
                      className="btn small primary"
                      onClick={() => handleAdd(inst.id, inst.name)}
                      disabled={addMutation.isPending}
                    >
                      {addMutation.isPending ? '添加中…' : '添加'}
                    </button>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

// ===== 主组件 =====
export default function WatchlistPage() {
  const navigate = useNavigate()
  const toast = useToast.getState()

  // --- 唯一数据源：聚合端点 ---
  const monitorStatusQuery = useWatchlistMonitorStatus()
  const items: WatchlistMonitorStatusItem[] = monitorStatusQuery.data?.items ?? []

  // --- 移出自选 ---
  const removeWatchlistMutation = useRemoveFromWatchlist()

  // --- UI 状态 ---
  const [searchModalOpen, setSearchModalOpen] = useState(false)

  // ===== 派生数据 =====

  const watchlistIds = useMemo(
    () => new Set(items.map((item) => item.instrument_id)),
    [items],
  )

  const rows: WatchlistMonitorRow[] = useMemo(
    () => items.map(adaptWatchlistMonitorStatusItem),
    [items],
  )

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (row: WatchlistMonitorRow) => {
      navigate(`/stock/${row.symbol}?source=watchlist`)
    },
    [navigate],
  )

  /** 移出自选（带确认） */
  const handleRemove = useCallback(
    (row: WatchlistMonitorRow) => {
      const confirmed = window.confirm(
        `确定要将 ${row.symbol} ${row.name} 从自选中移除吗？`,
      )
      if (!confirmed) return
      removeWatchlistMutation.mutate(row.instrument_id, {
        onSuccess: () => {
          toast.show('已移除', `${row.symbol} ${row.name} 已从自选中移除`)
        },
        onError: () => {
          toast.show('移除失败', '请稍后重试')
        },
      })
    },
    [removeWatchlistMutation, toast],
  )

  // ===== 渲染 =====

  return (
    <div>
      {/* 页面头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">自选股监控</h1>
          <div className="page-desc">自选股票池统一监控，合并 BB 布林带与 Volume Node 指标</div>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={() => setSearchModalOpen(true)}>
            ＋ 添加股票
          </button>
        </div>
      </div>

      {/* 错误提示 */}
      {monitorStatusQuery.isError && (
        <div className="notice error" style={{ marginBottom: '1rem' }}>
          数据加载失败，请刷新重试
        </div>
      )}

      {/* 桌面端表格 */}
      <div className="card watchlist-monitor-table-wrap">
        <WatchlistMonitorTable
          tableId="watchlist-monitor"
          rows={rows}
          loading={monitorStatusQuery.isLoading}
          error={null}
          emptyText={items.length === 0 ? '暂无自选股票，请点击右上角添加' : undefined}
          onDetail={goDetail}
          onRemove={handleRemove}
          removePending={removeWatchlistMutation.isPending}
        />
      </div>

      {/* 移动端卡片 */}
      <div className="watchlist-monitor-cards-wrap">
        <WatchlistMonitorCards
          rows={rows}
          onDetail={goDetail}
          onRemove={handleRemove}
          removePending={removeWatchlistMutation.isPending}
          emptyText={items.length === 0 ? '暂无自选股票，请点击右上角添加' : undefined}
        />
      </div>

      {/* 弹窗：搜索添加自选 */}
      {searchModalOpen && (
        <AddStockModal
          watchlistIds={watchlistIds}
          onClose={() => setSearchModalOpen(false)}
        />
      )}
    </div>
  )
}
