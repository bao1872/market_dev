// Strategy React Query hooks owner
//
// [S3-C] 由 useApi.ts 迁出。useApi.ts 以兼容 barrel 重新导出（caller import 零改动）。
//
// 边界：非 admin 的 strategy 查询/监控 hooks。
// 明确留在 useApi.ts（admin）：useAdminStrategyRuns / useTriggerStrategyRun。
//
// 依赖方向：api/strategy + marketRuntime ← useStrategyApi ← useApi(barrel)。
// query key / staleTime / refetchInterval 语义与迁移前逐字等价。

import { useQuery } from '@tanstack/react-query'
import * as strategyApi from '../api/strategy'
import type { StrategyEventQueryParams, StrategyResultQueryParams } from '../api/strategy'
import { isInTradingHours } from './marketRuntime'

// [S3-C] 窄常量（迁移前复用 useApi.ts 的同名常量；此处独立定义，避免跨文件常量耦合）。
const STALE_STRATEGIES = 5 * 60 * 1000 // 策略目录 5 分钟
const STALE_REALTIME = 30 * 1000 // 实时数据 30 秒

// ============================================================
// ===== Strategies hooks =====
// ============================================================

/** 获取策略列表（5 分钟缓存） */
export function useStrategies(kind?: string) {
  return useQuery({
    queryKey: ['strategies', kind],
    queryFn: () => strategyApi.getStrategies(kind),
    staleTime: STALE_STRATEGIES,
  })
}

/** 获取策略详情（5 分钟缓存） */
export function useStrategy(strategyKey: string | undefined) {
  return useQuery({
    queryKey: ['strategies', strategyKey],
    queryFn: () => strategyApi.getStrategy(strategyKey!),
    enabled: !!strategyKey,
    staleTime: STALE_STRATEGIES,
  })
}

/** 获取策略的所有版本（5 分钟缓存） */
export function useStrategyVersions(strategyKey: string | undefined) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'versions'],
    queryFn: () => strategyApi.getStrategyVersions(strategyKey!),
    enabled: !!strategyKey,
    staleTime: STALE_STRATEGIES,
  })
}

/** 获取策略版本的 schema（5 分钟缓存） */
export function useStrategyVersionSchema(strategyKey: string | undefined, version: string | undefined) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'versions', version, 'schema'],
    queryFn: () => strategyApi.getStrategyVersionSchema(strategyKey!, version!),
    enabled: !!strategyKey && !!version,
    staleTime: STALE_STRATEGIES,
  })
}

// ============================================================
// ===== Strategy Runs hooks =====
// ============================================================

/** 查询策略运行历史 */
export function useStrategyRuns(
  strategyKey: string | undefined,
  params?: { status?: string; limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'runs', params],
    queryFn: () => strategyApi.getStrategyRuns(strategyKey!, params),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

/** 查询已发布的运行批次（普通用户可访问） */
export function usePublishedRuns(
  strategyKey: string | undefined,
  params?: { limit?: number; offset?: number },
) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'published-runs', params],
    queryFn: () => strategyApi.getPublishedRuns(strategyKey!, params),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

/** 查询运行结果（分页+筛选+排序） */
export function useStrategyRunResults(runId: string | undefined, params?: StrategyResultQueryParams) {
  return useQuery({
    queryKey: ['strategy-runs', runId, 'results', params],
    queryFn: () => strategyApi.getStrategyRunResults(runId!, params),
    enabled: !!runId,
    staleTime: STALE_REALTIME,
  })
}

// ============================================================
// ===== Monitor States hooks =====
// ============================================================

/** 查询某股票的所有监控策略状态 */
export function useInstrumentMonitorStates(instrumentId: string | undefined) {
  return useQuery({
    queryKey: ['instruments', instrumentId, 'monitor-states'],
    queryFn: () => strategyApi.getInstrumentMonitorStates(instrumentId!),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询某策略的所有股票状态（支持 version 过滤，交易时段 30s 自动刷新） */
export function useStrategyMonitorStates(strategyKey: string | undefined, version?: string) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'monitor-states', version],
    queryFn: () => strategyApi.getStrategyMonitorStates(strategyKey!, version),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
    refetchInterval: () => isInTradingHours() ? 30000 : false,
  })
}

// ============================================================
// ===== Strategy Events hooks =====
// ============================================================

/** 查询某股票的策略事件 */
export function useInstrumentEvents(instrumentId: string | undefined, params?: StrategyEventQueryParams) {
  return useQuery({
    queryKey: ['instruments', instrumentId, 'events', params],
    queryFn: ({ signal }) => strategyApi.getInstrumentEvents(instrumentId!, params, { signal }),
    enabled: !!instrumentId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询某策略的事件 */
export function useStrategyEvents(
  strategyKey: string | undefined,
  params?: { version?: string } & StrategyEventQueryParams,
) {
  return useQuery({
    queryKey: ['strategies', strategyKey, 'events', params],
    queryFn: () => strategyApi.getStrategyEvents(strategyKey!, params),
    enabled: !!strategyKey,
    staleTime: STALE_REALTIME,
  })
}

/** 查询事件详情（含 snapshot 快照） */
export function useStrategyEventDetail(eventId: string | undefined) {
  return useQuery({
    queryKey: ['strategy-events', eventId],
    queryFn: () => strategyApi.getStrategyEventDetail(eventId!),
    enabled: !!eventId,
    staleTime: STALE_REALTIME,
  })
}
