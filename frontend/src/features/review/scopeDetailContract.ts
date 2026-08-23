// [ScopeDetailContract] - 描述: Scope Detail nested contract 解析 owner（Slice E）
//
// 职责：把 backend canonical composition 的嵌套载荷解析为前端展示可用的结构化 ViewModel。
// 原则：
// - 唯一解析 owner；组件绝不散落 `payload as SomeType` 强转。
// - 只做守卫/字段映射/格式，禁止业务重算（EMA/Velocity/Acceleration/Persistence/Phase/
//   Capital Tilt=AW-EW / Migration=1-Jaccard / HHI / leader set / reconciliation）。
// - null != 0、[] != null、unavailable != empty、ready phase=null != unavailable 语义保留。
//
// 纯 TS（无 React / @/ 别名依赖），可被 node --test 直接运行。

import type {
  ReviewScopeComposition,
  ScopePhaseFact,
  ScopeMemberEvidence,
  ScopeReconciliation,
} from './types'

// ============================================================
// 守卫：安全读取原始层（不做业务判断）
// ============================================================

function asNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}

function asString(v: unknown): string | null {
  return typeof v === 'string' ? v : null
}

/**
 * 读取 attribution 子分组（如 direction.positive）下的成员数组。
 * 分组结构 = { members: [...], sum_contribution?, ... }；成员在 .members，而非分组本身。
 */
function readGroupMembers(v: unknown): ScopeMemberEvidence[] | null {
  if (!v || typeof v !== 'object') return null
  const members = (v as Record<string, unknown>).members
  if (!Array.isArray(members)) return null
  return members.filter(
    (item) => item && typeof item === 'object' && 'member_id' in item,
  ) as ScopeMemberEvidence[]
}

/** 构建单个 attribution 子分组；分组不存在（未在载荷中）→ null，空 members → 空组 */
function buildSubGroup(
  parent: Record<string, unknown> | null | undefined,
  key: string,
): ScopeAttributionMemberGroup | null {
  if (!parent) return null
  const g = parent[key]
  if (g === undefined || g === null) return null
  const groupRecord = typeof g === 'object' ? (g as Record<string, unknown>) : null
  return buildGroup(groupRecord, readGroupMembers(g) ?? [])
}

// 可选标量求和（direction/capital_tilt 分组顶层）
function readOptionalNumber(record: Record<string, unknown> | null | undefined, key: string): number | null {
  if (!record) return null
  return asNumber(record[key])
}

function readOptionalString(record: Record<string, unknown> | null | undefined, key: string): string | null {
  if (!record) return null
  return asString(record[key])
}

// ============================================================
// Dynamics
// ============================================================

export interface ScopeDynamicsParsed {
  status: string
  /** 由 dynamics_phase[].trade_date 得到（每个观测一条），与序列逐位对齐 */
  dates: string[]
  position: (number | null)[]
  ema5: (number | null)[]
  ema20: (number | null)[]
  velocity: (number | null)[]
  signal: (number | null)[]
  acceleration: (number | null)[]
  persistence: (number | null)[]
  phaseFacts: ScopePhaseFact[]
}

function toNullableSeries(v: unknown): (number | null)[] {
  if (!Array.isArray(v)) return []
  return v.map((x) => asNumber(x))
}

/** 解析 historical_dynamics 层为结构化 ViewModel（不可用返回 null） */
export function parseDynamicsLayer(
  composition: ReviewScopeComposition | null,
): ScopeDynamicsParsed | null {
  const layer = composition?.historical_dynamics
  if (!layer) return null
  const sd = layer.scope_dynamics
  if (!sd) return null
  const phaseFacts = Array.isArray(sd.dynamics_phase) ? sd.dynamics_phase : []
  const series = sd.historical_dynamics
  const dates = phaseFacts.map((f) => f.trade_date)
  if (!series) {
    return {
      status: layer.status,
      dates,
      position: [],
      ema5: [],
      ema20: [],
      velocity: [],
      signal: [],
      acceleration: [],
      persistence: [],
      phaseFacts,
    }
  }
  return {
    status: layer.status,
    dates,
    position: toNullableSeries(series.position),
    ema5: toNullableSeries(series.ema5),
    ema20: toNullableSeries(series.ema20),
    velocity: toNullableSeries(series.velocity),
    signal: toNullableSeries(series.signal),
    acceleration: toNullableSeries(series.acceleration),
    persistence: toNullableSeries(series.persistence),
    phaseFacts,
  }
}

