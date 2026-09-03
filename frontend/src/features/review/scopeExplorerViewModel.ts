// [ScopeExplorerViewModel] - 描述: canonical Scope Explorer 展示层纯函数（Slice D + R2A）
// 只做 presentation-only 操作，绝不重算业务指标：
//   complete family snapshot → q 过滤 → phase 过滤 →
//   选定 sort 降序排序（null 恒最后）→ UI 分页
// 纯 TS，无 React/SCSS 依赖，可被 node --test 直接运行。
//
// P0-1：readiness 不再参与 Review scope list 过滤（普通用户不再用它筛选列表）。
// readiness 仍作为 row/detail/API canonical 字段保留（见 ReviewScopeListItem.readiness、
// urlState.readiness、详情响应），只是不再驱动列表过滤。
import type {
  ReviewScopeListItem,
  ReviewDynamicsPhase,
  ReviewScopeObservationSummary,
} from './types'
import { compareSortValue, isVisibleSortKey } from './scopeExplorerContract'
import {
  DEFAULT_REVIEW_SORT,
  parseReviewSort,
  type ReviewSort,
  type ReviewSortKey,
} from './urlState'

export interface ScopeExplorerQuery {
  q: string
  phase: ReviewDynamicsPhase | null
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
): ScopeExplorerQuery {
  return { q, phase }
}

/** 数值 null 恒排最后；仅对有限数值参与排序；NaN 视为 null（非数字不在有限集合内） */
function finiteOrNull(v: number | null | undefined): number | null {
  return v !== null && v !== undefined && Number.isFinite(v) ? v : null
}

/** 确定性 tie-break：scopeName ?? scopeKey，再 scopeKey（所有 sort 模式共用） */
function tieBreak(a: ReviewScopeListItem, b: ReviewScopeListItem): number {
  const na = a.scopeName ?? a.scopeKey
  const nb = b.scopeName ?? b.scopeKey
  if (na !== nb) return na.localeCompare(nb)
  return a.scopeKey.localeCompare(b.scopeKey)
}

/** phase 排序顺序索引（canonical phase 顺序，用于升/降序比较；null 恒最后） */
const PHASE_ORDER: Readonly<Record<string, number>> = {
  'Early Lift': 0,
  Strengthening: 1,
  Sustained: 2,
  Decelerating: 3,
  Weakening: 4,
  Repairing: 5,
}

/**
 * [Slice C] 前 5 强度占比：唯一 ratio owner（presentation-only derivation，非业务计算）。
 *
 * - denominator > 0 → numerator / denominator；
 * - 其余（缺失 / NaN / denominator <= 0）→ null。
 *
 * 绝不把缺失伪造成 0 或 1：缺失就是 null（展示 "—"，排序 null-last）。
 * 显示与排序必须共用本函数，杜绝「显示一个算法、排序另一个算法」。
 */
export function technicalTop5Ratio(
  obs: ReviewScopeObservationSummary | null | undefined,
): number | null {
  if (!obs) return null
  const num = obs.technicalTop5Numerator
  const den = obs.technicalTop5Denominator
  if (num === null || num === undefined || den === null || den === undefined) return null
  if (!Number.isFinite(num) || !Number.isFinite(den)) return null
  if (den <= 0) return null
  return num / den
}

/**
 * 取某 sort key 对应的排序数值（persisted 字段直接读取，绝不重算）。
 * - velocity/acceleration/position/equalWeightReturn/capitalTilt/advance_ratio/
 *   decline_ratio/unchanged_ratio/migration 来自 summary；
 * - coverage 来自行级 coverageRatio；
 * - freshness_* / technical_hhi / leader_median_gap 来自 observationSummary；
 * - technical_top5_ratio 走 technicalTop5Ratio 单一 owner（不在此处另算一遍）；
 * - phase 使用 canonical phase 顺序索引参与排序。
 * asc 与 desc 取同一数值，方向由 sortScopes 决定（null 恒最后）。
 */
