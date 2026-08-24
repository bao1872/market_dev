// [R3D] Structure Observation contract owner (pure TS, no React).
//
// Single deterministic parser for canonical L2 groups G5–G6 (structure_break_turn,
// structure_evolution_position). It consumes:
//   detail.observationGroups  (L2 projection; facts are L1 objects by reference)
//
// It does NOT recompute, score, signal, rank, derive opportunity/risk, or build
// bullish/bearish conclusions. It does NOT implement detectFactKind / inferEventShape
// / generic auto-renderer. Renderer components consume the typed ViewModels below.
//
// FRONTEND IS A PROJECTION LAYER (R3D §1):
// - No event aggregation / denominator calc / member-ratio calc / score / health /
//   signal / bullish-bearish / opportunity-risk / event-price inference.
// - member_ratio is the PRIMARY product fact; event_count is only evidence.
// - Persisted event denominator (PIT(T) ∩ event coverage) is NEVER replaced by
//   PIT count / event member count / sum(member_count) / event_count / filtered set.
//
// NUMERIC SCALE CONTRACT (P0 — no x100 errors):
//   - member_ratio: decimal ratio -> *100 (formatPercentNullable). NOT percentage points.
//   - trailing distance: ALREADY percentage points -> NO x100 (formatPercentagePointsNullable).
//
// DIRECTION COLOR (R3D §12): only the canonical direction token tone is colored
//   (bullish/up -> red, bearish/down -> green, unknown -> neutral, null -> malformed).
//   event_type / structure_level / counts / ratios stay neutral.

import { formatPercentNullable, NULL_DISPLAY } from './reviewFormat'
import { formatPercentagePointsNullable } from './scopePriceTrendContract'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

function isFiniteNonNegInt(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v) && Number.isInteger(v) && v >= 0
}

// Canonical event outer status must be exactly "ready" or "unavailable".
// Anything else (missing/unknown) is contract invalidity -> fail closed.
type EventStatus = 'ready' | 'unavailable' | 'invalid'

function normalizeEventStatus(v: unknown): EventStatus {
  if (v === 'ready') return 'ready'
  if (v === 'unavailable') return 'unavailable'
  return 'invalid'
}

// ---------------------------------------------------------------------------
// Direction tone (A-share: up=red, down=green; verbatim display token)
// ---------------------------------------------------------------------------

export type DirectionTone = 'up' | 'down' | 'neutral'

export type DirectionToken = string | null

export function directionTone(token: DirectionToken): DirectionTone {
  if (!token) return 'neutral' // null -> malformed for leveled canonical cell
  const t = token.toLowerCase()
  if (t === 'bullish' || t === 'up') return 'up'
  if (t === 'bearish' || t === 'down') return 'down'
  return 'neutral' // unknown token -> neutral (display verbatim)
}

// ---------------------------------------------------------------------------
// Typed event models (leveled vs extreme are semantically separate)
// ---------------------------------------------------------------------------

export type StructureLevel = 'Swing' | 'Internal' | null

export interface LeveledStructureEventCell {
  /** Opaque React key only; never semantically parsed. */
  cellKey: string
  eventType: string
  direction: DirectionToken
  structureLevel: StructureLevel
  eventCount: number | null
  memberCount: number | null
  memberRatio: number | null
  /** true when the canonical cell object is missing required canonical fields. */
  malformed: boolean
}

export interface ExtremeStructureEventCell {
  /** 'EQH' | 'EQL' — canonical map key is the event type. */
  eventType: 'EQH' | 'EQL'
  eventCount: number | null
  memberCount: number | null
  memberRatio: number | null
}

export interface StructureEventBundle {
  /** persisted canonical status: "ready" | "unavailable". */
  availability: StructureEventAvailability
  /** true when outer status is invalid (missing/unknown) -> fail closed. */
  contractInvalid: boolean
  /** persisted source denominator (PIT(T) ∩ coverage); null iff unavailable/contract-invalid. */
  denominator: number | null
  /** true when ready + denominator>0 + cells empty (legal zero-event day). */
  zeroEventToday: boolean
  /** presentation order only; NOT ranking. */
  leveled: LeveledStructureEventCell[]
  extreme: ExtremeStructureEventCell[]
  /** malformed-outer includes an unexpected event type that must fail closed. */
  hasContractInvalidity: boolean
}

