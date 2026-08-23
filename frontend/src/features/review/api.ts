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


