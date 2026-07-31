// [AuctionApi] - 描述: 竞价分析模块 React Query hooks（PRD §3.2 用户侧只读 GET 接口）
// 基于 axios apiClient（baseURL=/api，Vite 代理去掉 /api 前缀）
// 后端 router prefix=/api/v1/auction
//
// 规则：
// - 用户侧只读 DB，不触发计算（POST /admin/auction/scan 与 /admin/auction/anchors 由管理员后台触发）
// - 默认查询当日（上海业务日）数据，trade_date 可显式指定
// - 接口必须返回 trade_date、algorithm_version、publication_id、source run IDs、coverage 和 reason_codes
// - 前端不得重算业务结论
// - 404/422/500 由调用方通过 extractAuctionError 解析明确原因与 request_id
import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/api/client'
import type {
  AnchorStatusResponse,
  AuctionBoardPageData,
  AuctionInstrumentPageData,
  AuctionMarketPageData,
} from './types'

// ============================================================
// 错误解析（对齐 review/api.ts：404/422/500 显示明确原因及 request_id）
// ============================================================

export interface AuctionApiError {
  status: number | null
  detail: string
  requestId: string | null
  message: string
}

/**
 * 从 axios 错误中提取可展示的错误信息。
 * - 404：资源不存在或当日竞价未发布
 * - 422：参数校验失败，附带后端 detail
 * - 500：服务器错误，附带 x-request-id
 */
export function extractAuctionError(err: unknown): AuctionApiError {
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
    message = detail || '资源不存在或竞价尚未发布'
  } else if (status === 422) {
    message = `参数校验失败${detail ? `：${detail}` : ''}`
  } else if (status === 500) {
    message = `服务器错误${requestId ? `（request_id=${requestId}）` : ''}`
  } else if (status === 403) {
    message = '权限不足，当前账号无访问权限'
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
// API 调用函数
// ============================================================

/** GET /api/v1/auction — 市场级页面数据 */
export async function getAuctionMarket(
  tradeDate?: string,
  options: { top_n?: number; top_events?: number } = {},
): Promise<AuctionMarketPageData> {
  const { data } = await apiClient.get<AuctionMarketPageData>('/api/v1/auction', {
    params: {
      trade_date: tradeDate,
      top_n: options.top_n,
      top_events: options.top_events,
    },
  })
  return data
}

/** GET /api/v1/auction/board/{board_id} — 板块级页面数据 */
export async function getAuctionBoard(
  boardId: string,
  tradeDate?: string,
  options: { top_n?: number } = {},
): Promise<AuctionBoardPageData> {
  const { data } = await apiClient.get<AuctionBoardPageData>(
    `/api/v1/auction/board/${boardId}`,
    {
      params: {
        trade_date: tradeDate,
        top_n: options.top_n,
      },
    },
  )
  return data
}

/** GET /api/v1/auction/stock/{symbol} — 个股级页面数据 */
export async function getAuctionInstrument(
  symbol: string,
  tradeDate?: string,
): Promise<AuctionInstrumentPageData> {
  const { data } = await apiClient.get<AuctionInstrumentPageData>(
    `/api/v1/auction/stock/${symbol}`,
    {
      params: {
        trade_date: tradeDate,
      },
    },
  )
  return data
}

/** GET /api/v1/auction/anchors/{trade_date} — 锚点快照与发布状态 */
export async function getAuctionAnchors(
  tradeDate: string,
): Promise<AnchorStatusResponse> {
  const { data } = await apiClient.get<AnchorStatusResponse>(
    `/api/v1/auction/anchors/${tradeDate}`,
  )
  return data
}

// ============================================================
// React Query Key 工厂
// ============================================================

/** 竞价模块统一 key 前缀 */
export const auctionKeys = {
  all: ['auction'] as const,
  market: (tradeDate?: string, filters: { top_n?: number; top_events?: number } = {}) =>
    [...auctionKeys.all, 'market', tradeDate ?? 'latest', filters] as const,
  board: (boardId: string, tradeDate?: string, filters: { top_n?: number } = {}) =>
    [...auctionKeys.all, 'board', boardId, tradeDate ?? 'latest', filters] as const,
  instrument: (symbol: string, tradeDate?: string) =>
    [...auctionKeys.all, 'instrument', symbol, tradeDate ?? 'latest'] as const,
  anchors: (tradeDate: string) =>
    [...auctionKeys.all, 'anchors', tradeDate] as const,
} as const

// ============================================================
// React Query Hooks
// ============================================================

/**
 * 市场级页面数据 hook
 * GET /api/v1/auction
 * - trade_date 省略时后端默认取上海当前业务日
 * - 默认 top_n=10、top_events=20（与后端 DEFAULT_TOP_BOARDS/DEFAULT_TOP_EVENTS 对齐）
 */
export function useAuctionMarket(
  tradeDate?: string,
  options: { top_n?: number; top_events?: number; enabled?: boolean } = {},
) {
  const { enabled = true, ...filters } = options
  return useQuery({
    queryKey: auctionKeys.market(tradeDate, filters),
    queryFn: () => getAuctionMarket(tradeDate, filters),
    enabled,
    // 竞价数据盘后发布，1 分钟刷新足够；运行时 staleTime 略短以兼顾 admin 触发后能看到新数据
    staleTime: 30 * 1000,
  })
}

/**
 * 板块级页面数据 hook
 * GET /api/v1/auction/board/{board_id}
 */
export function useAuctionBoard(
  boardId: string | null | undefined,
  tradeDate?: string,
  options: { top_n?: number; enabled?: boolean } = {},
) {
  const { enabled = true, ...filters } = options
  return useQuery({
    queryKey: boardId ? auctionKeys.board(boardId, tradeDate, filters) : ['auction', 'board', 'disabled'],
    queryFn: () => getAuctionBoard(boardId as string, tradeDate, filters),
    enabled: enabled && !!boardId,
    staleTime: 30 * 1000,
  })
}

/**
 * 个股级页面数据 hook
 * GET /api/v1/auction/stock/{symbol}
 */
export function useAuctionInstrument(
  symbol: string | null | undefined,
  tradeDate?: string,
  options: { enabled?: boolean } = {},
) {
  const { enabled = true } = options
  return useQuery({
    queryKey: symbol ? auctionKeys.instrument(symbol, tradeDate) : ['auction', 'instrument', 'disabled'],
    queryFn: () => getAuctionInstrument(symbol as string, tradeDate),
    enabled: enabled && !!symbol,
    staleTime: 30 * 1000,
  })
}

/**
 * 锚点快照与发布状态 hook
 * GET /api/v1/auction/anchors/{trade_date}
 */
export function useAuctionAnchors(
  tradeDate: string | null | undefined,
  options: { enabled?: boolean } = {},
) {
  const { enabled = true } = options
  return useQuery({
    queryKey: tradeDate ? auctionKeys.anchors(tradeDate) : ['auction', 'anchors', 'disabled'],
    queryFn: () => getAuctionAnchors(tradeDate as string),
    enabled: enabled && !!tradeDate,
    staleTime: 60 * 1000,
  })
}
