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
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

// ===== 类型定义 =====

type MonitorStatus = 'PRE_MARKET' | 'TRADING' | 'LUNCH_BREAK' | 'AFTER_MARKET' | 'NON_TRADING_DAY' | 'WAITING_FIRST_RUN' | 'SUCCEEDED' | 'FAILED' | 'STALE'

// 统一监控行（从 WatchlistMonitorStatusItem 派生）
interface WatchlistRow {
  instrumentId: string
  symbol: string
  name: string
  monitorStatus: MonitorStatus
  bbUpper: number | null
  bbMid: number | null
  bbLower: number | null
  currentPrice: number | null
  upperNodePrice: number | null
  upperNodeLow: number | null
  upperNodeHigh: number | null
  lowerNodePrice: number | null
  lowerNodeLow: number | null
  lowerNodeHigh: number | null
  position01: number | null
  pocPrice: number | null
  latestEvent: { event_type: string; event_time: string; boundary: number | null } | null
  updatedAt: string | null
  [key: string]: unknown
}

// ===== 工具函数 =====

/** 转换为数字，失败返回 null */
function toNum(v: unknown): number | null {
  if (v === undefined || v === null || v === '') return null
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  return Number.isNaN(n) ? null : n
}

/** 格式化为数值字符串（保留指定小数位），未知返回 '-' */
function fmtNum(v: unknown, digits = 2): string {
  const n = toNum(v)
  return n === null ? '-' : n.toFixed(digits)
}

/** 格式化更新时间，取时间部分（上海时区） */
function fmtTime(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  try {
    return new Date(String(v)).toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return '-'
  }
}

