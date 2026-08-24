// [ReviewTypes] - 描述: 复盘模块 TypeScript 类型定义
// 对应后端 schemas/review.py（字段命名与后端 JSON 序列化保持一致）
// 规则：前端不计算 P/Q/U/C/V、筛选器或归因，只承载结构化展示
// PRD §12 / §15
// Slice F：物理删除 Legacy P/Q/U/C/V / Signal / Attribution / Instrument /
// Tracking / Discovery 类型家族，仅保留 canonical Scope-first 合同。

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
  /**
   * [R0 2026-08-24] Legacy Board lineage pointer。
   * Unified Review 现在是所有板块复盘事实的唯一当前 owner；当前 run 的 sourceBoardRunId
   * 恒为 null（正确的常态，既非异常也非降级）。仅历史/回溯数据中可能非空，供审计。
   * 前端不得将其作为 Review 的前置条件或 runtime owner 展示。
   */
  sourceBoardRunId: string | null
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
  coverageRatio: number | null
  expectedScopeCount: number
  succeededScopeCount: number
  failedScopeCount: number
  signalCount: number
  startedAt: string | null
  completedAt: string | null
  publishedAt: string | null
}

/** 分页响应通用结构（保留给 canonical Scope 复用） */
export interface ReviewPagedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

// ============================================================
// [CANONICAL] API 查询参数
// ============================================================

export interface ReviewScopeListParams {
  scope_type?: ReviewScopeFamily
  include_partial?: boolean
  page?: number
  page_size?: number
}

// ============================================================
// 前端枚举常量（展示用，禁止用作业务计算）
// ============================================================

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

// ------------------------------------------------------------
// [Slice E] Scope Detail 嵌套合同类型化
// 只类型化 Slice E 实际消费的 nested contracts；未消费的深层载荷保持
// Record<string, unknown> | null。Inner canonical composition 键保持后端
// snake_case；前端只承载、不重算。禁止 any。
// ------------------------------------------------------------

/** Historical Dynamics fact-object 形状（one entry per trading observation，never compressed） */

/** Position series: persisted position fact object
 *  position = number when status == "ready"
 *  position = null when status == "insufficient_history" or "unavailable_current" */
export interface ScopeDynamicsPositionPoint {
  trade_date: string
  position: number | null
  status: string | null
  history?: unknown
}

/** EMA / derived series: persisted value fact object with validity metadata */
export interface ScopeDynamicsValuePoint {
  trade_date: string
  value: number | null
  status: string | null
  valid_count?: number | null
  span?: number | null
}

/** Persistence series: full window fact object */
export interface ScopeDynamicsPersistencePoint {
  trade_date: string
  window_size: number | null
  minimum_valid_count: number | null
  candidate_count: number | null
  valid_count: number | null
  coverage: number | null
  upper_count: number | null
  lower_count: number | null
  upper_occupancy: number | null
  lower_occupancy: number | null
  status: string | null
}

/** Historical Dynamics 真实后端输出形状（fact-object 数组，非 number[]） */
export interface ScopeHistoricalDynamicsSeries {
  position: ScopeDynamicsPositionPoint[]
  ema5: ScopeDynamicsValuePoint[]
  ema20: ScopeDynamicsValuePoint[]
  velocity: ScopeDynamicsValuePoint[]
  signal: ScopeDynamicsValuePoint[]
  acceleration: ScopeDynamicsValuePoint[]
  persistence: ScopeDynamicsPersistencePoint[]
}

/** 单个交易观测的 dynamics phase 事实（persisted，非前端重算） */
export interface ScopePhaseFact {
  trade_date: string
  phase: ReviewDynamicsPhase | null
  status: string | null
  position: number | null
  velocity: number | null
  acceleration: number | null
  upper_occupancy: number | null
  lower_occupancy: number | null
  velocity_state: string | null
  acceleration_state: string | null
  high_regime: string | null
  bottom_recovery_context: string | null
}

/** historical_dynamics 下的 runtime 应用对象 */
export interface ScopeScopeDynamics {
  historical_dynamics: ScopeHistoricalDynamicsSeries | null
  dynamics_phase: ScopePhaseFact[] | null
}

/** Composition.historical_dynamics 层 */
export interface ScopeDynamicsLayer {
  status: string
  scope: Record<string, unknown> | null
  membership: Record<string, unknown> | null
  observation_series: Record<string, unknown> | null
  scope_dynamics: ScopeScopeDynamics | null
  metrics: Record<string, unknown> | null
}

/** Internal Structure: Breadth 事实（无 composite score） */
export interface ScopeBreadthFacts {
  equal_weight_return: number | null
  advance_ratio: number | null
  decline_ratio: number | null
  unchanged_ratio: number | null
  return_dispersion: number | null
}

/** Internal Structure: Capital Tilt 事实（persisted capital_tilt，不重算 AW-EW） */
export interface ScopeCapitalTiltFacts {
  equal_weight_return: number | null
  amount_weighted_return: number | null
  capital_tilt: number | null
}

/** Internal Structure: Concentration 事实 */
export interface ScopeConcentrationFacts {
  price_normalized_hhi: number | null
  amount_normalized_hhi: number | null
}

/** Composition.internal_structure_facts 层 */
export interface ScopeInternalStructureFacts {
  breadth: ScopeBreadthFacts | null
  capital_tilt: ScopeCapitalTiltFacts | null
  concentration: ScopeConcentrationFacts | null
}

/** Composition.leadership 层：T-1 → T leader set 迁移事实。
 *  Unavailable 侧用 null，绝非 0；empty array 与 null 必须区分。
 *  previous_direction / current_direction 为 +1/-1/null（number），不是 string。
 *  unavailable_snapshot / empty_leader_set 时仍可能保留有效 evidence。 */
