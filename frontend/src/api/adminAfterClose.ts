// Admin AfterClose / JobRuns 领域 API owner
//
// [S3-D] 由 endpoints.ts 迁出（对齐 S5 AfterClose 拆核）。endpoints.ts 仅兼容 re-export。

import { apiClient } from './client'
import type { DataFreshness } from './admin'

// ============================================================
// ===== AfterClose & JobRunEvents 端点 =====
// ============================================================

/** 任务执行事件（时间线条目） */
export interface JobRunEvent {
  id: string
  job_run_id: string
  step: string
  level: 'info' | 'warn' | 'error'
  message: string
  payload: Record<string, unknown> | null
  created_at: string
}

/** 任务事件时间线响应 */
export interface JobRunEventListResponse {
  items: JobRunEvent[]
  total: number
}

/** 盘后编排状态响应（含编排状态 + DSA run 状态 + 事件时间线 + [Phase7] 详情） */
export interface AfterCloseRunStatusResponse {
  job_run_id: string
  job_name: string
  business_date: string | null
  status: string
  orchestrator_status: string
  trade_date: string | null
  dsa_run_id: string | null
  dsa_run_status: string | null
  started_at: string | null
  finished_at: string | null
  error_message: string | null
  // [Phase7] - 详情字段（管理后台展示）
  worker_instance_id: string | null
  heartbeat_at: string | null
  lease_expires_at: string | null
  last_completed_step: string | null
  // [AfterClose] - 跳过原因（如 NON_TRADING_DAY 非交易日），供前端展示提示
  skip_reason: string | null
  interrupt_reason: string | null
  is_retryable: boolean
  heartbeat_stale: boolean
  events: JobRunEvent[]
}

/** 盘后编排创建/重试响应 */
export interface AfterCloseRunCreateResponse {
  job_run_id: string
  status: string
  orchestrator_status: string
  trade_date: string
  message: string
}

/** 查询任务执行事件时间线（按 created_at 倒序） */
export async function getJobRunEvents(
  runId: string,
  limit: number = 100,
): Promise<JobRunEventListResponse> {
  const { data } = await apiClient.get<JobRunEventListResponse>(
    `/v1/admin/job-runs/${runId}/events`,
    { params: { limit } },
  )
  return data
}

/** 查询盘后编排状态（含事件时间线 + DSA run 状态） */
export async function getAfterCloseRunStatus(
  runId: string,
): Promise<AfterCloseRunStatusResponse> {
  const { data } = await apiClient.get<AfterCloseRunStatusResponse>(
    `/v1/admin/after-close-runs/${runId}`,
  )
  return data
}

/** 创建并异步执行盘后编排 */
export async function createAfterCloseRun(
  tradeDate: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    '/v1/admin/after-close-runs',
    { trade_date: tradeDate },
  )
  return data
}

/** 强制重新执行盘后编排（非 failed 状态也可触发） */
export async function forceAfterCloseRun(
  runId: string,
  restartFrom?: 'daily_ready',
): Promise<AfterCloseRunCreateResponse> {
  const params = restartFrom ? { restart_from: restartFrom } : undefined
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    `/v1/admin/after-close-runs/${runId}/force`,
    undefined,
    { params },
  )
  return data
}

/** 重试失败的盘后编排任务 */
export async function retryAfterCloseRun(
  runId: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    `/v1/admin/after-close-runs/${runId}/retry`,
  )
  return data
}

/** [Phase6] 从失败步骤继续（保留断点检查点，不重复拉行情） */
export async function resumeAfterCloseRun(
  runId: string,
): Promise<AfterCloseRunCreateResponse> {
  const { data } = await apiClient.post<AfterCloseRunCreateResponse>(
    `/v1/admin/after-close-runs/${runId}/resume`,
  )
  return data
}

// ============================================================
// ===== AfterClose Pipeline 聚合状态端点（/admin/after-close/pipeline/*）=====
// ============================================================
//
// 与 backend/app/schemas/after_close_pipeline.py 严格对齐：
// - AfterClosePipelineResponse / PipelineStep / AfterCloseRunSummary
// - FeatureSnapshotRunSummary / PipelineEventItem / PipelineRunItem
// - AfterClosePipelineRunListResponse / AfterClosePipelineRunRequest / AfterClosePipelineRunResponse
//
// 复用已有类型：
// - DataFreshness / BarsFreshness / StrategyFreshness（同文件上方）
// - JobRunEvent（与 PipelineEventItem 字段完全一致，事件时间线条目）