/** 监控状态徽章渲染 */
function MonitorStatusBadge({ status }: { status: MonitorStatus }) {
  switch (status) {
    case 'PRE_MARKET':
      return <span className="tag small">盘前等待开市</span>
    case 'TRADING':
      return <span className="tag info small">交易中</span>
    case 'LUNCH_BREAK':
      return <span className="tag small">午间休市</span>
    case 'AFTER_MARKET':
      return <span className="tag small">已收盘</span>
    case 'NON_TRADING_DAY':
      return <span className="tag small">非交易日</span>
    case 'WAITING_FIRST_RUN':
      return <span className="tag small">等待首次计算</span>
    case 'SUCCEEDED':
      return <span className="tag success small">已计算</span>
    case 'FAILED':
      return <span className="tag error small">计算失败</span>
    case 'STALE':
      return <span className="tag warn small">数据延迟</span>
    default:
      return <span className="tag small">未知</span>
  }
}

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
          <div className="notice modal-stack">
            加入后可查看统一监控状态。
          </div>
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

  const rows: WatchlistRow[] = useMemo(
    () =>
      items.map((item) => {
        const metrics = item.metrics as Record<string, unknown> | null
        return {
          instrumentId: item.instrument_id,
          symbol: item.symbol,
          name: item.name,
          monitorStatus: item.monitor_status,
          bbUpper: metrics ? toNum(metrics.bb_upper) : null,
          bbMid: metrics ? toNum(metrics.bb_mid ?? metrics.bb_middle) : null,
          bbLower: metrics ? toNum(metrics.bb_lower) : null,
          currentPrice: metrics ? toNum(metrics.current_price ?? metrics.close) : null,
          upperNodePrice: metrics ? toNum(metrics.upper_node_price) : null,
          upperNodeLow: metrics ? toNum(metrics.upper_node_low) : null,
          upperNodeHigh: metrics ? toNum(metrics.upper_node_high) : null,
          lowerNodePrice: metrics ? toNum(metrics.lower_node_price) : null,
          lowerNodeLow: metrics ? toNum(metrics.lower_node_low) : null,
          lowerNodeHigh: metrics ? toNum(metrics.lower_node_high) : null,
          position01: metrics ? toNum(metrics.position_0_1 ?? metrics.node_strength) : null,
          pocPrice: metrics ? toNum(metrics.poc_price) : null,
          latestEvent: item.latest_event ?? null,
          updatedAt: item.updated_at ? fmtTime(item.updated_at) : null,
        }
      }),
    [items],
  )

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (_instrumentId: string, symbol: string) => {
      navigate(`/stock/${symbol}?source=watchlist`)
    },
    [navigate],
  )

  /** 移出自选（带确认） */
  const handleRemove = useCallback(
    (instrumentId: string, symbol: string, name: string) => {
      const confirmed = window.confirm(`确定要将 ${symbol} ${name} 从自选中移除吗？`)
      if (!confirmed) return
      removeWatchlistMutation.mutate(instrumentId, {
        onSuccess: () => {
          toast.show('已移除', `${symbol} ${name} 已从自选中移除`)
        },
        onError: () => {
          toast.show('移除失败', '请稍后重试')
        },
      })
    },
    [removeWatchlistMutation, toast],
  )

  // ===== 列定义 =====

  const columns: DataTableColumn<WatchlistRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.name,
        filterValue: (row) => `${row.name} ${row.symbol}`,
        render: (row) => (
          <div>
            <div className="symbol">{row.name}</div>
            <div className="symbol-sub">{row.symbol}</div>
          </div>
        ),
      },
      {
        key: 'status',
        title: '状态',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.monitorStatus,
        filterValue: (row) => {
          switch (row.monitorStatus) {
            case 'PRE_MARKET': return '盘前等待开市'
            case 'TRADING': return '交易中'
            case 'LUNCH_BREAK': return '午间休市'
            case 'AFTER_MARKET': return '已收盘'
            case 'NON_TRADING_DAY': return '非交易日'
            case 'SUCCEEDED': return '已计算'
            case 'FAILED': return '计算失败'
            case 'STALE': return '数据延迟'
            case 'WAITING_FIRST_RUN': return '等待首次计算'
            default: return '未知'
          }
        },
        render: (row) => <MonitorStatusBadge status={row.monitorStatus} />,
      },
      {
        key: 'currentPrice',
        title: '当前价',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.currentPrice ?? 0,
        render: (row) => <span className="num">{fmtNum(row.currentPrice)}</span>,
      },
      {
        key: 'bbUpper',
        title: 'BB上轨',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.bbUpper ?? 0,
        render: (row) => <span className="num">{fmtNum(row.bbUpper)}</span>,
      },
      {
        key: 'bbMid',
        title: 'BB中轨',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.bbMid ?? 0,
        render: (row) => <span className="num">{fmtNum(row.bbMid)}</span>,
      },
      {
        key: 'bbLower',
        title: 'BB下轨',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.bbLower ?? 0,
        render: (row) => <span className="num">{fmtNum(row.bbLower)}</span>,
      },
      {
        key: 'upperNode',
        title: '上节点',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.upperNodePrice ?? 0,
        render: (row) => (
          <span className="num" title={row.upperNodeLow != null && row.upperNodeHigh != null ? `${row.upperNodeLow} ~ ${row.upperNodeHigh}` : undefined}>
            {fmtNum(row.upperNodePrice)}
          </span>
        ),
      },
      {
        key: 'lowerNode',
        title: '下节点',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.lowerNodePrice ?? 0,
        render: (row) => (
          <span className="num" title={row.lowerNodeLow != null && row.lowerNodeHigh != null ? `${row.lowerNodeLow} ~ ${row.lowerNodeHigh}` : undefined}>
            {fmtNum(row.lowerNodePrice)}
          </span>
        ),
      },
      {
        key: 'position01',
        title: '位置',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.position01 ?? 0,
        render: (row) => <span className="num">{fmtNum(row.position01)}</span>,
      },
      {
        key: 'pocPrice',
        title: 'POC',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.pocPrice ?? 0,
        render: (row) => <span className="num">{fmtNum(row.pocPrice)}</span>,
      },
      {
        key: 'latestEvent',
        title: '最近触发',
        dataType: 'text',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.latestEvent?.event_time ?? '',
        render: (row) => {
          if (!row.latestEvent) return <span className="muted">-</span>
          const eventType = row.latestEvent.event_type
          const time = fmtTime(row.latestEvent.event_time)
          const boundary = row.latestEvent.boundary
          return (
            <div>
              <div className="symbol">{eventType}</div>
              <div className="symbol-sub">
                {time}{boundary != null ? ` · ${boundary}` : ''}
              </div>
            </div>
          )
        },
      },
      {
        key: 'updatedAt',
        title: '更新时间',
        dataType: 'text',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.updatedAt ?? '',
        render: (row) => <span className="num">{row.updatedAt ?? '-'}</span>,
      },
      {
        key: 'action',
        title: '操作',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => (
          <div className="actions">
            <button className="btn small" onClick={() => goDetail(row.instrumentId, row.symbol)}>
              详情
            </button>
            <button
              className="btn small danger"
              onClick={() => handleRemove(row.instrumentId, row.symbol, row.name)}
              disabled={removeWatchlistMutation.isPending}
            >
              移出自选
            </button>
          </div>
        ),
      },
    ],
    [goDetail, handleRemove, removeWatchlistMutation.isPending],
  )

  // ===== 渲染 =====

  return (
    <div>
      {/* 页面头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">自选股监控</h1>
          <div className="page-desc">
            自选股票池统一监控，合并 BB 布林带与 Volume Node 指标
          </div>
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

      {/* 统一监控表格 */}
      <div className="card">
        <StrategyDataTable
          tableId="watchlist-monitor"
          columns={columns}
          rows={rows}
          rowKey={(row) => row.instrumentId}
          loading={monitorStatusQuery.isLoading}
          error={null}
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
