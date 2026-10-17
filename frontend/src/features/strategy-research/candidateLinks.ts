import { strategyPreviewKey } from '../../api/strategyPreview'
/** A historical record is navigable only with a complete event identity. */
export interface CandidateLinkIdentity {
  symbol: string
  candidate_id?: string | null
  observation_id?: string | null
  stage?: 'D0' | 'D1' | 'D2' | 'D3' | null
  decision_date?: string | null
  model_release_id?: string | null
}
const symbolRE = /^(SH|SZ|BJ):[0-9]{6}$/
const dateRE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/

export function buildCandidateDeepLink(row: CandidateLinkIdentity): string | null {
  if (!symbolRE.test(row.symbol) || !row.candidate_id || !row.observation_id || !row.stage ||
      !dateRE.test(row.decision_date ?? '') || !row.model_release_id) return null
  const query = new URLSearchParams({
    tab: 'stock', symbol: row.symbol, candidate_id: row.candidate_id,
    observation_id: row.observation_id, stage: row.stage,
    decision_date: row.decision_date!, release_id: row.model_release_id,
  })
  const preview = strategyPreviewKey()
  if (preview) query.set('preview_batch', preview)
  return `/strategy?${query.toString()}`
}