// ---- G5 --------------------------------------------------------------------

export type StructureEventAvailability = 'ready' | 'unavailable'

export interface StructureBreakTurnVM {
  groupKey: 'structure_break_turn'
  availability: StructureEventAvailability
  /** true when outer status is invalid (missing/unknown/ready+denom=0) -> fail closed. */
  contractInvalid: boolean
  /** true when ready + denominator>0 + cells empty (legal zero-event day). */
  zeroEventToday: boolean
  denominator: number | null
  leveled: LeveledStructureEventCell[]
  hasContractInvalidity: boolean
  /** true when a leveled cell is malformed but rendering should not crash the workspace. */
  hasMalformedCell: boolean
}

// ---- G6 --------------------------------------------------------------------

export interface StructureAlignmentVM {
  alignedCount: number | null
  alignedRatio: number | null
  divergentCount: number | null
  divergentRatio: number | null
  denominator: number | null
  /** true when denominator == 0 (no valid alignment members). */
  zeroDenominator: boolean
}

export interface StructureDistanceVM {
  median: number | null
  p25: number | null
  p75: number | null
  validCount: number | null
  denominator: number | null
  /** true when canonical source is unavailable (status/reason shape). */
  unavailable: boolean
}

export interface StructureEvolutionPositionVM {
  groupKey: 'structure_evolution_position'
  // Independent facts — each keeps its own availability (R3D §23).
  events: StructureEventBundle | null
  eventsMalformed: boolean
  alignment: StructureAlignmentVM | null
  distanceTop: StructureDistanceVM | null
  distanceBottom: StructureDistanceVM | null
  hasContractInvalidity: boolean
}

// ---------------------------------------------------------------------------
// Leveled cell ingestion (G5 only accepts BOS/CHoCH; G6 only OB_CREATED/ENTERED/MITIGATED)
// ---------------------------------------------------------------------------

const G5_LEVELED_TYPES = new Set(['BOS', 'CHoCH'])
const G6_LEVELED_TYPES = new Set(['OB_CREATED', 'OB_ENTERED', 'OB_MITIGATED'])
const G6_EXTREME_TYPES = new Set(['EQH', 'EQL'])

interface RawLeveledCell {
  event_type?: unknown
  direction?: unknown
  structure_level?: unknown
  event_count?: unknown
  member_count?: unknown
  member_ratio?: unknown
}

function parseCanonicalLeveledCell(
  cellKey: string,
  raw: unknown,
  allowed: Set<string>,
): LeveledStructureEventCell | 'invalid' | 'unexpected' {
  if (raw === null || typeof raw !== 'object') return 'invalid'
  const o = raw as RawLeveledCell
  const eventType = typeof o.event_type === 'string' ? o.event_type : null
  if (eventType === null) return 'invalid'
  if (!allowed.has(eventType)) return 'unexpected'

  const direction = typeof o.direction === 'string' ? o.direction : null
  let structureLevel: StructureLevel = null
  if (o.structure_level === 'Swing') structureLevel = 'Swing'
  else if (o.structure_level === 'Internal') structureLevel = 'Internal'
  else if (o.structure_level !== null && o.structure_level !== undefined) {
    // unknown structure_level value -> fail closed (do not default Swing/Internal)
    structureLevel = null
  }

  const eventCount = isFiniteNonNegInt(o.event_count) ? o.event_count : null
  const memberCount = isFiniteNonNegInt(o.member_count) ? o.member_count : null
  const memberRatio = isFiniteNumber(o.member_ratio) ? o.member_ratio : null

  const malformed =
    direction === null ||
    structureLevel === null ||
    memberRatio === null ||
    memberCount === null ||
    eventCount === null

  return {
    cellKey,
    eventType,
    direction,
    structureLevel,
    eventCount,
    memberCount,
    memberRatio,
    malformed,
  }
}

// ---------------------------------------------------------------------------
// G5 — structure_break_turn
// ---------------------------------------------------------------------------

