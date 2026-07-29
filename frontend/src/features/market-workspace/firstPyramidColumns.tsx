// [第一金字塔列注册表] - 描述: 99 个 fp_ 字段的唯一前端列定义
//
// 职责：
//   1. 定义 99 个第一金字塔扁平化字段的列定义（与后端 FP_ALL_KEYS 一一对应）
//   2. 按 8 个分组组织：快照/趋势/结构/结构事件/动量/动量事件/筹码/量能
//   3. 每列从 row.firstPyramid[key] 读取，null 统一显示 "—"
//   4. 提供默认可见键集合（约 20 个核心列）
//
// 唯一性：PRD §三 要求"前端唯一 ColumnRegistry"，本文件为第一金字塔列定义唯一实现。
// 禁止 IndexPage/MarketWorkspacePage/ScreenerPage 重新定义同字段列。
//
// 不变量：
//   - 列 key 必须与后端 first_pyramid_flatten.FP_ALL_KEYS 完全一致（99 个 fp_ 键）
//   - 列不参与服务端排序/筛选（sortable=false/filterable=false），保留现有行排序机制
//   - null 值统一渲染 "—"，不得补 0 或其他占位
//   - 方向类字段用中文标签 + A 股颜色（涨红跌绿）
//   - 分位/BB 位置用 0~1 数值显示（后端已规范）
//   - 事件类列显示"最近一次"事件，不是历史列表
//
// 数据来源：MarketStockRow.first_pyramid（来自 /market/stocks API 批量返回的 99 个 fp_ 键）

import type { ReactNode } from 'react'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import type { TrendSelectionRow } from '@/features/trend-selection/types'

// ===== 99 列 key 定义（与后端 FP_ALL_KEYS 一一对应）=====

export const FP_FIELD_GROUPS = {
  快照: [
    'fp_trade_date', 'fp_data_source', 'fp_is_stale', 'fp_calculated_at',
    'fp_run_id', 'fp_summary', 'fp_chip_available',
  ],
  趋势: [
    'fp_trend_direction', 'fp_trend_bars', 'fp_dsa_vwap_dev_pct',
    'fp_segment_change_pct', 'fp_segment_slope', 'fp_trend_strength',
    'fp_segment_start_date', 'fp_segment_end_date',
    'fp_segment_start_price', 'fp_segment_end_price', 'fp_segment_bars',
    'fp_segment_volume_ratio', 'fp_segment_amount_ratio',
    'fp_segment_avg_volume', 'fp_segment_avg_amount',
    'fp_prev_segment_volume', 'fp_prev_segment_amount',
    'fp_vwap_ret_total',
  ],
  结构: [
    'fp_swing_direction', 'fp_internal_direction', 'fp_structure_alignment',
    'fp_active_ob_count', 'fp_trailing_top', 'fp_trailing_bottom',
    'fp_distance_to_trailing_top_pct', 'fp_distance_to_trailing_bottom_pct',
  ],
  结构事件: [
    'fp_structure_event_type', 'fp_structure_event_direction',
    'fp_structure_event_level', 'fp_structure_event_freshness',
    'fp_structure_event_date', 'fp_structure_event_price',
    'fp_structure_event_volume_badge',
    'fp_latest_bos_direction', 'fp_latest_bos_freshness', 'fp_latest_bos_level',
    'fp_latest_choch_direction', 'fp_latest_choch_freshness', 'fp_latest_choch_level',
    'fp_latest_ob_direction', 'fp_latest_ob_freshness',
    'fp_latest_ob_high', 'fp_latest_ob_low',
    'fp_latest_eqh_freshness', 'fp_latest_eqh_price',
    'fp_latest_eql_freshness', 'fp_latest_eql_price',
  ],
  动量: [
    'fp_momentum_direction', 'fp_squeeze_state', 'fp_momentum_change',
    'fp_sqzmom_value', 'fp_sqzmom_prev',
    'fp_bb_position', 'fp_bb_width',
    'fp_bb_upper', 'fp_bb_middle', 'fp_bb_lower',
    'fp_squeeze_avg_volume', 'fp_release_volume_ratio',
    'fp_momentum_volume_relation',
  ],
  动量事件: [
    'fp_momentum_event_type', 'fp_momentum_event_direction',
    'fp_momentum_event_freshness', 'fp_momentum_event_date',
    'fp_momentum_event_price', 'fp_momentum_event_volume_badge',
    'fp_latest_sqz_off_freshness',
    'fp_latest_diffusion_direction', 'fp_latest_diffusion_freshness',
  ],
  筹码: [
    'fp_chip_state', 'fp_poc_price', 'fp_poc_distance_pct',
    'fp_peak_node_count', 'fp_vah_price', 'fp_val_price',
    'fp_node_event_type', 'fp_node_event_direction',
    'fp_node_event_freshness', 'fp_node_event_price',
  ],
  量能: [
    'fp_volume_badge', 'fp_volume', 'fp_amount', 'fp_turnover_rate',
    'fp_volume_ma20', 'fp_volume_ma200',
    'fp_volume_ratio20', 'fp_volume_ratio200',
    'fp_volume_percentile20', 'fp_volume_percentile200',
    'fp_volume_zscore20', 'fp_volume_zscore200',
    'fp_volume_ready',
  ],
} as const

