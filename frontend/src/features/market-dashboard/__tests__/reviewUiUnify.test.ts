// [PANJI-REVIEW-UI-UNIFY] Review 列表/详情统一工作区 契约测试（源码 + 逻辑，无 DOM）。
//
// 覆盖任务规格 R1–R15：
//   R1   顶层 Review 标签仅 大盘/行业/概念，无对比 tab
//   R2   进入行业/概念先渲染 LIST VIEW（listWorkspace），与详情互斥（不常驻主从分栏）
//   R3   概念同样走列表优先（scopeType=concept 共用 ScopeExplorerPage）
//   R4   排序交互保留（onSort 三分支）
//   R5   筛选交互保留（列筛选弹层 applyColumnFilter）
//   R6   搜索入口保留（search-input）
//   R7   清除筛选入口保留（clear-all-filters）
//   R8   列表态分页保留（pagination + goPage）
//   R9   点击行 → DETAIL VIEW（scope-detail-view / scope-rail / detail-back）
//   R10  左导航栏仅显示板块名称（rail-item 只渲染 board_name，不渲染指标）
//   R11  左导航栏 = 同一份 server 过滤+排序结果全集（railExplorer 复用 explorer hook，page_size=上限）
//   R12  选中 rail / 行仅改变 board_id，精确保留列表 page
//   R13  返回列表清除 board_id，精确保留列表状态（URL 即 SSOT）
//   R14  右详情复用 canonical（useMarketScopeDetail + BreadthChart + meta.member_count/name）
//   R15  大盘页核心契约未被本任务破坏（DashboardTabs + 统一 BreadthChart + rankings）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { REVIEW_TABS } from '../reviewTabs'
import {
  clearAllStatePatch,
  isExplorerResetDisabled,
  NUMERIC_FILTER_KEYS,
  type NumericFilterKey,
} from '../scopeExplorerUrlState'
import {
  fetchScopeExplorerUniverse,
  scopeExplorerQueryKey,
  SCOPE_EXPLORER_SORT_DEFAULT,
  SCOPE_EXPLORER_DIRECTION_DEFAULT,
  type ScopeExplorerQuery,
} from '../scopeExplorerQuery'
import type { ScopeExplorerItem, ScopeExplorerResponse } from '../../../api/marketDashboard'

const PAGE_SOURCE = readFileSync(new URL('../ScopeExplorerPage.tsx', import.meta.url), 'utf8')
const MARKET_PAGE_SOURCE = readFileSync(new URL('../MarketDashboardPage.tsx', import.meta.url), 'utf8')

// ===========================================================================
// R1. 顶层 tab 无对比
// ===========================================================================
test('R1. REVIEW_TABS 仅三项且不含对比', () => {
  assert.equal(REVIEW_TABS.length, 3)
  assert.deepEqual(REVIEW_TABS.map((t) => t.key), ['market', 'industry', 'concept'])
  assert.deepEqual(REVIEW_TABS.map((t) => t.label), ['大盘', '行业', '概念'])
})

// ===========================================================================
// R2/R3. 列表优先 + 与详情互斥（不常驻主从分栏）
// ===========================================================================
test('R2/R3. 列表与详情是互斥工作区（listWorkspace / detailWorkspace），不常驻分栏', () => {
  assert.ok(PAGE_SOURCE.includes('listWorkspace'), '必须存在 listWorkspace')
  assert.ok(PAGE_SOURCE.includes('detailWorkspace'), '必须存在 detailWorkspace')
  assert.match(PAGE_SOURCE, /const listWorkspace = !parsed\.board_id &&/, '列表态仅在无 board_id 时渲染')
  assert.match(PAGE_SOURCE, /const detailWorkspace = parsed\.board_id &&/, '详情态仅在选中 board_id 时渲染')
})

// ===========================================================================
// R9. list → detail 过渡标记
// ===========================================================================
test('R9. 详情视图用 testid 标记 scope-detail-view / scope-rail / detail-back', () => {
  assert.match(PAGE_SOURCE, /data-testid="scope-detail-view"/)
  assert.match(PAGE_SOURCE, /data-testid="scope-rail"/)
  assert.match(PAGE_SOURCE, /data-testid="detail-back"/)
})

