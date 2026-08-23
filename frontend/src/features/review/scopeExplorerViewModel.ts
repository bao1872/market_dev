// [ScopeExplorerViewModel] - 描述: canonical Scope Explorer 展示层纯函数（Slice D）
// 只做 presentation-only 操作，绝不重算业务指标：
//   complete family snapshot → q 过滤 → phase 过滤 → readiness 过滤 →
//   velocity_desc 排序 → UI 分页
// 纯 TS，无 React/SCSS 依赖，可被 node --test 直接运行。
import type {
  ReviewScopeListItem,
  ReviewDynamicsPhase,
  ReviewCompositionReadiness,
} from './types'

export interface ScopeExplorerQuery {
  q: string
  phase: ReviewDynamicsPhase | null
  readiness: ReviewCompositionReadiness | null
}

export interface PaginatedScopes {
  items: ReviewScopeListItem[]
  total: number
  pageCount: number
}

/** 构造查询对象（q 为原始输入，过滤时做大小写不敏感 trim 匹配） */
export function buildScopeExplorerQuery(
  q: string,
  phase: ReviewDynamicsPhase | null,
  readiness: ReviewCompositionReadiness | null,
): ScopeExplorerQuery {
  return { q, phase, readiness }
}

/** 数值 null 恒排最后；仅对有限数值参与排序 */
function finiteOrNull(v: number | null | undefined): number | null {
  return v !== null && v !== undefined && Number.isFinite(v) ? v : null
}

/** 确定性 tie-break：scopeName ?? scopeKey，再 scopeKey */
function tieBreak(a: ReviewScopeListItem, b: ReviewScopeListItem): number {
  const na = a.scopeName ?? a.scopeKey
  const nb = b.scopeName ?? b.scopeKey
  if (na !== nb) return na.localeCompare(nb)
  return a.scopeKey.localeCompare(b.scopeKey)
}

/**
 * q：大小写不敏感匹配 scopeName / scopeKey（不搜索任意 JSON）。
 * phase：exact canonical phase 匹配（phase=null 不过滤）。
 * readiness：exact readiness 匹配（readiness=null 不过滤）。
 */
export function filterScopes(
  snapshot: ReviewScopeListItem[],
  query: ScopeExplorerQuery,
): ReviewScopeListItem[] {
  const q = query.q.trim().toLowerCase()
  return snapshot.filter((item) => {
    if (q) {
      const name = (item.scopeName ?? '').toLowerCase()
      const key = (item.scopeKey ?? '').toLowerCase()
      if (!name.includes(q) && !key.includes(q)) return false
    }
    if (query.phase !== null && item.summary?.phase !== query.phase) return false
    if (query.readiness !== null && item.readiness !== query.readiness) return false
    return true
  })
}

/** velocity_desc：数值 null 恒排最后，descending finite velocity，确定性 tie-break */
export function sortVelocityDesc(items: ReviewScopeListItem[]): ReviewScopeListItem[] {
  return [...items].sort((a, b) => {
    const va = finiteOrNull(a.summary?.velocity)
    const vb = finiteOrNull(b.summary?.velocity)
    if (va === null && vb === null) return tieBreak(a, b)
    if (va === null) return 1
    if (vb === null) return -1
    if (vb !== va) return vb - va
    return tieBreak(a, b)
  })
}

/** UI 分页：作用于过滤+排序后的完整列表 */
export function paginateScopes(
  items: ReviewScopeListItem[],
  page: number,
  pageSize: number,
): PaginatedScopes {
  const total = items.length
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const safePage = Math.min(Math.max(1, page), pageCount)
  const start = (safePage - 1) * pageSize
  return { items: items.slice(start, start + pageSize), total, pageCount }
}

/** 有效页码：把 URL 原始页码钳制到 [1, pageCount]（pageCount=0 → 1）。
 *  URL 可能含 ?page=999，pagination 已按 pageCount 钳制渲染数据；
 *  交互（上一页/下一页/禁用/显示）必须使用同一有效页，避免 999→998 这类越界导航。 */
export function computeEffectivePage(rawPage: number, pageCount: number): number {
  return Math.min(Math.max(1, rawPage), Math.max(1, pageCount))
}

/** 完整流水线：filter → sort → paginate（供 Workspace 单次调用） */
export function applyScopeExplorerPipeline(
  snapshot: ReviewScopeListItem[],
  query: ScopeExplorerQuery,
  page: number,
  pageSize: number,
): PaginatedScopes {
  const filtered = filterScopes(snapshot, query)
  const sorted = sortVelocityDesc(filtered)
  return paginateScopes(sorted, page, pageSize)
}

/** 在完整 family snapshot 中按 scopeKey 查找选中 Scope（不受过滤影响） */
export function findScopeById(
  snapshot: ReviewScopeListItem[],
  scopeKey: string | null,
): ReviewScopeListItem | undefined {
  if (!scopeKey) return undefined
  return snapshot.find((item) => item.scopeKey === scopeKey)
}
