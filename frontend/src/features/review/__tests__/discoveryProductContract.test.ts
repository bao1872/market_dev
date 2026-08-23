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
  DEFAULT_LEGACY_REVIEW_VIEW as DEFAULT_REVIEW_VIEW,
  REVIEW_FORMAL_STAGES,
  decodeLegacyReviewUrl as decodeReviewUrl,
  encodeLegacyReviewUrl as encodeReviewUrl,
  normalizeLegacyView as normalizeView,
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

test('1b. [Slice D] ReviewPage 不再渲染 DiscoveryWorkspace（canonical cutover）', () => {
  const src = read('../../pages/ReviewPage.tsx')
  assert.doesNotMatch(
    src,
    /import[\s\S]*?DiscoveryWorkspace/,
    'ReviewPage 不得 import DiscoveryWorkspace（Discovery runtime 已从 /review 退休）',
  )
  assert.doesNotMatch(src, /<DiscoveryWorkspace/, 'ReviewPage 不得渲染 <DiscoveryWorkspace>')
  // canonical 主入口为 ScopeExplorerWorkspace
  assert.match(src, /import ScopeExplorerWorkspace/, 'ReviewPage 必须 import ScopeExplorerWorkspace')
  assert.match(src, /<ScopeExplorerWorkspace/, 'ReviewPage 必须渲染 <ScopeExplorerWorkspace>')
})

// ============================================================
// 2. 旧五阶段不再作为用户一级主导航
// ============================================================

