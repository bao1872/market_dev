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
  weightMode: string | null
  readiness: ReviewMetricReadiness
}

export interface ReviewMetricReadiness {
  raw_ready?: boolean
  normalized_ready?: boolean
  status?: string
  reason?: string | null
  history_observations?: number | null
  min_required?: number | null
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
  readiness: ReviewMetricReadiness
}

// ============================================================
// 日期与总览（PRD §12.1）
// ============================================================

/** GET /v1/review/dates 响应 */
export interface ReviewDatesResponse {
  trade_dates: string[]
  latest_trade_date: string | null
}

/** GET /v1/review/latest 响应 */
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

/** [P0 2026-08-04] chip 真实覆盖率明细（以 stock_core expectedCount 为分母） */
export interface ReviewChipCoverage {
  expectedCount: number | null
  succeededCount: number
  failedCount: number
  skippedCount: number
  missingCount: number
  coverage: number | null
}

/** GET /v1/review/{trade_date}/overview 响应 */
export interface ReviewOverview {
  reviewRunId: string
  tradeDate: string
  status: string
  sourceCoreRunId: string
  sourceBoardRunId: string
  /**
   * [QM-63] 输入 chip 共识 run ID。
   * null 明确表示 chip 不可用、本次 run 降级为 core-only，
   * 不得理解为「未记录」或「未知」。
   */
  sourceChipRunId: string | null
  /**
   * [QM-63] 降级原因列表（如 CHIP_UNAVAILABLE / CHIP_PARTIAL）。
   * 空数组表示无降级。
   */
  degradedReasons: string[]
  /**
   * [P0 2026-08-04] chip 真实覆盖率明细。
   * sourceChipRunId 恒为 null（chip 无独立 run 记录）时，以此展示真实覆盖情况。
   */
  chipCoverage: ReviewChipCoverage | null
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

/** GET /v1/review/{trade_date}/scopes 单条记录 */
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

/** GET /v1/review/{trade_date}/signals 单条记录 */
export interface ReviewSignal {
  id: string
  reviewRunId: string
  tradeDate: string
  /** 筛选器族：A/B/C/D */
  filterFamily: 'A' | 'B' | 'C' | 'D'
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

/** GET /v1/review/signals/{signal_id}/attributions 单条记录 */
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

/** GET /v1/review/signals/{signal_id}/instruments 单条记录 */
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

export type ReviewTrackingType = 'signal' | 'scope' | 'instrument' | 'discovery'
export type ReviewTrackingStatus = 'active' | 'confirmed' | 'invalidated' | 'closed'

export interface ReviewTracking {
  id: string
  userId: string
  sourceSignalId: string | null
  trackingType: ReviewTrackingType | string
  scopeType: string | null
  scopeKey: string | null
  instrumentId: string | null
  /** [V2] Discovery logical identity（追踪 discovery 时填充） */
  discoveryId: string | null
  status: ReviewTrackingStatus | string
  confirmationConditions: Record<string, unknown>
  invalidationConditions: Record<string, unknown>
  note: string | null
  createdAt: string
  closedAt: string | null
}

export type ReviewTrackingListResponse = ReviewPagedResponse<ReviewTracking>

/** POST /v1/review/trackings 请求体（snake_case 对齐后端） */
export interface ReviewTrackingCreateRequest {
  tracking_type: ReviewTrackingType
  source_signal_id?: string | null
  scope_type?: string | null
  scope_key?: string | null
  instrument_id?: string | null
  /** [V2] Discovery logical identity（追踪 discovery 时必填） */
  discovery_id?: string | null
  confirmation_conditions?: Record<string, unknown>
  invalidation_conditions?: Record<string, unknown>
  note?: string | null
  idempotency_key: string
}

/** PATCH /v1/review/trackings/{id} 请求体 */
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
  filter_family?: 'A' | 'B' | 'C' | 'D'
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

// =========================================================================
// [V2] Discovery types
// =========================================================================

export interface DiscoveryMetricState {
  value: number | null
  historyPercentile: number | null
  crossSectionPercentile: number | null
}

export interface DiscoveryMetricChange {
  delta1d: number | null
  delta5d: number | null
}

export interface DiscoveryConcentrationState {
  hhi: number | null
  top5Contribution: number | null
  leaderMedianGap: number | null
}

export interface DiscoveryConcentrationChange {
  direction: string | null
  delta1d: number | null
}

export interface DiscoveryInternalStructure {
  trendBreadth: number | null
  structureBreadth: number | null
  momentumBreadth: number | null
  structureBreakdownDiffusion: number | null
  synchronizedImprovement: boolean
}

export interface DiscoveryState {
  metrics: Record<string, DiscoveryMetricState>
  concentration: DiscoveryConcentrationState
  internalStructure: DiscoveryInternalStructure
}

export interface DiscoveryChange {
  metrics: Record<string, DiscoveryMetricChange>
  concentration: DiscoveryConcentrationChange
}

export interface DiscoveryAnomaly {
  selfHistorical: Record<string, number | null>
  crossSectional: Record<string, number | null>
}

export interface DiscoveryScope {
  type: string
  key: string
  name: string
}

export interface DiscoveryRelatedScope {
  sourceScopeId: string
  targetScopeId: string
  relationType: string
  evidence: Record<string, unknown>
}

export interface DiscoveryRepresentativeInstrument {
  instrumentId: string
  boardRole: string | null
  relationToScope: string | null
  contributionValue: number | null
  contributionRank: number | null
  contributionPayload: unknown
  roleEvidence: unknown
}

export interface DiscoveryLifecycle {
  status: string
  firstSeen: string | null
  duration: number
}

export interface DiscoveryDataQuality {
  coverage: number
  readyCount: number
}

export interface DiscoveryRankKey {
  anomaly: number
  change: number
  evidenceConsistency: number
  crossScopeConfirmation: number
  coverage: number
  duration: number
  breadth: number
}

export interface Discovery {
  discoveryId: string
  reviewRunId: string
  tradeDate: string
  scope: DiscoveryScope
  state: DiscoveryState
  change: DiscoveryChange
  anomaly: DiscoveryAnomaly
  keyEvidence: string[]
  supportingSignalIds: string[]
  relatedScopes: DiscoveryRelatedScope[]
  representativeInstruments: DiscoveryRepresentativeInstrument[]
  lifecycle: DiscoveryLifecycle
  dataQuality: DiscoveryDataQuality
  rankKey: DiscoveryRankKey
}

export interface DiscoveryListResponse {
  trade_date: string
  total: number
  page: number
  page_size: number
  has_more: boolean
  items: Discovery[]
}

export interface DiscoveryDetailResponse {
  trade_date: string
  discovery: Discovery
}

// [CR-03] Update ReviewInstrument with contributionPayload and roleEvidence
// Override the existing interface to add the missing fields
export interface ReviewInstrumentV2 extends ReviewInstrument {
  contributionPayload: unknown
  roleEvidence: unknown
}
