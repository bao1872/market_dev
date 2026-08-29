// [ReviewFlowContract] - 描述: PHASE D1 Review 前端产品流契约测试
//
// 定位：C2 已用**真实 HTTP**（app.main.app + 真实 Postgres 验证库）证明 backend
// 可达、权限正确、JSON 正确、lineage 正确。因此 Phase D 允许 mock HTTP，
// 但 **mock payload 必须来自 C2 已验证的 JSON contract**（见下方 CANONICAL FIXTURES），
// 禁止前端测试自己发明另一套 payload。
//
// 测试层级说明（诚实声明）：
// 本仓库现有前端测试栈为 `tsx --test`（node test runner，纯 TS 逻辑，**无 DOM**）
// 与 playwright（e2e，需真实 server + 浏览器）。**没有** vitest / jsdom /
// @testing-library 等组件渲染能力，因此本文件覆盖的是
// "route -> state owner -> query identity -> API -> 解析/格式化"这一层，
// **不含** JSX 渲染断言（属 Phase G）。见 §16 的 honest 记录。
//
// 覆盖（task §16）：
//   A dates empty  B normal flow  C change date  D change scope
//   E 403  F 404  G 500  H degraded  I null 语义
//   + memberDirectory / family snapshot completeness fail-closed
import { strict as assert } from 'node:assert'
import { beforeEach, test } from 'node:test'
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'

// node test runner 无 DOM；apiClient 的 request interceptor 会读
// window.location.search / sessionStorage / localStorage（注入 Bearer Token）。
// 这里只补最小 test-harness shim，**不修改任何生产代码**。
const globalAny = globalThis as unknown as Record<string, unknown>
globalAny.window ??= { location: { search: '', pathname: '/' } }
globalAny.sessionStorage ??= {
  getItem: () => null, setItem: () => undefined, removeItem: () => undefined,
}
globalAny.localStorage ??= {
  getItem: () => null, setItem: () => undefined, removeItem: () => undefined,
}

import { apiClient } from '../../../api/client'
import { extractReviewError, getReviewOverview, getReviewScopes } from '../api'
import {
  decodeReviewUrl,
  defaultReviewUrlState,
  encodeReviewUrl,
  withReviewDateChange,
  withReviewFamilyChange,
  type ReviewUrlState,
} from '../urlState'
import { isScopeDetailEnabled, scopeDetailQueryOptions } from '../useReviewScopeDetail'
import { loadFamilySnapshot } from '../useReviewScopeFamilySnapshot'
import { findScopeById } from '../scopeExplorerViewModel'
import { displayMember, formatPercentNullable, NULL_DISPLAY } from '../reviewFormat'
import { reviewKeys } from '../queryKeys'
import type { ReviewScopeListItem } from '../types'

// ===========================================================================
// CANONICAL FIXTURES —— 键集合与取值全部来自 C2 实测 HTTP JSON
// （backend/tests/test_pg_review_http_runtime_c2.py::test_c2_http_success_matrix）
// ===========================================================================

const C2_TRADE_DATE = '2099-12-31'
const C2_CORE_RUN_ID = '11111111-2222-3333-4444-555555555555'
const C2_REVIEW_RUN_ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
const C2_MEMBER_ID = '99999999-8888-7777-6666-555555555555'

const FIXTURE_DATES = {
  trade_dates: [C2_TRADE_DATE, '2099-12-30'],
  latest_trade_date: C2_TRADE_DATE,
}

const FIXTURE_DATES_EMPTY = { trade_dates: [], latest_trade_date: null }

/** C2 实测 /overview 顶层 20 键；未激活家族 coverage 为 null（不是 0）。 */
const FIXTURE_OVERVIEW = {
  reviewRunId: C2_REVIEW_RUN_ID,
  tradeDate: C2_TRADE_DATE,
  status: 'published',
  sourceCoreRunId: C2_CORE_RUN_ID,
  sourceBoardRunId: null,
  sourceChipRunId: null,
  degradedReasons: [],
  chipCoverage: null,
  algorithmVersion: 'review-algo-v1',
  filterVersion: 'filters-1.0.0',
  baselineWindow: 20,
  coverage: { market: null, indices: null, styles: null, industryL1: 0.8 },
  coverageRatio: 0.8,
  expectedScopeCount: 5,
  succeededScopeCount: 5,
  failedScopeCount: 0,
  signalCount: 0,
  startedAt: null,
  completedAt: null,
  publishedAt: '2026-08-28T10:00:00+00:00',
}

