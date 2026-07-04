// [Chart] - 描述: 行情数据到图表组件的通用转换工具
// 供 StockDetailPage / CaptureStockPage 复用，保证 BarData 构造逻辑唯一

import type { BarData } from '@/components/StrategyChart'
import type { Bar, QuoteResponse } from '@/api/endpoints'

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

// [Chart] - 描述: 将实时行情合并到 Bar 列表末尾，仅用于显示
// 不污染 indicators 计算、不写数据库
export function mergeRealtimeQuoteIntoBars(
  bars: BarData[],
  quote: QuoteResponse | undefined,
): BarData[] {
  if (!quote || bars.length === 0) return bars
  const currentPrice = quote.current_price
  if (currentPrice == null) return bars
  const last = bars[bars.length - 1]
  const merged: BarData = {
    ...last,
    close: currentPrice,
    high: Math.max(last.high, currentPrice),
    low: Math.min(last.low, currentPrice),
    time: quote.update_time || last.time,
  }
  return [...bars.slice(0, -1), merged]
}
