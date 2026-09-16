// Board Analysis 领域 API owner（[S3-E] 由 endpoints.ts 迁出；read-only）。
// admin 计算触发（triggerComputeBoard/All）仍属 ./admin。

import { apiClient } from './client'

// ===== [CHANGE-20260730-011] 板块分析 V1 端点 =====
// ============================================================

/** 板块分析快照 DTO（与后端 BoardAnalysisSnapshotDTO 对齐） */
export interface BoardAnalysisSnapshotDTO {
  id: string
  trade_date: string
  board_id: string
  board_type: 'industry' | 'concept'
  board_name: string
  source_core_run_id: string
  algorithm_version: string
  parameter_hash: string
  eligible_count: number
  ready_count: number
  coverage_ratio: number
  missing_count: number
  missing_reasons: Record<string, number>
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'partial'
  payload: Record<string, unknown>
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
  is_stale: boolean
  is_published: boolean
}

/** 板块分析列表响应 */
export interface BoardAnalysisListResponse {
  items: BoardAnalysisSnapshotDTO[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

/** 板块分析详情响应 */
export interface BoardAnalysisDetailResponse {
  snapshot: BoardAnalysisSnapshotDTO
}

/** 板块分析列表查询参数 */
export interface BoardAnalysisListParams {
  type?: 'industry' | 'concept'
  trade_date?: string
  sort?: 'coverage_desc' | 'coverage_asc' | 'name_asc' | 'ready_desc'
  page?: number
  page_size?: number
}

/** 查询板块分析列表 */
export async function getBoardAnalysisList(
  params?: BoardAnalysisListParams,
  options?: { signal?: AbortSignal },
): Promise<BoardAnalysisListResponse> {
  const { data } = await apiClient.get<BoardAnalysisListResponse>('/v1/boards/analysis', {
    params,
    signal: options?.signal,
  })
  return data
}

/** 查询单板块分析详情 */
export async function getBoardAnalysisDetail(
  boardId: string,
  params?: { trade_date?: string },
  options?: { signal?: AbortSignal },
): Promise<BoardAnalysisDetailResponse> {
  const { data } = await apiClient.get<BoardAnalysisDetailResponse>(
    `/v1/boards/${boardId}/analysis`,
    { params, signal: options?.signal },
  )
  return data
}

/** [Admin] 触发单板块分析计算 */
// ============================================================
// ===== Structural Factors 端点 =====
// ============================================================

/**
 * 结构状态因子查询参数
 * - primary_timeframe: 主周期（默认 1d）
 * - secondary_timeframe: 副周期（默认 15m）
 * - adj: 复权方式（默认 qfq）
 * - as_of: 截止时间（默认 latest）
 */

