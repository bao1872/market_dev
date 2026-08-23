// [ReviewApi] - 描述: 复盘模块 API 调用函数（PRD §12）
// 基于 axios apiClient（baseURL=/api，Vite 代理去掉 /api 前缀）
// 后端 router prefix=/v1/review，调用时传 /v1/review/... 相对路径（浏览器请求为 /api/v1/review/...）
// endpoint 禁止包含网关前缀 /api。
//
// 规则：
// - 用户侧只读 DB，不触发计算
// - 写操作（追踪）要求 idempotency_key
// - 404/422/500 由调用方通过 extractReviewError 解析明确原因与 request_id
import { apiClient } from '@/api/client'
import type {
  ReviewDatesResponse,
  ReviewLatestResponse,
  ReviewOverview,
  ReviewScopeListResponse,
  ReviewScopeListParams,
  ReviewScopeCompositionDetailResponse,
  LegacyReviewScopeListResponse,
  LegacyReviewScopeListParams,
  ReviewSignalListResponse,
  ReviewSignalListParams,
  ReviewSignal,
  ReviewAttributionListResponse,
  ReviewAttributionListParams,
  ReviewInstrumentListResponse,
  ReviewInstrumentListParams,
  ReviewTrackingListResponse,
  ReviewTrackingListParams,
  ReviewTracking,
  ReviewTrackingCreateRequest,
  ReviewTrackingPatchRequest,
  ReviewTrackingEvaluationListResponse,
} from './types'

// ============================================================
// 错误解析（PRD §15：404/422/500 显示明确原因及 request_id）
// ============================================================

export interface ReviewApiError {
  status: number | null
  detail: string
  requestId: string | null
  message: string
}

/**
 * 从 axios 错误中提取可展示的错误信息。
 * - 404：资源不存在或未发布
 * - 422：参数校验失败，附带后端 detail
 * - 500：服务器错误，附带 x-request-id
 */
export function extractReviewError(err: unknown): ReviewApiError {
  const e = err as {
    response?: {
      status?: number
      data?: { detail?: string }
      headers?: { get?: (k: string) => string | null; 'x-request-id'?: string }
    }
    message?: string
  }
  const status = e?.response?.status ?? null
  const detail = e?.response?.data?.detail ?? ''
  const requestId =
    e?.response?.headers?.['x-request-id'] ??
    (e?.response?.headers?.get ? e.response.headers.get('x-request-id') : null) ??
    null
  let message: string
  if (status === 404) {
    message = detail || '资源不存在或复盘尚未发布'
  } else if (status === 422) {
    message = `参数校验失败${detail ? `：${detail}` : ''}`
  } else if (status === 500) {
    message = `服务器错误${requestId ? `（request_id=${requestId}）` : ''}`
  } else if (status === 403) {
    message = '权限不足，当前账号无复盘权限'
  } else if (status === 401) {
    message = '登录已过期，请重新登录'
  } else if (status) {
    message = `请求失败（HTTP ${status}）${detail ? `：${detail}` : ''}`
  } else {
    message = e?.message || '网络错误，请稍后重试'
  }
  return { status, detail, requestId, message }
}

// ============================================================
// 12.1 日期与总览
// ============================================================

/** GET /v1/review/dates — 已发布复盘交易日列表（降序） */
export async function getReviewDates(): Promise<ReviewDatesResponse> {
  const { data } = await apiClient.get<ReviewDatesResponse>('/v1/review/dates')
  return data
}

/** GET /v1/review/latest → /v1/review/latest — 最新已发布复盘 run 信息 */
export async function getReviewLatest(): Promise<ReviewLatestResponse> {
  const { data } = await apiClient.get<ReviewLatestResponse>('/v1/review/latest')
  return data
}

/** GET /v1/review/{trade_date}/overview → /v1/review/{trade_date}/overview — 当日总览 */
export async function getReviewOverview(
  tradeDate: string,
  includePartial = false,
): Promise<ReviewOverview> {
  const { data } = await apiClient.get<ReviewOverview>(
    `/v1/review/${tradeDate}/overview`,
    { params: { include_partial: includePartial } },
  )
  return data
}

