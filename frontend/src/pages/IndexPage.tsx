// 主页（首页，受保护路由）
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
import {
  WatchlistMonitorTable,
  WatchlistMonitorCards,
  adaptWatchlistMonitorStatusItem,
} from '@/features/watchlist-monitor'
import {
  adaptStrategyResultToTrendRow,
  getTrendSelectionColumns,
  visibleColumnKeys,
  INDEX_VISIBLE_COLUMN_KEYS,
  pickPayload,
  toNum,
  fmtRatioAsPct,
  changePctColorClass,
  DIR_BARS_KEYS,
  VWAP_RET_AVG_KEYS,
  OFFSET_PERCENTILE_KEYS,
  type TrendSelectionRow,
} from '@/features/trend-selection'

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
// [趋势选股] - 描述: 卡片格式与桌面列同源（共享 pickPayload/格式函数/候选 key/颜色规则）
interface SelectionResultCardsProps {
  rows: TrendSelectionRow[]
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
      {rows.map((row) => {
        // [趋势选股] - 描述: 从 payload 动态计算卡片展示字段（与桌面列同源）
        const dirBars = toNum(pickPayload(row.payload, DIR_BARS_KEYS))
        const direction = dirBars === null || dirBars === 0 ? '-' : dirBars > 0 ? '多头' : '空头'
        const duration = dirBars === null ? '-' : Math.abs(dirBars).toFixed(0)
        const avgReturn = fmtRatioAsPct(pickPayload(row.payload, VWAP_RET_AVG_KEYS))
        const avgReturnNum = toNum(pickPayload(row.payload, VWAP_RET_AVG_KEYS))
        const offsetPercentile = fmtRatioAsPct(pickPayload(row.payload, OFFSET_PERCENTILE_KEYS))
        return (
          <div className="selection-result-card" key={row.instrumentId}>
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
                  onClick={() => onAdd?.(row.instrumentId, row.name)}
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
                    direction === '多头' ? 'good' : direction === '空头' ? 'warn' : ''
                  }`}
                >
                  {direction}
                </b>
              </div>
              <div>
                <span>趋势持续天数</span>
                <b className="num">{duration}</b>
              </div>
              <div>
                <span>日均趋势变化</span>
                <b className={`num ${changePctColorClass(avgReturnNum)}`}>{avgReturn}</b>
              </div>
              <div>
                <span>当前强弱位置</span>
                <b className="num">{offsetPercentile}</b>
              </div>
            </div>
          </div>
        )
      })}
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

  // ===== 派生数据 =====

  // [趋势选股] - 描述: 选股结果行通过共享 adapter 转换（保留 payload 供列渲染动态计算）
  // 首页最多展示 10 条；watchedIds 传入以标记已自选状态
  const selectionRows: TrendSelectionRow[] = useMemo(
    () => selectionResults.slice(0, 10).map((r) => adaptStrategyResultToTrendRow(r, watchlistIds)),
    [selectionResults, watchlistIds],
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

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">主页</h1>
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

      {/* KPI 卡片（3 项：全市场股票数量 / 监控自选股 / 今日策略事件） */}
      <div className="grid kpi">
        {/* KPI 1：全市场股票数量（最新已发布 DSA 运行的标的总数） */}
        <div className="card kpi-card">
          <div className="kpi-label">全市场股票数量</div>
          <div className="kpi-value">
            {kpi1Loading ? '-' : (kpi1Value ?? '暂无')}
            {kpi1Value !== null && <small className="kpi-unit">只</small>}
          </div>
          <div className="kpi-foot">趋势选股</div>
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
        {/* 最新趋势快照 */}
        <section className="card index-main-panel">
          <div className="card-head">
            <div>
              <div className="card-title">最新趋势快照</div>
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
              columns={visibleColumnKeys(
                getTrendSelectionColumns({
                  onAddToWatchlist: (row) => handleAddToWatchlist(row.instrumentId, row.name),
                  addPending: addWatchlistMutation.isPending,
                }),
                INDEX_VISIBLE_COLUMN_KEYS,
              )}
              rows={selectionRows}
              rowKey={(row) => row.instrumentId}
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
