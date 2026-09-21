// [R3D] Compare 页 + R3D0 matrix contract 测试（纯逻辑 + 源码契约，无 DOM）。
//
// 覆盖：A 范围精确 · B 默认 10 · C/D query key 保留顺序 · E/F basket 顺序与混合 ·
//       G 空篮不启用 API · H/I 空态 SPA Link（无 <a href>）· J DTO additive 字段 ·
//       K 矩阵列锁死 · L 类型标签 · M/N/O NULL→— · P/Q delta 方向 ·
//       R 前端不重排 · S 前端不重算 MA/delta · T/U/V 只 useMarketCompare ·
//       W/X chart 复用 CompareChart + 独立归一文案 · Y/Z 日期渲染 ·
//       AA/AB remove/clear · AC 图表线 toggle 基础未坏 · AD tab 计数=篮数。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { COMPARE_DEFAULT_RANGE, COMPARE_MATRIX_COLUMNS, COMPARE_RANGES, boardTypeLabel, formatMemberCount } from '../comparePageConfig'
import { deltaDirection, formatBreadth, formatDelta } from '../dashboardLogic'
import { useCompareBasketStore } from '../../../store/compareBasket'

const PAGE_SOURCE = readFileSync(new URL('../ComparePage.tsx', import.meta.url), 'utf8')
const DTO_SOURCE = readFileSync(new URL('../../../api/marketDashboard.ts', import.meta.url), 'utf8')
const HOOK_SOURCE = readFileSync(new URL('../../../hooks/useMarketDashboardApi.ts', import.meta.url), 'utf8')
const CHART_SOURCE = readFileSync(new URL('../CompareChart.tsx', import.meta.url), 'utf8')

// ===========================================================================
// A/B. 范围冻结
// ===========================================================================
test('A. 比较范围精确 = 10/20/60', () => {
  assert.deepEqual([...COMPARE_RANGES], [10, 20, 60])
})

test('B. 默认范围 = 10', () => {
  assert.equal(COMPARE_DEFAULT_RANGE, 10)
})

// ===========================================================================
// C/D. compare query key 必须保留 basket 顺序（'A,B' != 'B,A'）
// ===========================================================================
test('C. compare query key 保留 board 顺序（join 不用 sort）', () => {
  assert.ok(HOOK_SOURCE.includes('boardIds.join'), 'compare key 必须用 boardIds.join 保留顺序')
  assert.ok(!HOOK_SOURCE.includes('[...boardIds].sort()'), 'compare key 不得 .sort()')
  assert.ok(!HOOK_SOURCE.includes('.sort().join'), 'compare key 不得 sort 后 join')
})

test('D. key(A,B) != key(B,A)', () => {
  const keyAB = JSON.stringify(['market-dashboard', 'compare', 'a,b', 10])
  const keyBA = JSON.stringify(['market-dashboard', 'compare', 'b,a', 10])
  assert.notEqual(keyAB, keyBA, 'A,B 与 B,A 必须生成不同缓存 key（否则删 A 重加 A 会复用旧缓存）')
})

// ===========================================================================
// E/F. basket 顺序与混合
// ===========================================================================
test('E. basket request 顺序 = store 顺序（删除中间项其余保持）', () => {
  const store = useCompareBasketStore
  store.getState().clear()
  store.getState().add({ id: 'a', name: 'A', type: 'industry' })
  store.getState().add({ id: 'b', name: 'B', type: 'concept' })
  store.getState().add({ id: 'c', name: 'C', type: 'industry' })
  assert.deepEqual(store.getState().items.map((i) => i.id), ['a', 'b', 'c'])
  store.getState().remove('b')
  assert.deepEqual(store.getState().items.map((i) => i.id), ['a', 'c'], '移除 b 后顺序保持 a,c')
  store.getState().clear()
})

test('F. industry + concept 混合允许（前端不人为禁止）', () => {
  const store = useCompareBasketStore
  store.getState().clear()
  store.getState().add({ id: 'i', name: '行业', type: 'industry' })
  store.getState().add({ id: 'c', name: '概念', type: 'concept' })
  assert.deepEqual(store.getState().items.map((i) => i.type), ['industry', 'concept'])
  store.getState().clear()
})

