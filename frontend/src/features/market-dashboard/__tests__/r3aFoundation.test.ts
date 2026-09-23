// [R3A] Review V2 frontend foundation 合同测试（纯逻辑 / 无 DOM）
//
// 覆盖：A 参数序列化 · B concept 不发层级 · C query key 覆盖全部 server-side state ·
//       D/E/F/G 共享比较篮 · H 二级导航 · I/J legend toggle 硬合同 · K aria-pressed ·
//       L 普通 series palette 不使用涨跌色。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  DEFAULT_SCOPE_EXPLORER_QUERY,
  SCOPE_EXPLORER_SORT_FIELDS,
  buildScopeExplorerParams,
  scopeExplorerQueryKey,
  type ScopeExplorerQuery,
} from '../scopeExplorerQuery'
import {
  BREADTH_REFERENCE_LINES,
  EW_LINE_WIDTH,
  MARKET_DIRECTION_COLORS,
  REVIEW_TOKENS,
  SERIES_LINE_WIDTH,
  SERIES_PALETTE,
  seriesColor,
} from '../chartTheme'
import {
  createLineSeriesController,
  legendAriaPressed,
  type LineChartHandle,
  type LineSeriesHandle,
  type LineSeriesSpec,
} from '../lineSeriesController'
import { REVIEW_TABS, reviewTabLabel } from '../reviewTabs'
import { MAX_COMPARE_BOARDS } from '../dashboardLogic'
import { COMPARE_BASKET_MAX, useCompareBasketStore } from '../../../store/compareBasket'

// ===========================================================================
// A/B/C. Explorer client contract（与 backend 参数 1:1）
// ===========================================================================
test('A. explorer query 序列化：默认值 / snake_case / q trim / null 不发送 / 0 必须发送', () => {
  assert.deepEqual(buildScopeExplorerParams({ scope_type: 'industry' }), {
    scope_type: 'industry',
    page: DEFAULT_SCOPE_EXPLORER_QUERY.page,
    page_size: DEFAULT_SCOPE_EXPLORER_QUERY.page_size,
    sort: DEFAULT_SCOPE_EXPLORER_QUERY.sort,
    direction: DEFAULT_SCOPE_EXPLORER_QUERY.direction,
  })

  assert.deepEqual(
    buildScopeExplorerParams({
      scope_type: 'industry',
      hierarchy_level: 'L2',
      q: '  半导体  ',
      page: 3,
      page_size: 50,
      sort: 'member_count',
      direction: 'asc',
      ma5_min: 0.5,
      ma10_max: 0.9,
      member_count_min: 10,
    }),
    {
      scope_type: 'industry',
      page: 3,
      page_size: 50,
      sort: 'member_count',
      direction: 'asc',
      hierarchy_level: 'L2',
      q: '半导体',
      ma5_min: 0.5,
      ma10_max: 0.9,
      member_count_min: 10,
    },
  )

  // null / undefined 一律不发送（不发送 ≠ 发送 0）
  const nulls = buildScopeExplorerParams({ scope_type: 'industry', ma5_min: null, ma5_max: undefined })
  assert.ok(!('ma5_min' in nulls), 'null 区间不得发送')
  assert.ok(!('ma5_max' in nulls), 'undefined 区间不得发送')
  // 空白 q 不发送
  assert.ok(!('q' in buildScopeExplorerParams({ scope_type: 'industry', q: '   ' })))
  // 0 是合法区间值，必须发送
  assert.equal(buildScopeExplorerParams({ scope_type: 'industry', ma5_min: 0 }).ma5_min, 0)
  // sort 白名单与 backend SORT_FIELDS 一致（API 层字段名是 name，不是 board_name）
  assert.deepEqual([...SCOPE_EXPLORER_SORT_FIELDS], [
    'name',
    'member_count',
    'ma5',
    'ma10',
    'ma20',
    'ma50',
    'ma120',
    'ma5_delta',
    'ma10_delta',
  ])
})

test('B. concept 不发送 hierarchy_level（backend 对 concept+hierarchy_level 是显式 422）', () => {
  assert.ok(!('hierarchy_level' in buildScopeExplorerParams({ scope_type: 'concept' })))
  assert.ok(
    !('hierarchy_level' in buildScopeExplorerParams({ scope_type: 'concept', hierarchy_level: 'L1' })),
    'concept 即使被传入层级也必须不发送',
  )
  assert.equal(buildScopeExplorerParams({ scope_type: 'industry', hierarchy_level: 'L3' }).hierarchy_level, 'L3')
  assert.ok(!('hierarchy_level' in buildScopeExplorerParams({ scope_type: 'industry' })))
  // key 语义一致：concept 的层级参数不参与缓存身份
  assert.deepEqual(
    scopeExplorerQueryKey({ scope_type: 'concept', hierarchy_level: 'L1' }),
    scopeExplorerQueryKey({ scope_type: 'concept' }),
  )
})