/** 盘后流水线单步骤真实状态（对齐后端 PipelineStep/step_summary） */
export type AfterCloseStepStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'skipped_unavailable'
  | 'cancelled'

export interface PipelineStep {
  step: string
  status: AfterCloseStepStatus
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  counts: Record<string, unknown>
  processed?: number | null
  total?: number | null
  last_progress_at?: string | null
  elapsed_seconds?: number | null
  error_code?: string | null
  retry_count?: number | null
  optional?: boolean
  attempt?: number | null
  error_message: string | null
  // [TIMELINE-FIX] 异常/诊断信息（如 invalid_order_or_zero_duration），
  // 存在时前端显示"未知"而非用 0/max(0,x) 掩盖。
  warnings?: string[] | null
}

/** after_close_orchestrator job_run 摘要（对齐后端 AfterCloseRunSummary） */
export interface AfterCloseRunSummary {
  job_run_id: string
  status: string
  orchestrator_status: string | null
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
  lease_expires_at: string | null
  last_completed_step: string | null
  error_code: string | null
  error_message: string | null
  worker_instance_id: string | null
  trade_date: string | null
  parent_job_run_id?: string | null
  restart_from?: string | null
  partial_success?: boolean
  step_summary?: PipelineStep[]
}

/** 服务端计算的盘后运行诊断，时间类语义不得由前端自行推导 */
export interface AfterCloseDiagnostics {
  processed: number | null
  total: number | null
  last_progress_at: string | null
  heartbeat_age_seconds: number | null
  lease_remaining_seconds: number | null
  elapsed_seconds: number | null
  retry_count: number | null
  publication_status: string | null
  partial_success: boolean | null
}

/** stock_feature_snapshot_run 摘要（对齐后端 FeatureSnapshotRunSummary） */
export interface FeatureSnapshotRunSummary {
  run_id: string
  run_type: string
  status: string
  scope: string
  snapshot_count: number | null
  failed_count: number | null
  skipped_count: number | null
  expected_count: number | null
  published_at: string | null
  started_at: string | null
  finished_at: string | null
}

/**
 * 盘后流水线聚合状态响应（对齐后端 AfterClosePipelineResponse）。
 *
 * overall_status 枚举：
 * - not_started：当日尚无 after_close_orchestrator 运行
 * - running：编排任务正在运行
 * - succeeded：编排成功且 watchlist_ready=true
 * - failed：编排失败
 * - blocked：收盘后超过 30 分钟仍无运行（含 has_backfill_full 时不计入 blocked）
 * - skipped：非交易日跳过
 *
 * watchlist_ready 严格判定：status='succeeded' AND published_at IS NOT NULL AND metadata_.scope='full'
 * （sample backfill 不计入 watchlist_ready，仅作为参考展示）
 */
export interface AfterClosePipelineResponse {
  trade_date: string
  market_session: string
  overall_status:
    | 'not_started'
    | 'running'
    | 'succeeded'
    | 'failed'
    | 'blocked'
    | 'skipped'
  watchlist_ready: boolean
  watchlist_reason: string
  // [AC2-2026-09-14] 失败诊断：由后端从 step_summary 推导，前端无需猜测
  failed_step: string | null
  has_backfill_full: boolean
  after_close_run: AfterCloseRunSummary | null
  steps: PipelineStep[]
  diagnostics?: AfterCloseDiagnostics | null
  data_freshness: DataFreshness
  feature_snapshot_run: FeatureSnapshotRunSummary | null
  events: JobRunEvent[]
}

/** 最近运行列表单条记录（after_close_orchestrator 或 snapshot_run，对齐后端 PipelineRunItem） */
export interface PipelineRunItem {
  kind: 'after_close_orchestrator' | 'snapshot_run'
  job_run_id: string | null
  run_id: string | null
  trade_date: string | null
  status: string
  orchestrator_status: string | null
  run_type: string | null
  scope: string | null
  snapshot_count: number | null
  failed_count: number | null
  published_at: string | null
  started_at: string | null
  finished_at: string | null
  error_message: string | null
  worker_instance_id: string | null
  last_completed_step: string | null
}

/** 最近运行列表响应（对齐后端 AfterClosePipelineRunListResponse） */
export interface AfterClosePipelineRunListResponse {
  items: PipelineRunItem[]
  total: number
}

