// [自选监控] - 共享模块类型定义
// 职责：定义首页与自选页统一使用的监控行数据模型
import type { WatchlistMonitorStatusItem } from '@/api/endpoints'

export type MonitorStatus = WatchlistMonitorStatusItem['monitor_status']

export interface LatestEvent {
  event_type: string
  event_time: string
  boundary: number | null
}

export interface WatchlistMonitorRow {
  instrument_id: string
  symbol: string
  name: string
  market: string
  monitor_status: MonitorStatus
  current_price: number | null
  // [自选股涨跌幅] - 描述: 上一交易日收盘价与当日涨跌幅（advice.md 第三节）
  previous_close: number | null
  change_pct: number | null
  bb_upper: number | null
  bb_mid: number | null
  bb_lower: number | null
  upper_node_price: number | null
  upper_node_low: number | null
  upper_node_high: number | null
  lower_node_price: number | null
  lower_node_low: number | null
  lower_node_high: number | null
  position_0_1: number | null
  poc_price: number | null
  latest_event: LatestEvent | null
  updated_at: string | null
  [key: string]: unknown
}