test('C. query key 覆盖全部 server-side state（filter / page / sort 任一变化即换 key）', () => {
  const base: ScopeExplorerQuery = {
    scope_type: 'industry',
    hierarchy_level: 'L1',
    q: 'Ind',
    page: 2,
    page_size: 50,
    sort: 'ma5',
    direction: 'desc',
  }
  const seen = new Set<string>([JSON.stringify(scopeExplorerQueryKey(base))])

  const variants: ScopeExplorerQuery[] = [
    { ...base, scope_type: 'concept', hierarchy_level: null },
    { ...base, hierarchy_level: 'L2' },
    { ...base, q: 'Con' },
    { ...base, page: 3 },
    { ...base, page_size: 10 },
    { ...base, sort: 'ma10' },
    { ...base, direction: 'asc' },
    { ...base, ma5_min: 0.5 },
    { ...base, ma5_max: 0.9 },
    { ...base, ma10_min: 0.1 },
    { ...base, ma10_max: 0.2 },
    { ...base, ma5_delta_min: -0.1 },
    { ...base, ma5_delta_max: 0.1 },
    { ...base, ma10_delta_min: -0.2 },
    { ...base, ma10_delta_max: 0.2 },
    { ...base, member_count_min: 5 },
    { ...base, member_count_max: 100 },
  ]
  for (const variant of variants) {
    const key = JSON.stringify(scopeExplorerQueryKey(variant))
    assert.ok(!seen.has(key), `query key 未覆盖字段：${JSON.stringify(variant)}`)
    seen.add(key)
  }

  // 默认值补全后语义相同 → key 必须一致（避免无意义 refetch）
  assert.deepEqual(
    scopeExplorerQueryKey({ scope_type: 'industry' }),
    scopeExplorerQueryKey({
      scope_type: 'industry',
      page: DEFAULT_SCOPE_EXPLORER_QUERY.page,
      page_size: DEFAULT_SCOPE_EXPLORER_QUERY.page_size,
      sort: DEFAULT_SCOPE_EXPLORER_QUERY.sort,
      direction: DEFAULT_SCOPE_EXPLORER_QUERY.direction,
    }),
  )
})

// ===========================================================================
// D/E/F/G. 共享比较篮
// ===========================================================================
test('D. basket add / remove / clear / contains', () => {
  const store = useCompareBasketStore
  store.getState().clear()

  assert.equal(store.getState().add({ id: 'a', name: '板块A', type: 'industry' }), 'added')
  assert.equal(store.getState().contains('a'), true)
  assert.equal(store.getState().items.length, 1)
  assert.deepEqual(store.getState().items[0], { id: 'a', name: '板块A', type: 'industry' })

  store.getState().remove('a')
  assert.equal(store.getState().contains('a'), false)
  assert.equal(store.getState().items.length, 0)

  store.getState().add({ id: 'b', name: '板块B', type: 'concept' })
  store.getState().clear()
  assert.equal(store.getState().items.length, 0)
  assert.equal(store.getState().contains('b'), false)
})

test('E. 重复 add 是 no-op（state 引用不变、不重复入篮、不覆盖已有 name）', () => {
  const store = useCompareBasketStore
  store.getState().clear()
  store.getState().add({ id: 'a', name: '板块A', type: 'industry' })

  const before = store.getState().items
  assert.equal(store.getState().add({ id: 'a', name: '改名了', type: 'industry' }), 'duplicate')
  assert.equal(store.getState().items, before, '重复 add 不得产生新的 state 引用')
  assert.equal(store.getState().items.length, 1)
  assert.equal(store.getState().items[0].name, '板块A')
})

test('F. HARD CAP = 20（第 21 个被拒绝，且不淘汰已有选择）', () => {
  const store = useCompareBasketStore
  store.getState().clear()

  for (let i = 0; i < COMPARE_BASKET_MAX; i += 1) {
    assert.equal(store.getState().add({ id: `b${i}`, name: `板块${i}`, type: 'industry' }), 'added')
  }
  assert.equal(store.getState().items.length, COMPARE_BASKET_MAX)

  assert.equal(store.getState().add({ id: 'overflow', name: '溢出', type: 'industry' }), 'full')
  assert.equal(store.getState().items.length, COMPARE_BASKET_MAX)
  assert.equal(store.getState().contains('overflow'), false)
  assert.equal(store.getState().contains('b0'), true, '满仓不得淘汰已有选择')

  // 与 backend compare 上限保持同源（防止两处常量漂移）
  assert.equal(COMPARE_BASKET_MAX, MAX_COMPARE_BOARDS)

  store.getState().clear()
})

