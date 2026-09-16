// Stock Data 领域 hooks owner（[S3-E] 由 useApi.ts 迁出；useApi.ts 仅兼容 barrel）。

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as api from '../api/stockData'
import { isInTradingHours } from './marketRuntime'
import type { BarQueryParams, CalendarQueryParams, ChartSnapshotQueryParams, IndicatorQueryParams, InstrumentQueryParams, StockMemoUpsertRequest, StructuralFactorQueryParams, TemporalFeaturesQueryParams } from '../api/stockData'

const STALE_STRATEGIES = 5 * 60 * 1000
const STALE_WATCHLIST = 60 * 1000
const STALE_PLANS = 60 * 1000
const STALE_REALTIME = 30 * 1000
const STALE_CALENDAR = 30 * 60 * 1000

export function useInstruments(
  params?: InstrumentQueryParams,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['instruments', params],
    queryFn: () => api.getInstruments(params),
    staleTime: STALE_PLANS,
    enabled: options?.enabled ?? true,
  })
}

/** 按 ID 列表批量查询股票（最多 1000 个） */
export function useBatchInstruments(ids: string[] | undefined) {
  return useQuery({
    queryKey: ['instruments', 'batch', ids],
    queryFn: () => api.batchGetInstruments(ids!),
    enabled: !!ids && ids.length > 0,
    staleTime: STALE_PLANS,
  })
}

// ============================================================
// ===== Market hooks =====
// ============================================================
//
// [S3-B] 实现已迁至 ./useMarketApi（唯一 owner），此处以兼容 barrel 重新导出。

export {
  useMarketStocks,
  useMarketBoards,
  useMarketFilterSpecs,
  useMarketSessionReactive,
} from './useMarketApi'

/** 按 ID 查询单个股票 */
export function useInstrument(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['instruments', instrumentId],
    queryFn: () => api.getInstrumentById(instrumentId!),
    enabled: !!instrumentId,
    staleTime: STALE_STRATEGIES,
  })
}

/** 按 symbol 查询股票 */
export function useInstrumentBySymbol(symbol: string | undefined) {
  return useQuery({
    queryKey: ['instruments', 'by-symbol', symbol],
    queryFn: () => api.getInstrumentBySymbol(symbol!),
    enabled: !!symbol,
    staleTime: STALE_STRATEGIES,
  })
}

// ============================================================

export function useEventsSummary(date: string | undefined) {
  return useQuery({
    queryKey: ['me', 'events', 'summary', date],
    queryFn: () => api.getEventsSummary(date!),
    enabled: !!date,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================

export function useStockMemo(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['stock-memo', instrumentId],
    queryFn: () => api.getStockMemo(instrumentId!),
    enabled: !!instrumentId,
    staleTime: 0,
  })
}

/** 创建/更新备忘录 */
export function useUpsertStockMemo() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ instrumentId, payload }: { instrumentId: string; payload: StockMemoUpsertRequest }) =>
      api.upsertStockMemo(instrumentId, payload),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({ queryKey: ['stock-memo', variables.instrumentId] })
    },
  })
}

/** 删除备忘录 */
export function useDeleteStockMemo() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (instrumentId: string) => api.deleteStockMemo(instrumentId),
    onSuccess: (_, instrumentId) => {
      queryClient.invalidateQueries({ queryKey: ['stock-memo', instrumentId] })
    },
  })
}

// ============================================================
// ===== Bars hooks =====
// ============================================================

/** 查询指定标的的行情数据（交易时段内 30s 轮询，响应式检测交易时段） */
export function useBars(instrumentId: string | undefined, params?: BarQueryParams, options?: { refetchInterval?: number | false }) {
  return useQuery({
    queryKey: ['bars', instrumentId, params],
    queryFn: ({ signal }) => api.getBars(instrumentId!, params, { signal }),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: options?.refetchInterval ?? (() => isInTradingHours() ? 30000 : false),
    refetchIntervalInBackground: false,
  })
}

/** 查询指定标的的所有策略图表指标（交易时段内 30s 轮询，响应式检测交易时段；页面隐藏时停止轮询） */
export function useIndicators(
  instrumentId: string | undefined,
  params?: IndicatorQueryParams,
  options?: { refetchInterval?: number | false },
) {
  return useQuery({
    // [DSA 数据契约] - queryKey 新增 'v3' 版本标识：后端响应新增 source_bar_times/source_bar_hash/visual_segments，
    //   旧缓存（无版本标识）结构不兼容，强制重新拉取
    queryKey: ['indicators', 'v3', instrumentId, params],
    queryFn: ({ signal }) => api.getIndicators(instrumentId!, params, { signal }),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: options?.refetchInterval ?? (() => isInTradingHours() ? 30000 : false),
    refetchIntervalInBackground: false,
  })
}

