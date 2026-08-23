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
  applyScopeExplorerPipeline,
  findScopeById,
} from '../scopeExplorerViewModel'
import { loadFamilySnapshot, FAMILY_SNAPSHOT_PAGE_SIZE } from '../useReviewScopeFamilySnapshot'
import { formatPosition, formatPercentNullable, NULL_DISPLAY } from '../reviewFormat'
import { DEFAULT_REVIEW_VIEW } from '../urlState'
import type {
  ReviewScopeListItem,
  ReviewScopeSummary,
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
    filterScopes(items, buildScopeExplorerQuery('有色', null, null)).map((i) => i.scopeKey),
    ['copper'],
  )
  // scopeKey 精确 + 大小写不敏感
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('copper', null, null)).map((i) => i.scopeKey),
    ['copper'],
  )
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('COPPER', null, null)).map((i) => i.scopeKey),
    ['copper'],
  )
  // q 空 → 全部
  assert.equal(filterScopes(items, buildScopeExplorerQuery('', null, null)).length, 3)
  // q 只匹配 key 时也命中（不搜索任意 JSON）
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('bank', null, null)).map((i) => i.scopeKey),
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
    filterScopes(items, buildScopeExplorerQuery('', 'Strengthening', null)).map((i) => i.scopeKey),
    ['a'],
  )
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('', 'Weakening', null)).map((i) => i.scopeKey),
    ['b'],
  )
  // phase=null → 全部（含 summary=null 的项）
  assert.equal(filterScopes(items, buildScopeExplorerQuery('', null, null)).length, 3)
})

test('VM3. readiness 精确过滤；readiness=null 不过滤', () => {
  const items = [
    makeItem('a', { readiness: 'ready' }),
    makeItem('b', { readiness: 'insufficient_history' }),
    makeItem('c', { readiness: 'unavailable_current' }),
  ]
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('', null, 'ready')).map((i) => i.scopeKey),
    ['a'],
  )
  assert.deepEqual(
    filterScopes(items, buildScopeExplorerQuery('', null, 'insufficient_history')).map((i) => i.scopeKey),
    ['b'],
  )
  assert.equal(filterScopes(items, buildScopeExplorerQuery('', null, null)).length, 3)
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
  const q = buildScopeExplorerQuery('有色', null, null)
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
  const fetchPage = (page: number): Promise<ReviewScopeListResponse> => {
    calls.push(page)
    // 每个请求各自等待 gate；Promise.all 会同步发起全部剩余页请求
    return new Promise((resolve) => {
      gates[page] = () => resolve(makeListResponse([makeItem(`k${page}`)], page, 100, 250))
    })
  }
  const pending = loadFamilySnapshot(fetchPage, FAMILY_SNAPSHOT_PAGE_SIZE)
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

// ============================================================
// 6. Table：canonical 列 + 无 legacy 字段 + null 显示
// ============================================================

test('T1. Table 精确 canonical 列（10 列，无 p/q/u/c/v/signalCount）', () => {
  const src = read('ScopeExplorerTable.tsx')
  const expected = [
    'Scope',
    'Phase',
    'Position',
    'Velocity',
    'Acceleration',
    'EW Return',
    'Capital Tilt',
    'Breadth',
    'Leadership Migration',
    'Coverage',
  ]
  for (const col of expected) {
    assert.ok(src.includes(`>${col}<`) || src.includes(`>{${'col'}}`), `Table 必须含列 ${col}`)
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