// 扁平化的全部 99 key
export const FP_ALL_KEYS: readonly string[] = Object.values(FP_FIELD_GROUPS).flat()

// 运行期断言：模块加载时校验 99 键（防止后续编辑漏键）
if (FP_ALL_KEYS.length !== 99) {
  throw new Error(`FP_ALL_KEYS must have 99 keys, got ${FP_ALL_KEYS.length}`)
}

// ===== 默认可见键（约 20 个核心金字塔列）=====
// 设计：覆盖 8 个分组中各自最具代表性的字段；其余默认隐藏但可在列设置中开启
export const DEFAULT_FP_VISIBLE_KEYS: readonly string[] = [
  // 快照（1）
  'fp_summary',
  // 趋势（4）
  'fp_trend_direction', 'fp_trend_bars', 'fp_dsa_vwap_dev_pct', 'fp_segment_change_pct',
  // 结构（4）
  'fp_swing_direction', 'fp_internal_direction', 'fp_structure_alignment', 'fp_active_ob_count',
  // 结构事件（2）
  'fp_structure_event_type', 'fp_structure_event_freshness',
  // 动量（3）
  'fp_momentum_direction', 'fp_squeeze_state', 'fp_bb_position',
  // 动量事件（2）
  'fp_momentum_event_type', 'fp_momentum_event_freshness',
  // 筹码（2）
  'fp_chip_state', 'fp_poc_distance_pct',
  // 量能（2）
  'fp_volume_badge', 'fp_volume_ratio20',
]

// ===== 渲染工具 =====

const NULL_DISPLAY = '—'

/** 从 row.firstPyramid 取值；firstPyramid 为 null/undefined 或 key 不存在统一返回 null */
function pickFp(row: TrendSelectionRow, key: string): unknown {
  const fp = row.firstPyramid
  if (!fp || typeof fp !== 'object') return null
  const val = (fp as Record<string, unknown>)[key]
  return val === undefined || val === null ? null : val
}

function fmtNum(val: unknown, digits = 2): string {
  if (val === null || val === undefined) return NULL_DISPLAY
  const n = typeof val === 'number' ? val : Number(val)
  if (!Number.isFinite(n)) return NULL_DISPLAY
  return n.toFixed(digits)
}

function fmtPct(val: unknown, digits = 2): string {
  if (val === null || val === undefined) return NULL_DISPLAY
  const n = typeof val === 'number' ? val : Number(val)
  if (!Number.isFinite(n)) return NULL_DISPLAY
  return `${n.toFixed(digits)}%`
}

function fmtText(val: unknown): string {
  if (val === null || val === undefined) return NULL_DISPLAY
  if (typeof val === 'boolean') return val ? '是' : '否'
  return String(val)
}

function fmtDirection(val: unknown): ReactNode {
  if (val === null || val === undefined) return <span className="market-flat">{NULL_DISPLAY}</span>
  const s = String(val)
  if (s === '上行' || s === 'up' || s === 'bullish') {
    return <span className="market-up">{s === 'up' || s === 'bullish' ? '上行' : s}</span>
  }
  if (s === '下行' || s === 'down' || s === 'bearish') {
    return <span className="market-down">{s === 'down' || s === 'bearish' ? '下行' : s}</span>
  }
  return <span className="market-flat">{s}</span>
}

/** 涨红跌绿：用于百分比方向字段（如 dsa_vwap_dev_pct、distance_pct） */
function fmtSignedPct(val: unknown, digits = 2): ReactNode {
  if (val === null || val === undefined) return <span className="market-flat">{NULL_DISPLAY}</span>
  const n = typeof val === 'number' ? val : Number(val)
  if (!Number.isFinite(n)) return <span className="market-flat">{NULL_DISPLAY}</span>
  const cls = n > 0 ? 'market-up' : n < 0 ? 'market-down' : 'market-flat'
  return <span className={cls}>{n > 0 ? '+' : ''}{n.toFixed(digits)}%</span>
}

// ===== 99 列定义 =====

interface FpColumnDef {
  key: string
  title: string
  shortTitle?: string
  dataType: 'text' | 'number' | 'percent' | 'datetime'
  width?: number
  helpText?: string
  render: (row: TrendSelectionRow) => ReactNode
}

