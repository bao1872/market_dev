// [MarketDashboardApi] - Market Dashboard 只读 API（F2 frozen endpoints）
// 后端 router prefix = /v1/market-dashboard；DTO 字段 snake_case 与后端 1:1，
// 不在 API 层偷偷转 camelCase（前端只渲染，绝不重算）。
import { apiClient } from '@/api/client'

export interface MarketDashboardCard {
  ma5: number | null
  ma20: number | null
  ma50: number | null
  equal_weight_index: number | null
}

export interface MarketDashboardPoint {
  trade_date: string
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma50: number | null
  ma120: number | null
  ew_index: number | null
}

export interface MarketDashboardResponse {
  projection_trade_date: string | null
  cards: MarketDashboardCard
  series: MarketDashboardPoint[]
}

export interface BreadthPair {
  ma5: number | null
  ma10: number | null
}

export interface RankingItem {
  board_id: string
  board_name: string
  board_type: string
  hierarchy_level: string
  current: BreadthPair
  previous: BreadthPair
  delta: BreadthPair
}

export interface RankingsResponse {
  top: RankingItem[]
  bottom: RankingItem[]
  lookback: number
}

export interface ScopeMetadata {
  board_id: string
  name: string
  type: string
  hierarchy_level: string
  membership_version: string
  // [R3C0] additive：详情最新 projection row 的成员数（冻结事实，不从 membership 表重算）。
  member_count: number
}

export interface ScopePoint {
  trade_date: string
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma50: number | null
  ma120: number | null
  ew_index: number | null
}

export interface ScopeDetailResponse {
  projection_trade_date: string | null
  metadata: ScopeMetadata
  series: ScopePoint[]
}

export interface ComparePoint {
  trade_date: string
  ew_index: number | null
}

export interface CompareBoard {
  board_id: string
  board_name: string
  board_type: string
  points: ComparePoint[]
  // [R3D0] additive：精确比较矩阵（全局 T / T-5 口径，由后端一次性算好）。
  // 前端绝不自行计算 MA / delta / member_count。
  member_count: number | null
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma50: number | null
  ma120: number | null
  ma5_delta: number | null
  ma10_delta: number | null
}

export interface CompareResponse {
  // [R3D0] additive：矩阵统一 T / T-5 口径（与 R2 Scope Explorer 同语义）。
  projection_trade_date: string | null
  previous_trade_date: string | null
  boards: CompareBoard[]
}

export interface MarketDashboardApiError {
  status?: number | null
  detail?: string | null
  requestId?: string | null
  message: string
}

/**
 * 从 axios 错误中提取可展示错误信息（与 review extractReviewError 同范式）。
 * 401 由 apiClient 拦截器统一处理（自动 refresh + 必要时跳登录），此处不感知；
 * 403 归为权限不足文案（不伪装成“数据加载失败”）。
 */
export function extractMarketDashboardError(err: unknown): MarketDashboardApiError {
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
  if (status === 404) message = detail || '资源不存在或尚未生成'
  else if (status === 422) message = `参数校验失败${detail ? `：${detail}` : ''}`
  else if (status === 500) message = `服务器错误${requestId ? `（request_id=${requestId}）` : ''}`
  else if (status === 403) message = '权限不足，当前账号无市场数据访问权限'
  else message = detail || e?.message || '请求失败'
  return { status, detail, requestId, message }
}

export async function getMarketDashboard(days = 250): Promise<MarketDashboardResponse> {
  const res = await apiClient.get<MarketDashboardResponse>('/v1/market-dashboard/market', { params: { days } })
  return res.data
}

export interface RankingsQuery {
  scope_type: 'industry' | 'concept'
  hierarchy_level?: 'L1' | 'L2' | 'L3' | null
  lookback?: number
  limit?: number
}

export async function getMarketRankings(query: RankingsQuery): Promise<RankingsResponse> {
  const params: Record<string, string | number> = {
    scope_type: query.scope_type,
    lookback: query.lookback ?? 5,
    limit: query.limit ?? 10,
  }
  // concept 不分层：hierarchy_level 不传；industry 仅在明确层级时传。
  if (query.hierarchy_level) params.hierarchy_level = query.hierarchy_level
  const res = await apiClient.get<RankingsResponse>('/v1/market-dashboard/rankings', { params })
  return res.data
}

export async function getMarketScopeDetail(boardId: string, days = 250): Promise<ScopeDetailResponse> {
  const res = await apiClient.get<ScopeDetailResponse>(`/v1/market-dashboard/scopes/${boardId}`, { params: { days } })
  return res.data
}

export async function getMarketCompare(boardIds: string[], days = 10): Promise<CompareResponse> {
  const res = await apiClient.get<CompareResponse>('/v1/market-dashboard/compare', {
    params: { board_ids: boardIds.join(','), days },
  })
  return res.data
}

// ---------------------------------------------------------------------------
// [R3A] Scope Explorer（R2 backend：GET /v1/market-dashboard/scopes）
//
// DTO 与 backend `ScopeExplorerItem` / `ScopeExplorerResponse` 1:1（snake_case，
// 不在 API 层转 camelCase）。参数序列化真源 = features/market-dashboard/scopeExplorerQuery.ts，
// 前端绝不重算 ratio / delta / 排序 / 分页。
// ---------------------------------------------------------------------------
export interface ScopeExplorerItem {
  board_id: string
  board_name: string
  board_type: string
  hierarchy_level: string
  membership_version: string
  member_count: number
  ma5: number | null
  ma10: number | null
  ma20: number | null
  ma50: number | null
  ma120: number | null
  previous_ma5: number | null
  previous_ma10: number | null
  ma5_delta: number | null
  ma10_delta: number | null
}

export interface ScopeExplorerResponse {
  projection_trade_date: string | null
  previous_trade_date: string | null
  total: number
  page: number
  page_size: number
  items: ScopeExplorerItem[]
}

/**
 * params 必须由 `buildScopeExplorerParams()` 生成（typed query 的归属方在 feature 层，
 * API 层只做 transport，避免 api → feature 的反向依赖）。
 */
export async function getMarketScopeExplorer(
  params: Record<string, string | number>,
): Promise<ScopeExplorerResponse> {
  const res = await apiClient.get<ScopeExplorerResponse>('/v1/market-dashboard/scopes', { params })
  return res.data
}