/** 查询指定标的的实时报价（交易时段内 10s 轮询，响应式检测交易时段；页面隐藏时停止轮询） */
export function useRealtimeQuote(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['quote', instrumentId],
    queryFn: ({ signal }) => api.getQuote(instrumentId!, { signal }),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
    refetchInterval: () => isInTradingHours() ? 10000 : false,
    refetchIntervalInBackground: false,
  })
}

/**
 * [PRD V2.0 §4.2 SNAP-01 Atomic Chart Snapshot] 个股详情页原子图表快照。
 *
 * 一次 MDAS DataFrame 同时返回 bars + indicators + render_frame，
 * 替代详情页独立的 useBars + useIndicators 两次实时请求，
 * 保证前端拿到的 bars 与 indicators 基于同一份后端 DataFrame 生成。
 *
 * render_frame.matched=false 时前端不得 Ready（与 Capture 同款合同）。
 *
 * 轮询策略与 useBars/useIndicators 一致：交易时段 30s，非交易时段不轮询。
 */
export function useChartSnapshot(
  instrumentId: string | undefined,
  params?: ChartSnapshotQueryParams,
  options?: { refetchInterval?: number | false },
) {
  return useQuery({
    // queryKey 包含 'v1' 版本标识，便于未来响应结构变更时强制刷新缓存
    queryKey: ['chart-snapshot', 'v1', instrumentId, params],
    queryFn: ({ signal }) => api.getChartSnapshot(instrumentId!, params, { signal }),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: options?.refetchInterval ?? (() => isInTradingHours() ? 30000 : false),
    refetchIntervalInBackground: false,
    // [P0-8] hidden 恢复时立即刷新（React Query 默认 refetchOnWindowFocus=true）
    refetchOnWindowFocus: true,
  })
}

// [S3-B] useMarketSessionReactive 已迁至 ./useMarketApi（见 "Market hooks" 兼容 re-export）。

// ============================================================
// ===== Calendar hooks =====
// ============================================================

/** 查询交易日历（30 分钟缓存，极少变更） */
export function useCalendar(params?: CalendarQueryParams) {
  return useQuery({
    queryKey: ['calendar', params],
    queryFn: () => api.getCalendar(params),
    staleTime: STALE_CALENDAR,
  })
}

/** 查询指定日期是否为交易日 */
export function useIsTradingDay(targetDate: string | undefined) {
  return useQuery({
    queryKey: ['calendar', 'is-trading-day', targetDate],
    queryFn: () => api.isTradingDay(targetDate!),
    enabled: !!targetDate,
    staleTime: STALE_CALENDAR,
  })
}

// ============================================================

export function useStructuralFactors(
  instrumentId: string | undefined,
  params?: StructuralFactorQueryParams,
) {
  return useQuery({
    queryKey: ['structural-factors', instrumentId, params],
    queryFn: () => api.getStructuralFactors(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: () => isInTradingHours() ? 60000 : false,
  })
}

// ============================================================
// ===== Temporal Features hooks =====
// ============================================================

/**
 * 查询指定标的的时序特征 V1（daily_context + m15_response + derived_relation）。
 * 交易时段 60s 轮询，非交易时段不轮询。
 * 前端只渲染后端 DTO，严禁重新计算。
 */
export function useTemporalFeatures(
  instrumentId: string | undefined,
  params?: TemporalFeaturesQueryParams,
) {
  return useQuery({
    queryKey: ['temporal-features', instrumentId, params],
    queryFn: () => api.getTemporalFeatures(instrumentId!, params),
    enabled: !!instrumentId,
    staleTime: STALE_WATCHLIST,
    refetchInterval: () => isInTradingHours() ? 60000 : false,
  })
}

// ============================================================

/** 查询用户侧 StockContext（/v1/stocks/{symbol}/context） */
export function useStockContext(
  symbol: string | undefined,
  params?: { as_of?: string },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['stock-context', symbol, params ?? null],
    queryFn: ({ signal }) => api.getStockContext(symbol!, params, { signal }),
    enabled: !!symbol && (options?.enabled ?? true),
    staleTime: STALE_REALTIME,
  })
}

/**
 * [Phase 5B-2] 查询第一金字塔统一快照（/v1/stocks/{symbol}/first-pyramid）。
 * - 固定维度顺序：trend → structure → momentum → chip_consensus
 * - 前三维必选，chip_consensus 可选（无有效峰时为 null）
 * - [CHANGE-20260730-012] 只读已发布 stock_core 快照；无快照返回结构化 unavailable
 * - retry=1 避免无限重试；明确 loading/error/empty 状态
 */
export function useFirstPyramid(
  symbol: string | undefined,
  params?: { as_of?: string },
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ['first-pyramid', symbol, params ?? null],
    queryFn: ({ signal }) => api.getFirstPyramid(symbol!, params, { signal }),
    enabled: !!symbol && (options?.enabled ?? true),
    staleTime: STALE_REALTIME,
    retry: 1,
  })
}

