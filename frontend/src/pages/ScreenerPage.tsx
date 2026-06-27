// 选股策略页（受保护路由）
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
import type { StrategyResult, StrategyResultQueryParams } from '@/api/endpoints'
import { formatShanghaiDate } from '@/utils/datetime'

// ===== 常量 =====
const PAGE_SIZE = 50

// ===== 类型定义 =====

// 表格行类型（从 StrategyResult 派生，含 instrument 级字段）
interface ScreenerRow {
  resultId: string
  instrumentId: string
  symbol: string
  name: string
  market: string
  payload: Record<string, unknown>
  [key: string]: unknown
}

// ===== summary 字段提取工具 =====

/** 从 payload 中按候选 key 列表取第一个非空值 */
function pickPayload(payload: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) {
    const v = payload[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}

/** 格式化为字符串，未知返回 '-' */
function fmtStr(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  return String(v)
}

/** 格式化为百分比字符串（不带正负号） */
function fmtPct(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  if (Number.isNaN(n)) return String(v)
  return `${n.toFixed(2)}%`
}

/** 格式化为涨跌幅字符串（正数带 + 号） */
function fmtChange(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  if (Number.isNaN(n)) return String(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`
}

/** 格式化为数值字符串（保留指定小数位） */
function fmtNum(v: unknown, digits = 2): string {
  if (v === undefined || v === null || v === '') return '-'
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  if (Number.isNaN(n)) return String(v)
  return n.toFixed(digits)
}

/** 格式化为带 x 后缀的量比 */
function fmtRatio(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  if (Number.isNaN(n)) return String(v)
  return `${n.toFixed(2)}x`
}

/** 转换为数字，失败返回 null */
function toNum(v: unknown): number | null {
  if (v === undefined || v === null || v === '') return null
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  return Number.isNaN(n) ? null : n
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

/** 从 row 中提取股票展示信息（优先使用 instrument 级字段，回退到 payload） */
function getStockDisplay(row: ScreenerRow): { symbol: string; name: string; market: string } {
  if (row.symbol !== '-' && row.name !== '-') {
    return { symbol: row.symbol, name: row.name, market: row.market }
  }
  const p = row.payload
  return {
    symbol: String(
      pickPayload(p, ['symbol', 'code', 'instrument_symbol']) ?? row.instrumentId.slice(0, 8),
    ),
    name: String(pickPayload(p, ['name', 'instrument_name', 'stock_name']) ?? '-'),
    market: String(pickPayload(p, ['market', 'board', 'exchange']) ?? ''),
  }
}

/** 将 StrategyResult 转换为 ScreenerRow */
function toRow(r: StrategyResult): ScreenerRow {
  return {
    resultId: r.id,
    instrumentId: r.instrument_id,
    symbol: r.instrument_symbol ?? '-',
    name: r.instrument_name ?? '-',
    market: r.instrument_market ?? '',
    payload: r.payload,
  }
}

// [ScreenerPage] - 描述: 策略通俗说明（保留策略名 + 增加一句通俗解释，普通用户友好）
function getStrategyHint(strategyKey: string): string {
  const k = strategyKey.toLowerCase()
  if (k.includes('dsa')) return '以下股票符合近期趋势特征，可关注其趋势延续性'
  if (k.includes('breakout')) return '以下股票出现突破信号，可关注其突破有效性'
  return '以下为策略选出的股票，可进一步查看详情'
}

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

  // --- 已发布的运行批次 ---
  const runsQuery = usePublishedRuns(activeStrategyKey || undefined, { limit: 30 })
  const runs = runsQuery.data?.items ?? []

  // --- 当前选中的运行（默认最新一条） ---
  const [selectedRunId, setSelectedRunId] = useState<string>('')
  const activeRunId = selectedRunId || runs[0]?.id || ''
  const activeRun = runs.find((r) => r.id === activeRunId)

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
    }
    if (query.sort) {
      params.sort_by = query.sort.key
      params.sort_desc = query.sort.direction === 'desc'
    }
    // [ScreenerPage] - 描述: 直接使用 globalQuery 透传的 keyword（由 StrategyDataTable 顶部搜索框传入）
    if (query.keyword) {
      params.keyword = query.keyword
    }
    return params
  }, [query])

  const resultsQuery = useStrategyRunResults(activeRunId || undefined, resultParams)
  const resultItems = resultsQuery.data?.items ?? []
  const totalResults = resultsQuery.data?.total ?? 0
  const sourceTotal = resultsQuery.data?.source_total
  const filteredTotal = resultsQuery.data?.filtered_total

  // --- 行数据 ---
  const rows: ScreenerRow[] = useMemo(
    () => resultItems.map(toRow),
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
    return {
      dataDate,
      runId: run.id.slice(0, 8),
      status: statusLabel,
      isOk: run.status === 'published' || run.status === 'completed',
    }
  }, [activeRun])

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (row: ScreenerRow) => {
      const { symbol } = getStockDisplay(row)
      navigate(`/stock/${symbol}?source=screener&strategy=${activeStrategyKey}`)
    },
    [navigate, activeStrategyKey],
  )

  /** 切换策略：重置运行选择、分页、选中行 */
  const handleStrategyChange = (key: string) => {
    setSelectedStrategyKey(key)
    setSelectedRunId('')
    setQuery({ page: 1, pageSize: PAGE_SIZE, filters: [] })
    setSelectedKeys(new Set())
  }

  /** 切换运行日期：重置分页、选中行 */
  const handleRunChange = (id: string) => {
    setSelectedRunId(id)
    setQuery((prev) => ({ ...prev, page: 1 }))
    setSelectedKeys(new Set())
  }

  /** 服务端查询变更 */
  const handleQueryChange = useCallback((newQuery: DataTableQuery) => {
    setQuery(newQuery)
  }, [])

  /** 批量加入自选 */
  const handleBatchAdd = async () => {
    if (selectedKeys.size === 0) return
    const selected = rows.filter((r) => selectedKeys.has(r.resultId))
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

  // 股票列渲染函数（复用）
  const renderStock = useCallback((row: ScreenerRow) => {
    const { name, symbol, market } = getStockDisplay(row)
    return (
      <div>
        <div className="symbol">{name}</div>
        <div className="symbol-sub">
          {symbol}
          {market ? ` · ${market}` : ''}
        </div>
      </div>
    )
  }, [])

  // DSA 方向稳定性列
  const dsaColumns: DataTableColumn<ScreenerRow>[] = useMemo(
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
        key: 'dsa_direction',
        title: '方向',
        dataType: 'text',
        sortable: false,
        filterable: false,
        helpText: '原始字段：dsa_dir_bars。专业定义：当前 DSA 趋势方向，正值为多头，负值为空头。',
        render: (row) => {
          const v = pickPayload(row.payload, ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration'])
          const dir = getDsaDirection(v)
          return (
            <span className={`tag ${dir === '多头' ? 'good' : dir === '空头' ? 'warn' : ''}`}>
              {dir}
            </span>
          )
        },
      },
      {
        key: 'dsa_dir_bars',
        title: '持续时间',
        dataType: 'number',
        sortable: true,
        filterable: false,
        helpText: '原始字段：dsa_dir_bars / dir_duration。专业定义：当前 DSA 趋势方向已持续的 K 线根数，显示绝对值。',
        sortValue: (row) =>
          Math.abs(
            Number(pickPayload(row.payload, ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration']) ?? 0),
          ),
        render: (row) => {
          const v = pickPayload(row.payload, ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration'])
          const n = toNum(v)
          return <span className="num">{n !== null ? Math.abs(n).toFixed(0) : '-'}</span>
        },
      },
      {
        key: 'vwap_ret_avg',
        title: '趋势内平均表现',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：vwap_ret_avg / dsa_avg_return。专业定义：趋势运行期间价格相对 VWAP 的平均偏离收益，反映趋势内的平均表现强度。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return'])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'pos' : n !== null && n < 0 ? 'neg' : ''}>
              {fmtRatioAsPct(v)}
            </span>
          )
        },
      },
      {
        key: 'vwap_ret_total',
        title: '趋势累计表现',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：vwap_ret_total / dsa_total_return。专业定义：趋势起点至当前的累计收益，反映趋势整体表现。',
        sortValue: (row) =>
          Number(
            pickPayload(row.payload, [
              'vwap_ret_total',
              'vwap_total_return',
              'total_return',
              'dsa_total_return',
            ]) ?? 0,
          ),
        render: (row) => {
          const v = pickPayload(row.payload, [
            'vwap_ret_total',
            'vwap_total_return',
            'total_return',
            'dsa_total_return',
          ])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'pos' : n !== null && n < 0 ? 'neg' : ''}>
              {fmtRatioAsPct(v)}
            </span>
          )
        },
      },
      {
        key: 'offset_mean',
        title: '当前偏离程度',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：offset_mean / shift_mean。专业定义：当前价格相对 DSA 锚点 VWAP 的平均偏离程度，正值表示价格高于锚点。',
        sortValue: (row) => Number(pickPayload(row.payload, ['offset_mean', 'shift_mean']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['offset_mean', 'shift_mean'])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'pos' : n !== null && n < 0 ? 'neg' : ''}>
              {fmtRatioAsPct(v)}
            </span>
          )
        },
      },
      {
        key: 'offset_std',
        title: '偏离标准差',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：offset_std / shift_std。专业定义：当前价格相对 DSA 锚点 VWAP 的偏离率标准差，反映偏离波动。',
        sortValue: (row) => Number(pickPayload(row.payload, ['offset_std', 'shift_std']) ?? 0),
        render: (row) => fmtRatioAsPct(pickPayload(row.payload, ['offset_std', 'shift_std'])),
      },
      {
        key: 'offset_percentile',
        title: '当前所处位置',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：offset_percentile / short_position。专业定义：当前偏离程度在趋势历史偏离中的百分位，0% 为最低、100% 为最高，反映当前所处相对位置。',
        sortValue: (row) =>
          Number(
            pickPayload(row.payload, ['offset_percentile', 'short_position', 'position_short', 'short_pos']) ?? 0,
          ),
        render: (row) =>
          fmtRatioAsPct(
            pickPayload(row.payload, ['offset_percentile', 'short_position', 'position_short', 'short_pos']),
          ),
      },
      {
        key: 'dsa_vwap',
        title: 'DSA VWAP',
        dataType: 'number',
        sortable: true,
        filterable: false,
        helpText: '原始字段：dsa_vwap / vwap。专业定义：当前 DSA 动态摆动锚定 VWAP 值。',
        sortValue: (row) => Number(pickPayload(row.payload, ['dsa_vwap', 'vwap', 'anchor_vwap']) ?? 0),
        render: (row) => fmtNum(pickPayload(row.payload, ['dsa_vwap', 'vwap', 'anchor_vwap']), 2),
      },
      {
        key: 'dsa_vwap_dev_pct',
        title: 'VWAP 偏离',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：dsa_vwap_dev_pct / vwap_dev_pct。专业定义：当前收盘价相对 DSA VWAP 的偏离百分比。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['dsa_vwap_dev_pct', 'vwap_dev_pct', 'close_vwap_dev_pct']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['dsa_vwap_dev_pct', 'vwap_dev_pct', 'close_vwap_dev_pct'])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'pos' : n !== null && n < 0 ? 'neg' : ''}>
              {fmtPct(v)}
            </span>
          )
        },
      },
      {
        key: 'offset_variance_rate',
        title: '趋势稳定度',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：offset_variance_rate / offset_var_rate。专业定义：偏离程度的方差率，数值越小表示价格围绕趋势线越稳定，趋势越健康。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['offset_variance_rate', 'offset_var_rate', 'shift_var']) ?? 0),
        render: (row) =>
          fmtPct(pickPayload(row.payload, ['offset_variance_rate', 'offset_var_rate', 'shift_var'])),
      },
      {
        key: 'price',
        title: '最新价格',
        dataType: 'number',
        sortable: true,
        filterable: false,
        helpText: '原始字段：last_close / price。专业定义：最新收盘价或当前价格。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['last_close', 'price', 'current_price', 'close']) ?? 0),
        render: (row) => fmtNum(pickPayload(row.payload, ['last_close', 'price', 'current_price', 'close'])),
      },
      {
        key: 'change_pct',
        title: '今日涨跌',
        dataType: 'percent',
        sortable: true,
        filterable: false,
        helpText: '原始字段：change_pct / pct_change。专业定义：当日价格相对前一交易日的涨跌百分比。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['change_pct', 'pct_change', 'change_percent']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['change_pct', 'pct_change', 'change_percent'])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'pos' : n !== null && n < 0 ? 'neg' : ''}>
              {fmtChange(v)}
            </span>
          )
        },
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

  // 突破强度列
  const breakoutColumns: DataTableColumn<ScreenerRow>[] = useMemo(
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
        filterable: false,
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
        filterable: false,
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['breakout_amplitude', 'amplitude']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['breakout_amplitude', 'amplitude'])
          const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
          return <span className={n > 0 ? 'pos' : ''}>{fmtPct(v)}</span>
        },
      },
      {
        key: 'pressure_distance',
        title: '压力距离',
        dataType: 'percent',
        sortable: true,
        filterable: false,
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
        filterable: false,
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
  const genericColumns: DataTableColumn<ScreenerRow>[] = useMemo(
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
  const getColumns = (strategyKey: string): DataTableColumn<ScreenerRow>[] => {
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
          <h1 className="page-title">DSA 因子结果</h1>
          <div className="page-desc">
            选择选股策略 → 选择运行批次 → 查看筛选结果，支持服务端排序与分页
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
        {/* 日期/批次选择 */}
        <select
          className="select"
          value={activeRunId}
          onChange={(e) => handleRunChange(e.target.value)}
          disabled={runs.length === 0 || runsQuery.isLoading}
        >
          {runsQuery.isLoading ? (
            <option value="">正在加载运行批次…</option>
          ) : runsQuery.isError ? (
            <option value="">运行批次加载失败</option>
          ) : runs.length === 0 ? (
            <option value="">暂无已发布运行批次</option>
          ) : (
            runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.trade_date?.slice(0, 10) ?? r.id.slice(0, 8)}
              </option>
            ))
          )}
        </select>
      </div>

      {/* 批次元数据 */}
      {batchMeta && (
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
          </div>
        </div>
      )}

      {/* 结果面板 */}
      <div className="card">
        {/* 策略通俗说明 */}
        {activeStrategyKey && (
          <div className="strategy-result-hint muted">
            {getStrategyHint(activeStrategyKey)}
          </div>
        )}
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
          <span className="muted">表头支持排序，悬停 ? 查看字段说明</span>
        </div>
        {/* 数据表 */}
        <StrategyDataTable
          key={activeRunId ? `run-${activeRunId}` : 'run-empty'}
          tableId={`screener-${activeStrategyKey}`}
          activeRunId={activeRunId}
          columns={activeColumns}
          rows={rows}
          rowKey={(row) => row.resultId}
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
        />
      </div>
    </div>
  )
}
