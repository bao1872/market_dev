// [R3B] 大盘页（Market Overview）合同测试（纯逻辑 + 源码契约，无 DOM）
//
// 覆盖：A ranges · B default · C cards 顺序 · D 无 MA10/MA120 card · E 单图 6 series ·
//       F series 顺序 · G MA 左轴 · H EW 右轴/primary/更粗 · I 左轴 0..1 → 0..100% ·
//       J 固定区间含 0/1 · K 80/20 参考线 · L toggle 硬合同 · M/N ranking 请求 ·
//       O explorer 链接 · P 旧双图已移除 · Q MA identity 色 · R 涨跌色只在 delta 方向。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  BREADTH_FIXED_RANGE,
  CONCEPT_ALL_LINK,
  INDUSTRY_ALL_LINK,
  INDUSTRY_DEFAULT_HIERARCHY_LEVEL,
  MARKET_CARD_LABELS,
  MARKET_CARD_ORDER,
  MARKET_OVERVIEW_SERIES,
  MARKET_RANKING_SUMMARIES,
  RANKING_SUMMARY_LIMIT,
  RANKING_SUMMARY_LOOKBACK,
  buildConceptExplorerLink,
  buildIndustryExplorerLink,
} from '../marketOverviewConfig'
import {
  BREADTH_REFERENCE_LINES,
  EW_LINE_WIDTH,
  MARKET_DIRECTION_COLORS,
  REVIEW_TOKENS,
  SERIES_LINE_WIDTH,
  seriesColor,
} from '../chartTheme'
import {
  breadthPercentFormatter,
  buildSeriesOptions,
  createLineSeriesController,
  type LineChartHandle,
  type LineSeriesHandle,
  type LineSeriesSpec,
} from '../lineSeriesController'

const PAGE_SOURCE = readFileSync(new URL('../MarketDashboardPage.tsx', import.meta.url), 'utf8')
const SUMMARY_SOURCE = readFileSync(new URL('../MarketRankingSummary.tsx', import.meta.url), 'utf8')

/** 与 MultiLineChart 的展开方式一致：把 overview 配置展开为图表 LineSeriesSpec。 */
function toLineSeriesSpecs(): LineSeriesSpec[] {
  return MARKET_OVERVIEW_SERIES.map((spec, index) => ({
    key: spec.field,
    label: spec.label,
    color: spec.color ?? seriesColor(index),
    scale: spec.scale,
    lineWidth: spec.lineWidth,
    breadthPercent: spec.breadthPercent,
    fixedScaleRange: spec.fixedScaleRange,
  }))
}

// ===========================================================================
// A–D. 固定窗口 / KPI 卡片
// ===========================================================================
test('A. 大盘页固定 250 日窗口，无时间范围切换', () => {
  // [PANJI-MARKET-OVERVIEW] Phase B：移除 20/60/120/250 切换，固定 250 日。
  assert.ok(!PAGE_SOURCE.includes('rangeSelector'), '不得保留时间范围 selector')
  // 仅校验原 selector 文案「时间范围：」（全角冒号）；空态提示「当前时间范围暂无数据」属正常文案。
  assert.ok(!PAGE_SOURCE.includes('时间范围：'), '不得保留「时间范围：」切换文案')
  assert.match(PAGE_SOURCE, /useMarketDashboard\(250\)/, '必须固定请求 250 日')
})

test('C. KPI 卡片顺序锁死 = MA5 / MA20 / MA50 / EW', () => {
  assert.deepEqual([...MARKET_CARD_ORDER], ['ma5', 'ma20', 'ma50', 'ew'])
  assert.deepEqual(
    MARKET_CARD_ORDER.map((key) => MARKET_CARD_LABELS[key]),
    ['MA5 上方占比', 'MA20 上方占比', 'MA50 上方占比', '全市场等权指数'],
  )
})

test('D. 不存在 MA10 / MA120 卡片', () => {
  const keys = MARKET_CARD_ORDER as readonly string[]
  assert.ok(!keys.includes('ma10'))
  assert.ok(!keys.includes('ma120'))
  const labels = Object.values(MARKET_CARD_LABELS).join('|')
  assert.ok(!labels.includes('MA10'))
  assert.ok(!labels.includes('MA120'))
})

