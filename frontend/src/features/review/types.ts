// [ReviewTypes] - 描述: 复盘模块 TypeScript 类型定义
// 对应后端 schemas/review.py（字段命名与后端 JSON 序列化保持一致）
// 规则：前端不计算 P/Q/U/C/V、筛选器或归因，只承载结构化展示
// PRD §7.1 / §12 / §15

// ============================================================
// P/Q/U/C/V 指标合同（PRD §7.1）
// ============================================================

/** P/Q/U/C/V 单个 component（PRD §7.1 components 元素） */
export interface ReviewMetricComponent {
  name: string
  rawValue: number | null
  normalizedValue: number | null
  /** 方向：positive（正向）/negative（反向）/neutral */
  direction: 'positive' | 'negative' | 'neutral' | string
  denominator: number | null
  /** 字段来源（权威扁平字段名） */
  fieldSource: string
  weight: number
  coverage: number | null
  /** 状态：ready/insufficient_history/partial/unavailable */
  status: string
  extra: Record<string, unknown> | null
}

/** P/Q/U/C/V 单个聚合变量 payload（PRD §7.1 通用结构） */
export interface ReviewMetricPayload {
  /** 归一化值（0-100） */
  value: number | null
  rawValue: number | null
  /** 1 日变化 */
  delta1d: number | null
  /** 5 日变化 */
  delta5d: number | null
  /** 120 日历史分位（0-100），不足 60 日为 null */
  historyPercentile120d: number | null
  /** 当日横截面分位（0-100） */
  crossSectionPercentile: number | null
  historyObservationCount: number | null
  components: ReviewMetricComponent[]
  coverage: number | null
  /** 状态：ready/insufficient_history/partial/unavailable */
  status: string
}

// ============================================================
// 日期与总览（PRD §12.1）
// ============================================================

/** GET /api/v1/review/dates 响应 */
export interface ReviewDatesResponse {
  trade_dates: string[]
  latest_trade_date: string | null
}

/** GET /api/v1/review/latest 响应 */
export interface ReviewLatestResponse {
  review_run_id: string
  trade_date: string
  status: string
  algorithm_version: string
  filter_version: string
}

/** overview.coverage 子结构 */
export interface ReviewOverviewCoverage {
  market: number | null
  indices: number | null
  styles: number | null
  industryL1: number | null
}

/** overview.signalSummary 子结构 */
export interface ReviewOverviewSignalSummary {
  new: number
  continuing: number
  confirmed: number
  weakened: number
  invalidated: number
  transformed: number
}

/** GET /api/v1/review/{trade_date}/overview 响应 */
export interface ReviewOverview {
  reviewRunId: string
  tradeDate: string
  status: string
  sourceCoreRunId: string
  sourceBoardRunId: string
  algorithmVersion: string
  filterVersion: string
  baselineWindow: number
  coverage: ReviewOverviewCoverage
  signalSummary: ReviewOverviewSignalSummary
  coverageRatio: number | null
  expectedScopeCount: number
  succeededScopeCount: number
  failedScopeCount: number
  signalCount: number
  startedAt: string | null
  completedAt: string | null
  publishedAt: string | null
}

// ============================================================
// 市场扫描（PRD §12.2）
// ============================================================

/** GET /api/v1/review/{trade_date}/scopes 单条记录 */
export interface ReviewScopeMetrics {
  id: string
  reviewRunId: string
  tradeDate: string
  scopeType: string
  scopeKey: string
  scopeName: string
  parentScopeType: string | null
  parentScopeKey: string | null
  eligibleCount: number
  readyCount: number
  coverageRatio: number
  status: string
  p: ReviewMetricPayload | null
  q: ReviewMetricPayload | null
  u: ReviewMetricPayload | null
  c: ReviewMetricPayload | null
  v: ReviewMetricPayload | null
  dataQuality: Record<string, unknown> | null
  signalCount: number
}

/** 分页响应通用结构 */
export interface ReviewPagedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

export type ReviewScopeListResponse = ReviewPagedResponse<ReviewScopeMetrics>

// ============================================================
// 信号（PRD §12.3）
// ============================================================

export type ReviewSignalStatus =
  | 'new'
  | 'continuing'
  | 'confirmed'
  | 'weakened'
  | 'invalidated'
  | 'transformed'

/** GET /api/v1/review/{trade_date}/signals 单条记录 */
export interface ReviewSignal {
  id: string
  reviewRunId: string
  tradeDate: string
  /** 筛选器族：A/B/C */
  filterFamily: 'A' | 'B' | 'C'
  signalType: string
  scopeType: string
  scopeKey: string
  scopeName: string
  status: ReviewSignalStatus | string
  firstSeenDate: string
  previousSignalId: string | null
  transformedToSignalId: string | null
  triggerPayload: Record<string, unknown>
  baselinePayload: Record<string, unknown>
  evidencePayload: Record<string, unknown>
  confirmationRule: Record<string, unknown>
  invalidationRule: Record<string, unknown>
  coverageRatio: number | null
  rankKey: Record<string, unknown>
  durationDays: number
  createdAt: string | null
}

export type ReviewSignalListResponse = ReviewPagedResponse<ReviewSignal>

// ============================================================
// 归因与个股（PRD §12.4）
// ============================================================

