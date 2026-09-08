// [FirstPyramidColumns] - 描述: Commit C / P0 列定义契约测试
// 用法：node --experimental-strip-types --test src/features/market-workspace/__tests__/firstPyramidColumnsContract.test.ts
//
// 覆盖用户裁决验收点：
//   13. fp_summary 不在 default visible keys（P0-5）
//   14. 行情/自选共用同一套 99 列定义（单一 ColumnRegistry，无第二套）
//   + 默认可见列的渲染已走语义中枢（结构事件完整语义 / 动量方向靠齐详情 / 结构对齐中文）
//   + 筛选器 enumOptions 接线（提交 canonical value，显示中文，验收 11 数据层）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import type { TrendSelectionRow } from '@/features/trend-selection/types'
import {
  DEFAULT_FP_VISIBLE_KEYS,
  getFirstPyramidColumns,
} from '../firstPyramidColumns.tsx'

function col(key: string) {
  const c = getFirstPyramidColumns().find((x) => x.key === key)
  if (!c) throw new Error(`column ${key} 缺失`)
  return c
}

const ROW = {
  firstPyramid: {
    fp_structure_event_type: 'BOS',
    fp_structure_event_direction: 'bullish',
    fp_structure_event_level: 'swing',
    fp_momentum_direction: '扩张',
    fp_structure_alignment: '共振',
  },
} as unknown as TrendSelectionRow

test('13. fp_summary 已从默认可见键移除（99 字段合同仍保留）', () => {
  assert.ok(
    !DEFAULT_FP_VISIBLE_KEYS.includes('fp_summary'),
    'fp_summary 不得出现在默认可见键',
  )
  assert.ok(DEFAULT_FP_VISIBLE_KEYS.includes('fp_momentum_direction'), '核心列仍默认可见')
})

test('14. 单一 ColumnRegistry：共 99 列且 momentum_direction 唯一', () => {
  const cols = getFirstPyramidColumns()
  assert.equal(cols.length, 99, '必须恰好 99 列')
  const md = cols.filter((c) => c.key === 'fp_momentum_direction')
  assert.equal(md.length, 1, 'fp_momentum_direction 必须唯一（行情/自选共用同一套定义）')
  // 两次调用返回同一套定义（数量一致，单一来源）
  assert.equal(getFirstPyramidColumns().length, 99)
})

test('默认可见「最新结构事件」列渲染完整语义（主要·多头突破）', () => {
  const out = col('fp_structure_event_type').render?.(ROW)
  assert.equal(out, '主要·多头突破')
})

test('默认可见「动量方向」列向详情 sqzmom 语义靠齐（扩张→偏多）', () => {
  const out = col('fp_momentum_direction').render?.(ROW)
  assert.equal(out, '偏多')
})

test('结构事件方向列显示 多头（非 bullish / 非 上行）', () => {
  const out = col('fp_structure_event_direction').render?.(ROW)
  assert.equal(out, '多头')
})

test('结构对齐列显示 长短结构同向（非 共振 裸字）', () => {
  const out = col('fp_structure_alignment').render?.(ROW)
  assert.equal(out, '长短结构同向')
})

test('11(数据层). 结构事件筛选器 enumOptions：提交 CHoCH，显示 结构转折', () => {
  const opts = col('fp_structure_event_type').enumOptions ?? []
  const choh = opts.find((o) => o.value === 'CHoCH')
  assert.ok(choh, 'CHoCH 必须出现在 enumOptions')
  assert.equal(choh?.label, '结构转折')
  // 提交值仍为 canonical
  assert.equal(choh?.value, 'CHoCH')
})

test('动量方向筛选器 enumOptions：扩张→偏多 / 收缩→偏空 / 平缓→中性', () => {
  const opts = col('fp_momentum_direction').enumOptions ?? []
  const map = Object.fromEntries(opts.map((o) => [o.value, o.label]))
  assert.equal(map['扩张'], '偏多')
  assert.equal(map['收缩'], '偏空')
  assert.equal(map['平缓'], '中性')
})

// ===== P0 corrective / Blocker 2：三个"事件方向"列必须走事件方向 formatter =====
test('动量事件方向列 bullish → 多头（不再 方向未知）', () => {
  const row = { firstPyramid: { fp_momentum_event_direction: 'bullish' } } as unknown as TrendSelectionRow
  assert.equal(col('fp_momentum_event_direction').render?.(row), '多头')
})

test('最新扩散方向列 bearish → 空头', () => {
  const row = { firstPyramid: { fp_latest_diffusion_direction: 'bearish' } } as unknown as TrendSelectionRow
  assert.equal(col('fp_latest_diffusion_direction').render?.(row), '空头')
})

test('成交密集区事件方向列 bullish → 多头', () => {
  const row = { firstPyramid: { fp_node_event_direction: 'bullish' } } as unknown as TrendSelectionRow
  assert.equal(col('fp_node_event_direction').render?.(row), '多头')
})

