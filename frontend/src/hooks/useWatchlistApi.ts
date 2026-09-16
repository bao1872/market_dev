// Watchlist React Query hooks owner
//
// [S3-B] 由 useApi.ts 迁出。useApi.ts 以兼容 barrel 重新导出（caller import 零改动）。
//
// 依赖方向：api/watchlist + marketRuntime ← useWatchlistApi ← useApi(barrel)。
// isInTradingHours 来自 marketRuntime.ts（单向依赖，避免与 useApi 形成循环依赖）。
// query key / staleTime / refetchInterval / mutation invalidation 逐字等价。

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as watchlistApi from '../api/watchlist'
import type { WatchlistAddRequest } from '../api/watchlist'
import { isInTradingHours } from './marketRuntime'

// [S3-B] 窄常量（迁移前复用 useApi.ts 的同名常量；此处独立定义，避免跨文件常量耦合）。
const STALE_WATCHLIST = 60 * 1000 // 自选股 1 分钟
const STALE_REALTIME = 30 * 1000 // 实时数据 30 秒

// ============================================================
// ===== Watchlist hooks =====
// ============================================================

/** 查询当前用户的自选列表（1 分钟缓存） */
export function useWatchlist(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ['watchlist'],
    queryFn: watchlistApi.getWatchlist,
    staleTime: STALE_WATCHLIST,
    enabled: options?.enabled ?? true,
  })
}

/** 查询自选股+监控状态聚合数据（交易时段 1s 自动刷新，[盘中监控1秒]） */
export function useWatchlistMonitorStatus(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ['watchlist', 'monitor-status'],
    queryFn: watchlistApi.getWatchlistMonitorStatus,
    staleTime: STALE_REALTIME,
    refetchInterval: () => isInTradingHours() ? 1000 : false,
    enabled: options?.enabled ?? true,
  })
}

/** 加入自选变更（自动失效 watchlist + monitor-status 缓存） */
export function useAddToWatchlist() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: WatchlistAddRequest) => watchlistApi.addToWatchlist(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      queryClient.invalidateQueries({ queryKey: ['watchlist', 'monitor-status'] })
      queryClient.invalidateQueries({ queryKey: ['market-stocks'] })
      // CHANGE-20260713-005: watchlist 变化后，universe=watchlist 的 strategy run results 也需失效，
      // 否则 /market?scope=watchlist 下加入/移除自选后行不会立即出现/消失
      queryClient.invalidateQueries({ queryKey: ['strategy-runs'] })
    },
  })
}

/** 移除自选变更（自动失效 watchlist + monitor-status + strategy-runs 缓存） */
export function useRemoveFromWatchlist() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (instrumentId: string) => watchlistApi.removeFromWatchlist(instrumentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      queryClient.invalidateQueries({ queryKey: ['watchlist', 'monitor-status'] })
      queryClient.invalidateQueries({ queryKey: ['market-stocks'] })
      // CHANGE-20260713-005: watchlist 变化后，universe=watchlist 的 strategy run results 也需失效，
      // 否则 /market?scope=watchlist 下移除自选后行不会立即消失
      queryClient.invalidateQueries({ queryKey: ['strategy-runs'] })
    },
  })
}