export type ReviewBoardRole =
  | 'core'
  | 'second_line'
  | 'elasticity'
  | 'follower'
  | 'laggard'
  | 'unclassified'

export type ReviewRelationToScope =
  | 'synchronized_strengthening'
  | 'synchronized_weakening'
  | 'instrument_leads_scope'
  | 'scope_strong_instrument_lags'
  | 'instrument_strong_scope_unsupported'
  | 'unconfirmed'

/** GET /api/v1/review/signals/{signal_id}/attributions 单条记录 */
export interface ReviewAttribution {
  id: string
  signalId: string
  childScopeType: string
  childScopeKey: string
  childScopeName: string
  relationType: string | null
  contributionValue: number | null
  contributionRank: number | null
  metricsPayload: Record<string, unknown>
  evidencePayload: Record<string, unknown>
  coverageRatio: number | null
  createdAt: string | null
}

export type ReviewAttributionListResponse = ReviewPagedResponse<ReviewAttribution>

/** GET /api/v1/review/signals/{signal_id}/instruments 单条记录 */
export interface ReviewInstrument {
  id: string
  signalId: string
  instrumentId: string
  symbol: string
  name: string
  boardRole: ReviewBoardRole | string | null
  relationToScope: ReviewRelationToScope | string | null
  contributionValue: number | null
  contributionRank: number | null
  firstPyramidPayload: Record<string, unknown>
  freshEventsPayload: Record<string, unknown>
  sourceSnapshotId: string | null
  createdAt: string | null
}

export type ReviewInstrumentListResponse = ReviewPagedResponse<ReviewInstrument>

// ============================================================
// 追踪（PRD §12.5）
// ============================================================

export type ReviewTrackingType = 'signal' | 'scope' | 'instrument'
export type ReviewTrackingStatus = 'active' | 'confirmed' | 'invalidated' | 'closed'

export interface ReviewTracking {
  id: string
  userId: string
  sourceSignalId: string | null
  trackingType: ReviewTrackingType | string
  scopeType: string | null
  scopeKey: string | null
  instrumentId: string | null
  status: ReviewTrackingStatus | string
  confirmationConditions: Record<string, unknown>
  invalidationConditions: Record<string, unknown>
  note: string | null
  createdAt: string
  closedAt: string | null
}

export type ReviewTrackingListResponse = ReviewPagedResponse<ReviewTracking>

/** POST /api/v1/review/trackings 请求体（snake_case 对齐后端） */
export interface ReviewTrackingCreateRequest {
  tracking_type: ReviewTrackingType
  source_signal_id?: string | null
  scope_type?: string | null
  scope_key?: string | null
  instrument_id?: string | null
  confirmation_conditions?: Record<string, unknown>
  invalidation_conditions?: Record<string, unknown>
  note?: string | null
  idempotency_key: string
}

/** PATCH /api/v1/review/trackings/{id} 请求体 */
export interface ReviewTrackingPatchRequest {
  status?: ReviewTrackingStatus
  confirmation_conditions?: Record<string, unknown> | null
  invalidation_conditions?: Record<string, unknown> | null
  note?: string | null
  idempotency_key: string
}

export interface ReviewTrackingEvaluation {
  id: string
  trackingId: string
  reviewRunId: string
  tradeDate: string
  previousState: string | null
  currentState: string
  evaluationPayload: Record<string, unknown>
  createdAt: string
}

export type ReviewTrackingEvaluationListResponse = ReviewPagedResponse<ReviewTrackingEvaluation>

// ============================================================
// API 查询参数
// ============================================================

export interface ReviewScopeListParams {
  scope_type?: string
  parent_scope_type?: string
  parent_scope_key?: string
  include_partial?: boolean
  page?: number
  page_size?: number
}

export interface ReviewSignalListParams {
  filter_family?: 'A' | 'B' | 'C'
  signal_type?: string
  status?: string
  scope_type?: string
  scope_key?: string
  include_partial?: boolean
  page?: number
  page_size?: number
}

export interface ReviewAttributionListParams {
  include_partial?: boolean
  page?: number
  page_size?: number
}

export interface ReviewInstrumentListParams {
  board_role?: string
  relation_to_scope?: string
  include_partial?: boolean
  page?: number
  page_size?: number
}

export interface ReviewTrackingListParams {
  tracking_type?: string
  status?: string
  page?: number
  page_size?: number
}

// ============================================================
// 前端枚举常量（展示用，禁止用作业务计算）
// ============================================================

/** 五阶段标识（对应 URL stage 参数） */
export type ReviewStage =
  | 'scan'
  | 'signals'
  | 'attribution'
  | 'validation'
  | 'tracking'
  | 'auction'

/** 追踪复核子 Tab */
export type TrackingTab = 'history' | 'watchlist' | 'events'

/** P/Q/U/C/V 聚合变量标识 */
export type MetricKey = 'p' | 'q' | 'u' | 'c' | 'v'

/** run 状态集合（PRD §5.1） */
export const REVIEW_RUN_STATUSES = [
  'created',
  'computing',
  'partial',
  'signals_ready',
  'published',
  'completed_with_errors',
  'failed',
  'cancelled',
] as const

/** 处于计算中、需要轮询的状态 */
export const COMPUTING_STATUSES = new Set<string>(['created', 'computing', 'signals_ready'])