// ===========================================================================
// E–H. 统一 chart
// ===========================================================================
test('E. 恰好多条统一 chart：6 条 series（不再是长/短两张图）', () => {
  assert.equal(MARKET_OVERVIEW_SERIES.length, 6)
  assert.equal((PAGE_SOURCE.match(/<BreadthChart/g) ?? []).length, 1, '大盘页只能有一个 chart 组件')
})

test('F. series 顺序精确 = MA5 / MA10 / MA20 / MA50 / MA120 / EW', () => {
  assert.deepEqual(
    MARKET_OVERVIEW_SERIES.map((s) => s.field),
    ['ma5', 'ma10', 'ma20', 'ma50', 'ma120', 'ew_index'],
  )
  assert.deepEqual(
    MARKET_OVERVIEW_SERIES.map((s) => s.label),
    ['MA5', 'MA10', 'MA20', 'MA50', 'MA120', 'EW'],
  )
})

test('G. MA5/MA10/MA20/MA50/MA120 全部在 left scale', () => {
  for (const spec of MARKET_OVERVIEW_SERIES) {
    if (spec.field === 'ew_index') continue
    assert.equal(spec.scale, 'left', `${spec.label} 必须在 left scale`)
    assert.equal(spec.breadthPercent, true, `${spec.label} 必须是 breadth 百分比轴`)
    assert.deepEqual(spec.fixedScaleRange, BREADTH_FIXED_RANGE)
  }
})

test('H. EW 在 right scale + text.primary + 更粗', () => {
  const ew = MARKET_OVERVIEW_SERIES.find((s) => s.field === 'ew_index')
  assert.ok(ew, 'EW series 必须存在')
  assert.equal(ew.scale, 'right')
  assert.equal(ew.color, REVIEW_TOKENS.text.primary)
  assert.equal(ew.lineWidth, EW_LINE_WIDTH)
  assert.ok(EW_LINE_WIDTH > SERIES_LINE_WIDTH, 'EW 必须比 MA 线更粗')
  // EW 不属于 breadth 轴
  assert.equal(ew.breadthPercent, undefined)
  assert.equal(ew.fixedScaleRange, undefined)
})

// ===========================================================================
// I–J. 左轴 0..100% 呈现
// ===========================================================================
test('I. 左轴把 0..1 呈现为 0%..100%（数据不 ×100）', () => {
  assert.equal(breadthPercentFormatter(0), '0%')
  assert.equal(breadthPercentFormatter(0.2), '20%')
  assert.equal(breadthPercentFormatter(0.8), '80%')
  assert.equal(breadthPercentFormatter(1), '100%')
  assert.equal(breadthPercentFormatter(0.6321), '63%')

  const specs = toLineSeriesSpecs()
  const ma5Options = buildSeriesOptions(specs[0])
  assert.ok(ma5Options.priceFormat, 'breadth series 必须有百分比 priceFormat')
  assert.equal(ma5Options.priceFormat.formatter(0.8), '80%')
  assert.equal(ma5Options.priceFormat.formatter(0.2), '20%')

  // EW 不参与百分比轴
  const ewOptions = buildSeriesOptions(specs[5])
  assert.equal(ewOptions.priceFormat, undefined)
  assert.equal(ewOptions.autoscaleInfoProvider, undefined)
})

test('J. 左轴固定区间包含 0 与 1（端点始终可见）', () => {
  assert.deepEqual(BREADTH_FIXED_RANGE, { min: 0, max: 1 })

  for (const spec of toLineSeriesSpecs().slice(0, 5)) {
    const options = buildSeriesOptions(spec)
    assert.ok(options.autoscaleInfoProvider, `${spec.key} 必须固定左轴区间`)
    assert.deepEqual(options.autoscaleInfoProvider().priceRange, { minValue: 0, maxValue: 1 })
  }

  // 页面不得把 breadth ×100 后再入图
  assert.ok(!/\*\s*100/.test(PAGE_SOURCE), '不得在页面内把 breadth ×100')
})

