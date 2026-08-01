// [StockDetailNavigation] - 描述: 详情页导航唯一真源（CHANGE-20260716-006）
// originScope 决定左栏来源；returnTo 只用于返回页面和恢复筛选。
// 3 个导航入口（MarketWorkspacePage / useStockDetailActions / StockDetailPage 左栏）
// 必须全部使用 buildStockDetailUrl，禁止手工字符串拼接。
//
// [PRD V2.0 §4.4] DetailEntryContext: origin = market|watchlist|direct
//   - market: 用户从 /market 选股结果进入，需 marketContext + selection 列表
//   - watchlist: 用户从自选监控进入，需 watchlist 列表
//   - direct: 用户直接进入（深链/书签/通知），无来源列表上下文
// [PRD V2.0 §7.3 CI门禁] market上下文不得回退watchlist（origin=market 失效时
//   显示"来源上下文失效"，禁止静默回退 watchlist）
//
// 纯 TS 模块（无 React 依赖，无 @/ 别名依赖），可被 node --experimental-strip-types 直接运行。
//
// [CHANGE-20260731-SAME-SOURCE] 详情左栏改用 /market/stocks 同源查询（取代旧 DSA strategy-results）：
//   - 新增 mcq（Market Canonical Query）URL 参数：JSON 序列化的 /market/stocks 查询参数快照
//   - mcq 包含：scope, query, industry, concept, fp_filter, fp_sort, page, page_size
//   - sourceRunId/cq（旧 StrategyResultQuery）标记 deprecated，不再消费
//   - 左栏顺序与 /market 当前页完全一致：按 mcq 查询返回的 items 顺序，不重新排序

export type OriginScope = 'market' | 'watchlist' | 'direct'

/**
 * [CHANGE-20260731-SAME-SOURCE] Market Canonical Query（/market/stocks 同源查询参数）。
 * 固定入口时刻的筛选/排序/分页口径，切换股票时原样透传。
 * 禁止详情页重新推导或 fresh 查询，确保左栏顺序与列表当前页完全一致。
 */
export interface MarketCanonicalQuery {
  scope: 'market' | 'watchlist'
  query?: string | null
  industry?: string | null
  concept?: string | null
  fp_filter?: string | null
  fp_sort?: string | null
  page: number
  page_size: number
}

export interface BuildStockDetailUrlOptions {
  /** 来源 scope（market|watchlist|direct），决定左栏来源列表 */
  originScope: OriginScope
  /** 返回 URL（仅用于返回原页面和恢复筛选，不决定来源） */
  returnTo?: string | null
  /** 时间周期（1d|15m|1h|1w|1mo），切换股票时保留 */
  timeframe?: string | null
  /**
   * [DEPRECATED 20260731-SAME-SOURCE] 旧 DSA sourceRunId，保留兼容但不再消费。
   * 左栏统一用 mcq（Market Canonical Query）查询 /market/stocks。
   */
  sourceRunId?: string | null
  /**
   * [DEPRECATED 20260731-SAME-SOURCE] 旧 DSA canonicalQuery（StrategyResultQuery JSON），
   * 保留兼容但不再消费。左栏统一用 mcq 查询 /market/stocks。
   */
  canonicalQuery?: string | null
  /**
   * [CHANGE-20260731-SAME-SOURCE] Market Canonical Query（/market/stocks 同源查询参数 JSON 字符串）。
   * 固定入口时刻的筛选/排序/分页口径，左栏顺序与列表当前页完全一致。
   * market/watchlist 来源必填；direct 可空。
   */
  marketCanonicalQuery?: string | null
}

export interface ResolvedDetailOrigin {
  /** 解析后的 originScope（含 direct） */
  originScope: OriginScope
  /** originScope 与 returnTo.scope 冲突时为 true（显示"来源上下文失效"，禁止静默回退自选） */
  contextMismatch: boolean
}

/**
 * originScope → source 映射。
 * market → selection；watchlist → watchlist；direct → watchlist（向后兼容数据获取，
 * 但 UI 应根据 originScope==='direct' 隐藏来源列表）。
 */
export function sourceForOriginScope(originScope: OriginScope): 'selection' | 'watchlist' {
  return originScope === 'market' ? 'selection' : 'watchlist'
}

