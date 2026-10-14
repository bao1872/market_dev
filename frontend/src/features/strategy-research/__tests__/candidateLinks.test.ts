import { test } from 'node:test'
import { strict as assert } from 'node:assert'
import { buildCandidateDeepLink } from '../candidateLinks.ts'

const event = {
  symbol: 'SH:600000', candidate_id: 'L|600000|2026-06-01',
  observation_id: 'obs#D2', stage: 'D2' as const,
  decision_date: '2026-06-03', model_release_id: 'release-17',
}

test('historical event links to exact candidate, observation, decision day and release', () => {
  const url = buildCandidateDeepLink(event)
  assert.ok(url)
  const params = new URL(url, 'https://panji.local').searchParams
  assert.equal(params.get('tab'), 'stock')
  assert.equal(params.get('candidate_id'), event.candidate_id)
  assert.equal(params.get('observation_id'), event.observation_id)
  assert.equal(params.get('decision_date'), event.decision_date)
  assert.equal(params.get('release_id'), event.model_release_id)
})

test('missing or incomplete provenance MUST NOT fall back to a stock-only link', () => {
  for (const key of ['candidate_id', 'observation_id', 'stage', 'decision_date', 'model_release_id'] as const) {
    assert.equal(buildCandidateDeepLink({...event, [key]: null}), null)
  }
  assert.equal(buildCandidateDeepLink({...event, symbol:'invalid'}), null)
  assert.equal(buildCandidateDeepLink({...event, decision_date:'2026/06/03'}), null)
})