export function sortValueFor(
  item: ReviewScopeListItem,
  key: ReviewSortKey | null,
): number | null {
  // [SLICE 5 / Explorer] 10 个 visible compare 列：只读 compareFacts
  // （即便 summary 里也有 EW / Tilt / Breadth / Migration，显示 owner 与排序
  //   owner 必须同为 compareFacts，避免分裂）。
  if (key !== null && isVisibleSortKey(key)) {
    return compareSortValue(item.compareFacts ?? null, key)
  }
  switch (key) {
    case 'position':
      return finiteOrNull(item.summary?.position)
    case 'velocity':
      return finiteOrNull(item.summary?.velocity)
    case 'acceleration':
      return finiteOrNull(item.summary?.acceleration)
    case 'phase': {
      const p = item.summary?.phase
      if (!p) return null
      return PHASE_ORDER[p] ?? null
    }
    // equal_weight_return / capital_tilt / advance_ratio / migration 已上移到
    // visible compare 分支（只读 compareFacts），此处不再重复，避免显示 owner
    // 与排序 owner 分裂。
    case 'decline_ratio':
      return finiteOrNull(item.summary?.declineRatio)
    case 'unchanged_ratio':
      return finiteOrNull(item.summary?.unchangedRatio)
    case 'coverage':
      return finiteOrNull(item.coverageRatio)
    case 'freshness_density':
      return finiteOrNull(item.observationSummary?.freshnessDecayWeightedDensity)
    case 'freshness_today':
      return finiteOrNull(item.observationSummary?.freshnessTodayCount)
    case 'technical_hhi':
      return finiteOrNull(item.observationSummary?.technicalHhi)
    case 'technical_top5_ratio':
      return technicalTop5Ratio(item.observationSummary)
    case 'leader_median_gap':
      return finiteOrNull(item.observationSummary?.technicalLeaderMedianGap)
    default:
      return finiteOrNull(item.summary?.velocity)
  }
}

/**
 * q：大小写不敏感匹配 scopeName / scopeKey（不搜索任意 JSON）。
 * phase：exact canonical phase 匹配（phase=null 不过滤）。
 * P0-1：readiness 不再参与过滤（普通用户不再用它筛选列表）；
 *       readiness 仍作为 row/detail/API canonical 字段保留，仅不再驱动列表过滤。
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
    return true
  })
}

/**
 * 通用排序 owner：数值 null 恒最后，确定性 tie-break。
 * 升序变体（_asc）反向比较；降序（_desc）保持历史默认行为。
 * 不进入 React 组件；不改 chart 几何（x=position/y=velocity 由 Trajectory 决定）。
 * 排序始终作用于完整 family snapshot（调用方传入全量 items），满足“跨分页完整结果集排序”。
 */
export function sortScopes(
  items: ReviewScopeListItem[],
  sort: ReviewSort,
): ReviewScopeListItem[] {
  const { key, dir } = parseReviewSort(sort)
  const sign = dir === 'desc' ? 1 : -1
  return [...items].sort((a, b) => {
    const va = sortValueFor(a, key)
    const vb = sortValueFor(b, key)
    if (va === null && vb === null) return tieBreak(a, b)
    if (va === null) return 1
    if (vb === null) return -1
    if (vb !== va) return sign * (vb - va)
    return tieBreak(a, b)
  })
}

/** 兼容别名：velocity_desc 仍是默认排序 */
export const sortVelocityDesc = (
  items: ReviewScopeListItem[],
): ReviewScopeListItem[] => sortScopes(items, 'velocity_desc')

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

/** 完整流水线：filter → 选定 sort 降序排序 → paginate（供 Workspace 单次调用）。
 * 排序在分页之前，且整组 filtered 一起排序（绝不按页独立排序）。
 * Trajectory 与 Table 共用同一 sort，但不改 chart 几何（x=position/y=velocity）。 */
export function applyScopeExplorerPipeline(
  snapshot: ReviewScopeListItem[],
  query: ScopeExplorerQuery,
  page: number,
  pageSize: number,
  sort: ReviewSort = DEFAULT_REVIEW_SORT,
): PaginatedScopes {
  const filtered = filterScopes(snapshot, query)
  const sorted = sortScopes(filtered, sort)
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
