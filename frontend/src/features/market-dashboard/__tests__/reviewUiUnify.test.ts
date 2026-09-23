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
// R11. rail = 同一份 server 过滤+排序结果全集
// ===========================================================================
test('R11. rail 复用 explorer hook（同 query，page_size=上限，仅详情态请求）', () => {
  assert.match(PAGE_SOURCE, /const railQuery = useMemo\(/)
  assert.match(PAGE_SOURCE, /page_size: SCOPE_EXPLORER_PAGE_SIZE_MAX/)
  assert.match(PAGE_SOURCE, /const railExplorer = useMarketScopeExplorer\(railQuery/)
  assert.match(PAGE_SOURCE, /useMarketScopeExplorer\(railQuery, !![^)]*board_id\)/, 'rail 仅在详情态（board_id 已选）请求')
  assert.match(PAGE_SOURCE, /\(railExplorer\.data\?\.items \?\? \[\]\)\.map/, 'rail 直接消费 explorer items（不另建数组 / 不前端过滤）')
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
test('R15. 大盘页核心契约稳定（DashboardTabs + 统一 BreadthChart + rankings）', () => {
  assert.match(MARKET_PAGE_SOURCE, /<DashboardTabs \/>/)
  assert.equal((MARKET_PAGE_SOURCE.match(/<BreadthChart/g) ?? []).length, 1, '大盘页只能有一个 chart')
  assert.equal((MARKET_PAGE_SOURCE.match(/useMarketRankings\(/g) ?? []).length, 2, '行业/概念 ranking 各一次')
})
