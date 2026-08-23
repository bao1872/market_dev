// [Slice C] Canonical Review Scope 合同测试（纯 TS，tsx --test 可跑）。
// 覆盖：API 路径、类型/nullable 语义、query key 差异、URL 编解码往返、语义禁止项。
// 不涉及 React 渲染 / SCSS；禁止引入新 test framework。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { reviewKeys } from '../queryKeys'
import {
  decodeReviewUrl,
  encodeReviewUrl,
  defaultReviewUrlState,
  normalizeFamily,
  normalizeExplorerView,
  normalizeDetailTab,
  normalizePhase,
  normalizeSort,
  normalizePage,
  normalizePageSize,
} from '../urlState'
import type {
  ReviewScopeListItem,
  ReviewScopeSummary,
  ReviewScopeListParams,
  ReviewScopeListResponse,
  ReviewScopeCompositionDetailResponse,
} from '../types'
import { formatPercentNullable, formatNumberNullable, formatPhaseLabel } from '../reviewFormat'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

/** node:assert 无 doesNotHaveAnyKeys；本地等价实现 */
function assertMissingKeys(obj: object, keys: string[]): void {
  const present = new Set(Object.keys(obj))
  for (const k of keys) {
    assert.ok(!present.has(k), `不应包含字段 ${k}`)
  }
}

// ============================================================
// A. API 路径与参数
// ============================================================

