/**
 * Chart-only ZigZag overlay with fixed TradingView example inputs.
 *
 * Behavioral reference: TradingView/ZigZag/9 public specification and Pine
 * sample. This is an independently authored implementation, NOT the imported
 * Pine library. TradingView numeric parity requires golden screenshot/data
 * verification before declaring exact parity.
 *
 * Not a strategy signal and not a historical point-in-time label. The last
 * pivot and projection are explicitly repaintable.
 */

export interface ZigZagBar {
  readonly high: number
  readonly low: number
  readonly volume: number
}

export type ZigZagKind = 'high' | 'low'

export interface ZigZagPivot {
  readonly index: number
  readonly price: number
  readonly kind: ZigZagKind
  /** Bar index at which this pivot first met the right-leg confirmation test. */
  readonly confirmedAt: number
}

export interface ZigZagSegment {
  readonly from: ZigZagPivot
  readonly to: ZigZagPivot
  readonly volume: number
  readonly priceChange: number
}

export interface ZigZagOverlay {
  readonly pivots: readonly ZigZagPivot[]
  readonly segments: readonly ZigZagSegment[]
  readonly projected: ZigZagSegment | null
}

/** No UI, URL, or localStorage override is allowed for these inputs. */
export const ZIGZAG_FIXED = Object.freeze({
  deviationPercent: 5,
  pivotLegs: 10,
  leftRight: 5,
  lineColor: '#2962FF',
  projectionPivots: true,
  displayReversalPrice: true,
  displayCumulativeVolume: true,
  displayReversalPriceChange: true,
  differencePriceMode: 'Absolute',
  draw: true,
  allowZigZagOnOneBar: true,
} as const)

const HALF = ZIGZAG_FIXED.leftRight
const THRESHOLD = ZIGZAG_FIXED.deviationPercent

function validBar(bar: ZigZagBar): boolean {
  return Number.isFinite(bar.high) && Number.isFinite(bar.low) &&
    Number.isFinite(bar.volume) && bar.high >= bar.low && bar.low > 0 && bar.volume >= 0
}

/**
 * Pivot ties follow the documented TradingView convention: an equal older
 * extreme wins; equal newer extremes do not disqualify the earlier extreme.
 * left/right refer to chronological index direction.
 */
function pivotAt(bars: readonly ZigZagBar[], index: number, kind: ZigZagKind, right: number): boolean {
  if (index < HALF || index + right >= bars.length) return false
  const value = kind === 'high' ? bars[index].high : bars[index].low
  for (let d = 1; d <= HALF; d++) {
    const older = kind === 'high' ? bars[index - d].high : bars[index - d].low
    if (kind === 'high' ? older >= value : older <= value) return false
  }
  for (let d = 1; d <= right; d++) {
    const newer = kind === 'high' ? bars[index + d].high : bars[index + d].low
    if (kind === 'high' ? newer > value : newer < value) return false
  }
  return true
}

function deviation(base: number, candidate: number): number {
  return Math.abs((candidate - base) / base) * 100
}

function addOrUpdate(pivots: ZigZagPivot[], candidate: ZigZagPivot): void {
  const last = pivots[pivots.length - 1]
  if (!last) {
    pivots.push(candidate)
  } else if (last.kind === candidate.kind) {
    const moreExtreme = candidate.kind === 'high'
      ? candidate.price > last.price
      : candidate.price < last.price
    if (moreExtreme) pivots[pivots.length - 1] = candidate
  } else if (deviation(last.price, candidate.price) >= THRESHOLD) {
    pivots.push(candidate)
  }
}

function volumePrefix(bars: readonly ZigZagBar[]): Float64Array {
  const prefix = new Float64Array(bars.length + 1)
  for (let i = 0; i < bars.length; i++) prefix[i + 1] = prefix[i] + bars[i].volume
  return prefix
}

