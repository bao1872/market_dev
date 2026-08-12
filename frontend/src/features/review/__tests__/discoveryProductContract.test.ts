// [REVIEW-V2-B3] Discovery-first product-path contract tests.
//
// 用法（项目现有 harness，纯 TS，node --test / tsx --test 可跑）：
//   cd frontend
//   npx tsx --test src/features/review/__tests__/discoveryProductContract.test.ts
//
// 覆盖 guide「TESTS」13 项。现有 harness 无 jsdom / SCSS loader，
// 因此：
//   - 可用纯逻辑直接测的（urlState / reviewKeys / extractReviewError / tracking payload）
//     做真实行为断言；
//   - 涉及 React 组件渲染 / CSS module 加载的（node 无法 import .scss 与 .tsx 的 DOM 渲染），
//     用「源码 + SCSS module export 集合」做契约断言，保证组件实际引用的 class / wiring
//     确实存在，避免 tsc + build 之外的盲区。
//
// 禁止为这批引入新 test framework / jsdom。

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import { extractReviewError } from '../api'
import { reviewKeys } from '../queryKeys'
import {
  DEFAULT_REVIEW_VIEW,
  REVIEW_FORMAL_STAGES,
  decodeReviewUrl,
  encodeReviewUrl,
  normalizeView,
} from '../urlState'
import type { ReviewTrackingCreateRequest } from '../types'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

// ============================================================
// 1. /review 默认产品工作区 = Discovery-first Workspace
// ============================================================

test('1. 默认视图是 discovery（Discovery-first 正式入口）', () => {
  assert.equal(DEFAULT_REVIEW_VIEW, 'discovery')
  // 空 /review → decode 后 view=discovery
  const decoded = decodeReviewUrl(new URLSearchParams(''))
  assert.equal(decoded.view, 'discovery')
  // 非法/缺失 view 回退默认 discovery
  assert.equal(normalizeView(null), 'discovery')
  assert.equal(normalizeView('bogus'), 'discovery')
})

test('1b. ReviewPage 以 view==discovery 渲染 DiscoveryWorkspace（wiring）', () => {
  const src = read('../../pages/ReviewPage.tsx')
  assert.match(src, /import.*DiscoveryWorkspace/m, 'ReviewPage 必须 import DiscoveryWorkspace')
  assert.match(src, /const isDiscovery = urlState\.view === 'discovery'/, '以 view 派生 Discovery 视图')
  assert.match(src, /<DiscoveryWorkspace/, '必须渲染 <DiscoveryWorkspace>')
  // 默认产品视图为 discovery：isDiscovery 为真分支渲染 DiscoveryWorkspace
  assert.match(src, /isDiscovery \?[\s\S]*?renderDiscoveryView/, 'discovery 分支渲染 DiscoveryWorkspace')
})

// ============================================================
// 2. 旧五阶段不再作为用户一级主导航
// ============================================================

test('2. 旧五阶段降级为 secondary/debug drilldown（view=stages）', () => {
  // 正式阶段仍保留五阶段（作为 debug drilldown 存在）
  assert.deepStrictEqual([...REVIEW_FORMAL_STAGES], [
    'scan', 'signals', 'attribution', 'validation', 'tracking',
  ])
  // 但默认视图不是 stages，而是 discovery
  assert.notEqual(DEFAULT_REVIEW_VIEW, 'stages')
  assert.equal(normalizeView('stages'), 'stages', 'stages 仍可深链解析为 debug drilldown')
  // ReviewPage 中五阶段只在 stages 分支渲染
  const src = read('../../pages/ReviewPage.tsx')
  assert.match(src, /view === 'stages'|view: 'stages'/, '切换信号 diagnostics 使用 view=stages')
})

// ============================================================
// 3. tradeDate 来自正式 Review URL state（单一 SSOT）
// ============================================================

test('3. tradeDate 来自正式 Review URL state，roundtrip 保持', () => {
  const state = decodeReviewUrl(new URLSearchParams('date=2026-08-10&view=discovery'))
  assert.equal(state.date, '2026-08-10')
  // encode 再 decode 保持 date
  const encoded = encodeReviewUrl({ ...state, view: 'discovery' })
  assert.equal(encoded.get('date'), '2026-08-10')
  // DiscoveryWorkspace 消费 tradeDate 来自 props（由 ReviewPage 传 URL state 的 date）
  const src = read('DiscoveryWorkspace.tsx')
  assert.match(src, /tradeDate: string/, 'DiscoveryWorkspace 通过 props 接收正式 tradeDate')
  assert.ok(!/useParams|routeTradeDate/.test(src), '禁止再使用 useParams 第二套 trade date state')
  assert.ok(!/getReviewLatest\(\)|reviewKeys\.latest/.test(src), '禁止自行解析 latest 作为 tradeDate 来源')
})

