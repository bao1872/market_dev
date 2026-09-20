// [MarketDashboard] - 纯逻辑单测（null 断线 / 格式 / 比较上限 / 错误分类）
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  formatBreadth,
  formatEwIndex,
  formatDelta,
  deltaDirection,
  buildLineData,
  buildRankingsParams,
  validateCompareSelection,
  classifyDashboardError,
} from '../dashboardLogic'

test('formatBreadth: 0.6321 -> 63.2%，null -> —（不显示 0%）', () => {
  assert.equal(formatBreadth(0.6321), '63.2%')
  assert.equal(formatBreadth(1), '100.0%')
  assert.equal(formatBreadth(0), '0.0%')
  assert.equal(formatBreadth(null), '—')
  assert.equal(formatBreadth(undefined), '—')
})

test('formatEwIndex: 104.28 -> 104.28，null -> —', () => {
  assert.equal(formatEwIndex(104.28), '104.28')
  assert.equal(formatEwIndex(null), '—')
})

test('formatDelta / deltaDirection: A股 红涨绿跌', () => {
  assert.equal(formatDelta(0.05), '+5.0%')
  assert.equal(formatDelta(-0.03), '-3.0%')
  assert.equal(formatDelta(null), '—')
  assert.equal(deltaDirection(0.05), 'up')
  assert.equal(deltaDirection(-0.03), 'down')
  assert.equal(deltaDirection(0), 'flat')
  assert.equal(deltaDirection(null), 'flat')
})

test('buildLineData: null 必须断线（不转 0 / 不 forward fill / 不插值）', () => {
  const data = [
    { trade_date: '2026-09-01', ma20: 0.5 },
    { trade_date: '2026-09-02', ma20: null },
    { trade_date: '2026-09-03', ma20: 0.7 },
    { trade_date: '2026-09-04', ma20: undefined as unknown as number | null },
  ]
  const out = buildLineData(data, 'trade_date', 'ma20')
  assert.deepEqual(out, [
    { time: '2026-09-01', value: 0.5 },
    { time: '2026-09-03', value: 0.7 },
  ])
  // 明确断言 null 那天的点不存在（没有 value:0 的断点伪装）
  assert.ok(!out.some((p) => p.time === '2026-09-02'))
})

test('buildRankingsParams: industry 传层级；concept 不传 hierarchy_level', () => {
  const ind = buildRankingsParams('industry', 'L2')
  assert.equal(ind.scope_type, 'industry')
  assert.equal(ind.hierarchy_level, 'L2')
  assert.equal(ind.lookback, 5)
  assert.equal(ind.limit, 10)
  const con = buildRankingsParams('concept', null)
  assert.equal(con.scope_type, 'concept')
  assert.equal(con.hierarchy_level, undefined)
})

test('validateCompareSelection: 最多 20，第 21 被前端阻止，混合 industry/concept 不被拒绝', () => {
  const ok = Array.from({ length: 20 }, (_, i) => `id${i}`)
  assert.equal(validateCompareSelection(ok).ok, true)
  const over = [...ok, 'id21']
  assert.equal(validateCompareSelection(over).ok, false)
  assert.match(validateCompareSelection(over).message ?? '', /20/)
  // 混合类型不被前端人为禁止
  assert.equal(validateCompareSelection(['ind-x', 'con-y']).ok, true)
})

test('classifyDashboardError: 403 forbidden / 404 not-found / 其他 error', () => {
  assert.equal(classifyDashboardError({ status: 403, detail: '', message: 'x' }).kind, 'forbidden')
  assert.equal(classifyDashboardError({ status: 404, detail: '', message: 'x' }).kind, 'not-found')
  assert.equal(classifyDashboardError({ status: 500, detail: '', message: 'boom' }).kind, 'error')
})