/**
 * originScope → strategy 映射。
 * market → dsa_selector；watchlist/direct → watchlist_monitor。
 */
export function strategyForOriginScope(originScope: OriginScope): string {
  return originScope === 'market' ? 'dsa_selector' : 'watchlist_monitor'
}

/**
 * 构建个股详情页 URL（唯一入口，3 个导航点共用）。
 *
 * 生成：/stock/:symbol?originScope=market&source=selection&strategy=dsa_selector&returnTo=...&timeframe=...&mcq=...
 *
 * originScope 是来源唯一真源；source/strategy 由 originScope 推导。
 * returnTo 只编码在 URL 中用于返回，不参与来源决策。
 * [CHANGE-20260731-SAME-SOURCE] mcq（Market Canonical Query）固定入口时刻 /market/stocks 查询快照，
 *   切换股票时原样透传，详情左栏用此 mcq 查询 /market/stocks，禁止重新推导。
 *   旧 DSA sourceRunId/cq 保留兼容（仍编码到 URL），但详情页不再消费。
 *
 * [PRD V2.0 §4.4] originScope 支持三值：market|watchlist|direct
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
  // [DEPRECATED 20260731-SAME-SOURCE] 旧 DSA 快照，保留兼容但不再消费
  if (opts.sourceRunId) {
    params.set('sourceRunId', opts.sourceRunId)
  }
  if (opts.canonicalQuery) {
    params.set('cq', opts.canonicalQuery)
  }
  // [CHANGE-20260731-SAME-SOURCE] Market Canonical Query（/market/stocks 同源查询参数）
  if (opts.marketCanonicalQuery) {
    params.set('mcq', opts.marketCanonicalQuery)
  }
  return `/stock/${symbol}?${params.toString()}`
}

/**
 * [DetailSourceContextV2] 计算稳定 contextId（不含 selectedSymbol，不含 returnTo）。
 *
 * [CHANGE-20260731-SAME-SOURCE] 来源列表身份 = origin + mcq（Market Canonical Query）。
 * 切换股票时这二者不变，故 stableContextId 不变。
 * 用于 React key 和左栏滚动位置 storage key，避免每次切股重置。
 *
 * 禁止：
 *   - 将 selectedSymbol 纳入 stableContextId（违反不变性）。
 *   - 将 returnTo 纳入 stableContextId（returnTo 含 selected=入口symbol，会间接纳入 symbol）。
 *     returnTo 仅用于返回导航，不参与来源身份。
 */
