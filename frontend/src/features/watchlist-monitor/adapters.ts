// [自选监控] - 数据适配层
// 职责：将后端 WatchlistMonitorStatusItem 转换为统一 WatchlistMonitorRow
import type { WatchlistMonitorStatusItem } from '@/api/endpoints'
import type { WatchlistMonitorRow } from './types'

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

/** 将 [0,1] 区间小数格式化为百分比（乘以 100），未知返回 '-' */
export function fmtPct(v: unknown, digits = 2): string {
  const n = toNum(v)
  return n === null ? '-' : `${(n * 100).toFixed(digits)}%`
}

/**
 * [自选股涨跌幅] - 描述: 格式化已为百分比的 change_pct（不乘 100），返回带正负号字符串
 * 输入已经是百分比数值（如 3.5 表示 +3.5%），未知返回 '-'
 */
export function fmtChangePct(v: unknown, digits = 2): string {
  const n = toNum(v)
  if (n === null) return '-'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(digits)}%`
}

/**
 * [自选股涨跌幅] - 描述: 按 A 股口径返回涨跌幅颜色类名（涨红/跌绿/平灰）
 * 与 market-colors.scss 中 .market-up/.market-down/.market-flat 对齐
 */
export function changePctColorClass(v: unknown): string {
  const n = toNum(v)
  if (n === null) return 'market-flat'
  if (n > 0) return 'market-up'
  if (n < 0) return 'market-down'
  return 'market-flat'
}

// [自选监控] - 格式化时间，返回 MM-DD HH:MM（上海时区），保留日期信息
export function fmtTime(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  try {
    const d = new Date(String(v))
    if (Number.isNaN(d.getTime())) return '-'
    const fmt = new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
    const parts = fmt.formatToParts(d)
    const get = (t: Intl.DateTimeFormatPartTypes): string =>
      parts.find((p) => p.type === t)?.value ?? ''
    return `${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`
  } catch {
    return '-'
  }
}

/** 将 WatchlistMonitorStatusItem 转换为统一行数据 */
export function adaptWatchlistMonitorStatusItem(
  item: WatchlistMonitorStatusItem,
): WatchlistMonitorRow {
  const metrics = item.metrics as Record<string, unknown> | null

  return {
    instrument_id: item.instrument_id,
    symbol: item.symbol,
    name: item.name,
    market: item.market ?? '',
    monitor_status: item.monitor_status,
    current_price: metrics ? toNum(metrics.current_price ?? metrics.close) : null,
    // [自选股涨跌幅] - 描述: 从 metrics 提取上一交易日收盘价与当日涨跌幅（advice.md 第三节）
    previous_close: metrics ? toNum(metrics.previous_close) : null,
    change_pct: metrics ? toNum(metrics.change_pct) : null,
    bb_upper: metrics ? toNum(metrics.bb_upper) : null,
    bb_mid: metrics ? toNum(metrics.bb_mid ?? metrics.bb_middle) : null,
    bb_lower: metrics ? toNum(metrics.bb_lower) : null,
    upper_node_price: metrics ? toNum(metrics.upper_node_price) : null,
    upper_node_low: metrics ? toNum(metrics.upper_node_low) : null,
    upper_node_high: metrics ? toNum(metrics.upper_node_high) : null,
    lower_node_price: metrics ? toNum(metrics.lower_node_price) : null,
    lower_node_low: metrics ? toNum(metrics.lower_node_low) : null,
    lower_node_high: metrics ? toNum(metrics.lower_node_high) : null,
    position_0_1: metrics ? toNum(metrics.position_0_1 ?? metrics.node_strength) : null,
    poc_price: metrics ? toNum(metrics.poc_price) : null,
    latest_event: item.latest_event ?? null,
    updated_at: item.updated_at ? fmtTime(item.updated_at) : null,
  }
}