export interface ScopeLeadershipLayer {
  status: string | null
  reason: string | null
  coverage: number | null
  /** +1 / -1 / null，不是 string */
  previous_direction: number | null
  current_direction: number | null
  previous_rankable_count: number | null
  current_rankable_count: number | null
  previous_leader_count: number | null
  current_leader_count: number | null
  retained_count: number | null
  entrant_count: number | null
  exit_count: number | null
  previous_retention: number | null
  jaccard_stability: number | null
  migration: number | null
  previous_leader_ids: string[] | null
  current_leader_ids: string[] | null
  entrant_ids: string[] | null
  exit_ids: string[] | null
}

/** Member Attribution 成员证据（direction/orientation 成员；缺失字段保持 null，不伪造 0）
 *  不同子分组使用不同字段语义：
 *  - Direction: contribution
 *  - Capital Tilt: tilt_contribution, aw_weight
 *  - Breadth: return_1d
 *  - Concentration: concentration_weight, hhi_contribution
 *  - Leadership: aligned_contribution */
export interface ScopeMemberEvidence {
  member_id: string | number
  member_name?: string | null
  return_1d?: number | null
  amount?: number | null
  amount_share?: number | null
  aw_weight?: number | null
  ew_weight?: number | null
  contribution?: number | null
  canonical_contribution?: number | null
  tilt_contribution?: number | null
  aligned_contribution?: number | null
  concentration_weight?: number | null
  hhi_contribution?: number | null
  in_price_universe?: boolean | null
  in_aw_universe?: boolean | null
  [k: string]: unknown
}

/** Reconciliation 单条 check（后端 key → check map，前端按 Object.entries 展示） */
export interface ScopeReconciliationCheck {
  pass: boolean | null
  resolved: string | null
  kind: string
  [k: string]: unknown
}

/** Reconciliation 完整性诊断（前端只展示，不重跑） */
export interface ScopeReconciliation {
  violation_count: number | null
  skipped: string[]
  tolerance: number | string | null
  checks: Record<string, ScopeReconciliationCheck>
  [k: string]: unknown
}

/** Composition.member_attribution 层 — 真实后端形状（Slice E correction）。
 *  各子分组有不同结构：Direction/CapitalTilt/Breadth/Leadership 用直接 MemberEvidence[]；
 *  只有 Concentration 使用 {members: MemberEvidence[]} 对象。 */

/** Direction 子分组：positive/negative 直接是 MemberEvidence[] */
export interface ScopeAttributionDirectionGroup {
  status?: string | null
  aw_universe_count?: number | null
  positive: ScopeMemberEvidence[] | null
  negative: ScopeMemberEvidence[] | null
  sum_contribution?: number | null
  canonical_aw_return?: number | null
}

/** Capital Tilt 子分组：positive/negative 直接是 MemberEvidence[] */
export interface ScopeAttributionCapitalTiltGroup {
  status?: string | null
  price_universe_count?: number | null
  aw_universe_count?: number | null
  positive: ScopeMemberEvidence[] | null
  negative: ScopeMemberEvidence[] | null
  sum_tilt_contribution?: number | null
  canonical_aw_return?: number | null
  canonical_ew_return?: number | null
}

/** Breadth 子分组：advance/decline/unchanged/unavailable 直接是 MemberEvidence[] */
export interface ScopeAttributionBreadthGroup {
  status?: string | null
  denominator?: number | null
  advance: ScopeMemberEvidence[] | null
  decline: ScopeMemberEvidence[] | null
  unchanged: ScopeMemberEvidence[] | null
  unavailable: ScopeMemberEvidence[] | null
}

/** Concentration 子分组：price/amount 使用 {members: MemberEvidence[]} 对象 */
export interface ScopeAttributionConcentrationSubGroup {
  members: ScopeMemberEvidence[]
  sum_hhi?: number | null
  canonical_raw_hhi?: number | null
  canonical_normalized_hhi?: number | null
}

export interface ScopeAttributionConcentrationGroup {
  price: ScopeAttributionConcentrationSubGroup | null
  amount: ScopeAttributionConcentrationSubGroup | null
}

/** Leadership 子分组：retained/entrants/exits 直接是 MemberEvidence[] */
export interface ScopeAttributionLeadershipGroup {
  status?: string | null
  reason?: string | null
  previous_direction?: number | null
  current_direction?: number | null
  retained: ScopeMemberEvidence[] | null
  entrants: ScopeMemberEvidence[] | null
  exits: ScopeMemberEvidence[] | null
}

/** Composition.member_attribution 顶层（后端固定 8-key，无顶层 status） */
export interface ScopeMemberAttributionLayer {
  scope: Record<string, unknown> | null
  direction: ScopeAttributionDirectionGroup | null
  capital_tilt: ScopeAttributionCapitalTiltGroup | null
  breadth: ScopeAttributionBreadthGroup | null
  concentration: ScopeAttributionConcentrationGroup | null
  leadership: ScopeAttributionLeadershipGroup | null
  reconciliation: ScopeReconciliation | null
  determinism_checksum: string | null
}

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
 * [Slice E] historical_dynamics / internal_structure_facts / leadership / member_attribution
 * 已针对实际消费的 nested contracts 做真值类型化；capability / scope_observation 仍为
 * Record<string, unknown>（Slice E 不深读其叶子）。
 */
export interface ReviewScopeComposition {
  scope: {
    scope_type: string
    scope_key: string
  }
  trade_date: string
  capability: Record<string, unknown>
  scope_observation: Record<string, unknown> | null
  historical_dynamics: ScopeDynamicsLayer | null
  internal_structure_facts: ScopeInternalStructureFacts | null
  leadership: ScopeLeadershipLayer | null
  member_attribution: ScopeMemberAttributionLayer | null
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
