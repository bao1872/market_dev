import assert from 'node:assert/strict'
import test from 'node:test'
import {currentSegmentAsOf} from './currentSegment'
import type {StrategyHistory} from '@/api/strategyCandidates'
const history = (): StrategyHistory => ({as_of:'2026-10-15',price_adjustment:'qfq',source_policy:'MDAS_DB_ONLY',
  source_bar_hash:'a',adj_factor_hash:'b',structure:{as_of:'2026-10-15',source_bar_hash:'a',adj_factor_hash:'b'},
  tv_zigzag:{asOf:'2026-10-15',algorithmSha:'tv'},
  segment:{direction:'向上',anchorDate:'2026-10-12',lastDate:'2026-10-14',asOf:'2026-10-15',sessions:2,totalReturnPct:30,slopePctPerSession:15,
    directionBasis:'TV_LAST_SEGMENT',algorithmSha:'tv',lineStyle:'dashed',fromPrice:10,toPrice:13}} as StrategyHistory)
test('presentation consumes the backend segment without computing prices',()=>{
  const h=history();assert.equal(currentSegmentAsOf(h,'2026-10-15'),h.segment)
})
test('stale dates, hashes and unavailable backend segments remain unavailable',()=>{
  assert.equal(currentSegmentAsOf(history(),'2026-10-16'),null)
  const h=history();h.structure!.source_bar_hash='wrong';assert.equal(currentSegmentAsOf(h,'2026-10-15'),null)
  const missing=history();missing.segment=null;assert.equal(currentSegmentAsOf(missing,'2026-10-15'),null)
})
