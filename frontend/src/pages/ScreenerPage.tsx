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

  // 股票列渲染函数（复用）：第一行=名称+涨跌幅（涨红跌绿），第二行=代码·市场
  const renderStock = useCallback((row: ScreenerRow) => {
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

  // 趋势稳定性筛选列
  const dsaColumns: DataTableColumn<ScreenerRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: false,
        width: 150,
        sortValue: (row) => getStockDisplay(row).name,
        filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
        render: renderStock,
      },
      {
        key: 'current_trend',
        title: '当前趋势',
        shortTitle: '趋势',
        dataType: 'text',
        sortable: true,
        filterable: false,
        width: 90,
        helpText: '原始字段：dsa_dir_bars。专业定义：当前趋势方向及持续天数，正值为上涨，负值为下跌。',
        sortValue: (row) => {
          const v = pickPayload(row.payload, ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration'])
          const n = toNum(v)
          return n === null ? 0 : Math.abs(n)
        },
        render: (row) => {
          const v = pickPayload(row.payload, ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration'])
          const n = toNum(v)
          if (n === null || n === 0) {
            return <span className="market-flat">方向未形成</span>
          }
          if (n > 0) {
            return <span className="market-up">上涨 {n.toFixed(0)}天</span>
          }
          return <span className="market-down">下跌 {Math.abs(n).toFixed(0)}天</span>
        },
      },
      {
        key: 'vwap_ret_avg',
        title: '日均趋势变化',
        shortTitle: '日均',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        width: 88,
        helpText: '原始字段：vwap_ret_avg / dsa_avg_return。专业定义：趋势运行期间价格相对趋势参考价的平均偏离收益，反映趋势内的平均表现强度。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return'])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'market-up' : n !== null && n < 0 ? 'market-down' : 'market-flat'}>
              {fmtRatioAsPct(v)}
            </span>
          )
        },
      },
      {
        key: 'vwap_ret_total',
        title: '本轮趋势涨跌',
        shortTitle: '累计',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        width: 88,
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
            <span className={n !== null && n > 0 ? 'market-up' : n !== null && n < 0 ? 'market-down' : 'market-flat'}>
              {fmtRatioAsPct(v)}
            </span>
          )
        },
      },
      {
        key: 'offset_mean',
        title: '平均偏离趋势线',
        shortTitle: '均偏',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        width: 90,
        helpText: '原始字段：offset_mean / shift_mean。专业定义：当前价格相对趋势参考价的平均偏离程度，正值表示价格高于锚点。',
        sortValue: (row) => Number(pickPayload(row.payload, ['offset_mean', 'shift_mean']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['offset_mean', 'shift_mean'])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'market-up' : n !== null && n < 0 ? 'market-down' : 'market-flat'}>
              {fmtRatioAsPct(v)}
            </span>
          )
        },
      },
      {
        key: 'offset_std',
        title: '趋势附近波动幅度',
        shortTitle: '波动',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        width: 90,
        helpText: '原始字段：offset_std / shift_std。专业定义：当前价格相对趋势参考价的偏离率标准差，反映偏离波动。',
        sortValue: (row) => Number(pickPayload(row.payload, ['offset_std', 'shift_std']) ?? 0),
        render: (row) => fmtRatioAsPct(pickPayload(row.payload, ['offset_std', 'shift_std'])),
      },
      {
        key: 'offset_percentile',
        title: '当前强弱位置',
        shortTitle: '分位',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        width: 86,
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
        title: '趋势参考价',
        shortTitle: '参考价',
        dataType: 'number',
        sortable: true,
        filterable: true,
        width: 82,
        helpText: '原始字段：dsa_vwap / vwap。专业定义：当前趋势参考价（动态摆动锚定值）。',
        sortValue: (row) => Number(pickPayload(row.payload, ['dsa_vwap', 'vwap', 'anchor_vwap']) ?? 0),
        render: (row) => fmtNum(pickPayload(row.payload, ['dsa_vwap', 'vwap', 'anchor_vwap']), 2),
      },
      {
        key: 'dsa_vwap_dev_pct',
        title: '距趋势参考价',
        shortTitle: '价差',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        width: 86,
        helpText: '原始字段：dsa_vwap_dev_pct / vwap_dev_pct。专业定义：当前收盘价相对趋势参考价的偏离百分比。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['dsa_vwap_dev_pct', 'vwap_dev_pct', 'close_vwap_dev_pct']) ?? 0),
        render: (row) => {
          const v = pickPayload(row.payload, ['dsa_vwap_dev_pct', 'vwap_dev_pct', 'close_vwap_dev_pct'])
          const n = toNum(v)
          return (
            <span className={n !== null && n > 0 ? 'market-up' : n !== null && n < 0 ? 'market-down' : 'market-flat'}>
              {fmtPct(v)}
            </span>
          )
        },
      },
      {
        key: 'offset_variance_rate',
        title: '趋势波动程度',
        shortTitle: '变异',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        width: 88,
        helpText: '原始字段：offset_variance_rate / offset_var_rate。专业定义：偏离程度的方差率，反映价格围绕趋势线的波动系数。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['offset_variance_rate', 'offset_var_rate', 'shift_var']) ?? 0),
        render: (row) =>
          fmtPct(pickPayload(row.payload, ['offset_variance_rate', 'offset_var_rate', 'shift_var'])),
      },
      {
        key: 'price',
        title: '最新价格',
        shortTitle: '现价',
        dataType: 'number',
        sortable: true,
        filterable: true,
        width: 76,
        helpText: '原始字段：last_close / price。专业定义：最新收盘价或当前价格。',
        sortValue: (row) =>
          Number(pickPayload(row.payload, ['last_close', 'price', 'current_price', 'close']) ?? 0),
        render: (row) => fmtNum(pickPayload(row.payload, ['last_close', 'price', 'current_price', 'close'])),
      },
      {
        key: 'action',
        title: '操作',
        dataType: 'text',
        sortable: false,
        filterable: false,
        width: 60,
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
          <h1 className="page-title">趋势因子结果</h1>
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
          tableClassName="compact-table"
        />
      </div>
    </div>
  )
}
