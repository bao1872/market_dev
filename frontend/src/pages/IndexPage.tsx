// 服务总览（首页，受保护路由）
// 对应原型：index.html (V1.6.3)
// 用法：集中查看选股策略结果与监控策略最新状态
// 依赖 hooks：useWatchlist / usePublishedRuns / useStrategyRunResults /
//             useWatchlistMonitorStatus / useNotificationChannels / useInstruments / useAddToWatchlist / useEventsSummary
// 路由：/
import { useState, useMemo, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { useQueries } from '@tanstack/react-query'
import { useToast } from '@/store/toast'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import {
  useWatchlist,
  usePublishedRuns,
  useStrategyRunResults,
  useWatchlistMonitorStatus,
  useNotificationChannels,
  useInstruments,
  useAddToWatchlist,
  useEventsSummary,
} from '@/hooks/useApi'
import * as api from '@/api/endpoints'
import type { Instrument, StrategyResult, WatchlistMonitorStatusItem } from '@/api/endpoints'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

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
  short_pos: string
  pos_tag: 'good' | 'warn'
  watched: boolean
  [key: string]: unknown
}

// 自选监控行（从 MonitorState.payload 派生）
interface WatchlistMonitorRow {
  instrument_id: string
  name: string
  symbol: string
  price: string
  lower_node_price: number | null
  upper_node_price: number | null
  latest_event: { event_type: string; event_time: string; boundary: number | null } | null
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

  // --- 自选列表（弹窗"已自选"判断）---
  const watchlistQuery = useWatchlist()
  const watchlistItems = watchlistQuery.data?.items ?? []
  const watchlistIds = useMemo(
    () => new Set(watchlistItems.map((w) => w.instrument_id)),
    [watchlistItems],
  )

  // --- DSA 最新运行（选股结果表 + KPI 1）---
  const dsaRunsQuery = usePublishedRuns(STRATEGY_KEYS.DSA_SELECTOR, { limit: 1 })
  const latestDsaRun = dsaRunsQuery.data?.items[0]
  const latestRunId = latestDsaRun?.id

  // --- DSA 运行结果（选股结果表数据）---
  const selectionResultsQuery = useStrategyRunResults(latestRunId, { limit: 20 })
  const selectionResults: StrategyResult[] = selectionResultsQuery.data?.items ?? []

  // --- 监控策略状态（右侧表格）---
  const monitorStatusQuery = useWatchlistMonitorStatus()
  const monitorStatusItems: WatchlistMonitorStatusItem[] = monitorStatusQuery.data?.items ?? []