export function computeStableContextIdV2(
  origin: OriginScope,
  sourceRunId: string | null,
  canonicalQueryRaw: string | null,
  marketCanonicalQueryRaw?: string | null,
): string {
  // [CHANGE-20260731-SAME-SOURCE] 优先用 mcq 计算；无 mcq 时回退旧 DSA 组合（兼容旧链接）
  if (marketCanonicalQueryRaw) {
    return `${origin}|mcq|${marketCanonicalQueryRaw}`
  }
  return `${origin}|${sourceRunId ?? ''}|${canonicalQueryRaw ?? ''}`
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
 * 解析详情页来源 originScope（V1 兼容，已弃用）。
 *
 * @deprecated 使用 `detailSourceContext.ts` 的 `resolveDetailSourceContextV2` 代替。
 *   V2 为唯一真源，支持 missing_origin invalid 状态；本函数保留仅为向后兼容旧测试。
 *   生产代码（StockDetailPage / useStockDetailActions）已迁移至 V2。
 *
 * 优先级（V1 旧逻辑，与 V2 不一致）：
 *   1. 显式 originScope 参数（最高优先级，不被 returnTo.scope 覆盖）
 *      支持三值：market|watchlist|direct（[PRD V2.0 §4.4]）
 *   2. 旧 URL 无 originScope 时兼容解析 returnTo.scope
 *   3. 无任何来源的直接 URL 默认 watchlist（V1 旧行为；V2 改为 missing_origin invalid）
 *
 * 冲突检测：
 *   originScope=market|watchlist 存在且 returnTo.scope 也存在但不同 → contextMismatch=true
 *   禁止静默回退自选，调用方应显示"来源上下文失效"。
 *   originScope=direct 不参与冲突检测（direct 无对应 returnTo.scope）。
 */
export function resolveStockDetailOrigin(
  originScopeRaw: string | null,
  returnTo: string | null | undefined,
): ResolvedDetailOrigin {
  const returnToScope = scopeFromReturnTo(returnTo)

  // [PRD V2.0 §4.4] 显式 originScope 支持三值
  if (originScopeRaw === 'market' || originScopeRaw === 'watchlist' || originScopeRaw === 'direct') {
    const originScope: OriginScope = originScopeRaw
    // 显式 originScope 存在 — 检查与 returnTo.scope 是否冲突
    // direct 无对应 returnTo.scope，不参与冲突检测
    const contextMismatch = originScope !== 'direct' && returnToScope !== null && returnToScope !== originScope
    return { originScope, contextMismatch }
  }

  // 无显式 originScope — 兼容旧 URL 解析 returnTo.scope
  if (returnToScope !== null) {
    return { originScope: returnToScope, contextMismatch: false }
  }

  // 无任何来源 — 默认 watchlist（向后兼容；显式 direct 需调用方传 originScope=direct）
  return { originScope: 'watchlist', contextMismatch: false }
}

/**
 * 计算来源徽标文案（纯函数，可测试）。
 *
 * 规则：
 * - 上下文失效 → '来源失效'
 * - direct → '直接访问'
 * - market → '行情来源'
 * - watchlist → '自选来源'
 */
export function computeSourceBadge(
  origin: OriginScope,
  contextInvalid: boolean,
): string {
  if (contextInvalid) return '来源失效'
  if (origin === 'direct') return '直接访问'
  if (origin === 'market') return '行情来源'
  return '自选来源'
}

// ===== [PRD V2.0 §4.4] DetailEntryContext 唯一对象 =====

/**
 * DetailEntryContext — 详情页入口上下文唯一对象（PRD V2.0 §4.4）。
 *
 * 字段契约：
 *   - origin: 来源类型 market|watchlist|direct
 *   - contextId: 上下文稳定标识（基于 origin+listQuery+returnTo+selectedSymbol 的 hash）
 *   - listQuery: 来源列表查询参数（market 时含 runId/strategy/scope；watchlist/direct 为 null）
 *   - returnTo: 返回原页面的 URL（可为 null）
 *   - selectedSymbol: 当前选中的股票代码
 *
 * CI 门禁（PRD V2.0 §7.3）：
 *   - origin=market 时 contextMismatch/sourceContextInvalid 为 true 不得静默回退 watchlist
 *   - origin=direct 时不显示来源列表（UI 隐藏左栏或显示"直接访问"占位）
 */
export interface DetailEntryContext {
  origin: OriginScope
  contextId: string
  listQuery: unknown | null
  returnTo: string | null
  selectedSymbol: string
}

/**
 * 计算 DetailEntryContext 的稳定 contextId。
 *
 * 基于 origin+listQuery+returnTo+selectedSymbol 的简单字符串拼接 hash，
 * 无密码学强度需求，仅用于 React key 和日志关联。
 */
export function computeDetailEntryContextId(
  origin: OriginScope,
  listQuery: unknown | null,
  returnTo: string | null,
  selectedSymbol: string,
): string {
  const listQueryStr = listQuery == null ? 'null' : JSON.stringify(listQuery)
  const raw = `${origin}|${listQueryStr}|${returnTo ?? ''}|${selectedSymbol}`
  return raw
}

/**
 * 构建 DetailEntryContext 唯一对象（PRD V2.0 §4.4）。
 *
 * 从 resolveStockDetailOrigin + listQuery + returnTo + selectedSymbol 构建统一对象。
 * 调用方应在 StockDetailPage 顶层调用一次，传递给下游 hooks。
 */
export function buildDetailEntryContext(
  resolved: ResolvedDetailOrigin,
  listQuery: unknown | null,
  returnTo: string | null,
  selectedSymbol: string,
): DetailEntryContext {
  return {
    origin: resolved.originScope,
    contextId: computeDetailEntryContextId(resolved.originScope, listQuery, returnTo, selectedSymbol),
    listQuery,
    returnTo,
    selectedSymbol,
  }
}