// ============================================================
// [CANONICAL] 12.2 Scope-first 列表
// ============================================================

/**
 * GET /v1/review/{trade_date}/scopes
 * 返回 canonical Scope 列表（ReviewScopeListItem[] + 薄投影 summary）。
 * 仅传后端支持的参数：scope_type / include_partial / page / page_size。
 */
export async function getReviewScopes(
  tradeDate: string,
  params: ReviewScopeListParams = {},
): Promise<ReviewScopeListResponse> {
  const { data } = await apiClient.get<ReviewScopeListResponse>(
    `/v1/review/${tradeDate}/scopes`,
    { params },
  )
  return data
}

/**
 * GET /v1/review/{trade_date}/scopes/{scope_type}/{scope_key}
 * 单个 Scope 详情（9-key composition + 薄 observation）。
 * 不请求列表中每条 Scope 的详情；选中一个 Scope 才请求一个。
 */
export async function getReviewScopeDetail(
  tradeDate: string,
  scopeType: string,
  scopeKey: string,
  includePartial = false,
): Promise<ReviewScopeCompositionDetailResponse> {
  const { data } = await apiClient.get<ReviewScopeCompositionDetailResponse>(
    `/v1/review/${tradeDate}/scopes/${scopeType}/${scopeKey}`,
    { params: { include_partial: includePartial } },
  )
  return data
}

// ============================================================
// [LEGACY] 12.2 旧 P/Q/U/C/V 市场扫描（retired 于 canonical Scope 之后）
// 仅供给 Slice F 前的 legacy ReviewPage / MarketScanPanel 使用，
// 不属于 canonical Review API surface。禁止在 Slice D/E 新代码中使用。
// ============================================================

/** [LEGACY] GET /v1/review/{trade_date}/scopes（旧 P/Q/U/C/V 形态） */
export async function getLegacyReviewScopes(
  tradeDate: string,
  params: LegacyReviewScopeListParams = {},
): Promise<LegacyReviewScopeListResponse> {
  const { data } = await apiClient.get<LegacyReviewScopeListResponse>(
    `/v1/review/${tradeDate}/scopes`,
    { params },
  )
  return data
}

// ============================================================
// [LEGACY] 12.3 信号（retired 于 canonical Scope 之后；Slice F 删除）
// 仅供 legacy BoardAttributionPanel / TrackingReviewPanel / FilterDiscoveryPanel 使用。
// ============================================================

/** GET /v1/review/{trade_date}/signals → /v1/review/{trade_date}/signals — 信号列表 */
export async function getReviewSignals(
  tradeDate: string,
  params: ReviewSignalListParams = {},
): Promise<ReviewSignalListResponse> {
  const { data } = await apiClient.get<ReviewSignalListResponse>(
    `/v1/review/${tradeDate}/signals`,
    { params },
  )
  return data
}

/** GET /v1/review/signals/{signal_id} → /v1/review/signals/{signal_id} — 单信号详情 */
export async function getReviewSignal(
  signalId: string,
  includePartial = false,
): Promise<ReviewSignal> {
  const { data } = await apiClient.get<ReviewSignal>(
    `/v1/review/signals/${signalId}`,
    { params: { include_partial: includePartial } },
  )
  return data
}

// ============================================================
// [LEGACY] 12.4 归因与个股（retired；Slice F 删除）
// 仅供 legacy BoardAttributionPanel / StockValidationPanel 使用。
// ============================================================

/** GET /v1/review/signals/{signal_id}/attributions → /v1/review/signals/{signal_id}/attributions — 子范围归因 */
export async function getSignalAttributions(
  signalId: string,
  params: ReviewAttributionListParams = {},
): Promise<ReviewAttributionListResponse> {
  const { data } = await apiClient.get<ReviewAttributionListResponse>(
    `/v1/review/signals/${signalId}/attributions`,
    { params },
  )
  return data
}

