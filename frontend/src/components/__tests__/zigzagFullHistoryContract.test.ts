import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { computeZigZag, type ZigZagBar } from '../zigzag.ts'

const chart = readFileSync('src/components/StrategyChart.tsx', 'utf8')

test('ZigZag consumes full snapshot bars, not CALCULATION_WINDOWS calc tail', () => {
  assert.ok(chart.includes('computeZigZag(bars)'))
  assert.ok(!chart.includes('computeZigZag(calc)'))
  assert.ok(chart.includes('[bars, effectiveLayers.breakout]'))
})

test('full-array pivot indices are offset into the truncated calc viewport', () => {
  assert.ok(chart.includes('bars.length - calc.length + viewport.fromIndex'))
  assert.ok(chart.includes('viewportFromIndex: zigzagViewportFromIndex'))
  assert.ok(chart.includes('fromIndex: viewportFromIndex'))
})

test('long trending tail needs the earlier pivot to draw its active projection', () => {
  // First 800 bars alternate trends. The final 400 bars rise monotonically,
  // so a 320-bar tail contains no confirmed pivot even though the full input
  // contains a valid earlier anchor and projects an active high at the end.
  const input: ZigZagBar[] = Array.from({ length: 1200 }, (_, i) => {
    const v = i < 800 ? 110 + 20 * Math.sin(i / 11) : 120 + (i - 800) * 0.02
    return { high: v + 1, low: v - 1, volume: 100 }
  })
  const full = computeZigZag(input)
  const tail = computeZigZag(input.slice(-320))
  assert.ok(full.pivots.length > 0)
  assert.equal(full.projected?.to.index, 1199)
  assert.equal(tail.pivots.length, 0)
  assert.equal(tail.projected, null)
  // Viewport is indexed in the 320-bar display tail; plotting uses full indices.
  const viewportFrom = 240
  const globalFrom = input.length - 320 + viewportFrom
  assert.equal(globalFrom, 1120)
})