test('G. industry + concept 混合允许（前端不人为禁止）', () => {
  const store = useCompareBasketStore
  store.getState().clear()
  store.getState().add({ id: 'i1', name: '行业一', type: 'industry' })
  store.getState().add({ id: 'c1', name: '概念一', type: 'concept' })
  assert.deepEqual(
    store.getState().items.map((i) => i.type),
    ['industry', 'concept'],
  )
  store.getState().clear()
})

// ===========================================================================
// H. 二级导航
// ===========================================================================
test('H. 二级导航冻结为三项：大盘 / 行业 / 概念（顶层无对比 tab）', () => {
  assert.equal(REVIEW_TABS.length, 3)
  assert.deepEqual(
    REVIEW_TABS.map((t) => t.label),
    ['大盘', '行业', '概念'],
  )
  assert.deepEqual(
    REVIEW_TABS.map((t) => t.key),
    ['market', 'industry', 'concept'],
  )
  assert.deepEqual(
    REVIEW_TABS.map((t) => t.path),
    ['/review', '/review/industry', '/review/concept'],
  )

  assert.equal(reviewTabLabel(REVIEW_TABS[0]), '大盘', 'tab 文案不得携带数量')
  assert.equal(reviewTabLabel(REVIEW_TABS[1]), '行业')
  assert.equal(reviewTabLabel(REVIEW_TABS[2]), '概念')

  // 旧命名（行业板块 / 概念板块）不得回归
  for (const tab of REVIEW_TABS) assert.ok(!tab.label.includes('板块'), `tab 文案不得含「板块」：${tab.label}`)
})

// ===========================================================================
// I/J. line visibility 硬合同
// ===========================================================================
interface RecordedSeries {
  createdOptions: { color: string; lineWidth: number; priceScaleId: string; visible: boolean }
  appliedVisibility: boolean[]
  dataSets: readonly unknown[]
  priceLines: unknown[]
  handle: LineSeriesHandle
}

function createFakeChart(): {
  chart: LineChartHandle
  created: RecordedSeries[]
  removeCount: () => number
} {
  const created: RecordedSeries[] = []
  let removeCount = 0
  const chart: LineChartHandle & { remove: () => void } = {
    addLineSeries(options) {
      const record = {
        createdOptions: options,
        appliedVisibility: [] as boolean[],
        dataSets: [] as readonly unknown[],
        priceLines: [] as unknown[],
        handle: undefined as unknown as LineSeriesHandle,
      }
      record.handle = {
        applyOptions(o) {
          record.appliedVisibility.push(o.visible)
          record.createdOptions.visible = o.visible
        },
        setData(data) {
          record.dataSets = [...record.dataSets, data]
        },
        createPriceLine(line) {
          record.priceLines.push(line)
          return {}
        },
      }
      created.push(record)
      return record.handle
    },
    remove() {
      removeCount += 1
    },
  }
  return { chart, created, removeCount: () => removeCount }
}

const SPECS: readonly LineSeriesSpec[] = [
  { key: 'ma5', label: 'MA5', color: seriesColor(0), scale: 'left', lineWidth: SERIES_LINE_WIDTH },
  { key: 'ma10', label: 'MA10', color: seriesColor(1), scale: 'left', lineWidth: SERIES_LINE_WIDTH },
]

test('I. legend toggle 只切换 visibility（不重建 chart/series、不 setData、不重取数）', () => {
  const { chart, created, removeCount } = createFakeChart()
  const controller = createLineSeriesController(chart, SPECS)

  controller.setData({ ma5: [{ time: '2026-09-10', value: 0.8 }], ma10: [{ time: '2026-09-10', value: 0.7 }] })
  assert.equal(created.length, 2, 'series 只创建一次')
  assert.equal(created[0].dataSets.length, 1)

  controller.toggle('ma5')
  controller.toggle('ma10')
  controller.toggle('ma5')

  assert.equal(created.length, 2, 'toggle 绝不重建 series')
  assert.equal(removeCount(), 0, 'toggle 绝不销毁 chart')
  assert.equal(created[0].dataSets.length, 1, 'toggle 绝不再次 setData')
  assert.equal(created[1].dataSets.length, 1, 'toggle 绝不再次 setData')
  assert.deepEqual(created[0].appliedVisibility, [false, true], 'toggle 只允许 applyOptions({visible})')
  assert.deepEqual(created[1].appliedVisibility, [false])

  assert.equal(controller.isVisible('ma5'), true)
  assert.equal(controller.isVisible('ma10'), false)
  assert.deepEqual(controller.visibility(), { ma5: true, ma10: false })

  // 未知 key 必须安全 no-op（不抛错、不误伤其他 series）
  controller.toggle('nope')
  assert.equal(created[0].appliedVisibility.length, 2)
})