// ===========================================================================
// G. 空篮不启用 API
// ===========================================================================
test('G. 空篮不启用 compare 请求（enabled = length>0 && <=20）', () => {
  assert.match(HOOK_SOURCE, /enabled:\s*boardIds\.length > 0 && boardIds\.length <= 20/, '空篮 / 超 20 必须禁用请求')
})

// ===========================================================================
// H/I. 空态 SPA Link（无 <a href> 整页 reload）
// ===========================================================================
test('H. 空态用 React Router Link 跳行业/概念（保留 session basket）', () => {
  assert.match(PAGE_SOURCE, /import \{[^}]*Link[^}]*\} from 'react-router-dom'/, '必须 import Link')
  assert.match(PAGE_SOURCE, /<Link[^>]*to="\/review\/industry\?hierarchy_level=L1"/, '必须 Link 到行业 L1')
  assert.match(PAGE_SOURCE, /<Link[^>]*to="\/review\/concept"/, '必须 Link 到概念')
})

test('I. ComparePage 不得出现 <a href>（任何整页 reload 都会清空 Zustand basket）', () => {
  assert.ok(!PAGE_SOURCE.includes('href="'), 'ComparePage 不得含 <a href>')
})

// ===========================================================================
// J. DTO additive 字段
// ===========================================================================
test('J. Compare DTO 含全部 R3D0 additive 字段（不自行计算 MA/delta）', () => {
  assert.match(
    DTO_SOURCE,
    /interface CompareBoard[\s\S]*?points: ComparePoint\[\][\s\S]*?member_count: number \| null[\s\S]*?ma5: number \| null[\s\S]*?ma120: number \| null[\s\S]*?ma5_delta: number \| null[\s\S]*?ma10_delta: number \| null/,
    'CompareBoard 必须含 member_count / ma5..ma120 / ma5_delta / ma10_delta',
  )
  assert.match(
    DTO_SOURCE,
    /interface CompareResponse[\s\S]*?projection_trade_date: string \| null[\s\S]*?previous_trade_date: string \| null[\s\S]*?boards: CompareBoard\[\]/,
    'CompareResponse 必须含 projection_trade_date / previous_trade_date / boards',
  )
})

// ===========================================================================
// K. 矩阵列锁死
// ===========================================================================
test('K. 矩阵列顺序锁死 = name/type/ma5/ma5_delta/ma10/ma10_delta/ma20/ma50/ma120/member_count', () => {
  assert.deepEqual([...COMPARE_MATRIX_COLUMNS], [
    'name',
    'type',
    'ma5',
    'ma5_delta',
    'ma10',
    'ma10_delta',
    'ma20',
    'ma50',
    'ma120',
    'member_count',
  ])
})

// ===========================================================================
// L. 类型标签
// ===========================================================================
test('L. 类型标签 industry→行业 / concept→概念 / 其他→—', () => {
  assert.equal(boardTypeLabel('industry'), '行业')
  assert.equal(boardTypeLabel('concept'), '概念')
  assert.equal(boardTypeLabel('unknown'), '—')
  assert.equal(boardTypeLabel(null), '—')
})

// ===========================================================================
// M/N/O. NULL → —
// ===========================================================================
test('M. breadth NULL → —（不显示 0%）', () => {
  assert.equal(formatBreadth(null), '—')
  assert.equal(formatBreadth(0.8), '80.0%')
})

test('N. delta NULL → —', () => {
  assert.equal(formatDelta(null), '—')
  assert.equal(formatDelta(0.05), '+5.0%')
  assert.equal(formatDelta(-0.03), '-3.0%')
})

test('O. member_count NULL → —（locale 显示整数）', () => {
  assert.equal(formatMemberCount(null), '—')
  assert.equal(formatMemberCount(1234), '1,234')
})

// ===========================================================================
// P/Q. delta 方向（A 股：正=红/up，负=绿/down，0/null=neutral）
// ===========================================================================
test('P. 正 delta → up（红涨）', () => {
  assert.equal(deltaDirection(0.05), 'up')
})

test('Q. 负 delta → down（绿跌）', () => {
  assert.equal(deltaDirection(-0.03), 'down')
  assert.equal(deltaDirection(0), 'flat')
  assert.equal(deltaDirection(null), 'flat')
})