// ===========================================================================
// R10. rail 仅名称
// ===========================================================================
test('R10. rail 仅渲染板块名称（不含成员数/MA 等指标字段）', () => {
  assert.match(PAGE_SOURCE, /data-testid=\{`rail-item-\$\{it\.board_id\}`\}/, 'rail 项按 board_id 标记')
  assert.match(PAGE_SOURCE, /\{it\.board_name\}/, 'rail 按钮只渲染板块名称')
  assert.ok(!PAGE_SOURCE.includes('it.member_count'), 'rail 不得渲染成员数')
  assert.ok(!PAGE_SOURCE.includes('it.ma5'), 'rail 不得渲染 MA 指标')
  assert.ok(!PAGE_SOURCE.includes('it.ma10'), 'rail 不得渲染 MA 指标')
})

// ===========================================================================
// P1-1. 大盘 / 行业 / 概念 统一顶部 shell（共享 ReviewHeader，行业/概念不再有独立大标题）
// ===========================================================================
test('P1-1. 大盘页与行业/概念页共享同一 ReviewHeader（统一 shell）', () => {
  assert.match(MARKET_PAGE_SOURCE, /<ReviewHeader /, '大盘页消费共享 ReviewHeader')
  assert.match(PAGE_SOURCE, /<ReviewHeader /, '行业/概念页消费共享 ReviewHeader')
  // 行业/概念不再渲染自己的大标题「行业」/「概念」
  assert.ok(!PAGE_SOURCE.includes("'行业' : '概念'"), '行业/概念页不得再渲染独立大标题')
})

// ===========================================================================
// R11 / P1-3. rail = 同一份 server 过滤+排序结果「全集」（前端分页拉全，不前端 slice）
// ===========================================================================
test('R11. rail 渲染筛选排序 universe 全集（前端分页拉全，不前端 slice，仅详情态请求）', () => {
  assert.match(PAGE_SOURCE, /const railUniverseQuery = useMemo</, 'rail 用独立 universe query（不含 page/page_size/board_id）')
  assert.match(
    PAGE_SOURCE,
    /useMarketScopeExplorerUniverse\(railUniverseQuery, !!parsed\.board_id\)/,
    'rail 仅详情态（board_id 已选）请求',
  )
  assert.match(PAGE_SOURCE, /\(railUniverse\.data \?\? \[\]\)\.map/, 'rail 直接消费 universe items（不另建数组 / 不前端过滤）')
  assert.ok(!PAGE_SOURCE.includes('page_size: SCOPE_EXPLORER_PAGE_SIZE_MAX'), 'rail 不再用单页 100 条冒充全集')
})

// ===========================================================================
// R12/R13. 选中仅改 scope / 返回保状态
// ===========================================================================
test('R12. 选中 rail / 行仅写入 board_id 且保留列表 page', () => {
  assert.match(
    PAGE_SOURCE,
    /const onRailSelect = \(id: string\) => applyPatch\(\{ board_id: id, page: parsed\.page \}\)/,
    'rail 选中只改 board_id，保留 page',
  )
  assert.match(
    PAGE_SOURCE,
    /const onSelect = \(id: string\) => applyPatch\(\{ board_id: id, page: parsed\.page \}\)/,
    '行选中只改 board_id，保留 page',
  )
})

test('R13. 返回列表仅清除 board_id，精确保留列表状态（URL 即 SSOT）', () => {
  assert.match(
    PAGE_SOURCE,
    /const onBackToList = \(\) => applyPatch\(\{ board_id: null, page: parsed\.page \}\)/,
    '返回只清 board_id，保留 page',
  )
})

// ===========================================================================
// R14. 详情复用 canonical
// ===========================================================================
test('R14. 右详情复用 canonical useMarketScopeDetail + BreadthChart + meta.member_count/name', () => {
  assert.match(PAGE_SOURCE, /useMarketScopeDetail\(parsed\.board_id, DETAIL_DAYS\)/, '复用现有 detail hook')
  assert.match(PAGE_SOURCE, /<BreadthChart/, '复用统一 BreadthChart')
  assert.match(PAGE_SOURCE, /meta\.member_count/, 'detail 头部消费 ScopeMetadata.member_count')
  assert.match(PAGE_SOURCE, /meta\.name/, 'detail 头部消费 ScopeMetadata.name')
})

