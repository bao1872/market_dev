// [ScopeDetailContract] - 描述: Scope Detail nested contract 解析 owner（Slice E correction）
//
// 职责：把 backend canonical composition 的嵌套载荷解析为前端展示可用的结构化 ViewModel。
// 原则：
// - 唯一解析 owner；组件绝不散落 `payload as SomeType` 强转。
// - 只做守卫/字段映射/格式，禁止业务重算（EMA/Velocity/Acceleration/Persistence/Phase/
//   Capital Tilt=AW-EW / Migration=1-Jaccard / HHI / leader set / reconciliation）。
// - null != 0、[] != null、unavailable != empty、ready phase=null != unavailable 语义保留。
//
// Slice E correction（对齐真实 backend producer 输出）：
// - Dynamics: fact-object 数组，不是 number[]；每个 point 自带 trade_date
// - Attribution: Direction/CapitalTilt/Breadth/Leadership 用直接 MemberEvidence[]；
//   只有 Concentration 用 {members: MemberEvidence[]} 对象
// - Reconciliation: skipped: string[], checks: Record<string, Check>
// - Leadership: previous/current_direction 为 number | null（+1/-1/null）
//
// 纯 TS（无 React / @/ 别名依赖），可被 node --test 直接运行。

import type {
  ReviewScopeComposition,
  ReviewDynamicsPhase,
  ScopePhaseFact,
  ScopeMemberEvidence,
  ScopeReconciliation,
  ScopeReconciliationCheck,
  ScopeDynamicsPositionPoint,
  ScopeDynamicsValuePoint,
  ScopeDynamicsPersistencePoint,
  ScopeAttributionDirectionGroup,
  ScopeAttributionCapitalTiltGroup,
  ScopeAttributionBreadthGroup,
  ScopeAttributionConcentrationGroup,
  ScopeAttributionConcentrationSubGroup,
  ScopeAttributionLeadershipGroup,
  ScopeObservationCurrentState,
  ScopeFreshnessFacts,
  ScopeFreshnessDimension,
  ScopeTechnicalConcentration,
  ScopeTechnicalDispersion,
  ScopeLatestEvents,
  ScopeLatestEventPair,
  ScopeContributionFraction,
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

function asNumberArray(v: unknown): (number | null)[] {
  if (!Array.isArray(v)) return []
  return v.map((x) => asNumber(x))
}

/** 从后端 fact-object 数组提取 value 字段（Position 特化：读 .position 字段，支持 null） */
function extractPositionValues(points: ScopeDynamicsPositionPoint[] | null | undefined): (number | null)[] {
  if (!Array.isArray(points)) return []
  return points.map((p) => {
    const raw = (p as unknown as Record<string, unknown>).position
    if (raw === null || raw === undefined) return null
    return asNumber(raw)
  })
}

/** 从后端 fact-object 数组提取 value 字段（通用：读 .value 字段） */
function extractValuePoints(points: ScopeDynamicsValuePoint[] | null | undefined): (number | null)[] {
  if (!Array.isArray(points)) return []
  return points.map((p) => asNumber((p as unknown as Record<string, unknown>).value))
}

/** 提取日期数组：每个 fact-object 的 trade_date */
function extractDatesFromPosition(points: ScopeDynamicsPositionPoint[] | null | undefined): string[] {
  if (!Array.isArray(points)) return []
  return points.map((p) => String((p as unknown as Record<string, unknown>).trade_date ?? ''))
}

function extractDatesFromValue(points: ScopeDynamicsValuePoint[] | null | undefined): string[] {
  if (!Array.isArray(points)) return []
  return points.map((p) => String((p as unknown as Record<string, unknown>).trade_date ?? ''))
}

/** 从 Attribution 子分组直接数组读取成员（Direction/CapitalTilt/Breadth/Leadership 形状） */
function readDirectMembers(v: unknown): ScopeMemberEvidence[] | null {
  if (!Array.isArray(v)) return null
  return v.filter(
    (item): item is ScopeMemberEvidence =>
      !!item && typeof item === 'object' && 'member_id' in item,
  )
}

// ============================================================
// Dynamics
// ============================================================

export interface ScopeDynamicsParsed {
  status: string
  /** 每个 series 自己的日期数组（from persisted fact-object trade_date） */
  positionDates: string[]
  ema5Dates: string[]
  ema20Dates: string[]
  velocityDates: string[]
  signalDates: string[]
  accelerationDates: string[]
  persistenceDates: string[]
  position: (number | null)[]
  ema5: (number | null)[]
  ema20: (number | null)[]
  velocity: (number | null)[]
  signal: (number | null)[]
  acceleration: (number | null)[]
  /** persistence 保留原始 fact-object（前端只展示不解析） */
  persistence: (number | null)[]
  phaseFacts: ScopePhaseFact[]
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
  if (!series) {
    return {
      status: layer.status,
      positionDates: [], ema5Dates: [], ema20Dates: [],
      velocityDates: [], signalDates: [], accelerationDates: [], persistenceDates: [],
      position: [], ema5: [], ema20: [],
      velocity: [], signal: [], acceleration: [],
      persistence: [],
      phaseFacts,
    }
  }
  const s = series
  return {
    status: layer.status,
    positionDates: extractDatesFromPosition(s.position),
    ema5Dates: extractDatesFromValue(s.ema5),
    ema20Dates: extractDatesFromValue(s.ema20),
    velocityDates: extractDatesFromValue(s.velocity),
    signalDates: extractDatesFromValue(s.signal),
    accelerationDates: extractDatesFromValue(s.acceleration),
    persistenceDates: extractDatesFromValue(s.persistence as unknown as ScopeDynamicsValuePoint[]),
    position: extractPositionValues(s.position),
    ema5: extractValuePoints(s.ema5),
    ema20: extractValuePoints(s.ema20),
    velocity: extractValuePoints(s.velocity),
    signal: extractValuePoints(s.signal),
    acceleration: extractValuePoints(s.acceleration),
    persistence: s.persistence
      ? asNumberArray((s.persistence as ScopeDynamicsPersistencePoint[]).map((p) => (p as unknown as Record<string, unknown>).coverage))
      : [],
    phaseFacts,
  }
}

/**
 * 当前事实来自最后一条 dynamics_phase observation（persisted），
 * 绝不从 chart series 反推。phase=null + ready → 显示 "—"，不是第 7 个 phase。
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
  /** +1/-1/null，来自 persisted backend direction */
  previousDirection: number | null
  currentDirection: number | null
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
    previousDirection: asNumber(l.previous_direction),
    currentDirection: asNumber(l.current_direction),
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
// Member Attribution — 真实后端形状解析
// ============================================================

export interface ScopeAttributionParsed {
  direction: {
    kind: string
    positive: ScopeMemberEvidence[] | null
    negative: ScopeMemberEvidence[] | null
    sumContribution: number | null
    canonicalAwReturn: number | null
  } | null
  capitalTilt: {
    kind: string
    positive: ScopeMemberEvidence[] | null
    negative: ScopeMemberEvidence[] | null
    sumTiltContribution: number | null
    canonicalAwReturn: number | null
    canonicalEwReturn: number | null
    priceUniverseCount: number | null
    awUniverseCount: number | null
  } | null
  breadth: {
    kind: string
    advance: ScopeMemberEvidence[] | null
    decline: ScopeMemberEvidence[] | null
    unchanged: ScopeMemberEvidence[] | null
    unavailable: ScopeMemberEvidence[] | null
    denominator: number | null
  } | null
  concentration: {
    kind: string
    price: {
      members: ScopeMemberEvidence[]
      sumHhi: number | null
      canonicalRawHhi: number | null
      canonicalNormalizedHhi: number | null
    } | null
    amount: {
      members: ScopeMemberEvidence[]
      sumHhi: number | null
      canonicalRawHhi: number | null
      canonicalNormalizedHhi: number | null
    } | null
  } | null
  leadership: {
    kind: string
    retained: ScopeMemberEvidence[] | null
    entrants: ScopeMemberEvidence[] | null
    exits: ScopeMemberEvidence[] | null
  } | null
  reconciliation: {
    violationCount: number | null
    skipped: string[]
    tolerance: number | string | null
    checks: Array<{ key: string; kind: string; pass: boolean | null; resolved: string | null }>
  } | null
  determinismChecksum: string | null
}

function parseDirectionGroup(g: ScopeAttributionDirectionGroup | null): ScopeAttributionParsed['direction'] {
  if (!g) return null
  return {
    kind: g.status ?? 'group',
    positive: readDirectMembers(g.positive),
    negative: readDirectMembers(g.negative),
    sumContribution: asNumber(g.sum_contribution),
    canonicalAwReturn: asNumber(g.canonical_aw_return),
  }
}

function parseCapitalTiltGroup(g: ScopeAttributionCapitalTiltGroup | null): ScopeAttributionParsed['capitalTilt'] {
  if (!g) return null
  return {
    kind: g.status ?? 'group',
    positive: readDirectMembers(g.positive),
    negative: readDirectMembers(g.negative),
    sumTiltContribution: asNumber(g.sum_tilt_contribution),
    canonicalAwReturn: asNumber(g.canonical_aw_return),
    canonicalEwReturn: asNumber(g.canonical_ew_return),
    priceUniverseCount: asNumber(g.price_universe_count),
    awUniverseCount: asNumber(g.aw_universe_count),
  }
}

function parseBreadthGroup(g: ScopeAttributionBreadthGroup | null): ScopeAttributionParsed['breadth'] {
  if (!g) return null
  return {
    kind: g.status ?? 'group',
    advance: readDirectMembers(g.advance),
    decline: readDirectMembers(g.decline),
    unchanged: readDirectMembers(g.unchanged),
    unavailable: readDirectMembers(g.unavailable),
    denominator: asNumber(g.denominator),
  }
}

function parseConcentrationSub(g: ScopeAttributionConcentrationSubGroup | null) {
  if (!g) return null
  return {
    members: readDirectMembers(g.members) ?? [],
    sumHhi: asNumber(g.sum_hhi),
    canonicalRawHhi: asNumber(g.canonical_raw_hhi),
    canonicalNormalizedHhi: asNumber(g.canonical_normalized_hhi),
  }
}

function parseConcentrationGroup(g: ScopeAttributionConcentrationGroup | null): ScopeAttributionParsed['concentration'] {
  if (!g) return null
  return {
    kind: 'group',
    price: parseConcentrationSub(g.price),
    amount: parseConcentrationSub(g.amount),
  }
}

function parseAttributionLeadershipGroup(g: ScopeAttributionLeadershipGroup | null): ScopeAttributionParsed['leadership'] {
  if (!g) return null
  return {
    kind: g.status ?? 'group',
    retained: readDirectMembers(g.retained),
    entrants: readDirectMembers(g.entrants),
    exits: readDirectMembers(g.exits),
  }
}

function parseReconciliation(r: ScopeReconciliation | null): ScopeAttributionParsed['reconciliation'] {
  if (!r) return null
  const checks: Array<{ key: string; kind: string; pass: boolean | null; resolved: string | null }> = []
  if (r.checks && typeof r.checks === 'object') {
    for (const [key, chk] of Object.entries(r.checks)) {
      checks.push({
        key,
        kind: String((chk as ScopeReconciliationCheck).kind ?? key),
        pass: (chk as ScopeReconciliationCheck).pass ?? null,
        resolved: (chk as ScopeReconciliationCheck).resolved ?? null,
      })
    }
  }
  return {
    violationCount: r.violation_count,
    skipped: Array.isArray(r.skipped) ? r.skipped : [],
    tolerance: r.tolerance,
    checks,
  }
}

export function parseAttribution(
  composition: ReviewScopeComposition | null,
): ScopeAttributionParsed {
  const m = composition?.member_attribution
  if (!m) {
    return {
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
    direction: parseDirectionGroup(m.direction),
    capitalTilt: parseCapitalTiltGroup(m.capital_tilt),
    breadth: parseBreadthGroup(m.breadth),
    concentration: parseConcentrationGroup(m.concentration),
    leadership: parseAttributionLeadershipGroup(m.leadership),
    reconciliation: parseReconciliation(m.reconciliation),
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
  'freshness',
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

// ============================================================
// [R1] Current Snapshot — 单一解析 owner（prompt §3、§11）
// ============================================================
//
// 职责：把 backend persisted Composition / Observation 中 Current Tab 实际消费的
// 嵌套载荷解析为前端展示 ViewModel。
// 原则（与文件顶部一致）：
// - 唯一解析 owner；组件绝不散落 `payload.xxx as SomeType`。
// - 只做守卫/字段映射/格式，禁止业务重算（Phase/score/信号/HHI/集中度/广度/
//   capital tilt=AW-EW/leader strength/动量-趋势分类/freshness/decay 全部来自 persisted）。
// - null != 0、[] != null、unavailable != empty 语义保留。
// - 内部复用已有解析 owner：currentPhaseFact(parseDynamicsLayer(...)) / parseInternalStructure(...)。

/** Current Regime 展示 ViewModel（来自 persisted dynamics_phase 末尾 fact） */
export interface ScopeCurrentRegime {
  status: string | null
  phase: ReviewDynamicsPhase | null
  position: number | null
  velocity: number | null
  acceleration: number | null
  upperOccupancy: number | null
  lowerOccupancy: number | null
}

/** Current 身份（来自已加载的 Scope list item，不发起新请求；eligible/provided/coverage） */
export interface ScopeCurrentIdentity {
  eligibleCount: number | null
  providedCount: number | null
  coverageRatio: number | null
}

/** Current Breadth & Participation 展示 ViewModel（来自 composition.internal_structure_facts） */
export interface ScopeCurrentParticipation {
  equalWeightReturn: number | null
  amountWeightedReturn: number | null
  capitalTilt: number | null
  advanceRatio: number | null
  declineRatio: number | null
  unchangedRatio: number | null
  returnDispersion: number | null
}

/** Current Snapshot 顶层 ViewModel（projection layer only，persisted facts） */
export interface ScopeCurrentSnapshot {
  regime: ScopeCurrentRegime | null
  identity: ScopeCurrentIdentity | null
  participation: ScopeCurrentParticipation | null
  currentState: ScopeObservationCurrentState | null
  freshness: ScopeFreshnessFacts | null
}

export interface ParseCurrentSnapshotArgs {
  composition: ReviewScopeComposition | null
  observation: Record<string, unknown> | null
  /** 来自已加载 Scope list item 的 identity；不传则 identity=null（不发起新请求） */
  identity?: ScopeCurrentIdentity | null
}

/** 安全读取嵌套对象（排除数组与 null/undefined） */
function asObj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null
}

function parseCurrentRegime(
  composition: ReviewScopeComposition | null,
): ScopeCurrentRegime | null {
  // 来源：末尾 persisted dynamics_phase fact；绝不从 chart series 反推（prompt §4）。
  const f = currentPhaseFact(parseDynamicsLayer(composition))
  if (!f) return null
  return {
    status: asString(f.status),
    phase: f.phase,
    position: asNumber(f.position),
    velocity: asNumber(f.velocity),
    acceleration: asNumber(f.acceleration),
    upperOccupancy: asNumber(f.upper_occupancy),
    lowerOccupancy: asNumber(f.lower_occupancy),
  }
}

function parseCurrentParticipation(
  composition: ReviewScopeComposition | null,
): ScopeCurrentParticipation | null {
  const f = composition?.internal_structure_facts
  if (!f) return null
  const ct = f.capital_tilt
  const b = f.breadth
  // capitalTilt 直接读 persisted，绝不重算 amountWeightedReturn - equalWeightReturn（CURRENT-5）。
  return {
    equalWeightReturn: asNumber(ct?.equal_weight_return),
    amountWeightedReturn: asNumber(ct?.amount_weighted_return),
    capitalTilt: asNumber(ct?.capital_tilt),
    advanceRatio: asNumber(b?.advance_ratio),
    declineRatio: asNumber(b?.decline_ratio),
    unchangedRatio: asNumber(b?.unchanged_ratio),
    returnDispersion: asNumber(b?.return_dispersion),
  }
}

function parseLatestEvents(le: Record<string, unknown>): ScopeLatestEvents {
  const pair = (v: unknown): ScopeLatestEventPair | null => {
    const o = asObj(v)
    if (!o) return null
    return { up: asNumber(o.up), down: asNumber(o.down) }
  }
  return {
    bos: pair(le.bos),
    choch: pair(le.choch),
    ob: pair(le.ob),
    eqh: asNumber(le.eqh),
    eql: asNumber(le.eql),
  }
}

function parseConcentration(c: Record<string, unknown>): ScopeTechnicalConcentration {
  const frac = (v: unknown): ScopeContributionFraction | null => {
    const o = asObj(v)
    if (!o) return null
    return { numerator: asNumber(o.numerator), denominator: asNumber(o.denominator) }
  }
  return {
    top3_contribution: frac(c.top3_contribution),
    top5_contribution: frac(c.top5_contribution),
    hhi: asNumber(c.hhi),
    leader_symbol: asString(c.leader_symbol),
    leader_magnitude: asNumber(c.leader_magnitude),
    median_magnitude: asNumber(c.median_magnitude),
    leader_median_gap: asNumber(c.leader_median_gap),
    count: asNumber(c.count),
  }
}

function parseDispersion(d: Record<string, unknown>): ScopeTechnicalDispersion {
  return {
    count: asNumber(d.count),
    mean: asNumber(d.mean),
    std: asNumber(d.std),
    cv: asNumber(d.cv),
    p25: asNumber(d.p25),
    p50: asNumber(d.p50),
    p75: asNumber(d.p75),
    iqr: asNumber(d.iqr),
    range: asNumber(d.range),
  }
}

function parseCurrentState(
  observation: Record<string, unknown> | null,
): ScopeObservationCurrentState | null {
  const structure = asObj(observation?.structure)
  const cs = asObj(structure?.current_state)
  if (!cs) return null
  const tech = asObj(cs.technical_state)
  const conc = asObj(tech?.concentration)
  const disp = asObj(tech?.dispersion)
  const le = asObj(cs.latest_events)
  return {
    board_ready_member_count: asNumber(cs.board_ready_member_count),
    mean_active_orderblock_count: asNumber(cs.mean_active_orderblock_count),
    latest_events: le ? parseLatestEvents(le) : null,
    technical_state: tech
      ? {
          concentration: conc ? parseConcentration(conc) : null,
          dispersion: disp ? parseDispersion(disp) : null,
        }
      : null,
  }
}

function parseFreshnessDimension(d: Record<string, unknown> | null): ScopeFreshnessDimension | null {
  if (!d) return null
  return {
    window_days: asNumber(d.window_days),
    event_count: asNumber(d.event_count),
    weighted_sum: asNumber(d.weighted_sum),
    density: asNumber(d.density),
  }
}

function parseFreshness(
  observation: Record<string, unknown> | null,
): ScopeFreshnessFacts | null {
  const f = asObj(observation?.freshness)
  if (!f) return null
  const bd = asObj(f.by_dimension)
  return {
    today_count: asNumber(f.today_count),
    last_5d_count: asNumber(f.last_5d_count),
    last_10d_count: asNumber(f.last_10d_count),
    last_20d_count: asNumber(f.last_20d_count),
    instrument_count: asNumber(f.instrument_count),
    by_dimension: bd
      ? {
          trend: parseFreshnessDimension(asObj(bd.trend)),
          structure: parseFreshnessDimension(asObj(bd.structure)),
          momentum: parseFreshnessDimension(asObj(bd.momentum)),
          chip: parseFreshnessDimension(asObj(bd.chip)),
        }
      : null,
    decay_weighted_density: asNumber(f.decay_weighted_density),
  }
}

/**
 * 解析 Current Snapshot ViewModel（唯一解析 owner）。
 * 输入：composition（Regime/Participation/identity 来源）、observation（Current State/Freshness 来源）、
 *       identity（来自已加载 list item 的 eligible/provided/coverage，可选）。
 * 不发起任何请求；所有值来自 persisted backend facts。
 */
export function parseCurrentSnapshot(args: ParseCurrentSnapshotArgs): ScopeCurrentSnapshot {
  const { composition, observation, identity } = args
  return {
    regime: parseCurrentRegime(composition),
    identity: identity ?? null,
    participation: parseCurrentParticipation(composition),
    currentState: parseCurrentState(observation),
    freshness: parseFreshness(observation),
  }
}