// [BOARD-LOCAL-OWNERSHIP-01] current restart-step vocabulary：syncing_boards 已迁出
// 盘后 DAG（板块/概念同步改为本地手动同步），不再是合法 restart 起点。
// 历史 run 若返回 syncing_boards 事件，前端按 legacy 只读展示，不作为 current step。
export type AfterCloseRestartStep =
  | 'refreshing_daily'
  | 'checking_coverage'
  | 'computing_features'
  | 'publishing'
  | 'computing_review'

/** POST /admin/after-close/pipeline/run 请求体；仅用于幂等创建。 */
export interface AfterClosePipelineRunRequest {
  trade_date: string
}

/** 所有盘后管理动作共用的稳定响应。 */
export interface AfterCloseRunActionResponse {
  job_run_id: string
  trade_date: string
  status: string
  message?: string
  is_new: boolean
  orchestrator_status?: string | null
  parent_job_run_id?: string | null
  restart_from?: string | null
}

export interface AfterClosePipelineRunResponse {
  job_run_id: string
  trade_date: string
  status: string
  orchestrator_status: string | null
  is_new: boolean
}

/**
 * 查询最近交易日的盘后流水线聚合状态（admin）。
 * 后端自动定位最近交易日（含今日）：GET /admin/after-close/pipeline/latest
 */
export async function getAfterClosePipelineLatest(): Promise<AfterClosePipelineResponse> {
  const { data } = await apiClient.get<AfterClosePipelineResponse>(
    '/v1/admin/after-close/pipeline/latest',
  )
  return data
}

/**
 * 查询指定交易日的盘后流水线聚合状态（admin）。
 * GET /admin/after-close/pipeline?trade_date=YYYY-MM-DD
 */
export async function getAfterClosePipelineByDate(
  tradeDate: string,
): Promise<AfterClosePipelineResponse> {
  const { data } = await apiClient.get<AfterClosePipelineResponse>(
    '/v1/admin/after-close/pipeline',
    { params: { trade_date: tradeDate } },
  )
  return data
}

/**
 * 查询最近 N 次运行（after_close_orchestrator + snapshot_run 混合列表，admin）。
 * GET /admin/after-close/pipeline/runs?limit=20
 */
export async function getAfterClosePipelineRuns(
  limit: number = 20,
): Promise<AfterClosePipelineRunListResponse> {
  const { data } = await apiClient.get<AfterClosePipelineRunListResponse>(
    '/v1/admin/after-close/pipeline/runs',
    { params: { limit } },
  )
  return data
}

/**
 * 管理员触发指定交易日的 after_close 编排任务（admin，幂等）。
 * POST /admin/after-close/pipeline/run
 * 同 trade_date 已有 queued/running/succeeded 时返回 existing，不重复创建。
 */
export async function createAfterClosePipelineRun(
  payload: AfterClosePipelineRunRequest,
): Promise<AfterClosePipelineRunResponse> {
  const { data } = await apiClient.post<AfterClosePipelineRunResponse>(
    '/v1/admin/after-close/pipeline/run',
    payload,
  )
  return data
}

export async function cancelAfterCloseRun(
  runId: string,
  reason?: string,
): Promise<AfterCloseRunActionResponse> {
  const { data } = await apiClient.post<AfterCloseRunActionResponse>(
    `/v1/admin/after-close-runs/${runId}/cancel`,
    reason ? { reason } : {},
  )
  return data
}

export async function reconcileAfterCloseRun(
  runId: string,
  reason?: string,
): Promise<AfterCloseRunActionResponse> {
  const { data } = await apiClient.post<AfterCloseRunActionResponse>(
    `/v1/admin/after-close-runs/${runId}/reconcile`,
    reason ? { reason } : {},
  )
  return data
}

export async function restartAfterCloseRun(runId: string): Promise<AfterCloseRunActionResponse> {
  const { data } = await apiClient.post<AfterCloseRunActionResponse>(
    `/v1/admin/after-close-runs/${runId}/resume`,
  )
  return data
}

export async function forceRestartAfterCloseRun(
  runId: string,
  restartFrom?: 'daily_ready',
): Promise<AfterCloseRunActionResponse> {
  const response = await forceAfterCloseRun(runId, restartFrom)
  return { ...response, is_new: true }
}