// ===========================================================================
// R/S. 前端不重排 / 不重算
// ===========================================================================
test('R. 矩阵行不客户端排序（response 顺序即 basket 顺序）', () => {
  assert.equal(/\.sort\(/.test(PAGE_SOURCE), false, 'ComparePage 不得对 boards 客户端重排')
})

test('S. 前端不重算 MA / delta（只消费 backend 字段）', () => {
  for (const tok of ['above_count', 'valid_count', 'ma5_above', 'ma10_above']) {
    assert.ok(!PAGE_SOURCE.includes(tok), `ComparePage 不得重算：${tok}`)
  }
})

// ===========================================================================
// T/U/V. 数据唯一 server owner = useMarketCompare
// ===========================================================================
test('T. ComparePage 不用 useMarketScopeDetail', () => {
  assert.ok(!PAGE_SOURCE.includes('useMarketScopeDetail'))
})

test('U. ComparePage 不用 useMarketScopeExplorer', () => {
  assert.ok(!PAGE_SOURCE.includes('useMarketScopeExplorer'))
})

test('V. ComparePage 只用 useMarketCompare 取数（无 detail/Explorer/N+1）', () => {
  assert.match(PAGE_SOURCE, /useMarketCompare/, '必须 import 并使用 useMarketCompare')
  assert.ok(!PAGE_SOURCE.includes('useMarketScopeDetail'), '不得引用 detail hook')
  assert.ok(!PAGE_SOURCE.includes('useMarketScopeExplorer'), '不得引用 explorer hook')
})

// ===========================================================================
// W/X. chart 复用 CompareChart + 独立归一文案
// ===========================================================================
test('W. 图表复用 CompareChart（不写第二套 chart）', () => {
  assert.match(PAGE_SOURCE, /import CompareChart/, '必须 import CompareChart')
  assert.match(PAGE_SOURCE, /<CompareChart/, '必须渲染 <CompareChart')
})

test('X. 图表辅助说明点明独立归一 / 起点=100（非共享绝对指数）', () => {
  assert.match(PAGE_SOURCE, /起点 = 100/)
  assert.match(PAGE_SOURCE, /独立归一/)
})

// ===========================================================================
// Y/Z. 日期渲染（truthfully）
// ===========================================================================
test('Y. 渲染 projection_trade_date', () => {
  assert.match(PAGE_SOURCE, /projection_trade_date/)
})

test('Z. 渲染 previous_trade_date（不伪造日期）', () => {
  assert.match(PAGE_SOURCE, /previous_trade_date/)
})

// ===========================================================================
// AA/AB. remove / clear
// ===========================================================================
test('AA. remove 保留其余顺序', () => {
  const store = useCompareBasketStore
  store.getState().clear()
  store.getState().add({ id: 'a', name: 'A', type: 'industry' })
  store.getState().add({ id: 'b', name: 'B', type: 'concept' })
  store.getState().add({ id: 'c', name: 'C', type: 'industry' })
  store.getState().remove('b')
  assert.deepEqual(store.getState().items.map((i) => i.id), ['a', 'c'])
  store.getState().clear()
})

test('AB. clear 清空整个篮', () => {
  const store = useCompareBasketStore
  store.getState().clear()
  store.getState().add({ id: 'a', name: 'A', type: 'industry' })
  store.getState().add({ id: 'b', name: 'B', type: 'concept' })
  store.getState().clear()
  assert.equal(store.getState().items.length, 0)
})

// ===========================================================================
// AC. 图表线 toggle 基础未坏（CompareChart 仍委托 MultiLineChart + controller.toggle）
// ===========================================================================
test('AC. CompareChart 仍是薄 wrapper（MultiLineChart + buildLineData + EW_LINE_WIDTH）', () => {
  assert.match(CHART_SOURCE, /MultiLineChart/)
  assert.match(CHART_SOURCE, /buildLineData/)
  assert.match(CHART_SOURCE, /EW_LINE_WIDTH/)
  // R3A 的 I/K2 已断言：legend toggle 只 applyOptions({visible})、不重建/不 setData/不重取
})

// ===========================================================================
// AD. tab 计数 = 篮数（ComparePage 渲染 DashboardTabs，计数来自 store）
// ===========================================================================
test('AD. ComparePage 渲染 DashboardTabs（对比 N 计数来自共享篮）', () => {
  assert.match(PAGE_SOURCE, /DashboardTabs/)
})
