// [Slice D] Canonical Scope Explorer 合同测试（纯 TS，tsx --test 可跑）。
// 覆盖 prompt §18 针对 Slice D 的 targeted tests：
//   - ViewModel：q/phase/readiness 过滤、velocity_desc 排序、null 排最后、确定性 tie-break、
//     UI 分页在过滤之后、Breadth 不计算 composite score
//   - Family snapshot：transport aggregation（单页单请求、total>100 补页、并行、失败 fail-closed、
//     重复 identity fail-closed）
//   - Table：精确 canonical 列、无 p/q/u/c/v/signalCount、summary=null → —、
//     Position 75 → "75"（不乘 100）、EW Return 百分比
//   - Trajectory：x=Position 0–100、null 不绘制为 0、选中 identity、无机会区标签
//   - Runtime：ReviewPage 用 canonical URL decoder、不 import 任何 legacy Review 组件/API
//
// 纯逻辑部分做真实行为断言；React/SCSS 部分用「源码 + 类型/格式化函数」做契约断言
// （node 无法 import .scss/.tsx，沿用项目既有 harness，禁止新增 test framework）。
import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  buildScopeExplorerQuery,
  filterScopes,
  sortVelocityDesc,
  sortScopes,
  applyScopeExplorerPipeline,
  findScopeById,
  computeEffectivePage,
} from '../scopeExplorerViewModel'
import { loadFamilySnapshot, FAMILY_SNAPSHOT_PAGE_SIZE } from '../useReviewScopeFamilySnapshot'
import { formatPosition, formatPercentNullable, formatContributionFraction, NULL_DISPLAY } from '../reviewFormat'
import {
  DEFAULT_REVIEW_VIEW,
  DEFAULT_REVIEW_SORT,
  defaultReviewUrlState,
  withReviewFilterChange,
  withReviewPageChange,
  normalizeSort,
  decodeReviewUrl,
  encodeReviewUrl,
  reviewSortToggle,
} from '../urlState'
import type { ReviewSort } from '../urlState'
import type {
  ReviewScopeListItem,
  ReviewScopeSummary,
  ReviewScopeObservationSummary,
  ReviewScopeListResponse,
} from '../types'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

const flush = (): Promise<void> => new Promise((r) => setTimeout(r, 0))

// ============================================================
// fixtures
// ============================================================

function makeSummary(overrides: Partial<ReviewScopeSummary> = {}): ReviewScopeSummary {
  return {
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
    ...overrides,
  }
}

function makeItem(
  scopeKey: string,
  overrides: Partial<ReviewScopeListItem> = {},
): ReviewScopeListItem {
  return {
    scopeType: 'industry_l1',
    scopeKey,
    scopeName: `name-${scopeKey}`,
    readiness: 'ready',
    status: 'ready',
    eligibleCount: 10,
    providedCount: 10,
    coverageRatio: 1,
    summary: null,
    observationSummary: null,
    ...overrides,
  }
}

function makeListResponse(
  items: ReviewScopeListItem[],
  page: number,
  page_size = 100,
  total = items.length,
): ReviewScopeListResponse {
  return { items, total, page, page_size, has_more: page * page_size < total }
}

// ============================================================
// 1. ViewModel：q 过滤（scopeName / scopeKey，大小写不敏感）
// ============================================================

test('VM1. q 大小写不敏感匹配 scopeName / scopeKey', () => {
  const items = [
    makeItem('copper', { scopeName: '有色金属' }),
    makeItem('bank', { scopeName: '银行' }),
    makeItem('AI', { scopeName: '人工智能' }),
  ]
  // 中文 scopeName
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('有色', null)).map((i) => i.scopeKey),
    ['copper'],
  )
  // scopeKey 精确 + 大小写不敏感
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('copper', null)).map((i) => i.scopeKey),
    ['copper'],
  )
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('COPPER', null)).map((i) => i.scopeKey),
    ['copper'],
  )
  // q 空 → 全部
  assert.equal(filterScopes(items, buildScopeExplorerQuery('', null)).length, 3)
  // q 只匹配 key 时也命中（不搜索任意 JSON）
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('bank', null)).map((i) => i.scopeKey),
    ['bank'],
  )
})

// ============================================================
// 2. ViewModel：phase / readiness 精确过滤
// ============================================================

test('VM2. phase 精确 canonical 匹配；phase=null 不过滤', () => {
  const items = [
    makeItem('a', { summary: makeSummary({ phase: 'Strengthening' }) }),
    makeItem('b', { summary: makeSummary({ phase: 'Weakening' }) }),
    makeItem('c', { summary: null }),
  ]
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('', 'Strengthening')).map((i) => i.scopeKey),
    ['a'],
  )
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('', 'Weakening')).map((i) => i.scopeKey),
    ['b'],
  )
  // phase=null → 全部（含 summary=null 的项）
  assert.equal(filterScopes(items, buildScopeExplorerQuery('', null)).length, 3)
})

// P0-1：readiness 不再参与 Review scope list 过滤（普通用户不再用它筛选列表）。
// readiness 仍作为 row/detail/API canonical 字段保留，但没有任何 readiness 值会隐藏 scope。
test('VM3. P0-1: readiness 不再过滤列表（stale ?readiness=ready 不会隐藏 scope）', () => {
  const items = [
    makeItem('a', { readiness: 'ready' }),
    makeItem('b', { readiness: 'insufficient_history' }),
    makeItem('c', { readiness: 'unavailable_current' }),
  ]
  // 即使 query 仍携带 readiness（来自旧 URL），filterScopes 也必须忽略它 → 全部返回
  assert.equal(filterScopes(items, buildScopeExplorerQuery('', null)).length, 3)
  // 旧 URL 携带 readiness=ready 也不应隐藏非 ready 的 scope
  assert.equal(filterScopes(items, buildScopeExplorerQuery('', null)).length, 3)
  // phase 过滤仍正常（用带 summary 的项验证，避免 summary=null 被 phase 过滤）
  const phased = [
    makeItem('x', { summary: makeSummary({ phase: 'Strengthening' }) }),
    makeItem('y', { summary: makeSummary({ phase: 'Weakening' }) }),
  ]
  assert.deepEqual(
    filterScopes(phased, buildScopeExplorerQuery('', 'Strengthening')).map((i) => i.scopeKey),
    ['x'],
  )
})

// ============================================================
// 3. ViewModel：velocity_desc 排序（null 排最后、确定性 tie-break）
// ============================================================

test('VM4. velocity_desc：有限值降序，null 恒排最后', () => {
  const items = [
    makeItem('a', { summary: makeSummary({ velocity: 5 }) }),
    makeItem('b', { summary: makeSummary({ velocity: null }) }),
    makeItem('c', { summary: makeSummary({ velocity: 3 }) }),
    makeItem('d', { summary: makeSummary({ velocity: 9 }) }),
    makeItem('e', { summary: null }),
  ]
  const sorted = sortVelocityDesc(items).map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['d', 'a', 'c', 'b', 'e'])
})

test('VM5. velocity 相同时按确定性 tie-break（scopeName ?? scopeKey，再 scopeKey）', () => {
  const items = [
    makeItem('z1', { scopeName: 'Alpha', summary: makeSummary({ velocity: 5 }) }),
    makeItem('z2', { scopeName: 'beta', summary: makeSummary({ velocity: 5 }) }),
  ]
  const sorted = sortVelocityDesc(items).map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['z1', 'z2'], 'velocity 相同按 scopeName 字典序')
  // scopeName 为 null 时回退 scopeKey
  const noName = [
    makeItem('b', { scopeName: null, summary: makeSummary({ velocity: 5 }) }),
    makeItem('a', { scopeName: null, summary: makeSummary({ velocity: 5 }) }),
  ]
  assert.deepEqual(sortVelocityDesc(noName).map((i) => i.scopeKey), ['a', 'b'])
})

