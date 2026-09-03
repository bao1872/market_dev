// [R3C-SMC] SMC 结构页 canonical Observation / history.smc 合同 owner（pure TS）。
//
// 单一确定性解析器：消费 scope_observation.compute_scope_observation 产出的
// canonical L1 Observation 的 structure 段，以及 review_scope_diagnostics_service
// 投影的 history.smc。仅读取 canonical key 并格式化；不重算、不打分、不另立别名。
//
// 锁定（prompt §六 / §七）：
// - Swing / Internal up-neutral-down 状态键
// - transition denominator（swing / internal）
// - changed_members（按 member_id 稳定排序，只列真变化）
// - BOS / CHoCH 精确事件 cell（event_type / direction / structure_level）
// - member_count 与 event_count 必须分别保留（前端不得混成一个数字）
// - Alignment（Aligned / Divergent）
// - trailing top / bottom（current-only 分布 median）
//
// 组件（ScopeSmcPanel）不得 deepGet raw payload、不得自建 formatter、不得猜 canonical key。
import {
  displayMember,
  type MemberDirectory,
  formatPercentNullable,
  formatNumberNullable,
  NULL_DISPLAY,
} from './reviewFormat'
import type {
  ReviewStructureState,
  ReviewStructureEvents,
  ReviewScopeSmcHistoryDTO,
} from './types'

type Json = Record<string, unknown>

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function deepGet(root: unknown, path: readonly string[]): unknown {
  let node: unknown = root
  for (const key of path) {
    if (node === null || typeof node !== 'object' || !(key in (node as Json))) {
      return undefined
    }
    node = (node as Json)[key]
  }
  return node
}

// ---------------------------------------------------------------------------
// Facts（canonical 读取，不格式化）
// ---------------------------------------------------------------------------

export interface SmcStateFacts {
  upRatio: number | null
  neutralRatio: number | null
  downRatio: number | null
  denominator: number | null
}

export interface SmcChangedMember {
  memberId: string
  previousState: string
  currentState: string
}

export interface SmcAlignmentFacts {
  alignedRatio: number | null
  divergentRatio: number | null
  denominator: number | null
}

export interface SmcTrailingDistance {
  median: number | null
  p25: number | null
  p75: number | null
  validCount: number | null
  denominator: number | null
}

export interface SmcBosChochCell {
  eventType: string // BOS | CHoCH
  direction: string | null // Up | Down（canonical 保证非 null，但解析层允许 null 以防畸形）
  structureLevel: string | null // Swing | Internal
  memberCount: number | null
  memberRatio: number | null
  eventCount: number | null
}

export interface SmcSecondaryCell {
  eventType: string // OB_CREATED | OB_ENTERED | OB_MITIGATED | EQH | EQL
  direction: string | null
  structureLevel: string | null
  memberCount: number | null
  memberRatio: number | null
  eventCount: number | null
}

export interface SmcEventFacts {
  status: string | null // ready | unavailable
  denominator: number | null
  bosChoch: SmcBosChochCell[]
  secondary: SmcSecondaryCell[]
}

export interface SmcObservationFacts {
  swingState: SmcStateFacts | null
  internalState: SmcStateFacts | null
  swingChangedMembers: SmcChangedMember[]
  internalChangedMembers: SmcChangedMember[]
  transitionDenominator: { swing: number | null; internal: number | null }
  alignment: SmcAlignmentFacts | null
  trailingTop: SmcTrailingDistance | null
  trailingBottom: SmcTrailingDistance | null
  events: SmcEventFacts | null
}

// ---------------------------------------------------------------------------
// Parsers（canonical keys only）
// ---------------------------------------------------------------------------

function parseState(node: unknown): SmcStateFacts | null {
  if (!node || typeof node !== 'object') return null
  const s = node as Json
  const num = (k: string): number | null =>
    isFiniteNumber(s[k]) ? (s[k] as number) : null
  return {
    upRatio: num('up_ratio'),
    neutralRatio: num('neutral_ratio'),
    downRatio: num('down_ratio'),
    denominator: num('denominator'),
  }
}

