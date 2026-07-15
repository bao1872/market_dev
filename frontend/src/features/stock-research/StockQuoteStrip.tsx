// CHANGE-20260713-010: 个股详情报价条组件
// 从 StockDetailPage 内联报价条抽取，保持现价/涨跌/开盘/最高/最低/成交额/总市值/流通市值共 8 项。
// 市值数据源不可用时为 null，显示 '--'；tooltip 显示数据日期。
import type { PriceSummary } from './useStockResearchData'
import { formatAmount } from '@/utils/market'

/**
 * 格式化市值：
 * - null → '--'
 * - < 1亿 → 万元
 * - >= 1亿 且 < 1万亿 → 亿元
 * - >= 1万亿 → 万亿元
 */
export function formatMarketCap(v: number | null | undefined): string {
  if (v === null || v === undefined) return '--'
  if (v >= 1e12) return (v / 1e12).toFixed(2) + '万亿元'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿元'
  if (v >= 1e4) return (v / 1e4).toFixed(2) + '万元'
  return v.toFixed(0) + '元'
}

interface QuoteMetricProps {
  label: string
  value: string
  valueClassName?: string
  title?: string
}

export function QuoteMetric({ label, value, valueClassName, title }: QuoteMetricProps) {
  return (
    <div className="quote-metric" title={title}>
      <span>{label}</span>
      <b className={valueClassName}>{value}</b>
    </div>
  )
}

interface StockQuoteStripProps {
  priceSummary: PriceSummary
}

export function StockQuoteStrip({ priceSummary }: StockQuoteStripProps) {
  const upDownClass = priceSummary.isUp ? 'market-up' : 'market-down'
  const changePercentText = priceSummary.changePercent !== null
    ? `${priceSummary.isUp ? '+' : ''}${priceSummary.changePercent.toFixed(2)}%`
    : '--'

  // 市值 tooltip：显示数据日期（market_cap_as_of）
  const marketCapTooltip = priceSummary.marketCapAsOf
    ? `数据日期: ${priceSummary.marketCapAsOf}`
    : undefined

  return (
    <div className="tv-quote-strip">
      <QuoteMetric
        label="现价"
        value={priceSummary.currentPrice !== null ? priceSummary.currentPrice.toFixed(2) : '--'}
        valueClassName={upDownClass}
      />
      <QuoteMetric
        label="涨跌"
        value={changePercentText}
        valueClassName={upDownClass}
      />
      <QuoteMetric
        label="开盘"
        value={priceSummary.openPrice !== null ? priceSummary.openPrice.toFixed(2) : '--'}
      />
      <QuoteMetric
        label="最高"
        value={priceSummary.highPrice !== null ? priceSummary.highPrice.toFixed(2) : '--'}
      />
      <QuoteMetric
        label="最低"
        value={priceSummary.lowPrice !== null ? priceSummary.lowPrice.toFixed(2) : '--'}
      />
      <QuoteMetric
        label="成交额"
        value={priceSummary.amountValue !== null ? formatAmount(priceSummary.amountValue) : '--'}
      />
      <QuoteMetric
        label="总市值"
        value={formatMarketCap(priceSummary.totalMarketCap)}
        title={marketCapTooltip}
      />
      <QuoteMetric
        label="流通市值"
        value={formatMarketCap(priceSummary.floatMarketCap)}
        title={marketCapTooltip}
      />
    </div>
  )
}
