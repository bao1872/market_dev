/** Admin control plane: the server owns rolling split dates and task readiness. */
import { apiClient } from './client'

export type DateWindow = { start: string; end: string }
export type TrainConfig = { as_of:string; train: DateWindow; val: DateWindow; test: DateWindow; trees: number }
export type TrainWindows = TrainConfig & {
  status: 'AUTHORITATIVE_ROLLING_24_6_3'
  train_enqueue_enabled: boolean
}
export interface ModelRelease {
  release_id: string | null
  bundle_sha256: string | null
  label_contract: string | null
  status: string | null
  created_at: string | null
}
export interface ReleaseList {
  status?: string
  active_release: { release_id: string; activated_at: string } | null
  releases: ModelRelease[]
}
export async function getTrainingWindows():Promise<TrainWindows> {
  const {data}=await apiClient.get<TrainWindows>('/v1/admin/strategy-research/zigzag/train/windows')
  return data
}
export async function listModelReleases(): Promise<ReleaseList> {
  const {data} = await apiClient.get<ReleaseList>('/v1/admin/strategy-research/zigzag/model-releases')
  return data
}
export async function previewTraining(config: TrainConfig): Promise<{valid:boolean;status:string;message:string}> {
  const {data} = await apiClient.post('/v1/admin/strategy-research/zigzag/train/preview',config)
  return data
}
export async function queueTraining(config: TrainConfig): Promise<{job_id:string;status:string}> {
  const {data} = await apiClient.post('/v1/admin/strategy-research/zigzag/train-runs',config)
  return data
}
export async function requestPrediction(trade_date: string): Promise<{job_id:string;status:string}> {
  const {data} = await apiClient.post('/v1/admin/strategy-research/zigzag/prediction-runs',{trade_date})
  return data
}
export async function getStrategyJob(jobId: string): Promise<{job_id:string;status:string;error?:string}> {
  const {data} = await apiClient.get(`/v1/admin/strategy-research/zigzag/train-runs/${encodeURIComponent(jobId)}`)
  return data
}
export async function activateModelRelease(releaseId:string, evaluationSha:string): Promise<{status:string}> {
  const {data} = await apiClient.post(`/v1/admin/strategy-research/zigzag/model-releases/${encodeURIComponent(releaseId)}/publish`,
    {release_id:releaseId,candidate_report_sha256:evaluationSha})
  return data
}