function parseChangedMembers(node: unknown): SmcChangedMember[] {
  if (!node || typeof node !== 'object') return []
  const t = node as Json
  const raw = Array.isArray(t['changed_members']) ? (t['changed_members'] as Json[]) : []
  return raw.map((m) => ({
    memberId:
      typeof m['member_id'] === 'string'
        ? (m['member_id'] as string)
        : String(m['member_id'] ?? ''),
    previousState: typeof m['previous_state'] === 'string' ? (m['previous_state'] as string) : '',
    currentState: typeof m['current_state'] === 'string' ? (m['current_state'] as string) : '',
  }))
}

function parseTransitionDenominator(node: unknown): number | null {
  if (!node || typeof node !== 'object') return null
  const t = node as Json
  return isFiniteNumber(t['denominator']) ? (t['denominator'] as number) : null
}

function parseAlignment(node: unknown): SmcAlignmentFacts | null {
  if (!node || typeof node !== 'object') return null
  const a = node as Json
  const num = (k: string): number | null =>
    isFiniteNumber(a[k]) ? (a[k] as number) : null
  return {
    alignedRatio: num('aligned_ratio'),
    divergentRatio: num('divergent_ratio'),
    denominator: num('denominator'),
  }
}

function parseTrailing(node: unknown): SmcTrailingDistance | null {
  if (!node || typeof node !== 'object') return null
  const t = node as Json
  const num = (k: string): number | null =>
    isFiniteNumber(t[k]) ? (t[k] as number) : null
  return {
    median: num('median'),
    p25: num('p25'),
    p75: num('p75'),
    validCount: num('valid_count'),
    denominator: num('denominator'),
  }
}

function parseEvents(node: unknown): SmcEventFacts | null {
  if (!node || typeof node !== 'object') return null
  const e = node as Json
  const status = typeof e['status'] === 'string' ? (e['status'] as string) : null
  const denominator = isFiniteNumber(e['denominator']) ? (e['denominator'] as number) : null
  const cells = e['cells']
  const leveled: Json =
    cells && typeof cells === 'object' && (cells as Json)['leveled']
      ? ((cells as Json)['leveled'] as Json)
      : {}
  const extreme: Json =
    cells && typeof cells === 'object' && (cells as Json)['extreme']
      ? ((cells as Json)['extreme'] as Json)
      : {}
  const bosChoch: SmcBosChochCell[] = []
  const secondary: SmcSecondaryCell[] = []
  for (const cell of Object.values(leveled)) {
    if (!cell || typeof cell !== 'object') continue
    const c = cell as Json
    const et = typeof c['event_type'] === 'string' ? (c['event_type'] as string) : ''
    const item: SmcBosChochCell | SmcSecondaryCell = {
      eventType: et,
      direction: typeof c['direction'] === 'string' ? (c['direction'] as string) : null,
      structureLevel: typeof c['structure_level'] === 'string' ? (c['structure_level'] as string) : null,
      eventCount: isFiniteNumber(c['event_count']) ? (c['event_count'] as number) : null,
      memberCount: isFiniteNumber(c['member_count']) ? (c['member_count'] as number) : null,
      memberRatio: isFiniteNumber(c['member_ratio']) ? (c['member_ratio'] as number) : null,
    }
    if (et === 'BOS' || et === 'CHoCH') bosChoch.push(item)
    else secondary.push(item)
  }
  for (const [key, cell] of Object.entries(extreme)) {
    if (!cell || typeof cell !== 'object') continue
    const c = cell as Json
    secondary.push({
      eventType: key,
      direction: null,
      structureLevel: null,
      eventCount: isFiniteNumber(c['event_count']) ? (c['event_count'] as number) : null,
      memberCount: isFiniteNumber(c['member_count']) ? (c['member_count'] as number) : null,
      memberRatio: isFiniteNumber(c['member_ratio']) ? (c['member_ratio'] as number) : null,
    })
  }
  return { status, denominator, bosChoch, secondary }
}

