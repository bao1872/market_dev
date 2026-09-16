// [S3-A] - 描述: auth/access domain 物理拆分契约测试（运行时函数引用比较）
// 用法：./node_modules/.bin/tsx --test src/hooks/__tests__/authDomainSplit.test.ts
//
// 目的：证明拆分为「实现 owner + 兼容 barrel」后，旧 import path 与新 domain owner
// 指向**同一个函数引用**（不是复制实现），且 query 合同未漂移。
//
// 这不是字符串 grep：直接比较运行时函数引用 + 真实挂载后的 query cache key。

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

// 迁移前位于 endpoints.ts / useApi.ts 的 auth/access 符号
const API_FNS = [
  'login',
  'register',
  'renew',
  'refreshToken',
  'changePassword',
  'getMe',
  'getMyAccess',
  'getMyMembership',
] as const
const HOOK_FNS = [
  'useMe',
  'useMyMembership',
  'useLogin',
  'useRegister',
  'useRenew',
  'useRefreshToken',
] as const

// ===== API barrel identity =====

test('API barrel identity: endpoints 重新导出 auth owner（同一函数引用，无第二份实现）', async () => {
  const auth = (await import('../../api/auth.ts')) as Record<string, unknown>
  const endpoints = (await import('../../api/endpoints.ts')) as Record<string, unknown>
  for (const name of API_FNS) {
    assert.equal(
      endpoints[name],
      auth[name],
      `endpoints.${name} 必须与 auth.${name} 是同一个函数（兼容 barrel 不得复制实现）`,
    )
  }
})

// ===== Hook barrel identity =====

test('Hook barrel identity: useApi 重新导出 useAuthApi owner（同一 hook 引用）', async () => {
  const authHooks = (await import('../useAuthApi.ts')) as Record<string, unknown>
  const legacyHooks = (await import('../useApi.ts')) as Record<string, unknown>
  for (const name of HOOK_FNS) {
    assert.equal(
      legacyHooks[name],
      authHooks[name],
      `useApi.${name} 必须与 useAuthApi.${name} 是同一个 hook`,
    )
  }
})

// ===== query 合同（key / staleTime）不得漂移 =====

test('query contract: useMe=[me] / useMyMembership=[me,membership]，staleTime=0', async () => {
  const { useMe, useMyMembership } = await import('../useAuthApi.ts')
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  function Probe() {
    useMe()
    useMyMembership()
    return null
  }
  renderToStaticMarkup(
    createElement(QueryClientProvider as never, { client: qc }, createElement(Probe as never)),
  )

  const all = qc.getQueryCache().getAll()
  const keys = all.map((q) => JSON.stringify(q.queryKey))
  assert.ok(keys.includes(JSON.stringify(['me'])), 'useMe queryKey 必须为 [me]')
  assert.ok(keys.includes(JSON.stringify(['me', 'membership'])), 'useMyMembership queryKey 必须为 [me, membership]')

  const meQ = all.find((q) => JSON.stringify(q.queryKey) === JSON.stringify(['me']))
  assert.equal(
    (meQ?.options as { staleTime?: number } | undefined)?.staleTime,
    0,
    'useMe staleTime 必须为 0（STALE_AUTH）',
  )
  const memQ = all.find((q) => JSON.stringify(q.queryKey) === JSON.stringify(['me', 'membership']))
  assert.equal(
    (memQ?.options as { staleTime?: number } | undefined)?.staleTime,
    0,
    'useMyMembership staleTime 必须为 0（STALE_AUTH）',
  )
})
