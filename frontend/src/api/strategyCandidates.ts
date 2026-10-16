/** Public read contracts for externally audited, never synthetic strategy data. */
import { apiClient } from './client'
export type Stage = 'D1' | 'D2' | 'D3'
export type Kind = 'H' | 'L'
export interface Bar { date: string; open: number; high: number; low: number; close: number }
export interface Observation {
  observation_id: string; stage: Stage; decision_date: string; data_cutoff: string
  l1_score: number | null; l2_score: number | null
  l1_threshold: number | null; l2_threshold: number | null
  joint_alert: boolean; first_breakout: boolean
  reference_price: number | null; reference_confirmed_at: string | null
}
export interface Candidate {
  candidate_id: string; anchor_date: string; visible_at: string
  anchor_price: number; kind: Kind; observations: Observation[]
}
export interface StockResearch { symbol: string; release_id: string; bars: Bar[]; candidates: Candidate[] }
export interface StructurePivot {
  anchor_date: string; kind: Kind; price: number; seen_at: string; confirmed_at: string
}
export interface StructureEarlyCandidate {
  anchor_date: string; visible_at: string; kind: Kind; price: number; model_scored: false
}
export interface StrategyStructure {
  algorithm: 'frozen-panji-lab-B55-replay'
  algorithm_sha256: string; as_of: string
  source_bar_hash: string; adj_factor_hash: string
  coverage_start: string; coverage_end: string
  confirmed_pivots: StructurePivot[]; early_candidates: StructureEarlyCandidate[]
  latest_mutable_count: number; ambiguous_anchor_count: number
  confirm_lineage: string; decision_pit_verified: false
  hindsight_label_used_for_model_scoring: false; raw_ohlcv_volume_used_for_features: false
  notes: string
}

export interface StrategyHistory {
  symbol:string; bars:Bar[]; count:number; requested_count:number; actual_start:string; actual_end:string
  as_of:string; price_adjustment:'qfq'; source_policy:'MDAS_DB_ONLY'; source_bar_hash:string
  adj_factor_hash:string; history_exhausted:boolean; candidate_overlay_supported:false
  candidate_overlay_reason:string
  structure?:StrategyStructure
}
export async function getStrategyHistory(symbol:string,limit=500):Promise<StrategyHistory> {
  const {data}=await apiClient.get<StrategyHistory>(
    `/v1/strategy-research/stocks/${encodeURIComponent(symbol)}/bars`,{params:{limit,include_structure:true}})
  return data
}
export interface DailyRow {
  observation_id: string; candidate_id: string; symbol: string; kind: Kind; stage: Stage
  l1_score: number | null; l2_score: number | null; joint_alert: boolean; first_breakout: boolean
}
export interface DailyResult { trade_date: string; release_id: string; status: 'PUBLISHED'; freshness: 'CURRENT' | 'HISTORICAL'; expected_trade_date: string; items: DailyRow[] }
export async function getStockResearch(symbol: string, releaseId?: string): Promise<StockResearch> {
  const { data } = await apiClient.get<StockResearch>(
    `/v1/strategy-research/stocks/${encodeURIComponent(symbol)}/candidates`,
    { params: releaseId ? { release_id: releaseId } : undefined },
  )
  return data
}
export async function getDailyResearch(): Promise<DailyResult> {
  const { data } = await apiClient.get<DailyResult>('/v1/strategy-research/daily')
  return data
}