const FIXTURE_OVERVIEW_DEGRADED = {
  ...FIXTURE_OVERVIEW,
  degradedReasons: ['CHIP_UNAVAILABLE'],
}

const FIXTURE_SCOPE_ITEM: ReviewScopeListItem = {
  scopeType: 'industry_l1',
  scopeKey: 'all',
  scopeName: 'C2 测试行业',
  readiness: 'ready',
  status: 'succeeded',
  eligibleCount: 100,
  providedCount: 80,
  coverageRatio: 0.8,
  summary: null,
  observationSummary: null,
}

const FIXTURE_SCOPES_PAGE_1 = {
  items: [FIXTURE_SCOPE_ITEM],
  total: 1,
  page: 1,
  page_size: 100,
  has_more: false,
}

/** C2 实测 detail 顶层 10 键 + 固定 8 组 observationGroups + memberDirectory。 */
const FIXTURE_DETAIL = {
  reviewRunId: C2_REVIEW_RUN_ID,
  tradeDate: C2_TRADE_DATE,
  scopeType: 'industry_l1',
  scopeKey: 'all',
  scopeName: 'C2 测试行业',
  algorithmVersion: 'review-algo-v1',
  observation: { price: { equal_weight_return: 0.0123 } },
  observationGroups: {
    price_capital: { group_key: 'price_capital', label: '', facts: {} },
    trend_state: { group_key: 'trend_state', label: '', facts: {} },
    trend_progress: { group_key: 'trend_progress', label: '', facts: {} },
    trend_volume_confirmation: {
      group_key: 'trend_volume_confirmation', label: '', facts: {},
    },
    structure_break_turn: { group_key: 'structure_break_turn', label: '', facts: {} },
    structure_evolution_position: {
      group_key: 'structure_evolution_position', label: '', facts: {},
    },
    momentum_squeeze_release: {
      group_key: 'momentum_squeeze_release', label: '', facts: {},
    },
    volume_anomaly: { group_key: 'volume_anomaly', label: '', facts: {} },
  },
  composition: {
    scope: { scope_type: 'industry_l1', scope_key: 'all' },
    trade_date: C2_TRADE_DATE,
    capability: {},
    scope_observation: null,
    historical_dynamics: null,
    internal_structure_facts: null,
    leadership: {
      status: 'ready',
      reason: null,
      coverage: 1,
      current_leader_ids: [C2_MEMBER_ID],
      previous_leader_ids: [C2_MEMBER_ID],
      entrant_ids: [],
      exit_ids: [],
    },
    member_attribution: null,
    composition_readiness: 'ready',
  },
  memberDirectory: {
    [C2_MEMBER_ID]: { symbol: 'C2TST', name: 'C2 测试标的' },
  },
}

// ===========================================================================
// HTTP mock harness（替换 apiClient 的 adapter，不发明 payload）
// ===========================================================================

interface MockResponse {
  status: number
  data: unknown
  headers?: Record<string, string>
}

let handler: ((url: string) => MockResponse) | null = null
const requestedUrls: string[] = []

function installMock(): void {
  apiClient.defaults.adapter = (async (
    config: InternalAxiosRequestConfig,
  ): Promise<AxiosResponse> => {
    const url = String(config.url ?? '')
    requestedUrls.push(url)
    const fallback = (): MockResponse => ({ status: 200, data: {} })
    const res = (handler ?? fallback)(url)
    if (res.status >= 400) {
      // 必须用真实 AxiosError：extractReviewError 读 error.response.status/data，
      // 用裸 Error 会被 axios 重新包装导致 response 丢失（前端解析不到 detail）。
      throw new AxiosError(
        `Request failed with status code ${res.status}`,
        String(res.status),
        config,
        undefined,
        {
          data: res.data,
          status: res.status,
          statusText: 'Error',
          headers: res.headers ?? {},
          config,
        } as AxiosResponse,
      )
    }
    return {
      data: res.data,
      status: res.status,
      statusText: 'OK',
      headers: res.headers ?? {},
      config,
    }
  }) as never
}

beforeEach(() => {
  requestedUrls.length = 0
  handler = null
  installMock()
})

// ===========================================================================
// A. dates empty
// ===========================================================================

