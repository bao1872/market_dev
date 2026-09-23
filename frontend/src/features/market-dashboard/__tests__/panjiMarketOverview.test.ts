// [PANJI-MARKET-OVERVIEW] 大盘快照 + 历史 2×2 面板合同测试（纯逻辑 + 源码契约，无 DOM）
//
// 覆盖：
//   A 快照 6 卡顺序锁死 · B 快照标签 · C 历史 4 图 · D 指数三色品牌色 ·
//   E 涨跌家数红涨绿跌 · F 成交额元->亿 transform · G 涨停跌停红绿 ·
//   H 格式器（index/count/turnover）· I buildLineData transform + null gap ·
//   J 页面渲染快照 + 历史组件、且不重复 MA 卡片。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  MARKET_HISTORY_CHARTS,
  SNAPSHOT_CARD_LABELS,
  SNAPSHOT_CARD_ORDER,
} from '../marketOverviewConfig'
import { REVIEW_TOKENS } from '../chartTheme'
import { buildLineData, formatCount, formatIndex, formatTurnover } from '../dashboardLogic'

const PAGE_SOURCE = readFileSync(new URL('../MarketDashboardPage.tsx', import.meta.url), 'utf8')

test('A. 快照 6 卡顺序锁死 = 上证 / 深证 / 创业板 / 涨跌家数 / 成交额 / 涨停跌停', () => {
  assert.deepEqual([...SNAPSHOT_CARD_ORDER], ['sse', 'szse', 'chinext', 'breadth', 'turnover', 'limit'])
})

test('B. 快照 6 卡标签精确', () => {
  assert.equal(SNAPSHOT_CARD_LABELS.sse, '上证指数')
  assert.equal(SNAPSHOT_CARD_LABELS.szse, '深证成指')
  assert.equal(SNAPSHOT_CARD_LABELS.chinext, '创业板指')
  assert.equal(SNAPSHOT_CARD_LABELS.breadth, '涨跌家数')
  assert.equal(SNAPSHOT_CARD_LABELS.turnover, '全市场成交额')
  assert.equal(SNAPSHOT_CARD_LABELS.limit, '涨停 / 跌停')
})

test('C. 历史面板恰好 4 张图（指数 / 涨跌家数 / 成交额 / 涨停跌停）', () => {
  assert.equal(MARKET_HISTORY_CHARTS.length, 4)
  assert.deepEqual(
    MARKET_HISTORY_CHARTS.map((c) => c.key),
    ['index', 'breadth', 'turnover', 'limit'],
  )
})

test('D. 指数相对走势用 3 个品牌色区分（info / purple / primary），且为 rebased 字段', () => {
  const index = MARKET_HISTORY_CHARTS[0]
  assert.deepEqual(
    index.series.map((s) => s.field),
    ['sse_rebased', 'szse_rebased', 'chinext_rebased'],
  )
  assert.equal(index.series[0].color, REVIEW_TOKENS.status.info)
  assert.equal(index.series[1].color, REVIEW_TOKENS.status.purple)
  assert.equal(index.series[2].color, REVIEW_TOKENS.brand.primary)
})

test('E. 涨跌家数序列：上涨红 / 下跌绿', () => {
  const breadth = MARKET_HISTORY_CHARTS[1]
  assert.equal(breadth.series[0].field, 'advance_count')
  assert.equal(breadth.series[0].color, REVIEW_TOKENS.market.up)
  assert.equal(breadth.series[1].field, 'decline_count')
  assert.equal(breadth.series[1].color, REVIEW_TOKENS.market.down)
})

test('F. 成交额序列元->亿 transform，且只用于 chart 呈现', () => {
  const turnover = MARKET_HISTORY_CHARTS[2]
  assert.equal(turnover.series.length, 1)
  assert.equal(turnover.series[0].field, 'turnover_amount')
  const tx = turnover.series[0].transform!
  assert.ok(typeof tx === 'function')
  assert.equal(tx(800_000_000_000), 8000) // 8000 亿
  assert.equal(tx(1_200_000_000_000), 12000) // 1.2 万亿
})

test('G. 涨停跌停序列：涨停红 / 跌停绿', () => {
  const limit = MARKET_HISTORY_CHARTS[3]
  assert.equal(limit.series[0].field, 'limit_up_count')
  assert.equal(limit.series[0].color, REVIEW_TOKENS.market.up)
  assert.equal(limit.series[1].field, 'limit_down_count')
  assert.equal(limit.series[1].color, REVIEW_TOKENS.market.down)
})

test('H. 格式器：index / count / turnover', () => {
  // 指数：千分位 + 2 位小数；null -> 「—」
  assert.equal(formatIndex(3210.45), '3,210.45')
  assert.equal(formatIndex(null), '—')
  // 计数：整数千分位；null -> 「—」
  assert.equal(formatCount(1234567), '1,234,567')
  assert.equal(formatCount(null), '—')
  // 成交额：元 -> 亿 / 万亿；null -> 「—」
  assert.equal(formatTurnover(800_000_000_000), '8,000 亿')
  assert.equal(formatTurnover(1_200_000_000_000), '1.20 万亿')
  assert.equal(formatTurnover(null), '—')
})

test('I. buildLineData 支持 transform 且保留 null gap', () => {
  const rows = [
    { trade_date: '2026-01-02', v: 200_0000_0000 },
    { trade_date: '2026-01-03', v: null as number | null },
    { trade_date: '2026-01-04', v: 300_0000_0000 },
  ]
  const out = buildLineData(rows, 'trade_date', 'v', (x) => x / 1e8)
  assert.deepEqual(out[0], { time: '2026-01-02', value: 200 })
  assert.deepEqual(out[1], { time: '2026-01-03' }) // null -> gap
  assert.deepEqual(out[2], { time: '2026-01-04', value: 300 })
})

test('J. 大盘页渲染快照 + 历史组件，且不重复 MA 卡片', () => {
  assert.match(PAGE_SOURCE, /<MarketSnapshotCards/)
  assert.match(PAGE_SOURCE, /<MarketHistoryCharts/)
  // KPI chips 直接消费 MA/EW 配置值，不再用 <Card 渲染 MA 卡片
  assert.ok(!/<Card\s/.test(PAGE_SOURCE), '大盘页不得再用 Card 渲染 MA 卡片')
  // 大盘页自身只有一个 <BreadthChart（历史 4 图在子组件内）
  assert.equal((PAGE_SOURCE.match(/<BreadthChart/g) ?? []).length, 1)
})