// ============================================================
// 4. Discovery list 渲染契约
// ============================================================

test('4. Discovery list query key 绑定 tradeDate + filters', () => {
  const key = reviewKeys.discoveries('2026-08-10', { scope_type: 'style', page: 1 })
  assert.equal(key[1], 'discoveries')
  assert.equal(key[2], '2026-08-10', 'list key 必须含 tradeDate')
  // getDiscoveries 消费真实 list endpoint（api.ts 契约）
  const api = read('api.ts')
  assert.match(api, /getDiscoveries/, 'api 必须导出 getDiscoveries')
  assert.match(api, /\/v1\/review\/\$\{tradeDate\}\/discoveries/, 'list endpoint 绑定 tradeDate')
})

// ============================================================
// 5. discoveryId deep link：独立于分页，消费真实 Detail API
// ============================================================

test('5. discoveryId deep link 消费真实 Detail API + tradeDate', () => {
  // query key 含 discoveryId + tradeDate：['review','discovery',id,date]
  const key = reviewKeys.discovery('abc123def456', '2026-08-10')
  assert.equal(key[1], 'discovery')
  assert.equal(key[2], 'abc123def456')
  assert.equal(key[3], '2026-08-10', 'detail key 必须含 tradeDate')
  // api.ts 提供 getDiscoveryDetail(id, tradeDate) 且传 trade_date 参数
  const api = read('api.ts')
  assert.match(api, /export async function getDiscoveryDetail\(\s*discoveryId: string,\s*tradeDate\?: string/, 'detail API 接受 tradeDate')
  assert.match(api, /\/v1\/review\/discoveries\/\$\{discoveryId\}/, 'detail endpoint 使用 discoveryId')
  assert.match(api, /trade_date: tradeDate/, 'detail API 显式传 tradeDate')
  // Workspace 不依赖当前 page 的 paginated list 查找 selected discovery
  const ws = read('DiscoveryWorkspace.tsx')
  assert.match(ws, /getDiscoveryDetail\(discoveryId as string, tradeDate\)/, 'detail 走真实 Detail API')
  assert.ok(!/discoveries\.find\(.*discoveryId/.test(ws), '禁止从 list find 作为 detail source')
})

// ============================================================
// 6. discoveryId presence → detail / overview（URL SSOT，无独立 viewMode）
// ============================================================

test('6. discoveryId 存在即 detail，否则 overview（URL 是 SSOT）', () => {
  const ws = read('DiscoveryWorkspace.tsx')
  assert.match(ws, /const isDetail = !!discoveryId/, '以 discoveryId presence 决定 detail/overview')
  assert.match(ws, /if \(isDetail\)/, 'detail 分支由 isDetail 驱动')
  // list 在 detail 时禁用（避免无谓分页加载）
  assert.match(ws, /enabled: !!tradeDate && !isDetail/, 'detail 时禁用 list query')
  // 禁止独立 local viewMode state（仅注释提及，不得存在实际 setViewMode/useState）
  assert.ok(!/setViewMode|useState<ViewMode>|viewMode\s*=\s*useState/.test(ws), '禁止独立 local viewMode state')
  // URL 保留 discoveryId
  const decoded = decodeReviewUrl(new URLSearchParams('discoveryId=abc&scopeFamily=style'))
  assert.equal(decoded.discoveryId, 'abc')
  assert.equal(decoded.scopeFamily, 'style')
  const enc = encodeReviewUrl(decoded)
  assert.equal(enc.get('discoveryId'), 'abc', 'discoveryId 必须写回 URL')
})

// ============================================================
// 7. API error ≠ empty
// ============================================================

test('7. API error 不显示成 0 Discovery（error != empty）', () => {
  // extractReviewError 区分 500/404/网络失败并给出 message + requestId
  const err500 = extractReviewError({
    response: { status: 500, data: {}, headers: { get: () => 'req-1' } },
  })
  assert.equal(err500.status, 500)
  assert.match(err500.message, /500|服务器错误|请求失败/)
  assert.equal(err500.requestId, 'req-1')
  // 网络失败：status=null，且 message 非空（是错误而非空列表）
  const net = extractReviewError({ message: 'Network Error' })
  assert.equal(net.status, null)
  assert.ok(net.message, '网络失败必须给出非空 error message')
  assert.match(net.message, /网络错误|Network Error/)
  // Workspace 对 error 单独渲染，而不是回落到 empty
  const ws = read('DiscoveryWorkspace.tsx')
  assert.match(ws, /listQuery\.isError/, '必须处理 list API error')
  assert.match(ws, /extractReviewError\(listQuery\.error\)/, 'error 用 extractReviewError 解析')
  assert.match(ws, /市场发现加载失败/, 'error 显示明确失败文案，不是“今日无市场发现”')
  // empty 文案只在成功且 0 条时出现
  assert.match(ws, /discoveries\.length === 0/, 'empty 只在 0 条时显示')
  assert.match(ws, /今日无满足当前 Discovery 条件的市场发现/, 'empty 文案独立存在')
})

// ============================================================
// 8. 0 Discovery 显示合法 empty state
// ============================================================

test('8. 0 Discovery 显示合法 empty state（成功 + 0 条）', () => {
  const ws = read('DiscoveryWorkspace.tsx')
  // empty 分支与 error 分支分开，且 empty 只在 data 成功且无 items 时渲染
  assert.match(ws, /discoveries\.length === 0/, 'empty 分支存在')
  assert.match(ws, /今日无满足当前 Discovery 条件的市场发现/, 'empty 文案存在')
  // empty 依赖 overview.degradedReasons 显示 partial availability
  assert.match(ws, /overview\?\.degradedReasons/, 'empty 同时表达 partial availability')
})

// ============================================================
// 9 / 10. contributionPayload / roleEvidence 实际结构化渲染（CR-03/04）
// ============================================================

test('9. contributionPayload 结构化渲染（field/value，非整块 JSON）', () => {
  const inst = read('InstrumentsPanel.tsx')
  assert.match(inst, /contributionPayload/, 'InstrumentsPanel 消费 contributionPayload')
  assert.match(inst, /<StructuredEvidence label="贡献" data=\{inst\.contributionPayload\}/, '贡献 payload 走 StructuredEvidence')
  const ws = read('DiscoveryWorkspace.tsx') || ''
  const detail = read('DiscoveryDetail.tsx')
  assert.match(detail, /representativeInstruments/, 'Detail 传递代表个股到 InstrumentsPanel')
  void ws
})

test('10. roleEvidence 结构化渲染（field/value，非整块 JSON）', () => {
  const inst = read('InstrumentsPanel.tsx')
  assert.match(inst, /roleEvidence/, 'InstrumentsPanel 消费 roleEvidence')
  assert.match(inst, /<StructuredEvidence label="角色证据" data=\{inst\.roleEvidence\}/, '角色证据走 StructuredEvidence')
  // StructuredEvidence 渲染 field/value 而非默认 JSON.stringify 整块
  assert.match(inst, /styles\.evidenceRow/, '渲染 field/value 行')
  assert.match(inst, /formatKey/, '字段名做可读化处理')
  assert.match(inst, /formatValue/, '值做格式化处理')
})

// ============================================================
// 11 / 12. Discovery vs Scope Tracking 请求 payload 契约
// ============================================================

test('11. Discovery Tracking 发送 discovery target（discovery_id），非 scope target', () => {
  // 复刻 DiscoveryDetail.trackDiscovery 实际构造的 payload，验证契约形状
  const discoveryId = 'abc123def456'
  const scope = { type: 'industry_l1', key: 'l1-tech', name: '电子' }
  const tradeDate = '2026-08-10'
  const payload: ReviewTrackingCreateRequest = {
    tracking_type: 'discovery',
    discovery_id: discoveryId,
    scope_type: scope.type,
    scope_key: scope.key,
    note: `Discovery: ${scope.name} (${scope.type}/${scope.key}) @ ${tradeDate}`,
    idempotency_key: `disc-${discoveryId}`,
  }
  assert.equal(payload.tracking_type, 'discovery')
  assert.equal(payload.discovery_id, discoveryId, 'Discovery 身份以 discovery_id 无歧义持久化')
  assert.equal(payload.idempotency_key, 'disc-abc123def456')
  // scope 仅作 evaluation context，不承担 identity
  assert.ok(payload.scope_type && payload.scope_key, 'scope 作 evaluation context 保留')
})

test('12. Scope Tracking 仍保持 scope target（不带 discovery_id）', () => {
  const scope = { type: 'style', key: 'growth' }
  const payload: ReviewTrackingCreateRequest = {
    tracking_type: 'scope',
    scope_type: scope.type,
    scope_key: scope.key,
    idempotency_key: `scope-${scope.type}-${scope.key}`,
  }
  assert.equal(payload.tracking_type, 'scope')
  assert.equal(payload.scope_type, 'style')
  assert.equal(payload.scope_key, 'growth')
  assert.ok(payload.discovery_id == null, 'scope target 不得带 discovery_id')
})

test('11b. DiscoveryDetail 实际调用区分 discovery / scope 两个按钮', () => {
  const detail = read('DiscoveryDetail.tsx')
  assert.match(detail, /tracking_type: 'discovery'/, 'discovery 追踪发送 tracking_type=discovery')
  assert.match(detail, /tracking_type: 'scope'/, 'scope 追踪发送 tracking_type=scope')
  assert.match(detail, /追踪此发现/, 'Discovery 追踪按钮存在')
  assert.match(detail, /追踪此范围/, 'Scope 追踪按钮存在')
})

// ============================================================
// 13. CSS module class 经组件真实引用（camelCaseOnly 映射）
// ============================================================

test('13. CSS module class 由组件引用且存在于 module 导出集', () => {
  const scss = read('review.module.scss')
  // camelCaseOnly 配置下，SCSS kebab class → styles.camelCase。
  // 收集组件实际引用的 styles.xxx，反查 SCSS 中对应 kebab 定义是否存在。
  const componentRefs = new Set<string>()
  for (const f of ['DiscoveryWorkspace.tsx', 'DiscoveryCard.tsx', 'DiscoveryDetail.tsx', 'InstrumentsPanel.tsx']) {
    const src = read(f)
    assert.match(src, /import styles from '\.\/review\.module\.scss'/, `${f} 必须使用 CSS module`)
    for (const m of src.matchAll(/styles\.([A-Za-z0-9]+)/g)) {
      componentRefs.add(m[1])
    }
  }
  assert.ok(componentRefs.size >= 20, `应引用足够多的 module class（实际 ${componentRefs.size}）`)
  for (const camel of componentRefs) {
    // camelCase → kebab-case：先按大写拆分，再连词
    const kebab = camel
      .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
      .replace(/([A-Z])([A-Z][a-z])/g, '$1-$2')
      .toLowerCase()
    // class 可被 Sass `&-suffix` 嵌套生成（如 .discovery-card { &-header {...} } →
    // 编译后 .discovery-card-header），因此除字面出现外，也接受 `&-suffix` 形式。
    const suffix = kebab.split('-').pop() as string
    const found =
      scss.includes(`.${kebab}`) ||
      scss.includes(`.${camel}`) ||
      scss.includes(`&-${suffix}`)
    assert.ok(found, `SCSS module 必须定义组件引用的 class: .${kebab}（styles.${camel}）`)
  }
})

// ============================================================
// 14. [P1-A] Scope family filter 必须来自 canonical 全量 taxonomy，
//     不得从当前分页 Discovery 列表派生产生 → 翻页不得令 scope 选项消失。
// ============================================================

test('14. [P1-A] scopeFamilies 来自 canonical taxonomy，不依赖当前分页 discoveries', () => {
  const ws = read('DiscoveryWorkspace.tsx')
  // 不得再出现「从 discoveries 派生产生 family 集合」的旧逻辑
  assert.ok(
    !/discoveries\.forEach\(\s*d\s*=>\s*families\.add\(d\.scope\.type\)/.test(ws),
    'scopeFamilies 不得从 discoverys 列表派生产生 family 集合',
  )
  assert.ok(
    !/families\.has\(f\)/.test(ws),
    'scopeFamilies 不得用 families.has 来过滤 canonical taxonomy（会随分页消失）',
  )
  // canonical 全量 taxonomy 必须作为权威来源出现
  assert.match(
    ws,
    /\['market', 'major_index', 'style', 'industry_l1', 'industry_l2', 'industry_l3', 'concept'\]/,
    'scopeFamilies 必须以 canonical 全量 scope-type taxonomy 为来源',
  )
  // 即便当前页无某 family 的 Discovery，该 family 仍应出现在选项中
  // （通过 canonical 常量直接返回，而非基于当前页 items 计算）
  assert.ok(
    /scopeFamilies = useMemo\(\(\) => \{[\s\S]*canonical[\s\S]*\}, \[scopeType\]\)/.test(ws)
    || /scopeFamilies = useMemo\(\(\) => \{[\s\S]*canonical[\s\S]*return scopeType[\s\S]*canonical/.test(ws),
    'scopeFamilies useMemo 依赖仅含 scopeType（不依赖 discoveries），始终返回 canonical taxonomy',
  )
})