test('J. toggle 不改变 input points（同一引用、内容不变）', () => {
  const { chart, created } = createFakeChart()
  const controller = createLineSeriesController(chart, SPECS)

  const points = [{ time: '2026-09-10', value: 0.8 }, { time: '2026-09-09' }]
  const snapshot = JSON.parse(JSON.stringify(points)) as unknown

  controller.setData({ ma5: points })
  controller.toggle('ma5')
  controller.toggle('ma5')

  assert.deepEqual(points, snapshot, 'input points 不得被改写')
  assert.equal(created[0].dataSets.length, 1, 'toggle 不得再次 setData')
  assert.equal(created[0].dataSets[0], points, 'setData 必须收到源数组本身（不复制、不改写）')
})

test('K. aria-pressed 契约：可见=true / 隐藏=false', () => {
  assert.equal(legendAriaPressed(true), 'true')
  assert.equal(legendAriaPressed(false), 'false')
})

test('K2. legend 用原生 button + aria-pressed + eye/eye-off（Enter/Space 可操作）', () => {
  const source = readFileSync(new URL('../MultiLineChart.tsx', import.meta.url), 'utf8')
  assert.match(source, /<button/, 'legend 必须是原生 button（原生支持 Enter/Space）')
  assert.match(source, /aria-pressed=\{legendAriaPressed\(/, 'legend 必须暴露 aria-pressed')
  assert.match(source, /EyeIcon/, '可见态需要 eye 图标')
  assert.match(source, /EyeOffIcon/, '隐藏态需要 eye-off 图标')
  assert.match(source, /data-visible=/, '需要可断言的可见态标记')
  // 唯一允许的可见性副作用
  assert.match(source, /\.toggle\(spec\.key\)/, 'legend 点击必须走 controller.toggle')
})

// ===========================================================================
// L. palette / tokens
// ===========================================================================
test('L. 普通 series palette 与参考线都不使用 market.up / market.down', () => {
  const forbidden = MARKET_DIRECTION_COLORS.map((c) => c.toUpperCase())

  assert.ok(SERIES_PALETTE.length >= COMPARE_BASKET_MAX, 'palette 至少覆盖 compare 上限 20 条线')
  for (const color of SERIES_PALETTE) {
    assert.ok(!forbidden.includes(color.toUpperCase()), `series palette 不得含行情方向色：${color}`)
  }
  for (const line of BREADTH_REFERENCE_LINES) {
    assert.ok(!forbidden.includes(line.color.toUpperCase()), `参考线不得用涨跌色：${line.color}`)
  }

  // 品牌绿是 focus/action：可作为 identity 起点，但不是涨跌
  assert.equal(seriesColor(0), REVIEW_TOKENS.brand.primary)
  assert.equal(SERIES_PALETTE.length, 20)
  assert.equal(new Set(SERIES_PALETTE).size, SERIES_PALETTE.length, 'palette 颜色必须互不相同')

  // EW 更粗（主线），但仍是普通身份色
  assert.ok(EW_LINE_WIDTH > SERIES_LINE_WIDTH)
})

test('L2. Review V2 tokens 与冻结值一致（品牌绿 = focus/action，红涨绿跌独立）', () => {
  assert.deepEqual(REVIEW_TOKENS.brand, { primary: '#00F6C2', primaryHover: '#39F5CF', deep: '#00B28A' })
  assert.deepEqual(REVIEW_TOKENS.background, { base: '#0A0F14', secondary: '#111A23', card: '#161F29' })
  assert.deepEqual(REVIEW_TOKENS.text, { primary: '#F2F6F8', secondary: '#98A1B3', muted: '#657281' })
  assert.equal(REVIEW_TOKENS.border, '#263440')
  assert.deepEqual(REVIEW_TOKENS.market, { up: '#FF4D4F', down: '#22C55E' })
  assert.deepEqual(REVIEW_TOKENS.status, { info: '#3882F6', warning: '#F59E0B', purple: '#8B5CF6' })
  assert.deepEqual(MARKET_DIRECTION_COLORS, ['#FF4D4F', '#22C55E'])
  // 参考线是阈值语义（warning / muted），不是涨跌
  assert.deepEqual(
    BREADTH_REFERENCE_LINES.map((l) => l.price),
    [0.8, 0.2],
  )
})