// ===========================================================================
// R4/R5/R6/R7/R8. 列表态既有交互全部保留
// ===========================================================================
test('R4/R5. 排序与筛选交互保留', () => {
  assert.match(PAGE_SOURCE, /const onSort = \(field: ExplorerParsed\['sort'\]\) => \{/, '排序三分支保留')
  assert.match(PAGE_SOURCE, /applyColumnFilter\(filterPopover\.column, mode, lower, upper\)/, '列筛选弹层保留')
})

test('R6/R7. 列表态保留搜索与清除筛选入口', () => {
  assert.match(PAGE_SOURCE, /data-testid="search-input"/, '搜索入口保留')
  assert.match(PAGE_SOURCE, /data-testid="clear-all-filters"/, '清除筛选入口保留')
})

test('R8. 列表态保留分页控件', () => {
  assert.match(PAGE_SOURCE, /className=\{styles\.pagination\}/, '分页容器保留')
  assert.match(PAGE_SOURCE, /goPage\(parsed\.page - 1\)/, '上一页保留')
  assert.match(PAGE_SOURCE, /goPage\(parsed\.page \+ 1\)/, '下一页保留')
})

// ===========================================================================
// R15. 大盘页核心契约未被 section 6 布局统一破坏
// ===========================================================================
test('R15. 大盘页核心契约稳定（共享 ReviewHeader + 统一 BreadthChart + rankings）', () => {
  assert.match(MARKET_PAGE_SOURCE, /<ReviewHeader /, '大盘页消费共享 ReviewHeader')
  assert.equal((MARKET_PAGE_SOURCE.match(/<BreadthChart/g) ?? []).length, 1, '大盘页只能有一个 chart')
  assert.equal((MARKET_PAGE_SOURCE.match(/useMarketRankings\(/g) ?? []).length, 2, '行业/概念 ranking 各一次')
})

// ===========================================================================
// P1-2. 清除排序与筛选：纯状态契约（reset 必须同时还原 sort/direction；按钮 disabled 语义正确）
// ===========================================================================
test('P1-2a. clearAllStatePatch 还原 filters + sort + direction 到 canonical default', () => {
  const patch = clearAllStatePatch()
  for (const k of NUMERIC_FILTER_KEYS) assert.equal(patch.filters?.[k], null, `filter ${k} 必须置空`)
  assert.equal(patch.sort, SCOPE_EXPLORER_SORT_DEFAULT)
  assert.equal(patch.direction, SCOPE_EXPLORER_DIRECTION_DEFAULT)
  assert.equal(patch.board_id, undefined, 'reset 不得触碰 board_id')
  assert.equal(patch.q, undefined, 'reset 不扩大语义：q 保持独立')
})

test('P1-2b. 清除按钮 disabled 语义：无筛选 + canonical sort → disabled；否则 active', () => {
  const empty: Record<NumericFilterKey, number | null> = NUMERIC_FILTER_KEYS.reduce(
    (a, k) => {
      a[k] = null
      return a
    },
    {} as Record<NumericFilterKey, number | null>,
  )
  // 非默认 sort + 无筛选 → active
  assert.equal(isExplorerResetDisabled(empty, 'ma20', 'asc'), false)
  // 默认 sort + 有筛选 → active
  const withFilter = { ...empty, ma5_min: 0.8 }
  assert.equal(
    isExplorerResetDisabled(withFilter, SCOPE_EXPLORER_SORT_DEFAULT, SCOPE_EXPLORER_DIRECTION_DEFAULT),
    false,
  )
  // 非默认 sort + 有筛选 → active
  assert.equal(isExplorerResetDisabled(withFilter, 'ma20', 'asc'), false)
  // canonical 默认 + 无筛选 → disabled
  assert.equal(
    isExplorerResetDisabled(empty, SCOPE_EXPLORER_SORT_DEFAULT, SCOPE_EXPLORER_DIRECTION_DEFAULT),
    true,
  )
})

// ===========================================================================
// P1-3. rail universe 分页拉全（不前端 slice / 不 N+1）
// ===========================================================================
function fakeExplorerFetcher(total: number) {
  const all: ScopeExplorerItem[] = Array.from({ length: total }, (_, i) => ({
    board_id: `b${i}`,
    board_name: `B${i}`,
    board_type: 'industry',
    hierarchy_level: 'L1',
    membership_version: 'v',
    member_count: 0,
    ma5: null,
    ma10: null,
    ma20: null,
    ma50: null,
    ma120: null,
    previous_ma5: null,
    previous_ma10: null,
    ma5_delta: null,
    ma10_delta: null,
  }))
  const calls: Record<string, string | number>[] = []
  const fetcher = async (params: Record<string, string | number>): Promise<ScopeExplorerResponse> => {
    calls.push(params)
    const page = Number(params.page)
    const size = Number(params.page_size)
    const start = (page - 1) * size
    const items = all.slice(start, start + size)
    return { projection_trade_date: null, previous_trade_date: null, total, page, page_size: size, items }
  }
  return { fetcher, calls }
}

test('P1-3a. total ≤ page_size → 仅 1 次请求，返回全部', async () => {
  const { fetcher, calls } = fakeExplorerFetcher(50)
  const items = await fetchScopeExplorerUniverse({ scope_type: 'industry' }, fetcher)
  assert.equal(calls.length, 1)
  assert.equal(items.length, 50)
  assert.deepEqual(
    items.map((i) => i.board_id),
    Array.from({ length: 50 }, (_, i) => `b${i}`),
  )
})

test('P1-3b. total > page_size → 按 page 顺序拉全，最终条数 == total', async () => {
  const { fetcher, calls } = fakeExplorerFetcher(250)
  const items = await fetchScopeExplorerUniverse({ scope_type: 'industry' }, fetcher)
  assert.equal(calls.length, 3, '250/100 → 3 次分页请求')
  assert.equal(items.length, 250)
  assert.equal(items[0].board_id, 'b0')
  assert.equal(items[249].board_id, 'b249', '顺序保持 server 顺序（concat，不重排）')
})

test('P1-3c. total == page_size（边界）→ 仅 1 次请求', async () => {
  const { fetcher, calls } = fakeExplorerFetcher(100)
  const items = await fetchScopeExplorerUniverse({ scope_type: 'industry' }, fetcher)
  assert.equal(calls.length, 1)
  assert.equal(items.length, 100)
})

test('P1-3d. 筛选 / 排序条件被带入每次分页请求（query identity 一致）', async () => {
  const { fetcher, calls } = fakeExplorerFetcher(250)
  const query: ScopeExplorerQuery = {
    scope_type: 'industry',
    hierarchy_level: 'L2',
    q: 'foo',
    sort: 'ma20',
    direction: 'asc',
    ma5_min: 0.8,
  }
  await fetchScopeExplorerUniverse(query, fetcher)
  for (const c of calls) {
    assert.equal(c.scope_type, 'industry')
    assert.equal(c.hierarchy_level, 'L2')
    assert.equal(c.q, 'foo')
    assert.equal(c.sort, 'ma20')
    assert.equal(c.direction, 'asc')
    assert.equal(c.ma5_min, 0.8)
  }
})

test('P1-3e. rail universe query identity 不含 board_id；随 filter/sort 变化', () => {
  const base: ScopeExplorerQuery = {
    scope_type: 'industry',
    hierarchy_level: 'L1',
    q: '',
    sort: 'ma5',
    direction: 'desc',
  }
  const withA = scopeExplorerQueryKey({ ...base, board_id: 'A' } as unknown as ScopeExplorerQuery)
  const withB = scopeExplorerQueryKey({ ...base, board_id: 'B' } as unknown as ScopeExplorerQuery)
  assert.deepEqual(withA, withB, 'board_id 不参与 rail universe 身份')
  assert.notDeepEqual(
    scopeExplorerQueryKey(base),
    scopeExplorerQueryKey({ ...base, sort: 'ma20' }),
    'sort 变化必须改变 rail identity',
  )
  assert.notDeepEqual(
    scopeExplorerQueryKey(base),
    scopeExplorerQueryKey({ ...base, ma5_min: 0.8 }),
    'filter 变化必须改变 rail identity',
  )
})