export function parseSmcObservation(observation: Json | null | undefined): SmcObservationFacts {
  if (!observation || typeof observation !== 'object') {
    return {
      swingState: null,
      internalState: null,
      swingChangedMembers: [],
      internalChangedMembers: [],
      transitionDenominator: { swing: null, internal: null },
      alignment: null,
      trailingTop: null,
      trailingBottom: null,
      events: null,
    }
  }
  const swingTransition = deepGet(observation, ['structure', 'swing', 'transition'])
  const internalTransition = deepGet(observation, ['structure', 'internal', 'transition'])
  return {
    swingState: parseState(deepGet(observation, ['structure', 'swing', 'state'])),
    internalState: parseState(deepGet(observation, ['structure', 'internal', 'state'])),
    swingChangedMembers: parseChangedMembers(swingTransition),
    internalChangedMembers: parseChangedMembers(internalTransition),
    transitionDenominator: {
      swing: parseTransitionDenominator(swingTransition),
      internal: parseTransitionDenominator(internalTransition),
    },
    alignment: parseAlignment(deepGet(observation, ['structure', 'alignment'])),
    trailingTop: parseTrailing(deepGet(observation, ['structure', 'distance_to_trailing_top_pct'])),
    trailingBottom: parseTrailing(deepGet(observation, ['structure', 'distance_to_trailing_bottom_pct'])),
    events: parseEvents(deepGet(observation, ['structure', 'events'])),
  }
}

// ---------------------------------------------------------------------------
// ViewModel（格式化；复用 shared numeric-scale contract）
// ---------------------------------------------------------------------------

export interface SmcStateVM {
  up: string
  neutral: string
  down: string
  upRatio: number | null
  neutralRatio: number | null
  downRatio: number | null
  denominator: number | null
}

export interface SmcTrailingVM {
  median: string
  p25: string
  p75: string
}

export interface SmcEventCellVM {
  eventType: string
  direction: string | null
  structureLevel: string | null
  memberCount: number | null
  memberRatio: string
  eventCount: number | null
}

export interface SmcEventVM {
  status: string | null
  denominator: number | null
  bosChoch: SmcEventCellVM[]
  secondary: SmcEventCellVM[]
}

export interface SmcVM {
  swingState: SmcStateVM | null
  internalState: SmcStateVM | null
  swingChangedMembers: SmcChangedMember[]
  internalChangedMembers: SmcChangedMember[]
  swingTransitionDenominator: number | null
  internalTransitionDenominator: number | null
  alignment: { aligned: string; divergent: string; denominator: number | null } | null
  trailingTop: SmcTrailingVM
  trailingBottom: SmcTrailingVM
  events: SmcEventVM | null
}

function buildStateVM(state: SmcStateFacts | null): SmcStateVM | null {
  if (!state) return null
  return {
    up: formatPercentNullable(state.upRatio, 1),
    neutral: formatPercentNullable(state.neutralRatio, 1),
    down: formatPercentNullable(state.downRatio, 1),
    upRatio: state.upRatio,
    neutralRatio: state.neutralRatio,
    downRatio: state.downRatio,
    denominator: state.denominator,
  }
}

function buildTrailingVM(t: SmcTrailingDistance | null): SmcTrailingVM {
  return {
    median: t?.median == null ? NULL_DISPLAY : formatNumberNullable(t.median, 2),
    p25: t?.p25 == null ? NULL_DISPLAY : formatNumberNullable(t.p25, 2),
    p75: t?.p75 == null ? NULL_DISPLAY : formatNumberNullable(t.p75, 2),
  }
}

function buildEventCellVM(c: SmcBosChochCell | SmcSecondaryCell): SmcEventCellVM {
  return {
    eventType: c.eventType,
    direction: c.direction,
    structureLevel: c.structureLevel,
    memberCount: c.memberCount,
    memberRatio: c.memberRatio == null ? NULL_DISPLAY : formatPercentNullable(c.memberRatio, 1),
    eventCount: c.eventCount,
  }
}

function buildEventVM(events: SmcEventFacts | null): SmcEventVM | null {
  if (!events) return null
  return {
    status: events.status,
    denominator: events.denominator,
    bosChoch: events.bosChoch.map(buildEventCellVM),
    secondary: events.secondary.map(buildEventCellVM),
  }
}

export function buildSmcVM(facts: SmcObservationFacts): SmcVM {
  return {
    swingState: buildStateVM(facts.swingState),
    internalState: buildStateVM(facts.internalState),
    swingChangedMembers: facts.swingChangedMembers,
    internalChangedMembers: facts.internalChangedMembers,
    swingTransitionDenominator: facts.transitionDenominator.swing,
    internalTransitionDenominator: facts.transitionDenominator.internal,
    alignment: facts.alignment
      ? {
          aligned: formatPercentNullable(facts.alignment.alignedRatio, 1),
          divergent: formatPercentNullable(facts.alignment.divergentRatio, 1),
          denominator: facts.alignment.denominator,
        }
      : null,
    trailingTop: buildTrailingVM(facts.trailingTop),
    trailingBottom: buildTrailingVM(facts.trailingBottom),
    events: buildEventVM(facts.events),
  }
}

