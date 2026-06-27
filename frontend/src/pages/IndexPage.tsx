// 服务总览（首页，受保护路由）
// 对应原型：index.html (V1.6.3)
// 用法：集中查看选股策略结果与自选股监控最新状态
// 依赖 hooks：useWatchlist / usePublishedRuns / useStrategyRunResults /
//             useWatchlistMonitorStatus / useInstruments / useAddToWatchlist / useEventsSummary
// 路由：/
// 说明：KPI 保留 3 项（选股结果 / 监控自选股 / 今日策略事件）；
//       选股行直接复用 StrategyResult.instrument_name/instrument_symbol/instrument_market，避免 N+1 查询。
import { useState, useMemo, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { useToast } from '@/store/toast'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import {
  useWatchlist,
  usePublishedRuns,
  useStrategyRunResults,
  useWatchlistMonitorStatus,
  useInstruments,
  useAddToWatchlist,
  useEventsSummary,
} from '@/hooks/useApi'
import type { StrategyResult } from '@/api/endpoints'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import {
  WatchlistMonitorTable,
  WatchlistMonitorCards,
  adaptWatchlistMonitorStatusItem,
} from '@/features/watchlist-monitor'

// ===== 行类型定义（带索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown>）=====

// 选股结果行（从 StrategyResult.payload 派生）
interface SelectionRow {
  instrument_id: string
  name: string
  symbol: string
  market: string
  direction: string
  duration: string
  avg_return: string
  total_return: string
  offset_mean: string
  offset_std: string
  offset_percentile: string
  dsa_vwap: string
  dsa_vwap_dev_pct: string
  offset_variance_rate: string
  watched: boolean
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

/** 将 ratio 小数格式化为百分比（乘以 100），未知返回 '-' */
function fmtRatioAsPct(v: unknown, digits = 2): string {
  const n = toNum(v)
  return n === null ? '-' : `${(n * 100).toFixed(digits)}%`
}

/** 根据 dsa_dir_bars 正负返回方向标签，未知返回 '-' */
function getDsaDirection(v: unknown): string {
  const n = toNum(v)
  if (n === null) return '-'
  return n > 0 ? '多头' : n < 0 ? '空头' : '-'
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
  const instruments = instrumentsQuery.data?.items ?? []

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
            加入自选后，可直接查看统一监控状态。
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
                      添加
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

// ===== 选股结果移动端卡片 =====
interface SelectionResultCardsProps {
  rows: SelectionRow[]
  onAdd?: (instrumentId: string, name: string) => void
  addPending?: boolean
  emptyText?: string
}

function SelectionResultCards({
  rows,
  onAdd,
  addPending = false,
  emptyText = '今日暂无选股结果',
}: SelectionResultCardsProps) {
  if (rows.length === 0) {
    return <div className="empty">{emptyText}</div>
  }

  return (
    <div className="selection-result-cards">
      {rows.map((row) => (
        <div className="selection-result-card" key={row.instrument_id}>
          <div className="selection-card-head">
            <div>
              <div className="symbol">{row.name}</div>
              <div className="symbol-sub">
                {row.symbol}
                {row.market ? ` · ${row.market}` : ''}
              </div>
            </div>
            {row.watched ? (
              <span className="tag info">已自选</span>
            ) : (
              <button
                className="btn small"
                onClick={() => onAdd?.(row.instrument_id, row.name)}
                disabled={addPending}
              >
                ＋ 自选
              </button>
            )}
          </div>

          <div className="selection-card-grid">
            <div>
              <span>当前趋势</span>
              <b
                className={`tag ${
                  row.direction === '多头' ? 'good' : row.direction === '空头' ? 'warn' : ''
                }`}
              >
                {row.direction}
              </b>
            </div>
            <div>
              <span>趋势持续天数</span>
              <b className="num">{row.duration}</b>
            </div>
            <div>
              <span>日均趋势变化</span>
              <b className="num market-up">{row.avg_return}</b>
            </div>
            <div>
              <span>当前强弱位置</span>
              <b className="num">{row.offset_percentile}</b>
            </div>
          </div>
        </div>
      ))}
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

  // --- 自选股监控（右侧表格）---
  const monitorStatusQuery = useWatchlistMonitorStatus()
  const monitorStatusItems = monitorStatusQuery.data?.items ?? []

  // --- KPI 3：今日策略事件汇总（通过 /me/events/summary API）---
  const todayStr = new Date().toISOString().slice(0, 10)
  const eventsSummaryQuery = useEventsSummary(todayStr)

  // --- 加入自选变更（选股结果表"＋ 自选"按钮）---
  const addWatchlistMutation = useAddToWatchlist()

  // ===== 行转换函数 =====

  /** 将 StrategyResult 转换为 SelectionRow（直接复用结果行自带的 instrument_* 字段，避免 N+1 查询） */
  const toSelectionRow = useCallback(
    (r: StrategyResult): SelectionRow => {
      const payload = r.payload
      const dirBars = pickPayload(payload, [
        'dsa_dir_bars',
        'dsa_duration',
        'dir_duration',
        'duration',
      ])
      const dirBarsNum = toNum(dirBars)
      return {
        instrument_id: r.instrument_id,
        name: r.instrument_name ?? '-',
        symbol: r.instrument_symbol ?? r.instrument_id.slice(0, 8),
        market: r.instrument_market ?? '',
        direction: getDsaDirection(dirBars),
        duration: fmtNum(dirBarsNum !== null ? Math.abs(dirBarsNum) : null, 0),
        avg_return: fmtRatioAsPct(
          pickPayload(payload, ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return']),
        ),
        total_return: fmtRatioAsPct(
          pickPayload(payload, [
            'vwap_ret_total',
            'dsa_total_return',
            'vwap_total_return',
            'total_return',
          ]),
        ),
        offset_mean: fmtRatioAsPct(
          pickPayload(payload, ['offset_mean', 'shift_mean']),
        ),
        offset_std: fmtRatioAsPct(
          pickPayload(payload, ['offset_std', 'shift_std']),
        ),
        offset_percentile: fmtRatioAsPct(
          pickPayload(payload, [
            'offset_percentile',
            'short_position',
            'position_short',
            'short_pos',
          ]),
        ),
        dsa_vwap: fmtNum(
          pickPayload(payload, ['dsa_vwap', 'vwap', 'anchor_vwap']),
          2,
        ),
        dsa_vwap_dev_pct: fmtPct(
          pickPayload(payload, [
            'dsa_vwap_dev_pct',
            'vwap_dev_pct',
            'close_vwap_dev_pct',
          ]),
        ),
        offset_variance_rate: fmtPct(
          pickPayload(payload, [
            'offset_variance_rate',
            'offset_var_rate',
            'shift_var',
          ]),
        ),
        watched: watchlistIds.has(r.instrument_id),
      }
    },
    [watchlistIds],
  )

  // ===== 派生数据 =====

  // 选股结果行（首页最多展示 10 条）
  const selectionRows: SelectionRow[] = useMemo(
    () => selectionResults.slice(0, 10).map(toSelectionRow),
    [selectionResults, toSelectionRow],
  )

  // 自选股监控行（首页最多展示 10 条）
  const monitorRows = useMemo(
    () => monitorStatusItems.slice(0, 10).map(adaptWatchlistMonitorStatusItem),
    [monitorStatusItems],
  )

  // KPI 1：今日选股结果数（最新已发布 DSA 运行的标的总数）
  const kpi1Value = latestDsaRun?.total_instruments ?? null
  const kpi1Loading = dsaRunsQuery.isLoading

  // KPI 2：监控自选股数（active 自选股数量）
  const kpi2Total = watchlistItems.filter((i) => i.active).length

  // ===== 事件处理 =====

  /** 选股结果表"＋ 自选"按钮 */
  const handleAddToWatchlist = useCallback(
    async (instrumentId: string, name: string) => {
      try {
        await addWatchlistMutation.mutateAsync({
          instrument_id: instrumentId,
          source: 'selection',
        })
        toast.show('已加入自选', `${name} 已加入自选`)
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
        key: 'direction',
        title: '当前趋势',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => (row.direction === '多头' ? 1 : row.direction === '空头' ? -1 : 0),
        render: (row) => (
          <span
            className={`tag ${
              row.direction === '多头' ? 'good' : row.direction === '空头' ? 'warn' : ''
            }`}
          >
            {row.direction}
          </span>
        ),
      },
      {
        key: 'duration',
        title: '趋势持续天数',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.duration) || 0,
        render: (row) => <span className="num">{row.duration}</span>,
      },
      {
        key: 'avg_return',
        title: '日均趋势变化',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.avg_return.replace('%', '')) || 0,
        render: (row) => <span className="num market-up">{row.avg_return}</span>,
      },
      {
        key: 'total_return',
        title: '本轮趋势涨跌',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.total_return.replace('%', '')) || 0,
        render: (row) => <span className="num market-up">{row.total_return}</span>,
      },
      {
        key: 'offset_mean',
        title: '平均偏离趋势线',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.offset_mean.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.offset_mean}</span>,
      },
      {
        key: 'offset_std',
        title: '趋势附近波动幅度',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.offset_std.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.offset_std}</span>,
      },
      {
        key: 'offset_percentile',
        title: '当前强弱位置',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.offset_percentile.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.offset_percentile}</span>,
      },
      {
        key: 'dsa_vwap',
        title: '趋势参考价',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.dsa_vwap) || 0,
        render: (row) => <span className="num">{row.dsa_vwap}</span>,
      },
      {
        key: 'dsa_vwap_dev_pct',
        title: '距趋势参考价',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.dsa_vwap_dev_pct.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.dsa_vwap_dev_pct}</span>,
      },
      {
        key: 'offset_variance_rate',
        title: '趋势波动系数',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(row.offset_variance_rate.replace('%', '')) || 0,
        render: (row) => <span className="num">{row.offset_variance_rate}</span>,
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

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">服务总览</h1>
          <div className="page-desc">
            集中查看选股策略结果与自选股监控最新状态
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

      {/* KPI 卡片（3 项：DSA 因子结果 / 监控自选股 / 今日策略事件） */}
      <div className="grid kpi">
        {/* KPI 1：DSA 因子结果（最新已发布 DSA 运行的标的总数） */}
        <div className="card kpi-card">
          <div className="kpi-label">DSA 因子结果</div>
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
          <div className="kpi-foot">已启用自选监控</div>
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
      </div>

      {/* 选股结果 + 自选股监控（两列等宽） */}
      <div className="grid split-even">
        {/* 最新 DSA 因子快照 */}
        <section className="card index-main-panel">
          <div className="card-head">
            <div>
              <div className="card-title">最新 DSA 因子快照</div>
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
          {/* 桌面端表格 */}
          <div className="selection-result-table-wrap">
            <StrategyDataTable
              key={latestRunId ? `run-${latestRunId}` : 'run-empty'}
              tableId="index-selection-results"
              activeRunId={latestRunId}
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
          </div>

          {/* 移动端卡片 */}
          <div className="selection-result-cards-wrap">
            <SelectionResultCards
              rows={selectionRows}
              onAdd={handleAddToWatchlist}
              addPending={addWatchlistMutation.isPending}
              emptyText="今日暂无选股结果"
            />
          </div>
        </section>

        {/* 自选股监控 */}
        <section className="card index-main-panel">
          <div className="card-head">
            <div>
              <div className="card-title">自选股监控</div>
              <div className="card-sub">自选监控最新节点与触发事件</div>
            </div>
            <div className="card-head-actions">
              <Link className="btn small ghost" to="/watchlist">
                查看全部 →
              </Link>
            </div>
          </div>

          {/* 桌面端表格 */}
          <div className="watchlist-monitor-table-wrap">
            <WatchlistMonitorTable
              tableId="index-watchlist-monitor"
              rows={monitorRows}
              loading={monitorStatusQuery.isLoading}
              error={monitorStatusQuery.isError ? '监控状态加载失败' : null}
              searchable={false}
              emptyText="暂无监控计算结果"
              readonly
            />
          </div>

          {/* 移动端卡片 */}
          <div className="watchlist-monitor-cards-wrap">
            <WatchlistMonitorCards
              rows={monitorRows}
              emptyText="暂无监控计算结果"
              readonly
            />
          </div>
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
