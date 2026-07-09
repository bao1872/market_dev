// 趋势选股页（受保护路由）
// 数据流：选择 selector 策略 → 加载该策略的 published runs → 选择 run_id → 服务端筛选/排序/分页
// 路由：/screener
// 依赖 hooks：useStrategies / usePublishedRuns / useStrategyRunResults / useAddToWatchlist
import { useState, useMemo, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import { useToast } from '@/store/toast'
import {
  useStrategies,
  usePublishedRuns,
  useStrategyRunResults,
  useAddToWatchlist,
} from '@/hooks/useApi'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn, DataTableQuery } from '@/components/StrategyDataTable'
import type { StrategyResultQueryParams } from '@/api/endpoints'
import { formatShanghaiDate } from '@/utils/datetime'
import {
  adaptStrategyResultToTrendRow,
  getTrendSelectionColumns,
  pickPayload,
  toNum,
  fmtPct,
  fmtChange,
  getStockDisplay,
  type TrendSelectionRow,
} from '@/features/trend-selection'

// ===== 常量 =====
const PAGE_SIZE = 50

// ===== 类型定义 =====
// [趋势选股] - 描述: 行类型使用共享模块的 TrendSelectionRow（spec 第七节唯一实现）
// breakoutColumns/genericColumns 共用同一行类型

// ===== breakoutColumns 专用工具（趋势选股列定义已迁移至 features/trend-selection） =====

/** 格式化为字符串，未知返回 '-'（仅 breakoutColumns 使用） */
function fmtStr(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  return String(v)
}

/** 格式化为带 x 后缀的量比（仅 breakoutColumns 使用） */
function fmtRatio(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  if (Number.isNaN(n)) return String(v)
  return `${n.toFixed(2)}x`
}

// [ScreenerPage] - 描述: 后端存储为小数的收益率/offset 类指标
const RATIO_METRICS = new Set([
  'vwap_ret_avg',
  'vwap_ret_total',
  'offset_mean',
  'offset_std',
  'offset_variance_rate',
])

// [ScreenerPage] - 描述: 后端存储为 0~1 的百分位类指标
const PERCENTILE_METRICS = new Set([
  'offset_percentile',
  'short_position',
  'position_short',
  'short_pos',
])

/** 将用户输入的筛选值归一化为后端口径。
 *
 * 规则：
 * - 收益率/offset 类：用户输入 3% → 0.03；输入 0.03 → 0.03
 * - 百分位类：用户输入 80% → 0.8；输入 0.8 → 0.8
 * - 已存储为百分比的字段（如 change_pct/dsa_vwap_dev_pct）：用户输入 3% → 3
 */
function normalizeMetricValue(
  key: string,
  raw: string | number | undefined,
): number | undefined {
  if (raw === undefined || raw === null || raw === '') return undefined
  const s = String(raw).replace(/,/g, '').trim()
  const hasPercent = s.includes('%')
  const n = parseFloat(s.replace(/%/g, ''))
  if (Number.isNaN(n)) return undefined
  if (RATIO_METRICS.has(key) || PERCENTILE_METRICS.has(key)) {
    return hasPercent ? n / 100 : n
  }
  return n
}

// [趋势选股] - 描述: getStockDisplay/adaptStrategyResultToTrendRow 已迁移至 features/trend-selection

