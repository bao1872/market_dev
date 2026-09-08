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
