// [DetailSourceContext] - 描述: 详情页来源上下文统一解析（唯一真源）
// CHANGE-20260715-007: 消除 stockResearchTypes.ts 与 marketWorkspaceUrlState.ts 之间的
// 重复 source/strategy 映射。本模块为唯一权威实现，其他模块只消费不复制。
// CHANGE-20260716-006: originScope 成为来源唯一真源，returnTo.scope 降级为兼容回退。
//
// [PRD V2.0 §4.4] DetailEntryContext: origin = market|watchlist|direct
//   - market: 用户从 /market 选股结果进入，需 marketContext + selection 列表
//   - watchlist: 用户从自选监控进入，需 watchlist 列表
//   - direct: 用户直接进入（深链/书签/通知），无来源列表上下文
// [PRD V2.0 §7.3 CI门禁] market上下文不得回退watchlist（origin=market 失效时
//   显示"来源上下文失效"，禁止静默回退 watchlist）
//
// 纯 TS 模块（无 React 依赖，无 @/ 别名依赖），可被 node --experimental-strip-types 直接运行。
// 通过相对路径 import marketWorkspaceUrlState 的 decodeMarketListContext（函数声明提升，
// ESM 循环引用安全：双方均在函数体内调用对方导出，不在模块顶层使用）。
//
// 优先级（CHANGE-20260716-006）：
//   显式 originScope > 有效 /market returnTo.scope（兼容旧链接）> watchlist 默认值
//   originScope=market  → source=selection, strategy=dsa_selector
//   originScope=watchlist → source=watchlist, strategy=watchlist_monitor
//   originScope=direct → source=watchlist, strategy=watchlist_monitor（无来源列表）
//   originScope 与 returnTo.scope 冲突 → sourceContextInvalid=true（显示"来源上下文失效"）
//   source=selection 且无有效 marketContext → sourceContextInvalid=true
//
// 禁止：
//   - 在 stockResearchTypes.ts 或 marketWorkspaceUrlState.ts 中复制 normalizeResearchSource / defaultStrategyForSource
//   - 在 StockDetailPage 或 useStockDetailActions 中各自推导 source/strategy
//   - source=selection 时静默回退到 watchlist

// 类型导入（运行时被 strip，不产生循环依赖）
import type { MarketListContext, StrategyResultQuery } from '../market-workspace/marketWorkspaceUrlState.ts'
// 值导入：decodeMarketListContext 用于解析 returnTo（函数声明提升，ESM 循环安全）
import { decodeMarketListContext } from '../market-workspace/marketWorkspaceUrlState.ts'
// [DetailSourceContextV2] computeStableContextIdV2 来自 stockDetailNavigation（纯 string hash，无循环依赖）
import { computeStableContextIdV2, type OriginScope } from './stockDetailNavigation.ts'

// ===== 来源类型与映射（唯一权威实现）=====

export type ResearchSource = 'watchlist' | 'selection'

export const DEFAULT_SOURCE: ResearchSource = 'watchlist'

/**
 * 校验 source 是否为允许值，非法回退 watchlist。
 * 唯一权威实现；stockResearchTypes.ts 和 marketWorkspaceUrlState.ts 只 re-export 或直接消费。
 */
export function normalizeResearchSource(raw: string | null): ResearchSource {
  return raw === 'selection' ? 'selection' : 'watchlist'
}

/**
 * 根据 source 推导默认策略 key。
 * watchlist → watchlist_monitor；selection → dsa_selector。
 * 值与 @/constants/strategyKeys 的 STRATEGY_KEYS 对齐。
 */
export function defaultStrategyForSource(source: ResearchSource): string {
  return source === 'selection' ? 'dsa_selector' : 'watchlist_monitor'
}

// ===== 详情来源上下文解析 =====

export interface DetailSourceContext {
  source: ResearchSource
  strategy: string
  marketContext: MarketListContext | null
  sourceContextInvalid: boolean
}

/**
 * 详情页来源上下文统一解析（唯一真源）。
 *
 * StockDetailPage 和 useStockDetailActions 只消费此函数的返回值，禁止各自推导。
 *
 * 优先级（CHANGE-20260716-006）：
 *   1. 显式 originScope（最高优先级，不被 returnTo.scope 覆盖）
 *      market  → source=selection, strategy=dsa_selector
 *      watchlist → source=watchlist, strategy=watchlist_monitor
 *      direct → source=watchlist, strategy=watchlist_monitor（无来源列表）
 *   2. 无显式 originScope 时兼容解析有效 /market returnTo.scope（旧链接回退）
 *   3. 无任何来源 → 默认 watchlist
 *
 * 冲突检测：
 *   originScope=market|watchlist 存在且 returnTo.scope 也存在但不同 → sourceContextInvalid=true
 *   （显示"来源上下文失效"，不静默回退自选）
 *   originScope=direct 不参与冲突检测（direct 无对应 returnTo.scope）。
 *
 * source=selection 且 marketContext=null 时也 sourceContextInvalid=true。
 */