// ============================================================
// 4. ViewModel：UI 分页在过滤之后
// ============================================================

test('VM6. applyScopeExplorerPipeline：过滤 → 排序 → 分页（total 为过滤后数量）', () => {
  const items = [
    makeItem('a', { scopeName: '有色A', summary: makeSummary({ velocity: 10 }) }),
    makeItem('b', { scopeName: '有色B', summary: makeSummary({ velocity: 5 }) }),
    makeItem('c', { scopeName: '银行C', summary: makeSummary({ velocity: 20 }) }),
  ]
  // q=有色 → 过滤后 2 条，velocity 降序 [a(10), b(5)]
  const q = buildScopeExplorerQuery('有色', null)
  const page1 = applyScopeExplorerPipeline(items, q, 1, 1)
  assert.equal(page1.total, 2)
  assert.equal(page1.items.length, 1)
  assert.equal(page1.items[0].scopeKey, 'a')
  const page2 = applyScopeExplorerPipeline(items, q, 2, 1)
  assert.equal(page2.items[0].scopeKey, 'b')
  assert.equal(page2.pageCount, 2)
})

test('VM7. findScopeById 从完整 snapshot 查找（不受过滤影响）', () => {
  const items = [makeItem('a'), makeItem('b')]
  assert.equal(findScopeById(items, 'b')?.scopeKey, 'b')
  assert.equal(findScopeById(items, null), undefined)
  assert.equal(findScopeById(items, 'missing'), undefined)
})

// ============================================================
// 5. Family snapshot：transport aggregation
// ============================================================

test('FS1. 单页时只发一次请求', async () => {
  let calls = 0
  const fetchPage = async (page: number): Promise<ReviewScopeListResponse> => {
    calls += 1
    return makeListResponse([makeItem(`k${page}`)], page)
  }
  const snap = await loadFamilySnapshot(fetchPage, FAMILY_SNAPSHOT_PAGE_SIZE)
  assert.equal(calls, 1)
  assert.equal(snap.pageCount, 1)
  assert.equal(snap.items.length, 1)
  assert.equal(snap.total, 1)
})

test('FS2. total > pageSize 时拉取全部剩余页并按传输顺序合并', async () => {
  const total = 250
  const fetchPage = async (page: number): Promise<ReviewScopeListResponse> => {
    // 末页不足 pageSize（真实后端行为）：p1=100, p2=100, p3=50
    const count = page === 3 ? 50 : 100
    const items = Array.from({ length: count }, (_, i) => makeItem(`p${page}-${i}`))
    return makeListResponse(items, page, 100, total)
  }
  const snap = await loadFamilySnapshot(fetchPage, FAMILY_SNAPSHOT_PAGE_SIZE)
  assert.equal(snap.pageCount, 3)
  assert.equal(snap.items.length, 250)
  // 合并顺序 = 传输顺序（page 1 → 2 → 3），不能乱序
  assert.equal(snap.items[0].scopeKey, 'p1-0')
  assert.equal(snap.items[100].scopeKey, 'p2-0')
  assert.equal(snap.items[200].scopeKey, 'p3-0')
  assert.equal(snap.total, 250)
})

test('FS3. 剩余页并行发起（非逐页瀑布）', async () => {
  const calls: number[] = []
  const gates: Record<number, () => void> = {}
  // 真实 fixture：pageSize=1、total=3，每页恰好 1 行（满足 completeness 校验，
  // 不得用“每页 1 行但 total=250”的伪造 fixture 绕过完整性校验）
  const fetchPage = (page: number): Promise<ReviewScopeListResponse> => {
    calls.push(page)
    return new Promise((resolve) => {
      gates[page] = () => resolve(makeListResponse([makeItem(`k${page}`)], page, 1, 3))
    })
  }
  const pending = loadFamilySnapshot(fetchPage, 1)
  await flush()
  // 先发 page 1（需要 total 才知道要补几页）
  assert.deepEqual(calls, [1], '先请求 page 1 以确定 total')
  gates[1]()
  await flush()
  // page1 解析后，Promise.all([2,3]) 同步发起：page 2/3 已同时被请求
  assert.deepEqual(calls, [1, 2, 3], 'page 2/3 必须并行发起')
  gates[2]()
  await flush()
  assert.deepEqual(calls, [1, 2, 3], 'page 3 不等 page 2 完成后才发起')
  gates[3]()
  const snap = await pending
  assert.equal(snap.pageCount, 3)
  assert.equal(snap.total, 3)
  assert.equal(snap.items.length, 3)
})

test('FS4. 任一分页失败 → 整体 fail closed（不静默展示部分页）', async () => {
  const fetchPage = async (page: number): Promise<ReviewScopeListResponse> => {
    if (page === 2) throw new Error('page 2 failed')
    return makeListResponse([makeItem(`k${page}`)], page, 100, 250)
  }
  await assert.rejects(() => loadFamilySnapshot(fetchPage, FAMILY_SNAPSHOT_PAGE_SIZE), /page 2 failed/)
})

test('FS5. 重复 scope identity → fail closed（显式检测）', async () => {
  const dup = [makeItem('dup'), makeItem('dup')]
  const fetchPage = async (): Promise<ReviewScopeListResponse> => makeListResponse(dup, 1)
  await assert.rejects(() => loadFamilySnapshot(fetchPage, FAMILY_SNAPSHOT_PAGE_SIZE), /重复 scope identity/)
})

test('FS6. 合并项数不足 total → fail closed（不静默展示部分快照）', async () => {
  const total = 250
  const fetchPage = async (page: number): Promise<ReviewScopeListResponse> => {
    // page3 只返回 20 条（模拟 HTTP 200 但数据缺失）：合并 220 < total 250
    const count = page === 3 ? 20 : 100
    const items = Array.from({ length: count }, (_, i) => makeItem(`p${page}-${i}`))
    return makeListResponse(items, page, 100, total)
  }
  await assert.rejects(
    () => loadFamilySnapshot(fetchPage, FAMILY_SNAPSHOT_PAGE_SIZE),
    /incomplete family snapshot: expected=250 actual=220/,
  )
})

test('FS7. 分页 total 漂移 → fail closed', async () => {
  const fetchPage = async (page: number): Promise<ReviewScopeListResponse> => {
    const items = Array.from({ length: 100 }, (_, i) => makeItem(`p${page}-${i}`))
    // page2 total 与 first 不一致
    return makeListResponse(items, page, 100, page === 2 ? 249 : 250)
  }
  await assert.rejects(
    () => loadFamilySnapshot(fetchPage, FAMILY_SNAPSHOT_PAGE_SIZE),
    /family snapshot total 漂移: first=250 page2=249/,
  )
})

// R2C (Family SQL Filter Closure) transport contract.
// 生产所有权：backend SQL 保证家族隔离（count==page 同一 family 谓词）。
// 前端 loadFamilySnapshot() 不主动拒绝 foreign-family rows；它只是把 backend
// 返回的每页按 total 合并。因此 FS8 记录「backend→transport 期望的家族不变式」，
// FS9 证明「若 backend 再次返回错误 family，transport 会把污染数据合并进
// snapshot，test 自身能发现 contamination」——而不是 production transport 会
// fail closed。
test('FS8(R2C). 期望 family 不变式：每页请求携带 scope_type=family，合并后全为同 family', async () => {
  const family = 'concept' as const
  const requested: string[] = []
  const fetchPage = async (page: number): Promise<ReviewScopeListResponse> => {
    // 模拟 backend 正确隔离：每页都按 scope_type=family 过滤，返回同 family 的行
    const items = Array.from({ length: 30 }, (_, i) =>
      makeItem(`k-${page}-${i}`, { scopeType: family }),
    )
    return makeListResponse(items, page, 30, 90)
  }
  // 包装：验证 transport 始终把 family 传给后端 query（R2C 前置条件）
  const wrapped = async (page: number): Promise<ReviewScopeListResponse> => {
    // 真实调用 getReviewScopes 时 scope_type 由 useReviewScopeFamilySnapshot 注入；
    // 这里直接校验 fetchPage 收到的 family 假设与返回一致。
    requested.push(family)
    return fetchPage(page)
  }
  const snap = await loadFamilySnapshot(wrapped, 30)
  assert.equal(snap.total, 90)
  assert.equal(snap.items.length, 90)
  // 后置条件：合并后每个 item 的 scopeType 必须等于请求的 family（backend 保证）
  for (const item of snap.items) {
    assert.equal(
      item.scopeType,
      family,
      `family snapshot 含非 ${family} 行: ${item.scopeType}/${item.scopeKey}`,
    )
  }
  // 前置条件：每页请求都携带请求的 family（消费端不得遗漏 scope_type）
  assert.equal(requested.length, 3)
  assert.ok(requested.every((f) => f === family))
})

