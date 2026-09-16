// Admin AfterClose / JobRuns React Query hooks owner
//
// [S3-D] 由 useApi.ts 迁出（对齐 S5 AfterClose 拆核）。useApi.ts 仅兼容 barrel。

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import * as adminApi from '../api/adminAfterClose'
import type { AfterClosePipelineRunRequest } from '../api/adminAfterClose'

const STALE_REALTIME = 30 * 1000 // 实时数据 30 秒

// ===== AfterClose & JobRunEvents hooks =====
// ============================================================

/** 查询任务执行事件时间线（抽屉打开时按需加载） */
export function useJobRunEvents(runId: string | null | undefined) {
  return useQuery({
    queryKey: ['job-runs', runId, 'events'],
    queryFn: () => adminApi.getJobRunEvents(runId!),
    enabled: !!runId,
    staleTime: STALE_REALTIME,
  })
}

/** 查询指定交易日的产品就绪状态 + 治理报告（admin，Commit G）。
 * 15 秒轮询紧跟盘后编排进度；页面不可见暂停。 */

export function useAfterCloseRunStatus(runId: string | null | undefined, enabled: boolean = true) {
  return useQuery({
    queryKey: ['after-close-runs', runId],
    queryFn: () => adminApi.getAfterCloseRunStatus(runId!),
    enabled: !!runId && enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: enabled ? 10_000 : false,
    refetchIntervalInBackground: false,
  })
}

/** 创建盘后编排变更 */
export function useCreateAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (tradeDate: string) => adminApi.createAfterCloseRun(tradeDate),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

/** 强制重新执行盘后编排变更。
 * 支持可选 restartFrom="daily_ready"：从 DSA 阶段重算（跳过日线刷新，需覆盖率≥90%）。 */
export function useForceAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (args: { runId: string; restartFrom?: 'daily_ready' }) =>
      adminApi.forceAfterCloseRun(args.runId, args.restartFrom),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

/** 重试盘后编排变更 */
export function useRetryAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => adminApi.retryAfterCloseRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

/** [Phase6] 从失败步骤继续变更（保留断点检查点，幂等）。
 * 成功后失效 after-close-runs / pipeline latest / pipeline by-date / pipeline runs /
 * system-overview 缓存，确保 UI 立即反映 queued 状态。 */
export function useResumeAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => adminApi.resumeAfterCloseRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
      queryClient.invalidateQueries({ queryKey: ['after-close-pipeline'] })
      queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
    },
  })
}

// ============================================================
// ===== AfterClose Pipeline 聚合状态 hooks（/admin/after-close/pipeline/*）=====
// ============================================================
//
// 轮询策略（遵循用户规范）：
// - running 状态 10 秒轮询
// - 非 running 状态 60 秒轮询
// - 页面不可见暂停轮询（refetchIntervalInBackground=false）
// - queryKey 与 useAdminSystemOverview / useAfterCloseRunStatus 隔离，避免缓存串扰

// [AfterClosePipeline] - 轮询间隔常量 + helper（从 adminAfterClosePipelineHelpers 导入并重导出）
// 定义在 helpers 文件中以便 node --experimental-strip-types 直接导入测试
import {
  PIPELINE_POLL_RUNNING,
  PIPELINE_POLL_IDLE,
  getPipelinePollInterval,
} from '@/pages/adminAfterClosePipelineHelpers'
export { PIPELINE_POLL_RUNNING, PIPELINE_POLL_IDLE, getPipelinePollInterval }

/**
 * 查询最近交易日的盘后流水线聚合状态（admin）。
 * overall_status==='running' 时 10s 轮询，其余 60s 轮询，页面不可见暂停。
 * @param enabled 是否启用查询（默认 true，可用于页面卸载或权限不足时停止）
 */
export function useAfterClosePipelineLatest(enabled: boolean = true) {
  return useQuery({
    queryKey: ['after-close-pipeline', 'latest'],
    queryFn: adminApi.getAfterClosePipelineLatest,
    enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: (query) => getPipelinePollInterval(query.state.data?.overall_status),
    refetchIntervalInBackground: false,
  })
}

