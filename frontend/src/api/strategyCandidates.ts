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
