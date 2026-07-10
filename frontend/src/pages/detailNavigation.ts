// [个股详情导航] - 描述: 趋势选股/自选进入个股详情与返回的 URL/state 纯函数
// 用途：把 ScreenerPage.goDetail 与 StockDetailPage 返回逻辑中可测试部分抽为纯函数

/** 构建个股详情页 URL */
export function buildStockDetailUrl(symbol: string, source: string, strategyKey: string): string {
  return `/stock/${symbol}?source=${source}&strategy=${strategyKey}`
}

/** 构建个股详情页导航 state（携带 returnTo） */
export function buildStockDetailState(returnTo: string): { returnTo: string } {
  return { returnTo }
}

/** 解析返回路径：优先使用导航时传入的 returnTo，否则按 source fallback */
export function resolveBackPath(returnTo: string | undefined, source: string): string {
  if (returnTo) return returnTo
  return source === 'selection' ? '/screener' : '/market?scope=watchlist'
}