/**
 * 查询指定交易日的盘后流水线聚合状态（admin）。
 * overall_status==='running' 时 10s 轮询，其余 60s 轮询，页面不可见暂停。
 * @param tradeDate 交易日（YYYY-MM-DD），undefined/null 时不启用查询
 * @param enabled 是否启用查询（默认 true）
 */
export function useAfterClosePipelineByDate(
  tradeDate: string | null | undefined,
  enabled: boolean = true,
) {
  return useQuery({
    queryKey: ['after-close-pipeline', 'by-date', tradeDate],
    queryFn: () => adminApi.getAfterClosePipelineByDate(tradeDate!),
    enabled: !!tradeDate && enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: (query) => getPipelinePollInterval(query.state.data?.overall_status),
    refetchIntervalInBackground: false,
  })
}

/**
 * 查询最近 N 次运行列表（after_close_orchestrator + snapshot_run 混合）。
 * 60s 轮询，页面不可见暂停（列表非实时关键数据，统一 60s）。
 * @param limit 最多返回条数（默认 20，后端上限 100）
 * @param enabled 是否启用查询
 */
export function useAfterClosePipelineRuns(
  limit: number = 20,
  enabled: boolean = true,
) {
  return useQuery({
    queryKey: ['after-close-pipeline', 'runs', limit],
    queryFn: () => adminApi.getAfterClosePipelineRuns(limit),
    enabled,
    staleTime: STALE_REALTIME,
    refetchInterval: PIPELINE_POLL_IDLE,
    refetchIntervalInBackground: false,
  })
}

/**
 * 管理员触发指定交易日的 after_close 编排任务（admin，幂等）。
 * 同 trade_date 已有 queued/running/succeeded 时返回 existing，不重复创建。
 * 成功后失效 pipeline latest/by-date/runs 与 system-overview 缓存。
 */
function invalidateAfterCloseAdminQueries(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ['after-close-runs'] })
  queryClient.invalidateQueries({ queryKey: ['after-close-pipeline'] })
  // [FIX] 任务管理页（AdminJobsPage）实际使用 ['admin', 'scheduler-job-runs', params]。
  // 原写法少了 'admin' 前缀，导致 after-close 变更成功后任务列表**永不刷新**，
  // 页面继续显示旧的 running/queued，看起来像"取消没生效"。
  // React Query 前缀失效会覆盖所有 params 版本，故这里不需要带 params。
  queryClient.invalidateQueries({ queryKey: ['admin', 'scheduler-job-runs'] })
  queryClient.invalidateQueries({ queryKey: ['admin', 'system-overview'] })
}

export function useCreateAfterClosePipelineRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: AfterClosePipelineRunRequest) =>
      adminApi.createAfterClosePipelineRun(payload),
    onSuccess: () => invalidateAfterCloseAdminQueries(queryClient),
  })
}

export function useCancelAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, reason }: { runId: string; reason?: string }) =>
      adminApi.cancelAfterCloseRun(runId, reason),
    onSuccess: () => invalidateAfterCloseAdminQueries(queryClient),
  })
}

export function useReconcileAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, reason }: { runId: string; reason?: string }) =>
      adminApi.reconcileAfterCloseRun(runId, reason),
    onSuccess: () => invalidateAfterCloseAdminQueries(queryClient),
  })
}

export function useRestartAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (runId: string) => adminApi.restartAfterCloseRun(runId),
    onSuccess: () => invalidateAfterCloseAdminQueries(queryClient),
  })
}

export function useForceRestartAfterCloseRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { runId: string; restartFrom?: 'daily_ready' }) =>
      adminApi.forceRestartAfterCloseRun(input.runId, input.restartFrom),
    onSuccess: () => invalidateAfterCloseAdminQueries(queryClient),
  })
}


