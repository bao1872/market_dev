// [S3-E] - 描述: 最终 residual domain 拆分契约测试（notification / stockData / preferences）
// 用法：./node_modules/.bin/tsx --test src/hooks/__tests__/residualDomainSplit.test.ts
//
// 证明 endpoints.ts / useApi.ts 兼容 barrel 与新 owner 指向同一函数引用；
// /v1/admin/* 已无 residual；queryKey/invalidation 未漂移。
// [REVIEW-V2-R1] boardAnalysis owner 已随旧 Board Analysis 后端退役删除。

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
  setItem: (k: string, v: string) => { _store[k] = v },
  removeItem: (k: string) => { delete _store[k] },
}
;(globalThis as unknown as { sessionStorage: unknown }).sessionStorage = {
  getItem: (k: string) => _store[k] ?? null,
  setItem: (k: string, v: string) => { _store[k] = v },
  removeItem: (k: string) => { delete _store[k] },
}

const NOTIF_API = ['getMessages','markMessageRead','getUnreadCount','readAllMessages','getNotificationChannels','createNotificationChannel','updateNotificationChannel','deleteNotificationChannel','verifyNotificationChannel','testNotificationChannel','testNotificationChannelLatestEvent','sendStockDetailFeishu','getStockDetailFeishuStatus','previewNotification'] as const
const NOTIF_HOOKS = ['useMessages','useUnreadCount','useMarkMessageRead','useReadAllMessages','useNotificationChannels','useCreateNotificationChannel','useUpdateNotificationChannel','useDeleteNotificationChannel','useVerifyNotificationChannel','useTestNotificationChannel','useTestNotificationChannelLatestEvent','usePreviewNotification'] as const
const STOCK_API = ['getEventsSummary','getInstruments','batchGetInstruments','getInstrumentById','getInstrumentBySymbol','getStockMemo','upsertStockMemo','deleteStockMemo','toggleMemoNotify','getBars','getQuote','getIndicators','getChartSnapshot','getCalendar','isTradingDay','getStructuralFactors','getTemporalFeatures','getStockContext','getFirstPyramid'] as const
const STOCK_HOOKS = ['useInstruments','useBatchInstruments','useInstrument','useInstrumentBySymbol','useEventsSummary','useStockMemo','useUpsertStockMemo','useDeleteStockMemo','useBars','useIndicators','useRealtimeQuote','useChartSnapshot','useCalendar','useIsTradingDay','useStructuralFactors','useTemporalFeatures','useStockContext','useFirstPyramid'] as const
const PREF_API = ['getTableViewPresets','createTableViewPreset','updateTableViewPreset','deleteTableViewPreset'] as const
const PREF_HOOKS = ['useTableViewPresets','useCreateTableViewPreset','useUpdateTableViewPreset','useDeleteTableViewPreset'] as const

test('API barrel identity: endpoints 重新导出 3 个新 owner（同一函数引用）', async () => {
  const ep = (await import('../../api/endpoints.ts')) as Record<string, unknown>
  const notif = (await import('../../api/notification.ts')) as Record<string, unknown>
  const stock = (await import('../../api/stockData.ts')) as Record<string, unknown>
  const pref = (await import('../../api/preferences.ts')) as Record<string, unknown>
  for (const n of NOTIF_API) assert.equal(ep[n], notif[n], `endpoints.${n} === notification.${n}`)
  for (const n of STOCK_API) assert.equal(ep[n], stock[n], `endpoints.${n} === stockData.${n}`)
  for (const n of PREF_API) assert.equal(ep[n], pref[n], `endpoints.${n} === preferences.${n}`)
})