export function parseStructureBreakTurn(groupFacts: Record<string, unknown> | undefined): StructureBreakTurnVM | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const raw = groupFacts['bos_choch_events']
  if (raw === null || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const status = normalizeEventStatus(o['status'])
  const denominator = isFiniteNumber(o['denominator']) ? (o['denominator'] as number) : null
  const cells = o['cells']
  const leveledMap =
    cells && typeof cells === 'object' && (cells as Record<string, unknown>)['leveled']
      ? ((cells as Record<string, unknown>)['leveled'] as Record<string, unknown>)
      : {}

  // Outer contract fail-closed (R3D-V §2):
  // - status must be exactly "ready"/"unavailable"
  // - ready requires finite integer denominator > 0
  // - ready + denominator == 0 is NOT a current production state -> invalid
  const contractInvalid =
    status === 'invalid' || (status === 'ready' && !(Number.isInteger(denominator) && (denominator as number) > 0))

  if (status === 'unavailable') {
    return {
      groupKey: 'structure_break_turn',
      availability: 'unavailable',
      contractInvalid: false,
      zeroEventToday: false,
      denominator: null,
      leveled: [],
      hasContractInvalidity: false,
      hasMalformedCell: false,
    }
  }

  if (contractInvalid) {
    return {
      groupKey: 'structure_break_turn',
      availability: 'ready', // nominal; renderer must treat contractInvalid as fail-closed
      contractInvalid: true,
      zeroEventToday: false,
      denominator: status === 'ready' ? denominator : null,
      leveled: [],
      hasContractInvalidity: false,
      hasMalformedCell: false,
    }
  }

  // ready: collect leveled (only BOS/CHoCH accepted; others fail closed)
  const leveled: LeveledStructureEventCell[] = []
  let hasContractInvalidity = false
  let hasMalformedCell = false
  for (const [key, val] of Object.entries(leveledMap)) {
    const parsed = parseCanonicalLeveledCell(key, val, G5_LEVELED_TYPES)
    if (parsed === 'invalid') {
      hasMalformedCell = true
      continue
    }
    if (parsed === 'unexpected') {
      hasContractInvalidity = true
      continue
    }
    if (parsed.malformed) hasMalformedCell = true
    leveled.push(parsed)
  }

  const zeroEventToday = leveled.length === 0

  return {
    groupKey: 'structure_break_turn',
    availability: 'ready',
    contractInvalid: false,
    zeroEventToday,
    denominator,
    leveled,
    hasContractInvalidity,
    hasMalformedCell,
  }
}

// ---------------------------------------------------------------------------
// G6 — structure_evolution_position
// ---------------------------------------------------------------------------

export function parseStructureAlignment(groupFacts: Record<string, unknown> | undefined): StructureAlignmentVM | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const raw = groupFacts['structure_alignment']
  if (raw === null || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const alignedCount = isFiniteNumber(o['aligned_count']) ? (o['aligned_count'] as number) : null
  const alignedRatio = isFiniteNumber(o['aligned_ratio']) ? (o['aligned_ratio'] as number) : null
  const divergentCount = isFiniteNumber(o['divergent_count']) ? (o['divergent_count'] as number) : null
  const divergentRatio = isFiniteNumber(o['divergent_ratio']) ? (o['divergent_ratio'] as number) : null
  const denominator = isFiniteNumber(o['denominator']) ? (o['denominator'] as number) : null
  return {
    alignedCount,
    alignedRatio,
    divergentCount,
    divergentRatio,
    denominator,
    zeroDenominator: denominator === 0,
  }
}

export function parseCurrentOnlyDistance(groupFacts: Record<string, unknown> | undefined, key: string): StructureDistanceVM | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const raw = groupFacts[key]
  if (raw === null || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  // unavailable shape carries status+reason (R3C/Current-only terminology)
  const status = typeof o['status'] === 'string' ? (o['status'] as string) : null
  if (status === 'unavailable') {
    return {
      median: null,
      p25: null,
      p75: null,
      validCount: isFiniteNumber(o['valid_count']) ? (o['valid_count'] as number) : 0,
      denominator: null,
      unavailable: true,
    }
  }
  const median = isFiniteNumber(o['median']) ? (o['median'] as number) : null
  const p25 = isFiniteNumber(o['p25']) ? (o['p25'] as number) : null
  const p75 = isFiniteNumber(o['p75']) ? (o['p75'] as number) : null
  const validCount = isFiniteNumber(o['valid_count']) ? (o['valid_count'] as number) : null
  const denominator = isFiniteNumber(o['denominator']) ? (o['denominator'] as number) : null
  return { median, p25, p75, validCount, denominator, unavailable: false }
}