// ---------------------------------------------------------------------------
// History（history.smc）解析
// ---------------------------------------------------------------------------

export interface SmcHistoryStateEntry {
  date: string
  vm: SmcStateVM | null
  facts: SmcStateFacts | null
}

export interface SmcHistoryEventEntry {
  date: string
  vm: SmcEventVM | null
  facts: SmcEventFacts | null
}

export interface SmcHistoryVM {
  dates: string[]
  swingState: SmcHistoryStateEntry[]
  internalState: SmcHistoryStateEntry[]
  eventTape: SmcHistoryEventEntry[]
}

function rawStateToFacts(raw: ReviewStructureState | null): SmcStateFacts | null {
  if (!raw) return null
  return {
    upRatio: raw.up_ratio,
    neutralRatio: raw.neutral_ratio,
    downRatio: raw.down_ratio,
    denominator: raw.denominator,
  }
}

function rawEventsToFacts(raw: ReviewStructureEvents | null): SmcEventFacts | null {
  if (!raw) return null
  const leveled: Json = raw.cells?.leveled ?? {}
  const extreme: Json = raw.cells?.extreme ?? {}
  const bosChoch: SmcBosChochCell[] = []
  const secondary: SmcSecondaryCell[] = []
  for (const cell of Object.values(leveled)) {
    if (!cell || typeof cell !== 'object') continue
    const c = cell as Json
    const et = typeof c['event_type'] === 'string' ? (c['event_type'] as string) : ''
    const item: SmcBosChochCell | SmcSecondaryCell = {
      eventType: et,
      direction: typeof c['direction'] === 'string' ? (c['direction'] as string) : null,
      structureLevel: typeof c['structure_level'] === 'string' ? (c['structure_level'] as string) : null,
      eventCount: isFiniteNumber(c['event_count']) ? (c['event_count'] as number) : null,
      memberCount: isFiniteNumber(c['member_count']) ? (c['member_count'] as number) : null,
      memberRatio: isFiniteNumber(c['member_ratio']) ? (c['member_ratio'] as number) : null,
    }
    if (et === 'BOS' || et === 'CHoCH') bosChoch.push(item)
    else secondary.push(item)
  }
  for (const [key, cell] of Object.entries(extreme)) {
    if (!cell || typeof cell !== 'object') continue
    const c = cell as Json
    secondary.push({
      eventType: key,
      direction: null,
      structureLevel: null,
      eventCount: isFiniteNumber(c['event_count']) ? (c['event_count'] as number) : null,
      memberCount: isFiniteNumber(c['member_count']) ? (c['member_count'] as number) : null,
      memberRatio: isFiniteNumber(c['member_ratio']) ? (c['member_ratio'] as number) : null,
    })
  }
  return { status: raw.status, denominator: raw.denominator, bosChoch, secondary }
}

export function parseSmcHistory(history: ReviewScopeSmcHistoryDTO | null | undefined): SmcHistoryVM {
  if (!history || typeof history !== 'object') {
    return { dates: [], swingState: [], internalState: [], eventTape: [] }
  }
  const dates = Array.isArray(history.dates) ? history.dates : []
  const swingState: SmcHistoryStateEntry[] = dates.map((d, i) => {
    const raw = rawStateToFacts(history.swing_state?.[i] ?? null)
    return { date: d, vm: buildStateVM(raw), facts: raw }
  })
  const internalState: SmcHistoryStateEntry[] = dates.map((d, i) => {
    const raw = rawStateToFacts(history.internal_state?.[i] ?? null)
    return { date: d, vm: buildStateVM(raw), facts: raw }
  })
  const eventTape: SmcHistoryEventEntry[] = dates.map((d, i) => {
    const raw = rawEventsToFacts(history.event_tape?.[i] ?? null)
    return { date: d, vm: buildEventVM(raw), facts: raw }
  })
  return { dates, swingState, internalState, eventTape }
}

/** 成员展示（统一走 displayMember，绝不裸 UUID）。 */
export function smcDisplayMember(
  memberId: string | number,
  directory: MemberDirectory | null | undefined,
): string {
  return displayMember(memberId, directory)
}
