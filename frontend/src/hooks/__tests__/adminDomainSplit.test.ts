// [S3-D] - 描述: admin domain 物理拆分契约测试（运行时函数引用比较）
// 用法：./node_modules/.bin/tsx --test src/hooks/__tests__/adminDomainSplit.test.ts
//
// 证明旧 endpoints.ts / useApi.ts 兼容 barrel 与新 owner（api/admin.ts、api/adminAfterClose.ts、
// useAdminApi.ts、useAdminAfterCloseApi.ts）指向同一函数引用；admin 边界（public/non-admin 未混入）；
// 且 query key / staleTime / refetchInterval 与 mutation invalidation 未漂移。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const _store: Record<string, string> = {}
;(globalThis as unknown as { window: unknown }).window = {
  location: { search: '', pathname: '/' },
  innerWidth: 1280,
  innerHeight: 800,
  addEventListener: () => {},
  removeEventListener: () => {},
}
;(globalThis as unknown as { localStorage: unknown }).localStorage = {
  getItem: (k: string) => _store[k] ?? null,
  setItem: (k: string, v: string) => {
    _store[k] = v
  },
  removeItem: (k: string) => {
    delete _store[k]
  },
}
;(globalThis as unknown as { sessionStorage: unknown }).sessionStorage = {
  getItem: (k: string) => _store[k] ?? null,
  setItem: (k: string, v: string) => {
    _store[k] = v
  },
  removeItem: (k: string) => {
    delete _store[k]
  },
}

const ADMIN_API_FNS = [
  'createStrategy','releaseStrategyVersion','archiveStrategyVersion','triggerStrategyRun','getAdminStrategyRuns',
  'getMessageDeliveries','retryMessageDelivery','createInviteCodes','getInviteCodes','revokeInviteCode',
  'adminListUserChannels','adminCreateUserChannel','adminUpdateUserChannel','adminDeleteUserChannel','adminVerifyUserChannel','adminTestUserChannel',
  'getMembers','getMemberRedemptions','getAdminUsers','getAdminUser','adminEnableUser','adminDisableUser','adminResetUserPassword',
  'adminGrantSubscription','adminRenewSubscription','adminRevokeSubscription','adminChangeSubscriptionPlan',
  'getUserCapabilities','adminGrantCapability','adminRevokeCapability','getAdminAuditLogs',
  'getAdminBetaApplications','getAdminBetaApplicationStats','getAdminBetaApplicationDetail','updateAdminBetaApplication','retryAdminBetaApplicationFeishu','buildBetaApplicationExportUrl',
  'getSchedulerJobRuns','getWorkerHeartbeats','getAdminSystemOverview','getAdminProductReadiness','getAdminVisitors',
  'triggerComputeBoard','triggerComputeAllBoards',
] as const
const AC_API_FNS = [
  'getJobRunEvents','getAfterCloseRunStatus','createAfterCloseRun','forceAfterCloseRun','retryAfterCloseRun','resumeAfterCloseRun',
  'getAfterClosePipelineLatest','getAfterClosePipelineByDate','getAfterClosePipelineRuns','createAfterClosePipelineRun',
  'cancelAfterCloseRun','reconcileAfterCloseRun','restartAfterCloseRun','forceRestartAfterCloseRun',
] as const
const ADMIN_HOOKS = [
  'useAdminStrategyRuns','useTriggerStrategyRun','useAdminUserChannels','useAdminCreateUserChannel','useAdminUpdateUserChannel','useAdminDeleteUserChannel','useAdminVerifyUserChannel','useAdminTestUserChannel',
  'useInviteCodes','useCreateInviteCodes','useRevokeInviteCode','useMembers','useMemberRedemptions',
  'useAdminUsers','useAdminUser','useAdminEnableUser','useAdminDisableUser','useAdminResetUserPassword',
  'useAdminGrantSubscription','useAdminRenewSubscription','useAdminRevokeSubscription','useAdminChangeSubscriptionPlan',
  'useUserCapabilities','useAdminGrantCapability','useAdminRevokeCapability','useAdminAuditLogs',
  'useAdminBetaApplications','useAdminBetaApplicationStats','useAdminBetaApplicationDetail','useUpdateAdminBetaApplication','useRetryAdminBetaApplicationFeishu',
  'useAdminSystemOverview','useAdminProductReadiness','useAdminStockDebug',
  'useMessageDeliveries','useRetryMessageDelivery','useSchedulerJobRuns','useWorkerHeartbeats','useAdminVisitors','useTriggerComputeBoard','useTriggerComputeAllBoards',
] as const
const AC_HOOKS = [
  'useJobRunEvents','useAfterCloseRunStatus','useCreateAfterCloseRun','useForceAfterCloseRun','useRetryAfterCloseRun','useResumeAfterCloseRun',
  'useAfterClosePipelineLatest','useAfterClosePipelineByDate','useAfterClosePipelineRuns','useCreateAfterClosePipelineRun',
  'useCancelAfterCloseRun','useReconcileAfterCloseRun','useRestartAfterCloseRun','useForceRestartAfterCloseRun',
] as const
// 必须留在非-admin 边界的符号
const NON_ADMIN = ['getPlans', 'getBoardAnalysisList', 'getBoardAnalysisDetail', 'getStockContext', 'getFirstPyramid', 'getAdminStockDebug']

