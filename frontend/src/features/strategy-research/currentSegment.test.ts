import assert from 'node:assert/strict'
import test from 'node:test'
import { currentSegmentAsOf } from './currentSegment'
import type { StrategyHistory } from '@/api/strategyCandidates'

const history = (): StrategyHistory => ({
  symbol: 'SH:600000', count: 4, requested_count: 240, actual_start: '2026-10-12', actual_end: '2026-10-15',
  as_of: '2026-10-15', price_adjustment: 'qfq', source_policy: 'MDAS_DB_ONLY',
  source_bar_hash: 'a', adj_factor_hash: 'b', history_exhausted: false,
  candidate_overlay_supported: false, candidate_overlay_reason: 'not aligned',
  bars: [10, 11, 12, 13].map((p, i) => ({date:`2026-10-${12+i}`,open:p,high:p+.1,low:p-.1,close:p})),
  structure: {
    algorithm: 'frozen-panji-lab-B55-replay', algorithm_sha256: 'sha', as_of: '2026-10-15', source_bar_hash: 'a', adj_factor_hash: 'b',
    coverage_start: '2026-10-12', coverage_end: '2026-10-15', latest_mutable_count: 0, ambiguous_anchor_count: 0,
    confirm_lineage: 'readonly', decision_pit_verified: false, hindsight_label_used_for_model_scoring: false,
    raw_ohlcv_volume_used_for_features: false, notes: '', early_candidates: [],
    confirmed_pivots: [{anchor_date:'2026-10-12', kind:'L',price:10,seen_at:'2026-10-12',confirmed_at:'2026-10-14'}],
  },
})
test('read-only confirmed L segment reports upward, positive log-OLS daily slope and cumulative return', () => {
  const result = currentSegmentAsOf(history(),'2026-10-15')
  assert.equal(result?.direction,'向上'); assert.equal(result?.sessions,3)
  assert.ok((result?.slopePctPerSession ?? 0) > 0)
  assert.ok(Math.abs((result?.totalReturnPct ?? 0) - 30) < 1e-8)
  assert.equal(result?.contrarySlope,false)
})
test('H pivot with rising price reports current up and structural conflict', () => {
  const h = history(); h.structure!.confirmed_pivots[0].kind = 'H'
  const result = currentSegmentAsOf(h,'2026-10-15')
  assert.equal(result?.direction,'向上'); assert.equal(result?.contrarySlope,true)
})
test('future confirmations, stale bars, mismatched hash, insufficient bars all reject', () => {
  const h = history(); h.structure!.confirmed_pivots[0].confirmed_at = '2026-10-16'
  assert.equal(currentSegmentAsOf(h,'2026-10-15'),null)
  const stale = history(); assert.equal(currentSegmentAsOf(stale,'2026-10-16'),null)
  const mismatch = history(); mismatch.structure!.source_bar_hash='different'
  assert.equal(currentSegmentAsOf(mismatch,'2026-10-15'),null)
  const missing = history(); missing.structure!.confirmed_pivots=[]
  assert.equal(currentSegmentAsOf(missing,'2026-10-15'),null)
})
test('pivot from later bars is forbidden and negative slope is signed', () => {
  const h = history()
  h.bars.forEach((bar,i) => { bar.close = 13-i })
  const trend = currentSegmentAsOf(h,'2026-10-15')
  assert.equal(trend?.direction,'向下'); assert.ok((trend?.slopePctPerSession ?? 0)<0)
  assert.equal(trend?.contrarySlope,true)
})