// 列定义（按分组顺序排列，便于列设置面板按分组显示）
const COLUMN_DEFS: FpColumnDef[] = [
  // ===== 快照 (7) =====
  {
    key: 'fp_trade_date',
    title: '快照交易日',
    shortTitle: '快照日',
    dataType: 'datetime',
    width: 100,
    helpText: '第一金字塔快照对应的交易日',
    render: (row) => fmtText(pickFp(row, 'fp_trade_date')),
  },
  {
    key: 'fp_data_source',
    title: '数据来源',
    shortTitle: '来源',
    dataType: 'text',
    width: 80,
    helpText: '快照数据来源（feature_snapshot）',
    render: (row) => fmtText(pickFp(row, 'fp_data_source')),
  },
  {
    key: 'fp_is_stale',
    title: '是否过期',
    shortTitle: '过期',
    dataType: 'text',
    width: 70,
    helpText: '快照交易日早于全局最新日线交易日时为 true',
    render: (row) => fmtText(pickFp(row, 'fp_is_stale')),
  },
  {
    key: 'fp_calculated_at',
    title: '计算时间',
    shortTitle: '计算于',
    dataType: 'datetime',
    width: 130,
    helpText: '快照写入时间（ISO）',
    render: (row) => fmtText(pickFp(row, 'fp_calculated_at')),
  },
  {
    key: 'fp_run_id',
    title: '快照Run ID',
    shortTitle: 'Run',
    dataType: 'text',
    width: 100,
    helpText: '归属的 snapshot run ID',
    render: (row) => fmtText(pickFp(row, 'fp_run_id')),
  },
  {
    key: 'fp_summary',
    title: '金字塔总览',
    shortTitle: '总览',
    dataType: 'text',
    width: 160,
    helpText: '第一金字塔状态总览（中文）',
    render: (row) => fmtText(pickFp(row, 'fp_summary')),
  },
  {
    key: 'fp_chip_available',
    title: '筹码是否可用',
    shortTitle: '筹码可用',
    dataType: 'text',
    width: 80,
    helpText: '筹码共识维度是否有数据',
    render: (row) => fmtText(pickFp(row, 'fp_chip_available')),
  },

  // ===== 趋势 (18) =====
  {
    key: 'fp_trend_direction',
    title: '趋势方向',
    shortTitle: '趋势',
    dataType: 'text',
    width: 80,
    helpText: 'DSA 趋势方向（上行/下行/震荡）',
    render: (row) => fmtDirection(pickFp(row, 'fp_trend_direction')),
  },
  {
    key: 'fp_trend_bars',
    title: '趋势连续天数',
    shortTitle: '连续天',
    dataType: 'number',
    width: 80,
    helpText: 'DSA 方向已确认连续 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_trend_bars'), 0),
  },
  {
    key: 'fp_dsa_vwap_dev_pct',
    title: '距DSA-VWAP',
    shortTitle: 'VWAP差',
    dataType: 'percent',
    width: 86,
    helpText: '当前价相对 DSA VWAP 偏离百分比',
    render: (row) => fmtSignedPct(pickFp(row, 'fp_dsa_vwap_dev_pct')),
  },
  {
    key: 'fp_segment_change_pct',
    title: '趋势段涨跌',
    shortTitle: '段涨跌',
    dataType: 'percent',
    width: 86,
    helpText: '当前趋势段累计涨跌幅',
    render: (row) => fmtSignedPct(pickFp(row, 'fp_segment_change_pct')),
  },
  {
    key: 'fp_segment_slope',
    title: '趋势段斜率',
    shortTitle: '斜率',
    dataType: 'number',
    width: 80,
    helpText: '当前趋势段线性回归斜率',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_slope'), 4),
  },
  {
    key: 'fp_trend_strength',
    title: '趋势强度',
    shortTitle: '强度',
    dataType: 'number',
    width: 80,
    helpText: '趋势强度综合指标',
    render: (row) => fmtNum(pickFp(row, 'fp_trend_strength'), 2),
  },
  {
    key: 'fp_segment_start_date',
    title: '趋势段起始日',
    shortTitle: '段起日',
    dataType: 'datetime',
    width: 100,
    helpText: '当前趋势段起始交易日',
    render: (row) => fmtText(pickFp(row, 'fp_segment_start_date')),
  },
  {
    key: 'fp_segment_end_date',
    title: '趋势段结束日',
    shortTitle: '段止日',
    dataType: 'datetime',
    width: 100,
    helpText: '当前趋势段结束交易日',
    render: (row) => fmtText(pickFp(row, 'fp_segment_end_date')),
  },
  {
    key: 'fp_segment_start_price',
    title: '趋势段起始价',
    shortTitle: '段起价',
    dataType: 'number',
    width: 86,
    helpText: '当前趋势段起始价格',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_start_price')),
  },
  {
    key: 'fp_segment_end_price',
    title: '趋势段结束价',
    shortTitle: '段止价',
    dataType: 'number',
    width: 86,
    helpText: '当前趋势段结束价格（≈当前价）',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_end_price')),
  },
  {
    key: 'fp_segment_bars',
    title: '趋势段bar数',
    shortTitle: '段bars',
    dataType: 'number',
    width: 80,
    helpText: '当前趋势段 bar 数量',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_bars'), 0),
  },
  {
    key: 'fp_segment_volume_ratio',
    title: '段量比',
    shortTitle: '段量比',
    dataType: 'number',
    width: 80,
    helpText: '当前段 vs 前一段成交量比',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_volume_ratio')),
  },
  {
    key: 'fp_segment_amount_ratio',
    title: '段额比',
    shortTitle: '段额比',
    dataType: 'number',
    width: 80,
    helpText: '当前段 vs 前一段成交额比',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_amount_ratio')),
  },
  {
    key: 'fp_segment_avg_volume',
    title: '段均量',
    shortTitle: '段均量',
    dataType: 'number',
    width: 90,
    helpText: '当前段平均成交量',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_avg_volume'), 0),
  },
  {
    key: 'fp_segment_avg_amount',
    title: '段均额',
    shortTitle: '段均额',
    dataType: 'number',
    width: 90,
    helpText: '当前段平均成交额',
    render: (row) => fmtNum(pickFp(row, 'fp_segment_avg_amount'), 0),
  },
  {
    key: 'fp_prev_segment_volume',
    title: '前段总量',
    shortTitle: '前段量',
    dataType: 'number',
    width: 90,
    helpText: '前一趋势段成交量总和',
    render: (row) => fmtNum(pickFp(row, 'fp_prev_segment_volume'), 0),
  },
  {
    key: 'fp_prev_segment_amount',
    title: '前段总额',
    shortTitle: '前段额',
    dataType: 'number',
    width: 90,
    helpText: '前一趋势段成交额总和',
    render: (row) => fmtNum(pickFp(row, 'fp_prev_segment_amount'), 0),
  },
  {
    key: 'fp_vwap_ret_total',
    title: '本轮VWAP收益',
    shortTitle: 'VWAP收益',
    dataType: 'percent',
    width: 90,
    helpText: '本轮趋势相对 VWAP 累计收益率',
    render: (row) => fmtSignedPct(pickFp(row, 'fp_vwap_ret_total')),
  },

  // ===== 结构 (8) =====
  {
    key: 'fp_swing_direction',
    title: '主要结构方向',
    shortTitle: '主要结构',
    dataType: 'text',
    width: 86,
    helpText: 'SMC swing bias 方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_swing_direction')),
  },
  {
    key: 'fp_internal_direction',
    title: '短线结构方向',
    shortTitle: '短线结构',
    dataType: 'text',
    width: 86,
    helpText: 'SMC internal bias 方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_internal_direction')),
  },
  {
    key: 'fp_structure_alignment',
    title: '结构对齐',
    shortTitle: '对齐',
    dataType: 'text',
    width: 70,
    helpText: '主要结构与短线方向是否一致（共振/背离）',
    render: (row) => fmtText(pickFp(row, 'fp_structure_alignment')),
  },
  {
    key: 'fp_active_ob_count',
    title: '活跃OB数',
    shortTitle: 'OB数',
    dataType: 'number',
    width: 76,
    helpText: '未 mitigated 的 Order Block 数量',
    render: (row) => fmtNum(pickFp(row, 'fp_active_ob_count'), 0),
  },
  {
    key: 'fp_trailing_top',
    title: '近期高点',
    shortTitle: '高顶',
    dataType: 'number',
    width: 80,
    helpText: '近期 swing 高点（trailing top）',
    render: (row) => fmtNum(pickFp(row, 'fp_trailing_top')),
  },
  {
    key: 'fp_trailing_bottom',
    title: '近期低点',
    shortTitle: '底',
    dataType: 'number',
    width: 80,
    helpText: '近期 swing 低点（trailing bottom）',
    render: (row) => fmtNum(pickFp(row, 'fp_trailing_bottom')),
  },
  {
    key: 'fp_distance_to_trailing_top_pct',
    title: '距高顶%',
    shortTitle: '距顶%',
    dataType: 'percent',
    width: 80,
    helpText: '当前价距近期高顶百分比',
    render: (row) => fmtSignedPct(pickFp(row, 'fp_distance_to_trailing_top_pct')),
  },
  {
    key: 'fp_distance_to_trailing_bottom_pct',
    title: '距低底%',
    shortTitle: '距底%',
    dataType: 'percent',
    width: 80,
    helpText: '当前价距近期低底百分比',
    render: (row) => fmtSignedPct(pickFp(row, 'fp_distance_to_trailing_bottom_pct')),
  },

  // ===== 结构事件 (21) =====
  {
    key: 'fp_structure_event_type',
    title: '结构事件类型',
    shortTitle: '事件',
    dataType: 'text',
    width: 90,
    helpText: '最近一次结构事件类型（BOS/CHoCH/OB_CREATED/OB_ENTERED/OB_MITIGATED/EQH/EQL）',
    render: (row) => fmtText(pickFp(row, 'fp_structure_event_type')),
  },
  {
    key: 'fp_structure_event_direction',
    title: '结构事件方向',
    shortTitle: '事件向',
    dataType: 'text',
    width: 80,
    helpText: '最近一次结构事件方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_structure_event_direction')),
  },
  {
    key: 'fp_structure_event_level',
    title: '结构事件级别',
    shortTitle: '级别',
    dataType: 'text',
    width: 70,
    helpText: 'swing 或 internal',
    render: (row) => fmtText(pickFp(row, 'fp_structure_event_level')),
  },
  {
    key: 'fp_structure_event_freshness',
    title: '结构事件新鲜度',
    shortTitle: '新鲜度',
    dataType: 'number',
    width: 80,
    helpText: '距今 bar 数（越小越新）',
    render: (row) => fmtNum(pickFp(row, 'fp_structure_event_freshness'), 0),
  },
  {
    key: 'fp_structure_event_date',
    title: '结构事件日期',
    shortTitle: '事件日',
    dataType: 'datetime',
    width: 100,
    helpText: '最近一次结构事件发生日期',
    render: (row) => fmtText(pickFp(row, 'fp_structure_event_date')),
  },
  {
    key: 'fp_structure_event_price',
    title: '结构事件价',
    shortTitle: '事件价',
    dataType: 'number',
    width: 86,
    helpText: '最近一次结构事件对应价格',
    render: (row) => fmtNum(pickFp(row, 'fp_structure_event_price')),
  },
  {
    key: 'fp_structure_event_volume_badge',
    title: '结构事件量徽标',
    shortTitle: '量徽标',
    dataType: 'text',
    width: 76,
    helpText: '事件 bar 量能徽标（放量/缩量/常态）',
    render: (row) => fmtText(pickFp(row, 'fp_structure_event_volume_badge')),
  },
  {
    key: 'fp_latest_bos_direction',
    title: '最新BOS方向',
    shortTitle: 'BOS向',
    dataType: 'text',
    width: 80,
    helpText: '最近一次 BOS 事件方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_latest_bos_direction')),
  },
  {
    key: 'fp_latest_bos_freshness',
    title: 'BOS新鲜度',
    shortTitle: 'BOS新鲜',
    dataType: 'number',
    width: 80,
    helpText: '最近 BOS 距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_bos_freshness'), 0),
  },
  {
    key: 'fp_latest_bos_level',
    title: 'BOS级别',
    shortTitle: 'BOS级',
    dataType: 'text',
    width: 70,
    helpText: '最近 BOS 级别（swing/internal）',
    render: (row) => fmtText(pickFp(row, 'fp_latest_bos_level')),
  },
  {
    key: 'fp_latest_choch_direction',
    title: '最新CHoCH方向',
    shortTitle: 'CHoCH向',
    dataType: 'text',
    width: 86,
    helpText: '最近一次 CHoCH 事件方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_latest_choch_direction')),
  },
  {
    key: 'fp_latest_choch_freshness',
    title: 'CHoCH新鲜度',
    shortTitle: 'CHoCH鲜',
    dataType: 'number',
    width: 86,
    helpText: '最近 CHoCH 距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_choch_freshness'), 0),
  },
  {
    key: 'fp_latest_choch_level',
    title: 'CHoCH级别',
    shortTitle: 'CHoCH级',
    dataType: 'text',
    width: 76,
    helpText: '最近 CHoCH 级别',
    render: (row) => fmtText(pickFp(row, 'fp_latest_choch_level')),
  },
  {
    key: 'fp_latest_ob_direction',
    title: '最新OB方向',
    shortTitle: 'OB向',
    dataType: 'text',
    width: 80,
    helpText: '最近一次 OB 生命周期事件方向（OB_CREATED/OB_ENTERED/OB_MITIGATED）',
    render: (row) => fmtDirection(pickFp(row, 'fp_latest_ob_direction')),
  },
  {
    key: 'fp_latest_ob_freshness',
    title: 'OB新鲜度',
    shortTitle: 'OB鲜',
    dataType: 'number',
    width: 80,
    helpText: '最近 OB 生命周期事件距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_ob_freshness'), 0),
  },
  {
    key: 'fp_latest_ob_high',
    title: 'OB上沿',
    shortTitle: 'OB高',
    dataType: 'number',
    width: 80,
    helpText: '最近 OB 区间上沿',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_ob_high')),
  },
  {
    key: 'fp_latest_ob_low',
    title: 'OB下沿',
    shortTitle: 'OB低',
    dataType: 'number',
    width: 80,
    helpText: '最近 OB 区间下沿',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_ob_low')),
  },
  {
    key: 'fp_latest_eqh_freshness',
    title: 'EQH新鲜度',
    shortTitle: 'EQH鲜',
    dataType: 'number',
    width: 80,
    helpText: '最近 equal high 距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_eqh_freshness'), 0),
  },
  {
    key: 'fp_latest_eqh_price',
    title: 'EQH价格',
    shortTitle: 'EQH价',
    dataType: 'number',
    width: 80,
    helpText: '最近 equal high 价格',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_eqh_price')),
  },
  {
    key: 'fp_latest_eql_freshness',
    title: 'EQL新鲜度',
    shortTitle: 'EQL鲜',
    dataType: 'number',
    width: 80,
    helpText: '最近 equal low 距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_eql_freshness'), 0),
  },
  {
    key: 'fp_latest_eql_price',
    title: 'EQL价格',
    shortTitle: 'EQL价',
    dataType: 'number',
    width: 80,
    helpText: '最近 equal low 价格',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_eql_price')),
  },

  // ===== 动量 (13) =====
  {
    key: 'fp_momentum_direction',
    title: '动量方向',
    shortTitle: '动量',
    dataType: 'text',
    width: 76,
    helpText: '扩张/收缩',
    render: (row) => fmtText(pickFp(row, 'fp_momentum_direction')),
  },
  {
    key: 'fp_squeeze_state',
    title: '挤压状态',
    shortTitle: '挤压',
    dataType: 'text',
    width: 80,
    helpText: '挤压中/已释放/无挤压',
    render: (row) => fmtText(pickFp(row, 'fp_squeeze_state')),
  },
  {
    key: 'fp_momentum_change',
    title: '动量变化',
    shortTitle: '动量变',
    dataType: 'number',
    width: 80,
    helpText: 'SQZMOM 当前值 - 前值',
    render: (row) => fmtNum(pickFp(row, 'fp_momentum_change'), 4),
  },
  {
    key: 'fp_sqzmom_value',
    title: 'SQZMOM值',
    shortTitle: 'SQZMOM',
    dataType: 'number',
    width: 86,
    helpText: 'SQZMOM 指标当前值',
    render: (row) => fmtNum(pickFp(row, 'fp_sqzmom_value'), 4),
  },
  {
    key: 'fp_sqzmom_prev',
    title: 'SQZMOM前值',
    shortTitle: 'SQZ前',
    dataType: 'number',
    width: 86,
    helpText: 'SQZMOM 上一 bar 值',
    render: (row) => fmtNum(pickFp(row, 'fp_sqzmom_prev'), 4),
  },
  {
    key: 'fp_bb_position',
    title: 'BB位置',
    shortTitle: 'BB位',
    dataType: 'number',
    width: 76,
    helpText: '当前价在布林带中位置（0~1）',
    render: (row) => fmtNum(pickFp(row, 'fp_bb_position'), 3),
  },
  {
    key: 'fp_bb_width',
    title: 'BB宽度',
    shortTitle: 'BB宽',
    dataType: 'number',
    width: 76,
    helpText: '布林带宽度',
    render: (row) => fmtNum(pickFp(row, 'fp_bb_width'), 4),
  },
  {
    key: 'fp_bb_upper',
    title: 'BB上轨',
    shortTitle: 'BB上',
    dataType: 'number',
    width: 80,
    helpText: '布林带上轨价格',
    render: (row) => fmtNum(pickFp(row, 'fp_bb_upper')),
  },
  {
    key: 'fp_bb_middle',
    title: 'BB中轨',
    shortTitle: 'BB中',
    dataType: 'number',
    width: 80,
    helpText: '布林带中轨价格',
    render: (row) => fmtNum(pickFp(row, 'fp_bb_middle')),
  },
  {
    key: 'fp_bb_lower',
    title: 'BB下轨',
    shortTitle: 'BB下',
    dataType: 'number',
    width: 80,
    helpText: '布林带下轨价格',
    render: (row) => fmtNum(pickFp(row, 'fp_bb_lower')),
  },
  {
    key: 'fp_squeeze_avg_volume',
    title: '挤压期均量',
    shortTitle: '挤均量',
    dataType: 'number',
    width: 90,
    helpText: '挤压期间平均成交量',
    render: (row) => fmtNum(pickFp(row, 'fp_squeeze_avg_volume'), 0),
  },
  {
    key: 'fp_release_volume_ratio',
    title: '释放量比',
    shortTitle: '释放比',
    dataType: 'number',
    width: 80,
    helpText: '释放 vs 挤压期量比',
    render: (row) => fmtNum(pickFp(row, 'fp_release_volume_ratio')),
  },
  {
    key: 'fp_momentum_volume_relation',
    title: '动量量能关系',
    shortTitle: '量能动量',
    dataType: 'text',
    width: 90,
    helpText: '动量与量能背离/共振',
    render: (row) => fmtText(pickFp(row, 'fp_momentum_volume_relation')),
  },

  // ===== 动量事件 (9) =====
  {
    key: 'fp_momentum_event_type',
    title: '动量事件类型',
    shortTitle: '动量事件',
    dataType: 'text',
    width: 90,
    helpText: '最近一次动量事件类型（SQZ_OFF/MOMENTUM_DIFFUSION）',
    render: (row) => fmtText(pickFp(row, 'fp_momentum_event_type')),
  },
  {
    key: 'fp_momentum_event_direction',
    title: '动量事件方向',
    shortTitle: '动量向',
    dataType: 'text',
    width: 80,
    helpText: '最近一次动量事件方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_momentum_event_direction')),
  },
  {
    key: 'fp_momentum_event_freshness',
    title: '动量事件新鲜度',
    shortTitle: '动量鲜',
    dataType: 'number',
    width: 86,
    helpText: '距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_momentum_event_freshness'), 0),
  },
  {
    key: 'fp_momentum_event_date',
    title: '动量事件日期',
    shortTitle: '动量日',
    dataType: 'datetime',
    width: 100,
    helpText: '最近一次动量事件日期',
    render: (row) => fmtText(pickFp(row, 'fp_momentum_event_date')),
  },
  {
    key: 'fp_momentum_event_price',
    title: '动量事件价',
    shortTitle: '动量价',
    dataType: 'number',
    width: 86,
    helpText: '最近一次动量事件价格',
    render: (row) => fmtNum(pickFp(row, 'fp_momentum_event_price')),
  },
  {
    key: 'fp_momentum_event_volume_badge',
    title: '动量事件量徽标',
    shortTitle: '动量量',
    dataType: 'text',
    width: 86,
    helpText: '动量事件 bar 量能徽标',
    render: (row) => fmtText(pickFp(row, 'fp_momentum_event_volume_badge')),
  },
  {
    key: 'fp_latest_sqz_off_freshness',
    title: 'SQZ_OFF新鲜度',
    shortTitle: 'SQZOFF鲜',
    dataType: 'number',
    width: 90,
    helpText: '最近 SQZ_OFF 距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_sqz_off_freshness'), 0),
  },
  {
    key: 'fp_latest_diffusion_direction',
    title: '最新扩散方向',
    shortTitle: '扩散向',
    dataType: 'text',
    width: 86,
    helpText: '最近 MOMENTUM_DIFFUSION 方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_latest_diffusion_direction')),
  },
  {
    key: 'fp_latest_diffusion_freshness',
    title: '扩散新鲜度',
    shortTitle: '扩散鲜',
    dataType: 'number',
    width: 86,
    helpText: '最近 MOMENTUM_DIFFUSION 距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_latest_diffusion_freshness'), 0),
  },

  // ===== 筹码 (10) =====
  {
    key: 'fp_chip_state',
    title: '筹码状态',
    shortTitle: '筹码',
    dataType: 'text',
    width: 100,
    helpText: 'Node Cluster 状态文字',
    render: (row) => fmtText(pickFp(row, 'fp_chip_state')),
  },
  {
    key: 'fp_poc_price',
    title: 'POC价格',
    shortTitle: 'POC',
    dataType: 'number',
    width: 80,
    helpText: 'Point of Control 价格',
    render: (row) => fmtNum(pickFp(row, 'fp_poc_price')),
  },
  {
    key: 'fp_poc_distance_pct',
    title: '距POC%',
    shortTitle: '距POC',
    dataType: 'percent',
    width: 80,
    helpText: '当前价距 POC 百分比',
    render: (row) => fmtSignedPct(pickFp(row, 'fp_poc_distance_pct')),
  },
  {
    key: 'fp_peak_node_count',
    title: '峰值节点数',
    shortTitle: '峰节点',
    dataType: 'number',
    width: 80,
    helpText: 'Node Cluster 峰值节点数量',
    render: (row) => fmtNum(pickFp(row, 'fp_peak_node_count'), 0),
  },
  {
    key: 'fp_vah_price',
    title: 'VAH价格',
    shortTitle: 'VAH',
    dataType: 'number',
    width: 80,
    helpText: 'Value Area High 价格',
    render: (row) => fmtNum(pickFp(row, 'fp_vah_price')),
  },
  {
    key: 'fp_val_price',
    title: 'VAL价格',
    shortTitle: 'VAL',
    dataType: 'number',
    width: 80,
    helpText: 'Value Area Low 价格',
    render: (row) => fmtNum(pickFp(row, 'fp_val_price')),
  },
  {
    key: 'fp_node_event_type',
    title: '节点事件类型',
    shortTitle: '节点事件',
    dataType: 'text',
    width: 90,
    helpText: '最近一次筹码节点事件类型',
    render: (row) => fmtText(pickFp(row, 'fp_node_event_type')),
  },
  {
    key: 'fp_node_event_direction',
    title: '节点事件方向',
    shortTitle: '节点向',
    dataType: 'text',
    width: 80,
    helpText: '最近一次筹码节点事件方向',
    render: (row) => fmtDirection(pickFp(row, 'fp_node_event_direction')),
  },
  {
    key: 'fp_node_event_freshness',
    title: '节点事件新鲜度',
    shortTitle: '节点鲜',
    dataType: 'number',
    width: 86,
    helpText: '距今 bar 数',
    render: (row) => fmtNum(pickFp(row, 'fp_node_event_freshness'), 0),
  },
  {
    key: 'fp_node_event_price',
    title: '节点事件价',
    shortTitle: '节点价',
    dataType: 'number',
    width: 86,
    helpText: '最近一次筹码节点事件价格',
    render: (row) => fmtNum(pickFp(row, 'fp_node_event_price')),
  },

  // ===== 量能 (13) =====
  {
    key: 'fp_volume_badge',
    title: '量能徽标',
    shortTitle: '量徽标',
    dataType: 'text',
    width: 76,
    helpText: '放量/缩量/常态',
    render: (row) => fmtText(pickFp(row, 'fp_volume_badge')),
  },
  {
    key: 'fp_volume',
    title: '成交量',
    shortTitle: '量',
    dataType: 'number',
    width: 90,
    helpText: '当日成交量',
    render: (row) => fmtNum(pickFp(row, 'fp_volume'), 0),
  },
  {
    key: 'fp_amount',
    title: '成交额',
    shortTitle: '额',
    dataType: 'number',
    width: 100,
    helpText: '当日成交额',
    render: (row) => fmtNum(pickFp(row, 'fp_amount'), 0),
  },
  {
    key: 'fp_turnover_rate',
    title: '换手率',
    shortTitle: '换手',
    dataType: 'percent',
    width: 76,
    helpText: '当日换手率',
    render: (row) => fmtPct(pickFp(row, 'fp_turnover_rate')),
  },
  {
    key: 'fp_volume_ma20',
    title: '20日均量',
    shortTitle: 'MA20量',
    dataType: 'number',
    width: 100,
    helpText: '20 日平均成交量',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_ma20'), 0),
  },
  {
    key: 'fp_volume_ma200',
    title: '200日均量',
    shortTitle: 'MA200量',
    dataType: 'number',
    width: 100,
    helpText: '200 日平均成交量',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_ma200'), 0),
  },
  {
    key: 'fp_volume_ratio20',
    title: '20日量比',
    shortTitle: '20比',
    dataType: 'number',
    width: 76,
    helpText: '当前量 / 20日均量',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_ratio20')),
  },
  {
    key: 'fp_volume_ratio200',
    title: '200日量比',
    shortTitle: '200比',
    dataType: 'number',
    width: 80,
    helpText: '当前量 / 200日均量',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_ratio200')),
  },
  {
    key: 'fp_volume_percentile20',
    title: '20日量分位',
    shortTitle: '20分位',
    dataType: 'number',
    width: 86,
    helpText: '当前量在 20 日窗口分位（0~1）',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_percentile20'), 3),
  },
  {
    key: 'fp_volume_percentile200',
    title: '200日量分位',
    shortTitle: '200分位',
    dataType: 'number',
    width: 90,
    helpText: '当前量在 200 日窗口分位（0~1）',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_percentile200'), 3),
  },
  {
    key: 'fp_volume_zscore20',
    title: '20日量Z分',
    shortTitle: '20Z',
    dataType: 'number',
    width: 80,
    helpText: '20 日窗口量 Z-score',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_zscore20'), 2),
  },
  {
    key: 'fp_volume_zscore200',
    title: '200日量Z分',
    shortTitle: '200Z',
    dataType: 'number',
    width: 80,
    helpText: '200 日窗口量 Z-score',
    render: (row) => fmtNum(pickFp(row, 'fp_volume_zscore200'), 2),
  },
  {
    key: 'fp_volume_ready',
    title: '量能就绪',
    shortTitle: '就绪',
    dataType: 'text',
    width: 70,
    helpText: '量能数据是否就绪（true/false）',
    render: (row) => fmtText(pickFp(row, 'fp_volume_ready')),
  },
]

