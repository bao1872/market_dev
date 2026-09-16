// [S3-C] - 描述: strategy domain 物理拆分契约测试（运行时函数引用比较）
// 用法：./node_modules/.bin/tsx --test src/hooks/__tests__/strategyDomainSplit.test.ts
//
// 目的：证明旧 endpoints.ts / useApi.ts 兼容 barrel 与新 domain owner
// （api/strategy.ts / useStrategyApi.ts）指向**同一个函数引用**（不是复制实现），
// admin strategy 端点仍留在 admin domain，且 query key / staleTime / 交易时段 refetch 未漂移。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// 被测模块会 transitively import axios / zustand persist / React Query；
// 按仓库既有 SSR 测试惯例，在动态 import 前打桩浏览器全局。
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

// 迁移到 api/strategy.ts 的非 admin 端点
const STRATEGY_API_FNS = [
  'getStrategies',
  'getStrategy',
  'getStrategyVersions',
  'getStrategyVersionSchema',
  'getStrategyRuns',
  'getPublishedRuns',
  'getStrategyRunResults',
  'getStrategyRunResultDetail',
  'getInstrumentMonitorStates',
  'getStrategyMonitorStates',
  'getInstrumentEvents',
  'getStrategyEvents',
  'getStrategyEventDetail',
] as const
// 留在 admin domain 的 strategy 端点（不得进入 strategy owner）
const ADMIN_API_FNS = [
  'createStrategy',
  'releaseStrategyVersion',
  'archiveStrategyVersion',
  'triggerStrategyRun',
  'getAdminStrategyRuns',
] as const
// 迁移到 useStrategyApi.ts 的非 admin hook
const STRATEGY_HOOKS = [
  'useStrategies',
  'useStrategy',
  'useStrategyVersions',
  'useStrategyVersionSchema',
  'useStrategyRuns',
  'usePublishedRuns',
  'useStrategyRunResults',
  'useInstrumentMonitorStates',
  'useStrategyMonitorStates',
  'useInstrumentEvents',
  'useStrategyEvents',
  'useStrategyEventDetail',
] as const
// 留在 useApi.ts 的 admin hook
const ADMIN_HOOKS = ['useAdminStrategyRuns', 'useTriggerStrategyRun'] as const

const RUNS_PARAMS = { limit: 10 }
const RESULT_PARAMS = { page: 1 }

// ===== API barrel identity =====

test('API barrel identity: endpoints 重新导出 strategy owner（同一函数引用）', async () => {
  const endpoints = (await import('../../api/endpoints.ts')) as Record<string, unknown>
  const strategy = (await import('../../api/strategy.ts')) as Record<string, unknown>
  for (const name of STRATEGY_API_FNS) {
    assert.equal(endpoints[name], strategy[name], `endpoints.${name} 必须与 strategy.${name} 是同一个函数`)
  }
  // admin strategy 边界：实现留在 endpoints.ts，不进入 strategy owner
  for (const name of ADMIN_API_FNS) {
    assert.equal(strategy[name], undefined, `${name} 应留在 admin domain（不属 strategy owner）`)
    assert.equal(typeof endpoints[name], 'function', `${name} 必须仍由 endpoints.ts 提供`)
  }
})

// ===== Hook barrel identity =====

test('Hook barrel identity: useApi 重新导出 useStrategyApi owner（同一 hook 引用）', async () => {
  const legacy = (await import('../useApi.ts')) as Record<string, unknown>
  const strategyHooks = (await import('../useStrategyApi.ts')) as Record<string, unknown>
  for (const name of STRATEGY_HOOKS) {
    assert.equal(legacy[name], strategyHooks[name], `useApi.${name} 必须与 useStrategyApi.${name} 是同一个 hook`)
  }
  // admin strategy hooks 边界
  for (const name of ADMIN_HOOKS) {
    assert.equal(strategyHooks[name], undefined, `${name} 应留在 useApi.ts（admin domain）`)
    assert.equal(typeof legacy[name], 'function', `${name} 必须仍由 useApi.ts 提供`)
  }
})

// ===== query contract（key / staleTime / refetch）不得漂移 =====

