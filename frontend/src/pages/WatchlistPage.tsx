// 我的自选页（受保护路由）
// 对应原型：watchlist.html (V1.6.3)
// 用法：展示用户自选股票池，支持监控方案切换、组合状态查看、单策略明细展开、搜索添加自选
// 路由：/watchlist
// 依赖 hooks：useWatchlist / useMonitoringPlans / useMonitoringPlanStates / useMonitoringPlanEvents /
//             useStrategyMonitorStates / useInstruments / useAddToWatchlist / useRemoveFromWatchlist
import { useState, useMemo, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import { useQueries } from '@tanstack/react-query'
import { useToast } from '@/store/toast'
import {
  useWatchlist,
  useMonitoringPlans,
  useMonitoringPlanStates,
  useMonitoringPlanEvents,
  useStrategyMonitorStates,
  useInstruments,
  useAddToWatchlist,
  useRemoveFromWatchlist,
} from '@/hooks/useApi'
import * as api from '@/api/endpoints'
import type {
  Instrument,
  MonitorState,
  MonitoringPlanState,
  CompositeMonitorEvent,
  MonitoringPlan,
} from '@/api/endpoints'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

// ===== 类型定义 =====

// 组合状态行（从 MonitoringPlanState + 各策略 MonitorState + 最新组合事件派生）
interface CombinedRow {
  instrumentId: string
  symbol: string
  name: string
  price: string
  nodeStatus: string
  nodeTag: string
  atrStatus: string
  atrTag: string
  volumeStatus: string
  volumeTag: string
  confirmedCount: number
  totalMembers: number
  comboStatus: string
  comboTag: 'ok' | 'wait' | 'off'
  windowRange: string
  lastEvent: string
  [key: string]: unknown
}

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

/** 格式化 ISO 时间字符串为 HH:MM:SS 形式，未知返回 '-' */
function fmtTime(isoString: string | null | undefined): string {
  if (!isoString) return '-'
  try {
    return new Date(isoString).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  } catch {
    return '-'
  }
}

/** 格式化 ISO 时间为 HH:MM 形式，未知返回 '-' */
function fmtTimeShort(isoString: string | null | undefined): string {
  if (!isoString) return '-'
  try {
    return new Date(isoString).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return '-'
  }
}

/** 根据事件文本推断 tag 类别：等待类 -> warn，确认/碰触/放量类 -> good，空 -> 空串 */
function inferTag(text: string): string {
  if (!text || text === '-') return ''
  if (text.includes('等待') || text.includes('pending')) return 'warn'
  if (
    text.includes('确认') ||
    text.includes('碰触') ||
    text.includes('放量') ||
    text.includes('触发') ||
    text.includes('向上') ||
    text.includes('流入')
  ) {
    return 'good'
  }
  return ''
}

/** 计算组合窗口范围：已用时间 / 总窗口（分钟），无窗口返回 '-' */
function computeWindowRange(state: MonitoringPlanState | undefined): string {
  if (!state?.window_started_at || !state?.window_deadline_at) return '-'
  const start = new Date(state.window_started_at).getTime()
  const end = new Date(state.window_deadline_at).getTime()
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return '-'
  const totalMin = Math.round((end - start) / 60000)
  const now = Date.now()
  const elapsedMin = Math.max(0, Math.round((now - start) / 60000))
  return `${elapsedMin}m / ${totalMin}m`
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
        toast.show('已加入并开始组合监控', `${name} 已加入自选`)
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
            加入后自动进入当前启用的监控组合方案。
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
  const toast = useToast.getState()

  // --- 自选列表 ---
  const watchlistQuery = useWatchlist()
  const watchlistItems = watchlistQuery.data?.items ?? []
  const watchlistIds = useMemo(
    () => new Set(watchlistItems.map((w) => w.instrument_id)),
    [watchlistItems],
  )

  // --- 监控方案列表 ---
  const plansQuery = useMonitoringPlans()
  const plans: MonitoringPlan[] = plansQuery.data?.items ?? []

  // --- 当前选中方案（默认取第一个 active 方案，否则取第一个） ---
  const [selectedPlanId, setSelectedPlanId] = useState<string>('')
  const activePlanId = useMemo(() => {
    if (selectedPlanId) return selectedPlanId
    const activePlan = plans.find((p) => p.status === 'active') ?? plans[0]
    return activePlan?.id ?? ''
  }, [selectedPlanId, plans])
  const activePlan = plans.find((p) => p.id === activePlanId)
  const activeRevision = activePlan?.current_revision_detail
  const activeMembers = activeRevision?.members ?? []
  const totalMembers = activeMembers.length

  // --- 组合状态（当前方案下所有股票的组合状态） ---
  const planStatesQuery = useMonitoringPlanStates(activePlanId || undefined)
  const planStates: MonitoringPlanState[] = planStatesQuery.data?.items ?? []

  // --- 组合事件（今日） ---
  const todayStart = useMemo(() => {
    const d = new Date()
    d.setHours(0, 0, 0, 0)
    return d.toISOString()
  }, [])
  const planEventsQuery = useMonitoringPlanEvents(activePlanId || undefined, {
    start_time: todayStart,
    limit: 200,
  })
  const planEvents: CompositeMonitorEvent[] = planEventsQuery.data?.items ?? []

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
  const [activeTab, setActiveTab] = useState<string>('watchCombined')
  const [searchModalOpen, setSearchModalOpen] = useState(false)
  const [groupModalOpen, setGroupModalOpen] = useState(false)

  // --- 移除自选变更 ---
  const removeMutation = useRemoveFromWatchlist()

  // ===== 派生数据 =====

  // 按 instrument_id 索引的 Map，便于组合状态行快速查找
  const planStateMap = useMemo(() => {
    const m = new Map<string, MonitoringPlanState>()
    for (const s of planStates) {
      if (watchlistIds.has(s.instrument_id)) {
        m.set(s.instrument_id, s)
      }
    }
    return m
  }, [planStates, watchlistIds])

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

  // 每只股票的最新组合事件（按 event_time 降序取首条）
  const latestEventMap = useMemo(() => {
    const m = new Map<string, CompositeMonitorEvent>()
    for (const e of planEvents) {
      if (!watchlistIds.has(e.instrument_id)) continue
      const prev = m.get(e.instrument_id)
      if (!prev || new Date(e.event_time) > new Date(prev.event_time)) {
        m.set(e.instrument_id, e)
      }
    }
    return m
  }, [planEvents, watchlistIds])

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

  /** 提取某策略状态的事件描述（用于组合状态表的 Node/ATR/Volume 列） */
  const getStrategyStatus = useCallback(
    (state: MonitorState | undefined): { text: string; tag: string } => {
      if (!state) return { text: '-', tag: '' }
      const lastEvent = fmtStr(
        pickPayload(state.payload, [
          'last_event',
          'event_description',
          'latest_event',
        ]),
      )
      const barTime = fmtTimeShort(state.bar_time)
      const text = lastEvent !== '-' ? `${barTime} ${lastEvent}` : '-'
      return { text, tag: inferTag(text) }
    },
    [],
  )

  /** 将自选股 + 组合状态 + 各策略状态 + 最新事件 组合为 CombinedRow */
  const toCombinedRow = useCallback(
    (instrumentId: string): CombinedRow => {
      const { symbol, name } = getStockDisplay(instrumentId)
      const planState = planStateMap.get(instrumentId)
      const nodeState = nodeStateMap.get(instrumentId)
      const atrState = atrStateMap.get(instrumentId)
      const volumeState = volumeStateMap.get(instrumentId)
      const latestEvent = latestEventMap.get(instrumentId)

      // 当前价格：优先取 Node，其次 ATR，再次 Volume
      const price = fmtNum(
        pickPayload(nodeState?.payload ?? atrState?.payload ?? volumeState?.payload ?? {}, [
          'price',
          'last_price',
          'close',
        ]),
      )

      const nodeStatus = getStrategyStatus(nodeState)
      const atrStatus = getStrategyStatus(atrState)
      const volumeStatus = getStrategyStatus(volumeState)

      // 组合进度
      const confirmedCount = planState?.confirmed_member_ids?.length ?? 0
      const total = totalMembers || 3
      let comboStatus = `${confirmedCount}/${total} 进行中`
      let comboTag: 'ok' | 'wait' | 'off' = 'wait'
      if (planState) {
        if (planState.status === 'confirmed' || confirmedCount >= total) {
          comboStatus = `${total}/${total} 已确认`
          comboTag = 'ok'
        } else if (planState.status === 'cooldown') {
          comboStatus = `${confirmedCount}/${total} 冷却中`
          comboTag = 'off'
        } else {
          comboStatus = `${confirmedCount}/${total} 进行中`
          comboTag = 'wait'
        }
      }

      // 组合窗口
      const windowRange = computeWindowRange(planState)

      // 最后组合事件
      const lastEventText = latestEvent
        ? `${fmtTimeShort(latestEvent.event_time)} · ${latestEvent.event_type}`
        : '-'

      return {
        instrumentId,
        symbol,
        name,
        price,
        nodeStatus: nodeStatus.text,
        nodeTag: nodeStatus.tag,
        atrStatus: atrStatus.text,
        atrTag: atrStatus.tag,
        volumeStatus: volumeStatus.text,
        volumeTag: volumeStatus.tag,
        confirmedCount,
        totalMembers: total,
        comboStatus,
        comboTag,
        windowRange,
        lastEvent: lastEventText,
      }
    },
    [
      getStockDisplay,
      planStateMap,
      nodeStateMap,
      atrStateMap,
      volumeStateMap,
      latestEventMap,
      totalMembers,
      getStrategyStatus,
    ],
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

  // 组合状态行：所有自选股
  const combinedRows: CombinedRow[] = useMemo(
    () => watchlistIdList.map(toCombinedRow),
    [watchlistIdList, toCombinedRow],
  )

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

  // ===== KPI 派生 =====

  // KPI 1：自选股票数
  const kpi1Total = watchlistItems.length

  // KPI 2：当前监控方案名称
  const kpi2Name = activePlan?.name ?? '未启用'
  const kpi2Meta =
    totalMembers > 0 ? `${totalMembers} 个策略 · ALL / 15m` : '未配置策略'

  // KPI 3：今日组合事件数
  const kpi3Total = planEvents.length
  const kpi3ProcessCount = planEvents.filter((e) =>
    String(e.event_type).toLowerCase().includes('process'),
  ).length

  // KPI 4：最近分钟数据时间（取所有监控状态中最新的 bar_time）
  const latestBarTime = useMemo(() => {
    const allStates = [...nodeStates, ...atrStates, ...volumeStates]
    let latest: string | null = null
    for (const s of allStates) {
      if (!s.bar_time) continue
      if (!latest || new Date(s.bar_time) > new Date(latest)) {
        latest = s.bar_time
      }
    }
    return latest
  }, [nodeStates, atrStates, volumeStates])

  const kpi4Time = fmtTime(latestBarTime)
  const kpi4Delay = useMemo(() => {
    if (!latestBarTime) return '-'
    const diffMs = Date.now() - new Date(latestBarTime).getTime()
    if (Number.isNaN(diffMs) || diffMs < 0) return '-'
    return `${Math.round(diffMs / 1000)} 秒`
  }, [latestBarTime])

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (instrumentId: string, strategy: string) => {
      const { symbol } = getStockDisplay(instrumentId)
      navigate(`/stock/${symbol}?source=watchlist&strategy=${strategy}`)
    },
    [navigate, getStockDisplay],
  )

  /** 切换方案 */
  const handlePlanChange = (id: string) => {
    setSelectedPlanId(id)
    setActiveTab('watchCombined')
  }

  /** 移除自选 */
  const handleRemove = useCallback(
    async (instrumentId: string, name: string) => {
      try {
        await removeMutation.mutateAsync(instrumentId)
        toast.show('已移出自选', `${name} 已从自选列表移除`)
      } catch {
        toast.show('移除失败', '请稍后重试')
      }
    },
    [removeMutation, toast],
  )

  // ===== 列定义 =====

  // 股票列渲染（复用）
  const renderStock = useCallback(
    (row: CombinedRow | NodeRow | AtrRow | VolumeRow) => {
      return (
        <div>
          <div className="symbol">{row.name}</div>
          <div className="symbol-sub">{row.symbol}</div>
        </div>
      )
    },
    [],
  )

  // 组合状态列
  const combinedColumns: DataTableColumn<CombinedRow>[] = useMemo(
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
        key: 'nodeStatus',
        title: 'Node 状态',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.nodeStatus,
        render: (row) =>
          row.nodeStatus === '-' ? (
            <span className="muted">-</span>
          ) : (
            <span className={clsx('tag', row.nodeTag || undefined)}>{row.nodeStatus}</span>
          ),
      },
      {
        key: 'atrStatus',
        title: 'ATR 状态',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.atrStatus,
        render: (row) =>
          row.atrStatus === '-' ? (
            <span className="muted">-</span>
          ) : (
            <span className={clsx('tag', row.atrTag || undefined)}>{row.atrStatus}</span>
          ),
      },
      {
        key: 'volumeStatus',
        title: 'Volume 状态',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.volumeStatus,
        render: (row) =>
          row.volumeStatus === '-' ? (
            <span className="muted">-</span>
          ) : (
            <span className={clsx('tag', row.volumeTag || undefined)}>{row.volumeStatus}</span>
          ),
      },
      {
        key: 'comboStatus',
        title: '组合进度',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => (
          <span className={clsx('status-pill', row.comboTag)}>{row.comboStatus}</span>
        ),
      },
      {
        key: 'windowRange',
        title: '组合窗口',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => <span className="num">{row.windowRange}</span>,
      },
      {
        key: 'lastEvent',
        title: '最后组合事件',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => row.lastEvent,
        render: (row) => (row.lastEvent === '-' ? <span className="muted">-</span> : row.lastEvent),
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
            <button className="btn small" onClick={() => goDetail(row.instrumentId, 'combined')}>
              详情
            </button>
            <button
              className="btn small"
              onClick={() => handleRemove(row.instrumentId, row.name)}
              disabled={removeMutation.isPending}
            >
              移除
            </button>
          </div>
        ),
      },
    ],
    [renderStock, goDetail, handleRemove, removeMutation.isPending],
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

  const plansLoading = plansQuery.isLoading
  const statesLoading = planStatesQuery.isLoading
  const statesError = planStatesQuery.isError ? '组合状态加载失败，请稍后重试' : null
  const isPlanRunning = activePlan?.status === 'active'

  // 成员策略展示名（用于组合成分 chips）
  const memberChips = useMemo(() => {
    const chips: Array<{ key: string; name: string; color: string }> = []
    const colors = ['blue', 'violet', 'green']
    activeMembers.forEach((m, i) => {
      chips.push({
        key: m.id,
        name: `策略 ${i + 1}`,
        color: colors[i % colors.length],
      })
    })
    // 若无成员，回退到默认三策略展示（对齐原型 Node/ATR Rope/Volume Delta）
    if (chips.length === 0) {
      return [
        { key: 'node', name: 'Node', color: 'blue' },
        { key: 'atr', name: 'ATR Rope', color: 'violet' },
        { key: 'volume', name: 'Volume Delta', color: 'green' },
      ]
    }
    return chips
  }, [activeMembers])

  return (
    <div>
      {/* 页面头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">我的自选</h1>
          <div className="page-desc">
            自选股票池共享给监控组合方案；可查看组合状态，也可展开单个策略计算结果
          </div>
        </div>
        <div className="actions">
          <Link className="btn" to="/monitoring-plan-editor">
            编辑当前监控方案
          </Link>
          <button className="btn" onClick={() => setGroupModalOpen(true)}>
            管理分组
          </button>
          <button className="btn primary" onClick={() => setSearchModalOpen(true)}>
            ＋ 添加股票
          </button>
        </div>
      </div>

      {/* KPI 组 */}
      <div className="grid kpi">
        <div className="card kpi-card">
          <div className="kpi-label">自选股票</div>
          <div className="kpi-value">
            {watchlistQuery.isLoading ? '-' : kpi1Total}
            <small className="kpi-unit">只</small>
          </div>
          <div className="kpi-foot">会员全功能 · 不限数量</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">当前监控方案</div>
          <div className="kpi-value kpi-value-sm">{kpi2Name}</div>
          <div className="kpi-foot">{kpi2Meta}</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">今日组合事件</div>
          <div className="kpi-value">
            {planEventsQuery.isLoading ? '-' : kpi3Total}
          </div>
          <div className="kpi-foot">过程事件 {kpi3ProcessCount}</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">最近分钟数据</div>
          <div className="kpi-value kpi-value-sm">{kpi4Time}</div>
          <div className="kpi-foot">
            <i className="dot ok"></i>延迟 {kpi4Delay}
          </div>
        </div>
      </div>

      {/* 方案切换栏 */}
      <div className="plan-switch-bar">
        <div>
          <span className="muted">监控方案</span>
          {plansLoading ? (
            <span className="muted">加载中…</span>
          ) : plans.length === 0 ? (
            <span className="muted">暂无方案</span>
          ) : (
            <select
              className="select"
              value={activePlanId}
              onChange={(e) => handlePlanChange(e.target.value)}
            >
              {plans.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} · {totalMembers || p.current_revision} 个策略
                </option>
              ))}
            </select>
          )}
        </div>
        {/* 组合成分 chips */}
        <div className="plan-composition">
          {memberChips.map((c, i) => (
            <span key={c.key} className="plan-composition-item">
              <span className={clsx('chip', c.color)}>{c.name}</span>
              {i < memberChips.length - 1 && <span className="combo-token">+</span>}
            </span>
          ))}
          <span className="combo-token">ALL / 15m</span>
        </div>
        <div className="toolbar-spacer" />
        <span className={clsx('status-pill', isPlanRunning ? 'ok' : 'off')}>
          {isPlanRunning ? '运行中' : '已暂停'}
        </span>
      </div>

      {/* 策略 tabs */}
      <div className="strategy-tabs-bar" data-strategy-group="watchMonitor">
        <button
          className={clsx('strategy-tab', activeTab === 'watchCombined' && 'active')}
          onClick={() => setActiveTab('watchCombined')}
        >
          组合状态 <small>{combinedRows.length}</small>
        </button>
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
        <button
          className={clsx('strategy-tab', activeTab === 'watchFuture' && 'active')}
          onClick={() => setActiveTab('watchFuture')}
        >
          ＋ 更多策略
        </button>
        <div className="toolbar-spacer" />
        <select className="select" defaultValue="all">
          <option value="all">全部自选 ({kpi1Total})</option>
          <option value="focus">重点追踪</option>
        </select>
      </div>

      {/* 组合状态面板 */}
      <div
        id="watchCombined"
        className={clsx('strategy-panel', activeTab === 'watchCombined' && 'active')}
      >
        <div className="strategy-ribbon">
          <div>
            <div className="strategy-ribbon-title">
              {activePlan?.name ?? '监控方案'} · 组合状态
            </div>
            <div className="strategy-ribbon-meta">
              Node 为触发策略，ATR Rope 与 Volume Delta 在 15 分钟内完成确认
            </div>
          </div>
          <Link className="btn small" to="/monitoring-plan-editor">
            查看组合逻辑
          </Link>
        </div>
        <div className="card">
          <StrategyDataTable
            tableId="watchlist-combined"
            columns={combinedColumns}
            rows={combinedRows}
            rowKey={(row) => row.instrumentId}
            loading={statesLoading}
            error={statesError}
            emptyText="暂无自选股票，点击右上角添加"
          />
        </div>
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
              组合成员策略 1/3 · 分钟 Bar 动态计算节点、POC 和碰触事件
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
              组合成员策略 2/3 · 趋势方向、偏离度和蓝带位置
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
              组合成员策略 3/3 · 量能方向与异常放量确认
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

      {/* 更多策略面板 */}
      <div
        id="watchFuture"
        className={clsx('strategy-panel', activeTab === 'watchFuture' && 'active')}
      >
        <div className="card">
          <div className="empty">
            <h3>更多监控策略可以加入当前方案</h3>
            <p>新增策略后可配置为触发、确认或否决角色。</p>
            <Link className="btn primary" to="/monitoring-plan-editor">
              前往编辑监控方案
            </Link>
          </div>
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