/**
 * 当前事实来自最后一条 dynamics_phase observation（persisted），
 * 绝不从 chart series 反推。phase=null + ready → 显示 “—”，不是第 7 个 phase。
 */
export function currentPhaseFact(
  dynamics: ScopeDynamicsParsed | null,
): ScopePhaseFact | null {
  if (!dynamics || dynamics.phaseFacts.length === 0) return null
  return dynamics.phaseFacts[dynamics.phaseFacts.length - 1]
}

// ============================================================
// Internal Structure
// ============================================================

export interface ScopeInternalParsed {
  breadth: {
    equalWeightReturn: number | null
    advanceRatio: number | null
    declineRatio: number | null
    unchangedRatio: number | null
    returnDispersion: number | null
  } | null
  capitalTilt: {
    equalWeightReturn: number | null
    amountWeightedReturn: number | null
    capitalTilt: number | null
  } | null
  concentration: {
    priceNormalizedHhi: number | null
    amountNormalizedHhi: number | null
  } | null
}

export function parseInternalStructure(
  composition: ReviewScopeComposition | null,
): ScopeInternalParsed {
  const f = composition?.internal_structure_facts
  if (!f) return { breadth: null, capitalTilt: null, concentration: null }
  const breadth = f.breadth
  const ct = f.capital_tilt
  const con = f.concentration
  return {
    breadth: breadth
      ? {
          equalWeightReturn: asNumber(breadth.equal_weight_return),
          advanceRatio: asNumber(breadth.advance_ratio),
          declineRatio: asNumber(breadth.decline_ratio),
          unchangedRatio: asNumber(breadth.unchanged_ratio),
          returnDispersion: asNumber(breadth.return_dispersion),
        }
      : null,
    capitalTilt: ct
      ? {
          equalWeightReturn: asNumber(ct.equal_weight_return),
          amountWeightedReturn: asNumber(ct.amount_weighted_return),
          capitalTilt: asNumber(ct.capital_tilt),
        }
      : null,
    concentration: con
      ? {
          priceNormalizedHhi: asNumber(con.price_normalized_hhi),
          amountNormalizedHhi: asNumber(con.amount_normalized_hhi),
        }
      : null,
  }
}

// ============================================================
// Leadership
// ============================================================

export interface ScopeLeadershipParsed {
  status: string | null
  reason: string | null
  coverage: number | null
  previousLeaderCount: number | null
  currentLeaderCount: number | null
  retainedCount: number | null
  entrantCount: number | null
  exitCount: number | null
  previousRetention: number | null
  jaccardStability: number | null
  migration: number | null
  previousLeaderIds: string[] | null
  currentLeaderIds: string[] | null
  entrantIds: string[] | null
  exitIds: string[] | null
}

function asStringArray(v: unknown): string[] | null {
  if (!Array.isArray(v)) return null
  return v.map((x) => String(x))
}

export function parseLeadership(
  composition: ReviewScopeComposition | null,
): ScopeLeadershipParsed | null {
  const l = composition?.leadership
  if (!l) return null
  return {
    status: asString(l.status),
    reason: asString(l.reason),
    coverage: asNumber(l.coverage),
    previousLeaderCount: asNumber(l.previous_leader_count),
    currentLeaderCount: asNumber(l.current_leader_count),
    retainedCount: asNumber(l.retained_count),
    entrantCount: asNumber(l.entrant_count),
    exitCount: asNumber(l.exit_count),
    previousRetention: asNumber(l.previous_retention),
    jaccardStability: asNumber(l.jaccard_stability),
    migration: asNumber(l.migration),
    previousLeaderIds: asStringArray(l.previous_leader_ids),
    currentLeaderIds: asStringArray(l.current_leader_ids),
    entrantIds: asStringArray(l.entrant_ids),
    exitIds: asStringArray(l.exit_ids),
  }
}

// ============================================================
// Member Attribution
// ============================================================

export interface ScopeAttributionMemberGroup {
  members: ScopeMemberEvidence[]
  sumContribution?: number | null
  canonicalAwReturn?: number | null
  canonicalEwReturn?: number | null
  sumTiltContribution?: number | null
  priceUniverseCount?: number | null
  awUniverseCount?: number | null
  denominator?: number | null
}

