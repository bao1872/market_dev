/** The score is an as-of DECISION observation, never an anchor-day fact. */
import type { Candidate, Observation } from '@/api/strategyCandidates'

export function displayedObservation(candidate: Candidate, selectedId: string | null, selectedStage: string): Observation | undefined {
  if (candidate.candidate_id === selectedId) {
    return candidate.observations.find(o => o.stage === selectedStage)
  }
  return candidate.observations[candidate.observations.length - 1]
}

export function scoreBadgeDate(candidate: Candidate, selectedId: string | null, selectedStage: string): string | undefined {
  return displayedObservation(candidate, selectedId, selectedStage)?.decision_date
}
