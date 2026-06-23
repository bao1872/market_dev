// 服务总览（首页，受保护路由）
// 对应原型：index.html (V1.6.3)
// 用法：集中查看选股策略结果、全部监控策略计算状态和最新事件
// 依赖 hooks：useWatchlist / useStrategies / useStrategyRuns / useStrategyRunResults /
//             useStrategyMonitorStates / useNotificationChannels / useInstruments / useAddToWatchlist
// 路由：/
import { useState, useMemo, useCallback, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { useQueries } from '@tanstack/react-query'
import { useToast } from '@/store/toast'
import {
  useWatchlist,
  useStrategies,
  useStrategyRuns,
  useStrategyRunResults,
  useStrategyMonitorStates,
  useNotificationChannels,
  useInstruments,
  useAddToWatchlist,
} from '@/hooks/useApi'
import * as api from '@/api/endpoints'
import type { Instrument, StrategyResult, MonitorState } from '@/api/endpoints'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import { StrategySwitcher } from '@/components/StrategySwitcher'
import type { StrategyOption, StrategyPanel } from '@/components/StrategySwitcher'
import { STRATEGIES } from '@/lib/strategy-manifest'
import type { PageState } from '@/api/types'

// ===== 行类型定义（带索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown>）=====

// 选股结果行（从 StrategyResult.payload 派生）
interface SelectionRow {
  instrument_id: string
  name: string
  symbol: string
  market: string
  duration: string
  avg_return: string
  total_return: string
  shift_mean: string
  shift_var: string
  short_pos: string
  pos_tag: 'good' | 'warn'
  watched: boolean
  [key: string]: unknown
}

// Node 监控行（从 MonitorState.payload 派生）
interface NodeMonitorRow {
  instrument_id: string
  name: string
  symbol: string
  price: string
  lower_node: string
  position: number
  poc_position?: number
  upper_node: string
  upper_tag?: 'warn'
  last_touch: string
  last_event: string
  [key: string]: unknown
}

// ATR 监控行（从 MonitorState.payload 派生）
interface AtrMonitorRow {
  instrument_id: string
  name: string
  symbol: string
  price: string
  direction: string
  direction_tag: 'good' | 'warn'
  rope_pos: string
  deviation: string
  band_width: string
  duration: string
  last_event: string
  [key: string]: unknown
}

// Volume Delta 监控行（从 MonitorState.payload 派生）
interface VolumeMonitorRow {
  instrument_id: string
  name: string
  symbol: string
  price: string
  delta_dir: string
  delta_tag: 'good' | 'bad'
  z_score: string
  z_score_pos: boolean
  buy_ratio: string
  consec_bars: string
  last_event: string
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

/** 格式化 ISO 时间字符串为 HH:MM 形式，未知返回 '-' */
function fmtTime(isoString: string | null | undefined): string {
  if (!isoString) return '-'
  try {
    return new Date(isoString).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return '-'
  }
}

// ===== NodePosition 组件（节点间位置 0–1 可视化）=====
// 对应原型 .node-position 结构：填充条 + 位置标记 + POC 标记 + 刻度标签
// 使用 CSS 自定义属性 --pos / --poc 传递动态值，避免内联 style 设置 width/left
function NodePosition({
  position,
  pocPosition,
}: {
  position: number
  pocPosition?: number
}) {
  const posPct = `${Math.round(position * 100)}%`
  const pocPct = pocPosition !== undefined ? `${Math.round(pocPosition * 100)}%` : undefined
  const style = {
    '--pos': posPct,
    ...(pocPct ? { '--poc': pocPct } : {}),
  } as CSSProperties
  return (
    <div className="node-position" style={style}>
      <div className="position-line">
        <div className="position-fill"></div>
        <i className="position-marker"></i>
        {pocPosition !== undefined && <i className="poc-marker"></i>}
      </div>
      <div className="position-labels">
        <span>0</span>
        <span>{position.toFixed(2)}</span>
        <span>1</span>
      </div>
    </div>
  )
}

// ===== 添加自选弹窗组件 =====
// 独立组件：仅在弹窗打开时挂载，避免未打开时触发 useInstruments 查询
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

  // 股票搜索：keyword 为空时传 undefined 返回默认列表
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
        toast.show('已加入自选', `${name} 已自动启用全部监控策略`)
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
          <b>添加自选股</b>
          <button className="icon-btn" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="modal-body">
          <div className="field search">
            <input
              className="input search modal-full-search"
              placeholder="输入股票代码或名称"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
          </div>
          <div className="notice modal-stack">
            加入自选后，可直接应用所有已发布的监控策略，不受数量额度限制。
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
                      添加并监控
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

// ===== 主页面 =====
export default function IndexPage() {
  const toast = useToast.getState()
  const [addStockOpen, setAddStockOpen] = useState(false)

  // --- 自选列表（KPI 2 + 弹窗"已自选"判断 + 底部表格过滤）---
  const watchlistQuery = useWatchlist()
  const watchlistItems = watchlistQuery.data?.items ?? []
  const watchlistIds = useMemo(
    () => new Set(watchlistItems.map((w) => w.instrument_id)),
    [watchlistItems],
  )

  // --- 策略目录（目录卡）---
  const strategiesQuery = useStrategies()
  const strategies = strategiesQuery.data?.items ?? []

  // --- DSA 最新运行（选股结果表 + KPI 1 + 目录卡 meta）---
  const dsaRunsQuery = useStrategyRuns('dsa', { limit: 1 })
  const latestDsaRun = dsaRunsQuery.data?.items[0]
  const latestRunId = latestDsaRun?.id

  // --- DSA 运行结果（选股结果表数据）---
  const selectionResultsQuery = useStrategyRunResults(latestRunId, { limit: 20 })
  const selectionResults: StrategyResult[] = selectionResultsQuery.data?.items ?? []

  // --- 监控策略状态（底部 3 个表格 + 目录卡 meta）---
  const nodeStatesQuery = useStrategyMonitorStates('node')
  const atrStatesQuery = useStrategyMonitorStates('atr')
  const volumeStatesQuery = useStrategyMonitorStates('volume')
  const nodeStates: MonitorState[] = nodeStatesQuery.data?.items ?? []
  const atrStates: MonitorState[] = atrStatesQuery.data?.items ?? []
  const volumeStates: MonitorState[] = volumeStatesQuery.data?.items ?? []

  // --- KPI 3：自选股的监控状态总数（useInstrumentMonitorStates 按自选股逐个查询后汇总）---
  const monitorStateQueries = useQueries({
    queries: watchlistItems.map((w) => ({
      queryKey: ['instruments', w.instrument_id, 'monitor-states'],
      queryFn: () => api.getInstrumentMonitorStates(w.instrument_id),
      staleTime: 30 * 1000,
    })),
  })
  const kpi3Total = monitorStateQueries.reduce((sum, q) => sum + (q.data?.total ?? 0), 0)
  const kpi3Loading = monitorStateQueries.some((q) => q.isLoading)

  // --- 通知渠道（KPI 4）---
  const channelsQuery = useNotificationChannels()
  const channels = channelsQuery.data?.items ?? []
  const feishuChannel = channels.find((c) => c.adapter_type === 'feishu_platform_app')

  // --- 加入自选变更（选股结果表"＋ 自选"按钮）---
  const addWatchlistMutation = useAddToWatchlist()

  // --- 股票名称查找：汇总所有出现的 instrument_id，批量查询后构建 Map ---
  const allInstrumentIds = useMemo(() => {
    const ids = new Set<string>()
    selectionResults.forEach((r) => ids.add(r.instrument_id))
    nodeStates.forEach((s) => ids.add(s.instrument_id))
    atrStates.forEach((s) => ids.add(s.instrument_id))
    volumeStates.forEach((s) => ids.add(s.instrument_id))
    return [...ids]
  }, [selectionResults, nodeStates, atrStates, volumeStates])

  const instrumentQueries = useQueries({
    queries: allInstrumentIds.map((id) => ({
      queryKey: ['instruments', id],
      queryFn: () => api.getInstrumentById(id),
      staleTime: 5 * 60 * 1000,
    })),
  })

  // 股票查找 Map：instrument_id -> Instrument
  const instrumentMap = useMemo(() => {
    const m = new Map<string, Instrument>()
    instrumentQueries.forEach((q, i) => {
      if (q.data) {
        m.set(allInstrumentIds[i], q.data)
      }
    })
    return m
  }, [instrumentQueries, allInstrumentIds])

  // ===== 行转换函数 =====

  /** 将 StrategyResult 转换为 SelectionRow */
  const toSelectionRow = useCallback(
    (r: StrategyResult): SelectionRow => {
      const payload = r.payload
      const inst = instrumentMap.get(r.instrument_id)
      const shortPos = toNum(
        pickPayload(payload, ['short_pos', 'short_position', 'position_short']),
      )
      return {
        instrument_id: r.instrument_id,
        name: inst?.name ?? '-',
        symbol: inst?.symbol ?? r.instrument_id.slice(0, 8),
        market: inst?.market ?? '',
        duration: fmtNum(
          pickPayload(payload, ['duration', 'dsa_duration', 'dir_duration']),
          0,
        ),
        avg_return: fmtPct(
          pickPayload(payload, ['avg_return', 'dsa_avg_return', 'vwap_avg_return']),
        ),
        total_return: fmtPct(
          pickPayload(payload, ['total_return', 'dsa_total_return', 'cumulative_return']),
        ),
        shift_mean: fmtPct(
          pickPayload(payload, ['shift_mean', 'offset_mean', 'dsa_offset_mean']),
        ),
        shift_var: fmtPct(
          pickPayload(payload, ['shift_var', 'offset_var_rate', 'offset_variance_rate']),
        ),
        short_pos: shortPos !== null ? `${Math.round(shortPos * 100)}%` : '-',
        pos_tag: shortPos !== null && shortPos > 0.7 ? 'warn' : 'good',
        watched: watchlistIds.has(r.instrument_id),
      }
    },
    [instrumentMap, watchlistIds],
  )

  /** 将 MonitorState 转换为 NodeMonitorRow */
  const toNodeRow = useCallback(
    (s: MonitorState): NodeMonitorRow => {
      const payload = s.payload
      const inst = instrumentMap.get(s.instrument_id)
      const position =
        toNum(pickPayload(payload, ['position', 'node_position', 'position_between_nodes'])) ?? 0
      const pocPosition = toNum(pickPayload(payload, ['poc_position', 'poc_pos']))
      const upperNode = fmtStr(
        pickPayload(payload, ['upper_node', 'node_upper', 'resistance_node']),
      )
      return {
        instrument_id: s.instrument_id,
        name: inst?.name ?? '-',
        symbol: inst?.symbol ?? s.instrument_id.slice(0, 8),
        price: fmtNum(pickPayload(payload, ['price', 'last_price', 'close'])),
        lower_node: fmtStr(
          pickPayload(payload, ['lower_node', 'node_lower', 'support_node']),
        ),
        position,
        poc_position: pocPosition !== null ? pocPosition : undefined,
        upper_node: upperNode,
        upper_tag: pocPosition !== null && pocPosition > 0.8 ? 'warn' : undefined,
        last_touch: fmtStr(
          pickPayload(payload, ['last_touch', 'last_touched_node', 'recent_touch']),
        ),
        last_event: fmtStr(
          pickPayload(payload, ['last_event', 'event_description', 'latest_event']),
        ),
      }
    },
    [instrumentMap],
  )

  /** 将 MonitorState 转换为 AtrMonitorRow */
  const toAtrRow = useCallback(
    (s: MonitorState): AtrMonitorRow => {
      const payload = s.payload
      const inst = instrumentMap.get(s.instrument_id)
      const direction = fmtStr(
        pickPayload(payload, ['direction', 'trend_direction', 'rope_direction']),
      )
      return {
        instrument_id: s.instrument_id,
        name: inst?.name ?? '-',
        symbol: inst?.symbol ?? s.instrument_id.slice(0, 8),
        price: fmtNum(pickPayload(payload, ['price', 'last_price', 'close'])),
        direction,
        direction_tag: direction === '向上' ? 'good' : 'warn',
        rope_pos: fmtNum(pickPayload(payload, ['rope_pos', 'rope_position', 'band_position'])),
        deviation: fmtPct(pickPayload(payload, ['deviation', 'deviation_pct', 'rope_deviation'])),
        band_width: fmtPct(pickPayload(payload, ['band_width', 'rope_width', 'atr_band_width'])),
        duration: fmtStr(pickPayload(payload, ['duration', 'state_duration', 'bars_in_state'])),
        last_event: fmtStr(
          pickPayload(payload, ['last_event', 'event_description', 'latest_event']),
        ),
      }
    },
    [instrumentMap],
  )

  /** 将 MonitorState 转换为 VolumeMonitorRow */
  const toVolumeRow = useCallback(
    (s: MonitorState): VolumeMonitorRow => {
      const payload = s.payload
      const inst = instrumentMap.get(s.instrument_id)
      const deltaDir = fmtStr(
        pickPayload(payload, ['delta_dir', 'delta_direction', 'flow_direction']),
      )
      const zScore = toNum(pickPayload(payload, ['z_score', 'volume_zscore', 'zscore']))
      return {
        instrument_id: s.instrument_id,
        name: inst?.name ?? '-',
        symbol: inst?.symbol ?? s.instrument_id.slice(0, 8),
        price: fmtNum(pickPayload(payload, ['price', 'last_price', 'close'])),
        delta_dir: deltaDir,
        delta_tag: deltaDir.includes('流入') ? 'good' : 'bad',
        z_score: zScore !== null ? zScore.toFixed(2) : '-',
        z_score_pos: zScore !== null && zScore > 0,
        buy_ratio: fmtPct(pickPayload(payload, ['buy_ratio', 'buy_percentage', 'active_buy_ratio'])),
        consec_bars: fmtStr(
          pickPayload(payload, ['consec_bars', 'consecutive_bars', 'consecutive_volume_bars']),
        ),
        last_event: fmtStr(
          pickPayload(payload, ['last_event', 'event_description', 'latest_event']),
        ),
      }
    },
    [instrumentMap],
  )

  // ===== 派生数据 =====

  // 选股结果行
  const selectionRows: SelectionRow[] = useMemo(
    () => selectionResults.map(toSelectionRow),
    [selectionResults, toSelectionRow],
  )

  // 底部监控表格：按自选股过滤
  const nodeRows: NodeMonitorRow[] = useMemo(
    () => nodeStates.filter((s) => watchlistIds.has(s.instrument_id)).map(toNodeRow),
    [nodeStates, watchlistIds, toNodeRow],
  )
  const atrRows: AtrMonitorRow[] = useMemo(
    () => atrStates.filter((s) => watchlistIds.has(s.instrument_id)).map(toAtrRow),
    [atrStates, watchlistIds, toAtrRow],
  )
  const volumeRows: VolumeMonitorRow[] = useMemo(
    () => volumeStates.filter((s) => watchlistIds.has(s.instrument_id)).map(toVolumeRow),
    [volumeStates, watchlistIds, toVolumeRow],
  )

  // KPI 1：今日选股结果数
  const kpi1Total = selectionResultsQuery.data?.total ?? 0
  const kpi1Loading = selectionResultsQuery.isLoading

  // KPI 2：监控自选股数
  const kpi2Total = watchlistItems.length

  // KPI 4：通知渠道状态
  const kpi4Status = feishuChannel ? '飞书正常' : '未配置'
  const kpi4Time = fmtTime(feishuChannel?.last_verified_at)

  // 目录卡数据
  const catalogCards = useMemo(() => {
    const cards: Array<{
      type: 'SELECTION' | 'MONITOR'
      status: string
      title: string
      desc: string
      meta: string
      active?: boolean
    }> = []
    // DSA 选股
    const dsa = strategies.find((s) => s.strategy_key === 'dsa')
    if (dsa) {
      cards.push({
        type: 'SELECTION',
        status: '正常',
        title: dsa.display_name,
        desc: '收盘后计算全市场方向稳定性、收益速度与偏移指标。',
        meta: latestDsaRun?.finished_at
          ? `最近完成 ${fmtTime(latestDsaRun.finished_at)}`
          : '今日尚未运行',
        active: true,
      })
    }
    // Volume Node Cluster
    const node = strategies.find((s) => s.strategy_key === 'node')
    if (node) {
      const latestBarTime = nodeStates[0]?.bar_time
      cards.push({
        type: 'MONITOR',
        status: '实时',
        title: node.display_name,
        desc: '分钟级动态节点、POC、区间位置与碰触事件。',
        meta: `${nodeStates.length} 只 · ${fmtTime(latestBarTime)}`,
      })
    }
    // ATR Rope
    const atr = strategies.find((s) => s.strategy_key === 'atr')
    if (atr) {
      const latestBarTime = atrStates[0]?.bar_time
      cards.push({
        type: 'MONITOR',
        status: '实时',
        title: atr.display_name,
        desc: '趋势方向、蓝带位置、偏离程度和状态切换事件。',
        meta: `${atrStates.length} 只 · ${fmtTime(latestBarTime)}`,
      })
    }
    return cards
  }, [strategies, latestDsaRun, nodeStates, atrStates])

  // ===== 事件处理 =====

  /** 选股结果表"＋ 自选"按钮 */
  const handleAddToWatchlist = useCallback(
    async (instrumentId: string, name: string) => {
      try {
        await addWatchlistMutation.mutateAsync({
          instrument_id: instrumentId,
          source: 'selection',
        })
        toast.show('已加入自选', `${name} 已自动启用全部监控策略`)
      } catch {
        toast.show('加入失败', '请稍后重试')
      }
    },
    [addWatchlistMutation, toast],
  )

  // ===== 列定义 =====

  // 选股结果表列
  const selectionColumns: DataTableColumn<SelectionRow>[] = useMemo(
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
            <div className="symbol-sub">
              {row.symbol}
              {row.market ? ` · ${row.market}` : ''}
            </div>
          </div>
        ),
      },
      {
        key: 'duration',
        title: '持续时间',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.duration) || 0,
        render: (row) => <span className="num">{row.duration}</span>,
      },
      {
        key: 'avg_return',
        title: '平均收益率',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.avg_return.replace('%', '')) || 0,
        render: (row) => <span className="num pos">{row.avg_return}</span>,
      },
      {
        key: 'total_return',
        title: '总收益率',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.total_return.replace('%', '')) || 0,
        render: (row) => <span className="num pos">{row.total_return}</span>,
      },
      {
        key: 'shift_mean',
        title: '偏移均值',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.shift_mean.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.shift_mean}</span>,
      },
      {
        key: 'shift_var',
        title: '偏移方差率',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.shift_var.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.shift_var}</span>,
      },
      {
        key: 'short_pos',
        title: '短期位置',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.short_pos.replace('%', '')) || 0,
        render: (row) => <span className={`tag ${row.pos_tag}`}>{row.short_pos}</span>,
      },
      {
        key: 'action',
        title: '操作',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) =>
          row.watched ? (
            <span className="tag info">已自选</span>
          ) : (
            <button
              className="btn small"
              onClick={() => handleAddToWatchlist(row.instrument_id, row.name)}
              disabled={addWatchlistMutation.isPending}
            >
              ＋ 自选
            </button>
          ),
      },
    ],
    [handleAddToWatchlist, addWatchlistMutation.isPending],
  )

  // Node 监控表列
  const nodeColumns: DataTableColumn<NodeMonitorRow>[] = useMemo(
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
        key: 'price',
        title: '当前价格',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.price) || 0,
        render: (row) => <span className="num">{row.price}</span>,
      },
      {
        key: 'lower_node',
        title: '下方最近节点',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => <span className="num">{row.lower_node}</span>,
      },
      {
        key: 'position',
        title: '节点间位置 0–1',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => <NodePosition position={row.position} pocPosition={row.poc_position} />,
      },
      {
        key: 'upper_node',
        title: '上方最近节点',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => (
          <span className="num">
            {row.upper_node}
            {row.upper_tag && (
              <span className="tag warn tag-gap">POC</span>
            )}
          </span>
        ),
      },
      {
        key: 'last_touch',
        title: '最近碰触节点',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => <span className="num">{row.last_touch}</span>,
      },
      {
        key: 'last_event',
        title: '最后事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        render: (row) => row.last_event,
      },
    ],
    [],
  )

  // ATR 监控表列
  const atrColumns: DataTableColumn<AtrMonitorRow>[] = useMemo(
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
        key: 'price',
        title: '当前价格',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.price) || 0,
        render: (row) => <span className="num">{row.price}</span>,
      },
      {
        key: 'direction',
        title: '方向',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => <span className={`tag ${row.direction_tag}`}>{row.direction}</span>,
      },
      {
        key: 'rope_pos',
        title: '蓝带位置 0–1',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.rope_pos) || 0,
        render: (row) => <span className="num">{row.rope_pos}</span>,
      },
      {
        key: 'deviation',
        title: '偏离度',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.deviation.replace('%', '')) || 0,
        render: (row) => <span className="num pos">{row.deviation}</span>,
      },
      {
        key: 'band_width',
        title: '蓝带宽度收益率',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.band_width.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.band_width}</span>,
      },
      {
        key: 'duration',
        title: '状态持续',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => <span className="num">{row.duration}</span>,
      },
      {
        key: 'last_event',
        title: '最后事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        render: (row) => row.last_event,
      },
    ],
    [],
  )

  // Volume Delta 监控表列
  const volumeColumns: DataTableColumn<VolumeMonitorRow>[] = useMemo(
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
        key: 'price',
        title: '当前价格',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.price) || 0,
        render: (row) => <span className="num">{row.price}</span>,
      },
      {
        key: 'delta_dir',
        title: 'Delta 方向',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => <span className={`tag ${row.delta_tag}`}>{row.delta_dir}</span>,
      },
      {
        key: 'z_score',
        title: '成交量 Z-score',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.z_score) || 0,
        render: (row) => <span className={`num${row.z_score_pos ? ' pos' : ''}`}>{row.z_score}</span>,
      },
      {
        key: 'buy_ratio',
        title: '主动买入占比',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.buy_ratio.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.buy_ratio}</span>,
      },
      {
        key: 'consec_bars',
        title: '连续放量',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => <span className="num">{row.consec_bars}</span>,
      },
      {
        key: 'last_event',
        title: '最后事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        render: (row) => row.last_event,
      },
    ],
    [],
  )

  // ===== StrategySwitcher 配置 =====

  const monitorOptions: StrategyOption[] = [
    {
      id: 'overviewNode',
      name: STRATEGIES.node.name,
      description: '节点、POC 与碰触事件',
      kind: 'monitor',
      version: STRATEGIES.node.version,
    },
    {
      id: 'overviewAtr',
      name: STRATEGIES.atr.name,
      description: '趋势、蓝带与偏离状态',
      kind: 'monitor',
      version: STRATEGIES.atr.version,
    },
    {
      id: 'overviewVolume',
      name: STRATEGIES.volume.name,
      description: '量能方向与异常放量',
      kind: 'monitor',
      version: STRATEGIES.volume.version,
    },
  ]

  // 面板状态计算
  const getPanelState = useCallback(
    (isLoading: boolean, isError: boolean, rowCount: number): PageState => {
      if (isLoading) return 'loading'
      if (isError) return 'error'
      if (rowCount === 0) return 'empty'
      return 'ready'
    },
    [],
  )

  const monitorPanels: Record<string, StrategyPanel> = {
    overviewNode: {
      id: 'overviewNode',
      state: getPanelState(nodeStatesQuery.isLoading, nodeStatesQuery.isError, nodeRows.length),
      content: (
        <StrategyDataTable
          tableId="index-node-monitor"
          columns={nodeColumns}
          rows={nodeRows}
          rowKey={(row) => row.instrument_id}
          loading={nodeStatesQuery.isLoading}
          error={nodeStatesQuery.isError ? '监控状态加载失败' : null}
          searchable={false}
          emptyText="自选股暂无 Node 监控状态"
        />
      ),
    },
    overviewAtr: {
      id: 'overviewAtr',
      state: getPanelState(atrStatesQuery.isLoading, atrStatesQuery.isError, atrRows.length),
      content: (
        <StrategyDataTable
          tableId="index-atr-monitor"
          columns={atrColumns}
          rows={atrRows}
          rowKey={(row) => row.instrument_id}
          loading={atrStatesQuery.isLoading}
          error={atrStatesQuery.isError ? '监控状态加载失败' : null}
          searchable={false}
          emptyText="自选股暂无 ATR 监控状态"
        />
      ),
    },
    overviewVolume: {
      id: 'overviewVolume',
      state: getPanelState(
        volumeStatesQuery.isLoading,
        volumeStatesQuery.isError,
        volumeRows.length,
      ),
      content: (
        <StrategyDataTable
          tableId="index-volume-monitor"
          columns={volumeColumns}
          rows={volumeRows}
          rowKey={(row) => row.instrument_id}
          loading={volumeStatesQuery.isLoading}
          error={volumeStatesQuery.isError ? '监控状态加载失败' : null}
          searchable={false}
          emptyText="自选股暂无 Volume Delta 监控状态"
        />
      ),
    },
  }

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">服务总览</h1>
          <div className="page-desc">
            集中查看选股策略结果、全部监控策略计算状态和最新事件
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => setAddStockOpen(true)}>
            ＋ 添加自选
          </button>
          <Link className="btn primary" to="/screener">
            查看选股策略
          </Link>
        </div>
      </div>

      {/* KPI 卡片 */}
      <div className="grid kpi">
        {/* KPI 1：今日选股结果数 */}
        <div className="card kpi-card">
          <div className="kpi-label">今日选股结果</div>
          <div className="kpi-value">
            {kpi1Loading ? '-' : kpi1Total}
            <small className="kpi-unit">只</small>
          </div>
          <div className="kpi-foot">DSA 选股策略</div>
        </div>
        {/* KPI 2：监控自选股数 */}
        <div className="card kpi-card">
          <div className="kpi-label">监控自选股</div>
          <div className="kpi-value">
            {watchlistQuery.isLoading ? '-' : kpi2Total}
            <small className="kpi-unit">只</small>
          </div>
          <div className="kpi-foot">已启用 3 个监控策略</div>
        </div>
        {/* KPI 3：今日策略事件数 */}
        <div className="card kpi-card">
          <div className="kpi-label">今日策略事件</div>
          <div className="kpi-value">
            {kpi3Loading ? '-' : kpi3Total}
          </div>
          <div className="kpi-foot">跨 {kpi2Total} 只自选股</div>
        </div>
        {/* KPI 4：通知渠道状态 */}
        <div className="card kpi-card">
          <div className="kpi-label">通知渠道</div>
          <div className="kpi-value-sm">
            <i className="dot ok"></i>
            {channelsQuery.isLoading ? '-' : kpi4Status}
          </div>
          <div className="kpi-foot">由用户配置 · {kpi4Time} 验证</div>
        </div>
      </div>

      {/* 选股结果 + 策略运行状态 */}
      <div className="grid split-2">
        {/* 最新选股策略结果 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">最新选股策略结果</div>
              <div className="card-sub">
                策略：DSA
                {latestDsaRun?.trade_date ? ` · ${latestDsaRun.trade_date}` : ''}
              </div>
            </div>
            <div className="card-head-actions">
              <Link className="btn small ghost" to="/screener">
                查看全部 →
              </Link>
            </div>
          </div>
          <StrategyDataTable
            tableId="index-selection-results"
            columns={selectionColumns}
            rows={selectionRows}
            rowKey={(row) => row.instrument_id}
            loading={selectionResultsQuery.isLoading || dsaRunsQuery.isLoading}
            error={
              selectionResultsQuery.isError || dsaRunsQuery.isError
                ? '选股结果加载失败'
                : null
            }
            searchable={false}
            emptyText="今日暂无选股结果"
          />
        </section>

        {/* 策略运行状态 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">策略运行状态</div>
              <div className="card-sub">
                共享后台统一计算，用户按自选关系接收结果
              </div>
            </div>
          </div>
          <div className="card-body">
            <div className="strategy-catalog">
              {catalogCards.map((card) => (
                <div
                  key={card.title}
                  className={`strategy-catalog-card${card.active ? ' active' : ''}`}
                >
                  <span className="strategy-type-pill">{card.type}</span>
                  <span className="badge-corner tag good">{card.status}</span>
                  <h3>{card.title}</h3>
                  <p>{card.desc}</p>
                  <div className="symbol-sub">{card.meta}</div>
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>

      {/* 监控策略计算结果（StrategySwitcher） */}
      <section className="card section-gap">
        <div className="card-head">
          <div>
            <div className="card-title">监控策略计算结果</div>
            <div className="card-sub">
              切换策略查看同一自选池的最新计算结果
            </div>
          </div>
        </div>
        <StrategySwitcher
          group="overviewMonitor"
          options={monitorOptions}
          panels={monitorPanels}
        />
      </section>

      {/* 添加自选弹窗 */}
      {addStockOpen && (
        <AddStockModal
          watchlistIds={watchlistIds}
          onClose={() => setAddStockOpen(false)}
        />
      )}
    </>
  )
}