export interface ScopeAttributionSub {
  kind: string
  positive?: ScopeAttributionMemberGroup | null
  negative?: ScopeAttributionMemberGroup | null
  advance?: ScopeAttributionMemberGroup | null
  decline?: ScopeAttributionMemberGroup | null
  unchanged?: ScopeAttributionMemberGroup | null
  unavailable?: ScopeAttributionMemberGroup | null
  retained?: ScopeAttributionMemberGroup | null
  entrants?: ScopeAttributionMemberGroup | null
  exits?: ScopeAttributionMemberGroup | null
  price?: ScopeAttributionMemberGroup | null
  amount?: ScopeAttributionMemberGroup | null
}

export interface ScopeAttributionParsed {
  status: string | null
  direction: ScopeAttributionSub | null
  capitalTilt: ScopeAttributionSub | null
  breadth: ScopeAttributionSub | null
  concentration: ScopeAttributionSub | null
  leadership: ScopeAttributionSub | null
  reconciliation: ScopeReconciliation | null
  determinismChecksum: string | null
}

function buildGroup(
  record: Record<string, unknown> | null | undefined,
  members: ScopeMemberEvidence[] | null,
): ScopeAttributionMemberGroup {
  return {
    members: members ?? [],
    sumContribution: readOptionalNumber(record, 'sum_contribution'),
    canonicalAwReturn: readOptionalNumber(record, 'canonical_aw_return'),
    canonicalEwReturn: readOptionalNumber(record, 'canonical_ew_return'),
    sumTiltContribution: readOptionalNumber(record, 'sum_tilt_contribution'),
    priceUniverseCount: readOptionalNumber(record, 'price_universe_count'),
    awUniverseCount: readOptionalNumber(record, 'aw_universe_count'),
    denominator: readOptionalNumber(record, 'denominator'),
  }
}

function parseAttributionSub(record: Record<string, unknown> | null): ScopeAttributionSub | null {
  if (!record) return null
  return {
    kind: readOptionalString(record, 'kind') ?? 'group',
    positive: buildSubGroup(record, 'positive'),
    negative: buildSubGroup(record, 'negative'),
    advance: buildSubGroup(record, 'advance'),
    decline: buildSubGroup(record, 'decline'),
    unchanged: buildSubGroup(record, 'unchanged'),
    unavailable: buildSubGroup(record, 'unavailable'),
    retained: buildSubGroup(record, 'retained'),
    entrants: buildSubGroup(record, 'entrants'),
    exits: buildSubGroup(record, 'exits'),
    price: buildSubGroup(record, 'price'),
    amount: buildSubGroup(record, 'amount'),
  }
}

export function parseAttribution(
  composition: ReviewScopeComposition | null,
): ScopeAttributionParsed {
  const m = composition?.member_attribution
  if (!m) {
    return {
      status: null,
      direction: null,
      capitalTilt: null,
      breadth: null,
      concentration: null,
      leadership: null,
      reconciliation: null,
      determinismChecksum: null,
    }
  }
  return {
    status: m.status === null ? null : asString(m.status),
    direction: parseAttributionSub(m.direction),
    capitalTilt: parseAttributionSub(m.capital_tilt),
    breadth: parseAttributionSub(m.breadth),
    concentration: parseAttributionSub(m.concentration),
    leadership: parseAttributionSub(m.leadership),
    reconciliation: m.reconciliation,
    determinismChecksum: asString(m.determinism_checksum),
  }
}

// ============================================================
// Raw Facts --- 顶层 observation 分组（按此顺序展示）
// ============================================================

export const OBSERVATION_GROUP_ORDER: ReadonlyArray<string> = [
  'scope',
  'price',
  'trend',
  'structure',
  'momentum',
  'participation',
  'chip',
]

/** 从完整 observation payload 中读取有序的顶层分组（key → value），缺失组不出现。 */
export function observationGroups(
  observation: Record<string, unknown> | null | undefined,
): ReadonlyArray<{ key: string; value: unknown }> {
  if (!observation) return []
  const out: Array<{ key: string; value: unknown }> = []
  for (const key of OBSERVATION_GROUP_ORDER) {
    if (key in observation) {
      out.push({ key, value: observation[key] })
    }
  }
  return out
}