// 运行期断言：列数必须 = 99
if (COLUMN_DEFS.length !== 99) {
  throw new Error(`COLUMN_DEFS must have 99 entries, got ${COLUMN_DEFS.length}`)
}

// ===== 导出：唯一列定义函数 =====

/**
 * 获取第一金字塔 99 列定义（唯一实现）。
 *
 * 列不参与 sortable/filterable（保留 false），保留现有行排序机制；
 * 列设置面板按 FP_FIELD_GROUPS 分组显示，支持隐藏/拖拽排序；
 * 默认可见键见 DEFAULT_FP_VISIBLE_KEYS（约 20 个核心列）。
 */
export function getFirstPyramidColumns(): DataTableColumn<TrendSelectionRow>[] {
  return COLUMN_DEFS.map((def) => ({
    key: def.key,
    title: def.title,
    shortTitle: def.shortTitle,
    dataType: def.dataType,
    sortable: false,
    filterable: false,
    width: def.width,
    helpText: def.helpText,
    render: def.render,
  }))
}

/**
 * 获取默认隐藏键集合 = FP_ALL_KEYS - DEFAULT_FP_VISIBLE_KEYS。
 * 用于 TableViewPreset.hiddenColumns 初始化。
 */
export function getDefaultHiddenFpKeys(): string[] {
  const visible = new Set(DEFAULT_FP_VISIBLE_KEYS)
  return FP_ALL_KEYS.filter((k) => !visible.has(k))
}