  // --- KPI 3：今日策略事件汇总（通过 /me/events/summary API）---
  const todayStr = new Date().toISOString().slice(0, 10)
  const eventsSummaryQuery = useEventsSummary(todayStr)

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
    monitorStatusItems.forEach((s) => ids.add(s.instrument_id))
    return [...ids]
  }, [selectionResults, monitorStatusItems])

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
        short_pos: shortPos !== null ? `${Math.round(shortPos * 100)}%` : '-',
        pos_tag: shortPos !== null && shortPos > 0.7 ? 'warn' : 'good',
        watched: watchlistIds.has(r.instrument_id),
      }
    },
    [instrumentMap, watchlistIds],
  )

  /** 将 WatchlistMonitorStatusItem 转换为 WatchlistMonitorRow */
  const toMonitorRow = useCallback(
    (s: WatchlistMonitorStatusItem): WatchlistMonitorRow => {
      const inst = instrumentMap.get(s.instrument_id)
      const metrics = s.metrics ?? {}

      // 后端已返回扁平节点字段；保留对对象型字段的兼容展平
      const upperNodeVal = metrics.upper_node_price ?? metrics.upper_node
      const lowerNodeVal = metrics.lower_node_price ?? metrics.lower_node

      let upperNodePrice: number | null = null
      if (typeof upperNodeVal === 'number') {
        upperNodePrice = upperNodeVal
      } else if (upperNodeVal && typeof upperNodeVal === 'object') {
        const obj = upperNodeVal as Record<string, unknown>
        upperNodePrice = toNum(obj.price_mid ?? obj.price)
      }

      let lowerNodePrice: number | null = null
      if (typeof lowerNodeVal === 'number') {
        lowerNodePrice = lowerNodeVal
      } else if (lowerNodeVal && typeof lowerNodeVal === 'object') {
        const obj = lowerNodeVal as Record<string, unknown>
        lowerNodePrice = toNum(obj.price_mid ?? obj.price)
      }

      return {
        instrument_id: s.instrument_id,
        name: inst?.name ?? s.name ?? '-',
        symbol: inst?.symbol ?? s.symbol ?? s.instrument_id.slice(0, 8),
        price: fmtNum(metrics.current_price ?? metrics.price ?? metrics.last_price ?? metrics.close),
        lower_node_price: lowerNodePrice,
        upper_node_price: upperNodePrice,
        latest_event: s.latest_event ?? null,
      }
    },
    [instrumentMap],
  )

  // ===== 派生数据 =====

  // 选股结果行（首页最多展示 10 条）
  const selectionRows: SelectionRow[] = useMemo(
    () => selectionResults.slice(0, 10).map(toSelectionRow),
    [selectionResults, toSelectionRow],
  )

  // 监控表格行（首页最多展示 10 条）
  const monitorRows: WatchlistMonitorRow[] = useMemo(
    () => monitorStatusItems.slice(0, 10).map(toMonitorRow),
    [monitorStatusItems, toMonitorRow],
  )

  // KPI 1：今日选股结果数（最新已发布 DSA 运行的标的总数）
  const kpi1Value = latestDsaRun?.total_instruments ?? null
  const kpi1Loading = dsaRunsQuery.isLoading

  // KPI 2：监控自选股数（active 自选股数量）
  const kpi2Total = watchlistItems.filter((i) => i.active).length

  // KPI 4：通知渠道状态
  const kpi4Status = feishuChannel ? '飞书正常' : '未配置'
  const kpi4Time = fmtTime(feishuChannel?.last_verified_at)

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
        title: '方向持续',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.duration) || 0,
        render: (row) => <span className="num">{row.duration}</span>,
      },
      {
        key: 'avg_return',
        title: '平均收益',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.avg_return.replace('%', '')) || 0,
        render: (row) => <span className="num pos">{row.avg_return}</span>,
      },
      {
        key: 'total_return',
        title: '总收益',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.total_return.replace('%', '')) || 0,
        render: (row) => <span className="num pos">{row.total_return}</span>,
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

  // 监控计算表列
  const monitorColumns: DataTableColumn<WatchlistMonitorRow>[] = useMemo(
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
        title: '当前价',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.price) || 0,
        render: (row) => <span className="num">{row.price}</span>,
      },
      {
        key: 'lower_node',
        title: '下方节点',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.lower_node_price ?? 0,
        render: (row) => (
          <span className="num">
            {row.lower_node_price !== null ? row.lower_node_price.toFixed(2) : '-'}
          </span>
        ),
      },
      {
        key: 'upper_node',
        title: '上方节点',
        dataType: 'number',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.upper_node_price ?? 0,
        render: (row) => (
          <span className="num">
            {row.upper_node_price !== null ? row.upper_node_price.toFixed(2) : '-'}
          </span>
        ),
      },
      {
        key: 'latest_event',
        title: '最近触发',
        dataType: 'text',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.latest_event?.event_time ?? '',
        render: (row) => {
          if (!row.latest_event) return <span className="muted">-</span>
          const eventType = row.latest_event.event_type
          const time = fmtTime(row.latest_event.event_time)
          const boundary = row.latest_event.boundary
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
    ],
    [],
  )

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">服务总览</h1>
          <div className="page-desc">
            集中查看选股策略结果与全部监控策略最新状态
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
        {/* KPI 1：今日选股结果（最新已发布 DSA 运行的标的总数） */}
        <div className="card kpi-card">
          <div className="kpi-label">今日选股结果</div>
          <div className="kpi-value">
            {kpi1Loading ? '-' : (kpi1Value ?? '暂无')}
            {kpi1Value !== null && <small className="kpi-unit">只</small>}
          </div>
          <div className="kpi-foot">DSA 选股策略</div>
        </div>
        {/* KPI 2：监控自选股数（active 自选股数量） */}
        <div className="card kpi-card">
          <div className="kpi-label">监控自选股</div>
          <div className="kpi-value">
            {watchlistQuery.isLoading ? '-' : (watchlistQuery.isError ? '加载失败' : kpi2Total)}
            {!watchlistQuery.isLoading && !watchlistQuery.isError && <small className="kpi-unit">只</small>}
          </div>
          <div className="kpi-foot">已启用监控策略</div>
        </div>
        {/* KPI 3：今日策略事件（通过 /me/events/summary API） */}
        <div className="card kpi-card">
          <div className="kpi-label">今日策略事件</div>
          <div className="kpi-value">
            {eventsSummaryQuery.isLoading
              ? '-'
              : eventsSummaryQuery.isError
                ? '加载失败'
                : eventsSummaryQuery.data?.total_events ?? 0}
          </div>
          <div className="kpi-foot">
            {eventsSummaryQuery.data
              ? `跨 ${eventsSummaryQuery.data.instruments_with_events} 只自选股`
              : '策略事件汇总'}
          </div>
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

      {/* 选股结果 + 监控状态 */}
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

        {/* 监控策略状态 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">监控策略状态</div>
              <div className="card-sub">
                自选监控最新节点与触发事件
              </div>
            </div>
            <div className="card-head-actions">
              <Link className="btn small ghost" to="/watchlist">
                查看全部 →
              </Link>
            </div>
          </div>
          <StrategyDataTable
            tableId="index-watchlist-monitor"
            columns={monitorColumns}
            rows={monitorRows}
            rowKey={(row) => row.instrument_id}
            loading={monitorStatusQuery.isLoading}
            error={monitorStatusQuery.isError ? '监控状态加载失败' : null}
            searchable={false}
            emptyText="暂无监控计算结果"
          />
        </section>
      </div>

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
