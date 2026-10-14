// S1: read-only evaluation API. No frontend success/risk recomputation.
import { apiClient } from './client'

export type StrategySide = 'BUY' | 'SELL'
export type OpportunityStatus = 'SUCCESS' | 'FAIL' | 'UNRESOLVED'
export type MetricKey = 'endpoint_pct' | 'mfe_pct' | 'mae_pct' | 'mdd_pct'
export interface HistogramBin { low: number; high: number; count: number }
export interface ResearchMetadata {
  strategy_key: string
  strategy_name: string
  version: string
  generated_at: string
  evaluation_protocol: string
  model_sha256: string
  data_sha256: string
  audit_ref: string
  status: 'AUDITED'
  cohort: 'ALL_EX_ANTE_ALERTS'
}
export interface ModelEvaluation {
  model_id: string
  model_name: string
  n_signals: number
  n_resolved: number
  n_unresolved: number
  n_success: number
  success_rate: number | null
  n_path: number
  joint_count: number
  joint_rate: number | null
  endpoint_p50: number | null
  mfe_p50: number | null
  mfe_p75: number | null
  mae_p80: number | null
  mdd_p90: number | null
  touch_order: Record<'TARGET_FIRST' | 'RISK_FIRST' | 'SAME_BAR_UNKNOWN' | 'NEITHER' | 'UNAVAILABLE', number>
  distributions: Record<MetricKey, HistogramBin[]>
}
export interface ResearchPoint {
  event_id: string
  model_id: string
  symbol: string
  signal_time: string
  mfe_pct: number
  mae_pct: number
  outcome: OpportunityStatus
  // Optional in legacy research snapshots; all five must be present for deep links.
  candidate_id?: string | null
  observation_id?: string | null
  stage?: 'D1' | 'D2' | 'D3' | null
  decision_date?: string | null
  model_release_id?: string | null
}
export interface ResearchEventRow extends Omit<ResearchPoint, 'mfe_pct' | 'mae_pct'> {
  mdd_pct: number | null
  endpoint_pct: number | null
  mfe_pct: number | null
  mae_pct: number | null
}
export interface StrategyEvaluationResponse {
  metadata: ResearchMetadata
  side: StrategySide
  symbol: string | null
  gain_pct: number
  risk_pct: number
  models: ModelEvaluation[]
  points: ResearchPoint[]
  points_sampled: boolean
  table: ResearchEventRow[]
}
export async function getZigzagEvaluation(params: {
  side: StrategySide; symbol?: string; gain_pct: number; risk_pct: number
}): Promise<StrategyEvaluationResponse> {
  const { data } = await apiClient.get<StrategyEvaluationResponse>(
    '/v1/strategy-research/zigzag/evaluation', { params },
  )
  return data
}
