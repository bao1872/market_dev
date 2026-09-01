// [Slice B] Dynamics 共享时间轴纯函数契约。
//
// 覆盖：
// - buildSharedTradingDates：相同 / 不同 / 含空日期数组 → 升序去重并集
// - alignToSharedDomain：middle / leading / trailing missing、单点、整条缺失
// - 三 series 对齐后 time key 必须完全一致（同一时间横截面）
// - 缺失 = whitespace gap（无 value），绝不填 0 / 插值 / carry
// - valueAtDate / chartValueAtTime / shouldApplyRange
import { strict as assert } from 'node:assert'
import { test } from 'node:test'

import {
  buildSharedTradingDates,
  alignToSharedDomain,
  valueAtDate,
  chartValueAtTime,
  shouldApplyRange,
  type LogicalRange,
  type ScopeDynamicsChartData,
} from '../scopeDynamicsChart'

const D = ['2026-08-25', '2026-08-26', '2026-08-27', '2026-08-28']

/** 从 chart data 抽出 time 序列 */
function times(data: ScopeDynamicsChartData): string[] {
  return data.map((p) => p.time)
}
/** 抽出「有值」的 (time,value) 对 */
function valued(data: ScopeDynamicsChartData): Array<[string, number]> {
  return data
    .filter((p): p is { time: string; value: number } => 'value' in p && typeof p.value === 'number')
    .map((p) => [p.time, p.value])
}

// ============================================================
// 1. buildSharedTradingDates
// ============================================================

test('shared dates: 三个相同日期数组 → 原样升序去重', () => {
  const out = buildSharedTradingDates(D, D, D)
  assert.deepEqual(out, D)
})

test('shared dates: 不同日期数组 → 升序去重并集', () => {
  const pos = ['2026-08-27', '2026-08-25']
  const vel = ['2026-08-26', '2026-08-25', '2026-08-25']
  const acc = ['2026-08-28']
  assert.deepEqual(buildSharedTradingDates(pos, vel, acc), D)
})

test('shared dates: 空数组 → 空 domain', () => {
  assert.deepEqual(buildSharedTradingDates([], [], []), [])
})

test('shared dates: 忽略空字符串日期', () => {
  assert.deepEqual(buildSharedTradingDates(['2026-08-26', ''], [], []), ['2026-08-26'])
})

// ============================================================
// 2. alignToSharedDomain：缺失一律 whitespace gap
// ============================================================

test('align: 完整序列 → 全部有值', () => {
  const out = alignToSharedDomain(D, D, [60, 62, 65, 63])
  assert.deepEqual(times(out), D)
  assert.equal(valued(out).length, 4)
  assert.deepEqual(valued(out), [['2026-08-25', 60], ['2026-08-26', 62], ['2026-08-27', 65], ['2026-08-28', 63]])
})

test('align: middle missing → 该日无 value（gap，不插值）', () => {
  // velocity 在 8/26 缺失
  const out = alignToSharedDomain(D, D, [1.2, null, 1.5, 1.1])
  assert.deepEqual(times(out), D, 'time 仍覆盖全部 domain')
  assert.equal(valued(out).length, 3, '缺失日不得产生 value')
  assert.deepEqual(valued(out), [['2026-08-25', 1.2], ['2026-08-27', 1.5], ['2026-08-28', 1.1]])
  // 8/26 是 whitespace point
  const gap = out.find((p) => p.time === '2026-08-26')
  assert.ok(gap && !('value' in gap), '8/26 必须是纯 time 的 whitespace point')
})

test('align: leading missing → 起始日 gap', () => {
  const out = alignToSharedDomain(D, D, [null, 0.5, 0.6, 0.7])
  assert.deepEqual(times(out), D)
  assert.deepEqual(valued(out), [['2026-08-26', 0.5], ['2026-08-27', 0.6], ['2026-08-28', 0.7]])
})

test('align: trailing missing → 末尾日 gap', () => {
  const out = alignToSharedDomain(D, D, [0.5, 0.6, 0.7, null])
  assert.deepEqual(valued(out), [['2026-08-25', 0.5], ['2026-08-26', 0.6], ['2026-08-27', 0.7]])
})

test('align: 整条 series 缺失 → 全 gap，不填 0', () => {
  const out = alignToSharedDomain(D, [], [])
  assert.deepEqual(times(out), D, '仍保留全部 domain 位置')
  assert.equal(valued(out).length, 0, '整条缺失不得填值')
})

