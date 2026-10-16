import test from 'node:test'
import assert from 'node:assert/strict'
import {assertUniqueSortedBars,candidateLifecycleAsOf,stageObservation} from './candidateTimeline'
import type {Bar,Candidate,Observation} from '@/api/strategyCandidates'

const bars:Bar[]=[
  {date:'2026-06-22',open:8.6226315966,high:8.8040601684,low:8.5748872356,close:8.7467669352},
  {date:'2026-06-23',open:8.7276691908,high:8.928195507,low:8.7181203186,close:8.8613534016},
  {date:'2026-06-24',open:8.8422556572,high:8.8518045294,low:8.4984962580,close:8.4984962580},
  {date:'2026-06-25',open:8.4507518970,high:8.5271428746,low:8.4221052804,close:8.4507518970},
]
function observation(stage:'D1'|'D2'|'D3',day:string):Observation {
  return {observation_id:day,stage,decision_date:day,data_cutoff:day,
    l1_score:0.2,l2_score:0.4,l1_threshold:0.3,l2_threshold:0.5,
    joint_alert:stage==='D1',first_breakout:false,
    reference_price:null,reference_confirmed_at:null}
}
const candidate:Candidate={
  candidate_id:'SH:600000|2026-06-22|L|B55',anchor_date:'2026-06-22',
  visible_at:'2026-06-23',anchor_price:8.5748872356,kind:'L',
  observations:[observation('D1','2026-06-23'),observation('D2','2026-06-24'),observation('D3','2026-06-25')],
}
test('6月23日K线存在且为上涨阳线',()=>{
  assertUniqueSortedBars(bars)
  assert.equal(bars[1].date,'2026-06-23')
  assert.ok(bars[1].close>bars[1].open)
})
test('D1合法早期候选',()=>{
  assert.equal(candidateLifecycleAsOf(bars,candidate,stageObservation(candidate,'D1')!).state,'EARLY_CANDIDATE')
})
test('D2/D3截至决策日锚点已经跌破',()=>{
  for(const stage of ['D2','D3']){
    assert.deepEqual(candidateLifecycleAsOf(bars,candidate,stageObservation(candidate,stage)!),{
      state:'ANCHOR_BREACHED',breachedAt:'2026-06-24'
    })
  }
})
test('未来价格不得回填D1',()=>{
  const future=[...bars,{date:'2026-06-26',open:8.4,high:8.4,low:8.3,close:8.35}]
  assert.equal(candidateLifecycleAsOf(future,candidate,stageObservation(candidate,'D1')!).state,'EARLY_CANDIDATE')
})
test('证据不足必须UNKNOWN',()=>{
  assert.equal(candidateLifecycleAsOf(bars.slice(1),candidate,stageObservation(candidate,'D3')!).state,'UNKNOWN')
})
test('重复日期或无序行情失败关闭',()=>{
  assert.throws(()=>assertUniqueSortedBars([bars[1],bars[0]]),/STRATEGY_BARS_NOT_STRICTLY_SORTED/)
  assert.throws(()=>assertUniqueSortedBars([bars[0],bars[0]]),/STRATEGY_BARS_NOT_STRICTLY_SORTED/)
})
