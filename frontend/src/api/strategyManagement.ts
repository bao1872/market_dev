/** Admin control plane: the server owns rolling split dates and task readiness. */
import { apiClient } from './client'

export interface ModelRelease {
  as_of?: string
  production_eligible?: boolean
  evaluation_sha256?: string
  validation?: Record<string, number>
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
export async function listModelReleases(): Promise<ReleaseList> {
  const {data} = await apiClient.get<ReleaseList>('/v1/admin/strategy-research/zigzag/model-releases')
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
export interface StrategyJob {
  job_id: string; kind: 'TRAIN' | 'PREDICT' | 'EVALUATE'; status: string; error?: string; stdout_tail?: string
  progress?: {stage:string;completed:number;total:number;updated_at:string}
  result?: {output?:string;bundle_path?:string}; created_at:string
}
export async function getStrategyJobs(): Promise<{status:string;items:StrategyJob[]}> {
  const {data}=await apiClient.get('/v1/admin/strategy-research/zigzag/jobs');return data
}
export async function retryStrategyJob(jobId:string):Promise<{job_id:string;status:string}> {
  const {data}=await apiClient.post(`/v1/admin/strategy-research/zigzag/jobs/${encodeURIComponent(jobId)}/retry`);return data
}
export async function getStrategyRuntime():Promise<{status:string;can_train:boolean;can_predict:boolean;can_validate_delivery?:boolean;after_close?:{status:string;checked_at?:string;error?:string;reason?:string;backlog_exceeds_31_sessions?:boolean}}> {
  const {data}=await apiClient.get('/v1/admin/strategy-research/zigzag/runtime');return data
}
export async function readTrainingReport(jobId:string):Promise<{status:string;model_count:number;metrics:Record<string,Record<string,{n:number;precision:number|null;recall:number|null;auc:number|null}>>}> {
  const {data}=await apiClient.get(`/v1/admin/strategy-research/zigzag/train-runs/${encodeURIComponent(jobId)}/report`);return data
}
export async function auditModelRelease(releaseId:string,sha:string):Promise<{status:string}> {
  const {data}=await apiClient.post(`/v1/admin/strategy-research/zigzag/model-releases/${encodeURIComponent(releaseId)}/audit`,{release_id:releaseId,candidate_report_sha256:sha});return data
}

export async function receiveLabDelivery(file:File, manifestSha:string, tradeDate:string):Promise<{job_id:string;status:string;delivery_id:string}> {
  const chunkSize=512*1024
  if(file.size<1 || file.size>128*1024*1024)throw new Error('交付包须为 1 字节至 128 MiB')
  const count=Math.ceil(file.size/chunkSize)
  let result:{job_id:string;status:string;delivery_id:string}|undefined
  for(let index=0;index<count;index++){
    const body=new FormData()
    body.append('file',file.slice(index*chunkSize,(index+1)*chunkSize),file.name)
    body.append('manifest_sha256',manifestSha)
    body.append('trade_date',tradeDate)
    body.append('chunk_index',String(index))
    body.append('total_bytes',String(file.size))
    const {data}=await apiClient.post('/v1/admin/strategy-research/zigzag/deliveries/chunks',body,{headers:{'Content-Type':undefined},timeout:120000})
    if(index===count-1)result=data
  }
  if(!result?.job_id)throw new Error('交付未完整接收，未登记评价任务')
  return result
}