test('Hook barrel identity: useApi 重新导出 3 个新 hook owner（同一引用）', async () => {
  const legacy = (await import('../useApi.ts')) as Record<string, unknown>
  const notif = (await import('../useNotificationApi.ts')) as Record<string, unknown>
  const stock = (await import('../useStockDataApi.ts')) as Record<string, unknown>
  const pref = (await import('../usePreferencesApi.ts')) as Record<string, unknown>
  for (const n of NOTIF_HOOKS) assert.equal(legacy[n], notif[n], `useApi.${n} === useNotificationApi.${n}`)
  for (const n of STOCK_HOOKS) assert.equal(legacy[n], stock[n], `useApi.${n} === useStockDataApi.${n}`)
  for (const n of PREF_HOOKS) assert.equal(legacy[n], pref[n], `useApi.${n} === usePreferencesApi.${n}`)
})

test('/v1/admin/* residual = 0：getAdminStockDebug 已入 admin owner', async () => {
  const ep = (await import('../../api/endpoints.ts')) as Record<string, unknown>
  const admin = (await import('../../api/admin.ts')) as Record<string, unknown>
  assert.equal(ep.getAdminStockDebug, admin.getAdminStockDebug, 'endpoints.getAdminStockDebug === admin.getAdminStockDebug')
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const epSrc = readFileSync(join(__dirname, '..', '..', 'api', 'endpoints.ts'), 'utf-8')
  const implLines = epSrc.split('\n').filter((l) => /\/v1\/admin\//.test(l) && !l.trim().startsWith('*') && !l.trim().startsWith('//'))
  assert.equal(implLines.length, 0, `endpoints.ts 不应再有 /v1/admin/* 实现，实际: ${implLines.join(' | ')}`)
})

test('query contract: notification/stockData/preferences queryKey 未漂移', async () => {
  const { useMessages, useUnreadCount, useNotificationChannels } = await import('../useNotificationApi.ts')
  const { useStockMemo, useBars, useCalendar, useStructuralFactors, useTemporalFeatures, useStockContext, useFirstPyramid } = await import('../useStockDataApi.ts')
  const { useTableViewPresets } = await import('../usePreferencesApi.ts')

  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const P = { limit: 10 }
  function Probe() {
    useMessages(P)
    useUnreadCount()
    useNotificationChannels()
    useStockMemo('i1')
    useBars('i1')
    useCalendar()
    useStructuralFactors('i1')
    useTemporalFeatures('i1')
    useStockContext('600519')
    useFirstPyramid('600519')
    useTableViewPresets('market')
    return null
  }
  renderToStaticMarkup(createElement(QueryClientProvider as never, { client: qc }, createElement(Probe as never)))
  const keys = qc.getQueryCache().getAll().map((q) => JSON.stringify(q.queryKey))
  const expectKey = (k: unknown[]) => assert.ok(keys.includes(JSON.stringify(k)), `queryKey ${JSON.stringify(k)} 必须存在`)
  expectKey(['messages', P])
  expectKey(['messages', 'unread-count'])
  expectKey(['notification-channels'])
  expectKey(['stock-memo', 'i1'])
  expectKey(['bars', 'i1', undefined])
  expectKey(['calendar', undefined])
  expectKey(['structural-factors', 'i1', undefined])
  expectKey(['temporal-features', 'i1', undefined])
  expectKey(['stock-context', '600519', null])
  expectKey(['first-pyramid', '600519', null])
  expectKey(['table-view-presets', 'market', null])
})

test('mutation invalidation 漂移守卫', () => {
  const __dirname = dirname(fileURLToPath(import.meta.url))
  const notifSrc = readFileSync(join(__dirname, '..', 'useNotificationApi.ts'), 'utf-8')
  const prefSrc = readFileSync(join(__dirname, '..', 'usePreferencesApi.ts'), 'utf-8')
  const cnt = (s: string, n: string) => s.split(n).length - 1
  assert.ok(notifSrc.includes("invalidateQueries({ queryKey: ['messages'] })"), 'mark/read-all 必须失效 [messages]')
  assert.ok(notifSrc.includes("invalidateQueries({ queryKey: ['notification-channels'] })"), 'channel mutation 必须失效 [notification-channels]')
  assert.ok(cnt(prefSrc, "invalidateQueries({ queryKey: ['table-view-presets'] })") >= 1, 'preset mutation 必须失效 [table-view-presets]')
})