// ===== 主组件 =====
export default function ScreenerPage() {
  const navigate = useNavigate()
  const toast = useToast.getState()

  // --- 策略目录（kind=selector） ---
  const strategiesQuery = useStrategies('selector')
  const selectorStrategies = strategiesQuery.data?.items ?? []

  // --- 当前选中的策略（默认第一个） ---
  const [selectedStrategyKey, setSelectedStrategyKey] = useState<string>('')
  const activeStrategyKey = selectedStrategyKey || selectorStrategies[0]?.strategy_key || ''

  // --- 已发布的运行批次（仅最新一个快照） ---
  const runsQuery = usePublishedRuns(activeStrategyKey || undefined, { limit: 1 })
  const runs = runsQuery.data?.items ?? []

  // --- 当前选中的运行（固定为最新发布批次） ---
  const activeRunId = runs[0]?.id || ''
  const activeRun = runs[0]

  // --- 服务端分页/筛选/排序状态 ---
  const [query, setQuery] = useState<DataTableQuery>({
    page: 1,
    pageSize: PAGE_SIZE,
    filters: [],
  })

  // --- 运行结果（服务端分页） ---
  const resultParams: StrategyResultQueryParams = useMemo(() => {
    const params: StrategyResultQueryParams = {
      page: query.page,
      page_size: query.pageSize,
      // [ScreenerPage] - 描述: 默认查询全市场 universe，与 spec 第 5.5 节对齐
      universe: 'all',
    }
    if (query.sort) {
      params.sort_by = query.sort.key
      params.sort_desc = query.sort.direction === 'desc'
    }
    // [ScreenerPage] - 描述: 直接使用 globalQuery 透传的 keyword（由 StrategyDataTable 顶部搜索框传入）
    if (query.keyword) {
      params.keyword = query.keyword
    }
    // [ScreenerPage] - 描述: 列筛选转 metric_filters，between 下界从 value 映射为 value1，并按字段类型换算单位
    const supportedOps = new Set(['gt', 'gte', 'lt', 'lte', 'eq', 'between'])
    const metricFilters = query.filters
      .filter((f) => supportedOps.has(f.operator) && f.key !== 'stock' && f.key !== 'action')
      .map((f) => {
        const value = normalizeMetricValue(f.key, f.value)
        if (value === undefined) return null
        if (f.operator === 'between') {
          const value2 = normalizeMetricValue(f.key, f.value2)
          if (value2 === undefined) return null
          return {
            metric_key: f.key,
            operator: f.operator,
            value1: value,
            value2,
          }
        }
        return {
          metric_key: f.key,
          operator: f.operator,
          value,
        }
      })
      .filter((f): f is NonNullable<typeof f> => f !== null)
    if (metricFilters.length > 0) {
      params.metric_filters = JSON.stringify(metricFilters)
    }
    return params
  }, [query])

  const resultsQuery = useStrategyRunResults(activeRunId || undefined, resultParams)
  const resultItems = resultsQuery.data?.items ?? []
  const totalResults = resultsQuery.data?.total ?? 0
  const sourceTotal = resultsQuery.data?.source_total
  const filteredTotal = resultsQuery.data?.filtered_total

  // --- 行数据 ---
  // [趋势选股] - 描述: 行数据通过共享 adapter 转换（保留 payload 供列渲染动态计算）
  const rows: TrendSelectionRow[] = useMemo(
    () => resultItems.map((r) => adaptStrategyResultToTrendRow(r)),
    [resultItems],
  )

  // --- UI 状态 ---
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set())

  // --- 加入自选变更 ---
  const addWatchlistMutation = useAddToWatchlist()

  // ===== 派生数据 =====

  // 批次元数据
  const batchMeta = useMemo(() => {
    const run = activeRun
    if (!run) return null
    const dataDate = run.trade_date
      ? formatShanghaiDate(run.trade_date)
      : '-'
    const statusLabel = run.status === 'published'
      ? '已发布'
      : run.status === 'completed'
        ? '计算完成'
        : run.status === 'failed'
          ? '计算失败'
          : run.status === 'running'
            ? '计算中'
            : run.status
    const succeeded = run.succeeded_count ?? 0
    const failed = run.failed_count ?? 0
    const skipped = run.skipped_count ?? 0
    const total = run.total_instruments ?? 0
    const coverage = total > 0 ? (succeeded + skipped) / total : 0
    return {
      dataDate,
      runId: run.id.slice(0, 8),
      status: statusLabel,
      isOk: run.status === 'published' || run.status === 'completed',
      // [ScreenerPage] - 描述: 批次计数与覆盖率（spec 第 5.5 节）
      succeeded,
      failed,
      skipped,
      total,
      coverage,
      incomplete: failed > 0 || (total > 0 && succeeded + skipped !== total),
    }
  }, [activeRun])

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (row: TrendSelectionRow) => {
      const { symbol } = getStockDisplay(row)
      navigate(`/stock/${symbol}?source=selection&strategy=${activeStrategyKey}`)
    },
    [navigate, activeStrategyKey],
  )

  /** 切换策略：重置分页、选中行 */
  const handleStrategyChange = (key: string) => {
    setSelectedStrategyKey(key)
    setQuery({ page: 1, pageSize: PAGE_SIZE, filters: [] })
    setSelectedKeys(new Set())
  }

  /** 服务端查询变更 */
  const handleQueryChange = useCallback((newQuery: DataTableQuery) => {
    setQuery(newQuery)
  }, [])

  /** 批量加入自选 */
  const handleBatchAdd = async () => {
    if (selectedKeys.size === 0) return
    // [趋势选股] - 描述: 按 instrumentId 匹配（与 rowKey 一致），并对 instrumentId 去重避免重复加入
    const seen = new Set<string>()
    const selected = rows.filter((r) => {
      if (!selectedKeys.has(r.instrumentId)) return false
      if (seen.has(r.instrumentId)) return false
      seen.add(r.instrumentId)
      return true
    })
    if (selected.length === 0) {
      toast.show('批量加入自选', '无可加入的股票（选中行未包含有效 instrumentId）')
      return
    }
    let success = 0
    let fail = 0
    for (const row of selected) {
      try {
        await addWatchlistMutation.mutateAsync({
          instrument_id: row.instrumentId,
          source: 'screener',
        })
        success++
      } catch {
        fail++
      }
    }
    if (fail === 0) {
      toast.show('批量加入完成', `成功加入 ${success} 只股票到自选`)
    } else {
      toast.show('批量加入部分失败', `成功 ${success} 只，失败 ${fail} 只`)
    }
    setSelectedKeys(new Set())
  }

  // ===== 列定义 =====

  // [趋势选股] - 描述: 股票列渲染（仅 breakoutColumns/genericColumns 使用，DSA 列已用共享定义）
  // 第一行=名称+涨跌幅（涨红跌绿），第二行=代码·市场
  const renderStock = useCallback((row: TrendSelectionRow) => {
    const { name, symbol, market } = getStockDisplay(row)
    const changePct = pickPayload(row.payload, ['change_pct', 'pct_change', 'change_percent'])
    const n = toNum(changePct)
    const cls = n !== null && n > 0 ? 'market-up' : n !== null && n < 0 ? 'market-down' : 'market-flat'
    return (
      <div>
        <div className="symbol">
          {name}
          <span className={cls} style={{ marginLeft: 6 }}>{fmtChange(changePct)}</span>
        </div>
        <div className="symbol-sub">
          {symbol}
          {market ? ` · ${market}` : ''}
        </div>
      </div>
    )
  }, [])

  // [趋势选股] - 描述: DSA 列定义引用 features/trend-selection 共享模块（spec 第七节唯一真源）
  // 同 key 的 title/format/颜色规则与 IndexPage 完全一致；onDetail 由本页注入跳转逻辑
  // breakoutColumns/genericColumns 仍为本地定义（spec 第七节仅统一 dsa 策略列）
  const dsaColumns: DataTableColumn<TrendSelectionRow>[] = useMemo(
    () => getTrendSelectionColumns({ onDetail: goDetail }),
    [goDetail],
  )

  // 突破强度列
  const breakoutColumns: DataTableColumn<TrendSelectionRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: false,
        sortValue: (row) => getStockDisplay(row).name,
        filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
        render: renderStock,
      },
      {
        key: 'structure_status',
        title: '结构状态',
        dataType: 'text',
        sortable: false,
        filterable: false,
        filterValue: (row) =>
          String(pickPayload(row.payload, ['structure_status', 'struct_status']) ?? ''),
        render: (row) => {
          const v = pickPayload(row.payload, ['structure_status', 'struct_status'])
          const s = fmtStr(v)
          const cls = s.includes('突破') ? 'good' : s.includes('回踩') ? 'warn' : ''
          return <span className={`tag ${cls}`}>{s}</span>
        },
      },
      {
        key: 'volume_confirm',
        title: '量能确认',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['volume_confirm', 'vol_confirm', 'volume_ratio']) ?? 0),
        render: (row) =>
          fmtRatio(pickPayload(row.payload, ['volume_confirm', 'vol_confirm', 'volume_ratio'])),
      },
      {
        key: 'breakout_amplitude',
        title: '突破幅度',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['breakout_amplitude', 'amplitude']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['breakout_amplitude', 'amplitude'])
          const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
          return <span className={n > 0 ? 'market-up' : 'market-flat'}>{fmtPct(v)}</span>
        },
      },
      {
        key: 'pressure_distance',
        title: '压力距离',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['pressure_distance', 'pressure_dist']) ?? 0),
        render: (row) =>
          fmtPct(pickPayload(row.payload, ['pressure_distance', 'pressure_dist'])),
      },
      {
        key: 'position_risk',
        title: '位置风险',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['position_risk', 'pos_risk']) ?? 0),
        render: (row) => fmtPct(pickPayload(row.payload, ['position_risk', 'pos_risk'])),
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
            <button className="btn small" onClick={() => goDetail(row)}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [renderStock, goDetail],
  )

  // 通用列（未知策略时使用，展示 payload 中的所有数值字段）
  const genericColumns: DataTableColumn<TrendSelectionRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: false,
        sortValue: (row) => getStockDisplay(row).name,
        filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
        render: renderStock,
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
            <button className="btn small" onClick={() => goDetail(row)}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [renderStock, goDetail],
  )

  /** 根据策略 key 选择列定义 */
  const getColumns = (strategyKey: string): DataTableColumn<TrendSelectionRow>[] => {
    const k = strategyKey.toLowerCase()
    if (k.includes('dsa')) return dsaColumns
    if (k.includes('breakout')) return breakoutColumns
    return genericColumns
  }

  // ===== 渲染 =====

  const resultsLoading = resultsQuery.isLoading
  const resultsError = resultsQuery.isError
    ? '运行结果加载失败'
    : null

  const activeColumns = getColumns(activeStrategyKey)

  return (
    <div>
      {/* 页面头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">趋势选股</h1>
          <div className="page-desc">
            选择选股策略 → 查看最新发布快照，表头筛选仅作用于已发布全量结果
          </div>
        </div>
      </div>

      {/* 策略 tabs */}
      <div className="strategy-tabs-bar" data-strategy-group="selectorResult">
        {strategiesQuery.isLoading ? (
          <span className="muted">正在加载选股策略…</span>
        ) : strategiesQuery.isError ? (
          <span className="muted neg">选股策略加载失败</span>
        ) : selectorStrategies.length === 0 ? (
          <span className="muted">暂无已发布选股策略</span>
        ) : (
          selectorStrategies.map((s) => (
            <button
              key={s.strategy_key}
              className={clsx('strategy-tab', activeStrategyKey === s.strategy_key && 'active')}
              onClick={() => handleStrategyChange(s.strategy_key)}
            >
              {s.display_name}
            </button>
          ))
        )}
        <div className="toolbar-spacer" />
      </div>

      {/* 批次元数据 */}
      {batchMeta && (
        <>
          {/* [ScreenerPage] - 描述: 不完整批次顶部红色警告（spec 第 5.5 节） */}
          {batchMeta.incomplete && (
            <div
              className="batch-warning"
              style={{
                background: '#fff1f0',
                border: '1px solid #ffccc7',
                color: '#cf1322',
                padding: '8px 12px',
                borderRadius: 4,
                marginBottom: 12,
              }}
            >
              警告：当前批次计算不完整（存在失败或覆盖率不足），结果可能不代表全市场，请谨慎使用。
            </div>
          )}
          <div className="batch-meta-bar">
            <div className="batch-meta-items">
              <div className="batch-meta-item">
                <span>数据日期</span>
                <b>{batchMeta.dataDate}</b>
              </div>
              <div className="batch-meta-item">
                <span>批次</span>
                <b>{batchMeta.runId}</b>
              </div>
              <div className="batch-meta-item">
                <span>状态</span>
                <b className={batchMeta.isOk ? 'pos' : 'neg'}>{batchMeta.status}</b>
              </div>
              {sourceTotal != null && (
                <div className="batch-meta-item">
                  <span>源</span>
                  <b>{sourceTotal}</b>
                </div>
              )}
              {filteredTotal != null && (
                <div className="batch-meta-item">
                  <span>命中</span>
                  <b className="pos">{filteredTotal}</b>
                </div>
              )}
              <div className="batch-meta-item">
                <span>结果数量</span>
                <b>{totalResults}</b>
              </div>
              {/* [ScreenerPage] - 描述: 批次成功/失败/跳过计数与覆盖率（spec 第 5.5 节） */}
              {batchMeta.total > 0 && (
                <>
                  <div className="batch-meta-item">
                    <span>成功</span>
                    <b className="pos">{batchMeta.succeeded}</b>
                  </div>
                  <div className="batch-meta-item">
                    <span>跳过</span>
                    <b>{batchMeta.skipped}</b>
                  </div>
                  <div className="batch-meta-item">
                    <span>覆盖率</span>
                    <b>{(batchMeta.coverage * 100).toFixed(1)}%</b>
                  </div>
                </>
              )}
              {batchMeta.failed > 0 && (
                <div className="batch-meta-item">
                  <span>失败</span>
                  <b className="neg">{batchMeta.failed}</b>
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* 结果面板 */}
      <div className="card">
        {/* 工具栏 */}
        <div className="toolbar flush">
          <span className="muted">
            已选择 <b>{selectedKeys.size}</b> 只
          </span>
          <button
            className="btn small"
            disabled={selectedKeys.size === 0 || addWatchlistMutation.isPending}
            onClick={handleBatchAdd}
          >
            {addWatchlistMutation.isPending ? '加入中…' : '批量加入自选'}
          </button>
          <div className="toolbar-spacer" />
        </div>
        {/* 数据表 */}
        <StrategyDataTable
          key={activeRunId ? `run-${activeRunId}` : 'run-empty'}
          tableId="screener"
          strategyKey={activeStrategyKey || undefined}
          activeRunId={activeRunId}
          columns={activeColumns}
          rows={rows}
          rowKey={(row) => row.instrumentId}
          total={totalResults}
          serverSide
          onQueryChange={handleQueryChange}
          loading={resultsLoading}
          error={resultsError}
          emptyText={resultsQuery.isError ? '运行结果加载失败' : '本批次无选股结果'}
          selectable
          selectedKeys={selectedKeys}
          onSelectionChange={setSelectedKeys}
          initialPageSize={PAGE_SIZE}
          tableClassName="compact-table"
        />
      </div>
    </div>
  )
}