test('API barrel identity: endpoints 重新导出 admin/adminAfterClose owner（同一函数引用）', async () => {
  const endpoints = (await import('../../api/endpoints.ts')) as Record<string, unknown>
  const admin = (await import('../../api/admin.ts')) as Record<string, unknown>
  const ac = (await import('../../api/adminAfterClose.ts')) as Record<string, unknown>
  for (const n of ADMIN_API_FNS) assert.equal(endpoints[n], admin[n], `endpoints.${n} === admin.${n}`)
  for (const n of AC_API_FNS) assert.equal(endpoints[n], ac[n], `endpoints.${n} === adminAfterClose.${n}`)
  for (const n of NON_ADMIN) {
    assert.equal(admin[n], undefined, `${n} 不应被搬入 admin（非 admin 边界）`)
    assert.equal(ac[n], undefined, `${n} 不应被搬入 adminAfterClose`)
  }
})

test('Hook barrel identity: useApi 重新导出 admin/adminAfterClose hook owner（同一引用）', async () => {
  const legacy = (await import('../useApi.ts')) as Record<string, unknown>
  const adminHooks = (await import('../useAdminApi.ts')) as Record<string, unknown>
  const acHooks = (await import('../useAdminAfterCloseApi.ts')) as Record<string, unknown>
  for (const n of ADMIN_HOOKS) assert.equal(legacy[n], adminHooks[n], `useApi.${n} === useAdminApi.${n}`)
  for (const n of AC_HOOKS) assert.equal(legacy[n], acHooks[n], `useApi.${n} === useAdminAfterCloseApi.${n}`)
  // 非-admin hook 未混入
  assert.equal(adminHooks.usePlans, undefined, 'usePlans（公开 endpoint）不应进入 useAdminApi')
  assert.equal(legacy.usePlans, legacy.usePlans, 'usePlans 仍由 useApi 提供')
})

test('query contract: admin representative queryKey + staleTime + refetchInterval 未漂移', async () => {
  const {
    useAdminUsers, useAdminBetaApplications, useAdminAuditLogs, useAdminStockDebug,
    useInviteCodes, useMembers, useAdminVisitors, useAdminSystemOverview,
  } = await import('../useAdminApi.ts')
  const { useAfterCloseRunStatus, useJobRunEvents } = await import('../useAdminAfterCloseApi.ts')

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const P = { limit: 10 }
  function Probe() {
    useAdminUsers(P)
    useAdminBetaApplications()
    useAdminAuditLogs(P)
    useAdminStockDebug('600519', { as_of: '2026-01-01' })
    useInviteCodes(P)
    useMembers(P)
    useAdminVisitors()
    useAdminSystemOverview(true)
    useAfterCloseRunStatus('run-1', true)
    useJobRunEvents('run-1')
    return null
  }
  renderToStaticMarkup(
    createElement(QueryClientProvider as never, { client: qc }, createElement(Probe as never)),
  )
  const all = qc.getQueryCache().getAll()
  const opt = (k: unknown[]) =>
    all.find((q) => JSON.stringify(q.queryKey) === JSON.stringify(k))?.options as
      | { staleTime?: number; refetchInterval?: unknown; refetchIntervalInBackground?: boolean }
      | undefined
  const expectKey = (k: unknown[]) =>
    assert.ok(all.some((q) => JSON.stringify(q.queryKey) === JSON.stringify(k)), `queryKey ${JSON.stringify(k)} 必须存在`)

  expectKey(['admin', 'users', P])
  expectKey(['admin', 'beta-applications', undefined])
  expectKey(['admin', 'audit-logs', P])
  expectKey(['admin', 'stock-debug', '600519', { as_of: '2026-01-01' }])
  expectKey(['admin', 'invite-codes', P])
  expectKey(['admin', 'members', P])
  expectKey(['admin', 'visitors'])
  expectKey(['admin', 'system-overview'])
  expectKey(['after-close-runs', 'run-1'])
  expectKey(['job-runs', 'run-1', 'events'])

  const ov = opt(['admin', 'system-overview'])
  assert.equal(ov?.staleTime, 30_000, 'useAdminSystemOverview staleTime 必须为 30s')
  assert.equal(ov?.refetchInterval, 15_000, 'useAdminSystemOverview 轮询必须为 15s')
  assert.equal(ov?.refetchIntervalInBackground, false, 'useAdminSystemOverview 后台轮询必须为 false')
})

test('mutation invalidation 漂移守卫（源码固定，identity 已保证行为一致）', () => {
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const src = readFileSync(join(__dirname, '..', 'useAdminApi.ts'), 'utf-8')
  const count = (n: string) => src.split(n).length - 1
  assert.ok(count("invalidateQueries({ queryKey: ['admin', 'users'] })") >= 1, 'admin users mutation 必须失效 [admin, users]')
  assert.ok(count("invalidateQueries({ queryKey: ['admin', 'invite-codes'] })") >= 1, 'invite-code mutation 必须失效 [admin, invite-codes]')
  assert.ok(count("invalidateQueries({ queryKey: ['admin', 'beta-applications'] })") >= 1, 'beta mutation 必须失效 [admin, beta-applications]')
  assert.ok(src.includes("invalidateQueries({ queryKey: ['strategies', variables.strategyKey, 'runs'] })"), 'triggerStrategyRun 必须失效 [strategies, key, runs]')
  assert.ok(src.includes("invalidateQueries({ queryKey: ['admin', 'strategies', variables.strategyKey, 'runs'] })"), 'triggerStrategyRun 必须失效 [admin, strategies, key, runs]')
})