test('A1. list endpoint path 正确且不含网关前缀 /api', () => {
  const src = read('api.ts')
  // canonical list 路径字面量
  assert.match(src, /\/v1\/review\/\$\{tradeDate\}\/scopes/, 'list 必须请求 /v1/review/{tradeDate}/scopes')
  // canonical 函数签名使用 canonical params 类型
  assert.match(
    src,
    /export async function getReviewScopes\(\s*tradeDate: string,\s*params: ReviewScopeListParams/,
    'canonical getReviewScopes 签名必须为 (tradeDate, ReviewScopeListParams)',
  )
  // 真实端点参数不得包含网关前缀 /api（仅注释允许提及）
  assert.doesNotMatch(src, /apiClient\.(get|post|patch|delete)\(\s*['"`]\/api\//, 'endpoint 调用不得含网关前缀 /api')
})

test('A2. list 仅传后端支持参数（scope_type/include_partial/page/page_size）', () => {
  const src = read('api.ts')
  // 截取 canonical getReviewScopes 函数体（到下一个 export async 前）
  const start = src.indexOf('export async function getReviewScopes(')
  const next = src.indexOf('export async function getReviewScopeDetail(', start)
  const fn = src.slice(start, next === -1 ? undefined : next)
  assert.ok(fn.includes('{ params }'), 'canonical getReviewScopes 必须直接透传 params 给 /scopes')
  assert.ok(fn.includes('return data'), 'canonical getReviewScopes 必须返回 data')
  // 不得出现 parent 过滤
  assert.doesNotMatch(fn, /parent_scope_type|parent_scope_key/, 'canonical params 不得含 parent 过滤')
})

test('A3. detail endpoint path 正确且包含 scope_type/scope_key', () => {
  const src = read('api.ts')
  assert.match(
    src,
    /export async function getReviewScopeDetail\([\s\S]*?`\/v1\/review\/\$\{tradeDate\}\/scopes\/\$\{scopeType\}\/\$\{scopeKey\}`/,
    'detail 必须请求 /v1/review/{tradeDate}/scopes/{scopeType}/{scopeKey}',
  )
})

test('A4. detail include_partial 透传', () => {
  const src = read('api.ts')
  assert.match(
    src,
    /getReviewScopeDetail[\s\S]*?include_partial: includePartial/,
    'detail 必须透传 include_partial',
  )
})

test('A5. 不出现 /api/api 双重前缀', () => {
  const src = read('api.ts')
  assert.doesNotMatch(src, /\/api\/api/, '不得出现 /api/api 重复前缀')
})

// ============================================================
// B. 类型 / fixtures：summary 可 null、所有数值字段可 null、phase 词表
// ============================================================

test('B1. ReviewScopeListItem.summary 允许为 null', () => {
  const sample: ReviewScopeListItem = {
    scopeType: 'industry_l1',
    scopeKey: 'bank',
    scopeName: '银行',
    readiness: 'ready',
    status: 'ready',
    eligibleCount: 42,
    providedCount: 42,
    coverageRatio: 1,
    summary: null,
  }
  assert.equal(sample.summary, null, 'Composition 缺失时 summary=null（不伪造全 0）')
})

test('B2. ReviewScopeSummary 所有数值字段接受 null', () => {
  const summary: ReviewScopeSummary = {
    dynamicsStatus: null,
    phase: null,
    position: null,
    velocity: null,
    acceleration: null,
    upperOccupancy: null,
    lowerOccupancy: null,
    equalWeightReturn: null,
    amountWeightedReturn: null,
    capitalTilt: null,
    advanceRatio: null,
    declineRatio: null,
    unchangedRatio: null,
    returnDispersion: null,
    priceNormalizedHhi: null,
    amountNormalizedHhi: null,
    leadershipStatus: null,
    jaccardStability: null,
    migration: null,
  }
  // 每个字段都应是 null（证明全部 nullable）
  for (const v of Object.values(summary)) {
    assert.equal(v, null)
  }
})

test('B3. phase 接受合法词表或 null', () => {
  const ok: Array<ReviewScopeSummary['phase']> = [
    null,
    'Early Lift',
    'Strengthening',
    'Sustained',
    'Decelerating',
    'Weakening',
    'Repairing',
  ]
  for (const p of ok) {
    const s: ReviewScopeSummary = { phase: p } as ReviewScopeSummary
    assert.equal(s.phase, p)
  }
})

test('B4. canonical list item 不要求 p/q/u/c/v/signalCount', () => {
  const item: ReviewScopeListItem = {
    scopeType: 'concept',
    scopeKey: 'ai',
    scopeName: null,
    readiness: 'insufficient_history',
    status: 'partial',
    eligibleCount: 10,
    providedCount: 5,
    coverageRatio: 0.5,
    summary: null,
  }
  assertMissingKeys(item, ['p', 'q', 'u', 'c', 'v', 'signalCount'])
})

test('B5. ReviewScopeListParams 不含 parent 过滤', () => {
  const params: ReviewScopeListParams = { scope_type: 'concept', page: 1, page_size: 50 }
  assertMissingKeys(params, ['parent_scope_type', 'parent_scope_key'])
})

test('B6. ReviewScopeListResponse 形状', () => {
  const resp: ReviewScopeListResponse = {
    items: [],
    total: 0,
    page: 1,
    page_size: 50,
    has_more: false,
  }
  assert.equal(resp.page_size, 50)
  assert.equal(resp.has_more, false)
})

test('B7. ReviewScopeCompositionDetailResponse 含 9-key composition + observation', () => {
  const detail: ReviewScopeCompositionDetailResponse = {
    reviewRunId: 'r1',
    tradeDate: '2026-08-21',
    scopeType: 'industry_l1',
    scopeKey: 'bank',
    scopeName: null,
    algorithmVersion: 'v1',
    composition: null,
    observation: null,
  }
  assert.equal(detail.composition, null)
  assert.equal(detail.observation, null)
})

// ============================================================
// C. Query keys：scopeDetail 因 identity 不同而不同
// ============================================================

test('C1. scopeDetail key 因 tradeDate 不同', () => {
  const a = reviewKeys.scopeDetail('2026-08-21', 'industry_l1', 'bank', false)
  const b = reviewKeys.scopeDetail('2026-08-22', 'industry_l1', 'bank', false)
  assert.notDeepEqual(a, b)
})

test('C2. scopeDetail key 因 scopeType 不同', () => {
  const a = reviewKeys.scopeDetail('2026-08-21', 'industry_l1', 'bank', false)
  const b = reviewKeys.scopeDetail('2026-08-21', 'concept', 'bank', false)
  assert.notDeepEqual(a, b)
})

test('C3. scopeDetail key 因 scopeKey 不同', () => {
  const a = reviewKeys.scopeDetail('2026-08-21', 'industry_l1', 'bank', false)
  const b = reviewKeys.scopeDetail('2026-08-21', 'industry_l1', 'tech', false)
  assert.notDeepEqual(a, b)
})

test('C4. scopeDetail key 因 includePartial 不同', () => {
  const a = reviewKeys.scopeDetail('2026-08-21', 'industry_l1', 'bank', false)
  const b = reviewKeys.scopeDetail('2026-08-21', 'industry_l1', 'bank', true)
  assert.notDeepEqual(a, b)
})

test('C5. canonical keys 暴露 dates/latest/overview/scopes/scopeDetail', () => {
  assert.deepEqual(reviewKeys.dates(), ['review', 'dates'])
  assert.deepEqual(reviewKeys.latest(), ['review', 'latest'])
  assert.deepEqual(reviewKeys.overview('2026-08-21', false), [
    'review',
    'overview',
    '2026-08-21',
    { includePartial: false },
  ])
  assert.deepEqual(reviewKeys.scopes('2026-08-21', { page: 1 }), [
    'review',
    'scopes',
    '2026-08-21',
    { page: 1 },
  ])
})

// ============================================================
// D. URL decode/encode 往返 + 默认值 + 非法值
// ============================================================

test('D1. 完整 canonical URL 编解码往返', () => {
  const state = {
    date: '2026-08-21',
    family: 'industry_l1' as const,
    scopeKey: 'bank',
    view: 'table' as const,
    tab: 'dynamics' as const,
    phase: 'Strengthening' as const,
    sort: 'velocity_desc' as const,
    page: 2,
    pageSize: 50,
    q: '有色',
  }
  const encoded = encodeReviewUrl(state)
  const decoded = decodeReviewUrl(encoded)
  assert.deepEqual(decoded, state)
})

test('D2. 默认状态编码为空 query（仅 /review）', () => {
  const encoded = encodeReviewUrl(defaultReviewUrlState())
  assert.equal(encoded.toString(), '')
})

test('D3. 缺失参数解码为默认值', () => {
  const d = decodeReviewUrl(new URLSearchParams(''))
  assert.deepEqual(d, defaultReviewUrlState())
})

test('D4. 非法 family → industry_l1', () => {
  assert.equal(normalizeFamily('bogus'), 'industry_l1')
  assert.equal(decodeReviewUrl(new URLSearchParams('family=bogus')).family, 'industry_l1')
})

test('D5. 非法 view → table', () => {
  assert.equal(normalizeExplorerView('bogus'), 'table')
  assert.equal(decodeReviewUrl(new URLSearchParams('view=bogus')).view, 'table')
})

test('D6. 非法 tab → dynamics', () => {
  assert.equal(normalizeDetailTab('bogus'), 'dynamics')
  assert.equal(decodeReviewUrl(new URLSearchParams('tab=bogus')).tab, 'dynamics')
})

test('D7. 非法 phase → null（不 fallback 映射）', () => {
  assert.equal(normalizePhase('bogus'), null)
  assert.equal(decodeReviewUrl(new URLSearchParams('phase=bogus')).phase, null)
})

test('D8. 非法 sort → velocity_desc', () => {
  assert.equal(normalizeSort('bogus'), 'velocity_desc')
  assert.equal(decodeReviewUrl(new URLSearchParams('sort=bogus')).sort, 'velocity_desc')
})

test('D9. 非法 page → 1；越界 page → 1', () => {
  assert.equal(normalizePage('bogus'), 1)
  assert.equal(normalizePage('0'), 1)
  assert.equal(normalizePage('-3'), 1)
  assert.equal(decodeReviewUrl(new URLSearchParams('page=0')).page, 1)
})

test('D10. 非法 pageSize → 50；超 100 → 100', () => {
  assert.equal(normalizePageSize('bogus'), 50)
  assert.equal(normalizePageSize('999'), 100)
  assert.equal(normalizePageSize('0'), 50)
  assert.equal(decodeReviewUrl(new URLSearchParams('pageSize=999')).pageSize, 100)
})

test('D11. Unicode q 往返', () => {
  const enc = encodeReviewUrl({ ...defaultReviewUrlState(), q: '有色金属' })
  assert.equal(decodeReviewUrl(enc).q, '有色金属')
})

test('D12. scopeName 不是 canonical URL 状态字段', () => {
  // canonical ReviewUrlState 不应含 scopeName；旧字段走 legacy
  const d = decodeReviewUrl(new URLSearchParams('scopeName=银行'))
  assertMissingKeys(d, ['scopeName'])
})

// ============================================================
// E. 语义禁止：canonical list 类型不含 p/q/u/c/v/signalCount
// ============================================================

test('E1. canonical ReviewScopeListItem 类型定义不含 p/q/u/c/v/signalCount', () => {
  const src = read('types.ts')
  const m = src.match(
    /export interface ReviewScopeListItem \{[\s\S]*?\n\}/,
  )
  assert.ok(m, '应存在 ReviewScopeListItem 接口定义')
  const body = m[0]
  for (const k of ['p:', 'q:', 'u:', 'c:', 'v:', 'signalCount:']) {
    assert.doesNotMatch(body, new RegExp(`\\b${k}`), `canonical list item 不得含字段 ${k}`)
  }
})

test('E2. canonical ReviewScopeSummary 不含 p/q/u/c/v/signalCount', () => {
  const src = read('types.ts')
  const m = src.match(/export interface ReviewScopeSummary \{[\s\S]*?\n\}/)
  assert.ok(m, '应存在 ReviewScopeSummary 接口定义')
  for (const k of ['p:', 'q:', 'u:', 'c:', 'v:', 'signalCount:']) {
    assert.doesNotMatch(m[0], new RegExp(`\\b${k}`), `canonical summary 不得含字段 ${k}`)
  }
})

test('E3. ReviewScopeListParams 不含 parent_scope_type/parent_scope_key', () => {
  const src = read('types.ts')
  const m = src.match(/export interface ReviewScopeListParams \{[\s\S]*?\n\}/)
  assert.ok(m, '应存在 canonical ReviewScopeListParams 接口定义')
  assert.doesNotMatch(m[0], /parent_scope_type|parent_scope_key/)
})

test('E4. ReviewDynamicsPhase 仅 6 个合法值，无第七 fallback', () => {
  const src = read('types.ts')
  const m = src.match(/export type ReviewDynamicsPhase =[\s\S]*?\| 'Repairing'/)
  assert.ok(m, '应存在 ReviewDynamicsPhase 联合类型')
  const count = (m[0].match(/'/g) ?? []).length
  assert.equal(count, 6 * 2, '应恰好 6 个 phase 字面量')
  assert.doesNotMatch(m[0], /unknown|fallback|other/i)
})

// ============================================================
// F. ViewModel 格式化（null → —，不伪造 0）
// ============================================================

test('F1. null/undefined → — 占位符', () => {
  assert.equal(formatPercentNullable(null), '—')
  assert.equal(formatPercentNullable(undefined), '—')
  assert.equal(formatNumberNullable(null), '—')
  assert.equal(formatPhaseLabel(null), '—')
})

test('F2. 百分比与数字正常格式化', () => {
  assert.equal(formatPercentNullable(0.123), '12.3%')
  assert.equal(formatNumberNullable(1.5), '1.50')
})

test('F3. 0 是合法值，不应被显示成占位符', () => {
  assert.equal(formatPercentNullable(0), '0.0%')
  assert.equal(formatNumberNullable(0), '0.00')
})
