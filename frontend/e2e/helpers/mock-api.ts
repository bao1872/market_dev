// E2E Mock API Helper - 使用 page.route() 拦截所有 /api/** 请求返回 fixture 数据
//
// 设计原则：
// 1. 所有 mock 数据来自 e2e/fixtures/stocks.ts（固定、可复现）
// 2. 不依赖生产数据；不影响非 /api/** 的请求（HTML/JS/CSS/asset 正常加载）
// 3. 自动注入认证 token，避免 ProtectedLayout 重定向 /login
// 4. 提供 setupMockApi(page) helper，每个 test 独立配置
// 5. 记录所有 API 调用到 calls[]，供测试断言（如验证 MDAS 调用次数、indicator_view 透传）
//
// URL 模式说明：
// apiClient.baseURL='/api'，前端代码调用 apiClient.get('/api/v1/instruments/...')
// axios combineURLs 会得到 '/api/api/v1/instruments/...'（双 /api 前缀）
// Vite proxy rewrite 去掉首个 /api → 后端收到 '/api/v1/instruments/...'
// Playwright 拦截到的 URL 路径为 '/api/api/v1/instruments/...'，故使用宽松子串匹配

import type { Page, Route } from '@playwright/test'
import {
  FIXTURE_INSTRUMENTS,
  FIXTURE_WATCHLIST,
  FIXTURE_ACCESS_PROFILE,
  FIXTURE_USER,
  FIXTURE_STRATEGIES,
  buildChartSnapshot,
  buildCaptureSnapshot,
  buildStrategyRunResults,
} from '../fixtures/stocks'

export interface MockApiCall {
  url: string
  method: string
  params: Record<string, string>
  body: unknown
  timestamp: number
}

export interface MockApiOptions {
  // 默认 indicator_view（capture 测试可指定）
  defaultIndicatorView?: 'node_cluster' | 'bollinger' | 'smc'
  // 是否记录调用日志（默认 true）
  recordCalls?: boolean
}

// 注入认证状态到 localStorage（绕过 ProtectedLayout 重定向）
// 必须在 page.goto 之前执行，否则 ProtectedLayout 会先渲染并触发 /login 重定向
//
// Storage 布局（对齐 src/store/auth.ts）：
//   - 'auth_token' / 'auth_refresh_token'：token 对（client.ts 拦截器读取）
//   - 'auth-store'：zustand persist key，含 { state: { isAuthenticated, user, token, ... } }
//   - 'capture_token'：capture 模式独立 key（CaptureStockPage 写入）
export async function injectAuthState(page: Page, opts: { captureMode?: boolean } = {}) {
  await page.addInitScript((captureMode: boolean) => {
    const token = captureMode ? 'fixture-capture-token' : 'fixture-access-token'
    const user = {
      id: 'fixture-user',
      name: 'fixture@example.com',
      email: 'fixture@example.com',
      is_admin: false,
      roles: ['member'],
      subscription_active: true,
      plan_code: 'pro',
      plan_display_name: 'Pro',
      features: ['market', 'stock_detail', 'capture'],
      limits: { watchlist: 100, strategies: 10 },
      expires_at: '2025-12-31T00:00:00Z',
    }
    if (captureMode) {
      localStorage.setItem('capture_token', token)
    } else {
      localStorage.setItem('auth_token', token)
      localStorage.setItem('auth_refresh_token', 'fixture-refresh-token')
      // zustand persist 格式：{ state: {...}, version: 0 }
      localStorage.setItem(
        'auth-store',
        JSON.stringify({
          state: {
            isAuthenticated: true,
            user,
            token,
            refreshToken: 'fixture-refresh-token',
            keepLogin: true,
          },
          version: 0,
        }),
      )
    }
  }, opts.captureMode ?? false)
}

// 解析 URL query 参数
function parseParams(url: string): Record<string, string> {
  const u = new URL(url, 'http://localhost')
  const params: Record<string, string> = {}
  u.searchParams.forEach((v, k) => {
    params[k] = v
  })
  return params
}

