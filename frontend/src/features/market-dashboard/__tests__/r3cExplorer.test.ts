// [R3C] Industry / Concept Explorer 合同测试（纯逻辑 + 源码契约，无 DOM）。
//
// 覆盖：A industry 默认 L1 · B L1/L2/L3 roundtrip · C concept 剥离 hierarchy ·
//       D q roundtrip · E page/page_size 默认+上限 · F 全部 sort · G 全部 filter ·
//       H 非法值归位 · I 百分比↔ratio 转换 · J filter/search/sort 重置第 1 页 ·
//       K 层级切换清 board_id · L query 全走 server · M 列顺序锁死 · N NULL→— ·
//       O 无客户端 filter/sort/slice · P 选中来自 board_id URL · Q detail member_count ·
//       R detail 6-series · S toggle 硬合同 · T basket · U 内联对比已移除 ·
//       V /boards→industry L1 · W/X /boards/:id resolver · Y resolver 失败提示 · Z 无申万。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  NUMERIC_FILTER_KEYS,
  breadthPctToRatio,
  breadthRatioToPct,
  deltaPpToRatio,
  deltaRatioToPp,
  parseConceptExplorerSearch,
  parseIndustryExplorerSearch,
  ratioToUi,
  serializeExplorerState,
  uiToRatio,
  updateExplorerState,
} from '../scopeExplorerUrlState'
import { buildScopeExplorerParams, scopeExplorerQueryKey, SCOPE_EXPLORER_SORT_FIELDS } from '../scopeExplorerQuery'
import { MARKET_OVERVIEW_SERIES } from '../marketOverviewConfig'
import { EXPLORER_TABLE_COLUMNS } from '../scopeExplorerUrlState'
import { formatBreadth, formatDelta } from '../dashboardLogic'
import { useCompareBasketStore } from '../../../store/compareBasket'

const PAGE_SOURCE = readFileSync(new URL('../ScopeExplorerPage.tsx', import.meta.url), 'utf8')
const APP_SOURCE = readFileSync(new URL('../../../App.tsx', import.meta.url), 'utf8')
const RESOLVER_SOURCE = readFileSync(new URL('../BoardCompatResolver.tsx', import.meta.url), 'utf8')
const DTO_SOURCE = readFileSync(new URL('../../../api/marketDashboard.ts', import.meta.url), 'utf8')

function roundtrip(scopeType: 'industry' | 'concept', search: string) {
  const parsed = scopeType === 'industry' ? parseIndustryExplorerSearch : parseConceptExplorerSearch
  return parsed(new URLSearchParams(search))
}

// ===========================================================================
// A–E. URL state 基础
// ===========================================================================
test('A. industry 默认 hierarchy_level = L1', () => {
  assert.equal(parseIndustryExplorerSearch(new URLSearchParams('')).hierarchy_level, 'L1')
})

test('B. industry L1/L2/L3 roundtrip 稳定', () => {
  for (const lv of ['L1', 'L2', 'L3'] as const) {
    const once = roundtrip('industry', `hierarchy_level=${lv}&page=2`)
    const twice = roundtrip('industry', serializeExplorerState(once, 'industry').toString())
    assert.equal(twice.hierarchy_level, lv)
    assert.equal(twice.page, 2)
  }
})

test('C. concept 永远不带 hierarchy_level（解析忽略 + 序列化省略）', () => {
  const parsed = parseConceptExplorerSearch(new URLSearchParams('hierarchy_level=L2&page=3'))
  assert.equal(parsed.hierarchy_level, 'L1', 'concept 解析忽略 URL 上的 hierarchy_level')
  const serialized = serializeExplorerState(parsed, 'concept')
  assert.equal(serialized.has('hierarchy_level'), false, 'concept 序列化不输出 hierarchy_level')
  assert.equal(serialized.get('page'), '3')
})

test('D. q URL roundtrip（trim + 空不写）', () => {
  const withQ = roundtrip('industry', 'q=%E5%8D%8A%E5%AF%BC%E4%BD%93')
  assert.equal(withQ.q, '半导体')
  const back = roundtrip('industry', serializeExplorerState(withQ, 'industry').toString())
  assert.equal(back.q, '半导体')
  // 空 q 序列化时不写入
  const empty = roundtrip('industry', 'q=')
  assert.equal(serializeExplorerState(empty, 'industry').has('q'), false)
})

test('E. page/page_size 默认 + 上限 100', () => {
  const d = roundtrip('industry', '')
  assert.equal(d.page, 1)
  assert.equal(d.page_size, 20)
  const big = roundtrip('industry', 'page=5&page_size=150')
  assert.equal(big.page, 5)
  assert.equal(big.page_size, 100, 'page_size 超过 100 必须夹到 100')
  const neg = roundtrip('industry', 'page=-3&page_size=0')
  assert.equal(neg.page, 1)
  assert.equal(neg.page_size, 20)
})

