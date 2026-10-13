import assert from 'node:assert/strict'
import { test } from 'node:test'
import { computeZigZag, ZIGZAG_FIXED, type ZigZagBar } from '../zigzag.ts'

function bars(highs: number[], lows?: number[], vol = 10): ZigZagBar[] {
  return highs.map((h, i) => ({ high: h, low: lows?.[i] ?? h - 1, volume: vol }))
}

test('TV example parameters frozen; no user options', () => {
  assert.equal(ZIGZAG_FIXED.deviationPercent, 5)
  assert.equal(ZIGZAG_FIXED.pivotLegs, 10)
  assert.equal(ZIGZAG_FIXED.leftRight, 5)
  assert.equal(ZIGZAG_FIXED.projectionPivots, true)
  assert.equal(ZIGZAG_FIXED.differencePriceMode, 'Absolute')
  assert.equal(ZIGZAG_FIXED.lineColor, '#2962FF')
  assert.equal(Object.isFrozen(ZIGZAG_FIXED), true)
})

test('requires five completed bars after the candidate', () => {
  const h = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10]
  assert.equal(computeZigZag(bars(h.slice(0, 10))).pivots.length, 0)
  const result = computeZigZag(bars(h))
  assert.deepEqual(result.pivots.map(p => [p.index, p.price, p.kind, p.confirmedAt]), [[5, 20, 'high', 10]])
})

test('a confirmed opposite pivot at >=5% appends a new zigzag segment', () => {
  const h = [10, 11, 12, 13, 14, 20, 16, 15, 14, 13, 12, 10, 11, 12, 13, 14, 15]
  const l = h.map(v => v - 0.5)
  const result = computeZigZag(bars(h, l))
  assert.deepEqual(result.pivots.map(p => [p.index, p.kind]), [[5, 'high'], [11, 'low']])
  assert.equal(result.segments.length, 1)
  assert.equal(result.segments[0].volume, 60)
  assert.equal(result.segments[0].priceChange, -10.5)
})

test('an opposite pivot below the 5% threshold does not append', () => {
  const high = [96, 97, 98, 99, 99.5, 100, 98, 97, 97, 96.5, 96, 96.2, 97, 98, 99, 99.5, 99.8]
  const low = high.map(v => v - 0.1)
  const result = computeZigZag(bars(high, low))
  assert.deepEqual(result.pivots.map(p => [p.index, p.kind]), [[5, 'high']])
})

test('new higher confirmed high updates rather than appends the same pivot type', () => {
  const h = [10, 11, 12, 13, 14, 20, 18, 17, 16, 15, 17, 22, 18, 17, 16, 15, 14]
  const result = computeZigZag(bars(h))
  assert.deepEqual(result.pivots.map(p => [p.index, p.price, p.kind]), [[11, 22, 'high']])
})

test('projection requires left leg, sufficient reversal, and missing right leg', () => {
  const h = [10, 11, 12, 13, 14, 20, 19, 18, 17, 16, 15, 14, 15, 16, 17, 18]
  const result = computeZigZag(bars(h))
  assert.deepEqual(result.pivots.map(p => p.index), [5])
  assert.equal(result.projected?.to.index, 11)
  assert.equal(result.projected?.to.kind, 'low')
  assert.equal(result.projected?.to.confirmedAt, -1)
})

test('malformed price / volume does not create false lines', () => {
  const h = Array.from({ length: 22 }, (_, i) => i + 100)
  const input = bars(h)
  input[5] = { high: Number.NaN, low: 11, volume: 1 }
  assert.deepEqual(computeZigZag(input), { pivots: [], segments: [], projected: null })
})

test('flat price series has no pivots or projections', () => {
  assert.deepEqual(computeZigZag(bars(Array(30).fill(100), Array(30).fill(100))), {
    pivots: [], segments: [], projected: null,
  })
})

