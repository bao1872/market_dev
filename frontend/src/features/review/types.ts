// [ReviewTypes] - 描述: 复盘模块 TypeScript 类型定义
// 对应后端 schemas/review.py（字段命名与后端 JSON 序列化保持一致）
// 规则：前端不计算 P/Q/U/C/V、筛选器或归因，只承载结构化展示
// PRD §7.1 / §12 / §15

// ============================================================
// [LEGACY] P/Q/U/C/V 指标合同（PRD §7.1， retired 于 canonical Scope 模型之后）
// Slice C 起仅保留为 legacy 类型，禁止在 canonical 路径中使用。
// 不得构造 canonical→legacy 适配器伪造 p/q/u/c/v/signalCount。
// ============================================================

/** [LEGACY] P/Q/U/C/V 单个 component（PRD §7.1 components 元素） */
export interface LegacyReviewMetricComponent {
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
  readiness: LegacyReviewMetricReadiness
}

export interface LegacyReviewMetricReadiness {
  raw_ready?: boolean
  normalized_ready?: boolean
  status?: string
  reason?: string | null
  history_observations?: number | null
  min_required?: number | null
}

/** [LEGACY] P/Q/U/C/V 单个聚合变量 payload（PRD §7.1 通用结构） */
export interface LegacyReviewMetricPayload {
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
  components: LegacyReviewMetricComponent[]
  coverage: number | null
  /** 状态：ready/insufficient_history/partial/unavailable */
  status: string
  readiness: LegacyReviewMetricReadiness
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
// [LEGACY] 市场扫描（PRD §12.2，P/Q/U/C/V 形态，retired）
// Slice C 起仅作 legacy 类型，禁止在 canonical Scope 路径中使用。
// ============================================================

/** [LEGACY] GET /v1/review/{trade_date}/scopes 旧 P/Q/U/C/V 单条记录 */
export interface LegacyReviewScopeMetrics {
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
  p: LegacyReviewMetricPayload | null
  q: LegacyReviewMetricPayload | null
  u: LegacyReviewMetricPayload | null
  c: LegacyReviewMetricPayload | null
  v: LegacyReviewMetricPayload | null
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

export type LegacyReviewScopeListResponse = ReviewPagedResponse<LegacyReviewScopeMetrics>

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

/**
 * [LEGACY] 旧 scopes 列表参数（含 parent_scope_type/parent_scope_key）。
 * Slice C 起 retired；canonical 列表参数见 ReviewScopeListParams。
 */
export interface LegacyReviewScopeListParams {
  scope_type?: string
  parent_scope_type?: string
  parent_scope_key?: string
  include_partial?: boolean
  page?: number
  page_size?: number
}

// ============================================================
// [CANONICAL] Scope-first 列表参数（仅后端支持的字段）
// 不含 parent_scope_type / parent_scope_key（后端无此过滤）。
// 不添加任何前端-only 过滤字段。
// ============================================================

export interface ReviewScopeListParams {
  scope_type?: ReviewScopeFamily
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

/** [LEGACY] P/Q/U/C/V 聚合变量标识 */
export type LegacyMetricKey = 'p' | 'q' | 'u' | 'c' | 'v'

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
  symbol: string | null
  name: string | null
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

// =========================================================================
// [CANONICAL] Scope-first Review 合同（Slice C 引入）
// 对应后端 schemas/review.py 的 ReviewScopeSummaryDTO / ReviewCanonicalScopeResponse / ReviewScopeCompositionDetailResponse。
// 字段命名与后端 JSON 序列化保持一致。
// 规则：前端不重新计算 phase / capital tilt / migration / score / ranking；
//       所有分析字段为 nullable，缺失（Composition 不存在或 JSON 缺键）= null，绝不伪造 0。
// =========================================================================

// ------------------------------------------------------------
// 枚举
// ------------------------------------------------------------

/** Scope 族（对应后端 scope_type 合法值） */
export type ReviewScopeFamily =
  | 'industry_l1'
  | 'industry_l2'
  | 'industry_l3'
  | 'concept'

/**
 * Dynamics Phase 合法词表（后端 canonical vocabulary）。
 * 不含第七个 fallback phase；未匹配/缺失 phase 保持 null。
 */
export type ReviewDynamicsPhase =
  | 'Early Lift'
  | 'Strengthening'
  | 'Sustained'
  | 'Decelerating'
  | 'Weakening'
  | 'Repairing'

/** Composition readiness 合法值 */
export type ReviewCompositionReadiness =
  | 'ready'
  | 'insufficient_history'
  | 'unavailable_current'

// ------------------------------------------------------------
// Scope 列表项（GET /v1/review/{trade_date}/scopes）
// ------------------------------------------------------------

/** 单 Scope 的薄投影分析字段；Composition 缺失时整体为 null */
export interface ReviewScopeSummary {
  dynamicsStatus: string | null
  phase: ReviewDynamicsPhase | null
  position: number | null
  velocity: number | null
  acceleration: number | null
  upperOccupancy: number | null
  lowerOccupancy: number | null
  equalWeightReturn: number | null
  amountWeightedReturn: number | null
  capitalTilt: number | null
  advanceRatio: number | null
  declineRatio: number | null
  unchangedRatio: number | null
  returnDispersion: number | null
  priceNormalizedHhi: number | null
  amountNormalizedHhi: number | null
  leadershipStatus: string | null
  jaccardStability: number | null
  migration: number | null
}

/** 列表单条记录：Scope 身份 + readiness + 薄投影（永远不含 p/q/u/c/v/signalCount） */
export interface ReviewScopeListItem {
  scopeType: ReviewScopeFamily
  scopeKey: string
  scopeName: string | null
  readiness: ReviewCompositionReadiness | string
  status: string
  eligibleCount: number
  providedCount: number
  coverageRatio: number | null
  summary: ReviewScopeSummary | null
}

/** 列表分页响应 */
export interface ReviewScopeListResponse {
  items: ReviewScopeListItem[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

// ------------------------------------------------------------
// Scope 详情（GET /v1/review/{trade_date}/scopes/{scope_type}/{scope_key}）
// ------------------------------------------------------------

/**
 * Composition 顶层 9-key 稳定结构（canonical owner `canonical_composition.py` 产出，前端只承载、不重算）。
 * 对应后端返回的固定 9-key（scope / trade_date / capability / scope_observation /
 * historical_dynamics / internal_structure_facts / leadership / member_attribution /
 * composition_readiness）。
 *
 * 关键纠正（Slice C 修复）：
 * - scope 仅有 scope_type / scope_key；owner 不输出 scope_name，也不在 scope 内嵌 trade_date。
 * - trade_date 是顶层 key，不在 scope 内。
 * - composition_readiness 是合并后的字符串 status（'ready'/'insufficient_history'/'unavailable_current'），不是 object。
 * - scope_observation / historical_dynamics / internal_structure_facts / leadership /
 *   member_attribution 在 owner 上允许为 None（非必产层），必须保留 nullable 语义。
 *
 * 未知/原始叶子载荷用 Record<string, unknown>，禁止 any，禁止前端重算。
 */
export interface ReviewScopeComposition {
  scope: {
    scope_type: string
    scope_key: string
  }
  trade_date: string
  capability: Record<string, unknown>
  scope_observation: Record<string, unknown> | null
  historical_dynamics: Record<string, unknown> | null
  internal_structure_facts: Record<string, unknown> | null
  leadership: Record<string, unknown> | null
  member_attribution: Record<string, unknown> | null
  composition_readiness: ReviewCompositionReadiness
}

/**
 * 详情响应：对应后端 ReviewScopeCompositionDetailResponse（9-key composition + 完整 Observation payload）。
 *
 * 关键纠正（Slice C 修复）：`observation` 是后端 fact.observation_payload 的完整
 * Canonical Observation Core payload（dict[str, Any]），**不是** Slice B 的 ReviewScopeSummary。
 * 前端不得将其按 Summary 字段读取，也不得在 frontend 重算/Duplicate Observation。
 * 待 Slice E 真正消费各组字段时，再依据 canonical observation owner 精确定义 nested types。
 */
export interface ReviewScopeCompositionDetailResponse {
  reviewRunId: string
  tradeDate: string
  scopeType: string
  scopeKey: string
  scopeName: string | null
  algorithmVersion: string
  composition: ReviewScopeComposition | null
  observation: Record<string, unknown> | null
}
