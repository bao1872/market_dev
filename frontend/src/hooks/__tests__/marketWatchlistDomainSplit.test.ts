// [S3-B] - 描述: market/watchlist domain 物理拆分契约测试（运行时函数引用比较）
// 用法：./node_modules/.bin/tsx --test src/hooks/__tests__/marketWatchlistDomainSplit.test.ts
//
// 目的：证明旧 endpoints.ts / useApi.ts 兼容 barrel 与新 domain owner
// （market.ts / watchlist.ts / useMarketApi.ts / useWatchlistApi.ts / marketRuntime.ts）
// 指向**同一个函数引用**（不是复制实现），且 query key / staleTime 未漂移；
// mutation invalidation 集合用源码漂移守卫固定（add/remove 各 1 组）。
//
// 不是字符串 grep 冒充语义：identity 用运行时引用比较，queryKey/staleTime 用真实挂载后的 query cache。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
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

const MARKET_FNS = ['getMarketStatus', 'getMarketStocks', 'getMarketBoards', 'getMarketFilterSpecs'] as const
const WATCHLIST_FNS = ['getWatchlist', 'addToWatchlist', 'removeFromWatchlist', 'getWatchlistMonitorStatus'] as const
const MARKET_HOOKS = ['useMarketStocks', 'useMarketBoards', 'useMarketFilterSpecs', 'useMarketSessionReactive'] as const
const WATCHLIST_HOOKS = ['useWatchlist', 'useWatchlistMonitorStatus', 'useAddToWatchlist', 'useRemoveFromWatchlist'] as const

// ===== API barrel identity =====

test('API barrel identity: endpoints 重新导出 market/watchlist owner（同一函数引用）', async () => {
  const endpoints = (await import('../../api/endpoints.ts')) as Record<string, unknown>
  const market = (await import('../../api/market.ts')) as Record<string, unknown>
  const watchlist = (await import('../../api/watchlist.ts')) as Record<string, unknown>
  for (const name of MARKET_FNS) {
    assert.equal(endpoints[name], market[name], `endpoints.${name} 必须与 market.${name} 是同一个函数`)
  }
  for (const name of WATCHLIST_FNS) {
    assert.equal(endpoints[name], watchlist[name], `endpoints.${name} 必须与 watchlist.${name} 是同一个函数`)
  }
})

// ===== Hook barrel identity =====

test('Hook barrel identity: useApi 重新导出 useMarketApi/useWatchlistApi owner（同一 hook 引用）', async () => {
  const legacy = (await import('../useApi.ts')) as Record<string, unknown>
  const marketHooks = (await import('../useMarketApi.ts')) as Record<string, unknown>
  const watchlistHooks = (await import('../useWatchlistApi.ts')) as Record<string, unknown>
  for (const name of MARKET_HOOKS) {
    assert.equal(legacy[name], marketHooks[name], `useApi.${name} 必须与 useMarketApi.${name} 是同一个 hook`)
  }
  for (const name of WATCHLIST_HOOKS) {
    assert.equal(legacy[name], watchlistHooks[name], `useApi.${name} 必须与 useWatchlistApi.${name} 是同一个 hook`)
  }
})

// ===== marketRuntime identity =====

test('marketRuntime identity: useApi 重新导出 marketRuntime 时钟 utility（同一引用）', async () => {
  const legacy = (await import('../useApi.ts')) as Record<string, unknown>
  const runtime = (await import('../marketRuntime.ts')) as Record<string, unknown>
  assert.equal(legacy.isInTradingHours, runtime.isInTradingHours)
  assert.equal(legacy.setCachedMarketStatus, runtime.setCachedMarketStatus)
  assert.equal(legacy.getCachedMarketStatus, runtime.getCachedMarketStatus)
})

// ===== query contract（key / staleTime）不得漂移 =====

test('query contract: market/watchlist queryKey + staleTime 精确不变', async () => {
  const { useMarketStocks, useMarketBoards, useMarketFilterSpecs, useMarketSessionReactive } =
    await import('../useMarketApi.ts')
  const { useWatchlist, useWatchlistMonitorStatus } = await import('../useWatchlistApi.ts')

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Probe() {
    useMarketStocks({ scope: 'market' })
    useMarketBoards()
    useMarketFilterSpecs()
    useWatchlist()
    useWatchlistMonitorStatus()
    useMarketSessionReactive()
    return null
  }
  renderToStaticMarkup(
    createElement(QueryClientProvider as never, { client: qc }, createElement(Probe as never)),
  )

  const all = qc.getQueryCache().getAll()
  const keys = all.map((q) => JSON.stringify(q.queryKey))
  const expectKey = (k: unknown[]) =>
    assert.ok(keys.includes(JSON.stringify(k)), `queryKey ${JSON.stringify(k)} 必须存在`)

  expectKey(['market-stocks', { scope: 'market' }])
  expectKey(['market-boards', 'all'])
  expectKey(['market-filter-specs'])
  expectKey(['watchlist'])
  expectKey(['watchlist', 'monitor-status'])
  expectKey(['market-status', 'reactive'])

  const stale = (k: unknown[]) =>
    (all.find((q) => JSON.stringify(q.queryKey) === JSON.stringify(k))?.options as
      | { staleTime?: number }
      | undefined)?.staleTime
  assert.equal(stale(['watchlist']), 60_000, 'useWatchlist staleTime 必须为 60s')
  assert.equal(stale(['watchlist', 'monitor-status']), 30_000, 'useWatchlistMonitorStatus staleTime 必须为 30s')
  assert.equal(stale(['market-stocks', { scope: 'market' }]), 30_000, 'useMarketStocks staleTime 必须为 30s')
})

// ===== mutation invalidation 集合漂移守卫 =====

test('mutation invalidation: add/remove 各失效 watchlist + monitor-status + market-stocks + strategy-runs', () => {
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const src = readFileSync(join(__dirname, '..', 'useWatchlistApi.ts'), 'utf-8')
  const count = (needle: string) => src.split(needle).length - 1
  assert.equal(count("invalidateQueries({ queryKey: ['watchlist'] })"), 2, 'watchlist 失效：add + remove = 2')
  assert.equal(
    count("invalidateQueries({ queryKey: ['watchlist', 'monitor-status'] })"),
    2,
    'monitor-status 失效：add + remove = 2',
  )
  assert.equal(count("invalidateQueries({ queryKey: ['market-stocks'] })"), 2, 'market-stocks 失效：add + remove = 2')
  assert.equal(count("invalidateQueries({ queryKey: ['strategy-runs'] })"), 2, 'strategy-runs 失效：add + remove = 2')
})