test('last confirmed pivot is removed when a more extreme price arrives before sufficient reversal', () => {
  const highs = [10, 11, 12, 13, 14, 20, 19, 18, 17, 16, 11, 10.4, 10.3, 10.35, 10.32, 10.34, 10.36, 10.4]
  const lows = highs.map(h => h - 1)
  lows[10] = 10.5
  lows[11] = 10
  for (let i = 12; i <= 16; i++) lows[i] = 10.1
  lows[17] = 9
  const before = computeZigZag(bars(highs.slice(0, 17), lows.slice(0, 17)))
  assert.deepEqual(before.pivots.map(p => [p.index, p.kind]), [[5, 'high'], [11, 'low']])
  const after = computeZigZag(bars(highs, lows))
  assert.deepEqual(after.pivots.map(p => [p.index, p.kind]), [[5, 'high']])
  assert.equal(after.projected?.to.price, 9)
})

test('calculation is independent of visible viewport and handles 4,000 bars', () => {
  const input = Array.from({ length: 4000 }, (_, i) => ({
    high: 110 + 25 * Math.sin(i / 19),
    low: 108 + 25 * Math.sin(i / 19),
    volume: 100 + i,
  }))
  const result = computeZigZag(input)
  assert.ok(result.pivots.length > 3)
  assert.ok(result.pivots.length < 500)
  for (const pivot of result.pivots) {
    assert.ok(pivot.confirmedAt >= pivot.index + ZIGZAG_FIXED.leftRight)
  }
})

test('high and low may both be pivot points on a single outside bar', () => {
  const high = Array(21).fill(15)
  const low = Array(21).fill(14)
  high[10] = 20
  low[10] = 10
  const result = computeZigZag(bars(high, low))
  assert.deepEqual(result.pivots.map(p => [p.index, p.kind]), [[10, 'high'], [10, 'low']])
  assert.equal(result.segments[0].volume, 0)
})

test('Canvas overlay uses global pivot index and draws projection with dash', async () => {
  const { renderZigZagOverlay } = await import('../zigzagCanvas.ts')
  const calls: { name: string; args: number[] }[] = []
  const record = (name: string) => (...args: number[]) => { calls.push({ name, args }) }
  const ctx = {
    save: record('save'), restore: record('restore'),
    beginPath: record('beginPath'), rect: record('rect'), clip: record('clip'),
    setLineDash: (dash: number[]) => { calls.push({ name: 'dash', args: dash }) },
    moveTo: record('moveTo'), lineTo: record('lineTo'), stroke: record('stroke'),
    measureText: (label: string) => ({ width: label.length * 5 }),
    fillRect: record('fillRect'), fillText: (_label: string, x: number, y: number) => { calls.push({ name: 'label', args: [x, y] }) },
    strokeStyle: '', fillStyle: '', lineWidth: 1, font: '', textAlign: 'left', textBaseline: 'middle',
  } as unknown as CanvasRenderingContext2D
  const a = { index: 20, price: 100, kind: 'high' as const, confirmedAt: 25 }
  const b = { index: 30, price: 90, kind: 'low' as const, confirmedAt: 35 }
  const c = { index: 38, price: 105, kind: 'high' as const, confirmedAt: -1 }
  renderZigZagOverlay(ctx, {
    pivots: [a, b],
    segments: [{ from: a, to: b, volume: 120, priceChange: -10 }],
    projected: { from: b, to: c, volume: 99, priceChange: 15 },
  }, {
    left: 50, right: 750, top: 20, bottom: 300, step: 10,
    fromIndex: 25, visibleCount: 40, font: '10px sans-serif',
    priceToY: p => 300 - p, formatVolume: x => String(x),
  })
  const firstLine = calls.find(c => c.name === 'moveTo')
  assert.deepEqual(firstLine?.args, [5, 200]) // global bar 20 is left of viewport 25
  assert.ok(calls.some(c => c.name === 'dash' && c.args.join(',') === '6,4'))
})