/** GET /v1/review/signals/{signal_id}/instruments → /v1/review/signals/{signal_id}/instruments — 代表股票 */
export async function getSignalInstruments(
  signalId: string,
  params: ReviewInstrumentListParams = {},
): Promise<ReviewInstrumentListResponse> {
  const { data } = await apiClient.get<ReviewInstrumentListResponse>(
    `/v1/review/signals/${signalId}/instruments`,
    { params },
  )
  return data
}

// ============================================================
// [LEGACY] 12.5 追踪（retired；Slice F 删除）
// 仅供 legacy TrackingReviewPanel 使用。
// ============================================================

/** GET /v1/review/trackings → /v1/review/trackings — 当前用户追踪列表 */
export async function getReviewTrackings(
  params: ReviewTrackingListParams = {},
): Promise<ReviewTrackingListResponse> {
  const { data } = await apiClient.get<ReviewTrackingListResponse>(
    '/v1/review/trackings',
    { params },
  )
  return data
}

/** POST /v1/review/trackings → /v1/review/trackings — 新增追踪（幂等） */
export async function createReviewTracking(
  payload: ReviewTrackingCreateRequest,
): Promise<ReviewTracking> {
  const { data } = await apiClient.post<ReviewTracking>(
    '/v1/review/trackings',
    payload,
  )
  return data
}

/** PATCH /v1/review/trackings/{id} → /v1/review/trackings/{id} — 修改追踪（幂等） */
export async function updateReviewTracking(
  trackingId: string,
  payload: ReviewTrackingPatchRequest,
): Promise<ReviewTracking> {
  const { data } = await apiClient.patch<ReviewTracking>(
    `/v1/review/trackings/${trackingId}`,
    payload,
  )
  return data
}

/** DELETE /v1/review/trackings/{id} → /v1/review/trackings/{id} — 关闭追踪（不物理删除） */
export async function closeReviewTracking(
  trackingId: string,
  idempotencyKey: string,
): Promise<ReviewTracking> {
  const { data } = await apiClient.delete<ReviewTracking>(
    `/v1/review/trackings/${trackingId}`,
    { params: { idempotency_key: idempotencyKey } },
  )
  return data
}

/** GET /v1/review/trackings/{id}/evaluations → /v1/review/trackings/{id}/evaluations — 追踪逐日评估 */
export async function getTrackingEvaluations(
  trackingId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<ReviewTrackingEvaluationListResponse> {
  const { data } = await apiClient.get<ReviewTrackingEvaluationListResponse>(
    `/v1/review/trackings/${trackingId}/evaluations`,
    { params },
  )
  return data
}

// =========================================================================
// [LEGACY] [V2] Discovery API（retired 于 canonical Scope 之后；Slice F 删除）
// 仅供 legacy DiscoveryWorkspace / DiscoveryDetail 使用。
// =========================================================================

import type {
  DiscoveryListResponse,
  DiscoveryDetailResponse,
} from './types'

/** GET /v1/review/{tradeDate}/discoveries */
export async function getDiscoveries(
  tradeDate: string,
  params: {
    scope_type?: string
    scope_family?: string
    status?: string
    sort?: string
    page?: number
    page_size?: number
  } = {},
): Promise<DiscoveryListResponse> {
  const { data } = await apiClient.get<DiscoveryListResponse>(
    `/v1/review/${tradeDate}/discoveries`,
    { params },
  )
  return data
}

/** GET /v1/review/discoveries/{discoveryId} */
export async function getDiscoveryDetail(
  discoveryId: string,
  tradeDate?: string,
): Promise<DiscoveryDetailResponse> {
  const { data } = await apiClient.get<DiscoveryDetailResponse>(
    `/v1/review/discoveries/${discoveryId}`,
    { params: tradeDate ? { trade_date: tradeDate } : {} },
  )
  return data
}
