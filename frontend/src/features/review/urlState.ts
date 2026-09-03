// [ReviewUrlState] - 描述: /review URL 状态解析/编码纯函数（PRD §3.1、§15）
// URL 是页面状态的唯一可分享入口（SSOT），支持前进/后退恢复。
//
// [CANONICAL] Slice C Scope-first 产品 URL 合同（Slice F 退休后唯一合同）：
//   /review
//     ?date=2026-08-21
//     &family=industry_l1
//     &scopeKey=<board_id>
//     &view=table
//     &tab=dynamics
//     &phase=Strengthening
//     &readiness=ready
//     &sort=velocity_desc
//     &page=1
//     &pageSize=50
//     &q=有色
// 排序词表（R2A，降序；升序为表头排序增强，仅作用于前端全量排序）：
//   velocity_desc | velocity_asc | acceleration_desc | acceleration_asc |
//   position_desc | position_asc | phase_desc | phase_asc |
//   equal_weight_return_desc | capital_tilt_desc | migration_desc | coverage_desc
// 本文件为纯 TS（无 React 依赖），可被 node --test 直接运行。
// 旧 Legacy URL 合同（stage/signalId/discoveryId/trackingTab 等）已于 Slice F 物理删除。

import type { ReviewScopeFamily, ReviewDynamicsPhase, ReviewCompositionReadiness } from './types'

// ============================================================
// [CANONICAL] 枚举与默认
// ============================================================

export type ReviewExplorerView = 'table' | 'trajectory'

export type ReviewDetailTab =
  | 'dsa'
  | 'smc'
  | 'momentum'
  | 'price'
  | 'current'
  | 'dynamics'
  | 'internal'
  | 'leadership'
  | 'attribution'
  | 'facts'

/**
 * [Slice C] 表头可排序列的 canonical 排序 key（不含方向）。
 *
 * 每个 key 都必须同时支持 asc 与 desc —— 由下方 ReviewSort 的模板字面量类型
 * 在编译期保证，杜绝「某些字段只有 _desc / 某些 enum 存在但 UI 点不了」的半状态。
 */
export type ReviewSortKey =
  | 'position'
  | 'velocity'
  | 'acceleration'
  | 'phase'
  | 'equal_weight_return'
  | 'capital_tilt'
  | 'advance_ratio'
  | 'decline_ratio'
  | 'unchanged_ratio'
  | 'migration'
  | 'coverage'
  | 'freshness_density'
  | 'freshness_today'
  | 'technical_hhi'
  | 'technical_top5_ratio'
  | 'leader_median_gap'

/**
 * 全部 sort key 的顺序化清单。
 * SORT_VALUES 与测试遍历均以此唯一来源派生，避免「枚举与集合漂移」。
 */
export const REVIEW_SORT_KEYS: readonly ReviewSortKey[] = [
  'position',
  'velocity',
  'acceleration',
  'phase',
  'equal_weight_return',
  'capital_tilt',
  'advance_ratio',
  'decline_ratio',
  'unchanged_ratio',
  'migration',
  'coverage',
  'freshness_density',
  'freshness_today',
  'technical_hhi',
  'technical_top5_ratio',
  'leader_median_gap',
]

/**
 * 排序词表：每个 key × {asc, desc} 的全组合。
 *
 * 向后兼容：既有 URL 值（velocity_desc / *_asc / equal_weight_return_desc /
 * capital_tilt_desc / migration_desc / coverage_desc / freshness_density_desc /
 * freshness_today_desc / technical_hhi_desc / leader_median_gap_desc）全部仍合法。
 *
 * 排序始终在客户端对完整 family snapshot 进行（后端 list_review_scopes 不接收
 * sort 参数），不改动后端 contract。
 */
export type ReviewSort = `${ReviewSortKey}_asc` | `${ReviewSortKey}_desc`