// ===== P0 corrective / 筛选器方向枚举补线：单元格与筛选器不得双轨 =====
// 后端 fp_momentum_event_direction / fp_latest_diffusion_direction / fp_node_event_direction
// 均为 enum（enum_values = bullish/bearish/up/down）。行内已中文化，筛选器必须同步。
const DIRECTION_ENUM_KEYS = [
  'fp_structure_event_direction',
  'fp_latest_bos_direction',
  'fp_latest_choch_direction',
  'fp_latest_ob_direction',
  'fp_momentum_event_direction',
  'fp_latest_diffusion_direction',
  'fp_node_event_direction',
] as const

test('动量事件方向筛选器 enumOptions：bullish→多头（提交仍为 canonical）', () => {
  const opts = col('fp_momentum_event_direction').enumOptions ?? []
  const hit = opts.find((o) => o.value === 'bullish')
  assert.ok(hit, 'bullish 必须出现在 enumOptions')
  assert.equal(hit?.label, '多头')
  assert.equal(hit?.value, 'bullish')
})

test('最新扩散方向筛选器 enumOptions：bearish→空头（提交仍为 canonical）', () => {
  const opts = col('fp_latest_diffusion_direction').enumOptions ?? []
  const hit = opts.find((o) => o.value === 'bearish')
  assert.ok(hit, 'bearish 必须出现在 enumOptions')
  assert.equal(hit?.label, '空头')
  assert.equal(hit?.value, 'bearish')
})

test('成交密集区事件方向筛选器 enumOptions：bullish→多头（提交仍为 canonical）', () => {
  const opts = col('fp_node_event_direction').enumOptions ?? []
  const hit = opts.find((o) => o.value === 'bullish')
  assert.ok(hit, 'bullish 必须出现在 enumOptions')
  assert.equal(hit?.label, '多头')
  assert.equal(hit?.value, 'bullish')
})

test('全部方向 enum 列：enumOptions 已接线，且不向用户暴露 up/down，label 不泄露 raw code', () => {
  for (const key of DIRECTION_ENUM_KEYS) {
    const opts = col(key).enumOptions
    assert.ok(opts && opts.length > 0, `列 ${key} 必须接线 enumOptions（否则筛选器回退 raw enum_values）`)
    assert.deepEqual(
      opts.map((o) => o.value),
      ['bullish', 'bearish'],
      `列 ${key} 提交值必须为 canonical bullish/bearish，且不得暴露 up/down`,
    )
    for (const o of opts) {
      for (const leak of ['bullish', 'bearish', 'up', 'down']) {
        assert.ok(!o.label.includes(leak), `列 ${key} 的 label 不得泄露 raw code ${leak}`)
      }
    }
  }
})

// ===== P0 corrective / Blocker 4：残留缩写已清除（列设置可见标题也中文化）=====
test('P0-6 残留缩写已清除：结构突破/转折/承接压制区/双顶双底/布林带/挤压动量', () => {
  assert.equal(col('fp_latest_bos_direction').title, '最新结构突破方向')
  assert.equal(col('fp_latest_choch_direction').title, '最新结构转折方向')
  assert.equal(col('fp_latest_ob_direction').title, '承接/压制区方向')
  assert.equal(col('fp_latest_eqh_price').title, '双顶压力价')
  assert.equal(col('fp_latest_eql_price').title, '双底支撑价')
  assert.equal(col('fp_bb_upper').title, '布林带上轨')
  assert.equal(col('fp_bb_middle').title, '布林带中轨')
  assert.equal(col('fp_bb_lower').title, '布林带下轨')
  assert.equal(col('fp_sqzmom_prev').title, '上一周期挤压动量值')
  assert.equal(col('fp_peak_node_count').title, '成交密集区数量')
})

// ===== P0 corrective / Blocker 4：列文案契约（单一 ColumnRegistry 全量扫描，非仓库扫描）=====
// 用户明确 token：BOS / CHoCH / OB / EQH / EQL / BB / SQZMOM 不得出现在任何用户可见标题/缩写/tooltip
test('列文案契约：99 列 title/shortTitle/helpText 不得出现 BOS/CHoCH/OB/EQH/EQL/BB/SQZMOM', () => {
  const banned = ['BOS', 'CHoCH', 'OB', 'EQH', 'EQL', 'BB', 'SQZMOM']
  const cols = getFirstPyramidColumns()
  assert.equal(cols.length, 99)
  for (const c of cols) {
    for (const field of [c.title, c.shortTitle ?? '', c.helpText ?? ''] as string[]) {
      for (const tok of banned) {
        assert.ok(
          !field.includes(tok),
          `列 ${c.key} 用户文案不得包含 ${tok}（实际片段：${field}）`,
        )
      }
    }
  }
})
