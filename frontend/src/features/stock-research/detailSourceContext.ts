// [DetailSourceContext] - 描述: 详情页来源上下文统一解析（唯一真源）
// CHANGE-20260715-007: 消除 stockResearchTypes.ts 与 marketWorkspaceUrlState.ts 之间的
// 重复 source/strategy 映射。本模块为唯一权威实现，其他模块只消费不复制。
// CHANGE-20260716-006: originScope 成为来源唯一真源，returnTo.scope 降级为兼容回退。
//
// 纯 TS 模块（无 React 依赖，无 @/ 别名依赖），可被 node --experimental-strip-types 直接运行。
// 通过相对路径 import marketWorkspaceUrlState 的 decodeMarketListContext（函数声明提升，
// ESM 循环引用安全：双方均在函数体内调用对方导出，不在模块顶层使用）。
//
// 优先级（CHANGE-20260716-006）：
//   显式 originScope > 有效 /market returnTo.scope（兼容旧链接）> watchlist 默认值
//   originScope=market  → source=selection, strategy=dsa_selector
//   originScope=watchlist → source=watchlist, strategy=watchlist_monitor
//   originScope 与 returnTo.scope 冲突 → sourceContextInvalid=true（显示"来源上下文失效"）
//   source=selection 且无有效 marketContext → sourceContextInvalid=true
//
// 禁止：
//   - 在 stockResearchTypes.ts 或 marketWorkspaceUrlState.ts 中复制 normalizeResearchSource / defaultStrategyForSource
//   - 在 StockDetailPage 或 useStockDetailActions 中各自推导 source/strategy
//   - source=selection 时静默回退到 watchlist

// 类型导入（运行时被 strip，不产生循环依赖）
import type { MarketListContext } from '../market-workspace/marketWorkspaceUrlState.ts'
// 值导入：decodeMarketListContext 用于解析 returnTo（函数声明提升，ESM 循环安全）
import { decodeMarketListContext } from '../market-workspace/marketWorkspaceUrlState.ts'

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
 *   2. 无显式 originScope 时兼容解析有效 /market returnTo.scope（旧链接回退）
 *   3. 无任何来源 → 默认 watchlist
 *
 * 冲突检测：
 *   originScope 存在且 returnTo.scope 也存在但不同 → sourceContextInvalid=true
 *   （显示"来源上下文失效"，不静默回退自选）
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
  if (originScopeRaw === 'market' || originScopeRaw === 'watchlist') {
    const source: ResearchSource =
      originScopeRaw === 'market' ? 'selection' : 'watchlist'
    const strategy = defaultStrategyForSource(source)
    // 冲突检测：originScope 与 returnTo.scope 不一致
    const returnToScope = marketContext?.scope ?? null
    const contextMismatch = returnToScope !== null && returnToScope !== originScopeRaw
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