test('query contract: strategy queryKey + staleTime 精确不变', async () => {
  const {
    useStrategies,
    useStrategy,
    useStrategyVersions,
    useStrategyVersionSchema,
    useStrategyRuns,
    usePublishedRuns,
    useStrategyRunResults,
    useInstrumentMonitorStates,
    useStrategyMonitorStates,
    useInstrumentEvents,
    useStrategyEvents,
    useStrategyEventDetail,
  } = await import('../useStrategyApi.ts')

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Probe() {
    useStrategies('dsa')
    useStrategy('dsa_selector')
    useStrategyVersions('dsa_selector')
    useStrategyVersionSchema('dsa_selector', 'v1')
    useStrategyRuns('dsa_selector', RUNS_PARAMS)
    usePublishedRuns('dsa_selector', RUNS_PARAMS)
    useStrategyRunResults('run-1', RESULT_PARAMS)
    useInstrumentMonitorStates('inst-1')
    useStrategyMonitorStates('dsa_selector', 'v1')
    useInstrumentEvents('inst-1')
    useStrategyEvents('dsa_selector')
    useStrategyEventDetail('ev-1')
    return null
  }
  renderToStaticMarkup(
    createElement(QueryClientProvider as never, { client: qc }, createElement(Probe as never)),
  )

  const all = qc.getQueryCache().getAll()
  const keys = all.map((q) => JSON.stringify(q.queryKey))
  const expectKey = (k: unknown[]) =>
    assert.ok(keys.includes(JSON.stringify(k)), `queryKey ${JSON.stringify(k)} 必须存在`)

  expectKey(['strategies', 'dsa'])
  expectKey(['strategies', 'dsa_selector'])
  expectKey(['strategies', 'dsa_selector', 'versions'])
  expectKey(['strategies', 'dsa_selector', 'versions', 'v1', 'schema'])
  expectKey(['strategies', 'dsa_selector', 'runs', RUNS_PARAMS])
  expectKey(['strategies', 'dsa_selector', 'published-runs', RUNS_PARAMS])
  expectKey(['strategy-runs', 'run-1', 'results', RESULT_PARAMS])
  expectKey(['instruments', 'inst-1', 'monitor-states'])
  expectKey(['strategies', 'dsa_selector', 'monitor-states', 'v1'])
  expectKey(['instruments', 'inst-1', 'events', undefined])
  expectKey(['strategies', 'dsa_selector', 'events', undefined])
  expectKey(['strategy-events', 'ev-1'])

  const opt = (k: unknown[]) =>
    all.find((q) => JSON.stringify(q.queryKey) === JSON.stringify(k))?.options as
      | { staleTime?: number; refetchInterval?: unknown }
      | undefined
  assert.equal(opt(['strategies', 'dsa'])?.staleTime, 5 * 60 * 1000, 'useStrategies staleTime 必须为 5m')
  assert.equal(
    opt(['strategies', 'dsa_selector', 'monitor-states', 'v1'])?.staleTime,
    30_000,
    'useStrategyMonitorStates staleTime 必须为 30s',
  )
})

test('refetch contract: useStrategyMonitorStates 交易时段 30s / 非交易时段 false', async () => {
  const { useStrategyMonitorStates } = await import('../useStrategyApi.ts')
  const { setCachedMarketStatus } = await import('../marketRuntime.ts')

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Probe() {
    useStrategyMonitorStates('dsa_selector', 'v1')
    return null
  }
  renderToStaticMarkup(
    createElement(QueryClientProvider as never, { client: qc }, createElement(Probe as never)),
  )

  const q = qc
    .getQueryCache()
    .getAll()
    .find((x) => JSON.stringify(x.queryKey) === JSON.stringify(['strategies', 'dsa_selector', 'monitor-states', 'v1']))
  const ri = (q?.options as { refetchInterval?: unknown } | undefined)?.refetchInterval
  assert.equal(typeof ri, 'function', 'refetchInterval 必须为函数（依赖交易时段）')

  setCachedMarketStatus({
    is_trading_day: true,
    is_trading_hours: true,
    status_text: '交易中',
    market_session: 'MORNING_SESSION',
  })
  assert.equal((ri as () => unknown)(), 30000, '交易时段 refetchInterval 必须为 30s')

  setCachedMarketStatus({
    is_trading_day: true,
    is_trading_hours: false,
    status_text: '已收盘',
    market_session: 'MARKET_CLOSED',
  })
  assert.equal((ri as () => unknown)(), false, '非交易时段 refetchInterval 必须为 false')

  setCachedMarketStatus(null)
})