const SORT_KEY_SET: ReadonlySet<string> = new Set<string>(REVIEW_SORT_KEYS)

/**
 * 解析 sort 字符串为 {key, dir}；非法/未知值回退 { key: null, dir: 'desc' }。
 * 用 lastIndexOf('_') 切分，以兼容含下划线的 key（如 equal_weight_return_desc）。
 */
export function parseReviewSort(sort: ReviewSort): { key: ReviewSortKey | null; dir: 'asc' | 'desc' } {
  const idx = sort.lastIndexOf('_')
  if (idx <= 0) return { key: null, dir: 'desc' }
  const key = sort.slice(0, idx)
  const dir = sort.slice(idx + 1)
  if (!SORT_KEY_SET.has(key)) return { key: null, dir: 'desc' }
  if (dir !== 'asc' && dir !== 'desc') return { key: key as ReviewSortKey, dir: 'desc' }
  return { key: key as ReviewSortKey, dir: dir }
}

/** 由 {key, dir} 构造 sort 字符串 */
export function buildReviewSort(key: ReviewSortKey, dir: 'asc' | 'desc'): ReviewSort {
  return `${key}_${dir}` as ReviewSort
}

/**
 * P0-2 表头排序切换：第一次点击 → 降序 desc；第二次点击同一列 → 升序 asc。
 * 切换不同列时回到该列降序。无第三态（无“无排序”）。
 */
export function reviewSortToggle(key: ReviewSortKey, current: ReviewSort): ReviewSort {
  const { key: curKey, dir: curDir } = parseReviewSort(current)
  if (curKey === key && curDir === 'desc') {
    return buildReviewSort(key, 'asc')
  }
  return buildReviewSort(key, 'desc')
}

export interface ReviewUrlState {
  /** 交易日（YYYY-MM-DD） */
  date: string | null
  /** Scope 族 */
  family: ReviewScopeFamily
  /** 当前选中 Scope 标识 */
  scopeKey: string | null
  /** 产品视图：table（默认）/ trajectory */
  view: ReviewExplorerView
  /** 详情子 Tab */
  tab: ReviewDetailTab
  /** Dynamics 相位过滤（null = 不过滤） */
  phase: ReviewDynamicsPhase | null
  /** Composition readiness 过滤（null = 不过滤；UI 过滤，不发给后端） */
  readiness: ReviewCompositionReadiness | null
  /** 排序 */
  sort: ReviewSort
  /** 列表页码（>=1） */
  page: number
  /** 每页条数（后端 <=100） */
  pageSize: number
  /** 自由文本搜索 */
  q: string
}

export const DEFAULT_REVIEW_FAMILY: ReviewScopeFamily = 'industry_l1'
export const DEFAULT_REVIEW_VIEW: ReviewExplorerView = 'table'
/** 默认详情 Tab = dsa（R3 研究页第一入口；旧 current/dynamics 等保留但退居其后） */
export const DEFAULT_REVIEW_TAB: ReviewDetailTab = 'dsa'
export const DEFAULT_REVIEW_PHASE: ReviewDynamicsPhase | null = null
export const DEFAULT_REVIEW_SORT: ReviewSort = 'velocity_desc'
export const DEFAULT_REVIEW_READINESS: ReviewCompositionReadiness | null = null
export const DEFAULT_REVIEW_PAGE = 1
export const DEFAULT_REVIEW_PAGE_SIZE = 50
export const REVIEW_MAX_PAGE_SIZE = 100
export const DEFAULT_REVIEW_Q = ''

const FAMILY_VALUES: ReadonlySet<string> = new Set([
  'industry_l1',
  'industry_l2',
  'industry_l3',
  'concept',
])

const VIEW_VALUES: ReadonlySet<string> = new Set(['table', 'trajectory'])

const TAB_VALUES: ReadonlySet<string> = new Set([
  'dsa',
  'smc',
  'momentum',
  'price',
  'current',
  'dynamics',
  'internal',
  'leadership',
  'attribution',
  'facts',
])

