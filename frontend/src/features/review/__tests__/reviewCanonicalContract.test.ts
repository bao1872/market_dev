// [Slice C] Canonical Review Scope 合同测试（纯 TS，tsx --test 可跑）。
// 覆盖：API 路径、类型/nullable 语义、query key 差异、URL 编解码往返、语义禁止项。
// 不涉及 React 渲染 / SCSS；禁止引入新 test framework。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import { readFileSync, existsSync } from 'node:fs'
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
  normalizeReadiness,
  normalizeSort,
  normalizePage,
  normalizePageSize,
  withReviewDateChange,
  withReviewFamilyChange,
  withReviewFilterChange,
  withReviewPageChange,
} from '../urlState'
import type {
  ReviewScopeListItem,
  ReviewScopeSummary,
  ReviewScopeListParams,
  ReviewScopeListResponse,
  ReviewScopeCompositionDetailResponse,
  ReviewScopeComposition,
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
    observationSummary: null,
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
    observationSummary: null,
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

test('B7. ReviewScopeCompositionDetailResponse 含 observation + observationGroups（R3A Fact-first）', () => {
  const detail: ReviewScopeCompositionDetailResponse = {
    reviewRunId: 'r1',
    tradeDate: '2026-08-21',
    scopeType: 'industry_l1',
    scopeKey: 'bank',
    scopeName: null,
    algorithmVersion: 'v1',
    observation: { price: { return_level: 0.5 } },
    memberDirectory: {},
    observationGroups: {
      price_capital: { group_key: 'price_capital', label: '涨跌与成交', facts: {} },
      trend_state: { group_key: 'trend_state', label: '趋势方向与强度', facts: {} },
      trend_progress: { group_key: 'trend_progress', label: '趋势进展', facts: {} },
      trend_volume_confirmation: { group_key: 'trend_volume_confirmation', label: '趋势与量能', facts: {} },
      structure_break_turn: { group_key: 'structure_break_turn', label: '结构突破与转折', facts: {} },
      structure_evolution_position: { group_key: 'structure_evolution_position', label: '结构演化与位置', facts: {} },
      momentum_squeeze_release: { group_key: 'momentum_squeeze_release', label: '压缩与释放', facts: {} },
      volume_anomaly: { group_key: 'volume_anomaly', label: '量能异常', facts: {} },
    },
    composition: null,
    history: null,
    crossSection: null,
  }
  assert.equal(detail.composition, null)
  assert.ok(detail.observation !== null)
  assert.equal(Object.keys(detail.observationGroups).length, 8)
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
    readiness: 'ready' as const,
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

test('D6. [R3A] 非法 tab → dsa（默认研究 tab）', () => {
  assert.equal(normalizeDetailTab('bogus'), 'dsa')
  assert.equal(decodeReviewUrl(new URLSearchParams('tab=bogus')).tab, 'dsa')
})

// ============================================================
// [R1] Current tab URL 合同（CURRENT-1/2）
// ============================================================

test('CURRENT-1. current tab 被 URL decoder/encoder 接受并往返', () => {
  const state = { ...defaultReviewUrlState(), tab: 'current' as const }
  const enc = encodeReviewUrl(state)
  assert.equal(decodeReviewUrl(enc).tab, 'current', 'current 经编解码往返保留')
  assert.equal(normalizeDetailTab('current'), 'current', 'normalizeDetailTab 接受 current')
})

test('CURRENT-2. [R3A] 默认 detail tab 为 dsa', () => {
  assert.equal(defaultReviewUrlState().tab, 'dsa', '默认 tab = dsa')
  assert.equal(normalizeDetailTab(undefined), 'dsa')
  assert.equal(decodeReviewUrl(new URLSearchParams('')).tab, 'dsa')
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

test('D13. readiness valid/invalid/null roundtrip', () => {
  // 合法值 roundtrip
  for (const r of ['ready', 'insufficient_history', 'unavailable_current']) {
    assert.equal(normalizeReadiness(r), r)
    const enc = encodeReviewUrl({ ...defaultReviewUrlState(), readiness: r as never })
    assert.equal(decodeReviewUrl(enc).readiness, r, `readiness=${r} roundtrip`)
  }
  // 非法 → null
  assert.equal(normalizeReadiness('bogus'), null)
  assert.equal(normalizeReadiness(null), null)
  assert.equal(decodeReviewUrl(new URLSearchParams('readiness=bogus')).readiness, null)
  // 缺失 → null（不编码）
  assert.equal(defaultReviewUrlState().readiness, null)
  assert.equal(encodeReviewUrl(defaultReviewUrlState()).toString(), '')
})

test('D14. 过滤类 URL 变化重置页码为 1（保留 scopeKey）', () => {
  const base = { ...defaultReviewUrlState(), page: 3, scopeKey: 'bank' }
  const next = withReviewFilterChange(base, { phase: 'Strengthening' as never })
  assert.equal(next.page, 1, 'q/phase/readiness 变化必须重置页码')
  assert.equal(next.scopeKey, 'bank', '过滤变化不得清除 scopeKey')
  assert.equal(next.phase, 'Strengthening')
  // pageSize 变化同样重置页码
  assert.equal(withReviewFilterChange(base, { pageSize: 100 }).page, 1)
})

test('D15. 日期变化清除 scopeKey 并重置页码（保留 family）', () => {
  const base = {
    ...defaultReviewUrlState(),
    family: 'concept' as const,
    scopeKey: 'ai',
    page: 4,
  }
  const next = withReviewDateChange(base, '2026-08-22')
  assert.equal(next.date, '2026-08-22')
  assert.equal(next.scopeKey, null, '日期变化必须清除 scopeKey')
  assert.equal(next.page, 1, '日期变化必须重置页码')
  assert.equal(next.family, 'concept', '日期变化保留当前 family')
})

test('D16. family 变化设置 family、清除 scopeKey、重置页码', () => {
  const base = {
    ...defaultReviewUrlState(),
    family: 'industry_l1' as const,
    scopeKey: 'bank',
    page: 3,
  }
  const next = withReviewFamilyChange(base, 'concept')
  assert.equal(next.family, 'concept')
  assert.equal(next.scopeKey, null, 'family 变化必须清除 scopeKey')
  assert.equal(next.page, 1, 'family 变化必须重置页码')
})

test('D17. 翻页只改 page，保留全部其他状态（与过滤重置语义分离）', () => {
  const base = {
    ...defaultReviewUrlState(),
    family: 'concept' as const,
    scopeKey: 'ai',
    view: 'trajectory' as const,
    tab: 'dynamics' as const,
    phase: 'Strengthening' as const,
    readiness: 'ready' as const,
    sort: 'velocity_desc' as const,
    pageSize: 100,
    q: '有色',
    page: 1,
  }
  const next = withReviewPageChange(base, 2)
  assert.equal(next.page, 2, '翻页必须更新 page')
  // 其余状态必须原样保留
  assert.equal(next.family, 'concept')
  assert.equal(next.scopeKey, 'ai')
  assert.equal(next.view, 'trajectory')
  assert.equal(next.tab, 'dynamics')
  assert.equal(next.phase, 'Strengthening')
  assert.equal(next.readiness, 'ready')
  assert.equal(next.sort, 'velocity_desc')
  assert.equal(next.pageSize, 100)
  assert.equal(next.q, '有色')
  // 上一页：page 3 → 2
  assert.equal(withReviewPageChange({ ...base, page: 3 }, 2).page, 2)
  // 非法页码（<1）钳制到 1
  assert.equal(withReviewPageChange(base, 0).page, 1)
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

// ============================================================
// G. [Slice C 修复] Composition / Detail contract 真实性
// ============================================================

test('G1. ReviewScopeComposition.scope 仅含 scope_type / scope_key', () => {
  const comp: ReviewScopeComposition = {
    scope: { scope_type: 'industry_l1', scope_key: 'bank' },
    trade_date: '2026-08-21',
    capability: {},
    scope_observation: null,
    historical_dynamics: null,
    internal_structure_facts: null,
    leadership: null,
    member_attribution: null,
    composition_readiness: 'ready',
  }
  // scope 内不得有 scope_name / trade_date
  assertMissingKeys(comp.scope, ['scope_name', 'trade_date'])
  assert.equal(comp.trade_date, '2026-08-21')
  // 9-key 顶层齐全
  assertMissingKeys(comp, [])
  for (const k of [
    'scope',
    'trade_date',
    'capability',
    'scope_observation',
    'historical_dynamics',
    'internal_structure_facts',
    'leadership',
    'member_attribution',
    'composition_readiness',
  ]) {
    assert.ok(k in comp, `composition 必须含顶层 key ${k}`)
  }
})

test('G2. composition_readiness 接受 canonical readiness 字符串', () => {
  const ok: Array<ReviewScopeComposition['composition_readiness']> = [
    'ready',
    'insufficient_history',
    'unavailable_current',
  ]
  for (const r of ok) {
    const comp: ReviewScopeComposition = {
      scope: { scope_type: 'concept', scope_key: 'ai' },
      trade_date: '2026-08-21',
      capability: {},
      scope_observation: null,
      historical_dynamics: null,
      internal_structure_facts: null,
      leadership: null,
      member_attribution: null,
      composition_readiness: r,
    }
    assert.equal(comp.composition_readiness, r)
  }
})

test('G3. composition 非必产层保持 nullable', () => {
  // 非必产层允许同时为 null（owner 在不可产时返回 None）
  const compNullable: ReviewScopeComposition = {
    scope: { scope_type: 'industry_l1', scope_key: 'bank' },
    trade_date: '2026-08-21',
    capability: {},
    scope_observation: null,
    historical_dynamics: null,
    internal_structure_facts: null,
    leadership: null,
    member_attribution: null,
    composition_readiness: 'unavailable_current',
  }
  assert.equal(compNullable.scope_observation, null)
  assert.equal(compNullable.historical_dynamics, null)
  assert.equal(compNullable.internal_structure_facts, null)
  assert.equal(compNullable.leadership, null)
  assert.equal(compNullable.member_attribution, null)
  // 同一字段也可承载非 null 对象（status 携带）
  const compFilled: ReviewScopeComposition = {
    ...compNullable,
    scope_observation: { status: 'ready' },
    leadership: {
      status: 'insufficient_history',
      reason: null,
      coverage: null,
      previous_direction: null,
      current_direction: null,
      previous_rankable_count: null,
      current_rankable_count: null,
      previous_leader_count: null,
      current_leader_count: null,
      retained_count: null,
      entrant_count: null,
      exit_count: null,
      previous_retention: null,
      jaccard_stability: null,
      migration: null,
      previous_leader_ids: null,
      current_leader_ids: null,
      entrant_ids: null,
      exit_ids: null,
    },
  }
  assert.equal(typeof compFilled.scope_observation, 'object')
  assert.equal(typeof compFilled.leadership, 'object')
})

test('G4. Detail observation 是原始 payload（Record<string, unknown>），非 ReviewScopeSummary', () => {
  const detail: ReviewScopeCompositionDetailResponse = {
    reviewRunId: 'r1',
    tradeDate: '2026-08-21',
    scopeType: 'industry_l1',
    scopeKey: 'bank',
    scopeName: null,
    algorithmVersion: 'v1',
    observation: { price: { return_level: 0.5 }, trend: {}, participation: {} },
    memberDirectory: {},
    observationGroups: {
      price_capital: { group_key: 'price_capital', label: '涨跌与成交', facts: {} },
      trend_state: { group_key: 'trend_state', label: '趋势方向与强度', facts: {} },
      trend_progress: { group_key: 'trend_progress', label: '趋势进展', facts: {} },
      trend_volume_confirmation: { group_key: 'trend_volume_confirmation', label: '趋势与量能', facts: {} },
      structure_break_turn: { group_key: 'structure_break_turn', label: '结构突破与转折', facts: {} },
      structure_evolution_position: { group_key: 'structure_evolution_position', label: '结构演化与位置', facts: {} },
      momentum_squeeze_release: { group_key: 'momentum_squeeze_release', label: '压缩与释放', facts: {} },
      volume_anomaly: { group_key: 'volume_anomaly', label: '量能异常', facts: {} },
    },
    composition: null,
    history: null,
    crossSection: null,
  }
  // observation 接受任意原始 payload（成功响应中 NON-NULL）
  assert.equal(typeof detail.observation, 'object')
  assert.ok(detail.observation !== null)
  // observationGroups 是 8 组 canonical L2 projection
  assert.equal(Object.keys(detail.observationGroups).length, 8)
})

test('G5. canonical scopes key 接受 canonical params（无 cast）', () => {
  const key = reviewKeys.scopes('2026-08-21', { scope_type: 'concept', page: 1, page_size: 50 })
  assert.deepEqual(key, [
    'review',
    'scopes',
    '2026-08-21',
    { scope_type: 'concept', page: 1, page_size: 50 },
  ])
})

test('G6. canonical ReviewScopeComposition 类型定义不含 scope_name / 嵌套 trade_date', () => {
  const src = read('types.ts')
  const m = src.match(/export interface ReviewScopeComposition \{[\s\S]*?\n\}/)
  assert.ok(m, '应存在 ReviewScopeComposition 接口定义')
  const body = m[0]
  // scope 块内不得含 scope_name
  const scopeBlock = body.match(/scope: \{[\s\S]*?\}/)
  assert.ok(scopeBlock, 'scope 应为对象块')
  assert.doesNotMatch(scopeBlock[0], /scope_name/, 'scope 内不得含 scope_name')
  // 顶层不得有 scope_name
  assert.doesNotMatch(body, /^\s*scope_name:/m, 'composition 顶层不得有 scope_name')
  // composition_readiness 不得是 Record<string, unknown>
  assert.doesNotMatch(body, /composition_readiness:\s*Record<string, unknown>/, 'composition_readiness 不得是 Record<string, unknown>')
})

test('G7. Detail observation 类型定义为原始 payload，非 ReviewScopeSummary', () => {
  const src = read('types.ts')
  const m = src.match(/export interface ReviewScopeCompositionDetailResponse \{[\s\S]*?\n\}/)
  assert.ok(m, '应存在 ReviewScopeCompositionDetailResponse 接口定义')
  assert.doesNotMatch(
    m[0],
    /observation:\s*ReviewScopeSummary/,
    'detail.observation 不得被定义为 ReviewScopeSummary',
  )
  assert.match(
    m[0],
    /observation:\s*Record<string, unknown>/,
    'detail.observation 应为 Record<string, unknown>（R3A 成功响应中 NON-NULL）',
  )
})

// ============================================================
// H. [Slice F] Legacy Retirement 终局断言
// ============================================================

test('H1. reviewKeys 不含 legacy 键（legacyScopes/signals/signal/attributions/instruments/trackings/tracking/evaluations/discoveries/discovery）', () => {
  const src = read('queryKeys.ts')
  for (const k of [
    'legacyScopes',
    'signals',
    'signal',
    'attributions',
    'instruments',
    'trackings',
    'tracking',
    'evaluations',
    'discoveries',
    'discovery',
  ]) {
    assert.doesNotMatch(src, new RegExp(`reviewKeys\\.${k}`), `reviewKeys 不应暴露 ${k} 键`)
    assert.doesNotMatch(src, new RegExp(`\\b${k}\\s*:`), `queryKeys 不应定义 ${k}`)
  }
})

test('H2. api.ts 不导出 legacy 用户侧函数', () => {
  const src = read('api.ts')
  const removed = [
    'getLegacyReviewScopes',
    'getReviewSignals',
    'getReviewSignal',
    'getSignalAttributions',
    'getSignalInstruments',
    'getReviewTrackings',
    'createReviewTracking',
    'updateReviewTracking',
    'closeReviewTracking',
    'getTrackingEvaluations',
    'getDiscoveries',
    'getDiscoveryDetail',
  ]
  for (const name of removed) {
    assert.doesNotMatch(
      src,
      new RegExp(`export (async )?function ${name}\\b`),
      `api.ts 不应导出 ${name}`,
    )
  }
})

test('H3. api.ts 不含 retired Review 路径字面量（/signals /trackings /discoveries）', () => {
  const src = read('api.ts')
  // 检查字面量 '/signals' '/trackings' '/discoveries' 均不作为 endpoint 出现
  assert.doesNotMatch(
    src,
    /['"`]\/v1\/review\/[^'"`]*\/signals[^'"`]*['"`]/,
    'api.ts 不得请求 /signals 路径',
  )
  assert.doesNotMatch(
    src,
    /['"`]\/v1\/review\/[^'"`]*\/trackings[^'"`]*['"`]/,
    'api.ts 不得请求 /trackings 路径',
  )
  assert.doesNotMatch(
    src,
    /['"`]\/v1\/review\/[^'"`]*\/discoveries[^'"`]*['"`]/,
    'api.ts 不得请求 /discoveries 路径',
  )
})

test('H4. urlState.ts 不含 legacy URL 合同/helpers', () => {
  const src = read('urlState.ts')
  for (const name of [
    'LegacyReviewUrlState',
    'decodeLegacyReviewUrl',
    'encodeLegacyReviewUrl',
    'normalizeLegacyStage',
    'normalizeLegacyView',
    'normalizeLegacyTrackingTab',
    'REVIEW_FORMAL_STAGES',
    'DEFAULT_LEGACY_REVIEW_VIEW',
    'DEFAULT_LEGACY_REVIEW_STAGE',
    'DEFAULT_LEGACY_TRACKING_TAB',
    'normalizeStage',
    'normalizeView',
  ]) {
    assert.doesNotMatch(src, new RegExp(`\\b${name}\\b`), `urlState.ts 不应含 ${name}`)
  }
})

test('H5. canonical ReviewPage 仍 import/render ScopeExplorerWorkspace，且不 import 已删除 legacy 组件', () => {
  const src = read('../../pages/ReviewPage.tsx')
  assert.match(
    src,
    /import.*ScopeExplorerWorkspace.*from/,
    'ReviewPage 仍需 import ScopeExplorerWorkspace',
  )
  for (const deleted of [
    'MarketScanPanel',
    'ScopeMetricsTable',
    'ReviewStageNav',
    'SignalCard',
    'BoardAttributionPanel',
    'AttributionTable',
    'InstrumentsPanel',
    'StockValidationPanel',
    'TrackingReviewPanel',
    'DiscoveryWorkspace',
    'DiscoveryCard',
    'DiscoveryDetail',
    'FilterDiscoveryPanel',
    'EvidenceDrawer',
    'StatePanel',
    'RelatedScopesPanel',
    'RankKeyPanel',
  ]) {
    assert.doesNotMatch(src, new RegExp(`import.*${deleted}`), `ReviewPage 不应再 import ${deleted}`)
  }
})

test('H6. canonical Scope 类型仍编译并保留关键字段', () => {
  // 引用类型以证明仍存在且可编译
  const item: ReviewScopeListItem = {
    scopeType: 'industry_l1',
    scopeKey: 'bank',
    scopeName: null,
    readiness: 'ready',
    status: 'ready',
    eligibleCount: 10,
    providedCount: 10,
    coverageRatio: 1,
    summary: null,
    observationSummary: null,
  }
  assert.ok(item.scopeKey)
  const list: ReviewScopeListResponse = {
    items: [],
    total: 0,
    page: 1,
    page_size: 50,
    has_more: false,
  }
  assert.equal(list.total, 0)
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
  assert.equal(summary.phase, null)
  const detail: ReviewScopeCompositionDetailResponse = {
    reviewRunId: 'r1',
    tradeDate: '2026-08-21',
    scopeType: 'industry_l1',
    scopeKey: 'bank',
    scopeName: null,
    algorithmVersion: 'v1',
    observation: { price: { return_level: 0.5 } },
    memberDirectory: {},
    observationGroups: {
      price_capital: { group_key: 'price_capital', label: '涨跌与成交', facts: {} },
      trend_state: { group_key: 'trend_state', label: '趋势方向与强度', facts: {} },
      trend_progress: { group_key: 'trend_progress', label: '趋势进展', facts: {} },
      trend_volume_confirmation: { group_key: 'trend_volume_confirmation', label: '趋势与量能', facts: {} },
      structure_break_turn: { group_key: 'structure_break_turn', label: '结构突破与转折', facts: {} },
      structure_evolution_position: { group_key: 'structure_evolution_position', label: '结构演化与位置', facts: {} },
      momentum_squeeze_release: { group_key: 'momentum_squeeze_release', label: '压缩与释放', facts: {} },
      volume_anomaly: { group_key: 'volume_anomaly', label: '量能异常', facts: {} },
    },
    composition: null,
    history: null,
    crossSection: null,
  }
  assert.equal(detail.composition, null)
  assert.ok(detail.observation !== null)
})

test('H7. AuctionBackflowPanel 仍存在且未被 Slice F 修改', () => {
  const src = read('AuctionBackflowPanel.tsx')
  assert.ok(src.length > 0, 'AuctionBackflowPanel.tsx 必须仍存在且非空')
  assert.match(src, /export default function AuctionBackflowPanel|export function AuctionBackflowPanel|AuctionBackflowPanel/, 'AuctionBackflowPanel 仍导出 AuctionBackflowPanel')
})

test('H8. types.ts 不含 legacy 类型家族（Legacy/ReviewSignal/ReviewAttribution/ReviewInstrument/ReviewTracking/Discovery 家族等）', () => {
  const src = read('types.ts')
  const removed = [
    'LegacyReviewMetricComponent',
    'LegacyReviewMetricReadiness',
    'LegacyReviewMetricPayload',
    'LegacyReviewScopeMetrics',
    'LegacyReviewScopeListResponse',
    'LegacyReviewScopeListParams',
    'LegacyMetricKey',
    'ReviewSignalStatus',
    'ReviewSignal\\b',
    'ReviewSignalListResponse',
    'ReviewSignalListParams',
    'ReviewBoardRole',
    'ReviewRelationToScope',
    'ReviewAttribution\\b',
    'ReviewAttributionListResponse',
    'ReviewAttributionListParams',
    'ReviewInstrument\\b',
    'ReviewInstrumentListResponse',
    'ReviewInstrumentListParams',
    'ReviewInstrumentV2',
    'ReviewTrackingType',
    'ReviewTrackingStatus',
    'ReviewTracking\\b',
    'ReviewTrackingListResponse',
    'ReviewTrackingListParams',
    'ReviewTrackingCreateRequest',
    'ReviewTrackingPatchRequest',
    'ReviewTrackingEvaluation',
    'ReviewTrackingEvaluationListResponse',
    'DiscoveryMetricState',
    'DiscoveryMetricChange',
    'DiscoveryConcentrationState',
    'DiscoveryConcentrationChange',
    'DiscoveryInternalStructure',
    'DiscoveryState',
    'DiscoveryChange',
    'DiscoveryAnomaly',
    'DiscoveryScope',
    'DiscoveryRelatedScope',
    'DiscoveryRepresentativeInstrument',
    'DiscoveryLifecycle',
    'DiscoveryDataQuality',
    'DiscoveryRankKey',
    'Discovery\\b',
    'DiscoveryListResponse',
    'DiscoveryDetailResponse',
    'ReviewStage\\b',
    'TrackingTab\\b',
  ]
  for (const name of removed) {
    assert.doesNotMatch(
      src,
      new RegExp(`export (interface|type) ${name}`),
      `types.ts 不应导出 ${name}`,
    )
  }
})

test('H9. 已删除 legacy 组件文件不再存在（物理删除）', () => {
  for (const file of [
    'MarketScanPanel.tsx',
    'ScopeMetricsTable.tsx',
    'ReviewStageNav.tsx',
    'SignalCard.tsx',
    'BoardAttributionPanel.tsx',
    'AttributionTable.tsx',
    'InstrumentsPanel.tsx',
    'ReviewInstrumentTable.tsx',
    'StockValidationPanel.tsx',
    'TrackingReviewPanel.tsx',
    'DiscoveryWorkspace.tsx',
    'DiscoveryCard.tsx',
    'DiscoveryDetail.tsx',
    'FilterDiscoveryPanel.tsx',
    'EvidenceDrawer.tsx',
    'StatePanel.tsx',
    'RelatedScopesPanel.tsx',
    'RankKeyPanel.tsx',
    'reviewReadiness.ts',
  ]) {
    const exists = existsSync(join(REVIEW_DIR, file))
    assert.ok(!exists, `legacy 文件 ${file} 应已物理删除`)
  }
})