// 主 mock API 配置函数
export async function setupMockApi(
  page: Page,
  options: MockApiOptions = {},
): Promise<{ calls: MockApiCall[] }> {
  const calls: MockApiCall[] = []
  const recordCalls = options.recordCalls ?? true
  const defaultIndicatorView = options.defaultIndicatorView ?? 'node_cluster'

  // URL 模式 `**/api/**`：
  // Playwright 使用 preview 模式（npm run build && npm run preview），JS bundle 在 /assets/，
  // 不会有 /src/api/ 模块加载请求，故 `**/api/**` 不会误拦截模块。
  // 真实 API 请求路径有两种形式：
  //   1. /api/api/v1/...（apiClient.get('/api/v1/...')，baseURL=/api 双 /api）
  //   2. /api/me/... 或 /api/auth/...（apiClient.get('/me/...')，baseURL=/api 单 /api）
  // `**/api/api/**` 会漏掉第 2 种导致 /me/access、/auth/refresh 401 → 跳转 /login。
  // 故用 `**/api/**` 覆盖所有真实 API 请求。
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = request.url()
    const method = request.method()
    const params = parseParams(url)
    let body: unknown = null
    try {
      const postData = request.postData()
      if (postData) body = JSON.parse(postData)
    } catch {
      // 非 JSON body 忽略
    }

    if (recordCalls) {
      calls.push({
        url,
        method,
        params,
        body,
        timestamp: Date.now(),
      })
    }

    // === Auth ===
    // /me/access 和 /me（注意：apiClient.get('/me') → URL /api/me，不带 /auth 前缀）
    if (url.includes('/me/access')) {
      return route.fulfill({ status: 200, json: FIXTURE_ACCESS_PROFILE })
    }
    if (url.match(/\/me(?:\?|$)/) && !url.includes('/access')) {
      return route.fulfill({ status: 200, json: FIXTURE_USER })
    }

    // === Instruments ===
    if (url.includes('/instruments/by-symbol/')) {
      const m = url.match(/\/instruments\/by-symbol\/([^/?]+)/)
      const symbol = m?.[1] ?? '000001'
      const inst = FIXTURE_INSTRUMENTS[symbol] ?? FIXTURE_INSTRUMENTS['000001']
      return route.fulfill({ status: 200, json: inst })
    }
    if (url.match(/\/instruments\/[^/]+\/quote/)) {
      const snapshot = buildChartSnapshot('000001', '1d')
      return route.fulfill({ status: 200, json: snapshot.quote })
    }
    if (url.match(/\/instruments\/[^/]+\/events/)) {
      return route.fulfill({ status: 200, json: { items: [], total: 0 } })
    }
    if (url.match(/\/instruments\/[^/]+\/memo/) && method === 'GET') {
      return route.fulfill({
        status: 200,
        json: { instrument_id: 'inst-000001', content: '', updated_at: '2024-01-01T00:00:00Z' },
      })
    }
    if (url.match(/\/instruments\/[^/]+\b/) && !url.includes('by-symbol') && !url.includes('chart-snapshot') && !url.includes('quote') && !url.includes('events') && !url.includes('memo') && !url.includes('bars') && !url.includes('indicators') && !url.includes('structural') && !url.includes('temporal') && method === 'GET') {
      // 单个 instrument by id（非 by-symbol）
      const m = url.match(/\/instruments\/([^/?]+)/)
      const id = m?.[1] ?? 'inst-000001'
      const inst = Object.values(FIXTURE_INSTRUMENTS).find((i) => i.id === id) ?? FIXTURE_INSTRUMENTS['000001']
      return route.fulfill({ status: 200, json: inst })
    }
    if (url.match(/\/instruments(?:\?|$)/) && method === 'GET') {
      return route.fulfill({
        status: 200,
        json: {
          items: Object.values(FIXTURE_INSTRUMENTS),
          total: 3,
          page: 1,
          page_size: 50,
          pages: 1,
        },
      })
    }

    // === Chart Snapshot（详情页主请求） ===
    // 后端路径：/api/v1/instruments/{id}/chart-snapshot
    if (url.includes('/chart-snapshot')) {
      const symbol = params.symbol || '000001'
      const timeframe = params.timeframe || '1d'
      const includeSmc = params.include_smc === '1' || params.include_smc === 'true'
      const indicatorView = includeSmc ? 'smc' : defaultIndicatorView
      const snapshot = buildChartSnapshot(symbol, timeframe, indicatorView)
      return route.fulfill({ status: 200, json: snapshot })
    }

    // === Capture Snapshot ===
    if (url.match(/\/capture\/stocks\/[^/]+\/snapshot/)) {
      const match = url.match(/\/capture\/stocks\/([^/]+)\/snapshot/)
      const instrumentId = match?.[1] ?? 'inst-000001'
      const symbol =
        Object.entries(FIXTURE_INSTRUMENTS).find(([, v]) => v.id === instrumentId)?.[0] ?? '000001'
      const timeframe = params.timeframe || '1d'
      const indicatorView =
        (params.indicator_view as 'node_cluster' | 'bollinger' | 'smc') || defaultIndicatorView
      const snapshot = buildCaptureSnapshot(symbol, indicatorView, timeframe)
      return route.fulfill({ status: 200, json: snapshot })
    }

    // === Bars/Indicators（旧端点，详情页已改用 chart-snapshot，保留兼容） ===
    if (url.match(/\/instruments\/[^/]+\/bars/)) {
      const snapshot = buildChartSnapshot('000001', params.timeframe || '1d')
      return route.fulfill({ status: 200, json: snapshot.bars })
    }
    if (url.match(/\/instruments\/[^/]+\/indicators/)) {
      const snapshot = buildChartSnapshot('000001', params.timeframe || '1d')
      return route.fulfill({ status: 200, json: snapshot.indicators })
    }

    // === Watchlist ===
    if (url.includes('/watchlist')) {
      // /watchlist/monitor-status 等
      if (url.includes('/watchlist/monitor-status')) {
        // 注意：useStockDetailActions 期望 monitorStatusQuery.data.items 为数组
        return route.fulfill({
          status: 200,
          json: {
            items: FIXTURE_WATCHLIST.items.map((w) => ({
              instrument_id: w.instrument_id,
              symbol: w.instrument_symbol,
              name: w.instrument_name,
              status: 'active',
              last_run_at: '2024-06-01T08:00:00Z',
            })),
            total: FIXTURE_WATCHLIST.total,
            last_run_at: '2024-06-01T08:00:00Z',
            status: 'idle',
          },
        })
      }
      if (method === 'GET') {
        return route.fulfill({ status: 200, json: FIXTURE_WATCHLIST })
      }
      return route.fulfill({ status: 200, json: {} })
    }

    // === Strategies ===
    if (url.match(/\/strategies(?:\?|$)/) && method === 'GET') {
      return route.fulfill({ status: 200, json: FIXTURE_STRATEGIES })
    }
    // /strategies/{key}/published-runs — 必须先于 /strategies/{key} 匹配
    if (url.match(/\/strategies\/[^/]+\/published-runs/)) {
      // 返回单个 published run（useStockDetailActions 取 items[0].id 作为 activeRunId）
      const items = Object.keys(FIXTURE_INSTRUMENTS).map((symbol, idx) => ({
        id: `fixture-run-${String(idx + 1).padStart(3, '0')}`,
        strategy_key: 'dsa_selector',
        strategy_version_id: 'strategy-version-dsa-selector',
        run_status: 'published',
        published_at: '2024-06-01T08:00:00Z',
        trade_date: '2024-06-01',
        total_items: 3,
        instrument_symbol: symbol,
      }))
      return route.fulfill({
        status: 200,
        json: { items: items.slice(0, 1), total: 1, page: 1, page_size: 50 },
      })
    }
    if (url.match(/\/strategies\/[^/]+$/) && method === 'GET') {
      return route.fulfill({ status: 200, json: FIXTURE_STRATEGIES.items[0] })
    }
    if (url.includes('/strategies/') && url.includes('/versions')) {
      return route.fulfill({ status: 200, json: { items: [], total: 0 } })
    }

    // === Strategy Runs / Results ===
    // /strategy-runs/{run_id}/results — 必须先于 /strategy-runs 匹配
    if (url.match(/\/strategy-runs\/[^/]+\/results/)) {
      let items = Object.keys(FIXTURE_INSTRUMENTS).flatMap((symbol) =>
        buildStrategyRunResults(symbol).items,
      )
      // [V2 同源同序验收] 尊重 sort_by/sort_desc 参数，使非默认排序产生不同数组顺序。
      // 列表页为 serverSide 模式，排序由后端负责；mock 必须根据 sort 参数返回对应顺序，
      // 才能验证详情左栏（用相同 canonicalQuery 请求）与列表页数组完全一致。
      const sortBy = params.sort_by
      const sortDesc = params.sort_desc === 'true' || params.sort_desc === '1'
      if (sortBy) {
        items = [...items].sort((a, b) => {
          let cmp = 0
          if (sortBy === 'stock') {
            cmp = String(a.instrument_symbol).localeCompare(String(b.instrument_symbol))
          } else if (sortBy === 'change_pct') {
            cmp = Number(a.latest_change_pct) - Number(b.latest_change_pct)
          } else {
            cmp = String(a.instrument_symbol).localeCompare(String(b.instrument_symbol))
          }
          return sortDesc ? -cmp : cmp
        })
      }
      return route.fulfill({
        status: 200,
        json: {
          items,
          total: items.length,
          page: 1,
          page_size: 50,
          source_total: items.length,
          filtered_total: items.length,
        },
      })
    }
    if (url.includes('/strategy-runs') && method === 'GET') {
      const items = Object.keys(FIXTURE_INSTRUMENTS).map((symbol, idx) => ({
        id: `fixture-run-${String(idx + 1).padStart(3, '0')}`,
        strategy_key: 'dsa_selector',
        run_status: 'published',
        published_at: '2024-06-01T08:00:00Z',
        trade_date: '2024-06-01',
        total_items: 3,
        instrument_symbol: symbol,
      }))
      return route.fulfill({
        status: 200,
        json: {
          items,
          total: items.length,
          page: 1,
          page_size: 50,
        },
      })
    }
    if (url.match(/\/strategy-results\/[^/]+/)) {
      return route.fulfill({
        status: 200,
        json: buildStrategyRunResults('000001').items[0],
      })
    }
    if (url.includes('/monitor-states')) {
      return route.fulfill({ status: 200, json: { items: [], total: 0 } })
    }
    if (url.includes('/strategy-events')) {
      return route.fulfill({ status: 200, json: { items: [], total: 0 } })
    }

    // === Market ===
    if (url.includes('/market/status')) {
      return route.fulfill({
        status: 200,
        json: {
          is_trading_day: true,
          is_trading_hours: true,
          status_text: '交易中',
          next_session_at: null,
        },
      })
    }
    if (url.includes('/market/stocks')) {
      return route.fulfill({
        status: 200,
        json: {
          items: Object.values(FIXTURE_INSTRUMENTS).map((inst) => ({
            instrument_id: inst.id,
            symbol: inst.symbol,
            name: inst.name,
            market: inst.market,
            last_price: 10.0,
            change_pct: 1.23,
          })),
          total: 3,
        },
      })
    }
    if (url.includes('/market/boards')) {
      // 前端 MarketWorkspacePage 期望 boardsQuery.data 为 { items, available, stale }
      return route.fulfill({ status: 200, json: { items: [], available: true, stale: false } })
    }

    // === Messages / Notifications ===
    if (url.includes('/messages/unread-count')) {
      return route.fulfill({ status: 200, json: { unread_count: 0 } })
    }
    if (url.includes('/messages') && method === 'GET') {
      return route.fulfill({ status: 200, json: { items: [], total: 0 } })
    }
    if (url.includes('/notification-channels')) {
      return route.fulfill({ status: 200, json: { items: [], total: 0 } })
    }

    // === Calendar ===
    if (url.includes('/calendar/is-trading-day/')) {
      return route.fulfill({
        status: 200,
        json: { is_trading_day: true, market: 'A', date: '2024-06-01' },
      })
    }
    if (url.includes('/calendar')) {
      return route.fulfill({ status: 200, json: { items: [], total: 0 } })
    }

    // === Stock Detail Feishu ===
    if (url.includes('/stock-detail-feishu')) {
      return route.fulfill({
        status: 200,
        json: { share_id: 'fixture-share-001', status: 'pending' },
      })
    }

    // === Version / Health ===
    if (url.match(/\/version(?:\?|$)/)) {
      return route.fulfill({ status: 200, json: { version: 'fixture-1.0.0' } })
    }
    if (url.match(/\/health(?:\?|$)/)) {
      return route.fulfill({ status: 200, json: { status: 'ok' } })
    }

    // === Default: 200 空响应，避免未 mock 的 API 导致测试失败 ===
    // 测试若依赖某个未 mock 的端点的特定数据，应在断言前显式检查
    return route.fulfill({
      status: 200,
      json: { items: [], total: 0 },
    })
  })

  return { calls }
}