export function parseStructureEvolutionPosition(groupFacts: Record<string, unknown> | undefined): StructureEvolutionPositionVM | null {
  if (!groupFacts || typeof groupFacts !== 'object') return null
  const raw = groupFacts['ob_and_eq_events']
  let events: StructureEventBundle | null = null
  let eventsMalformed = false
  let hasContractInvalidity = false

  if (raw && typeof raw === 'object') {
    const o = raw as Record<string, unknown>
    const status = normalizeEventStatus(o['status'])
    const denominator = isFiniteNumber(o['denominator']) ? (o['denominator'] as number) : null
    const cells = o['cells']
    const leveledMap =
      cells && typeof cells === 'object' && (cells as Record<string, unknown>)['leveled']
        ? ((cells as Record<string, unknown>)['leveled'] as Record<string, unknown>)
        : {}
    const extremeMap =
      cells && typeof cells === 'object' && (cells as Record<string, unknown>)['extreme']
        ? ((cells as Record<string, unknown>)['extreme'] as Record<string, unknown>)
        : {}

    const contractInvalid =
      status === 'invalid' || (status === 'ready' && !(Number.isInteger(denominator) && (denominator as number) > 0))

    const leveled: LeveledStructureEventCell[] = []
    if (status !== 'unavailable' && !contractInvalid) {
      for (const [key, val] of Object.entries(leveledMap)) {
        const parsed = parseCanonicalLeveledCell(key, val, G6_LEVELED_TYPES)
        if (parsed === 'invalid') {
          eventsMalformed = true
          continue
        }
        if (parsed === 'unexpected') {
          hasContractInvalidity = true
          continue
        }
        if (parsed.malformed) eventsMalformed = true
        leveled.push(parsed)
      }
    }

    const extreme: ExtremeStructureEventCell[] = []
    for (const [key, val] of Object.entries(extremeMap)) {
      if (!G6_EXTREME_TYPES.has(key)) {
        hasContractInvalidity = true
        continue
      }
      if (val === null || typeof val !== 'object') {
        eventsMalformed = true
        continue
      }
      const e = val as Record<string, unknown>
      // extreme cell requires finite event_count/member_count/member_ratio (R3D-V §5)
      const evCount = isFiniteNonNegInt(e['event_count']) ? (e['event_count'] as number) : null
      const memCount = isFiniteNonNegInt(e['member_count']) ? (e['member_count'] as number) : null
      const memRatio = isFiniteNumber(e['member_ratio']) ? (e['member_ratio'] as number) : null
      if (evCount === null || memCount === null || memRatio === null) {
        eventsMalformed = true
        continue
      }
      extreme.push({
        eventType: key as 'EQH' | 'EQL',
        eventCount: evCount,
        memberCount: memCount,
        memberRatio: memRatio,
      })
    }

    // G6 event availability is preserved from persisted status, NOT inferred from denominator.
    const availability: StructureEventAvailability = status === 'unavailable' ? 'unavailable' : 'ready'
    events = {
      availability,
      contractInvalid,
      denominator: status === 'unavailable' || contractInvalid ? null : denominator,
      zeroEventToday: !contractInvalid && status === 'ready' && leveled.length === 0 && extreme.length === 0,
      leveled,
      extreme,
      hasContractInvalidity,
    }
  }

  const alignment = parseStructureAlignment(groupFacts)
  const distanceTop = parseCurrentOnlyDistance(groupFacts, 'distance_to_trailing_top_pct')
  const distanceBottom = parseCurrentOnlyDistance(groupFacts, 'distance_to_trailing_bottom_pct')

  return {
    groupKey: 'structure_evolution_position',
    events,
    eventsMalformed,
    alignment,
    distanceTop,
    distanceBottom,
    hasContractInvalidity: hasContractInvalidity || events?.hasContractInvalidity === true,
  }
}

// ---------------------------------------------------------------------------
// Formatters (explicit, non-overlapping semantics)
// ---------------------------------------------------------------------------

/** member_ratio decimal -> *100. NO x100 ambiguity (R3D §14). */
export function formatMemberRatioNullable(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return NULL_DISPLAY
  return formatPercentNullable(value) // default 1dp, *100
}

/** Trailing distance already percentage points -> NO x100 (R3D §18). */
export function formatTrailingDistanceNullable(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return NULL_DISPLAY
  return formatPercentagePointsNullable(value)
}