/** 合法 sort 集合：由 REVIEW_SORT_KEYS 派生（每个 key 的 asc + desc 全组合） */
const SORT_VALUES: ReadonlySet<string> = new Set<string>(
  REVIEW_SORT_KEYS.flatMap((k) => [`${k}_desc`, `${k}_asc`]),
)

const PHASE_VALUES: ReadonlySet<string> = new Set([
  'Early Lift',
  'Strengthening',
  'Sustained',
  'Decelerating',
  'Weakening',
  'Repairing',
])

const READINESS_VALUES: ReadonlySet<string> = new Set([
  'ready',
  'insufficient_history',
  'unavailable_current',
])

/** 归一化 family，非法值回退 industry_l1 */
export function normalizeFamily(raw: string | null | undefined): ReviewScopeFamily {
  return raw && FAMILY_VALUES.has(raw) ? (raw as ReviewScopeFamily) : DEFAULT_REVIEW_FAMILY
}

/** 归一化 view，非法值回退 table */
export function normalizeExplorerView(raw: string | null | undefined): ReviewExplorerView {
  return raw && VIEW_VALUES.has(raw) ? (raw as ReviewExplorerView) : DEFAULT_REVIEW_VIEW
}

/** 归一化 tab，非法值回退 dynamics */
export function normalizeDetailTab(raw: string | null | undefined): ReviewDetailTab {
  return raw && TAB_VALUES.has(raw) ? (raw as ReviewDetailTab) : DEFAULT_REVIEW_TAB
}

/** 归一化 sort，非法值回退 velocity_desc */
export function normalizeSort(raw: string | null | undefined): ReviewSort {
  return raw && SORT_VALUES.has(raw) ? (raw as ReviewSort) : DEFAULT_REVIEW_SORT
}

/** 归一化 phase，非法值回退 null（不做 fallback 映射） */
export function normalizePhase(raw: string | null | undefined): ReviewDynamicsPhase | null {
  return raw && PHASE_VALUES.has(raw) ? (raw as ReviewDynamicsPhase) : null
}

/** 归一化 readiness，非法值回退 null（UI 过滤用，不发后端） */
export function normalizeReadiness(raw: string | null | undefined): ReviewCompositionReadiness | null {
  return raw && READINESS_VALUES.has(raw) ? (raw as ReviewCompositionReadiness) : null
}

/** 安全解析正整数页码，非法/缺失回退 1，强制 >=1 */
export function normalizePage(raw: string | null | undefined): number {
  const n = Number.parseInt(raw ?? '', 10)
  if (!Number.isFinite(n) || n < 1) return DEFAULT_REVIEW_PAGE
  return n
}

/** 安全解析每页条数，非法/越界回退 50，强制 [1,100] */
export function normalizePageSize(raw: string | null | undefined): number {
  const n = Number.parseInt(raw ?? '', 10)
  if (!Number.isFinite(n) || n < 1) return DEFAULT_REVIEW_PAGE_SIZE
  if (n > REVIEW_MAX_PAGE_SIZE) return REVIEW_MAX_PAGE_SIZE
  return n
}

/** 默认 canonical 状态 */
export function defaultReviewUrlState(): ReviewUrlState {
  return {
    date: null,
    family: DEFAULT_REVIEW_FAMILY,
    scopeKey: null,
    view: DEFAULT_REVIEW_VIEW,
    tab: DEFAULT_REVIEW_TAB,
    phase: DEFAULT_REVIEW_PHASE,
    readiness: DEFAULT_REVIEW_READINESS,
    sort: DEFAULT_REVIEW_SORT,
    page: DEFAULT_REVIEW_PAGE,
    pageSize: DEFAULT_REVIEW_PAGE_SIZE,
    q: DEFAULT_REVIEW_Q,
  }
}