test('A. dates 空 -> 不产生 tradeDate -> 进入 empty state（绝不访问 undefined date）', async () => {
  handler = () => ({ status: 200, data: FIXTURE_DATES_EMPTY })
  const { getReviewDates } = await import('../api')
  const dates = await getReviewDates()
  assert.deepEqual(dates.trade_dates, [])
  assert.equal(dates.latest_trade_date, null)

  // 页面 tradeDate 推导（ReviewPage.tsx: urlState.date ?? latest ?? ''）
  const urlState = defaultReviewUrlState()
  assert.equal(urlState.date, null, 'URL 无 date 参数时为 null（不是空串）')
  const tradeDate = urlState.date ?? dates.latest_trade_date ?? ''
  assert.equal(tradeDate, '', '空 dates 时派生 tradeDate 必须是空串，不得是 undefined/null')
  // tradeDate 为空 -> overview / scopes / detail 均不发起（enabled=false）
  assert.equal(isScopeDetailEnabled({ tradeDate: '', scopeType: 'industry_l1', scopeKey: 'all' }), false)
})

// ===========================================================================
// B. normal flow: dates -> overview -> scopes -> detail
// ===========================================================================

test('B. normal flow dates -> overview -> scopes -> detail 全链路真实 api.ts 调用', async () => {
  handler = (url: string) => {
    if (url.includes('/review/dates')) return { status: 200, data: FIXTURE_DATES }
    if (url.includes('/overview')) return { status: 200, data: FIXTURE_OVERVIEW }
    if (url.includes('/scopes/')) return { status: 200, data: FIXTURE_DETAIL }
    if (url.includes('/scopes')) return { status: 200, data: FIXTURE_SCOPES_PAGE_1 }
    throw new Error(`unexpected url: ${url}`)
  }

  const { getReviewDates, getReviewScopeDetail } = await import('../api')

  // 1) dates
  const dates = await getReviewDates()
  assert.equal(dates.latest_trade_date, C2_TRADE_DATE)

  // 2) overview：Review Y -> Core X lineage 必须来自真实 JSON
  const overview = await getReviewOverview(C2_TRADE_DATE)
  assert.equal(overview.reviewRunId, C2_REVIEW_RUN_ID)
  assert.equal(overview.sourceCoreRunId, C2_CORE_RUN_ID, 'HTTP_LINEAGE: overview 必须暴露 Core X')
  assert.equal(overview.status, 'published')

  // 3) scopes（family snapshot 的完整 transport aggregation）
  const snapshot = await loadFamilySnapshot((page) =>
    getReviewScopes(C2_TRADE_DATE, { scope_type: 'industry_l1', page, page_size: 100 }),
  )
  assert.equal(snapshot.total, 1)
  assert.equal(snapshot.items.length, 1)

  // 4) URL 派生已选 scope -> detail
  const urlState: ReviewUrlState = { ...defaultReviewUrlState(), date: C2_TRADE_DATE, scopeKey: 'all' }
  const selected = findScopeById(snapshot.items, urlState.scopeKey)
  assert.ok(selected, '已选 scope 必须能从 family snapshot 中定位')
  const detail = await getReviewScopeDetail(C2_TRADE_DATE, selected!.scopeType, selected!.scopeKey)
  assert.equal(detail.reviewRunId, C2_REVIEW_RUN_ID)
  assert.equal(detail.scopeKey, 'all')
  assert.equal(Object.keys(detail.observationGroups).length, 8, 'observationGroups 必须固定 8 组')
})

// ===========================================================================
// C. change date
// ===========================================================================

test('C. 切换日期：清空 scopeKey + 重置 page，且 overview/scopes query identity 随日期变化', () => {
  const base: ReviewUrlState = {
    ...defaultReviewUrlState(),
    date: C2_TRADE_DATE,
    scopeKey: 'all',
    page: 3,
  }
  const next = withReviewDateChange(base, '2099-12-30')
  assert.equal(next.date, '2099-12-30')
  assert.equal(next.scopeKey, null, '切换日期必须清空已选 scope，不得跨日期沿用')
  assert.equal(next.page, 1)

  // identity：日期不同 -> key 不同（React Query 不会跨日期复用数据，避免 mixed world）
  const k1 = reviewKeys.overview(C2_TRADE_DATE)
  const k2 = reviewKeys.overview('2099-12-30')
  assert.notDeepEqual(k1, k2)
  assert.notDeepEqual(
    reviewKeys.familySnapshot(C2_TRADE_DATE, 'industry_l1'),
    reviewKeys.familySnapshot('2099-12-30', 'industry_l1'),
  )
  // 切换日期后无 scopeKey -> detail 不发起
  assert.equal(
    isScopeDetailEnabled({ tradeDate: next.date, scopeType: 'industry_l1', scopeKey: next.scopeKey }),
    false,
  )
})