export function resolveDetailSourceContext(
  returnTo: string | null | undefined,
  rawSource: string | null,
  rawStrategy: string | null,
  originScopeRaw?: string | null,
): DetailSourceContext {
  const marketContext = decodeMarketListContext(returnTo)

  // CHANGE-20260716-006: 显式 originScope 优先（不被 returnTo.scope 覆盖）
  // [PRD V2.0 §4.4] originScope 支持三值：market|watchlist|direct
  if (originScopeRaw === 'market' || originScopeRaw === 'watchlist' || originScopeRaw === 'direct') {
    const source: ResearchSource =
      originScopeRaw === 'market' ? 'selection' : 'watchlist'
    const strategy = defaultStrategyForSource(source)
    // 冲突检测：originScope 与 returnTo.scope 不一致
    // direct 无对应 returnTo.scope，不参与冲突检测
    const returnToScope = marketContext?.scope ?? null
    const contextMismatch = originScopeRaw !== 'direct' && returnToScope !== null && returnToScope !== originScopeRaw
    // source=selection 但无有效 marketContext → 失效
    const sourceContextInvalid = contextMismatch || (source === 'selection' && marketContext === null)
    return { source, strategy, marketContext, sourceContextInvalid }
  }

  // 无显式 originScope — 兼容旧 URL 解析 returnTo.scope
  if (marketContext !== null) {
    const source: ResearchSource =
      marketContext.scope === 'market' ? 'selection' : 'watchlist'
    const strategy = defaultStrategyForSource(source)
    return { source, strategy, marketContext, sourceContextInvalid: false }
  }

  // 无有效 /market returnTo — 使用合法 source 参数
  const source = normalizeResearchSource(rawSource)
  const strategy = rawStrategy || defaultStrategyForSource(source)
  // source=selection 但无有效市场上下文 → 失效（不静默回退自选）
  const sourceContextInvalid = source === 'selection'

  return { source, strategy, marketContext: null, sourceContextInvalid }
}

// ===== [DetailSourceContextV2] 来源同源同序合同（唯一权威 V2 实现）=====
// V2 修复两个根因：
//   1. 行情来源失真：V1 详情页通过 fresh usePublishedRuns 重新推导 activeRunId，
//      新 run 发布后会漂移；V2 固定入口时刻 sourceRunId + canonicalQuery 快照。
//   2. 自选来源不同链：V1 详情左栏 watchlist 用 useWatchlistMonitorStatus（monitor-status API），
//      与列表页 dsa_selector universe=watchlist 不同数据链导致顺序跳变；
//      V2 统一用 useStrategyRunResults(sourceRunId, canonicalQuery)。
//
// V2 字段契约：
//   - origin: market|watchlist|direct（来源唯一真源）
//   - sourceRunId: 入口时刻 DSA published run id（market/watchlist 必填，direct 可空）
//   - canonicalQuery: 入口时刻 StrategyResultQuery（market=universe=all, watchlist=universe=watchlist）
//   - returnTo: 返回原页面 URL（仅用于返回按钮，不决定来源）
//   - stableContextId: origin+sourceRunId+canonicalQuery+returnTo 的 hash（不含 selectedSymbol，切股不变）
//   - sourceContextInvalid: market/watchlist 缺 runId/cq/universe不匹配/冲突 → true；direct 永不 invalid
//   - invalidReason: 失效原因（供 UI 显示）
//
// 禁止：
//   - useWatchlistMonitorStatus 充当来源列表数据源（仅用于 inWatchlist 状态）
//   - market/watchlist 缺 sourceRunId 时静默回退自选或另一来源（必须显示 invalid）
//   - direct 伪造行情来源
//   - stableContextId 纳入 selectedSymbol

export type DetailSourceInvalidReason =
  | 'none'
  | 'context_mismatch'
  | 'missing_run_id'
  | 'missing_canonical_query'
  | 'canonical_query_parse_failed'
  | 'universe_mismatch'

export interface DetailSourceContextV2 {
  origin: OriginScope
  sourceRunId: string | null
  canonicalQuery: StrategyResultQuery | null
  /** 入口时刻 canonical query 原始 JSON 字符串（来自 URL cq 参数，切股时原样透传，供导航重建 URL） */
  canonicalQueryRaw: string | null
  returnTo: string | null
  stableContextId: string
  sourceContextInvalid: boolean
  invalidReason: DetailSourceInvalidReason
}

