// [趋势选股] - 数据适配层
// 职责：将后端 StrategyResult 转换为统一 TrendSelectionRow，并提供列渲染所需的工具函数
// 唯一性：DSA 字段候选 key 列表在此处统一维护，禁止散落在 IndexPage/ScreenerPage
import type { StrategyResult } from '@/api/endpoints'
import type { TrendSelectionRow } from './types.ts'

// [趋势选股] - 描述: DSA 字段候选 key 列表（统一维护，禁止散落在各页面）
// 趋势持续天数（正=多头/上涨，负=空头/下跌，绝对值=持续天数）
export const DIR_BARS_KEYS = ['dsa_dir_bars', 'dsa_duration', 'dir_duration', 'duration'] as const
// 日均趋势变化（后端存储为小数，显示需 ×100）
export const VWAP_RET_AVG_KEYS = ['vwap_ret_avg', 'dsa_avg_return', 'vwap_avg_return', 'avg_return'] as const
// 本轮趋势涨跌（后端存储为小数，显示需 ×100）
export const VWAP_RET_TOTAL_KEYS = [
  'vwap_ret_total',
  'dsa_total_return',
  'vwap_total_return',
  'total_return',
] as const
// 平均偏离趋势线（后端存储为小数，显示需 ×100）
export const OFFSET_MEAN_KEYS = ['offset_mean', 'shift_mean'] as const
// 当前强弱位置（0~1 小数，显示需 ×100）
export const OFFSET_PERCENTILE_KEYS = [
  'offset_percentile',
  'short_position',
  'position_short',
  'short_pos',
] as const

/** 从 payload 中按候选 key 列表取第一个非空值 */
export function pickPayload(payload: Record<string, unknown>, keys: readonly string[]): unknown {
  for (const k of keys) {
    const v = payload[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}

/** 转换为数字，失败返回 null */
export function toNum(v: unknown): number | null {
  if (v === undefined || v === null || v === '') return null
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  return Number.isNaN(n) ? null : n
}

/** 格式化为数值字符串（保留指定小数位），未知返回 '-' */
export function fmtNum(v: unknown, digits = 2): string {
  const n = toNum(v)
  return n === null ? '-' : n.toFixed(digits)
}

/** 格式化为百分比字符串（不带正负号，输入已是百分比数值），未知返回 '-' */
export function fmtPct(v: unknown, digits = 2): string {
  const n = toNum(v)
  return n === null ? '-' : `${n.toFixed(digits)}%`
}

/** 将 ratio 小数格式化为百分比（乘以 100），未知返回 '-' */
export function fmtRatioAsPct(v: unknown, digits = 2): string {
  const n = toNum(v)
  return n === null ? '-' : `${(n * 100).toFixed(digits)}%`
}

/** 格式化为涨跌幅字符串（正数带 + 号，输入已是百分比数值），未知返回 '-' */
export function fmtChange(v: unknown, digits = 2): string {
  const n = toNum(v)
  if (n === null) return '-'
  return `${n > 0 ? '+' : ''}${n.toFixed(digits)}%`
}

/**
 * [趋势选股] - 描述: 按 A 股口径返回涨跌幅颜色类名（涨红/跌绿/平灰）
 * 与 market-colors.scss 中 .market-up/.market-down/.market-flat 对齐
 */
export function changePctColorClass(v: unknown): string {
  const n = toNum(v)
  if (n === null) return 'market-flat'
  if (n > 0) return 'market-up'
  if (n < 0) return 'market-down'
  return 'market-flat'
}

/** 从 row 中提取股票展示信息（优先使用 instrument 级字段，回退到 payload） */
export function getStockDisplay(row: TrendSelectionRow): {
  symbol: string
  name: string
  market: string
} {
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

/**
 * 将 StrategyResult 转换为 TrendSelectionRow
 * @param result 后端策略结果
 * @param watchedIds 已自选 instrument_id 集合（主页传入以标记 watched，ScreenerPage 不传默认 false）
 */
export function adaptStrategyResultToTrendRow(
  result: StrategyResult,
  watchedIds?: Set<string>,
): TrendSelectionRow {
  return {
    resultId: result.id,
    instrumentId: result.instrument_id,
    symbol: result.instrument_symbol ?? '-',
    name: result.instrument_name ?? '-',
    market: result.instrument_market ?? '',
    payload: result.payload,
    watched: watchedIds ? watchedIds.has(result.instrument_id) : false,
  }
}