// ===========================================================================
// D. change scope
// ===========================================================================

test('D. 切换 scope：detail query identity 变化，tab 切换不改变 identity', () => {
  const input = {
    tradeDate: C2_TRADE_DATE,
    scopeType: 'industry_l1' as const,
    scopeKey: 'all',
  }
  assert.equal(isScopeDetailEnabled(input), true)

  const keyAll = scopeDetailQueryOptions(input).queryKey
  const keyOther = scopeDetailQueryOptions({ ...input, scopeKey: 'bank' }).queryKey
  assert.notDeepEqual(keyAll, keyOther, '切换 scope 必须产生新的 detail identity')

  // tab 不是 detail key 的 input：切 tab 不得重新请求 detail
  assert.deepEqual(
    scopeDetailQueryOptions(input).queryKey,
    scopeDetailQueryOptions(input).queryKey,
  )
  assert.equal(
    JSON.stringify(scopeDetailQueryOptions(input).queryKey).includes('tab'),
    false,
    'detail queryKey 不得包含 tab',
  )

  // 无 scopeKey -> enabled=false（表格行绝不触发 detail，无 N+1）
  assert.equal(
    scopeDetailQueryOptions({ ...input, scopeKey: null }).enabled,
    false,
  )
})

// ===========================================================================
// E. 403 capability
// ===========================================================================

test('E. 403 research_replay 缺失 -> extractReviewError 给出权限不足，requestId 可空', () => {
  // C2 实测：/v1/review/dates 无 research_replay -> 403，body 为 {detail: "..."}
  const err403 = {
    response: {
      status: 403,
      data: { detail: "缺少 capability 'research_replay'" },
      headers: {} as Record<string, string>,
    },
  }
  const parsed = extractReviewError(err403)
  assert.equal(parsed.status, 403)
  assert.equal(parsed.message, '权限不足，当前账号无复盘权限')
  assert.equal(parsed.requestId, null)
  assert.ok(parsed.detail.includes('research_replay'))
})

// ===========================================================================
// F. 404 publication missing
// ===========================================================================

test('F. 404 出版撤销/日期过期 -> 明确 not-published 状态，不得偷偷读另一天', async () => {
  handler = () => ({
    status: 404,
    data: { detail: 'trade_date=2099-12-31 无已发布复盘' },
  })
  await assert.rejects(
    () => getReviewOverview(C2_TRADE_DATE),
    (err: unknown) => {
      const parsed = extractReviewError(err)
      assert.equal(parsed.status, 404)
      assert.equal(parsed.message, 'trade_date=2099-12-31 无已发布复盘')
      return true
    },
  )
})

// ===========================================================================
// G. 500 integrity fail-closed
// ===========================================================================

test('G. 500 data-integrity -> 明确错误态；requestId 为 null 时 graceful（无 x-request-id）', async () => {
  handler = () => ({
    status: 500,
    data: {
      detail:
        'trade_date=2099-12-31 的正式 Review pointer 指向 run=...，但该 run 不满足正式发布合同，数据一致性异常',
    },
  })
  await assert.rejects(
    () => getReviewOverview(C2_TRADE_DATE),
    (err: unknown) => {
      const parsed = extractReviewError(err)
      assert.equal(parsed.status, 500)
      // C2 实测 app 不产出 x-request-id（owner 是 gateway），requestId 必须优雅为 null
      assert.equal(parsed.requestId, null)
      assert.equal(parsed.message, '服务器错误', 'requestId 为空时不得渲染出 request_id=null')
      assert.ok(parsed.detail.includes('数据一致性异常'))
      return true
    },
  )
})

// ===========================================================================
// H. degraded reasons
// ===========================================================================

test('H. degradedReasons 非空 -> 必须可见（不静默降级）', () => {
  // ReviewHeader 的渲染条件：overview.degradedReasons.length > 0
  assert.equal(FIXTURE_OVERVIEW.degradedReasons.length, 0, '正常 run 无降级')
  assert.ok(
    FIXTURE_OVERVIEW_DEGRADED.degradedReasons.length > 0,
    '降级 run 的 degradedReasons 必须非空，驱动降级横幅',
  )
  // 降级不意味着失败：status 仍为 published，页面继续渲染
  assert.equal(FIXTURE_OVERVIEW_DEGRADED.status, 'published')
})

