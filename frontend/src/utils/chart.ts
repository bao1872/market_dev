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

// [Chart] - 描述: 将可信实时行情合并到 Bar 列表末尾，仅用于显示
// 不污染 indicators 计算、不写数据库
// 合并条件：quote.is_realtime === true && source === 'pytdx' && freshness_seconds <= 60
// timeframe 决定合并语义：1d 保留日期粒度，intraday 使用 quote.update_time
export function mergeRealtimeQuoteIntoBars(
  bars: BarData[],
  quote: QuoteResponse | undefined,
  timeframe: string = '1d',
): BarData[] {
  if (!quote || bars.length === 0) return bars

  // [QuoteTrust] - 只合并可信实时行情；daily_fallback / 延迟 / 降级均不混入 K 线
  const isTrustworthy =
    quote.is_realtime === true &&
    quote.source === 'pytdx' &&
    quote.freshness_seconds <= 60
  if (!isTrustworthy) return bars

  const currentPrice = quote.current_price
  if (currentPrice == null) return bars
  const last = bars[bars.length - 1]

  if (timeframe === '1d') {
    // [Chart] - 日线保留日期语义，不将 trade_date 改成 intraday timestamp
    const quoteDate = quote.update_time ? quote.update_time.slice(0, 10) : null
    const lastDate = typeof last.time === 'string' ? last.time.slice(0, 10) : null
    if (quoteDate && lastDate && quoteDate > lastDate) {
      // 当前无今日日线，追加一根日期粒度的实时 bar
      const realtimeBar: BarData = {
        time: quoteDate,
        open: last.close,
        high: Math.max(last.close, currentPrice),
        low: Math.min(last.close, currentPrice),
        close: currentPrice,
        volume: 0,
      }
      return [...bars, realtimeBar]
    }
    // 同一天或无法解析，合并到最后一根并保留其日期
    const merged: BarData = {
      ...last,
      close: currentPrice,
      high: Math.max(last.high, currentPrice),
      low: Math.min(last.low, currentPrice),
    }
    return [...bars.slice(0, -1), merged]
  }

  // intraday：使用 quote.update_time 合并到最后一根
  const merged: BarData = {
    ...last,
    close: currentPrice,
    high: Math.max(last.high, currentPrice),
    low: Math.min(last.low, currentPrice),
    time: quote.update_time || last.time,
  }
  return [...bars.slice(0, -1), merged]
}
