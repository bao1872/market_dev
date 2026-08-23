// [ReviewUrlState] - 描述: /review URL 状态解析/编码纯函数（PRD §3.1、§15）
// URL 是页面状态的唯一可分享入口（SSOT），支持前进/后退恢复。
//
// [CANONICAL] Slice C Scope-first 产品 URL 合同：
//   /review
//     ?date=2026-08-21
//     &family=industry_l1
//     &scopeKey=<board_id>
//     &view=table
//     &tab=dynamics
//     &phase=Strengthening
//     &sort=velocity_desc
//     &page=1
//     &pageSize=50
//     &q=有色
// 本文件为纯 TS（无 React 依赖），可被 node --test 直接运行。
//
// [LEGACY] 旧 URL 字段（stage/signalId/discoveryId/trackingTab/scopeFamily/status/
//          scopeName/parentScopeType/parentScopeKey）已退出 canonical 合同，
//          仅保留 LegacyReviewUrlState 供 Slice D 前的 ReviewPage 解析使用，
//          不进入 canonical 合同；禁止新增两个同义 decodeReviewUrl/encodeReviewUrl。

import type { ReviewScopeFamily, ReviewDynamicsPhase, ReviewCompositionReadiness } from './types'

// ============================================================
// [CANONICAL] 枚举与默认
// ============================================================

export type ReviewExplorerView = 'table' | 'trajectory'

export type ReviewDetailTab =
  | 'dynamics'
  | 'internal'
  | 'leadership'
  | 'attribution'
  | 'facts'

/** 排序词表：至少 velocity_desc 为默认；仅含当前 PRD 已要求的取值 */
export type ReviewSort = 'velocity_desc'

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
export const DEFAULT_REVIEW_TAB: ReviewDetailTab = 'dynamics'
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
  'dynamics',
  'internal',
  'leadership',
  'attribution',
  'facts',
])

const SORT_VALUES: ReadonlySet<string> = new Set(['velocity_desc'])

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

// ============================================================
// [LEGACY] 旧 URL 状态（retired 于 canonical Scope 之后；Slice D 前 ReviewPage 仍读取）
// 不进入 canonical 合同；scopeName 不作为 canonical URL 状态。
// ============================================================

import type { ReviewStage, TrackingTab } from './types'

export interface LegacyReviewUrlState {
  date: string | null
  view: 'discovery' | 'stages'
  stage: ReviewStage
  scopeType: string | null
  scopeKey: string | null
  scopeName: string | null
  parentScopeType: string | null
  parentScopeKey: string | null
  signalId: string | null
  boardId: string | null
  symbol: string | null
  trackingTab: TrackingTab
  discoveryId: string | null
  scopeFamily: string | null
  status: string | null
}

export const DEFAULT_LEGACY_REVIEW_VIEW: 'discovery' | 'stages' = 'discovery'
export const DEFAULT_LEGACY_REVIEW_STAGE: ReviewStage = 'scan'
export const DEFAULT_LEGACY_TRACKING_TAB: TrackingTab = 'history'

/** [LEGACY] 正式 Review 五阶段（Phase 5B 契约）：auction 为 auxiliary entry。
 *  ReviewStageNav 从此派生正式导航项。 */
export const REVIEW_FORMAL_STAGES: ReadonlyArray<ReviewStage> = [
  'scan',
  'signals',
  'attribution',
  'validation',
  'tracking',
]

const LEGACY_VIEW_VALUES: ReadonlySet<string> = new Set(['discovery', 'stages'])
const LEGACY_STAGE_VALUES: ReadonlySet<string> = new Set([
  'scan',
  'signals',
  'attribution',
  'validation',
  'tracking',
  'auction',
])
const LEGACY_TRACKING_TAB_VALUES: ReadonlySet<string> = new Set([
  'history',
  'watchlist',
  'events',
])

/** [LEGACY] 归一化旧阶段值，非法值回退 scan */
export function normalizeLegacyStage(raw: string | null | undefined): ReviewStage {
  return raw && LEGACY_STAGE_VALUES.has(raw) ? (raw as ReviewStage) : DEFAULT_LEGACY_REVIEW_STAGE
}

/** [LEGACY] 归一化旧视图值，非法值回退 discovery */
export function normalizeLegacyView(raw: string | null | undefined): 'discovery' | 'stages' {
  return raw && LEGACY_VIEW_VALUES.has(raw)
    ? (raw as 'discovery' | 'stages')
    : DEFAULT_LEGACY_REVIEW_VIEW
}

/** [LEGACY] 归一化旧追踪 Tab 值 */
export function normalizeLegacyTrackingTab(raw: string | null | undefined): TrackingTab {
  return raw && LEGACY_TRACKING_TAB_VALUES.has(raw)
    ? (raw as TrackingTab)
    : DEFAULT_LEGACY_TRACKING_TAB
}

/** [LEGACY] 解析旧 URL 状态（ReviewPage 在 Slice D 替换前使用） */
export function decodeLegacyReviewUrl(params: URLSearchParams): LegacyReviewUrlState {
  return {
    date: params.get('date') || null,
    view: normalizeLegacyView(params.get('view')),
    stage: normalizeLegacyStage(params.get('stage')),
    scopeType: params.get('scopeType') || null,
    scopeKey: params.get('scopeKey') || null,
    scopeName: params.get('scopeName') || null,
    parentScopeType: params.get('parentScopeType') || null,
    parentScopeKey: params.get('parentScopeKey') || null,
    signalId: params.get('signalId') || null,
    boardId: params.get('boardId') || null,
    symbol: params.get('symbol') || null,
    trackingTab: normalizeLegacyTrackingTab(params.get('trackingTab')),
    discoveryId: params.get('discoveryId') || null,
    scopeFamily: params.get('scopeFamily') || null,
    status: params.get('status') || null,
  }
}

/** [LEGACY] 编码旧 URL 状态（仅写入非默认值） */
export function encodeLegacyReviewUrl(state: LegacyReviewUrlState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.date) params.set('date', state.date)
  if (state.view !== DEFAULT_LEGACY_REVIEW_VIEW) params.set('view', state.view)
  if (state.stage !== DEFAULT_LEGACY_REVIEW_STAGE) params.set('stage', state.stage)
  if (state.scopeType) params.set('scopeType', state.scopeType)
  if (state.scopeKey) params.set('scopeKey', state.scopeKey)
  if (state.scopeName) params.set('scopeName', state.scopeName)
  if (state.parentScopeType) params.set('parentScopeType', state.parentScopeType)
  if (state.parentScopeKey) params.set('parentScopeKey', state.parentScopeKey)
  if (state.signalId) params.set('signalId', state.signalId)
  if (state.boardId) params.set('boardId', state.boardId)
  if (state.symbol) params.set('symbol', state.symbol)
  if (state.trackingTab !== DEFAULT_LEGACY_TRACKING_TAB) params.set('trackingTab', state.trackingTab)
  if (state.discoveryId) params.set('discoveryId', state.discoveryId)
  if (state.scopeFamily) params.set('scopeFamily', state.scopeFamily)
  if (state.status) params.set('status', state.status)
  return params
}

// [LEGACY] 向后兼容别名：保留旧测试/组件导入名（normalizeStage / normalizeView）。
// 语义与 legacy 归一化一致；Slice F 清理时一并移除。
export const normalizeStage = normalizeLegacyStage
export const normalizeView = normalizeLegacyView
