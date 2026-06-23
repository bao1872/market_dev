// 我的自选页（受保护路由）
// 对应原型：watchlist.html (V1.6.3)
// 用法：展示用户自选股票池，支持单策略明细展开、搜索添加自选
// 路由：/watchlist
// 依赖 hooks：useWatchlist / useStrategyMonitorStates / useInstruments / useAddToWatchlist
import { useState, useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import { useQueries } from '@tanstack/react-query'
import { useToast } from '@/store/toast'
import {
  useWatchlist,
  useStrategyMonitorStates,
  useInstruments,
  useAddToWatchlist,
} from '@/hooks/useApi'
import * as api from '@/api/endpoints'
import type {
  Instrument,
  MonitorState,
} from '@/api/endpoints'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

// ===== 类型定义 =====

// Node 监控行（从 MonitorState.payload 派生）
interface NodeRow {
  instrumentId: string
  symbol: string
  name: string
  price: string
  lowerNode: string
  position: number
  upperNode: string
  upperTag: string
  lastTouch: string
  lastEvent: string
  [key: string]: unknown
}

// ATR 监控行（从 MonitorState.payload 派生）
interface AtrRow {
  instrumentId: string
  symbol: string
  name: string
  price: string
  direction: string
  directionTag: 'good' | 'warn'
  ropePos: string
  deviation: string
  bandWidth: string
  lastEvent: string
  [key: string]: unknown
}

// Volume 监控行（从 MonitorState.payload 派生）
interface VolumeRow {
  instrumentId: string
  symbol: string
  name: string
  price: string
  deltaDir: string
  deltaTag: 'good' | 'bad'
  zScore: string
  zScorePos: boolean
  buyRatio: string
  consecBars: string
  lastEvent: string
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

/** 格式化为百分比字符串（不带正负号），未知返回 '-' */
function fmtPct(v: unknown, digits = 2): string {
  const n = toNum(v)
  return n === null ? '-' : `${n.toFixed(digits)}%`
}

/** 格式化为字符串，未知返回 '-' */
function fmtStr(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  return String(v)
}

// ===== 添加自选弹窗（仅在打开时挂载，避免未打开时触发 useInstruments 查询）=====
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

  // 关键词搜索股票（去抖由 React Query staleTime 提供）
  const instrumentsQuery = useInstruments({
    keyword: keyword.trim() || undefined,
    page_size: 20,
  })
  const instruments: Instrument[] = instrumentsQuery.data?.items ?? []

  // 加入自选：调用 mutation 并提示
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
            加入后可查看各策略监控状态。
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

// ===== 管理分组弹窗（UI 占位，后端分组 API 尚未提供）=====
function GroupModal({ onClose }: { onClose: () => void }) {
  const [groupName, setGroupName] = useState('')
  const toast = useToast.getState()

  // 提交新分组：当前后端无分组 API，仅提示
  const handleAddGroup = () => {
    const name = groupName.trim()
    if (!name) {
      toast.show('请输入分组名称', '分组名称不能为空')
      return
    }
    toast.show('分组功能开发中', `已记录分组「${name}」，后端能力开放后将自动同步`)
    setGroupName('')
  }

  return (
    <div className="modal-backdrop open" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <b>管理自选分组</b>
          <button className="icon-btn" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="modal-body">
          <div className="form-row">
            <label className="form-label">新分组名称</label>
            <div className="form-inline-row">
              <input
                className="input"
                placeholder="例如：光模块"
                value={groupName}
                onChange={(e) => setGroupName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAddGroup()
                }}
              />
              <button className="btn" onClick={handleAddGroup}>
                添加
              </button>
            </div>
          </div>
          <div className="notice modal-stack">
            分组用于在自选列表中按主题归类股票，后续将支持按分组筛选与批量监控配置。
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

  // --- 单策略监控状态（node/atr/volume，全量后按自选过滤） ---
  const nodeStatesQuery = useStrategyMonitorStates('node')
  const atrStatesQuery = useStrategyMonitorStates('atr')
  const volumeStatesQuery = useStrategyMonitorStates('volume')
  const nodeStates: MonitorState[] = nodeStatesQuery.data?.items ?? []
  const atrStates: MonitorState[] = atrStatesQuery.data?.items ?? []
  const volumeStates: MonitorState[] = volumeStatesQuery.data?.items ?? []

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
  const [activeTab, setActiveTab] = useState<string>('watchNode')
  const [searchModalOpen, setSearchModalOpen] = useState(false)
  const [groupModalOpen, setGroupModalOpen] = useState(false)

  // ===== 派生数据 =====

  const nodeStateMap = useMemo(() => {
    const m = new Map<string, MonitorState>()
    for (const s of nodeStates) {
      if (watchlistIds.has(s.instrument_id)) {
        m.set(s.instrument_id, s)
      }
    }
    return m
  }, [nodeStates, watchlistIds])

  const atrStateMap = useMemo(() => {
    const m = new Map<string, MonitorState>()
    for (const s of atrStates) {
      if (watchlistIds.has(s.instrument_id)) {
        m.set(s.instrument_id, s)
      }
    }
    return m
  }, [atrStates, watchlistIds])

  const volumeStateMap = useMemo(() => {
    const m = new Map<string, MonitorState>()
    for (const s of volumeStates) {
      if (watchlistIds.has(s.instrument_id)) {
        m.set(s.instrument_id, s)
      }
    }
    return m
  }, [volumeStates, watchlistIds])

  // ===== 行转换函数 =====

  /** 提取股票展示信息 */
  const getStockDisplay = useCallback(
    (instrumentId: string): { symbol: string; name: string; market: string } => {
      const inst = instrumentMap.get(instrumentId)
      return {
        symbol: inst?.symbol ?? instrumentId.slice(0, 8),
        name: inst?.name ?? '-',
        market: inst?.market ?? '',
      }
    },
    [instrumentMap],
  )

  /** 将 MonitorState 转换为 NodeRow */
  const toNodeRow = useCallback(
    (s: MonitorState): NodeRow => {
      const { symbol, name } = getStockDisplay(s.instrument_id)
      const position =
        toNum(pickPayload(s.payload, ['position', 'node_position', 'position_between_nodes'])) ?? 0
      const pocPosition = toNum(pickPayload(s.payload, ['poc_position', 'poc_pos']))
      const upperNode = fmtStr(
        pickPayload(s.payload, ['upper_node', 'node_upper', 'resistance_node']),
      )
      return {
        instrumentId: s.instrument_id,
        symbol,
        name,
        price: fmtNum(pickPayload(s.payload, ['price', 'last_price', 'close'])),
        lowerNode: fmtStr(
          pickPayload(s.payload, ['lower_node', 'node_lower', 'support_node']),
        ),
        position,
        upperNode,
        upperTag: pocPosition !== null && pocPosition > 0.8 ? 'warn' : '',
        lastTouch: fmtStr(
          pickPayload(s.payload, ['last_touch', 'last_touched_node', 'recent_touch']),
        ),
        lastEvent: fmtStr(
          pickPayload(s.payload, ['last_event', 'event_description', 'latest_event']),
        ),
      }
    },
    [getStockDisplay],
  )

  /** 将 MonitorState 转换为 AtrRow */
  const toAtrRow = useCallback(
    (s: MonitorState): AtrRow => {
      const { symbol, name } = getStockDisplay(s.instrument_id)
      const direction = fmtStr(
        pickPayload(s.payload, ['direction', 'trend_direction', 'rope_direction']),
      )
      return {
        instrumentId: s.instrument_id,
        symbol,
        name,
        price: fmtNum(pickPayload(s.payload, ['price', 'last_price', 'close'])),
        direction,
        directionTag: direction === '向上' ? 'good' : 'warn',
        ropePos: fmtNum(
          pickPayload(s.payload, ['rope_pos', 'rope_position', 'band_position']),
        ),
        deviation: fmtPct(
          pickPayload(s.payload, ['deviation', 'deviation_pct', 'rope_deviation']),
        ),
        bandWidth: fmtPct(
          pickPayload(s.payload, ['band_width', 'rope_width', 'atr_band_width']),
        ),
        lastEvent: fmtStr(
          pickPayload(s.payload, ['last_event', 'event_description', 'latest_event']),
        ),
      }
    },
    [getStockDisplay],
  )

  /** 将 MonitorState 转换为 VolumeRow */
  const toVolumeRow = useCallback(
    (s: MonitorState): VolumeRow => {
      const { symbol, name } = getStockDisplay(s.instrument_id)
      const deltaDir = fmtStr(
        pickPayload(s.payload, ['delta_dir', 'delta_direction', 'flow_direction']),
      )
      const zScore = toNum(pickPayload(s.payload, ['z_score', 'volume_zscore', 'zscore']))
      return {
        instrumentId: s.instrument_id,
        symbol,
        name,
        price: fmtNum(pickPayload(s.payload, ['price', 'last_price', 'close'])),
        deltaDir,
        deltaTag: deltaDir.includes('流入') ? 'good' : 'bad',
        zScore: zScore !== null ? zScore.toFixed(2) : '-',
        zScorePos: zScore !== null && zScore > 0,
        buyRatio: fmtPct(
          pickPayload(s.payload, ['buy_ratio', 'buy_percentage', 'active_buy_ratio']),
        ),
        consecBars: fmtStr(
          pickPayload(s.payload, ['consec_bars', 'consecutive_bars', 'consecutive_volume_bars']),
        ),
        lastEvent: fmtStr(
          pickPayload(s.payload, ['last_event', 'event_description', 'latest_event']),
        ),
      }
    },
    [getStockDisplay],
  )

  // ===== 表格行数据 =====

  // Node 行：自选股中有 Node 状态的
  const nodeRows: NodeRow[] = useMemo(
    () =>
      watchlistIdList
        .map((id) => nodeStateMap.get(id))
        .filter((s): s is MonitorState => !!s)
        .map(toNodeRow),
    [watchlistIdList, nodeStateMap, toNodeRow],
  )

  // ATR 行
  const atrRows: AtrRow[] = useMemo(
    () =>
      watchlistIdList
        .map((id) => atrStateMap.get(id))
        .filter((s): s is MonitorState => !!s)
        .map(toAtrRow),
    [watchlistIdList, atrStateMap, toAtrRow],
  )

  // Volume 行
  const volumeRows: VolumeRow[] = useMemo(
    () =>
      watchlistIdList
        .map((id) => volumeStateMap.get(id))
        .filter((s): s is MonitorState => !!s)
        .map(toVolumeRow),
    [watchlistIdList, volumeStateMap, toVolumeRow],
  )

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (instrumentId: string, strategy: string) => {
      const { symbol } = getStockDisplay(instrumentId)
      navigate(`/stock/${symbol}?source=watchlist&strategy=${strategy}`)
    },
    [navigate, getStockDisplay],
  )

  // ===== 列定义 =====

  // 股票列渲染（复用）
  const renderStock = useCallback(
    (row: NodeRow | AtrRow | VolumeRow) => {
      return (
        <div>
          <div className="symbol">{row.name}</div>
          <div className="symbol-sub">{row.symbol}</div>
        </div>
      )
    },
    [],
  )

  // Node 明细列
  const nodeColumns: DataTableColumn<NodeRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.name,
        filterValue: (row) => `${row.name} ${row.symbol}`,
        render: renderStock,
      },
      {
        key: 'price',
        title: '当前价格',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.price === '-' ? 0 : row.price),
        render: (row) => <span className="num">{row.price}</span>,
      },
      {
        key: 'lowerNode',
        title: '下方最近节点',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.lowerNode,
        render: (row) => <span className="num">{row.lowerNode}</span>,
      },
      {
        key: 'position',
        title: '节点间位置 0–1',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.position,
        render: (row) => <span className="num">{row.position.toFixed(2)}</span>,
      },
      {
        key: 'upperNode',
        title: '上方最近节点',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.upperNode,
        render: (row) => (
          <span className={clsx('num', row.upperTag && `tag ${row.upperTag}`)}>
            {row.upperNode}
          </span>
        ),
      },
      {
        key: 'lastTouch',
        title: '最近碰触节点',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.lastTouch,
        render: (row) => <span className="num">{row.lastTouch}</span>,
      },
      {
        key: 'lastEvent',
        title: '最后事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.lastEvent,
        render: (row) => row.lastEvent,
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
            <button className="btn small" onClick={() => goDetail(row.instrumentId, 'node')}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [renderStock, goDetail],
  )

  // ATR 明细列
  const atrColumns: DataTableColumn<AtrRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.name,
        filterValue: (row) => `${row.name} ${row.symbol}`,
        render: renderStock,
      },
      {
        key: 'price',
        title: '当前价格',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.price === '-' ? 0 : row.price),
        render: (row) => <span className="num">{row.price}</span>,
      },
      {
        key: 'direction',
        title: '趋势方向',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.direction,
        render: (row) => <span className={clsx('tag', row.directionTag)}>{row.direction}</span>,
      },
      {
        key: 'ropePos',
        title: '蓝带位置 0–1',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.ropePos === '-' ? 0 : row.ropePos),
        render: (row) => <span className="num">{row.ropePos}</span>,
      },
      {
        key: 'deviation',
        title: '偏离度',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.deviation === '-' ? 0 : row.deviation.replace('%', '')),
        render: (row) => <span className="num">{row.deviation}</span>,
      },
      {
        key: 'bandWidth',
        title: '蓝带宽度收益率',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.bandWidth === '-' ? 0 : row.bandWidth.replace('%', '')),
        render: (row) => <span className="num">{row.bandWidth}</span>,
      },
      {
        key: 'lastEvent',
        title: '最后事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.lastEvent,
        render: (row) => row.lastEvent,
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
            <button className="btn small" onClick={() => goDetail(row.instrumentId, 'atr')}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [renderStock, goDetail],
  )

  // Volume 明细列
  const volumeColumns: DataTableColumn<VolumeRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.name,
        filterValue: (row) => `${row.name} ${row.symbol}`,
        render: renderStock,
      },
      {
        key: 'price',
        title: '当前价格',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.price === '-' ? 0 : row.price),
        render: (row) => <span className="num">{row.price}</span>,
      },
      {
        key: 'deltaDir',
        title: 'Delta 方向',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.deltaDir,
        render: (row) => <span className={clsx('tag', row.deltaTag)}>{row.deltaDir}</span>,
      },
      {
        key: 'zScore',
        title: '成交量 Z-score',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.zScore === '-' ? 0 : row.zScore),
        render: (row) => (
          <span className={clsx('num', row.zScorePos && 'pos')}>{row.zScore}</span>
        ),
      },
      {
        key: 'buyRatio',
        title: '主动买入占比',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.buyRatio === '-' ? 0 : row.buyRatio.replace('%', '')),
        render: (row) => <span className="num">{row.buyRatio}</span>,
      },
      {
        key: 'consecBars',
        title: '连续放量',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.consecBars,
        render: (row) => <span className="num">{row.consecBars}</span>,
      },
      {
        key: 'lastEvent',
        title: '最后事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.lastEvent,
        render: (row) => row.lastEvent,
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
            <button className="btn small" onClick={() => goDetail(row.instrumentId, 'volume')}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [renderStock, goDetail],
  )

  // ===== 渲染 =====

  return (
    <div>
      {/* 页面头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">我的自选</h1>
          <div className="page-desc">
            自选股票池监控状态，可展开单个策略查看计算结果
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => setGroupModalOpen(true)}>
            管理分组
          </button>
          <button className="btn primary" onClick={() => setSearchModalOpen(true)}>
            ＋ 添加股票
          </button>
        </div>
      </div>

      {/* 策略 tabs */}
      <div className="strategy-tabs-bar" data-strategy-group="watchMonitor">
        <button
          className={clsx('strategy-tab', activeTab === 'watchNode' && 'active')}
          onClick={() => setActiveTab('watchNode')}
        >
          Volume Node Cluster <small>{nodeRows.length}</small>
        </button>
        <button
          className={clsx('strategy-tab', activeTab === 'watchAtr' && 'active')}
          onClick={() => setActiveTab('watchAtr')}
        >
          ATR Rope <small>{atrRows.length}</small>
        </button>
        <button
          className={clsx('strategy-tab', activeTab === 'watchVolume' && 'active')}
          onClick={() => setActiveTab('watchVolume')}
        >
          Volume Delta <small>{volumeRows.length}</small>
        </button>
        <div className="toolbar-spacer" />
        <select className="select" defaultValue="all">
          <option value="all">全部自选 ({watchlistItems.length})</option>
          <option value="focus">重点追踪</option>
        </select>
      </div>

      {/* Node 单策略面板 */}
      <div
        id="watchNode"
        className={clsx('strategy-panel', activeTab === 'watchNode' && 'active')}
      >
        <div className="strategy-ribbon">
          <div>
            <div className="strategy-ribbon-title">Volume Node Cluster</div>
            <div className="strategy-ribbon-meta">
              分钟 Bar 动态计算节点、POC 和碰触事件
            </div>
          </div>
          <span className="tag good">实时运行</span>
        </div>
        <div className="card">
          <StrategyDataTable
            tableId="watchlist-node"
            columns={nodeColumns}
            rows={nodeRows}
            rowKey={(row) => row.instrumentId}
            loading={nodeStatesQuery.isLoading}
            error={nodeStatesQuery.isError ? 'Node 状态加载失败' : null}
            emptyText="暂无 Node 监控状态"
          />
        </div>
      </div>

      {/* ATR 单策略面板 */}
      <div
        id="watchAtr"
        className={clsx('strategy-panel', activeTab === 'watchAtr' && 'active')}
      >
        <div className="strategy-ribbon">
          <div>
            <div className="strategy-ribbon-title">ATR Rope</div>
            <div className="strategy-ribbon-meta">
              趋势方向、偏离度和蓝带位置
            </div>
          </div>
          <span className="tag good">实时运行</span>
        </div>
        <div className="card">
          <StrategyDataTable
            tableId="watchlist-atr"
            columns={atrColumns}
            rows={atrRows}
            rowKey={(row) => row.instrumentId}
            loading={atrStatesQuery.isLoading}
            error={atrStatesQuery.isError ? 'ATR 状态加载失败' : null}
            emptyText="暂无 ATR 监控状态"
          />
        </div>
      </div>

      {/* Volume 单策略面板 */}
      <div
        id="watchVolume"
        className={clsx('strategy-panel', activeTab === 'watchVolume' && 'active')}
      >
        <div className="strategy-ribbon">
          <div>
            <div className="strategy-ribbon-title">Volume Delta</div>
            <div className="strategy-ribbon-meta">
              量能方向与异常放量确认
            </div>
          </div>
          <span className="tag good">实时运行</span>
        </div>
        <div className="card">
          <StrategyDataTable
            tableId="watchlist-volume"
            columns={volumeColumns}
            rows={volumeRows}
            rowKey={(row) => row.instrumentId}
            loading={volumeStatesQuery.isLoading}
            error={volumeStatesQuery.isError ? 'Volume 状态加载失败' : null}
            emptyText="暂无 Volume 监控状态"
          />
        </div>
      </div>

      {/* 弹窗：搜索添加自选 */}
      {searchModalOpen && (
        <AddStockModal
          watchlistIds={watchlistIds}
          onClose={() => setSearchModalOpen(false)}
        />
      )}

      {/* 弹窗：管理分组 */}
      {groupModalOpen && <GroupModal onClose={() => setGroupModalOpen(false)} />}
    </div>
  )
}