/**
 * 解析 V2 canonical query JSON 字符串。
 * 返回 [query, parseFailed]：解析成功且为对象 → [query, false]；空输入 → [null, false]；解析失败 → [null, true]。
 */
function parseCanonicalQuery(raw: string | null): [StrategyResultQuery | null, boolean] {
  if (!raw) return [null, false]
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return [parsed as StrategyResultQuery, false]
    }
    return [null, true]
  } catch {
    return [null, true]
  }
}

/**
 * [DetailSourceContextV2] 详情页来源上下文统一解析（V2 唯一真源）。
 *
 * StockDetailPage 顶层调用一次，传递给 useStockDetailActions。禁止 useStockDetailActions 自行推导。
 *
 * origin 解析优先级：
 *   1. 显式 originScope（market|watchlist|direct）
 *   2. 无显式 originScope + 有效 /market returnTo → 按 returnTo.scope 推导（兼容旧 /market 链接）
 *   3. 无显式 originScope + 无 /market returnTo → direct（V2：不再默认 watchlist，避免伪造来源）
 *
 * 失效规则（仅 market/watchlist，direct 永不失效）：
 *   - origin 与 returnTo.scope 冲突 → context_mismatch
 *   - 缺 sourceRunId → missing_run_id
 *   - canonicalQuery JSON 解析失败 → canonical_query_parse_failed
 *   - 缺 canonicalQuery → missing_canonical_query
 *   - canonicalQuery.universe 与 origin 不匹配 → universe_mismatch
 *
 * stableContextId：origin+sourceRunId+canonicalQueryRaw+returnTo（不含 selectedSymbol，切股不变）。
 */
export function resolveDetailSourceContextV2(
  originScopeRaw: string | null,
  returnTo: string | null | undefined,
  sourceRunIdRaw: string | null,
  canonicalQueryRaw: string | null,
): DetailSourceContextV2 {
  const marketContext = decodeMarketListContext(returnTo)

  // 1. 解析 origin
  let origin: OriginScope
  let contextMismatch = false
  if (originScopeRaw === 'market' || originScopeRaw === 'watchlist' || originScopeRaw === 'direct') {
    origin = originScopeRaw
    // 冲突检测：origin 非 direct 且 returnTo.scope 存在但不一致
    const returnToScope = marketContext?.scope ?? null
    const expectedScope = origin === 'market' ? 'market' : 'watchlist'
    if (origin !== 'direct' && returnToScope !== null && returnToScope !== expectedScope) {
      contextMismatch = true
    }
  } else if (marketContext !== null) {
    // 无显式 originScope，从 /market returnTo 推导（兼容旧 /market 链接）
    origin = marketContext.scope === 'market' ? 'market' : 'watchlist'
  } else {
    // 无 originScope 且无 /market returnTo → direct（V2：不默认 watchlist，避免伪造来源）
    origin = 'direct'
  }

  // 2. 解析 canonicalQuery
  const [canonicalQuery, cqParseFailed] = parseCanonicalQuery(canonicalQueryRaw)

  // 3. 判定失效（仅 market/watchlist；direct 永不失效）
  let sourceContextInvalid = false
  let invalidReason: DetailSourceInvalidReason = 'none'
  if (origin === 'market' || origin === 'watchlist') {
    if (contextMismatch) {
      sourceContextInvalid = true
      invalidReason = 'context_mismatch'
    } else if (!sourceRunIdRaw) {
      sourceContextInvalid = true
      invalidReason = 'missing_run_id'
    } else if (cqParseFailed) {
      sourceContextInvalid = true
      invalidReason = 'canonical_query_parse_failed'
    } else if (!canonicalQuery) {
      sourceContextInvalid = true
      invalidReason = 'missing_canonical_query'
    } else {
      const expectedUniverse = origin === 'market' ? 'all' : 'watchlist'
      if (canonicalQuery.universe !== expectedUniverse) {
        sourceContextInvalid = true
        invalidReason = 'universe_mismatch'
      }
    }
  }

  // 4. stableContextId（不含 selectedSymbol，切股不变）
  const stableContextId = computeStableContextIdV2(
    origin,
    sourceRunIdRaw,
    canonicalQueryRaw,
    returnTo ?? null,
  )

  return {
    origin,
    sourceRunId: sourceRunIdRaw,
    canonicalQuery: sourceContextInvalid ? null : canonicalQuery,
    canonicalQueryRaw: canonicalQueryRaw,
    returnTo: returnTo ?? null,
    stableContextId,
    sourceContextInvalid,
    invalidReason,
  }
}
