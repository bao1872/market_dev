// [Chart] - 描述: 行情数据到图表组件的通用转换工具
// 供 StockDetailPage / CaptureStockPage 复用，保证 BarData 构造逻辑唯一
//
// [CH-03 fix] PRD §3.3: Exchange/MDAS 是唯一 Bar 生产者；前端 quote 只更新
// 价格摘要，不构造 K 线。已移除 mergeRealtimeQuoteIntoBars（曾用 quote 合成/修改
// 末根 bar，导致 MDAS 不是唯一 Bar 真源）。realtime 价格更新走 priceSummary。

import type { BarData } from '@/components/StrategyChart'
import type { Bar } from '@/api/endpoints'

// 将后端 Bar 列表转换为 StrategyChart 需要的 BarData 格式
export function mapBarsToBarData(items: Bar[] | undefined): BarData[] {
  if (!items) return []
  return items.map((b) => ({
    time: b.trade_time || b.trade_date || '',
    open: b.open,
    high: b.high,
    low: b.low,
    close: b.close,
    volume: b.volume,
  }))
}
