import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { displayedObservation, scoreBadgeDate } from '../candidateObservation.ts'
import type { Candidate, Observation } from '@/api/strategyCandidates'

const observations: Observation[] = [
  {observation_id:'o1',stage:'D1',decision_date:'2026-06-03',data_cutoff:'2026-06-03',l1_score:.5,l2_score:.4,l1_threshold:.3,l2_threshold:.3,joint_alert:true,first_breakout:false,reference_price:null,reference_confirmed_at:null},
  {observation_id:'o2',stage:'D2',decision_date:'2026-06-04',data_cutoff:'2026-06-04',l1_score:.7,l2_score:.6,l1_threshold:.3,l2_threshold:.3,joint_alert:true,first_breakout:false,reference_price:null,reference_confirmed_at:null},
]
const candidate: Candidate = {candidate_id:'c1',anchor_date:'2026-06-01',visible_at:'2026-06-02',anchor_price:9.4,kind:'L',observations}

test('score badge is attached to decision date, never anchor date', () => {
  assert.equal(scoreBadgeDate(candidate,null,'D1'), '2026-06-04')
  assert.equal(scoreBadgeDate(candidate,'c1','D1'), '2026-06-03')
  assert.equal(scoreBadgeDate(candidate,'c1','D2'), '2026-06-04')
  assert.notEqual(scoreBadgeDate(candidate,'c1','D1'),candidate.anchor_date)
})

test('selected candidate does not silently fall back to latest when stage unavailable', () => {
  assert.equal(displayedObservation(candidate,'c1','D3'),undefined)
})