// 辅助：等待 data-render-ready="true" 出现（Capture 就绪标志）
export async function waitForRenderReady(page: Page, timeout = 15_000) {
  await page.waitForSelector('[data-render-ready="true"]', { timeout })
}

// 辅助：断言 capture 调用了指定 indicator_view
export function assertCaptureIndicatorView(
  calls: MockApiCall[],
  expected: 'node_cluster' | 'bollinger' | 'smc',
): void {
  const captureCalls = calls.filter((c) => c.url.includes('/capture/stocks/'))
  if (captureCalls.length === 0) {
    throw new Error(`未发现 capture API 调用，期望 indicator_view=${expected}`)
  }
  const lastCall = captureCalls[captureCalls.length - 1]
  if (lastCall.params.indicator_view !== expected) {
    throw new Error(
      `capture API indicator_view 不匹配：期望 ${expected}，实际 ${lastCall.params.indicator_view}`,
    )
  }
}

// 辅助：统计 chart-snapshot API 调用次数（用于断言单 MDAS 调用）
export function countChartSnapshotCalls(calls: MockApiCall[]): number {
  return calls.filter((c) => c.url.includes('/chart-snapshot')).length
}

// 辅助：统计 capture snapshot 调用次数
export function countCaptureSnapshotCalls(calls: MockApiCall[]): number {
  return calls.filter((c) => c.url.includes('/capture/stocks/') && c.url.includes('/snapshot')).length
}