// ===========================================================================
// I. null 语义：null 绝不当 0
// ===========================================================================

test('I. null coverage 显示 — 且真实 0 仍显示 0.0%（两者不可混淆）', () => {
  assert.equal(formatPercentNullable(null), NULL_DISPLAY)
  assert.equal(formatPercentNullable(undefined), NULL_DISPLAY)
  assert.equal(formatPercentNullable(Number.NaN), NULL_DISPLAY)
  assert.equal(formatPercentNullable(0), '0.0%', '真实 0 覆盖率必须显示 0.0%，不是 —')
  assert.equal(formatPercentNullable(0.8), '80.0%')

  // C2 实测：未激活家族 coverage 为 null
  assert.equal(FIXTURE_OVERVIEW.coverage.market, null)
  assert.equal(formatPercentNullable(FIXTURE_OVERVIEW.coverage.market), NULL_DISPLAY)
  assert.equal(formatPercentNullable(FIXTURE_OVERVIEW.coverage.industryL1), '80.0%')
})

// ===========================================================================
// + memberDirectory / family snapshot fail-closed
// ===========================================================================

test('memberDirectory 命中 -> symbol/name；缺失 -> 不伪造，回退 id', () => {
  const dir = FIXTURE_DETAIL.memberDirectory
  assert.equal(displayMember(C2_MEMBER_ID, dir), 'C2 测试标的 · C2TST')
  const missingId = '00000000-0000-0000-0000-000000000000'
  assert.equal(displayMember(missingId, dir), missingId, '目录缺失时回退 id，不得伪造名称')
  assert.equal(displayMember(C2_MEMBER_ID, null), C2_MEMBER_ID)
})

test('family snapshot completeness fail-closed：部分/漂移/重复一律抛错，不渲染不完整数据', async () => {
  // 完整快照正常返回
  const ok = await loadFamilySnapshot(async () => ({
    items: [FIXTURE_SCOPE_ITEM], total: 1, page: 1, page_size: 100, has_more: false,
  }))
  assert.equal(ok.items.length, 1)

  // items.length != total -> fail closed
  await assert.rejects(
    () => loadFamilySnapshot(async () => ({
      items: [FIXTURE_SCOPE_ITEM], total: 5, page: 1, page_size: 100, has_more: true,
    })),
    /incomplete family snapshot/,
  )

  // 跨页 total 漂移 -> fail closed
  // pageSize 必须缩到 1，否则 pageCount=1 只拉第一页，永远触发不到跨页漂移检查
  await assert.rejects(
    () =>
      loadFamilySnapshot(
        async (page) => ({
          items: [FIXTURE_SCOPE_ITEM],
          total: page === 1 ? 2 : 9,
          page,
          page_size: 1,
          has_more: page === 1,
        }),
        1,
      ),
    /family snapshot total 漂移/,
  )

  // (scopeType, scopeKey) 重复 -> fail closed
  await assert.rejects(
    () => loadFamilySnapshot(async () => ({
      items: [FIXTURE_SCOPE_ITEM, FIXTURE_SCOPE_ITEM],
      total: 2,
      page: 1,
      page_size: 100,
      has_more: false,
    })),
    /重复 scope identity/,
  )
})

// ===========================================================================
// 日期 owner 单一性（§4）：URL 是 SSOT
// ===========================================================================

test('selectedTradeDate 唯一 owner = URL（encode/decode 往返稳定）', () => {
  const state: ReviewUrlState = {
    ...defaultReviewUrlState(),
    date: C2_TRADE_DATE,
    family: 'industry_l1',
    scopeKey: 'all',
    page: 2,
    pageSize: 50,
  }
  const params = encodeReviewUrl(state)
  const decoded = decodeReviewUrl(params)
  assert.equal(decoded.date, C2_TRADE_DATE)
  assert.equal(decoded.scopeKey, 'all')
  assert.equal(decoded.page, 2)
  assert.equal(decoded.pageSize, 50)
})

test('切换 family 同样清空 scopeKey（避免跨族沿用 scope 身份）', () => {
  const base: ReviewUrlState = { ...defaultReviewUrlState(), date: C2_TRADE_DATE, scopeKey: 'all' }
  const next = withReviewFamilyChange(base, 'concept')
  assert.equal(next.family, 'concept')
  assert.equal(next.scopeKey, null)
})