// ===========================================================================
// K–L. 参考线 + toggle 硬合同
// ===========================================================================
test('K. 统一图保留 80% / 20% 参考线（阈值语义，非涨跌）', () => {
  assert.deepEqual(
    BREADTH_REFERENCE_LINES.map((l) => l.price),
    [0.8, 0.2],
  )
  assert.deepEqual(
    BREADTH_REFERENCE_LINES.map((l) => l.color),
    [REVIEW_TOKENS.status.warning, REVIEW_TOKENS.text.muted],
  )
  for (const line of BREADTH_REFERENCE_LINES) assert.equal(line.dashed, true, '参考线必须 dashed')
  assert.match(PAGE_SOURCE, /referenceLines=\{BREADTH_REFERENCE_LINES\}/)
})

interface RecordedSeries {
  createdOptions: Parameters<LineChartHandle['addLineSeries']>[0]
  appliedVisibility: boolean[]
  dataSets: readonly unknown[]
  handle: LineSeriesHandle
}

function createFakeChart() {
  const created: RecordedSeries[] = []
  let removeCount = 0
  const chart: LineChartHandle & { remove: () => void } = {
    addLineSeries(options) {
      const record = {
        createdOptions: options,
        appliedVisibility: [] as boolean[],
        dataSets: [] as readonly unknown[],
        handle: undefined as unknown as LineSeriesHandle,
      }
      record.handle = {
        applyOptions(o) {
          record.appliedVisibility.push(o.visible)
        },
        setData(data) {
          record.dataSets = [...record.dataSets, data]
        },
        createPriceLine() {
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

test('L. 六条线全部可单独 hide/show，且 toggle 只 applyOptions({visible})', () => {
  const { chart, created, removeCount } = createFakeChart()
  const controller = createLineSeriesController(chart, toLineSeriesSpecs())

  assert.equal(created.length, 6, '6 条 series 只创建一次')
  // 创建期已带上百分比轴 / 固定区间配置
  assert.ok(created[0].createdOptions.priceFormat, 'MA5 创建期即带百分比轴')
  assert.ok(created[0].createdOptions.autoscaleInfoProvider, 'MA5 创建期即带固定区间')

  controller.setData(
    Object.fromEntries(MARKET_OVERVIEW_SERIES.map((s) => [s.field, [{ time: '2026-09-10', value: 0.5 }]])),
  )
  for (const record of created) assert.equal(record.dataSets.length, 1)

  // 六条线逐条 hide
  for (const spec of MARKET_OVERVIEW_SERIES) {
    controller.toggle(spec.field)
    assert.equal(controller.isVisible(spec.field), false)
  }
  assert.equal(created.length, 6, 'toggle 绝不重建 series')
  assert.equal(removeCount(), 0, 'toggle 绝不销毁 chart')
  for (const record of created) {
    assert.deepEqual(record.appliedVisibility, [false], 'toggle 只允许 applyOptions({visible})')
    assert.equal(record.dataSets.length, 1, 'toggle 绝不再次 setData')
  }

  // 再逐条 show
  for (const spec of MARKET_OVERVIEW_SERIES) {
    controller.toggle(spec.field)
    assert.equal(controller.isVisible(spec.field), true)
  }
  for (const record of created) assert.deepEqual(record.appliedVisibility, [false, true])
  assert.equal(created.length, 6)
  assert.equal(removeCount(), 0)
})

// ===========================================================================
// M–N. ranking summary 请求参数
// ===========================================================================
test('M. 行业摘要：industry / L1 / lookback 5 / limit 5', () => {
  const industry = MARKET_RANKING_SUMMARIES[0]
  assert.equal(industry.key, 'industry')
  assert.equal(industry.scopeType, 'industry')
  assert.equal(industry.hierarchyLevel, 'L1')
  assert.equal(industry.hierarchyLevel, INDUSTRY_DEFAULT_HIERARCHY_LEVEL)
  assert.equal(industry.lookback, 5)
  assert.equal(industry.limit, 5)
  assert.equal(RANKING_SUMMARY_LOOKBACK, 5)
  assert.equal(RANKING_SUMMARY_LIMIT, 5)

  // 页面用配置值调用现有 rankings hook（不重排、不用 Explorer collection API）
  assert.equal((PAGE_SOURCE.match(/useMarketRankings\(/g) ?? []).length, 2)
  assert.match(PAGE_SOURCE, /RANKING_SUMMARY_LOOKBACK/)
  assert.match(PAGE_SOURCE, /RANKING_SUMMARY_LIMIT/)
  assert.ok(!PAGE_SOURCE.includes('useMarketScopeExplorer'), '大盘页不得用 Explorer API 做 ranking')
})

test('N. 概念摘要：concept / 无层级 / lookback 5 / limit 5', () => {
  const concept = MARKET_RANKING_SUMMARIES[1]
  assert.equal(concept.key, 'concept')
  assert.equal(concept.scopeType, 'concept')
  assert.equal(concept.hierarchyLevel, null, 'concept 不得携带层级')
  assert.equal(concept.lookback, 5)
  assert.equal(concept.limit, 5)
})

// ===========================================================================
// O–P. 链接 + 旧结构移除
// ===========================================================================
test('O. Explorer 链接精确（R3B 只生成，R3C 消费）', () => {
  assert.equal(buildIndustryExplorerLink('abc-123'), '/review/industry?hierarchy_level=L1&board_id=abc-123')
  assert.equal(buildIndustryExplorerLink('abc', 'L2'), '/review/industry?hierarchy_level=L2&board_id=abc')
  assert.equal(buildConceptExplorerLink('c-1'), '/review/concept?board_id=c-1')
  assert.equal(INDUSTRY_ALL_LINK, '/review/industry?hierarchy_level=L1')
  assert.equal(CONCEPT_ALL_LINK, '/review/concept')
  assert.equal(MARKET_RANKING_SUMMARIES[0].seeAllLink, INDUSTRY_ALL_LINK)
  assert.equal(MARKET_RANKING_SUMMARIES[1].seeAllLink, CONCEPT_ALL_LINK)
  assert.equal(MARKET_RANKING_SUMMARIES[0].explorerLink('x'), '/review/industry?hierarchy_level=L1&board_id=x')
  assert.equal(MARKET_RANKING_SUMMARIES[1].explorerLink('y'), '/review/concept?board_id=y')
  assert.ok(buildConceptExplorerLink('a b&c').includes('board_id=a%20b%26c'), 'board_id 必须转义')
})

test('P. 旧「长周期 / 短周期」双图结构已移除', () => {
  assert.ok(!PAGE_SOURCE.includes('长周期'), '不得再有长周期图')
  assert.ok(!PAGE_SOURCE.includes('短周期'), '不得再有短周期图')
  assert.equal((PAGE_SOURCE.match(/<BreadthChart/g) ?? []).length, 1)
  // 页面必须使用统一 series 配置（而不是内联两套 series 常量）
  assert.match(PAGE_SOURCE, /MARKET_OVERVIEW_SERIES/)
})

// ===========================================================================
// Q–R. 颜色语义边界
// ===========================================================================
test('Q. 普通 MA / EW identity 色不含 market.up / market.down', () => {
  const forbidden = MARKET_DIRECTION_COLORS.map((c) => c.toUpperCase())
  MARKET_OVERVIEW_SERIES.forEach((spec, index) => {
    const color = (spec.color ?? seriesColor(index)).toUpperCase()
    assert.ok(!forbidden.includes(color), `series(${spec.field}) 不得使用涨跌色：${color}`)
  })
  assert.ok(!forbidden.includes(REVIEW_TOKENS.text.primary.toUpperCase()))
})

test('R. 涨跌色只用于 ranking delta 方向语义', () => {
  // ranking summary 用 deltaDirection 决定红涨绿跌
  assert.match(SUMMARY_SOURCE, /deltaDirection\(/)
  assert.match(SUMMARY_SOURCE, /styles\.up/)
  assert.match(SUMMARY_SOURCE, /styles\.down/)

  // 图表侧完全没有方向色语义
  const chartSource = readFileSync(new URL('../MultiLineChart.tsx', import.meta.url), 'utf8')
  assert.ok(!chartSource.includes('deltaDirection'))
  assert.ok(!/styles\.(up|down)\b/.test(chartSource))

  assert.deepEqual(MARKET_DIRECTION_COLORS, [REVIEW_TOKENS.market.up, REVIEW_TOKENS.market.down])
})