// ===========================================================================
// F–H. sort / filter / 非法归位
// ===========================================================================
test('F. 全部 sort 字段合法透传，非法归位 ma5', () => {
  for (const f of SCOPE_EXPLORER_SORT_FIELDS) {
    assert.equal(roundtrip('industry', `sort=${f}`).sort, f)
  }
  assert.equal(roundtrip('industry', 'sort=unknown').sort, 'ma5')
})

test('G. 全部 10 个 numeric filter 字段 roundtrip（backend ratio 单位）', () => {
  const params = new URLSearchParams()
  const expected: Record<string, number> = {}
  for (const k of NUMERIC_FILTER_KEYS) {
    const v = k.startsWith('member_count') ? 42 : 0.55
    params.set(k, String(v))
    expected[k] = v
  }
  const parsed = roundtrip('industry', params.toString())
  for (const k of NUMERIC_FILTER_KEYS) assert.equal(parsed.filters[k], expected[k])
})

test('H. 非法 URL 值 canonical 归位', () => {
  const p = roundtrip('industry', 'sort=zzz&direction=sideways&page=abc&page_size=999&hierarchy_level=ZZ&ma5_min=notnum')
  assert.equal(p.sort, 'ma5')
  assert.equal(p.direction, 'desc')
  assert.equal(p.page, 1)
  assert.equal(p.page_size, 100)
  assert.equal(p.hierarchy_level, 'L1')
  assert.equal(p.filters.ma5_min, null, 'NaN filter 必须清除')
})

// ===========================================================================
// I–K. 单位转换 / 状态更新语义
// ===========================================================================
test('I. 百分比↔ratio 转换诚实（UI % / pp ↔ backend ratio）', () => {
  assert.equal(breadthPctToRatio(80), 0.8)
  assert.equal(breadthPctToRatio(20), 0.2)
  assert.equal(deltaPpToRatio(5), 0.05)
  assert.equal(deltaPpToRatio(-3), -0.03)
  assert.equal(breadthRatioToPct(0.8), 80)
  assert.equal(deltaRatioToPp(0.05), 5)
  // UI 草稿 ↔ ratio
  assert.equal(uiToRatio('ma5_min', '80'), 0.8)
  assert.equal(ratioToUi('ma5_min', 0.8), '80')
  assert.equal(uiToRatio('member_count_min', '42'), 42)
  assert.equal(uiToRatio('ma5_min', ''), null, '空输入 → null（不过滤）')
})

test('J. filter / search / sort 变化重置第 1 页；显式 page 不重置', () => {
  const base = roundtrip('industry', 'page=3&sort=ma5&direction=desc')
  assert.equal(updateExplorerState(base, { q: 'x' }).page, 1)
  assert.equal(updateExplorerState(base, { filters: { ma5_min: 0.8 } }).page, 1)
  assert.equal(updateExplorerState(base, { sort: 'ma20' }).page, 1)
  assert.equal(updateExplorerState(base, { page: 5 }).page, 5, '显式改 page 不重置')
})

test('K. 层级切换清除 board_id 并回到第 1 页', () => {
  const base = roundtrip('industry', 'board_id=xyz&page=4')
  const next = updateExplorerState(base, { hierarchy_level: 'L2' })
  assert.equal(next.board_id, null, '切层级必须清除 board_id')
  assert.equal(next.hierarchy_level, 'L2')
  assert.equal(next.page, 1)
})

// ===========================================================================
// L. query 全走 server（无客户端 filter/sort/page）
// ===========================================================================
test('L. 所有 server-state 进入 hook query（filter/sort/page 都由后端负责）', () => {
  const parsed = roundtrip('industry', 'hierarchy_level=L2&q=foo&page=2&page_size=50&sort=ma20&direction=asc&ma5_min=0.8&member_count_min=10')
  const query = {
    scope_type: 'industry' as const,
    hierarchy_level: parsed.hierarchy_level,
    q: parsed.q || null,
    page: parsed.page,
    page_size: parsed.page_size,
    sort: parsed.sort,
    direction: parsed.direction,
    ...parsed.filters,
  }
  const params = buildScopeExplorerParams(query)
  assert.equal(params.hierarchy_level, 'L2')
  assert.equal(params.q, 'foo')
  assert.equal(params.page, 2)
  assert.equal(params.page_size, 50)
  assert.equal(params.sort, 'ma20')
  assert.equal(params.direction, 'asc')
  assert.equal(params.ma5_min, 0.8)
  assert.equal(params.member_count_min, 10)
  // queryKey 同样覆盖全部 state（缓存不串页）：queryKey 持有的是值（非 key 名）。
  const key = scopeExplorerQueryKey(query)
  assert.ok(key.includes('L2'))
  assert.ok(key.includes('foo'))
  assert.ok(key.includes(2))
  assert.ok(key.includes(50))
  assert.ok(key.includes('ma20'))
  assert.ok(key.includes('asc'))
  assert.ok(key.includes(0.8))
  assert.ok(key.includes(10))
})