/** 从 URLSearchParams 解析 canonical 复盘页面状态（URL 是 SSOT） */
export function decodeReviewUrl(params: URLSearchParams): ReviewUrlState {
  return {
    date: params.get('date') || null,
    family: normalizeFamily(params.get('family')),
    scopeKey: params.get('scopeKey') || null,
    view: normalizeExplorerView(params.get('view')),
    tab: normalizeDetailTab(params.get('tab')),
    phase: normalizePhase(params.get('phase')),
    readiness: normalizeReadiness(params.get('readiness')),
    sort: normalizeSort(params.get('sort')),
    page: normalizePage(params.get('page')),
    pageSize: normalizePageSize(params.get('pageSize')),
    q: params.get('q') ?? '',
  }
}

/** 将 canonical 状态编码为 URLSearchParams（仅写入非默认值） */
export function encodeReviewUrl(state: ReviewUrlState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.date) {
    params.set('date', state.date)
  }
  if (state.family !== DEFAULT_REVIEW_FAMILY) {
    params.set('family', state.family)
  }
  if (state.scopeKey) {
    params.set('scopeKey', state.scopeKey)
  }
  if (state.view !== DEFAULT_REVIEW_VIEW) {
    params.set('view', state.view)
  }
  if (state.tab !== DEFAULT_REVIEW_TAB) {
    params.set('tab', state.tab)
  }
  if (state.phase !== null) {
    params.set('phase', state.phase)
  }
  if (state.readiness !== null) {
    params.set('readiness', state.readiness)
  }
  if (state.sort !== DEFAULT_REVIEW_SORT) {
    params.set('sort', state.sort)
  }
  if (state.page !== DEFAULT_REVIEW_PAGE) {
    params.set('page', String(state.page))
  }
  if (state.pageSize !== DEFAULT_REVIEW_PAGE_SIZE) {
    params.set('pageSize', String(state.pageSize))
  }
  if (state.q !== '') {
    params.set('q', state.q)
  }
  return params
}

/** 基于现有状态部分更新字段，返回新状态对象（纯函数，泛型以兼容 canonical/legacy 状态） */
export function patchReviewUrl<T extends object>(
  prev: T,
  patch: Partial<T>,
): T {
  return { ...prev, ...patch }
}

/** 构建 /review?... 完整 URL（用于 navigate） */
export function buildReviewUrl(state: ReviewUrlState): string {
  const params = encodeReviewUrl(state)
  const qs = params.toString()
  return qs ? `/review?${qs}` : '/review'
}

/** 切换日期：清除 scopeKey 与页码，保留当前 family */
export function withReviewDateChange(state: ReviewUrlState, date: string): ReviewUrlState {
  return { ...state, date, scopeKey: null, page: DEFAULT_REVIEW_PAGE }
}

/** 切换 family：设置 family、清除 scopeKey、页码重置为 1 */
export function withReviewFamilyChange(
  state: ReviewUrlState,
  family: ReviewScopeFamily,
): ReviewUrlState {
  return { ...state, family, scopeKey: null, page: DEFAULT_REVIEW_PAGE }
}

/** 搜索/phase/readiness 等过滤类变化：页码重置为 1（保留 scopeKey） */
export function withReviewFilterChange(
  state: ReviewUrlState,
  patch: Partial<ReviewUrlState>,
): ReviewUrlState {
  return { ...state, ...patch, page: DEFAULT_REVIEW_PAGE }
}

/** 页码变化：只改 page，保留全部其他状态（不清 scopeKey、不重置过滤）。
 *  翻页必须走本 helper；q/phase/readiness/pageSize 等过滤变化才应使用
 *  withReviewFilterChange（重置 page=1）。 */
export function withReviewPageChange(
  state: ReviewUrlState,
  page: number,
): ReviewUrlState {
  return { ...state, page: Math.max(DEFAULT_REVIEW_PAGE, page) }
}


