import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const chart = readFileSync('src/components/StrategyChart.tsx', 'utf-8')
const manifest = readFileSync('src/features/stock-research/stockResearchTypes.ts', 'utf-8')
const toolbar = readFileSync('src/features/stock-research/IndicatorToolbar.tsx', 'utf-8')

test('original breakout overlay has been replaced by ZigZag', () => {
  assert.ok(!chart.includes('function renderBreakout('))
  assert.ok(!chart.includes("drawText(ctx, '结构压力'"))
  assert.ok(chart.includes('computeZigZag(bars)'))
  assert.ok(chart.includes('renderZigZagOverlay(ctx, zigzag'))
  assert.ok(manifest.includes("id: 'breakout', name: '波段'"))
})

test('legacy breakout visibility key preserved, without a user parameter input', () => {
  assert.ok(manifest.includes("'breakout'"))
  assert.ok(toolbar.includes('onToggle(entry.id, !active)'))
  assert.ok(!toolbar.includes('input type="number"'))
  assert.ok(!toolbar.includes('settings'))
})

test('compute is a memoized full-series operation, not a per-viewport render operation', () => {
  assert.ok(chart.includes('useMemo(() => effectiveLayers.breakout ? computeZigZag(bars) : null'))
  assert.ok(chart.includes('fromIndex: viewportFromIndex'))
})