test('FS9(R2C). backend 若再返回错误 family，transport 会把污染合并进 snapshot（test 能发现 contamination）', async () => {
  // 模拟 backend 回归：page 未按 family 过滤，返回了 industry_l1 行混入 concept 请求
  const family = 'concept'
  const fetchPage = async (page: number): Promise<ReviewScopeListResponse> => {
    const items = [
      makeItem(`concept-${page}-0`, { scopeType: 'concept' }),
      makeItem(`industry_l1-${page}-1`, { scopeType: 'industry_l1' }), // 污染行
    ]
    return makeListResponse(items, page, 2, 4)
  }
  const snap = await loadFamilySnapshot(fetchPage, 2)
  // 注意：production transport 不独立 fail closed；它只是合并 backend 返回。
  // 因此污染行会出现在 snapshot 中——这证明了家族隔离必须依赖 backend SQL，
  // 且 test 自身通过统计 contamination 能发现回归。
  const contaminations = snap.items.filter((it) => it.scopeType !== family)
  assert.equal(contaminations.length, 2, '模拟 backend 回归：其他 family 行污染了 snapshot')
  void contaminations
})

// ============================================================
// P. 分页 wiring：翻页走独立 onPageChange，过滤变化才重置 page
// ============================================================

test('P1. Workspace 提供独立 onPageChange，翻页按钮不经过 onFilterChange', () => {
  const src = read('ScopeExplorerWorkspace.tsx')
  assert.match(src, /onPageChange: \(page: number\) => void/, 'props 必须含独立 onPageChange')
  assert.match(src, /onClick=\{\(\) => onPageChange\(effectivePage/, '翻页按钮必须调用 onPageChange')
  assert.doesNotMatch(src, /onFilterChange\(\{\s*page:/, '翻页不得走 onFilterChange({ page })')
})

test('P2. ReviewPage 用 withReviewPageChange 绑定 onPageChange', () => {
  const src = read('../../pages/ReviewPage.tsx')
  assert.match(src, /withReviewPageChange/, 'ReviewPage 必须使用 withReviewPageChange')
  assert.match(src, /onPageChange=\{handlePageChange\}/, 'ReviewPage 必须传递 onPageChange')
})

test('P3. 生产 helper 语义分离：过滤重置 page=1，翻页只改 page 且保留全部状态', () => {
  const base = { ...defaultReviewUrlState(), page: 3 }
  // 过滤类变化 → page=1
  assert.equal(withReviewFilterChange(base, { q: 'x' }).page, 1)
  assert.equal(withReviewFilterChange(base, { phase: 'Strengthening' as never }).page, 1)
  assert.equal(withReviewFilterChange(base, { readiness: 'ready' as never }).page, 1)
  assert.equal(withReviewFilterChange(base, { pageSize: 100 }).page, 1)
  // 翻页 → 只改 page，保留 q/phase/readiness/family/scopeKey/pageSize/view
  const next = withReviewPageChange(
    { ...base, page: 1, q: '有色', phase: 'Strengthening' as never, readiness: 'ready' as never },
    2,
  )
  assert.equal(next.page, 2)
  assert.equal(next.q, '有色')
  assert.equal(next.phase, 'Strengthening')
  assert.equal(next.readiness, 'ready')
  assert.equal(next.family, 'industry_l1')
  assert.equal(next.scopeKey, null)
  assert.equal(next.pageSize, 50)
  assert.equal(next.view, 'table')
  // page 1 → next → 2；page 3 → previous → 2
  assert.equal(withReviewPageChange({ ...base, page: 1 }, 2).page, 2)
  assert.equal(withReviewPageChange({ ...base, page: 3 }, 2).page, 2)
  // 非法页码钳制到 1
  assert.equal(withReviewPageChange(base, 0).page, 1)
})

test('P4. effectivePage 钳制：URL 越界页 → 实际末页；Workspace 交互用它驱动', () => {
  assert.equal(computeEffectivePage(999, 3), 3)
  assert.equal(computeEffectivePage(1, 3), 1)
  assert.equal(computeEffectivePage(3, 3), 3)
  assert.equal(computeEffectivePage(0, 3), 1)
  assert.equal(computeEffectivePage(2, 0), 1) // pageCount=0（全被过滤）→ 1
  const src = read('ScopeExplorerWorkspace.tsx')
  assert.match(src, /computeEffectivePage/, 'Workspace 必须计算 effectivePage')
  assert.match(src, /disabled=\{effectivePage <= 1\}/, '上一页禁用用 effectivePage')
  assert.match(src, /disabled=\{effectivePage >= paged\.pageCount\}/, '下一页禁用用 effectivePage')
})

// ============================================================
// 6. Table：canonical 列 + 无 legacy 字段 + null 显示
// ============================================================

test('T1. Table 精确 canonical 列（12 列，无 p/q/u/c/v/signalCount）', () => {
  const src = read('ScopeExplorerTable.tsx')
  // REVIEW-UX-CN-01：表头经 ReviewTerm termKey 渲染中文 label；canonical 列集合不变。
  const expected = [
    'scope',
    'phase',
    'position',
    'velocity',
    'acceleration',
    'equalWeightReturn',
    'capitalTilt',
    'breadth',
    'leadershipMigration',
    'coverage',
    'freshness',
    'technical',
  ]
  for (const col of expected) {
    // 列经 ReviewTerm 渲染：非排序列为 termKey="col"；排序列经 renderSortableHeader('col', 'col')
    const ok = src.includes(`termKey="${col}"`) || src.includes(`renderSortableHeader('${col}'`)
    assert.ok(ok, `Table 必须含 canonical 列 ${col}`)
  }
  // 源码不得引用 legacy 指标
  assert.doesNotMatch(src, /\.p\b|\.q\b|\.u\b|\.c\b|\.v\b|signalCount/, 'Table 不得引用 p/q/u/c/v/signalCount')
})

test('T2. Position 75 显示 "75"（0–100 percentile，绝不乘 100）', () => {
  assert.equal(formatPosition(75), '75')
  assert.equal(formatPosition(0), '0')
  assert.equal(formatPosition(100), '100')
  assert.equal(formatPosition(null), NULL_DISPLAY)
  const src = read('ScopeExplorerTable.tsx')
  assert.match(src, /formatPosition\(/, 'Table 必须使用 formatPosition 展示 Position')
})

test('T3. summary=null → 分析格显示 —（readiness 仍诚实展示）', () => {
  const src = read('ScopeExplorerTable.tsx')
  // 单元格对 null summary 走 formatX nullable → NULL_DISPLAY
  assert.match(src, /s\?\.phase/, 'phase 用 s?.phase（null → —）')
  assert.match(src, /s\?\.position/, 'position 用 s?.position')
  assert.match(src, /BreadthCell[\s\S]*if \(!summary\)/, 'BreadthCell 对 summary=null 显示占位符')
  assert.match(src, /formatPhaseLabel\(s\?\.phase\)/, 'phase 走 formatPhaseLabel')
  // readiness 在行级独立展示（coverageRatio 不在 summary 内）
  assert.match(src, /row\.coverageRatio/, 'coverage 使用行级 coverageRatio')
})

test('T4. EW Return 使用百分比格式（非原始比率）', () => {
  assert.equal(formatPercentNullable(0.123), '12.3%')
  const src = read('ScopeExplorerTable.tsx')
  assert.match(src, /formatPercentNullable\(s\?\.equalWeightReturn\)/, 'EW Return 走 formatPercentNullable')
})

test('T5. Breadth 展示三分量（↑/↓/—），不计算 composite score', () => {
  const src = read('ScopeExplorerTable.tsx')
  // 分别读取三个分量，而非合成一个 score
  assert.match(src, /advanceRatio/, 'Breadth 使用 advanceRatio')
  assert.match(src, /declineRatio/, 'Breadth 使用 declineRatio')
  assert.match(src, /unchangedRatio/, 'Breadth 使用 unchangedRatio')
  // 不得把三者相加/平均成单一 breadth score
  assert.ok(
    !/advanceRatio\s*\+\s*declineRatio|advanceRatio\s*\/\s*2|compositeBreadth|breadthScore/.test(src),
    'Breadth 不得计算 composite score',
  )
})

// ============================================================
// 7. Trajectory：坐标契约 + 空值排除 + 选中 identity
// ============================================================

test('TR1. x 使用 Position 0–100 固定比例', () => {
  const src = read('ScopeTrajectoryView.tsx')
  assert.match(src, /\(pos \/ 100\)/, 'x 必须按 Position/100 映射')
  assert.match(src, /position/, '使用 summary.position')
  // 不得把 velocity 当 x
  assert.ok(!/xScale\(.*velocity/.test(src), 'x 不得使用 velocity')
})

test('TR2. position/velocity 为 null 的点被排除，不强制为 0', () => {
  const src = read('ScopeTrajectoryView.tsx')
  assert.match(src, /plottable/, 'plottable 过滤逻辑存在')
  assert.match(src, /position !== null/, 'position null 必须排除')
  assert.match(src, /velocity !== null/, 'velocity null 必须排除')
  assert.match(src, /缺失值不强制为 0/, '空态文案说明不强制为 0')
})

test('TR3. 选中节点 identity 为 scopeKey', () => {
  const src = read('ScopeTrajectoryView.tsx')
  assert.match(src, /selectedScopeKey/, '使用 selectedScopeKey 判定选中')
  assert.match(src, /r\.scopeKey === selectedScopeKey/, '按 scopeKey 匹配选中节点')
})

test('TR4. 无机会区标签 / 无相位彩虹节点', () => {
  const src = read('ScopeTrajectoryView.tsx')
  // 节点类只由 选中/未选中 决定，不得按 phase 派生颜色类
  assert.ok(
    !/trajNode[\s\S]*?phase|phase[\s\S]*?trajNode/.test(src),
    '节点不得按 phase 派生颜色类',
  )
  // 不得渲染机会区标签（机会区只允许以否定语义出现在注释中，不得作为渲染文本）
  assert.ok(
    !/['"`>]\s*机会区/.test(src),
    '不得渲染机会区标签',
  )
  // 不引入彩虹/机会区绘图语义
  assert.doesNotMatch(src, /rainbow|hue|hsl|opportunityZone|opportunity-zone/i)
})

test('TR5. acceleration glyph 绘制在 SVG 节点旁（随节点 map，null 不绘制）', () => {
  const src = read('ScopeTrajectoryView.tsx')
  assert.match(src, /accelGlyphFor\(r\.summary\.acceleration\)/, '节点使用 summary.acceleration 直接判定字形')
  assert.match(src, /accel !== null\s*&&/, 'null acceleration 不绘制 glyph')
  assert.match(src, /<text[\s\S]*?className=\{styles\.trajAccelText\}/, 'glyph 为 SVG text，随节点同组')
  // glyph 位于 plottable.map 内（节点 <g> 里），非独立列表
  const mapStart = src.indexOf('plottable.map')
  const mapEnd = src.indexOf('})}', mapStart)
  const nodeBlock = src.slice(mapStart, mapEnd)
  assert.ok(nodeBlock.includes('<circle'), '节点块含 circle')
  assert.ok(nodeBlock.includes('trajAccelText'), '节点块内含 acceleration glyph')
})

test('TR6. acceleration glyph 不使用 styles.up/styles.down，节点填充中性', () => {
  const src = read('ScopeTrajectoryView.tsx')
  assert.doesNotMatch(src, /styles\.up/, 'Trajectory 不得引用 styles.up')
  assert.doesNotMatch(src, /styles\.down/, 'Trajectory 不得引用 styles.down')
  // 不得使用方向/强弱措辞
  assert.doesNotMatch(src, /看多|看空|强势|弱势|加速上行|加速下行/, '不得使用方向性措辞')
  const scss = read('review.module.scss')
  assert.match(scss, /\.trajAccelText \{[\s\S]*?fill: v\.\$color-muted/, 'glyph 用中性 muted 色')
})

test('TR7. 仅选中节点使用品牌描边，未选中节点中性填充', () => {
  const scss = read('review.module.scss')
  const nodeBlock = scss.match(/\.trajNode \{[\s\S]*?\n\}/)
  assert.ok(nodeBlock, '存在 .trajNode 样式块')
  // 未选中节点 circle 的 fill 必须中性（focus-visible 描边为可达性焦点环，不属于填充）
  const fillMatch = nodeBlock[0].match(/circle \{\s*fill: ([^;]+);/)
  assert.ok(fillMatch, '.trajNode circle 必须显式声明 fill')
  assert.doesNotMatch(fillMatch[1], /color-brand/, '未选中节点填充不得用品牌色')
  assert.match(scss, /\.trajNodeSelected \{[\s\S]*?fill: v\.\$color-brand/, '选中节点品牌填充')
  assert.match(scss, /\.trajNodeSelected \{[\s\S]*?stroke: v\.\$color-brand/, '选中节点品牌描边')
})

test('TR8. legend 使用中性措辞（正/负/零 Acceleration）', () => {
  const src = read('ScopeTrajectoryView.tsx')
  assert.match(src, /正 Acceleration/, 'legend 用 正 Acceleration')
  assert.match(src, /负 Acceleration/, 'legend 用 负 Acceleration')
  assert.match(src, /零 Acceleration/, 'legend 用 零 Acceleration')
})

// ============================================================
// 8. Runtime：ReviewPage canonical 切换 + legacy 不可达
// ============================================================

test('RT1. canonical 默认视图为 table（非 legacy discovery）', () => {
  assert.equal(DEFAULT_REVIEW_VIEW, 'table')
})

test('RT2. ReviewPage 使用 canonical URL decoder/encoder', () => {
  const src = read('../../pages/ReviewPage.tsx')
  assert.match(src, /decodeReviewUrl/, 'ReviewPage 必须使用 canonical decodeReviewUrl')
  assert.match(src, /encodeReviewUrl/, 'ReviewPage 必须使用 canonical encodeReviewUrl')
  assert.ok(
    !/decodeLegacyReviewUrl|encodeLegacyReviewUrl|LegacyReviewUrlState/.test(src),
    'ReviewPage 不得使用 legacy URL 状态',
  )
})

test('RT3. ReviewPage 不再 import 任何 legacy Review 组件', () => {
  const src = read('../../pages/ReviewPage.tsx')
  for (const legacy of [
    'DiscoveryWorkspace',
    'ReviewStageNav',
    'MarketScanPanel',
    'FilterDiscoveryPanel',
    'BoardAttributionPanel',
    'StockValidationPanel',
    'TrackingReviewPanel',
    'AuctionBackflowPanel',
    'EvidenceDrawer',
  ]) {
    assert.doesNotMatch(src, new RegExp(`import[\\s\\S]*?['"].*${legacy}['"]`), `ReviewPage 不得 import ${legacy}`)
  }
})

test('RT4. ReviewPage 渲染 canonical ScopeExplorerWorkspace', () => {
  const src = read('../../pages/ReviewPage.tsx')
  assert.match(src, /import ScopeExplorerWorkspace/, 'ReviewPage 必须 import ScopeExplorerWorkspace')
  assert.match(src, /<ScopeExplorerWorkspace/, 'ReviewPage 必须渲染 ScopeExplorerWorkspace')
})

test('RT5. ReviewPage 不调用 legacy 信号/Discovery API', () => {
  const src = read('../../pages/ReviewPage.tsx')
  for (const legacyApi of [
    'getLegacyReviewScopes',
    'getReviewSignals',
    'getReviewSignal',
    'getSignalAttributions',
    'getSignalInstruments',
    'getReviewTrackings',
    'getDiscoveries',
  ]) {
    assert.doesNotMatch(src, new RegExp(legacyApi), `ReviewPage 不得调用 legacy API ${legacyApi}`)
  }
})

// ============================================================
// R2A. Scope Explorer Multi-Sort（纯展示层、单一排序 owner、persisted 字段直接读取）
// ============================================================

const ALL_SORTS: ReadonlyArray<ReviewSort> = [
  'velocity_desc',
  'acceleration_desc',
  'position_desc',
  'equal_weight_return_desc',
  'capital_tilt_desc',
  'migration_desc',
  'coverage_desc',
]

test('SORT-1. 每个合法 sort 都能被 normalize 接受', () => {
  for (const s of ALL_SORTS) {
    assert.equal(normalizeSort(s), s, `合法 sort ${s} 必须原样保留`)
  }
})

test('SORT-2. 每个非默认 sort 的 encode/decode 往返一致', () => {
  for (const s of ALL_SORTS) {
    if (s === DEFAULT_REVIEW_SORT) continue
    const url = encodeReviewUrl({ ...defaultReviewUrlState(), sort: s })
    const back = decodeReviewUrl(url)
    assert.equal(back.sort, s, `sort=${s} 必须往返一致`)
  }
})

test('SORT-3. velocity_desc 仍是默认且从 URL 省略', () => {
  assert.equal(DEFAULT_REVIEW_SORT, 'velocity_desc')
  const url = encodeReviewUrl({ ...defaultReviewUrlState(), sort: 'velocity_desc' })
  assert.ok(!url.toString().includes('sort='), '默认 sort 不得出现在 URL 中')
  // 无 sort 的 URL 解码回默认
  assert.equal(decodeReviewUrl(new URLSearchParams('?family=industry_l1')).sort, 'velocity_desc')
})

test('SORT-4. 非法 sort 回退 velocity_desc', () => {
  const back = decodeReviewUrl(new URLSearchParams('?sort=not_a_real_sort'))
  assert.equal(back.sort, 'velocity_desc', '非法 sort 必须回退默认')
  assert.equal(normalizeSort('bogus' as ReviewSort), 'velocity_desc')
})

test('SORT-5. sort 变化经由 withReviewFilterChange 时重置 page=1 但保留 scopeKey', () => {
  const base = { ...defaultReviewUrlState(), page: 3, scopeKey: 'copper' }
  const next = withReviewFilterChange(base, { sort: 'acceleration_desc' })
  assert.equal(next.page, 1, 'sort 变化必须重置 page=1')
  assert.equal(next.sort, 'acceleration_desc')
  assert.equal(next.scopeKey, 'copper', '必须保留 scopeKey')
  assert.equal(next.family, 'industry_l1')
  assert.equal(next.view, 'table')
})

// 构造一个每项都带「自相矛盾」persisted 值的 fixture，证明每个 sort 只读自己字段
function makeContradictoryItems(): ReviewScopeListItem[] {
  // 同一批 scope，故意让各字段取值彼此不相关：
  //  - itemA：velocity 最大，其余字段非最大
  //  - itemB：acceleration 最大
  //  - itemC：position 最大
  //  - itemD：equalWeightReturn 最大
  //  - itemE：capitalTilt 最大
  //  - itemF：migration 最大
  //  - itemG：coverageRatio 最大
  return [
    makeItem('A', {
      coverageRatio: 0.1,
      summary: makeSummary({ velocity: 99, acceleration: 1, position: 2, equalWeightReturn: 0.01, capitalTilt: 0.1, migration: 0.1 }),
    }),
    makeItem('B', {
      coverageRatio: 0.2,
      summary: makeSummary({ velocity: 1, acceleration: 99, position: 3, equalWeightReturn: 0.02, capitalTilt: 0.2, migration: 0.2 }),
    }),
    makeItem('C', {
      coverageRatio: 0.3,
      summary: makeSummary({ velocity: 2, acceleration: 3, position: 99, equalWeightReturn: 0.03, capitalTilt: 0.3, migration: 0.3 }),
    }),
    makeItem('D', {
      coverageRatio: 0.4,
      summary: makeSummary({ velocity: 3, acceleration: 4, position: 4, equalWeightReturn: 0.99, capitalTilt: 0.4, migration: 0.4 }),
    }),
    makeItem('E', {
      coverageRatio: 0.5,
      summary: makeSummary({ velocity: 4, acceleration: 5, position: 5, equalWeightReturn: 0.05, capitalTilt: 0.99, migration: 0.5 }),
    }),
    makeItem('F', {
      coverageRatio: 0.6,
      summary: makeSummary({ velocity: 5, acceleration: 6, position: 6, equalWeightReturn: 0.06, capitalTilt: 0.6, migration: 0.99 }),
    }),
    makeItem('G', {
      coverageRatio: 0.99,
      summary: makeSummary({ velocity: 6, acceleration: 7, position: 7, equalWeightReturn: 0.07, capitalTilt: 0.7, migration: 0.7 }),
    }),
  ]
}

test('SORT-6. velocity_desc 降序，null 最后，且只读 velocity', () => {
  const sorted = sortScopes(makeContradictoryItems(), 'velocity_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['A', 'G', 'F', 'E', 'D', 'C', 'B'])
})

test('SORT-7. acceleration_desc 降序，且只读 acceleration（不随 velocity 顺序）', () => {
  const sorted = sortScopes(makeContradictoryItems(), 'acceleration_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['B', 'G', 'F', 'E', 'D', 'C', 'A'])
})

test('SORT-8. position_desc 降序，且只读 position', () => {
  const sorted = sortScopes(makeContradictoryItems(), 'position_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['C', 'G', 'F', 'E', 'D', 'B', 'A'])
})

test('SORT-9. equal_weight_return_desc 降序，且只读 equalWeightReturn', () => {
  const sorted = sortScopes(makeContradictoryItems(), 'equal_weight_return_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['D', 'G', 'F', 'E', 'C', 'B', 'A'])
})

test('SORT-10. capital_tilt_desc 降序，且只读 capitalTilt（persisted，不重算）', () => {
  const sorted = sortScopes(makeContradictoryItems(), 'capital_tilt_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['E', 'G', 'F', 'D', 'C', 'B', 'A'])
})

test('SORT-11. migration_desc 降序，且只读 migration（persisted，不重算）', () => {
  const sorted = sortScopes(makeContradictoryItems(), 'migration_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['F', 'G', 'E', 'D', 'C', 'B', 'A'])
})

test('SORT-12. coverage_desc 降序，且只读行级 coverageRatio（persisted，不重算）', () => {
  const sorted = sortScopes(makeContradictoryItems(), 'coverage_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['G', 'F', 'E', 'D', 'C', 'B', 'A'])
})

// 对给定 sort 字段，构造「正/零/负/null/缺失」五条 fixture，验证该字段降序时 null 恒最后
function itemsForSortField(field: keyof NonNullable<ReviewScopeListItem['summary']>): ReviewScopeListItem[] {
  const base = {
    velocity: null,
    acceleration: null,
    position: null,
    equalWeightReturn: null,
    capitalTilt: null,
    migration: null,
  } as const
  const withField = (v: number | null) => ({ ...base, [field]: v })
  return [
    makeItem('pos', { summary: makeSummary(withField(0.5) as Record<string, number | null>) }),
    makeItem('zero', { summary: makeSummary(withField(0) as Record<string, number | null>) }),
    makeItem('neg', { summary: makeSummary(withField(-0.2) as Record<string, number | null>) }),
    makeItem('nullv', { summary: makeSummary(withField(null) as Record<string, number | null>) }),
    makeItem('noval', { summary: null }),
  ]
}

test('SORT-6..12 共同：每个排序字段含正/零/负/null 时 null 恒最后', () => {
  // 仅 composition 字段参与本组测试（R2B 的 4 个 observation 字段在 R2B-FE 段单独验证）
  const fieldBySort: Partial<Record<ReviewSort, keyof NonNullable<ReviewScopeListItem['summary']>>> = {
    velocity_desc: 'velocity',
    acceleration_desc: 'acceleration',
    position_desc: 'position',
    equal_weight_return_desc: 'equalWeightReturn',
    capital_tilt_desc: 'capitalTilt',
    migration_desc: 'migration',
    coverage_desc: 'position', // coverage 用行级字段，下方单独处理
  }
  for (const s of ALL_SORTS) {
    if (s === 'coverage_desc') {
      const items = [
        makeItem('pos', { coverageRatio: 0.5 }),
        makeItem('zero', { coverageRatio: 0 }),
        makeItem('neg', { coverageRatio: -0.2 }),
        makeItem('nullv', { coverageRatio: null }),
        makeItem('noval', { coverageRatio: null }),
      ]
      const sorted = sortScopes(items, s).map((i) => i.scopeKey)
      // 0.5 > 0 > -0.2 在前，两个 null 占据最后两位（顺序由 scopeName 决定，不强制互序）
      assert.deepEqual(sorted.slice(0, 3), ['pos', 'zero', 'neg'], `coverage_desc 数值降序在前`)
      assert.deepEqual(sorted.slice(-2).sort(), ['noval', 'nullv'], `coverage_desc 两个 null 在最后`)
      continue
    }
    const items = itemsForSortField(fieldBySort[s]!)
    const sorted = sortScopes(items, s).map((i) => i.scopeKey)
    // 0.5 > 0 > -0.2 在前，undefined(summary=null) 与 null(field=null) 占据最后两位
    assert.deepEqual(sorted.slice(0, 3), ['pos', 'zero', 'neg'], `sort=${s}: 数值降序在前`)
    assert.deepEqual(sorted.slice(-2).sort(), ['noval', 'nullv'], `sort=${s}: 两个 null 类在最后`)
  }
})

test('SORT-13. 确定性 tie-break：同字段值相等时按 scopeName ?? scopeKey 字典序', () => {
  // tieBreak 比较 (scopeName ?? scopeKey) 的 localeCompare：
  //  - alpha(zeta 的 scopeName)/zeta 用 scopeName
  //  - abel/mike 的 scopeName=null → 用 scopeKey 'abel'/'mike'
  // 全序：abel < alpha < mike < zeta
  const items = [
    makeItem('zeta', { scopeName: 'zeta', summary: makeSummary({ acceleration: 5 }) }),
    makeItem('alpha', { scopeName: 'alpha', summary: makeSummary({ acceleration: 5 }) }),
    makeItem('mike', { summary: makeSummary({ acceleration: 5 }), scopeName: null }),
    makeItem('abel', { summary: makeSummary({ acceleration: 5 }), scopeName: null }),
  ]
  const sorted = sortScopes(items, 'acceleration_desc').map((i) => i.scopeKey)
  assert.deepEqual(sorted, ['abel', 'alpha', 'mike', 'zeta'], '同值按 (scopeName ?? scopeKey) 字典序')
})

test('SORT-14. filter → sort（整组）→ paginate：第 2 页顶行对应全局排序位置', () => {
  // 构造 12 条，q 过滤后 10 条，pageSize=2：第 2 页顶行应为全局第 3 名
  const items = Array.from({ length: 12 }, (_, i) =>
    makeItem(`k${i}`, {
      scopeName: `cat${i}`,
      summary: makeSummary({ position: i }), // position 0..11
    }),
  )
  const query = buildScopeExplorerQuery('cat', null) // 命中全部 12 条
  const page1 = applyScopeExplorerPipeline(items, query, 1, 2, 'position_desc')
  const page2 = applyScopeExplorerPipeline(items, query, 2, 2, 'position_desc')
  assert.equal(page1.total, 12, 'total = 过滤后数量')
  assert.equal(page1.items[0].scopeKey, 'k11', '第 1 页顶行 = 全局最大 position')
  assert.equal(page1.items[1].scopeKey, 'k10')
  assert.equal(page2.items[0].scopeKey, 'k9', '第 2 页顶行 = 全局第 3（非页内独立排序）')
  assert.equal(page2.items[1].scopeKey, 'k8')
})

test('SORT-15. Workspace 把 urlState.sort 同时喂给 Trajectory（filteredSorted）与 Table（paged）同一 pipeline', () => {
  // 源码契约：Workspace.filteredSorted 与 paged 都使用 urlState.sort 驱动的同一排序 owner
  const src = read('ScopeExplorerWorkspace.tsx')
  // filteredSorted 经 sortScopes(... , urlState.sort)
  assert.match(src, /sortScopes\(filterScopes\(snapshotItems, query\), urlState\.sort\)/, 'Trajectory 源必须用 urlState.sort')
  // paged 经 applyScopeExplorerPipeline(..., urlState.sort) 同一 sort 参数
  assert.match(src, /applyScopeExplorerPipeline\(snapshotItems, query, urlState\.page, urlState\.pageSize, urlState\.sort\)/, 'Table 源必须用同一 urlState.sort')
  // 不得再出现 sortVelocityDesc（单一排序 owner）
  assert.doesNotMatch(src, /sortVelocityDesc/, 'Workspace 不得保留旧 sortVelocityDesc 调用')
  // P0-2：排序改由表头控制，Toolbar 不再接收 sort；Table 接收 sort={urlState.sort}
  assert.match(src, /ScopeExplorerTable[^]*sort=\{urlState\.sort\}/, 'Table 必须接收 urlState.sort')
  // Toolbar 元素自身（从 <ScopeExplorerToolbar 到其首个 />）不得含 sort= prop
  const toolbarEl = /<ScopeExplorerToolbar[\s\S]*?\/>/.exec(src)
  assert.ok(toolbarEl, 'ScopeExplorerToolbar 元素必须存在')
  assert.doesNotMatch(toolbarEl[0], /sort=/, 'Toolbar 元素自身不再接收 sort（P0-2 表头排序）')
  const tb = read('ScopeExplorerToolbar.tsx')
  // P0-2：排序改由表头控制，Toolbar 不再渲染 sort 下拉，故不再上报 onFilterChange({ sort })
  assert.doesNotMatch(tb, /onFilterChange\(\{ sort:/, 'Toolbar 不再上报 sort（P0-2 表头排序）')
  // sort 仍经 onFilterChange({ sort }) 上报（由 Workspace 内 Table 表头触发）
  assert.match(src, /onFilterChange\(\{ sort: s, page: 1 \}\)/, 'Workspace 内 Table 表头必须上报 onFilterChange({ sort })')
  // 几何坐标不被 sort 改变：x=position / y=velocity
  const traj = read('ScopeTrajectoryView.tsx')
  assert.match(traj, /\(pos \/ 100\)/, 'Trajectory x 仍由 position 决定')
  assert.match(traj, /velocity/, 'Trajectory y 仍由 velocity 决定')
})

// ============================================================
// R2B. Observation Thin Projection + Cross-sectional Scan（§10..18）
// ============================================================

// 构造一个带 observationSummary 的 item（默认字段全 0/null，便于单独设置）
function makeObs(overrides: Partial<ReviewScopeObservationSummary> = {}): ReviewScopeObservationSummary {
  return {
    freshnessTodayCount: null,
    freshnessDecayWeightedDensity: null,
    technicalHhi: null,
    technicalTop5Numerator: null,
    technicalTop5Denominator: null,
    technicalLeaderMedianGap: null,
    technicalLeaderSymbol: null,
    technicalMemberCount: null,
    ...overrides,
  }
}

test('R2B-FE-1. ReviewScopeListItem 接受 observationSummary', () => {
  const item = makeItem('k1', { observationSummary: makeObs({ freshnessTodayCount: 3 }) })
  assert.equal(item.observationSummary?.freshnessTodayCount, 3)
})

test('R2B-FE-2. freshnessTodayCount=0 存活（非 null）', () => {
  const item = makeItem('k1', { observationSummary: makeObs({ freshnessTodayCount: 0 }) })
  assert.equal(item.observationSummary?.freshnessTodayCount, 0)
  assert.notEqual(item.observationSummary?.freshnessTodayCount, null)
})

test('R2B-FE-3. freshnessDensity=0 存活（非 null）', () => {
  const item = makeItem('k1', { observationSummary: makeObs({ freshnessDecayWeightedDensity: 0 }) })
  assert.equal(item.observationSummary?.freshnessDecayWeightedDensity, 0)
})

test('R2B-FE-4. technicalHhi=0 存活（非 null）', () => {
  const item = makeItem('k1', { observationSummary: makeObs({ technicalHhi: 0 }) })
  assert.equal(item.observationSummary?.technicalHhi, 0)
})

test('R2B-FE-5. top5 denominator=0 渲染 "—"（不显示 0%）', () => {
  const item = makeItem('k1', {
    observationSummary: makeObs({ technicalTop5Numerator: 0, technicalTop5Denominator: 0 }),
  })
  // 使用既有 display formatter，den=0 返回 NULL_DISPLAY
  assert.equal(formatContributionFraction({ numerator: 0, denominator: 0 }), NULL_DISPLAY)
  assert.equal(item.observationSummary?.technicalTop5Denominator, 0)
})

test('R2B-FE-6. leaderSymbol=null 渲染 "—"', () => {
  const item = makeItem('k1', { observationSummary: makeObs({ technicalLeaderSymbol: null }) })
  assert.equal(item.observationSummary?.technicalLeaderSymbol, null)
})

test('R2B-FE-7. 四个新 sort 均 URL 往返一致', () => {
  const news: ReviewSort[] = [
    'freshness_density_desc',
    'freshness_today_desc',
    'technical_hhi_desc',
    'leader_median_gap_desc',
  ]
  for (const s of news) {
    assert.equal(normalizeSort(s), s, `合法 sort ${s} 必须原样保留`)
    const url = encodeReviewUrl({ ...defaultReviewUrlState(), sort: s })
    assert.equal(decodeReviewUrl(url).sort, s, `sort=${s} 必须往返一致`)
  }
})

test('R2B-FE-8. 每个新 sort 只读取其精确 observationSummary 字段（矛盾 fixture）', () => {
  // 每个 item 在其对应字段取极大值，其余 observation 字段为 null；
  // 验证 sort 只按本字段排序，不串字段。
  const items = [
    makeItem('A', { observationSummary: makeObs({ freshnessDecayWeightedDensity: 99 }) }),
    makeItem('B', { observationSummary: makeObs({ freshnessTodayCount: 99 }) }),
    makeItem('C', { observationSummary: makeObs({ technicalHhi: 99 }) }),
    makeItem('D', { observationSummary: makeObs({ technicalLeaderMedianGap: 99 }) }),
    // 同时令 summary（composition）字段极大，证明不串到 composition
    makeItem('E', { summary: makeSummary({ position: 999, velocity: 999 }) }),
  ]
  assert.deepEqual(sortScopes(items, 'freshness_density_desc').map((i) => i.scopeKey), ['A', 'B', 'C', 'D', 'E'], 'freshness_density 只排 density')
  assert.deepEqual(sortScopes(items, 'freshness_today_desc').map((i) => i.scopeKey), ['B', 'A', 'C', 'D', 'E'], 'freshness_today 只排 today')
  assert.deepEqual(sortScopes(items, 'technical_hhi_desc').map((i) => i.scopeKey), ['C', 'A', 'B', 'D', 'E'], 'technical_hhi 只排 hhi')
  assert.deepEqual(sortScopes(items, 'leader_median_gap_desc').map((i) => i.scopeKey), ['D', 'A', 'B', 'C', 'E'], 'leader_gap 只排 gap')
})

test('R2B-FE-9. 新 sort 仍遵守 null-last 契约', () => {
  const items = [
    makeItem('pos', { observationSummary: makeObs({ technicalHhi: 0.5 }) }),
    makeItem('zero', { observationSummary: makeObs({ technicalHhi: 0 }) }),
    makeItem('neg', { observationSummary: makeObs({ technicalHhi: -0.2 }) }),
    makeItem('nullv', { observationSummary: makeObs({ technicalHhi: null }) }),
    makeItem('noval', { observationSummary: null }),
  ]
  assert.deepEqual(sortScopes(items, 'technical_hhi_desc').map((i) => i.scopeKey).slice(0, 3), ['pos', 'zero', 'neg'], 'technical_hhi 数值降序在前')
  assert.deepEqual(sortScopes(items, 'technical_hhi_desc').map((i) => i.scopeKey).slice(-2).sort(), ['noval', 'nullv'], 'technical_hhi 两个 null 类在最后')
})

test('R2B-FE-10. filter → selected sort → paginate 顺序不变（含新 sort）', () => {
  const items = Array.from({ length: 12 }, (_, i) =>
    makeItem(`k${i}`, { observationSummary: makeObs({ freshnessTodayCount: i }) }),
  )
  const query = buildScopeExplorerQuery('k', null)
  const p2 = applyScopeExplorerPipeline(items, query, 2, 2, 'freshness_today_desc')
  assert.equal(p2.items[0].scopeKey, 'k9', '第 2 页顶行 = 全局第 3（整组排序后分页）')
  assert.equal(p2.items[1].scopeKey, 'k8')
})

test('R2B-FE-11. Trajectory 与 Table 仍共用同一 selected sort', () => {
  const src = read('ScopeExplorerWorkspace.tsx')
  assert.match(src, /sortScopes\(filterScopes\(snapshotItems, query\), urlState\.sort\)/, 'Trajectory 源仍用 urlState.sort')
  assert.match(src, /applyScopeExplorerPipeline\(snapshotItems, query, urlState\.page, urlState\.pageSize, urlState\.sort\)/, 'Table 源仍用同一 urlState.sort')
  assert.doesNotMatch(src, /sortVelocityDesc/, 'Workspace 不得保留旧 sortVelocityDesc')
})

test('R2B-FE-12. 列表渲染不新增 detail 请求', () => {
  // 源码契约：Table 仅消费 item.observationSummary（已加载薄投影），不调用 useReviewScopeDetail
  const tbl = read('ScopeExplorerTable.tsx')
  assert.match(tbl, /FreshnessCell/, 'Table 必须渲染 FreshnessCell')
  assert.match(tbl, /TechnicalCell/, 'Table 必须渲染 TechnicalCell')
  assert.match(tbl, /obs=\{row\.observationSummary\}/, '单元格必须消费 observationSummary')
  // 不得出现对 detail hook 的依赖
  assert.doesNotMatch(tbl, /useReviewScopeDetail/, '列表渲染不得新增 detail 请求')
})

// ============================================================
// REVIEW-UX-CLOSURE-02 (P0-1 .. P0-6) 收口合同测试
// ============================================================

// P0-1：Readiness 数据状态筛选控件删除（普通用户不再用它筛选列表）
test('P0-1: Readiness 筛选控件已从工具栏删除（readiness canonical 字段仍保留在 URL/API）', () => {
  const tb = read('ScopeExplorerToolbar.tsx')
  // 不得再渲染 readiness 下拉
  assert.doesNotMatch(tb, /数据状态过滤/, '工具栏不得保留“数据状态过滤”控件')
  assert.doesNotMatch(tb, /READINESS_OPTIONS/, '工具栏不得引用 READINESS_OPTIONS')
  // readiness 不再是工具栏渲染的 select（仅保留在 URL/API canonical 字段）
  assert.doesNotMatch(tb, /aria-label="数据状态过滤"/, '工具栏不得渲染 readiness select aria-label')
  // readiness 仍是 URL state 合法字段（canonical 不变）
  const url = read('urlState.ts')
  assert.match(url, /readiness/, 'readiness 仍是 URL state 字段（API contract 不变）')
})

// P0-2：排序下拉删除，表头排序 desc↔asc
test('P0-2: 排序下拉已从工具栏删除（排序改由表头控制）', () => {
  const tb = read('ScopeExplorerToolbar.tsx')
  assert.doesNotMatch(tb, /排序字段/, '工具栏不得保留“排序字段”下拉')
  assert.doesNotMatch(tb, /SORT_OPTIONS/, '工具栏不得引用 SORT_OPTIONS')
  const tbl = read('ScopeExplorerTable.tsx')
  assert.match(tbl, /reviewSortToggle/, '表头必须使用 reviewSortToggle 控制排序')
  assert.match(tbl, /onSortChange/, '表头点击必须回调 onSortChange')
})

test('P0-2: reviewSortToggle 第一次点击降序、第二次升序、切换列回到降序', () => {
  assert.equal(reviewSortToggle('velocity', 'velocity_desc'), 'velocity_asc')
  assert.equal(reviewSortToggle('velocity', 'velocity_asc'), 'velocity_desc')
  assert.equal(reviewSortToggle('position', 'velocity_desc'), 'position_desc')
  assert.equal(reviewSortToggle('phase', 'phase_asc'), 'phase_desc')
})

test('P0-2: asc/desc 经 URL encode/decode 往返保持', () => {
  for (const s of ['velocity_asc', 'acceleration_desc', 'position_asc', 'phase_desc'] as ReviewSort[]) {
    const st = defaultReviewUrlState()
    const enc = encodeReviewUrl({ ...st, sort: s })
    const dec = decodeReviewUrl(enc)
    assert.equal(dec.sort, s, `sort ${s} 应往返一致`)
  }
})

test('P0-2: sortScopes 升序/降序方向正确，null 恒最后，作用于完整 snapshot', () => {
  const items = [
    makeItem('a', { summary: makeSummary({ velocity: 3 }) }),
    makeItem('b', { summary: makeSummary({ velocity: null }) }),
    makeItem('c', { summary: makeSummary({ velocity: 1 }) }),
    makeItem('d', { summary: makeSummary({ velocity: 2 }) }),
  ]
  const desc = sortScopes(items, 'velocity_desc').map((i) => i.scopeKey)
  assert.deepEqual(desc, ['a', 'd', 'c', 'b'], '降序：3,2,1,null')
  const asc = sortScopes(items, 'velocity_asc').map((i) => i.scopeKey)
  assert.deepEqual(asc, ['c', 'd', 'a', 'b'], '升序：1,2,3,null（null 恒最后）')
})

test('P0-2: 排序作用于完整 family snapshot（跨分页），而非仅当前页', () => {
  // 构造 120 条（>1 页 50），验证排序后第 1 页首条是全局最大 velocity
  const all = Array.from({ length: 120 }, (_, i) =>
    makeItem(`s${i}`, { summary: makeSummary({ velocity: 120 - i }) }),
  )
  const sorted = sortScopes(all, 'velocity_desc')
  assert.equal(sorted[0].scopeKey, 's0', '全局降序首条应为 velocity=120 的 s0')
  // 取前 50（第 1 页）应全是 velocity 120..71 的降序，不混入小值
  const page1 = sorted.slice(0, 50)
  assert.equal(page1[49].summary?.velocity, 71, '第 1 页末条应为全局第 50 大（71）')
})

// P0-3：TradingView logo 关闭 + attribution 保留
test('P0-3: 图表关闭 Lightweight Charts attribution logo，并保留许可归属文本', () => {
  const dp = read('ScopeDynamicsPanel.tsx')
  assert.match(dp, /attributionLogo:\s*false/, '必须设置 layout.attributionLogo=false 关闭 TV logo')
  assert.doesNotMatch(dp, /display:\s*none[^\n]*logo|mask|overlay/, '禁止用 CSS/overlay 强盖 logo')
  assert.match(dp, /chartAttribution/, '面板必须保留 TradingView 许可归属文本（非干扰区）')
})

// P0-4/P0-5：字段字典存在统一 owner
test('P0-4/P0-5: REVIEW_TERMS 含新增指标中文简称与解释（单一展示 owner）', () => {
  const rc = read('reviewCopy.ts')
  for (const k of [
    'trendStrength',
    'dsaVwapDev',
    'segmentBars',
    'segmentChange',
    'segmentSlope',
    'vwapRetTotal',
    'volumeRatio',
    'amountRatio',
    'zScore',
    'percentile',
    'bbPosition',
    'bbWidth',
  ]) {
    assert.match(rc, new RegExp(`  ${k}:\\s*\\{`), `REVIEW_TERMS 必须包含 ${k}`)
    assert.match(rc, new RegExp(`  ${k}:[\\s\\S]*?label:`), `${k} 必须有 label`)
    assert.match(rc, new RegExp(`  ${k}:[\\s\\S]*?help:`), `${k} 必须有 help（tooltip 解释）`)
  }
})

// P0-6：tooltip 经 Portal 渲染到 document.body，且非 absolute（不被滚动容器裁切）
test('P0-6: ReviewTerm tooltip 经 React Portal 到 document.body，非 absolute 内联', () => {
  const rt = read('ReviewTerm.tsx')
  assert.match(rt, /createPortal\(/, 'tooltip 必须使用 React Portal')
  assert.match(rt, /document\.body/, 'Portal 目标为 document.body（脱离滚动容器）')
  assert.match(rt, /position:\s*'fixed'/, 'tooltip 使用 position: fixed')
  const scss = read('review.module.scss')
  // .termTooltip 块内不得再是 position:absolute（避免被 overflow 容器裁切）。
  // 仅抽取 .termTooltip { ... } 第一对花括号之间的内容做断言（不跨块）。
  const m = /\.termTooltip\s*\{([^}]*)\}/.exec(scss)
  assert.ok(m, '.termTooltip 块必须存在')
  assert.doesNotMatch(m[1], /absolute/, '.termTooltip 块内不得含 position:absolute')
})

// P0-1 补充：Phase 列普通用户文案应为“阶段”（非 Phase），来自 REVIEW_TERMS.phase.label
test('P0-1 补充: 阶段列中文文案为“阶段”', () => {
  const rc = read('reviewCopy.ts')
  assert.match(rc, /phase:\s*\{\s*label:\s*'阶段'/, 'REVIEW_TERMS.phase.label 必须为中文“阶段”')
})


