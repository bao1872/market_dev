// 自选股监控页
// 用法：展示用户自选股票池的统一监控状态（BB + VN 合并指标）
// 路由：/watchlist
// 依赖 hooks：useWatchlist / useStrategyMonitorStates / useInstruments / useAddToWatchlist
import { useState, useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueries } from '@tanstack/react-query'
import { useToast } from '@/store/toast'
import {
  useWatchlist,
  useStrategyMonitorStates,
  useInstruments,
  useAddToWatchlist,
} from '@/hooks/useApi'
import * as api from '@/api/endpoints'
import type { Instrument, MonitorState } from '@/api/endpoints'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

// ===== 类型定义 =====

// 统一监控行（从 watchlist_monitor 的 MonitorState.payload 派生）
interface WatchlistRow {
  instrumentId: string
  symbol: string
  name: string
  bbUpper: string
  bbMiddle: string
  bbLower: string
  nodePrice: string
  nodeStrength: string
  lastEvent: string
  updatedAt: string
  [key: string]: unknown
}

// ===== 工具函数 =====

/** 从 payload 中按候选 key 列表取第一个非空值 */
function pickPayload(payload: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) {
    const v = payload[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}

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

/** 格式化为字符串，未知返回 '-' */
function fmtStr(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  return String(v)
}

/** 格式化更新时间，取时间部分 */
function fmtTime(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  const s = String(v)
  // 尝试提取 HH:MM:SS 或 MM-DD HH:MM
  const timeMatch = s.match(/(\d{2}:\d{2}:\d{2})/)
  if (timeMatch) return timeMatch[1]
  return s.slice(-8)
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
  const instruments: Instrument[] = instrumentsQuery.data?.items ?? []

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

  // --- 自选列表 ---
  const watchlistQuery = useWatchlist()
  const watchlistItems = watchlistQuery.data?.items ?? []
  const watchlistIds = useMemo(
    () => new Set(watchlistItems.map((w) => w.instrument_id)),
    [watchlistItems],
  )

  // --- 统一监控状态（watchlist_monitor，全量后按自选过滤） ---
  const monitorStatesQuery = useStrategyMonitorStates('watchlist_monitor')
  const monitorStates: MonitorState[] = monitorStatesQuery.data?.items ?? []

  // --- 批量查询自选股的 Instrument 信息（名称/代码/市场） ---
  const watchlistIdList = useMemo(() => Array.from(watchlistIds), [watchlistIds])
  const instrumentQueries = useQueries({
    queries: watchlistIdList.map((id) => ({
      queryKey: ['instruments', id],
      queryFn: () => api.getInstrumentById(id),
      staleTime: 5 * 60 * 1000,
    })),
  })
  const instrumentMap = useMemo(() => {
    const m = new Map<string, Instrument>()
    instrumentQueries.forEach((q, i) => {
      if (q.data) {
        m.set(watchlistIdList[i], q.data)
      }
    })
    return m
  }, [instrumentQueries, watchlistIdList])

  // --- UI 状态 ---
  const [searchModalOpen, setSearchModalOpen] = useState(false)

  // ===== 派生数据 =====

  const monitorStateMap = useMemo(() => {
    const m = new Map<string, MonitorState>()
    for (const s of monitorStates) {
      if (watchlistIds.has(s.instrument_id)) {
        m.set(s.instrument_id, s)
      }
    }
    return m
  }, [monitorStates, watchlistIds])

  // ===== 行转换 =====

  /** 提取股票展示信息 */
  const getStockDisplay = useCallback(
    (instrumentId: string): { symbol: string; name: string } => {
      const inst = instrumentMap.get(instrumentId)
      return {
        symbol: inst?.symbol ?? instrumentId.slice(0, 8),
        name: inst?.name ?? '-',
      }
    },
    [instrumentMap],
  )

  /** 将 MonitorState 转换为 WatchlistRow */
  const toRow = useCallback(
    (s: MonitorState): WatchlistRow => {
      const { symbol, name } = getStockDisplay(s.instrument_id)
      return {
        instrumentId: s.instrument_id,
        symbol,
        name,
        bbUpper: fmtNum(pickPayload(s.payload, ['bb_upper', 'bollinger_upper', 'upper_band'])),
        bbMiddle: fmtNum(pickPayload(s.payload, ['bb_middle', 'bollinger_middle', 'middle_band', 'ma20'])),
        bbLower: fmtNum(pickPayload(s.payload, ['bb_lower', 'bollinger_lower', 'lower_band'])),
        nodePrice: fmtNum(pickPayload(s.payload, ['node_price', 'vn_price', 'volume_node_price'])),
        nodeStrength: fmtNum(pickPayload(s.payload, ['node_strength', 'vn_strength', 'volume_node_strength'])),
        lastEvent: fmtStr(pickPayload(s.payload, ['last_event', 'event_description', 'latest_event'])),
        updatedAt: fmtTime(s.updated_at),
      }
    },
    [getStockDisplay],
  )

  // ===== 表格行数据 =====

  const rows: WatchlistRow[] = useMemo(
    () =>
      watchlistIdList
        .map((id) => monitorStateMap.get(id))
        .filter((s): s is MonitorState => !!s)
        .map(toRow),
    [watchlistIdList, monitorStateMap, toRow],
  )

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (instrumentId: string) => {
      const { symbol } = getStockDisplay(instrumentId)
      navigate(`/stock/${symbol}?source=watchlist`)
    },
    [navigate, getStockDisplay],
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
        key: 'bbUpper',
        title: 'BB上轨',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => Number(row.bbUpper === '-' ? 0 : row.bbUpper),
        render: (row) => <span className="num">{row.bbUpper}</span>,
      },
      {
        key: 'bbMiddle',
        title: 'BB中轨',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => Number(row.bbMiddle === '-' ? 0 : row.bbMiddle),
        render: (row) => <span className="num">{row.bbMiddle}</span>,
      },
      {
        key: 'bbLower',
        title: 'BB下轨',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => Number(row.bbLower === '-' ? 0 : row.bbLower),
        render: (row) => <span className="num">{row.bbLower}</span>,
      },
      {
        key: 'nodePrice',
        title: 'Node价格',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => Number(row.nodePrice === '-' ? 0 : row.nodePrice),
        render: (row) => <span className="num">{row.nodePrice}</span>,
      },
      {
        key: 'nodeStrength',
        title: 'Node强度',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => Number(row.nodeStrength === '-' ? 0 : row.nodeStrength),
        render: (row) => <span className="num">{row.nodeStrength}</span>,
      },
      {
        key: 'lastEvent',
        title: '最近事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.lastEvent,
        render: (row) => row.lastEvent,
      },
      {
        key: 'updatedAt',
        title: '更新时间',
        dataType: 'text',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.updatedAt,
        render: (row) => <span className="num">{row.updatedAt}</span>,
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
            <button className="btn small" onClick={() => goDetail(row.instrumentId)}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [goDetail],
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

      {/* 统一监控表格 */}
      <div className="card">
        <StrategyDataTable
          tableId="watchlist-monitor"
          columns={columns}
          rows={rows}
          rowKey={(row) => row.instrumentId}
          loading={monitorStatesQuery.isLoading}
          error={monitorStatesQuery.isError ? '监控状态加载失败' : null}
          emptyText="暂无监控状态，请先添加自选股票"
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
