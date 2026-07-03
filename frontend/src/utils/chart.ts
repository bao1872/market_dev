// [Chart] - 描述: 行情数据到图表组件的通用转换工具
// 供 StockDetailPage / CaptureStockPage 复用，保证 BarData 构造逻辑唯一

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
