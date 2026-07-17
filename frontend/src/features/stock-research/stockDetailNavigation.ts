// [StockDetailNavigation] - 描述: 详情页导航唯一真源（CHANGE-20260716-006）
// originScope 决定左栏来源；returnTo 只用于返回页面和恢复筛选。
// 3 个导航入口（MarketWorkspacePage / useStockDetailActions / StockDetailPage 左栏）
// 必须全部使用 buildStockDetailUrl，禁止手工字符串拼接。
//
// 纯 TS 模块（无 React 依赖，无 @/ 别名依赖），可被 node --experimental-strip-types 直接运行。

export type OriginScope = 'market' | 'watchlist'

export interface BuildStockDetailUrlOptions {
  /** 来源 scope（market|watchlist），决定左栏来源列表 */
  originScope: OriginScope
  /** 返回 URL（仅用于返回原页面和恢复筛选，不决定来源） */
  returnTo?: string | null
  /** 时间周期（1d|15m|1h|1w|1mo），切换股票时保留 */
  timeframe?: string | null
}

export interface ResolvedDetailOrigin {
  /** 解析后的 originScope */
  originScope: OriginScope
  /** originScope 与 returnTo.scope 冲突时为 true（显示"来源上下文失效"，禁止静默回退自选） */
  contextMismatch: boolean
}

/**
 * originScope → source 映射。
 * market → selection；watchlist → watchlist。
 */
export function sourceForOriginScope(originScope: OriginScope): 'selection' | 'watchlist' {
  return originScope === 'market' ? 'selection' : 'watchlist'
}

/**
 * originScope → strategy 映射。
 * market → dsa_selector；watchlist → watchlist_monitor。
 */
export function strategyForOriginScope(originScope: OriginScope): string {
  return originScope === 'market' ? 'dsa_selector' : 'watchlist_monitor'
}

/**
 * 构建个股详情页 URL（唯一入口，3 个导航点共用）。
 *
 * 生成：/stock/:symbol?originScope=market&source=selection&strategy=dsa_selector&returnTo=...&timeframe=...
 *
 * originScope 是来源唯一真源；source/strategy 由 originScope 推导。
 * returnTo 只编码在 URL 中用于返回，不参与来源决策。
 */
export function buildStockDetailUrl(symbol: string, opts: BuildStockDetailUrlOptions): string {
  const source = sourceForOriginScope(opts.originScope)
  const strategy = strategyForOriginScope(opts.originScope)
  const params = new URLSearchParams({
    originScope: opts.originScope,
    source,
    strategy,
  })
  if (opts.returnTo) {
    params.set('returnTo', opts.returnTo)
  }
  if (opts.timeframe) {
    params.set('timeframe', opts.timeframe)
  }
  return `/stock/${symbol}?${params.toString()}`
}

/**
 * 从 returnTo URL 中提取 scope（兼容旧链接）。
 * 仅当 URL 无显式 originScope 时作为 fallback 使用。
 */
function scopeFromReturnTo(returnTo: string | null | undefined): 'market' | 'watchlist' | null {
  if (!returnTo) return null
  if (!returnTo.startsWith('/market')) return null
  const qs = returnTo.split('?')[1]
  if (!qs) return null
  const params = new URLSearchParams(qs)
  const scope = params.get('scope')
  return scope === 'market' ? 'market' : scope === 'watchlist' ? 'watchlist' : null
}

/**
 * 解析详情页来源 originScope（唯一真源）。
 *
 * 优先级：
 *   1. 显式 originScope 参数（最高优先级，不被 returnTo.scope 覆盖）
 *   2. 旧 URL 无 originScope 时兼容解析 returnTo.scope
 *   3. 无任何来源的直接 URL 默认 watchlist
 *
 * 冲突检测：
 *   originScope 存在且 returnTo.scope 也存在但不同 → contextMismatch=true
 *   禁止静默回退自选，调用方应显示"来源上下文失效"。
 */
export function resolveStockDetailOrigin(
  originScopeRaw: string | null,
  returnTo: string | null | undefined,
): ResolvedDetailOrigin {
  const returnToScope = scopeFromReturnTo(returnTo)

  if (originScopeRaw === 'market' || originScopeRaw === 'watchlist') {
    const originScope: OriginScope = originScopeRaw
    // 显式 originScope 存在 — 检查与 returnTo.scope 是否冲突
    const contextMismatch = returnToScope !== null && returnToScope !== originScope
    return { originScope, contextMismatch }
  }

  // 无显式 originScope — 兼容旧 URL 解析 returnTo.scope
  if (returnToScope !== null) {
    return { originScope: returnToScope, contextMismatch: false }
  }

  // 无任何来源 — 默认 watchlist
  return { originScope: 'watchlist', contextMismatch: false }
}
