import {strict as assert} from 'node:assert'
import {test} from 'node:test'
import type {Candidate} from '../../../api/strategyCandidates'
import {scorePoints,scoreValue} from '../scorePoints'
import {chartWindow} from '../chartWindow'

test('missing scores stay absent; a real zero and its own threshold survive',()=>{
  const candidate={candidate_id:'c',kind:'L',anchor_date:'2026-10-09',observations:[
    {observation_id:'o1',stage:'D1',decision_date:'2026-10-12',d0_score:null,l1_score:0,l1_threshold:.4,l2_score:.8,l2_threshold:null},
    {observation_id:'o2',stage:'D2',decision_date:'2026-10-13',l1_score:.6,l1_threshold:.5,l2_score:.7,l2_threshold:.9},
  ]} as unknown as Candidate
  const points=scorePoints([candidate])
  assert.deepEqual(points.map(p=>p.id),['o1:L1','o2:L1','o2:L2'])
  assert.equal(points[0].anchorDate,'2026-10-09')
  assert.equal(points[0].date,'2026-10-12')
  assert.equal(scoreValue(points[0],true),0)
  assert.equal(scoreValue(points[0],false),-.4)
  assert.equal(points[1].date,points[2].date) // overlapping tasks remain distinct, no time jitter
  assert.notEqual(points[1].color,points[2].color)
})


test('initial and edge-event viewports retain 90 real bars without padding missing history',()=>{
  for(const focus of [undefined,0,179]){
    const window=chartWindow(180,focus,5)!
    assert.equal(window.to-window.from+1,90)
    assert.ok(window.from>=0 && window.to<180)
    if(focus!=null)assert.ok(window.from<=focus && window.to>=focus)
  }
  assert.deepEqual(chartWindow(89),{from:0,to:88})
  assert.equal(chartWindow(0),null)
})
