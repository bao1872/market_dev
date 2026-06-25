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

/** 格式化更新时间，取时间部分（上海时区） */
export function fmtTime(v: unknown): string {
  if (v === undefined || v === null || v === '') return '-'
  try {
    return new Date(String(v)).toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
    })
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