function makeSegment(from: ZigZagPivot, to: ZigZagPivot, prefix: Float64Array): ZigZagSegment {
  // Excludes the previous pivot's bar; includes the ending bar.
  // A same-bar double pivot produces zero interval volume.
  const volume = prefix[to.index + 1] - prefix[from.index + 1]
  return { from, to, volume: Math.max(0, volume), priceChange: to.price - from.price }
}

/**
 * Apply v9's latest-point invalidation concept. A more extreme same-direction
 * price that appears before a sufficient opposite reversal can invalidate
 * the last saved pivot, even before a new confirmed extremum forms.
 * The very first pivot cannot be popped without an earlier anchor.
 */
function invalidateLast(bars: readonly ZigZagBar[], pivots: ZigZagPivot[]): void {
  while (pivots.length > 1) {
    const last = pivots[pivots.length - 1]
    let invalid = false
    for (let i = last.index + 1; i < bars.length; i++) {
      const bar = bars[i]
      const reversed = last.kind === 'high'
        ? bar.low <= last.price * (1 - THRESHOLD / 100)
        : bar.high >= last.price * (1 + THRESHOLD / 100)
      if (reversed) break
      if (last.kind === 'high' ? bar.high > last.price : bar.low < last.price) {
        invalid = true
        break
      }
    }
    if (!invalid) return
    pivots.pop()
  }
}

function findProjection(
  bars: readonly ZigZagBar[], last: ZigZagPivot,
): ZigZagPivot | null {
  const kind: ZigZagKind = last.kind === 'high' ? 'low' : 'high'
  let candidate: ZigZagPivot | null = null
  // Unconfirmed pivots are in the newest HALF bars. All previous confirmed
  // candidates have already been processed by the confirmed-pivot pass.
  const from = Math.max(last.index + 1, HALF, bars.length - HALF)
  for (let i = from; i < bars.length; i++) {
    const partialRight = bars.length - 1 - i
    if (!pivotAt(bars, i, kind, partialRight)) continue
    const price = kind === 'high' ? bars[i].high : bars[i].low
    if (deviation(last.price, price) < THRESHOLD) continue
    if (!candidate || (kind === 'high' ? price > candidate.price : price < candidate.price)) {
      candidate = { index: i, price, kind, confirmedAt: -1 }
    }
  }
  return candidate
}

/**
 * O(N * HALF) confirmed pivot detection + O(N) volume prefix and bounded
 * projection; HALF is a frozen constant 5, so complexity is linear in bars.
 * Must be invoked on the full loaded chart series, not a viewport slice.
 */
export function computeZigZag(bars: readonly ZigZagBar[]): ZigZagOverlay {
  if (bars.length < HALF * 2 + 1 || bars.some(bar => !validBar(bar))) {
    return { pivots: [], segments: [], projected: null }
  }

  const prefix = volumePrefix(bars)
  const pivots: ZigZagPivot[] = []
  for (let confirmedAt = HALF * 2; confirmedAt < bars.length; confirmedAt++) {
    const i = confirmedAt - HALF
    // The order of high and low is observable for a same-bar double pivot.
    if (pivotAt(bars, i, 'high', HALF)) {
      addOrUpdate(pivots, { index: i, price: bars[i].high, kind: 'high', confirmedAt })
    }
    if (pivotAt(bars, i, 'low', HALF)) {
      addOrUpdate(pivots, { index: i, price: bars[i].low, kind: 'low', confirmedAt })
    }
  }

  if (ZIGZAG_FIXED.projectionPivots) invalidateLast(bars, pivots)

  const segments: ZigZagSegment[] = []
  for (let i = 1; i < pivots.length; i++) segments.push(makeSegment(pivots[i - 1], pivots[i], prefix))

  let projected: ZigZagSegment | null = null
  const last = pivots[pivots.length - 1]
  if (ZIGZAG_FIXED.projectionPivots && last) {
    const next = findProjection(bars, last)
    if (next) projected = makeSegment(last, next, prefix)
  }
  return { pivots, segments, projected }
}
