/**
 * Historical candidate lifecycle. This is DISPLAY metadata, not an ML label.
 * No confirmed ZigZag pivots are inferred from the limited chart window.
 * Strictly as-of the selected observation's decision date, never later.
 */
import type { Bar, Candidate, Observation } from '@/api/strategyCandidates'

export type CandidateLifecycle =
  | { state: 'EARLY_CANDIDATE'; breachedAt: null }
  | { state: 'ANCHOR_BREACHED'; breachedAt: string }
  | { state: 'UNKNOWN'; breachedAt: null }

export function assertUniqueSortedBars(bars: Bar[]): void {
  let last = ''
  for (const bar of bars) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(bar.date) || bar.date <= last) {
      throw new Error(`STRATEGY_BARS_NOT_STRICTLY_SORTED: ${bar.date}`)
    }
    if (![bar.open, bar.high, bar.low, bar.close].every(Number.isFinite)
      || bar.high < Math.max(bar.open, bar.close, bar.low)
      || bar.low > Math.min(bar.open, bar.close, bar.high)) {
      throw new Error(`STRATEGY_INVALID_OHLC: ${bar.date}`)
    }
    last = bar.date
  }
}

export function candidateLifecycleAsOf(
  bars: Bar[], candidate: Candidate, observation: Observation,
): CandidateLifecycle {
  // Earliest observable candidate is left-5/right-1, as defined in panji_lab.
  if (observation.decision_date < candidate.visible_at) {
    return { state: 'UNKNOWN', breachedAt: null }
  }
  const anchor = bars.findIndex(b => b.date === candidate.anchor_date)
  const decision = bars.findIndex(b => b.date === observation.decision_date)
  if (anchor < 0 || decision < anchor || decision >= bars.length) {
    return { state: 'UNKNOWN', breachedAt: null }
  }
  // A strict new price extreme invalidates the old anchor; never treat this
  // as the research L1/L2 label or as a retrospectively confirmed pivot.
  const epsilon = 1e-9
  for (let i = anchor + 1; i <= decision; i++) {
    const b = bars[i]
    const breached = candidate.kind === 'L'
      ? b.low < candidate.anchor_price - epsilon
      : b.high > candidate.anchor_price + epsilon
    if (breached) return { state: 'ANCHOR_BREACHED', breachedAt: b.date }
  }
  return { state: 'EARLY_CANDIDATE', breachedAt: null }
}

export function lifecycleLabel(x: CandidateLifecycle): string {
  if (x.state === 'ANCHOR_BREACHED') return `候选锚点已被突破（${x.breachedAt}）`
  if (x.state === 'EARLY_CANDIDATE') return '早期候选，尚非已确认 ZigZag 拐点'
  return '缺少足够的同期价格证据，状态未确认'
}

export function stageObservation(candidate: Candidate, stage: string): Observation | undefined {
  return candidate.observations.find(o => o.stage === stage)
}

export function allStageObservations(candidate: Candidate): Observation[] {
  const order: Record<string, number> = { D1: 1, D2: 2, D3: 3 }
  return [...candidate.observations].sort((a,b) =>
    (order[a.stage] ?? 100) - (order[b.stage] ?? 100))
}
