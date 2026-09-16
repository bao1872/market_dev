// Watchlist 领域 API owner
//
// [S3-B] 由 endpoints.ts 迁出。endpoints.ts 仅保留兼容 barrel 重新导出，
// 既有 caller 的 import path 与运行时行为完全不变。
//
// 依赖方向：client ← market ← watchlist ← endpoints(barrel) / useWatchlistApi。
// WatchlistMonitorStatusItem 复用 market.MarketSession（单一 owner，不复制）。

import { apiClient } from './client'
import type { MarketSession } from './market'

// ============================================================
// Watchlist 领域类型
// ============================================================

/** 自选股项 */
export interface WatchlistItem {
  id: string
  user_id: string
  instrument_id: string
  source: string
  active: boolean
  created_at: string
  removed_at: string | null
}

/** 自选股列表响应 */
export interface WatchlistListResponse {
  items: WatchlistItem[]
  total: number
}

/** 自选股 metadata-only summary 项（与 backend WatchlistSummaryItem 对齐）
 *  P0 后续数据边界：仅 instrument 元数据 + 加入时间，禁止行情/策略指标 */
export interface WatchlistSummaryItem {
  watchlist_item_id: string
  instrument_id: string
  symbol: string
  name: string
  market: string
  source: string
  created_at: string
}

/** 自选股 metadata-only summary 列表响应（GET /v1/watchlist 专用） */
export interface WatchlistSummaryResponse {
  items: WatchlistSummaryItem[]
  total: number
}

/** 自选股+监控状态聚合项（与 backend/app/schemas/watchlist.py WatchlistMonitorStatusItem 对齐） */
export interface WatchlistMonitorStatusItem {
  watchlist_item_id: string
  instrument_id: string
  symbol: string
  name: string
  market: string
  watchlist_created_at: string
  monitor_status: MarketSession | 'WAITING_FIRST_RUN' | 'SUCCEEDED' | 'FAILED' | 'STALE'
  market_session: MarketSession
  calculation_status: 'SUCCEEDED' | 'FAILED' | 'STALE' | 'WAITING_FIRST_RUN'
  freshness_seconds: number | null
  last_bar_time: string | null
  evaluation_status: string | null
  retry_count: number | null
  error_code: string | null
  source_bar_time: string | null
  metrics: Record<string, unknown> | null
  latest_event?: {
    event_type: string
    event_time: string
    boundary: number | null
  } | null
  updated_at: string | null
}

/** 自选股+监控状态聚合响应 */
export interface WatchlistMonitorStatusResponse {
  items: WatchlistMonitorStatusItem[]
}

/** 加入自选请求 */
export interface WatchlistAddRequest {
  instrument_id: string
  source?: string
}

// ============================================================
// ===== Watchlist 端点 =====
// ============================================================

/** 查询当前用户的自选列表（metadata-only summary，仅 active=true） */
export async function getWatchlist(): Promise<WatchlistSummaryResponse> {
  const { data } = await apiClient.get<WatchlistSummaryResponse>('/v1/watchlist')
  return data
}

/** 加入自选（instrument_id，user_id 由认证上下文注入） */
export async function addToWatchlist(payload: WatchlistAddRequest): Promise<WatchlistItem> {
  const { data } = await apiClient.post<WatchlistItem>('/v1/watchlist', payload)
  return data
}

/** 移除自选（软删除：active=false + removed_at） */
export async function removeFromWatchlist(instrumentId: string): Promise<void> {
  await apiClient.delete(`/v1/watchlist/${instrumentId}`)
}

/** 查询自选股+监控状态聚合数据 */
export async function getWatchlistMonitorStatus(): Promise<WatchlistMonitorStatusResponse> {
  const { data } = await apiClient.get<WatchlistMonitorStatusResponse>('/v1/watchlist/monitor-status')
  return data
}
