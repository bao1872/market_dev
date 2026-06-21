// 选股策略页（受保护路由）
// 对应原型：screener.html (V1.6.3)
// 用法：展示用户选股组合方案，支持方案切换、组合结果与单策略明细查看、批量加入自选
// 路由：/screener
// 依赖 hooks：useSelectionPlans / useSelectionPlan / useSelectionPlanRuns / useSelectionPlanRunResults / useAddToWatchlist / useStrategies
import { useState, useMemo, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import { useToast } from '@/store/toast'
import {
  useSelectionPlans,
  useSelectionPlan,
  useSelectionPlanRuns,
  useSelectionPlanRunResults,
  useAddToWatchlist,
  useStrategies,
  usePublishedRuns,
  useStrategyRunResults,
  useBatchInstruments,
} from '@/hooks/useApi'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import type { SelectionPlanResult, StrategyResult, Instrument } from '@/api/endpoints'

// ===== 类型定义 =====

// 表格行类型（从 SelectionPlanResult 派生）
// 扩展索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown> 约束
interface ScreenerRow {
  resultId: string
  instrumentId: string
  matched: boolean
  matchedMemberIds: string[]
  rankValue: number | null
  summary: Record<string, unknown>
  [key: string]: unknown
}

// ===== summary 字段提取工具 =====
// SelectionPlanResult.summary 为 Record<string, unknown>，字段名由后端策略决定
// 按候选 key 列表取第一个非空值，兼容不同策略的命名差异

/** 从 summary 中按候选 key 列表取第一个非空值 */
function pickSummary(summary: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) {
    const v = summary[k]
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

/** 从 row 中提取股票展示信息（symbol/name/market） */
function getStockDisplay(row: ScreenerRow): { symbol: string; name: string; market: string } {
  const s = row.summary
  return {
    symbol: String(
      pickSummary(s, ['symbol', 'code', 'instrument_symbol']) ?? row.instrumentId.slice(0, 8),
    ),
    name: String(pickSummary(s, ['name', 'instrument_name', 'stock_name']) ?? '-'),
    market: String(pickSummary(s, ['market', 'board', 'exchange']) ?? ''),
  }
}

/** 将 SelectionPlanResult 转换为 ScreenerRow */
function toRow(r: SelectionPlanResult): ScreenerRow {
  return {
    resultId: r.id,
    instrumentId: r.instrument_id,
    matched: r.matched,
    matchedMemberIds: r.matched_member_ids,
    rankValue: r.rank_value,
    summary: r.summary,
  }
}

/**
 * 将 StrategyResult 转换为 ScreenerRow（降级模式：无选股方案时使用）。
 *
 * StrategyResult.payload 包含策略指标（如 dsa_dir_bars, vwap_ret_avg, offset_mean 等），
 * 需映射到 ScreenerRow.summary。无选股方案时所有结果视为 matched=true。
 */
function strategyResultToRow(r: StrategyResult): ScreenerRow {
  return {
    resultId: r.id,
    instrumentId: r.instrument_id,
    matched: true,
    matchedMemberIds: ['dsa'],
    rankValue: null,
    summary: r.payload,
  }
}

// ===== 主组件 =====
export default function ScreenerPage() {
  const navigate = useNavigate()
  const toast = useToast.getState()

  // --- 方案列表 ---
  const plansQuery = useSelectionPlans()
  const plans = plansQuery.data?.items ?? []
  const hasPlans = plans.length > 0

  // --- 当前选中方案（默认第一个） ---
  const [selectedPlanId, setSelectedPlanId] = useState<string>('')
  const activePlanId = selectedPlanId || plans[0]?.id || ''
  const planDetailQuery = useSelectionPlan(activePlanId || undefined)
  const planDetail = planDetailQuery.data
  const revision = planDetail?.current_revision_data
  const members = revision?.members ?? []
  const operator = revision?.operator ?? 'AND'
  const planName = hasPlans ? (planDetail?.name ?? '选股方案') : 'DSA 方向稳定性选股'

  // --- 策略目录（将 strategy_definition_id 映射到展示名） ---
  const strategiesQuery = useStrategies('selector')
  const strategyMap = useMemo(() => {
    const m = new Map<string, { key: string; name: string }>()
    for (const s of strategiesQuery.data?.items ?? []) {
      m.set(s.id, { key: s.strategy_key, name: s.display_name })
    }
    return m
  }, [strategiesQuery.data])

  // --- 方案运行历史（用于日期下拉） ---
  const runsQuery = useSelectionPlanRuns(activePlanId || undefined, { limit: 30 })
  const runs = runsQuery.data?.items ?? []

  // --- 降级模式：无选股方案时，使用 published-runs API（普通用户可访问） ---
  const strategyRunsQuery = usePublishedRuns(hasPlans ? undefined : 'dsa_selector', { limit: 30 })
  const strategyRuns = strategyRunsQuery.data?.items ?? []

  // --- 当前选中运行（默认最新一条） ---
  const [selectedRunId, setSelectedRunId] = useState<string>('')
  // 有选股方案时用 selection_plan_runs，无方案时用 strategy_runs
  const activeRunId = selectedRunId
    || (hasPlans ? runs[0]?.id : strategyRuns[0]?.id)
    || ''
  const activeRun = hasPlans
    ? runs.find((r) => r.id === activeRunId)
    : strategyRuns.find((r) => r.id === activeRunId)

  // --- 运行结果（全量加载，客户端按 tab 过滤） ---
  // 有选股方案时用 selection_plan_run_results，无方案时用 strategy_run_results
  const planResultsQuery = useSelectionPlanRunResults(
    hasPlans ? activeRunId || undefined : undefined,
    { matched_only: false, limit: 200 },
  )
  const strategyResultsQuery = useStrategyRunResults(
    hasPlans ? undefined : activeRunId || undefined,
    { limit: 200 },
  )
  const allResults: SelectionPlanResult[] = planResultsQuery.data?.items ?? []
  const allStrategyResults: StrategyResult[] = strategyResultsQuery.data?.items ?? []

  // --- 降级模式：根据 strategy_results 的 instrument_id 列表批量查询股票主数据 ---
  // 避免加载全量 8268 只股票（page_size 上限 100），改为按需批量查询
  const instrumentIds = useMemo(
    () => (hasPlans ? undefined : allStrategyResults.map((r) => r.instrument_id)),
    [hasPlans, allStrategyResults],
  )
  const instrumentsQuery = useBatchInstruments(instrumentIds)
  const instrumentMap = useMemo(() => {
    const m = new Map<string, Instrument>()
    for (const inst of instrumentsQuery.data?.items ?? []) {
      m.set(inst.id, inst)
    }
    return m
  }, [instrumentsQuery.data])

  const allRows: ScreenerRow[] = useMemo(() => {
    if (hasPlans) {
      return allResults.map(toRow)
    }
    // 降级模式：将 StrategyResult 转换为 ScreenerRow，并补充 instrument 信息
    return allStrategyResults.map((r) => {
      const row = strategyResultToRow(r)
      const inst = instrumentMap.get(r.instrument_id)
      if (inst) {
        row.summary.symbol = inst.symbol
        row.summary.name = inst.name
        row.summary.market = inst.market
      }
      return row
    })
  }, [hasPlans, allResults, allStrategyResults, instrumentMap])

  // --- UI 状态 ---
  const [activeTab, setActiveTab] = useState<string>('selectorCombined')
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set())

  // --- 加入自选变更 ---
  const addWatchlistMutation = useAddToWatchlist()

  // ===== 派生数据 =====

  // 组合命中结果（matched=true）
  const combinedRows = useMemo(() => allRows.filter((r) => r.matched), [allRows])

  // 按成员过滤的命中结果
  const getMemberRows = useCallback(
    (memberId: string) => allRows.filter((r) => r.matchedMemberIds.includes(memberId)),
    [allRows],
  )

  // 成员展示信息列表
  const memberInfos = useMemo(() => {
    return members.map((m, i) => {
      const strategy = strategyMap.get(m.strategy_definition_id)
      return {
        member: m,
        index: i,
        strategyKey: strategy?.key ?? '',
        strategyName: strategy?.name ?? `策略 ${i + 1}`,
        rows: getMemberRows(m.id),
      }
    })
  }, [members, strategyMap, getMemberRows])

  // 成员名称列表（用于 ribbon 展示）
  const memberNames = memberInfos.map((m) => m.strategyName)

  // 计算完成时间
  const finishedTime = activeRun?.finished_at
    ? new Date(activeRun.finished_at).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '-'

  // ===== 事件处理 =====

  /** 跳转个股详情 */
  const goDetail = useCallback(
    (row: ScreenerRow, strategy: string) => {
      const { symbol } = getStockDisplay(row)
      navigate(`/stock/${symbol}?source=selection&strategy=${strategy}`)
    },
    [navigate],
  )

  /** 切换方案：重置运行选择、tab、选中行 */
  const handlePlanChange = (id: string) => {
    setSelectedPlanId(id)
    setSelectedRunId('')
    setActiveTab('selectorCombined')
    setSelectedKeys(new Set())
  }

  /** 切换运行日期：重置选中行 */
  const handleRunChange = (id: string) => {
    setSelectedRunId(id)
    setSelectedKeys(new Set())
  }

  /** 批量加入自选 */
  const handleBatchAdd = async () => {
    if (selectedKeys.size === 0) return
    const selected = combinedRows.filter((r) => selectedKeys.has(r.resultId))
    let success = 0
    let fail = 0
    for (const row of selected) {
      try {
        await addWatchlistMutation.mutateAsync({
          instrument_id: row.instrumentId,
          source: 'selection',
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

  // 组合结果列
  const combinedColumns: DataTableColumn<ScreenerRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => getStockDisplay(row).name,
        filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
        render: renderStock,
      },
      {
        key: 'comboStatus',
        title: '组合状态',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => {
          if (!hasPlans) {
            // 降级模式：单策略结果，全部命中
            return <span className="status-pill ok">DSA</span>
          }
          return (
            <span className="status-pill ok">
              {row.matchedMemberIds.length}/{members.length} 满足
            </span>
          )
        },
      },
      {
        key: 'dsaDuration',
        title: 'dir 持续',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration']) ?? 0),
        render: (row) =>
          fmtNum(pickSummary(row.summary, ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration']), 0),
      },
      {
        key: 'dsaAvgReturn',
        title: 'VWAP 平均收益',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return']) ?? 0),
        render: (row) => {
          const v = pickSummary(row.summary, ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return'])
          const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
          return <span className={n > 0 ? 'pos' : ''}>{fmtPct(v)}</span>
        },
      },
      {
        key: 'offsetMean',
        title: 'offset_mean',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(pickSummary(row.summary, ['offset_mean', 'shift_mean']) ?? 0),
        render: (row) => fmtPct(pickSummary(row.summary, ['offset_mean', 'shift_mean'])),
      },
      {
        key: 'offsetVarRate',
        title: '偏移方差率',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(
            pickSummary(row.summary, ['offset_variance_rate', 'offset_var_rate', 'shift_var']) ?? 0,
          ),
        render: (row) =>
          fmtPct(
            pickSummary(row.summary, ['offset_variance_rate', 'offset_var_rate', 'shift_var']),
          ),
      },
      {
        key: 'shortPosition',
        title: '短期位置',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(
            pickSummary(row.summary, ['offset_percentile', 'short_position', 'position_short', 'short_pos']) ?? 0,
          ),
        render: (row) =>
          fmtPct(pickSummary(row.summary, ['offset_percentile', 'short_position', 'position_short', 'short_pos'])),
      },
      {
        key: 'price',
        title: '现价',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['last_close', 'price', 'current_price', 'close']) ?? 0),
        render: (row) => fmtNum(pickSummary(row.summary, ['last_close', 'price', 'current_price', 'close'])),
      },
      {
        key: 'changePct',
        title: '涨跌幅',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['change_pct', 'pct_change', 'change_percent']) ?? 0),
        render: (row) => {
          const v = pickSummary(row.summary, ['change_pct', 'pct_change', 'change_percent'])
          const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
          return (
            <span className={n > 0 ? 'pos' : n < 0 ? 'neg' : ''}>{fmtChange(v)}</span>
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
            <button className="btn small" onClick={() => goDetail(row, 'combined')}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [members.length, hasPlans, renderStock, goDetail],
  )

  // DSA 明细列
  const dsaDetailColumns: DataTableColumn<ScreenerRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => getStockDisplay(row).name,
        filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
        render: renderStock,
      },
      {
        key: 'dsaDuration',
        title: 'dir 持续',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['dsa_dir_bars', 'dir_duration', 'dsa_duration', 'duration']) ?? 0),
        render: (row) =>
          fmtNum(pickSummary(row.summary, ['dsa_dir_bars', 'dir_duration', 'dsa_duration', 'duration']), 0),
      },
      {
        key: 'dsaAvgReturn',
        title: 'VWAP 平均收益',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['vwap_ret_avg', 'vwap_avg_return', 'avg_return', 'dsa_avg_return']) ?? 0),
        render: (row) => {
          const v = pickSummary(row.summary, ['vwap_ret_avg', 'vwap_avg_return', 'avg_return', 'dsa_avg_return'])
          const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
          return <span className={n > 0 ? 'pos' : ''}>{fmtPct(v)}</span>
        },
      },
      {
        key: 'vwapTotalReturn',
        title: 'VWAP 总收益',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(
            pickSummary(row.summary, [
              'vwap_ret_total',
              'vwap_total_return',
              'total_return',
              'dsa_total_return',
            ]) ?? 0,
          ),
        render: (row) => {
          const v = pickSummary(row.summary, [
            'vwap_ret_total',
            'vwap_total_return',
            'total_return',
            'dsa_total_return',
          ])
          const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
          return <span className={n > 0 ? 'pos' : ''}>{fmtPct(v)}</span>
        },
      },
      {
        key: 'offsetMean',
        title: 'offset_mean',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) => Number(pickSummary(row.summary, ['offset_mean', 'shift_mean']) ?? 0),
        render: (row) => fmtPct(pickSummary(row.summary, ['offset_mean', 'shift_mean'])),
      },
      {
        key: 'offsetVarRate',
        title: '偏移方差率',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['offset_variance_rate', 'offset_var_rate', 'shift_var']) ?? 0),
        render: (row) => fmtPct(pickSummary(row.summary, ['offset_variance_rate', 'offset_var_rate', 'shift_var'])),
      },
      {
        key: 'shortPosition',
        title: '短期位置',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(
            pickSummary(row.summary, ['offset_percentile', 'short_position', 'position_short', 'short_pos']) ?? 0,
          ),
        render: (row) =>
          fmtPct(pickSummary(row.summary, ['offset_percentile', 'short_position', 'position_short', 'short_pos'])),
      },
      {
        key: 'inCombo',
        title: '是否进入组合',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => {
          if (row.matched) return <span className="tag good">是</span>
          const missed = members.filter((m) => !row.matchedMemberIds.includes(m.id))
          const missedNames = missed.map(
            (m) => strategyMap.get(m.strategy_definition_id)?.name ?? '其他策略',
          )
          return <span className="tag">否 · 未通过 {missedNames.join('、')}</span>
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
            <button className="btn small" onClick={() => goDetail(row, 'dsa')}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [members, strategyMap, renderStock, goDetail],
  )

  // 突破强度明细列
  const breakoutDetailColumns: DataTableColumn<ScreenerRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => getStockDisplay(row).name,
        filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
        render: renderStock,
      },
      {
        key: 'structureStatus',
        title: '结构状态',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) =>
          String(pickSummary(row.summary, ['structure_status', 'struct_status']) ?? ''),
        render: (row) => {
          const v = pickSummary(row.summary, ['structure_status', 'struct_status'])
          const s = fmtStr(v)
          // 突破确认/突破 → good；回踩确认/回踩 → warn
          const cls = s.includes('突破') ? 'good' : s.includes('回踩') ? 'warn' : ''
          return <span className={`tag ${cls}`}>{s}</span>
        },
      },
      {
        key: 'volumeConfirm',
        title: '量能确认',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(
            pickSummary(row.summary, ['volume_confirm', 'vol_confirm', 'volume_ratio']) ?? 0,
          ),
        render: (row) =>
          fmtRatio(
            pickSummary(row.summary, ['volume_confirm', 'vol_confirm', 'volume_ratio']),
          ),
      },
      {
        key: 'breakoutAmplitude',
        title: '突破幅度',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['breakout_amplitude', 'amplitude']) ?? 0),
        render: (row) => {
          const v = pickSummary(row.summary, ['breakout_amplitude', 'amplitude'])
          const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''))
          return <span className={n > 0 ? 'pos' : ''}>{fmtPct(v)}</span>
        },
      },
      {
        key: 'pressureDistance',
        title: '压力距离',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['pressure_distance', 'pressure_dist']) ?? 0),
        render: (row) =>
          fmtPct(pickSummary(row.summary, ['pressure_distance', 'pressure_dist'])),
      },
      {
        key: 'positionRisk',
        title: '位置风险',
        dataType: 'percent',
        sortable: true,
        filterable: true,
        sortValue: (row) =>
          Number(pickSummary(row.summary, ['position_risk', 'pos_risk']) ?? 0),
        render: (row) => fmtPct(pickSummary(row.summary, ['position_risk', 'pos_risk'])),
      },
      {
        key: 'inCombo',
        title: '是否进入组合',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => {
          if (row.matched) return <span className="tag good">是</span>
          const missed = members.filter((m) => !row.matchedMemberIds.includes(m.id))
          const missedNames = missed.map(
            (m) => strategyMap.get(m.strategy_definition_id)?.name ?? '其他策略',
          )
          return <span className="tag">否 · 未通过 {missedNames.join('、')}</span>
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
            <button className="btn small" onClick={() => goDetail(row, 'breakout')}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [members, strategyMap, renderStock, goDetail],
  )

  // 通用明细列（未知策略时使用）
  const genericDetailColumns: DataTableColumn<ScreenerRow>[] = useMemo(
    () => [
      {
        key: 'stock',
        title: '股票',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => getStockDisplay(row).name,
        filterValue: (row) => `${getStockDisplay(row).name} ${getStockDisplay(row).symbol}`,
        render: renderStock,
      },
      {
        key: 'rankValue',
        title: '排名值',
        dataType: 'number',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.rankValue ?? 0,
        render: (row) => fmtNum(row.rankValue),
      },
      {
        key: 'inCombo',
        title: '是否进入组合',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) =>
          row.matched ? <span className="tag good">是</span> : <span className="tag">否</span>,
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
            <button className="btn small" onClick={() => goDetail(row, 'member')}>
              详情
            </button>
          </div>
        ),
      },
    ],
    [renderStock, goDetail],
  )

  /** 根据策略 key 选择明细列 */
  const getDetailColumns = (strategyKey: string): DataTableColumn<ScreenerRow>[] => {
    const k = strategyKey.toLowerCase()
    if (k.includes('dsa')) return dsaDetailColumns
    if (k.includes('breakout')) return breakoutDetailColumns
    return genericDetailColumns
  }

  // ===== 渲染 =====

  const plansLoading = plansQuery.isLoading
  const resultsLoading = hasPlans ? planResultsQuery.isLoading : strategyResultsQuery.isLoading
  const resultsError = (hasPlans ? planResultsQuery.isError : strategyResultsQuery.isError)
    ? '运行结果加载失败，请稍后重试'
    : null

  return (
    <div>
      {/* 页面头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">选股策略</h1>
          <div className="page-desc">
            用户以"组合方案"使用一个或多个选股策略；可切换查看最终组合结果与单策略明细
          </div>
        </div>
        <div className="actions">
          <Link className="btn" to="/strategy-plan-editor">
            编辑当前组合方案
          </Link>
          <Link className="btn primary" to="/strategy-plan-editor?mode=new">
            ＋ 新建组合方案
          </Link>
        </div>
      </div>

      {/* 方案切换栏 */}
      <div className="plan-switch-bar">
        <div>
          <span className="muted">当前方案</span>
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
                  {p.name} · {p.current_revision} 版
                </option>
              ))}
            </select>
          )}
        </div>
        {/* 组合成分 chips */}
        {memberInfos.length > 0 && (
          <div className="plan-composition">
            {memberInfos.map((mi, i) => (
              <span key={mi.member.id} className="plan-composition-item">
                <span className={clsx('chip', i % 2 === 0 ? 'blue' : 'violet')}>
                  {mi.strategyName}
                </span>
                {i < memberInfos.length - 1 && (
                  <span className="combo-token">{operator}</span>
                )}
              </span>
            ))}
          </div>
        )}
        <div className="toolbar-spacer" />
        <span className="status-pill ok">每日推送已启用</span>
      </div>

      {/* 策略 tabs */}
      <div className="strategy-tabs-bar" data-strategy-group="selectorResult">
        <button
          className={clsx('strategy-tab', activeTab === 'selectorCombined' && 'active')}
          onClick={() => setActiveTab('selectorCombined')}
        >
          组合结果 <small>{combinedRows.length}</small>
        </button>
        {memberInfos.map((mi) => {
          const tabId = `selectorMember_${mi.member.id}`
          return (
            <button
              key={mi.member.id}
              className={clsx('strategy-tab', activeTab === tabId && 'active')}
              onClick={() => setActiveTab(tabId)}
            >
              {mi.strategyName} 明细 <small>{mi.rows.length}</small>
            </button>
          )
        })}
        {/* 降级模式：无选股方案时显示 DSA 明细 tab */}
        {!hasPlans && (
          <button
            className={clsx('strategy-tab', activeTab === 'selectorDsaDetail' && 'active')}
            onClick={() => setActiveTab('selectorDsaDetail')}
          >
            DSA 明细 <small>{allRows.length}</small>
          </button>
        )}
        <button
          className={clsx('strategy-tab', activeTab === 'selectorFuture' && 'active')}
          onClick={() => setActiveTab('selectorFuture')}
        >
          ＋ 更多策略
        </button>
        <div className="toolbar-spacer" />
        {/* 日期下拉 - 无条件渲染，无运行记录时显示禁用状态（对齐原型 V1.6.3） */}
        {/* 有选股方案时用 selection_plan_runs，无方案时降级到 strategy_runs */}
        {(() => {
          const dropdownRuns = hasPlans ? runs : strategyRuns
          return (
            <select
              className="select"
              value={activeRunId}
              onChange={(e) => handleRunChange(e.target.value)}
              disabled={dropdownRuns.length === 0}
            >
              {dropdownRuns.length === 0 ? (
                <option value="">暂无运行记录</option>
              ) : (
                dropdownRuns.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.trade_date?.slice(0, 10) ?? ''}
                  </option>
                ))
              )}
            </select>
          )
        })()}
      </div>

      {/* 组合结果面板 */}
      <div
        id="selectorCombined"
        className={clsx('strategy-panel', activeTab === 'selectorCombined' && 'active')}
      >
        <div className="strategy-ribbon">
          <div>
            <div className="strategy-ribbon-title">{planName} · 最终组合结果</div>
            <div className="strategy-ribbon-meta">
              {memberNames.length > 0
                ? `${memberNames.join(' 与 ')} 同时满足（${operator}）· 计算完成于 ${finishedTime}`
                : `计算完成于 ${finishedTime}`}
            </div>
          </div>
          <div className="actions">
            <Link className="btn small" to="/strategy-plan-editor">
              查看组合逻辑
            </Link>
          </div>
        </div>
        <div className="card">
          {/* 命中说明卡 */}
          <div className="card-head">
            <div>
              <div className="card-title">组合命中说明</div>
              <div className="card-sub">
                最终 {combinedRows.length} 只
                {memberInfos.map((mi) => ` · ${mi.strategyName} ${mi.rows.length} 只`).join('')}
              </div>
            </div>
            <div className="chip-row">
              <span className="chip blue">全部策略满足</span>
              <span className="chip">{members.length} 个策略</span>
              <span className="chip green">推送前 20 只</span>
            </div>
          </div>
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
            <span className="muted">表头支持排序与逐列过滤</span>
          </div>
          {/* 数据表 */}
          <StrategyDataTable
            tableId="screener-combined"
            columns={combinedColumns}
            rows={combinedRows}
            rowKey={(row) => row.resultId}
            loading={resultsLoading}
            error={resultsError}
            emptyText="暂无组合命中结果"
            selectable
            selectedKeys={selectedKeys}
            onSelectionChange={setSelectedKeys}
          />
        </div>
      </div>

      {/* 各成员明细面板 */}
      {memberInfos.map((mi) => {
        const tabId = `selectorMember_${mi.member.id}`
        return (
          <div
            key={mi.member.id}
            id={tabId}
            className={clsx('strategy-panel', activeTab === tabId && 'active')}
          >
            <div className="strategy-ribbon">
              <div>
                <div className="strategy-ribbon-title">{mi.strategyName}选股</div>
                <div className="strategy-ribbon-meta">
                  组合成员策略 {mi.index + 1}/{members.length} · 当前命中 {mi.rows.length} 只
                </div>
              </div>
              <span className="tag good">已参与组合</span>
            </div>
            <div className="card">
              <StrategyDataTable
                tableId={`screener-member-${mi.member.id}`}
                columns={getDetailColumns(mi.strategyKey)}
                rows={mi.rows}
                rowKey={(row) => row.resultId}
                loading={resultsLoading}
                error={resultsError}
                emptyText={`暂无${mi.strategyName}命中结果`}
              />
            </div>
          </div>
        )
      })}

      {/* 降级模式：DSA 明细面板（无选股方案时显示） */}
      {!hasPlans && (
        <div
          id="selectorDsaDetail"
          className={clsx('strategy-panel', activeTab === 'selectorDsaDetail' && 'active')}
        >
          <div className="strategy-ribbon">
            <div>
              <div className="strategy-ribbon-title">DSA 方向稳定性选股明细</div>
              <div className="strategy-ribbon-meta">
                共 {allRows.length} 只 · 计算完成于 {finishedTime}
              </div>
            </div>
          </div>
          <div className="card">
            <StrategyDataTable
              tableId="screener-dsa-detail"
              columns={dsaDetailColumns}
              rows={allRows}
              rowKey={(row) => row.resultId}
              loading={resultsLoading}
              error={resultsError}
              emptyText="暂无 DSA 明细结果"
            />
          </div>
        </div>
      )}

      {/* 更多策略面板 */}
      <div
        id="selectorFuture"
        className={clsx('strategy-panel', activeTab === 'selectorFuture' && 'active')}
      >
        <div className="card">
          <div className="empty">
            <h3>组合方案可继续加入其他选股策略</h3>
            <p>新增策略后由 Manifest 定义参数、结果字段和可组合能力。</p>
            <Link className="btn primary" to="/strategy-plan-editor">
              前往编辑组合方案
            </Link>
          </div>
        </div>
      </div>
    </div>
  )
}
