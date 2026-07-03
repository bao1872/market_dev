// [Market] - 描述: 市场标签与成交额格式化共享工具
// 供 StockDetailPage / CaptureStockPage 复用，避免同一转换逻辑复制两份

// 市场代码 -> 中文标签映射
export const MARKET_LABELS: Record<string, string> = {
  A_SHARE: 'A股',
  STAR: '科创板',
  MAIN: '主板',
  SME: '中小板',
  GEM: '创业板',
  BSE: '北交所',
}

// 格式化成交额（元 -> 亿/万）
export function formatAmount(v: number): string {
  if (!v || v <= 0) return '--'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return v.toFixed(0)
}
