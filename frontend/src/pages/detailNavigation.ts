// [个股详情导航] - 描述: 趋势选股/消息中心进入行情工作区的 URL 构建 + 返回路径解析（纯函数）
// 本文件被 node --experimental-strip-types --test 直接执行，不得使用 @/ 别名导入。

import { normalizeInternalReturnTo } from '../features/market-workspace/marketWorkspaceUrlState.ts'

/** 构建从趋势选股进入行情工作区的 URL
 * /market?scope=market&symbol=xxx&source=selection&strategy=dsa_selector&returnTo=<原screener URL>
 */
export function buildMarketEntryFromScreener(
  symbol: string,
  strategyKey: string,
  returnTo: string,
): string {
  const safeReturnTo = normalizeInternalReturnTo(returnTo)
  const params = new URLSearchParams({
    scope: 'market',
    symbol,
    source: 'selection',
    strategy: strategyKey,
  })
  if (safeReturnTo) {
    params.set('returnTo', safeReturnTo)
  }
  return `/market?${params.toString()}`
}

/** 构建从消息中心进入行情工作区的 URL
 * /market?symbol=xxx&event_id=xxx
 */
export function buildMarketEntryFromMessage(symbol: string, eventId: string): string {
  const params = new URLSearchParams({ symbol, event_id: eventId })
  return `/market?${params.toString()}`
}

/** 构建个股详情页 URL（/stock/:symbol 兼容路由，保留旧链接可用） */
export function buildStockDetailUrl(symbol: string, source: string, strategyKey: string): string {
  return `/stock/${symbol}?source=${source}&strategy=${strategyKey}`
}

/** 构建个股详情页导航 state（携带 returnTo，用于 /stock/:symbol 兼容路由） */
export function buildStockDetailState(returnTo: string): { returnTo: string } {
  return { returnTo }
}

/** 解析返回路径：优先使用 returnTo（经 normalizeInternalReturnTo 安全校验），否则按 source fallback */
export function resolveBackPath(returnTo: string | undefined | null, source: string): string {
  const safe = normalizeInternalReturnTo(returnTo)
  if (safe) return safe
  return source === 'selection' ? '/screener' : '/market?scope=watchlist'
}