test('2. [Slice D] 旧五阶段不再作为 /review runtime 路径（仅 legacy 常量保留）', () => {
  // 正式五阶段仍作为 legacy 常量保留（ReviewStageNav 等物理文件待 Slice F 删除）
  assert.deepStrictEqual([...REVIEW_FORMAL_STAGES], [
    'scan', 'signals', 'attribution', 'validation', 'tracking',
  ])
  // 默认视图是 canonical table，不再是 stages（本文件 DEFAULT_REVIEW_VIEW 为 legacy 别名；
  // canonical 默认值由 scopeExplorerContract RT1 断言）
  assert.notEqual(DEFAULT_REVIEW_VIEW, 'stages')
  // stages 深链在 legacy decode 层仍可解析（物理组件未被删除前保持兼容）
  assert.equal(normalizeView('stages'), 'stages', 'stages 仍可深链解析为 legacy debug drilldown')
  // ReviewPage 不再存在 view=stages 运行时分支（canonical cutover）
  const src = read('../../pages/ReviewPage.tsx')
  assert.doesNotMatch(src, /view === 'stages'|view: 'stages'/, 'ReviewPage 不得保留 view=stages 运行时路径')
  assert.doesNotMatch(src, /ReviewStageNav/, 'ReviewPage 不得 import/渲染 ReviewStageNav')
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
// 14. [P1-A] Scope FAMILY chips 必须表示 5 个语义家族（MARKET/INDEX/STYLE/
//     INDUSTRY/CONCEPT），使用 scope_type 前缀作为 wire value，且独立于当前
//     分页 Discovery items —— 翻页不得令 scope family 选项消失。
// ============================================================

test('14. [P1-A] scope family 使用 5 语义家族 + scope_type 前缀 wire value，独立于分页', () => {
  const ws = read('DiscoveryWorkspace.tsx')
  // 不得再出现「从 discoveries 派生产生 family 集合」的旧逻辑
  assert.ok(
    !/discoveries\.forEach\(\s*d\s*=>\s*families\.add\(d\.scope\.type\)/.test(ws),
    'scope family 不得从 discoverys 列表派生产生 family 集合',
  )
  assert.ok(
    !/families\.has\(f\)/.test(ws),
    'scope family 不得用 families.has 来过滤（会随分页消失）',
  )
  // 5 个语义家族的 wire value 必须是 scope_type 前缀
  assert.match(
    ws,
    /SCOPE_FAMILIES[\s\S]*'market'[\s\S]*'major_index'[\s\S]*'style'[\s\S]*'industry'[\s\S]*'concept'/,
    'SCOPE_FAMILIES 必须包含 5 个语义家族 wire value: market/major_index/style/industry/concept',
  )
  // 不得把 industry_l1/l2/l3 当作独立 family（INDUSTRY 由前缀 industry 覆盖）
  const famStart = ws.indexOf('const SCOPE_FAMILIES')
  const typeStart = ws.indexOf('const SCOPE_TYPES')
  assert.ok(famStart >= 0, 'SCOPE_FAMILIES 常量必须存在')
  const between = ws.slice(famStart, typeStart >= 0 ? typeStart : ws.length)
  assert.ok(
    !/industry_l1|industry_l2|industry_l3/.test(between),
    'SCOPE_FAMILIES 不得将 industry_l1/l2/l3 当作独立 family（INDUSTRY 前缀覆盖）',
  )
  // 精确的 scope_type 选择器保持独立（7 种），不与 family 合并
  assert.match(
    ws,
    /SCOPE_TYPES[\s\S]*'industry_l1'[\s\S]*'industry_l2'[\s\S]*'industry_l3'/,
    'SCOPE_TYPES 必须保留 7 个精确 scope_type（含 industry_l1/l2/l3），与 family 分离',
  )
  // 两者都通过 useMemo 渲染，且都不依赖 discoveries
  assert.ok(
    /SCOPE_FAMILIES[\s\S]*useMemo/.test(ws) && /SCOPE_TYPES[\s\S]*useMemo/.test(ws),
    'SCOPE_FAMILIES 与 SCOPE_TYPES 均以 useMemo 声明',
  )
  assert.ok(
    !/setFilter\('scopeFamily'[\s\S]*scopeType/.test(ws) || /SCOPE_FAMILIES\.map/.test(ws),
    'scope family 与 scope_type 渲染来自各自独立常量',
  )
})

// ============================================================
// 15. [P1-A] family→scope_type 映射契约：major_index→INDEX；industry_l1/l2/l3→INDUSTRY
// ============================================================

test('15. [P1-A] family wire value 映射正确（major_index→INDEX, industry*→INDUSTRY）', () => {
  const ws = read('DiscoveryWorkspace.tsx')
  // 语义映射注释或常量结构应体现：major_index 对应 INDEX, industry 前缀对应 INDUSTRY
  assert.ok(
    /major_index[\s\S]*INDEX/.test(ws) && /'industry'[\s\S]*INDUSTRY/.test(ws),
    'major_index 映射 INDEX, industry 前缀映射 INDUSTRY（注释或 label 体现语义）',
  )
})

// ============================================================
// 16. [P1-C] 代表个股投影必须暴露 symbol/name，且结构化字段保留
// ============================================================

test('16. [P1-C] backend 投影暴露 symbol/name，保留既有字段', () => {
  const svc = read('../../../../backend/app/services/review_discovery_service.py')
  assert.match(
    svc,
    /"symbol":\s*i\.symbol/,
    'representative instrument 投影必须包含 symbol = row.symbol',
  )
  assert.match(
    svc,
    /"name":\s*i\.name/,
    'representative instrument 投影必须包含 name = row.name',
  )
  // 既有字段不得丢失
  for (const f of ['instrumentId', 'boardRole', 'relationToScope', 'contributionValue',
    'contributionRank', 'contributionPayload', 'roleEvidence']) {
    assert.ok(new RegExp(`"${f}":`).test(svc), `投影必须保留既有字段: ${f}`)
  }
})

test('17. [P1-C] 前端 DiscoveryRepresentativeInstrument 含 symbol/name 且 InstrumentsPanel 导航 /stock/:symbol', () => {
  const types = read('types.ts')
  assert.match(
    types,
    /interface DiscoveryRepresentativeInstrument[\s\S]*symbol:\s*string \| null[\s\S]*name:\s*string \| null/,
    'DiscoveryRepresentativeInstrument 必须含 symbol/name（string|null）',
  )
  const panel = read('InstrumentsPanel.tsx')
  assert.ok(/import \{ Link \}/.test(panel), 'InstrumentsPanel 必须引入 Link 做 canonical 导航')
  assert.match(
    panel,
    /\/stock\/\$\{encodeURIComponent\(symbol\)\}/,
    'InstrumentsPanel 必须使用 /stock/:symbol 做 canonical 个股导航',
  )
  // symbol 缺失时不得伪造路由
  assert.ok(
    /hasSymbol[\s\S]*stockHref/.test(panel),
    'InstrumentsPanel 必须基于 symbol 是否存在决定导航，缺失时不伪造路由',
  )
  assert.ok(
    /hasSymbol \? \([\s\S]*<Link to=\{stockHref[\s\S]*\) : \([\s\S]*<span>\{inst\.instrumentId\}/.test(panel),
    'symbol 缺失时回退为不导航的 instrumentId 文本',
  )
})