test('O. 页面不对 items 做客户端 sort/filter/slice', () => {
  assert.equal(/\.sort\(|\.filter\(|\.slice\(/.test(PAGE_SOURCE), false, 'ScopeExplorerPage 不得对 items 客户端重排/筛选/切片')
})

// ===========================================================================
// M. 列顺序锁死
// ===========================================================================
test('M. 表格列顺序锁死 = name/member_count/ma5/ma5_delta/ma10/ma10_delta/ma20/ma50/ma120/operation', () => {
  assert.deepEqual([...EXPLORER_TABLE_COLUMNS], [
    'name',
    'member_count',
    'ma5',
    'ma5_delta',
    'ma10',
    'ma10_delta',
    'ma20',
    'ma50',
    'ma120',
    'operation',
  ])
})

// ===========================================================================
// N. NULL 渲染为 —
// ===========================================================================
test('N. NULL breadth / delta 渲染为 —（不显示 0 冒充 NULL）', () => {
  assert.equal(formatBreadth(null), '—')
  assert.equal(formatBreadth(0.8), '80.0%')
  assert.equal(formatDelta(null), '—')
  assert.equal(formatDelta(0.05), '+5.0%')
  assert.equal(formatDelta(-0.03), '-3.0%')
  assert.equal(formatDelta(0), '0.0%')
})

// ===========================================================================
// P. 选中来自 board_id URL
// ===========================================================================
test('P. 选中板块 = board_id URL param', () => {
  assert.equal(roundtrip('industry', 'board_id=abc-123').board_id, 'abc-123')
  assert.equal(serializeExplorerState(roundtrip('industry', 'board_id=abc-123'), 'industry').get('board_id'), 'abc-123')
})

// ===========================================================================
// Q. detail member_count 来自 ScopeMetadata（R3C0 additive）
// ===========================================================================
test('Q. detail member_count 消费 ScopeMetadata.member_count（R3C0 additive）', () => {
  assert.match(DTO_SOURCE, /interface ScopeMetadata[\s\S]*?member_count: number/)
  // 页面渲染 detail.subs 时用 meta.member_count（非 collection item 的 member_count）
  assert.match(PAGE_SOURCE, /meta\.member_count/)
  assert.match(PAGE_SOURCE, /meta\.name/)
})

// ===========================================================================
// R–S. detail chart：6-series + toggle 硬合同
// ===========================================================================
test('R. detail 图表精确 6-series unified chart（MA left / EW right）', () => {
  assert.equal(MARKET_OVERVIEW_SERIES.length, 6)
  assert.deepEqual(
    MARKET_OVERVIEW_SERIES.map((s) => s.field),
    ['ma5', 'ma10', 'ma20', 'ma50', 'ma120', 'ew_index'],
  )
  for (const s of MARKET_OVERVIEW_SERIES) {
    if (s.field === 'ew_index') {
      assert.equal(s.scale, 'right')
      assert.equal(s.breadthPercent, undefined)
    } else {
      assert.equal(s.scale, 'left')
      assert.equal(s.breadthPercent, true)
      assert.ok(s.fixedScaleRange, 'MA 左轴固定 [0,1]')
    }
  }
})

test('S. detail 图表 line-toggle 硬合同仍成立（只 applyOptions({visible})）', async () => {
  interface FakeSeries {
    vis: boolean[]
    data: readonly unknown[]
    handle: unknown
  }
  const created: FakeSeries[] = []
  const chart = {
    addLineSeries(opts: { visible: boolean }) {
      const rec: FakeSeries = { vis: [opts.visible], data: [], handle: {} }
      created.push(rec)
      return {
        applyOptions(o: { visible: boolean }) {
          rec.vis.push(o.visible)
        },
        setData(d: readonly unknown[]) {
          rec.data = [...rec.data, d]
        },
        createPriceLine() {
          return {}
        },
      }
    },
    remove() {},
  }
  // 复用 R3B 同样的控制器（轻量 duck-typed）
  const { createLineSeriesController } = await import('../lineSeriesController')
  const specs = MARKET_OVERVIEW_SERIES.map((s, i) => ({
    key: s.field,
    label: s.label,
    color: s.color ?? `#${i}`,
    scale: s.scale,
    lineWidth: s.lineWidth,
  }))
  const controller = createLineSeriesController(chart as never, specs as never)
  assert.equal(created.length, 6)
  controller.setData(
    Object.fromEntries(specs.map((s) => [s.key, [{ time: '2026-09-10', value: 0.5 }]])),
  )
  for (const s of MARKET_OVERVIEW_SERIES) {
    controller.toggle(s.field)
    assert.equal(controller.isVisible(s.field), false)
  }
  assert.equal(created.length, 6, 'toggle 绝不重建 series')
  for (const rec of created) {
    // vis 记录调用序列：创建时 visible:true → 一次 toggle 变 false（绝不重建 / 绝不再次 setData）。
    assert.deepEqual(rec.vis, [true, false], 'toggle 只允许 applyOptions({visible})')
    assert.equal(rec.data.length, 1, 'toggle 绝不再次 setData')
  }
  for (const s of MARKET_OVERVIEW_SERIES) {
    controller.toggle(s.field)
    assert.equal(controller.isVisible(s.field), true)
  }
})

// ===========================================================================
// T. 比较篮
// ===========================================================================
test('T. 比较篮 add / duplicate / full / mixed industry+concept', () => {
  const store = useCompareBasketStore
  store.setState({ items: [] })
  assert.equal(store.getState().add({ id: 'a', name: 'A', type: 'industry' }), 'added')
  assert.equal(store.getState().add({ id: 'a', name: 'A', type: 'industry' }), 'duplicate', '重复 add 安全 no-op')
  assert.equal(store.getState().items.length, 1, '重复不得覆盖已有项')
  for (let i = 1; i <= 20; i++) store.getState().add({ id: `b${i}`, name: `B${i}`, type: 'concept' })
  assert.equal(store.getState().add({ id: 'c', name: 'C', type: 'concept' }), 'full', '满 20 后第 21 个 full')
  assert.equal(store.getState().items.length, 20, 'full 时不淘汰已有选择')
  const types = store.getState().items.map((it) => it.type)
  assert.ok(types.includes('industry') && types.includes('concept'), 'industry + concept 可混合')
  store.getState().clear()
  assert.equal(store.getState().items.length, 0)
})

// ===========================================================================
// U. 内联对比 workspace 已移除（不在 Explorer 页面）
// ===========================================================================
test('U. Explorer 页面不再内联 CompareChart / 重点板块比较', () => {
  assert.ok(!PAGE_SOURCE.includes('CompareChart'), 'ScopeExplorerPage 不得渲染 CompareChart')
  assert.ok(!PAGE_SOURCE.includes('重点板块比较'), '不得保留旧内联对比区块')
  assert.ok(!PAGE_SOURCE.includes('useMarketRankings'), '主列表不得用 rankings')
  assert.ok(!PAGE_SOURCE.includes('useMarketCompare'), 'Explorer 页面不得用 compare hook')
})

// ===========================================================================
// V–Y. /boards 兼容（源契约）
// ===========================================================================
test('V. /boards 重定向到 industry L1', () => {
  assert.match(APP_SOURCE, /path: '\/boards',[\s\S]*?element: <Navigate to="\/review\/industry\?hierarchy_level=L1" replace \/>/)
})

test('W. /boards/:id industry resolver → /review/industry?hierarchy_level=&board_id=', () => {
  assert.match(RESOLVER_SOURCE, /\/review\/industry\?hierarchy_level=\$\{meta\.hierarchy_level\}&board_id=/)
})

test('X. /boards/:id concept resolver → /review/concept?board_id=', () => {
  assert.match(RESOLVER_SOURCE, /\/review\/concept\?board_id=/)
})

test('Y. resolver 失败保留迁移提示页（不恢复旧 Board Analysis API / 不按 UUID 猜 type）', () => {
  assert.match(RESOLVER_SOURCE, /import BoardAnalysisRetiredPage/)
  assert.match(RESOLVER_SOURCE, /detail\.isError \|\| !detail\.data/)
  assert.ok(!RESOLVER_SOURCE.includes('useBoardAnalysis'), '不得恢复旧 Board Analysis API')
})

// ===========================================================================
// Z. 无用户可见「申万」
// ===========================================================================
test('Z. Explorer 前端无用户可见「申万」', () => {
  const files = ['ScopeExplorerPage.tsx', 'ScopeExplorerTable.tsx', 'marketOverviewConfig.ts', 'scopeExplorerUrlState.ts']
  for (const f of files) {
    const src = readFileSync(new URL(`../${f}`, import.meta.url), 'utf8')
    assert.ok(!src.includes('申万'), `${f} 不得出现「申万」`)
  }
})