test('align: 单个交易日', () => {
  const out = alignToSharedDomain(['2026-08-25'], ['2026-08-25'], [42])
  assert.deepEqual(valued(out), [['2026-08-25', 42]])
  const outGap = alignToSharedDomain(['2026-08-25'], ['2026-08-25'], [null])
  assert.deepEqual(times(outGap), ['2026-08-25'])
  assert.equal(valued(outGap).length, 0)
})

test('align: NaN 视为缺失（不得变成数值点）', () => {
  const out = alignToSharedDomain(D, D, [1.0, Number.NaN, 2.0, 3.0])
  assert.deepEqual(valued(out), [['2026-08-25', 1.0], ['2026-08-27', 2.0], ['2026-08-28', 3.0]])
})

test('align: 真实 0 是有效值（不得被当成缺失）', () => {
  const out = alignToSharedDomain(D, D, [0, 1, 0, 2])
  assert.equal(valued(out).length, 4, '0 必须保留为数据点')
  assert.deepEqual(valued(out), [['2026-08-25', 0], ['2026-08-26', 1], ['2026-08-27', 0], ['2026-08-28', 2]])
})

// ============================================================
// 3. 三 series 对齐后 time key 完全一致
// ============================================================

test('三 series 各自不同日期 → 对齐后 time key 完全一致', () => {
  const posDates = ['2026-08-25', '2026-08-26', '2026-08-27']
  const velDates = ['2026-08-26', '2026-08-28']
  const accDates = ['2026-08-25', '2026-08-28']
  const domain = buildSharedTradingDates(posDates, velDates, accDates)
  assert.deepEqual(domain, ['2026-08-25', '2026-08-26', '2026-08-27', '2026-08-28'])

  const p = alignToSharedDomain(domain, posDates, [60, 62, 65])
  const v = alignToSharedDomain(domain, velDates, [1.2, 1.5])
  const a = alignToSharedDomain(domain, accDates, [0.3, 0.5])

  assert.deepEqual(times(p), domain)
  assert.deepEqual(times(v), domain)
  assert.deepEqual(times(a), domain)
  assert.deepEqual(times(p), times(v), 'position 与 velocity 时间轴一致')
  assert.deepEqual(times(v), times(a), 'velocity 与 acceleration 时间轴一致')
})

test('各 series 保留自己的缺失语义（8/26 只在 velocity 有值）', () => {
  const domain = ['2026-08-25', '2026-08-26']
  const p = alignToSharedDomain(domain, ['2026-08-25'], [60])
  const v = alignToSharedDomain(domain, ['2026-08-25', '2026-08-26'], [1.2, 1.5])
  assert.equal(chartValueAtTime(p, '2026-08-26'), null, 'position 在 8/26 缺失')
  assert.equal(chartValueAtTime(v, '2026-08-26'), 1.5, 'velocity 在 8/26 有值')
  // 同一天跨 series 取数用于 tooltip
  assert.equal(chartValueAtTime(p, '2026-08-25'), 60)
  assert.equal(chartValueAtTime(v, '2026-08-25'), 1.2)
})

// ============================================================
// 4. 取值与同步守卫
// ============================================================

test('valueAtDate: 有值 / 缺失 / 无该日期', () => {
  assert.equal(valueAtDate(D, [1, null, 3, 4], '2026-08-25'), 1)
  assert.equal(valueAtDate(D, [1, null, 3, 4], '2026-08-26'), null)
  assert.equal(valueAtDate(D, [1, null, 3, 4], '2026-09-01'), null)
  assert.equal(valueAtDate(D, [1, null, 3, 4], null), null)
})

test('chartValueAtTime: gap / 未命中 / null 输入', () => {
  const data = alignToSharedDomain(D, D, [1, null, 3, 4])
  assert.equal(chartValueAtTime(data, '2026-08-25'), 1)
  assert.equal(chartValueAtTime(data, '2026-08-26'), null, 'gap 返回 null')
  assert.equal(chartValueAtTime(data, '2026-09-01'), null)
  assert.equal(chartValueAtTime(data, null), null)
})

test('shouldApplyRange: 防同步回环 + 过滤非法 range', () => {
  // lightweight-charts 的 Logical 是 branded number，测试里需显式构造
  const range = (from: number, to: number): LogicalRange =>
    ({ from, to } as unknown as LogicalRange)

  assert.equal(shouldApplyRange(range(0, 5), false), true)
  assert.equal(shouldApplyRange(range(0, 5), true), false, 'syncing 中不得再触发')
  assert.equal(shouldApplyRange(null, false), false)
  assert.equal(shouldApplyRange(range(Number.NaN, 5), false), false)
})
